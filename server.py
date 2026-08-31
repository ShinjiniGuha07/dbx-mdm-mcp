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

# CREATE_RELATIONSHIP_TYPES: {from_entity_alias: {rel_alias: {"mdm_rel": "...", "to_entity": "..."}}}
# Override via CREATE_RELATIONSHIP_TYPES env var as JSON, e.g.:
#   '{"person":{"household":{"mdm_rel":"household","to_entity":"person"}}}'
CREATE_RELATIONSHIP_TYPES: dict = json.loads(os.environ.get("CREATE_RELATIONSHIP_TYPES", "{}")) or {}

# Source system query param used in the Create Relationship and Create Entity API calls
CREATE_REL_SOURCE_SYSTEM = os.environ.get("CREATE_REL_SOURCE_SYSTEM", "c360.default.system")

# CREATE_ENTITY_FIELDS: {entity_alias: [{label, field, required}, ...]}
# Defines the fields the LLM should gather before calling create_mdm_entity.
# Override via CREATE_ENTITY_FIELDS env var as JSON, e.g.:
#   '{"person":[{"label":"First Name","field":"firstName","required":true}]}'
CREATE_ENTITY_FIELDS: dict = json.loads(os.environ.get("CREATE_ENTITY_FIELDS", "{}")) or {}

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

def _resolve_create_relationship(from_entity_alias: str, rel_alias: str):
    """Returns (mdm_rel, to_entity_alias, warning)."""
    entity_key = from_entity_alias.lower()
    rel_key    = rel_alias.lower()
    entity_rels = CREATE_RELATIONSHIP_TYPES.get(entity_key)
    if entity_rels is None:
        configured = list(CREATE_RELATIONSHIP_TYPES.keys())
        return None, None, f"No create relationships configured for entity '{from_entity_alias}'. Configured entities: {configured}"
    rel_cfg = entity_rels.get(rel_key)
    if rel_cfg is None:
        configured = list(entity_rels.keys())
        return None, None, f"Relationship '{rel_alias}' not configured for create on entity '{from_entity_alias}'. Configured: {configured}"
    return rel_cfg["mdm_rel"], rel_cfg["to_entity"], None

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


def _mdm_create_relationship(from_business_id, from_be, to_business_id, to_be, rel_id):
    global _session_id
    url = (
        f"{MDM_BASE_URL}/business-entity/public/api/v1/relationship/{rel_id}"
        f"?sourceSystem={CREATE_REL_SOURCE_SYSTEM}"
    )
    body = json.dumps({
        "_from": {"businessEntity": from_be, "businessId": from_business_id},
        "_to":   {"businessEntity": to_be,   "businessId": to_business_id},
    }).encode()

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
                _session_id = None
                continue
            raise RuntimeError(
                f"MDM create relationship failed ({e.code}): "
                f"{e.read().decode('utf-8', errors='replace')}"
            ) from e


@mcp.tool()
def create_mdm_relationship(
    from_business_id: str,
    to_business_id: str,
    from_entity_type: str = "person",
    relationship_type: str = "household",
) -> dict:
    """
    Create a relationship between two MDM records.

    Args:
        from_business_id:  MDM businessId of the source record (e.g. "MDM00000000IN6")
        to_business_id:    MDM businessId of the target record
        from_entity_type:  Logical entity alias for the source (e.g. "person", "guest")
        relationship_type: Logical relationship alias (e.g. "household")

    Returns the created relationship businessId, or a warning if the combo is not configured.
    """
    mdm_rel, to_entity_alias, warning = _resolve_create_relationship(from_entity_type, relationship_type)
    if warning:
        return {"warning": warning}
    from_be = _resolve_entity_type(from_entity_type)
    to_be   = _resolve_entity_type(to_entity_alias)
    return _mdm_create_relationship(from_business_id, from_be, to_business_id, to_be, mdm_rel)


