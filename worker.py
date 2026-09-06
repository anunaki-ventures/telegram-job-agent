import os
import re
import sqlite3
import asyncio
import random
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from telethon import TelegramClient, utils
from telethon.tl import types
from telethon.errors import RPCError, FloodWaitError

from candidate_classifier import classify_post

ROOT = Path('/opt/tg-job-agent')
DB = ROOT / 'telegram_jobs.db'
STATE_DB = ROOT / 'worker_state.db'
ENV = ROOT / '.env'
DEFAULT_CV = ROOT / 'cv' / 'Ruslans_Strakis_CV.pdf'
MIN_SEND_DELAY_SECONDS = 600
MAX_SEND_DELAY_SECONDS = 900
EMPTY_QUEUE_POLL_SECONDS = 20
FLOODWAIT_MARGIN_SECONDS = 300
SESSION_CACHES = [
    ROOT / 'telegram_worker.session',
    ROOT / 'telegram_scanner.session',
    ROOT / 'telegram_discovery.session',
    ROOT / 'telegram.session',
    ROOT / 'telegram_poster.session',
]


def load_env():
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
API_ID = int(os.environ['TG_API_ID'])
API_HASH = os.environ['TG_API_HASH']
client = TelegramClient('/opt/tg-job-agent/telegram_worker', API_ID, API_HASH)


def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    return con


def state_db():
    con = sqlite3.connect(STATE_DB, timeout=10)
    con.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    con.commit()
    return con


def ensure_schema():
    con = db()
    cols = {r[1] for r in con.execute('PRAGMA table_info(send_queue)')}
    if 'retry_at' not in cols:
        con.execute('ALTER TABLE send_queue ADD COLUMN retry_at TEXT')
    con.commit()
    con.close()


def now():
    return datetime.now(timezone.utc).isoformat()


def normalize_contact(v):
    if not v:
        return ''
    v = v.strip()
    if not v.startswith('@'):
        v = '@' + v
    return v.lower()


def set_state(key, value):
    con = state_db()
    try:
        con.execute(
            'INSERT INTO state(key,value) VALUES(?,?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, str(value)),
        )
        con.commit()
    finally:
        con.close()


def get_state_float(key, default=0.0):
    con = state_db()
    try:
        row = con.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return float(row[0]) if row else float(default)
    except Exception:
        return float(default)
    finally:
        con.close()


def resolve_block_until():
    return get_state_float('telegram_resolve_blocked_until', 0.0)


def set_resolve_block(seconds):
    until = datetime.now(timezone.utc).timestamp() + max(0, int(seconds)) + FLOODWAIT_MARGIN_SECONDS
    current = resolve_block_until()
    if until > current:
        set_state('telegram_resolve_blocked_until', until)
    return max(until, current)


def resolve_wait_remaining():
    return max(0, int(resolve_block_until() - datetime.now(timezone.utc).timestamp()))


def cached_input_peer(recipient):
    username = normalize_contact(recipient).lstrip('@')
    if not username:
        return None
    for path in SESSION_CACHES:
        if not path.exists():
            continue
        try:
            con = sqlite3.connect(path, timeout=2)
            row = con.execute(
                "SELECT id,hash FROM entities WHERE lower(COALESCE(username,''))=? LIMIT 1",
                (username.lower(),),
            ).fetchone()
            con.close()
        except Exception:
            row = None
        if not row:
            continue
        marked_id = int(row[0])
        access_hash = int(row[1] or 0)
        real_id, peer_type = utils.resolve_id(marked_id)
        if peer_type is types.PeerUser:
            return types.InputPeerUser(real_id, access_hash)
        if peer_type is types.PeerChannel:
            return types.InputPeerChannel(real_id, access_hash)
        if peer_type is types.PeerChat:
            return types.InputPeerChat(real_id)
    return None


def already_contacted(con, recipient):
    r = normalize_contact(recipient)
    row = con.execute(
        "SELECT 1 FROM contacts WHERE lower(contact)=? AND status='contacted' LIMIT 1",
        (r,),
    ).fetchone()
    if row:
        return True
    row = con.execute(
        """
        SELECT 1 FROM applications
        WHERE lower(recipient)=?
          AND (sent_at IS NOT NULL OR lower(status) IN ('sent','done','success','successful'))
        LIMIT 1
        """,
        (r,),
    ).fetchone()
    return bool(row)


def job_source_text(con, job_id):
    if not job_id:
        return ''
    row = con.execute('SELECT raw_text,title FROM jobs WHERE job_id=? LIMIT 1', (job_id,)).fetchone()
    if not row:
        return ''
    return ((row['raw_text'] or '') + '\n' + (row['title'] or '')).strip()


