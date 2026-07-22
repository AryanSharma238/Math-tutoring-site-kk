# Math Tutoring Portal

A simple tutoring portal with an admin (teacher) and student roles.

- Real email + password sign-in, handled by **Supabase Auth** -- passwords
  are never touched by our own code; Supabase stores and verifies them.
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

Account deletion (Settings tab) only removes the student's row from our own
database -- it does not delete their Supabase Auth login. Fully deleting the
Supabase-side account requires the Supabase Admin API (a service-role key),
which isn't wired up yet. Fine for a small trusted class; flag if you need
full deletion later.

## Tech stack

- Flask + Flask-SQLAlchemy
- **Supabase Auth** for real email/password sign-in and sign-up (passwords
  are hashed and verified entirely by Supabase -- our own database only
  stores a `supabase_uid` to link a Supabase user to their course/profile
  data)
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
| `SUPABASE_URL` | Yes | Your Supabase project URL, e.g. `https://xxxx.supabase.co` |
| `SUPABASE_ANON_KEY` | Yes | Your Supabase project's `anon` public API key |
| `ADMIN_EMAIL` | Yes | The email address that becomes admin on first sign-up. Everyone else who signs up becomes a student |
| `OPENROUTER_API_KEY` | Yes, for quizzes | Free key from [openrouter.ai/keys](https://openrouter.ai/keys), used server-side to generate quizzes |
| `PYTHON_VERSION` | Yes, on Render | Set to `3.12.7` -- avoids a build failure where `psycopg2-binary`'s prebuilt wheel doesn't yet support Render's newer default Python |

## Deploying (Supabase auth + database, Render web service)

1. **Push this repo to GitHub** (already done if you're reading this on GitHub).

2. **Create a free Supabase project:**
   - Go to [supabase.com](https://supabase.com) -> sign in -> **New project**
   - Pick an organization, name the project (e.g. `mathtutor`), set a
     database password (save it somewhere), pick a region close to you,
     and choose the **Free** plan
   - **Important**: use only letters and numbers in the database password
     (avoid `@ / ? # & %` etc.). Those characters have special meaning inside
     a connection-string URL and will break the connection if they aren't
     percent-encoded. If you already have a password with special characters,
     go to **Project Settings** -> **Database** -> **Reset database password**
     and generate a new one (Supabase's generator is alphanumeric-safe)
   - Wait for the project to finish provisioning (~2 minutes)

3. **Get your database connection string:**
   - Go to **Project Settings** (gear icon) -> **Database**
   - Under **Connection string**, select the **URI** tab and choose
     **Session pooler** (recommended for long-running servers like Render's
     free web service) -- copy that URI
   - It looks like:
     `postgresql://postgres.xxxxxxxx:[YOUR-PASSWORD]@aws-0-<region>.pooler.supabase.com:5432/postgres`
   - Replace `[YOUR-PASSWORD]` with the database password you set above --
     this full string is your `DATABASE_URL`

4. **Get your Supabase API keys:**
   - Go to **Project Settings** -> **API**
   - Copy the **Project URL** -> this is `SUPABASE_URL`
   - Copy the **anon / public** key -> this is `SUPABASE_ANON_KEY`
     (safe to use here -- it's the key meant for this kind of use, not the
     `service_role` secret key, which you should never use in this app)

5. **(Recommended for a small class) Turn off email confirmation:**
   - Go to **Authentication** -> **Providers** -> **Email**
   - Turn off **Confirm email**
   - Without this, new accounts must click a confirmation link emailed by
     Supabase before they can sign in -- fine if you want that extra step,
     but turning it off means signup logs someone in immediately

6. **Create a Web Service on Render:**
   - Go to [dashboard.render.com](https://dashboard.render.com) -> **New** -> **Web Service**
   - Connect your GitHub account and select the `Math-tutoring-site-kk` repo
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
   - **Instance Type**: Free
   - Render's default Python version can be too new for `psycopg2-binary`'s
     prebuilt wheels. Add an environment variable `PYTHON_VERSION` set to
     `3.12.7` (see step 7) -- this repo's `runtime.txt` is a fallback but
     Render's current build system reads the env var, not that file

7. **Set environment variables** on the Web Service (Render dashboard -> your service -> Environment):
   - `PYTHON_VERSION` -- `3.12.7`
   - `SECRET_KEY` -- generate one, e.g. run `python3 -c "import secrets; print(secrets.token_hex(32))"` locally and paste the result
   - `DATABASE_URL` -- the Supabase connection string from step 3
   - `SUPABASE_URL` -- from step 4
   - `SUPABASE_ANON_KEY` -- from step 4
   - `ADMIN_EMAIL` -- the email you (the teacher) will sign up with
   - `OPENROUTER_API_KEY` -- your free key from [openrouter.ai/keys](https://openrouter.ai/keys)

8. **Deploy.** Render will build and start the app. The first request creates
   all database tables automatically (`db.create_all()` runs at startup) --
   you'll see the tables appear under Supabase's **Table Editor** afterward.

9. **Sign up as admin**: visit your Render URL, click "Get started", and
   create an account with the email you set as `ADMIN_EMAIL`. You'll land on
   the admin dashboard.

10. **Students sign up** with their own email/password at the same URL.
    They'll see the "waiting for setup" screen until you (the admin) open
    their profile from the sidebar and fill in their course, class count,
    timezone, curriculum, and schedule.

## Running locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
ADMIN_EMAIL=you@example.com SECRET_KEY=dev \
  SUPABASE_URL=https://xxxx.supabase.co SUPABASE_ANON_KEY=your-anon-key \
  python3 app.py
```

Without `DATABASE_URL` set, it falls back to a local `local.db` SQLite file.
Visit `http://localhost:5000` (or whatever `PORT` you set).
