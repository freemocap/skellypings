# Skellypings

A lightweight telemetry system for collecting anonymous usage pings from desktop applications. Events are sent to a small server running on Google Cloud, stored in Firestore, and backed up daily to plain JSON files so you always have a portable copy of your data.

## Repository Structure

```
├── pyproject.toml            # Client package (pip-installable, depends only on `requests`)
├── skellypings/              # Client package source
│   ├── __init__.py
│   └── telemetry_client.py   # Batched async telemetry client
├── server/                   # Cloud Run server (deployed to GCP)
│   ├── main.py               # FastAPI telemetry ingestion service
│   ├── pyproject.toml         # Server dependencies (fastapi, firestore, etc.)
│   ├── Dockerfile
│   └── uv.lock
├── infra/                    # Terraform config (optional deployment method)
│   ├── main.tf
│   └── terraform.tfvars.example
├── test_ping.py              # Quick script to test your deployment
├── env.example               # Template for .env file
├── .gitignore
├── LICENSE
└── README.md
```

This repo contains two separate Python projects:

- **`skellypings/`** (root `pyproject.toml`) — the client library. This is what your desktop app depends on. It only requires `requests`.
- **`server/`** (its own `pyproject.toml`) — the Cloud Run service. This gets deployed to GCP and has server-only dependencies (Firestore, Cloud Storage, etc.).

## Architecture

```
Desktop App (Python backend)
  → TelemetryClient (batched, async HTTP POST)
    → Cloud Run service (FastAPI)
      → Firestore (primary event storage)
      → Cloud Storage (daily JSONL backups — your escape hatch)

Cloud Scheduler
  → triggers POST /backup daily at 3 AM UTC
    → Cloud Run reads new events from Firestore
    → writes them as .jsonl to Cloud Storage
```

## What Each Piece Does

| Component | What it is | Why we're using it |
|---|---|---|
| **Cloud Run** | A service that runs your Docker container and gives it a public URL. It turns off when nobody is calling it and turns on when a request comes in. | Hosts the telemetry API. You don't manage a server. |
| **Firestore** | A NoSQL document database. You write JSON objects to it, you read them back. No tables, no schema, no SQL. | Stores the telemetry events. Lowest-friction database in GCP. |
| **Cloud Storage** | A place to store files (like S3 on AWS, or Dropbox but for code). | Holds the daily JSONL backup files — plain text, fully portable. |
| **Cloud Scheduler** | A cron job service. You say "run this HTTP request at 3 AM every day" and it does. | Triggers the daily backup. |

## Client Integration

Install the client as a dependency from GitHub:

```toml
# In your app's pyproject.toml dependencies:
"skellypings",

# In [tool.uv.sources]:
[tool.uv.sources]
skellypings = { git = "https://github.com/freemocap/skellypings" }
```

Then use it in your Python backend:

```python
from pathlib import Path
from skellypings import TelemetryClient

telemetry = TelemetryClient(
    server_url="https://your-cloud-run-url.run.app",
    secret="your-64-char-secret",
    app_version="1.0.0",
    user_id_file=Path.home() / "my_app_data" / "telemetry_uid",
)

# Track events anywhere in your code
telemetry.track("app_opened")
telemetry.track("feature_used", payload={"feature": "export_csv", "row_count": 1500})
telemetry.track("error", payload={"type": "ValueError", "message": "invalid input"})
```

The `user_id_file` is where the client stores a persistent anonymous user ID (a random hex string). On first run it generates one; on subsequent runs it reuses it. You decide where this file lives in your app's data directory.

Events accumulate in memory and are flushed every 60 seconds or every 50 events (whichever comes first) on a background thread. Telemetry failures log a warning but never crash your app.

On shutdown, `telemetry.shutdown()` flushes remaining events. This is also registered with `atexit` automatically, so it runs when your app exits.

## Billing: What Will This Cost?

**The goal is $0/month.** Here's the honest breakdown.

### The Bad News First

**Cloud Run requires a billing account.** This means you must enter a credit card to deploy to Cloud Run, even if your usage is within the free tier. There is no way around this. Google uses it for identity verification and to charge you if you exceed free limits.

### The Good News

All four services have free tiers that reset monthly. For a desktop app's telemetry (a few hundred to a few thousand events per day), you will stay well within all of these:

