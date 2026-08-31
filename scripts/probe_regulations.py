"""Ground-truth probe against the live Regulations.gov API.

Run this FIRST, before building anything on top of the adapter. It verifies the
filter syntax, prints the real response shape, and reports your rate-limit
headroom. If any variant below fails, the adapter gets corrected to match
reality rather than to match the documentation.

    python scripts/probe_regulations.py
    python scripts/probe_regulations.py --term "broadband" --dump
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from agenda7.config import settings  # noqa: E402
from agenda7.sources.regulations_gov import RegulationsGovClient  # noqa: E402

OUT_DIR = pathlib.Path(__file__).resolve().parents[1] / "probe-output"


def show(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" - {detail}" if detail else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--term", default="broadband", help="search term to probe with")
    ap.add_argument("--dump", action="store_true", help="write raw JSON to probe-output/")
    args = ap.parse_args()

    cfg = settings()
    try:
        key = cfg.require_regulations_key()
    except RuntimeError as exc:
        print(f"\n{exc}\n")
        return 2

    print(f"\nRegulations.gov probe  (key ...{key[-4:]})")
    print(f"base: {cfg.regulations_base_url}\n")

    client = RegulationsGovClient(key, cfg.regulations_base_url)
    since = date.today() - timedelta(days=30)
    first_id: str | None = None
    failures = 0

    # --- 1. bare listing: does auth work at all? ---
    print("1. auth + bare listing")
    try:
        payload = client.search_documents(page_size=5)
        records = payload.get("data") or []
        show("GET /documents", True, f"{len(records)} records")
        if records:
            first_id = records[0].get("id")
        meta = payload.get("meta") or {}
        print(f"       meta keys: {sorted(meta.keys())}")
        if "totalElements" in meta:
            print(f"       totalElements: {meta['totalElements']}")
    except httpx.HTTPStatusError as exc:
        failures += 1
        show("GET /documents", False, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
        if exc.response.status_code in (401, 403):
            print("\n  -> Key rejected. Confirm it is the regulations.gov key and is active.")
            return 1
    except Exception as exc:  # noqa: BLE001
        failures += 1
        show("GET /documents", False, f"{type(exc).__name__}: {exc}")

    # --- 2. each filter variant, isolated ---
    print("\n2. filter syntax")
    variants = {
        "filter[searchTerm]": {"search_term": args.term},
        "filter[postedDate][ge]": {"posted_on_or_after": since},
        "filter[agencyId]": {"agency_ids": ["FCC"]},
        "searchTerm + postedDate": {"search_term": args.term, "posted_on_or_after": since},
        "sort=-postedDate": {"sort": "-postedDate"},
    }
    for label, kwargs in variants.items():
        try:
            payload = client.search_documents(page_size=5, **kwargs)  # type: ignore[arg-type]
            count = len(payload.get("data") or [])
            show(label, True, f"{count} records")
        except httpx.HTTPStatusError as exc:
            failures += 1
            body = exc.response.text[:200].replace("\n", " ")
            show(label, False, f"HTTP {exc.response.status_code}: {body}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            show(label, False, f"{type(exc).__name__}: {exc}")

    # --- 3. attribute shape (drives domain.py) ---
    print("\n3. document attributes")
    sample = None
    try:
        payload = client.search_documents(
            search_term=args.term, posted_on_or_after=since, page_size=5
        )
        records = payload.get("data") or []
        if records:
            sample = records[0]
            first_id = sample.get("id")
            attrs = sample.get("attributes") or {}
            print(f"       listing attributes: {sorted(attrs.keys())}")
            norm = RegulationsGovClient.to_source_document(sample)
            print(f"       normalized -> {norm.title[:70]!r}")
            print(
                f"       docket={norm.docket_id} agency={norm.agency_id} "
                f"type={norm.document_type} posted={norm.posted_date} "
                f"comment_end={norm.comment_end_date} open={norm.open_for_comment}"
            )
            show("normalization", True)
        else:
            show("normalization", False, "no records matched; try a different --term")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        show("normalization", False, f"{type(exc).__name__}: {exc}")

    # --- 4. detail endpoint: where the PDF URLs live ---
    print("\n4. detail endpoint (fileFormats / PDF urls)")
    detail = None
    if first_id:
        try:
            detail = client.get_document(first_id)
            attrs = (detail.get("data") or {}).get("attributes") or {}
            print(f"       detail attributes: {sorted(attrs.keys())}")
            fmts = attrs.get("fileFormats") or []
            show("fileFormats present", bool(fmts), f"{len(fmts)} file(s)")
            for fmt in fmts[:3]:
                if isinstance(fmt, dict):
                    print(f"         {fmt.get('format')} {fmt.get('size')} {fmt.get('fileUrl')}")
            if not fmts:
                print("       -> No inline files. Attachments may live on /comments"
                      " or the attachments relationship; check the dump.")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            show("GET /documents/{id}", False, f"{type(exc).__name__}: {exc}")
    else:
        show("detail fetch", False, "no document id available from earlier steps")

    # --- 5. quota ---
    print("\n5. rate limit")
    if client.last_rate_limit:
        for k, v in client.last_rate_limit.items():
            print(f"       {k}: {v}")
    else:
        print("       (no rate-limit headers returned)")

    if args.dump:
        OUT_DIR.mkdir(exist_ok=True)
        if sample:
            (OUT_DIR / "sample_listing.json").write_text(
                json.dumps(sample, indent=2), encoding="utf-8"
            )
        if detail:
            (OUT_DIR / "sample_detail.json").write_text(
                json.dumps(detail, indent=2), encoding="utf-8"
            )
        print(f"\n       raw JSON written to {OUT_DIR}")

    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}\n")
    client.close()
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
