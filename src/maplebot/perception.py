from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

import cv2
import numpy as np

from .config import DetectorConfig, HSVRange, PerceptionConfig
from .models import Box, Detection, PerceptionResult, Point


class ObjectDetector(ABC):
    @abstractmethod
    def detect(self, frame: np.ndarray) -> list[Detection]:
        raise NotImplementedError


class NoopDetector(ObjectDetector):
    def detect(self, frame: np.ndarray) -> list[Detection]:
        return []


class TemplateDetector(ObjectDetector):
    """Fixed-resolution template detector used to bootstrap data collection."""

    def __init__(self, config: DetectorConfig):
        self._threshold = config.confidence_threshold
        self._nms_threshold = config.nms_threshold
        self._templates: list[tuple[str, np.ndarray]] = []
        allowed = set(config.class_names)
        directory = config.templates_dir
        if directory.exists():
            for path in sorted(directory.glob("*.png")):
                class_name = next(
                    (
                        name
                        for name in sorted(allowed, key=len, reverse=True)
                        if path.stem == name or path.stem.startswith(name + "_")
                    ),
                    None,
                )
                if class_name is None:
                    continue
                image = cv2.imread(str(path), cv2.IMREAD_COLOR)
                if image is not None and image.size:
                    self._templates.append((class_name, image))

    def detect(self, frame: np.ndarray) -> list[Detection]:
        detections: list[Detection] = []
        frame_h, frame_w = frame.shape[:2]
        for class_name, template in self._templates:
            template_h, template_w = template.shape[:2]
            if template_h > frame_h or template_w > frame_w:
                continue
            scores = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(scores >= self._threshold)
            candidates = [
                Detection(
                    class_name=class_name,
                    confidence=float(scores[y, x]),
                    box=Box(
                        x=float(x),
                        y=float(y),
                        width=float(template_w),
                        height=float(template_h),
                    ),
                )
                for y, x in zip(ys, xs, strict=True)
            ]
            detections.extend(_nms(candidates, self._nms_threshold))
        return detections


class OnnxDetector(ObjectDetector):
    """Small YOLO-style ONNX adapter supporting common Nx6 and v8 outputs."""

    def __init__(self, config: DetectorConfig):
        if config.model_path is None:
            raise ValueError("perception.detector.model_path is required for ONNX")
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "install the 'onnx' extra to use ONNX detection"
            ) from exc
        self._session = ort.InferenceSession(
            str(config.model_path), providers=["CPUExecutionProvider"]
        )
        self._input_name = self._session.get_inputs()[0].name
        self._width = config.input_width
        self._height = config.input_height
        self._classes = config.class_names
        self._confidence = config.confidence_threshold
        self._nms_threshold = config.nms_threshold

    def detect(self, frame: np.ndarray) -> list[Detection]:
        frame_h, frame_w = frame.shape[:2]
        resized = cv2.resize(
            frame, (self._width, self._height), interpolation=cv2.INTER_LINEAR
        )
        blob = cv2.dnn.blobFromImage(
            resized, 1 / 255.0, (self._width, self._height), swapRB=True
        )
        raw = np.asarray(self._session.run(None, {self._input_name: blob})[0]).squeeze()
        if raw.ndim != 2:
            return []
        if (
            raw.shape[0] in {4 + len(self._classes), 5 + len(self._classes)}
            and raw.shape[0] < raw.shape[1]
        ):
            raw = raw.T

        sx, sy = frame_w / self._width, frame_h / self._height
        found: list[Detection] = []
        for row in raw:
            parsed = self._parse_row(row, sx, sy)
            if parsed is not None:
                found.append(parsed)
        by_class: dict[str, list[Detection]] = {}
        for detection in found:
            by_class.setdefault(detection.class_name, []).append(detection)
        return [
            item
            for group in by_class.values()
            for item in _nms(group, self._nms_threshold)
        ]

    def _parse_row(self, row: np.ndarray, sx: float, sy: float) -> Detection | None:
        if len(row) == 6:  # exported NMS: x1, y1, x2, y2, score, class
            x1, y1, x2, y2, confidence, class_id = row
            box = Box(
                x=float(x1 * sx),
                y=float(y1 * sy),
                width=float((x2 - x1) * sx),
                height=float((y2 - y1) * sy),
            )
        else:  # raw YOLO: cx, cy, width, height, [objectness], class scores
            cx, cy, width, height = row[:4]
            scores = row[4:]
            if len(scores) == len(self._classes) + 1:
                scores = scores[1:] * scores[0]
            class_id = int(np.argmax(scores))
            confidence = float(scores[class_id])
            box = Box(
                x=float((cx - width / 2) * sx),
                y=float((cy - height / 2) * sy),
                width=float(width * sx),
                height=float(height * sy),
            )
        class_id = int(class_id)
        if (
            confidence < self._confidence
            or class_id < 0
            or class_id >= len(self._classes)
        ):
            return None
        class_name = self._classes[class_id]
        if class_name not in {"player", "monster", "death_dialog", "blocking_dialog"}:
            return None
        return Detection(class_name=class_name, confidence=float(confidence), box=box)


