from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum

from .models import WireModel, WorldState


class AdviceKind(StrEnum):
    MAP_NOTE = "map_note"
    ANOMALY_EXPLANATION = "anomaly_explanation"
    STRATEGY_SUGGESTION = "strategy_suggestion"


class IntelligenceAdvice(WireModel):
    kind: AdviceKind
    text: str
    confidence: float


class IntelligenceProvider(ABC):
    """Advisory-only extension point; its output cannot contain input actions."""

    @abstractmethod
    async def advise(self, world: WorldState) -> IntelligenceAdvice | None:
        raise NotImplementedError


class NoopIntelligenceProvider(IntelligenceProvider):
    async def advise(self, world: WorldState) -> IntelligenceAdvice | None:
        return None
