#!/usr/bin/env python3
import argparse
import ast
import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from candidate_classifier import classify_post
from salary_policy import salary_rejection_reason

ROOT = Path('/opt/tg-job-agent')
DB = ROOT / 'telegram_jobs.db'
SELECTOR = ROOT / 'selector.py'

EMAIL_RE = re.compile(r'(?i)(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])')
URL_RE = re.compile(r'https?://[^\s<>()]+', re.I)

APPLY_WORDS = re.compile(
    r'apply|application|send\s+(?:your\s+)?(?:cv|resume)|cv\s*(?:to|at)|resume\s*(?:to|at)|'
    r'email\s*(?:to|at)|recruit|career|vacanc|hiring|job\s+opening|'
    r'отправ|присыл|резюме|отклик|вакан|нанима|'
    r'สมัคร|งาน|tuyển\s*dụng|việc\s*làm|招聘|应聘|応募|採用|채용|구인|'
    r'empleo|vacante|postul|curr[ií]cul|vaga|emprego|candidat', re.I
)
HR_WORDS = re.compile(r'hr|human\s*resources|talent|recruit|career|job|people', re.I)
BAD_EMAIL_LOCAL = re.compile(r'^(?:privacy|legal|support|press|media|abuse|noreply|no-reply)$', re.I)
SOCIAL_HOSTS = {
    't.me','telegram.me','telegram.org','instagram.com','www.instagram.com',
    'facebook.com','www.facebook.com','youtube.com','www.youtube.com','x.com','twitter.com',
    'linkedin.com','www.linkedin.com','whatsapp.com','www.whatsapp.com'
}
ATS_HOST_HINTS = (
    'greenhouse.io','lever.co','workdayjobs.com','myworkdayjobs.com','smartrecruiters.com',
    'ashbyhq.com','recruitee.com','workable.com','jobvite.com','breezy.hr','personio.',
    'bamboohr.com','teamtailor.com','comeet.com','careers-page.com','camhr.com'
)
JOB_PATH_HINT = re.compile(r'/jobs?\b|/careers?\b|/vacanc|/apply\b|application|jobid=|job_id=|position', re.I)
JOB_BOARD_HOSTS = ('camhr.com','indeed.','glassdoor.','jobsdb.','jobstreet.','jooble.','layboard.')


def now():
    return datetime.now(timezone.utc).isoformat()


def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    return con


def ensure_schema(con):
    con.executescript('''
    CREATE TABLE IF NOT EXISTS application_routes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        job_row_id INTEGER,
        route_kind TEXT NOT NULL,
        target TEXT NOT NULL,
        target_key TEXT NOT NULL,
        employer_key TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        reason TEXT,
        subject TEXT,
        message TEXT,
        cv_variant TEXT,
        payload_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        processed_at TEXT,
        external_id TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ux_application_routes_target
      ON application_routes(route_kind,target_key);
    CREATE INDEX IF NOT EXISTS ix_application_routes_status
      ON application_routes(status,route_kind,created_at);
    CREATE INDEX IF NOT EXISTS ix_application_routes_employer
      ON application_routes(employer_key,status);

    CREATE TABLE IF NOT EXISTS outbound_contact_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel TEXT NOT NULL,
        target TEXT NOT NULL,
        target_key TEXT NOT NULL,
        employer_key TEXT,
        job_id TEXT,
        status TEXT NOT NULL,
        sent_at TEXT,
        external_id TEXT,
        metadata_json TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ux_outbound_history_target
      ON outbound_contact_history(channel,target_key);
    CREATE INDEX IF NOT EXISTS ix_outbound_history_employer
      ON outbound_contact_history(employer_key,status);
    ''')
    con.commit()


def selector_keywords():
    values = {'GOOD': [], 'BAD': [], 'HARD_TECH': []}
    try:
        tree = ast.parse(SELECTOR.read_text())
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in values:
                try:
                    values[target.id] = list(ast.literal_eval(node.value))
                except Exception:
                    pass
    except Exception:
        pass
    return values


def fit_ok(text, kw):
    t = (text or '').lower()
    good = kw.get('GOOD') or []
    bad = kw.get('BAD') or []
    hard = kw.get('HARD_TECH') or []
    if good and not any(x.lower() in t for x in good):
        return False
    if any(x.lower() in t for x in bad):
        return False
    if any(x.lower() in t for x in hard):
        return False
    return True


def norm_email(v):
    return (v or '').strip().lower()


def extract_emails(text):
    out = []
    seen = set()
    for m in EMAIL_RE.finditer(text or ''):
        e = norm_email(m.group(1))
        if e not in seen:
            seen.add(e); out.append((e, m.start(), m.end()))
    return out


