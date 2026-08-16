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

# Each entry: (value sent by the <select>, provider, human label).
# value = "{provider}|{model id}" -- parsed in _generate_quiz_attempt to pick the right API.
# Order matters: this is also the automatic-failover order -- if the selected model's daily
# free quota is exhausted (or it fails for any reason), generation moves on to the next one.
FREE_MODELS = [
    ("gemini|gemini-2.5-flash", "gemini", "Gemini: 2.5 Flash (free)"),
    ("groq|openai/gpt-oss-20b", "groq", "Groq: GPT-OSS 20B (fastest, free)"),
    ("groq|llama-3.3-70b-versatile", "groq", "Groq: Llama 3.3 70B (free)"),
    ("groq|llama-3.1-8b-instant", "groq", "Groq: Llama 3.1 8B Instant (free)"),
    ("gemini|gemini-2.0-flash", "gemini", "Gemini: 2.0 Flash (free)"),
    ("openrouter|openai/gpt-oss-20b:free", "openrouter", "OpenRouter: GPT-OSS 20B (free)"),
    ("openrouter|nvidia/nemotron-3-nano-30b-a3b:free", "openrouter", "OpenRouter: Nemotron Nano 30B (free)"),
    ("openrouter|google/gemma-4-31b-it:free", "openrouter", "OpenRouter: Gemma 4 31B (free)"),
    ("openrouter|cohere/north-mini-code:free", "openrouter", "OpenRouter: Cohere North Mini (free)"),
    ("openrouter|inclusionai/ling-3.0-tiny:free", "openrouter", "OpenRouter: Ling 3.0 Tiny (free)"),
]
DEFAULT_MODEL = FREE_MODELS[0][0]

