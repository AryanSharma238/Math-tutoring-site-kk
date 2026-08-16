from datetime import datetime, timezone as dt_timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class TodoItem(db.Model):
    __tablename__ = "todo_items"

    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(dt_timezone.utc))


class SiteEmbed(db.Model):
    """Single row per admin-pasted embed (e.g. the Canva slideshow) --
    `slot` is a fixed key like 'canva_slideshow' so there's exactly one row per embed."""
    __tablename__ = "site_embeds"

    id = db.Column(db.Integer, primary_key=True)
    slot = db.Column(db.String(64), unique=True, nullable=False, index=True)
    src_url = db.Column(db.Text, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(dt_timezone.utc),
        onupdate=lambda: datetime.now(dt_timezone.utc),
    )

    @property
    def edit_src_url(self):
        """Same design but pointed at Canva's edit mode instead of view mode.
        Only actually editable in the browser if the viewer is logged into Canva
        with edit access to the design -- otherwise Canva shows it read-only anyway,
        same as it always did."""
        if "/view" in self.src_url:
            return self.src_url.replace("/view", "/edit", 1)
        return self.src_url


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    supabase_uid = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(255), nullable=True)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(dt_timezone.utc))

    profile = db.relationship(
        "StudentProfile", backref="user", uselist=False, cascade="all, delete-orphan"
    )


class StudentProfile(db.Model):
    __tablename__ = "student_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)

    course_name = db.Column(db.String(255), nullable=True)
    timezone = db.Column(db.String(64), default="America/New_York", nullable=False)
    setup_complete = db.Column(db.Boolean, default=False, nullable=False)

    curriculum_files = db.relationship(
        "CurriculumFile", backref="profile", cascade="all, delete-orphan",
        order_by="CurriculumFile.uploaded_at.desc()",
    )
    class_sessions = db.relationship(
        "ClassSession", backref="profile", cascade="all, delete-orphan",
        order_by="ClassSession.start_at",
    )
    quizzes = db.relationship(
        "Quiz", backref="profile", cascade="all, delete-orphan",
        order_by="Quiz.created_at.desc()",
    )

    @property
    def next_class(self):
        now = datetime.now(dt_timezone.utc)
        upcoming = [c for c in self.class_sessions if c.start_at.replace(tzinfo=dt_timezone.utc) >= now]
        return upcoming[0] if upcoming else None

    @property
    def latest_curriculum(self):
        return self.curriculum_files[0] if self.curriculum_files else None

    @property
    def latest_assigned_quiz(self):
        return self.quizzes[0] if self.quizzes else None

    @property
    def latest_completed_quiz(self):
        completed = [q for q in self.quizzes if q.completed_at]
        if not completed:
            return None
        return max(completed, key=lambda q: q.completed_at)


class CurriculumFile(db.Model):
    __tablename__ = "curriculum_files"

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("student_profiles.id"), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    mimetype = db.Column(db.String(100), nullable=False)
    data = db.Column(db.LargeBinary, nullable=False)
    uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(dt_timezone.utc))


class ClassSession(db.Model):
    __tablename__ = "class_sessions"

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("student_profiles.id"), nullable=False)
    start_at = db.Column(db.DateTime, nullable=False)  # stored in UTC
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(dt_timezone.utc))


class Quiz(db.Model):
    __tablename__ = "quizzes"

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("student_profiles.id"), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    questions_json = db.Column(db.Text, nullable=False)
    model_used = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(dt_timezone.utc))
    completed_at = db.Column(db.DateTime, nullable=True)
    answers_json = db.Column(db.Text, nullable=True)
    is_student_created = db.Column(db.Boolean, default=False, nullable=False)

    @property
    def question_count(self):
        import json
        try:
            return len(json.loads(self.questions_json))
        except (ValueError, TypeError):
            return 0
