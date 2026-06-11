#!/usr/bin/env python3
"""
MDM MCP Configurator — local UI to configure and deploy dbx-mdm-mcp to Cloud Run.
Run: python ui.py   then open http://localhost:7000
"""
import asyncio
import json
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
}


def build_env_vars(cfg: dict) -> dict:
    entity_map = {r["alias"]: r["mdm_type"] for r in cfg["entity_types"]}
    return {
        "IDMC_USER":           cfg["idmc_user"],
        "IDMC_PASS":           cfg["idmc_pass"],
        "MDM_BASE_URL":        cfg["mdm_base_url"],
        "IDMC_LOGIN_HOST":     cfg["idmc_login_host"],
        "OAUTH_CLIENT_ID":     cfg["oauth_client_id"],
        "OAUTH_CLIENT_SECRET": cfg["oauth_client_secret"],
        "ENTITY_TYPES":        json.dumps(entity_map, separators=(",", ":")),
    }


async def get_index(request: Request):
    return HTMLResponse((PROJECT_DIR / "ui.html").read_text())


async def get_defaults(request: Request):
    return JSONResponse(DEFAULTS)


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
    env_str = "^|^" + "|".join(f"{k}={v}" for k, v in env_vars.items())
    cmd = [
        "gcloud", "run", "deploy", cfg["service_name"],
        "--source", str(PROJECT_DIR),
        "--region", cfg["region"],
        "--project", cfg["project"],
        "--allow-unauthenticated",
        "--port", "8000",
        "--set-env-vars", env_str,
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
    Route("/",         get_index),
    Route("/defaults", get_defaults),
    Route("/status",   get_status),
    Route("/deploy",   post_deploy,   methods=["POST"]),
    Route("/undeploy", post_undeploy, methods=["POST"]),
])

if __name__ == "__main__":
    print("MDM MCP Configurator -> http://localhost:7000")
    uvicorn.run(app, host="127.0.0.1", port=7000)