| Service | Free Tier | What would exceed it |
|---|---|---|
| **Cloud Run** | 2M requests/month, 180K vCPU-seconds, 360K GiB-seconds | Hundreds of thousands of active users |
| **Firestore** | 1 GiB storage, 20K writes/day, 50K reads/day | Tens of thousands of events per day |
| **Cloud Storage** | 5 GB storage | Years of daily backups |
| **Cloud Scheduler** | 3 free jobs | You're using 1 |

### How to Guarantee $0

**If you exceed the free tier, Cloud Run and Firestore will charge your credit card. They do NOT shut off — they bill you.** This is different from some services that hard-stop at the free limit.

To protect yourself:

1. **Set a billing budget alert** (covered in the setup instructions below) — Google will email you when spending approaches a threshold you define
2. **Set up automatic billing disable** — you can configure a Cloud Function that automatically disables billing if costs exceed $0. Google documents this at: https://cloud.google.com/billing/docs/how-to/disable-billing-with-notifications
3. **Set Cloud Run max instances to 1** (already done in our config) — this caps how much compute you can possibly use

Realistically: for a telemetry endpoint receiving fewer than 10,000 events per day, you will not be charged. But I want you to understand the mechanism so there are no surprises.

## Security: How Requests Are Authenticated

The telemetry endpoint is publicly reachable, but authenticity is established with an **HMAC-SHA256 signature** on every request. Here's how it works:

1. You generate a random secret string (a long hex string) when you set up the system
2. The desktop app and the server both know this secret
3. When the desktop app sends telemetry events, it computes a cryptographic hash of the request body using the secret as a key, and sends that hash in an HTTP header (`X-Telemetry-Signature`)
4. The server independently computes the same hash and compares. Only requests whose signature matches are treated as authentic telemetry.

This means:
- The secret never travels over the wire — only the hash does
- Only your signed apps produce authentic telemetry; a valid signature can't be forged without the secret
- If the secret is ever compromised, you just rotate it (change the env var on Cloud Run and in your app)

The endpoint is additionally protected against abuse and runaway cost by a per-IP rate limit and request-size caps, so a flood of junk traffic is cheaply rejected before it can reach the database.

The secret is stored as an environment variable on Cloud Run. It's not in your code or your repo.

## Prerequisites

All three deployment methods require:

1. **A Google account** — any Gmail or Google Workspace account
2. **A GCP project** — a container that holds all your cloud resources (created during setup)
3. **A billing account** — requires a credit card, but you won't be charged within free tier limits

Generate a shared secret now and save it somewhere safe (a password manager, a sticky note, whatever):

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

This prints a 64-character hex string like `a3f8b2c1d4e5...`. Save it. You'll need it twice: once for the server config and once in your desktop app.

## Deployment Options

There are three ways to set everything up. They all produce the exact same result — pick based on your comfort level:

