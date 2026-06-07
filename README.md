# dbx-mdm-mcp

MCP server that wraps the Informatica MDM Search API for registration in Databricks Genie.
Exposes one tool (`search_mdm_entity`) and a `/oauth/token` endpoint for Databricks client-credentials auth.

## Ports

| Port | Purpose |
|------|---------|
| `8000` | MCP endpoint (`/mcp`) |
| `8080` | OAuth token endpoint (`/oauth/token`) |

## Local run

```bash
pip install -r requirements.txt

export IICS_USER=your_username
export IICS_PASS=your_password
export MDM_BASE_URL=https://usw1-mdm.dmp-us.informaticacloud.com
export OAUTH_CLIENT_ID=mdm-search-client
export OAUTH_CLIENT_SECRET=change-me

python server.py
```

Test the OAuth token endpoint:
```bash
curl -s -X POST http://localhost:8080/oauth/token \
  -d "grant_type=client_credentials&client_id=mdm-search-client&client_secret=change-me"
```

## Deploy to Cloud Run

```bash
gcloud run deploy mdm-search-mcp \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --port 8000 \
  --set-env-vars IICS_USER=...,IICS_PASS=...,MDM_BASE_URL=...,OAUTH_CLIENT_ID=...,OAUTH_CLIENT_SECRET=...
```

> Cloud Run exposes one port. To serve both ports, use a reverse proxy or deploy the OAuth
> endpoint as a separate service (or run both on the same port with a router — open an issue).
> For demo purposes, deploying two revisions (one per port) is simplest.

## Databricks registration

Register in Databricks catalog as an MCP connection with:

| Field | Value |
|-------|-------|
| Host URL | `https://<your-cloud-run-url>/mcp` |
| Auth type | OAuth 2.0 client credentials |
| Token URL | `https://<your-cloud-run-url>/oauth/token` |
| Client ID | value of `OAUTH_CLIENT_ID` |
| Client Secret | value of `OAUTH_CLIENT_SECRET` |

## Environment variables

See `.env.example` for all variables and descriptions.