QUIZ_SYSTEM_PROMPT = """You are a math problem generator. Given a topic/prompt, generate exactly {count} distinct multiple-choice math problems matching it, plus a short descriptive title for the quiz as a whole.

Each question must have exactly 4 answer choices, exactly one of which is correct.
Each question must include a step-by-step solution.
Each incorrect choice must include a brief explanation of the specific mistake or misconception that leads to it.
Verify all numbers and answer choices are mathematically correct and consistent before outputting.
Be concise everywhere: solutions should be the shortest sequence of steps that fully justifies the answer (typically 2-5 short steps, not an exhaustive essay), and each wrong-choice explanation should be one short sentence. Do not pad with restated problem text, filler phrases, or redundant recaps. Shorter output generates faster, so favor brevity without sacrificing correctness.

The "question" field must contain ONLY the question text -- never embed the answer choices inside it.
The "title" field should be a short, specific, human-readable name for the quiz (4-8 words), based on the topic -- e.g. "Trigonometric Identities Practice" or "Quadratic Formula Word Problems". Do not just repeat the raw topic text verbatim.

=== MATH FORMATTING (read carefully, this is checked) ===
Every mathematical expression, symbol, equation, or notation anywhere in "question", "choices[].text", "choices[].explanation", and "solution" must be written in LaTeX -- never plain text or ASCII approximations (no "x^2" outside LaTeX, no "sqrt(x)", no "1/2" as bare text, no "theta", no "<=" or ">="). Wrap inline math in single dollar signs: "$...$". Wrap standalone/display equations that should get their own line in double dollar signs: "$$...$$". Plain English sentences around the math do not need LaTeX -- only the notation itself.

Use these exact LaTeX conventions (all are supported by the renderer):
- Basic operators: $+$ $-$ $\\times$ $\\div$ $=$ $\\neq$ $<$ $>$ $\\leq$ $\\geq$ $\\pm$ $\\approx$
- Fractions: $\\frac{{a}}{{b}}$ (always use \\frac, never "a/b" as bare text)
- Exponents & subscripts: $x^2$, $x_i$, $a_n^2$, $10^{{-3}}$
- Roots: $\\sqrt{{16}}$, $\\sqrt[3]{{27}}$ (cube root), $\\sqrt[n]{{x}}$
- Trig functions: $\\sin(\\theta)$, $\\cos(x)$, $\\tan(x)$, $\\csc(x)$, $\\sec(x)$, $\\cot(x)$; inverse trig as $\\sin^{{-1}}(x)$ or $\\arcsin(x)$; hyperbolic as $\\sinh(x)$, $\\cosh(x)$, $\\tanh(x)$
- Logarithms: $\\log(x)$, $\\log_2(x)$ (log base b), $\\ln(x)$ (natural log)
- Derivatives: $\\frac{{d}}{{dx}}$, $\\frac{{dy}}{{dx}}$, $f'(x)$, $f''(x)$, partial derivatives $\\frac{{\\partial f}}{{\\partial x}}$
- Integrals: $\\int f(x)\\,dx$, definite integrals $\\int_a^b f(x)\\,dx$, double integrals $\\iint$
- Limits: $\\lim_{{x \\to a}} f(x)$, $\\lim_{{x \\to \\infty}} f(x)$
- Summation & product: $\\sum_{{i=1}}^{{n}} i$, $\\prod_{{i=1}}^{{n}} i$
- Vectors: $\\vec{{v}}$ or $\\mathbf{{v}}$, magnitude $\\|\\vec{{v}}\\|$
- Matrices: $\\begin{{pmatrix}} a & b \\\\ c & d \\end{{pmatrix}}$ (parentheses) or $\\begin{{bmatrix}} a & b \\\\ c & d \\end{{bmatrix}}$ (brackets)
- Piecewise functions: $$f(x) = \\begin{{cases}} x^2 & x \\geq 0 \\\\ -x & x < 0 \\end{{cases}}$$
- Set notation: $\\in$, $\\notin$, $\\subset$, $\\subseteq$, $\\cup$, $\\cap$, $\\emptyset$, $\\mathbb{{R}}$ (reals), $\\mathbb{{N}}$ (naturals), $\\mathbb{{Z}}$ (integers), $\\mathbb{{Q}}$ (rationals)
- Interval notation: $[a, b]$, $(a, b)$, $[a, b)$
- Absolute value: $|x|$ or $\\left| x \\right|$ for larger expressions
- Combinatorics: $\\binom{{n}}{{k}}$, factorial $n!$, permutations $P(n,k)$, combinations $C(n,k)$
- Greek letters: $\\alpha$, $\\beta$, $\\gamma$, $\\theta$, $\\pi$, $\\lambda$, $\\mu$, $\\sigma$, $\\omega$, $\\Delta$, $\\Sigma$, $\\Omega$
- Special symbols: $\\infty$, $\\to$ (arrow), $\\Rightarrow$, $\\Leftrightarrow$, $30^\\circ$ (degrees), $\\%$ (percent), $\\cdot$ (multiplication dot)
- Use \\left( and \\right) (etc.) instead of plain ( ) when they wrap a tall expression like a fraction, so the parentheses scale correctly.

=== GRAPH QUESTIONS ===
If the topic/prompt is about graphing, reading a graph, identifying features of a function's graph, or any question would clearly benefit from a visual plot, include an optional "graph" field on that question (omit it entirely for questions that don't need one). It must be one of these two shapes:
- Equation form (for a continuous function): {{"type": "equation", "equation": "x^2 - 4", "x_min": -10, "x_max": 10}}. "equation" is a function of x in plain math notation (NOT LaTeX here -- use "sin(x)", "sqrt(x)", "x^2", "2*x+1", standard operators + - * / ^ and functions sin, cos, tan, sqrt, abs, log, ln, exp). "x_min"/"x_max" are optional (default to -10..10).
- Points form (for discrete/scatter data): {{"type": "points", "points": [[0, 1], [1, 3], [2, 5]]}}, an array of [x, y] number pairs.
Do not include a "graph" field on questions that are purely symbolic/algebraic with nothing to plot.

Do not include any internal reasoning, revisions, second-guessing, notes, or commentary anywhere in the output, including inside string fields. Do not use markdown code fences.
Return ONLY a single valid JSON object with exactly this shape:

{{
  "title": "string",
  "questions": [
    {{
      "question": "string",
      "graph": {{"type": "equation", "equation": "x^2 - 4", "x_min": -10, "x_max": 10}},
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

The "graph" field is OPTIONAL -- only include it on questions that involve a graph; leave it out entirely otherwise. Exactly one choice per question must have "correct": true; the rest must be "correct": false with a non-empty "explanation". The correct choice's "explanation" should be an empty string."""


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


