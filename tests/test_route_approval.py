import json
import sqlite3

import channel_router
from route_approval import approve, approved_metadata, route_fingerprint


def prepare_route(tmp_path):
    dbpath = tmp_path / "job-agent.db"
    con = sqlite3.connect(dbpath)
    con.row_factory = sqlite3.Row
    channel_router.ensure_schema(con)
    con.execute(
        """INSERT INTO application_routes(
             job_id,route_kind,target,target_key,status,subject,message,
             cv_variant,payload_json,created_at,updated_at
           ) VALUES('job-A','email','hr@example.org','hr@example.org',
                    'pending','Application','My approved motivation','general','{}',
                    '2026-11-13T00:00:00+00:00','2026-11-13T00:00:00+00:00')"""
    )
    con.commit()
    cv = tmp_path / "candidate.pdf"
    cv.write_bytes(b"CV - user-approved version")
    row = con.execute("SELECT * FROM application_routes WHERE id=1").fetchone()
    return dbpath, con, row, cv


def test_unapproved_route_cannot_be_claimed_even_when_enabled(tmp_path, monkeypatch, capsys):
    path, con, row, cv = prepare_route(tmp_path)
    con.close()
    monkeypatch.setattr(channel_router, "DB", path)
    monkeypatch.setenv("TG_CHANNEL_OUTBOUND_ENABLED", "1")
    channel_router.claim("email", 5)
    assert json.loads(capsys.readouterr().out) == []
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT status FROM application_routes WHERE id=1").fetchone()[0] == "pending"


def test_channel_disabled_even_for_approved_route(tmp_path, monkeypatch, capsys):
    path, con, row, cv = prepare_route(tmp_path)
    approve(con, row, cv, route_fingerprint(row, cv), "founder-approved-message-and-cv")
    con.close()
    monkeypatch.setattr(channel_router, "DB", path)
    monkeypatch.delenv("TG_CHANNEL_OUTBOUND_ENABLED", raising=False)
    channel_router.claim("email", 5)
    assert json.loads(capsys.readouterr().out) == []


def test_exact_route_claim_includes_reviewed_cv_path(tmp_path, monkeypatch, capsys):
    path, con, row, cv = prepare_route(tmp_path)
    approve(con, row, cv, route_fingerprint(row, cv), "founder-approved-message-and-cv")
    con.close()
    monkeypatch.setattr(channel_router, "DB", path)
    monkeypatch.setenv("TG_CHANNEL_OUTBOUND_ENABLED", "1")
    channel_router.claim("email", 5)
    output = json.loads(capsys.readouterr().out)
    assert len(output) == 1
    assert output[0]["approved_cv_path"] == str(cv.resolve())
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT status FROM application_routes WHERE id=1").fetchone()[0] == "processing"


def test_cv_or_message_change_invalidates_route_approval(tmp_path):
    path, con, row, cv = prepare_route(tmp_path)
    approve(con, row, cv, route_fingerprint(row, cv), "founder-approved-message-and-cv")
    assert approved_metadata(con, row) is not None
    cv.write_bytes(b"CV - changed without approval")
    assert approved_metadata(con, row) is None
    cv.write_bytes(b"CV - user-approved version")
    con.execute("UPDATE application_routes SET message='Changed' WHERE id=1")
    edited = con.execute("SELECT * FROM application_routes WHERE id=1").fetchone()
    assert approved_metadata(con, edited) is None
    con.close()
