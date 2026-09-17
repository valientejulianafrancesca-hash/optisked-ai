import streamlit as st
import sqlite3
import hashlib
import os
import datetime
from PIL import Image

# Google OR-Tools CP-SAT Constraint Engine
from ortools.sat.python import cp_model

# ==========================================
# 1. DATABASE INITIALIZATION & UTILITIES
# ==========================================
DB_NAME = "optisked.db"

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(password: str) -> str:
    return hashlib.sha256(str.encode(password)).hexdigest()

def init_production_db():
    conn = get_db_connection()
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

    # Teacher Preferences Table (For AI Optimization Engine)
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
            FOREIGN KEY (schedule_id) REFERENCES schedules (id) ON DELETE CASCADE
        )
    ''')

    # Student Schedule Claims
    c.execute('''
        CREATE TABLE IF NOT EXISTS student_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_username TEXT NOT NULL,
            schedule_id INTEGER NOT NULL,
            FOREIGN KEY (schedule_id) REFERENCES schedules (id) ON DELETE CASCADE
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
    conn.close()

init_production_db()

if not os.path.exists("attendance_photos"):
    os.makedirs("attendance_photos")


# ==========================================
# 2. AI CONSTRAINT-BASED SCHEDULER ENGINE
# ==========================================
TIME_SLOTS = [
    ("08:00 AM", "10:00 AM", "Morning"),
    ("10:00 AM", "12:00 PM", "Morning"),
    ("01:00 PM", "03:00 PM", "Afternoon"),
    ("03:00 PM", "05:00 PM", "Afternoon"),
    ("05:00 PM", "07:00 PM", "Evening")
]

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]

def solve_ai_schedule(course_requests, available_rooms, teacher_prefs):
    """
    Uses Google OR-Tools CP-SAT Solver to generate an optimal, conflict-free schedule.
    Hard Constraints:
      - No teacher can teach two classes at the same time slot.
      - No room can host two classes at the same time slot.
      - No student section/program cohort can be in two classes simultaneously.
    Soft Constraints (Optimized via Objective Score):
      - Maximize teacher day and time slot preferences.
    """
    model = cp_model.CpModel()
    assignments = {}

    num_slots = len(TIME_SLOTS)
    num_days = len(DAYS)
    num_rooms = len(available_rooms)

    # Decision Variables: assign[c, d, t, r] = 1 if course c is assigned to day d, slot t, room r
    for c_idx, course in enumerate(course_requests):
        for d_idx in range(num_days):
            for t_idx in range(num_slots):
                for r_idx in range(num_rooms):
                    var_name = f"c{c_idx}_d{d_idx}_t{t_idx}_r{r_idx}"
                    assignments[(c_idx, d_idx, t_idx, r_idx)] = model.NewBoolVar(var_name)

    # Constraint 1: Each course must be scheduled exactly once
    for c_idx in range(len(course_requests)):
        model.AddExactlyOne(
            assignments[(c_idx, d_idx, t_idx, r_idx)]
            for d_idx in range(num_days)
            for t_idx in range(num_slots)
            for r_idx in range(num_rooms)
        )

    # Constraint 2: No teacher double-booking
    for d_idx in range(num_days):
        for t_idx in range(num_slots):
            for teacher in set(c["teacher_username"] for c in course_requests):
                t_courses = [i for i, c in enumerate(course_requests) if c["teacher_username"] == teacher]
                model.Add(
                    sum(assignments[(c_idx, d_idx, t_idx, r_idx)] 
                        for c_idx in t_courses 
                        for r_idx in range(num_rooms)) <= 1
                )

    # Constraint 3: No room double-booking
    for d_idx in range(num_days):
        for t_idx in range(num_slots):
            for r_idx in range(num_rooms):
                model.Add(
                    sum(assignments[(c_idx, d_idx, t_idx, r_idx)] 
                        for c_idx in range(len(course_requests))) <= 1
                )

    # Constraint 4: No student section cohort double-booking
    for d_idx in range(num_days):
        for t_idx in range(num_slots):
            for sec in set((c["program"], c["year_level"]) for c in course_requests):
                s_courses = [i for i, c in enumerate(course_requests) if (c["program"], c["year_level"]) == sec]
                model.Add(
                    sum(assignments[(c_idx, d_idx, t_idx, r_idx)] 
                        for c_idx in s_courses 
                        for r_idx in range(num_rooms)) <= 1
                )

    # Soft Constraint Objective: Teacher Preference Maximization
    objective_terms = []
    for c_idx, course in enumerate(course_requests):
        t_uname = course["teacher_username"]
        prefs = teacher_prefs.get(t_uname, {"days": DAYS, "timeslot": "Any"})
        
        pref_days = prefs["days"]
        pref_slot_cat = prefs["timeslot"]

        for d_idx, day_str in enumerate(DAYS):
            for t_idx, slot in enumerate(TIME_SLOTS):
                for r_idx in range(num_rooms):
                    score = 0
                    if day_str in pref_days:
                        score += 10
                    if pref_slot_cat != "Any" and slot[2] == pref_slot_cat:
                        score += 15
                    
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
            for d_idx, day_str in enumerate(DAYS):
                for t_idx, slot in enumerate(TIME_SLOTS):
                    for r_idx, room_str in enumerate(available_rooms):
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
                                "time_start": slot[0],
                                "time_end": slot[1],
                                "room": room_str,
                                "teacher_code": tch_code,
                                "section_code": stu_code
                            })
        return generated_schedule, "Success"
    else:
        return None, "Infeasible constraint combination. Try adding more rooms or time slots."


