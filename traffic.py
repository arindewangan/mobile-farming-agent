"""Network interface byte counters for the admin panel's traffic readout.
Reads /sys/class/net directly (Linux only — e.g. Raspberry Pi OS — which is
the only place --listen mode runs) — no extra dependency like psutil needed."""
from __future__ import annotations

import os

IFACE_BASE = "/sys/class/net"
# Skip loopback and virtual/container interfaces — show only "real" links.
SKIP_PREFIXES = ("lo", "docker", "veth", "br-")


def _read_int(path: str) -> int:
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:  # noqa: BLE001
        return 0


def interface_counters() -> list[dict]:
    if not os.path.isdir(IFACE_BASE):
        return []
    out = []
    for name in sorted(os.listdir(IFACE_BASE)):
        if name.startswith(SKIP_PREFIXES):
            continue
        stats = os.path.join(IFACE_BASE, name, "statistics")
        if not os.path.isdir(stats):
            continue
        out.append({
            "iface": name,
            "rx_bytes": _read_int(os.path.join(stats, "rx_bytes")),
            "tx_bytes": _read_int(os.path.join(stats, "tx_bytes")),
        })
    return out
