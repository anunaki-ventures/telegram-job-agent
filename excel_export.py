"""Export a read-only, single-workbook audit snapshot of the existing SQLite data.

The workbook is a review/report artifact, not an outbound queue or credentials store.
It must never be imported back automatically to generate sends.
"""

import argparse
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


TABLES = (
    ("Sources", "sources", ("username", "handle", "title", "country", "active", "status", "last_scanned_at")),
    ("Vacancies", "jobs", ("job_id", "title", "country", "source_username", "source_country", "status", "selector_status", "raw_text")),
    ("Telegram Outreach", "send_queue", ("id", "job_id", "recipient", "status", "message", "cv_path", "telegram_message_id", "processed_at", "retry_at", "error")),
    ("Group Posts", "seeker_group_posts", ("id", "source_key", "group_title", "posted_at", "telegram_message_id", "language", "status", "note")),
    ("Email And Forms", "application_routes", ("id", "job_id", "route_kind", "target", "status", "created_at", "processed_at", "external_id", "reason")),
    ("Contact History", "outbound_contact_history", ("id", "channel", "target", "employer_key", "job_id", "status", "sent_at", "external_id")),
    ("Founder Approvals", "founder_outbound_approvals", ("queue_id", "fingerprint", "founder_confirmation", "approved_at")),
)
HEADER = PatternFill(fill_type="solid", fgColor="E7F2ED")
HEADER_FONT = Font(name="Aptos", size=11, bold=True, color="01261F")


def _table_columns(connection, table):
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def _safe_cell(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return "[binary omitted]"
    if not isinstance(value, (int, float, str)):
        value = str(value)
    if isinstance(value, str):
        value = value[:32760]
        # Treat all content fetched from Telegram / third parties as text,
        # never as an Excel formula when opening the workbook.
        if value.startswith(("=", "+", "-", "@")):
            value = "'" + value
    return value


def _style(sheet):
    sheet.freeze_panes = "A2"
    for cell in sheet[1]:
        cell.fill = HEADER
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
    if sheet.max_column and sheet.max_row:
        sheet.auto_filter.ref = sheet.dimensions
    sheet.row_dimensions[1].height = 28
    for column in sheet.columns:
        letter = get_column_letter(column[0].column)
        longest = max((len(str(cell.value or "")) for cell in list(column)[:100]), default=12)
        sheet.column_dimensions[letter].width = min(max(longest + 2, 15), 62)


def export_database(database_path, workbook_path):
    source = Path(database_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source}")
    target = Path(workbook_path).expanduser().resolve()
    if source == target:
        raise ValueError("Excel output must not overwrite the SQLite database")
    target.parent.mkdir(parents=True, exist_ok=True)

    # Opening SQLite read-only guarantees export never changes real queue state.
    uri = f"file:{quote(str(source), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    book = Workbook()
    summary = book.active
    summary.title = "Summary"
    summary.append(["Table", "Rows", "Status"])
    counts = {}

    try:
        for sheet_name, table, requested_columns in TABLES:
            sheet = book.create_sheet(sheet_name)
            actual = _table_columns(connection, table)
            selected = [field for field in requested_columns if field in actual]
            if not selected:
                sheet.append(["Note"])
                sheet.append(["No compatible table or columns found in the recovered database."])
                counts[sheet_name] = 0
                summary.append([sheet_name, 0, "NOT AVAILABLE"])
                _style(sheet)
                continue
            sheet.append(selected)
            sql_fields = ",".join('"' + name + '"' for name in selected)
            rows = connection.execute(f'SELECT {sql_fields} FROM "{table}"')
            count = 0
            for record in rows:
                sheet.append([_safe_cell(record[field]) for field in selected])
                count += 1
            counts[sheet_name] = count
            summary.append([sheet_name, count, "EXPORTED"])
            _style(sheet)

        summary.append(["Exported at UTC", datetime.now(timezone.utc).isoformat(), "Read-only snapshot"])
        summary.append(["Source database", source.name, "Not a live queue"])
        _style(summary)

        # Atomic replacement avoids corrupting a previously valid workbook.
        staging = target.with_name(target.name + ".tmp.xlsx")
        try:
            book.save(staging)
            os.replace(staging, target)
        finally:
            if staging.exists():
                staging.unlink()
    finally:
        connection.close()
    return counts


def main():
    parser = argparse.ArgumentParser(description="Export Telegram Job Agent database to a reviewable .xlsx")
    root = Path(os.environ.get("TG_JOB_AGENT_HOME", "/opt/tg-job-agent"))
    parser.add_argument("--database", default=str(root / "telegram_jobs.db"))
    parser.add_argument("--output", default=str(root / "reports" / "telegram-job-agent.xlsx"))
    args = parser.parse_args()
    print(export_database(args.database, args.output))
    print(f"Workbook: {args.output}")


if __name__ == "__main__":
    main()
