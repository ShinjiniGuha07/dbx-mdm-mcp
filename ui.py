#!/usr/bin/env python3
"""
MDM MCP Configurator — local UI to configure and deploy dbx-mdm-mcp to Cloud Run.
Run: python ui.py   then open http://localhost:7000
"""
import asyncio
import json
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route
import uvicorn

PROJECT_DIR = Path(__file__).parent

DEFAULTS = {
    "service_name":      "mdm-search-mcp",
    "project":           "shin-mdm-dbx-demo",
    "region":            "us-central1",
    "idmc_user":         "sguha_DITA_NA_tspod",
    "idmc_pass":         "Ginger@2026",
    "mdm_base_url":      "https://usw1-mdm.dmp-us.informaticacloud.com",
    "idmc_login_host":   "https://dmp-us.informaticacloud.com",
    "oauth_client_id":   "JAVANAVI",
    "oauth_client_secret": "STRONGJAVANAVI",
    "entity_types": [
        {"alias": "person",       "mdm_type": "c360.person"},
        {"alias": "guest",        "mdm_type": "c360_person_1780596889717"},
        {"alias": "organization", "mdm_type": "c360.organization"},
    ],
    "relationship_types": [
        {"entity_alias": "person", "rel_alias": "household", "mdm_rel": "household"},
        {"entity_alias": "guest",  "rel_alias": "household", "mdm_rel": "household"},
    ],
    "create_rel_source_system": "c360.default.system",
    "create_relationship_types": [],
    "create_entity_fields": [],
}


def build_env_vars(cfg: dict) -> dict:
    entity_map = {r["alias"]: r["mdm_type"] for r in cfg["entity_types"]}
    # Group relationship types by entity alias: {entity_alias: {rel_alias: mdm_rel}}
    rel_map: dict = {}
    for r in cfg.get("relationship_types", []):
        rel_map.setdefault(r["entity_alias"], {})[r["rel_alias"]] = r["mdm_rel"]
    create_rel_map: dict = {}
    for r in cfg.get("create_relationship_types", []):
        create_rel_map.setdefault(r["from_entity_alias"], {})[r["rel_alias"]] = {
            "mdm_rel":   r["mdm_rel"],
            "to_entity": r["to_entity_alias"],
        }
    create_entity_fields_map: dict = {}
    for f in cfg.get("create_entity_fields", []):
        create_entity_fields_map.setdefault(f["entity_alias"], []).append({
            "label":    f["label"],
            "field":    f["field"],
            "required": f["required"],
        })
    return {
        "IDMC_USER":                  cfg["idmc_user"],
        "IDMC_PASS":                  cfg["idmc_pass"],
        "MDM_BASE_URL":               cfg["mdm_base_url"],
        "IDMC_LOGIN_HOST":            cfg["idmc_login_host"],
        "OAUTH_CLIENT_ID":            cfg["oauth_client_id"],
        "OAUTH_CLIENT_SECRET":        cfg["oauth_client_secret"],
        "ENTITY_TYPES":               json.dumps(entity_map,       separators=(",", ":")),
        "RELATIONSHIP_TYPES":         json.dumps(rel_map,          separators=(",", ":")),
        "CREATE_REL_SOURCE_SYSTEM":   cfg.get("create_rel_source_system", "c360.default.system"),
        "CREATE_RELATIONSHIP_TYPES":  json.dumps(create_rel_map,   separators=(",", ":")),
        "CREATE_ENTITY_FIELDS":       json.dumps(create_entity_fields_map, separators=(",", ":")),
    }


async def get_index(request: Request):
    return HTMLResponse((PROJECT_DIR / "ui.html").read_text())


def _load_env_yaml() -> dict:
    """Parse env.yaml (single-quoted YAML) and return a flat key→value dict."""
    path = PROJECT_DIR / "env.yaml"
    if not path.exists():
        return {}
    result = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Split on first ": " — keys never contain spaces; values may contain bare colons (JSON)
        key, sep, rest = line.partition(": ")
        if not sep:
            continue
        val = rest.strip()
        # strip wrapping single-quotes and unescape doubled single-quotes
        if val.startswith("'") and val.endswith("'"):
            val = val[1:-1].replace("''", "'")
        result[key.strip()] = val
    return result


