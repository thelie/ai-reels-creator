"""Машина состояний проекта."""

from __future__ import annotations

from enum import StrEnum


class State(StrEnum):
    COLLECTING = "collecting"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    RENDERING = "rendering"
    PREVIEW_SENT = "preview_sent"
    DELIVERED = "delivered"
    FAILED = "failed"


# Разрешённые переходы
TRANSITIONS: dict[State, set[State]] = {
    State.COLLECTING: {State.ANALYZING},
    State.ANALYZING: {State.PLANNING, State.FAILED, State.COLLECTING},
    State.PLANNING: {State.AWAITING_APPROVAL, State.RENDERING, State.FAILED},
    State.AWAITING_APPROVAL: {State.PLANNING, State.RENDERING, State.COLLECTING},
    State.RENDERING: {State.PREVIEW_SENT, State.DELIVERED, State.FAILED},
    State.PREVIEW_SENT: {State.PLANNING, State.RENDERING, State.COLLECTING},
    State.DELIVERED: {State.PLANNING, State.RENDERING, State.COLLECTING},
    State.FAILED: {State.COLLECTING, State.ANALYZING, State.PLANNING, State.RENDERING},
}

BUSY = {State.ANALYZING, State.PLANNING, State.RENDERING}


class InvalidTransition(RuntimeError):
    pass


def check(current: str, new: State) -> None:
    cur = State(current)
    if new == cur:
        return
    if new not in TRANSITIONS[cur]:
        raise InvalidTransition(f"{cur} -> {new}")
