# Deploying Agenda Item 7 to Google Cloud

Follow this top to bottom on a machine with the Google Cloud SDK. Every command
is copy-pasteable. After each step there is a **Check** telling you what success
looks like, so you never advance on a broken foundation.

Two honest notes before you start:

- The application code in this guide is tested. The service runs, all endpoints
  respond, and the full pipeline has been verified end to end locally against
  live Google Cloud services.
- The **deployment commands themselves have not been run**. They are written from
  the documented behaviour of each product. Expect one or two to need a small
  adjustment, and use the Troubleshooting table at the end when something bites.

Budget: this deployment costs roughly a few dollars a month at a daily schedule,
dominated by the deep reads. Cloud Run scales to zero, so idle time is free.

---

## What you are building

```
Cloud Scheduler  --(daily, OIDC)-->  POST /tasks/poll
                                          |
                                     discovers new documents
                                          |
                                     publishes one message each
                                          v
                                   Pub/Sub: agenda7-documents
                                          |
                                     push subscription (OIDC)
                                          v
                                     POST /tasks/document  --> Firestore
                                          |                    Cloud Storage
                                     5 failures                Vertex AI
                                          v
                                   agenda7-documents-dlq
```

The poller does no model work - it discovers and queues, then returns. Each
document is processed by its own request, so one slow filing cannot stall
discovery and one bad filing retries alone.

---

## Step 0 - Prerequisites

You need:

- [ ] Google Cloud SDK installed (`gcloud --version`)
- [ ] Python 3.11+ (`python --version`)
- [ ] Your Regulations.gov API key
- [ ] The project `agenda-item-7-8220` with billing enabled

Set your shell variables once. Everything below reuses them.

```bash
export PROJECT_ID=agenda-item-7-8220
```

```bash
export PROJECT_NUMBER=876473976323
```

```bash
export REGION=us-central1
```

On Windows PowerShell use `$env:PROJECT_ID = "agenda-item-7-8220"` instead, and
`$env:PROJECT_ID` where this guide writes `$PROJECT_ID`.

**Check:** `gcloud config get-value project` prints `agenda-item-7-8220`. If not:

```bash
gcloud config set project $PROJECT_ID
```

---

## Step 1 - Get the code running locally first

Do not skip this. If the pipeline does not work locally, it will not work on
Cloud Run, and debugging is far harder there.

```bash
git clone https://github.com/Blump1/agenda-item-7.git
```

```bash
cd agenda-item-7 && python -m venv .venv
```

Activate it - PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS / Linux:

