from pathlib import Path

p = Path('/opt/tg-job-agent/discover_sources.py')
t = p.read_text()

if 'CONTACT_QUERY_BANK = [' not in t:
    anchor = "QUERIES_PER_RUN = 14\n"
    block = """CONTACT_QUERY_BANK = [
    'remote jobs Telegram recruiter', 'hiring Telegram contact', 'send CV Telegram', 'apply Telegram recruiter',
    'Mexico empleos Telegram', 'Mexico vacantes Telegram', 'Colombia empleos Telegram', 'Bogota empleo Telegram',
    'Medellin empleo Telegram', 'Argentina empleos Telegram', 'Buenos Aires empleo Telegram',
    'Chile empleos Telegram', 'Peru empleos Telegram', 'Brasil vagas Telegram', 'São Paulo vagas Telegram',
    'Rio vagas Telegram', 'LATAM remote Telegram', 'project manager Telegram recruiter',
    'operations manager Telegram recruiter', 'business development Telegram recruiter',
    'sales manager Telegram recruiter', 'logistics manager Telegram recruiter',
    'Indonesia jobs Telegram', 'Thailand jobs Telegram', 'Dubai jobs Telegram', 'Cyprus jobs Telegram'
]

QUERIES_PER_RUN = 14
GENERAL_QUERIES_PER_RUN = 10
CONTACT_QUERIES_PER_RUN = 4
"""
    if anchor not in t:
        raise SystemExit('ANCHOR_MISSING discovery query constants')
    t = t.replace(anchor, block, 1)

old = """def next_queries():
    cursor = int(get_state('query_cursor', '0')) % len(QUERY_BANK)
    out = [QUERY_BANK[(cursor+i) % len(QUERY_BANK)] for i in range(QUERIES_PER_RUN)]
    set_state('query_cursor', (cursor + QUERIES_PER_RUN) % len(QUERY_BANK))
    return out
"""
new = """def next_queries():
    cursor = int(get_state('query_cursor', '0')) % len(QUERY_BANK)
    general = [QUERY_BANK[(cursor+i) % len(QUERY_BANK)] for i in range(GENERAL_QUERIES_PER_RUN)]
    set_state('query_cursor', (cursor + GENERAL_QUERIES_PER_RUN) % len(QUERY_BANK))

    contact_cursor = int(get_state('contact_query_cursor', '0')) % len(CONTACT_QUERY_BANK)
    contact = [CONTACT_QUERY_BANK[(contact_cursor+i) % len(CONTACT_QUERY_BANK)] for i in range(CONTACT_QUERIES_PER_RUN)]
    set_state('contact_query_cursor', (contact_cursor + CONTACT_QUERIES_PER_RUN) % len(CONTACT_QUERY_BANK))
    return general + contact
"""
if 'contact_query_cursor' not in t:
    if old not in t:
        raise SystemExit('ANCHOR_MISSING discovery next_queries')
    t = t.replace(old, new, 1)

p.write_text(t)
print('CONTACT_FOCUSED_DISCOVERY_PATCHED')
