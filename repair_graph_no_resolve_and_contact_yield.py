from pathlib import Path

ROOT = Path('/opt/tg-job-agent')


def replace_once(name, old, new, marker):
    p = ROOT / name
    text = p.read_text()
    if marker in text:
        print('ALREADY_PATCHED', name, marker)
        return
    if old not in text:
        raise SystemExit(f'ANCHOR_MISSING {name}: {old[:120]!r}')
    p.write_text(text.replace(old, new, 1))
    print('PATCHED', name, marker)


# Discovery graph: construct InputPeer directly from the local Telethon session DB.
replace_once(
    'discover_sources.py',
    'from telethon import TelegramClient\n',
    'from telethon import TelegramClient, utils\nfrom telethon.tl import types\n',
    'from telethon import TelegramClient, utils',
)

p = ROOT / 'discover_sources.py'
t = p.read_text()
if 'def discovery_cached_input_peer(' not in t:
    anchor = "def extract_refs(text):\n    found = {m.group(1) for m in TG_LINK_RE.finditer(text or '')}\n    found.update(m.group(1) for m in HANDLE_RE.finditer(text or ''))\n    return found\n\n\n"
    helper = """def extract_refs(text):
    found = {m.group(1) for m in TG_LINK_RE.finditer(text or '')}
    found.update(m.group(1) for m in HANDLE_RE.finditer(text or ''))
    return found


def discovery_cached_input_peer(username):
    if not username or not SESSION_DB.exists():
        return None
    con = sqlite3.connect(SESSION_DB, timeout=5)
    try:
        row = con.execute(
            "SELECT id,hash FROM entities WHERE lower(COALESCE(username,''))=lower(?) LIMIT 1",
            (username,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    marked_id = int(row[0])
    access_hash = int(row[1] or 0)
    real_id, peer_type = utils.resolve_id(marked_id)
    if peer_type is types.PeerChannel:
        return types.InputPeerChannel(real_id, access_hash)
    if peer_type is types.PeerUser:
        return types.InputPeerUser(real_id, access_hash)
    if peer_type is types.PeerChat:
        return types.InputPeerChat(real_id)
    return None


"""
    if anchor not in t:
        raise SystemExit('ANCHOR_MISSING discover_sources.py extract_refs')
    t = t.replace(anchor, helper, 1)

old = """        try:
            entity = await client.get_entity(username)
            if isinstance(entity, Channel) and not (getattr(entity, 'broadcast', False) or getattr(entity, 'megagroup', False)):
                continue
            if not isinstance(entity, (Channel, Chat)):
                continue
            sources_read += 1
            evidence_parts = [getattr(entity, 'title', '') or '']
            async for msg in client.iter_messages(entity, limit=GRAPH_MESSAGES_PER_SOURCE):
"""
new = """        try:
            # Never resolve usernames in graph discovery. The InputPeer comes directly from the local session cache.
            entity = discovery_cached_input_peer(username)
            if entity is None:
                continue
            sources_read += 1
            evidence_parts = [row['name'] or username]
            async for msg in client.iter_messages(entity, limit=GRAPH_MESSAGES_PER_SOURCE):
"""
if 'Never resolve usernames in graph discovery' not in t:
    if old not in t:
        raise SystemExit('ANCHOR_MISSING discover_sources.py graph entity block')
    t = t.replace(old, new, 1)

p.write_text(t)
print('PATCHED discover_sources.py graph_no_username_resolve')

# Scanner: prioritize sources that historically yield direct Telegram contacts, then explore never-scanned sources.
p = ROOT / 'scanner.py'
t = p.read_text()
if 'def source_contact_rank(' not in t:
    anchor = """    open_pool = sum(1 for s in eligible if source_window_open(s))
    sources = sorted(
        eligible,
        key=lambda s: (
            0 if source_window_open(s) else 1,
            source_priority(s['country']),
            0 if s['last_scanned_at'] is None else 1,
            s['last_scanned_at'] or '',
            s['id']
        )
    )[:120]
"""
    replacement = """    open_pool = sum(1 for s in eligible if source_window_open(s))

    stats_con = db()
    try:
        contact_stats = {
            (r['source_key'] or ''): (int(r['contact_jobs'] or 0), int(r['total_jobs'] or 0))
            for r in stats_con.execute(
                "SELECT lower(COALESCE(source,'')) AS source_key, "
                "SUM(CASE WHEN contact IS NOT NULL AND contact!='' THEN 1 ELSE 0 END) AS contact_jobs, "
                "COUNT(*) AS total_jobs FROM jobs GROUP BY lower(COALESCE(source,''))"
            )
        }
    finally:
        stats_con.close()

    def source_contact_yield(source):
        key = (normalize_username(source['username']) or normalize_username(source['telegram_url']) or '').lower()
        hits, total = contact_stats.get(key, (0, 0))
        return (hits / total) if total else 0.0

    def source_contact_rank(source):
        if source_contact_yield(source) > 0:
            return 0  # proven direct-contact source
        if source['last_scanned_at'] is None:
            return 1  # exploration
        return 2      # scanned but has not yielded a Telegram contact yet

    sources = sorted(
        eligible,
        key=lambda s: (
            0 if source_window_open(s) else 1,
            source_priority(s['country']),
            source_contact_rank(s),
            -source_contact_yield(s),
            s['last_scanned_at'] or '',
            s['id']
        )
    )[:120]
"""
    if anchor not in t:
        raise SystemExit('ANCHOR_MISSING scanner.py global sort')
    t = t.replace(anchor, replacement, 1)
    p.write_text(t)
    print('PATCHED scanner.py contact_yield_routing')

print('GRAPH_NO_RESOLVE_AND_CONTACT_YIELD_COMPLETE')