def _sanitize_graph_field(q):
    """Validate the optional "graph" field; drop it if malformed rather than failing
    the whole quiz over a non-essential visual extra."""
    graph = q.get("graph")
    if not isinstance(graph, dict):
        q.pop("graph", None)
        return

    gtype = graph.get("type")
    if gtype == "equation" and isinstance(graph.get("equation"), str) and graph["equation"].strip():
        clean = {"type": "equation", "equation": graph["equation"].strip()}
        for bound in ("x_min", "x_max"):
            val = graph.get(bound)
            if isinstance(val, (int, float)):
                clean[bound] = val
        q["graph"] = clean
    elif gtype == "points" and isinstance(graph.get("points"), list) and graph["points"]:
        points = []
        for p in graph["points"]:
            if (
                isinstance(p, (list, tuple)) and len(p) == 2
                and isinstance(p[0], (int, float)) and isinstance(p[1], (int, float))
            ):
                points.append([p[0], p[1]])
        if points:
            q["graph"] = {"type": "points", "points": points}
        else:
            q.pop("graph", None)
    else:
        q.pop("graph", None)


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
        _sanitize_graph_field(q)

    title = (parsed.get("title") or "").strip() or (topic[:100] if topic else "Untitled quiz")
    return title, parsed["questions"]


_PROVIDER_CONFIG = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "env_var": "GROQ_API_KEY",
        "display_name": "Groq",
        "supports_json_mode": True,
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "env_var": "OPENROUTER_API_KEY",
        "display_name": "OpenRouter",
        "supports_json_mode": False,
    },
    "gemini": {
        # Google's OpenAI-compatibility layer -- same request/response shape as the others.
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "env_var": "GEMINI_API_KEY",
        "display_name": "Gemini",
        "supports_json_mode": True,
    },
}


def _generate_quiz_attempt(topic, model_value, count, strict_reminder=False):
    if "|" in model_value:
        provider_key, model = model_value.split("|", 1)
    else:
        # Back-compat: a bare OpenRouter model id with no provider prefix.
        provider_key, model = "openrouter", model_value

    provider = _PROVIDER_CONFIG.get(provider_key)
    if not provider:
        raise RuntimeError(f"Unknown model provider \"{provider_key}\".")

    api_key = os.environ.get(provider["env_var"])
    if not api_key:
        raise RuntimeError(f"Server is missing {provider['env_var']} -- ask the admin to set it in Render.")

    system_prompt = QUIZ_SYSTEM_PROMPT.format(count=count)
    if strict_reminder:
        system_prompt += (
            "\n\nIMPORTANT: your previous attempt did not follow the required JSON shape exactly. "
            "Double check before responding: every question must have EXACTLY one choice with "
            "\"correct\": true and all others \"correct\": false -- not zero, not two or more."
        )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": topic or "general math problems, mixed topics"},
        ],
        # Without an explicit limit, longer quizzes (more questions) can get cut off mid-JSON,
        # which providers with strict JSON-mode validation (Groq) reject outright as a 400.
        "max_tokens": min(1200 * count + 800, 8192),
    }
    if provider["supports_json_mode"]:
        payload["response_format"] = {"type": "json_object"}

    try:
        resp = requests.post(
            provider["url"],
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=280,
        )
    except requests.exceptions.Timeout:
        raise RuntimeError("The AI model took too long to respond. Try again, or pick a faster model / fewer questions.")
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Could not reach {provider['display_name']}: {exc}")

    if resp.status_code == 404:
        raise RuntimeError(
            f"The model \"{model}\" doesn't exist on {provider['display_name']} anymore (it may have been retired). "
            "Please pick a different model from the dropdown."
        )
    if resp.status_code in (401, 403):
        raise RuntimeError(f"{provider['display_name']} rejected the API key. Check {provider['env_var']} in Render.")
    if resp.status_code == 429:
        raise RuntimeError(f"{provider['display_name']}'s free quota is exhausted for now.")
    if resp.status_code == 400:
        # Some providers (Gemini) also use 400 for an invalid/missing API key, not just bad JSON --
        # check the error body so a key problem surfaces clearly instead of being retried as if
        # it were just a one-off malformed-output slip.
        error_text = ""
        try:
            error_text = json.dumps(resp.json()).lower()
        except ValueError:
            error_text = resp.text.lower()
        if "api key" in error_text or "api_key" in error_text or "unauthenticated" in error_text:
            raise RuntimeError(f"{provider['display_name']} rejected the API key. Check {provider['env_var']} in Render.")
        if provider["supports_json_mode"]:
            # This provider validates JSON syntax server-side and 400s if the model's output wasn't
            # valid JSON (often from truncation or a one-off model slip) -- worth retrying, not fatal.
            raise ValueError(f"{provider['display_name']} rejected the generated output as invalid JSON (HTTP 400).")
        raise RuntimeError(f"{provider['display_name']} returned an error (HTTP 400). Try again in a moment.")
    if not resp.ok:
        raise RuntimeError(f"{provider['display_name']} returned an error (HTTP {resp.status_code}). Try again in a moment.")

    try:
        body = resp.json()
    except ValueError:
        raise RuntimeError(f"{provider['display_name']} returned an unexpected (non-JSON) response. Try again.")

    if not body.get("choices"):
        raise RuntimeError(f"{provider['display_name']}'s response didn't include any content -- the model may be overloaded. Try again.")

    raw = body["choices"][0].get("message", {}).get("content") or ""
    parsed = _extract_json_object(raw)
    title, questions = _validate_quiz_payload(parsed, topic)
    return {"title": title, "model": model_value, "questions": questions}