def email_score(text, item):
    email, start, end = item
    local = email.split('@', 1)[0]
    if BAD_EMAIL_LOCAL.match(local):
        return -20
    ctx = (text or '')[max(0,start-180):min(len(text or ''),end+180)]
    score = 0
    if APPLY_WORDS.search(ctx): score += 4
    if HR_WORDS.search(local): score += 3
    if HR_WORDS.search(ctx): score += 1
    if re.search(r'cv|resume|резюме|curr[ií]cul', ctx, re.I): score += 2
    return score


def best_email(text):
    emails = extract_emails(text)
    if not emails:
        return None, None
    ranked = sorted(((email_score(text, x), x[0]) for x in emails), reverse=True)
    score, email = ranked[0]
    return (email, score) if score >= 1 else (None, score)


def clean_url(raw):
    raw = (raw or '').rstrip('.,);]}!')
    try:
        p = urlparse(raw)
        if not p.scheme or not p.netloc:
            return None
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path, '', p.query, ''))
    except Exception:
        return None


def url_score(text, raw, start=0, end=0):
    u = clean_url(raw)
    if not u:
        return -99, None
    p = urlparse(u)
    host = p.netloc.lower()
    bare = host[4:] if host.startswith('www.') else host
    if host in SOCIAL_HOSTS or bare in SOCIAL_HOSTS:
        return -99, u
    if host.endswith('.t.me') or bare == 't.me':
        return -99, u
    if bare in {'bit.ly','tinyurl.com','t.co','goo.gl'}:
        return 0, u
    ctx = (text or '')[max(0,start-180):min(len(text or ''),end+180)]
    score = 0
    if any(h in host for h in ATS_HOST_HINTS): score += 6
    if JOB_PATH_HINT.search(p.path + '?' + p.query): score += 5
    if APPLY_WORDS.search(ctx): score += 3
    if re.search(r'apply|career|job|vacan|recruit', u, re.I): score += 2
    if p.path in ('','/') and not APPLY_WORDS.search(ctx): score -= 2
    return score, u


def best_form_url(text):
    ranked = []
    for m in URL_RE.finditer(text or ''):
        score, u = url_score(text, m.group(0), m.start(), m.end())
        if u: ranked.append((score, u))
    if not ranked:
        return None, None
    ranked.sort(reverse=True)
    score, u = ranked[0]
    return (u, score) if score >= 5 else (None, score)


def other_link(text):
    ranked = []
    for m in URL_RE.finditer(text or ''):
        score, u = url_score(text, m.group(0), m.start(), m.end())
        if u and score > -50:
            ranked.append((score, u))
    return sorted(ranked, reverse=True)[0][1] if ranked else None


def direct_domain_from_url(url):
    if not url:
        return None
    host = urlparse(url).netloc.lower().split(':')[0]
    host = host[4:] if host.startswith('www.') else host
    if any(x in host for x in ATS_HOST_HINTS + JOB_BOARD_HOSTS):
        return None
    return host or None


def employer_key(row, email=None, form_url=None):
    company = re.sub(r'[^a-z0-9]+', '', (row['company'] or '').lower()) if 'company' in row.keys() else ''
    if len(company) >= 4:
        return 'company:' + company
    if email:
        domain = email.split('@',1)[1].lower()
        if domain not in {'gmail.com','outlook.com','hotmail.com','yahoo.com','icloud.com','mail.ru','yandex.ru','qq.com'}:
            return 'domain:' + domain
    d = direct_domain_from_url(form_url)
    if d:
        return 'domain:' + d
    # If a Telegram-sent vacancy also contains a corporate email, use its domain for cross-channel de-dupe.
    for e, _, _ in extract_emails(row['raw_text'] or ''):
        domain = e.split('@',1)[1].lower()
        if domain not in {'gmail.com','outlook.com','hotmail.com','yahoo.com','icloud.com','mail.ru','yandex.ru','qq.com'}:
            return 'domain:' + domain
    return None


def cv_variant(country):
    c = (country or '').lower()
    if 'china' in c or '中国' in c:
        return 'china'
    if 'mexico' in c or 'méxico' in c:
        return 'mexico'
    return 'general'


def is_russian(text):
    letters = [ch for ch in (text or '') if ch.isalpha()]
    if not letters:
        return False
    cyr = sum(('а' <= ch.lower() <= 'я') or ch.lower() == 'ё' for ch in letters)
    return cyr >= 30 and cyr / max(1, len(letters)) >= 0.60


