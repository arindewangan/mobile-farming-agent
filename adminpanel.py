"""Local admin panel for agent.py's --listen (server) mode — lets an operator
sitting at the edge device (or reaching it over Tailscale) manage everything
about how this agent accepts control-plane connections: named connection
tokens (each scoped to local/remote/both), an IP ban list, a global remote-access kill
switch, and live status (Tailscale details, LAN/remote addresses, connected
ADB devices, network traffic, who's currently connected).

Runs as its own small FastAPI app alongside the agent's websockets.serve
loop, in the same asyncio event loop (see agent.py's --listen startup).
Gated by adminauth's session password — creating a connection here mints a
token with full device control, so this can't be left open just because
it's reachable over Tailscale.
"""
from __future__ import annotations

import os

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import adminauth
import bans as bans_mod
import connections as connections_mod
import githubupdate
import netinfo
import selfupdate
import traffic

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adminstatic")


# Pydantic request models MUST live at module scope, not inside create_app():
# with `from __future__ import annotations` active, FastAPI resolves a route's
# type hints via the function's __globals__ — a class local to create_app()
# isn't in there, so FastAPI can't tell it's a body model and silently treats
# the param as an unresolvable query param instead.
class LoginReq(BaseModel):
    password: str


class ChangePasswordReq(BaseModel):
    old_password: str
    new_password: str


class CreateReq(BaseModel):
    name: str
    scope: str = "both"


class ToggleReq(BaseModel):
    enabled: bool


class ScopeReq(BaseModel):
    scope: str


class BanReq(BaseModel):
    ip: str
    reason: str = ""


class SettingsReq(BaseModel):
    allow_remote: bool


class VisibilityReq(BaseModel):
    hidden: bool


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "token"} | {"token_preview": row["token"][:6] + "…"}


