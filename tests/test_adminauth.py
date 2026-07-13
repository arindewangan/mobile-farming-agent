"""
adminauth.py: the agent's local admin-panel password/session gate, plus
the change-password flow — this mints/holds the token that grants full
control of every device attached to the agent, so it gets real coverage.
"""
from __future__ import annotations

import adminauth


def _auth(tmp_path, password="orig-pw"):
    path = str(tmp_path / "admin_password.txt")
    return adminauth.SessionAuth(password, path=path), path


def test_login_with_correct_password_returns_a_session_id(tmp_path):
    auth, _ = _auth(tmp_path)
    sid = auth.login("orig-pw")
    assert sid
    assert auth.is_valid(sid)


def test_login_with_wrong_password_returns_none(tmp_path):
    auth, _ = _auth(tmp_path)
    assert auth.login("wrong") is None


def test_logout_invalidates_the_session(tmp_path):
    auth, _ = _auth(tmp_path)
    sid = auth.login("orig-pw")
    auth.logout(sid)
    assert not auth.is_valid(sid)


def test_change_password_rejects_wrong_old_password(tmp_path):
    auth, _ = _auth(tmp_path)
    assert auth.change_password("wrong-old", "brand-new-pw") is None
    assert auth.password == "orig-pw"


def test_change_password_with_correct_old_password_updates_and_returns_new_session(tmp_path):
    auth, _ = _auth(tmp_path)
    sid = auth.change_password("orig-pw", "brand-new-pw")
    assert sid
    assert auth.password == "brand-new-pw"
    assert auth.is_valid(sid)


def test_change_password_persists_to_disk(tmp_path):
    auth, path = _auth(tmp_path)
    auth.change_password("orig-pw", "brand-new-pw")
    assert open(path, encoding="utf-8").read().strip() == "brand-new-pw"


def test_change_password_invalidates_other_existing_sessions(tmp_path):
    auth, _ = _auth(tmp_path)
    other_sid = auth.login("orig-pw")
    assert auth.is_valid(other_sid)
    auth.change_password("orig-pw", "brand-new-pw")
    assert not auth.is_valid(other_sid)


def test_old_password_no_longer_works_after_change(tmp_path):
    auth, _ = _auth(tmp_path)
    auth.change_password("orig-pw", "brand-new-pw")
    assert auth.login("orig-pw") is None
    assert auth.login("brand-new-pw")
