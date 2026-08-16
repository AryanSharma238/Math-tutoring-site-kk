from datetime import datetime, timedelta, timezone as dt_timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class TodoItem(db.Model):
    __tablename__ = "todo_items"

    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(dt_timezone.utc))


class SiteEmbed(db.Model):
    """One admin-pasted embed (e.g. a Canva slideshow) per student profile."""
    __tablename__ = "site_embeds"

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("student_profiles.id"), unique=True, nullable=False, index=True)
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

    # Weekly recurring class time -- set once by the admin, then "next class" is always computed
    # from this instead of needing a fresh one-off entry added by hand every week.
    # class_weekday: 0=Monday .. 6=Sunday (Python's datetime.weekday() convention).
    class_weekday = db.Column(db.Integer, nullable=True)
    class_time = db.Column(db.String(5), nullable=True)  # "HH:MM", 24-hour, in `timezone`

    whiteboard_workspace = db.relationship(
        "WhiteboardWorkspace", backref="profile", uselist=False, cascade="all, delete-orphan",
    )

    curriculum_files = db.relationship(
        "CurriculumFile", backref="profile", cascade="all, delete-orphan",
        order_by="CurriculumFile.uploaded_at.desc()",
    )
    quizzes = db.relationship(
        "Quiz", backref="profile", cascade="all, delete-orphan",
        order_by="Quiz.created_at.desc()",
    )
    embed = db.relationship(
        "SiteEmbed", backref="profile", uselist=False, cascade="all, delete-orphan",
    )

    @property
    def next_class(self):
        """The next occurrence of the weekly recurring class time, computed on the fly --
        no manually-added one-off session rows to maintain."""
        if self.class_weekday is None or not self.class_time:
            return None
        try:
            from zoneinfo import ZoneInfo
            hour, minute = (int(p) for p in self.class_time.split(":"))
            tz = ZoneInfo(self.timezone)
        except Exception:
            return None

        now_local = datetime.now(tz)
        days_ahead = (self.class_weekday - now_local.weekday()) % 7
        candidate = (now_local + timedelta(days=days_ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate < now_local:
            candidate += timedelta(days=7)
        return candidate.astimezone(dt_timezone.utc)

    @property
    def latest_curriculum(self):
        return self.curriculum_files[0] if self.curriculum_files else None

    @property
    def latest_assigned_quiz(self):
        # Quizzes a student generates for themselves ("My Quizzes") are a separate practice
        # feature and should never show up as "homework" -- only teacher-assigned ones count.
        assigned = [q for q in self.quizzes if not q.is_student_created]
        return assigned[0] if assigned else None

    @property
    def latest_completed_quiz(self):
        completed = [q for q in self.quizzes if q.completed_at and not q.is_student_created]
        if not completed:
            return None
        return max(completed, key=lambda q: q.completed_at)


class WhiteboardWorkspace(db.Model):
    """One per student -- the root container for their whiteboard. Created lazily the first
    time anyone (the student or the admin) opens it."""
    __tablename__ = "whiteboard_workspaces"

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("student_profiles.id"), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(dt_timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(dt_timezone.utc),
        onupdate=lambda: datetime.now(dt_timezone.utc),
    )

    pages = db.relationship(
        "WhiteboardPage", backref="workspace", cascade="all, delete-orphan",
        order_by="WhiteboardPage.position",
    )


class WhiteboardPage(db.Model):
    """One independent canvas ('slide') within a workspace. Its elements are stored
    separately (WhiteboardElement) rather than as one giant blob, so a single stroke can be
    added/moved/deleted without rewriting the whole page."""
    __tablename__ = "whiteboard_pages"

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("whiteboard_workspaces.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False, default="Page 1")
    position = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(dt_timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(dt_timezone.utc),
        onupdate=lambda: datetime.now(dt_timezone.utc),
    )

    elements = db.relationship(
        "WhiteboardElement", backref="page", cascade="all, delete-orphan",
    )


class WhiteboardElement(db.Model):
    """A single whiteboard object -- one stroke, one text box, one image, one shape. `id` is a
    client-generated UUID string (not an autoincrement int) so the browser can reference an
    element by a stable ID from the moment it's created, before the server round-trip even
    finishes -- needed for undo/redo and for other collaborators to address the same object.
    `data` holds that object's Fabric.js JSON representation (position, color, path points,
    etc.) -- never a full-canvas snapshot, so persisting a move/color-change/delete only ever
    touches the one row for that object, not the whole page."""
    __tablename__ = "whiteboard_elements"

    id = db.Column(db.String(36), primary_key=True)
    page_id = db.Column(db.Integer, db.ForeignKey("whiteboard_pages.id"), nullable=False, index=True)
    type = db.Column(db.String(20), nullable=False)  # "path" | "text" | "image" | "rect" | "line" | "circle"
    data = db.Column(db.Text, nullable=False)  # JSON string
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(dt_timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(dt_timezone.utc),
        onupdate=lambda: datetime.now(dt_timezone.utc),
        index=True,  # polling queries filter/sort on this
    )


class WhiteboardDeletion(db.Model):
    """A short-lived tombstone log: deleting a WhiteboardElement removes its row entirely (no
    reason to keep dead rows around), but other collaborators polling for changes need some way
    to learn 'element X is gone' rather than just never seeing it mentioned again. This table
    is intentionally tiny (an id + a timestamp) and safe to prune periodically -- once every
    active client has polled past a deletion's timestamp, the row has no further purpose."""
    __tablename__ = "whiteboard_deletions"

    id = db.Column(db.Integer, primary_key=True)
    page_id = db.Column(db.Integer, db.ForeignKey("whiteboard_pages.id"), nullable=False, index=True)
    element_id = db.Column(db.String(36), nullable=False)
    deleted_at = db.Column(db.DateTime, default=lambda: datetime.now(dt_timezone.utc), index=True)


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
