"""Print everything the agent currently knows.

Read-only. Makes no API calls and no model calls, so it is free to run and safe
to point a camera at. Useful for verifying a deployed run actually changed
state, and as the "proof of action" shot in a demo.

    python scripts/show_state.py
    python scripts/show_state.py --drafts        # include full draft text
    python scripts/show_state.py --rejected      # include what was filtered out
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from agenda7.config import settings  # noqa: E402
from agenda7.store import Collections, Store  # noqa: E402


def rule(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--drafts", action="store_true", help="print full draft bodies")
    ap.add_argument("--rejected", action="store_true", help="list rejected documents")
    args = ap.parse_args()

    cfg = settings()
    store = Store(project=cfg.gcp_project, database=cfg.firestore_database)
    print(f"project {cfg.gcp_project}   database {cfg.firestore_database}")

    # ---- counts ----
    rule("STATE")
    for col in (
        Collections.WATCHES,
        Collections.CHECKPOINTS,
        Collections.DOCUMENTS,
        Collections.DOCKETS,
        Collections.DRAFTS,
        Collections.RUNS,
    ):
        print(f"  {col:14} {len(list(store.db.collection(col).stream()))}")

    # ---- watches ----
    rule("WATCHES")
    for watch in store.list_watches(active_only=False):
        cp = store.get_checkpoint(watch.watch_id)
        print(f"  {watch.watch_id}  ({'active' if watch.active else 'paused'})")
        print(f"    agencies: {watch.agency_ids or 'any'}   term: {watch.search_term or '(none)'}")
        print(f"    topics  : {', '.join(watch.keywords) or '(none)'}")
        print(f"    last run: {cp.last_run_at or 'never'}  status={cp.last_run_status}")
        print(f"    watermark posted_date: {cp.last_posted_date or '(none)'}")

    # ---- dockets: the mutating trackers ----
    rule("DOCKET TRACKERS")
    dockets = store.list_dockets(limit=50)
    if not dockets:
        print("  (none yet)")
    for p in dockets:
        print(f"\n  {p.docket_id}   revision {p.revision}   filings {p.document_count}")
        print(f"    {p.current_understanding[:150]}")
        if p.parties:
            print("    parties:")
            for party in p.parties:
                print(f"      - {party.party} [{party.stance}] {party.summary[:70]}")
        if p.open_deadlines:
            print("    deadlines:")
            for d in p.open_deadlines:
                delta = (d.due - date.today()).days
                tag = "PAST" if delta < 0 else f"+{delta}d"
                print(f"      {d.due} ({tag:>6}) {d.what[:60]}")
        if p.history:
            print("    history:")
            for h in p.history:
                print(f"      {h[:110]}")

    # ---- deadlines ----
    rule("UPCOMING DEADLINES")
    upcoming = store.upcoming_deadlines()
    if not upcoming:
        print("  (none)")
    for row in upcoming:
        print(f"  {row['due']}  (+{row['days_out']:>4}d)  {row['docket_id']:18} {row['what'][:52]}")

    # ---- drafts ----
    drafts = store.list_drafts(limit=50)
    rule(f"COMMENT DRAFTS ({len(drafts)}) - staged, never filed")
    for d in drafts:
        print(f"\n  {d.docket_id} / {d.document_id}   deadline {d.comment_deadline or '?'}")
        print(f"    subject: {d.subject}")
        if d.key_points:
            print("    points:")
            for point in d.key_points:
                print(f"      - {point[:100]}")
        if d.needs_from_you:
            print("    needs you:")
            for item in d.needs_from_you:
                print(f"      ! {item[:100]}")
        if args.drafts:
            print("    ---- body ----")
            for line in d.body.splitlines():
                print(f"    {line}")
            print("    --------------")
    if drafts:
        print("\n  Nothing above has been filed. This system cannot file.")

    # ---- rejections ----
    if args.rejected:
        rule("FILTERED OUT")
        docs = store.db.collection(Collections.DOCUMENTS).stream()
        rejected = []
        for snap in docs:
            data = snap.to_dict() or {}
            if data.get("stage") == "rejected":
                rejected.append(
                    (
                        (data.get("document") or {}).get("title", "?"),
                        (data.get("triage") or {}).get("rationale", ""),
                    )
                )
        print(f"  {len(rejected)} document(s) judged not relevant\n")
        for title, why in rejected:
            print(f"  - {title[:88]}")
            print(f"      {why[:110]}")

    # ---- runs ----
    rule("RECENT RUNS")
    runs = list(store.db.collection(Collections.RUNS).stream())
    rows = sorted(
        (r.to_dict() or {} for r in runs), key=lambda x: str(x.get("at", "")), reverse=True
    )
    for r in rows[:10]:
        print(
            f"  {str(r.get('at'))[:19]}  {r.get('watch_id','?'):16} "
            f"discovered={r.get('discovered',0):<4} relevant={r.get('triaged_relevant',0):<3} "
            f"analyzed={r.get('analyzed',0):<3} {r.get('status','?')}"
        )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