| Method | Best for | What you need installed |
|---|---|---|
| **[Option A: CLI](#option-a-cli-gcloud-commands)** | People comfortable with a terminal | `gcloud` CLI, `uv` |
| **[Option B: Web Console](#option-b-gcp-web-console-click-through)** | People who prefer clicking buttons in a browser | Just a web browser |
| **[Option C: Terraform](#option-c-terraform-infrastructure-as-code)** | People who want repeatable, version-controlled infra | `terraform`, `gcloud` CLI |

---

## Option A: CLI (`gcloud` commands)

You run commands in your terminal. Each step is one command.

### A1. Install the `gcloud` CLI

The `gcloud` CLI is Google's command-line tool for managing GCP resources. Install it:

- **macOS**: `brew install google-cloud-cli`
- **Linux**: https://cloud.google.com/sdk/docs/install#linux
- **Windows**: https://cloud.google.com/sdk/docs/install#windows

After installing, log in:

```bash
gcloud auth login
```

This opens a browser window where you sign into your Google account.

### A2. Install `uv`

`uv` is a fast Python package manager. We use it to manage the server's dependencies.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### A3. Create a GCP project

A "project" is just a container that groups your cloud resources together. Pick a globally unique ID (lowercase letters, numbers, hyphens only):

```bash
gcloud projects create your-project-id
gcloud config set project your-project-id
```

Replace `your-project-id` with whatever you want (e.g., `myapp-telemetry`).

### A4. Link a billing account

If you don't have a billing account yet, create one at https://console.cloud.google.com/billing. You'll need a credit card.

Then link it to your project:

```bash
# List your billing accounts to find the account ID
gcloud billing accounts list

# Link it (replace ACCOUNT_ID with the one from the list above)
gcloud billing projects link your-project-id --billing-account=ACCOUNT_ID
```

### A5. Set environment variables

These are used by the commands below. Set them all now:

```bash
export PROJECT_ID="your-project-id"
export REGION="us-east1"
export SKELLYPINGS_SECRET="paste-your-64-char-secret-here"
export BACKUP_BUCKET="${PROJECT_ID}-telemetry-backups"
```

Note: the backup bucket name must be globally unique across all of Google Cloud. Prefixing it with your project ID is a simple way to ensure that.

### A6. Enable APIs

Google Cloud requires you to explicitly "turn on" each service before using it. This is a one-time step:

```bash
gcloud services enable \
  run.googleapis.com \
  firestore.googleapis.com \
  storage.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com
```

### A7. Create Firestore database

```bash
gcloud firestore databases create --location=$REGION
```

### A8. Create Cloud Storage bucket

```bash
gcloud storage buckets create gs://$BACKUP_BUCKET --location=$REGION
```

### A9. Generate the uv lockfile

The Dockerfile needs a `uv.lock` file. Generate it and commit it:

```bash
cd server
uv lock
cd ..
git add server/uv.lock
git commit -m "add uv lockfile"
```

### A10. Deploy to Cloud Run

```bash
cd server

gcloud run deploy telemetry \
  --source . \
  --region $REGION \
  --allow-unauthenticated \
  --max-instances 1 \
  --set-env-vars "SKELLYPINGS_SECRET=$SKELLYPINGS_SECRET,BACKUP_BUCKET=$BACKUP_BUCKET"

cd ..
```

This builds your Docker container in the cloud and deploys it. It takes a few minutes the first time.

When it finishes, it prints a URL like `https://telemetry-xxxxx-ue.a.run.app`. Save this — it's your server's address.

```bash
export SERVICE_URL="https://telemetry-xxxxx-ue.a.run.app"  # paste yours here
```

### A11. Set up Cloud Scheduler for daily backups

This creates a cron job that hits your `/backup` endpoint every day at 3 AM UTC.

```bash
# Create a service account (an identity for the scheduler to use)
gcloud iam service-accounts create telemetry-scheduler \
  --display-name="Telemetry Backup Scheduler"

# Give it permission to call your Cloud Run service
gcloud run services add-iam-policy-binding telemetry \
  --region=$REGION \
  --member="serviceAccount:telemetry-scheduler@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

# Compute the HMAC signature for the backup endpoint.
# The backup endpoint expects a signature computed over the literal string "backup".
BACKUP_SIGNATURE=$(python3 -c "
import hmac, hashlib
print(hmac.new('${SKELLYPINGS_SECRET}'.encode(), b'backup', hashlib.sha256).hexdigest())
")

# Create the scheduled job
gcloud scheduler jobs create http telemetry-daily-backup \
  --location=$REGION \
  --schedule="0 3 * * *" \
  --time-zone="UTC" \
  --uri="${SERVICE_URL}/backup" \
  --http-method=POST \
  --headers="X-Telemetry-Signature=${BACKUP_SIGNATURE}" \
  --oidc-service-account-email="telemetry-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
```

### A12. Set up a billing budget alert

This emails you if spending approaches a threshold. Set it to $1 so you get warned long before any real charge:

```bash
# This is easier to do in the web console — see Option B, step B9
# But here's the gcloud way:
gcloud billing budgets create \
  --billing-account=ACCOUNT_ID \
  --display-name="Telemetry Budget" \
  --budget-amount=1.00USD \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 \
  --threshold-rule=percent=1.0
```

### A13. Verify everything works

```bash
# Health check — should return {"status": "ok"}
curl $SERVICE_URL/health

# Send a test event
BODY='{"events":[{"event_type":"test","app_name":"test","app_version":"0.0.1","os_platform":"manual","user_id":"test","timestamp":0,"payload":{}}]}'

SIG=$(python3 -c "
import hmac, hashlib
body = '${BODY}'.encode()
print(hmac.new('${SKELLYPINGS_SECRET}'.encode(), body, hashlib.sha256).hexdigest())
")

curl -X POST $SERVICE_URL/events \
  -H "Content-Type: application/json" \
  -H "X-Telemetry-Signature: $SIG" \
  -d "$BODY"

# Should return {"stored": 1}
```

---

## Option B: GCP Web Console (click-through)

Everything in a browser. No terminal needed (except for one step).

### B1. Create a GCP project

1. Go to https://console.cloud.google.com
2. Click the project dropdown at the top of the page (it might say "Select a project" or show an existing project name)
3. Click **New Project**
4. Enter a project name (e.g., `myapp-telemetry`)
5. Click **Create**
6. Make sure the new project is selected in the dropdown

### B2. Set up billing

1. Go to https://console.cloud.google.com/billing
2. If you don't have a billing account, click **Create Account** and follow the prompts (you'll need a credit card)
3. Once you have a billing account, go to https://console.cloud.google.com/billing/linkedaccount
4. Click **Link a billing account** and select your billing account

### B3. Enable APIs

1. Go to https://console.cloud.google.com/apis/library
2. In the search bar, search for each of these and click **Enable** on each one:
   - `Cloud Run Admin API`
   - `Cloud Firestore API`
   - `Cloud Storage` (usually already enabled)
   - `Cloud Scheduler API`
   - `Cloud Build API`

### B4. Create Firestore database

1. Go to https://console.cloud.google.com/firestore
2. Click **Create Database**
3. Select **Native mode** (not Datastore mode)
4. Choose a location: `us-east1` (or your preferred region — just be consistent)
5. Click **Create**

### B5. Create Cloud Storage bucket

1. Go to https://console.cloud.google.com/storage/browser
2. Click **Create** (the big blue button at the top)
3. **Name**: something globally unique, e.g., `myapp-telemetry-backups`
4. **Location type**: Region
5. **Region**: same region you chose for Firestore
6. Leave everything else as defaults
7. Click **Create**
8. If it asks about public access prevention, leave it enforced (the default)

### B6. Deploy to Cloud Run via GitHub

This connects your GitHub repo so that Cloud Run automatically redeploys whenever you push to `main`.

**First**: make sure you've pushed this repo to GitHub, and that the `server/uv.lock` file exists. Generate it locally first:

```bash
cd server && uv lock && cd ..
git add -A && git commit -m "initial commit" && git push
```

**Then**:

1. Go to https://console.cloud.google.com/run
2. Click **Create Service**
3. Select **Continuously deploy from a repository**
4. Click **Set up with Cloud Build**
5. Authenticate with GitHub and select your repository
6. Configure the build:
   - **Branch**: `^main$` (or whatever your default branch is)
   - **Build type**: **Dockerfile**
   - **Source location**: `/server/Dockerfile`
7. Click **Save**
8. Back on the Cloud Run page, fill in:
   - **Service name**: `telemetry`
   - **Region**: same region as Firestore
   - **Authentication**: select **Allow unauthenticated invocations**
9. Expand **Container(s), Volumes, Networking, Security**
10. Under **Container** → **Settings** → **Environment variables**, click **Add Variable** twice:
    - Name: `SKELLYPINGS_SECRET`, Value: your 64-character secret
    - Name: `BACKUP_BUCKET`, Value: your bucket name from step B5
11. Under **Container** → **Settings** → **Resources**:
    - Memory: `256 MiB`
    - CPU: `1`
12. Under **Revision scaling** (or **Autoscaling**):
    - Min instances: `0`
    - Max instances: `1`
13. Click **Create**

Wait for the deployment to finish. The service URL appears at the top of the details page (something like `https://telemetry-xxxxx-ue.a.run.app`). Save this.

### B7. Create a service account for Cloud Scheduler

1. Go to https://console.cloud.google.com/iam-admin/service-accounts
2. Click **Create Service Account**
3. **Service account name**: `telemetry-scheduler`
4. Click **Create and Continue**
5. In the **Grant this service account access to project** step, search for the role: **Cloud Run Invoker**
6. Select it and click **Continue**
7. Click **Done**

### B8. Set up Cloud Scheduler

First, compute the backup signature locally:

```bash
python3 -c "import hmac,hashlib; print(hmac.new(b'YOUR_SECRET_HERE', b'backup', hashlib.sha256).hexdigest())"
```

Replace `YOUR_SECRET_HERE` with your actual secret. Copy the output.

Then:

1. Go to https://console.cloud.google.com/cloudscheduler
2. Click **Create Job**
3. **Name**: `telemetry-daily-backup`
4. **Region**: same region
5. **Frequency**: `0 3 * * *`
6. **Timezone**: `UTC`
7. Click **Continue**
8. **Target type**: HTTP
9. **URL**: `https://your-cloud-run-url.run.app/backup` (paste your actual URL)
10. **HTTP method**: POST
11. Click **Show More** to expand headers
12. Add a header: key = `X-Telemetry-Signature`, value = the signature you computed above
13. Under **Auth header**: select **Add OIDC token**
14. **Service account**: select `telemetry-scheduler@your-project-id.iam.gserviceaccount.com`
15. Click **Create**

### B9. Set up a billing budget alert

1. Go to https://console.cloud.google.com/billing/budgets
2. Click **Create Budget**
3. **Name**: `Telemetry Budget`
4. **Amount**: `$1`
5. Under **Thresholds**, add alerts at 50%, 90%, and 100%
6. Make sure your email is listed under notifications
7. Click **Finish**

---

## Option C: Terraform (infrastructure as code)

Terraform lets you define all your cloud resources in a file and create them with one command. If you ever need to recreate everything (or tear it down), it's a single command.

### C1. Install Terraform

- **macOS**: `brew install terraform`
- **Linux / Windows**: https://developer.hashicorp.com/terraform/downloads

### C2. Install and authenticate `gcloud`

Same as steps A1 and A4 — install the CLI, log in, and make sure you have a billing account.

Then authenticate for Terraform:

```bash
gcloud auth application-default login
```

### C3. Configure variables

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars`:

```hcl
project_id         = "your-gcp-project-id"
region             = "us-east1"
skellypings_secret = "your-64-char-secret"
backup_bucket_name = "your-project-id-telemetry-backups"
```

### C4. Deploy everything

```bash
terraform init    # downloads the Google Cloud provider plugin
terraform plan    # shows you exactly what will be created (review this!)
terraform apply   # type "yes" to confirm
```

This creates: the Firestore database, the Cloud Storage bucket, the Cloud Run service definition, the service account, and the Cloud Scheduler job.

### C5. Deploy the actual container

Terraform creates the Cloud Run service but uses a placeholder container image. You need to deploy your real code.

**Option 1 — Connect GitHub** (recommended): Follow steps B6.1–B6.13 above in the Cloud Run web console to set up continuous deployment.

**Option 2 — Deploy from terminal**:
```bash
cd server
uv lock  # if you haven't already
gcloud run deploy telemetry --source . --region us-east1
```

### C6. Set the scheduler signature

Terraform can't compute the HMAC at plan time, so you need to update the scheduler header manually:

```bash
BACKUP_SIGNATURE=$(python3 -c "
import hmac, hashlib
print(hmac.new(b'YOUR_SECRET_HERE', b'backup', hashlib.sha256).hexdigest())
")

gcloud scheduler jobs update http telemetry-daily-backup \
  --location=us-east1 \
  --headers="X-Telemetry-Signature=${BACKUP_SIGNATURE}"
```

### C7. Set up a billing budget alert

Terraform doesn't manage billing budgets well. Do this in the web console:

1. Go to https://console.cloud.google.com/billing/budgets
2. Follow the steps in B9 above

### C8. Tear everything down (if you ever want to)

```bash
terraform destroy   # type "yes" to confirm — deletes everything
```

---

## Downloading Your Backups

Your JSONL backup files live in Cloud Storage. Pull them all to your local machine:

```bash
gcloud storage cp -r gs://your-bucket-name/backups/ ./local-backups/
```

Each file is one JSON object per line. Load them into anything:

```python
import json
from pathlib import Path

events: list[dict] = []
for f in Path("local-backups").glob("*.jsonl"):
    for line in f.read_text().splitlines():
        events.append(json.loads(line))

# Now do whatever — pandas, SQLite, DuckDB, grep, etc.
```

## Browsing Events in Firestore

If you want to poke around in the live data:

**Web console**: Go to https://console.cloud.google.com/firestore and browse the `telemetry_events` collection.

**Script**:
```python
from google.cloud import firestore

db = firestore.Client()
for doc in db.collection("telemetry_events").order_by("timestamp").limit(10).stream():
    print(doc.to_dict())
```

## Portability: Leaving Google

The daily JSONL backups are your insurance policy. If you ever want to leave GCP:

1. Download all your backups (see above)
2. You now have plain text JSON files with every event ever recorded
3. Load them into Postgres, SQLite, DuckDB, a spreadsheet, whatever
4. Point the `TelemetryClient` at a different server URL
5. Delete the GCP project

The client only knows about an HTTP endpoint and a shared secret. It has zero coupling to Firestore, GCP, or any Google-specific API.
