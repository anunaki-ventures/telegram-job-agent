import re
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from candidate_classifier import classify_post
from salary_policy import salary_rejection_reason
from telegram_contact import extract_telegram_contact

ROOT = Path("/opt/tg-job-agent")
DB = ROOT / "telegram_jobs.db"
CV = str(ROOT / "cv" / "Ruslans_Strakis_CV.pdf")

COUNTRY_TZ = {
    "indonesia": "Asia/Makassar",
    "bali": "Asia/Makassar",
    "thailand": "Asia/Bangkok",
    "maldives": "Indian/Maldives",
    "seychelles": "Indian/Mahe",
    "mauritius": "Indian/Mauritius",
    "sri lanka": "Asia/Colombo",
    "vietnam": "Asia/Ho_Chi_Minh",
    "malaysia": "Asia/Kuala_Lumpur",
    "philippines": "Asia/Manila",
    "china": "Asia/Shanghai",
    "japan": "Asia/Tokyo",
    "south korea": "Asia/Seoul",
    "korea": "Asia/Seoul",
    "nepal": "Asia/Kathmandu",
    "portugal": "Europe/Lisbon",
    "spain": "Europe/Madrid",
    "malta": "Europe/Malta",
    "italy": "Europe/Rome",
    "cyprus": "Asia/Nicosia",
    "greece": "Europe/Athens",
    "uae": "Asia/Dubai",
    "united arab emirates": "Asia/Dubai",
    "dubai": "Asia/Dubai",
    "abu dhabi": "Asia/Dubai",
    "saudi arabia": "Asia/Riyadh",
    "qatar": "Asia/Qatar",
    "bahrain": "Asia/Bahrain",
    "oman": "Asia/Muscat",
    "kuwait": "Asia/Kuwait",
    "mexico": "America/Mexico_City",
    "colombia": "America/Bogota",
    "brazil": "America/Sao_Paulo",
    "argentina": "America/Argentina/Buenos_Aires",
    "chile": "America/Santiago",
    "south africa": "Africa/Johannesburg",
    "netherlands": "Europe/Amsterdam",
    "germany": "Europe/Berlin",
    "belgium": "Europe/Brussels",
    "france": "Europe/Paris",
    "ireland": "Europe/Dublin",
    "uk": "Europe/London",
    "united kingdom": "Europe/London",
    "singapore": "Asia/Singapore",
    "cambodia": "Asia/Phnom_Penh"
}

PRIORITY_COUNTRIES = {
    "indonesia","thailand","vietnam","malaysia","philippines","singapore","cambodia",
    "laos","myanmar","brunei","timor-leste","east timor",
    "china","japan","south korea","korea",
    "mexico","colombia","brazil","argentina","chile","peru","ecuador","uruguay",
    "paraguay","bolivia","costa rica","panama","guatemala","dominican republic",
    "spain","portugal"
}
EUROPE_COUNTRIES = {
    "netherlands","germany","belgium","france","ireland","uk","united kingdom",
    "italy","malta","cyprus","greece","austria","switzerland","poland","czechia",
    "czech republic","slovakia","slovenia","croatia","hungary","romania","bulgaria",
    "denmark","sweden","norway","finland","estonia","latvia","lithuania","luxembourg"
}

GOOD = [
    "general manager","operations manager","operation manager","project manager","program manager",
    "property manager","construction manager","development manager","business development manager",
    "business development","sales manager","account manager","customer success","procurement manager",
    "purchasing manager","supply chain","logistics manager","warehouse manager","production manager",
    "e-commerce manager","ecommerce manager","marketplace manager","partnerships manager","expansion manager",
    "branch manager","country manager","regional manager","area manager","hotel manager","resort manager",
    "restaurant manager","f&b manager","office manager","service manager","community manager",
    "gerente general","gerente de operaciones","gerente de proyectos","jefe de proyecto","desarrollo de negocios",
    "gerente comercial","gerente de ventas","gerente de cuentas","éxito del cliente","logística","compras",
    "cadena de suministro","gerente de almacén","gerente de producción","comercio electrónico","alianzas","expansión",
    "gerente geral","gerente de operações","gerente de projetos","desenvolvimento de negócios","gerente de vendas",
    "gerente de contas","sucesso do cliente","cadeia de suprimentos","gerente de armazém","gerente de produção","parcerias",
    "manajer proyek","manajer operasional","manajer penjualan","pengembangan bisnis","pengadaan","rantai pasok",
    "quản lý dự án","quản lý vận hành","phát triển kinh doanh","quản lý bán hàng","quản lý kho","chuỗi cung ứng",
    "项目经理","运营经理","业务发展","销售经理","采购经理","供应链","物流经理","总经理",
    "プロジェクトマネージャー","オペレーションマネージャー","営業マネージャー","事業開発","物流","調達","サプライチェーン",
    "프로젝트 매니저","운영 매니저","영업 매니저","사업개발","물류","구매","공급망",
    "ผู้จัดการโครงการ","ผู้จัดการฝ่ายปฏิบัติการ","ผู้จัดการฝ่ายขาย","พัฒนาธุรกิจ","โลจิสติกส์","จัดซื้อ","ซัพพลายเชน",
    "генеральный менеджер","операционный менеджер","руководитель проекта","менеджер проекта","развитие бизнеса",
    "менеджер по продажам","аккаунт-менеджер","логистика","закупки","цепочка поставок"
]

