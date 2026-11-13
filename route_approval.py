"""Exact founder approval for queued email and web-form application routes.

A route is claimable only after an explicit approval of its message, recipient,
and actual CV file bytes. Nothing in this module performs external outreach.
"""

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


FIELDS = (
    "id", "job_id", "route_kind", "target", "target_key",
    "employer_key", "subject", "message", "cv_variant", "payload_json",
)


def ensure_schema(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS founder_route_approvals (
            route_id INTEGER PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            cv_path TEXT NOT NULL,
            founder_confirmation TEXT NOT NULL,
            approved_at TEXT NOT NULL
        )
        """
    )


def route_fingerprint(route, cv_path):
    path = Path(cv_path).expanduser().resolve()
    cv_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    payload = {key: str(route[key] or "") for key in FIELDS}
    payload["cv_sha256"] = cv_digest
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def approved_metadata(connection, route):
    """Return the approved CV path only when every bound value still matches."""
    try:
        ensure_schema(connection)
        approval = connection.execute(
            "SELECT fingerprint,cv_path,founder_confirmation FROM founder_route_approvals WHERE route_id=?",
            (int(route["id"]),),
        ).fetchone()
        if not approval or not approval[2] or not str(approval[2]).strip():
            return None
        path = approval[1]
        if route_fingerprint(route, path) != approval[0]:
            return None
        return {"approved_cv_path": path, "approved_fingerprint": approval[0]}
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError):
        return None


def approve(connection, route, cv_path, expected_fingerprint, founder_confirmation):
    confirmation = (founder_confirmation or "").strip()
    if len(confirmation) < 5:
        raise ValueError("founder confirmation reference required")
    path = str(Path(cv_path).expanduser().resolve())
    current = route_fingerprint(route, path)
    if current != expected_fingerprint:
        raise ValueError("route or CV changed: refresh approval preview")
    ensure_schema(connection)
    connection.execute(
        """
        INSERT INTO founder_route_approvals(route_id,fingerprint,cv_path,founder_confirmation,approved_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(route_id) DO UPDATE SET
          fingerprint=excluded.fingerprint,
          cv_path=excluded.cv_path,
          founder_confirmation=excluded.founder_confirmation,
          approved_at=excluded.approved_at
        """,
        (int(route["id"]), current, path, confirmation, datetime.now(timezone.utc).isoformat()),
    )
    connection.commit()
    return current


def main():
    parser = argparse.ArgumentParser(description="Review founder approvals for email/form routes")
    root = Path(os.environ.get("TG_JOB_AGENT_HOME", "/opt/tg-job-agent"))
    parser.add_argument("--database", default=str(root / "telegram_jobs.db"))
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("show", "approve"):
        p = sub.add_parser(command)
        p.add_argument("--route-id", type=int, required=True)
        p.add_argument("--cv-path", required=True)
        if command == "approve":
            p.add_argument("--expected-fingerprint", required=True)
            p.add_argument("--founder-confirmation", required=True)
    args = parser.parse_args()
    connection = sqlite3.connect(args.database, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        route = connection.execute("SELECT * FROM application_routes WHERE id=?", (args.route_id,)).fetchone()
        if route is None:
            raise SystemExit("application route not found")
        if args.command == "show":
            print(json.dumps({
                "route_id": route["id"], "job_id": route["job_id"],
                "kind": route["route_kind"], "target": route["target"],
                "subject": route["subject"], "message": route["message"],
                "cv_path": args.cv_path,
                "fingerprint": route_fingerprint(route, args.cv_path),
                "approved": bool(approved_metadata(connection, route)),
            }, ensure_ascii=False, indent=2))
        else:
            digest = approve(connection, route, args.cv_path, args.expected_fingerprint, args.founder_confirmation)
            print(json.dumps({"route_id": args.route_id, "approved_fingerprint": digest}))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
