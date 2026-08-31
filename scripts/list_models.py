"""List reachable Gemini models AND prove the configured ones actually serve.

The distinction matters. client.models.list() reports models that a regional
endpoint will refuse to serve: every Gemini 3.x model shows up under
us-central1 and then returns 404 NOT_FOUND on the first real request. So this
script does not trust the listing -- it sends a real one-token request to each
configured model and reports what happened.

    python scripts/list_models.py
    python scripts/list_models.py --all-locations   # compare global vs region
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from agenda7.config import settings  # noqa: E402

PROBE_LOCATIONS = ["global", "us-central1", "us-east4", "europe-west4"]


def serve_check(client, model: str) -> tuple[bool, str]:
    """Send a real request. Listing presence is not proof of serving."""
    from google.genai import types

    try:
        resp = client.models.generate_content(
            model=model,
            contents="Reply with the single word: ok",
            config=types.GenerateContentConfig(max_output_tokens=2000),
        )
        return True, (resp.text or "").strip()[:30]
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).replace("\n", " ")
        if "404" in msg or "NOT_FOUND" in msg:
            return False, "404 NOT_FOUND (not served from this location)"
        return False, f"{type(exc).__name__}: {msg[:110]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-locations", action="store_true")
    args = ap.parse_args()

    cfg = settings()
    if not cfg.gcp_project:
        print("GOOGLE_CLOUD_PROJECT is not set in .env.")
        return 2

    try:
        from google import genai
    except ImportError:
        print("google-genai not installed. Run: pip install -r requirements.txt")
        return 2

    print(f"\nproject={cfg.gcp_project}")
    print(f"vertex_location={cfg.vertex_location}  (model serving)")
    print(f"gcp_region={cfg.gcp_region}  (Cloud Run / Firestore / bucket)\n")

    try:
        client = genai.Client(
            vertexai=True, project=cfg.gcp_project, location=cfg.vertex_location
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Client init failed: {type(exc).__name__}: {exc}")
        print("  - authenticate with: gcloud auth application-default login")
        return 1

    # ---- listing (informational only) ----
    try:
        names = [
            (getattr(m, "name", "") or "").split("/")[-1] for m in client.models.list()
        ]
        names = [n for n in names if n]
    except Exception as exc:  # noqa: BLE001
        print(f"Could not list models: {type(exc).__name__}: {exc}")
        names = []

    gemini = sorted({n for n in names if "gemini" in n.lower()})
    gemma = sorted({n for n in names if "gemma" in n.lower()})

    print(f"LISTED GEMINI MODELS ({len(gemini)}) - presence here is NOT proof of serving:")
    for n in gemini:
        print(f"  {n}")
    if gemma:
        print(f"\nGEMMA MODELS ({len(gemma)}) - each extra Google model is +0.2 bonus points:")
        for n in gemma:
            print(f"  {n}")

    # ---- the part that actually matters ----
    print(f"\n--- SERVE CHECK against location={cfg.vertex_location} ---")
    failures = 0
    for label, model in (("TRIAGE_MODEL", cfg.triage_model), ("ANALYST_MODEL", cfg.analyst_model)):
        ok, detail = serve_check(client, model)
        print(f"  [{'SERVES' if ok else 'FAILS '}] {label} = {model}  -> {detail}")
        if not ok:
            failures += 1

    if failures:
        print("\n  A listed-but-404 model almost always means wrong location.")
        print("  Gemini 3.x is served from the GLOBAL endpoint. Set in .env:")
        print("    GOOGLE_CLOUD_LOCATION=global")

    if args.all_locations:
        print("\n--- location comparison for TRIAGE_MODEL ---")
        for loc in PROBE_LOCATIONS:
            try:
                c = genai.Client(vertexai=True, project=cfg.gcp_project, location=loc)
                ok, detail = serve_check(c, cfg.triage_model)
                print(f"  [{'OK  ' if ok else 'FAIL'}] {loc:16} {detail}")
            except Exception as exc:  # noqa: BLE001
                print(f"  [FAIL] {loc:16} client init: {type(exc).__name__}")

    print()
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
