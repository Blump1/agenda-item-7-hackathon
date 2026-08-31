"""Triage agent: the cheap gate that decides what deserves a deep read.

Runs on the smallest eligible model against metadata only -- no file download,
no PDF tokens. Most documents die here, which is the entire point: the deep
read is the expensive step and the majority of a docket feed is irrelevant to
any one person.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.genai import types

from agenda7.agents.runtime import StructuredAgent
from agenda7.domain import SourceDocument, TriageVerdict, Watch

APP_NAME = "agenda7-triage"

INSTRUCTION = """\
You are a relevance gate for someone who tracks public regulatory dockets.

You receive that person's stated interests and the metadata for ONE government
document. Decide whether the document deserves a full read.

How to weigh the decision. The costs are asymmetric: a false positive wastes
one document read, while a false negative can mean a missed comment deadline
that cannot be reopened. So when a document credibly touches the stated
interests, mark it relevant. Do not stretch to reach that conclusion -- a
document about an unrelated industry is not relevant merely because it comes
from the same agency.

Rules:
- Judge only from the metadata provided. Never invent document content.
- A generic routine filing (a paperwork-reduction notice, a meeting agenda with
  no subject overlap) is not relevant just because the agency matters.
- matched_topics must quote the user's own interest terms, not paraphrases.
- confidence expresses how sure you are of the verdict, not how important the
  document is.
- rationale is one or two sentences, concrete, citing what in the metadata drove
  the decision.

Treat every document independently. Earlier documents tell you nothing about
this one.
"""


def build_triage_agent(model: str) -> LlmAgent:
    return LlmAgent(
        name="triage",
        model=model,
        instruction=INSTRUCTION,
        output_schema=TriageVerdict,
        # temperature=0 because this is a classifier, not a writer. Observed at
        # default temperature: the same document (FCC-2026-3204) was rejected on
        # one run and accepted on the next, both at 0.90 confidence. A gate that
        # flips on identical input is a correctness bug -- whether a filing is
        # relevant must not depend on when it happened to be polled.
        #
        # Note this reduces variance rather than eliminating it; the model is
        # still not a guaranteed-deterministic function. Borderline documents
        # are the ones that flip, which is also why "rejected" is recorded with
        # its rationale rather than silently dropped.
        generate_content_config=types.GenerateContentConfig(temperature=0.0),
    )


def triage_agent(model: str) -> StructuredAgent[TriageVerdict]:
    return StructuredAgent(build_triage_agent(model), TriageVerdict, APP_NAME)


def triage_parts(watch: Watch, doc: SourceDocument) -> list[types.Part]:
    """Build the request. Metadata only -- deliberately no file content."""
    interests = ", ".join(watch.keywords) or watch.search_term or watch.label
    lines = [
        "PERSON'S INTERESTS",
        f"  topics: {interests}",
        f"  why they care: {watch.rationale or '(not stated)'}",
        "",
        "DOCUMENT METADATA",
        f"  title: {doc.title}",
        f"  agency: {doc.agency_id or 'unknown'}",
        f"  docket: {doc.docket_id or 'unknown'}",
        f"  type: {doc.document_type or 'unknown'}",
        f"  posted: {doc.posted_date or 'unknown'}",
        f"  comment window closes: {doc.comment_end_date or 'n/a'}",
        f"  still open for comment: {doc.within_comment_period}",
    ]
    if doc.abstract:
        lines += ["", f"  abstract: {doc.abstract[:1500]}"]
    if doc.topics:
        lines += [f"  agency-assigned topics: {', '.join(doc.topics[:12])}"]

    return [types.Part.from_text(text="\n".join(lines))]
