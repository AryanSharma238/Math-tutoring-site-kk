import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from functools import wraps
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from flask import (
    Flask, abort, flash, redirect, render_template, request,
    send_file, session, url_for,
)
from io import BytesIO
from supabase import create_client

from models import ClassSession, CurriculumFile, Quiz, StudentProfile, TodoItem, User, db

GITHUB_REPO = "AryanSharma238/Math-tutoring-site-kk"

# In-memory quiz-generation job store. Generation runs in a background thread so the
# HTTP request that kicks it off returns instantly -- this avoids Render's platform
# request timeout killing a long-running OpenRouter call.
_quiz_jobs = {}
_quiz_jobs_lock = threading.Lock()


def _ensure_sslmode(db_url):
    parts = urlsplit(db_url)
    query = dict(parse_qsl(parts.query))
    query.setdefault("sslmode", "require")
    return urlunsplit(parts._replace(query=urlencode(query)))

class SupabaseNotConfigured(Exception):
    pass


_supabase_client = None


def get_supabase():
    global _supabase_client
    if _supabase_client is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_ANON_KEY")
        if not url or not key:
            raise SupabaseNotConfigured(
                "SUPABASE_URL / SUPABASE_ANON_KEY are not set on the server."
            )
        _supabase_client = create_client(url, key)
    return _supabase_client

COMMON_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Anchorage", "Pacific/Honolulu", "America/Sao_Paulo",
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Moscow",
    "Africa/Cairo", "Africa/Johannesburg",
    "Asia/Dubai", "Asia/Kolkata", "Asia/Shanghai", "Asia/Tokyo", "Asia/Singapore",
    "Australia/Sydney", "Pacific/Auckland", "UTC",
]

FREE_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "poolside/laguna-s-2.1:free",
    "cohere/north-mini-code:free",
]

QUIZ_SYSTEM_PROMPT = """You are a math problem generator. Given a topic/prompt, generate exactly {count} distinct multiple-choice math problems matching it, plus a short descriptive title for the quiz as a whole.

Each question must have exactly 4 answer choices, exactly one of which is correct.
Each question must include a detailed step-by-step solution.
Each incorrect choice must include an explanation of the specific mistake or misconception that leads to it.
Verify all numbers and answer choices are mathematically correct and consistent before outputting.

The "question" field must contain ONLY the question text -- never embed the answer choices inside it.
The "title" field should be a short, specific, human-readable name for the quiz (4-8 words), based on the topic -- e.g. "Trigonometric Identities Practice" or "Quadratic Formula Word Problems". Do not just repeat the raw topic text verbatim.
Do not include any internal reasoning, revisions, second-guessing, notes, or commentary anywhere in the output, including inside string fields. Do not use markdown code fences.
Return ONLY a single valid JSON object with exactly this shape:

{{
  "title": "string",
  "questions": [
    {{
      "question": "string",
      "choices": [
        {{"label": "A", "text": "string", "correct": true, "explanation": ""}},
        {{"label": "B", "text": "string", "correct": false, "explanation": "why this is wrong"}},
        {{"label": "C", "text": "string", "correct": false, "explanation": "why this is wrong"}},
        {{"label": "D", "text": "string", "correct": false, "explanation": "why this is wrong"}}
      ],
      "solution": "detailed step-by-step solution string"
    }}
  ]
}}

Exactly one choice per question must have "correct": true; the rest must be "correct": false with a non-empty "explanation". The correct choice's "explanation" should be an empty string."""


def _extract_json_object(raw):
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("The AI didn't return any JSON. Please try again.")
    return json.loads(cleaned[start:end + 1])


def _validate_quiz_payload(parsed, topic):
    if not isinstance(parsed, dict) or not isinstance(parsed.get("questions"), list) or not parsed["questions"]:
        raise ValueError("The AI response was missing its question list. Please try again.")

    for i, q in enumerate(parsed["questions"], start=1):
        if not isinstance(q, dict) or not q.get("question") or not q.get("solution"):
            raise ValueError(f"Question {i} is missing text or a solution. Please try again.")
        choices = q.get("choices")
        if not isinstance(choices, list) or len(choices) < 2:
            raise ValueError(f"Question {i} is missing answer choices. Please try again.")
        for c in choices:
            if not isinstance(c, dict) or "text" not in c or "correct" not in c:
                raise ValueError(f"Question {i} has a malformed answer choice. Please try again.")
        correct_count = sum(1 for c in choices if c.get("correct"))
        if correct_count != 1:
            raise ValueError(f"Question {i} doesn't have exactly one correct answer. Please try again.")

    title = (parsed.get("title") or "").strip() or (topic[:100] if topic else "Untitled quiz")
    return title, parsed["questions"]


