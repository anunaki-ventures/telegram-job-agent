"""No-network tests: approvals never send any Telegram messages."""

import sqlite3

import pytest

from outbound_policy import approve, is_approved, message_fingerprint


@pytest.fixture
def queue(tmp_path):
    cv = tmp_path / "approved-cv.pdf"
    cv.write_bytes(b"%PDF-1.4 original CV test bytes")
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        """CREATE TABLE send_queue(
             id INTEGER PRIMARY KEY,job_id TEXT,recipient TEXT,message TEXT,
             cv_path TEXT,status TEXT,error TEXT,retry_at TEXT
        )"""
    )
    con.execute(
        """INSERT INTO send_queue(id,job_id,recipient,message,cv_path,status)
           VALUES(1,'job-123','@employer','Dear hiring team',?,'held_approval')""",
        (str(cv),),
    )
    yield con, cv
    con.close()


def row(con):
    return con.execute("SELECT * FROM send_queue WHERE id=1").fetchone()


def test_default_is_unapproved(queue):
    con, cv = queue
    assert is_approved(con, row(con), cv) is False


def test_exact_approval_releases_only_that_item(queue):
    con, cv = queue
    fingerprint = message_fingerprint(row(con), cv)
    result = approve(con, row(con), cv, fingerprint, "founder-confirmed-2026-11-13")
    assert result == fingerprint
    assert is_approved(con, row(con), cv) is True
    assert row(con)["status"] == "pending"


@pytest.mark.parametrize("field,value", [
    ("message", "Different motivation letter"),
    ("recipient", "@somebodyelse"),
    ("job_id", "job-456"),
])
def test_material_change_invalidates_approval(queue, field, value):
    con, cv = queue
    fingerprint = message_fingerprint(row(con), cv)
    approve(con, row(con), cv, fingerprint, "explicit-founder-confirmation")
    con.execute(f"UPDATE send_queue SET {field}=? WHERE id=1", (value,))
    assert is_approved(con, row(con), cv) is False


def test_cv_replacement_invalidates_approval(queue):
    con, cv = queue
    fingerprint = message_fingerprint(row(con), cv)
    approve(con, row(con), cv, fingerprint, "explicit-founder-confirmation")
    cv.write_bytes(b"%PDF-1.4 REPLACED test CV bytes")
    assert is_approved(con, row(con), cv) is False


def test_stale_preview_cannot_approve(queue):
    con, cv = queue
    fingerprint = message_fingerprint(row(con), cv)
    con.execute("UPDATE send_queue SET message='Modified' WHERE id=1")
    with pytest.raises(ValueError, match="fingerprint changed"):
        approve(con, row(con), cv, fingerprint, "explicit-founder-confirmation")
    assert is_approved(con, row(con), cv) is False


def test_missing_founder_confirmation_is_refused(queue):
    con, cv = queue
    fingerprint = message_fingerprint(row(con), cv)
    with pytest.raises(ValueError, match="founder_confirmation"):
        approve(con, row(con), cv, fingerprint, "yes")
    assert is_approved(con, row(con), cv) is False


def test_missing_cv_fails_closed(queue):
    con, cv = queue
    fingerprint = message_fingerprint(row(con), cv)
    approve(con, row(con), cv, fingerprint, "explicit-founder-confirmation")
    cv.unlink()
    assert is_approved(con, row(con), cv) is False
