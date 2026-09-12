#!/usr/bin/env bash
set -euo pipefail

APP=/opt/tg-job-agent
DB="$APP/telegram_jobs.db"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=/root/tg-hourly-outbound-$STAMP.tgz
REPORT=/root/tg-hourly-outbound-$STAMP.txt
exec > >(tee "$REPORT") 2>&1

echo '=== TELEGRAM HOURLY OUTBOUND + WRITABLE FALLBACK REPAIR ==='
date -u

for f in "$APP/worker.py" "$APP/scanner.py" "$APP/discover_sources.py" "$APP/selector.py" "$APP/seeker_poster.py" "$DB"; do
  test -e "$f" || { echo "FATAL missing $f"; exit 10; }
done

tar --exclude='*.session-journal' -czf "$BACKUP" \
  "$APP/worker.py" "$APP/scanner.py" "$APP/discover_sources.py" \
  "$APP/selector.py" "$APP/seeker_poster.py" "$DB" 2>/dev/null || true
echo "backup=$BACKUP"

# Do not allow the legacy independent poster timer to race the hourly coordinator.
systemctl stop tg-job-seeker-poster.timer tg-job-seeker-poster.service 2>/dev/null || true
systemctl stop tg-job-hourly-outbound.timer tg-job-hourly-outbound.service 2>/dev/null || true

# Small Telegram FloodWaits (the observed 8s/19s class) should be slept through,
# not converted into a dead hour. Apply this to long-running/direct intake clients
# conservatively by setting Telethon's flood_sleep_threshold after start.
python3 - <<'PY'
from pathlib import Path
import ast,re
app=Path('/opt/tg-job-agent')
for name in ('worker.py','scanner.py','discover_sources.py'):
    p=app/name
    s=p.read_text()
    marker='# --- 2026-09-12 small FloodWait auto-sleep ---'
    if marker in s:
        print(name,'small FloodWait threshold already present')
        continue
    # Patch only the first await client.start() in each program.
    m=re.search(r'(?m)^(\s*)await client\.start\(\)\s*$',s)
    if not m:
        print(name,'WARN client.start anchor not found; left unchanged')
        continue
    indent=m.group(1)
    inject=(m.group(0)+'\n'+indent+marker+'\n'+indent+'client.flood_sleep_threshold = 60')
    s=s[:m.start()]+inject+s[m.end():]
    ast.parse(s)
    p.write_text(s)
    print(name,'patched flood_sleep_threshold=60')
PY

cat >"$APP/seeker_poster.py" <<'PY'
#!/usr/bin/env python3
import asyncio
import json
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import FloodWaitError, ChatWriteForbiddenError, UserBannedInChannelError
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.types import Channel, Chat, PeerChannel

APP=Path('/opt/tg-job-agent')
DB=APP/'telegram_jobs.db'
load_dotenv(APP/'.env')
API_ID=int(os.environ['TG_API_ID'])
API_HASH=os.environ['TG_API_HASH']
SESSION=str(APP/'telegram_poster')

COOLDOWN_DAYS=14
MAX_POSTS_PER_RUN=1
MAX_RUN_SECONDS=8*60
SMALL_FLOODWAIT_SECONDS=90
HARD_BLOCK_DAYS=30
RULE_RECHECK_DAYS=7
TRANSIENT_RECHECK_HOURS=6
API_PACE_MIN=1.0
API_PACE_MAX=1.8

EN_MSG=(
    "Hello! I’m open to new opportunities in operations, project/operations management, "
    "business development, partnerships, sales/account management, logistics/supply chain, "
    "procurement, e-commerce, hospitality/property operations and related management roles. "
    "Open to international and remote opportunities. CV available on request. "
    "Please DM me if you know of a relevant role. Thank you!"
)
RU_MSG=(
    "Здравствуйте! Рассматриваю новые возможности в операционном и проектном управлении, "
    "business development, партнёрствах, продажах/account management, логистике и supply chain, "
    "закупках, e-commerce, hospitality/property operations и смежных управленческих ролях. "
    "Рассматриваю международные и удалённые позиции. Резюме отправлю по запросу. "
    "Буду благодарен за личное сообщение, если знаете подходящую вакансию."
)

POSITIVE=[
    r'job\s*seek', r'jobseek', r'candidate', r'candidates\s+welcome',
    r'post\s+(?:your\s+)?(?:cv|resume)', r'cv\s+welcome', r'resume\s+welcome',
    r'looking\s+for\s+(?:a\s+)?job', r'looking\s+for\s+work', r'open\s+to\s+work',
    r'seeking\s+(?:a\s+)?job', r'seeking\s+(?:new\s+)?opportunit',
    r'vacanc(?:y|ies).{0,30}candidate', r'candidate.{0,30}vacanc(?:y|ies)',
    r'ищу\s+работ', r'в\s+поиске\s+работ', r'соискател', r'резюме', r'кандидат',
    r'поиск\s+работ', r'рассматриваю\s+(?:ваканс|предлож|позиц)',
]
NEGATIVE=[
    r'vacanc(?:y|ies)\s+only', r'jobs?\s+only', r'no\s+self.?promo', r'no\s+ads',
    r'no\s+advertis', r'employers?\s+only', r'recruiters?\s+only',
    r'только\s+ваканс', r'без\s+реклам', r'реклама\s+запрещ',
    r'соискател.{0,20}запрещ', r'резюме.{0,20}запрещ',
]

