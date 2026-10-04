# Project: SDM Office Complaint Tracker

Full spec: `docs/SDM Complaint Tracker — Source of Truth.pdf`. Read it before large changes. If code and spec disagree, ask.

## What this is
A Django app for a sub-divisional (SDM) office in Haryana to track citizen complaints through a repeating loop:
notice to a department -> inquiry report -> SDM review (may loop back) -> upload to CM portal (may be rejected and loop back) -> closed.
Scale: ~100 complaints/week, one office, 10-20 users. Do not over-engineer.

## Stack (do not add to it without asking)
Python 3.12, Django 5.2, PostgreSQL 16, Django templates + HTMX (vendored in `static/vendor`), Pico.css, Gunicorn, Docker Compose, pytest + pytest-django, ruff.
NOT allowed: React/Vite, Celery/Redis, DRF API layer, S3, microservices, LLM calls.
Hosting is undecided (free tier target), so keep the app host-agnostic: configuration through env vars (`DATABASE_URL`, `MEDIA_ROOT`).

## Hard rules
1. Every status change of a Complaint goes through `complaints/workflow.py::apply_transition()`. No view, form, admin action or signal may set `current_state`, `current_department` or `current_stage_started_at` directly. (A test greps for this.)
2. `apply_transition()` runs in one DB transaction: validates the transition, writes one Event, creates/closes Referral / InquiryReport / PortalSubmission rows, updates the derived fields.
3. Event is append-only. Never update or delete Event rows.
4. Documents are immutable. Never overwrite a file. Corrections = new Document with `supersedes=old`. Soft delete only (`is_deleted`). Files are stored under a path built from the SHA-256 hash.
5. Departments, villages, categories, SLA days and ageing thresholds are data or settings, never hard-coded in templates or views.
6. Every main table has `office` (FK). Every complaint query goes through `visible_complaints(user)`.
7. Constraints live in the database (unique `round_no` per complaint, unique `attempt_no` per complaint, one open Referral per complaint, one pending PortalSubmission per complaint, unique `ref_no` per office). Use `on_delete=PROTECT`.
8. Files are served only through a permission-checked view, never a public media URL.
9. Secrets only via environment variables (`.env`). `DEBUG` from env, default False.
10. Every new rule gets a test.

## States
RECEIVED, AWAITING_REPORT, UNDER_REVIEW, APPROVED, PORTAL_SUBMITTED, CLOSED

## Transitions (action: from -> to : side effects)
- issue_notice: RECEIVED -> AWAITING_REPORT : new Referral round 1, due_on from SLA
- reissue_notice: AWAITING_REPORT -> AWAITING_REPORT : close open Referral (outcome no_response or superseded), new Referral round+1 (department may change)
- record_report: AWAITING_REPORT -> UNDER_REVIEW : close Referral (report_received), new InquiryReport (review_outcome pending) with its Document (optional)
- review_not_satisfied: UNDER_REVIEW -> AWAITING_REPORT : report not_satisfied + note, new Referral round+1
- review_satisfied: UNDER_REVIEW -> APPROVED : report satisfied + note
- submit_to_portal: APPROVED -> PORTAL_SUBMITTED : new PortalSubmission attempt+1 (pending), ATR document optional
- portal_accepted: PORTAL_SUBMITTED -> CLOSED : submission accepted, closed_on set
- portal_rejected: PORTAL_SUBMITTED -> APPROVED | UNDER_REVIEW | AWAITING_REPORT : submission rejected + reason; next_step = resubmit | review_again | new_inquiry (new_inquiry also creates a new Referral round+1)
- close_without_portal: APPROVED -> CLOSED : reason required (sources that need no CM portal upload)
- reopen: CLOSED -> UNDER_REVIEW : reason required

Any other (state, action) pair raises `InvalidTransition`.

## Derived fields
- current_department = the open Referral's department in AWAITING_REPORT, else null (shown as "SDM office", or "CM portal" in PORTAL_SUBMITTED).
- current_stage_started_at = time of the last transition.
- Days at current office = today - current_stage_started_at (calendar days).
- SLA days = category.sla_days or department.default_sla_days.
- Ageing buckets (settings `AGEING_THRESHOLDS`): 0-2 safe, 3+ warning, 7+ overdue, 10+ critical.

## Style
Small functions, type hints, services in `complaints/services/`, thin views. Plain, readable code. No clever metaprogramming.
