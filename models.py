from datetime import datetime, timedelta, timezone as dt_timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class TodoItem(db.Model):
    __tablename__ = "todo_items"

    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(dt_timezone.utc))


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
    quizzes = db.relationship(
        "Quiz", backref="profile", cascade="all, delete-orphan",
        order_by="Quiz.created_at.desc()",
    )
    schedule_slots = db.relationship(
        "ClassScheduleSlot", backref="profile", cascade="all, delete-orphan",
        order_by="ClassScheduleSlot.weekday, ClassScheduleSlot.time",
    )
    homework_files = db.relationship(
        "HomeworkFile", backref="profile", cascade="all, delete-orphan",
        order_by="HomeworkFile.uploaded_at.desc()",
    )

    @property
    def next_class(self):
        """The next occurrence across every recurring weekly slot -- a student can now have
        more than one class time a week, so this checks all of them and returns the soonest.
        Still fully computed on the fly, no manually-added one-off session rows to maintain."""
        if not self.schedule_slots:
            return None
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(self.timezone)
        except Exception:
            return None

        now_local = datetime.now(tz)
        candidates = []
        for slot in self.schedule_slots:
            try:
                hour, minute = (int(p) for p in slot.time.split(":"))
            except (ValueError, AttributeError):
                continue
            days_ahead = (slot.weekday - now_local.weekday()) % 7
            candidate = (now_local + timedelta(days=days_ahead)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if candidate < now_local:
                candidate += timedelta(days=7)
            candidates.append(candidate)
        if not candidates:
            return None
        return min(candidates).astimezone(dt_timezone.utc)

    @property
    def latest_curriculum(self):
        return self.curriculum_files[0] if self.curriculum_files else None

    @property
    def assigned_quizzes(self):
        """Everything still outstanding -- teacher-assigned and self-assigned together, since
        both now live in the same 'Assignments' list."""
        return [q for q in self.quizzes if not q.completed_at]

    @property
    def completed_quizzes(self):
        return sorted(
            (q for q in self.quizzes if q.completed_at), key=lambda q: q.completed_at, reverse=True,
        )

    @property
    def questions_answered(self):
        import json
        total = 0
        for quiz in self.quizzes:
            try:
                total += sum(1 for answer in json.loads(quiz.answers_json or "{}").values() if answer.get("submitted"))
            except (ValueError, TypeError, AttributeError):
                continue
        return total


class ClassScheduleSlot(db.Model):
    """One weekly recurring class time. A student can have several of these (e.g. Tuesdays
    and Thursdays) -- StudentProfile.next_class checks all of them and returns the soonest."""
    __tablename__ = "class_schedule_slots"

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("student_profiles.id"), nullable=False, index=True)
    weekday = db.Column(db.Integer, nullable=False)  # 0=Monday .. 6=Sunday
    time = db.Column(db.String(5), nullable=False)  # "HH:MM", 24-hour, in the profile's timezone
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(dt_timezone.utc))


class CurriculumFile(db.Model):
    __tablename__ = "curriculum_files"

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("student_profiles.id"), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    mimetype = db.Column(db.String(100), nullable=False)
    data = db.Column(db.LargeBinary, nullable=False)
    uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(dt_timezone.utc))


class HomeworkFile(db.Model):
    __tablename__ = "homework_files"

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("student_profiles.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
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
