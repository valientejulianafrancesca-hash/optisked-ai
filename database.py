import sqlite3
import hashlib

DB_NAME = "optisked.db"

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def init_production_db():
    conn = get_db()
    cursor = conn.cursor()

    # Enforce foreign key constraints
    cursor.execute("PRAGMA foreign_keys = ON;")

    # 1. Users Table (Section 4.1 Schema)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('Program Head', 'Teacher', 'Student'))
        );
    ''')

    # 2. Master Schedules Table (Section 4.1 Schema)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_name TEXT NOT NULL,
            teacher_username TEXT NOT NULL,
            subject_code TEXT NOT NULL,
            subject_name TEXT NOT NULL,
            program TEXT NOT NULL,
            year_level TEXT NOT NULL,
            day TEXT NOT NULL,
            time_start TEXT NOT NULL,
            time_end TEXT NOT NULL,
            room TEXT NOT NULL,
            teacher_code TEXT UNIQUE NOT NULL,
            section_code TEXT UNIQUE NOT NULL
        );
    ''')

    # 3. Teacher Schedule Claims (Junction Table)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS teacher_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_username TEXT NOT NULL,
            schedule_id INTEGER NOT NULL,
            FOREIGN KEY (schedule_id) REFERENCES schedules (id) ON DELETE CASCADE
        );
    ''')

    # 4. Student Schedule Claims (Junction Table)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS student_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_username TEXT NOT NULL,
            schedule_id INTEGER NOT NULL,
            FOREIGN KEY (schedule_id) REFERENCES schedules (id) ON DELETE CASCADE
        );
    ''')

    # 5. Attendance Verification Logs
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS attendance_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_username TEXT NOT NULL,
            schedule_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            photo_path TEXT NOT NULL,
            status TEXT DEFAULT 'Attended',
            FOREIGN KEY (schedule_id) REFERENCES schedules (id)
        );
    ''')

    # 6. Faculty Leave Applications
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS leave_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_username TEXT NOT NULL,
            schedule_id INTEGER NOT NULL,
            leave_date TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT DEFAULT 'Pending' CHECK(status IN ('Pending', 'Approved', 'Rejected')),
            has_activity BOOLEAN DEFAULT 0,
            FOREIGN KEY (schedule_id) REFERENCES schedules (id)
        );
    ''')

    # Insert Demo Accounts matching your design specification
    demo_users = [
        ('programhead', hash_password('ph123'), 'Program Head Admin', 'Program Head'),
        ('branzuela', hash_password('t123'), 'Prof. Branzuela', 'Teacher'),
        ('student1', hash_password('s123'), 'Student Account', 'Student')
    ]

    for user in demo_users:
        cursor.execute('''
            INSERT OR IGNORE INTO users (username, password, full_name, role)
            VALUES (?, ?, ?, ?)
        ''', user)

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_production_db()
    print("Database schema successfully generated.")