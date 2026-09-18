import streamlit as st
import sqlite3
import hashlib
import os
import datetime
import uuid
import pandas as pd
from datetime import timedelta, time
from PIL import Image

# Google OR-Tools CP-SAT Constraint Engine
from ortools.sat.python import cp_model

# ==========================================
# 1. DATABASE INITIALIZATION & UTILITIES
# ==========================================
DB_NAME = "optisked.db"

def get_db_connection():
    """
    Returns a connection to the SQLite database.
    Always context-manage this using 'with get_db_connection() as conn:'
    """
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(password: str) -> str:
    return hashlib.sha256(str.encode(password)).hexdigest()

def init_production_db():
    with get_db_connection() as conn:
        c = conn.cursor()
        
        # Users Table
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                full_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('Program Head', 'Teacher', 'Student'))
            )
        ''')
        
        # Master Schedules Table
        c.execute('''
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
                teacher_code TEXT NOT NULL,
                section_code TEXT NOT NULL
            )
        ''')

        # Teacher Preferences Table
        c.execute('''
            CREATE TABLE IF NOT EXISTS teacher_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_username TEXT UNIQUE NOT NULL,
                preferred_days TEXT NOT NULL,
                preferred_timeslot TEXT NOT NULL
            )
        ''')

        # Teacher Schedule Claims
        c.execute('''
            CREATE TABLE IF NOT EXISTS teacher_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_username TEXT NOT NULL,
                schedule_id INTEGER NOT NULL,
                FOREIGN KEY (schedule_id) REFERENCES schedules (id) ON DELETE CASCADE,
                UNIQUE(teacher_username, schedule_id)
            )
        ''')

        # Student Schedule Claims
        c.execute('''
            CREATE TABLE IF NOT EXISTS student_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_username TEXT NOT NULL,
                schedule_id INTEGER NOT NULL,
                FOREIGN KEY (schedule_id) REFERENCES schedules (id) ON DELETE CASCADE,
                UNIQUE(student_username, schedule_id)
            )
        ''')

        # Attendance Verification Logs
        c.execute('''
            CREATE TABLE IF NOT EXISTS attendance_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_username TEXT NOT NULL,
                schedule_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                photo_path TEXT NOT NULL,
                status TEXT DEFAULT 'Attended',
                FOREIGN KEY (schedule_id) REFERENCES schedules (id)
            )
        ''')

        # Faculty Leave Requests
        c.execute('''
            CREATE TABLE IF NOT EXISTS leave_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_username TEXT NOT NULL,
                schedule_id INTEGER NOT NULL,
                leave_date TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT DEFAULT 'Pending' CHECK(status IN ('Pending', 'Approved', 'Rejected')),
                has_activity BOOLEAN DEFAULT 0,
                FOREIGN KEY (schedule_id) REFERENCES schedules (id)
            )
        ''')

        # Student Daily Attendance
        c.execute('''
            CREATE TABLE IF NOT EXISTS student_attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id INTEGER NOT NULL,
                student_username TEXT NOT NULL,
                date TEXT NOT NULL,
                status TEXT CHECK(status IN ('Present', 'Absent', 'Excused')),
                FOREIGN KEY (schedule_id) REFERENCES schedules (id)
            )
        ''')

        conn.commit()

init_production_db()

if not os.path.exists("attendance_photos"):
    os.makedirs("attendance_photos")


# ==========================================
# 2. DURATION CALCULATION & TIME OVERLAP HELPERS
# ==========================================
MORNING_START = time(7, 30)
EVENING_END = time(21, 0)  # 9:00 PM

def time_to_minutes(time_str: str) -> int:
    if not time_str:
        return 0
    dt = datetime.datetime.strptime(time_str.strip(), "%I:%M %p")
    return dt.hour * 60 + dt.minute

def times_overlap(start1: str, end1: str, start2: str, end2: str) -> bool:
    """Calculates whether two time windows overlap."""
    try:
        s1, e1 = time_to_minutes(start1), time_to_minutes(end1)
        s2, e2 = time_to_minutes(start2), time_to_minutes(end2)
        return max(s1, s2) < min(e1, e2)
    except Exception:
        return False

def is_within_allowed_range(dt_start, dt_end):
    t_start = dt_start.time()
    t_end = dt_end.time()

    # 1. Start Time Validations
    if t_start < MORNING_START:
        return False, "Class start time cannot be earlier than 7:30 AM."
    
    if t_start >= EVENING_END:
        return False, "Class start time cannot be later than 9:00 PM."

    # 2. Class Span & Boundary Validations
    if t_end > EVENING_END:
        return False, "Class end time cannot exceed 9:00 PM."
        
    if t_start < time(12, 0) and t_end > time(12, 0):
        return False, "Classes cannot cross into lunch break (12:00 PM - 1:00 PM)."
        
    if time(12, 0) <= t_start < time(13, 0):
        return False, "Cannot schedule classes during lunch break (12:00 PM - 1:00 PM)."

    return True, ""

def calculate_end_time(start_time_obj, duration_obj_or_minutes, time_pref="No Preference / Auto-Resolve"):
    if not start_time_obj:
        return None, None, None, "Start time is required."

    # Automatic PM adjustment when Evening is selected
    if time_pref == "Evening":
        if start_time_obj.hour < 12:
            start_time_obj = time(start_time_obj.hour + 12, start_time_obj.minute)

    if isinstance(duration_obj_or_minutes, (int, float)):
        hours = int(duration_obj_or_minutes // 60)
        minutes = int(duration_obj_or_minutes % 60)
    else:
        hours = duration_obj_or_minutes.hour
        minutes = duration_obj_or_minutes.minute

    if hours == 0 and minutes == 0:
        return None, None, None, "Duration must be greater than 00:00."

    dt_start = datetime.datetime.combine(datetime.date.today(), start_time_obj)
    dt_end = dt_start + timedelta(hours=hours, minutes=minutes)

    valid, err_msg = is_within_allowed_range(dt_start, dt_end)
    if not valid:
        return None, None, None, err_msg

    # Explicit AM/PM formatting
    formatted_start = dt_start.strftime("%I:%M %p").lstrip("0")
    formatted_end = dt_end.strftime("%I:%M %p").lstrip("0")

    return formatted_start, formatted_end, f"{formatted_start} - {formatted_end}", ""


# ==========================================
# 3. AI CONSTRAINT-BASED SCHEDULER ENGINE & CONFLICT RESOLVER
# ==========================================
TIME_SLOTS = [
    ("07:30 AM", "09:30 AM", "Morning", 2.0),
    ("09:30 AM", "11:30 AM", "Morning", 2.0),
    ("01:00 PM", "03:00 PM", "Afternoon", 2.0),
    ("03:00 PM", "05:00 PM", "Afternoon", 2.0),
    ("05:00 PM", "07:00 PM", "Evening", 2.0),
    ("07:00 PM", "09:00 PM", "Evening", 2.0)
]

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

def detect_schedule_conflicts(schedules_list):
    """Detects overlaps in rooms, teachers, or student cohorts."""
    conflicts = []
    for i in range(len(schedules_list)):
        for j in range(i + 1, len(schedules_list)):
            s1, s2 = schedules_list[i], schedules_list[j]
            if s1["day"] == s2["day"] and times_overlap(s1["time_start"], s1["time_end"], s2["time_start"], s2["time_end"]):
                if s1["room"] == s2["room"]:
                    conflicts.append({
                        "type": "Room Conflict",
                        "item1": s1,
                        "item2": s2,
                        "reason": f"Room {s1['room']} double-booked on {s1['day']} at {s1['time_start']}-{s1['time_end']}"
                    })
                elif s1["teacher_username"] == s2["teacher_username"]:
                    conflicts.append({
                        "type": "Teacher Conflict",
                        "item1": s1,
                        "item2": s2,
                        "reason": f"{s1['teacher_name']} double-booked on {s1['day']} at {s1['time_start']}-{s1['time_end']}"
                    })
                elif s1["program"] == s2["program"] and str(s1["year_level"]) == str(s2["year_level"]):
                    conflicts.append({
                        "type": "Cohort Conflict",
                        "item1": s1,
                        "item2": s2,
                        "reason": f"Cohort {s1['program']} Y{s1['year_level']} double-booked on {s1['day']} at {s1['time_start']}-{s1['time_end']}"
                    })
    return conflicts

def propose_conflict_fix(conflict, all_schedules, available_rooms):
    """Generates an automated fix payload for a detected conflict."""
    target_item = conflict["item2"]
    
    # 1. Try finding an available alternative room
    for room in available_rooms:
        if room == target_item["room"]:
            continue
        occupied = any(
            s["room"] == room and s["day"] == target_item["day"] and times_overlap(s["time_start"], s["time_end"], target_item["time_start"], target_item["time_end"])
            for s in all_schedules
        )
        if not occupied:
            return {
                "item_id": target_item.get("id"),
                "subject_code": target_item["subject_code"],
                "old_room": target_item["room"],
                "new_room": room,
                "old_slot": f"{target_item['day']} {target_item['time_start']}-{target_item['time_end']}",
                "new_slot": f"{target_item['day']} {target_item['time_start']}-{target_item['time_end']}",
                "description": f"Move {target_item['subject_code']} to Room {room}"
            }
            
    # 2. Try moving to an available time slot on the same day
    for slot in TIME_SLOTS:
        if slot[0] == target_item["time_start"]:
            continue
        occupied = any(
            s["day"] == target_item["day"] and 
            times_overlap(s["time_start"], s["time_end"], slot[0], slot[1]) and
            (s["room"] == target_item["room"] or s["teacher_username"] == target_item["teacher_username"])
            for s in all_schedules
        )
        if not occupied:
            return {
                "item_id": target_item.get("id"),
                "subject_code": target_item["subject_code"],
                "old_room": target_item["room"],
                "new_room": target_item["room"],
                "old_slot": f"{target_item['day']} {target_item['time_start']}-{target_item['time_end']}",
                "new_slot": f"{target_item['day']} {slot[0]}-{slot[1]}",
                "new_start": slot[0],
                "new_end": slot[1],
                "description": f"Shift {target_item['subject_code']} time slot to {slot[0]} - {slot[1]}"
            }
            
    return None

def solve_ai_schedule(course_requests, teacher_prefs, existing_schedules):
    model = cp_model.CpModel()
    assignments = {}

    num_slots = len(TIME_SLOTS)

    # 1. Decision Variable Instantiation
    for c_idx, course in enumerate(course_requests):
        c_rooms = course["assigned_rooms"]
        c_days = course.get("available_days", DAYS)
        
        for d_idx, day_str in enumerate(DAYS):
            if day_str not in c_days:
                continue
            for t_idx in range(num_slots):
                for r_idx, room in enumerate(c_rooms):
                    var_name = f"c{c_idx}_d{d_idx}_t{t_idx}_r{r_idx}"
                    assignments[(c_idx, d_idx, t_idx, r_idx)] = model.NewBoolVar(var_name)

    # Constraint 1: Assign course per required teaching days
    for c_idx, course in enumerate(course_requests):
        c_rooms = course["assigned_rooms"]
        c_days = course.get("available_days", DAYS)
        required_days = course.get("days_to_teach", 1)
        
        valid_vars = [
            assignments[(c_idx, d_idx, t_idx, r_idx)]
            for d_idx, day_str in enumerate(DAYS) if day_str in c_days
            for t_idx in range(num_slots)
            for r_idx in range(len(c_rooms))
            if (c_idx, d_idx, t_idx, r_idx) in assignments
        ]
        
        if len(valid_vars) < required_days:
            return None, f"Course '{course['subject_code']}' has fewer available combinations than the required {required_days} days to teach."
            
        model.Add(sum(valid_vars) == required_days)

        # Ensure no more than 1 class per day for the same course
        for d_idx, day_str in enumerate(DAYS):
            day_vars = [
                assignments[(c_idx, d_idx, t_idx, r_idx)]
                for t_idx in range(num_slots)
                for r_idx in range(len(c_rooms))
                if (c_idx, d_idx, t_idx, r_idx) in assignments
            ]
            if day_vars:
                model.Add(sum(day_vars) <= 1)

    # Constraint 2: Overlap Prevention (Teacher, Room, Cohort & Custom Time Spans)
    for c1_idx, c1 in enumerate(course_requests):
        for c2_idx, c2 in enumerate(course_requests):
            if c1_idx >= c2_idx:
                continue

            same_cohort = (c1["program"] == c2["program"] and c1["year_level"] == c2["year_level"])
            same_teacher = (c1["teacher_username"] == c2["teacher_username"])
            shared_rooms = set(c1["assigned_rooms"]).intersection(set(c2["assigned_rooms"]))

            if same_cohort or same_teacher or shared_rooms:
                for d_idx, day_str in enumerate(DAYS):
                    for t1_idx, slot1 in enumerate(TIME_SLOTS):
                        for t2_idx, slot2 in enumerate(TIME_SLOTS):
                            s1_start = c1.get("time_start") or slot1[0]
                            s1_end = c1.get("time_end") or slot1[1]
                            s2_start = c2.get("time_start") or slot2[0]
                            s2_end = c2.get("time_end") or slot2[1]

                            if times_overlap(s1_start, s1_end, s2_start, s2_end):
                                for r1_idx in range(len(c1["assigned_rooms"])):
                                    for r2_idx in range(len(c2["assigned_rooms"])):
                                        room_match = (c1["assigned_rooms"][r1_idx] == c2["assigned_rooms"][r2_idx])
                                        if room_match or same_teacher or same_cohort:
                                            var1 = assignments.get((c1_idx, d_idx, t1_idx, r1_idx))
                                            var2 = assignments.get((c2_idx, d_idx, t2_idx, r2_idx))
                                            if var1 is not None and var2 is not None:
                                                model.Add(var1 + var2 <= 1)

    # Constraint 3: Cross-program Published Database Schedule Conflict Resolution
    for c_idx, course in enumerate(course_requests):
        c_rooms = course["assigned_rooms"]
        teacher = course["teacher_username"]
        c_start, c_end = course.get("time_start"), course.get("time_end")
        
        for d_idx, day_str in enumerate(DAYS):
            for t_idx, slot in enumerate(TIME_SLOTS):
                slot_start = c_start if c_start else slot[0]
                slot_end = c_end if c_end else slot[1]
                
                for ex in existing_schedules:
                    if ex["day"] == day_str:
                        if times_overlap(slot_start, slot_end, ex["time_start"], ex["time_end"]):
                            if ex["teacher_username"] == teacher:
                                for r_idx in range(len(c_rooms)):
                                    if (c_idx, d_idx, t_idx, r_idx) in assignments:
                                        model.Add(assignments[(c_idx, d_idx, t_idx, r_idx)] == 0)
                            
                            for r_idx, room in enumerate(c_rooms):
                                if ex["room"] == room and (c_idx, d_idx, t_idx, r_idx) in assignments:
                                    model.Add(assignments[(c_idx, d_idx, t_idx, r_idx)] == 0)

                            if ex["program"] == course["program"] and str(ex["year_level"]) == str(course["year_level"]):
                                for r_idx in range(len(c_rooms)):
                                    if (c_idx, d_idx, t_idx, r_idx) in assignments:
                                        model.Add(assignments[(c_idx, d_idx, t_idx, r_idx)] == 0)

    # Soft Preferences Maximization Objective
    objective_terms = []
    for c_idx, course in enumerate(course_requests):
        c_rooms = course["assigned_rooms"]
        time_pref = course["time_pref"]
        t_uname = course["teacher_username"]
        
        teacher_prof = teacher_prefs.get(t_uname, {"days": DAYS, "timeslot": "Any"})

        for d_idx, day_str in enumerate(DAYS):
            for t_idx, slot in enumerate(TIME_SLOTS):
                for r_idx in range(len(c_rooms)):
                    if (c_idx, d_idx, t_idx, r_idx) not in assignments:
                        continue
                        
                    score = 10

                    if time_pref != "No Preference / Auto-Resolve":
                        if slot[2] == time_pref:
                            score += 30
                    else:
                        if day_str in teacher_prof["days"]:
                            score += 15
                        if teacher_prof["timeslot"] != "Any" and slot[2] == teacher_prof["timeslot"]:
                            score += 20

                    if score > 0:
                        objective_terms.append(score * assignments[(c_idx, d_idx, t_idx, r_idx)])

    if objective_terms:
        model.Maximize(sum(objective_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10.0
    status = solver.Solve(model)

    generated_schedule = []
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for c_idx, course in enumerate(course_requests):
            c_rooms = course["assigned_rooms"]
            c_start = course.get("time_start")
            c_end = course.get("time_end")

            for d_idx, day_str in enumerate(DAYS):
                for t_idx, slot in enumerate(TIME_SLOTS):
                    for r_idx, room_str in enumerate(c_rooms):
                        if (c_idx, d_idx, t_idx, r_idx) in assignments:
                            if solver.Value(assignments[(c_idx, d_idx, t_idx, r_idx)]) == 1:
                                clean_subj = course["subject_code"].replace(" ", "").upper()
                                clean_prof = "".join([w[0] for w in course["teacher_name"].split()]).upper()
                                
                                tch_code = f"TCH-{clean_prof}-{clean_subj}"
                                stu_code = f"STU-{course['program'].replace(' ', '')}-{clean_subj}"

                                generated_schedule.append({
                                    "teacher_name": course["teacher_name"],
                                    "teacher_username": course["teacher_username"],
                                    "subject_code": course["subject_code"],
                                    "subject_name": course["subject_name"],
                                    "program": course["program"],
                                    "year_level": course["year_level"],
                                    "day": day_str,
                                    "time_start": c_start if c_start else slot[0],
                                    "time_end": c_end if c_end else slot[1],
                                    "room": room_str,
                                    "teacher_code": tch_code,
                                    "section_code": stu_code
                                })
        return generated_schedule, "Success"
    else:
        return None, "Infeasible constraint combination! Conflicts exist across existing programs, overlapping room selections, or student section cohorts."


# ==========================================
# 4. DESIGN SYSTEM & UI STYLES
# ==========================================
def apply_custom_styles():
    st.markdown("""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');
        
        html, body, [class*="css"] {
            font-family: 'Plus Jakarta Sans', sans-serif !important;
        }

        .stApp {
            background-color: #F8FAFC !important;
            color: #0F172A !important;
        }
        
        [data-testid="stSidebar"] {
            background-color: #0A121E !important;
            border-right: 1px solid #1E293B !important;
        }
        [data-testid="stSidebar"] * {
            color: #94A3B8 !important;
        }
        [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {
            color: #FFFFFF !important;
        }
        
        div[data-testid="stSidebarUserContent"] div[role="radiogroup"] > label {
            background-color: transparent !important;
            padding: 12px 16px !important;
            border-radius: 8px !important;
            margin-bottom: 4px !important;
        }
        div[data-testid="stSidebarUserContent"] div[role="radiogroup"] > label[data-checked="true"] {
            background-color: #162438 !important;
            border-left: 3px solid #00B4D8 !important;
        }
        div[data-testid="stSidebarUserContent"] div[role="radiogroup"] > label[data-checked="true"] span {
            color: #38BDF8 !important;
            font-weight: 700 !important;
        }

        .opti-hero-card {
            background-color: #0A121E !important;
            padding: 40px;
            border-radius: 16px;
            color: #FFFFFF !important;
            box-shadow: 0 10px 30px rgba(9, 18, 30, 0.15);
        }

        .sched-section-card {
            background-color: #FFFFFF !important;
            padding: 24px;
            border-radius: 12px;
            border: 1px solid #E2E8F0;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.02);
            margin-bottom: 24px;
        }

        .section-header {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 20px;
        }
        
        .section-title {
            font-size: 18px;
            font-weight: 800;
            color: #1E293B;
            margin: 0;
        }
        
        .class-badge {
            background-color: #E0F2FE;
            color: #0284C7;
            font-weight: 700;
            font-size: 12px;
            padding: 4px 10px;
            border-radius: 12px;
        }

        .subject-pill {
            background-color: #F1F5F9;
            color: #475569;
            font-family: monospace;
            font-weight: 700;
            font-size: 13px;
            padding: 4px 10px;
            border-radius: 6px;
            display: inline-block;
        }

        div.stButton > button[kind="primary"] {
            background-color: #0F172A !important;
            color: #FFFFFF !important;
            border: none !important;
            border-radius: 8px !important;
            font-weight: 700 !important;
            height: 44px !important;
            padding: 0 20px !important;
        }

        div.stButton > button:has(div:contains("Delete")) {
            background-color: #EF4444 !important;
            color: #FFFFFF !important;
            border: none !important;
            border-radius: 8px !important;
            font-weight: 700 !important;
        }
        </style>
    """, unsafe_allow_html=True)


def render_sidebar():
    with st.sidebar:
        st.markdown("""
        <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 32px; padding: 0 8px;">
            <div style="background-color: #00B4D8; width: 36px; height: 36px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-weight: bold; color: #0A121E; font-size: 18px;">OS</div>
            <div>
                <h3 style="margin: 0; font-size: 16px; font-weight: 800; color: #FFFFFF;">OptiSked AI</h3>
                <p style="margin: 0; font-size: 10px; letter-spacing: 1px; color: #38BDF8; font-weight: 700;">PROGRAM HEAD</p>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # Removed "Teacher Directory" option
        view = st.radio("Navigation", [
            "Schedule Builder", 
            "Code Generation", 
            "Attendance Records"
        ], label_visibility="collapsed")

        st.markdown("<br/><br/><br/>", unsafe_allow_html=True)
        st.write("---")

        user_name = st.session_state.get('full_name', 'Dr. Maria Santos')
        first_letter = user_name[0] if user_name else 'D'
        st.markdown(f"""
        <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 16px; padding: 0 8px;">
            <div style="background-color: #0284C7; width: 36px; height: 36px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: bold; color: #FFFFFF; font-size: 15px;">{first_letter}</div>
            <div style="overflow: hidden;">
                <p style="margin: 0; font-size: 14px; font-weight: 700; color: #FFFFFF; white-space: nowrap; text-overflow: ellipsis; overflow: hidden;">{user_name}</p>
            </div>
        </div>
        """, unsafe_allow_html=True)

        if st.button("Sign out", use_container_width=True):
            st.session_state.clear()
            st.rerun()

        return view


# ==========================================
# 5. AUTHENTICATION MODULE
# ==========================================
def render_authentication():
    col_hero, col_form = st.columns([1.1, 0.9], gap="large")

    with col_hero:
        hero_html = """
<div class="opti-hero-card">
    <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 48px;">
        <div style="background-color: #38BDF8; width: 40px; height: 40px; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-weight: bold; color: #091322; font-size: 18px;">OS</div>
        <div>
            <h3 style="margin: 0; color: #FFFFFF; font-size: 20px; font-weight: 800;">OptiSked AI</h3>
            <p style="margin: 0; color: #64748B; font-size: 11px; letter-spacing: 1.5px; font-weight: 700;">SMCTI SCHEDULING SYSTEM</p>
        </div>
    </div>
    <h1 style="color: #FFFFFF !important; font-size: 46px; font-weight: 800; line-height: 1.05; margin-bottom: 24px; letter-spacing: -1px;">
        Smart<br/>
        <span style="color: #38BDF8;">Academic</span><br/>
        Scheduling.
    </h1>
    <p style="color: #94A3B8; font-size: 15px; line-height: 1.6; margin-bottom: 48px; max-width: 90%;">
        Automated constraint scheduling, faculty preferences, duration calculation, and automated conflict resolution.
    </p>
</div>
"""
        st.markdown(hero_html, unsafe_allow_html=True)

    with col_form:
        st.markdown("<br/>", unsafe_allow_html=True)
        auth_mode = st.tabs(["Log In", "Sign Up"])
        
        with auth_mode[0]:
            st.markdown("<h2 style='margin-top:24px; font-size: 26px; font-weight: 800; color: #0F172A;'>Welcome back</h2>", unsafe_allow_html=True)
            username = st.text_input("Username", value="", placeholder="your.username", key="login_user")
            password = st.text_input("Password", type="password", value="", placeholder="••••••••", key="login_pass")
            
            if st.button("Log In", use_container_width=True, type="primary"):
                with get_db_connection() as conn:
                    user = conn.execute(
                        "SELECT * FROM users WHERE username = ? AND password = ?",
                        (username, hash_password(password))
                    ).fetchone()

                if user:
                    st.session_state["authenticated"] = True
                    st.session_state["user_id"] = user["id"]
                    st.session_state["username"] = user["username"]
                    st.session_state["full_name"] = user["full_name"]
                    st.session_state["role"] = user["role"]
                    st.success(f"Welcome back, {user['full_name']}!")
                    st.rerun()
                else:
                    st.error("Invalid Username or Password.")

        with auth_mode[1]:
            st.markdown("<h2 style='margin-top:24px; font-size: 26px; font-weight: 800; color: #0F172A;'>Create Account</h2>", unsafe_allow_html=True)
            new_fullname = st.text_input("Full Name", value="", placeholder="e.g. Dr. Maria Santos", key="signup_name")
            new_username = st.text_input("Username", value="", placeholder="msantos", key="signup_user")
            new_password = st.text_input("Password", type="password", value="", placeholder="••••••••", key="signup_pass")
            new_role = st.selectbox("Select Role", ["Program Head", "Teacher", "Student"], key="signup_role")
            
            if st.button("Sign Up", use_container_width=True, type="primary"):
                if new_fullname and new_username and new_password:
                    try:
                        with get_db_connection() as conn:
                            conn.execute(
                                "INSERT INTO users (username, password, full_name, role) VALUES (?, ?, ?, ?)",
                                (new_username, hash_password(new_password), new_fullname, new_role)
                            )
                            conn.commit()
                        st.success("Account created successfully! Please log in.")
                    except sqlite3.IntegrityError:
                        st.error("Username already taken.")


# ==========================================
# 6. PROGRAM HEAD DASHBOARD MODULE
# ==========================================
def render_program_head_dashboard():
    view = render_sidebar()

    if "Schedule Builder" in view:
        st.markdown("<h1 style='font-size: 28px; font-weight: 800; margin: 0;'>100% AI Schedule Builder</h1>", unsafe_allow_html=True)
        st.markdown("<p style='color: #64748B; font-size: 14px;'>Allowed Scheduling Hours: <b>7:30 AM – 12:00 PM</b> and <b>1:00 PM – 9:00 PM</b> (Lunch break: 12:00 PM – 1:00 PM).</p>", unsafe_allow_html=True)
        st.markdown("<br/>", unsafe_allow_html=True)

        col_setup, col_preview = st.columns([1, 1], gap="large")

        if "ai_course_queue" not in st.session_state:
            st.session_state["ai_course_queue"] = []

        LAB_ROOMS = ["Room 201", "Room 202"] + [f"Room {r}" for r in range(301, 308)] + [f"Room {r}" for r in range(103, 108)]
        LECTURE_ROOMS = [f"Room {r}" for r in range(203, 226)]
        ALL_ROOMS = LAB_ROOMS + LECTURE_ROOMS

        with col_setup:
            st.markdown("### Faculty Schedule Entry Form")
            
            with get_db_connection() as conn:
                teachers = conn.execute("SELECT username, full_name FROM users WHERE role = 'Teacher'").fetchall()
            
            t_options = {t["full_name"]: t["username"] for t in teachers} if teachers else {"Select Faculty": ""}

            PROGRAM_CHOICES = [
                "BS Computer Science",
                "BS Business Administration",
                "BS Nursing",
                "BS Civil Engineering",
                "BS Tourism Management",
                "BS Hospitality Management",
                "BS Education",
                "AB Psychology"
            ]

            t_name = st.selectbox("Faculty Name", list(t_options.keys()))

            c1, c2 = st.columns(2)
            with c1:
                subj_c = st.text_input("Subject Code", value="", placeholder="e.g. CS301")
                prog = st.selectbox("Program", PROGRAM_CHOICES)
            with c2:
                subj_n = st.text_input("Subject Name", value="", placeholder="e.g. Algorithms")
                y_lvl = st.selectbox("Year Level", ["1", "2", "3", "4"])

            st.markdown("---")
            st.markdown("**Weekly Requirements & Scheduling Input**")
            
            col_req1, col_req2 = st.columns(2)
            with col_req1:
                req_hours_week = st.number_input("Required Hours in a Week", min_value=1.0, max_value=40.0, value=8.0, step=0.5)
            with col_req2:
                days_to_teach = st.number_input("Days to Teach", min_value=1, max_value=6, value=3, step=1)
                
            daily_hours = req_hours_week / days_to_teach
            daily_minutes = daily_hours * 60
            st.info(f"💡 Calculated Daily Class Duration: **{int(daily_minutes // 60)}h {int(daily_minutes % 60)}m** across **{days_to_teach} days**.")

            class_type = st.radio("Class Type", ["Lecture", "Laboratory"], horizontal=True)
            
            if class_type == "Laboratory":
                selected_rooms = st.multiselect("Assigned Laboratory Room(s)", LAB_ROOMS, default=[LAB_ROOMS[0]])
            else:
                selected_rooms = st.multiselect("Assigned Lecture Room(s)", LECTURE_ROOMS, default=[LECTURE_ROOMS[0]])

            avail_days = st.multiselect("Available Days", DAYS, default=["Mon", "Wed", "Fri"])
            
            time_pref = st.selectbox("Time Preference Category", ["No Preference / Auto-Resolve", "Morning", "Afternoon", "Evening"])

            start_time_val = st.time_input("Start Time", time(7, 30))
            if time_pref == "Evening" and start_time_val.hour < 12:
                st.caption("ℹ️ Evening category auto-adjusts morning start times to **PM** (e.g., 7:30 AM ➔ 7:30 PM).")

            if st.button("Add to AI Queue", type="primary"):
                if not subj_c or not subj_n:
                    st.error("Please enter Subject Code and Subject Name.")
                elif not selected_rooms:
                    st.error("At least one room must be selected from the dropdown.")
                elif len(avail_days) < days_to_teach:
                    st.error(f"Selected available days ({len(avail_days)}) cannot be less than required days to teach ({days_to_teach}).")
                else:
                    start_str, end_str, calculated_range, err = calculate_end_time(start_time_val, daily_minutes, time_pref)
                    if err:
                        st.error(err)
                    else:
                        st.session_state["ai_course_queue"].append({
                            "id": str(uuid.uuid4()),
                            "teacher_name": t_name,
                            "teacher_username": t_options.get(t_name, ""),
                            "subject_code": subj_c,
                            "subject_name": subj_n,
                            "program": prog,
                            "year_level": y_lvl,
                            "req_hours_week": req_hours_week,
                            "days_to_teach": days_to_teach,
                            "daily_minutes": daily_minutes,
                            "time_start": start_str,
                            "time_end": end_str,
                            "calculated_time_range": calculated_range,
                            "available_days": avail_days,
                            "time_pref": time_pref,
                            "assigned_rooms": selected_rooms
                        })
                        st.success(f"Added {subj_c} to AI Queue ({days_to_teach} days/wk)!")

        with col_preview:
            st.markdown(f"### AI Queue ({len(st.session_state['ai_course_queue'])} Items)")

            if st.session_state["ai_course_queue"]:
                queue_to_remove = None
                for idx, q in enumerate(st.session_state["ai_course_queue"]):
                    c_del, c_info = st.columns([0.25, 0.75])
                    with c_del:
                        if st.button("Delete", key=f"q_del_{q['id']}"):
                            queue_to_remove = q['id']
                    with c_info:
                        st.markdown(f"**{q['subject_code']}** - {q['subject_name']} | **Faculty:** {q['teacher_name']}")
                        st.caption(f"Weekly Target: **{q['req_hours_week']} hrs** over **{q['days_to_teach']} days** | Days: {', '.join(q['available_days'])}")
                        st.caption(f"Range: **{q['calculated_time_range']}** | Category: **{q['time_pref']}**")
                        st.caption(f"Prog: {q['program']} {q['year_level']} | Rooms: {', '.join(q['assigned_rooms'])}")
                
                if queue_to_remove:
                    st.session_state["ai_course_queue"] = [item for item in st.session_state["ai_course_queue"] if item['id'] != queue_to_remove]
                    st.rerun()
            else:
                st.info("No items in queue. Fill form on left.")

            st.markdown("<br/>", unsafe_allow_html=True)
            
            if st.button("Generate AI Conflict-Free Schedule", type="primary", use_container_width=True):
                if not st.session_state["ai_course_queue"]:
                    st.error("Add at least one entry to the queue.")
                else:
                    with get_db_connection() as conn:
                        prefs_raw = conn.execute("SELECT * FROM teacher_preferences").fetchall()
                        existing_schedules = conn.execute("SELECT * FROM schedules").fetchall()

                    teacher_prefs = {
                        p["teacher_username"]: {
                            "days": p["preferred_days"].split(","),
                            "timeslot": p["preferred_timeslot"]
                        } for p in prefs_raw
                    }

                    with st.spinner("AI Solver evaluating constraints..."):
                        gen_sched, status = solve_ai_schedule(
                            st.session_state["ai_course_queue"], 
                            teacher_prefs,
                            existing_schedules
                        )

                    if gen_sched:
                        st.session_state["preview_generated_schedule"] = gen_sched
                        st.success(f"AI Calculation Complete! Generated {len(gen_sched)} conflict-free slots.")
                    else:
                        st.error(f"AI Solver Conflict Error: {status}")

        if "preview_generated_schedule" in st.session_state and st.session_state["preview_generated_schedule"]:
            st.markdown("<br/><h3>Preview Generated AI Schedule</h3>", unsafe_allow_html=True)
            preview_data = st.session_state["preview_generated_schedule"]
            st.dataframe(preview_data, use_container_width=True)

            if st.button("Publish Schedule to Database", type="primary"):
                with get_db_connection() as conn:
                    for item in preview_data:
                        conn.execute("""
                            INSERT INTO schedules (
                                teacher_name, teacher_username, subject_code, subject_name, 
                                program, year_level, day, time_start, time_end, room, teacher_code, section_code
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            item["teacher_name"], item["teacher_username"], item["subject_code"],
                            item["subject_name"], item["program"], item["year_level"],
                            item["day"], item["time_start"], item["time_end"],
                            item["room"], item["teacher_code"], item["section_code"]
                        ))
                    conn.commit()
                st.session_state["preview_generated_schedule"] = []
                st.session_state["ai_course_queue"] = []
                st.success("Schedules published successfully!")
                st.rerun()

        st.markdown("<br/><hr/><br/>", unsafe_allow_html=True)

        # Master Schedules & Automated Conflict Resolver Display
        with get_db_connection() as conn:
            schedules = [dict(s) for s in conn.execute("SELECT * FROM schedules ORDER BY program, year_level, id").fetchall()]

        if schedules:
            # --- AUTOMATED CONFLICT DETECTION & FIX RESOLVER ---
            detected_conflicts = detect_schedule_conflicts(schedules)
            if detected_conflicts:
                st.warning(f"⚠️ {len(detected_conflicts)} Schedule Conflict(s) Detected in Database!")
                for idx, c in enumerate(detected_conflicts):
                    with st.expander(f"Conflict #{idx+1}: {c['type']} - {c['reason']}", expanded=True):
                        st.write(f"**Description:** {c['reason']}")
                        proposed_fix = propose_conflict_fix(c, schedules, ALL_ROOMS)
                        if proposed_fix:
                            st.info(f"**Suggested Fix:** {proposed_fix['description']} ({proposed_fix['old_slot']} ➔ {proposed_fix['new_slot']})")
                            
                            btn_key = f"apply_fix_{c['item2']['id']}_{idx}"
                            if st.button("Apply Fix", key=btn_key, type="primary"):
                                with get_db_connection() as conn:
                                    if "new_start" in proposed_fix:
                                        conn.execute(
                                            "UPDATE schedules SET room = ?, time_start = ?, time_end = ? WHERE id = ?",
                                            (proposed_fix["new_room"], proposed_fix["new_start"], proposed_fix["new_end"], proposed_fix["item_id"])
                                        )
                                    else:
                                        conn.execute(
                                            "UPDATE schedules SET room = ? WHERE id = ?",
                                            (proposed_fix["new_room"], proposed_fix["item_id"])
                                        )
                                    conn.commit()
                                st.success("Fix applied successfully!")
                                st.rerun()
                        else:
                            st.error("No automated single-room/slot fix available. Manual adjustment required.")

            # Consolidated Display Logic: Group entries by class attributes and concatenate days into a single row
            df_sched = pd.DataFrame(schedules)
            grouped = df_sched.groupby([
                "program", "year_level", "teacher_name", 
                "subject_code", "subject_name", "room", 
                "time_start", "time_end"
            ]).agg({
                "day": lambda days: ", ".join(sorted(set(days), key=lambda d: DAYS.index(d) if d in DAYS else 99)),
                "id": list
            }).reset_index()

            programs = grouped['program'].unique()
            for prog_name in programs:
                prog_df = grouped[grouped['program'] == prog_name]
                for yr in sorted(prog_df['year_level'].unique()):
                    cohort_df = prog_df[prog_df['year_level'] == yr]
                    sec_name = f"{prog_name} {yr}"

                    st.markdown(f"""
                    <div class="sched-section-card">
                        <div class="section-header">
                            <span class="section-title">{sec_name}</span>
                            <span class="class-badge">{len(cohort_df)} classes</span>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)

                    for _, item in cohort_df.iterrows():
                        r1, r2, r3, r4, r5, r6, r7 = st.columns([2, 1, 2.2, 1.5, 2.5, 1.2, 0.8])
                        with r1: st.markdown(f"**{item['teacher_name']}**")
                        with r2: st.write(item['day'])
                        with r3: st.write(f"**{item['time_start']} - {item['time_end']}**")
                        with r4: st.markdown(f"<span class='subject-pill'>{item['subject_code']}</span>", unsafe_allow_html=True)
                        with r5: st.write(item['subject_name'])
                        with r6: st.write(item['room'])
                        with r7:
                            # Delete all associated schedule record IDs for this consolidated group
                            if st.button("Delete", key=f"del_group_{item['id'][0]}"):
                                with get_db_connection() as conn:
                                    conn.executemany("DELETE FROM schedules WHERE id = ?", [(i,) for i in item['id']])
                                    conn.commit()
                                st.rerun()
                    st.markdown("<br/>", unsafe_allow_html=True)
        else:
            st.info("No master schedules generated yet.")

    elif "Code Generation" in view:
        st.title("Access Code Hub")
        with get_db_connection() as conn:
            # Query distinct teacher codes
            teacher_codes = conn.execute("""
                SELECT DISTINCT teacher_name, subject_code, teacher_code 
                FROM schedules 
                ORDER BY teacher_name, subject_code
            """).fetchall()

            # Query distinct student codes
            student_codes = conn.execute("""
                SELECT DISTINCT subject_code, section_code 
                FROM schedules 
                ORDER BY subject_code, section_code
            """).fetchall()

        col_tch, col_stu = st.columns(2)
        with col_tch:
            st.subheader("Teacher Access Codes")
            for t in teacher_codes:
                st.markdown(f"**{t['teacher_name']}** (`{t['subject_code']}`): `{t['teacher_code']}`")

        with col_stu:
            st.subheader("Student Access Codes")
            for s in student_codes:
                st.markdown(f"**Subject:** `{s['subject_code']}`: `{s['section_code']}`")

    elif "Attendance Records" in view:
        st.title("Attendance Center")
        st.info("View faculty leave requests and photo check-in verification records.")


# ==========================================
# 7. TEACHER PORTAL MODULE
# ==========================================
def render_teacher_portal():
    with st.sidebar:
        st.markdown(f"### {st.session_state['full_name']}")
        st.markdown("**Role:** Teacher")
        if st.button("Sign Out", use_container_width=True):
            st.session_state.clear()
            st.rerun()

    st.title("Teacher Workspace")
    code_input = st.text_input("Enter Teacher Access Code", value="", placeholder="e.g. TCH-MS-CS301").strip()
    if st.button("Link Schedule", type="primary"):
        with get_db_connection() as conn:
            sched = conn.execute("SELECT * FROM schedules WHERE teacher_code = ?", (code_input,)).fetchone()
            if sched:
                try:
                    conn.execute("INSERT INTO teacher_claims (teacher_username, schedule_id) VALUES (?, ?)",
                                 (st.session_state["username"], sched["id"]))
                    conn.commit()
                    st.success("Linked schedule!")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.warning("You have already claimed this schedule.")
            else:
                st.error("Invalid Access Code.")


# ==========================================
# 8. STUDENT PORTAL MODULE
# ==========================================
def render_student_portal():
    with st.sidebar:
        st.markdown(f"### {st.session_state['full_name']}")
        st.markdown("**Role:** Student")
        if st.button("Sign Out", use_container_width=True):
            st.session_state.clear()
            st.rerun()

    st.title("Student Portal")
    sec_code = st.text_input("Enter Student Access Code", value="", placeholder="e.g. STU-BSCS-CS301").strip()
    if st.button("Enroll", type="primary"):
        with get_db_connection() as conn:
            sched = conn.execute("SELECT * FROM schedules WHERE section_code = ?", (sec_code,)).fetchone()
            if sched:
                try:
                    conn.execute("INSERT INTO student_claims (student_username, schedule_id) VALUES (?, ?)",
                                 (st.session_state["username"], sched["id"]))
                    conn.commit()
                    st.success("Enrolled successfully!")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.warning("You are already enrolled in this schedule.")
            else:
                st.error("Invalid Section Access Code.")


# ==========================================
# 9. ROUTING CONTROLLER
# ==========================================
def main():
    st.set_page_config(page_title="OptiSked AI", layout="wide")
    apply_custom_styles()

    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False

    if not st.session_state["authenticated"]:
        render_authentication()
    else:
        role = st.session_state.get("role")
        if role == "Program Head":
            render_program_head_dashboard()
        elif role == "Teacher":
            render_teacher_portal()
        elif role == "Student":
            render_student_portal()

if __name__ == "__main__":
    main()