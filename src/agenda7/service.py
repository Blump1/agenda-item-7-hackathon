"""Cloud Run service: the HTTP surface that makes the agent autonomous.

Three endpoints, matching the architecture:

  GET  /healthz          liveness
  POST /tasks/poll       Cloud Scheduler target. Discovers work for every active
                         watch and publishes one Pub/Sub message per document.
                         Returns fast; does no model work itself.
  POST /tasks/document   Pub/Sub push subscription target. Processes exactly one
                         document. This is the unit of retry.

Why the split. A deep read of a long filing takes minutes and can fail. If
discovery and analysis shared a request, one slow document would stall the whole
poll and a single bad document would fail the run. Separating them means
discovery stays fast, documents retry individually, and repeated failures land in
the dead-letter topic instead of wedging the pipeline.

There is also POST /tasks/run-inline, which runs a whole watch in one request.
It bypasses Pub/Sub entirely and exists for first-deploy smoke testing and for
demos, where watching one request do everything is easier to narrate.

Auth: no endpoint is public. Cloud Scheduler and Pub/Sub both authenticate with
OIDC tokens against a service account that has run.invoker; the service is
deployed with --no-allow-unauthenticated so Cloud Run rejects anonymous callers
before this code runs. See docs/DEPLOY.md.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request

from agenda7.actions.drafter import drafter_agent
from agenda7.agents.analyst import analyst_agent
from agenda7.agents.triage import triage_agent
from agenda7.config import configure_genai_env, settings
from agenda7.domain import SourceDocument, Watch
from agenda7.pipeline import process_document, process_watch
from agenda7.sources.regulations_gov import RegulationsGovClient
from agenda7.store import Store

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("agenda7.service")

TOPIC_DOCUMENTS = os.environ.get("PUBSUB_TOPIC", "agenda7-documents")

# Whether the poller asks for comment drafts. Off by default: drafting is the
# most expensive call in the pipeline and should be an explicit choice.
DRAFT_ENABLED = os.environ.get("ENABLE_DRAFTS", "").lower() in {"1", "true", "yes"}


class Deps:
    """Long-lived clients, built once per container rather than per request.

    Cloud Run reuses a warm container across requests, so building the Firestore
    client, the agents, and the publisher on every call would add latency and
    connection churn for no benefit.
    """

    def __init__(self) -> None:
        configure_genai_env()
        self.cfg = settings()
        self.store = Store(project=self.cfg.gcp_project, database=self.cfg.firestore_database)
        self.client = RegulationsGovClient(
            self.cfg.require_regulations_key(), self.cfg.regulations_base_url
        )
        self.triage = triage_agent(self.cfg.triage_model)
        self.analyst = analyst_agent(self.cfg.analyst_model)
        self.drafter = drafter_agent(self.cfg.analyst_model) if DRAFT_ENABLED else None
        self._publisher: Any | None = None

    @property
    def publisher(self) -> Any:
        if self._publisher is None:
            from google.cloud import pubsub_v1

            self._publisher = pubsub_v1.PublisherClient()
        return self._publisher

    def topic_path(self) -> str:
        return self.publisher.topic_path(self.cfg.gcp_project, TOPIC_DOCUMENTS)

    async def aclose(self) -> None:
        await self.triage.close()
        await self.analyst.close()
        if self.drafter is not None:
            await self.drafter.close()
        self.client.close()


deps: Deps | None = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global deps
    deps = Deps()
    log.info(
        "ready: project=%s triage=%s analyst=%s drafts=%s",
        deps.cfg.gcp_project, deps.cfg.triage_model, deps.cfg.analyst_model, DRAFT_ENABLED,
    )
    try:
        yield
    finally:
        if deps is not None:
            await deps.aclose()


app = FastAPI(title="Agenda Item 7", lifespan=lifespan)


def _deps() -> Deps:
    if deps is None:  # pragma: no cover - lifespan always runs under uvicorn
        raise HTTPException(status_code=503, detail="not ready")
    return deps


@app.get("/healthcheck")
async def healthcheck() -> dict[str, Any]:
    d = _deps()
    return {
        "ok": True,
        "project": d.cfg.gcp_project,
        "vertex_location": d.cfg.vertex_location,
        "triage_model": d.cfg.triage_model,
        "analyst_model": d.cfg.analyst_model,
        "drafts_enabled": DRAFT_ENABLED,
    }


@app.post("/tasks/poll")
async def poll() -> dict[str, Any]:
    """Discover work for every active watch and fan it out over Pub/Sub."""
    d = _deps()
    published, per_watch = 0, {}

    for watch in d.store.list_watches(active_only=True):
        checkpoint = d.store.get_checkpoint(watch.watch_id)
        try:
            candidates = list(
                d.client.documents_for_watch(
                    watch,
                    since=checkpoint.last_posted_date,
                    lookback_days=d.cfg.initial_lookback_days,
                    max_docs=d.cfg.max_docs_per_run,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.error("poll: discovery failed for %s: %s", watch.watch_id, exc)
            per_watch[watch.watch_id] = {"error": str(exc)[:200]}
            continue

        fresh = d.store.documents_needing_work(candidates)
        newest = checkpoint.last_posted_date
        futures = []
        for doc in fresh:
            if doc.posted_date and (newest is None or doc.posted_date > newest):
                newest = doc.posted_date
            payload = json.dumps(
                {"watch_id": watch.watch_id, "document": doc.model_dump(mode="json")}
            ).encode("utf-8")
            futures.append(d.publisher.publish(d.topic_path(), payload))

        # Block until Pub/Sub has accepted every message. Without this the
        # checkpoint could advance past documents that were never queued.
        for future in futures:
            future.result(timeout=30)

        # Only advance the checkpoint once the work is durably queued.
        checkpoint.last_posted_date = newest
        checkpoint.last_run_status = "queued"
        d.store.save_checkpoint(checkpoint)

        published += len(fresh)
        per_watch[watch.watch_id] = {
            "discovered": len(candidates),
            "queued": len(fresh),
        }
        log.info("poll: %s discovered=%d queued=%d", watch.watch_id, len(candidates), len(fresh))

    return {"published": published, "watches": per_watch}


@app.post("/tasks/document")
async def handle_document(request: Request) -> dict[str, Any]:
    """Pub/Sub push target. Processes one document.

    Status codes matter here: Pub/Sub retries on any non-2xx. A malformed
    envelope returns 204 so it is acknowledged and dropped rather than redelivered
    forever, while a genuine processing failure returns 500 so it is retried and
    eventually dead-lettered.
    """
    d = _deps()
    try:
        envelope = await request.json()
        message = envelope["message"]
        raw = base64.b64decode(message["data"])
        payload = json.loads(raw)
        watch_id = payload["watch_id"]
        doc = SourceDocument.model_validate(payload["document"])
    except Exception as exc:  # noqa: BLE001
        log.error("undecodable Pub/Sub message, acking to avoid a retry loop: %s", exc)
        return {"status": "dropped", "reason": "undecodable"}

    watch = d.store.get_watch(watch_id) or Watch(watch_id=watch_id, label=watch_id)

    result = await process_document(
        doc, watch,
        store=d.store, client=d.client, triage=d.triage, analyst=d.analyst,
        cfg=d.cfg, drafter=d.drafter,
    )

    if result.failed:
        # Non-2xx so Pub/Sub redelivers; repeated failures reach the DLQ.
        raise HTTPException(
            status_code=500, detail=f"{doc.source_id} failed at {result.outcome.stage}"
        )

    return {
        "document": doc.source_id,
        "stage": result.outcome.stage,
        "relevant": result.outcome.relevant,
        "docket": result.outcome.docket_id,
        "revision": result.outcome.revision,
        "drafted": result.drafted,
    }


@app.post("/tasks/run-inline")
async def run_inline(watch_id: str, max_docs: int = 10, analyze: bool = True) -> dict[str, Any]:
    """Run one watch start to finish in a single request.

    Bypasses Pub/Sub. Intended for verifying a fresh deployment and for demos,
    not for scheduled operation -- a long watch will approach the request timeout.
    """
    d = _deps()
    watch = d.store.get_watch(watch_id)
    if watch is None:
        raise HTTPException(status_code=404, detail=f"no watch {watch_id!r}")

    stats = await process_watch(
        watch,
        store=d.store, client=d.client, triage=d.triage, analyst=d.analyst,
        cfg=d.cfg, drafter=d.drafter, max_docs=max_docs, analyze=analyze,
    )
    return {
        "run_id": stats.run_id,
        "status": stats.status,
        "discovered": stats.discovered,
        "triaged": stats.triaged,
        "relevant": stats.relevant,
        "analyzed": stats.analyzed,
        "drafted": stats.drafted,
        "failed": stats.failed,
        "dockets": [
            {"docket_id": p.docket_id, "revision": p.revision} for p in stats.positions
        ],
    }
