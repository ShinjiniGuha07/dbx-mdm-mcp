#!/usr/bin/env python3
"""
MDM Search MCP Server
Wraps the Informatica MDM Search API as a single MCP tool for Databricks Genie.
Serves both the MCP endpoint (/mcp) and OAuth token endpoint (/oauth/token) on one port.
"""
import os
import json
import hmac
import hashlib
import urllib.request
import urllib.error

from dotenv import load_dotenv
load_dotenv()

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# --- Config ---
IDMC_USER           = os.environ["IDMC_USER"]
IDMC_PASS           = os.environ["IDMC_PASS"]
MDM_BASE_URL        = os.environ["MDM_BASE_URL"]   # e.g. https://usw1-mdm.dmp-us.informaticacloud.com
LOGIN_HOST          = os.environ.get("IDMC_LOGIN_HOST", "https://dmp-us.informaticacloud.com")
OAUTH_CLIENT_ID     = os.environ["OAUTH_CLIENT_ID"]
OAUTH_CLIENT_SECRET = os.environ["OAUTH_CLIENT_SECRET"]
PORT                = int(os.environ.get("PORT", "8080"))

# Entity type mapping — logical name → MDM entityType string.
# Override via ENTITY_TYPES env var as JSON, e.g.:
#   '{"person":"c360.person","organization":"c360.organization","guest":"c360_person_1780596889717"}'
_DEFAULT_ENTITY_TYPES = {
    "person":       "c360.person",
    "guest":        "c360_person_1780596889717",
    "organization": "c360.organization",
}
ENTITY_TYPES: dict = json.loads(os.environ.get("ENTITY_TYPES", "{}")) or _DEFAULT_ENTITY_TYPES

def _resolve_entity_type(name: str) -> str:
    """Accept a logical name (e.g. 'person', 'guest') or a raw entity type string."""
    return ENTITY_TYPES.get(name.lower(), name)

# RELATIONSHIP_TYPES: {entity_alias: {rel_alias: mdm_rel_string}}
# Override via RELATIONSHIP_TYPES env var as JSON, e.g.:
#   '{"person":{"household":"household"},"guest":{"household":"household"}}'
_DEFAULT_RELATIONSHIP_TYPES = {
    "person": {"household": "household"},
    "guest":  {"household": "household"},
}
RELATIONSHIP_TYPES: dict = json.loads(os.environ.get("RELATIONSHIP_TYPES", "{}")) or _DEFAULT_RELATIONSHIP_TYPES

def _resolve_relationship(entity_alias: str, rel_alias: str):
    """
    Returns (mdm_rel_string, warning) tuple.
    warning is None if the combo is configured, a message string if not.
    """
    entity_key = entity_alias.lower()
    rel_key    = rel_alias.lower()
    entity_rels = RELATIONSHIP_TYPES.get(entity_key)
    if entity_rels is None:
        configured = list(RELATIONSHIP_TYPES.keys())
        return None, f"No relationships configured for entity '{entity_alias}'. Configured entities: {configured}"
    mdm_rel = entity_rels.get(rel_key)
    if mdm_rel is None:
        configured = list(entity_rels.keys())
        return None, f"Relationship '{rel_alias}' not configured for entity '{entity_alias}'. Configured relationships: {configured}"
    return mdm_rel, None

# Static bearer token derived from the client secret — no JWT needed for demo
BEARER_TOKEN = hmac.new(
    OAUTH_CLIENT_SECRET.encode(),
    b"mdm-search-bearer",
    hashlib.sha256,
).hexdigest()

