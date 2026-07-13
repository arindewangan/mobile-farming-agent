"""Network classification + Tailscale status for the admin panel — figures out
whether an incoming connection is on the same LAN as this device or arriving
from elsewhere (Tailscale/internet), and surfaces enough Tailscale detail
(hostname, IPs, peers) for the operator to see without SSHing in."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from typing import Optional

TAILSCALE_CGNAT = ipaddress.IPv4Network("100.64.0.0/10")


def local_ip() -> Optional[str]:
    """This host's LAN-facing IP, found via the connect-a-UDP-socket trick (no
    packet actually sent) — works regardless of interface name/count."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:  # noqa: BLE001
        return None


def _local_subnets() -> list[ipaddress.IPv4Network]:
    """Best-effort /24s this device is directly attached to, excluding its
    Tailscale CGNAT address (that one counts as remote, not LAN). Pure-stdlib
    socket enumeration — no subprocess involved, so it can't block the event
    loop, and it works cross-platform (unlike GNU `hostname -I`, which doesn't
    exist on Windows, this project's actual target/dev platform)."""
    nets: list[ipaddress.IPv4Network] = []
    seen: set[str] = set()
    candidates: list[str] = []
    try:
        candidates.append(socket.gethostbyname(socket.gethostname()))
    except Exception:  # noqa: BLE001
        pass
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        candidates.extend(addrs)
    except Exception:  # noqa: BLE001
        pass
    ip = local_ip()
    if ip:
        candidates.append(ip)
    for tok in candidates:
        if tok in seen:
            continue
        seen.add(tok)
        try:
            addr = ipaddress.IPv4Address(tok)
        except ValueError:
            continue
        if addr.is_loopback or addr in TAILSCALE_CGNAT:
            continue
        nets.append(ipaddress.IPv4Network(f"{addr}/24", strict=False))
    return nets


def classify_peer(ip_str: Optional[str]) -> str:
    """"local" if the peer is on the same LAN /24 as this device (or loopback),
    else "remote" — covers Tailscale and anything on the open internet."""
    if not ip_str:
        return "remote"
    try:
        ip = ipaddress.IPv4Address(ip_str)
    except ValueError:
        return "remote"
    if ip.is_loopback:
        return "local"
    return "local" if any(ip in net for net in _local_subnets()) else "remote"


async def tailscale_status() -> dict:
    """Parsed `tailscale status --json` — hostname, IPs, peer online states.
    Reports installed=False if tailscale isn't installed / isn't up."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale", "status", "--json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        data = json.loads(out.decode(errors="replace"))
    except Exception:  # noqa: BLE001
        return {"installed": False}

    self_info = data.get("Self", {}) or {}
    peers = data.get("Peer", {}) or {}
    peer_list = [
        {
            "hostname": p.get("HostName"),
            "ip": (p.get("TailscaleIPs") or [None])[0],
            "online": bool(p.get("Online")),
            "os": p.get("OS"),
        }
        for p in peers.values()
    ]
    return {
        "installed": True,
        "backend_state": data.get("BackendState"),
        "hostname": self_info.get("HostName"),
        "ips": self_info.get("TailscaleIPs", []),
        "online": bool(self_info.get("Online", True)),
        "peers": peer_list,
    }