BAD = [
    "internship","intern ","unpaid","volunteer",
    "стажировка","без оплаты",
    "citizens only","citizen only","nationals only",
    "local candidates only","local candidate only"
]

HARD_TECH = [
    "senior software engineer","developer","devops","data scientist",
    "machine learning engineer","cybersecurity engineer",
    "java developer","python developer","frontend developer",
    "backend developer","full stack developer",
    "operations research engineer","optimization engineer","mathematical optimization",
    "milp","constraint programming","or-tools","cp-sat","gurobi","cplex"
]

def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con

def norm_contact(v):
    if not v:
        return ""
    v = v.strip()
    if not v.startswith("@"):
        v = "@" + v
    return v.lower()

def already_blocked(con, contact):
    c = norm_contact(contact)

    row = con.execute("""
        SELECT 1 FROM contacts
        WHERE lower(contact)=?
        LIMIT 1
    """,(c,)).fetchone()
    if row:
        return True

    row = con.execute("""
        SELECT 1 FROM applications
        WHERE lower(recipient)=?
        LIMIT 1
    """,(c,)).fetchone()
    if row:
        return True

    row = con.execute("""
        SELECT 1 FROM send_queue
        WHERE lower(recipient)=?
          AND status IN ('pending','sent','failed','skipped','held_floodwait','held_time')
        LIMIT 1
    """,(c,)).fetchone()
    return bool(row)

def infer_country(row):
    parts = [
        row["country"] or "",
        row["city"] or "",
        row["source_country"] or "",
        row["raw_text"] or "",
        row["title"] or ""
    ]
    text = " ".join(parts).lower()

    aliases = [
        ("united arab emirates","uae"),
        ("dubai","uae"),
        ("abu dhabi","uae"),
        ("bali","indonesia"),
        ("phuket","thailand"),
        ("bangkok","thailand"),
        ("limassol","cyprus"),
        ("nicosia","cyprus"),
        ("bogota","colombia"),
        ("medellin","colombia"),
        ("manila","philippines"),
        ("kuala lumpur","malaysia"),
        ("ho chi minh","vietnam"),
        ("hanoi","vietnam"),
        ("shanghai","china"),
        ("beijing","china"),
        ("shenzhen","china"),
        ("guangzhou","china")
    ]

    for needle, country in aliases:
        if needle in text:
            return country

    for country in COUNTRY_TZ:
        if country in text:
            return country

    return None

def region_priority(row):
    country = (infer_country(row) or '').strip().lower()
    if any(x in country for x in PRIORITY_COUNTRIES):
        return 0
    if any(x in country for x in EUROPE_COUNTRIES):
        return 2
    return 1

def global_remote_without_specific_country(row):
    label = ' '.join([str(row['country'] or ''), str(row['source_country'] or '')]).lower()
    broad = any(x in label for x in ('global', 'worldwide', 'remote', 'latam'))
    if not broad:
        return False
    specific = any(country in label for country in COUNTRY_TZ.keys())
    return not specific

def allowed_now(country, source_timezone=None):
    tzname = (source_timezone or '').strip()
    if not tzname and country:
        tzname = COUNTRY_TZ.get((country or '').strip().lower())
    # Unknown timezone must NOT globally block the pipeline.
    # Known employer timezone still respects 07:30-19:00 local time.
    if not tzname:
        local = datetime.now(timezone.utc)
        return True, '', local.strftime("%Y-%m-%d %H:%M")
    try:
        local = datetime.now(timezone.utc).astimezone(ZoneInfo(tzname))
    except Exception:
        local = datetime.now(timezone.utc)
        return True, tzname, local.strftime("%Y-%m-%d %H:%M")
    mins = local.hour * 60 + local.minute
    allowed = (7 * 60 + 30) <= mins < (19 * 60)
    return allowed, tzname, local.strftime("%Y-%m-%d %H:%M")

def fit(text):
    t = text.lower()

    if any(x in t for x in BAD):
        return False

    if any(x in t for x in HARD_TECH):
        return False

    return any(x in t for x in GOOD)

def salary_too_low(text):
    return salary_rejection_reason(text) is not None

def language(text):
    cyr = sum(1 for ch in text if "а" <= ch.lower() <= "я")
    lat = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    return "ru" if cyr >= 20 and cyr > lat * 1.5 else "en"

