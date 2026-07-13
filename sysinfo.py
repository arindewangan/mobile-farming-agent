"""Tiny system helper for the admin panel — "what's my Tailscale IP" so the
operator doesn't have to hunt for the address to paste into the dashboard."""
from __future__ import annotations

import asyncio
from typing import Optional


async def tailscale_ip() -> Optional[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale", "ip", "-4",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        lines = out.decode(errors="replace").strip().splitlines()
        return lines[0].strip() if lines else None
    except Exception:  # noqa: BLE001 — tailscale not installed / not up
        return None
