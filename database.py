import sqlite3
import hashlib
import os
import shutil
import tempfile
from datetime import datetime

PROJECT_DB_PATH = os.path.join(os.path.dirname(__file__), 'classwatch.db')
RUNTIME_DB_DIR = os.path.join(tempfile.gettempdir(), 'classwatch_runtime')
RUNTIME_DB_PATH = os.path.join(RUNTIME_DB_DIR, 'classwatch.db')


def can_write_project_db():
    project_dir = os.path.dirname(PROJECT_DB_PATH)
    return os.access(PROJECT_DB_PATH, os.W_OK) and os.access(project_dir, os.W_OK)


def resolve_db_path():
    env_path = os.environ.get('CLASSWATCH_DB_PATH')
    if env_path:
        return env_path

    if can_write_project_db():
        return PROJECT_DB_PATH

    os.makedirs(RUNTIME_DB_DIR, exist_ok=True)
    if not os.path.exists(RUNTIME_DB_PATH) and os.path.exists(PROJECT_DB_PATH):
        shutil.copy2(PROJECT_DB_PATH, RUNTIME_DB_PATH)
    if os.path.exists(RUNTIME_DB_PATH):
        os.chmod(RUNTIME_DB_PATH, 0o666)
    return RUNTIME_DB_PATH


DB_PATH = resolve_db_path()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_id INTEGER NOT NULL,
            code TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            ended_at TEXT,
            is_active INTEGER DEFAULT 1,
            FOREIGN KEY (host_id) REFERENCES users(id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_code TEXT NOT NULL,
            student_name TEXT NOT NULL,
            total_frames INTEGER DEFAULT 0,
            engaged_frames INTEGER DEFAULT 0,
            sleeping_frames INTEGER DEFAULT 0,
            away_frames INTEGER DEFAULT 0,
            no_face_frames INTEGER DEFAULT 0,
            total_alerts INTEGER DEFAULT 0,
            sleeping_alerts INTEGER DEFAULT 0,
            away_alerts INTEGER DEFAULT 0,
            no_face_alerts INTEGER DEFAULT 0,
            engagement_score REAL DEFAULT 0.0,
            joined_at TEXT,
            left_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (meeting_code) REFERENCES meetings(code)
        )
    ''')

    # Add ended_at column if it doesn't exist (for existing databases)
    try:
        cursor.execute('ALTER TABLE meetings ADD COLUMN ended_at TEXT')
    except sqlite3.OperationalError:
        pass  # Column already exists

    conn.commit()
    conn.close()


def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()


def create_user(name, email, password, role):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'INSERT INTO users (name, email, password, role) VALUES (?, ?, ?, ?)',
            (name, email, hash_password(password), role)
        )
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def get_user_by_email(email):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE email = ?', (email,))
    user = cursor.fetchone()
    conn.close()
    return dict(user) if user else None


def authenticate_user(email, password):
    user = get_user_by_email(email)
    if user and user['password'] == hash_password(password):
        return user
    return None


def create_meeting(host_id, title, code):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO meetings (host_id, code, title, created_at) VALUES (?, ?, ?, ?)',
        (host_id, code, title, datetime.now().isoformat())
    )
    conn.commit()
    meeting_id = cursor.lastrowid
    conn.close()
    return meeting_id


def get_meeting_by_code(code):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM meetings WHERE code = ? AND is_active = 1', (code,))
    meeting = cursor.fetchone()
    conn.close()
    return dict(meeting) if meeting else None


def get_meeting_any(code):
    """Get meeting regardless of active status (for viewing reports)."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM meetings WHERE code = ?', (code,))
    meeting = cursor.fetchone()
    conn.close()
    return dict(meeting) if meeting else None


def get_meetings_by_host(host_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT * FROM meetings WHERE host_id = ? ORDER BY created_at DESC',
        (host_id,)
    )
    meetings = [dict(m) for m in cursor.fetchall()]
    conn.close()
    return meetings


def end_meeting(code):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'UPDATE meetings SET is_active = 0, ended_at = ? WHERE code = ?',
        (datetime.now().isoformat(), code)
    )
    conn.commit()
    conn.close()


# ── Report functions ──────────────────────────────────────────────────────────

def save_report(meeting_code, student_name, stats):
    """Save an engagement report for a student in a meeting."""
    conn = get_db()
    cursor = conn.cursor()

    total = stats.get('total_frames', 0)
    engaged = stats.get('engaged_frames', 0)
    score = round((engaged / total) * 100, 1) if total > 0 else 0.0

    cursor.execute('''
        INSERT INTO reports (
            meeting_code, student_name,
            total_frames, engaged_frames, sleeping_frames, away_frames, no_face_frames,
            total_alerts, sleeping_alerts, away_alerts, no_face_alerts,
            engagement_score, joined_at, left_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        meeting_code,
        student_name,
        total,
        engaged,
        stats.get('sleeping_frames', 0),
        stats.get('away_frames', 0),
        stats.get('no_face_frames', 0),
        stats.get('total_alerts', 0),
        stats.get('sleeping_alerts', 0),
        stats.get('away_alerts', 0),
        stats.get('no_face_alerts', 0),
        score,
        stats.get('joined_at', ''),
        stats.get('left_at', datetime.now().strftime('%H:%M:%S')),
        datetime.now().isoformat(),
    ))
    conn.commit()
    report_id = cursor.lastrowid
    conn.close()
    return report_id


def get_reports_by_meeting(meeting_code):
    """Get all student reports for a meeting."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT * FROM reports WHERE meeting_code = ? ORDER BY engagement_score DESC',
        (meeting_code,)
    )
    reports = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return reports


def has_reports(meeting_code):
    """Check if a meeting has any saved reports."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) as cnt FROM reports WHERE meeting_code = ?', (meeting_code,))
    result = cursor.fetchone()
    conn.close()
    return result['cnt'] > 0