COUNTRY_TZ={
    'singapore':'Asia/Singapore','cambodia':'Asia/Phnom_Penh','vietnam':'Asia/Ho_Chi_Minh',
    'thailand':'Asia/Bangkok','indonesia':'Asia/Jakarta','bali':'Asia/Makassar',
    'malaysia':'Asia/Kuala_Lumpur','philippines':'Asia/Manila','hong kong':'Asia/Hong_Kong',
    'china':'Asia/Shanghai','taiwan':'Asia/Taipei','japan':'Asia/Tokyo','south korea':'Asia/Seoul',
    'india':'Asia/Kolkata','pakistan':'Asia/Karachi','bangladesh':'Asia/Dhaka','nepal':'Asia/Kathmandu',
    'uae':'Asia/Dubai','united arab emirates':'Asia/Dubai','dubai':'Asia/Dubai','qatar':'Asia/Qatar',
    'saudi arabia':'Asia/Riyadh','israel':'Asia/Jerusalem','turkey':'Europe/Istanbul',
    'netherlands':'Europe/Amsterdam','germany':'Europe/Berlin','france':'Europe/Paris','spain':'Europe/Madrid',
    'italy':'Europe/Rome','switzerland':'Europe/Zurich','austria':'Europe/Vienna','poland':'Europe/Warsaw',
    'czechia':'Europe/Prague','czech republic':'Europe/Prague','slovakia':'Europe/Bratislava',
    'finland':'Europe/Helsinki','sweden':'Europe/Stockholm','norway':'Europe/Oslo','denmark':'Europe/Copenhagen',
    'latvia':'Europe/Riga','lithuania':'Europe/Vilnius','estonia':'Europe/Tallinn','uk':'Europe/London',
    'united kingdom':'Europe/London','ireland':'Europe/Dublin','portugal':'Europe/Lisbon','greece':'Europe/Athens',
    'romania':'Europe/Bucharest','bulgaria':'Europe/Sofia','serbia':'Europe/Belgrade','croatia':'Europe/Zagreb',
    'egypt':'Africa/Cairo','south africa':'Africa/Johannesburg','kenya':'Africa/Nairobi','nigeria':'Africa/Lagos',
    'morocco':'Africa/Casablanca','ghana':'Africa/Accra','ethiopia':'Africa/Addis_Ababa','tanzania':'Africa/Dar_es_Salaam',
    'brazil':'America/Sao_Paulo','argentina':'America/Argentina/Buenos_Aires','chile':'America/Santiago',
    'colombia':'America/Bogota','peru':'America/Lima','ecuador':'America/Guayaquil','uruguay':'America/Montevideo',
    'mexico':'America/Mexico_City','costa rica':'America/Costa_Rica','panama':'America/Panama',
    'usa':'America/New_York','united states':'America/New_York','canada':'America/Toronto',
}
INFER_TZ=[
    (r'\b(?:singapore|sg)\b','Asia/Singapore'),(r'\bcambodia\b|phnom','Asia/Phnom_Penh'),
    (r'\bvietnam\b|saigon|hcmc','Asia/Ho_Chi_Minh'),(r'\bthailand\b|bangkok','Asia/Bangkok'),
    (r'\bbali\b','Asia/Makassar'),(r'\bindonesia\b|jakarta','Asia/Jakarta'),
    (r'\bmalaysia\b|kuala','Asia/Kuala_Lumpur'),(r'\bphilippines\b|manila','Asia/Manila'),
    (r'\bdubai\b|\buae\b','Asia/Dubai'),(r'\bqatar\b|doha','Asia/Qatar'),
    (r'\bsaudi\b|riyadh','Asia/Riyadh'),(r'\bisrael\b|tel aviv','Asia/Jerusalem'),
    (r'\bgermany\b|berlin|frankfurt','Europe/Berlin'),(r'\bnetherlands\b|amsterdam','Europe/Amsterdam'),
    (r'\bswitzerland\b|zurich|geneva','Europe/Zurich'),(r'\bpoland\b|warsaw','Europe/Warsaw'),
    (r'\buk\b|london|united kingdom','Europe/London'),(r'\bspain\b|barcelona|madrid','Europe/Madrid'),
    (r'\bfrance\b|paris','Europe/Paris'),(r'\bitaly\b|milan|rome','Europe/Rome'),
    (r'\bsouth africa\b|johannesburg|cape town','Africa/Johannesburg'),(r'\bkenya\b|nairobi','Africa/Nairobi'),
    (r'\bnigeria\b|lagos','Africa/Lagos'),(r'\bmorocco\b|casablanca','Africa/Casablanca'),
    (r'\bcolombia\b|bogota|medellin','America/Bogota'),(r'\bbrazil\b|sao paulo','America/Sao_Paulo'),
    (r'\bargentina\b|buenos aires','America/Argentina/Buenos_Aires'),(r'\bchile\b|santiago','America/Santiago'),
    (r'\bmexico\b|mexico city','America/Mexico_City'),(r'\busa\b|united states|new york','America/New_York'),
    (r'california|los angeles|san francisco','America/Los_Angeles'),(r'\bcanada\b|toronto','America/Toronto'),
]


