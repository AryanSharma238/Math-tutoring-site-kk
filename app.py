import json
import math
import os
import random
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from functools import wraps
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from flask import (
    Flask, abort, flash, jsonify, redirect, render_template, request,
    send_file, session, url_for,
)
from io import BytesIO
from supabase import create_client

from models import CurriculumFile, Quiz, SiteEmbed, StudentProfile, TodoItem, User, db

GITHUB_REPO = "AryanSharma238/Math-tutoring-site-kk"

# Video call: one fixed room shared by the admin and every student (the admin only ever runs
# one class at a time, so everyone landing in the same room is exactly right -- no per-student
# bookkeeping needed).
#
# This used to point at meet.jit.si, Jitsi's public server -- but embedding it in an iframe
# throws its own "Embedding meet.jit.si is only meant for demo purposes... will disconnect in
# 5 minutes" warning and actually does that, so it's unusable for a real class. Whereby is
# built specifically to be embedded like this and supports waiting-room style control via room
# permissions.
#
# One-time setup:
# - CLASS_CALL_URL: participant room URL (no roomKey) used for students.
# - CLASS_CALL_HOST_URL: host link (includes roomKey) used for admins.
# If only CLASS_CALL_HOST_URL is set, students automatically get the same URL with roomKey
# stripped so they join as participants.
def _strip_whereby_room_key(raw_url):
    if not raw_url:
        return ""
    parts = urlsplit(raw_url.strip())
    if not parts.scheme or not parts.netloc:
        return raw_url.strip()
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "roomKey"]
    return urlunsplit(parts._replace(query=urlencode(query)))


_configured_participant_url = os.environ.get("CLASS_CALL_URL", "").strip()
_configured_host_url = os.environ.get("CLASS_CALL_HOST_URL", "").strip()
CLASS_CALL_PARTICIPANT_URL = _strip_whereby_room_key(_configured_participant_url or _configured_host_url)
CLASS_CALL_HOST_URL = _configured_host_url or CLASS_CALL_PARTICIPANT_URL

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


_supabase_storage_client = None


def get_supabase_storage():
    """A separate client for server-side Storage writes (whiteboard image uploads). Prefers
    SUPABASE_SERVICE_ROLE_KEY -- the anon key is normally blocked from writing to a Storage
    bucket by that bucket's own access policy, and the service role key is what lets our
    backend (which already authenticates the user itself via the app's own login, not
    Supabase's) upload on the user's behalf. Falls back to the anon client if no service role
    key is set, which only works if the bucket's policy explicitly allows anon inserts."""
    global _supabase_storage_client
    if _supabase_storage_client is None:
        url = os.environ.get("SUPABASE_URL")
        service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if url and service_key:
            _supabase_storage_client = create_client(url, service_key)
        else:
            _supabase_storage_client = get_supabase()
    return _supabase_storage_client

COMMON_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Anchorage", "Pacific/Honolulu", "America/Sao_Paulo",
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Moscow",
    "Africa/Cairo", "Africa/Johannesburg",
    "Asia/Dubai", "Asia/Kolkata", "Asia/Shanghai", "Asia/Tokyo", "Asia/Singapore",
    "Australia/Sydney", "Pacific/Auckland", "UTC",
]

# Gemini-only now -- every entry is a free Gemini model. Order matters: this is also the
# automatic-failover order. Since Gemini's free tier is quota'd per-model per-day, quiz
# generation tries the first one and, if its daily quota is exhausted (or it fails for any
# other reason), automatically moves on to the next Gemini model instead of stopping.
# gemini-2.0-flash and gemini-2.0-flash-lite were discontinued (June 2026) -- keep this list to
# models Google currently actually serves. Checked against https://ai.google.dev/gemini-api/docs/models.
FREE_MODELS = [
    ("gemini|gemini-2.5-flash", "gemini", "Gemini 2.5 Flash (free)"),
    ("gemini|gemini-3.5-flash", "gemini", "Gemini 3.5 Flash (free)"),
    ("gemini|gemini-2.5-flash-lite", "gemini", "Gemini 2.5 Flash Lite (free)"),
    ("gemini|gemini-3.5-flash-lite", "gemini", "Gemini 3.5 Flash Lite (free)"),
]
DEFAULT_MODEL = FREE_MODELS[0][0]

