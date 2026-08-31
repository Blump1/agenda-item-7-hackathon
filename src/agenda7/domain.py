"""Domain models.

These are the contracts between layers: source adapters emit SourceDocument,
the triage agent emits TriageVerdict, the analyst agent emits DocumentAnalysis,
and the tracker folds analyses into a DocketPosition that mutates over time.

Source adapters normalize into these shapes so that adding a second feed
(FCC ECFS, a municipal portal) requires no changes downstream.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SourceKind(StrEnum):
    REGULATIONS_GOV = "regulations_gov"
    FCC_ECFS = "fcc_ecfs"
    MUNICIPAL = "municipal"


class Watch(BaseModel):
    """A standing instruction: what to look for, and why it matters to me.

    `keywords` is deliberately separate from `search_term`. The search term is
    what we hand the upstream API (coarse, cheap, server-side); the keywords and
    rationale are what we hand the triage model so it can judge relevance the
    way a human would.
    """

    watch_id: str
    label: str
    source: SourceKind = SourceKind.REGULATIONS_GOV
    search_term: str | None = None
    agency_ids: list[str] = Field(default_factory=list)
    docket_ids: list[str] = Field(default_factory=list)

    # Structural pre-filter -- e.g. ["Proposed Rule", "Rule"]. Distinct in kind
    # from topical relevance: documentType is reliable metadata describing what
    # KIND of document this is, whereas searchTerm tries to guess what it is
    # ABOUT and does so badly. Narrowing retrieval structurally is legitimate;
    # narrowing it topically is what the triage agent is for.
    #
    # Worth the distinction: FERC posts ~157 Notices a month but only ~30
    # rulemakings a year, and the rulemakings are what carry comment deadlines.
    document_types: list[str] = Field(default_factory=list)

    keywords: list[str] = Field(default_factory=list)
    rationale: str = ""
    active: bool = True


class SourceDocument(BaseModel):
    """One normalized document from any upstream feed."""

    source: SourceKind
    source_id: str
    title: str
    docket_id: str | None = None
    agency_id: str | None = None
    document_type: str | None = None
    posted_date: date | None = None
    comment_start_date: date | None = None
    comment_end_date: date | None = None
    open_for_comment: bool = False
    # Distinct from open_for_comment: a docket can accept late comments after
    # the window closes, so this is the field that answers "can I still file".
    within_comment_period: bool = False
    last_modified: datetime | None = None
    web_url: str | None = None
    file_urls: list[str] = Field(default_factory=list)

    # Detail-endpoint only. Absent until enrich_from_detail() runs, which is why
    # triage works off the title and the analyst works off these.
    abstract: str | None = None
    page_count: int | None = None
    topics: list[str] = Field(default_factory=list)

    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable Firestore document id. Used for dedup across runs."""
        return f"{self.source.value}__{self.source_id}".replace("/", "_")


class TriageVerdict(BaseModel):
    """Cheap Flash-model gate. Decides whether a deep read is worth paying for."""

    relevant: bool
    confidence: float = Field(ge=0.0, le=1.0)
    matched_topics: list[str] = Field(default_factory=list)
    rationale: str = ""


class Deadline(BaseModel):
    what: str
    due: date
    source_quote: str = ""


class Position(BaseModel):
    """Who is arguing what. Folded into DocketPosition as filings accumulate."""

    party: str
    stance: str
    summary: str


class DocumentAnalysis(BaseModel):
    """Deep read output from the analyst agent."""

    headline: str
    why_it_matters: str
    positions: list[Position] = Field(default_factory=list)
    deadlines: list[Deadline] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    changes_prior_understanding: bool = False
    change_note: str = ""


class DocketPosition(BaseModel):
    """The living tracker — this is the thing that visibly mutates on camera.

    Every analyzed filing folds into here, so the record is a running synthesis
    rather than a pile of independent summaries.
    """

    docket_id: str
    title: str = ""
    agency_id: str | None = None
    current_understanding: str = ""
    parties: list[Position] = Field(default_factory=list)
    open_deadlines: list[Deadline] = Field(default_factory=list)
    document_count: int = 0
    revision: int = 0
    updated_at: datetime | None = None
    history: list[str] = Field(default_factory=list)


class CommentDraft(BaseModel):
    """A public comment, drafted and staged for human review.

    status is deliberately not an enum with a "filed" member. Nothing in this
    system files anything; the value exists so a reviewer can mark their own
    progress, and the absence of a submission path is the point.
    """

    docket_id: str
    document_id: str
    document_title: str = ""
    subject: str
    body: str
    key_points: list[str] = Field(default_factory=list)
    # Where the draft needs a human: facts only the person has, decisions the
    # agent must not make for them. Surfacing these is more useful than
    # inventing plausible filler.
    needs_from_you: list[str] = Field(default_factory=list)
    filing_url: str | None = None
    comment_deadline: date | None = None
    status: str = "draft"
    created_at: datetime | None = None


class Checkpoint(BaseModel):
    """Per-watch high-water mark, so a re-run costs one API call, not a rescan."""

    watch_id: str
    last_posted_date: date | None = None
    seen_ids: list[str] = Field(default_factory=list)
    last_run_at: datetime | None = None
    last_run_status: str = "never"