def utcnow():
    return datetime.now(timezone.utc)

def iso(dt=None):
    return (dt or utcnow()).isoformat()

def parse_dt(v):
    if not v: return None
    s=str(v).strip().replace('Z','+00:00')
    try:
        d=datetime.fromisoformat(s)
        if d.tzinfo is None: d=d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None

def cols(conn, table):
    return [r[1] for r in conn.execute(f'pragma table_info("{table}")')]

def pick(candidates, available):
    low={x.lower():x for x in available}
    for c in candidates:
        if c.lower() in low: return low[c.lower()]
    return None

def parse_target(v):
    if v is None: return None
    s=str(v).strip()
    if not s: return None
    s=re.sub(r'^https?://','',s,flags=re.I)
    if s.startswith('t.me/+'):
        return ('invite',s.split('t.me/+',1)[1].split('?',1)[0].strip('/'))
    if s.startswith('t.me/joinchat/'):
        return ('invite',s.split('t.me/joinchat/',1)[1].split('?',1)[0].strip('/'))
    if s.startswith('t.me/'):
        s=s.split('t.me/',1)[1].split('?',1)[0].strip('/')
    if s.startswith('@') and re.fullmatch(r'@[A-Za-z0-9_]{5,}',s): return ('public',s)
    if re.fullmatch(r'[A-Za-z0-9_]{5,}',s): return ('public','@'+s)
    try: return ('id',int(s))
    except Exception: return None

def russianish(text):
    cyr=len(re.findall(r'[А-Яа-яЁё]', text or ''))
    lat=len(re.findall(r'[A-Za-z]', text or ''))
    return cyr > lat and cyr >= 10

def rules_decision(text):
    t=(text or '').lower()
    if any(re.search(p,t,re.S) for p in NEGATIVE): return 'negative'
    if any(re.search(p,t,re.S) for p in POSITIVE): return 'positive'
    return 'unknown'

def infer_timezone(country, blob):
    c=(country or '').strip().lower()
    if c in COUNTRY_TZ: return COUNTRY_TZ[c], 'country'
    b=(blob or '').lower()
    for pat,tz in INFER_TZ:
        if re.search(pat,b,re.I): return tz, 'inferred'
    return None, 'unknown'

def in_contact_window(tzname):
    if not tzname: return True, None
    try:
        local=utcnow().astimezone(ZoneInfo(tzname))
        mins=local.hour*60+local.minute
        return (8*60)<=mins<(20*60), local
    except Exception:
        return True, None

def ensure_tables(conn):
    conn.execute('''create table if not exists seeker_group_posts(
        id integer primary key autoincrement,
        source_key text not null,
        group_title text,
        posted_at text not null,
        telegram_message_id integer,
        language text,
        status text not null,
        note text
    )''')
    conn.execute('create index if not exists ix_seeker_posts_key_time on seeker_group_posts(source_key,posted_at)')
    conn.execute('''create table if not exists seeker_group_state(
        source_key text primary key,
        canonical_key text,
        group_title text,
        state text not null default 'unknown',
        next_check_at text,
        last_checked_at text,
        linked_key text,
        failures integer not null default 0,
        last_error text,
        country text,
        timezone text
    )''')
    conn.execute('''create table if not exists seeker_runtime_state(
        key text primary key,
        value text
    )''')
    conn.execute('''create table if not exists telegram_rate_limits(
        scope text primary key,
        until_at text,
        seconds integer,
        updated_at text,
        note text
    )''')
    conn.execute('''create table if not exists seeker_run_log(
        id integer primary key autoincrement,
        started_at text not null,
        finished_at text,
        considered integer not null default 0,
        telegram_checked integer not null default 0,
        blocked integer not null default 0,
        rule_rejected integer not null default 0,
        sent integer not null default 0,
        floodwait_seconds integer,
        status text,
        note text
    )''')
    conn.commit()