def _defaults_from_env_yaml(env: dict) -> dict:
    """Convert a flat env.yaml dict back into the DEFAULTS shape for the UI."""
    d = dict(DEFAULTS)  # shallow copy so we don't mutate the module-level dict

    if "IDMC_USER"         in env: d["idmc_user"]         = env["IDMC_USER"]
    if "IDMC_PASS"         in env: d["idmc_pass"]         = env["IDMC_PASS"]
    if "MDM_BASE_URL"      in env: d["mdm_base_url"]      = env["MDM_BASE_URL"]
    if "IDMC_LOGIN_HOST"   in env: d["idmc_login_host"]   = env["IDMC_LOGIN_HOST"]
    if "OAUTH_CLIENT_ID"   in env: d["oauth_client_id"]   = env["OAUTH_CLIENT_ID"]
    if "OAUTH_CLIENT_SECRET" in env: d["oauth_client_secret"] = env["OAUTH_CLIENT_SECRET"]
    if "CREATE_REL_SOURCE_SYSTEM" in env:
        d["create_rel_source_system"] = env["CREATE_REL_SOURCE_SYSTEM"]

    if "ENTITY_TYPES" in env:
        try:
            et = json.loads(env["ENTITY_TYPES"])
            d["entity_types"] = [{"alias": k, "mdm_type": v} for k, v in et.items()]
        except Exception:
            pass

    if "RELATIONSHIP_TYPES" in env:
        try:
            rt = json.loads(env["RELATIONSHIP_TYPES"])
            rows = []
            for entity_alias, rels in rt.items():
                for rel_alias, mdm_rel in rels.items():
                    rows.append({"entity_alias": entity_alias, "rel_alias": rel_alias, "mdm_rel": mdm_rel})
            d["relationship_types"] = rows
        except Exception:
            pass

    if "CREATE_RELATIONSHIP_TYPES" in env:
        try:
            crt = json.loads(env["CREATE_RELATIONSHIP_TYPES"])
            rows = []
            for from_alias, rels in crt.items():
                for rel_alias, cfg in rels.items():
                    rows.append({
                        "from_entity_alias": from_alias,
                        "rel_alias":         rel_alias,
                        "to_entity_alias":   cfg["to_entity"],
                        "mdm_rel":           cfg["mdm_rel"],
                    })
            d["create_relationship_types"] = rows
        except Exception:
            pass

    if "CREATE_ENTITY_FIELDS" in env:
        try:
            cef = json.loads(env["CREATE_ENTITY_FIELDS"])
            rows = []
            for entity_alias, fields in cef.items():
                for f in fields:
                    rows.append({
                        "entity_alias": entity_alias,
                        "label":        f["label"],
                        "field":        f["field"],
                        "required":     f["required"],
                    })
            d["create_entity_fields"] = rows
        except Exception:
            pass

    return d


async def get_defaults(request: Request):
    env = _load_env_yaml()
    data = _defaults_from_env_yaml(env) if env else DEFAULTS
    return JSONResponse(data)


async def get_status(request: Request):
    p = request.query_params
    proc = await asyncio.create_subprocess_exec(
        "gcloud", "run", "services", "describe",
        p.get("service_name", DEFAULTS["service_name"]),
        "--region",  p.get("region",  DEFAULTS["region"]),
        "--project", p.get("project", DEFAULTS["project"]),
        "--format", "value(status.url)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    url = stdout.decode().strip()
    deployed = proc.returncode == 0 and bool(url)
    return JSONResponse({"deployed": deployed, "url": url or None})


async def _stream(cmd: list):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(PROJECT_DIR),
    )
    async for line in proc.stdout:
        yield f"data: {line.decode(errors='replace').rstrip()}\n\n"
    await proc.wait()
    yield f"data: [EXIT:{proc.returncode}]\n\n"


async def post_deploy(request: Request):
    cfg = await request.json()
    env_vars = build_env_vars(cfg)
    env_yaml_path = PROJECT_DIR / "env.yaml"
    yaml_lines = "\n".join(f"{k}: '{v}'" for k, v in env_vars.items())
    env_yaml_path.write_text(yaml_lines + "\n")
    cmd = [
        "gcloud", "run", "deploy", cfg["service_name"],
        "--source", str(PROJECT_DIR),
        "--region", cfg["region"],
        "--project", cfg["project"],
        "--allow-unauthenticated",
        "--port", "8000",
        "--env-vars-file", str(env_yaml_path),
        "--set-build-env-vars", f"CACHEBUST={os.getpid()}",
    ]
    return StreamingResponse(
        _stream(cmd), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def post_undeploy(request: Request):
    cfg = await request.json()
    cmd = [
        "gcloud", "run", "services", "delete", cfg["service_name"],
        "--region", cfg["region"],
        "--project", cfg["project"],
        "--quiet",
    ]
    return StreamingResponse(
        _stream(cmd), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


app = Starlette(routes=[
    Route("/",          get_index),
    Route("/home",      get_index),
    Route("/mdm_config", get_index),
    Route("/defaults",  get_defaults),
    Route("/status",    get_status),
    Route("/deploy",    post_deploy,   methods=["POST"]),
    Route("/undeploy",  post_undeploy, methods=["POST"]),
])

if __name__ == "__main__":
    print("MDM MCP Configurator -> http://localhost:7000")
    uvicorn.run(app, host="127.0.0.1", port=7000)
