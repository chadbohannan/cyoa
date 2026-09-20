"""Pydantic shapes shared by the API. Mirrors the tables in db.py."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class StoryCreate(BaseModel):
    title: str
    description: str = ""


class Story(StoryCreate):
    id: int
    created_at: str


class PassageCreate(BaseModel):
    title: str
    body: str = ""
    is_start: bool = False
    pos_x: float = 0
    pos_y: float = 0


class Passage(PassageCreate):
    id: int
    story_id: int


class ChoiceCreate(BaseModel):
    from_passage: int
    to_passage: int | None = None
    text: str
    sort_order: int = 0
    condition: dict[str, Any] = Field(default_factory=dict)
    effects: dict[str, Any] = Field(default_factory=dict)


class Choice(ChoiceCreate):
    id: int
    story_id: int


class Variable(BaseModel):
    id: int
    story_id: int
    name: str
    kind: str
    default: Any = None


class StoryGraph(BaseModel):
    """Everything the graph editor needs to render one story."""

    story: Story
    passages: list[Passage]
    choices: list[Choice]
    variables: list[Variable]