def set_state(conn,key,state,*,title='',canonical=None,next_after=None,error='',country='',tz='',linked=None,increment_failure=False):
    next_at=iso(utcnow()+next_after) if next_after else None
    conn.execute('''insert into seeker_group_state(source_key,canonical_key,group_title,state,next_check_at,last_checked_at,linked_key,failures,last_error,country,timezone)
                    values(?,?,?,?,?,?,?,?,?,?,?)
                    on conflict(source_key) do update set
                    canonical_key=coalesce(excluded.canonical_key,seeker_group_state.canonical_key),
                    group_title=case when excluded.group_title<>'' then excluded.group_title else seeker_group_state.group_title end,
                    state=excluded.state,next_check_at=excluded.next_check_at,last_checked_at=excluded.last_checked_at,
                    linked_key=coalesce(excluded.linked_key,seeker_group_state.linked_key),
                    failures=case when ? then seeker_group_state.failures+1 else seeker_group_state.failures end,
                    last_error=excluded.last_error,country=excluded.country,timezone=excluded.timezone''',
                 (key,canonical,title,state,next_at,iso(),linked,1 if increment_failure else 0,error[:300],country,tz,1 if increment_failure else 0))
    conn.commit()

def state_due(conn,key):
    r=conn.execute('select state,next_check_at from seeker_group_state where source_key=?',(key,)).fetchone()
    if not r: return True
    nxt=parse_dt(r[1])
    return not nxt or nxt<=utcnow()

def set_rate_limit(conn,seconds,note):
    sec=max(0,int(seconds or 0))
    until=utcnow()+timedelta(seconds=sec+2)
    conn.execute('''insert into telegram_rate_limits(scope,until_at,seconds,updated_at,note) values('seeker_poster',?,?,?,?)
                    on conflict(scope) do update set until_at=excluded.until_at,seconds=excluded.seconds,updated_at=excluded.updated_at,note=excluded.note''',
                 (iso(until),sec,iso(),note[:300]))
    conn.commit()

def current_rate_limit(conn):
    r=conn.execute("select until_at,seconds,note from telegram_rate_limits where scope='seeker_poster'").fetchone()
    if not r: return None
    until=parse_dt(r[0])
    if until and until>utcnow(): return (until,int(r[1] or 0),r[2] or '')
    conn.execute("delete from telegram_rate_limits where scope='seeker_poster'")
    conn.commit()
    return None

def canonical_key(entity):
    username=getattr(entity,'username',None)
    if username: return '@'+username.lower()
    eid=getattr(entity,'id',None)
    return f'id:{eid}' if eid is not None else None

async def get_full(client,entity):
    try:
        return await client(GetFullChannelRequest(entity)) if isinstance(entity,Channel) else None
    except FloodWaitError:
        raise
    except Exception:
        return None

async def linked_discussion(client,channel,full=None):
    if not isinstance(channel,Channel): return None
    full=full or await get_full(client,channel)
    if not full: return None
    lid=getattr(full.full_chat,'linked_chat_id',None)
    if not lid: return None
    for chat in getattr(full,'chats',[]) or []:
        if getattr(chat,'id',None)==lid:
            return chat
    try:
        return await client.get_entity(PeerChannel(lid))
    except FloodWaitError:
        raise
    except Exception:
        return None

async def collect_evidence(client,entity,title_hint=''):
    title=getattr(entity,'title',None) or title_hint or canonical_key(entity) or 'group'
    about=''
    full=None
    if isinstance(entity,Channel):
        try:
            full=await client(GetFullChannelRequest(entity))
            about=getattr(full.full_chat,'about','') or ''
        except FloodWaitError:
            raise
        except Exception:
            pass
    recent=[]
    try:
        async for m in client.iter_messages(entity,limit=20):
            if m.message: recent.append(m.message)
    except FloodWaitError:
        raise
    except Exception:
        pass
    return title,'\n'.join([title,about]+recent)[:30000],full

async def ensure_joined(client,entity):
    if isinstance(entity,Channel) and getattr(entity,'megagroup',False) and getattr(entity,'left',False):
        await client(JoinChannelRequest(entity))
        await asyncio.sleep(random.uniform(2.0,4.0))
        try: entity=await client.get_entity(entity)
        except Exception: pass
    return entity

def hard_error(e):
    blob=(type(e).__name__+' '+str(e)).lower()
    tokens=(
        'chatwriteforbidden','userbannedinchannel','chatadminrequired','channelprivate',
        'allowpaymentrequired','allow_payment_required','payment_required','write forbidden',
        'admin required','usernameinvalid','usernamenotoccupied','peeridinvalid'
    )
    return any(t in blob for t in tokens)

def joinable_error(e):
    blob=(type(e).__name__+' '+str(e)).lower()
    return 'usernotparticipant' in blob or 'not participant' in blob

async def resolve_public(client,target):
    kind,value=target
    if kind=='invite':
        # Do not auto-join arbitrary private invite links without rules evidence.
        return None
    return await client.get_entity(value)

