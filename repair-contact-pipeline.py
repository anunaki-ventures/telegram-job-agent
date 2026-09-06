from pathlib import Path

ROOT = Path('/opt/tg-job-agent')
scanner = ROOT / 'scanner.py'
selector = ROOT / 'selector.py'


def replace_once(text, old, new, name):
    if old not in text:
        raise RuntimeError(f'{name}: expected block not found')
    return text.replace(old, new, 1)


# Patch scanner: import shared extractor and populate contact from explicit t.me application routes.
s = scanner.read_text()
if 'from telegram_contact import extract_telegram_contact' not in s:
    s = replace_once(
        s,
        'from candidate_classifier import classify_post\n',
        'from candidate_classifier import classify_post\nfrom telegram_contact import extract_telegram_contact\n',
        'scanner import',
    )
s = s.replace("CONTACT_RE = re.compile('(?<![\\\\w])@([A-Za-z0-9_]{5,32})')\n", '')
s = replace_once(
    s,
    "            contacts = ['@' + x for x in CONTACT_RE.findall(text)]\n            contact = contacts[0] if contacts else None\n",
    "            contact = extract_telegram_contact(text, username)\n",
    'scanner contact extraction',
)
scanner.write_text(s)

# Patch selector: re-extract Telegram contact for legacy/new rows whose scanner left contact empty.
t = selector.read_text()
if 'from telegram_contact import extract_telegram_contact' not in t:
    t = replace_once(
        t,
        'from candidate_classifier import classify_post\n',
        'from candidate_classifier import classify_post\nfrom telegram_contact import extract_telegram_contact\n',
        'selector import',
    )
t = replace_once(
    t,
    "WHERE j.status='new'\n  AND j.contact IS NOT NULL\n  AND j.contact!=''\n  AND COALESCE(j.selector_status,'') IN ('','held_time')\n",
    "WHERE j.status='new'\n  AND COALESCE(j.selector_status,'') IN ('','held_time')\n",
    'selector SQL contact gate',
)
t = replace_once(
    t,
    "for row in rows:\n    text = (row[\"raw_text\"] or \"\") + \"\\n\" + (row[\"title\"] or \"\")\n\n    # Level 2 safety: selector requires positive employer/recruiter hiring intent.\n",
    "for row in rows:\n    text = (row[\"raw_text\"] or \"\") + \"\\n\" + (row[\"title\"] or \"\")\n    contact = row[\"contact\"] or extract_telegram_contact(row[\"raw_text\"] or \"\", row[\"source\"] or \"\")\n    if contact and not row[\"contact\"]:\n        con.execute(\"UPDATE jobs SET contact=? WHERE id=?\", (contact, row[\"id\"]))\n\n    # Level 2 safety: selector requires positive employer/recruiter hiring intent.\n",
    'selector contact recovery',
)
t = replace_once(
    t,
    "    if already_blocked(con,row[\"contact\"]):\n",
    "    if not contact:\n        continue\n\n    if already_blocked(con, contact):\n",
    'selector contact required',
)
t = replace_once(
    t,
    "        f\"job-{row['id']}-{norm_contact(row['contact']).replace('@','')}\",\n        row[\"contact\"],\n",
    "        f\"job-{row['id']}-{norm_contact(contact).replace('@','')}\",\n        contact,\n",
    'selector queue contact',
)
selector.write_text(t)

print('CONTACT_PIPELINE_PATCHED')
