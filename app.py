import os
import json
import random
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, g)
from questions import QUESTIONS

app = Flask(__name__)
app.secret_key = 'sn-quiz-secret-2024-xK9mP3wQ'

DB_PATH = os.path.join(os.path.dirname(__file__), 'quiz.db')

# ─────────────────────────────────────────────
# Database helpers
# ─────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db:
        db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS admin (
            username TEXT PRIMARY KEY,
            password TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS participants (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            email    TEXT UNIQUE NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS quiz_attempts (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_email  TEXT NOT NULL,
            started_at         TEXT,
            submitted_at       TEXT,
            score              INTEGER DEFAULT 0,
            is_submitted       INTEGER DEFAULT 0,
            question_order     TEXT,
            answer_order       TEXT,
            answers            TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS exam_status (
            id        INTEGER PRIMARY KEY,
            is_active INTEGER DEFAULT 0
        );
    """)
    # Seed defaults — INSERT OR REPLACE ensures password update takes effect
    db.execute("INSERT OR REPLACE INTO admin VALUES ('admin', 'manideep')")
    db.execute("INSERT OR IGNORE INTO exam_status VALUES (1, 0)")
    db.commit()
    db.close()

# ─────────────────────────────────────────────
# Auth decorators
# ─────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('participant_login'))
        return f(*args, **kwargs)
    return decorated

def participant_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('participant_email'):
            return redirect(url_for('participant_login'))
        return f(*args, **kwargs)
    return decorated

# ─────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────

def get_exam_active():
    db = get_db()
    row = db.execute("SELECT is_active FROM exam_status WHERE id=1").fetchone()
    return bool(row['is_active']) if row else False

def build_randomized_attempt(email):
    """Create shuffled question/option order and store in DB."""
    db = get_db()
    q_indices = list(range(len(QUESTIONS)))
    random.shuffle(q_indices)

    # For each question, shuffle option order
    option_orders = []
    for qi in q_indices:
        opt_order = list(range(4))
        random.shuffle(opt_order)
        option_orders.append(opt_order)

    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO quiz_attempts
           (participant_email, started_at, question_order, answer_order, answers)
           VALUES (?, ?, ?, ?, ?)""",
        (email, now, json.dumps(q_indices), json.dumps(option_orders), '{}')
    )
    db.commit()
    row = db.execute(
        "SELECT id FROM quiz_attempts WHERE participant_email=? ORDER BY id DESC LIMIT 1",
        (email,)
    ).fetchone()
    return row['id']

def compute_score(attempt_id):
    db = get_db()
    row = db.execute("SELECT * FROM quiz_attempts WHERE id=?", (attempt_id,)).fetchone()
    if not row:
        return 0
    q_order = json.loads(row['question_order'])
    a_order = json.loads(row['answer_order'])
    answers = json.loads(row['answers'])
    score = 0
    for pos, qi in enumerate(q_order):
        q = QUESTIONS[qi]
        opt_order = a_order[pos]
        user_choice = answers.get(str(pos))
        if user_choice is not None:
            user_choice = int(user_choice)
            # user_choice is the index in the SHUFFLED options list
            # opt_order[user_choice] gives the original option index
            if opt_order[user_choice] == q['answer']:
                score += 1
    return score

# ─────────────────────────────────────────────
# Admin routes
# ─────────────────────────────────────────────

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """No longer used — admin logs in from the main login page."""
    return redirect(url_for('participant_login'))

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('participant_login'))

@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    db = get_db()
    participants = db.execute("SELECT * FROM participants ORDER BY created_at DESC").fetchall()
    exam_active  = get_exam_active()
    scores = db.execute(
        """SELECT participant_email, score, submitted_at, is_submitted
           FROM quiz_attempts WHERE is_submitted=1
           ORDER BY score DESC"""
    ).fetchall()
    total_q = len(QUESTIONS)
    return render_template('admin_dashboard.html',
                           participants=participants,
                           exam_active=exam_active,
                           scores=scores,
                           total_q=total_q)

@app.route('/admin/toggle-exam', methods=['POST'])
@admin_required
def toggle_exam():
    db = get_db()
    current = get_exam_active()
    db.execute("UPDATE exam_status SET is_active=? WHERE id=1", (0 if current else 1,))
    db.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/add-participant', methods=['POST'])
@admin_required
def add_participant():
    email = request.form.get('email', '').strip().lower()
    if email:
        db = get_db()
        try:
            db.execute("INSERT INTO participants (email) VALUES (?)", (email,))
            db.commit()
        except sqlite3.IntegrityError:
            pass  # duplicate – ignore silently
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete-participant', methods=['POST'])
@admin_required
def delete_participant():
    email = request.form.get('email', '').strip().lower()
    if email:
        db = get_db()
        db.execute("DELETE FROM participants WHERE email=?", (email,))
        db.commit()
    return redirect(url_for('admin_dashboard'))

# ─────────────────────────────────────────────
# Participant routes
# ─────────────────────────────────────────────

@app.route('/', methods=['GET', 'POST'])
def participant_login():
    error = None
    if request.method == 'POST':
        username = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        db = get_db()

        # ── Admin check (username + password, case-sensitive) ──
        admin_row = db.execute(
            "SELECT * FROM admin WHERE username=? AND password=?", (username, password)
        ).fetchone()
        if admin_row:
            session['admin_logged_in'] = True
            return redirect(url_for('admin_dashboard'))

        # ── Participant check (email == password) ──
        email = username.lower()
        pwd   = password.lower()
        if email and email == pwd:
            row = db.execute("SELECT * FROM participants WHERE email=?", (email,)).fetchone()
            if row:
                if not get_exam_active():
                    error = 'The exam is not active yet. Please wait for the admin to start it.'
                else:
                    session['participant_email'] = email
                    attempt = db.execute(
                        "SELECT * FROM quiz_attempts WHERE participant_email=? AND is_submitted=1",
                        (email,)
                    ).fetchone()
                    if attempt:
                        return redirect(url_for('result'))
                    return redirect(url_for('instructions'))
            else:
                error = 'Email not registered. Contact your administrator.'
        else:
            error = 'Invalid credentials. Use your email as both username and password.'
    return render_template('participant_login.html', error=error)

@app.route('/instructions')
@participant_required
def instructions():
    email = session['participant_email']
    db = get_db()
    # Already submitted?
    attempt = db.execute(
        "SELECT * FROM quiz_attempts WHERE participant_email=? AND is_submitted=1", (email,)
    ).fetchone()
    if attempt:
        return redirect(url_for('result'))
    return render_template('instructions.html')

@app.route('/quiz')
@participant_required
def quiz():
    email = session['participant_email']
    db = get_db()

    if not get_exam_active():
        return redirect(url_for('participant_login'))

    # Already submitted?
    attempt = db.execute(
        "SELECT * FROM quiz_attempts WHERE participant_email=? AND is_submitted=1", (email,)
    ).fetchone()
    if attempt:
        return redirect(url_for('result'))

    # Get or create in-progress attempt
    attempt = db.execute(
        "SELECT * FROM quiz_attempts WHERE participant_email=? AND is_submitted=0", (email,)
    ).fetchone()
    if not attempt:
        attempt_id = build_randomized_attempt(email)
        attempt = db.execute("SELECT * FROM quiz_attempts WHERE id=?", (attempt_id,)).fetchone()

    q_order   = json.loads(attempt['question_order'])
    a_order   = json.loads(attempt['answer_order'])
    answers   = json.loads(attempt['answers'])

    # Section labels by original question index (0-based)
    SECTIONS = (['Basics'] * 20 + ['Scripting'] * 20 +
                ['UI & Forms'] * 20 + ['Integration & API'] * 20 +
                ['Advanced'] * 40)

    # Build question list for the template
    questions_for_template = []
    for pos, qi in enumerate(q_order):
        q = QUESTIONS[qi]
        opt_order = a_order[pos]
        shuffled_opts = [q['options'][i] for i in opt_order]
        questions_for_template.append({
            'pos': pos,
            'text': q['question'],
            'options': shuffled_opts,
            'selected': answers.get(str(pos)),
            'section': SECTIONS[qi]   # derived from ORIGINAL question index
        })

    return render_template('quiz.html',
                           questions=questions_for_template,
                           attempt_id=attempt['id'],
                           total=len(QUESTIONS))

@app.route('/api/save-answer', methods=['POST'])
@participant_required
def save_answer():
    data = request.get_json(silent=True) or {}
    pos    = data.get('pos')
    choice = data.get('choice')
    email  = session['participant_email']
    db = get_db()
    attempt = db.execute(
        "SELECT * FROM quiz_attempts WHERE participant_email=? AND is_submitted=0", (email,)
    ).fetchone()
    if attempt:
        answers = json.loads(attempt['answers'])
        answers[str(pos)] = choice
        db.execute("UPDATE quiz_attempts SET answers=? WHERE id=?",
                   (json.dumps(answers), attempt['id']))
        db.commit()
    return jsonify({'ok': True})

@app.route('/submit', methods=['POST'])
@participant_required
def submit_quiz():
    email = session['participant_email']
    db = get_db()
    attempt = db.execute(
        "SELECT * FROM quiz_attempts WHERE participant_email=? AND is_submitted=0", (email,)
    ).fetchone()
    if attempt:
        # If answers sent in body (beacon)
        try:
            body = request.get_json(silent=True) or {}
            if 'answers' in body:
                answers = body['answers']
                db.execute("UPDATE quiz_attempts SET answers=? WHERE id=?",
                           (json.dumps(answers), attempt['id']))
                db.commit()
                attempt = db.execute("SELECT * FROM quiz_attempts WHERE id=?",
                                     (attempt['id'],)).fetchone()
        except Exception:
            pass

        score = compute_score(attempt['id'])
        now   = datetime.utcnow().isoformat()
        db.execute(
            "UPDATE quiz_attempts SET is_submitted=1, submitted_at=?, score=? WHERE id=?",
            (now, score, attempt['id'])
        )
        db.commit()
    return redirect(url_for('result'))

@app.route('/api/submit-beacon', methods=['POST'])
def submit_beacon():
    """Called by sendBeacon on page unload."""
    email = session.get('participant_email')
    if not email:
        return '', 204
    db = get_db()
    attempt = db.execute(
        "SELECT * FROM quiz_attempts WHERE participant_email=? AND is_submitted=0", (email,)
    ).fetchone()
    if attempt:
        try:
            body = request.get_data(as_text=True)
            if body:
                data = json.loads(body)
                answers = data.get('answers', {})
                db.execute("UPDATE quiz_attempts SET answers=? WHERE id=?",
                           (json.dumps(answers), attempt['id']))
                db.commit()
                attempt = db.execute("SELECT * FROM quiz_attempts WHERE id=?",
                                     (attempt['id'],)).fetchone()
        except Exception:
            pass

        score = compute_score(attempt['id'])
        now   = datetime.utcnow().isoformat()
        db.execute(
            "UPDATE quiz_attempts SET is_submitted=1, submitted_at=?, score=? WHERE id=?",
            (now, score, attempt['id'])
        )
        db.commit()
    return '', 204

@app.route('/result')
@participant_required
def result():
    email = session['participant_email']
    db = get_db()
    attempt = db.execute(
        "SELECT * FROM quiz_attempts WHERE participant_email=? AND is_submitted=1", (email,)
    ).fetchone()
    if not attempt:
        # Not yet submitted — redirect to quiz
        return redirect(url_for('quiz'))
    return render_template('result.html',
                           score=attempt['score'],
                           total=len(QUESTIONS),
                           email=email)

@app.route('/logout')
def participant_logout():
    session.pop('participant_email', None)
    return redirect(url_for('participant_login'))

@app.route('/api/exam-status')
def api_exam_status():
    return jsonify({'active': get_exam_active()})

# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)
