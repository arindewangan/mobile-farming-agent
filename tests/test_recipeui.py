"""
ui_dump_diagnostic(): the richer node-level dump added after a Google
sign-in automation fix (dismissing an intro screen's "Next" button) worked
in every unit test and passed live redeploy verification, but still failed
identically against real hardware — visible_texts alone couldn't say why the
tap missed. This surfaces bounds/class/clickable/resource-id per node so a
WebView-rendered button (inaccurate bounds) can be told apart from a real
native widget from the API response alone.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import adb
import recipeui

pytestmark = pytest.mark.asyncio

_XML = """<hierarchy><node text="Welcome" resource-id="" content-desc="" class="android.widget.TextView" clickable="false" enabled="true" bounds="[0,100][1080,300]" package="com.google.android.gms" />
<node text="NEXT" resource-id="" content-desc="" class="android.webkit.WebView" clickable="false" enabled="true" bounds="[0,0][1080,2280]" package="com.google.android.gms" />
<node text="" resource-id="com.android.settings:id/button1" content-desc="Cancel" class="android.widget.Button" clickable="true" enabled="true" bounds="[100,2000][500,2100]" package="com.android.settings" />
</hierarchy>"""


def _stub_dump(monkeypatch, xml: str = _XML):
    async def fake_shell(serial, cmd):
        if cmd.startswith("uiautomator dump"):
            return {"ok": True, "rc": 0, "stdout": "", "stderr": ""}
        if cmd.startswith("cat "):
            return {"ok": True, "rc": 0, "stdout": xml, "stderr": ""}
        return {"ok": True, "rc": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(adb, "shell", AsyncMock(side_effect=fake_shell))


async def test_ui_dump_diagnostic_reports_clickable_and_bounds_for_needle_matches(monkeypatch):
    _stub_dump(monkeypatch)
    out = await recipeui.ui_dump_diagnostic("S1", needle="next")
    assert out["ok"] is True
    assert out["node_count"] == 3
    assert len(out["text_matches"]) == 1
    m = out["text_matches"][0]
    assert m["text"] == "NEXT"
    assert m["class"] == "android.webkit.WebView"
    assert m["clickable"] == "false"  # exactly the WebView-vs-native distinction this exists to surface
    assert m["center"] == [540, 1140]


async def test_ui_dump_diagnostic_lists_all_clickable_nodes_regardless_of_needle(monkeypatch):
    _stub_dump(monkeypatch)
    out = await recipeui.ui_dump_diagnostic("S1", needle="")
    assert out["text_matches"] == []  # empty needle matches nothing
    assert len(out["clickable_nodes"]) == 1
    assert out["clickable_nodes"][0]["resource-id"] == "com.android.settings:id/button1"


async def test_ui_state_reports_enabled_false_for_a_disabled_matched_node(monkeypatch):
    xml = """<hierarchy><node text="NEXT" resource-id="" content-desc="" class="android.widget.Button"
        clickable="true" enabled="false" bounds="[756,1596][1020,1740]" package="com.google.android.gms" />
    </hierarchy>"""
    _stub_dump(monkeypatch, xml)
    out = await recipeui.ui_state("S1", ["Next"])
    assert out["matches"]["Next"]["present"] is True
    assert out["matches"]["Next"]["enabled"] == "false"


async def test_ui_state_defaults_enabled_true_when_the_query_isnt_present(monkeypatch):
    _stub_dump(monkeypatch, "<hierarchy></hierarchy>")
    out = await recipeui.ui_state("S1", ["Next"])
    assert out["matches"]["Next"]["present"] is False
    assert out["matches"]["Next"]["enabled"] == "true"


async def test_ui_state_password_needle_prefers_the_real_password_field_over_a_toggle(monkeypatch):
    """Regression test for a real failure found live on Google's WebView-
    rendered sign-in form: the actual password <input> has no text,
    resource-id, or content-desc of its own (only the platform's own
    password="true" marker), while a "Show password" checkbox right next to
    it does contain the substring "password" — and appears LATER in the
    node list. The "password" needle must resolve to the real field, not
    the checkbox, regardless of document order."""
    xml = """<hierarchy>
    <node text="" resource-id="" content-desc="" class="android.widget.EditText"
        clickable="true" enabled="true" password="true" bounds="[78,276][1002,435]" package="com.google.android.gms" />
    <node text="Show password" resource-id="" content-desc="" class="android.widget.CheckBox"
        clickable="true" enabled="true" bounds="[36,426][180,573]" package="com.google.android.gms" />
    </hierarchy>"""
    _stub_dump(monkeypatch, xml)
    out = await recipeui.ui_state("S1", ["password"])
    m = out["matches"]["password"]
    assert m["present"] is True
    assert m["x"] == (78 + 1002) // 2  # the EditText's center, not the checkbox's
    assert m["y"] == (276 + 435) // 2


async def test_ui_state_password_needle_falls_back_to_text_match_without_the_attribute(monkeypatch):
    xml = """<hierarchy><node text="Enter your password" resource-id="" content-desc=""
        class="android.widget.TextView" clickable="false" enabled="true"
        bounds="[100,200][900,300]" package="com.google.android.gms" /></hierarchy>"""
    _stub_dump(monkeypatch, xml)
    out = await recipeui.ui_state("S1", ["password"])
    assert out["matches"]["password"]["present"] is True


async def test_ui_dump_diagnostic_handles_a_dump_failure_gracefully(monkeypatch):
    async def raising_shell(serial, cmd):
        raise TimeoutError("adb timed out")
    monkeypatch.setattr(adb, "shell", AsyncMock(side_effect=raising_shell))

    out = await recipeui.ui_dump_diagnostic("S1", needle="next")
    assert out["ok"] is False
    assert "error" in out
    assert out["text_matches"] == []
    assert out["clickable_nodes"] == []
