"""Run one watch through the full pipeline, locally, with a readable report.

This is the same process_watch() that Cloud Run will call from Pub/Sub, so what
you see here is what runs in production.

    # cheap: triage only, no deep reads
    python scripts/run_once.py --term "pole attachment" --agency FCC --no-analyze

    # full run
    python scripts/run_once.py --term "pole attachment" --agency FCC --max-docs 5

    # re-run the same watch (checkpoint means it finds only what is new)
    python scripts/run_once.py --watch-id pole-attachments
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import pathlib
import sys
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from agenda7.actions import (  # noqa: E402
    build_calendar,
    digest_filename,
    drafter_agent,
    event_count,
    render_digest,
)
from agenda7.agents.analyst import analyst_agent  # noqa: E402
from agenda7.agents.triage import triage_agent  # noqa: E402
from agenda7.config import configure_genai_env, settings  # noqa: E402
from agenda7.domain import SourceKind, Watch  # noqa: E402
from agenda7.pipeline import process_watch  # noqa: E402
from agenda7.sources.regulations_gov import RegulationsGovClient  # noqa: E402
from agenda7.store import Store  # noqa: E402

STAGE_MARK = {
    "analyzed": "[ANALYZED]",
    "rejected": "[rejected ]",
    "passed_triage": "[RELEVANT ]",
    "triage_failed": "[!! triage]",
    "staging_failed": "[!! stage ]",
    "analysis_failed": "[!! read  ]",
}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch-id", default="pole-attachments")
    ap.add_argument("--term", default=None, help="search term (creates/updates the watch)")
    ap.add_argument("--agency", default=None, help="agency id, e.g. FCC")
    ap.add_argument("--keywords", default=None, help="comma-separated triage topics")
    ap.add_argument(
        "--doc-types",
        default=None,
        help='comma-separated document types, e.g. "Proposed Rule,Rule". '
        "A structural filter; leave unset to see everything an agency posts.",
    )
    ap.add_argument("--rationale", default=None, help="why these matter to you")
    ap.add_argument("--max-docs", type=int, default=None)
    ap.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="ignore the checkpoint and scan back this many days (backfill)",
    )
    ap.add_argument("--no-analyze", action="store_true", help="stop after triage (cheap)")
    ap.add_argument(
        "--draft",
        action="store_true",
        help="stage a comment draft for filings still inside the comment window",
    )
    ap.add_argument("--out", default="out", help="directory for .ics and digest artifacts")
    ap.add_argument("--no-artifacts", action="store_true", help="skip writing files")
    ap.add_argument("--reset", action="store_true", help="clear this watch's checkpoint first")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    # ADK/genai emit a noisy AFC advisory on every call; it is not actionable.
    logging.getLogger("google_genai").setLevel(logging.ERROR)

    configure_genai_env()
    cfg = settings()
    store = Store(project=cfg.gcp_project, database=cfg.firestore_database)

    # ---- resolve the watch ----
    watch = store.get_watch(args.watch_id)
    if args.term or args.agency or watch is None:
        if not (args.term or args.agency):
            print(
                f"Watch {args.watch_id!r} does not exist. "
                "Provide --agency (preferred) and/or --term to create it."
            )
            return 2
        keywords = (
            [k.strip() for k in args.keywords.split(",") if k.strip()]
            if args.keywords
            else ([args.term] if args.term else [])
        )
        # Prefer agency-scoped retrieval with agent-side relevance. Verified
        # against the live API: filter[searchTerm] is both noisy across agencies
        # and near-empty when combined with an agency filter, so using it as the
        # relevance mechanism loses documents. See docs/architecture.md.
        watch = Watch(
            watch_id=args.watch_id,
            label=args.term or f"{args.agency} docket feed",
            source=SourceKind.REGULATIONS_GOV,
            search_term=args.term,
            agency_ids=[args.agency] if args.agency else [],
            document_types=(
                [t.strip() for t in args.doc_types.split(",") if t.strip()]
                if args.doc_types
                else []
            ),
            keywords=keywords,
            rationale=args.rationale
            or "Tracking these proceedings for comment deadlines and shifting positions.",
        )
        store.upsert_watch(watch)
        print(f"watch {watch.watch_id!r} saved")

    if args.reset:
        store.db.collection("checkpoints").document(watch.watch_id).delete()
        print(f"checkpoint for {watch.watch_id!r} cleared")

    print(f"\nwatch      : {watch.watch_id}  ({watch.label})")
    print(f"agencies   : {watch.agency_ids or 'any'}")
    print(f"search term: {watch.search_term or '(none - agent decides relevance)'}")
    print(f"doc types  : {', '.join(watch.document_types) or '(all)'}")
    print(f"topics     : {', '.join(watch.keywords)}")
    print(f"models     : triage={cfg.triage_model}  analyst={cfg.analyst_model}")
    print(f"mode       : {'TRIAGE ONLY' if args.no_analyze else 'full pipeline'}")

    print(f"actions    : {'draft comments' if args.draft else 'none'}")

    client = RegulationsGovClient(cfg.require_regulations_key(), cfg.regulations_base_url)
    triage = triage_agent(cfg.triage_model)
    analyst = analyst_agent(cfg.analyst_model)
    drafter = drafter_agent(cfg.analyst_model) if args.draft else None

    try:
        stats = await process_watch(
            watch,
            store=store,
            client=client,
            triage=triage,
            analyst=analyst,
            drafter=drafter,
            cfg=cfg,
            max_docs=args.max_docs,
            analyze=not args.no_analyze,
            since_override=(
                date.today() - timedelta(days=args.lookback_days)
                if args.lookback_days
                else None
            ),
        )
    finally:
        await triage.close()
        await analyst.close()
        if drafter is not None:
            await drafter.close()
        client.close()

    # ---- report ----
    print(f"\n=== run {stats.run_id} : {stats.status.upper()} ===")
    print(
        f"  discovered={stats.discovered}  already_seen={stats.already_seen}  "
        f"triaged={stats.triaged}  relevant={stats.relevant}  "
        f"analyzed={stats.analyzed}  failed={stats.failed}"
    )

    if stats.outcomes:
        print("\n--- per document ---")
        for o in stats.outcomes:
            mark = STAGE_MARK.get(o.stage, f"[{o.stage}]")
            conf = f" conf={o.confidence:.2f}" if o.confidence is not None else ""
            print(f"  {mark} {o.source_id}{conf}")
            print(f"              {o.title[:88]}")
            if o.note:
                print(f"              -> {o.note[:110]}")
            if o.revision is not None:
                print(f"              -> docket {o.docket_id} now at revision {o.revision}")

    if stats.positions:
        print("\n--- docket trackers touched ---")
        for p in stats.positions:
            print(f"  {p.docket_id}  rev={p.revision}  filings={p.document_count}")
            print(f"    understanding: {p.current_understanding[:130]}")
            for d in p.open_deadlines[:4]:
                print(f"    deadline {d.due}: {d.what}")

    upcoming = store.upcoming_deadlines()
    if upcoming:
        print(f"\n--- upcoming deadlines ({len(upcoming)}) ---")
        for row in upcoming[:8]:
            print(
                f"  {row['due']}  (+{row['days_out']:>4}d)  "
                f"{row['docket_id']:16} {row['what'][:56]}"
            )
    past = [
        r
        for r in store.upcoming_deadlines(include_past=True)
        if r["days_out"] < 0
    ]
    if past:
        print(f"  ({len(past)} past deadline(s) tracked but hidden)")

    if stats.drafts:
        print(f"\n--- comment drafts staged ({len(stats.drafts)}) ---")
        for d in stats.drafts:
            print(f"  {d.docket_id}  deadline {d.comment_deadline or '?'}")
            print(f"    subject: {d.subject[:96]}")
            for point in d.key_points[:4]:
                print(f"      - {point[:96]}")
            if d.needs_from_you:
                print("    needs you before filing:")
                for item in d.needs_from_you[:4]:
                    print(f"      ! {item[:96]}")
        print("\n  Nothing was filed. This system has no ability to file.")

    # ---- write artifacts ----
    if not args.no_artifacts:
        outdir = pathlib.Path(args.out)
        outdir.mkdir(parents=True, exist_ok=True)

        ics = build_calendar(upcoming)
        ics_path = outdir / "deadlines.ics"
        ics_path.write_text(ics, encoding="utf-8", newline="")

        digest = render_digest(
            run_id=stats.run_id,
            watch_label=f"{watch.watch_id} ({watch.label})",
            discovered=stats.discovered,
            triaged=stats.triaged,
            relevant=stats.relevant,
            analyzed=stats.analyzed,
            failed=stats.failed,
            positions=stats.positions,
            deadlines=upcoming,
            drafts=stats.drafts,
            rejected=stats.rejected,
        )
        digest_path = outdir / digest_filename(stats.run_id)
        digest_path.write_text(digest, encoding="utf-8")

        print("\n--- artifacts written ---")
        print(f"  {ics_path}   ({event_count(ics)} calendar events, import into any calendar)")
        print(f"  {digest_path}")

    print(f"\n  regulations.gov quota left: {client.last_rate_limit.get('X-RateLimit-Remaining', '?')}")
    print()
    return 0 if stats.status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