async def try_candidate(client,conn,key,target,title_hint,country,tzname):
    entity=await resolve_public(client,target)
    if entity is None:
        set_state(conn,key,'invite_unverified',title=title_hint,next_after=timedelta(days=RULE_RECHECK_DAYS),error='private invite not auto-joined without rules evidence',country=country,tz=tzname)
        return 'blocked',None,None

    # Broadcast channels are discovery sources, not posting targets. Prefer linked discussion.
    original=entity
    full=None
    if isinstance(entity,Channel) and not getattr(entity,'megagroup',False):
        try: full=await client(GetFullChannelRequest(entity))
        except FloodWaitError: raise
        except Exception: full=None
        linked=await linked_discussion(client,entity,full)
        if linked is None:
            set_state(conn,key,'broadcast_no_discussion',title=getattr(entity,'title',title_hint) or title_hint,next_after=timedelta(days=HARD_BLOCK_DAYS),error='broadcast channel; no linked discussion',country=country,tz=tzname)
            return 'blocked',None,None
        entity=linked

    if not isinstance(entity,(Channel,Chat)):
        set_state(conn,key,'not_group',title=title_hint,next_after=timedelta(days=HARD_BLOCK_DAYS),error='resolved entity is not a group',country=country,tz=tzname)
        return 'blocked',None,None
    if isinstance(entity,Channel) and not getattr(entity,'megagroup',False):
        set_state(conn,key,'not_writable_group',title=getattr(entity,'title',title_hint) or title_hint,next_after=timedelta(days=HARD_BLOCK_DAYS),error='resolved target not megagroup',country=country,tz=tzname)
        return 'blocked',None,None

    title,evidence,_=await collect_evidence(client,entity,title_hint)
    decision=rules_decision(evidence)
    ckey=canonical_key(entity) or key
    linked_key=ckey if entity is not original else None
    if decision=='negative':
        set_state(conn,key,'rules_negative',title=title,canonical=ckey,next_after=timedelta(days=HARD_BLOCK_DAYS),error='rules/evidence prohibit seeker posts',country=country,tz=tzname,linked=linked_key)
        return 'rule_rejected',None,None
    if decision!='positive':
        set_state(conn,key,'rules_unknown',title=title,canonical=ckey,next_after=timedelta(days=RULE_RECHECK_DAYS),error='no positive seeker-post evidence',country=country,tz=tzname,linked=linked_key)
        return 'rule_rejected',None,None

    # Cooldown is by canonical writable group, not merely by discovery source.
    cutoff=iso(utcnow()-timedelta(days=COOLDOWN_DAYS))
    if conn.execute('select 1 from seeker_group_posts where lower(source_key)=lower(?) and status="sent" and posted_at>=? limit 1',(ckey,cutoff)).fetchone():
        set_state(conn,key,'cooldown',title=title,canonical=ckey,next_after=timedelta(days=COOLDOWN_DAYS),error='canonical group still in repost cooldown',country=country,tz=tzname,linked=linked_key)
        return 'blocked',None,None

    try:
        entity=await ensure_joined(client,entity)
        msg=RU_MSG if russianish(evidence) else EN_MSG
        lang='ru' if msg==RU_MSG else 'en'
        try:
            sent=await client.send_message(entity,msg,link_preview=False)
        except Exception as e:
            if joinable_error(e) and isinstance(entity,Channel):
                entity=await client(JoinChannelRequest(entity))
                await asyncio.sleep(random.uniform(2.0,4.0))
                sent=await client.send_message(entity,msg,link_preview=False)
            else:
                raise
        ckey=canonical_key(entity) or ckey
        conn.execute('insert into seeker_group_posts(source_key,group_title,posted_at,telegram_message_id,language,status,note) values(?,?,?,?,?,?,?)',
                     (ckey,title,iso(),getattr(sent,'id',None),lang,'sent',f'approved seeker post; source={key}; no CV attached'))
        conn.commit()
        set_state(conn,key,'writable_sent',title=title,canonical=ckey,next_after=timedelta(days=COOLDOWN_DAYS),error='',country=country,tz=tzname,linked=linked_key)
        return 'sent',title,(getattr(entity,'username',None),getattr(sent,'id',None),lang,ckey)
    except (ChatWriteForbiddenError,UserBannedInChannelError) as e:
        set_state(conn,key,'blocked_write',title=title,canonical=ckey,next_after=timedelta(days=HARD_BLOCK_DAYS),error=type(e).__name__+':'+str(e),country=country,tz=tzname,linked=linked_key,increment_failure=True)
        return 'blocked',None,None
    except Exception as e:
        if hard_error(e):
            set_state(conn,key,'blocked_hard',title=title,canonical=ckey,next_after=timedelta(days=HARD_BLOCK_DAYS),error=type(e).__name__+':'+str(e),country=country,tz=tzname,linked=linked_key,increment_failure=True)
            return 'blocked',None,None
        raise

