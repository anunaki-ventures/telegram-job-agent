from pathlib import Path
import re

ROOT = Path('/opt/tg-job-agent')


def patch_selector():
    p = ROOT / 'selector.py'
    t = p.read_text()
    if 'from salary_policy import salary_rejection_reason' not in t:
        anchor = 'from candidate_classifier import classify_post\n'
        if anchor not in t:
            raise SystemExit('selector import anchor missing')
        t = t.replace(anchor, anchor + 'from salary_policy import salary_rejection_reason\n', 1)

    pattern = r'def salary_too_low\(text\):\n.*?\n(?=def language\(text\):)'
    replacement = "def salary_too_low(text):\n    return salary_rejection_reason(text) is not None\n\n"
    t2, n = re.subn(pattern, replacement, t, count=1, flags=re.S)
    if n != 1:
        raise SystemExit(f'selector salary function patch count={n}')
    p.write_text(t2)
    print('PATCHED selector.py salary_floor')


def patch_worker():
    p = ROOT / 'worker.py'
    t = p.read_text()
    if 'from salary_policy import salary_rejection_reason' not in t:
        anchor = 'from candidate_classifier import classify_post\n'
        if anchor not in t:
            raise SystemExit('worker import anchor missing')
        t = t.replace(anchor, anchor + 'from salary_policy import salary_rejection_reason\n', 1)

    if 'SKIP_SALARY_FLOOR' not in t:
        anchor = """        verified, safety_reason = outbound_employer_verified(con, row)
        if not verified:
            mark_skipped(con, row['id'], 'outbound_safety:' + safety_reason)
            print('SKIP_UNVERIFIED_EMPLOYER', recipient, safety_reason)
            return False

"""
        if anchor not in t:
            raise SystemExit('worker employer safety anchor missing')
        insertion = anchor + """        salary_reason = salary_rejection_reason(job_source_text(con, row['job_id']))
        if salary_reason:
            mark_skipped(con, row['id'], 'salary_floor:' + salary_reason)
            print('SKIP_SALARY_FLOOR', recipient, salary_reason)
            return False

"""
        t = t.replace(anchor, insertion, 1)
    p.write_text(t)
    print('PATCHED worker.py salary_floor_final_gate')


patch_selector()
patch_worker()
print('SALARY_FLOOR_PATCH_COMPLETE')