def make_message(row):
    title = (row["title"] or "the position").strip()
    company = (row["company"] or "").strip()
    text = row["raw_text"] or ""

    if language(text) == "ru":
        company_part = f" в {company}" if company else ""
        return (
            f"Здравствуйте! Увидел вакансию {title}{company_part}. "
            f"Хотел бы предложить свою кандидатуру. У меня опыт в операционном и проектном управлении, "
            f"развитии бизнеса, логистике, международной работе и запуске/координации процессов. "
            f"Прикладываю CV для рассмотрения. Спасибо!"
        )

    company_part = f" at {company}" if company else ""
    return (
        f"Hello! I saw the {title} vacancy{company_part} and would like to apply. "
        f"My background includes operations and project management, business development, logistics, "
        f"international coordination, and process launch/improvement. "
        f"I am attaching my CV for your consideration. Thank you."
    )

con = db()

con.executescript("""
ALTER TABLE jobs ADD COLUMN selector_status TEXT;
""") if False else None

cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}

if "selector_status" not in cols:
    con.execute("ALTER TABLE jobs ADD COLUMN selector_status TEXT")
if "timezone" not in cols:
    con.execute("ALTER TABLE jobs ADD COLUMN timezone TEXT")

rows = con.execute("""
SELECT
    j.*,
    s.country AS source_country,
    s.timezone AS source_timezone
FROM jobs j
LEFT JOIN sources s
  ON lower(COALESCE(j.source,'')) =
     lower(COALESCE(s.username,s.name,''))
WHERE j.status='new'
  AND COALESCE(j.selector_status,'') IN ('','held_time')
ORDER BY j.id ASC
LIMIT 1000
""").fetchall()

# Founder priority: SEA + China/Japan/Korea + LATAM + Spain/Portugal first;
# other non-European jobs next; the rest of Europe last.
def selection_window_priority(row):
    country = infer_country(row)
    ok, _, _ = allowed_now(country, row['source_timezone'])
    return 0 if ok else 1

rows = sorted(rows, key=lambda r: (selection_window_priority(r), region_priority(r), r['id']))[:250]

queued = 0
held_time = 0
rejected = 0
blocked = 0
no_contact = 0  # selector-starvation-fix

for row in rows:
    text = (row["raw_text"] or "") + "\n" + (row["title"] or "")
    contact = row["contact"] or extract_telegram_contact(row["raw_text"] or "", row["source"] or "")
    if contact and not row["contact"]:
        con.execute("UPDATE jobs SET contact=? WHERE id=?", (contact, row["id"]))

    # Level 2 safety: selector requires positive employer/recruiter hiring intent.
    classification = classify_post(text)
    if not classification.is_employer_post:
        con.execute(
            "UPDATE jobs SET selector_status='rejected_safety' WHERE id=?",
            (row["id"],)
        )
        rejected += 1
        continue

    if not contact:
        con.execute("UPDATE jobs SET selector_status='no_telegram_contact' WHERE id=?", (row['id'],))
        no_contact += 1
        continue

    if already_blocked(con, contact):
        con.execute(
            "UPDATE jobs SET selector_status='blocked_contact' WHERE id=?",
            (row["id"],)
        )
        blocked += 1
        continue

    if not fit(text) or salary_too_low(text):
        con.execute(
            "UPDATE jobs SET selector_status='rejected' WHERE id=?",
            (row["id"],)
        )
        rejected += 1
        continue

    country = infer_country(row)

    if country and not row["country"]:
        con.execute(
            "UPDATE jobs SET country=? WHERE id=?",
            (country,row["id"])
        )

    if global_remote_without_specific_country(row):
        ok, tz, localtime = True, '', 'global-remote'
    else:
        ok, tz, localtime = allowed_now(country, row["source_timezone"])

    if tz:
        con.execute(
            "UPDATE jobs SET timezone=? WHERE id=?",
            (tz,row["id"])
        )

    if not ok:
        con.execute(
            "UPDATE jobs SET selector_status='held_time' WHERE id=?",
            (row["id"],)
        )
        held_time += 1
        continue

    msg = make_message(row)

    con.execute("""
    INSERT INTO send_queue(
        command_id,
        recipient,
        message,
        cv_path,
        job_id,
        status
    )
    VALUES(?,?,?,?,?,'pending')
    """,(
        f"job-{row['id']}-{norm_contact(contact).replace('@','')}",
        contact,
        msg,
        CV,
        row["job_id"]
    ))

    con.execute(
        "UPDATE jobs SET selector_status='queued', status='queued' WHERE id=?",
        (row["id"],)
    )

    queued += 1

con.commit()

print("checked:",len(rows))
print("queued:",queued)
print("held_time:",held_time)
print("rejected:",rejected)
print("blocked_contact:",blocked)
print("no_telegram_contact:",no_contact)
print("pending_queue:",con.execute(
    "SELECT COUNT(*) FROM send_queue WHERE status='pending'"
).fetchone()[0])
print("integrity:",con.execute("PRAGMA integrity_check").fetchone()[0])

con.close()

# MULTICHANNEL_ROUTER_AFTER_SELECTOR_V1
try:
    import subprocess as _mcr_subprocess, sys as _mcr_sys
    _mcr_subprocess.run([_mcr_sys.executable, "/opt/tg-job-agent/channel_router.py"], timeout=120, check=False)
except Exception as _mcr_e:
    print("MULTICHANNEL_ROUTER_ERROR", repr(_mcr_e))
