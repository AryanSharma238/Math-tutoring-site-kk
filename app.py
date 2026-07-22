import json
import os
from datetime import datetime, timezone as dt_timezone
from functools import wraps

import requests
from flask import (
    Flask, abort, flash, redirect, render_template, request,
    send_file, session, url_for,
)
from io import BytesIO

from models import ClassSession, CurriculumFile, Quiz, StudentProfile, User, db

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

QUIZ_SYSTEM_PROMPT = """You are a math problem generator. Given a topic/prompt, generate exactly {count} distinct multiple-choice math problems matching it.

Each question must have exactly 4 answer choices, exactly one of which is correct.
Each question must include a detailed step-by-step solution.
Each incorrect choice must include an explanation of the specific mistake or misconception that leads to it.
Verify all numbers and answer choices are mathematically correct and consistent before outputting.

The "question" field must contain ONLY the question text -- never embed the answer choices inside it.
Do not include any internal reasoning, revisions, second-guessing, notes, or commentary anywhere in the output, including inside string fields. Do not use markdown code fences.
Return ONLY a valid JSON array, where each element has exactly this shape:

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

Exactly one choice per question must have "correct": true; the rest must be "correct": false with a non-empty "explanation". The correct choice's "explanation" should be an empty string."""


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    db_url = os.environ.get("DATABASE_URL", "sqlite:///local.db")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
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
            return redirect(url_for("landing"))
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
    def landing():
        if "user_id" in session:
            return redirect(url_for("dashboard"))
        return render_template("landing.html")

    @app.route("/auth", methods=["POST"])
    def auth():
        email = request.form.get("email", "").strip().lower()
        name = request.form.get("name", "").strip()
        if not email or "@" not in email:
            flash("Please enter a valid email.")
            return redirect(url_for("landing"))

        user = User.query.filter_by(email=email).first()
        if not user:
            admin_email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
            is_admin = bool(admin_email) and email == admin_email
            user = User(email=email, name=name or None, is_admin=is_admin)
            db.session.add(user)
            db.session.commit()
            if not is_admin:
                profile = StudentProfile(user_id=user.id)
                db.session.add(profile)
                db.session.commit()

        session["user_id"] = user.id
        return redirect(url_for("dashboard"))

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("landing"))

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
            next_class=profile.next_class,
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
        return render_template("quizzes.html", user=user, profile=profile)

    @app.route("/quizzes/<int:quiz_id>")
    @login_required
    def take_quiz(quiz_id):
        user = current_user()
        quiz = Quiz.query.get_or_404(quiz_id)
        if user.is_admin or quiz.profile_id != user.profile.id:
            abort(403)
        questions = json.loads(quiz.questions_json)
        return render_template("take_quiz.html", user=user, quiz=quiz, questions=questions)

    @app.route("/settings")
    @login_required
    def settings():
        user = current_user()
        return render_template("settings.html", user=user)

    @app.route("/account/delete", methods=["POST"])
    @login_required
    def delete_account():
        user = current_user()
        db.session.delete(user)
        db.session.commit()
        session.clear()
        return redirect(url_for("landing"))

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

        profile.course_name = request.form.get("course_name", "").strip() or None
        try:
            profile.total_classes = max(int(request.form.get("total_classes", 0)), 0)
        except ValueError:
            profile.total_classes = 0
        profile.timezone = request.form.get("timezone") or profile.timezone
        profile.setup_complete = True

        db.session.commit()
        flash("Student profile updated.")
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
            from zoneinfo import ZoneInfo
            naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            local_dt = naive.replace(tzinfo=ZoneInfo(tz_name))
            utc_dt = local_dt.astimezone(dt_timezone.utc).replace(tzinfo=None)
        except Exception:
            flash("Invalid date/time/timezone.")
            return redirect(url_for("admin_student", user_id=user_id))

        session_obj = ClassSession(profile_id=student.profile.id, start_at=utc_dt)
        db.session.add(session_obj)
        db.session.commit()
        flash("Class scheduled.")
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

    @app.route("/admin/student/<int:user_id>/quiz/generate", methods=["POST"])
    @login_required
    @admin_required
    def admin_student_generate_quiz(user_id):
        student = User.query.filter_by(id=user_id, is_admin=False).first_or_404()
        topic = request.form.get("topic", "").strip()
        model = request.form.get("model") or FREE_MODELS[0]
        try:
            count = max(min(int(request.form.get("count", 5)), 25), 1)
        except ValueError:
            count = 5

        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            flash("Server is missing OPENROUTER_API_KEY -- ask the admin to set it in Render.")
            return redirect(url_for("admin_student", user_id=user_id))

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
                timeout=60,
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"]
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("```")[1]
                if cleaned.startswith("json"):
                    cleaned = cleaned[4:]
            start, end = cleaned.find("["), cleaned.rfind("]")
            questions = json.loads(cleaned[start:end + 1])
        except Exception as exc:
            flash(f"Quiz generation failed: {exc}")
            return redirect(url_for("admin_student", user_id=user_id))

        quiz = Quiz(
            profile_id=student.profile.id,
            title=topic[:100] if topic else "Untitled quiz",
            questions_json=json.dumps(questions),
            model_used=model,
        )
        db.session.add(quiz)
        db.session.commit()
        flash(f"Quiz generated with {len(questions)} question(s).")
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
