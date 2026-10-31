"""#942: failed browser checks and visual gate disclosure."""
import sqlite3
import inspect
from office import closeout, doctor

def test_doctor_visual_launch_failure_is_counted():
    source = inspect.getsource(doctor.doctor)
    assert 'visual_status, visual_problem = _visual_capture_status()' in source
    assert 'if visual_problem:\n        problems += 1' in source

def test_closeout_discloses_blocked_visual_review_per_task():
    con = sqlite3.connect(':memory:')
    con.row_factory = sqlite3.Row
    con.execute('CREATE TABLE gates (id TEXT, run_id TEXT, task_id TEXT, kind TEXT, evidence_status TEXT, status TEXT, summary TEXT, created_at TEXT)')
    con.executemany('INSERT INTO gates VALUES (?,?,?,?,?,?,?,?)', [
        ('a','run','T1','visual','CAPTURE_BLOCKED','blocked','Chromium cannot launch','1'),
        ('b','run','T2','visual','COMPARABLE','done','approved','1'),
        ('c','run','T3','visual','INVALID_COMPARISON','done','DOM only','1'),
    ])
    lines = closeout.visual_review_disclosures(con, {'id':'run'})
    assert len(lines) == 2
    assert 'T1' in lines[0] and 'CAPTURE_BLOCKED' in lines[0]
    assert 'T3' in lines[1] and 'INVALID_COMPARISON' in lines[1]