QUIZ_SYSTEM_PROMPT = """You are a math problem generator. Given a topic/prompt, generate exactly {count} distinct multiple-choice math problems matching it, plus a short descriptive title for the quiz as a whole.

Each question must have exactly 4 answer choices, exactly one of which is correct. All 4 choices must be genuinely distinct values from each other -- never repeat the same number/expression across multiple choices, even by accident (e.g. simplifying to the same reduced form through different arithmetic slips). Each wrong choice should come from a different plausible mistake, not the same result reached two different ways.
Each question must include a step-by-step solution.
Each incorrect choice must include a brief explanation of the specific mistake or misconception that leads to it.
Verify all numbers and answer choices are mathematically correct and consistent before outputting.
Be concise everywhere: solutions should be the shortest sequence of steps that fully justifies the answer (typically 2-5 short steps, not an exhaustive essay), and each wrong-choice explanation should be one short sentence. Do not pad with restated problem text, filler phrases, or redundant recaps. Shorter output generates faster, so favor brevity without sacrificing correctness.

The "question" field must contain ONLY the question text -- never embed the answer choices inside it.
The "title" field should be a short, specific, human-readable name for the quiz (4-8 words), based on the topic -- e.g. "Trigonometric Identities Practice" or "Quadratic Formula Word Problems". Do not just repeat the raw topic text verbatim.

=== MATH FORMATTING (read carefully, this is checked) ===
Every mathematical expression, symbol, equation, or notation anywhere in "question", "choices[].text", "choices[].explanation", and "solution" must be written in LaTeX -- never plain text or ASCII approximations (no "x^2" outside LaTeX, no "sqrt(x)", no "1/2" as bare text, no "theta", no "<=" or ">="). Wrap inline math in single dollar signs: "$...$". Wrap standalone/display equations that should get their own line in double dollar signs: "$$...$$". Plain English sentences around the math do not need LaTeX -- only the notation itself.

CRITICAL JSON ESCAPING RULE: your entire response is parsed as JSON, and LaTeX is full of backslashes. Every single literal backslash you write must be doubled inside the JSON string, or the JSON parser will corrupt it. Write "\\\\theta" (two backslash characters) in your actual output so it decodes to "\\theta" -- writing only one backslash ("\\theta") is INVALID and will silently mangle the command (a lone "\\b" or "\\f" gets read as a control character, wrecking \\begin, \\binom, \\bar, \\boxed, \\frac, \\forall, and anything else starting with b or f). This applies to every backslash: \\frac -> "\\\\frac", \\theta -> "\\\\theta", \\times -> "\\\\times", \\right -> "\\\\right". For a matrix row break, which is itself two backslashes ("\\\\" in rendered LaTeX), write FOUR backslash characters in your JSON output ("\\\\\\\\") so it decodes to two. Double-check every backslash in your response before finishing.

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
- Equation form (for a continuous function): {{"type": "equation", "equation": "x^2 - 4", "x_min": -10, "x_max": 10}}. "equation" MUST be solved explicitly for y as a function of x, in plain math notation (NOT LaTeX here) -- e.g. for the line "2y + 3x = 5", write "equation": "-1.5*x + 2.5" (solved for y), never the unsolved original form. Use "sin(x)", "sqrt(x)", "x^2", "2*x+1", standard operators + - * / ^ and functions sin, cos, tan, sqrt, abs, log, ln, exp -- nothing else (no implicit/multi-variable equations, no "=" sign, no plain "y"). "x_min"/"x_max" are optional (default to -10..10) -- pick a range that actually shows the interesting part of the curve (e.g. its vertex, roots, or asymptote), not an arbitrary default.
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


def _repair_latex_backslashes(text):
    """Models are extremely inconsistent about JSON-escaping the backslashes inside their own
    LaTeX output: they write $\\theta$, $\\begin{pmatrix}...$, etc. as if a single backslash
    character were enough, when valid JSON requires every literal backslash inside a string to
    be doubled. The dangerous part is that a *lone* backslash before b/f/n/r/t/u is still
    syntactically valid JSON -- json.loads just silently decodes it as a control character
    (backspace, formfeed, CR, tab, or a \\uXXXX escape) and swallows the letter. That's exactly
    why "\\begin{pmatrix}" was rendering as a stray box glyph followed by "egin{pmatrix}", and
    "\\frac{3}{2}" as a box glyph followed by "rac{3}{2}" -- \\b and \\f are common LaTeX command
    starts (\\begin, \\binom, \\bar, \\boxed, \\frac, \\forall...) that collide head-on with
    JSON's own escape codes. A literal backslash can only legally occur inside a JSON string
    value, so it's safe to double every run of them in the raw text before parsing -- this turns
    the model's (invalid-but-common) single-backslash LaTeX into properly escaped JSON, without
    touching anything else in the payload."""
    return re.sub(r"\\+", lambda m: m.group(0) * 2, text)


_SUSPICIOUS_CONTROL_CHARS = "\x08\x0c\x09\x0d"  # backspace, formfeed, tab, CR


def _has_suspicious_control_chars(value):
    """Detects the telltale sign of under-escaped LaTeX backslashes surviving a "successful"
    json.loads: a backspace/formfeed/tab/CR control character embedded in decoded text. These
    are legal JSON escapes on their own so parsing doesn't fail -- but a real quiz question has
    no legitimate reason to contain one, so finding one means \\b, \\f, \\t, or \\r ate a LaTeX
    command letter (\\begin, \\frac, \\tan, \\theta, \\times, \\right, ...) rather than being an
    intentional control character."""
    if isinstance(value, str):
        return any(ch in value for ch in _SUSPICIOUS_CONTROL_CHARS)
    if isinstance(value, dict):
        return any(_has_suspicious_control_chars(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_suspicious_control_chars(v) for v in value)
    return False


def _extract_json_object(raw):
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("The AI didn't return any JSON. Please try again.")
    candidate = cleaned[start:end + 1]

    try:
        parsed = json.loads(candidate)
        if not _has_suspicious_control_chars(parsed):
            return parsed
    except json.JSONDecodeError:
        pass  # fall through to the repaired parse below

    # Either the parse failed outright, or it "succeeded" but swallowed LaTeX command letters
    # into control characters -- both point at the same under-escaped-backslash problem.
    return json.loads(_repair_latex_backslashes(candidate))


# Mirrors static/app.js's evalMathExpr -- kept in sync so a graph that passes this server-side
# check is guaranteed to actually plot something in the browser too.
_GRAPH_FUNCS = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "sqrt": math.sqrt, "abs": abs, "exp": math.exp,
    "log": math.log10, "ln": math.log,
}
_GRAPH_CONSTS = {"pi": math.pi, "e": math.e}
_GRAPH_IDENT_RE = re.compile(r"[a-zA-Z]+")
_GRAPH_ALLOWED_CHARS_RE = re.compile(r"^[0-9a-zA-Z.+\-*/^() ]*$")


def _eval_graph_equation(equation, x):
    """Safely evaluate a plain-math (non-LaTeX) function-of-x string, e.g. "x^2 - 4" or
    "sin(x)/2". Raises ValueError/ZeroDivisionError/etc. on anything invalid or undefined at x --
    callers should expect and handle that, same as the client-side evaluator does."""
    compact = equation.replace(" ", "")
    if not compact or not _GRAPH_ALLOWED_CHARS_RE.match(compact):
        raise ValueError("disallowed characters in equation")
    for ident in _GRAPH_IDENT_RE.findall(compact):
        if ident == "x" or ident in _GRAPH_FUNCS or ident in _GRAPH_CONSTS:
            continue
        raise ValueError(f"unknown identifier \"{ident}\" in equation")

    expr = compact.replace("^", "**")
    safe_locals = {**_GRAPH_FUNCS, **_GRAPH_CONSTS, "x": x}
    return eval(expr, {"__builtins__": {}}, safe_locals)  # noqa: S307 -- restricted globals/locals only


def _equation_is_plottable(equation, x_min, x_max):
    """Samples a handful of points across the range and checks at least a couple produce a
    finite real y -- catches equations the model wrote in an unsolved/implicit form (e.g. the
    literal "2y + 3x = 5" instead of solving for y) or otherwise-unparseable expressions, so we
    drop the graph instead of shipping students an empty chart."""
    finite_count = 0
    for i in range(9):
        x = x_min + (x_max - x_min) * i / 8
        try:
            y = _eval_graph_equation(equation, x)
        except Exception:
            continue
        if isinstance(y, (int, float)) and math.isfinite(y):
            finite_count += 1
    return finite_count >= 2


def _sanitize_graph_field(q):
    """Validate the optional "graph" field; drop it if malformed rather than failing
    the whole quiz over a non-essential visual extra."""
    graph = q.get("graph")
    if not isinstance(graph, dict):
        q.pop("graph", None)
        return

    gtype = graph.get("type")
    if gtype == "equation" and isinstance(graph.get("equation"), str) and graph["equation"].strip():
        equation = graph["equation"].strip()
        x_min = graph.get("x_min") if isinstance(graph.get("x_min"), (int, float)) else -10
        x_max = graph.get("x_max") if isinstance(graph.get("x_max"), (int, float)) else 10
        if x_min >= x_max or not _equation_is_plottable(equation, x_min, x_max):
            q.pop("graph", None)
            return
        clean = {"type": "equation", "equation": equation}
        for bound, val in (("x_min", x_min), ("x_max", x_max)):
            if graph.get(bound) is not None:
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

        # Catches the model repeating the same value across multiple choices (e.g. four "1"s
        # with only one marked correct) -- technically satisfies the schema but is a broken
        # question, since a student can't distinguish the choices.
        normalized_texts = [
            re.sub(r"\s+", "", (c.get("text") or "")).lower() for c in choices
        ]
        if len(set(normalized_texts)) != len(normalized_texts):
            raise ValueError(f"Question {i} has duplicate answer choices. Please try again.")

        # Models are heavily biased toward putting the correct answer first -- shuffle here
        # instead of relying on the prompt, so the correct choice's position is genuinely
        # random regardless of what the model does. Relabel A/B/C/... to match the new order.
        random.shuffle(choices)
        labels = "ABCDEFGHIJ"
        for idx, c in enumerate(choices):
            c["label"] = labels[idx] if idx < len(labels) else str(idx + 1)

        _sanitize_graph_field(q)

    title = (parsed.get("title") or "").strip() or (topic[:100] if topic else "Untitled quiz")
    return title, parsed["questions"]


_PROVIDER_CONFIG = {
    "gemini": {
        # Google's OpenAI-compatibility layer -- same request/response shape used before.
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
            "\"correct\": true and all others \"correct\": false -- not zero, not two or more. "
            "Every choice's \"text\" must also be genuinely distinct from every other choice on "
            "that question -- do not repeat the same value (e.g. multiple choices that are all "
            "just \"1\") even if only one of them is marked correct; each wrong choice should "
            "represent a different plausible mistake, not a duplicate of another option."
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
    "ALTER TABLE student_profiles ADD COLUMN class_weekday INTEGER",
    "ALTER TABLE student_profiles ADD COLUMN class_time VARCHAR(5)",
    # site_embeds used to be keyed by a required "slot" string; it's now one row per student
    # profile instead. Add the new column and relax the old one so existing rows (and the
    # app's new profile_id-only inserts) don't hit a NOT NULL violation.
    "ALTER TABLE site_embeds ADD COLUMN profile_id INTEGER",
    "ALTER TABLE site_embeds ALTER COLUMN slot DROP NOT NULL",
    # The old link-based whiteboard (one row per student, pointing at an external Excalidraw
    # room) is gone -- replaced by the custom object-model whiteboard in whiteboard_routes.py.
    # These columns belonged only to that old system.
    "ALTER TABLE student_profiles DROP COLUMN whiteboard_room",
    "ALTER TABLE student_profiles DROP COLUMN whiteboard_key",
    # A separate whiteboard rebuild (Excalidraw-backed "boards"/"board_collaborators", with
    # its own RLS policies) was briefly merged in parallel and is also being replaced by the
    # object-model whiteboard here -- drop its tables so they don't linger unused.
    "DROP TABLE IF EXISTS board_collaborators",
    "DROP TABLE IF EXISTS boards",
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


def _drop_legacy_whiteboard_pages_table():
    """The OLD whiteboard system also had a table literally named "whiteboard_pages", but with
    completely different columns (profile_id, title, src_url) than the new one (workspace_id,
    name). db.create_all() only creates tables that don't exist yet, so if the old table is
    still sitting there from a previous deploy, it would silently block the new schema from
    ever being created -- drop it first (only if it's still on the OLD schema, detected by the
    presence of "src_url", so this never touches an already-migrated table) so create_all()
    below creates it fresh with the right columns. All rows in the old table were just links to
    external Excalidraw rooms, not real content, so there's nothing worth preserving."""
    from sqlalchemy import inspect, text
    try:
        inspector = inspect(db.engine)
        if "whiteboard_pages" not in inspector.get_table_names():
            return
        columns = {c["name"] for c in inspector.get_columns("whiteboard_pages")}
        if "src_url" in columns:
            with db.engine.connect() as conn:
                conn.execute(text("DROP TABLE whiteboard_pages"))
                conn.commit()
    except Exception:
        pass


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
        _drop_legacy_whiteboard_pages_table()
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

        _log_in_local_user(result.user.id, email, name, result.session)
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

        _log_in_local_user(result.user.id, email, None, result.session)
        return redirect(url_for("dashboard"))

    def _log_in_local_user(supabase_uid, email, name, supabase_session):
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
        if supabase_session:
            session["sb_access_token"] = supabase_session.access_token
            session["sb_refresh_token"] = supabase_session.refresh_token

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
            return render_template(
                "admin_dashboard.html", user=user, students=students, active="dashboard",
                class_call_url=CLASS_CALL_HOST_URL,
            )

        profile = user.profile
        if not profile or not profile.setup_complete:
            return render_template("waiting.html", user=user)

        return render_template(
            "student_dashboard.html", user=user, profile=profile,
            next_class=profile.next_class, active="dashboard",
            class_call_url=CLASS_CALL_PARTICIPANT_URL,
        )

    @app.route("/admin/assign-quiz")
    @login_required
    @admin_required
    def admin_assign_quiz():
        students = User.query.filter_by(is_admin=False).order_by(User.created_at).all()
        return render_template(
            "admin_assign_quiz.html", user=current_user(), students=students, active="assign-quiz",
        )

    # ============ Whiteboard ============
    # See whiteboard_routes.py for the full route set (workspace/page/element CRUD, image
    # upload, sync polling) -- kept in its own module since it's a self-contained subsystem
    # and app.py was already large. Imported lazily here (not at module top) to avoid a
    # circular import, since it in turn imports login_required/admin_required/current_user
    # back from this module.
    from whiteboard_routes import register_whiteboard_routes
    register_whiteboard_routes(app)

    @app.route("/admin/student/<int:user_id>/embed", methods=["POST"])
    @login_required
    @admin_required
    def admin_student_set_embed(user_id):
        student = User.query.filter_by(id=user_id, is_admin=False).first_or_404()
        pasted = request.form.get("embed_code", "")
        # Accept either a full pasted embed blob (iframe/div/script, any line breaks/whitespace)
        # or a bare URL -- pull the iframe src out with a regex so formatting never matters.
        match = re.search(r'<iframe[^>]*\ssrc=["\']([^"\']+)["\']', pasted, re.IGNORECASE | re.DOTALL)
        src = match.group(1) if match else pasted.strip()

        if not src:
            flash("Paste the embed code (or its URL) first.")
            return redirect(url_for("admin_student", user_id=user_id))

        host = urlsplit(src).netloc.lower()
        if not host.endswith("canva.com"):
            flash("Only canva.com embed links are allowed.")
            return redirect(url_for("admin_student", user_id=user_id))

        embed = SiteEmbed.query.filter_by(profile_id=student.profile.id).first()
        if embed is None:
            embed = SiteEmbed(profile_id=student.profile.id, src_url=src)
            db.session.add(embed)
        else:
            embed.src_url = src
        db.session.commit()
        flash("Slideshow updated.")
        return redirect(url_for("admin_student", user_id=user_id))

    @app.route("/slideshow")
    @login_required
    def slideshow():
        user = current_user()
        if user.is_admin:
            return redirect(url_for("dashboard"))
        profile = user.profile
        if not profile or not profile.setup_complete:
            return render_template("waiting.html", user=user)

        embed = SiteEmbed.query.filter_by(profile_id=profile.id).first()
        return render_template(
            "slideshow.html", user=user, profile=profile, embed=embed, active="slideshow",
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
            own_quizzes=own_quizzes, active="my-quizzes",
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
            profile=student.profile, timezones=COMMON_TIMEZONES,
        )

    @app.route("/admin/student/<int:user_id>/update", methods=["POST"])
    @login_required
    @admin_required
    def admin_student_update(user_id):
        student = User.query.filter_by(id=user_id, is_admin=False).first_or_404()
        profile = student.profile

        student.name = request.form.get("student_name", "").strip() or None
        profile.course_name = request.form.get("course_name", "").strip() or None
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

    @app.route("/admin/student/<int:user_id>/schedule", methods=["POST"])
    @login_required
    @admin_required
    def admin_student_set_schedule(user_id):
        """Set the student's weekly recurring class time once -- "next class" is then always
        computed from this automatically, no manual re-adding needed week to week."""
        student = User.query.filter_by(id=user_id, is_admin=False).first_or_404()
        profile = student.profile
        weekday_str = request.form.get("class_weekday", "")
        time_str = request.form.get("class_time", "")

        if weekday_str == "" or not time_str:
            profile.class_weekday = None
            profile.class_time = None
            db.session.commit()
            flash("Weekly class time cleared.")
            return redirect(url_for("admin_student", user_id=user_id))

        try:
            weekday = int(weekday_str)
            if not (0 <= weekday <= 6):
                raise ValueError
            hour, minute = (int(p) for p in time_str.split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except (ValueError, TypeError):
            flash("Invalid day or time.")
            return redirect(url_for("admin_student", user_id=user_id))

        profile.class_weekday = weekday
        profile.class_time = f"{hour:02d}:{minute:02d}"
        db.session.commit()
        flash("Weekly class time saved.")
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

        if not os.environ.get("GEMINI_API_KEY"):
            return {"error": "Server is missing GEMINI_API_KEY -- ask the admin to set it in Render."}, 400

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

        if not os.environ.get("GEMINI_API_KEY"):
            return {"error": "Server is missing GEMINI_API_KEY -- ask your teacher to set it up."}, 400

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
