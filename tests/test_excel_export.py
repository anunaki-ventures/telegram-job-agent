import sqlite3

import pytest
from openpyxl import load_workbook

from excel_export import export_database


def test_export_preserves_row_counts_and_escapes_formula_content(tmp_path):
    db_path = tmp_path / "telegram_jobs.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE sources (username TEXT, title TEXT, country TEXT)")
    con.execute("INSERT INTO sources VALUES ('@jobs', 'Jobs Europe', 'Latvia')")
    con.execute("CREATE TABLE jobs (job_id TEXT, title TEXT, raw_text TEXT)")
    con.execute("INSERT INTO jobs VALUES ('J1','Operations Manager','=HYPERLINK(\"https://example.test\",\"wrong\")')")
    con.execute("CREATE TABLE send_queue (id INTEGER,job_id TEXT,recipient TEXT,status TEXT,message TEXT)")
    con.execute("INSERT INTO send_queue VALUES (1,'J1','@recruiter','sent','Sent safely')")
    con.execute("CREATE TABLE seeker_group_posts (id INTEGER,source_key TEXT,status TEXT)")
    con.execute("INSERT INTO seeker_group_posts VALUES (1,'@jobs','sent')")
    con.commit()
    con.close()

    output = tmp_path / "single-register.xlsx"
    counts = export_database(db_path, output)
    assert counts["Sources"] == 1
    assert counts["Vacancies"] == 1
    assert counts["Telegram Outreach"] == 1
    assert counts["Group Posts"] == 1
    assert counts["Founder Approvals"] == 0

    book = load_workbook(output, data_only=False)
    assert "Summary" in book.sheetnames
    assert "Email And Forms" in book.sheetnames
    assert book["Sources"]["A2"].value == "'@jobs"
    title_row = next(book["Vacancies"].iter_rows(min_row=2))
    raw = title_row[2]
    assert raw.data_type != "f"
    assert raw.value.startswith("'=HYPERLINK")
    assert book["Telegram Outreach"]["D2"].value == "sent"


def test_export_is_read_only_and_does_not_create_missing_database(tmp_path):
    db_path = tmp_path / "missing.db"
    output = tmp_path / "target.xlsx"
    with pytest.raises(FileNotFoundError):
        export_database(db_path, output)
    assert not db_path.exists()
    assert not output.exists()


def test_export_refuses_source_overwrite(tmp_path):
    db_path = tmp_path / "jobs.db"
    sqlite3.connect(db_path).close()
    with pytest.raises(ValueError, match="must not overwrite"):
        export_database(db_path, db_path)
