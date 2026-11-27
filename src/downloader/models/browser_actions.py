"""Framework-free declarative browser action contracts and query-parameter parser."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

MAX_BROWSER_ACTIONS = 20

BrowserActionType = Literal[
    "click",
    "fill",
    "select",
    "press",
    "hover",
    "scroll",
    "wait_for_selector",
    "wait",
]


class BrowserActionError(Exception):
    """Raised when a declarative browser interaction action fails."""


class BrowserAction(BaseModel):
    """A single allowlisted Playwright page action executed prior to capture."""

    model_config = ConfigDict(extra="forbid")

    type: BrowserActionType = Field(..., description="Allowlisted browser action type")
    selector: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="CSS selector target (required for element actions)",
    )
    value: str | None = Field(
        default=None,
        max_length=2000,
        description="Input text or option value (required for fill/select)",
    )
    key: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Keyboard key name (required for press, e.g. 'Enter')",
    )
    x: int = Field(default=0, ge=-50_000, le=50_000, description="Horizontal scroll offset in px")
    y: int = Field(default=0, ge=-50_000, le=50_000, description="Vertical scroll offset in px")
    timeout_ms: int = Field(
        default=5000,
        ge=100,
        le=30_000,
        description="Per-action timeout in milliseconds",
    )
    duration_ms: int = Field(
        default=500,
        ge=0,
        le=10_000,
        description="Wait duration in milliseconds for 'wait' action",
    )

    @model_validator(mode="after")
    def validate_required_fields(self) -> BrowserAction:
        if self.type in {"click", "fill", "select", "hover", "wait_for_selector"}:
            if not self.selector:
                raise ValueError(f"Action '{self.type}' requires 'selector'")
        if self.type in {"fill", "select"} and self.value is None:
            raise ValueError(f"Action '{self.type}' requires 'value'")
        if self.type == "press" and not self.key:
            raise ValueError("Action 'press' requires 'key'")
        return self


def parse_actions_param(raw: str | None) -> list[BrowserAction]:
    """Parse and validate the JSON `?actions=` query parameter into BrowserAction items."""
    if not raw or not raw.strip():
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in actions parameter: {exc}") from exc

    if not isinstance(decoded, list):
        raise ValueError("actions parameter must be a JSON array of action objects")
    if len(decoded) > MAX_BROWSER_ACTIONS:
        raise ValueError(f"Too many browser actions: maximum is {MAX_BROWSER_ACTIONS}")

    actions: list[BrowserAction] = []
    for idx, item in enumerate(decoded):
        if not isinstance(item, dict):
            raise ValueError(f"Action at index {idx} must be a JSON object")
        try:
            actions.append(BrowserAction.model_validate(item))
        except ValidationError as exc:
            raise ValueError(f"Invalid browser action at index {idx}: {exc}") from exc
    return actions
