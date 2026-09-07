import sqlite3
from pathlib import Path
from salary_policy import salary_rejection_reason

DB = Path('/opt/tg-job-agent/telegram_jobs.db')
con = sqlite3.connect(DB, timeout=30)
con.row_factory = sqlite3.Row
rows = con.execute("""
    SELECT q.id AS qid, q.status, q.job_id, q.recipient,
           j.id AS jid, j.title, j.raw_text
    FROM send_queue q
    JOIN jobs j ON j.job_id=q.job_id
    WHERE q.status IN ('pending','held_time','held_floodwait')
""").fetchall()
blocked = []
for row in rows:
    text = (row['raw_text'] or '') + '\n' + (row['title'] or '')
    reason = salary_rejection_reason(text)
    if not reason:
        continue
    con.execute(
        "UPDATE send_queue SET status='skipped',error=?,processed_at=datetime('now'),retry_at=NULL WHERE id=?",
        ('salary_floor:' + reason, row['qid'])
    )
    con.execute(
        "UPDATE jobs SET status='Rejected',selector_status='rejected_salary' WHERE id=?",
        (row['jid'],)
    )
    blocked.append((row['qid'], row['recipient'], row['job_id'], reason))
con.commit()
remaining = 0
for row in con.execute("""
    SELECT j.raw_text,j.title
    FROM send_queue q JOIN jobs j ON j.job_id=q.job_id
    WHERE q.status IN ('pending','held_time','held_floodwait')
"""):
    if salary_rejection_reason((row[0] or '') + '\n' + (row[1] or '')):
        remaining += 1
print('SALARY_QUEUE_CLEANED', blocked)
print('LOW_SALARY_ACTIVE_REMAINING', remaining)
print('ACTIVE_QUEUE', list(con.execute("SELECT status,count(*) FROM send_queue WHERE status IN ('pending','held_time','held_floodwait') GROUP BY status")))
con.close()
