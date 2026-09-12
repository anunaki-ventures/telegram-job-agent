#!/usr/bin/env bash
set -euo pipefail

APP=/opt/tg-job-agent
DB="$APP/telegram_jobs.db"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=/root/tg-routing-resilience-$STAMP.tgz
REPORT=/root/tg-routing-resilience-$STAMP.txt
exec > >(tee "$REPORT") 2>&1

echo '=== TELEGRAM ROUTING RESILIENCE REPAIR ==='
date -u

for f in "$APP/worker.py" "$APP/seeker_poster.py" "$DB"; do
  test -e "$f" || { echo "FATAL missing $f"; exit 10; }
done

tar --exclude='*.session-journal' -czf "$BACKUP" "$APP/worker.py" "$APP/seeker_poster.py" "$DB"
echo "backup=$BACKUP"

# Stop only the outbound processes while patching. Discovery/scanner/selector stay intact.
systemctl stop tg-job-agent.service 2>/dev/null || true
systemctl stop tg-job-seeker-poster.service 2>/dev/null || true

python3 - <<'PY'
from pathlib import Path

APP=Path('/opt/tg-job-agent')

# ------------------------------------------------------------------
# 1) Direct worker: invalid/unresolvable recipients are not FloodWait.
#    Immediate terminal handling for Telegram's explicit invalid-user errors;
#    a ValueError gets one retry and then becomes terminal. This prevents one
#    bad handle from being recycled as held_floodwait forever.
# ------------------------------------------------------------------
p=APP/'worker.py'
src=p.read_text()
marker='# --- routing resilience 2026-09-12: terminal recipient guard ---'
if marker not in src:
    lines=src.splitlines(keepends=True)
    inserted=False
    for i,line in enumerate(lines):
        if line.strip() != 'except Exception as e:':
            continue
        window=''.join(lines[i:i+16])
        if 'resolve_retry:' not in window or 'mark_held' not in window:
            continue
        indent=line[:len(line)-len(line.lstrip())]+'    '
        inject=[
            indent+marker+'\n',
            indent+"err_name = type(e).__name__\n",
            indent+"err_text = str(e)\n",
            indent+"prev_error = str(row['error'] or '') if hasattr(row, 'keys') and 'error' in row.keys() else ''\n",
            indent+"explicit_invalid = err_name in ('UsernameInvalidError','UsernameNotOccupiedError','PeerIdInvalidError')\n",
            indent+"repeated_unresolvable = err_name == 'ValueError' and 'resolve_retry:ValueError' in prev_error\n",
            indent+"if explicit_invalid or repeated_unresolvable:\n",
            indent+"    terminal_reason = f'invalid_recipient:{err_name}:{err_text[:160]}'\n",
            indent+"    sq_cols = {r[1] for r in con.execute('pragma table_info(send_queue)')}\n",
            indent+"    if 'processed_at' in sq_cols:\n",
            indent+"        con.execute(\"UPDATE send_queue SET status='failed', error=?, processed_at=? WHERE id=?\", (terminal_reason, datetime.now(timezone.utc).isoformat(), row['id']))\n",
            indent+"    else:\n",
            indent+"        con.execute(\"UPDATE send_queue SET status='failed', error=? WHERE id=?\", (terminal_reason, row['id']))\n",
            indent+"    con.commit()\n",
            indent+"    print('TERMINAL_RECIPIENT_ERROR', row['id'], err_name, err_text[:120])\n",
            indent+"    continue\n",
        ]
        lines[i+1:i+1]=inject
        inserted=True
        break
    if not inserted:
        raise SystemExit('FATAL worker.py resolve_retry exception anchor not found')
    src=''.join(lines)
    p.write_text(src)
    print('PATCHED worker.py terminal recipient guard')
else:
    print('worker.py terminal recipient guard already present')

# ------------------------------------------------------------------
# 2) Seeker poster: use local Telegram entity cache only, refresh joined
#    dialogs once, disable Telethon implicit FloodWait sleeping, bound the
#    candidate scan, and remember hard write/payment/admin failures.
# ------------------------------------------------------------------
p=APP/'seeker_poster.py'
src=p.read_text()

if 'MAX_CANDIDATES_PER_RUN=' not in src:
    anchor='MAX_POSTS_PER_RUN=3\n'
    if anchor not in src:
        raise SystemExit('FATAL seeker_poster.py MAX_POSTS_PER_RUN anchor not found')
    src=src.replace(anchor, anchor+'MAX_CANDIDATES_PER_RUN=20\n', 1)

if 'scanned=0' not in src:
    anchor='    posted=0\n'
    if anchor not in src:
        raise SystemExit('FATAL seeker_poster.py posted anchor not found')
    src=src.replace(anchor, anchor+'    scanned=0\n', 1)

if '# --- routing resilience 2026-09-12: no implicit flood sleep ---' not in src:
    anchor='    await client.start()\n'
    if anchor not in src:
        raise SystemExit('FATAL seeker_poster.py client.start anchor not found')
    repl=(
        anchor+
        '    # --- routing resilience 2026-09-12: no implicit flood sleep ---\n'
        '    client.flood_sleep_threshold = 0\n'
        '    try:\n'
        '        await client.get_dialogs(limit=200)\n'
        '    except FloodWaitError as e:\n'
        "        print('SEEKER_POSTER DIALOG_REFRESH_FLOOD_WAIT',getattr(e,'seconds',None))\n"
        '    except Exception as e:\n'
        "        print('SEEKER_POSTER DIALOG_REFRESH_SKIP',type(e).__name__,str(e)[:120])\n"
    )
    src=src.replace(anchor,repl,1)