```bash
source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Edit `.env` and set `REGULATIONS_GOV_API_KEY` and
`GOOGLE_CLOUD_PROJECT=agenda-item-7-8220`. Leave
`GOOGLE_CLOUD_LOCATION=global` alone - Gemini 3.x is only served there.

Authenticate:

```bash
gcloud auth login
```

```bash
gcloud auth application-default login
```

Now verify, in this order:

```bash
python scripts/list_models.py
```

**Check:** both models report `[SERVES]`. If either says `FAILS`, fix that before
continuing - nothing downstream can work.

```bash
python scripts/probe_regulations.py
```

**Check:** `ALL CHECKS PASSED`.

```bash
python scripts/run_once.py --watch-id fcc-feed --agency FCC --keywords "pole attachment,make-ready timelines,broadband deployment" --lookback-days 30 --max-docs 10 --no-analyze
```

**Check:** documents are discovered and triaged, most rejected with readable
reasons. This is the whole system working minus the expensive parts.

---

## Step 2 - Put the API key in Secret Manager

Do not pass the key as an environment variable on the deploy command line; it
would land in your shell history and in the service's revision metadata.

```bash
gcloud secrets create regulations-gov-api-key --replication-policy=automatic --data-file=-
```

The command waits for input. **Paste your key, press Enter, then Ctrl+D**
(Ctrl+Z then Enter on Windows). Nothing is echoed.

**Check:**

```bash
gcloud secrets versions list regulations-gov-api-key
```

One `ENABLED` version.

Grant the runtime service account read access to just this secret:

```bash
gcloud secrets add-iam-policy-binding regulations-gov-api-key --member="serviceAccount:agenda7-runtime@$PROJECT_ID.iam.gserviceaccount.com" --role=roles/secretmanager.secretAccessor
```

---

## Step 3 - Deploy to Cloud Run

Source-based deploy: Cloud Build builds the container for you, so **no local
Docker is needed**. The `Procfile` in the repo root tells it how to start.

```bash
gcloud run deploy agenda7 --source . --region $REGION --no-allow-unauthenticated --service-account agenda7-runtime@$PROJECT_ID.iam.gserviceaccount.com --set-secrets REGULATIONS_GOV_API_KEY=regulations-gov-api-key:latest --set-env-vars GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,GCP_REGION=$REGION,GOOGLE_GENAI_USE_VERTEXAI=true,GCS_BUCKET=$PROJECT_ID-docs,TRIAGE_MODEL=gemini-3.5-flash-lite,ANALYST_MODEL=gemini-3.7-flash,MAX_DOCS_PER_RUN=25,INITIAL_LOOKBACK_DAYS=7,ENABLE_DRAFTS=true --memory 1Gi --cpu 1 --timeout 900 --max-instances 3 --min-instances 0
```

The important flags and why:

| Flag | Why |
| --- | --- |
| `--no-allow-unauthenticated` | Nothing here should be publicly callable. Scheduler and Pub/Sub authenticate with OIDC. |
| `--min-instances 0` | Scale to zero. You pay nothing while idle. |
| `--max-instances 3` | A cost ceiling. A runaway loop cannot spawn 100 containers of deep reads. |
| `--timeout 900` | A long filing can take minutes to read. |
| `--set-secrets` | Key injected at runtime, never in revision config. |
| `GOOGLE_CLOUD_LOCATION=global` | Gemini 3.x is not served from regional endpoints. |

Capture the URL:

```bash
export SERVICE_URL=$(gcloud run services describe agenda7 --region $REGION --format='value(status.url)')
```

**Check:**

```bash
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" $SERVICE_URL/healthz
```

You should get JSON with `"ok": true` and `"vertex_location": "global"`. A 403
means the token is missing; a 500 means the container failed to start - see
Troubleshooting.

---

## Step 4 - Smoke-test the deployed service

Before wiring any automation, prove the deployed container can do the work.

```bash
curl -X POST -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$SERVICE_URL/tasks/run-inline?watch_id=fcc-feed&max_docs=5&analyze=false"
```

**Check:** JSON with `"status": "ok"` and non-zero `discovered`. If `discovered`
is 0, that is fine - it means the checkpoint has already consumed recent
documents, not that anything is broken.

---

## Step 5 - Wire Pub/Sub

One service account acts as the OIDC identity for both Pub/Sub push and Cloud
Scheduler. Two would be marginally tighter, but this keeps the moving parts
countable and neither caller needs anything beyond `run.invoker`.

```bash
gcloud iam service-accounts create agenda7-invoker --display-name="Agenda Item 7 OIDC invoker"
```

```bash
gcloud run services add-iam-policy-binding agenda7 --region $REGION --member="serviceAccount:agenda7-invoker@$PROJECT_ID.iam.gserviceaccount.com" --role=roles/run.invoker
```

Let the Pub/Sub service agent write to the dead-letter topic:

```bash
gcloud pubsub topics add-iam-policy-binding agenda7-documents-dlq --member="serviceAccount:service-$PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com" --role=roles/pubsub.publisher
```

Create the push subscription:

```bash
gcloud pubsub subscriptions create agenda7-documents-push --topic=agenda7-documents --push-endpoint="$SERVICE_URL/tasks/document" --push-auth-service-account="agenda7-invoker@$PROJECT_ID.iam.gserviceaccount.com" --ack-deadline=600 --dead-letter-topic=agenda7-documents-dlq --max-delivery-attempts=5 --min-retry-delay=30s --max-retry-delay=600s
```

Then let the service agent manage that subscription's dead-lettering:

```bash
gcloud pubsub subscriptions add-iam-policy-binding agenda7-documents-push --member="serviceAccount:service-$PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com" --role=roles/pubsub.subscriber
```

**Check:**

```bash
gcloud pubsub subscriptions describe agenda7-documents-push --format="yaml(pushConfig,deadLetterPolicy,ackDeadlineSeconds)"
```

You should see your endpoint, the OIDC service account, `maxDeliveryAttempts: 5`,
and `ackDeadlineSeconds: 600`.

`--ack-deadline=600` matters: 600 seconds is the maximum, and a deep read of a
long filing can take minutes. Too short and Pub/Sub redelivers a document that
is still being processed, producing duplicate work and duplicate revisions.

---

## Step 6 - Wire Cloud Scheduler

This is the step that makes the agent autonomous. Everything before it still
required you to press something.

```bash
gcloud scheduler jobs create http agenda7-daily-poll --location=$REGION --schedule="0 7 * * *" --time-zone="America/Phoenix" --uri="$SERVICE_URL/tasks/poll" --http-method=POST --oidc-service-account-email="agenda7-invoker@$PROJECT_ID.iam.gserviceaccount.com" --oidc-token-audience="$SERVICE_URL" --attempt-deadline=600s
```

`0 7 * * *` is 7:00 AM daily. `America/Phoenix` has no daylight saving, so the
time never shifts - change both to taste.

Trigger it immediately rather than waiting a day:

```bash
gcloud scheduler jobs run agenda7-daily-poll --location=$REGION
```

**Check:**

```bash
gcloud scheduler jobs describe agenda7-daily-poll --location=$REGION --format="yaml(state,status,lastAttemptTime)"
```

`state: ENABLED` and a recent `lastAttemptTime` with no error.

---

## Step 7 - Verify it actually ran without you

This is the part that matters, and the part worth filming.

Watch the logs:

```bash
gcloud run services logs read agenda7 --region $REGION --limit 60
```

Look for `poll: <watch> discovered=N queued=N`, then per-document lines from
`/tasks/document`.

Confirm the queue drained:

```bash
gcloud pubsub subscriptions describe agenda7-documents-push --format="value(name)" && gcloud monitoring time-series list --filter='metric.type="pubsub.googleapis.com/subscription/num_undelivered_messages"' --format="value(points[0].value.int64Value)" 2>/dev/null | head -1
```

Confirm nothing dead-lettered:

```bash
gcloud pubsub topics list-subscriptions agenda7-documents-dlq
```

Confirm real state changed - this is the proof of action:

```bash
python scripts/show_state.py
```

Or in the console: **Firestore → Data → `dockets`** should show documents with
`revision` values, and **Cloud Storage → `agenda-item-7-8220-docs`** should
contain staged PDFs under `sources/`.

---

## Step 8 - Capture proof for the demo video

The contest requires visible evidence the backend runs on Google Cloud. Capture
these, in this order, in one unbroken take if you can:

1. **Cloud Scheduler** page showing the job and its last run time
2. **Trigger it live** - `gcloud scheduler jobs run agenda7-daily-poll --location=$REGION`
3. **Cloud Run logs** streaming as the poll discovers and queues documents
4. **Pub/Sub** subscription showing messages delivered
5. **Firestore** `dockets` document incrementing its `revision`
6. **Cloud Storage** with the newly staged PDF
7. **The `.ics` file and the staged comment draft** - the human-facing output
8. The Cloud Run **`.run.app` URL visible in the address bar**

Run `python scripts/reset_state.py --yes` first so the run visibly creates
things rather than saying "already seen".

---

## Cost control

```bash
gcloud run services update agenda7 --region $REGION --min-instances 0
```

Already the default above, but confirm it - a stray `--min-instances 1` bills
continuously.

To pause everything without deleting it:

```bash
gcloud scheduler jobs pause agenda7-daily-poll --location=$REGION
```

Deep reads dominate cost. To make a run cheaper, lower `MAX_DOCS_PER_RUN` or
turn drafting off:

```bash
gcloud run services update agenda7 --region $REGION --update-env-vars MAX_DOCS_PER_RUN=10,ENABLE_DRAFTS=false
```

## Teardown

In this order:

```bash
gcloud scheduler jobs delete agenda7-daily-poll --location=$REGION --quiet
```

```bash
gcloud pubsub subscriptions delete agenda7-documents-push --quiet
```

```bash
gcloud run services delete agenda7 --region $REGION --quiet
```

Leave Firestore and the bucket if you want to keep the evidence; they cost
almost nothing at this size.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `404 NOT_FOUND` naming a publisher model | Gemini 3.x is not served from regional endpoints | `GOOGLE_CLOUD_LOCATION=global`. Confirm with `python scripts/list_models.py` |
| Container fails to start, logs show `ModuleNotFoundError: agenda7` | `src` layout not on the path | Confirm the root `Procfile` has `PYTHONPATH=src` |
| `403` calling any endpoint | Missing or wrong OIDC token | Include `Authorization: Bearer $(gcloud auth print-identity-token)`. For Scheduler, `--oidc-token-audience` must equal the service URL exactly |
| `RuntimeError: REGULATIONS_GOV_API_KEY is not set` in Cloud Run | Secret not mounted | Re-check `--set-secrets`, and that `agenda7-runtime` has `secretmanager.secretAccessor` on the secret |
| `SessionNotFoundError` | An ADK session was not created before `run_async` | Already handled in `StructuredAgent`; if you wrote new agent code, create the session first |
| Documents download as `403 Forbidden` | `downloads.regulations.gov` rejects User-Agents without a `Mozilla/5.0` token | Already handled in `sources/fetch.py`; do not strip that header |
| Same document processed repeatedly, revision climbing | Ack deadline shorter than processing time | `--ack-deadline=600` (the maximum) |
| Messages piling up in the DLQ | A document fails consistently | Read the Cloud Run log line for that document id; the stage name tells you where it died |
| `discovered=0` every run | The checkpoint has advanced past everything | Expected. To backfill, run locally with `--lookback-days N` |
| Scheduler shows `UNKNOWN` / permission denied | Invoker lacks `run.invoker` | Re-run the `add-iam-policy-binding` in Step 5 |

---

## What is deliberately not here

**Nothing in this system transmits anything.** There is no email sender, no
Calendar API write, and no path that files a comment. The actions produce
artifacts - an `.ics` file, a markdown digest, a staged draft - for a human to
review and act on.

If you later want delivery, the honest additions are:

- **Email:** a `notify` action posting the digest through an email API, with the
  recipient in config rather than inferred.
- **Calendar:** swap `.ics` generation for the Google Calendar API. This needs an
  OAuth consent flow for your own account, which is why it is not the default.
- **Filing:** intentionally out of scope. Regulations.gov has a submission API,
  and wiring an autonomous agent to it is a decision that deserves more thought
  than a hackathon deadline allows.