class Perception:
    def __init__(
        self, config: PerceptionConfig, detector: ObjectDetector | None = None
    ):
        self.config = config
        self.detector = detector or build_detector(config.detector)

    def analyze(
        self, session_id: str, frame_id: int, captured_at_ms: int, frame: np.ndarray
    ) -> PerceptionResult:
        if frame is None or frame.size == 0:
            raise ValueError("cannot analyze an empty frame")
        detections = self.detector.detect(frame)
        minimap = _crop_normalized(frame, self.config.rois.minimap)
        hp = _crop_normalized(frame, self.config.rois.hp)
        mp = _crop_normalized(frame, self.config.rois.mp)
        minimap_position = _color_centroid(
            minimap, self.config.minimap_player_hsv, self.config.min_color_pixels
        )
        hp_ratio = _bar_fill_ratio(hp, self.config.hp_hsv, self.config.min_color_pixels)
        mp_ratio = _bar_fill_ratio(mp, self.config.mp_hsv, self.config.min_color_pixels)
        dead = any(item.class_name == "death_dialog" for item in detections)
        blocked = any(item.class_name == "blocking_dialog" for item in detections)
        confidences = [item.confidence for item in detections]
        if minimap_position is not None:
            confidences.append(0.8)
        confidence = float(np.mean(confidences)) if confidences else 0.0
        return PerceptionResult(
            session_id=session_id,
            frame_id=frame_id,
            observed_at_ms=captured_at_ms,
            detections=detections,
            minimap_player_position=minimap_position,
            hp_ratio=hp_ratio,
            mp_ratio=mp_ratio,
            is_dead=dead,
            is_ui_blocked=blocked,
            confidence=confidence,
            diagnostics={"detector": type(self.detector).__name__},
        )


def build_detector(config: DetectorConfig) -> ObjectDetector:
    if config.backend == "none":
        return NoopDetector()
    if config.backend == "template":
        return TemplateDetector(config)
    return OnnxDetector(config)


def decode_jpeg(image_bytes: bytes) -> np.ndarray:
    array = np.frombuffer(image_bytes, dtype=np.uint8)
    frame = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("invalid JPEG frame")
    return frame


def _crop_normalized(
    frame: np.ndarray, roi: tuple[float, float, float, float]
) -> np.ndarray:
    height, width = frame.shape[:2]
    x, y, roi_width, roi_height = roi
    left, top = int(x * width), int(y * height)
    right, bottom = (
        max(left + 1, int((x + roi_width) * width)),
        max(top + 1, int((y + roi_height) * height)),
    )
    return frame[top : min(bottom, height), left : min(right, width)]


def _mask(image: np.ndarray, hsv_range: HSVRange) -> np.ndarray:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return cv2.inRange(
        hsv,
        np.array(hsv_range.lower, dtype=np.uint8),
        np.array(hsv_range.upper, dtype=np.uint8),
    )


def _color_centroid(
    image: np.ndarray, hsv_range: HSVRange, min_pixels: int
) -> Point | None:
    if image.size == 0:
        return None
    mask = _mask(image, hsv_range)
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask)
    if count <= 1:
        return None
    largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    if int(stats[largest, cv2.CC_STAT_AREA]) < min_pixels:
        return None
    x, y = centroids[largest]
    return Point(x=float(x), y=float(y))


def _bar_fill_ratio(
    image: np.ndarray, hsv_range: HSVRange, min_pixels: int
) -> float | None:
    if image.size == 0:
        return None
    mask = _mask(image, hsv_range)
    if int(np.count_nonzero(mask)) < min_pixels:
        return None
    active_columns = np.count_nonzero(mask, axis=0) >= max(1, image.shape[0] // 5)
    indices = np.flatnonzero(active_columns)
    if len(indices) == 0:
        return None
    return float(np.clip((indices.max() + 1) / image.shape[1], 0, 1))


def _nms(detections: Iterable[Detection], threshold: float) -> list[Detection]:
    ordered = sorted(detections, key=lambda item: item.confidence, reverse=True)
    kept: list[Detection] = []
    for candidate in ordered:
        if all(_iou(candidate.box, item.box) < threshold for item in kept):
            kept.append(candidate)
    return kept


def _iou(first: Box, second: Box) -> float:
    left = max(first.x, second.x)
    top = max(first.y, second.y)
    right = min(first.x + first.width, second.x + second.width)
    bottom = min(first.y + first.height, second.y + second.height)
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    union = first.width * first.height + second.width * second.height - intersection
    return intersection / union if union else 0.0