def _generate_quiz(topic, model, count):
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("Server is missing OPENROUTER_API_KEY.")

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": QUIZ_SYSTEM_PROMPT.format(count=count)},
                    {"role": "user", "content": topic or "general math problems, mixed topics"},
                ],
            },
            timeout=280,
        )
    except requests.exceptions.Timeout:
        raise RuntimeError("The AI model took too long to respond. Try again, or pick a faster model / fewer questions.")
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Could not reach OpenRouter: {exc}")

    if not resp.ok:
        raise RuntimeError(f"OpenRouter returned an error (HTTP {resp.status_code}). Try again in a moment.")

    try:
        body = resp.json()
    except ValueError:
        raise RuntimeError("OpenRouter returned an unexpected (non-JSON) response. Try again.")

    if not body.get("choices"):
        raise RuntimeError("OpenRouter's response didn't include any content -- the model may be overloaded. Try again.")

    raw = body["choices"][0].get("message", {}).get("content") or ""
    parsed = _extract_json_object(raw)
    title, questions = _validate_quiz_payload(parsed, topic)
    return {"title": title, "model": model, "questions": questions}


def _run_quiz_generation_job(job_id, topic, model, count):
    try:
        result = _generate_quiz(topic, model, count)
        with _quiz_jobs_lock:
            _quiz_jobs[job_id] = {"status": "done", "result": result}
    except Exception as exc:
        with _quiz_jobs_lock:
            _quiz_jobs[job_id] = {"status": "error", "error": str(exc)}


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    db_url = os.environ.get("DATABASE_URL", "sqlite:///local.db")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    if db_url.startswith("postgresql://"):
        db_url = _ensure_sslmode(db_url)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    with app.app_context():
        db.create_all()

    register_routes(app)
    return app


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or not user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return wrapper


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)