if '# --- routing resilience 2026-09-12: bounded scan ---' not in src:
    anchor='    for handle,title_hint in candidates:\n'
    if anchor not in src:
        raise SystemExit('FATAL seeker_poster.py candidate loop anchor not found')
    repl=(
        anchor+
        '        # --- routing resilience 2026-09-12: bounded scan ---\n'
        '        scanned += 1\n'
        '        if scanned > MAX_CANDIDATES_PER_RUN:\n'
        "            print('SEEKER_POSTER_SCAN_LIMIT',MAX_CANDIDATES_PER_RUN)\n"
        '            break\n'
    )
    src=src.replace(anchor,repl,1)

# Avoid repeated ResolveUsernameRequest calls. A candidate must already be in
# the authenticated session cache (normally because the account is in/joined
# to the group). This also prevents trying to post into arbitrary public
# announcement channels merely because a username was discovered.
if '# --- routing resilience 2026-09-12: cached entity only ---' not in src:
    anchor='            entity=await client.get_entity(handle)\n'
    if anchor not in src:
        raise SystemExit('FATAL seeker_poster.py get_entity anchor not found')
    repl=(
        '            # --- routing resilience 2026-09-12: cached entity only ---\n'
        '            try:\n'
        '                input_entity=await client.get_input_entity(handle)\n'
        '            except ValueError:\n'
        "                print('SEEKER_POST_SKIP_UNCACHED',key)\n"
        '                continue\n'
        '            entity=await client.get_entity(input_entity)\n'
    )
    src=src.replace(anchor,repl,1)

marker='# --- routing resilience 2026-09-12: remember hard blockers ---'
if marker not in src:
    lines=src.splitlines(keepends=True)
    inserted=False
    for i,line in enumerate(lines):
        if line.strip() != 'except Exception as e:':
            continue
        window=''.join(lines[i:i+6])
        if 'SEEKER_POST_SKIP' not in window:
            continue
        indent=line[:len(line)-len(line.lstrip())]+'    '
        inject=[
            indent+marker+'\n',
            indent+"err_name=type(e).__name__\n",
            indent+"err_text=str(e)\n",
            indent+"err_blob=(err_name+' '+err_text).lower()\n",
            indent+"hard_tokens=('chatwriteforbidden','userbannedinchannel','chatadminrequired','channelprivate','usernotparticipant','allowpaymentrequired','allow_payment_required','payment_required','usernameinvalid','usernamenotoccupied','peeridinvalid','write forbidden','admin required')\n",
            indent+"if any(tok in err_blob for tok in hard_tokens):\n",
            indent+"    conn.execute('insert into seeker_group_posts(source_key,group_title,posted_at,telegram_message_id,language,status,note) values(?,?,?,?,?,?,?)', (key,title_hint,datetime.now(timezone.utc).isoformat(),None,None,'blocked',(err_name+':'+err_text)[:220]))\n",
            indent+"    conn.commit()\n",
            indent+"    print('SEEKER_POST_BLOCKED',key,err_name,err_text[:120])\n",
            indent+"    continue\n",
        ]
        lines[i+1:i+1]=inject
        inserted=True
        break
    if not inserted:
        raise SystemExit('FATAL seeker_poster.py generic skip exception anchor not found')
    src=''.join(lines)

p.write_text(src)
print('PATCHED seeker_poster.py routing resilience')
PY

# Migrate only clearly invalid direct-recipient rows. Do not touch genuine
# FloodWait rows or generic ValueError rows; the latter get one controlled
# retry under the new worker guard.
python3 - <<'PY'
import sqlite3
p='/opt/tg-job-agent/telegram_jobs.db'
c=sqlite3.connect(p,timeout=30)
c.execute('pragma busy_timeout=30000')
cols={r[1] for r in c.execute('pragma table_info(send_queue)')}
if {'status','error'} <= cols:
    q="""UPDATE send_queue
         SET status='failed',
             error='invalid_recipient:migrated:' || coalesce(error,'')
         WHERE status='held_floodwait'
           AND (lower(coalesce(error,'')) LIKE '%usernameinvalid%'
                OR lower(coalesce(error,'')) LIKE '%username_not_occupied%'
                OR lower(coalesce(error,'')) LIKE '%usernamenotoccupied%'
                OR lower(coalesce(error,'')) LIKE '%peeridinvalid%')"""
    cur=c.execute(q)
    print('migrated_invalid_recipient_rows=',cur.rowcount)
else:
    print('send_queue schema missing status/error; migration skipped')
print('db_integrity=',c.execute('pragma integrity_check').fetchone()[0])
c.commit(); c.close()
PY

# Syntax validation before services are allowed back up.
"$APP/venv/bin/python" -m py_compile "$APP/worker.py" "$APP/seeker_poster.py"
echo 'py_compile=ok'

systemctl daemon-reload
systemctl enable --now tg-job-agent.service
# Keep seeker poster on its existing timer. Do not force a post during repair.
if systemctl list-unit-files --no-pager | grep -q '^tg-job-seeker-poster.timer'; then
  systemctl enable --now tg-job-seeker-poster.timer
fi

echo 'worker_state='$(systemctl is-active tg-job-agent.service || true)
echo 'poster_timer_state='$(systemctl is-active tg-job-seeker-poster.timer 2>/dev/null || true)

# Verification is read-only: service health + recent errors only. We do not
# invoke seeker_poster.py and therefore do not publish a test post.
systemctl status tg-job-agent.service --no-pager -l || true
journalctl -u tg-job-agent.service -n 40 --no-pager || true

echo "REPORT=$REPORT"
echo '=== TELEGRAM ROUTING RESILIENCE REPAIR COMPLETE ==='