# Smaller/faster free models occasionally don't follow the strict JSON schema (e.g. a
# question with zero or two+ "correct" choices). Rather than surfacing that as a failure
# to the user, silently retry a couple of times with a sharper reminder before giving up.
_MAX_QUIZ_GENERATION_ATTEMPTS = 3


def _generate_quiz_with_retries(topic, model, count, max_attempts):
    """Try one specific model up to max_attempts times, retrying only on schema/JSON
    validation failures (ValueError) -- those are worth a reminder + reattempt on the
    same model. Anything else (bad key, quota exhausted, model retired, etc.) bubbles
    up immediately so the caller can fail over to a different model."""
    last_error = None
    for attempt in range(max_attempts):
        try:
            return _generate_quiz_attempt(topic, model, count, strict_reminder=attempt > 0)
        except ValueError as exc:
            last_error = exc
            continue
    raise RuntimeError(str(last_error))


def _generate_quiz(topic, model, count):
    # Try the requested model first (with a few in-place retries for schema slips), then
    # automatically fail over through every other configured free model in order -- e.g. if
    # today's free quota on one provider/model is used up (a 401/403/429), generation just
    # moves on to the next one instead of failing outright.
    fallback_order = [model] + [m[0] for m in FREE_MODELS if m[0] != model]
    last_error = None
    for i, candidate in enumerate(fallback_order):
        attempts = _MAX_QUIZ_GENERATION_ATTEMPTS if i == 0 else 1
        try:
            return _generate_quiz_with_retries(topic, candidate, count, attempts)
        except RuntimeError as exc:
            last_error = exc
            continue
    raise RuntimeError(
        f"All available free models failed or are rate-limited right now. Last error: {last_error}"
    )


def _run_quiz_generation_job(job_id, topic, model, count):
    try:
        result = _generate_quiz(topic, model, count)
        with _quiz_jobs_lock:
            _quiz_jobs[job_id] = {"status": "done", "result": result}
    except Exception as exc:
        with _quiz_jobs_lock:
            _quiz_jobs[job_id] = {"status": "error", "error": str(exc)}



# Columns added to models after the initial deploy. db.create_all() only creates
# brand-new tables -- it never adds columns to tables that already exist. Rather
# than requiring a manual ALTER TABLE in Supabase every time a model changes,
# this list is applied automatically on every startup; each statement is a no-op
# (caught and ignored) once the column already exists.
_PENDING_COLUMN_MIGRATIONS = [
    "ALTER TABLE quizzes ADD COLUMN completed_at TIMESTAMP",
    "ALTER TABLE quizzes ADD COLUMN answers_json TEXT",
    "ALTER TABLE student_profiles ADD COLUMN classes_left INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quizzes ADD COLUMN is_student_created BOOLEAN NOT NULL DEFAULT false",
]