def create_app(
    store: connections_mod.ConnectionStore, agent, listen_host: str, listen_port: int,
    ban_store: bans_mod.BanStore, settings_store,
) -> FastAPI:
    app = FastAPI(title="Mobile Farming — Agent Admin")
    password = adminauth.load_or_create_password()
    auth = adminauth.SessionAuth(password)

    def require_session(request: Request) -> None:
        if not auth.is_valid(request.cookies.get("mf_admin_session")):
            raise HTTPException(status_code=401, detail="not logged in")

    @app.post("/api/login")
    async def login(req: LoginReq, response: Response):
        sid = auth.login(req.password)
        if not sid:
            raise HTTPException(status_code=401, detail="wrong password")
        response.set_cookie("mf_admin_session", sid, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response):
        auth.logout(request.cookies.get("mf_admin_session"))
        response.delete_cookie("mf_admin_session")
        return {"ok": True}

    @app.get("/api/session")
    async def session(request: Request):
        return {"logged_in": auth.is_valid(request.cookies.get("mf_admin_session"))}

    @app.post("/api/change-password", dependencies=[Depends(require_session)])
    async def change_password(req: ChangePasswordReq, response: Response):
        new_pw = req.new_password.strip()
        if len(new_pw) < 8:
            raise HTTPException(status_code=400, detail="new password must be at least 8 characters")
        sid = auth.change_password(req.old_password, new_pw)
        if not sid:
            raise HTTPException(status_code=401, detail="current password is wrong")
        response.set_cookie("mf_admin_session", sid, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
        return {"ok": True}

    @app.get("/api/status", dependencies=[Depends(require_session)])
    async def status():
        ts = await netinfo.tailscale_status()
        lip = netinfo.local_ip()
        remote_ip = (ts.get("ips") or [None])[0] if ts.get("installed") else None
        settings = settings_store.get_all()
        return {
            "listen_port": listen_port,
            "connected": agent.ws is not None,
            "device_count": len(agent.devices),
            "current_peer": agent.current_peer,
            # Additional read-only connections beyond the primary above (see
            # agent.py's Agent.observers) — never includes the ws/lock objects.
            "observers": [
                {"connection_id": cid, "ip": info["ip"], "scope": info["scope"],
                 "connection_name": info["connection_name"], "connected_since": info["connected_since"]}
                for cid, info in agent.observers.items()
            ],
            "local_ip": lip,
            "local_addr": f"ws://{lip}:{listen_port}" if lip else None,
            "remote_ip": remote_ip,
            "remote_addr": f"ws://{remote_ip}:{listen_port}" if remote_ip else None,
            "tailscale": ts,
            "allow_remote": settings["allow_remote"],
            "traffic": {
                "interfaces": traffic.interface_counters(),
                "session_bytes_sent": agent.bytes_sent,
                "session_bytes_received": agent.bytes_received,
            },
        }

    @app.post("/api/kick", dependencies=[Depends(require_session)])
    async def kick():
        if not await agent.kick():
            raise HTTPException(status_code=400, detail="nothing is connected")
        return {"ok": True}

    @app.post("/api/observers/{connection_id}/kick", dependencies=[Depends(require_session)])
    async def kick_observer(connection_id: str):
        if not await agent.kick_observer(connection_id):
            raise HTTPException(status_code=404, detail="no such observer connection")
        return {"ok": True}

    # ---- self-update — checks GitHub Releases directly (no control-plane
    # connection required); complements the push-from-backend path that
    # still exists for boxes that only reach the control plane over LAN/
    # Tailscale, never the open internet. ----------------------------------
    @app.get("/api/update/check", dependencies=[Depends(require_session)])
    async def update_check():
        return await githubupdate.check_latest()

    @app.post("/api/update/apply", dependencies=[Depends(require_session)])
    async def update_apply():
        result = await githubupdate.apply_latest()
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "update failed"))
        selfupdate.schedule_restart()
        return result

    @app.get("/api/adb-devices", dependencies=[Depends(require_session)])
    async def adb_devices():
        hidden = agent.hidden_store.list_hidden() if agent.hidden_store else set()
        return {"devices": [{"serial": s, "hidden": s in hidden, **info} for s, info in agent.devices.items()]}

    @app.post("/api/adb-devices/{serial}/visibility", dependencies=[Depends(require_session)])
    async def set_device_visibility(serial: str, req: VisibilityReq):
        if not agent.hidden_store:
            raise HTTPException(status_code=500, detail="visibility store unavailable")
        if serial not in agent.devices:
            raise HTTPException(status_code=404, detail="device not found")
        (agent.hidden_store.hide if req.hidden else agent.hidden_store.show)(serial)
        await agent.push_devices_update()
        return {"ok": True}

    @app.get("/api/connection-log", dependencies=[Depends(require_session)])
    async def connection_log():
        return {"log": list(agent.connection_log)}

    @app.get("/api/settings", dependencies=[Depends(require_session)])
    async def get_settings():
        return settings_store.get_all()

    @app.post("/api/settings", dependencies=[Depends(require_session)])
    async def update_settings(req: SettingsReq):
        settings_store.set_allow_remote(req.allow_remote)
        return {"ok": True}

    # -- connections (tokens) ----------------------------------------------
    @app.get("/api/connections", dependencies=[Depends(require_session)])
    async def list_connections():
        return {"connections": [_public(r) for r in store.list_all()]}

    @app.post("/api/connections", dependencies=[Depends(require_session)])
    async def create_connection(req: CreateReq):
        if req.scope not in connections_mod.VALID_SCOPES:
            raise HTTPException(status_code=400, detail="scope must be local, remote, or both")
        row = store.create(req.name, req.scope)
        return row  # unmasked ONCE, right after creation — the only time the token is shown

    @app.get("/api/connections/{conn_id}/token", dependencies=[Depends(require_session)])
    async def reveal_token(conn_id: int):
        token = store.get_token(conn_id)
        if token is None:
            raise HTTPException(status_code=404, detail="connection not found")
        return {"token": token}

    @app.post("/api/connections/{conn_id}/toggle", dependencies=[Depends(require_session)])
    async def toggle_connection(conn_id: int, req: ToggleReq):
        if not store.toggle(conn_id, req.enabled):
            raise HTTPException(status_code=404, detail="connection not found")
        return {"ok": True}

    @app.post("/api/connections/{conn_id}/scope", dependencies=[Depends(require_session)])
    async def set_connection_scope(conn_id: int, req: ScopeReq):
        if req.scope not in connections_mod.VALID_SCOPES:
            raise HTTPException(status_code=400, detail="scope must be local, remote, or both")
        if not store.set_scope(conn_id, req.scope):
            raise HTTPException(status_code=404, detail="connection not found")
        return {"ok": True}

    @app.delete("/api/connections/{conn_id}", dependencies=[Depends(require_session)])
    async def delete_connection(conn_id: int):
        if not store.delete(conn_id):
            raise HTTPException(status_code=404, detail="connection not found")
        return {"ok": True}

    # -- banned IPs ----------------------------------------------------------
    @app.get("/api/bans", dependencies=[Depends(require_session)])
    async def list_bans():
        return {"bans": ban_store.list_all()}

    @app.post("/api/bans", dependencies=[Depends(require_session)])
    async def add_ban(req: BanReq):
        ip = req.ip.strip()
        if not ip:
            raise HTTPException(status_code=400, detail="ip is required")
        return ban_store.ban(ip, req.reason)

    @app.post("/api/bans/current", dependencies=[Depends(require_session)])
    async def ban_current():
        peer = agent.current_peer
        if not peer or not peer.get("ip"):
            raise HTTPException(status_code=400, detail="no active connection to ban")
        row = ban_store.ban(peer["ip"], "banned from admin panel while connected")
        await agent.kick()
        return row

    @app.delete("/api/bans/{ip}", dependencies=[Depends(require_session)])
    async def remove_ban(ip: str):
        if not ban_store.unban(ip):
            raise HTTPException(status_code=404, detail="that ip isn't banned")
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    return app