def register_routes(app):
    @app.route("/")
    def home():
        return render_template("home.html")

    @app.route("/login")
    def login():
        if "user_id" in session:
            return redirect(url_for("dashboard"))
        return render_template("login.html")

    @app.route("/auth/signup", methods=["POST"])
    def auth_signup():
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        name = request.form.get("name", "").strip()

        if not email or "@" not in email:
            flash("Please enter a valid email.")
            return redirect(url_for("login"))
        if len(password) < 8:
            flash("Password must be at least 8 characters.")
            return redirect(url_for("login"))

        try:
            result = get_supabase().auth.sign_up({"email": email, "password": password})
        except Exception as exc:
            flash(f"Could not create account: {exc}")
            return redirect(url_for("login"))

        if not result.user:
            flash("Could not create account. Please try again.")
            return redirect(url_for("login"))

        if not result.session:
            flash(
                "Account created! Check your email to confirm it, then sign in. "
                "(If you're the admin setting this up, you can disable email confirmation "
                "in Supabase: Authentication -> Providers -> Email.)"
            )
            return redirect(url_for("login"))

        _log_in_local_user(result.user.id, email, name)
        return redirect(url_for("dashboard"))

    @app.route("/auth/login", methods=["POST"])
    def auth_login():
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Please enter your email and password.")
            return redirect(url_for("login"))

        try:
            result = get_supabase().auth.sign_in_with_password(
                {"email": email, "password": password}
            )
        except SupabaseNotConfigured as exc:
            flash(str(exc))
            return redirect(url_for("login"))
        except Exception:
            flash("Incorrect email or password.")
            return redirect(url_for("login"))

        if not result.user:
            flash("Incorrect email or password.")
            return redirect(url_for("login"))

        _log_in_local_user(result.user.id, email, None)
        return redirect(url_for("dashboard"))

    def _log_in_local_user(supabase_uid, email, name):
        user = User.query.filter_by(supabase_uid=supabase_uid).first()
        if not user:
            admin_emails = {
                e.strip().lower()
                for e in os.environ.get("ADMIN_EMAIL", "").split(",")
                if e.strip()
            }
            is_admin = email in admin_emails
            user = User(
                supabase_uid=supabase_uid, email=email, name=name or None,
                is_admin=is_admin,
            )
            db.session.add(user)
            db.session.commit()
            if not is_admin:
                profile = StudentProfile(user_id=user.id)
                db.session.add(profile)
                db.session.commit()

        session["user_id"] = user.id

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("home"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        user = current_user()
        if user.is_admin:
            students = User.query.filter_by(is_admin=False).order_by(User.created_at).all()
            return render_template("admin_dashboard.html", user=user, students=students)

        profile = user.profile
        if not profile or not profile.setup_complete:
            return render_template("waiting.html", user=user)

        return render_template(
            "student_dashboard.html", user=user, profile=profile,
            next_class=profile.next_class, active="dashboard",
        )

    @app.route("/quizzes")
    @login_required
    def quizzes():
        user = current_user()
        if user.is_admin:
            return redirect(url_for("dashboard"))
        profile = user.profile
        if not profile or not profile.setup_complete:
            return render_template("waiting.html", user=user)

        all_quizzes = profile.quizzes
        recently_assigned = all_quizzes[0] if all_quizzes else None
        todo = [q for q in all_quizzes if not q.completed_at and q.id != (recently_assigned.id if recently_assigned else None)]
        completed = sorted(
            (q for q in all_quizzes if q.completed_at), key=lambda q: q.completed_at, reverse=True
        )
        return render_template(
            "quizzes.html", user=user, profile=profile,
            recently_assigned=recently_assigned, todo=todo, completed=completed,
            active="quizzes",
        )

    @app.route("/quizzes/<int:quiz_id>")
    @login_required
    def take_quiz(quiz_id):
        user = current_user()
        quiz = Quiz.query.get_or_404(quiz_id)
        if user.is_admin or quiz.profile_id != user.profile.id:
            abort(403)
        questions = json.loads(quiz.questions_json)
        try:
            saved_answers = json.loads(quiz.answers_json) if quiz.answers_json else {}
        except ValueError:
            saved_answers = {}
        return render_template(
            "take_quiz.html", user=user, quiz=quiz, questions=questions,
            saved_answers=saved_answers, active="quizzes",
        )

    @app.route("/quizzes/<int:quiz_id>/answer", methods=["POST"])
    @login_required
    def save_quiz_answer(quiz_id):
        user = current_user()
        quiz = Quiz.query.get_or_404(quiz_id)
        if user.is_admin or quiz.profile_id != user.profile.id:
            abort(403)

        data = request.get_json(silent=True) or {}
        try:
            q_index = str(int(data.get("question_index")))
            choice_index = int(data.get("choice_index"))
        except (TypeError, ValueError):
            return {"error": "Invalid answer payload."}, 400
        submitted = bool(data.get("submitted"))

        try:
            answers = json.loads(quiz.answers_json) if quiz.answers_json else {}
        except ValueError:
            answers = {}
        answers[q_index] = {"choice": choice_index, "submitted": submitted}
        quiz.answers_json = json.dumps(answers)

        total_questions = len(json.loads(quiz.questions_json))
        all_submitted = (
            len(answers) == total_questions
            and all(a.get("submitted") for a in answers.values())
        )
        if all_submitted and not quiz.completed_at:
            quiz.completed_at = datetime.now(dt_timezone.utc)

        db.session.commit()
        return {"ok": True, "completed": bool(quiz.completed_at)}

    @app.route("/settings")
    @login_required
    def settings():
        user = current_user()
        return render_template("settings.html", user=user, active="settings")

    @app.route("/logs")
    @login_required
    def logs():
        user = current_user()
        commits = []
        error = None
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/commits",
                params={"per_page": 30},
                headers={"Accept": "application/vnd.github+json"},
                timeout=10,
            )
            resp.raise_for_status()
            for c in resp.json():
                commits.append({
                    "sha": c["sha"][:7],
                    "message": c["commit"]["message"].split("\n")[0],
                    "author": c["commit"]["author"]["name"],
                    "date": c["commit"]["author"]["date"],
                    "url": c["html_url"],
                })
        except Exception as exc:
            error = f"Could not load GitHub commits: {exc}"

        todos = TodoItem.query.order_by(TodoItem.created_at).all()
        return render_template(
            "logs.html", user=user, active="settings",
            commits=commits, error=error, todos=todos, repo=GITHUB_REPO,
        )

    @app.route("/logs/todo/add", methods=["POST"])
    @login_required
    def add_todo():
        text = request.form.get("text", "").strip()
        if text:
            db.session.add(TodoItem(text=text[:500]))
            db.session.commit()
        return redirect(url_for("logs"))

    @app.route("/logs/todo/<int:todo_id>/complete", methods=["POST"])
    @login_required
    def complete_todo(todo_id):
        item = TodoItem.query.get_or_404(todo_id)
        db.session.delete(item)
        db.session.commit()
        return redirect(url_for("logs"))

    @app.route("/account/delete", methods=["POST"])
    @login_required
    def delete_account():
        user = current_user()
        db.session.delete(user)
        db.session.commit()
        session.clear()
        return redirect(url_for("login"))

    # --- Admin: manage a specific student ---

    @app.route("/admin/student/<int:user_id>")
    @login_required
    @admin_required
    def admin_student(user_id):
        student = User.query.filter_by(id=user_id, is_admin=False).first_or_404()
        admin = current_user()
        students = User.query.filter_by(is_admin=False).order_by(User.created_at).all()
        return render_template(
            "admin_student.html", user=admin, students=students, student=student,
            profile=student.profile, timezones=COMMON_TIMEZONES, models=FREE_MODELS,
        )

    @app.route("/admin/student/<int:user_id>/update", methods=["POST"])
    @login_required
    @admin_required
    def admin_student_update(user_id):
        student = User.query.filter_by(id=user_id, is_admin=False).first_or_404()
        profile = student.profile

        student.name = request.form.get("student_name", "").strip() or None
        profile.course_name = request.form.get("course_name", "").strip() or None
        try:
            profile.total_classes = max(int(request.form.get("total_classes", 0)), 0)
        except ValueError:
            profile.total_classes = 0
        profile.timezone = request.form.get("timezone") or profile.timezone
        if not profile.setup_complete:
            profile.classes_left = profile.total_classes
        profile.setup_complete = True

        db.session.commit()
        flash("Student profile updated.")
        return redirect(url_for("admin_student", user_id=user_id))

    @app.route("/admin/student/<int:user_id>/classes_left", methods=["POST"])
    @login_required
    @admin_required
    def admin_student_set_classes_left(user_id):
        student = User.query.filter_by(id=user_id, is_admin=False).first_or_404()
        try:
            student.profile.classes_left = max(int(request.form.get("classes_left", 0)), 0)
        except ValueError:
            pass
        db.session.commit()
        flash("Classes left updated.")
        return redirect(url_for("admin_student", user_id=user_id))

    @app.route("/admin/student/<int:user_id>/curriculum", methods=["POST"])
    @login_required
    @admin_required
    def admin_student_curriculum(user_id):
        student = User.query.filter_by(id=user_id, is_admin=False).first_or_404()
        file = request.files.get("curriculum")
        if file and file.filename:
            allowed = {"pdf", "jpg", "jpeg", "png"}
            ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
            if ext not in allowed:
                flash("Only PDF, JPG, JPEG, or PNG files are allowed.")
                return redirect(url_for("admin_student", user_id=user_id))

            for old in list(student.profile.curriculum_files):
                db.session.delete(old)

            record = CurriculumFile(
                profile_id=student.profile.id,
                filename=file.filename,
                mimetype=file.mimetype,
                data=file.read(),
            )
            db.session.add(record)
            db.session.commit()
            flash("Curriculum uploaded.")
        return redirect(url_for("admin_student", user_id=user_id))

    @app.route("/admin/student/<int:user_id>/classes", methods=["POST"])
    @login_required
    @admin_required
    def admin_student_add_class(user_id):
        student = User.query.filter_by(id=user_id, is_admin=False).first_or_404()
        date_str = request.form.get("class_date")
        time_str = request.form.get("class_time")
        tz_name = request.form.get("class_timezone") or student.profile.timezone
        try:
            repeat_weeks = max(min(int(request.form.get("repeat_weeks", 1)), 52), 1)
        except (ValueError, TypeError):
            repeat_weeks = 1

        try:
            from zoneinfo import ZoneInfo
            naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            local_dt = naive.replace(tzinfo=ZoneInfo(tz_name))
            utc_dt = local_dt.astimezone(dt_timezone.utc).replace(tzinfo=None)
        except Exception:
            flash("Invalid date/time/timezone.")
            return redirect(url_for("admin_student", user_id=user_id))

        for week in range(repeat_weeks):
            db.session.add(ClassSession(
                profile_id=student.profile.id,
                start_at=utc_dt + timedelta(weeks=week),
            ))
        db.session.commit()
        flash(f"Scheduled {repeat_weeks} class(es)." if repeat_weeks > 1 else "Class scheduled.")
        return redirect(url_for("admin_student", user_id=user_id))

    @app.route("/admin/student/<int:user_id>/classes/<int:class_id>/delete", methods=["POST"])
    @login_required
    @admin_required
    def admin_student_delete_class(user_id, class_id):
        student = User.query.filter_by(id=user_id, is_admin=False).first_or_404()
        cls = ClassSession.query.filter_by(id=class_id, profile_id=student.profile.id).first_or_404()
        db.session.delete(cls)
        db.session.commit()
        return redirect(url_for("admin_student", user_id=user_id))

    @app.route("/admin/student/<int:user_id>/quiz/generate/start", methods=["POST"])
    @login_required
    @admin_required
    def admin_student_quiz_generate_start(user_id):
        User.query.filter_by(id=user_id, is_admin=False).first_or_404()
        data = request.get_json(silent=True) or {}
        topic = (data.get("topic") or "").strip()
        model = data.get("model") or FREE_MODELS[0]
        try:
            count = max(min(int(data.get("count", 5)), 25), 1)
        except (ValueError, TypeError):
            count = 5

        if not os.environ.get("OPENROUTER_API_KEY"):
            return {"error": "Server is missing OPENROUTER_API_KEY -- ask the admin to set it in Render."}, 400

        job_id = uuid.uuid4().hex
        with _quiz_jobs_lock:
            _quiz_jobs[job_id] = {"status": "pending"}

        thread = threading.Thread(
            target=_run_quiz_generation_job, args=(job_id, topic, model, count), daemon=True,
        )
        thread.start()
        return {"job_id": job_id}

    @app.route("/admin/student/<int:user_id>/quiz/generate/status/<job_id>")
    @login_required
    @admin_required
    def admin_student_quiz_generate_status(user_id, job_id):
        User.query.filter_by(id=user_id, is_admin=False).first_or_404()
        with _quiz_jobs_lock:
            job = _quiz_jobs.get(job_id)
        if not job:
            return {"status": "error", "error": "That generation job could not be found (it may have expired)."}, 404
        return job

    @app.route("/admin/student/<int:user_id>/quiz/assign", methods=["POST"])
    @login_required
    @admin_required
    def admin_student_quiz_assign(user_id):
        student = User.query.filter_by(id=user_id, is_admin=False).first_or_404()
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "Untitled quiz")[:100]
        model = data.get("model") or FREE_MODELS[0]
        questions = data.get("questions")

        if not isinstance(questions, list) or not questions:
            return {"error": "No questions to assign."}, 400

        quiz = Quiz(
            profile_id=student.profile.id,
            title=title,
            questions_json=json.dumps(questions),
            model_used=model,
        )
        db.session.add(quiz)
        db.session.commit()
        return {"ok": True, "quiz_id": quiz.id, "title": quiz.title}

    @app.route("/admin/student/<int:user_id>/quiz/<int:quiz_id>/delete", methods=["POST"])
    @login_required
    @admin_required
    def admin_student_quiz_delete(user_id, quiz_id):
        student = User.query.filter_by(id=user_id, is_admin=False).first_or_404()
        quiz = Quiz.query.filter_by(id=quiz_id, profile_id=student.profile.id).first_or_404()
        db.session.delete(quiz)
        db.session.commit()
        flash("Quiz removed.")
        return redirect(url_for("admin_student", user_id=user_id))

    # --- Curriculum file serving ---

    @app.route("/curriculum/<int:file_id>")
    @login_required
    def serve_curriculum(file_id):
        record = CurriculumFile.query.get_or_404(file_id)
        user = current_user()
        if not user.is_admin and (not user.profile or user.profile.id != record.profile_id):
            abort(403)
        return send_file(BytesIO(record.data), mimetype=record.mimetype, download_name=record.filename)


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