mcp = FastMCP(
    "mdm-search",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

# --- IDMC session (lazy, auto-refreshes on 401) ---
_session_id = None

def _login():
    body = json.dumps({"username": IDMC_USER, "password": IDMC_PASS}).encode()
    req = urllib.request.Request(
        f"{LOGIN_HOST}/identity-service/api/v1/Login",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.load(r)
    sid = data.get("sessionId")
    if not sid:
        raise RuntimeError(f"IDMC login failed: {data}")
    print(f"[auth] logged in to {data.get('orgName')} — expires {data.get('sessionExpireTime')}")
    return sid

def _session():
    global _session_id
    if not _session_id:
        _session_id = _login()
    return _session_id

def _mdm_search(entity_type, search, max_records):
    global _session_id
    url  = f"{MDM_BASE_URL}/search/public/api/v1/search"
    body = json.dumps({"entityType": entity_type, "search": search, "maxRecords": max_records}).encode()

    for attempt in range(2):
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type":   "application/json",
                "IDS-SESSION-ID": _session(),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                _session_id = None  # force re-login
                continue
            raise RuntimeError(f"MDM search failed ({e.code}): {e.read().decode('utf-8', errors='replace')}") from e

# --- MCP tool ---

@mcp.tool()
def search_mdm_entity(
    search: str,
    entity_type: str = "person",
    max_records: int = 50,
) -> dict:
    """
    Search Informatica MDM (Customer 360) for persons, guests, or organizations.

    Args:
        search:       Name, email, or free-text to search for (e.g. "John Doe")
        entity_type:  Who to search for. Accepts logical names or raw MDM entity type strings:
                        - "person" or "guest"  → person records
                        - "organization" or "org" → organization records
                        - any raw MDM entityType string (e.g. "c360_person_1780596889717")
        max_records:  Maximum number of results to return (default 50)

    Returns the raw MDM search response including matched records and SMscore values.
    """
    return _mdm_search(_resolve_entity_type(entity_type), search, max_records)


@mcp.tool()
def list_entity_types() -> dict:
    """
    List all available MDM entity types that can be searched.
    Returns the logical name aliases and their underlying MDM entityType strings.
    """
    return {"entity_types": ENTITY_TYPES}


def _mdm_get_relationship(business_id, business_entity, rel_type):
    global _session_id
    url  = f"{MDM_BASE_URL}/business-entity/public/api/v1/relationship/{rel_type}/filter"
    body = json.dumps({
        "filter": {
            "_from": {"businessId": business_id, "businessEntity": business_entity}
        }
    }).encode()

    for attempt in range(2):
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type":   "application/json",
                "Accept":         "application/json",
                "IDS-SESSION-ID": _session(),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                _session_id = None
                continue
            err_body = e.read().decode('utf-8', errors='replace')
            raise RuntimeError(f"MDM relationship lookup failed ({e.code}): {err_body}") from e


@mcp.tool()
def get_mdm_relationships(
    business_id: str,
    entity_type: str = "guest",
    relationship_type: str = "household",
) -> dict:
    """
    Get related records linked to a given MDM entity via a configured relationship type.

    Args:
        business_id:       The MDM businessId of the record (e.g. "MDM00000000IN6")
        entity_type:       Logical entity alias (e.g. "person", "guest", "organization").
                           Must be one of the configured entity types.
        relationship_type: Logical relationship alias (e.g. "household").
                           Must be configured for the given entity type.

    Returns relationship records, each with _from and _to business entity references.
    If the entity/relationship combination is not configured, returns a warning instead.
    """
    mdm_rel, warning = _resolve_relationship(entity_type, relationship_type)
    if warning:
        return {"warning": warning}
    return _mdm_get_relationship(business_id, _resolve_entity_type(entity_type), mdm_rel)


@mcp.tool()
def list_relationship_types() -> dict:
    """
    List all configured relationship types grouped by entity.
    Returns the entity aliases and their allowed relationship aliases with MDM relationship strings.
    """
    return {"relationship_types": RELATIONSHIP_TYPES}


def _mdm_get_entity(be_name, business_id):
    global _session_id
    url = f"{MDM_BASE_URL}/business-entity/public/api/v1/entity/{be_name}/{business_id}"

    for attempt in range(2):
        req = urllib.request.Request(
            url,
            headers={
                "Accept":         "application/json",
                "IDS-SESSION-ID": _session(),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                _session_id = None
                continue
            raise RuntimeError(f"MDM get entity failed ({e.code}): {e.read().decode('utf-8', errors='replace')}") from e


@mcp.tool()
def get_mdm_entity(
    be_name: str,
    business_id: str,
) -> dict:
    """
    Retrieve a specific Informatica MDM entity by business entity name and business ID.

    Args:
        be_name:      Business entity name (e.g. 'c360.person', 'c360.organization')
        business_id:  The MDM businessId / SMbusinessId of the record to fetch

    Returns the full entity record from MDM.
    """
    return _mdm_get_entity(be_name, business_id)


# --- OAuth token endpoint ---

async def oauth_token(request: Request):
    import base64

    # Extract client_id + client_secret from Basic auth header OR request body (RFC 6749 s2.3)
    cid, csecret = None, None
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            cid, _, csecret = decoded.partition(":")
        except Exception:
            pass

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        params = await request.json()
    else:
        form = await request.form()
        params = dict(form)

    # Body values override header if present
    if not cid:
        cid = params.get("client_id")
    if not csecret:
        csecret = params.get("client_secret")

    if params.get("grant_type") != "client_credentials":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    if cid != OAUTH_CLIENT_ID or csecret != OAUTH_CLIENT_SECRET:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    return JSONResponse({
        "access_token": BEARER_TOKEN,
        "token_type":   "Bearer",
        "expires_in":   3600,
        "scope":        "",
    })


# --- Combine MCP + OAuth into one Starlette app on one port ---
# The MCP streamable-http app manages an internal task group via its lifespan.
# We forward that lifespan to our outer Starlette app so it initialises correctly.

from contextlib import asynccontextmanager

mcp_app = mcp.streamable_http_app()

@asynccontextmanager
async def lifespan(app):
    async with mcp_app.router.lifespan_context(app):
        yield

app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/oauth/token", oauth_token, methods=["POST"]),
        Mount("/", app=mcp_app),
    ],
)

if __name__ == "__main__":
    print(f"[server] MCP at :{PORT}/mcp  |  OAuth at :{PORT}/oauth/token")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
