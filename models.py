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
    schedule_slots = db.relationship(
        "ClassScheduleSlot", backref="profile", cascade="all, delete-orphan",
        order_by="ClassScheduleSlot.weekday, ClassScheduleSlot.time",
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


class ClassScheduleSlot(db.Model):
    """One weekly recurring class time. A student can have several of these (e.g. Tuesdays
    and Thursdays) -- StudentProfile.next_class checks all of them and returns the soonest."""
    __tablename__ = "class_schedule_slots"

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("student_profiles.id"), nullable=False, index=True)
    weekday = db.Column(db.Integer, nullable=False)  # 0=Monday .. 6=Sunday
    time = db.Column(db.String(5), nullable=False)  # "HH:MM", 24-hour, in the profile's timezone
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(dt_timezone.utc))


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


class WhiteboardImage(db.Model):
    """Fallback storage for whiteboard image uploads when Supabase Storage isn't configured
    (no SUPABASE_URL/keys) or a call to it fails for any reason (e.g. the 'whiteboard-uploads'
    bucket hasn't been created yet) -- uploading should always work out of the box, not depend
    on a separate manual setup step succeeding first. Supabase Storage is still tried first and
    is the recommended path (see the README); this only exists to guarantee the feature isn't
    broken without it."""
    __tablename__ = "whiteboard_images"

    id = db.Column(db.String(36), primary_key=True)
    page_id = db.Column(db.Integer, db.ForeignKey("whiteboard_pages.id"), nullable=False, index=True)
    mimetype = db.Column(db.String(100), nullable=False)
    data = db.Column(db.LargeBinary, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(dt_timezone.utc))


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