def outbound_employer_verified(con, row):
    text = job_source_text(con, row['job_id'])
    classification = classify_post(text)
    return classification.is_employer_post, classification.reason


def outbound_time_allowed(con, row):
    job = con.execute('SELECT timezone FROM jobs WHERE job_id=? LIMIT 1', (row['job_id'],)).fetchone()
    tzname = (job['timezone'] or '').strip() if job else ''
    if not tzname:
        return True, None
    try:
        tz = ZoneInfo(tzname)
        local = datetime.now(timezone.utc).astimezone(tz)
    except Exception:
        return True, None
    mins = local.hour * 60 + local.minute
    if 7 * 60 + 30 <= mins < 19 * 60:
        return True, None
    if mins < 7 * 60 + 30:
        target_local = local.replace(hour=7, minute=30, second=0, microsecond=0)
    else:
        target_local = (local + timedelta(days=1)).replace(hour=7, minute=30, second=0, microsecond=0)
    return False, target_local.astimezone(timezone.utc).isoformat()


def mark_skipped(con, row_id, reason):
    con.execute(
        "UPDATE send_queue SET status='skipped',error=?,processed_at=?,retry_at=NULL WHERE id=?",
        (reason, now(), row_id),
    )
    con.commit()


def mark_failed(con, row_id, error):
    con.execute(
        "UPDATE send_queue SET status='failed',error=?,processed_at=?,retry_at=NULL WHERE id=?",
        (str(error)[:2000], now(), row_id),
    )
    con.commit()


def mark_held(con, row_id, status, reason, retry_at):
    con.execute(
        'UPDATE send_queue SET status=?,error=?,processed_at=NULL,retry_at=? WHERE id=?',
        (status, str(reason)[:2000], retry_at, row_id),
    )
    con.commit()


def mark_floodwait(con, row_id, seconds, reason='ResolveUsernameRequest FloodWait'):
    until_epoch = set_resolve_block(seconds)
    retry_at = datetime.fromtimestamp(until_epoch, timezone.utc).isoformat()
    mark_held(con, row_id, 'held_floodwait', reason, retry_at)
    return retry_at


