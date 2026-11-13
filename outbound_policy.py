"""Founder approval gate for vacancy-specific Telegram outreach.

A queued message may be delivered only if the founder explicitly approved its
exact recipient, job, message and CV bytes. Approval never sends a message.
"""

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def ensure_schema(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS founder_outbound_approvals (
            queue_id INTEGER PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            founder_confirmation TEXT NOT NULL,
            approved_at TEXT NOT NULL
        )
        """
    )


def _field(row, key):
    return row[key]


def message_fingerprint(row, cv_path):
    """Bind approval to the actual PDF, message, recipient and vacancy."""
    path = Path(cv_path)
    cv_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    payload = {
        "queue_id": int(_field(row, "id")),
        "job_id": str(_field(row, "job_id") or ""),
        "recipient": str(_field(row, "recipient") or "").strip().lower(),
        "message": str(_field(row, "message") or ""),
        "cv_sha256": cv_digest,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_approved(connection, row, cv_path):
    """Fail closed on missing files, missing approvals, and malformed state."""
    try:
        ensure_schema(connection)
        fingerprint = message_fingerprint(row, cv_path)
        approval = connection.execute(
            "SELECT fingerprint, founder_confirmation FROM founder_outbound_approvals WHERE queue_id=?",
            (int(row["id"]),),
        ).fetchone()
        return bool(
            approval
            and approval[0] == fingerprint
            and approval[1]
            and str(approval[1]).strip()
        )
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return False


def approve(connection, row, cv_path, expected_fingerprint, founder_confirmation):
    """Record an explicit founder confirmation; never infer it from old queue state."""
    confirmation = (founder_confirmation or "").strip()
    if len(confirmation) < 5:
        raise ValueError("founder_confirmation must identify an explicit approval")
    actual = message_fingerprint(row, cv_path)
    if not expected_fingerprint or actual != expected_fingerprint:
        raise ValueError("fingerprint changed: refresh the approval preview")
    ensure_schema(connection)
    connection.execute(
        """
        INSERT INTO founder_outbound_approvals(queue_id,fingerprint,founder_confirmation,approved_at)
        VALUES(?,?,?,?)
        ON CONFLICT(queue_id) DO UPDATE SET
            fingerprint=excluded.fingerprint,
            founder_confirmation=excluded.founder_confirmation,
            approved_at=excluded.approved_at
        """,
        (int(row["id"]), actual, confirmation, datetime.now(timezone.utc).isoformat()),
    )
    # This never delivers the message. An enabled worker still applies all other guards.
    connection.execute(
        """UPDATE send_queue SET status='pending',error=NULL,retry_at=NULL
           WHERE id=? AND status='held_approval'""",
        (int(row["id"]),),
    )
    connection.commit()
    return actual


def main():
    parser = argparse.ArgumentParser(description="Review or record a founder-approved outbound item")
    default_root = Path(os.environ.get("TG_JOB_AGENT_HOME", "/opt/tg-job-agent"))
    parser.add_argument("--database", default=str(default_root / "telegram_jobs.db"))
    sub = parser.add_subparsers(dest="command", required=True)
    preview = sub.add_parser("show", help="Show exact message and fingerprint; no send")
    preview.add_argument("--queue-id", type=int, required=True)
    approval = sub.add_parser("approve", help="Record an approval already given by the founder; no send")
    approval.add_argument("--queue-id", type=int, required=True)
    approval.add_argument("--expected-fingerprint", required=True)
    approval.add_argument("--founder-confirmation", required=True)
    args = parser.parse_args()

    connection = sqlite3.connect(args.database, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM send_queue WHERE id=?", (args.queue_id,)).fetchone()
        if row is None:
            raise SystemExit("queue item not found")
        root = Path(os.environ.get("TG_JOB_AGENT_HOME", "/opt/tg-job-agent"))
        cv_path = row["cv_path"] or str(root / "cv" / "Ruslans_Strakis_CV.pdf")
        if args.command == "show":
            result = {
                "queue_id": row["id"],
                "job_id": row["job_id"],
                "recipient": row["recipient"],
                "message": row["message"],
                "cv_path": cv_path,
                "fingerprint": message_fingerprint(row, cv_path),
                "approved": is_approved(connection, row, cv_path),
            }
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            digest = approve(
                connection,
                row,
                cv_path,
                args.expected_fingerprint,
                args.founder_confirmation,
            )
            print(json.dumps({"queue_id": args.queue_id, "approved_fingerprint": digest}))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
