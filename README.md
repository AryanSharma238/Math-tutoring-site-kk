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
| `GEMINI_API_KEY` | Yes, for quiz generation | Free key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) -- quiz generation uses Gemini exclusively now, automatically rotating through 4 free Gemini models (2.5 Flash, 3.5 Flash, 2.5 Flash Lite, 3.5 Flash Lite) as each one's free daily quota runs out |
| `CLASS_CALL_URL` | No (class call just won't show up without it) | Your Daily.co room URL for the "Join/Start Class" video call feature -- see the Daily.co setup step below |
| `SUPABASE_SERVICE_ROLE_KEY` | No, but recommended if you use the whiteboard's image upload | Lets the server upload to Supabase Storage on a user's behalf. Without it, image uploads fall back to the anon key, which only works if your Storage bucket's policy explicitly allows anonymous inserts -- see the Whiteboard section below |
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
   - **Start Command**: `gunicorn app:app --timeout 120` (this repo's
     `Procfile` already sets this -- the longer timeout matters because free
     OpenRouter models can take a while to generate a quiz, and gunicorn's
     default 30-second timeout would otherwise kill the request mid-generation)
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
   - `GEMINI_API_KEY` -- your free key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) (sign in with a Google account, click "Create API Key", no card needed)
   - `CLASS_CALL_URL` -- (optional) sign up free at [dashboard.daily.co](https://dashboard.daily.co) (no card needed), create a room (Rooms -> Create room), turn on "Enable knocking" under that room's settings for a waiting room, then set this to the room's URL (looks like `https://your-subdomain.daily.co/your-room-name`). Without this set, the "Join/Start Class" button just won't appear.

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

## Collaborative Tutoring Whiteboard

A custom whiteboard built specifically for this site -- not an embedded third-party product.
Every student has their own persistent workspace; the admin opens any student's board from
the "Whiteboards" tab and draws on the exact same live board they see. A small pull-tab on
the left edge of every page (once logged in) opens the current user's board in a fullscreen
overlay without leaving whatever page they're on.

### What it does

- **Marker (pen) and highlighter** -- freehand drawing, each stroke its own independently
  selectable/movable/deletable object (not raw pixels on a flat canvas)
- **Eraser** -- deletes whichever individual stroke/shape/text/image it touches, not pixels;
  it can't accidentally wipe the whole board
- **Select** -- click to select one element (or drag a selection box for several), then move,
  resize, or delete it
- **Copy / cut / paste** -- `Ctrl/Cmd+C`, `Ctrl/Cmd+X`, `Ctrl/Cmd+V`; pastes are offset from
  the original so the duplicate is visibly distinct
- **Undo / redo** -- `Ctrl/Cmd+Z` / `Ctrl/Cmd+Shift+Z`, tracked per operation (one stroke, one
  move, one delete, one paste), never a full-canvas snapshot
- **Text** -- click to place an editable text box
- **Shapes** -- rectangle, circle, line, arrow
- **Color and stroke width** -- five preset swatches plus a custom color picker; four stroke
  widths. Changing either only affects strokes/shapes drawn *after* the change
- **Image upload** -- PNG/JPG/WEBP, stored in Supabase Storage (never as binary data in
  Postgres), inserted as a movable/resizable/deletable element
- **Multiple pages** -- add, rename (double-click a page tab), delete, and switch between as
  many independent pages as needed (tested with 100+); the last remaining page can't be
  deleted
- **PDF export** -- "Download PDF" renders every page (not just the visible one) into a
  single PDF, generated entirely client-side (no server-side rendering cost)
- **Automatic saving** -- every create/move/edit/delete persists immediately; a failed save
  never discards local work, it just retries and shows a "Reconnecting..." status until the
  next poll succeeds

### Access control

- A student can only ever open their own workspace -- there's no ID a student could guess or
  edit in a URL to reach another student's board; every whiteboard API route re-checks
  ownership against the logged-in session on every request.
- The admin can open any student's workspace (this app has exactly one teacher, matching how
  every other admin feature already works -- there's no multi-teacher assignment model to
  build against).
