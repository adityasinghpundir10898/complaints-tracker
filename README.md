# Complaint Tracker

A small Django app for any organisation that receives complaints (or requests, grievances, tickets) and has to get them investigated by other units, review the answer, and report it to an external system. It tracks each complaint through a loop that repeats until the complaint is closed:

```
notice sent to a unit -> inquiry report -> internal review (may loop back)
        -> upload to an external portal (may be rejected and loop back) -> closed
```

Example: a complaint arrives at the office. A clerk registers it and sends a notice to the responsible unit. The unit sends back a report. A reviewer is not satisfied, so a second notice goes out. The second report is approved and uploaded to the external portal. The portal rejects it with a reason, so the case goes back for another review, and finally the portal accepts it and the complaint is closed. Every step, with who did it and when, stays in the history.

Size it is built for: about 100 complaints a week, one office, 10-20 users. It is deliberately small.

- Full specification: [specification PDF](docs/SDM%20Complaint%20Tracker%20—%20Source%20of%20Truth.pdf). If the code and the spec disagree, raise it before changing either.
- Rules for contributors (and AI assistants): [.github/copilot-instructions.md](.github/copilot-instructions.md).

### Terms used

| Term | Meaning | Name in the code |
| --- | --- | --- |
| Office | The organisation using the app; all data belongs to one office | `Office` |
| Complaint | One case being tracked | `Complaint` |
| Unit | A department or team that is asked to investigate and report | `Department` |
| Location | Where the complaint comes from (village, ward, area) | `Village` |
| Category | Type of complaint; may set its own deadline (SLA) | `Category` |
| Referral | One round of sending a complaint to a unit | `Referral` |
| Portal | The external system the final answer is uploaded to | `PortalSubmission` |

## What it does

- Register a complaint by hand, or upload the complainant's letter (PDF, JPG, PNG) and get the form pre-filled from the text (see "Intake and OCR").
- Move a complaint through its states with named actions. Every action writes one entry to an append-only history.
- Show who currently holds each complaint and for how many days, with ageing colours (safe, warning, overdue, critical).
- Search by text (complaint subject, complainant, extracted file text), including approximate name matching.
- An Overdue page: late inquiry reports by unit, portal rejections waiting, and cases sitting in the office too long.
- Keep uploaded files immutable and serve them only to users who may see the complaint.

### States and actions

`RECEIVED -> AWAITING_REPORT -> UNDER_REVIEW -> APPROVED -> PORTAL_SUBMITTED -> CLOSED`

| Action | From -> To |
| --- | --- |
| `issue_notice` | RECEIVED -> AWAITING_REPORT |
| `reissue_notice` | AWAITING_REPORT -> AWAITING_REPORT (new round, the unit may change) |
| `record_report` | AWAITING_REPORT -> UNDER_REVIEW |
| `review_not_satisfied` | UNDER_REVIEW -> AWAITING_REPORT (new round) |
| `review_satisfied` | UNDER_REVIEW -> APPROVED |
| `submit_to_portal` | APPROVED -> PORTAL_SUBMITTED |
| `portal_accepted` | PORTAL_SUBMITTED -> CLOSED |
| `portal_rejected` | PORTAL_SUBMITTED -> APPROVED, UNDER_REVIEW or AWAITING_REPORT |
| `close_without_portal` | APPROVED -> CLOSED (reason required) |
| `reopen` | CLOSED -> UNDER_REVIEW (reason required) |

Any other combination raises `InvalidTransition`.

### Roles

Each role is a Django group created by a migration.

| Role | Can do |
| --- | --- |
| Clerk | Register, edit, add notes and files, issue and reissue notices, record reports, submit to the portal, record portal accepted or rejected |
| DepartmentOfficer | See only complaints currently with their own unit; add notes and files |
| Reviewer | Everything a Clerk can, plus review (satisfied or not), close without portal, reopen |
| Admin | Everything a Reviewer can, plus master data and users in the Django admin (`/admin/`; the user needs "staff status") |
| Auditor | Read-only |

Django's superuser flag grants nothing inside the app; roles do. Case tables (complaints, referrals, documents, events) are read-only even in the Django admin.

## Tech stack

Python 3.12, Django 5.2, PostgreSQL 16, Django templates + HTMX 1.9 and Pico.css 2 (both vendored in `static/vendor`, no CDN), Gunicorn, Docker Compose, pytest + pytest-django, ruff.

Not used on purpose: React/Vite, Celery/Redis, a REST API layer, S3, microservices, LLM calls. Do not add these without discussion.

