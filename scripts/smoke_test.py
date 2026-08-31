"""Live end-to-end smoke test: real API -> Firestore -> mutating tracker.

Proves the whole state layer against real infrastructure, with no model calls,
so it costs nothing but a handful of API requests. Run it after `gcloud auth
application-default login`.

    python scripts/smoke_test.py
    python scripts/smoke_test.py --term "pole attachment" --agency FCC
    python scripts/smoke_test.py --cleanup      # remove what this wrote

What it verifies, in order:
  1. Regulations.gov returns live documents for a watch
  2. Firestore accepts a Watch and reads it back identically
  3. new_documents() dedups on a second pass
  4. enrich_from_detail() adds abstract / pageCount / file urls
  5. fold_analysis() is transactional and bumps the revision
  6. a second fold merges rather than overwrites (the tracker mutates)
  7. deadlines surface, sorted
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from agenda7.config import settings  # noqa: E402
from agenda7.domain import (  # noqa: E402
    Deadline,
    DocumentAnalysis,
    Position,
    SourceKind,
    Watch,
)
from agenda7.sources.regulations_gov import RegulationsGovClient  # noqa: E402
from agenda7.store import Collections, Store  # noqa: E402

SMOKE_WATCH_ID = "smoke-test-watch"


def step(n: int, label: str) -> None:
    print(f"\n{n}. {label}")


def ok(msg: str) -> None:
    print(f"   [ok] {msg}")


def bad(msg: str) -> None:
    print(f"   [!!] {msg}")


def cleanup(store: Store) -> None:
    """Delete only what this script created."""
    removed = 0
    store.db.collection(Collections.WATCHES).document(SMOKE_WATCH_ID).delete()
    store.db.collection(Collections.CHECKPOINTS).document(SMOKE_WATCH_ID).delete()
    for col in (Collections.DOCUMENTS, Collections.DOCKETS, Collections.RUNS):
        for snap in store.db.collection(col).stream():
            data = snap.to_dict() or {}
            marker = data.get("smoke_test") or data.get("watch_id") == SMOKE_WATCH_ID
            if marker:
                snap.reference.delete()
                removed += 1
    print(f"removed {removed} smoke-test document(s) plus watch/checkpoint")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--term", default="broadband")
    ap.add_argument("--agency", default="FCC")
    ap.add_argument("--cleanup", action="store_true")
    args = ap.parse_args()

    cfg = settings()
    store = Store(project=cfg.gcp_project, database=cfg.firestore_database)

    if args.cleanup:
        cleanup(store)
        return 0

    key = cfg.require_regulations_key()
    client = RegulationsGovClient(key, cfg.regulations_base_url)
    failures = 0

    print(f"project={cfg.gcp_project}  db={cfg.firestore_database}")

    # ---------------------------------------------------------------
    step(1, "Firestore write + read back")
    watch = Watch(
        watch_id=SMOKE_WATCH_ID,
        label=f"smoke: {args.term} @ {args.agency}",
        source=SourceKind.REGULATIONS_GOV,
        search_term=args.term,
        agency_ids=[args.agency],
        keywords=["broadband", "pole attachment", "universal service"],
        rationale="Smoke test only.",
    )
    store.upsert_watch(watch)
    echoed = store.get_watch(SMOKE_WATCH_ID)
    if echoed and echoed.label == watch.label and echoed.agency_ids == watch.agency_ids:
        ok(f"round tripped watch {echoed.watch_id!r}")
    else:
        failures += 1
        bad(f"watch round trip mismatch: {echoed}")

    # ---------------------------------------------------------------
    step(2, "Live document discovery")
    docs = list(
        client.documents_for_watch(
            watch,
            since=date.today() - timedelta(days=45),
            max_docs=5,
        )
    )
    if docs:
        ok(f"{len(docs)} documents")
        for d in docs:
            print(f"        {d.source_id}  docket={d.docket_id}  {d.title[:58]}")
    else:
        failures += 1
        bad("no documents returned - widen --term or the date window")
        return 1

    # ---------------------------------------------------------------
    step(3, "Dedup via new_documents()")
    fresh = store.new_documents(docs)
    ok(f"{len(fresh)} of {len(docs)} are new on first pass")
    for d in fresh:
        store.save_document(d, stage="discovered")
        store.db.collection(Collections.DOCUMENTS).document(d.key).set(
            {"smoke_test": True}, merge=True
        )
    again = store.new_documents(docs)
    if not again:
        ok("second pass returns 0 - dedup works")
    else:
        failures += 1
        bad(f"dedup leaked {len(again)} document(s)")

    # ---------------------------------------------------------------
    step(4, "Detail enrichment")
    target = docs[0]
    enriched = client.enrich_from_detail(target)
    gained = []
    if enriched.abstract:
        gained.append(f"abstract({len(enriched.abstract)} chars)")
    if enriched.page_count:
        gained.append(f"pageCount={enriched.page_count}")
    if enriched.file_urls:
        gained.append(f"{len(enriched.file_urls)} file url(s)")
    if enriched.topics:
        gained.append(f"topics={enriched.topics[:3]}")
    if gained:
        ok("gained " + ", ".join(gained))
    else:
        bad("detail fetch added nothing (may be a thin record, not fatal)")

    # ---------------------------------------------------------------
    step(5, "Transactional fold - revision 1")
    analysis_a = DocumentAnalysis(
        headline="Commission proposes shorter make-ready timelines",
        why_it_matters="Compresses the review window for attachment requests.",
        positions=[Position(party="Commission", stance="proposing", summary="Shorten to 45 days.")],
        deadlines=[
            Deadline(
                what="Initial comments due",
                due=enriched.comment_end_date or (date.today() + timedelta(days=30)),
                source_quote="Comments are due on or before the date above.",
            )
        ],
        entities=["FCC"],
    )
    pos1 = store.fold_analysis(enriched, analysis_a)
    ok(f"docket {pos1.docket_id} revision={pos1.revision} docs={pos1.document_count}")

    # ---------------------------------------------------------------
    step(6, "Second fold - proves mutation, not append")
    analysis_b = DocumentAnalysis(
        headline="Trade association opposes the compressed timeline",
        why_it_matters="Opposition filed; timeline now contested.",
        positions=[
            Position(party="Trade association", stance="opposing", summary="45 days is infeasible."),
            Position(party="Commission", stance="proposing", summary="Reaffirms 45 days."),
        ],
        deadlines=[
            Deadline(what="Reply comments due", due=date.today() + timedelta(days=60)),
            Deadline(
                what="Initial comments due",
                due=enriched.comment_end_date or (date.today() + timedelta(days=30)),
            ),
        ],
        changes_prior_understanding=True,
        change_note="Proposal is now contested rather than unopposed.",
    )
    pos2 = store.fold_analysis(enriched, analysis_b)

    checks = {
        "revision incremented": pos2.revision == pos1.revision + 1,
        "document_count incremented": pos2.document_count == pos1.document_count + 1,
        "parties merged not duplicated": len({p.party for p in pos2.parties}) == len(pos2.parties),
        "both parties present": len(pos2.parties) == 2,
        "deadlines deduped": len(pos2.open_deadlines) == 2,
        "deadlines sorted": pos2.open_deadlines == sorted(pos2.open_deadlines, key=lambda d: d.due),
        "history retained": len(pos2.history) == 2,
    }
    for label, passed in checks.items():
        if passed:
            ok(label)
        else:
            failures += 1
            bad(label)

    print(f"\n        tracker now reads: {pos2.current_understanding}")
    print(f"        parties: {[(p.party, p.stance) for p in pos2.parties]}")
    for entry in pos2.history:
        print(f"        {entry}")

    # ---------------------------------------------------------------
    step(7, "Deadline surfacing")
    deadlines = store.upcoming_deadlines()
    if deadlines:
        ok(f"{len(deadlines)} open deadline(s), soonest first")
        for row in deadlines[:5]:
            print(f"        {row['due']}  {row['docket_id']}  {row['what']}")
    else:
        failures += 1
        bad("no deadlines surfaced")

    store.log_run(
        run_id=f"smoke-{date.today().isoformat()}",
        watch_id=SMOKE_WATCH_ID,
        discovered=len(docs),
        triaged_relevant=1,
        analyzed=2,
        status="ok" if not failures else "partial",
    )

    print(f"\n  quota remaining: {client.last_rate_limit.get('X-RateLimit-Remaining', '?')}")
    print(f"\n{'SMOKE TEST PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
    print("  cleanup with: python scripts/smoke_test.py --cleanup\n")
    client.close()
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
