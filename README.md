# Agenda Item 7

--NOTE: This is a copy of the original repository that I built to wipe some of my
commit history. Previous commits included some personal information, which is why
I copied over the project to this new repository. I have given collaborator access
to the 2 required emails for my original repository. --

A background agent that reads public regulatory dockets and government meeting
packets so a human does not have to, and makes sure the comment deadline is never
the thing that gets missed.

Nobody talks to this agent. It wakes on a schedule, decides what is worth reading,
reads it, folds what it learned into a running position tracker, and surfaces the
deadline. A person appears only at the very end, to approve a drafted comment.

Built for the **All Things Agentic Hackathon** - Taskmaster track.

![Agenda Item 7 architecture: Cloud Scheduler triggers a Cloud Run poller, which
discovers documents from Regulations.gov and publishes them to Pub/Sub; a second
Cloud Run endpoint processes one document at a time through triage, analyst, and
drafter agents on Vertex AI, writing to Firestore and Cloud Storage, and emitting
a calendar file, a digest, and a staged comment draft for human
review.](docs/architecture.svg)

## The friction

Regulations.gov posts thousands of documents a week. The handful that matter to
any given person are buried in hundreds of pages of scanned PDF, inside a comment
window that closes whether or not you noticed it opened. Keeping up is not a
reading problem, it is a monitoring problem, and monitoring is what humans are
worst at and schedulers are best at.

## What it does

1. **Watches.** You declare what you care about - a search term, an agency, a
   specific docket - and why it matters to you.
2. **Discovers.** A Cloud Scheduler heartbeat drives a checkpointed poll of each
   watch. No rescans, so the API quota lasts.
3. **Triages.** Gemini Flash judges relevance against your stated rationale and
   discards the large majority cheaply.
4. **Reads deeply.** Survivors go to Gemini Pro, which extracts positions,
   parties, deadlines, and whether this filing changes the prior understanding.
5. **Mutates the tracker.** Each analysis folds into one evolving record per
   docket with a revision counter - a running synthesis, not a pile of summaries.
6. **Acts.** Three outputs, none of which transmit anything:
   - **`deadlines.ics`** - every tracked deadline as an importable calendar file
     with reminders 14 and 3 days out. Works in Google Calendar, Outlook, or
     Apple Calendar.
   - **A markdown digest** - what changed, what is due, and *what was filtered
     out and why*, because a filter you cannot inspect is a filter you cannot
     trust.
   - **A staged comment draft** - for filings still inside their comment window.
     It makes concrete arguments and explicitly lists what it *cannot* know
     (your name, a local cost figure, a specific example) instead of inventing
     plausible filler.

**The agent never files anything.** There is no submission path anywhere in this
codebase. That is a design commitment, not unfinished work - an agent with
authority to enter text into a federal rulemaking record on someone's behalf is
the wrong shape for this problem.

See [docs/architecture.md](docs/architecture.md) for the diagram and the
engineering rationale, including the decisions that measurement forced.

## Tech stack

| Requirement | Used |
| --- | --- |
| Gemini 3.5+ | Gemini via **Vertex AI** - Flash for triage, Pro for deep reads |
| Google agent framework | **Google ADK 2.7** (`google-adk`) |
| Google Cloud infrastructure | **Cloud Run**, **Firestore**, **Pub/Sub**, **Cloud Scheduler**, Cloud Storage, Cloud Build, Cloud Trace |

## Data sources

All public. No private, proprietary, or employer data is used anywhere in this
project.

- [Regulations.gov API v4](https://open.gsa.gov/api/regulationsgov/) - federal
  dockets, filings, and comment deadlines
- FCC ECFS - *planned*
- Municipal agenda portals - *planned*

## Spin-up

### Prerequisites

- Python 3.11+ (developed on 3.14)
- [Google Cloud SDK](https://cloud.google.com/sdk/docs/install)
- A Google Cloud project with billing enabled
- A free [Regulations.gov API key](https://open.gsa.gov/api/regulationsgov/#getting-started)

### 1. Clone and install

```bash
git clone <your-repo-url> && cd agenda-item-7
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS / Linux:

```bash
source .venv/bin/activate
```

Then:

```bash
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Fill in `REGULATIONS_GOV_API_KEY` and `GOOGLE_CLOUD_PROJECT`.

### 3. Provision Google Cloud

```powershell
gcloud auth login
gcloud auth application-default login
.\scripts\setup_gcp.ps1 -ProjectId <your-project-id>
```

The script is idempotent - safe to re-run. It enables the required APIs and
creates the Firestore database, storage bucket, Pub/Sub topics, and a
least-privilege runtime service account.

### 4. Verify before building

Both scripts check reality instead of trusting configuration:

```bash
python scripts/list_models.py
```

Confirms which Gemini model IDs your project can actually reach and flags a
mismatch against `.env`. Model IDs move - set `TRIAGE_MODEL` and `ANALYST_MODEL`
to IDs that appear in this output.

```bash
python scripts/probe_regulations.py --dump
```

Exercises every upstream filter in isolation, prints the real response shape, and
reports your rate-limit headroom.

```bash
python scripts/smoke_test.py
```

Live end-to-end check: pulls real documents, writes them to Firestore, proves
dedup works, and folds two analyses into one docket to show the tracker mutating
and the revision incrementing. Makes no model calls, so it costs nothing.
Clean up after it with `python scripts/smoke_test.py --cleanup`.

### 5. Run it

Cheap first - triage only, no deep reads:

```bash
python scripts/run_once.py --watch-id fcc-feed --agency FCC --keywords "pole attachment,make-ready timelines,broadband deployment" --lookback-days 30 --max-docs 10 --no-analyze
```

Then the full pipeline, including a staged comment draft:

```bash
python scripts/run_once.py --watch-id fcc-feed --lookback-days 60 --max-docs 25 --draft
```

Artifacts land in `out/`. Inspect everything the agent knows at any time:

```bash
python scripts/show_state.py --drafts --rejected
```

Clear state for a clean run or before recording a demo:

```bash
python scripts/reset_state.py --yes
```

### 6. Deploy

The service in `src/agenda7/service.py` is what makes this autonomous: Cloud
Scheduler drives a poll, Pub/Sub fans documents out, and each document is
processed by its own retryable request.

**Step-by-step instructions: [docs/DEPLOY.md](docs/DEPLOY.md)** - Secret Manager,
Cloud Run, Pub/Sub with a dead-letter queue, Cloud Scheduler, verification, cost
controls, teardown, and a troubleshooting table.

**Picking this up on a different machine?** Start with
[docs/HANDOFF.md](docs/HANDOFF.md) - project constants, what is already
provisioned, what is not, and an honest list of known gaps.

## Cost control

A $150 credit does not survive careless use, and overage is the entrant's
responsibility. This project keeps costs down by triaging on Flash before paying
for Pro, scaling Cloud Run to zero, capping documents per run
(`MAX_DOCS_PER_RUN`), and checkpointing instead of rescanning. Set a budget alert
at $50 before you deploy anything.

## Disclosure

Written during the hackathon submission period with AI coding assistance
(Claude Code), which is permitted under the contest rules. No pre-existing code
was incorporated.

## License

MIT
