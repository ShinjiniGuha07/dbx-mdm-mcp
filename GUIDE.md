# dbx-mdm-mcp — Complete Guide

An MCP (Model Context Protocol) server that exposes Informatica MDM (Customer 360) search and
entity retrieval as tools for Databricks Genie. Built for presales demos — not production-hardened,
but fully functional end-to-end.

---

## Table of Contents

1. [What This Does](#what-this-does)
2. [Architecture](#architecture)
3. [How Authentication Works](#how-authentication-works)
4. [Code Walkthrough](#code-walkthrough)
5. [MCP Tools Reference](#mcp-tools-reference)
6. [Environment Variables](#environment-variables)
7. [Local Development](#local-development)
8. [Deployment to Cloud Run](#deployment-to-cloud-run)
9. [Databricks Registration](#databricks-registration)
10. [Troubleshooting](#troubleshooting)
11. [Adding New Tools](#adding-new-tools)

---

## What This Does

Databricks Genie can call external MCP servers to answer natural language questions using live data.
This server bridges Genie to Informatica MDM so you can ask things like:

- "Find all people named John Doe in MDM"
- "Get the full record for business ID 12345"
- "Search for Alan Guy and show me his address"

Genie calls the MCP tools, the server calls the MDM APIs using an IICS session, and the results
come back as structured JSON that Genie's LLM interprets into a natural language answer.

---

## Architecture

```
Databricks Genie
      │
      │  1. POST /oauth/token  (client_credentials)
      ▼
┌─────────────────────────────────────────────┐
│           Cloud Run: mdm-search-mcp          │
│                                             │
│  /oauth/token  ──► validates client creds   │
│                    returns Bearer token      │
│                                             │
│  /mcp          ──► MCP protocol handler     │
│                    tools/list               │
│                    tools/call               │
└─────────────┬───────────────────────────────┘
              │  2. IDS-SESSION-ID header
              │  (IICS session, auto-refreshes)
              ▼
   Informatica MDM (dmp-us.informaticacloud.com)
      - /search/public/api/v1/search
      - /business-entity/public/api/v1/entity/{be}/{id}
```

Both the MCP endpoint and the OAuth token endpoint run on **one port** (8000), which is required
by Cloud Run's single-port constraint.

---

## How Authentication Works

There are two separate auth layers:

### Layer 1: Databricks → MCP Server (OAuth M2M)

Databricks uses the OAuth 2.0 client credentials grant to authenticate to the MCP server.

1. Databricks POSTs to `/oauth/token` with `client_id` + `client_secret`
2. The server validates them against `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` env vars
3. Returns a **static Bearer token** — derived as `HMAC-SHA256(client_secret, "mdm-search-bearer")`
4. Databricks sends this token as `Authorization: Bearer <token>` on every MCP request

The Bearer token is static (no expiry in practice) but the `expires_in: 3600` field tells
Databricks to re-fetch it hourly, which is fine for a demo.

The server accepts credentials via **both** HTTP Basic auth header (`Authorization: Basic base64(id:secret)`)
and request body form fields — because different OAuth clients use different conventions (RFC 6749 §2.3).

### Layer 2: MCP Server → MDM (IICS Session)

The server authenticates to Informatica MDM using IICS identity service:

1. On first tool call, POSTs `{username, password}` to `https://dmp-us.informaticacloud.com/identity-service/api/v1/Login`
2. Gets back a `sessionId` — stored as a global `_session_id`
3. Sends `IDS-SESSION-ID: <sessionId>` on every MDM API call
4. If MDM returns 401 (session expired), clears `_session_id` and re-logs in automatically

IICS sessions are short-lived (~30 min). The auto-refresh handles expiry transparently.

### OAuth Metadata Endpoint

The server also exposes `GET /.well-known/oauth-authorization-server` — a standard RFC 8414
endpoint that OAuth clients can probe to discover the token URL. Databricks may use this during
connection validation.

---

## Code Walkthrough

### `server.py`

#### Imports and config
```python
from dotenv import load_dotenv
load_dotenv()
```
Loads `.env` from the current directory so you can run locally without setting env vars manually.

```python
BEARER_TOKEN = hmac.new(
    OAUTH_CLIENT_SECRET.encode(),
    b"mdm-search-bearer",
    hashlib.sha256,
).hexdigest()
```
Derives a deterministic static token from the client secret. No JWT library needed. The token
is the same every restart, so Databricks' cached token stays valid across server restarts.

#### MCP server init
```python
mcp = FastMCP(
    "mdm-search",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
```
`enable_dns_rebinding_protection=False` is required because Databricks proxies requests through
its own MCP proxy (`Databricks-MCP-Proxy/1.0`), which changes the `Host` header. Without this,
the MCP library returns **421 Misdirected Request** and rejects every call.

#### IICS session management
```python
_session_id = None

def _session():
    global _session_id
    if not _session_id:
        _session_id = _login()
    return _session_id
```
Lazy initialization — logs in on first use, not at startup. This avoids a startup failure if
the MDM environment is temporarily unreachable.

#### MDM API calls with auto-retry
```python
for attempt in range(2):
    ...
    except urllib.error.HTTPError as e:
        if e.code == 401 and attempt == 0:
            _session_id = None  # force re-login
            continue
```
Two-attempt loop: first attempt uses the cached session, if it gets a 401 it clears the session
and retries once with a fresh login. Any other error or a second 401 raises immediately.

#### OAuth token endpoint
```python
auth_header = request.headers.get("authorization", "")
if auth_header.startswith("Basic "):
    decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
    cid, _, csecret = decoded.partition(":")
```
Checks the `Authorization: Basic` header first (standard OAuth client_secret_basic method),
then falls back to reading `client_id` / `client_secret` from the form body (client_secret_post).
Databricks uses Basic auth; the body fallback is there for curl testing.

#### Combining MCP + OAuth on one port
```python
mcp_app = mcp.streamable_http_app()

@asynccontextmanager
async def lifespan(app):
    async with mcp_app.router.lifespan_context(app):
        yield

app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/oauth/token", oauth_token, methods=["POST"]),
        Route("/.well-known/oauth-authorization-server", oauth_metadata, methods=["GET"]),
        Mount("/", app=mcp_app),
    ],
)
```
The MCP library's `streamable_http_app()` returns a Starlette app that manages an internal
async task group via its lifespan. If you just `Mount` it without forwarding the lifespan,
the task group never initializes and every request crashes with
`RuntimeError: Task group is not initialized`.

The fix is to forward the lifespan: `mcp_app.router.lifespan_context(app)` starts the MCP
task group when our outer app starts up.

### `Dockerfile`
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
EXPOSE 8000
EXPOSE 8080
CMD ["python", "server.py"]
```
Cloud Run builds this via Cloud Build. `python:3.12-slim` is required — `mcp` package needs
Python 3.10+. The system Python on macOS is 3.9 and won't work.

---

## MCP Tools Reference

### `search_mdm_entity`

Searches MDM for matching records using the Search API.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `search` | string | required | Name, email, or free-text query |
| `entity_type` | string | `c360_person_1780596889717` | MDM business entity type |
| `max_records` | integer | `50` | Max results to return |

**MDM API called:**
```
POST {MDM_BASE_URL}/search/public/api/v1/search
Body: {"entityType": "...", "search": "...", "maxRecords": 50}
```

**Response fields to know:**
- `SMtotalRecords` — total matches found
- `SMbusinessId` — unique MDM ID for the record (use this with `get_mdm_entity`)
- `SMscore` — similarity score as a **string** (cast to Number before comparing)
- `description` — human-readable record summary

---

### `get_mdm_entity`

Fetches a single complete entity record by its business entity name and business ID.

| Parameter | Type | Description |
|-----------|------|-------------|
| `be_name` | string | Business entity name (e.g. `c360_person_1780596889717`) |
| `business_id` | string | The `SMbusinessId` from a search result |

**MDM API called:**
```
GET {MDM_BASE_URL}/business-entity/public/api/v1/entity/{be_name}/{business_id}
```

**Typical flow:** call `search_mdm_entity` first to find the `SMbusinessId`, then pass it here
to get the full record including all fields, relationships, and source cross-references.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `IICS_USER` | Yes | Informatica username |
| `IICS_PASS` | Yes | Informatica password |
| `MDM_BASE_URL` | Yes | MDM environment base URL, e.g. `https://usw1-mdm.dmp-us.informaticacloud.com` |
| `IICS_LOGIN_HOST` | No | IICS login host. Default: `https://dmp-us.informaticacloud.com` |
| `OAUTH_CLIENT_ID` | Yes | Client ID you choose — give this to Databricks |
| `OAUTH_CLIENT_SECRET` | Yes | Client secret you choose — give this to Databricks |
| `BASE_URL` | No | Public URL of this server (used in OAuth metadata). Default: `http://localhost:{PORT}` |
| `PORT` | No | Port to listen on. Default: `8000` |

Copy `.env.example` to `.env` and fill in values for local development.

---

## Local Development

**Requirements:** Python 3.12+ (the system macOS Python 3.9 is too old for the `mcp` package)

```bash
# Install Python 3.12 if needed
brew install python@3.12

# Create and activate virtual environment
/opt/homebrew/bin/python3.12 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your credentials

# Start the server
python server.py
```

Server starts on `http://localhost:8000`.

### Testing locally

**Get an OAuth token:**
```bash
curl -s -X POST http://localhost:8000/oauth/token \
  -d "grant_type=client_credentials&client_id=JAVANAVI&client_secret=STRONGJAVANAVI" \
  | python3 -m json.tool
```

**Capture token and session, then call a tool:**
```bash
TOKEN=$(curl -s -X POST http://localhost:8000/oauth/token \
  -d "grant_type=client_credentials&client_id=JAVANAVI&client_secret=STRONGJAVANAVI" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

SESSION=$(curl -si -X POST http://localhost:8000/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}' \
  | grep -i mcp-session-id | awk '{print $2}' | tr -d '\r')

curl -s -X POST http://localhost:8000/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SESSION" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search_mdm_entity","arguments":{"search":"John Doe"}}}'
```

Note: raw MCP calls require the `Accept: application/json, text/event-stream` header — the
protocol uses Server-Sent Events (SSE) for responses and returns 406 without it.

---

## Deployment to Cloud Run

Cloud Run builds from source using the `Dockerfile`. It handles TLS termination — your server
listens on HTTP port 8000 but the public URL is always HTTPS on port 443.

### One-command deploy
```bash
gcloud run deploy mdm-search-mcp \
  --source /path/to/dbx-mdm-mcp \
  --region us-central1 \
  --allow-unauthenticated \
  --port 8000 \
  --set-env-vars "IICS_USER=...,IICS_PASS=...,MDM_BASE_URL=https://usw1-mdm.dmp-us.informaticacloud.com,IICS_LOGIN_HOST=https://dmp-us.informaticacloud.com,OAUTH_CLIENT_ID=JAVANAVI,OAUTH_CLIENT_SECRET=STRONGJAVANAVI,BASE_URL=https://<your-run-url>"
```

### First-time setup on a new GCP project
1. **Enable billing** — required to enable APIs (free tier still applies after billing is linked)
2. Say **Y** when prompted to enable `cloudbuild`, `run`, and `artifactregistry` APIs
3. If you get a permissions error on the default service account, run:
   ```bash
   gcloud projects add-iam-policy-binding <project-id> \
     --member="serviceAccount:<project-number>-compute@developer.gserviceaccount.com" \
     --role="roles/storage.objectViewer"
   gcloud projects add-iam-policy-binding <project-id> \
     --member="serviceAccount:<project-number>-compute@developer.gserviceaccount.com" \
     --role="roles/logging.logWriter"
   ```

### Current deployment
- **Project:** `shin-mdm-dbx-demo`
- **Service URL:** `https://mdm-search-mcp-229007842141.us-central1.run.app`
- **MCP endpoint:** `https://mdm-search-mcp-229007842141.us-central1.run.app/mcp`
- **OAuth endpoint:** `https://mdm-search-mcp-229007842141.us-central1.run.app/oauth/token`

### Checking logs
```bash
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=mdm-search-mcp" \
  --project=shin-mdm-dbx-demo --limit=50 --format="value(textPayload)"
```

---

## Databricks Registration

### Step 1 — Create a Unity Catalog HTTP connection

In Databricks: **Catalog** → **+ Add** → **Add a connection**

| Field | Value |
|-------|-------|
| Connection type | HTTP |
| Is MCP connection | ✅ (must be checked) |
| Host | `https://mdm-search-mcp-229007842141.us-central1.run.app` |
| Base path | `/mcp` |
| Auth type | OAuth M2M (machine-to-machine) |
| Token endpoint | `https://mdm-search-mcp-229007842141.us-central1.run.app/oauth/token` |
| Client ID | `JAVANAVI` |
| Client secret | `STRONGJAVANAVI` |
| OAuth scope | *(leave blank)* |

### Step 2 — Add to Genie as External MCP Server

In Genie Code settings → **MCP Servers** → **Add Server** → **External MCP server**
→ select the connection you just created.

### Notes
- The "Is MCP connection" checkbox is mandatory — without it the connection won't appear in
  the External MCP Server dropdown
- After adding, you may need to refresh Genie for it to discover the tools
- Databricks caps total tools at **20 across all MCP servers** — this server contributes 2

---

## Troubleshooting

### `421 Misdirected Request` on MCP calls
Databricks proxies requests via `Databricks-MCP-Proxy/1.0`, which changes the `Host` header.
The MCP library's DNS rebinding protection rejects it. Fixed by:
```python
mcp = FastMCP("mdm-search", transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
```

### `RuntimeError: Task group is not initialized`
The MCP app's lifespan wasn't being forwarded to the outer Starlette app. Fixed by:
```python
@asynccontextmanager
async def lifespan(app):
    async with mcp_app.router.lifespan_context(app):
        yield
```

### `406 Not Acceptable` on curl
Add the Accept header: `-H "Accept: application/json, text/event-stream"`

### `KeyError: 'OAUTH_CLIENT_ID'` on Cloud Run startup
The `--set-env-vars` string had a line break splitting the variable name. Always pass env vars
as a single unbroken string, or use the `gcloud` tool via a script rather than pasting into
the terminal.

### `IICS login failed` / MDM 401
- Check `IICS_USER` and `IICS_PASS` are correct for the target environment
- Verify `IICS_LOGIN_HOST` matches the org's region (`dmp-us` vs `dmp-eu` etc.)
- The session auto-refreshes on 401 — if it fails twice, the credentials are wrong

### `mcp` package not found / Python version error
The `mcp` package requires Python 3.10+. macOS ships with Python 3.9. Install via:
```bash
brew install python@3.12
/opt/homebrew/bin/python3.12 -m venv venv
```

---

## Adding New Tools

Adding a new MDM API as an MCP tool is a two-step pattern:

**1. Add a private function that calls the MDM API:**
```python
def _mdm_my_new_call(param1, param2):
    global _session_id
    url = f"{MDM_BASE_URL}/some/api/endpoint/{param1}"

    for attempt in range(2):
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "IDS-SESSION-ID": _session()},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                _session_id = None
                continue
            raise RuntimeError(f"Call failed ({e.code}): {e.read().decode()}") from e
```

**2. Decorate a public function with `@mcp.tool()`:**
```python
@mcp.tool()
def my_new_tool(param1: str, param2: str = "default") -> dict:
    """
    One-line description for Genie to understand when to use this tool.

    Args:
        param1: what this is
        param2: what this is
    """
    return _mdm_my_new_call(param1, param2)
```

The docstring is what Genie's LLM reads to decide whether to call the tool — make it specific
and action-oriented. Redeploy with `gcloud run deploy ...` after any change.
