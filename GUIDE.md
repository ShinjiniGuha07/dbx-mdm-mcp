# dbx-mdm-mcp — Guide

Architecture, client connection setup, environment variables, and troubleshooting.

---

## Table of Contents

1. [Architecture](#architecture)
2. [How Authentication Works](#how-authentication-works)
3. [Environment Variables](#environment-variables)
4. [Databricks Setup](#databricks)
5. [Claude Code Setup](#claude-code)
6. [Slack Setup](#slack)
7. [Troubleshooting](#troubleshooting)
8. [Adding New Tools](#adding-new-tools)

---

## Architecture

```
AI Client (Genie / Claude / Slack)
      │
      │  1. POST /oauth/token  →  Bearer token
      │  2. MCP calls with Authorization: Bearer <token>
      ▼
┌─────────────────────────────────────────────┐
│           Cloud Run: mdm-search-mcp          │
│                                             │
│  /oauth/authorize  ──► auth code redirect   │
│  /oauth/token      ──► validates creds,     │
│                        returns Bearer token  │
│                                             │
│  /mcp              ──► MCP protocol handler │
│                        tools/list           │
│                        tools/call           │
└──────────────┬──────────────────────────────┘
               │  IDS-SESSION-ID header
               │  (IDMC session, auto-refreshes on 401)
               ▼
    Informatica MDM (dmp-us.informaticacloud.com)
       - /search/public/api/v1/search
       - /business-entity/public/api/v1/entity/...
       - /business-entity/public/api/v1/relationship/...
```

All endpoints — MCP and OAuth — run on **one port (8000)**, required by Cloud Run's single-port constraint.

The configurator UI (`ui.py` + `ui.html`) runs separately on port 7000 and is only used locally for setup and deployment.

---

## How Authentication Works

### Layer 1 — Client → MCP Server

The server implements OAuth 2.0 with two grant types:

**`client_credentials`** (Databricks, Claude Code):
1. Client POSTs to `/oauth/token` with `client_id` + `client_secret`
2. Server validates against `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` env vars
3. Returns a static Bearer token — `HMAC-SHA256(client_secret, "mdm-search-bearer")`
4. Client sends `Authorization: Bearer <token>` on every MCP request

**`authorization_code`** (Slack):
1. Slack redirects user to `/oauth/authorize?redirect_uri=...&state=...`
2. Server immediately redirects back to Slack's callback with a code (no consent screen)
3. Slack exchanges code at `/oauth/token` → same Bearer token returned
4. Slack sends `Authorization: Bearer <token>` on every MCP request

The Bearer token is deterministic — same client secret always produces the same token, so cached tokens stay valid across server restarts.

Credentials are accepted via **HTTP Basic auth header** (`Authorization: Basic base64(id:secret)`) or **request body** form fields — different clients use different conventions (RFC 6749 §2.3).

### Layer 2 — MCP Server → MDM

1. On first tool call: POSTs `{username, password}` to IDMC Login endpoint
2. Gets back a `sessionId` — cached as a module-level global
3. Sends `IDS-SESSION-ID: <sessionId>` on every MDM API call
4. On 401: clears session and re-logs in automatically (2-attempt retry loop)

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `IDMC_USER` | Yes | Informatica username |
| `IDMC_PASS` | Yes | Informatica password |
| `MDM_BASE_URL` | Yes | MDM environment base URL, e.g. `https://usw1-mdm.dmp-us.informaticacloud.com` |
| `IDMC_LOGIN_HOST` | No | IDMC login host. Default: `https://dmp-us.informaticacloud.com` |
| `OAUTH_CLIENT_ID` | Yes | Client ID you choose — share with Databricks/Claude/Slack |
| `OAUTH_CLIENT_SECRET` | Yes | Client secret you choose — share with Databricks/Claude/Slack |
| `ENTITY_TYPES` | No | JSON: `{"alias": "mdm_entity_type_string", ...}` |
| `RELATIONSHIP_TYPES` | No | JSON: `{"entity_alias": {"rel_alias": "mdm_rel_string"}}` |
| `CREATE_RELATIONSHIP_TYPES` | No | JSON: `{"from_alias": {"rel_alias": {"mdm_rel": "...", "to_entity": "..."}}}` |
| `CREATE_ENTITY_FIELDS` | No | JSON: `{"entity_alias": [{"label": "...", "field": "...", "required": true}]}` |
| `CREATE_REL_SOURCE_SYSTEM` | No | Source system for creates. Default: `c360.default.system` |
| `PORT` | No | Port to listen on. Default: `8000`. Do not set manually on Cloud Run. |

All of these are written to `env.yaml` by the configurator UI's Deploy button and passed to Cloud Run via `--env-vars-file`.

---

## Databricks

### 1. Create a Unity Catalog HTTP connection

**Catalog → + Add → Add a connection**

| Field | Value |
|-------|-------|
| Connection type | HTTP |
| Is MCP connection | ✅ checked |
| Host | `https://<your-cloud-run-url>` |
| Base path | `/mcp` |
| Auth type | OAuth M2M (machine-to-machine) |
| Token endpoint | `https://<your-cloud-run-url>/oauth/token` |
| Client ID | value of `OAUTH_CLIENT_ID` |
| Client secret | value of `OAUTH_CLIENT_SECRET` |
| OAuth scope | *(leave blank)* |

The Connection Settings card in the Configurator UI (shown after a successful deploy) has all these values pre-filled with copy buttons.

### 2. Add to Genie as External MCP Server

Genie space settings → **MCP Servers → Add Server → External MCP server** → select the connection above.

**Notes:**
- The "Is MCP connection" checkbox is mandatory — without it the connection won't appear in the External MCP Server dropdown
- Databricks caps total tools at 20 across all MCP servers
- After adding, refresh Genie for it to discover the tools

---

## Claude Code

Add to `.mcp.json` in your project root (or `~/.claude/mcp.json` for global):

```json
{
  "mcpServers": {
    "mdm-search": {
      "type": "http",
      "url": "https://<your-cloud-run-url>/mcp",
      "headers": {
        "Authorization": "Bearer <bearer-token>"
      }
    }
  }
}
```

The bearer token is `HMAC-SHA256(OAUTH_CLIENT_SECRET, "mdm-search-bearer")` as a hex string. The Connection Settings → Claude Code tab in the configurator UI generates the exact JSON with the computed token — just copy and paste.

---

## Slack

### Requirements
- A Slack app with `mcp:connect` bot scope
- The MCP server deployed (needs `/oauth/authorize` endpoint — added alongside `/oauth/token`)

### Add MCP Server in Slack

In your Slack app's MCP server settings, click **Add MCP Server** and fill in:

| Field | Value |
|-------|-------|
| Name | `mdm-extended` (or any name) |
| URL | `https://<your-cloud-run-url>/mcp` |
| Auth type | Manual OAuth |
| Client ID | value of `OAUTH_CLIENT_ID` |
| Client Secret | value of `OAUTH_CLIENT_SECRET` |
| Authorization URL | `https://<your-cloud-run-url>/oauth/authorize` |
| Token request URL | `https://<your-cloud-run-url>/oauth/token` |
| Use HTTP Basic Authentication | ✅ checked |
| Use PKCE | unchecked |
| OAuth scopes | *(leave empty)* |
| Identity URL | *(leave empty)* |
| Custom headers | *(leave empty)* |

The Connection Settings → Slack tab in the configurator UI has all these values pre-filled.

**How the Slack OAuth flow works:** Slack redirects to `/oauth/authorize`, which immediately redirects back to Slack's callback (`https://oauth2.slack.com/external/auth/callback`) with a static code — no consent screen is shown. Slack then exchanges the code at `/oauth/token` and gets the same bearer token used by Databricks and Claude Code.

---

## Troubleshooting

### `421 Misdirected Request` on MCP calls
Databricks proxies requests via `Databricks-MCP-Proxy/1.0`, which changes the `Host` header. The MCP library's DNS rebinding protection rejects it. Fixed with:
```python
mcp = FastMCP("mdm-search", transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
```

### `RuntimeError: Task group is not initialized`
The MCP app's lifespan wasn't forwarded to the outer Starlette app. Fixed by forwarding it:
```python
@asynccontextmanager
async def lifespan(app):
    async with mcp_app.router.lifespan_context(app):
        yield
```

### `406 Not Acceptable` on curl
Add the Accept header: `-H "Accept: application/json, text/event-stream"`

### `CREATE_ENTITY_FIELDS` shows empty on deployed server
The `ui.py` process was started before `create_entity_fields` code was added. Restart it: `kill <pid> && python ui.py &`

### `IDMC login failed` / MDM 401
- Check `IDMC_USER` and `IDMC_PASS` are correct
- Verify `IDMC_LOGIN_HOST` matches the org's region (`dmp-us` vs `dmp-eu`)
- Sessions auto-refresh on 401 — if it fails twice, credentials are wrong

### Cloud Run deploy fails with `--allow-unauthenticated`
Corporate GCP projects block `allUsers` IAM via org policy. Use a personal/sandbox project, or ask your GCP admin for an exception.

### `mcp` package not found / Python version error
The `mcp` package requires Python 3.10+. macOS ships with Python 3.9:
```bash
brew install python@3.12
/opt/homebrew/bin/python3.12 -m venv venv
```

---

## Adding New Tools

**1. Add a private MDM API function** (copy the retry/auth pattern):
```python
def _mdm_my_call(param1, param2):
    global _session_id
    url = f"{MDM_BASE_URL}/some/api/{param1}"
    for attempt in range(2):
        req = urllib.request.Request(url,
            headers={"Accept": "application/json", "IDS-SESSION-ID": _session()})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                _session_id = None; continue
            raise RuntimeError(f"Call failed ({e.code}): {e.read().decode()}") from e
```

**2. Expose it as an MCP tool:**
```python
@mcp.tool()
def my_new_tool(param1: str, param2: str = "default") -> dict:
    """
    One-line description — this is what the AI reads to decide when to call this tool.

    Args:
        param1: what this is
        param2: what this is
    """
    return _mdm_my_call(param1, param2)
```

Redeploy via the configurator UI after any change to `server.py`.