async def main():
    started=utcnow(); deadline=time.monotonic()+MAX_RUN_SECONDS
    conn=sqlite3.connect(DB,timeout=30)
    conn.execute('pragma busy_timeout=30000')
    ensure_tables(conn)
    run_id=conn.execute('insert into seeker_run_log(started_at,status) values(?,?)',(iso(started),'running')).lastrowid
    conn.commit()

    rl=current_rate_limit(conn)
    if rl:
        until,seconds,note=rl
        conn.execute('update seeker_run_log set finished_at=?,status=?,floodwait_seconds=?,note=? where id=?',(iso(),'rate_limited',seconds,f'until={iso(until)} {note}'[:300],run_id))
        conn.commit(); conn.close()
        print('SEEKER_POSTER_RATE_LIMIT_ACTIVE',seconds,iso(until),note)
        return

    available=cols(conn,'sources')
    handle_col=pick(['username','handle','telegram_username','source_username','chat_username','url','link','source','chat_id','telegram_id','peer_id'],available)
    title_col=pick(['title','name','source_name','chat_title'],available)
    active_col=pick(['active','enabled','is_active','status'],available)
    country_col=pick(['country','country_name','market','location_country','region'],available)
    tz_col=pick(['timezone','source_timezone','tz'],available)
    if not handle_col:
        conn.execute('update seeker_run_log set finished_at=?,status=?,note=? where id=?',(iso(),'no_handle_column','sources table has no usable handle column',run_id)); conn.commit(); conn.close()
        print('SEEKER_POSTER no usable source handle column')
        return

    rows=conn.execute('select rowid,* from sources order by rowid').fetchall()
    names=['rowid']+available
    candidates=[]
    for row in rows:
        d=dict(zip(names,row))
        if active_col:
            av=str(d.get(active_col,'')).strip().lower()
            if av in ('0','false','disabled','inactive','rejected','closed'): continue
        target=parse_target(d.get(handle_col))
        if not target: continue
        title=str(d.get(title_col) or '')
        country=str(d.get(country_col) or '') if country_col else ''
        explicit_tz=str(d.get(tz_col) or '').strip() if tz_col else ''
        blob=' '.join([str(target[1]),title,country])
        tzname=explicit_tz or infer_timezone(country,blob)[0]
        in_window,local=in_contact_window(tzname)
        if not in_window: continue
        key=(target[0]+':'+str(target[1])).lower()
        if not state_due(conn,key): continue
        # Known timezone candidates first; preserve rowid for a stable rotating cursor.
        candidates.append((0 if tzname else 1,int(d['rowid']),key,target,title,country,tzname))

    cursor_row=0
    r=conn.execute("select value from seeker_runtime_state where key='poster_cursor_rowid'").fetchone()
    if r:
        try: cursor_row=int(r[0])
        except Exception: cursor_row=0
    candidates.sort(key=lambda x:(x[0],0 if x[1]>cursor_row else 1,x[1]))

    considered=telegram_checked=blocked=rule_rejected=sent_count=0
    floodwait_seconds=None
    status='zero'
    note=''
    client=TelegramClient(SESSION,API_ID,API_HASH)
    client.flood_sleep_threshold=0
    await client.start()

    try:
        for _,rowid,key,target,title,country,tzname in candidates:
            if time.monotonic()>=deadline:
                status='runtime_budget'
                note='candidate scan reached safe 8-minute runtime budget; cursor persisted'
                break
            considered+=1
            conn.execute("insert into seeker_runtime_state(key,value) values('poster_cursor_rowid',?) on conflict(key) do update set value=excluded.value",(str(rowid),)); conn.commit()
            await asyncio.sleep(random.uniform(API_PACE_MIN,API_PACE_MAX))
            telegram_checked+=1
            outcome=None
            for attempt in range(2):
                try:
                    outcome,title_out,meta=await try_candidate(client,conn,key,target,title,country,tzname)
                    break
                except FloodWaitError as e:
                    sec=max(1,int(getattr(e,'seconds',0) or 1))
                    set_rate_limit(conn,sec,f'{key}:{type(e).__name__}')
                    print('SEEKER_POSTER_FLOOD_WAIT',sec,key,'attempt',attempt+1)
                    if sec<=SMALL_FLOODWAIT_SECONDS and attempt==0 and time.monotonic()+sec+5<deadline:
                        await asyncio.sleep(sec+random.uniform(2.0,4.0))
                        current_rate_limit(conn)  # clears elapsed record
                        continue
                    floodwait_seconds=sec
                    status='floodwait'
                    note=f'FloodWait {sec}s at {key}; resume after server-enforced cooldown'
                    outcome='stop'
                    break
                except Exception as e:
                    if hard_error(e):
                        set_state(conn,key,'blocked_hard',title=title,next_after=timedelta(days=HARD_BLOCK_DAYS),error=type(e).__name__+':'+str(e),country=country,tz=tzname,increment_failure=True)
                        outcome='blocked'
                        print('SEEKER_POST_BLOCKED',key,type(e).__name__,str(e)[:140])
                        break
                    set_state(conn,key,'transient_error',title=title,next_after=timedelta(hours=TRANSIENT_RECHECK_HOURS),error=type(e).__name__+':'+str(e),country=country,tz=tzname,increment_failure=True)
                    outcome='transient'
                    print('SEEKER_POST_TRANSIENT',key,type(e).__name__,str(e)[:140])
                    break
            if outcome=='sent':
                sent_count+=1; status='sent'
                username,msg_id,lang,ckey=meta
                print('SEEKER_POST_SENT',country or 'unknown',title_out,('@'+username if username else ckey),msg_id,lang)
                break
            if outcome=='blocked': blocked+=1
            elif outcome=='rule_rejected': rule_rejected+=1
            elif outcome=='stop': break
        else:
            if sent_count==0:
                status='exhausted_current_window'
                note='all immediately due candidates in current contact windows were evaluated/skipped by persisted safety state'
    finally:
        await client.disconnect()
        conn.execute('''update seeker_run_log set finished_at=?,considered=?,telegram_checked=?,blocked=?,rule_rejected=?,sent=?,floodwait_seconds=?,status=?,note=? where id=?''',
                     (iso(),considered,telegram_checked,blocked,rule_rejected,sent_count,floodwait_seconds,status,note[:300],run_id))
        conn.commit()
        print('SEEKER_POSTER_DONE','status=',status,'considered=',considered,'telegram_checked=',telegram_checked,'blocked=',blocked,'rule_rejected=',rule_rejected,'sent=',sent_count,'floodwait=',floodwait_seconds)
        conn.close()

