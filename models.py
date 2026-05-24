"""Shared Pydantic models for the Agent6 agentic system."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

INLINE_BUDGET = 4096  # bytes — tool output exceeding this becomes an artifact


class MemoryItem(BaseModel):
    id: str
    kind: Literal["fact", "preference", "tool_outcome", "scratchpad"]
    keywords: list[str]
    descriptor: str
    value: dict
    artifact_id: str | None = None
    source: str
    run_id: str
    goal_id: str | None = None
    confidence: float = 1.0
    created_at: datetime


class Goal(BaseModel):
    id: str
    text: str
    done: bool = False
    attach_artifact_id: str | None = None


class Observation(BaseModel):
    goals: list[Goal]


class Observe(BaseModel):
    query: str
    memory_hits: list[MemoryItem]
    history: list[dict]
    prior_goals: list[Goal] | None = None


class ToolCallModel(BaseModel):
    name: str
    arguments: dict


class DecisionOutput(BaseModel):
    answer: str | None = None
    tool_call: ToolCallModel | None = None


class Artifact(BaseModel):
    id: str
    content_type: str
    size_bytes: int
    source: str
    descriptor: str


class MemoryItemDraft(BaseModel):
    kind: Literal["fact", "preference", "tool_outcome", "scratchpad"]
    keywords: list[str]
    descriptor: str
    value: dict


class MemoryExtraction(BaseModel):
    items: list[MemoryItemDraft]
