"""
Fast, deterministic on-screen UI state for recipe watchers.

`detect.py` classifies the whole screen with a vision LLM (great for "is this
account blocked?") but it can't answer "is the text/element X on screen right
now, and where?". Watchers need exactly that — a cheap, sub-second poll that
returns whether a given element/popup is present and its tappable center — so
this uses a `uiautomator` XML dump (no LLM, ~0.3-0.8s) instead.

Matching is substring, case-insensitive, against a node's text, resource-id, and
content-desc — so a watcher query like "Allow", "com.app:id/close", or "Not now"
all work. Bounds are in the device's current-rotation pixel space, which is the
same space `adb shell input tap` uses, so a watcher can tap the returned center
directly.
"""
from __future__ import annotations

import base64
import re

import adb

_NODE = re.compile(r"<node\b[^>]*?/>", re.DOTALL)
_ATTR = re.compile(r'(\w[\w-]*)="([^"]*)"')
_BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _parse_nodes(xml: str) -> list[dict]:
    out = []
    for raw in _NODE.findall(xml):
        attrs = dict(_ATTR.findall(raw))
        out.append(attrs)
    return out


def _center(bounds: str):
    m = _BOUNDS.search(bounds or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return [(x1 + x2) // 2, (y1 + y2) // 2]


def _matches(node: dict, needle: str) -> bool:
    n = needle.lower()
    for key in ("text", "resource-id", "content-desc"):
        if n in (node.get(key, "") or "").lower():
            return True
    return False


def _find_node(nodes: list[dict], needle: str) -> dict | None:
    """Like `next((n for n in nodes if _matches(n, needle)), None)`, but a
    needle of "password" prefers a node with the platform's own
    `password="true"` marker over a text/resource-id substring hit. Found
    live on a real WebView-rendered Google sign-in form: the actual password
    <input> has no text, resource-id, or content-desc of its own, while a
    "Show password" checkbox right next to it does contain the substring
    "password" — without this, "password" as a needle would match the
    checkbox instead of the field."""
    if needle.lower() == "password":
        pw = next((n for n in nodes if n.get("password") == "true"), None)
        if pw:
            return pw
    return next((n for n in nodes if _matches(n, needle)), None)


async def ui_state(serial: str, queries: list[str] | None = None) -> dict:
    """Dump the current UI tree and report presence + center for each query.

    Returns {ok, matches: {query: {present, x, y, text, enabled}}, texts:
    [...visible text...]}. `enabled` ("true"/"false") lets a caller tell a
    genuinely tappable match from a rendered-but-not-yet-interactive one —
    e.g. some GMS consent screens disable their primary button for a moment,
    where a raw tap is a silent no-op. A dump failure returns ok=False with
    all queries absent — the watcher loop treats that as "nothing matched"
    and simply tries again next tick."""
    queries = queries or []
    try:
        await adb.shell(serial, "uiautomator dump /sdcard/mf_ui.xml")
        r = await adb.shell(serial, "cat /sdcard/mf_ui.xml")
        xml = r.get("stdout", "")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e),
                "matches": {q: {"present": False, "x": None, "y": None} for q in queries},
                "texts": []}

    nodes = _parse_nodes(xml)
    matches: dict[str, dict] = {}
    for q in queries:
        hit = _find_node(nodes, q)
        if hit:
            c = _center(hit.get("bounds", "")) or [None, None]
            matches[q] = {"present": True, "x": c[0], "y": c[1], "text": (hit.get("text") or "").strip(),
                          "enabled": hit.get("enabled", "true")}
        else:
            matches[q] = {"present": False, "x": None, "y": None, "text": "", "enabled": "true"}

    # compact list of visible texts — handy when authoring/debugging watchers
    seen, texts = set(), []
    for n in nodes:
        t = (n.get("text") or "").strip()
        if t and t not in seen:
            seen.add(t)
            texts.append(t)
        if len(texts) >= 60:
            break
    return {"ok": bool(xml), "matches": matches, "texts": texts}


_DIAG_ATTRS = ("text", "resource-id", "content-desc", "class", "clickable", "enabled", "bounds", "package")


async def ui_dump_diagnostic(serial: str, needle: str = "") -> dict:
    """Full node-level dump — bounds, class, clickable, resource-id, not just
    text — for elements matching `needle`, plus every clickable element on
    screen as a fallback. `ui_state()` only surfaces a computed tap center
    and stripped text, which is enough for normal watcher use but not enough
    to root-cause a tap that silently misses: this is for that case — e.g.
    telling apart a real native widget (clickable=true, class=android.widget.*)
    from WebView-rendered content whose reported bounds may not correspond to
    an actually-tappable screen location."""
    try:
        await adb.shell(serial, "uiautomator dump /sdcard/mf_ui.xml")
        r = await adb.shell(serial, "cat /sdcard/mf_ui.xml")
        xml = r.get("stdout", "")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "node_count": 0, "text_matches": [], "clickable_nodes": []}

    nodes = _parse_nodes(xml)

    def _summary(n: dict) -> dict:
        d = {k: n.get(k, "") for k in _DIAG_ATTRS}
        d["center"] = _center(n.get("bounds", ""))
        return d

    text_matches = [_summary(n) for n in nodes if needle and _matches(n, needle)]
    clickable_nodes = [_summary(n) for n in nodes if n.get("clickable") == "true"]
    return {"ok": bool(xml), "node_count": len(nodes),
            "text_matches": text_matches[:10], "clickable_nodes": clickable_nodes[:20]}


async def match_template(serial: str, template_b64: str, threshold: float = 0.82) -> dict:
    """Find a template image on the current screen via normalized cross-correlation.

    Complements text/vision watchers for icons and images that carry no text /
    resource-id (a logo, a specific button graphic, a captcha widget). Returns
    {ok, present, score, x, y} where (x,y) is the matched region's center in
    device pixels — directly tappable."""
    import cv2  # local import: only the agent host needs opencv
    import numpy as np

    jpg = await adb.screencap_full_jpeg(serial, quality=90)
    if not jpg:
        return {"ok": False, "present": False, "error": "screencap failed"}
    try:
        screen = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_GRAYSCALE)
        tmpl = cv2.imdecode(np.frombuffer(base64.b64decode(template_b64), np.uint8), cv2.IMREAD_GRAYSCALE)
        if screen is None or tmpl is None or tmpl.shape[0] > screen.shape[0] or tmpl.shape[1] > screen.shape[1]:
            return {"ok": False, "present": False, "error": "bad image / template larger than screen"}
        res = cv2.matchTemplate(screen, tmpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        h, w = tmpl.shape
        cx, cy = max_loc[0] + w // 2, max_loc[1] + h // 2
        present = max_val >= threshold
        return {"ok": True, "present": bool(present), "score": round(float(max_val), 3),
                "x": int(cx) if present else None, "y": int(cy) if present else None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "present": False, "error": str(e)}
