<#
.SYNOPSIS
  One-shot, idempotent Google Cloud setup for Agenda Item 7.

.DESCRIPTION
  Run this AFTER `gcloud auth login` and after the project has a billing account
  attached. Safe to re-run; every step tolerates "already exists".

  Preflight aborts immediately if the project does not exist or has no billing,
  rather than emitting a cascade of downstream permission errors.

.EXAMPLE
  .\scripts\setup_gcp.ps1 -ProjectId agenda-item-7-8220
  .\scripts\setup_gcp.ps1 -ProjectId agenda-item-7-8220 -SkipPreflight
#>
param(
  [Parameter(Mandatory = $true)][string]$ProjectId,
  [string]$Region = "us-central1",
  [switch]$SkipPreflight
)

$ErrorActionPreference = "Continue"

# Prefer gcloud on PATH; fall back to the winget install location.
$gcloud = (Get-Command gcloud -ErrorAction SilentlyContinue).Source
if (-not $gcloud) {
  $fallback = "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
  if (Test-Path $fallback) { $gcloud = $fallback }
  else { Write-Host "gcloud not found. Open a NEW terminal or reinstall the SDK." -ForegroundColor Red; exit 1 }
}

function Step($msg) { Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "   [ok] $msg" -ForegroundColor Green }
function Note($msg) { Write-Host "   [--] $msg" -ForegroundColor DarkGray }
function Fail($msg) { Write-Host "   [!!] $($msg -replace '\s+', ' ')" -ForegroundColor Red }

# Run a gcloud command; classify by EXIT CODE first, then by message.
# Returns $true on success or benign-already-exists.
# NOTE: the parameter is CmdArgs, not Args -- $Args is a PowerShell automatic
# variable and naming a parameter after it silently empties the argument list.
function Invoke-Gcloud {
  param([string[]]$CmdArgs, [string]$SuccessMsg, [string]$ExistsPattern = "already exists|ALREADY_EXISTS|HTTPError 409")
  $out = & $gcloud @CmdArgs 2>&1 | Out-String
  $code = $LASTEXITCODE
  if ($code -eq 0) { Ok $SuccessMsg; return $true }
  if ($out -match $ExistsPattern) { Note "$SuccessMsg - already exists, skipping"; return $true }
  Fail $out.Trim()
  return $false
}

