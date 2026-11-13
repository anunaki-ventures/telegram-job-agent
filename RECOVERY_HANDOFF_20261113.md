# Telegram Job Agent — recovery handoff (2026-11-13)

## Project and source of truth

- Existing repository: `anunaki-ventures/telegram-job-agent` (public).
- `main` was observed at `d803869e182e73ae9d2b958bb718ca9204304a62`. It mainly contains operational repair scripts and the old diagnostic report.
- The newest independently located branch with a complete set of source modules was `fix/candidate-self-ad-filter`, observed at `a3f0d744d6c0b82e05e095e83666e854f08d7418`. It is **diverged** from main (36 ahead / 2 behind at recovery).
- Recovery work is isolated in `recovery/guarded-resume-20261113`, branched from the existing source branch. Do not merge or deploy to production automatically.
- There was no open PR in this repository at the start of recovery.
- Portfolio Tower: `anunaki-ventures/portfolio-tower`, with command/result bridge on `tower/worker-queue`.

## Last verifiable runtime evidence (HISTORICAL, not live)

- The DigitalOcean host was called `telegram-job-agent`; application path `/opt/tg-job-agent`, service `tg-job-agent.service`.
- Tower heartbeat `ops/digitalocean/heartbeat/telegram-job-agent.json` reported the service active at **2026-10-06T10:25:08Z**, agent version 3, and existing app directory. No newer heartbeat was observed.
- A Tower command result from **2026-09-13T13:13:25Z** reported queue totals: **68 sent**, 10 failed, 4 held by FloodWait, 21 held by time window, and 29 skipped. These are historical *queue* counts, not a verified lifetime total.
- Another result reported seeker/group publishing `rate_limited` with a 78,182-second FloodWait. The agent must not try to guarantee an hourly send by bypassing Telegram limits.
- A separate DigitalOcean connector did not return current droplet inventory during this recovery. Therefore live server status and the recoverability of its SQLite files, sessions, and snapshots remain unknown.
- No matching Vercel project was found under the name `telegram-job`; this worker does not require a website Preview.

## Current implementation

Existing Python modules recovered: `worker.py`, `scanner.py`, `discover_sources.py`, `selector.py`, `seeker_poster.py`, `channel_router.py`, filtering helpers and older operational repair scripts.

Recovery branch adds:

1. **Default-deny Telegram direct outreach**: `TG_OUTBOUND_ENABLED=1` is required, and every queue row needs exact founder confirmation of the job, recipient, message and CV bytes (`outbound_policy.py`).
2. **Default-deny group publishing**: `TG_GROUP_POSTS_ENABLED=1` is required. Keep per-group rule checks, 14-day cooldown and pacing; validate against Telegram live restrictions before activation.
3. **Default-deny email and form route claims**: `TG_CHANNEL_OUTBOUND_ENABLED=1` is required and every route needs a matching confirmed message and CV fingerprint (`route_approval.py`). Downstream senders must use the returned `approved_cv_path` and obey the same founder gate.
4. **Read-only Excel export**: `excel_export.py` generates one `.xlsx` workbook with Summary, Sources, Vacancies, Telegram Outreach, Group Posts, Email And Forms, Contact History and Founder Approvals. It reads the SQLite DB in read-only mode, escapes formula-like content, and will not create a fake database. The workbook is not an automatic send queue.
5. **No-secret tests and CI**: Python compilation and pytest on GitHub Actions, using no Telegram credentials.
6. **Source hygiene**: `.gitignore` excludes credentials, sessions, CV PDFs, SQLite files and Excel exports. `.env.example` records safe defaults.

### Approval and export commands (examples; require recovered DB)

```sh
python outbound_policy.py show --queue-id 123 --database /opt/tg-job-agent/telegram_jobs.db
python route_approval.py show --route-id 123 --cv-path /private/reviewed-CV.pdf
python excel_export.py --database /opt/tg-job-agent/telegram_jobs.db --output /private/telegram-job-agent.xlsx
```

The `approve` commands exist only to record *actual* founder confirmation; they do not create permission themselves. They require a digest from the unchanged preview and a real founder confirmation reference. Do not enter a guessed confirmation or turn on outbound flags without the founder's approval of each vacancy-specific CV and letter.

## Missing or uncertain items

- Last full `telegram_jobs.db` and its WAL/journal, other state DBs, and group-post history.
- Old Telegram authenticated session, and existing `TG_API_ID`/`TG_API_HASH` if still available. Never export or commit these publicly.
- An exact snapshot of the latest DigitalOcean runtime: some operational files (including `hourly_outbound.py`) were constructed on the host by shell repair scripts; the source branch does not by itself prove which version was deployed.
- Actual Telegram account restrictions and permission to post in each destination group.
- Reconciliation of main's later operational changes with the diverged source branch.
- Geographic and salary instructions for THIS Telegram workflow. Older source defaults include multiple regions and must not be silently reactivated against newer China/Riga-specific streams.
- External providers/auth and any production scheduling environment. No active Telegram MCP connector has been confirmed in this recovery.

## Resume order

1. Locate the old DigitalOcean droplet **or backup/snapshot** and retrieve read-only history first. If unavailable, reconcile the list of sent contacts/group posts with Telegram before any new outbound.
2. Export the recovered DB to the **single** Excel review workbook; do not fabricate historical rows. Use Excel for review and reporting. Keep transactional queue/deduplication state in SQLite or a managed database with locking.
3. Verify current Telegram auth method and limits. Telethon requires MTProto user authorization and API credentials; a third-party MCP wrapper still requires authenticated transport. A Bot API token is an alternative only where bots have actual group permissions.
4. Confirm target geography, permitted outreach type, group announcement text, and per-vacancy approval workflow.
5. Finish runtime portability (remove remaining `/opt` assumptions), restore operational entrypoints, and connect a persistent state store/scheduler. A new DigitalOcean droplet is NOT prerequisite for manual or periodically scheduled processing; unattended execution still needs a reliable host/scheduler and durable state.
6. Run a read-only scan and compare deduplication history. Authorize at most one controlled canary after review; respect Telegram flood limits and group rules. Do not apply silently or retry aggressively.
7. Inspect live delivery, new records, error paths, idempotency, account health; only then consider a limited automatic schedule.

## Progress (provisional recovery estimate, not a production acceptance claim)

- **CODE: 60%** — recovered sources, guards, export and tests; runtime portability and missing operational components remain.
- **DESIGN: 15%** — Excel review format exists; no separately approved UI/design system.
- **CONNECTIONS: 0% verified live** — historical accounts existed; none verified as presently connected and usable.
- **VALIDATION: 25%** — offline syntax and regression CI passes, but no live Telegram, DB restore, or end-to-end sending verification.

## Safety boundaries

Do not force-push canonical history, merge main, deploy Production, create duplicate repositories/servers, commit secrets/CVs, automatically import editable Excel into a send queue, auto-approve applications, or send Telegram/email/form outreach before history recovery and individual founder approvals.
