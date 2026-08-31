"""Ground-truth probe for ADK 2.x structured output against Vertex AI.

Makes exactly two small Gemini calls (one per configured model) to establish:
  1. Vertex auth works through ADK, not just through google-genai directly
  2. output_schema with a pydantic class actually returns parseable JSON
  3. how to reliably extract the final payload from the Event stream

Costs a fraction of a cent at flash-lite prices. Run before building agents.

    python scripts/probe_adk.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from google.adk.agents import LlmAgent  # noqa: E402
from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from agenda7.config import configure_genai_env, settings  # noqa: E402
from agenda7.domain import TriageVerdict  # noqa: E402

SAMPLE_TITLE = (
    "Empowering Broadband Consumers through Transparency: Notice of Proposed "
    "Rulemaking regarding broadband consumer labels and pole attachment timelines"
)


def final_text(events: list) -> str | None:
    """Pull the last text payload out of an ADK event stream.

    Walks backwards because the final response is the last event carrying
    content, and intermediate events may carry partial or empty parts.
    """
    for event in reversed(events):
        content = getattr(event, "content", None)
        if not content:
            continue
        for part in reversed(getattr(content, "parts", None) or []):
            text = getattr(part, "text", None)
            if text and text.strip():
                return text.strip()
    return None


async def probe(model: str, label: str) -> bool:
    print(f"\n--- {label}: {model} ---")
    agent = LlmAgent(
        name="triage_probe",
        model=model,
        instruction=(
            "You judge whether a government document is relevant to a person's "
            "stated interests. Reply only with the structured verdict."
        ),
        output_schema=TriageVerdict,
    )
    runner = InMemoryRunner(agent=agent, app_name="agenda7-probe")

    message = types.Content(
        role="user",
        parts=[
            types.Part(
                text=(
                    "Interests: broadband deployment, pole attachments, "
                    "utility make-ready timelines.\n"
                    f"Document title: {SAMPLE_TITLE}\n"
                    "Is this relevant?"
                )
            )
        ],
    )

    try:
        # run_async does not create sessions; Runner.auto_create_session defaults
        # to False and InMemoryRunner does not expose it, so create it explicitly.
        await runner.session_service.create_session(
            app_name="agenda7-probe",
            user_id="probe_user",
            session_id="probe_session",
        )
        events = []
        async for event in runner.run_async(
            user_id="probe_user",
            session_id="probe_session",
            new_message=message,
        ):
            events.append(event)
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] run_async raised {type(exc).__name__}: {exc}")
        return False
    finally:
        await runner.close()

    print(f"  events: {len(events)}")
    raw = final_text(events)
    if not raw:
        print("  [FAIL] no text payload found in event stream")
        return False

    print(f"  raw: {raw[:200]}")
    try:
        verdict = TriageVerdict.model_validate(json.loads(raw))
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] not parseable into TriageVerdict: {type(exc).__name__}: {exc}")
        return False

    print(
        f"  [PASS] relevant={verdict.relevant} confidence={verdict.confidence} "
        f"topics={verdict.matched_topics}"
    )
    print(f"         rationale: {verdict.rationale[:140]}")
    return True


async def main() -> int:
    configure_genai_env()
    cfg = settings()
    print(
        f"project={cfg.gcp_project} vertex_location={cfg.vertex_location} "
        f"vertex={cfg.use_vertex}"
    )

    results = {
        "triage model": await probe(cfg.triage_model, "triage model"),
        "analyst model": await probe(cfg.analyst_model, "analyst model"),
    }

    print("\n=== summary ===")
    for label, ok in results.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
