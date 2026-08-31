"""Thin execution wrapper around ADK agents that return structured output.

Why this exists: ADK's Runner is a streaming, session-oriented API built for
conversation, but triage and analysis are one-shot classification calls. This
adapts one to the other in a single place, so the agent modules stay
declarative and the pipeline never touches session plumbing.

Contract pinned by scripts/probe_adk.py against ADK 2.7.1:
  * output_schema accepts a pydantic model class directly
  * Runner.auto_create_session defaults to False, so the session must be
    created before run_async or it raises SessionNotFoundError
  * the structured payload arrives as text on the final event's content parts
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Generic, Sequence, TypeVar

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

USER_ID = "agenda7"


def final_text(events: Sequence[object]) -> str | None:
    """Extract the last non-empty text payload from an ADK event stream.

    Walks backwards: the final response is the last event carrying content, and
    intermediate events may carry empty or partial parts.
    """
    for event in reversed(list(events)):
        content = getattr(event, "content", None)
        if not content:
            continue
        for part in reversed(getattr(content, "parts", None) or []):
            text = getattr(part, "text", None)
            if text and text.strip():
                return text.strip()
    return None


class StructuredAgent(Generic[T]):
    """Runs one ADK agent repeatedly, parsing each result into a pydantic model.

    One runner is reused across calls; each call gets a fresh session so that
    documents never leak context into each other. That isolation is deliberate:
    triage decisions must be independent, or a run of relevant documents starts
    dragging later ones across the line.
    """

    def __init__(self, agent: LlmAgent, output_model: type[T], app_name: str) -> None:
        self.agent = agent
        self.output_model = output_model
        self.app_name = app_name
        self._runner = InMemoryRunner(agent=agent, app_name=app_name)

    async def run(self, parts: list[types.Part], *, label: str = "") -> T | None:
        """Execute once. Returns None on any failure, having logged the cause.

        Returning None rather than raising is intentional: the caller is a
        batch pipeline where one unparseable document must not abort the run.
        Pub/Sub redelivery handles genuine retries at a higher level.
        """
        session_id = f"{label or 'run'}-{uuid.uuid4().hex[:8]}"
        try:
            await self._runner.session_service.create_session(
                app_name=self.app_name, user_id=USER_ID, session_id=session_id
            )
            events = []
            async for event in self._runner.run_async(
                user_id=USER_ID,
                session_id=session_id,
                new_message=types.Content(role="user", parts=parts),
            ):
                events.append(event)
        except Exception as exc:  # noqa: BLE001
            log.error("%s: agent run failed: %s: %s", label, type(exc).__name__, exc)
            return None

        raw = final_text(events)
        if not raw:
            log.error("%s: agent returned no text payload (%d events)", label, len(events))
            return None

        try:
            return self.output_model.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as exc:
            log.error("%s: unparseable output: %s | raw=%.300s", label, exc, raw)
            return None

    async def close(self) -> None:
        await self._runner.close()

    async def __aenter__(self) -> "StructuredAgent[T]":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
