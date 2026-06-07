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

```bash
gcloud run deploy mdm-search-mcp \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --port 8000 \
  --set-env-vars "IICS_USER=...,IICS_PASS=...,MDM_BASE_URL=https://usw1-mdm.dmp-us.informaticacloud.com,IICS_LOGIN_HOST=https://dmp-us.informaticacloud.com,OAUTH_CLIENT_ID=...,OAUTH_CLIENT_SECRET=...,BASE_URL=https://<your-run-url>"
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