# ==========================================
# 3. DESIGN SYSTEM & UI STYLES
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

        .grid-header {
            font-size: 11px;
            font-weight: 800;
            color: #94A3B8;
            letter-spacing: 0.8px;
            text-transform: uppercase;
            padding-bottom: 12px;
            border-bottom: 1px solid #F1F5F9;
            margin-bottom: 12px;
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

        div[data-baseweb="input"] > div, div[data-baseweb="select"] > div {
            background-color: #FFFFFF !important;
            color: #0F172A !important;
            border-radius: 8px !important;
            border: 1px solid #CBD5E1 !important;
        }
        label {
            color: #475569 !important;
            font-size: 13px !important;
            font-weight: 600 !important;
        }
        </style>
    """, unsafe_allow_html=True)


def render_sidebar():
    with st.sidebar:
        st.markdown("""
        <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 32px; padding: 0 8px;">
            <div style="background-color: #00B4D8; width: 36px; height: 36px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-weight: bold; color: #0A121E; font-size: 18px;">🗓️</div>
            <div>
                <h3 style="margin: 0; font-size: 16px; font-weight: 800; color: #FFFFFF;">OptiSked AI</h3>
                <p style="margin: 0; font-size: 10px; letter-spacing: 1px; color: #38BDF8; font-weight: 700;">PROGRAM HEAD</p>
            </div>
        </div>
        """, unsafe_allow_html=True)

        view = st.radio("Navigation", [
            "🤖  AI Schedule Generator",
            "🗓️  Schedule Builder", 
            "🔑  Code Generation", 
            "📋  Attendance Records",
            "👨‍🏫  Teacher Directory"
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

        if st.button("🚪 Sign out", use_container_width=True):
            st.session_state.clear()
            st.rerun()

        return view


# ==========================================
# 4. AUTHENTICATION MODULE
# ==========================================
def render_authentication():
    col_hero, col_form = st.columns([1.1, 0.9], gap="large")

    with col_hero:
        hero_html = """
<div class="opti-hero-card">
    <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 48px;">
        <div style="background-color: #38BDF8; width: 40px; height: 40px; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-weight: bold; color: #091322; font-size: 20px;">🗓️</div>
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
        Automated constraint scheduling, faculty preferences, attendance tracking, and room optimization — all in one platform.
    </p>
    <ul style="list-style: none; padding: 0; margin: 0; color: #CBD5E1; font-size: 15px; line-height: 2.4; font-weight: 500;">
        <li style="display: flex; align-items: center; gap: 10px;">🤖 AI CP-SAT Constraint Optimization</li>
        <li style="display: flex; align-items: center; gap: 10px;">📋 Conflict-free schedule generation</li>
        <li style="display: flex; align-items: center; gap: 10px;">🔑 Secure class code system</li>
        <li style="display: flex; align-items: center; gap: 10px;">📷 Photo-verified attendance</li>
    </ul>
</div>
"""
        st.markdown(hero_html, unsafe_allow_html=True)

    with col_form:
        st.markdown("<br/>", unsafe_allow_html=True)
        auth_mode = st.tabs(["Log In", "Sign Up"])
        
        with auth_mode[0]:
            st.markdown("<h2 style='margin-top:24px; font-size: 26px; font-weight: 800; color: #0F172A;'>Welcome back</h2>", unsafe_allow_html=True)
            st.markdown("<p style='color: #64748B; font-size: 14px; margin-bottom: 32px;'>Sign in to your OptiSked account</p>", unsafe_allow_html=True)
            
            username = st.text_input("Username", placeholder="your.username", key="login_user")
            password = st.text_input("Password", type="password", placeholder="••••••••", key="login_pass")
            
            st.markdown("<br/>", unsafe_allow_html=True)
            if st.button("Log In", use_container_width=True, type="primary"):
                conn = get_db_connection()
                user = conn.execute(
                    "SELECT * FROM users WHERE username = ? AND password = ?",
                    (username, hash_password(password))
                ).fetchone()
                conn.close()

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
            st.markdown("<p style='color: #64748B; font-size: 14px; margin-bottom: 24px;'>Register a new institutional profile</p>", unsafe_allow_html=True)
            
            new_fullname = st.text_input("Full Name", placeholder="e.g. Dr. Maria Santos", key="signup_name")
            new_username = st.text_input("Username", placeholder="msantos", key="signup_user")
            new_password = st.text_input("Password", type="password", placeholder="••••••••", key="signup_pass")
            new_role = st.selectbox("Select Role", ["Program Head", "Teacher", "Student"], key="signup_role")
            
            st.markdown("<br/>", unsafe_allow_html=True)
            if st.button("Sign Up", use_container_width=True, type="primary"):
                if new_fullname and new_username and new_password:
                    conn = get_db_connection()
                    try:
                        conn.execute(
                            "INSERT INTO users (username, password, full_name, role) VALUES (?, ?, ?, ?)",
                            (new_username, hash_password(new_password), new_fullname, new_role)
                        )
                        conn.commit()
                        st.success("Account created successfully! Please proceed to Log In.")
                    except sqlite3.IntegrityError:
                        st.error("Username already taken. Please choose another.")
                    finally:
                        conn.close()
                else:
                    st.warning("Please fill in all registration fields.")


# ==========================================
# 5. PROGRAM HEAD DASHBOARD MODULE
# ==========================================
def check_schedule_conflict(teacher_name, room, day, time_start, time_end, exclude_id=None):
    conn = get_db_connection()
    query = """
        SELECT * FROM schedules 
        WHERE day = ? 
        AND (time_start < ? AND time_end > ?)
        AND (teacher_name = ? OR room = ?)
    """
    params = [day, time_end, time_start, teacher_name, room]
    
    if exclude_id:
        query += " AND id != ?"
        params.append(exclude_id)
        
    conflicts = conn.execute(query, params).fetchall()
    conn.close()
    return conflicts

def render_program_head_dashboard():
    view = render_sidebar()

    if "🤖  AI Schedule Generator" in view:
        st.markdown("<h1 style='font-size: 28px; font-weight: 800; margin: 0;'>🤖 AI Constraint Optimization Engine</h1>", unsafe_allow_html=True)
        st.markdown("<p style='color: #64748B; font-size: 14px; margin-top: 4px;'>Automatically generate conflict-free schedules while maximizing teacher availability preferences.</p>", unsafe_allow_html=True)
        st.markdown("<br/>", unsafe_allow_html=True)

        col_setup, col_preview = st.columns([1, 1], gap="large")

        with col_setup:
            st.markdown("""
            <div class="sched-section-card">
                <h3>1. Available Physical Facilities</h3>
                <p style="color: #64748B; font-size: 13px;">Define rooms available for AI assignment:</p>
            </div>
            """, unsafe_allow_html=True)
            rooms_str = st.text_area("Rooms (comma-separated)", "Room 101, Room 102, Lab 1, Lab 2", height=80)
            rooms_list = [r.strip() for r in rooms_str.split(",") if r.strip()]

            st.markdown("<br/>", unsafe_allow_html=True)

            st.markdown("""
            <div class="sched-section-card">
                <h3>2. Unassigned Course Load Batch</h3>
                <p style="color: #64748B; font-size: 13px;">Add courses requiring automated scheduling:</p>
            </div>
            """, unsafe_allow_html=True)

            if "ai_course_queue" not in st.session_state:
                st.session_state["ai_course_queue"] = [
                    {"teacher_name": "Prof. Branzuela", "teacher_username": "jbranzuela", "subject_code": "CC101", "subject_name": "Programming 1", "program": "BS CS", "year_level": "1"},
                    {"teacher_name": "Prof. Branzuela", "teacher_username": "jbranzuela", "subject_code": "CC102", "subject_name": "Data Structures", "program": "BS CS", "year_level": "2"},
                    {"teacher_name": "Dr. Maria Santos", "teacher_username": "msantos", "subject_code": "IT201", "subject_name": "Database Systems", "program": "BS IT", "year_level": "2"}
                ]

            with st.form("add_course_batch"):
                conn = get_db_connection()
                teachers = conn.execute("SELECT username, full_name FROM users WHERE role = 'Teacher'").fetchall()
                conn.close()
                
                t_options = {t["full_name"]: t["username"] for t in teachers} if teachers else {"Prof. Branzuela": "jbranzuela"}

                c1, c2 = st.columns(2)
                with c1:
                    t_name = st.selectbox("Assign Faculty", list(t_options.keys()))
                    subj_c = st.text_input("Subject Code", "CS301")
                    subj_n = st.text_input("Subject Name", "Algorithms")
                with c2:
                    prog = st.selectbox("Program", ["BS CS", "BS IT", "BS ED"])
                    y_lvl = st.selectbox("Year Level", ["1", "2", "3", "4"])

                if st.form_submit_button("+ Add to Queue", type="primary"):
                    st.session_state["ai_course_queue"].append({
                        "teacher_name": t_name,
                        "teacher_username": t_options[t_name],
                        "subject_code": subj_c,
                        "subject_name": subj_n,
                        "program": prog,
                        "year_level": y_lvl
                    })
                    st.success("Added to course queue!")

        with col_preview:
            st.markdown("""
            <div class="sched-section-card">
                <h3>3. Course Queue ({})</h3>
            </div>
            """.format(len(st.session_state["ai_course_queue"])), unsafe_allow_html=True)

            for idx, q in enumerate(st.session_state["ai_course_queue"]):
                c_del, c_info = st.columns([0.15, 0.85])
                with c_del:
                    if st.button("❌", key=f"q_del_{idx}"):
                        st.session_state["ai_course_queue"].pop(idx)
                        st.rerun()
                with c_info:
                    st.markdown(f"**{q['subject_code']}** - {q['subject_name']} | **Faculty:** {q['teacher_name']} ({q['program']} {q['year_level']})")

            st.markdown("<br/>", unsafe_allow_html=True)
            
            if st.button("⚡ Run AI Schedule Solver", type="primary", use_container_width=True):
                if not st.session_state["ai_course_queue"]:
                    st.error("Please add courses to the queue first.")
                elif not rooms_list:
                    st.error("Please specify at least one room.")
                else:
                    conn = get_db_connection()
                    prefs_raw = conn.execute("SELECT * FROM teacher_preferences").fetchall()
                    conn.close()

                    teacher_prefs = {}
                    for p in prefs_raw:
                        teacher_prefs[p["teacher_username"]] = {
                            "days": p["preferred_days"].split(","),
                            "timeslot": p["preferred_timeslot"]
                        }

                    with st.spinner("AI Engine calculating optimal non-conflicting schedule..."):
                        gen_sched, status = solve_ai_schedule(
                            st.session_state["ai_course_queue"], 
                            rooms_list, 
                            teacher_prefs
                        )

                    if gen_sched:
                        st.session_state["preview_generated_schedule"] = gen_sched
                        st.success(f"AI Optimization Complete! Found {len(gen_sched)} conflict-free slots.")
                    else:
                        st.error(f"AI Solver Failed: {status}")

        if "preview_generated_schedule" in st.session_state and st.session_state["preview_generated_schedule"]:
            st.markdown("<br/><h3>Preview AI Suggested Schedule</h3>", unsafe_allow_html=True)
            
            preview_data = st.session_state["preview_generated_schedule"]
            st.dataframe(preview_data, use_container_width=True)

            if st.button("💾 Apply & Publish Generated Schedule to Database", type="primary"):
                conn = get_db_connection()
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
                conn.close()
                st.session_state["preview_generated_schedule"] = []
                st.session_state["ai_course_queue"] = []
                st.success("All AI generated schedules successfully saved and published!")
                st.rerun()

    elif "🗓️  Schedule Builder" in view:
        col_title, col_btn = st.columns([3, 1])
        with col_title:
            st.markdown("<h1 style='font-size: 28px; font-weight: 800; margin: 0;'>Schedule Builder</h1>", unsafe_allow_html=True)
            st.markdown("<p style='color: #64748B; font-size: 14px; margin-top: 4px;'>Add teacher information and generate conflict-free class schedules</p>", unsafe_allow_html=True)
        with col_btn:
            st.markdown("<br/>", unsafe_allow_html=True)
            show_add_modal = st.button("+ Add Schedule", type="primary", use_container_width=True)

        st.markdown("<br/>", unsafe_allow_html=True)

        if show_add_modal or st.session_state.get("show_add_form", False):
            st.session_state["show_add_form"] = True
            with st.expander("➕ Add New Schedule Entry", expanded=True):
                with st.form("add_schedule_form"):
                    c1, c2 = st.columns(2)
                    with c1:
                        teacher_name = st.text_input("Teacher Full Name (e.g. Prof. Branzuela)")
                        teacher_username = st.text_input("Teacher Username")
                        subject_code = st.text_input("Subject Code (e.g. CC101)")
                        subject_name = st.text_input("Subject Name (e.g. Programming)")
                        program = st.selectbox("Program", ["BS CS", "BS IT", "BS ED"])
                    with c2:
                        year_level = st.selectbox("Section / Year", ["1", "2", "3", "4"])
                        day = st.selectbox("Day", ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"])
                        t_start = st.time_input("Start Time", datetime.time(9, 0))
                        t_end = st.time_input("End Time", datetime.time(12, 0))
                        room = st.text_input("Room (e.g. Room 101)")

                    submit_sched = st.form_submit_button("Save & Generate Schedule", type="primary")

                    if submit_sched:
                        t_start_str = t_start.strftime("%I:%M %p")
                        t_end_str = t_end.strftime("%I:%M %p")

                        if not all([teacher_name, teacher_username, subject_code, subject_name, room]):
                            st.error("All fields are required.")
                        else:
                            conflicts = check_schedule_conflict(teacher_name, room, day, t_start_str, t_end_str)
                            if conflicts:
                                st.error("⚠️ Conflict Detected!")
                            else:
                                clean_subj = subject_code.replace(" ", "").upper()
                                clean_prof = "".join([w[0] for w in teacher_name.split()]).upper()
                                tch_code = f"TCH-{clean_prof}-{clean_subj}"
                                stu_code = f"STU-{program.replace(' ', '')}-{clean_subj}"

                                conn = get_db_connection()
                                conn.execute("""
                                    INSERT INTO schedules (
                                        teacher_name, teacher_username, subject_code, subject_name, 
                                        program, year_level, day, time_start, time_end, room, teacher_code, section_code
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """, (teacher_name, teacher_username, subject_code, subject_name, program, year_level, day, t_start_str, t_end_str, room, tch_code, stu_code))
                                conn.commit()
                                conn.close()
                                st.success("Schedule entry added successfully!")
                                st.session_state["show_add_form"] = False
                                st.rerun()

        conn = get_db_connection()
        schedules = conn.execute("SELECT * FROM schedules ORDER BY program, year_level, id").fetchall()
        conn.close()

        if schedules:
            sections = {}
            for s in schedules:
                sec_key = f"{s['program']} {s['year_level']}"
                if sec_key not in sections:
                    sections[sec_key] = []
                sections[sec_key].append(s)

            for sec_name, items in sections.items():
                st.markdown(f"""
                <div class="sched-section-card">
                    <div class="section-header">
                        <span class="section-title">{sec_name}</span>
                        <span class="class-badge">{len(items)} classes</span>
                    </div>
                </div>
                """, unsafe_allow_html=True)

                col1, col2, col3, col4, col5, col6, col7 = st.columns([2.2, 1, 2.2, 1.5, 2.5, 1.5, 0.5])
                with col1: st.markdown("<div class='grid-header'>TEACHER</div>", unsafe_allow_html=True)
                with col2: st.markdown("<div class='grid-header'>DAY</div>", unsafe_allow_html=True)
                with col3: st.markdown("<div class='grid-header'>TIME</div>", unsafe_allow_html=True)
                with col4: st.markdown("<div class='grid-header'>SUBJECT CODE</div>", unsafe_allow_html=True)
                with col5: st.markdown("<div class='grid-header'>SUBJECT NAME</div>", unsafe_allow_html=True)
                with col6: st.markdown("<div class='grid-header'>ROOM</div>", unsafe_allow_html=True)
                with col7: st.markdown("<div class='grid-header'></div>", unsafe_allow_html=True)

                for item in items:
                    r1, r2, r3, r4, r5, r6, r7 = st.columns([2.2, 1, 2.2, 1.5, 2.5, 1.5, 0.5])
                    with r1: st.markdown(f"**{item['teacher_name']}**")
                    with r2: st.write(item['day'])
                    with r3: st.write(f"{item['time_start']} - {item['time_end']}")
                    with r4: st.markdown(f"<span class='subject-pill'>{item['subject_code']}</span>", unsafe_allow_html=True)
                    with r5: st.write(item['subject_name'])
                    with r6: st.write(item['room'])
                    with r7:
                        if st.button("🗑️", key=f"del_{item['id']}"):
                            conn = get_db_connection()
                            conn.execute("DELETE FROM schedules WHERE id = ?", (item['id'],))
                            conn.commit()
                            conn.close()
                            st.rerun()
                st.markdown("<br/>", unsafe_allow_html=True)
        else:
            st.info("No master schedules generated yet. Click '+ Add Schedule' or use the '🤖 AI Schedule Generator'.")

    elif "🔑  Code Generation" in view:
        st.title("🔑 Role-Based Access Code Hub")
        st.caption("Distribute auto-generated access codes for teacher and student class enrollment.")

        conn = get_db_connection()
        schedules = conn.execute("SELECT * FROM schedules").fetchall()
        conn.close()

        col_tch, col_stu = st.columns(2)

        with col_tch:
            st.subheader("Teacher Access Codes")
            for s in schedules:
                st.markdown(f"""
                <div class="sched-section-card">
                    <b>{s['teacher_name']}</b> ({s['subject_code']})<br/>
                    <small style="color: #64748B;">{s['program']} {s['year_level']} | {s['day']} {s['time_start']}-{s['time_end']}</small><br/><br/>
                    Teacher Access Code: <span class="subject-pill" style="background-color: #0F172A; color: #38BDF8;">{s['teacher_code']}</span>
                </div>
                """, unsafe_allow_html=True)

        with col_stu:
            st.subheader("Student Section Access Codes")
            for s in schedules:
                st.markdown(f"""
                <div class="sched-section-card">
                    <b>Subject:</b> {s['subject_code']} - {s['subject_name']}<br/>
                    <small style="color: #64748B;">Faculty: {s['teacher_name']} | Room: {s['room']}</small><br/><br/>
                    Student Access Code: <span class="subject-pill" style="background-color: #10B981; color: white;">{s['section_code']}</span>
                </div>
                """, unsafe_allow_html=True)

    elif "📋  Attendance Records" in view:
        st.title("📋 Faculty Attendance & Leave Center")

        tab_leave, tab_att = st.tabs(["Pending Leave Applications", "Verified Teacher Photo Logs"])

        with tab_leave:
            conn = get_db_connection()
            pending_leaves = conn.execute("""
                SELECT l.id, l.teacher_username, l.leave_date, l.reason, s.subject_code, s.teacher_name 
                FROM leave_requests l
                JOIN schedules s ON l.schedule_id = s.id
                WHERE l.status = 'Pending'
            """).fetchall()
            conn.close()

            if pending_leaves:
                for l in pending_leaves:
                    st.warning(f"**Faculty:** {l['teacher_name']} (@{l['teacher_username']}) | **Subject:** {l['subject_code']} | **Date:** {l['leave_date']}")
                    st.write(f"**Reason:** {l['reason']}")
                    
                    c1, c2, c3 = st.columns([1, 1, 2])
                    with c1:
                        if st.button(f"✓ Approve (Class Suspended)", key=f"app_sus_{l['id']}"):
                            conn = get_db_connection()
                            conn.execute("UPDATE leave_requests SET status = 'Approved', has_activity = 0 WHERE id = ?", (l['id'],))
                            conn.commit()
                            conn.close()
                            st.rerun()
                    with c2:
                        if st.button(f"✓ Approve (With Async Task)", key=f"app_act_{l['id']}"):
                            conn = get_db_connection()
                            conn.execute("UPDATE leave_requests SET status = 'Approved', has_activity = 1 WHERE id = ?", (l['id'],))
                            conn.commit()
                            conn.close()
                            st.rerun()
                    with c3:
                        if st.button(f"✗ Reject Request", key=f"rej_{l['id']}"):
                            conn = get_db_connection()
                            conn.execute("UPDATE leave_requests SET status = 'Rejected' WHERE id = ?", (l['id'],))
                            conn.commit()
                            conn.close()
                            st.rerun()
                    st.write("---")
            else:
                st.info("No pending faculty leave applications.")

        with tab_att:
            conn = get_db_connection()
            logs = conn.execute("""
                SELECT a.date, a.photo_path, u.full_name, s.subject_code, s.room
                FROM attendance_logs a
                JOIN users u ON a.teacher_username = u.username
                JOIN schedules s ON a.schedule_id = s.id
                ORDER BY a.id DESC
            """).fetchall()
            conn.close()

            if logs:
                for log in logs:
                    col_img, col_info = st.columns([1, 3])
                    with col_img:
                        if os.path.exists(log["photo_path"]):
                            st.image(log["photo_path"], width=150)
                    with col_info:
                        st.markdown(f"**{log['full_name']}** — `{log['subject_code']}`")
                        st.write(f"Date: {log['date']} | Facility: {log['room']}")
                    st.write("---")
            else:
                st.info("No attendance verification logs recorded today.")

    elif "👨‍🏫  Teacher Directory" in view:
        st.title("👨‍🏫 Registered Faculty Directory")
        conn = get_db_connection()
        teachers = conn.execute("SELECT id, username, full_name FROM users WHERE role = 'Teacher'").fetchall()
        conn.close()
        
        if teachers:
            st.dataframe([dict(t) for t in teachers], use_container_width=True)
        else:
            st.info("No faculty profiles registered in system database.")


# ==========================================
# 6. TEACHER PORTAL MODULE (WITH PREFERENCES)
# ==========================================
def render_teacher_portal():
    with st.sidebar:
        st.markdown(f"### 👨‍🏫 {st.session_state['full_name']}")
        st.markdown("**Role:** Teacher")
        st.write("---")

        if st.button("Sign Out", use_container_width=True):
            st.session_state.clear()
            st.rerun()

    st.title("Teacher Workspace & AI Availability Preferences")

    # Teacher Schedule Preference Setting
    with st.expander("⭐ Set Availability Preferences (For AI Schedule Generator)", expanded=True):
        st.caption("The AI algorithm will prioritize scheduling your classes on your preferred days and time slots.")
        
        conn = get_db_connection()
        curr_pref = conn.execute("SELECT * FROM teacher_preferences WHERE teacher_username = ?", (st.session_state["username"],)).fetchone()
        conn.close()

        default_days = curr_pref["preferred_days"].split(",") if curr_pref else ["Mon", "Wed", "Fri"]
        default_slot = curr_pref["preferred_timeslot"] if curr_pref else "Morning"

        pref_days = st.multiselect("Preferred Teaching Days", DAYS, default=default_days)
        pref_slot = st.selectbox("Preferred Time Window", ["Morning", "Afternoon", "Evening", "Any"], index=["Morning", "Afternoon", "Evening", "Any"].index(default_slot))

        if st.button("Save AI Preferences", type="primary"):
            days_str = ",".join(pref_days)
            conn = get_db_connection()
            conn.execute("""
                INSERT INTO teacher_preferences (teacher_username, preferred_days, preferred_timeslot)
                VALUES (?, ?, ?)
                ON CONFLICT(teacher_username) DO UPDATE SET
                    preferred_days = excluded.preferred_days,
                    preferred_timeslot = excluded.preferred_timeslot
            """, (st.session_state["username"], days_str, pref_slot))
            conn.commit()
            conn.close()
            st.success("AI preferences saved! The Program Head will be able to optimize your schedule.")

    # Access Code Claim Section
    with st.expander("🔑 Claim Class Schedule via Access Code", expanded=False):
        code_input = st.text_input("Enter Teacher Access Code (e.g., TCH-...)").strip()
        if st.button("Link Schedule", type="primary"):
            conn = get_db_connection()
            sched = conn.execute("SELECT * FROM schedules WHERE teacher_code = ?", (code_input,)).fetchone()
            if sched:
                exists = conn.execute("SELECT * FROM teacher_claims WHERE teacher_username = ? AND schedule_id = ?",
                                      (st.session_state["username"], sched["id"])).fetchone()
                if not exists:
                    conn.execute("INSERT INTO teacher_claims (teacher_username, schedule_id) VALUES (?, ?)",
                                 (st.session_state["username"], sched["id"]))
                    conn.commit()
                    st.success(f"Successfully linked schedule: {sched['subject_code']} - {sched['subject_name']}")
                    conn.close()
                    st.rerun()
                else:
                    st.warning("You have already claimed this schedule.")
            else:
                st.error("Invalid Teacher Access Code.")
            conn.close()

    conn = get_db_connection()
    claimed = conn.execute("""
        SELECT s.* FROM schedules s
        JOIN teacher_claims tc ON s.id = tc.schedule_id
        WHERE tc.teacher_username = ?
    """, (st.session_state["username"],)).fetchall()
    conn.close()

    if claimed:
        for c in claimed:
            st.markdown(f"""
            <div class="sched-section-card">
                <h3>{c['subject_code']} - {c['subject_name']}</h3>
                <p style="color: #64748B;"><b>Program:</b> {c['program']} {c['year_level']} | <b>Schedule:</b> {c['day']} ({c['time_start']} - {c['time_end']}) | <b>Room:</b> {c['room']}</p>
            </div>
            """, unsafe_allow_html=True)

            col_a, col_b, col_c = st.columns(3)

            with col_a:
                st.subheader("📷 Photo Check-In")
                photo = st.camera_input(f"Verify Attendance ({c['subject_code']})", key=f"cam_{c['id']}")
                if photo:
                    today_str = datetime.date.today().strftime("%Y-%m-%d")
                    img_path = f"attendance_photos/{st.session_state['username']}_{c['id']}_{today_str}.png"
                    img = Image.open(photo)
                    img.save(img_path)

                    conn = get_db_connection()
                    conn.execute("""
                        INSERT INTO attendance_logs (teacher_username, schedule_id, date, photo_path)
                        VALUES (?, ?, ?, ?)
                    """, (st.session_state["username"], c["id"], today_str, img_path))
                    conn.commit()
                    conn.close()
                    st.success("Attendance verified & photo logged successfully!")

            with col_b:
                st.subheader("📝 Request Leave")
                with st.form(key=f"leave_form_{c['id']}"):
                    l_date = st.date_input("Leave Date", datetime.date.today())
                    l_reason = st.text_area("Reason for Absence")
                    sub_leave = st.form_submit_button("Submit Leave Application", type="primary")

                    if sub_leave:
                        conn = get_db_connection()
                        conn.execute("""
                            INSERT INTO leave_requests (teacher_username, schedule_id, leave_date, reason)
                            VALUES (?, ?, ?, ?)
                        """, (st.session_state["username"], c["id"], l_date.strftime("%Y-%m-%d"), l_reason))
                        conn.commit()
                        conn.close()
                        st.success("Leave submitted for Program Head approval.")

            with col_c:
                st.subheader("👥 Student Roster")
                conn = get_db_connection()
                enrolled_students = conn.execute("""
                    SELECT u.username, u.full_name FROM users u
                    JOIN student_claims sc ON u.username = sc.student_username
                    WHERE sc.schedule_id = ?
                """, (c["id"],)).fetchall()
                conn.close()

                if enrolled_students:
                    today_str = datetime.date.today().strftime("%Y-%m-%d")
                    for stu in enrolled_students:
                        st.write(f"**{stu['full_name']}**")
                        st_status = st.radio(
                            "Status", ["Present", "Absent", "Excused"], 
                            key=f"st_{c['id']}_{stu['username']}", horizontal=True
                        )
                        if st.button("Save", key=f"save_st_{c['id']}_{stu['username']}"):
                            conn = get_db_connection()
                            conn.execute("""
                                INSERT INTO student_attendance (schedule_id, student_username, date, status)
                                VALUES (?, ?, ?, ?)
                            """, (c["id"], stu["username"], today_str, st_status))
                            conn.commit()
                            conn.close()
                            st.toast(f"Marked {stu['full_name']} as {st_status}")
                else:
                    st.caption("No students enrolled via Section Code yet.")
            st.write("---")
    else:
        st.info("You haven't claimed any class schedules yet. Use your Teacher Code above.")


# ==========================================
# 7. STUDENT PORTAL MODULE
# ==========================================
def render_student_portal():
    with st.sidebar:
        st.markdown(f"### 🎓 {st.session_state['full_name']}")
        st.markdown("**Role:** Student")
        st.write("---")

        if st.button("Sign Out", use_container_width=True):
            st.session_state.clear()
            st.rerun()

    st.title("Student Class Portal")

    with st.expander("🔑 Enroll in Class via Section Code", expanded=True):
        sec_code = st.text_input("Enter Student Section Access Code (e.g., STU-...)").strip()
        if st.button("Add Class to Timetable", type="primary"):
            conn = get_db_connection()
            sched = conn.execute("SELECT * FROM schedules WHERE section_code = ?", (sec_code,)).fetchone()
            if sched:
                exists = conn.execute("SELECT * FROM student_claims WHERE student_username = ? AND schedule_id = ?",
                                      (st.session_state["username"], sched["id"])).fetchone()
                if not exists:
                    conn.execute("INSERT INTO student_claims (student_username, schedule_id) VALUES (?, ?)",
                                 (st.session_state["username"], sched["id"]))
                    conn.commit()
                    st.success(f"Successfully enrolled in {sched['subject_code']} - {sched['subject_name']}!")
                    conn.close()
                    st.rerun()
                else:
                    st.warning("You are already enrolled in this class.")
            else:
                st.error("Invalid Section Access Code.")
            conn.close()

    st.subheader("🗓️ Personal Timetable & Real-Time Faculty Attendance Status")

    conn = get_db_connection()
    my_classes = conn.execute("""
        SELECT s.* FROM schedules s
        JOIN student_claims sc ON s.id = sc.schedule_id
        WHERE sc.student_username = ?
    """, (st.session_state["username"],)).fetchall()
    
    today_str = datetime.date.today().strftime("%Y-%m-%d")

    if my_classes:
        for c in my_classes:
            leave = conn.execute("""
                SELECT * FROM leave_requests 
                WHERE schedule_id = ? AND leave_date = ? AND status = 'Approved'
            """, (c["id"], today_str)).fetchone()

            att = conn.execute("""
                SELECT * FROM attendance_logs 
                WHERE schedule_id = ? AND date = ?
            """, (c["id"], today_str)).fetchone()

            if leave:
                if leave["has_activity"]:
                    status_badge = '<span class="class-badge" style="background-color: #E0F2FE; color: #0369A1;">📘 Activity Assigned (Async)</span>'
                else:
                    status_badge = '<span class="class-badge" style="background-color: #FEE2E2; color: #991B1B;">🔴 Class Suspended / On Leave</span>'
            elif att:
                status_badge = '<span class="class-badge" style="background-color: #D1FAE5; color: #065F46;">🟢 Attending (Photo Verified)</span>'
            else:
                status_badge = '<span class="class-badge" style="background-color: #FEF3C7; color: #92400E;">🟡 Scheduled / Awaiting Check-in</span>'

            st.markdown(f"""
            <div class="sched-section-card">
                <div style="float: right;">{status_badge}</div>
                <h3 style="margin:0;">{c['subject_code']} - {c['subject_name']}</h3>
                <p style="color: #64748B; margin-top:5px;">
                    <b>Faculty:</b> {c['teacher_name']} | <b>Room:</b> {c['room']}<br/>
                    <b>Schedule:</b> {c['day']} ({c['time_start']} - {c['time_end']}) | <b>Program:</b> {c['program']} {c['year_level']}
                </p>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.info("No subjects added to your timetable. Enter your Section Code above to enroll.")

    conn.close()


# ==========================================
# 8. ROUTING CONTROLLER
# ==========================================
def main():
    st.set_page_config(
        page_title="OptiSked AI",
        page_icon="🗓️",
        layout="wide"
    )
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