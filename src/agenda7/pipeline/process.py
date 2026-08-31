"""Discovery and per-document processing.

Two entry points, deliberately:

  process_document - everything that happens to ONE document. The unit of retry.
  process_watch    - discovery plus a loop over process_document. The unit of
                     scheduling.

The split exists so the same code serves both shapes of deployment. Running
locally, process_watch loops in one process. On Cloud Run, the poller discovers
and publishes and a Pub/Sub handler calls process_document once per message, so
a slow deep read cannot block discovery and a poison document retries alone
instead of taking a whole run down with it.

Ordering inside process_document is a cost decision. Metadata triage runs before
anything is downloaded; the detail request, the PDF fetch, the deep read, and the
draft only happen for documents that survived the gate.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from agenda7.actions.drafter import drafter_parts
from agenda7.agents.analyst import analyst_parts
from agenda7.agents.runtime import StructuredAgent
from agenda7.agents.triage import triage_parts
from agenda7.config import Settings
from agenda7.domain import (
    CommentDraft,
    DocketPosition,
    DocumentAnalysis,
    SourceDocument,
    TriageVerdict,
    Watch,
)
from agenda7.sources.fetch import stage_document
from agenda7.sources.regulations_gov import RegulationsGovClient
from agenda7.store import Store

log = logging.getLogger(__name__)


# Words that mark a deadline as an invitation to file something, as opposed to
# an effective date or a compliance milestone, which are not draftable.
_COMMENT_WORDS = ("comment", "reply", "protest", "intervention", "response")


def comment_window_open(
    doc: SourceDocument, analysis: DocumentAnalysis, today: date | None = None
) -> tuple[bool, str]:
    """Decide whether a comment could still be filed on this document.

    The API's withinCommentPeriod is authoritative when it says yes, but it is
    frequently absent on correction and supplemental notices even when the
    underlying proceeding is very much open. Observed on FERC-2026-3934:
    withinCommentPeriod False and commentEndDate None, while the document
    itself set reply comments for 2026-09-22, four weeks out. Trusting metadata
    alone silently drops exactly the filings a person most wants to act on.

    So the document's own extracted deadlines count as evidence too. The
    drafting agent is separately instructed to say so in the body if the filing
    turns out not to be open after all, which bounds the cost of a false
    positive to one wasted draft rather than a misleading one.
    """
    today = today or date.today()
    if doc.within_comment_period:
        return True, "API reports the comment window open"
    if doc.comment_end_date and doc.comment_end_date >= today:
        return True, f"API comment window closes {doc.comment_end_date}"
    for deadline in analysis.deadlines:
        if deadline.due < today:
            continue
        if any(word in deadline.what.lower() for word in _COMMENT_WORDS):
            return True, f"document states a filing deadline of {deadline.due}"
    return False, "no open comment window in metadata or document"


@dataclass
class DocOutcome:
    source_id: str
    title: str
    stage: str
    relevant: bool | None = None
    confidence: float | None = None
    note: str = ""
    docket_id: str | None = None
    revision: int | None = None


@dataclass
class DocResult:
    """What happened to one document, in a form the caller can aggregate."""

    outcome: DocOutcome
    position: DocketPosition | None = None
    draft: CommentDraft | None = None
    rejection: tuple[str, str] | None = None
    triaged: bool = False
    relevant: bool = False
    analyzed: bool = False
    drafted: bool = False
    failed: bool = False


@dataclass
class RunStats:
    run_id: str
    watch_id: str
    discovered: int = 0
    already_seen: int = 0
    triaged: int = 0
    relevant: int = 0
    analyzed: int = 0
    drafted: int = 0
    failed: int = 0
    outcomes: list[DocOutcome] = field(default_factory=list)
    positions: list[DocketPosition] = field(default_factory=list)
    drafts: list[CommentDraft] = field(default_factory=list)
    # (title, rationale) for rejected documents. Kept so the digest can show its
    # work; a filter nobody can inspect is a filter nobody trusts.
    rejected: list[tuple[str, str]] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.failed and not self.analyzed:
            return "failed"
        if self.failed:
            return "partial"
        return "ok"

    def absorb(self, result: DocResult) -> None:
        self.outcomes.append(result.outcome)
        self.triaged += int(result.triaged)
        self.relevant += int(result.relevant)
        self.analyzed += int(result.analyzed)
        self.drafted += int(result.drafted)
        self.failed += int(result.failed)
        if result.position is not None:
            self.positions.append(result.position)
        if result.draft is not None:
            self.drafts.append(result.draft)
        if result.rejection is not None:
            self.rejected.append(result.rejection)


async def process_document(
    doc: SourceDocument,
    watch: Watch,
    *,
    store: Store,
    client: RegulationsGovClient,
    triage: StructuredAgent[TriageVerdict],
    analyst: StructuredAgent[DocumentAnalysis],
    cfg: Settings,
    drafter: StructuredAgent[CommentDraft] | None = None,
    analyze: bool = True,
) -> DocResult:
    """Take one document as far as it should go. Never raises."""
    store.save_document(doc, stage="discovered")

    # ---- triage: metadata only, nothing downloaded ----
    verdict = await triage.run(triage_parts(watch, doc), label=f"triage-{doc.source_id}")
    if verdict is None:
        return DocResult(
            outcome=DocOutcome(doc.source_id, doc.title, "triage_failed"), failed=True
        )

    if not verdict.relevant:
        # "rejected" is terminal; this document will not be reconsidered.
        store.save_document(doc, triage=verdict, stage="rejected")
        return DocResult(
            outcome=DocOutcome(
                doc.source_id, doc.title, "rejected",
                relevant=False, confidence=verdict.confidence, note=verdict.rationale,
            ),
            triaged=True,
            rejection=(doc.title, verdict.rationale),
        )

    # Non-terminal on purpose: if anything below fails, the next run retries.
    store.save_document(doc, triage=verdict, stage="triaged")

    if not analyze:
        return DocResult(
            outcome=DocOutcome(
                doc.source_id, doc.title, "passed_triage",
                relevant=True, confidence=verdict.confidence, note=verdict.rationale,
            ),
            triaged=True,
            relevant=True,
        )

    # ---- enrich + stage: survivors only ----
    try:
        enriched = client.enrich_from_detail(doc)
    except Exception as exc:  # noqa: BLE001
        log.warning("%s: detail enrich failed, continuing: %s", doc.source_id, exc)
        enriched = doc

    staged = stage_document(enriched, project=cfg.gcp_project, bucket=cfg.gcs_bucket)
    if staged is None or not staged.is_staged:
        return DocResult(
            outcome=DocOutcome(
                enriched.source_id, enriched.title, "staging_failed", relevant=True
            ),
            triaged=True, relevant=True, failed=True,
        )

    # ---- deep read, against the docket's prior understanding ----
    prior = store.get_docket(enriched.docket_id) if enriched.docket_id else None
    analysis = await analyst.run(
        analyst_parts(watch, enriched, staged, prior),
        label=f"analyst-{enriched.source_id}",
    )
    if analysis is None:
        return DocResult(
            outcome=DocOutcome(
                enriched.source_id, enriched.title, "analysis_failed", relevant=True
            ),
            triaged=True, relevant=True, failed=True,
        )

    store.save_document(enriched, triage=verdict, analysis=analysis, stage="analyzed")
    position = store.fold_analysis(enriched, analysis)

    result = DocResult(
        outcome=DocOutcome(
            enriched.source_id, enriched.title, "analyzed",
            relevant=True, confidence=verdict.confidence, note=analysis.headline,
            docket_id=position.docket_id, revision=position.revision,
        ),
        position=position,
        triaged=True, relevant=True, analyzed=True,
    )

    # ---- action: stage a comment draft where one could still be filed ----
    draftable, why = comment_window_open(enriched, analysis)
    if drafter is not None:
        log.info(
            "%s: draft %s (%s)",
            enriched.source_id, "yes" if draftable else "skipped", why,
        )
    if drafter is not None and draftable:
        draft = await drafter.run(
            drafter_parts(watch, enriched, analysis, position),
            label=f"draft-{enriched.source_id}",
        )
        if draft is None:
            log.warning("%s: draft generation failed (analysis kept)", enriched.source_id)
        else:
            # Trust our own metadata over the model's for the hard facts.
            draft.docket_id = position.docket_id
            draft.document_id = enriched.source_id
            draft.document_title = enriched.title
            draft.filing_url = enriched.web_url
            # The API's commentEndDate is the OUTER bound of the window. A
            # rulemaking usually stages deadlines inside it -- initial comments
            # well before reply comments -- and those appear in the filing, not
            # the metadata. Observed on FCC-2026-3072: the API reported
            # 2026-11-06 while the document set initial comments at 2026-09-21.
            # Reporting the outer bound would understate urgency by six weeks.
            future = sorted(d.due for d in analysis.deadlines if d.due >= date.today())
            draft.comment_deadline = future[0] if future else enriched.comment_end_date
            draft.status = "draft"
            store.save_draft(draft)
            result.draft = draft
            result.drafted = True

    return result


async def process_watch(
    watch: Watch,
    *,
    store: Store,
    client: RegulationsGovClient,
    triage: StructuredAgent[TriageVerdict],
    analyst: StructuredAgent[DocumentAnalysis],
    cfg: Settings,
    drafter: StructuredAgent[CommentDraft] | None = None,
    max_docs: int | None = None,
    analyze: bool = True,
    since_override: date | None = None,
) -> RunStats:
    """Discover work for one watch and run each document through the pipeline.

    since_override ignores the checkpoint and scans from an explicit date. Used
    for backfills and for the first run on a narrow watch, where the default
    lookback window can legitimately contain nothing.
    """
    run_id = f"{watch.watch_id}-{uuid.uuid4().hex[:8]}"
    stats = RunStats(run_id=run_id, watch_id=watch.watch_id)
    limit = max_docs or cfg.max_docs_per_run

    checkpoint = store.get_checkpoint(watch.watch_id)
    since = (
        since_override
        or checkpoint.last_posted_date
        or (date.today() - timedelta(days=cfg.initial_lookback_days))
    )
    log.info("run %s: watch=%s since=%s limit=%d", run_id, watch.watch_id, since, limit)

    try:
        candidates = list(
            client.documents_for_watch(
                watch, since=since, lookback_days=cfg.initial_lookback_days, max_docs=limit
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.error("run %s: discovery failed: %s: %s", run_id, type(exc).__name__, exc)
        store.log_run(
            run_id, watch_id=watch.watch_id, discovered=0, triaged_relevant=0,
            analyzed=0, status="failed", error=f"{type(exc).__name__}: {exc}",
        )
        stats.failed += 1
        return stats

    stats.discovered = len(candidates)
    # Not plain dedup: also returns documents stuck mid-pipeline on an earlier
    # run, so they retry rather than being lost.
    fresh = store.documents_needing_work(candidates)
    stats.already_seen = len(candidates) - len(fresh)
    log.info("run %s: %d candidates, %d need work", run_id, stats.discovered, len(fresh))

    newest_posted = checkpoint.last_posted_date
    for doc in fresh:
        if doc.posted_date and (newest_posted is None or doc.posted_date > newest_posted):
            newest_posted = doc.posted_date
        checkpoint.seen_ids.append(doc.key)
        stats.absorb(
            await process_document(
                doc, watch,
                store=store, client=client, triage=triage, analyst=analyst,
                cfg=cfg, drafter=drafter, analyze=analyze,
            )
        )

    checkpoint.last_posted_date = newest_posted
    checkpoint.last_run_status = stats.status
    store.save_checkpoint(checkpoint)

    store.log_run(
        run_id,
        watch_id=watch.watch_id,
        discovered=stats.discovered,
        triaged_relevant=stats.relevant,
        analyzed=stats.analyzed,
        status=stats.status,
    )
    log.info(
        "run %s complete: discovered=%d new=%d relevant=%d analyzed=%d drafted=%d failed=%d",
        run_id, stats.discovered, len(fresh), stats.relevant, stats.analyzed,
        stats.drafted, stats.failed,
    )
    return stats
