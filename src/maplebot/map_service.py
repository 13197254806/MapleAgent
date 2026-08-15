from __future__ import annotations

import json
import math
from collections import deque
from pathlib import Path

from .models import EdgeAction, MapEdge, MapModel, MapNode, Point


class MapService:
    def __init__(self, model: MapModel, snap_distance: float = 16):
        self.model = model
        self.snap_distance = snap_distance
        self.nodes = {node.id: node for node in model.nodes}
        unknown = {
            node_id
            for edge in model.edges
            for node_id in (edge.source, edge.target)
            if node_id not in self.nodes
        }
        if unknown:
            raise ValueError(f"map edges reference unknown nodes: {sorted(unknown)}")
        if any(node_id not in self.nodes for node_id in model.patrol_route):
            raise ValueError("patrol_route contains an unknown node")

    @classmethod
    def load(cls, path: str | Path, snap_distance: float = 16) -> MapService:
        with Path(path).open("r", encoding="utf-8") as handle:
            return cls(MapModel.model_validate(json.load(handle)), snap_distance)

    def nearest_node(self, position: Point) -> str | None:
        if not self.nodes:
            return None
        node = min(
            self.nodes.values(),
            key=lambda item: math.hypot(item.x - position.x, item.y - position.y),
        )
        distance = math.hypot(node.x - position.x, node.y - position.y)
        return node.id if distance <= max(self.snap_distance, node.radius) else None

    def next_edge(self, source: str, target: str) -> MapEdge | None:
        if source == target:
            return None
        queue: deque[str] = deque([source])
        previous: dict[str, tuple[str, MapEdge] | None] = {source: None}
        while queue:
            current = queue.popleft()
            for edge in self._outgoing(current):
                if edge.target in previous:
                    continue
                previous[edge.target] = (current, edge)
                if edge.target == target:
                    cursor = target
                    while previous[cursor] and previous[cursor][0] != source:
                        cursor = previous[cursor][0]  # type: ignore[index]
                    first_step = previous[cursor]
                    return first_step[1] if first_step else None
                queue.append(edge.target)
        return None

    def _outgoing(self, source: str) -> list[MapEdge]:
        outgoing: list[MapEdge] = []
        for edge in self.model.edges:
            if edge.source == source:
                outgoing.append(edge)
            if edge.bidirectional and edge.target == source:
                outgoing.append(
                    MapEdge(
                        source=source,
                        target=edge.source,
                        action=_reverse_action(edge.action),
                        bidirectional=True,
                    )
                )
        return outgoing


def _reverse_action(action: EdgeAction) -> EdgeAction:
    if action == EdgeAction.WALK_LEFT:
        return EdgeAction.WALK_RIGHT
    if action == EdgeAction.WALK_RIGHT:
        return EdgeAction.WALK_LEFT
    return action


class MappingTrace:
    """Produces coarse candidate nodes/edges from the operator's minimap trace."""

    def __init__(
        self, name: str, minimap_width: int, minimap_height: int, node_distance: float
    ):
        self.name = name
        self.minimap_width = minimap_width
        self.minimap_height = minimap_height
        self.node_distance = node_distance
        self._nodes: list[MapNode] = []
        self._transitions: set[tuple[str, str]] = set()
        self._last_node: str | None = None

    def add(self, point: Point | None) -> None:
        if point is None:
            return
        node = self._closest(point)
        if node is None:
            node = MapNode(
                id=f"candidate_{len(self._nodes):03d}",
                x=point.x,
                y=point.y,
                radius=self.node_distance,
            )
            self._nodes.append(node)
        else:
            # Slowly refine the center without retaining every frame.
            node.x = node.x * 0.9 + point.x * 0.1
            node.y = node.y * 0.9 + point.y * 0.1
        if self._last_node and self._last_node != node.id:
            self._transitions.add((self._last_node, node.id))
        self._last_node = node.id

    def model(self) -> MapModel:
        edges: list[MapEdge] = []
        for source, target in sorted(self._transitions):
            source_node, target_node = self._node(source), self._node(target)
            action = (
                EdgeAction.WALK_RIGHT
                if target_node.x >= source_node.x
                else EdgeAction.WALK_LEFT
            )
            edges.append(
                MapEdge(
                    source=source, target=target, action=action, bidirectional=False
                )
            )
        return MapModel(
            name=f"{self.name}_candidate",
            minimap_width=self.minimap_width,
            minimap_height=self.minimap_height,
            nodes=self._nodes,
            edges=edges,
            patrol_route=[node.id for node in self._nodes],
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.model().model_dump_json(indent=2), encoding="utf-8")

    def _closest(self, point: Point) -> MapNode | None:
        close = [
            node
            for node in self._nodes
            if math.hypot(node.x - point.x, node.y - point.y) <= self.node_distance
        ]
        return (
            min(close, key=lambda node: math.hypot(node.x - point.x, node.y - point.y))
            if close
            else None
        )

    def _node(self, node_id: str) -> MapNode:
        return next(node for node in self._nodes if node.id == node_id)