def mark_sent(con, row_id, recipient, message, cv_path, msg_id, job_id):
    ts = now()
    con.execute(
        "UPDATE send_queue SET status='sent',telegram_message_id=?,processed_at=?,error=NULL,retry_at=NULL WHERE id=?",
        (str(msg_id), ts, row_id),
    )
    con.execute(
        """
        INSERT INTO contacts(contact,first_contact_at,last_message_id,status)
        VALUES (?,?,?,'contacted')
        ON CONFLICT(contact) DO UPDATE SET
            first_contact_at=COALESCE(contacts.first_contact_at,excluded.first_contact_at),
            last_message_id=excluded.last_message_id,
            status='contacted'
        """,
        (normalize_contact(recipient), ts, str(msg_id)),
    )
    con.execute(
        """
        INSERT INTO applications(job_id,recipient,message,cv_name,status,sent_at,telegram_message_id,raw_json)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (job_id, recipient, message, Path(cv_path).name, 'sent', ts, str(msg_id), '{}'),
    )
    con.commit()


def migrate_floodwait_failures():
    con = db()
    rows = con.execute(
        "SELECT id,error,processed_at FROM send_queue WHERE status='failed' AND error LIKE '%ResolveUsernameRequest%'"
    ).fetchall()
    migrated = 0
    latest_until = resolve_block_until()
    for row in rows:
        error = row['error'] or ''
        m = re.search(r'wait of\s+(\d+)\s+seconds', error, re.I)
        seconds = int(m.group(1)) if m else 3600
        try:
            base = datetime.fromisoformat(row['processed_at']) if row['processed_at'] else datetime.now(timezone.utc)
            if base.tzinfo is None:
                base = base.replace(tzinfo=timezone.utc)
        except Exception:
            base = datetime.now(timezone.utc)
        retry_dt = base.astimezone(timezone.utc) + timedelta(seconds=seconds + FLOODWAIT_MARGIN_SECONDS)
        latest_until = max(latest_until, retry_dt.timestamp())
        con.execute(
            "UPDATE send_queue SET status='held_floodwait',retry_at=?,processed_at=NULL WHERE id=?",
            (retry_dt.isoformat(), row['id']),
        )
        migrated += 1
    con.commit()
    con.close()
    if latest_until > datetime.now(timezone.utc).timestamp():
        set_state('telegram_resolve_blocked_until', latest_until)
    print('MIGRATED_FLOODWAIT_FAILURES', migrated)


def promote_due_holds():
    con = db()
    ts = now()
    con.execute(
        """
        UPDATE send_queue
        SET status='pending',error=NULL,processed_at=NULL,retry_at=NULL
        WHERE status IN ('held_floodwait','held_time')
          AND retry_at IS NOT NULL
          AND retry_at<=?
        """,
        (ts,),
    )
    count = con.total_changes
    con.commit()
    con.close()
    return count


async def process_one(row):
    con = db()
    try:
        recipient = row['recipient']
        message = row['message']
        cv_path = row['cv_path'] or str(DEFAULT_CV)
        if already_contacted(con, recipient):
            print('SKIP CONTACTED', recipient)
            mark_skipped(con, row['id'], 'contact_already_contacted')
            return False
        if not Path(cv_path).exists():
            mark_failed(con, row['id'], f'CV missing: {cv_path}')
            print('CV MISSING', cv_path)
            return False

        verified, safety_reason = outbound_employer_verified(con, row)
        if not verified:
            mark_skipped(con, row['id'], 'outbound_safety:' + safety_reason)
            print('SKIP_UNVERIFIED_EMPLOYER', recipient, safety_reason)
            return False

        time_ok, retry_at = outbound_time_allowed(con, row)
        if not time_ok:
            mark_held(con, row['id'], 'held_time', 'outside_employer_local_window', retry_at)
            print('HOLD_LOCAL_TIME', recipient, retry_at)
            return False

        entity = cached_input_peer(recipient)
        if entity is None:
            remaining = resolve_wait_remaining()
            if remaining > 0:
                retry_at = datetime.fromtimestamp(resolve_block_until(), timezone.utc).isoformat()
                mark_held(con, row['id'], 'held_floodwait', f'resolve_cooldown:{remaining}s', retry_at)
                print('HOLD_RESOLVE_COOLDOWN', recipient, remaining)
                return False
            try:
                entity = await client.get_input_entity(recipient)
            except FloodWaitError as e:
                seconds = int(getattr(e, 'seconds', 3600) or 3600)
                retry_at = mark_floodwait(con, row['id'], seconds, f'ResolveUsernameRequest FloodWait {seconds}s')
                print('HOLD_FLOODWAIT', recipient, seconds, retry_at)
                return False
            except Exception as e:
                mark_failed(con, row['id'], f'Invalid/unresolvable recipient: {e}')
                print('INVALID_RECIPIENT', recipient, repr(e))
                return False

        try:
            msg = await client.send_message(entity, message, file=cv_path)
        except FloodWaitError as e:
            seconds = int(getattr(e, 'seconds', 3600) or 3600)
            retry_at = mark_floodwait(con, row['id'], seconds, f'Send FloodWait {seconds}s')
            print('HOLD_SEND_FLOODWAIT', recipient, seconds, retry_at)
            return False
        except RPCError as e:
            mark_failed(con, row['id'], e)
            print('SEND FAILED', recipient, repr(e))
            return False

        mark_sent(con, row['id'], recipient, message, cv_path, msg.id, row['job_id'])
        print('SENT', recipient, 'MESSAGE_ID', msg.id)
        return True
    finally:
        con.close()


async def main():
    if not DB.exists():
        raise SystemExit(f'DB missing: {DB}')
    if not DEFAULT_CV.exists():
        raise SystemExit(f'CV missing: {DEFAULT_CV}')
    ensure_schema()
    migrate_floodwait_failures()
    await client.start()
    print('=== SQLITE TELEGRAM WORKER READY ===')
    print('DB:', DB)
    print('CV:', DEFAULT_CV.name)
    print(f'OUTBOUND PACING: one send every {MIN_SEND_DELAY_SECONDS}-{MAX_SEND_DELAY_SECONDS}s')
    while True:
        promoted = promote_due_holds()
        if promoted:
            print('PROMOTED_DUE_HOLDS', promoted)
        con = db()
        row = con.execute(
            "SELECT * FROM send_queue WHERE status='pending' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        con.close()
        if not row:
            await asyncio.sleep(EMPTY_QUEUE_POLL_SECONDS)
            continue
        sent = await process_one(row)
        if sent:
            delay = random.randint(MIN_SEND_DELAY_SECONDS, MAX_SEND_DELAY_SECONDS)
            print('NEXT_SEND_DELAY_SECONDS', delay)
            await asyncio.sleep(delay)
        else:
            await asyncio.sleep(5)


if __name__ == '__main__':
    asyncio.run(main())