- This is enforced at the **Flask route level**, the same way every other feature in this app
  is (quizzes, curriculum uploads, class scheduling, the Canva embed) -- not via Postgres Row
  Level Security. The backend talks to Postgres directly through SQLAlchemy with one trusted
  database role, not through Supabase's PostgREST/RLS layer, so there's no `auth.uid()`-based
  policy for RLS to attach to here without a much larger change (issuing each browser session
  its own Supabase-verified JWT instead of this app's own Flask session, which nothing else in
  the app does either). If that ever changes app-wide, RLS could be added as a second layer;
  today the Flask-level checks are the actual and only enforcement.

### Realtime sync

Collaborators poll for changes every ~1.5 seconds rather than holding open a websocket
connection -- this is a deliberate simplification, not an oversight: this Flask app runs on
Render's free tier via a standard WSGI server (gunicorn), which isn't set up for long-lived
websocket connections, and adding one (or wiring the frontend to authenticate directly against
Supabase Realtime, which requires issuing it a Supabase-verified session the rest of the app
doesn't use) would be a meaningfully larger change than this feature justified on its own.

In practice this means edits show up for the other person within a second or two, not
instantly -- and only the elements that actually changed since the last poll are ever sent
(never the whole page), so it stays cheap even with many elements on a page.

If a poll or a save request fails (network hiccup, server restart), the local canvas is left
completely untouched and the status indicator switches to "Reconnecting..."; the next
successful poll picks up exactly where it left off. Nothing is ever lost or cleared on a
temporary disconnect.

### Data model

```
Student
  -> WhiteboardWorkspace   (one per student, auto-created on first open)
       -> WhiteboardPage   (one or more; a page is an independent canvas)
            -> WhiteboardElement   (one row per stroke/text/image/shape)
```

- `whiteboard_workspaces` -- `id`, `profile_id` (FK to the existing `student_profiles` table,
  unique), timestamps
- `whiteboard_pages` -- `id`, `workspace_id`, `name`, `position`, timestamps
- `whiteboard_elements` -- `id` (a client-generated UUID string, not an autoincrement int, so
  the browser can reference an object the instant it's created, before the server round-trip
  finishes), `page_id`, `type`, `data` (that object's serialized JSON -- position, color,
  path points, etc.), `created_by`, timestamps (`updated_at` indexed, since sync polling
  filters on it)
- `whiteboard_deletions` -- a small tombstone log (`page_id`, `element_id`, `deleted_at`) so a
  poller can learn "element X is gone" instead of just never hearing about it again; rows here
  are safe to prune once every client has polled past that timestamp

No student/teacher/auth tables were duplicated -- everything hangs off the app's existing
`User` and `StudentProfile` models.

### Storage

Images upload straight to a Supabase Storage bucket named **`whiteboard-uploads`** (create
this once, manually, as a **public** bucket in your Supabase project's Storage tab -- there's
no other setup needed there). Only the resulting public URL is stored in
`whiteboard_elements.data`; no binary image data is ever written to Postgres. Uploads use
`SUPABASE_SERVICE_ROLE_KEY` if you've set it (recommended -- lets the server write on the
user's behalf regardless of the bucket's own access policy); without it, uploads fall back to
the anon key, which only works if you've separately configured that bucket's policy to allow
anonymous inserts.

### Free-tier considerations

- **Postgres**: one row per whiteboard element. A heavily-used board (thousands of strokes)
  is still a small amount of relational data -- nothing like storing full-canvas image
  snapshots would be.
- **Supabase Storage**: only uploaded images live here, at whatever size the original file
  was (no server-side resizing yet) -- large uploads will use free-tier storage faster than
  small ones.
- **No Supabase Realtime usage** -- sync is plain HTTP polling against this app's own Flask
  routes, so it doesn't count against Supabase's Realtime concurrent-connection limits at all.
- This is not unlimited: a large, very active class over months would eventually approach
  Supabase's free-tier Postgres/Storage caps like any other data in this app (quizzes,
  curriculum files, etc. share the same database and free tier).

### Removed

The previous whiteboard (one static link per student pointing at an external Excalidraw
room, embedded in an iframe) has been fully removed -- the `whiteboard_room`/`whiteboard_key`
columns and the old `whiteboard_pages` schema are dropped automatically on first startup after
this update (see `_drop_legacy_whiteboard_pages_table` in `app.py`), and no old whiteboard
templates, routes, or JS remain in the codebase.