@mcp.tool()
def list_create_relationship_types() -> dict:
    """
    List all configured relationship types that can be created.
    Returns the from-entity aliases and their allowed relationship aliases with target entity and MDM rel string.
    """
    return {"create_relationship_types": CREATE_RELATIONSHIP_TYPES}


def _mdm_create_entity(be_name, fields):
    global _session_id
    url = (
        f"{MDM_BASE_URL}/business-entity/public/api/v1/entity/{be_name}"
        f"?sourceSystem={CREATE_REL_SOURCE_SYSTEM}"
    )
    body = json.dumps(fields).encode()
    for attempt in range(2):
        req = urllib.request.Request(url, data=body,
            headers={"Content-Type": "application/json", "IDS-SESSION-ID": _session()})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                _session_id = None
                continue
            raise RuntimeError(
                f"MDM create entity failed ({e.code}): "
                f"{e.read().decode('utf-8', errors='replace')}"
            ) from e


@mcp.tool()
def list_entity_create_fields(entity_type: str = "person") -> dict:
    """
    List the configured fields for creating a new entity of the given type.
    ALWAYS call this before create_mdm_entity to discover which fields are required vs optional.
    Ask the user for any required fields they have not provided before calling create_mdm_entity.

    Args:
        entity_type: Logical entity alias (e.g. "person", "organization")

    Returns field definitions: label (human-readable prompt), field (MDM body key), required flag.
    If no fields are configured for the entity type, returns a warning.
    """
    key = entity_type.lower()
    fields = CREATE_ENTITY_FIELDS.get(key)
    if fields is None:
        return {"warning": f"No create fields configured for entity '{entity_type}'. "
                           f"Configured entities: {list(CREATE_ENTITY_FIELDS.keys())}"}
    return {"entity_type": entity_type, "fields": fields}


@mcp.tool()
def create_mdm_entity(
    entity_type: str,
    fields: dict,
) -> dict:
    """
    Create a new master record in Informatica MDM.
    IMPORTANT: Call list_entity_create_fields(entity_type) first.
    Gather all required fields from the user before calling this tool.

    Args:
        entity_type: Logical entity alias (e.g. "person", "organization"). Must be in ENTITY_TYPES.
        fields:      Dict of MDM field name → value. Use the exact 'field' keys from
                     list_entity_create_fields() (e.g. {"firstName": "John", "lastName": "Doe"}).

    Returns the new record's businessId and a view_url to retrieve the full record, plus
    approvalRequired indicating whether a data steward must approve the new record.
    """
    be = _resolve_entity_type(entity_type)
    result = _mdm_create_entity(be, fields)
    if "businessId" in result:
        result["view_url"] = (
            f"{MDM_BASE_URL}/business-entity/public/api/v1/entity/{be}/{result['businessId']}"
        )
    return result


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


# --- OAuth endpoints ---

async def oauth_authorize(request: Request):
    """
    Authorization endpoint for the authorization-code flow (e.g. Slack).
    We don't show a consent screen — just redirect straight back with a static code.
    """
    from starlette.responses import RedirectResponse
    redirect_uri = request.query_params.get("redirect_uri", "")
    state        = request.query_params.get("state", "")
    if not redirect_uri:
        return JSONResponse({"error": "missing redirect_uri"}, status_code=400)
    sep = "&" if "?" in redirect_uri else "?"
    location = redirect_uri + sep + "code=mdm-auth-code&state=" + state
    return RedirectResponse(url=location, status_code=302)


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

    grant_type = params.get("grant_type")
    if grant_type not in ("client_credentials", "authorization_code"):
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    # authorization_code flow (Slack): validate client id only — no secret required
    if grant_type == "authorization_code":
        if cid and cid != OAUTH_CLIENT_ID:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
    else:
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
        Route("/oauth/authorize", oauth_authorize, methods=["GET"]),
        Route("/oauth/token",     oauth_token,     methods=["POST"]),
        Mount("/", app=mcp_app),
    ],
)

if __name__ == "__main__":
    print(f"[server] MCP at :{PORT}/mcp  |  OAuth at :{PORT}/oauth/token")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
