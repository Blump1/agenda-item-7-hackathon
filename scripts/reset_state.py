"""Clear pipeline state so a run starts from scratch.

Useful for two things: getting a clean end-to-end run after changing agent
behaviour, and resetting to a known-empty state before recording a demo.

Watches are configuration, not state, so they are kept unless --all is passed.
Cloud Storage objects are left alone; they are provenance and cost almost
nothing.

    python scripts/reset_state.py --yes
    python scripts/reset_state.py --yes --all      # also delete watches
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from agenda7.config import settings  # noqa: E402
from agenda7.store import Collections, Store  # noqa: E402

STATE = [
    Collections.DOCUMENTS,
    Collections.DOCKETS,
    Collections.CHECKPOINTS,
    Collections.RUNS,
    Collections.DRAFTS,
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="required; this deletes data")
    ap.add_argument("--all", action="store_true", help="also delete watches")
    args = ap.parse_args()

    cfg = settings()
    store = Store(project=cfg.gcp_project, database=cfg.firestore_database)
    targets = STATE + ([Collections.WATCHES] if args.all else [])

    counts = {c: len(list(store.db.collection(c).stream())) for c in targets}
    total = sum(counts.values())

    print(f"\nproject {cfg.gcp_project}, database {cfg.firestore_database}")
    for col, n in counts.items():
        print(f"  {col:14} {n}")
    print(f"  {'TOTAL':14} {total}")

    if not args.yes:
        print("\nNothing deleted. Re-run with --yes to confirm.\n")
        return 1
    if not total:
        print("\nAlready empty.\n")
        return 0

    deleted = 0
    for col in targets:
        for snap in store.db.collection(col).stream():
            snap.reference.delete()
            deleted += 1
    print(f"\ndeleted {deleted} document(s)")
    if not args.all:
        kept = len(list(store.db.collection(Collections.WATCHES).stream()))
        print(f"kept {kept} watch(es) - pass --all to remove those too")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
