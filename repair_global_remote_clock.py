from pathlib import Path

ROOT = Path('/opt/tg-job-agent')


def replace_once(name, old, new):
    p = ROOT / name
    text = p.read_text()
    if old not in text:
        raise SystemExit(f'ANCHOR_MISSING {name}: {old[:100]!r}')
    p.write_text(text.replace(old, new, 1))
    print('PATCHED', name)

# Selector: truly global/remote jobs should not inherit an arbitrary source timezone.
anchor = "def allowed_now(country, source_timezone=None):\n"
helper = "def global_remote_without_specific_country(row):\n    label = ' '.join([str(row['country'] or ''), str(row['source_country'] or '')]).lower()\n    broad = any(x in label for x in ('global', 'worldwide', 'remote', 'latam'))\n    if not broad:\n        return False\n    specific = any(country in label for country in COUNTRY_TZ.keys())\n    return not specific\n\ndef allowed_now(country, source_timezone=None):\n"
replace_once('selector.py', anchor, helper)

old = "    ok, tz, localtime = allowed_now(country, row[\"source_timezone\"])\n"
new = "    if global_remote_without_specific_country(row):\n        ok, tz, localtime = True, '', 'global-remote'\n    else:\n        ok, tz, localtime = allowed_now(country, row[\"source_timezone\"])\n"
replace_once('selector.py', old, new)

# Worker: same final-gate rule, so queued global/remote jobs are never re-held by a bogus timezone.
old = "def outbound_time_allowed(con, row):\n    job = con.execute('SELECT timezone FROM jobs WHERE job_id=? LIMIT 1', (row['job_id'],)).fetchone()\n    tzname = (job['timezone'] or '').strip() if job else ''\n"
new = "def outbound_time_allowed(con, row):\n    job = con.execute('SELECT timezone,country FROM jobs WHERE job_id=? LIMIT 1', (row['job_id'],)).fetchone()\n    label = (job['country'] or '').strip().lower() if job else ''\n    broad = any(x in label for x in ('global', 'worldwide', 'remote', 'latam'))\n    specific_needles = (\n        'indonesia','thailand','maldives','seychelles','mauritius','sri lanka','vietnam','malaysia',\n        'philippines','china','japan','south korea','korea','nepal','portugal','spain','malta','italy',\n        'cyprus','greece','uae','dubai','saudi arabia','qatar','bahrain','oman','kuwait','mexico',\n        'colombia','brazil','argentina','chile','south africa','netherlands','germany','belgium','france',\n        'ireland','united kingdom','singapore','cambodia'\n    )\n    if broad and not any(x in label for x in specific_needles):\n        return True, None\n    tzname = (job['timezone'] or '').strip() if job else ''\n"
replace_once('worker.py', old, new)

print('GLOBAL_REMOTE_CLOCK_PATCH_COMPLETE')
