# Math Tutoring Portal

A simple tutoring portal with an admin (teacher) and student roles.

- Sign in with just an email (no password yet -- see "Security note" below).
- Admin sees every student who has signed in, in the sidebar, and can:
  - Set a student's course name, total number of classes, and timezone
  - Upload their curriculum (PDF, JPG, JPEG, or PNG)
  - Schedule classes (date, time, timezone -- stored and converted to UTC)
  - Generate and assign a multiple-choice quiz (uses the same
    question-generation logic as the [Math Problem Generator](https://github.com/AryanSharma238/Math-problem-generator-open-router-based)
    repo, via a free OpenRouter model)
- A student who hasn't been set up yet sees a "waiting for your teacher"
  screen. Once the admin fills in their profile, the full dashboard appears:
  - Left: classes remaining
  - Middle: next class date/time (in the student's own timezone), with a
    live countdown once it's within 30 minutes
  - Right: their curriculum file, rendered inline (PDF embed or image)
  - A collapsible sidebar with Dashboard / Quizzes / Settings tabs
- Settings tab: light/dark mode toggle and delete-account button.
- Quizzes tab: lists assigned quizzes; clicking one opens an interactive
  quiz with clickable answer choices, instant feedback, and a
  step-by-step solution toggle.

## Security note

This is a "just get something working" auth system: typing any email logs
you in as that email (creating the account if it doesn't exist yet). There
is no password or email verification. Don't put real student data in here
until real authentication (e.g. magic links or OAuth) is added.

## Tech stack

- Flask + Flask-SQLAlchemy
- Postgres in production, hosted for free on **Supabase** (Render's free
  Postgres auto-expires after 30 days -- Supabase's free tier doesn't).
  SQLite is used for local dev if `DATABASE_URL` is unset.
- Curriculum files are stored as binary blobs directly in the database
  (no separate file storage needed)
- Quiz generation calls OpenRouter server-side using a free model

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | Yes | Flask session signing key -- set to a long random string |
| `DATABASE_URL` | Yes (prod) | Postgres connection string from Supabase (see step 2 below) |
| `ADMIN_EMAIL` | Yes | The email address that becomes admin on first sign-in. Everyone else who signs in becomes a student |
| `OPENROUTER_API_KEY` | Yes, for quizzes | Free key from [openrouter.ai/keys](https://openrouter.ai/keys), used server-side to generate quizzes |

## Deploying (Supabase database + Render web service)

1. **Push this repo to GitHub** (already done if you're reading this on GitHub).

2. **Create a free Postgres database on Supabase:**
   - Go to [supabase.com](https://supabase.com) -> sign in -> **New project**
   - Pick an organization, name the project (e.g. `mathtutor`), set a
     database password (save it somewhere), pick a region close to you,
     and choose the **Free** plan
   - Wait for the project to finish provisioning (~2 minutes)
   - Go to **Project Settings** (gear icon) -> **Database**
   - Under **Connection string**, select the **URI** tab and choose
     **Session pooler** (recommended for long-running servers like Render's
     free web service) -- copy that URI
   - It looks like:
     `postgresql://postgres.xxxxxxxx:[YOUR-PASSWORD]@aws-0-<region>.pooler.supabase.com:5432/postgres`
   - Replace `[YOUR-PASSWORD]` with the database password you set above --
     this full string is your `DATABASE_URL`

3. **Create a Web Service on Render:**
   - Go to [dashboard.render.com](https://dashboard.render.com) -> **New** -> **Web Service**
   - Connect your GitHub account and select the `Math-tutoring-site-kk` repo
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
   - **Instance Type**: Free

4. **Set environment variables** on the Web Service (Render dashboard -> your service -> Environment):
   - `SECRET_KEY` -- generate one, e.g. run `python3 -c "import secrets; print(secrets.token_hex(32))"` locally and paste the result
   - `DATABASE_URL` -- the Supabase connection string from step 2
   - `ADMIN_EMAIL` -- the email you (the teacher) will sign in with
   - `OPENROUTER_API_KEY` -- your free key from [openrouter.ai/keys](https://openrouter.ai/keys)

5. **Deploy.** Render will build and start the app. The first request creates
   all database tables automatically (`db.create_all()` runs at startup) --
   you'll see the tables appear under Supabase's **Table Editor** afterward.

6. **Sign in as admin**: visit your Render URL and sign in with the email you
   set as `ADMIN_EMAIL`. You'll land on the admin dashboard.

7. **Students sign in** with their own email at the same URL. They'll see the
   "waiting for setup" screen until you (the admin) open their profile from
   the sidebar and fill in their course, class count, timezone, curriculum,
   and schedule.

## Running locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
ADMIN_EMAIL=you@example.com SECRET_KEY=dev python3 app.py
```

Without `DATABASE_URL` set, it falls back to a local `local.db` SQLite file.
Visit `http://localhost:5000` (or whatever `PORT` you set).