# ---------------------------------------------------------------
# Preflight. The whole point: catch a wrong/missing project id here
# instead of producing six confusing permission errors downstream.
# ---------------------------------------------------------------
if (-not $SkipPreflight) {
  Step "Preflight"

  & $gcloud projects describe $ProjectId --format="value(projectId)" 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Fail "Project '$ProjectId' does not exist or you lack access."
    Write-Host ""
    Write-Host "   Projects you can see:" -ForegroundColor Yellow
    & $gcloud projects list --format="table(projectId,name,lifecycleState)"
    Write-Host ""
    Write-Host "   Create it with:" -ForegroundColor Yellow
    Write-Host "     gcloud projects create $ProjectId --name=`"Agenda Item 7`"" -ForegroundColor White
    exit 1
  }
  Ok "project $ProjectId exists"

  $billing = & $gcloud billing projects describe $ProjectId --format="value(billingEnabled)" 2>&1 | Out-String
  if ($billing -match "True") {
    Ok "billing is enabled"
  } else {
    Fail "No billing account linked to $ProjectId. Vertex AI will not work without it."
    Write-Host ""
    Write-Host "   Your billing accounts:" -ForegroundColor Yellow
    & $gcloud billing accounts list
    Write-Host ""
    Write-Host "   Link one with:" -ForegroundColor Yellow
    Write-Host "     gcloud billing projects link $ProjectId --billing-account=ACCOUNT_ID" -ForegroundColor White
    Write-Host ""
    Write-Host "   (Pick an account whose OPEN column is True.)" -ForegroundColor DarkGray
    exit 1
  }
}

Step "Targeting project $ProjectId ($Region)"
& $gcloud config set project $ProjectId 2>$null | Out-Null
& $gcloud config set run/region $Region 2>$null | Out-Null
Ok "active project and default run region set"

# ---------------------------------------------------------------
# APIs. One call is far faster than one-at-a-time.
# ---------------------------------------------------------------
Step "Enabling APIs (this takes 1-2 minutes)"
$apis = @(
  "aiplatform.googleapis.com",        # Vertex AI / Gemini
  "firestore.googleapis.com",         # state + position tracker
  "run.googleapis.com",               # serverless hosting
  "pubsub.googleapis.com",            # async fan-out
  "cloudscheduler.googleapis.com",    # the heartbeat that makes it autonomous
  "cloudbuild.googleapis.com",        # source deploys (no local Docker needed)
  "artifactregistry.googleapis.com",  # images built by Cloud Build
  "storage.googleapis.com",           # fetched PDFs
  "secretmanager.googleapis.com",     # the regulations.gov key
  "logging.googleapis.com",
  "cloudtrace.googleapis.com"         # reasoning-chain traces
)
$null = Invoke-Gcloud -CmdArgs (@("services", "enable") + $apis) -SuccessMsg "$($apis.Count) APIs enabled"

# ---------------------------------------------------------------
# Firestore (Native mode) - watches, checkpoints, docket tracker.
# ---------------------------------------------------------------
Step "Creating Firestore database"
$null = Invoke-Gcloud `
  -CmdArgs @("firestore", "databases", "create", "--location=$Region", "--type=firestore-native") `
  -SuccessMsg "Firestore (native) in $Region"

# ---------------------------------------------------------------
# Storage for fetched source PDFs.
# ---------------------------------------------------------------
$bucket = "$ProjectId-docs"
Step "Creating bucket gs://$bucket"
$null = Invoke-Gcloud `
  -CmdArgs @("storage", "buckets", "create", "gs://$bucket", "--location=$Region", "--uniform-bucket-level-access") `
  -SuccessMsg "gs://$bucket"

# ---------------------------------------------------------------
# Pub/Sub - decouples discovery from analysis so a slow deep read
# never blocks the poller, and retries are free.
# ---------------------------------------------------------------
Step "Creating Pub/Sub topics"
foreach ($topic in @("agenda7-documents", "agenda7-documents-dlq")) {
  $null = Invoke-Gcloud -CmdArgs @("pubsub", "topics", "create", $topic) -SuccessMsg $topic
}

# ---------------------------------------------------------------
# Runtime service account, least privilege.
# ---------------------------------------------------------------
Step "Creating runtime service account"
$sa = "agenda7-runtime"
$saEmail = "$sa@$ProjectId.iam.gserviceaccount.com"
$saOk = Invoke-Gcloud `
  -CmdArgs @("iam", "service-accounts", "create", $sa, "--display-name=Agenda Item 7 runtime") `
  -SuccessMsg $saEmail

if ($saOk) {
  foreach ($role in @("roles/datastore.user", "roles/aiplatform.user", "roles/pubsub.publisher",
                      "roles/storage.objectAdmin", "roles/secretmanager.secretAccessor",
                      "roles/cloudtrace.agent")) {
    & $gcloud projects add-iam-policy-binding $ProjectId `
        --member="serviceAccount:$saEmail" --role=$role --condition=None 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Note "granted $role" } else { Fail "could not grant $role" }
  }
} else {
  Fail "skipping role grants because the service account was not created"
}

# ---------------------------------------------------------------
# Cost guardrail. $150 of credits is not much and overage is on you.
# ---------------------------------------------------------------
Step "Budget alert (do this manually, 60 seconds)"
Write-Host @"
     console.cloud.google.com  ->  Billing  ->  Budgets & alerts  ->  Create budget
     Scope: project $ProjectId   Amount: 50 USD   Alerts at 50% / 90% / 100%

   Also confirm your credit landed:
     Billing -> Credits
"@ -ForegroundColor Yellow

Step "Done"
Write-Host @"
   Next:
     1. gcloud auth application-default login      (local creds for the SDK)
     2. Put GOOGLE_CLOUD_PROJECT=$ProjectId in .env
     3. python scripts\list_models.py              (confirm real Gemini model ids)
     4. python scripts\probe_regulations.py --dump (confirm the API contract)
"@ -ForegroundColor Cyan

