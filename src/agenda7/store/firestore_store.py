"""Firestore-backed state.

Design notes worth knowing before changing anything here:

* Pydantic models are persisted via ``model_dump(mode="json")``. That turns
  ``date`` into an ISO string, which Firestore stores happily and which round
  trips through ``model_validate`` without a custom codec. Trying to persist raw
  ``date`` objects fails, because Firestore has ``datetime`` but no ``date``.

* ``fold_analysis`` is a real Firestore transaction. The docket tracker is the
  one document in the system that multiple concurrent workers will contend for
  (two filings in the same docket analyzed in parallel), so read-modify-write
  has to be atomic or a revision gets silently lost.

* ``seen_ids`` on a checkpoint is bounded. Dedup only needs to cover the overlap
  window created by a same-day re-poll, not all history, and an unbounded array
  eventually blows the 1 MiB document ceiling.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Iterable

from google.cloud import firestore

from agenda7.domain import (
    Checkpoint,
    CommentDraft,
    DocketPosition,
    DocumentAnalysis,
    SourceDocument,
    TriageVerdict,
    Watch,
)

log = logging.getLogger(__name__)

SEEN_ID_CAP = 500

# A document is done when it has either been rejected by triage or fully
# analyzed. Anything else -- triage crashed, the download 403'd, the model
# returned junk -- is stuck, not finished, and must be retried on the next run.
# Plain existence-based dedup would bury those documents permanently, which is
# the worst possible failure for a tool whose whole job is not missing things.
TERMINAL_STAGES = frozenset({"analyzed", "rejected"})


class Collections:
    WATCHES = "watches"
    CHECKPOINTS = "checkpoints"
    DOCUMENTS = "documents"
    DOCKETS = "dockets"
    RUNS = "runs"
    DRAFTS = "drafts"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dump(model: Any) -> dict[str, Any]:
    """Serialize a pydantic model for Firestore (dates -> ISO strings)."""
    return model.model_dump(mode="json")


class Store:
    def __init__(self, project: str, database: str = "(default)") -> None:
        if not project:
            raise ValueError("project is required; set GOOGLE_CLOUD_PROJECT")
        self.db = firestore.Client(project=project, database=database)

    # ------------------------------------------------------------------
    # Watches
    # ------------------------------------------------------------------

    def upsert_watch(self, watch: Watch) -> None:
        self.db.collection(Collections.WATCHES).document(watch.watch_id).set(_dump(watch))
        log.info("upserted watch %s", watch.watch_id)

    def list_watches(self, active_only: bool = True) -> list[Watch]:
        col = self.db.collection(Collections.WATCHES)
        query = col.where(filter=firestore.FieldFilter("active", "==", True)) if active_only else col
        return [Watch.model_validate(doc.to_dict()) for doc in query.stream()]

    def get_watch(self, watch_id: str) -> Watch | None:
        snap = self.db.collection(Collections.WATCHES).document(watch_id).get()
        return Watch.model_validate(snap.to_dict()) if snap.exists else None

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------

    def get_checkpoint(self, watch_id: str) -> Checkpoint:
        snap = self.db.collection(Collections.CHECKPOINTS).document(watch_id).get()
        if snap.exists:
            return Checkpoint.model_validate(snap.to_dict())
        return Checkpoint(watch_id=watch_id)

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        checkpoint.last_run_at = _now()
        # Keep only the most recent ids; older ones can never collide again
        # because the posted-date floor has already moved past them.
        if len(checkpoint.seen_ids) > SEEN_ID_CAP:
            checkpoint.seen_ids = checkpoint.seen_ids[-SEEN_ID_CAP:]
        self.db.collection(Collections.CHECKPOINTS).document(checkpoint.watch_id).set(
            _dump(checkpoint)
        )

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    def document_exists(self, key: str) -> bool:
        return self.db.collection(Collections.DOCUMENTS).document(key).get().exists

    def new_documents(self, docs: Iterable[SourceDocument]) -> list[SourceDocument]:
        """Filter to documents not already stored.

        Uses one batched get instead of N round trips, because the poller may
        hand over a few dozen candidates per run.
        """
        docs = list(docs)
        if not docs:
            return []
        refs = [self.db.collection(Collections.DOCUMENTS).document(d.key) for d in docs]
        existing = {snap.id for snap in self.db.get_all(refs) if snap.exists}
        return [d for d in docs if d.key not in existing]

    def documents_needing_work(self, docs: Iterable[SourceDocument]) -> list[SourceDocument]:
        """Filter to documents that are not in a terminal stage.

        Unlike new_documents(), this deliberately returns documents already
        stored if they got stuck mid-pipeline, so a transient failure costs one
        extra pass instead of losing the filing forever.
        """
        docs = list(docs)
        if not docs:
            return []
        refs = [self.db.collection(Collections.DOCUMENTS).document(d.key) for d in docs]
        done: set[str] = set()
        for snap in self.db.get_all(refs):
            if not snap.exists:
                continue
            if (snap.to_dict() or {}).get("stage") in TERMINAL_STAGES:
                done.add(snap.id)
        return [d for d in docs if d.key not in done]

    def save_document(
        self,
        doc: SourceDocument,
        *,
        triage: TriageVerdict | None = None,
        analysis: DocumentAnalysis | None = None,
        stage: str = "discovered",
    ) -> None:
        payload: dict[str, Any] = {
            "document": _dump(doc),
            "stage": stage,
            "updated_at": _now(),
        }
        if triage is not None:
            payload["triage"] = _dump(triage)
        if analysis is not None:
            payload["analysis"] = _dump(analysis)
        self.db.collection(Collections.DOCUMENTS).document(doc.key).set(payload, merge=True)

    # ------------------------------------------------------------------
    # Docket position tracker - the record that visibly mutates
    # ------------------------------------------------------------------

    def get_docket(self, docket_id: str) -> DocketPosition | None:
        snap = self.db.collection(Collections.DOCKETS).document(docket_id).get()
        return DocketPosition.model_validate(snap.to_dict()) if snap.exists else None

    def fold_analysis(
        self,
        doc: SourceDocument,
        analysis: DocumentAnalysis,
    ) -> DocketPosition:
        """Fold one analysis into the docket's running position, atomically.

        Returns the post-fold state so the caller can log or notify on the
        revision bump without a second read.
        """
        docket_id = doc.docket_id or f"orphan__{doc.source_id}"
        ref = self.db.collection(Collections.DOCKETS).document(docket_id)

        @firestore.transactional
        def _fold(txn: firestore.Transaction) -> DocketPosition:
            snap = ref.get(transaction=txn)
            if snap.exists:
                position = DocketPosition.model_validate(snap.to_dict())
            else:
                position = DocketPosition(
                    docket_id=docket_id,
                    title=doc.title,
                    agency_id=doc.agency_id,
                )

            position.current_understanding = analysis.why_it_matters
            position.document_count += 1
            position.revision += 1
            position.updated_at = _now()

            # Merge parties, newest stance wins for a party already tracked.
            by_party = {p.party: p for p in position.parties}
            for incoming in analysis.positions:
                by_party[incoming.party] = incoming
            position.parties = list(by_party.values())

            # Deadlines are keyed by (what, due) so a re-analysis is idempotent.
            seen = {(d.what, d.due) for d in position.open_deadlines}
            for deadline in analysis.deadlines:
                if (deadline.what, deadline.due) not in seen:
                    position.open_deadlines.append(deadline)
            position.open_deadlines.sort(key=lambda d: d.due)

            entry = f"r{position.revision}: {analysis.headline}"
            if analysis.changes_prior_understanding and analysis.change_note:
                entry += f" | CHANGE: {analysis.change_note}"
            position.history.append(entry)

            txn.set(ref, _dump(position))
            return position

        result = _fold(self.db.transaction())
        log.info(
            "docket %s -> revision %s (%s docs)",
            docket_id,
            result.revision,
            result.document_count,
        )
        return result

    def list_dockets(self, limit: int = 25) -> list[DocketPosition]:
        query = (
            self.db.collection(Collections.DOCKETS)
            .order_by("updated_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [DocketPosition.model_validate(d.to_dict()) for d in query.stream()]

    def upcoming_deadlines(
        self, limit: int = 50, *, include_past: bool = False
    ) -> list[dict[str, Any]]:
        """Flatten deadlines across dockets, soonest first.

        Past dates are excluded by default. Filings routinely reference dates
        that have already passed -- the Unified Agenda in particular is largely
        a historical record -- and surfacing those as "upcoming" is worse than
        useless in a tool whose only job is to tell you what still needs doing.
        """
        today = date.today()
        rows: list[dict[str, Any]] = []
        for position in self.list_dockets(limit=limit):
            for deadline in position.open_deadlines:
                if not include_past and deadline.due < today:
                    continue
                rows.append(
                    {
                        "docket_id": position.docket_id,
                        "what": deadline.what,
                        "due": deadline.due,
                        "days_out": (deadline.due - today).days,
                    }
                )
        return sorted(rows, key=lambda r: r["due"])

    # ------------------------------------------------------------------
    # Comment drafts - staged for human review, never filed
    # ------------------------------------------------------------------

    def save_draft(self, draft: CommentDraft) -> str:
        """Persist a draft. Keyed by document so a re-analysis replaces rather
        than accumulates near-duplicate drafts for the same filing."""
        draft.created_at = draft.created_at or _now()
        key = f"{draft.docket_id}__{draft.document_id}".replace("/", "_")
        self.db.collection(Collections.DRAFTS).document(key).set(_dump(draft))
        log.info("staged comment draft %s", key)
        return key

    def list_drafts(self, limit: int = 25) -> list[CommentDraft]:
        query = self.db.collection(Collections.DRAFTS).limit(limit)
        return [CommentDraft.model_validate(d.to_dict()) for d in query.stream()]

    # ------------------------------------------------------------------
    # Run log - cheap observability, queryable without Cloud Trace
    # ------------------------------------------------------------------

    def log_run(
        self,
        run_id: str,
        *,
        watch_id: str,
        discovered: int,
        triaged_relevant: int,
        analyzed: int,
        status: str,
        error: str = "",
    ) -> None:
        self.db.collection(Collections.RUNS).document(run_id).set(
            {
                "run_id": run_id,
                "watch_id": watch_id,
                "discovered": discovered,
                "triaged_relevant": triaged_relevant,
                "analyzed": analyzed,
                "status": status,
                "error": error,
                "at": _now(),
            }
        )
