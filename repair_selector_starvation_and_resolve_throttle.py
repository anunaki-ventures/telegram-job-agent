from pathlib import Path

ROOT = Path('/opt/tg-job-agent')


def patch_once(name: str, old: str, new: str, marker: str):
    p = ROOT / name
    text = p.read_text()
    if marker in text:
        print('ALREADY_PATCHED', name, marker)
        return
    if old not in text:
        raise SystemExit(f'ANCHOR_MISSING {name}: {old[:120]!r}')
    p.write_text(text.replace(old, new, 1))
    print('PATCHED', name, marker)


# 1) Selector must not revisit the same contactless jobs forever.
patch_once(
    'selector.py',
    "queued = 0\nheld_time = 0\nrejected = 0\nblocked = 0\n",
    "queued = 0\nheld_time = 0\nrejected = 0\nblocked = 0\nno_contact = 0  # selector-starvation-fix\n",
    'selector-starvation-fix',
)

p = ROOT / 'selector.py'
t = p.read_text()
if "selector_status='no_telegram_contact'" not in t:
    old = "    if not contact:\n        continue\n"
    new = (
        "    if not contact:\n"
        "        con.execute(\"UPDATE jobs SET selector_status='no_telegram_contact' WHERE id=?\", (row['id'],))\n"
        "        no_contact += 1\n"
        "        continue\n"
    )
    if old not in t:
        raise SystemExit('ANCHOR_MISSING selector.py contactless continue')
    t = t.replace(old, new, 1)
if 'print("no_telegram_contact:",no_contact)' not in t:
    old = 'print("blocked_contact:",blocked)\n'
    new = old + 'print("no_telegram_contact:",no_contact)\n'
    if old not in t:
        raise SystemExit('ANCHOR_MISSING selector.py print block')
    t = t.replace(old, new, 1)
p.write_text(t)
print('PATCHED selector.py no_telegram_contact_status')

# Reject optimization/OR engineering roles that can otherwise match generic supply-chain words.
p = ROOT / 'selector.py'
t = p.read_text()
if 'operations research engineer' not in t:
    old = '    "backend developer","full stack developer"\n]\n'
    new = (
        '    "backend developer","full stack developer",\n'
        '    "operations research engineer","optimization engineer","mathematical optimization",\n'
        '    "milp","constraint programming","or-tools","cp-sat","gurobi","cplex"\n'
        ']\n'
    )
    if old not in t:
        raise SystemExit('ANCHOR_MISSING selector.py HARD_TECH')
    t = t.replace(old, new, 1)
    p.write_text(t)
    print('PATCHED selector.py technical exclusions')

# 2) Worker may resolve at most one unknown username per hour. Cached recipients are unaffected.
p = ROOT / 'worker.py'
t = p.read_text()
if 'RESOLVE_ATTEMPT_INTERVAL_SECONDS = 3600' not in t:
    old = 'FLOODWAIT_MARGIN_SECONDS = 300\n'
    new = old + 'RESOLVE_ATTEMPT_INTERVAL_SECONDS = 3600\n'
    if old not in t:
        raise SystemExit('ANCHOR_MISSING worker.py constants')
    t = t.replace(old, new, 1)

old_block = """        entity = cached_input_peer(recipient)
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
"""
new_block = """        entity = cached_input_peer(recipient)
        if entity is None:
            remaining = resolve_wait_remaining()
            if remaining > 0:
                retry_at = datetime.fromtimestamp(resolve_block_until(), timezone.utc).isoformat()
                mark_held(con, row['id'], 'held_floodwait', f'resolve_cooldown:{remaining}s', retry_at)
                print('HOLD_RESOLVE_COOLDOWN', recipient, remaining)
                return False

            now_epoch = datetime.now(timezone.utc).timestamp()
            last_attempt = get_state_float('last_resolve_attempt_at', 0.0)
            next_attempt = last_attempt + RESOLVE_ATTEMPT_INTERVAL_SECONDS
            if next_attempt > now_epoch:
                retry_at = datetime.fromtimestamp(next_attempt, timezone.utc).isoformat()
                mark_held(con, row['id'], 'held_floodwait', 'resolve_rate_limit', retry_at)
                print('HOLD_RESOLVE_RATE_LIMIT', recipient, int(next_attempt - now_epoch))
                return False

            # One unknown-username resolution attempt per hour maximum. This is separate from message pacing.
            set_state('last_resolve_attempt_at', now_epoch)
            try:
                entity = await client.get_input_entity(recipient)
            except FloodWaitError as e:
                seconds = int(getattr(e, 'seconds', 3600) or 3600)
                retry_at = mark_floodwait(con, row['id'], seconds, f'ResolveUsernameRequest FloodWait {seconds}s')
                print('HOLD_FLOODWAIT', recipient, seconds, retry_at)
                return False
            except Exception as e:
                retry_at = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
                mark_held(con, row['id'], 'held_floodwait', f'resolve_retry:{type(e).__name__}', retry_at)
                print('HOLD_RESOLVE_RETRY', recipient, type(e).__name__)
                return False
"""
if 'HOLD_RESOLVE_RATE_LIMIT' not in t:
    if old_block not in t:
        raise SystemExit('ANCHOR_MISSING worker.py resolve block')
    t = t.replace(old_block, new_block, 1)

p.write_text(t)
print('PATCHED worker.py resolve throttle')
print('SELECTOR_STARVATION_AND_RESOLVE_THROTTLE_COMPLETE')
