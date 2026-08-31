"""Agents.

Two agents, two tiers, one job each:

  triage  - cheap metadata-only relevance gate on the smallest eligible model.
            Most documents stop here.
  analyst - deep read of the actual filing on a larger model, given the docket's
            prior understanding so it can judge what changed.

Both return validated pydantic models via ADK's output_schema, so nothing
downstream parses free text.
"""

from agenda7.agents.analyst import analyst_agent, analyst_parts, build_analyst_agent
from agenda7.agents.runtime import StructuredAgent
from agenda7.agents.triage import build_triage_agent, triage_agent, triage_parts

__all__ = [
    "StructuredAgent",
    "analyst_agent",
    "analyst_parts",
    "build_analyst_agent",
    "build_triage_agent",
    "triage_agent",
    "triage_parts",
]