def _run_pending_migrations():
    from sqlalchemy import text
    for stmt in _PENDING_COLUMN_MIGRATIONS:
        try:
            with db.engine.connect() as conn:
                conn.execute(text(stmt))
                conn.commit()
        except Exception:
            pass  # column already exists (or table doesn't exist yet) -- safe to ignore


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
        _run_pending_migrations()

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

        teacher_quizzes = [q for q in profile.quizzes if not q.is_student_created]

        # "Recently assigned" only makes sense for something still outstanding --
        # once it's completed it belongs in the Completed list instead, not here too.
        not_done = [q for q in teacher_quizzes if not q.completed_at]
        recently_assigned = not_done[0] if not_done else None
        todo = [q for q in not_done if q.id != (recently_assigned.id if recently_assigned else None)]
        completed = sorted(
            (q for q in teacher_quizzes if q.completed_at), key=lambda q: q.completed_at, reverse=True
        )
        return render_template(
            "quizzes.html", user=user, profile=profile,
            recently_assigned=recently_assigned, todo=todo, completed=completed,
            active="quizzes",
        )

    @app.route("/my-quizzes")
    @login_required
    def own_quizzes_page():
        user = current_user()
        if user.is_admin:
            return redirect(url_for("dashboard"))
        profile = user.profile
        if not profile or not profile.setup_complete:
            return render_template("waiting.html", user=user)

        own_quizzes = [q for q in profile.quizzes if q.is_student_created]
        return render_template(
            "own_quizzes.html", user=user, profile=profile,
            own_quizzes=own_quizzes, models=FREE_MODELS,
            active="my-quizzes",
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
        model = data.get("model") or DEFAULT_MODEL
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

    # --- Student: create their own quiz (separate from teacher-assigned ones) ---

    def _run_own_quiz_generation_job(job_id, profile_id, topic, model, count):
        try:
            result = _generate_quiz(topic, model, count)
            with app.app_context():
                quiz = Quiz(
                    profile_id=profile_id,
                    title=result["title"],
                    questions_json=json.dumps(result["questions"]),
                    model_used=model,
                    is_student_created=True,
                )
                db.session.add(quiz)
                db.session.commit()
                quiz_id = quiz.id
            with _quiz_jobs_lock:
                _quiz_jobs[job_id] = {"status": "done", "result": {"quiz_id": quiz_id, "title": result["title"]}}
        except Exception as exc:
            with _quiz_jobs_lock:
                _quiz_jobs[job_id] = {"status": "error", "error": str(exc)}

    @app.route("/quizzes/create/start", methods=["POST"])
    @login_required
    def own_quiz_generate_start():
        user = current_user()
        if user.is_admin or not user.profile:
            abort(403)
        data = request.get_json(silent=True) or {}
        topic = (data.get("topic") or "").strip()
        model = data.get("model") or DEFAULT_MODEL
        try:
            count = max(min(int(data.get("count", 5)), 25), 1)
        except (ValueError, TypeError):
            count = 5

        if not os.environ.get("OPENROUTER_API_KEY"):
            return {"error": "Server is missing OPENROUTER_API_KEY -- ask your teacher to set it up."}, 400

        job_id = uuid.uuid4().hex
        with _quiz_jobs_lock:
            _quiz_jobs[job_id] = {"status": "pending"}

        thread = threading.Thread(
            target=_run_own_quiz_generation_job,
            args=(job_id, user.profile.id, topic, model, count), daemon=True,
        )
        thread.start()
        return {"job_id": job_id}

    @app.route("/quizzes/create/status/<job_id>")
    @login_required
    def own_quiz_generate_status(job_id):
        with _quiz_jobs_lock:
            job = _quiz_jobs.get(job_id)
        if not job:
            return {"status": "error", "error": "That generation job could not be found (it may have expired)."}, 404
        return job

    @app.route("/quizzes/<int:quiz_id>/delete", methods=["POST"])
    @login_required
    def delete_own_quiz(quiz_id):
        user = current_user()
        quiz = Quiz.query.filter_by(id=quiz_id, is_student_created=True).first_or_404()
        if user.is_admin or not user.profile or quiz.profile_id != user.profile.id:
            abort(403)
        db.session.delete(quiz)
        db.session.commit()
        return redirect(url_for("quizzes"))

    @app.route("/admin/student/<int:user_id>/quiz/assign", methods=["POST"])
    @login_required
    @admin_required
    def admin_student_quiz_assign(user_id):
        student = User.query.filter_by(id=user_id, is_admin=False).first_or_404()
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "Untitled quiz")[:100]
        model = data.get("model") or DEFAULT_MODEL
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