def make_email(row, text):
    title = (row['title'] or 'the position').strip()
    if is_russian(text):
        subject = f'Отклик на вакансию — {title}'
        body = (
            f'Здравствуйте! Хочу откликнуться на вакансию {title}. '
            'У меня опыт в операционном и проектном управлении, business development, '
            'логистике, международной координации и развитии процессов. '
            'Прикладываю резюме для рассмотрения. Буду рад обсудить позицию подробнее. Спасибо!'
        )
    else:
        subject = f'Application — {title}'
        body = (
            f'Hello! I would like to apply for the {title} position. '
            'My background includes operations and project management, business development, '
            'logistics, international coordination, and process improvement. '
            'I am attaching my CV for your consideration and would be glad to discuss the role. Thank you.'
        )
    return subject[:220], body


def seed_telegram_history(con):
    rows = con.execute('''
        SELECT q.recipient,q.job_id,q.telegram_message_id,q.processed_at,
               j.company,j.raw_text,j.country,j.title
        FROM send_queue q LEFT JOIN jobs j ON j.job_id=q.job_id
        WHERE q.status='sent'
    ''').fetchall()
    added = 0
    for r in rows:
        if not r['recipient']:
            continue
        key = r['recipient'].strip().lower()
        ek = employer_key(r)
        cur = con.execute('''
            INSERT OR IGNORE INTO outbound_contact_history(
              channel,target,target_key,employer_key,job_id,status,sent_at,external_id,metadata_json
            ) VALUES('telegram',?,?,?,?, 'sent',?,?,?)
        ''', (r['recipient'], key, ek, r['job_id'], r['processed_at'], r['telegram_message_id'], '{}'))
        added += cur.rowcount
    con.commit()
    return added


def history_blocks(con, route_kind, target_key, ek):
    if con.execute("SELECT 1 FROM outbound_contact_history WHERE target_key=? AND status='sent' LIMIT 1", (target_key,)).fetchone():
        return True, 'target_already_contacted'
    if ek and con.execute("SELECT 1 FROM outbound_contact_history WHERE employer_key=? AND status='sent' LIMIT 1", (ek,)).fetchone():
        return True, 'employer_already_contacted'
    if ek and con.execute("SELECT 1 FROM application_routes WHERE employer_key=? AND status IN ('pending','processing','sent') LIMIT 1", (ek,)).fetchone():
        return True, 'employer_already_routed'
    return False, None


def add_route(con, row, kind, target, reason, form_url=None):
    key = norm_email(target) if kind == 'email' else (clean_url(target) or target)
    ek = employer_key(row, email=target if kind == 'email' else None, form_url=form_url if kind == 'form' else None)
    blocked, why = history_blocks(con, kind, key, ek)
    if blocked:
        return False, why
    text = (row['raw_text'] or '') + '\n' + (row['title'] or '')
    subject, message = make_email(row, text)
    payload = {'country': row['country'], 'title': row['title'], 'source': row['source'], 'form_url': form_url}
    try:
        con.execute('''
            INSERT INTO application_routes(
              job_id,job_row_id,route_kind,target,target_key,employer_key,status,reason,
              subject,message,cv_variant,payload_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'pending',?,?,?,?,?,?,?)
        ''', (row['job_id'], row['id'], kind, target, key, ek, reason, subject, message,
              cv_variant(row['country']), json.dumps(payload, ensure_ascii=False), now(), now()))
        con.execute("UPDATE jobs SET selector_status=? WHERE id=?", (kind + '_pending', row['id']))
        return True, None
    except sqlite3.IntegrityError:
        return False, 'route_target_duplicate'


