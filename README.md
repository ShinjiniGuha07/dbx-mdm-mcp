# dbx-mdm-mcp

MCP server that wraps the Informatica MDM Search and Business Entity APIs for registration in
Databricks Genie. Exposes three tools and a `/oauth/token` endpoint for Databricks client-credentials auth.
Both the MCP and OAuth endpoints run on a single port (required by Cloud Run).

## Tools

| Tool | Description |
|------|-------------|
| `search_mdm_entity` | Search MDM by name/text across any entity type |
| `get_mdm_entity` | Fetch a full record by business entity name + business ID |
| `list_entity_types` | List available entity type aliases for the current environment |

## Entity types

Entity types are configured as a logical name → MDM entityType string mapping, so the same
server code works across environments. The default mapping is:

| Logical name | MDM entityType |
|-------------|----------------|
| `person` | `c360.person` |
| `guest` | `c360_person_1780596889717` |
| `organization` / `org` | `c360.organization` |

Override per-environment by setting `ENTITY_TYPES` as a JSON string (see `.env.example`).
Genie can also pass raw MDM entityType strings directly if needed.

## Local run

```bash
# Requires Python 3.10+ (macOS system Python 3.9 is too old)
brew install python@3.12
/opt/homebrew/bin/python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in your credentials
python server.py
```

Test the OAuth token endpoint:
```bash
curl -s -X POST http://localhost:8000/oauth/token \
  -d "grant_type=client_credentials&client_id=<OAUTH_CLIENT_ID>&client_secret=<OAUTH_CLIENT_SECRET>" \
  | python3 -m json.tool
```

## Deploy to Cloud Run

 **Corporate GCP projects (org policy):** Many work GCP projects block `allUsers` IAM via org policy, which means `--allow-unauthenticated` will silently fail and Databricks won't be able to reach the service. If you hit this, use a personal/sandbox project instead (e.g. `shin-mdm-dbx-demo` is mine and it works). Ask your GCP admin for an exception if you need it on a corporate project. 

### Prerequisites

**1. Install and authenticate gcloud CLI**
```bash
brew install google-cloud-sdk
gcloud auth login
gcloud auth application-default login
```

**2. Create a GCP project and enable billing** — required to activate APIs (free tier still applies).

**3. Enable required APIs**
```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  --project=<your-project-id>
```

**4. Grant required permissions** (needed on new projects — two different service accounts):
```bash
# Find your project number (different from project ID):
gcloud projects describe <your-project-id> --format='value(projectNumber)'

PROJECT_NUMBER=<number-from-above>

# Compute SA — lets Cloud Build read uploaded source from GCS
gcloud projects add-iam-policy-binding <your-project-id> \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/storage.objectViewer"
gcloud projects add-iam-policy-binding <your-project-id> \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/logging.logWriter"

# Cloud Build SA — lets Cloud Build push the built image to Artifact Registry
gcloud projects add-iam-policy-binding <your-project-id> \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"
```

### Deploy

> **Note:** Do not include `PORT` in `--set-env-vars` — Cloud Run sets it automatically and will reject the deploy if you pass it.

```bash
gcloud run deploy mdm-search-mcp \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --port 8000 \
  --set-env-vars "IDMC_USER=...,IDMC_PASS=...,MDM_BASE_URL=https://usw1-mdm.dmp-us.informaticacloud.com,IDMC_LOGIN_HOST=https://dmp-us.informaticacloud.com,OAUTH_CLIENT_ID=...,OAUTH_CLIENT_SECRET=...,BASE_URL=https://<your-run-url>"
```

To override entity types for a specific environment, add:
```
ENTITY_TYPES={"person":"c360.person","guest":"c360_person_xxxx","organization":"c360.organization"}
```

## Databricks registration

Create a Unity Catalog **HTTP connection** with:

| Field | Value |
|-------|-------|
| Connection type | HTTP |
| Is MCP connection | ✅ checked |
| Host | `https://<your-cloud-run-url>` |
| Base path | `/mcp` |
| Auth type | OAuth M2M |
| Token URL | `https://<your-cloud-run-url>/oauth/token` |
| Client ID | value of `OAUTH_CLIENT_ID` |
| Client Secret | value of `OAUTH_CLIENT_SECRET` |
| OAuth scope | *(leave blank)* |

Then add it to Genie via **Settings → MCP Servers → Add Server → External MCP server**.

## Environment variables

See `.env.example` for all variables and descriptions.
See `GUIDE.md` for full architecture, code walkthrough, and troubleshooting.