## Requirements for running locally

| Needed | Why | Check |
| --- | --- | --- |
| Python 3.12 | the app | `py -3.12 --version` |
| Docker Desktop (Compose v2) | PostgreSQL 16, and the optional web container | `docker compose version` |
| Git | clone | `git --version` |

Optional, only for intake pre-fill from uploaded files when Django runs directly on your machine (the Docker `web` container already has them): Poppler (`pdftotext`, `pdftoppm`) and Tesseract with the `hin` and `eng` language packs, all on `PATH`. Without them everything works except the pre-fill.

Python packages are in [requirements.txt](requirements.txt) (Django, psycopg, django-environ, gunicorn) and [requirements-dev.txt](requirements-dev.txt) (adds pytest, pytest-django, ruff).

## Run it locally

The commands are for Windows PowerShell. On macOS/Linux use `python3.12 -m venv .venv`, `.venv/bin/python` and `cp` instead of `copy`.

```powershell
# 1. Get the code
git clone <repo-url> SDM-complaints-tracker
cd SDM-complaints-tracker

# 2. Python environment (once)
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt

# 3. Settings and database (once; start the db again whenever Docker was stopped)
copy .env.example .env
docker compose up -d db

# 4. Create tables, load demo data, start the server
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py seed
.venv\Scripts\python manage.py runserver
```

Open http://127.0.0.1:8000/ and sign in with a demo user: `clerk`, `officer`, `reviewer`, `admin` or `auditor`, all with password `demo-pass-123`. These users and the 30 complaints from `seed` are fake and for development only.

The database container is published on host port **5433** (not 5432) so it does not clash with a Postgres already installed on the machine. `.env.example` already points to it.

### Everyday commands

```powershell
docker compose up -d db                   # start the database
docker compose stop db                    # stop it (data is kept in the pgdata volume)
docker compose down -v                    # stop and DELETE all database data
.venv\Scripts\python manage.py migrate    # apply new migrations
.venv\Scripts\python manage.py seed       # load demo data; skips complaints if any exist
.venv\Scripts\python manage.py runserver  # dev server on port 8000
.venv\Scripts\python manage.py createsuperuser
```

`seed` refuses to run when `DEBUG=False` unless you pass `--allow-production`. Do not seed a real deployment.

### Optional: run the web app in Docker (with OCR tools)

```powershell
docker compose --profile web up --build
```

This runs Django on http://localhost:8000/ in a container that has Poppler and Tesseract (`hin` + `eng`). The first time, run migrations and seed inside it:

```powershell
docker compose --profile web run --rm web python manage.py migrate
docker compose --profile web run --rm web python manage.py seed
```

The compose file is a development setup only (dev settings, `runserver`, a throwaway secret key). It is not a production configuration.

### Check that it works

- `docker compose ps` shows the `db` service as running (healthy or up).
- `manage.py migrate` ends without errors.
- http://127.0.0.1:8000/ shows the login page, and `reviewer` / `demo-pass-123` signs in and lists complaints.
- `python -m pytest` passes.

### Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `docker compose` cannot connect to the daemon | Docker Desktop is not running; start it |
| `connection refused` on port 5433 | The `db` container is not running (`docker compose up -d db`), or another program uses port 5433; change the host port in `docker-compose.yml` and `DATABASE_URL` in `.env` |
| `SECRET_KEY` or `DATABASE_URL` missing | `.env` was not created; run `copy .env.example .env` in the project root |
| Intake does not pre-fill from an uploaded file | Poppler or Tesseract is not on `PATH`; use the Docker `web` container, which has them |
| `seed` says it refuses to run | `DEBUG` is `False`; it is meant for development only |

## Tests and lint

```powershell
.venv\Scripts\python -m pytest            # needs the db container running
.venv\Scripts\ruff check .
.venv\Scripts\ruff format --check .
```

Tests create their own `test_sdm` database. One test greps the code to make sure nothing outside `complaints/workflow.py` assigns `current_state`, `current_department` or `current_stage_started_at`.

## Configuration