def route_jobs(limit=3000):
    con = db(); ensure_schema(con); seed_telegram_history(con)
    # Recover abandoned claims after two hours.
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    con.execute("UPDATE application_routes SET status='pending',updated_at=? WHERE status='processing' AND updated_at<?", (now(), stale))
    kw = selector_keywords()
    rows = con.execute('''
        SELECT * FROM jobs
        WHERE contact IS NULL OR contact=''
          AND COALESCE(selector_status,'') NOT IN ('queued','blocked_contact','rejected_safety','rejected_salary','email_sent','form_sent')
        ORDER BY id DESC LIMIT ?
    ''', (int(limit),)).fetchall()
    stats = {'checked':0,'email':0,'form':0,'link_review':0,'no_actionable':0,'unsafe':0,'unfit':0,'salary':0,'blocked':0}
    for row in rows:
        # Keep one live route per job.
        if con.execute("SELECT 1 FROM application_routes WHERE job_id=? AND status IN ('pending','processing','sent') LIMIT 1", (row['job_id'],)).fetchone():
            continue
        stats['checked'] += 1
        text = (row['raw_text'] or '') + '\n' + (row['title'] or '')
        cl = classify_post(text)
        if not cl.is_employer_post:
            stats['unsafe'] += 1; continue
        if not fit_ok(text, kw):
            stats['unfit'] += 1; continue
        sr = salary_rejection_reason(text)
        if sr:
            stats['salary'] += 1; continue
        email, escore = best_email(row['raw_text'] or '')
        form, fscore = best_form_url(row['raw_text'] or '')
        # Explicit recruitment email wins when it is clearly an application address.
        if email and escore is not None and escore >= 4:
            ok, why = add_route(con, row, 'email', email, f'email_score:{escore}')
            stats['email' if ok else 'blocked'] += 1
        elif form:
            ok, why = add_route(con, row, 'form', form, f'form_score:{fscore}', form_url=form)
            stats['form' if ok else 'blocked'] += 1
        elif email:
            ok, why = add_route(con, row, 'email', email, f'email_score:{escore}')
            stats['email' if ok else 'blocked'] += 1
        else:
            link = other_link(row['raw_text'] or '')
            if link:
                con.execute("UPDATE jobs SET selector_status='link_review' WHERE id=?", (row['id'],))
                stats['link_review'] += 1
            else:
                con.execute("UPDATE jobs SET selector_status='no_actionable_contact' WHERE id=?", (row['id'],))
                stats['no_actionable'] += 1
    con.commit()
    q = {r[0]:r[1] for r in con.execute("SELECT route_kind||':'||status,count(*) FROM application_routes GROUP BY route_kind,status")}
    print(json.dumps({'route_stats':stats,'queue':q}, ensure_ascii=False))
    con.close()


def claim(kind, n):
    con=db(); ensure_schema(con)
    con.execute('BEGIN IMMEDIATE')
    rows=con.execute("SELECT * FROM application_routes WHERE route_kind=? AND status='pending' ORDER BY id ASC LIMIT ?", (kind,int(n))).fetchall()
    ids=[r['id'] for r in rows]
    if ids:
        marks=','.join('?' for _ in ids)
        con.execute(f"UPDATE application_routes SET status='processing',updated_at=? WHERE id IN ({marks})", (now(),*ids))
    con.commit()
    out=[]
    for r in rows:
        d=dict(r)
        try: d['payload']=json.loads(d.get('payload_json') or '{}')
        except Exception: d['payload']={}
        out.append(d)
    print(json.dumps(out,ensure_ascii=False))
    con.close()


def mark(route_id, status, external_id=None, note=None):
    con=db(); ensure_schema(con)
    row=con.execute("SELECT * FROM application_routes WHERE id=?",(int(route_id),)).fetchone()
    if not row:
        raise SystemExit('route_not_found')
    processed = now() if status in ('sent','failed','skipped') else None
    con.execute("UPDATE application_routes SET status=?,updated_at=?,processed_at=?,external_id=?,reason=COALESCE(?,reason) WHERE id=?",
                (status,now(),processed,external_id,note,int(route_id)))
    if status == 'sent':
        con.execute('''INSERT OR REPLACE INTO outbound_contact_history(
            channel,target,target_key,employer_key,job_id,status,sent_at,external_id,metadata_json
        ) VALUES(?,?,?,?,?,'sent',?,?,?)''',
        (row['route_kind'],row['target'],row['target_key'],row['employer_key'],row['job_id'],now(),external_id,'{}'))
        con.execute("UPDATE jobs SET status='Applied',selector_status=? WHERE job_id=?", (row['route_kind']+'_sent',row['job_id']))
    con.commit(); print('OK',route_id,status); con.close()


def stats():
    con=db(); ensure_schema(con)
    print(json.dumps({
        'routes':[(r[0],r[1],r[2]) for r in con.execute('SELECT route_kind,status,count(*) FROM application_routes GROUP BY route_kind,status ORDER BY 1,2')],
        'history':[(r[0],r[1]) for r in con.execute('SELECT channel,count(*) FROM outbound_contact_history WHERE status=\'sent\' GROUP BY channel')]
    },ensure_ascii=False)); con.close()


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--claim-email',type=int)
    ap.add_argument('--claim-form',type=int)
    ap.add_argument('--mark',type=int)
    ap.add_argument('--status')
    ap.add_argument('--external-id')
    ap.add_argument('--note')
    ap.add_argument('--stats',action='store_true')
    ap.add_argument('--limit',type=int,default=3000)
    args=ap.parse_args()
    if args.claim_email is not None: return claim('email',args.claim_email)
    if args.claim_form is not None: return claim('form',args.claim_form)
    if args.mark is not None: return mark(args.mark,args.status or 'failed',args.external_id,args.note)
    if args.stats: return stats()
    return route_jobs(args.limit)

if __name__=='__main__':
    main()