if __name__=='__main__':
    asyncio.run(main())
PY

cat >"$APP/hourly_outbound.py" <<'PY'
#!/usr/bin/env python3
import sqlite3, subprocess, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

APP=Path('/opt/tg-job-agent')
DB=APP/'telegram_jobs.db'

def utcnow(): return datetime.now(timezone.utc)
def parse_dt(v):
    if not v: return None
    try:
        d=datetime.fromisoformat(str(v).replace('Z','+00:00'))
        if d.tzinfo is None: d=d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception: return None

def conn():
    c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row; c.execute('pragma busy_timeout=30000'); return c

def recent_direct_sent(since):
    c=conn()
    try:
        cols={r[1] for r in c.execute('pragma table_info(send_queue)')}
        needed={'status','telegram_message_id'}
        if not needed<=cols: return None
        timecols=[x for x in ('processed_at','sent_at','updated_at','created_at') if x in cols]
        select=['id','recipient','status','telegram_message_id']+timecols
        rows=c.execute('select '+','.join(select)+" from send_queue where lower(coalesce(status,''))='sent' and telegram_message_id is not null order by id desc limit 100").fetchall()
        for r in rows:
            stamp=None
            for tc in timecols:
                stamp=parse_dt(r[tc])
                if stamp: break
            if stamp and stamp>=since:
                return dict(r)
        return None
    finally: c.close()

def queue_counts():
    c=conn()
    try:
        return {str(r[0]):int(r[1]) for r in c.execute('select coalesce(status,""),count(*) from send_queue group by status')}
    except Exception:
        return {}
    finally: c.close()

def latest_poster_run(since):
    c=conn()
    try:
        if not c.execute("select 1 from sqlite_master where type='table' and name='seeker_run_log'").fetchone(): return None
        r=c.execute('select * from seeker_run_log where started_at>=? order by id desc limit 1',(since.isoformat(),)).fetchone()
        return dict(r) if r else None
    finally: c.close()

def recent_seeker_sent(since):
    c=conn()
    try:
        r=c.execute("select source_key,group_title,posted_at,telegram_message_id,language from seeker_group_posts where status='sent' order by id desc limit 20").fetchall()
        for x in r:
            if (parse_dt(x['posted_at']) or datetime.min.replace(tzinfo=timezone.utc))>=since:
                return dict(x)
        return None
    except Exception:
        return None
    finally: c.close()

def run(cmd,timeout):
    try:
        return subprocess.run(cmd,check=False,timeout=timeout,text=True,capture_output=True)
    except Exception as e:
        print('COMMAND_ERROR',cmd,type(e).__name__,str(e)[:180],flush=True); return None

def main():
    started=utcnow()
    print('HOURLY_OUTBOUND_START',started.isoformat(),flush=True)

    # Priority 1: direct Telegram queue. Trigger selector, then give the continuous worker
    # a bounded opportunity to prove delivery.
    r=run(['systemctl','start','tg-job-selector.service'],90)
    if r and r.returncode!=0: print('SELECTOR_START_ERROR',r.stderr[-400:],flush=True)
    for _ in range(6):
        direct=recent_direct_sent(started-timedelta(seconds=2))
        if direct:
            print('HOURLY_DIRECT_SUCCESS',direct.get('recipient'),direct.get('telegram_message_id'),flush=True)
            return 0
        time.sleep(5)

    # Priority 2: one candidate post. The poster itself enforces group rules,
    # world-local contact windows, cooldowns, join policy and FloodWait.
    r=run(['systemctl','start','--wait','tg-job-seeker-poster.service'],10*60)
    if r:
        if r.stdout: print(r.stdout[-2000:],flush=True)
        if r.returncode!=0: print('POSTER_SERVICE_ERROR',r.returncode,r.stderr[-1000:],flush=True)
    sent=recent_seeker_sent(started-timedelta(seconds=2))
    if sent:
        print('HOURLY_CANDIDATE_POST_SUCCESS',sent.get('group_title'),sent.get('source_key'),sent.get('telegram_message_id'),flush=True)
        return 0

    pr=latest_poster_run(started-timedelta(seconds=2)) or {}
    qc=queue_counts()
    print('HOURLY_FLOOR_MISSED',
          'direct_queue=',qc,
          'candidate_status=',pr.get('status'),
          'candidate_considered=',pr.get('considered'),
          'telegram_checked=',pr.get('telegram_checked'),
          'blocked=',pr.get('blocked'),
          'rule_rejected=',pr.get('rule_rejected'),
          'floodwait_seconds=',pr.get('floodwait_seconds'),
          'note=',pr.get('note'),flush=True)
    return 2

