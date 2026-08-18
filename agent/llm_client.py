"""Thin wrapper around the Anthropic Messages API for the discovery loop."""
from __future__ import annotations

import os

import anthropic

DEFAULT_MODEL = "claude-sonnet-5"


class LLMClient:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.client = anthropic.Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])
        self.model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    def step(self, system: str, messages: list[dict], tools: list[dict]) -> anthropic.types.Message:
        return self.client.messages.create(
            model=self.model,
            max_tokens=1500,
            system=system,
            messages=messages,
            tools=tools,
        )
