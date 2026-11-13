# Telegram Job Agent

Existing Anunaki Ventures project — recovery is continuing, **not a new bot**.

The historical DigitalOcean/Portfolio Tower runtime used Telethon (personal Telegram account), a SQLite job database and optional Airtable integration. The last verified Tower heartbeat is historical and does not confirm that any Telegram session or server is currently active.

The development recovery branch is `recovery/guarded-resume-20261113`. It restores source-level safeguards and tests without turning on any outbound automation. Never merge to `main` or deploy to Production without explicit founder authorization.

## Read first

[Recovery evidence, metrics and resumption checklist](RECOVERY_HANDOFF_20261113.md)

## Offline validation

```sh
python -m pip install pytest openpyxl
python -m pytest -q tests
python -m compileall -q worker.py seeker_poster.py channel_router.py outbound_policy.py route_approval.py excel_export.py
```

All delivery is disabled by default. Both direct Telegram applications and email/form routes need exact founder approval of the recipient, message, and CV. The group poster is separately disabled until existing delivery history and group permissions are checked.

## Excel

`excel_export.py` exports the **existing** SQLite records to one reviewable Excel workbook. It does not manufacture missing data or run sends from spreadsheet edits:

```sh
python excel_export.py --database /path/to/telegram_jobs.db --output /private/telegram-job-agent.xlsx
```

Keep tokens, authenticated sessions, CVs, live databases and private Excel exports out of this public Git repository.