All configuration is through environment variables, read from `.env` if present. `.env` is not in the repository: you create it by copying [.env.example](.env.example), and it must never be committed.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DJANGO_SETTINGS_MODULE` | none | `config.settings.dev` locally, `config.settings.prod` in production |
| `SECRET_KEY` | required | Django secret key |
| `DEBUG` | `False` (dev settings: `True`) | Never `True` in production |
| `ALLOWED_HOSTS` | empty | Comma-separated host names |
| `DATABASE_URL` | required | e.g. `postgres://sdm:sdm@127.0.0.1:5433/sdm` |
| `MEDIA_ROOT` | `./media` | Where uploaded files are stored (the folder is created at run time, not in the repository); must persist |
| `ADMIN_URL` | `admin/` | Change this in production |
| `CSRF_TRUSTED_ORIGINS` | empty | Comma-separated origins, prod settings only |
| `SECURE_SSL_REDIRECT` | `True` | prod settings only |
| `SECURE_HSTS_SECONDS` | `31536000` | prod settings only |

Values kept in `config/settings/base.py`: `AGEING_THRESHOLDS` (0 safe, 3 warning, 7 overdue, 10 critical days), `PAGE_SIZE` (25), `MAX_UPLOAD_BYTES` (10 MB; PDF, JPG, PNG only), `INTAKE_OCR_MAX_PAGES` (6).

PostgreSQL needs the `pg_trgm` extension (name matching). The first migration creates it and the `postgres:16` image includes it.

## Intake and OCR

On the "new complaint" screen you can upload the complainant's letter. The app reads it and pre-fills name, phone, address, location, subject, description and any reference number, and warns about possible duplicates. A person always reviews the form before saving.

- Text PDFs are read with `pdftotext`. Pages without a text layer (scans, photos) are rendered at 300 dpi and read with Tesseract, once with Hindi and once with English; the more confident text wins and the English pass is used for phone numbers.
- Reading is best effort. Hindi OCR misreads some words and drops some underlined lines, so treat the pre-fill as a draft.
- OCR runs inside the web request and took about 15-25 seconds for a two-page letter on a laptop. A production Gunicorn timeout must be well above that.

Helper scripts in `scripts/`:

| Script | Purpose |
| --- | --- |
| `scripts/try_intake.py` | Print what intake reads from the files or folders you pass, e.g. `docker compose --profile web run --rm web python scripts/try_intake.py samples/synthetic` (the Docker `web` container has the OCR tools) |
| `scripts/make_sample_complaints.py` | Windows only. Draws fake Hindi/English complaint letters described in `scripts/sample_complaints.json` into `samples/synthetic/`, and calls `scripts/render_pages.ps1` to make scan-like JPEGs |

The `samples/` folder is not in the repository (it is git-ignored so real complaint data never reaches it). `make_sample_complaints.py` creates `samples/synthetic/` when you run it; for your own test letters, create `samples/` yourself. Never commit real complaint files, `.env` or `media/`.

## Design rules (short version)

The full list is in [.github/copilot-instructions.md](.github/copilot-instructions.md).

1. Every status change goes through `complaints/workflow.py::apply_transition()`, in one database transaction. Views, forms and admin never set state directly.
2. The `Event` table is append-only. Corrections are new rows.
3. Documents are immutable: stored under a SHA-256 based path, corrected by a new Document that `supersedes` the old one, deleted only softly. Files are served only through a permission-checked view.
4. Units, locations, categories, SLA days and ageing thresholds are data or settings, not code.
5. Every main table has an `office`; every complaint query goes through `visible_complaints(user)`.
6. Business constraints live in the database (unique round and attempt numbers, one open referral and one pending portal submission per complaint, unique `ref_no` per office), with `on_delete=PROTECT`.

## Project layout

```
config/            settings (base, dev, prod), urls, wsgi
core/              Office, unit, location, category, User and roles
complaints/        the app: models, workflow.py, permissions.py, views, forms
  services/        search, documents (storage and OCR), intake (form pre-fill)
  management/      the `seed` command
  templates/       complaint screens
  tests/           pytest tests
templates/         base layout and login
static/            app.css and vendored HTMX and Pico.css
scripts/           helper scripts (see above)
docs/              the specification PDF
```

## First Admin on a new database

Run `python manage.py createsuperuser` once to reach `/admin/`, then give that user an office, the Admin group and staff status. (`seed` creates a demo `admin` user, for development only.) Then add your real units, locations, categories and SLA days there.

## Status

Built: the full workflow, roles, search, overdue page, document handling, intake pre-fill, seed data, tests.

Not done yet: deployment (host undecided), production compose and reverse proxy, production static file serving, custom error pages, backup and restore scripts, a tag management screen, name search across Hindi and English spellings.

Decide before real use: SLA days per unit or category, how long records must be kept, and whether cases that need no portal upload are closed with "close without portal".