if __name__=='__main__':
    raise SystemExit(main())
PY

cat >/etc/systemd/system/tg-job-seeker-poster.service <<'EOF'
[Unit]
Description=Telegram writable-group job-seeker fallback
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=tgjob
WorkingDirectory=/opt/tg-job-agent
ExecStart=/opt/tg-job-agent/venv/bin/python /opt/tg-job-agent/seeker_poster.py
TimeoutStartSec=9min
Nice=10
EOF

cat >/etc/systemd/system/tg-job-hourly-outbound.service <<'EOF'
[Unit]
Description=Telegram hourly outbound coordinator (direct first, writable-group fallback)
After=network-online.target tg-job-agent.service
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/tg-job-agent
ExecStart=/opt/tg-job-agent/venv/bin/python /opt/tg-job-agent/hourly_outbound.py
TimeoutStartSec=12min
Nice=5
EOF

cat >/etc/systemd/system/tg-job-hourly-outbound.timer <<'EOF'
[Unit]
Description=Run Telegram global outbound cycle every hour

[Timer]
OnBootSec=8min
OnUnitActiveSec=1h
RandomizedDelaySec=2min
AccuracySec=30s
Persistent=true
Unit=tg-job-hourly-outbound.service

[Install]
WantedBy=timers.target
EOF

# Reduce account-level API bursts without slowing the outbound floor. Discovery
# does not need to run every 30m once the inventory is large; scanner remains fresh.
cat >/etc/systemd/system/tg-job-discovery.timer <<'EOF'
[Unit]
Description=Telegram job source discovery every 60 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=60min
RandomizedDelaySec=3min
AccuracySec=30s
Persistent=true
Unit=tg-job-discovery.service

[Install]
WantedBy=timers.target
EOF

cat >/etc/systemd/system/tg-job-scanner.timer <<'EOF'
[Unit]
Description=Telegram job scanner every 15 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=15min
RandomizedDelaySec=90s
AccuracySec=20s
Persistent=true
Unit=tg-job-scanner.service

[Install]
WantedBy=timers.target
EOF

chown tgjob:tgjob "$APP/seeker_poster.py" "$APP/hourly_outbound.py" 2>/dev/null || true
chmod 750 "$APP/seeker_poster.py" "$APP/hourly_outbound.py"

# Validate before enabling anything.
"$APP/venv/bin/python" -m py_compile "$APP/worker.py" "$APP/scanner.py" "$APP/discover_sources.py" "$APP/seeker_poster.py" "$APP/hourly_outbound.py"
python3 - <<'PY'
import sqlite3
p='/opt/tg-job-agent/telegram_jobs.db'; c=sqlite3.connect(p,timeout=30)
print('db_integrity=',c.execute('pragma integrity_check').fetchone()[0])
print('sources=',c.execute('select count(*) from sources').fetchone()[0])
print('queue=',list(c.execute('select status,count(*) from send_queue group by status order by status')))
c.close()
PY

systemctl daemon-reload
systemctl enable --now tg-job-agent.service
systemctl enable --now tg-job-discovery.timer tg-job-scanner.timer tg-job-selector.timer
# Legacy independent poster schedule is intentionally disabled; hourly coordinator owns fallback.
systemctl disable --now tg-job-seeker-poster.timer 2>/dev/null || true
systemctl enable --now tg-job-hourly-outbound.timer

echo '--- TIMER STATUS ---'
systemctl list-timers --all --no-pager | grep -E 'tg-job-(hourly-outbound|discovery|scanner|selector|seeker-poster)' || true
echo '--- WORKER ---'
systemctl is-active tg-job-agent.service || true

echo '--- DB SAFETY STATE ---'
python3 - <<'PY'
import sqlite3
c=sqlite3.connect('/opt/tg-job-agent/telegram_jobs.db',timeout=30)
print('integrity=',c.execute('pragma integrity_check').fetchone()[0])
for t in ('seeker_group_posts','seeker_group_state','seeker_run_log','telegram_rate_limits'):
    try: print(t,c.execute(f'select count(*) from {t}').fetchone()[0])
    except Exception as e: print(t,'not-created-until-first-run',repr(e))
c.close()
PY

echo "backup=$BACKUP"
echo "report=$REPORT"
echo 'NOTE: repair does not force a test publication; next hourly coordinator run uses the normal safety path.'
echo '=== REPAIR COMPLETE ==='
