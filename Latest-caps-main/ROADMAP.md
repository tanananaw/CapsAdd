# MD57 System Roadmap

Working notes for cross-session continuity. When starting a new session, point
Claude at this file (or paste the relevant section) and name which item to
work on.

## Already done (this cuts across security, backend, tests, frontend, backend architecture)

- **Security/hygiene**: `md57_carwash.db` untracked from git going forward
  (still in old history — needs a coordinated `git filter-repo` purge later,
  deferred since a teammate has the branch cloned); `requirements.txt` added
  (also fixed a real bug: `reportlab` wasn't installed, app couldn't boot);
  default seed passwords now read from `.env` (`DEFAULT_ADMIN_PASSWORD` etc.)
  instead of being hardcoded; stale dump files removed from repo root.
- **Backend correctness**: dropped dead `workforce_analytics` table; hardened
  the wait-time analytics datetime parsing (implausible/corrupt durations no
  longer silently skew averages); booking service-lookup no longer relies on
  hardcoded `service_id` 1/2/3.
- **Redundancy cleanup**: removed `_Backend/seed_data.py` (100% duplicate of
  `app.py`'s built-in seeding); `_Backend/create_user.py` no longer defines
  its own drifted copy of the `users` table schema.
- **Tests**: `tests/test_booking_logic.py` (slot conflicts, service
  fallback) and `tests/test_ai_engine.py` (AI/MCDA unit tests) added
  alongside the original `tests/test_rbac.py`. 17 tests total.
- **Frontend architecture**: all 9 authenticated pages (`queue_dashboard`,
  `manager_analytics`, `ready`, `booking`, `cancelled`, `homepage`, `staff`,
  `history`, `feedback`) now extend a shared `_Frontend/templates/base.html`,
  pulling from `_Frontend/static/css/style.css` and
  `_Frontend/static/js/common.js` instead of each duplicating the header/nav/
  CSRF-wrapper/toast/footer. Fixed a real bug in the process: every admin
  page except `manager_analytics.html` itself was missing the Analytics nav
  link. Also fixed: `history.html`'s "Export History CSV" read a
  `localStorage` key nothing ever wrote (always empty) — now uses the
  already-fetched history data.
- **Backend architecture**: `_Backend/app.py` split from a 1,911-line
  monolith into `config.py` / `database.py` / `extensions.py` / `utils.py` +
  `blueprints/{auth,pages,admin_api,queue_api,feedback_api,staff_api}.py`.
  All 48 routes verified to register at identical URLs; full live smoke test
  passed (every endpoint, every role's redirect behavior).
- **Dead UI cleanup**: removed dead `#auth-overlay` CSS in
  `admin-dashboard.html` (no matching markup existed), the fake "RFR
  Accuracy/MAE/RMSE" cards in `manager_analytics.html` (permanently frozen
  hardcoded numbers — `/api/analytics-metrics` never returns a
  `model_evaluation` key), the fake static "Live Activity Feed", dead
  `#logoutAnimationModal` markup in `ready.html`, dead `.credentials-hint`
  CSS in `login.html`. Fixed (not removed) `register.html`'s "Confirm
  Password" field — it now actually validates against "Password" via
  `setCustomValidity`, previously did nothing.

## ✅ Item 1 done (2026-09-11)

**Queue Dashboard test timers replaced with real logic**, per user decision:
30-minute grace period past the *scheduled* booking time (not "since staff
glanced at the dashboard"); on expiry the booking is auto-released
(`queue_status` -> `cancelled`, new `cancel_reason` -> `'no_show'`) and the
customer is emailed. Walk-ins (no `booking_date`/`booking_time`) are exempt
— nothing to measure lateness from. No separate "advance to next car" logic
was needed: releasing the row from the `'waiting'` set is what already
makes the next queued car visible to staff.

- New `_Backend/no_show.py`: `process_no_show_bookings()` (the sweep) +
  `start_no_show_sweeper(app)` (daemon thread, 60s interval, only started
  from `app.py`'s `if __name__ == '__main__':` block — never during test
  imports or `python -c "import app"` smoke checks).
- `database.py`: added `reservations.cancel_reason` column
  (`'no_show'` / `'customer'` / `'staff'`).
- `blueprints/queue_api.py`: `api_cancel_booking` now records
  `cancel_reason`; `api_get_cancelled` returns it.
- `queue_dashboard.html`: removed the fake client-side
  `sessionStorage`-based auto-cancel loop and the now-unused
  `isAutoCancel` param on `cancelBooking()`; `getServiceDurationMinutes()`
  (hardcoded `return 1`) replaced with `getServiceDurationMinutesFor(plate)`
  which reads each item's real `duration_minutes` from `/api/get-queue`
  (used by the countdown timer, the est.-wait stat, and completion-time
  estimates); added a 20s dashboard poll so a server-side release becomes
  visible without a manual reload; badge changed from `"TEST (1 MIN
  AUTO-CANCEL)"` to `"30-MIN NO-SHOW AUTO-RELEASE"`.
- `cancelled.html`: auto-released bookings now show an amber "No-Show
  (Auto)" badge instead of the generic red "Cancelled" one.

Verified: 17/17 tests pass; live sweep test against a manufactured
35-minutes-late booking correctly auto-released it while an otherwise
identical walk-in (no scheduled time) was correctly left untouched; live
booking confirmed real per-service `duration_minutes` (e.g. 90min
Detailing + 5min Car = 95) flows through to the dashboard.

---

## ✅ Item 2 done (2026-09-11)

**Booking race condition closed with two layers of defense:**

1. `database.py`: added a partial unique index —
   `CREATE UNIQUE INDEX idx_active_slot_unique ON reservations
   (assigned_bay, booking_date, booking_time) WHERE queue_status IN
   (<active statuses>)`. This is the real guarantee: SQLite enforces it
   atomically regardless of application-level timing. Walk-ins (NULL
   booking_date/time) are exempt since SQL never treats NULLs as equal to
   each other. Cancelling/completing a booking frees the slot automatically
   since the row leaves the partial index's `WHERE` filter.
2. `blueprints/queue_api.py`, `api_submit_booking`: the whole
   check-then-insert section (slot-conflict check → service lookup →
   queue-number assignment → insert) now runs inside an explicit
   `BEGIN IMMEDIATE` transaction (`conn.isolation_level = None` for manual
   control), so a second concurrent request blocks until the first commits
   instead of interleaving with it — this is what actually prevents the
   duplicate-queue-number half of the bug, which the unique index alone
   doesn't cover. A `sqlite3.IntegrityError` on the INSERT (defense in
   depth, in case the index catches something the lock didn't) now returns
   a clean 400 instead of falling through to a generic 500.

**Verified, not assumed**: wrote a throwaway script that fired 10 genuinely
concurrent Python threads (separate SQLite connections, same as separate
Flask worker processes/threads would be) all racing for the *identical*
bay/date/time slot. Result: exactly 1 accepted, 9 correctly rejected, zero
double-bookings, zero duplicate queue numbers. 17/17 existing tests still
pass, including the pre-existing `test_double_booking_same_bay_slot_rejected`
and `test_different_bay_same_slot_allowed`.

**Worth knowing**: Flask's dev server (`app.run()` without `threaded=True`,
which is how this app currently starts) processes one request at a time by
default — so this exact race wasn't actually exploitable under how you're
running it today. It becomes a real risk the moment this gets deployed
behind a real WSGI server (Gunicorn/uWSGI with multiple workers/threads) or
if `threaded=True` is ever added to `app.run()`. The fix is in place
regardless, since that's the correct thing to have before either happens.

---

## ✅ Item 3 done (2026-09-11)

**Server-side past-date/time validation added to `/api/submit-booking`.**
Moved the datetime-parsing logic that already existed in `no_show.py`
(duplicating it would have been the same kind of redundancy flagged
earlier in this project) into `utils.py` as a shared
`parse_booking_datetime(booking_date, booking_time)`, and both
`no_show.py` and `queue_api.py` now call the one copy. In
`api_submit_booking`: if `booking_date`/`booking_time` are both provided
(walk-ins, which have neither, are exempt — same as the slot-conflict
check), the value is parsed and rejected with a 400 if it doesn't parse at
all or if it's earlier than `datetime.now()`.

Verified: 3 new tests added to `tests/test_booking_logic.py`
(`test_past_booking_datetime_rejected`,
`test_future_booking_datetime_still_allowed`,
`test_malformed_booking_time_rejected`) — all pass, 20/20 total. Live HTTP
round-trip also confirmed: a `2020-01-01` booking gets a clean 400
("Booking date/time cannot be in the past."), a real future date still
succeeds normally.

---

## ✅ Item 4 done (2026-09-11)

**Login brute-force lockout added.** New `database.py` table
`login_attempts` (one row per attempt, success or failure) plus a new
`_Backend/login_security.py` module: `is_locked_out(username, ip_address)`
does a sliding-window COUNT (5 failures / 15 minutes) checked independently
by username (stops one account being brute-forced) and by IP (stops one
source spraying many different usernames) — whichever limit is hit first
blocks the request. Checked in `login_page` *before* `check_password_hash`
runs, so a locked-out request skips the password check entirely and
behaves identically whether or not the username exists (doesn't leak
account existence). `record_login_attempt()` logs every attempt.

**Real bug caught and fixed while building this**: the first version
compared a cutoff built from Python's local `datetime.now()` against
`attempted_at` timestamps written via SQLite's `DEFAULT CURRENT_TIMESTAMP`
— which SQLite evaluates in **UTC**. Since local time here runs ahead of
UTC, the sliding-window query silently never matched and the lockout never
triggered — caught by the test suite (2 failing tests) before it ever went
live. Fixed by setting `attempted_at` explicitly in Python at insert time,
matching how `cancelled_at`/`completed_at` are already handled elsewhere
in this codebase (see item 13 below — this exact class of bug already
existed in production code before this fix).

Verified: 3 new tests in `tests/test_login_security.py` (lockout blocks
even the correct password after threshold, applies the same to
nonexistent usernames, doesn't trigger below threshold) — 23/23 total
pass. Live HTTP test against the running server: 5 wrong passwords for
`customer_user`, then the 6th attempt with the *correct* password still
blocked with the lockout message.

---

## ✅ Item 5 done (2026-09-11)

**ID verification dead end removed — stopped collecting IDs entirely**, per
user decision (the alternative, building a real staff review screen for a
feature nothing in the UI ever exposed, was explicitly declined). Confirmed
first that this was genuinely 100% dead: grepped every template and found
`booking.html` never rendered a file-upload input at all — the whole
collection path was unreachable from the actual booking flow, meaning any
`valid_id_image`/`id_verified_status` values already in the database were
pure collect-and-forget PII with zero function.

- `database.py`: removed both columns from the `reservations` CREATE TABLE,
  and replaced their old "ADD COLUMN if missing" migration blocks with
  "DROP COLUMN if present" ones — dropping (not just no-longer-writing)
  matters here because it also destroys any images/status already stored
  on existing databases, which is the actual privacy fix.
- `utils.py`: removed `verify_id_with_ai()` (the function backing the
  never-reachable AI "verification" step).
- `blueprints/queue_api.py`: `api_submit_booking` no longer accepts/reads
  an uploaded ID file, base64-encodes it, or writes `id_verified_status`/
  `valid_id_image` into the INSERT; `api_get_queue`'s SELECT and PII-masking
  logic no longer reference `valid_id_image`.
- `database/md57_schema.sql`: reference schema updated to match.
- `tests/test_booking_logic.py`: removed
  `test_id_verified_status_reflects_whether_id_was_submitted` (asserted
  behavior of a column that no longer exists).

Verified: 22/22 tests pass. Live smoke test against the running server —
logged in as `customer_user`, fetched a real CSRF token, submitted a
booking via `/api/submit-booking` with no ID fields at all — succeeded
cleanly and the booking appeared correctly in `/api/get-queue` (including
the still-working email-masking logic for non-staff viewers), confirming
the removal didn't break the booking path it shares code with.

---

## ✅ Item 6 done (2026-09-11)

**Feedback now requires a real, completed, owned reservation** — built the
real submission flow rather than just hardening the unreachable backend
endpoint, per user decision. Investigation first found the problem was
bigger than the roadmap description: `/feedback` (`feedback.html`) was a
read-only reviews table with no submission form anywhere, and
`/api/submit-feedback` was completely uncalled from any template — the
same "dead endpoint, zero UI" pattern as item 5's ID verification. Also
found `/api/my-bookings` (a customer's-own-bookings endpoint) existed but
was likewise never called from anywhere in the frontend.

- `database.py`: `feedbacks` gained `reservation_id`/`user_id` columns
  (FK to `reservations`/`users`) plus a partial unique index
  (`idx_feedback_reservation_unique`, `WHERE reservation_id IS NOT NULL`)
  enforcing one review per reservation at the database level. Existing/
  seeded demo rows keep `reservation_id = NULL` and are exempt from that
  constraint.
- `blueprints/feedback_api.py`, `api_submit_feedback`: now takes only
  `reservation_id` + `rating` + `comment` from the client — `customer_name`/
  `plate_number`/`service_type` are read from the reservation itself
  server-side, not trusted from the request body. Validates, in order: the
  reservation exists (404), it's actually `completed` (400 — can't review a
  visit that hasn't happened), a logged-in customer owns it (403 — staff/
  admin are exempt, e.g. entering feedback a walk-in gave in person), and no
  feedback already exists for it (409 — the fake/duplicate-review hole the
  roadmap item was about).
- `blueprints/queue_api.py`, `api_my_bookings`: now LEFT JOINs `feedbacks`
  and returns `has_feedback`, so the frontend can tell which completed
  visits are still reviewable.
- `feedback.html`: added a "Leave a Review" card, shown only for
  `role == 'customer'`, that fetches `/api/my-bookings`, lists completed +
  not-yet-reviewed visits in a dropdown with a star-rating picker and
  comment box, and posts to `/api/submit-feedback`. Refreshes both the
  reviewable list and the reviews table on success.
- `database/md57_schema.sql` updated to match.

**Real bug caught along the way**: `tests/test_rbac.py`'s `tearDown`
deleted `reservations` before `feedbacks` — with the new FK in place, that
threw `IntegrityError`, which skipped `conn.close()` and left the SQLite
file locked for every test that ran afterward (`database is locked`
cascading through 4 unrelated tests). Fixed by deleting `feedbacks` first.

Verified: 27/27 tests pass, including a new `tests/test_feedback_logic.py`
(own-completed-reservation succeeds, not-yet-completed rejected,
someone-else's-reservation rejected, duplicate-on-same-reservation
rejected, nonexistent-reservation rejected) and an updated
`test_rbac.py::test_feedback_system`. Live smoke test against the running
server: booked as `customer_user`, ran it through
start-service/complete-service/archive-ready as staff, confirmed
`/api/my-bookings` showed `has_feedback: 0`, submitted a real review,
confirmed it flipped to `has_feedback: 1` and appeared in
`/api/get-feedbacks`, and confirmed a second submission for the same
reservation was rejected with 409.

---

## ✅ Item 7 done (2026-09-11)

**`vehicle_type` now validated against the `vehicle_types` table in
`api_submit_booking`.** The booking UI's dropdown only ever sends one of
the 4 seeded values, so this wasn't reachable through the normal booking
flow - same "only a problem via direct API call" shape as items 5/6's
unreachable-endpoint issues, just smaller: a raw request could send any
string and it would silently get a 0-minute duration multiplier via the
`COALESCE(vt.multiplier_minutes, 0)` fallback in `/api/get-queue`,
understating that booking's real service time in the queue/analytics.

- `blueprints/queue_api.py`, `api_submit_booking`: added a case-insensitive
  lookup against `vehicle_types` right after the transaction begins (same
  place/pattern as the slot-conflict and service-lookup checks that follow
  it). No match -> 400 with the list of valid types. A match normalizes
  `vehicle_type` to the table's exact stored casing before the INSERT -
  this incidentally also closes a related latent bug where a
  differently-cased but "valid" value (e.g. `"van"` instead of `"Van"`)
  would have been accepted but then silently mismatched the exact-match
  JOIN in `/api/get-queue` anyway, since SQLite's default string comparison
  is case-sensitive.

Verified: 2 new tests in `tests/test_booking_logic.py`
(`test_unrecognized_vehicle_type_rejected`,
`test_vehicle_type_matched_case_insensitively`) - 29/29 total pass. Live
HTTP test against the running server: `"vehicle_type": "Spaceship"` gets a
clean 400 listing the real options; `"vehicle_type": "van"` (lowercase)
succeeds and is stored/normalized as `"Van"`, with `/api/get-queue`
correctly showing the real 45-minute duration (20 base + 25 Van
multiplier) instead of a silently-wrong 20.

---

## ✅ Item 13 done (2026-09-11, fixed as part of item 11)

**Latent UTC/local timezone mismatch in wait-time analytics, resolved.**
Originally found while fixing the identical class of bug in item 4
(`get_analytics_metrics`'s "actual wait time" diffed `reservations
.created_at`, written via SQLite's `DEFAULT CURRENT_TIMESTAMP` - UTC -
against `completed_at`, written explicitly via Python's local
`datetime.now()`), and deferred at the time since it was a different
endpoint outside item 4's scope. Building item 11's AI retraining pipeline
made this a hard prerequisite rather than an optional cleanup: retraining
depends on that exact same `completed_at - created_at` diff being honest,
so `api_submit_booking` now sets `created_at` explicitly in Python (local
time) at insert time, the same pattern already used for `cancelled_at`/
`completed_at`. Fixes both `get_analytics_metrics`'s wait-time chart and
the new AI retraining data going forward; existing rows created before
this fix keep whatever `created_at` SQLite's UTC default already wrote and
aren't retroactively corrected.

---

## ✅ Items 8 & 9 done (2026-09-11)

**Admin pages restyled onto `base.html` and added to the nav**, per user
decision (kept both pages rather than folding `/admin` into `/analytics` —
lower risk, no functionality lost). Checked first whether `/admin` was
truly redundant with `/analytics` as the roadmap note suggested: it isn't
— `/admin`'s live metric counts (active queue / completed-ready /
cancelled, from `/api/admin/metrics`) aren't shown anywhere in
`manager_analytics.html`.

- `admin-dashboard.html`: rebuilt on `{% extends "base.html" %}` instead of
  its own full `<html>`/dark-slate-sidebar/amber-accent document. Same
  content as before (live metrics strip + quick-link cards to every admin
  section, including the previously-orphaned Create User page) restyled
  with the shared `--card-bg`/`--accent-amber`/`--text-muted` tokens and
  the site's existing `.card`/`.cards-grid` pattern (from `homepage.html`)
  and `.stat-card`/`.stats-grid` pattern (from `feedback.html`), instead of
  inventing new ones.
- `create_user.html`: rebuilt the same way - was a bare dark-mode
  (`#121212`/teal) document with no header, footer, or nav at all. Now a
  single centered form card in the shared theme. Both pages also dropped
  their own hand-rolled CSRF-fetch-patch `<script>` (duplicated inline in
  each) since `base.html` already loads that once, globally, via
  `common.js`.
- `base.html`: added "🛡️ Dashboard" (`/admin`) and "➕ Create User"
  (`/admin/create-user-page`) to the nav dropdown, admin-only - closing the
  actual reachability gap. `role_required('admin')` on both routes was
  already correct and unchanged; the fix was purely that nothing linked to
  them.

Verified: 29/29 tests pass (template/nav-only change, no backend logic
touched). Live test against the running server: logged in as `manager`,
confirmed both pages render inside the shared header/nav/footer, confirmed
the live metrics strip still polls and displays real counts, confirmed the
create-user form still successfully creates an account end-to-end through
its (now page-level, not inline) CSRF token. Logged in as staff and
confirmed neither nav link appears and both routes still redirect away
(RBAC unaffected by the template rewrite).

---

## ✅ Item 10 done (2026-09-11)

**Staff roster now backed by real seeded accounts, and the fake PIN gate
is gone**, per user decision (session identity replaces the PIN entirely
rather than making the PIN mechanism itself real). The old PINs
(`"1234"`, `"5678"`, `"4321"`) were hardcoded in this page's own JS source
- readable by anyone via browser devtools - and verified nothing real: the
backend never checked `staff_name` against any actual account either.

- `blueprints/staff_api.py`: new `GET /api/get-staff-list` returns the real
  `users` rows where `role = 'staff'`. `POST /api/staff-attendance` no
  longer trusts a client-supplied `staff_name` for a staff-role session -
  it always uses the session's own identity (`session['fullname']`,
  falling back to `session['user']`, the same fallback `auth.py` already
  uses when populating the session at login). An admin-role session may
  still specify any `staff_name`, but it's now validated against real
  staff accounts (400 "Unknown staff member" otherwise). `POST
  /api/staff-bonus` (admin-only) gained the same real-account validation.
- `staff.html`: hardcoded `staffListData` replaced with a fetch from
  `/api/get-staff-list` on load. The time-clock modal's PIN input is gone;
  a staff-role session sees only their own name and Time In/Out buttons
  (no picker - nothing to pick, they can only clock themselves), an
  admin-role session sees a dropdown of real staff to clock in/out on their
  behalf (e.g. a front-desk correction).
- `base.html`: `CURRENT_USER` gained `user_id` (from `session['user_id']`,
  already set at login but not previously exposed to page JS) so staff.html
  can match "this session's own staff row" by id rather than by
  string-matching a name.

**Real bug caught while writing tests**: the live dev database's 5 seeded
staff rows all have `fullname = NULL` (stale rows from before the
fullname-seeding code existed; the seed logic only inserts when a username
doesn't already exist, so it never backfilled them). Not a new bug -
`auth.py`'s login and the new `/api/get-staff-list` both already
`COALESCE`/fall back to `username` for this - but it means a naive fix
reading raw `fullname` anywhere would silently break. Caught by a test
that initially read the raw column and got `None`.

**New issue found while investigating, deliberately not fixed here (out of
scope for "match the roster to real accounts")**: `/api/get-ready` has no
`washer`/`assigned_staff` field at all - no schema column exists to record
which staff member actually handled a job. `staff.html`'s commissions
table (`loadStaffData()`) was papering over this with `car.washer ||
staffListData[index % staffListData.length].name` - a deterministic
round-robin fabrication that fires on *every* row, 100% of the time, since
`car.washer` never exists. That fake rotation is now attributing
fabricated commission money to the 5 **real** staff members' real names
instead of 3 fictional ones, which if anything makes it look more
legitimate than before. Fixing this for real means adding an actual
job-assignment mechanism (e.g. `assigned_staff_id` on `reservations`, set
when a staff member starts/completes a service) - a meaningfully bigger
feature than this item's scope, flagged here the same way item 13 was.

Verified: 35/35 tests pass, including a new `tests/test_staff_roster.py`
(real roster returned and excludes the old fictional names, a staff
session cannot spoof another name into `/api/staff-attendance`, admin can
record attendance/bonus for a real staff member, both reject an unknown
name with 400). Live test against the running server: logged in as real
staff `TJ Barcebal`, POSTed a spoofed `staff_name` in the attendance
request body and confirmed the database recorded `"TJ Barcebal"` (the
session identity) rather than the spoofed value; logged in as `manager`
and confirmed clocking in a real staff member succeeds while clocking in
the old fictional `"Juan Dela Cruz"` now correctly gets rejected.

---

## ✅ Item 11 done (2026-09-11)

**Wait-time model now retrains on real completed bookings, per user
decision** (start capturing real operational data going forward + a
periodic auto-retrain job, rather than a one-off manual button or just
labeling the model as synthetic-only). Investigated first what a real fix
actually required: the model's features are `queue_length` and
`active_staff` *at the time of the booking* - neither was ever recorded
anywhere, only today's live counts exist, so no historical training data
could be reconstructed from existing rows. Had to start capturing that
context going forward; nothing here can retroactively fix older bookings.

- `database.py`: `reservations` gained `queue_length_snapshot` and
  `active_staff_snapshot` (nullable - old rows stay `NULL` and are excluded
  from training, not backfilled with a guess).
- `blueprints/queue_api.py`, `api_submit_booking`: captures both snapshots
  right before the INSERT, inside the existing transaction - queue length
  is a same-`service_id` count of active reservations (mirrors how the
  booking form already computes its live EWT preview), active staff count
  mirrors `staff.html`'s own "currently on-duty" definition (clocked in
  today, `Present`/`Late`, hasn't clocked out). Also now sets `created_at`
  explicitly in Python (local time) instead of leaving it on the column's
  `DEFAULT CURRENT_TIMESTAMP` (SQLite evaluates that in UTC) - this was
  item 13's root cause, and training on `completed_at - created_at` would
  have silently inherited that bug (a constant local/UTC offset error on
  every real training row) if left unfixed. `completed_at` was already
  set explicitly the same way, so this makes both sides of that diff
  consistent.
- `ai_engine.py`: `WaitTimePredictorAI` refactored so the synthetic
  bootstrap dataset is reusable (not just fit-and-discard), plus a new
  `retrain_with_real_data(real_rows)`: below `MIN_REAL_SAMPLES_TO_RETRAIN`
  (20) it's a no-op (too few real rows to trust over noise); at/above it,
  blends real rows with the synthetic set (as a floor/regularizer so a
  still-small real sample can't swing predictions wildly) and fits a
  **new** `RandomForestRegressor` instance, then does a single atomic
  attribute swap (`self.model = new_model`) rather than mutating the live
  model in place - so a concurrent `predict_ewt()` call from a request
  thread is never handed a half-fitted model. Real precipitation in mm was
  never persisted anywhere (only a Sunny/Cloudy/Rainy bucket) - real rows
  use `0.0` for that feature, the same convention `get_analytics_metrics`
  already uses for live predictions, so train and predict stay consistent
  rather than inventing a number that was never measured.
- New `_Backend/ai_retrain.py`: `_fetch_real_training_rows()` pulls
  completed/ready reservations with both snapshots present, computes an
  honest actual-wait-time, and drops implausible durations (same
  480-minute bound `get_analytics_metrics` already uses). A background
  daemon thread (`start_ai_retrain_scheduler`, same pattern as
  `no_show.py`'s sweeper) runs one retrain cycle immediately at startup
  and then every 6 hours - retraining doesn't need to react as fast as the
  no-show sweep, real completed bookings accumulate slowly. Started only
  from `app.py`'s `if __name__ == '__main__':` block, never during test
  imports.

**New issue found while investigating, deliberately not fixed here**: the
booking form's live EWT preview (`booking.html`) still hardcodes
`active_staff: 3` client-side with a comment admitting it isn't tracked -
unrelated to the training pipeline itself (that preview call doesn't touch
the database), but the same "fake number presented as real" pattern this
item was about. Small, separate fix - flagged rather than folded in here.

Verified: 44/44 tests pass, including new tests in `test_ai_engine.py`
(below-threshold real data doesn't retrain, at-threshold retrains and
swaps the model, a retrained model still predicts normally), a new
`tests/test_ai_retrain.py` (snapshot-missing/wrong-status/implausible-
duration rows correctly excluded, valid rows correctly included with the
right computed wait time), and a new booking-logic test confirming
`queue_length_snapshot`/`active_staff_snapshot`/explicit local `created_at`
all get set on submit. Live end-to-end test against the running server:
seeded 20 real completed bookings with snapshots directly in the database,
restarted the server, and confirmed the startup log showed `"Retrained
wait-time model with 20 real completed booking(s) blended with the
synthetic baseline"` - then confirmed `/api/predict-ewt` still returned a
normal prediction afterward, proving the swapped-in model works.

---

## ✅ Item 12 done (2026-09-11)

**staff.html's Audit Logs modal now shows the real server-side log**, not
`localStorage`. Confirmed first that the real log (`system_activity.log`,
written by `utils.log_activity()`) was already far more comprehensive than
the fake one it was replacing: 16 call sites across the codebase already
log logins/logouts, registrations, bookings, cancellations, restores,
start/complete-service, archiving, feedback submissions, email
notifications, PDF exports, and no-show auto-releases - the fake
client-side version only ever recorded two kinds of event (staff time-
clock and bonus additions), and only in the browser that triggered them.

- `blueprints/staff_api.py`: `api_staff_attendance` and `api_staff_bonus`
  now call `log_activity()` themselves (they didn't before) - this was a
  prerequisite, not just plumbing: without it, wiring the UI straight to
  the real log would have silently dropped the two event types the old
  fake log did track.
- `blueprints/admin_api.py`: new admin-only `GET /api/get-audit-logs`
  parses `system_activity.log`'s `[timestamp] User: X - Action: Y` lines
  and returns the most recent N (default 100, capped 500) newest-first.
  Deliberately has no companion delete/clear endpoint - the old fake
  version had a "Clear Logs" button, but an audit trail the people being
  audited can erase isn't much of one, and the real file is meant to be
  the durable record. Log rotation/growth over the app's lifetime is a
  separate ops concern, not addressed here.
- `staff.html`: `renderAuditLogs()` now fetches from that endpoint instead
  of reading `localStorage`; `logActivity()` and `clearAuditLogs()` (and
  their call sites, and the "🗑️ Clear Logs" button) removed entirely -
  nothing client-side needs to log these events anymore since the backend
  does it directly. Table's "Role" column renamed "User" to match what the
  real log actually records (who did it, not what role they were logged
  in as).

Verified: 49/49 tests pass, including a new `tests/test_audit_log.py`
(admin-only access enforced, entries come back newest-first, `limit` is
respected, a staff time-clock action and an admin bonus action each
produce a real matching log entry). Live test against the running server:
clocked in a real staff member and added a bonus as admin, then confirmed
`/api/get-audit-logs` showed both new entries at the top, correctly
interleaved with unrelated real events (logins, bookings, feedback) from
elsewhere in the system - something the old per-browser fake log could
never have shown. Confirmed a staff-role session gets a clean 403 from the
endpoint, matching the modal's existing admin-only UI gating.

---

---

## ✅ Items 27-31 done (2026-09-12 to 2026-09-13): post-Phase-2 session

Undocumented until now - work delivered directly from user requests rather
than off this roadmap's open-item list. Summarized here for continuity.

**27. Real per-staff Speed/Completion/Rating + `started_at`.** Manager
Analytics' "Performance Metrics Control" panel used to be a manual
simulator (admin typed in numbers by hand). `reservations.started_at`
added (set on `/api/start-service`, distinct from `created_at` which
includes queue wait); new `/api/staff-real-metrics` computes Speed
(benchmark/actual duration), Completion % (resolved jobs only), and Rating
(real feedback) per staff member from real data, auto-loaded into the
panel. Also added real MAE/RMSE model-accuracy tracking
(`/api/model-accuracy`), replacing frozen hardcoded cards, and removed the
dead "Quick Gmail Test" widget.

**28. Fixed `completed_at` was fake.** Traced from "how does the system
know a car is done?" - `completed_at` was set by a client-side countdown
timer auto-firing at the benchmark duration, not a real staff action,
undermining the AI retraining/MAE work above. Replaced with a manual
"✅ Mark as Complete" button; the countdown is now display-only.

**29. Per-bay washer indicator + online/walk-in view clarified.** Bay
headers, per-car cards, and the detail modal now show who's currently
assigned to a bay (`/api/get-queue`'s LEFT JOIN on `assigned_staff_id`).
Relabeled the "Online Bookings" vs. "Walk-in Queue" views (they're the
same 3 physical bays filtered by how the customer joined, not 6 bays -
was a real source of confusion).

**30. Automated staff recommendation.** New `/api/recommend-staff`: ranks
on-duty, not-currently-busy staff by the existing MCDA/SAW formula and
surfaces "Suggested: NAME" in the detail modal plus a dashboard alert when
the logged-in staff member is the one recommended. Advisory only - staff
still self-claim by clicking Start.

**31. Bookings auto-decline when no washer is on duty.** `/api/submit-booking`
now checks `get_active_staff_count()` for bookings dated today (walk-ins
included); 409 with a clear message if nobody's clocked in. A future-dated
booking is unaffected - that's a different day's staffing.

**32. Lifecycle actions locked to admin; two-step completion confirmation.**
Per user decision, several actions that were `role_required('admin',
'staff')` are now admin-only: `/api/staff-attendance` (staff can no longer
clock themselves in), `/api/restore-booking`, `/api/send-email`
(notifying a customer), `/api/archive-ready`. Bigger change: staff marking
a job done no longer goes straight to Ready - it lands in a new
`pending_confirmation` status until admin calls the new
`/api/confirm-complete` (-> Ready) or `/api/reject-complete` (-> back to
`in_progress`, same staff member picks it up again). Admin completing a
job directly still goes straight to Ready. Also: `/api/get-history` now
includes cancelled bookings automatically (previously completed-only), and
washer attribution was added to the Ready and History pages (previously
only visible on the live queue, so it vanished once a job finished).

**33. Full security/confidentiality audit (items 15 & 21 above, plus):**
`/api/get-feedbacks` (public testimonials wall, intentional) was leaking
full plate numbers to any customer - masked, same tail-kept pattern as the
queue. Self-registration and admin's Create User had no password-length
check (reset flows already enforced 6+ chars) - now consistent everywhere.
`create_user.html` gained a live "Can / Cannot" role-specification panel
so whoever's provisioning an account sees its real capabilities first.
Also: `get_db_connection()` gained `timeout=30` (a real leaked-connection
bug in a new test was cascading into `database is locked` failures across
the suite - fixed at the source, but the timeout is good defense-in-depth
regardless). Deleted `repomix-output.xml` (588KB AI-context dump) and
`_Backend/create_user.py` (dead CLI script that bypassed the new
password-length check entirely - a real gap on its own). Weather lookup
(`utils.py`) now pins `,PH` on the OpenWeatherMap query, matching what
`manager_analytics.html`'s widget already did - the backend snapshot used
for AI training wasn't locked to the Philippines the same way.

Verified throughout: full suite at 126/126 passing; every item above was
also live-tested against the running server (curl round-trips for the API
changes, rendered-HTML checks for the UI changes), not just unit-tested.

---

## ✅ Item 34 done (2026-09-14): staff clock-in confirmation workflow

Follow-up to item 32's admin-only attendance change, per user idea: instead
of staff being fully locked out of the clock-in flow, they can request it
and admin confirms - same shape as the existing job-completion confirmation
workflow (item 32), applied to attendance too. Also fixed a related real
bug found live: a staff account had started a job without ever being
clocked in (`/api/start-service` had no on-duty check at all) - closed
with a new `is_staff_on_duty()` gate (403 if a staff session tries to
start a job while not clocked in; admin unaffected).

- `staff_attendance.status` gained a `'Pending'` value alongside the
  existing Present/Late/Absent - no schema change needed, and it's
  automatically excluded from every existing on-duty check (`get_active_
  staff_count`, `is_staff_on_duty`, `/api/recommend-staff`) since those all
  filter `status IN ('Present', 'Late')`.
- `utils.py`: new `get_latest_attendance(staff_name, date_str=None)` -
  the "latest attendance_id wins" resolution pattern was already
  duplicated 3+ times across this codebase; factored out once and reused
  everywhere it's needed now (`is_staff_on_duty` refactored to use it too).
- `blueprints/staff_api.py`: `POST /api/staff-clock-in-request`
  (staff-only) inserts a `Pending` row with the real requested time -
  blocks if already clocked in or already has an outstanding request.
  `GET /api/clock-in-requests` (admin-only) lists everyone currently
  Pending. `POST /api/confirm-clock-in` (admin-only) turns it into
  Present/Late - using the *requested* time_in, not the confirm moment,
  and auto-detecting Late past 8:15 AM (the same cutoff the old, removed
  client-side self-service check used). `POST /api/reject-clock-in`
  (admin-only) turns it into Absent.
- `staff.html`: staff see a "🙋 Request Clock-In" button (replacing the
  now admin-only Time Clock trigger for their role) and a banner while
  their request is pending. Admin sees a new "Pending Clock-In Requests"
  card with Confirm/Reject per request, plus the attendance table now
  renders a distinct purple "Pending" badge instead of falling through to
  the red "everything else" bucket.
- `blueprints/queue_api.py`, `api_start_service`: now calls
  `is_staff_on_duty()` for a staff-role session before allowing the job to
  start - 403 with a clear message if not clocked in.

Verified: 143/143 tests pass, including a new `tests/test_clock_in_
request.py` (request/confirm/reject happy paths, double-request and
already-clocked-in rejected, late-cutoff detection, requested time
preserved through confirmation, role checks on every new endpoint, and an
end-to-end tie-in confirming a freshly-confirmed staff member can then
actually start a service) plus 4 new tests in `test_washer_assignment.py`
for the on-duty gate itself. Live test against the running server: staff
requested a clock-in, admin saw it in the pending queue, confirmed it
(correctly marked Late given the time of day), and `/api/recommend-staff`
immediately reflected them as available.

---

## How to resume

Tell Claude: *"Check ROADMAP.md in the project root, let's do item N."*
Claude should re-read the relevant file(s) named above before making
changes, since code may have shifted since this was written.

---

## ✅ Post-roadmap cleanup done (2026-09-11): the two loose ends from items 10 & 11

After all 13 roadmap items were done, two smaller fake-data spots were
flagged but deliberately left unfixed at the time (noted in items 10 and
11 above). Fixed both on request:

**1. Fake round-robin washer assignment (from item 10).** `/api/get-ready`
never had a `washer` field - nothing recorded which staff member actually
handled a job - so `staff.html`'s commissions table papered over it with
`car.washer || staffListData[index % staffListData.length].name`, a
deterministic rotation that fired on every single row.

- `database.py`: `reservations` gained `assigned_staff_id` (nullable FK to
  `users`).
- `blueprints/queue_api.py`, `api_start_service`: now records
  `assigned_staff_id = session['user_id']` when a staff-role session
  starts the job - the natural "claiming" moment, and the UPDATE's
  existing `WHERE queue_status IN (...)` clause means this can only ever
  fire once per booking, so a later re-click (by anyone) can't reassign
  it. Admin-started jobs get `NULL` - admins aren't washers, so no
  attribution is invented for them.
- `blueprints/queue_api.py`, `api_get_ready`: now LEFT JOINs `users` on
  `assigned_staff_id` and returns the real name as `washer`.
- `staff.html`, `loadStaffData()`: the round-robin fallback is gone - a
  job with no recorded washer (admin-started, or from before this existed)
  is left unattributed rather than fabricated onto a random staff member.
  Added a small note under the commissions table when this happens, so the
  gap is visible instead of silently hidden. "Total Vehicles Completed"
  now counts every ready/completed car directly (`readyList.length`)
  instead of summing only the attributed ones, so it can't shrink just
  because some jobs lack attribution.

**2. Hardcoded `active_staff: 3` in the live EWT preview (from item 11).**
Three call sites (`booking.html` x2, `queue_dashboard.html` x1) sent a
constant `active_staff: 3` to `/api/predict-ewt` with a comment admitting
it wasn't tracked client-side.

- `utils.py`: new shared `get_active_staff_count(date_str=None)` - the
  same "latest attendance row per staff, Present/Late, not yet clocked
  out" query already used for item 11's booking-time snapshot, now
  reusable. Also replaced a *second*, cruder pre-existing version of this
  same idea in `get_analytics_metrics` (`SELECT COUNT(*) WHERE status =
  'Present'` with no per-staff dedup and no date filter - it counted every
  Present row ever logged across every past day, not who's on duty now).
- `blueprints/staff_api.py`, `api_predict_ewt`: now calls
  `get_active_staff_count()` itself and ignores whatever `active_staff`
  the client sends - moves authority server-side instead of trusting a
  guess, so the three frontend call sites no longer need to send anything
  for it at all (removed the field and the stale comment from all three).

Verified: 59/59 tests pass, including two new test files -
`tests/test_washer_assignment.py` (staff-started jobs get attributed,
admin-started jobs don't, `/api/get-ready` reflects both cases correctly,
re-starting an already-claimed job can't reassign it) and
`tests/test_active_staff_count.py` (counts Present/Late-still-clocked-in
correctly, excludes clocked-out and absent staff, only the latest of
multiple same-day rows per staff counts, defaults to today when no date is
given). Live end-to-end test against the running server: clocked in TJ
Barcebal, booked/started/completed a walk-in as him, confirmed
`/api/get-ready` returned `"washer": "TJ Barcebal"`; called
`/api/predict-ewt` twice with wildly different client-sent `active_staff`
values (999 and 0) and confirmed both returned the identical prediction
(the server-computed real count of 1 on-duty staff member, verified
directly), proving the client value is now fully ignored.

---

# Phase 2 (2026-09-11): fresh full-system re-analysis

With all 13 original items plus the two follow-ups done, did a second
top-to-bottom pass over the whole system: backend logic, security,
redundancy, and what's genuinely missing feature-wise. Item 14 was fixed
immediately given its severity (per user decision); items 15-26 are still
open analysis, not yet implemented.

## ✅ Item 14 done (2026-09-11): Google OAuth shared-password bypass

**Fixed immediately given severity.** `google_auth_callback` used to set
every Google-created account's password to
`generate_password_hash('google_auth_secure_2026')` - a fixed string in
source control, meaning anyone who read `auth.py` could log into any
Google-authenticated customer's account through the ordinary `/login`
form, no access to the victim's actual Google account required.

- `database.py`: `users` gained `auth_provider TEXT` (nullable - `'google'`
  for OAuth-created accounts, `NULL` for everyone else).
- `blueprints/auth.py`, `google_auth_callback`: new accounts now get
  `generate_password_hash(secrets.token_hex(32))` - a random value
  discarded immediately after hashing, known to nobody - and
  `auth_provider = 'google'`.
- `blueprints/auth.py`, `login_page`: checks `auth_provider == 'google'`
  *before* attempting any password comparison and refuses password login
  outright with a clear "use Google Sign-In" message. This is the real
  fix - it closes the hole even for any already-existing row that still
  has the old shared hash, not just new signups, since it never even
  reaches the password check for those accounts.

Verified: 62/62 tests pass, including a new
`tests/test_google_auth_security.py` (the old known shared password is
explicitly rejected, any password attempt against a Google-linked account
is rejected, a regular non-Google account still logs in normally - the
regression guard). Live test against the running server: seeded a
Google-linked test account, attempted login with the literal old shared
password string, confirmed it was refused with the Google Sign-In message
instead of granting access; confirmed a normal account's password login
was unaffected.

---

## ✅ Item 15 done (2026-09-13): `/api/get-queue` was fully public

Fixed as part of a full security/confidentiality audit (see item 31).
`@role_required('admin', 'staff', 'customer')` added - it had none at all
before, reachable by anyone, logged in or not. Customers now see their own
booking in full but every other customer's row is masked (name -> generic
"Customer", email partially hidden as before, plate reduced to a
3-character tail like `***234`) - a shared "where am I in line" view, not
a directory of everyone else's booking. `/api/booked-slots` had the same
missing-auth problem (no PII in its response, but still reachable
anonymously) and got the same role check.

## 🟠 High

### ✅ 16. Reference schema's indexes were never real - done (2026-09-14)
Added the three real indexes to `database.py`'s `init_db()`:
`idx_reservations_status`, `idx_reservations_plate`,
`idx_reservations_date`. `idx_users_username` deliberately *not* added
explicitly - `users.username` is already `UNIQUE`, which SQLite indexes
implicitly on its own, so a same-named explicit index would just be a
redundant duplicate. `database/md57_schema.sql` updated to note that and
to point out it's documentation only (database.py is what actually runs).
Verified: 3 new tests in `tests/test_database_indexes.py` confirm the
indexes exist in the live `sqlite_master`; live check against the running
server's actual database confirmed the same.

---

## ✅ Item 17 done (2026-09-11): self-service password reset

Built both gaps the item named: a real self-service "Forgot password"
email flow, and an admin-facing manual reset action.

- `database.py` / `md57_schema.sql`: new `password_resets` table (one row
  per issued link: `user_id`, unique `token`, `created_at`, `expires_at`,
  `used`), indexed on `token` (lookup) and `(user_id, used, created_at)`
  (the cooldown check).
- `blueprints/auth.py`:
  - `GET/POST /forgot-password` - takes a username, always returns the
    *same* generic "if an account exists..." message regardless of
    whether it does, is Google-linked, or the email send succeeds -
    anything that varied by those cases would let an attacker enumerate
    real accounts. A real account (not Google-linked) gets a
    `secrets.token_urlsafe(32)` token, valid 30 minutes, emailed as a
    reset link via the existing Flask-Mail setup (same pattern as
    `no_show.py`'s notification emails). A repeat request within 2 minutes
    of an still-valid outstanding token is throttled - no second token,
    no second email.
  - `GET/POST /reset-password?token=...` - validates the token exists,
    is unused, and hasn't expired before showing the form at all. On
    submit: requires the two password fields to match and be 6+
    characters, updates `password_hash`, and marks *every* outstanding
    token for that account used (not just the one redeemed) - an older,
    unopened link shouldn't stay valid after a successful reset.
  - New templates `forgot_password.html` / `reset_password.html`,
    styled to match the existing `login.html` (same pre-auth page family,
    not yet carried over to the new navy/gold theme). `login.html` gained
    a "Forgot password?" link and a `?reset=1` success banner.
- `blueprints/admin_api.py`: new admin-only `POST
  /api/admin/reset-user-password` (username + new password) for when a
  customer can't/didn't use the email link - refuses Google-linked
  accounts (nothing to reset) and unknown usernames, and invalidates any
  outstanding self-service tokens the same way a successful self-service
  reset does. `create_user.html` gained a second card wired to it.

**Real bug caught and fixed while building this**: the first version of
the cooldown check compared a cutoff built from Python's local
`datetime.now()` against `created_at` written via SQLite's `DEFAULT
CURRENT_TIMESTAMP` - UTC. Exact same class of bug as `login_security.py`'s
original lockout bug (item 4) and the analytics timezone bug (item 13) -
the throttle silently never triggered, caught by
`test_forgot_password_throttles_repeated_requests` before it shipped.
Fixed by setting `created_at` explicitly in Python at insert time,
matching the pattern used everywhere else in this codebase now.

Verified: 76/76 tests pass, including a new `tests/test_password_reset.py`
(generic message for unknown/Google/real accounts, token created only for
a real non-Google account, repeat requests throttled, bogus/expired
tokens rejected, a valid token resets the password and can't be reused,
mismatched confirmation and short passwords rejected without touching the
stored hash) and admin-reset tests (real account succeeds, Google account
and unknown username rejected, non-admin gets 403). Live end-to-end test
against the running server: requested a reset for the real `customer_user`
account, pulled the real token from the database, completed the reset,
confirmed the *old* password (`customer123`) was rejected and the *new*
one logged in successfully, confirmed the token couldn't be reused
afterward ("Link Expired"), then restored the account's original password
so the rest of the test environment stays consistent. Also verified the
admin reset form renders on `create_user.html` and the endpoint correctly
rejects an unknown username with 404.

---

## 🟡 Medium

### ✅ 18. `/api/mark-ready` dead duplicate route - removed (2026-09-14)
Confirmed zero references anywhere in `_Frontend/templates` and deleted
the route entirely. Verified: `POST /api/mark-ready` now 404s (new test in
`tests/test_booking_logic.py`), confirmed live against the running server.

### 19. No pagination on ever-growing lists
`/api/get-history`, `/api/get-feedbacks`, and `/api/get-cancelled` return
every matching row, unbounded, every time. Fine today; as the shop
accumulates months of history this becomes a slow, ever-larger response
and an ever-taller table with no way to page through it. **Deliberately
not done in the same pass as 16/18/20** - all three existing frontends
(`history.html`, `feedback.html`'s reviews table, `cancelled.html`)
already implement their own pagination UI (page numbers, "showing X-Y of
Z") over a fully-fetched array; making the API paginated means reworking
that UI to page against the server instead, not just adding
`LIMIT`/`OFFSET` to three queries. Bigger than the other three items here,
worth its own pass.

### ✅ 20. `plate_number` format validation - done (2026-09-14)
Added a deliberately loose check in `api_submit_booking`: rejects a plate
with fewer than 2 letters/digits (catches `"---"`, a single character,
whitespace-only, pure punctuation) but doesn't enforce any specific format
- real PH plates vary too much (old 3+3, new 3+4, motorcycle, conduction/
temporary, personalized) to validate strictly without risking rejecting a
real customer's plate. Verified: new tests confirm garbage is rejected
and realistic plates in different formats (`"ABC 1234"`, `"NGP-1023"`,
`"1234 AB"`) still succeed; live-tested against the running server.

### ✅ 21. Session cookie hardening - done (2026-09-13)
Fixed as part of the same security audit as item 15. `app.py` now sets
`SESSION_COOKIE_HTTPONLY = True` and `SESSION_COOKIE_SAMESITE = 'Lax'`
explicitly (previously relying silently on Flask's defaults), plus
`SESSION_COOKIE_SECURE`, read from a new `SESSION_COOKIE_SECURE` env var
(default off, since this still runs over local HTTP - flip it to `true`
in `.env` once this is behind real HTTPS). Verified live: the `Set-Cookie`
header on a real login response now shows `HttpOnly; SameSite=Lax`.

### ✅ 23. SMS notifications - done (2026-09-14)
Per user idea: added SMS alongside email, plus a per-booking customer
choice of channel(s) - Email, SMS, or Both - rather than silently forcing
one on everyone. Deliberately scoped to the two notification points that
already existed (no-show release, admin's "car is ready" notify) - a
`api_submit_booking`-time confirmation message doesn't currently exist
either, so adding SMS to it would be a separate new feature, not "add SMS
to what's already there."

- `_Backend/sms_service.py` (new): `send_sms()` via Semaphore
  (api.semaphore.co, the standard PH SMS gateway, plain REST - no SDK
  dependency added). Same graceful-failure shape as `get_weather_
  condition()` - a missing `SEMAPHORE_API_KEY` or a network hiccup prints
  a warning and returns `False`, never raises, never blocks the rest of a
  notification flow. `is_plausible_phone()` is deliberately loose, same
  philosophy as item 20's plate check.
- `database.py`: `reservations` gained `customer_phone` and `notify_via`
  (`'email'`/`'sms'`/`'both'`, app-validated like `cancel_reason` already
  is - not a DB CHECK). Captured per-booking, same as `customer_email`
  already is, not as a customer account setting - there's no account/
  profile page in this app, and a walk-in customer may not even have an
  account.
- `utils.py`: `normalize_notify_via()`/`wants_email()`/`wants_sms()` -
  shared so `queue_api.py`, `staff_api.py`, and `no_show.py` all agree on
  what a booking's preference means. Unset/garbage silently falls back to
  `'email'` - every booking's behavior before this feature existed.
- `blueprints/queue_api.py`, `api_submit_booking`: validates the
  preference and requires a plausible phone number when SMS is chosen.
- `blueprints/staff_api.py`, `/api/send-email` (the admin "car is ready"
  notify action): now looks up the reservation's real notify_via/
  customer_phone/customer_email itself rather than trusting the request -
  same "read it from the record" pattern item 6's feedback fix
  established. Sends only the channel(s) the customer actually chose; the
  admin's subject/message inputs only apply to the email leg (SMS is
  always a short fixed template, same as the no-show SMS). Returns
  `email_sent`/`sms_sent` so the frontend can report accurately.
- `no_show.py`: the auto-release now checks the same preference before
  emailing and/or texting.
- `booking.html` and `queue_dashboard.html`'s walk-in modal: new "How
  should we notify you?" radio group (Email/SMS/Both) with a phone field
  that only appears (and is required) when SMS is selected.
- `ready.html`'s notify modal: shows the customer's chosen channel(s) as a
  badge, hides the email fields entirely if they didn't choose email, and
  shows a read-only SMS preview (not admin-editable, matching the no-show
  SMS) if they chose SMS.

Verified: 168/168 tests pass, including a new `tests/test_sms_
notifications.py` (phone/notify_via validation, backward-compat default
of 'email' for old clients, and - using `unittest.mock` specifically at
the external Semaphore gateway boundary, the one place in this codebase's
test suite where mocking a real integration point made sense - that
`/api/send-email` sends exactly the channel(s) chosen and neither more
nor less). Live end-to-end test against the running server: booked with
`notify_via: "both"`, pushed it through start/complete/confirm to Ready,
called the notify endpoint, and got back `email_sent: true` (a real email
went out) and `sms_sent: false` with `"SMS failed to send"` - correct,
since no real `SEMAPHORE_API_KEY` is configured in this dev environment;
the branching logic itself is confirmed correct by the mocked tests.

## 🟢 Suggestions / integrations worth considering

Philippine-market-aware, given the shop's actual location (Malabon),
currency (₱), and bilingual Tagalog/English UI:

### 22. GCash/Maya online payment at booking time
`services.price` is stored and shown but never charged - booking is
reservation-only today. A PH car wash's customers overwhelmingly expect
GCash/Maya as a payment option; even a simple "pay on arrival vs. pay now
via GCash" toggle at booking would be a meaningful upgrade.

### 24. Google Business review funnel
Item 6 built real, verified feedback (tied to an actual completed
reservation). A natural next step: after a 4-5 star review, prompt the
customer with a one-click link to also post that review to MD57's real
Google Business listing, while lower ratings stay internal for the shop to
follow up on rather than becoming a public complaint.

### ✅ 25. QR-code digital ticket - done (2026-09-15)
The booking confirmation ticket (`booking.html`) now encodes its
`booking_id` ("MD57-N") as a scannable QR code, and the staff-facing
Queue Dashboard can scan one back to jump straight to that reservation -
closing the loop the suggestion described (scan instead of typing/
searching the plate manually).

- `booking.html`: loads `qrcodejs` (davidshimjs, via cdnjs) and renders a
  QR of the exact `bookingId` string already shown as text ("MD57-N")
  into a new `#ticketQrCode` div inside the existing ticket modal, right
  after the ticket's text fields are populated - no backend change
  needed, since the ID was already returned by `/api/submit-booking`.
  The QR's background is pinned to a literal white, not a theme token -
  called out with a comment, since it looks like the exact "hardcoded
  light background" bug class item 36 swept out, but here it's required:
  a QR code needs real contrast against its quiet zone or a scanner can't
  read it regardless of site theme.
- `queue_dashboard.html`: loads `jsQR` (via jsdelivr) and adds a "Scan QR"
  button next to Walk-in (staff/admin only, same Jinja role guard). The
  button starts hidden and is only revealed once `window.isSecureContext`
  and `navigator.mediaDevices.getUserMedia` are both confirmed present -
  camera access needs a secure context (HTTPS, or localhost) that a LAN
  dev deployment over plain HTTP won't have, so this avoids offering a
  control that would silently fail to open there.
- New QR-scan modal: opens the device camera, runs a
  `requestAnimationFrame` loop decoding frames via `jsQR`, and validates
  each decoded string against `/^MD57-(\d+)$/i` - a non-matching code
  (some unrelated QR the camera happens to catch) just updates the status
  line and keeps scanning rather than closing early.
- The existing plate/service/name search box gained one new form: a query
  starting with `md57-` is treated as an *exact* reservation-id lookup
  instead of the normal substring match, so it can't collide with a real
  plate/service query. `applyScannedBooking()` looks the id up in the
  already-cached `fullQueueCache` (no extra server round trip for an
  instant "is it here right now" answer), auto-switches the Walk-in/
  Online category dropdown to whichever one the matched booking actually
  belongs to (a scanned walk-in wouldn't otherwise show up while the
  Online view is selected), and always closes the scan modal - including
  on a not-found result, so a bad scan doesn't strand the user staring at
  a live camera feed.

Verified: 197/197 backend tests still pass (both changes are frontend-
only). Both edited templates' script blocks re-parsed clean with the
esprima syntax checker, both edited `extra_style` blocks brace-balanced.
Live-tested with Playwright against the running dev server: confirmed the
`qrcodejs`/`jsQR` CDN scripts actually load and the ticket modal really
renders a QR image (not just markup) for a real booking id; confirmed
`scanQrBtn` becomes visible under the feature-detection gate; and,
because seeding a *real* walk-in booking from a script hits the same
staffing/business-hours gates items 31/46 added (slow and flaky to set
up repeatably), exercised the real `processAndRenderQueue()` and
`applyScannedBooking()` functions directly against controlled fake queue
data: confirmed an exact `md57-501` search returns only reservation 501
even alongside a `5010` that would have matched as a substring, confirmed
a walk-in match correctly flips the category dropdown, confirmed a
not-found id returns zero rows without crashing and still closes the
modal, and confirmed ordinary substring search (e.g. a plate fragment)
is completely unaffected. Zero browser console errors across all of the
above.

### ✅ 26. Staff shift roster / reminders - done (2026-09-14)
Chosen over items 22/24 as the one suggestion that actually reinforces the
paper's title (extends "Staff Allocation" directly, unlike GCash payment
or a review funnel). Deliberately a separate table, not an extension of
`staff_attendance` - that table only ever records what already happened
(real clock-ins), and conflating "planned" with "actual" would have meant
either fake future attendance rows or overloading one table with two
different meanings.

- `database.py` / `md57_schema.sql`: new `staff_shifts` table
  (`staff_name`, `shift_date`, `shift_note`, `created_by`) with a unique
  index on `(staff_name, shift_date)` - one roster entry per staff/day. No
  formal shift start/end time modeled, matching how the real attendance
  system doesn't track that either (staff just clock in whenever) -
  `shift_note` is a free-text slot ("Morning", "Covering for Alan", etc.).
- `blueprints/staff_api.py`: `GET /api/staff-shifts` (admin+staff, 7-day
  window by default, `start_date`/`end_date` override it) -
  `POST /api/staff-shifts` (admin-only, same "must be a real staff
  account" validation `api_staff_attendance` already uses; upserts via
  `ON CONFLICT` so re-scheduling the same staff/day updates the note
  instead of erroring or duplicating) - `POST /api/delete-staff-shift`
  (admin-only).
- `staff.html`: new "📅 Upcoming Shift Roster" card - admin gets an
  inline add form and a Remove button per entry, staff see the same
  roster read-only (useful for seeing who else is scheduled, not just
  themselves). A banner ("You're scheduled to work TODAY/TOMORROW...")
  shows for a staff session when they have an entry in either bucket.
- `queue_dashboard.html`: the same reminder banner reuses the existing
  `renderDashboardAlerts()` pattern from items 29/30 - staff's actual
  day-to-day working page, not just the roster page they may not open
  daily.

**Scope note, deliberately not built**: a real push notification (email/
SMS) shift reminder was considered, but staff accounts have no email/
phone field at all today (only customers do, from item 23's work) -
adding that is a bigger change (new contact fields + an admin UI to set
them) than "a lightweight roster view" implied. The in-app banner on two
pages staff already visit covers the "reminder" half of this item's
description without that added scope.

Verified: 179/179 tests pass, including a new
`tests/test_staff_shift_roster.py` (admin-only scheduling/removal,
unknown-staff-name rejected, upsert-not-duplicate on re-scheduling,
default 7-day window vs. an explicit wider range, staff can view but not
modify). Live test against the running server: scheduled TJ Barcebal for
the next day as admin, confirmed his own session could see it in
`/api/staff-shifts` but got a 403 trying to schedule one himself, removed
it and confirmed the roster came back empty.

---

## ✅ Item 35 done (2026-09-14): fresh full-system audit (logic, presentability, function)

Requested pass over the whole system rather than a specific roadmap item -
a background research agent plus direct investigation, to find concrete
issues in the huge amount of code accumulated since item 17.

**Real bug found and fixed**: `queue_dashboard.html`'s walk-in modal
offered vehicle types `Sedan`/`SUV`/`Van`/`Pick up`/`Motorcycle`, but the
real `vehicle_types` table only has `Car`/`Motor`/`Pick up`/`Van` - three
of five options always failed item 7's vehicle-type validation with a 400,
and the two most common types (Car, Motor) weren't offered at all. Walk-in
booking through this modal was broken as shipped. Fixed to match the real
table (same options `booking.html` already correctly used).

**Presentability - pre-auth pages migrated to the site theme.**
`login.html`, `register.html`, `forgot_password.html`, `reset_password.html`
are standalone documents (not extending `base.html`) that had never been
carried over from the old royal-blue palette to the navy/gold theme every
other page uses since the executive redesign. Replaced every `#2563eb` /
`#1d4ed8` / `rgba(37, 99, 235, ...)` literal (both the CSS variable and
several raw literals that bypassed it) with the real gold accent
(`#9c7a2e` / `#b6913f` / `rgba(156, 122, 46, ...)`) across all four files.
Google's own multi-color logo SVG on the sign-in buttons was deliberately
left untouched - that's Google's brand, not this app's theme.

**Dead code removed** (each confirmed to have zero references anywhere -
templates, tests, or other backend code - before deletion):
- Duplicate route aliases: `/api/create-user` (real one is
  `/api/admin/create-user`), `/api/export-pdf` (real one is `/export-pdf`).
  `tests/test_rbac.py` was calling the dead alias in three places - updated
  to the real route rather than left pointing at a route that no longer
  exists.
- `/customer_portal` (pages.py) - a redirect stub nothing ever linked to.
- `/api/get-vehicle-types` (queue_api.py) - built, never wired to any
  dropdown. (Note: wiring it up instead of deleting it - driving the
  vehicle-type dropdowns from real data - would have prevented the bug
  above from ever existing; worth considering as a follow-up if you'd
  rather have that than hardcoded option lists.)
- Four status strings (`'queue'`, `'cleaning'`, `'pending'`, `'scheduled'`)
  checked in ~10 `WHERE ... IN (...)` clauses across `queue_api.py` and
  `admin_api.py`, but never produced by any insert/update anywhere -
  confirmed zero live rows use them before removing the tolerance.
  `database.py`'s partial unique index left untouched on purpose - SQLite's
  `CREATE INDEX IF NOT EXISTS` wouldn't apply a changed `WHERE` clause to
  an already-initialized database anyway, and the practical difference is
  nil either way.
- `manager_analytics.html`'s local `openModal()` - shadowed the real
  `common.js` one (different visibility convention, `style.display` vs.
  `classList`) and was never called. **Found but deliberately not
  touched**: the two modals it used to open (`contactModal`,
  `locationModal`) have no trigger button anywhere in the file, so they're
  still unreachable - looks like a missing button rather than confirmed
  debris, left for a product decision rather than silently deleted.

**Also fixed**: the color used for "N cars awaiting your confirmation"
(queue_dashboard.html) was green, the same "assignment" color as the
unrelated "you've been recommended for this job" banner - every other
"awaiting an admin decision" state in the app (pending clock-in requests,
pending job confirmations) uses purple. Changed to match. `staff.html`'s
new roster row was missing `flex-wrap` (would overflow under ~400px),
inconsistent with the identical-purpose clock-in-request row right next to
it - fixed. `ready.html` never called its own `triggerCrossScreenSync()`
after notifying/archiving, unlike every other mutating page - fixed so
other open tabs refresh instantly instead of waiting for their own poll.

**Confirmed solid, no changes needed**: traced the full `queue_status`
transition graph - no dead-end states exist (every non-terminal status has
a real route forward; `completed`/terminal and the `cancelled` \<-\>
`waiting` pair are correctly the only two "stop here" points).
`create_user.html`'s ROLE_SPECS panel was checked against every current
`@role_required` decorator and found fully accurate - no stale claims.

Verified: 179/179 tests pass throughout. Live-tested against the running
server: confirmed the walk-in fix accepts a real booking, confirmed all
four pre-auth pages render the gold accent with zero blue literals left,
confirmed both dead-route removals 404 while their real counterparts still
work normally (including a real account creation through
`/api/admin/create-user` to prove the alias removal didn't collateral-
damage the working route).

---

## ✅ Item 36 done (2026-09-14): dark mode restored after a silent regression

Between item 35 and this session, `base.html` and `style.css` were rebuilt
into the navy/gold "executive redesign" (outside this tool's own edits -
found already in place at the start of this session). That rebuild dropped
the entire dark/light theme system that previously existed: no
`data-theme` toggle, no dark CSS variable overrides, no Bootstrap-Icons-era
icon set (replaced with Font Awesome, which is fine and was kept), and
emoji/Tagalog text had crept back into `admin-dashboard.html` and
`create_user.html`. None of this was caught by the test suite, since it's
100% backend/Python and has zero visibility into frontend theming or
rendered UI text (see item 39 below).

- `style.css`: re-added `html[data-theme="dark"] { ... }` redefining every
  design token (`--bg-light`, `--card-bg`, `--border-color`, `--text-*`,
  `--sidebar-*`, `--accent-green/red`), plus a dimmed-logo-watermark
  background rule for dark mode specifically (kept the MD57 logo visible
  behind the UI rather than a flat color, per standing product decision).
  Added `.theme-toggle-btn` styling and a `@media (max-width: 480px)` block
  so the header (toggle + role badge + logout button, all `flex-shrink:0`)
  wraps instead of overflowing on phones.
- `base.html`: added the toggle button + a pre-paint FOUC-prevention
  inline `<script>` reading `localStorage['md57_theme']`.
- `common.js`: added `toggleTheme()` / `syncThemeToggleIcon()`.
- The 4 standalone pre-auth pages (`login.html`, `register.html`,
  `forgot_password.html`, `reset_password.html`) don't extend `base.html`,
  so each got its own mirrored dark `:root` block, toggle button, and (for
  `login`/`register`) a mobile nav-wrap fix, added independently.
- Fixed the same recurring `bayIdSlug` pagination bug from before
  (`bayKey.toLowerCase().replace(' ', '-')` turns `"Bay #1"` into
  `"bay-#1"`, not `"bay-1"`, silently breaking `getElementById` lookups) -
  it had reappeared in `cancelled.html`. Also converted `ready.html`'s
  pagination from the old Prev/1/2/3/Next numbered-button style to the
  "N / M" static format the other pages use, and removed a stray
  `paginationContainer.innerHTML = ''` that was wiping the static markup
  on every render (the exact bug class flagged in an earlier session).
- Swept and replaced every emoji with Font Awesome icons and translated
  all remaining Tagalog text across all templates (`admin-dashboard.html`,
  `create_user.html`, and 6 more pages - `booking`, `queue_dashboard`,
  `history`, `staff`, `manager_analytics`, `feedback` - had regressed or
  were never fully covered). Swept 77 hardcoded light background-color
  literals (`#f8fafc`/`#ffffff`) across 9 templates that bypassed the
  token system and would've stayed light-colored under dark mode.
- **Real bug caught while verifying this**: `.dropdown-item`,
  `.btn-secondary` (`style.css`), and `homepage.html`'s `.card h3` all used
  a fixed `var(--navy-800)` as text color sitting on top of a background
  that *does* go dark (`var(--card-bg)`/`var(--bg-light)`) - readable in
  light mode, nearly invisible in dark mode (navy-on-navy). Caught live via
  a user screenshot showing unreadable homepage card titles. Fixed by
  switching all three to the theme-aware `var(--text-bright)` token. A
  full re-sweep for sibling instances of this exact pattern found none
  remaining.

Verified: 179/179 backend tests still pass (none of this touched backend
logic). Every template's `<script>` blocks re-checked with the esprima
JS-syntax checker after editing (the same tool built in an earlier session
after a real emoji-replacement regex broke a script block by colliding
quote characters). Live-tested logged in as admin: confirmed zero emoji
and a working theme toggle across all 10 authenticated pages plus all 4
pre-auth pages, confirmed the dark-mode contrast fix renders correctly.

---

## Open items found in a fresh audit (2026-09-14), not yet fixed

Found via a full current-state re-analysis after item 36 - none of these
are covered elsewhere above.

### 🔴 High
**37. `_Backend/create_user.py` and `repomix-output.xml` still exist**,
contradicting item 33's claim both were deleted. `create_user.py` is a
working CLI script, entirely in Tagalog, with no password-length check -
it bypasses every validation `auth.py`/`admin_api.py` now enforce
elsewhere, the exact gap item 33 said was closed by removing this file.
`repomix-output.xml` is a stale 587KB pre-refactor code dump (no secrets
found in it on inspection, just staleness/clutter risk). Either the
roadmap entry never actually ran in this checkout, or something restored
them afterward - unclear which. Needs actually deleting.

**38. Connection leak in `api_submit_booking`'s catch-all exception
handler** (`blueprints/queue_api.py`, ~line 823). Every specific failure
path after `BEGIN IMMEDIATE` (line ~654) explicitly does `conn.execute
("ROLLBACK"); conn.close()` - but the outer `except Exception` wrapping
the whole function (weather lookup, service lookup, queue-number/snapshot
computation) does neither, just prints a traceback and returns 500. Any
exception in that window leaves a write-lock-holding connection to
whatever CPython's GC timing does, instead of the deterministic close used
everywhere else in this same function - the same "leaked connection ->
`database is locked`" bug class item 33 already fixed once, as a second
un-roadmapped instance of the pattern.

### 🟡 Medium
**39. Zero test coverage for any frontend/UI behavior** - `tests/` is
100% backend Python (`unittest` + Flask test client); there's no
JS/browser test tooling in this repo at all (no Selenium/Playwright/Jest).
Dark/light theme toggling, the contrast-token bugs from item 36, pagination
math, and icon rendering are all invisible to `python -m unittest
discover` - which is exactly how dark mode silently vanished once already
without anything catching it. Worth at minimum a lightweight headless-
browser smoke test if this project continues past the capstone deadline.

**40. Stray emoji in a real customer-facing email** -
`blueprints/staff_api.py:932`, the "car is ready" notification subject:
`'MD57 Car Wash - Your Vehicle is Ready! ✨'`. Missed by item 36's cleanup
since that was scoped to frontend templates, not backend-generated
email/SMS text. (Checked: SMS bodies and other email bodies are clean -
this is the only remaining instance found anywhere in live code paths.)

---

## ✅ Item 41 done (2026-09-15): small UI fixes + OTP-based password reset

Three small requests plus one larger one, all from direct user feedback
on the running app.

- `homepage.html`: `.cards-grid` converted from CSS Grid to Flexbox
  (`flex-wrap` + `justify-content: center`) so an incomplete last row
  (e.g. Contacts/Location alone on row 2) centers itself instead of
  sitting left-aligned under the first row - Grid can't center a partial
  row without extra tricks, and the card count already varies by role
  (admin/staff see extra cards a customer doesn't).
- Dark/light toggle changed from an icon button to a sliding switch
  (checkbox + `.slider` knob, sun/moon icons flanking it) - `base.html` +
  `style.css` + all 4 standalone pre-auth pages updated identically, plus
  `common.js`'s `toggleTheme()`/`syncThemeToggleIcon()` reworked to read/
  set the checkbox's `checked` state instead of an icon class.
- `forgot_password.html`/`reset_password.html` gained the same
  `<nav class="top-navbar">` (brand + toggle) that `login.html`/
  `register.html` already had - previously the toggle floated as a
  separate `position: fixed` corner button on just these two pages, a
  real inconsistency the user caught ("not fixed position in top nav").
- `ready.html`'s "Notify Customer" modal improved per user request: it
  used to call `closeNotifyModal()` and show a toast *before* the
  `/api/send-email` request even resolved - if it then failed, the modal
  was already gone and the customer info had to be re-typed to retry. Now
  the modal stays open with a disabled, spinning "Sending..." button
  until the whole send+archive chain settles, closing only on success; a
  failure shows inline in the still-open, still-filled-in modal. Also
  added icons to every field label, a live character counter on the
  message box, and `.btn-primary:disabled`/`.btn-secondary:disabled`
  styling to `style.css` (didn't exist before).
- **Self-service password reset changed from an emailed link to an
  emailed 6-digit OTP**, per user request. `database.py`: `password_resets`
  gained an `attempts` column - a 6-digit code has only 1,000,000 possible
  values (unlike the old `secrets.token_urlsafe(32)` link, which was
  practically unguessable), so capping wrong guesses per issued code is
  what actually keeps this safe. `auth.py`: `RESET_TOKEN_LIFETIME_MINUTES`
  shortened 30 -> 10 (a code is meant to be typed in immediately, not
  saved for later - also narrows the brute-force window), new
  `MAX_OTP_ATTEMPTS = 5` (the row is force-invalidated once hit, even if
  the correct code is later supplied). `forgot_password_page` now always
  renders `reset_password.html`'s new "Enter Reset Code" step after POST,
  regardless of whether the account existed/was Google-linked/an email
  actually sent - preserving the original anti-enumeration property
  (identical response either way) without needing an intermediate generic-
  message page. `reset_password_page` no longer takes a `token` from the
  URL at all (`GET` now just redirects to `/forgot-password` - there's no
  link to click anymore); `POST` takes `username` + `otp` + the new
  password together in one step, looks up the latest unused/unexpired
  code for that username, and increments `attempts` on a mismatch rather
  than revealing which part was wrong. `reset_password.html` rewritten
  with a centered, letter-spaced 6-digit `.otp-input` field alongside the
  password fields, replacing the old "Link Expired" dead-end state.

Verified: 182/182 tests pass, including a rewritten `tests/
test_password_reset.py` (OTP created only for a real non-Google account,
identical response for unknown/Google/real usernames, cooldown still
throttles repeat requests, wrong/expired/reused codes all rejected with
the same generic message, wrong attempts increment and lock out at the
cap even with the eventually-correct code, mismatched confirmation and
short passwords rejected without touching the stored hash). Live
end-to-end test against the running server: requested a code for the real
`customer_user` account, pulled the real 6-digit code from the database,
confirmed a wrong code was rejected and the real one succeeded (302 to
`/login?reset=1`), then restored the account's original password
afterward so the dev environment stays consistent.

---

## ✅ Item 42 done (2026-09-15): toggle sizing fix + branded HTML emails

**Toggle sizing bug**: `login.html`/`register.html` had a
`@media (max-width: 600px)` rule shrinking `.theme-switch` to 40x22px on
phones; `forgot_password.html`/`reset_password.html` (added in item 41)
never got that same rule, so on a phone their toggle stayed at the full
46x24px desktop size while every other pre-auth page's toggle was
visibly smaller - the exact inconsistency the user caught. Added the
missing mobile override to both files, matching login/register exactly.

**Every real customer-facing email is now branded HTML, not plain text**,
per user request ("send a UI with our logo like forms" for notify and
password-reset emails). New `_Backend/utils.py` function
`send_branded_email(subject, recipient, heading, plain_text_body,
highlight=None)`: wraps the message in an HTML layout (navy header bar,
the MD57 logo, a white content card, footer) with the logo attached as a
real MIME attachment referenced via `cid:` (Content-ID) rather than a
link to this app's own `/static/` URL - the CID approach means the logo
renders correctly in the recipient's actual inbox (Gmail, etc.) even
though this app runs on localhost and isn't publicly reachable, which a
plain `<img src="http://127.0.0.1:5000/...">` never could. The original
plain-text body is kept as the `body` fallback for clients that don't
render HTML. An optional `highlight` param renders a large, letter-spaced
box for short standout content - used for the OTP code specifically.
All user-supplied content is escaped via `markupsafe.escape()` before
insertion into the HTML.

Applied to all three real customer email call sites, replacing their
individual raw `Message(...)` + `mail.send(...)` calls: `auth.py`'s
password-reset code email (now shows the OTP in the highlight box),
`staff_api.py`'s "vehicle is ready" notify email (also fixed item 40's
stray emoji in its default subject line in the process), and
`no_show.py`'s auto-release email - unifying all three to look like one
system instead of three different bare-text formats.

Verified: 182/182 tests pass (email body/HTML construction has no
backend logic dependency the test suite exercises differently). Live
smoke test using Flask-Mail's `record_messages()` test recorder:
confirmed the sent message carries both `body` and `html`, exactly one
attachment with `Content-Type: image/jpeg` and `Content-ID: <md57_logo>`
matching the `cid:md57_logo` reference inside the HTML, the real logo
file's bytes were read successfully, and a test OTP value appeared
correctly inside the highlight box. Live end-to-end test against the
running server confirmed the mobile toggle-sizing rule is present in the
served HTML for `reset_password.html`.

---

## ✅ Items 37, 38, and a new item 43 done (2026-09-15)

A follow-up fresh audit re-checked items 37/38 (still open) and reviewed
today's new OTP/branded-email code for fresh issues, surfacing one real
new bug.

**37 - stray files actually deleted this time.** `_Backend/create_user.py`
(Tagalog CLI script, no password-length check) and `repomix-output.xml`
(587KB stale pre-refactor dump, at repo root) both confirmed to still
exist despite item 33 claiming deletion, confirmed zero references
anywhere in `_Backend`, and deleted.

**38 - connection leak in `api_submit_booking`'s catch-all handler,
fixed.** `blueprints/queue_api.py`: added `conn = None` before the
function's `try:`, and the outer `except Exception` now closes it the
same way every specific failure path inside the `BEGIN IMMEDIATE`
transaction already did (`conn.execute("ROLLBACK")` wrapped in its own
try/except in case no transaction was actually open yet, then
`conn.close()`) - guarded by `if conn is not None` since the exception
could fire before the connection is even created (several early-return
validation checks happen first).

**43 (new) - OTP codes could eventually crash the reset flow.**
`password_resets.token` is still `UNIQUE` - fine for the old design's
long random link (unique by construction), but item 41's 6-digit OTP
only has 1,000,000 possible values, and `password_resets` rows are never
deleted (only marked `used`). Over this app's lifetime, two different
accounts will eventually draw the same code and collide on that
constraint; an uncaught `IntegrityError` there would 500 the whole
`/forgot-password` request - and crash-vs-normal-page is exactly the
kind of distinguishable response the anti-enumeration design in this
same flow was built to avoid. `auth.py`'s `forgot_password_page` now
retries with a fresh code (up to 5 attempts) on an `IntegrityError`
rather than letting it propagate; if all 5 collide (never observed,
astronomically unlikely at any real usage volume) it logs and falls
through to the same generic verify-step render as the Google-linked/
unknown-account cases, so the anti-enumeration property holds either way.

Verified: 182/182 tests pass throughout (none of the three changes alter
any behavior the suite exercises differently - the OTP retry path only
diverges from the happy path on a randomness collision the suite can't
practically trigger). Live-tested against the running server: confirmed
both stray files are gone from disk, confirmed `app.py` still imports
and boots cleanly after both edits.

---

## ✅ Item 44 done (2026-09-15): toggle reverted to a button + professionalism pass

**Theme toggle reverted from a slide switch back to a compact icon-only
button.** Per user feedback, twice: first "too wide, not like in the
other page that is only button," confirmed via a headless-Edge screenshot
+ injected `getComputedStyle` diagnostic (the DOM/CSS were actually
correct - single element, zero border-radius, full width - the wide
pill-with-two-icons was just what a slide switch inherently looks like
compared to a plain button), then explicitly confirmed: revert to the
~34px single-icon circular button design item 36 replaced. Reverted
identically across `base.html`/`style.css` (authenticated pages) and all
4 standalone pre-auth pages (`login`, `register`, `forgot_password`,
`reset_password`) - same markup/CSS/JS `toggleTheme()`/
`syncThemeToggleIcon()` this app used before item 36, this time kept
consistent everywhere from the start.

**SMS-on-notify investigated - confirmed a missing credential, not a
bug.** `sms_service.py`'s `send_sms()` requires `SEMAPHORE_API_KEY`;
`.env` has real Gmail SMTP/Google OAuth/Weather keys but no Semaphore key
at all, so SMS legitimately no-ops (by design - a missing key must never
break the rest of a notification, per the function's own docstring).
Confirmed directly (`send_sms()` called standalone, printed the exact
"not set" warning). Not fixable in code - needs a real Semaphore account
+ API key in `.env`; `.env.example` already documents the variable.

**Professionalism audit + fixes** (a dedicated pass, not a bug hunt -
prior rounds covered security/correctness/contrast):
- `.page-title`/`.page-subtitle` had drifted to 5 different font-sizes
  across 8 pages (each redeclared its own copy instead of sharing one).
  Consolidated into one definition in `style.css`; pages needing extra
  non-typography layout (`queue_dashboard.html`'s live-badge row) kept
  only that part locally.
- `manager_analytics.html`'s page title was the only one missing an icon
  prefix that every sibling page has - added `fa-chart-line`.
- `booking.html`'s subtitle said "your reservation" - the only spot in
  the app using that word instead of "booking" (the term used
  everywhere else, including this page's own `<h2>`) - fixed.
- `staff.html`'s attendance table had no empty-state row (every other
  list on that same page - audit logs, clock-in requests, shift roster -
  does); added one.
- `admin-dashboard.html`'s live metrics poll failed completely silently
  on error (stat tiles just stuck at "--" forever, no on-screen signal) -
  added a one-time `showToast` warning (suppressed on repeat failures
  since it polls every 10s).
- Replaced 15 routine-outcome `alert()` calls with the shared styled
  `showToast()` across `staff.html` (9), `cancelled.html` (2),
  `ready.html` (2), `history.html` (1), `manager_analytics.html` (1) -
  covering permission-denied notices and success/error confirmations that
  were blocking browser dialogs while sibling pages already used the
  proper toast for the same kind of event. Deliberately left two `alert()`
  calls alone (`staff.html`, `history.html`) - both are pre-content
  role-guards that redirect away from the page entirely, where a blocking
  dialog before navigation is reasonable.

Verified: 182/182 tests pass (all changes were frontend CSS/JS/markup,
plus a config investigation - no backend logic touched). Every edited
template's `<script>` re-checked with the esprima syntax checker, all
`<style>` blocks re-checked for brace balance. Live-tested against the
running server logged in as admin: confirmed the reverted toggle renders
correctly (real headless-Edge screenshot, not just an HTTP status check)
and confirmed all 8 pages using the shared `.page-title` still render
their headings correctly post-consolidation.

---

## ✅ Item 45 done (2026-09-15): compact slide toggle + first browser-driven tests

**Theme toggle changed one more time - now a compact slide toggle**, per
user request, reconciling both prior rounds of feedback: item 36 shipped
a slide switch that was "too wide" (46-50px, icons flanking both ends of
the track); item 44 reverted to a plain 34px circular button per that
feedback; this round brings back the sliding motion but sized to the same
footprint as the button (38x20px track, single icon inside the knob
itself rather than two icons flanking the track). Applied identically
across `base.html`/`style.css` and all 4 standalone pre-auth pages -
`toggleTheme()`/`syncThemeToggleIcon()` in `common.js` needed no changes,
since they still target the same `#themeToggleBtn`/`#themeToggleIcon`
ids, just now wrapped in a `.toggle-knob` span that CSS slides via
`transform: translateX()` keyed off `html[data-theme="dark"]`.

**First browser-driven test coverage for this project**, closing the gap
flagged repeatedly (items 39, and implicitly every time a UI regression
slipped past the 100%-backend test suite - item 36's dark mode vanishing
being the clearest example). Installed Playwright + a headless Chromium
(`requirements-dev.txt`, new - deliberately kept separate from
`requirements.txt` since it's test-only and pulls down a ~115MB browser
binary nothing else needs). New `tests/test_frontend_smoke.py`: boots a
real instance of the Flask app in a background thread on its own port
(5099, distinct from the port a developer runs the app on manually) and
drives a real browser against it - something the rest of the suite's
`Flask test_client()`-based approach structurally cannot do, since that
never parses or executes the page's actual CSS/JS.

Six tests: login page loads clean (no console errors), login actually
reaches an authenticated page, ten key authenticated pages all load
without console errors, the theme toggle both flips `data-theme` *and*
visibly changes the rendered background color (not just an attribute
nobody reads), the dark-mode choice persists across a fresh page
navigation (proving the FOUC-prevention script actually re-applies it),
and the Cancelled page's pagination renders as real "N / M" text instead
of being blank or malformed.

**Verified these tests actually have teeth, not just that they pass**:
deliberately re-created item 36's exact regression by stripping the
`html[data-theme="dark"]` CSS blocks from `style.css` and re-ran the
toggle test - it failed correctly (`'rgb(241, 243, 246)' ==
'rgb(241, 243, 246)'`, i.e. the background provably didn't change),
confirming this test would have caught that entire incident before it
ever reached the point of needing a dedicated fix-it session. Restored
the file immediately after and re-verified all 6 tests pass again on the
correct code, plus the full 182-test backend suite still passes alongside
the new file (188 total).

---

## ✅ Item 46 done (2026-09-15): compact slide toggle (again) + 8am-9pm business hours

**Toggle changed one more time - compact slide switch.** Reconciling both
rounds of feedback: brings back the sliding motion (item 36's original
ask) but sized to the same footprint as the plain-button revert (item
44) - a 38x20px track with a single icon inside the knob, instead of a
wider track with icons flanking both ends. Applied identically across
`base.html`/`style.css` and all 4 pre-auth pages; `toggleTheme()`/
`syncThemeToggleIcon()` in `common.js` needed no changes at all, since
they still target the same element ids either way.

**First browser-driven test coverage for this project.** Installed
Playwright + headless Chromium (`requirements-dev.txt`, new - kept
separate from `requirements.txt` since it's test-only and pulls a
~115MB browser binary). New `tests/test_frontend_smoke.py` boots a real
Flask instance on its own port (5099) and drives an actual browser
against it - something the rest of the suite's `test_client()` approach
structurally cannot do, since that never parses or executes the page's
real CSS/JS. Six tests: clean page loads (no console errors) for login
and ten key authenticated pages, login actually reaching an authenticated
page, the theme toggle both flipping `data-theme` *and* visibly changing
the rendered background color, the dark-mode choice surviving a fresh
page navigation, and the Cancelled page's pagination rendering as real
"N / M" text. **Verified these tests have teeth, not just that they
pass**: deliberately re-created item 36's exact regression (stripped the
`html[data-theme="dark"]` blocks from `style.css`) and confirmed the
toggle test failed correctly, then restored the file and re-verified.

**MD57 now enforces its real 8:00 AM - 9:00 PM operating hours**, per
user request. New `utils.py` helpers: `BUSINESS_OPEN_HOUR`/
`BUSINESS_CLOSE_HOUR` constants and `is_within_business_hours(dt)`
(inclusive of both ends - 9:00 PM itself is still a valid time, matching
the booking UI's last offered slot). `queue_api.py`'s `api_submit_booking`
now checks this for both booking paths: a *scheduled* booking's requested
time must fall in the window (checked alongside the existing past-date
validation), and a *walk-in* (no scheduled time - the check runs against
the current moment instead) can only be added while the shop is actually
open. `booking.html`'s and `homepage.html`'s time-slot pickers (previously
offering slots up to 11:00 PM, two hours past the real closing time) were
trimmed to match, and a stale "Slots until 11:00 PM" label was corrected.

**Real pre-existing test caught and fixed along the way**:
`test_same_day_scheduled_booking_declined_when_no_washer_on_duty` used
`11:59 PM` specifically because that was the safest way to guarantee "a
same-day time that's still in the future no matter what time tests
happen to run" - it now gets rejected by the new business-hours check
before ever reaching the staffing-gap check it was actually testing.
Changed to `09:00 PM` (the last valid in-hours time), preserving the
original test's intent.

Verified: 197/197 tests pass (188 + 9 new - 3 API-level business-hours
tests in `test_booking_logic.py`, and 6 direct edge-case tests for
`is_within_business_hours()` itself, since a walk-in's check runs against
`datetime.now()` and can't be pinned to a specific hour through a live
HTTP request the way a scheduled booking's explicit time can). Live
end-to-end test against the running server: an 11:30 PM booking request
was correctly rejected with the business-hours message, a 9:00 PM
boundary request correctly succeeded; the real Playwright smoke suite
and esprima/CSS-balance checks re-run clean on every touched file.

---

## ✅ Item 47 done (2026-09-15): scheduled-booking start-date gate, dark-mode contrast fixes, customer role badge

Three items from live user testing of item 46's changes: a real bug found
by hand (a booking scheduled for tomorrow was started today), plus two
direct requests.

**47a. A booking can only be started on the day it's actually scheduled
for.** `blueprints/queue_api.py`, `api_start_service` had no date check at
all - it matched purely on plate number and `queue_status = 'waiting'`,
so admin/staff could start a booking scheduled for any day, including
ones that hadn't arrived yet. Walk-ins (no `booking_date`) are exempt -
same exemption every other date-based rule in this codebase already
gives them (business hours, past-date validation, the no-show sweep).
The check is a strict equality against today's date, not a `<=` - a
stale `waiting` row from a day that's already passed (the no-show sweeper
should have already caught that, but isn't a guarantee) isn't startable
either, by the same "must be today" rule.
- `queue_dashboard.html`'s detail modal mirrors the same check client-side
  (`getTodayDateStr()`, a real local-date helper - not `toISOString()`,
  which is UTC and can land on the wrong calendar day near midnight): a
  future-dated booking's "Start Service Now" button is hidden entirely,
  replaced with "Scheduled for `<date>` - not startable yet", so staff see
  why up front instead of clicking Start and getting a rejection toast.
  The backend check is what actually enforces this - the UI change is
  just to not offer a button that's guaranteed to fail.

Verified: 4 new tests in `tests/test_washer_assignment.py` (a tomorrow-
dated booking rejected 400 with the real date in the message and no state
change, a yesterday-dated one also rejected, a today-dated one succeeds,
a walk-in with no date at all still succeeds) - 201/201 total pass. Live
tested against the running dev server.

**47b. Dark-mode text contrast fixed where it was genuinely broken, not
just mismatched.** A user-reported "fonts aren't matching dark mode"
report turned into a full dark-mode screenshot sweep (Playwright, every
authenticated page, all three roles) rather than guessing from a grep.
Found two real instances of the same bug shape - a hardcoded light
background literal paired with a theme-following text color token, which
is fine in light mode (dark token color on light literal background) but
becomes near-invisible in dark mode once the token's value flips to a
light color while the background stays hardcoded light too:
- `create_user.html`: the `input, select` rule had
  `background: #f8fafc` (hardcoded) with `color: var(--text-bright)`
  (theme-following) - in dark mode this meant near-white text on a
  near-white background, i.e. **anything typed into Create User's
  username/full name/password fields, or the password-reset form, was
  invisible while typing.** Changed to `background: var(--bg-light)`.
  The `#roleSpecBox` (the "Can:/Cannot:" role-capability panel) had the
  exact same bug with the same hardcoded `#f8fafc` - fixed the same way.
  Confirmed via computed-style check post-fix (not just a screenshot):
  input background/text now resolve to real theme colors with real
  contrast, and a live-typed value is visibly readable.
- `staff.html`: the Shift Roster's Date and Note fields had no styling at
  all (no matching CSS rule anywhere - `#rosterStaffSelect` right next to
  them was already correctly themed, which is what made the other two
  stand out as plain white browser-default boxes in dark mode). Not
  literally unreadable (default browser black-on-white is still legible)
  but a real "doesn't match dark mode" inconsistency - given the shared
  `var(--card-bg)`/`var(--text-bright)` background/color pair already
  used by their sibling field.

Verified: 182 backend tests unaffected (frontend-only change). Playwright
screenshot sweep re-run post-fix across all pages/roles in dark mode to
confirm both fixes render correctly and nothing else regressed; grepped
the full template set for the same hardcoded-light-background pattern
and confirmed no further instances remain (the handful of other
hardcoded white/light literals found - the QR ticket's white quiet zone,
the Google Sign-In button, a hover-state blue - are all deliberately
theme-independent by design, not bugs).

**47c. Customer accounts now get a role badge too.** `base.html`'s header
only ever rendered the `.user-role-badge` for `admin`/`staff`
(`{% if user_role in ['admin', 'staff'] %}`) - a logged-in customer saw
no role indicator at all next to their Sign Out button, unlike every
other role. Changed to always render the badge, with a third
`badge-customer` variant (`style.css`, a muted blue chip matching the
existing gold-admin/green-staff pattern) reading "CUSTOMER: `<name>`".

Verified: 201/201 tests pass. Live-tested logged in as `customer_user` in
both light and dark mode - badge renders correctly in the header, sized
and positioned identically to the admin/staff badges, no layout shift on
the existing mobile header-wrap rule.

---

## ✅ Item 48 done (2026-09-16): replaced two manager_analytics.html widgets

Both from direct user request, after being talked through the tradeoffs
first rather than just implementing on request.

**AI Workforce Demand vs Actual -> AI-Recommended Staff Count vs
Scheduled.** The old chart compared two values on different scales (a
per-weekday-occurrence average vs. an all-time cumulative total), which
always visually read as "the AI is way off" even though nothing was
actually broken - just not a real comparison. Replaced with a genuine
capacity-planning calculation in `get_analytics_metrics`
(`blueprints/staff_api.py`): expected bookings for a typical occurrence
of each weekday, divided by real empirical throughput (completed jobs
÷ distinct staff-days actually worked, from `staff_attendance` -
falling back to a documented constant only when there's no attendance
history yet), ceiling-divided into a recommended headcount capped at
the real number of staff accounts. Cross-referenced against
`staff_shifts` (item 26's roster) for the nearest upcoming date per
weekday, so the chart is now Recommended vs. Actually Scheduled - a
real gap-finder instead of a chart that just looked pessimistic.
Weekdays with fewer than 2 historical samples render as a visibly
lighter bar rather than a confident-looking number built on almost
nothing.

**Performance Metrics Control -> Staff Leaderboard.** The old panel was
a leftover manual simulator (item 27's history) that had since been
wired to auto-populate from real data, but was still presented as five
*editable* inputs (hardcoded to five specific staff names) behind a
"Calculate Rankings" button - 100% redundant with the Staff
Productivity Ranking table right below it, which already computes and
displays the same three metrics live. Worse: because the five rows
were hardcoded by name, the panel would have silently broken (shown
nothing) for a sixth hire or stayed showing someone who'd left.
Replaced with a read-only card leaderboard fed by the exact same ranked
result as the table (`updateTableUI()` now renders both from one
calculation, instead of the table and a second independent client-side
SAW fallback existing side by side). `calculateMCDA()` no longer reads
from five hardcoded DOM inputs - it takes whatever real staff array
`/api/staff-real-metrics` returned, so the ranking (and both of its
displays) scale to however many staff accounts actually exist.

**Real bug caught along the way, unrelated to either widget**: running
the full test suite after seeding item 47's earlier analytics-testing
data (a since-removed 150-row batch, see below) surfaced that four
tests assume the shared dev database is near-empty - they check for an
*exact* row count or an *exact* SAW score without scoping their query
to their own test data, so a realistic amount of unrelated real-looking
data in the same table breaks them. Not fixed here (a test-isolation
gap, not a bug in today's changes) - flagged since it'll resurface the
next time real usage or another seeded batch accumulates in this
database, and the test suite has no separate test-DB path to fall back
on. The seed data itself was removed (reservation ids 1376-1525 and
their 59 feedback rows) per user decision, restoring a clean run rather
than leaving the demo data in with four tests visibly broken.

Verified: 201/201 tests pass (with the seed data removed). Both
templates' script blocks re-parsed clean (esprima, with Jinja
expressions neutralized first - `manager_analytics.html` embeds real
`{{ }}` templating inside its script tag, same as `base.html`, which
esprima can't parse as raw JS on its own). `staff_api.py` re-compiled
clean. Live-tested against the running dev server: confirmed the new
`demand_chart` response shape, confirmed the bar chart renders with the
low-sample-day lightening applied, confirmed the Staff Leaderboard and
Staff Productivity Ranking table show identical numbers (same source),
and confirmed both gracefully show all-zero/"not enough data" state
correctly now that the database is back to its small real dataset.

---

## ✅ Item 49 done (2026-09-16): full-system redundancy/inconsistency/trash-code audit

Direct user request to analyze the whole system and just remove what's
confirmed trash. Used two research-only subagents (one backend, one
frontend) to exhaustively cross-reference every function/route/class/id
against its call sites before anything was deleted - every item below was
verified zero-reference (or verified genuinely duplicated) before being
touched, same rigor this project has used throughout. One subagent
finding turned out to be a false positive (see below) - caught by running
the test suite before trusting the report.

**Environment/file hygiene:**
- Untracked a stray compiled `_Backend/__pycache__/ai_engine.cpython-314.pyc`
  from git (`.gitignore` already covers `__pycache__/` going forward, but
  never retroactively untracks a file added before the ignore rule
  existed).
- Removed `_Backend/.env` - a dead duplicate. Traced `config.py`'s
  `load_dotenv_custom()` call precisely (`BASE_DIR` resolves two
  `dirname()` calls up from `_Backend/config.py`, i.e. the repo root) and
  confirmed only the root-level `.env` is ever actually read; `_Backend/.env`
  had never been loaded by anything.
- Removed `_Backend/.venv/` - a second, orphaned Python virtual
  environment (357MB) alongside the root `.venv/` actually in use.
  Neither is git-tracked, so this is pure local disk reclamation, not a
  repo change.
- Removed 3 orphaned images from `_Frontend/static/images/` (`anniv.jpg`,
  `mot.jpg`, `scent.jpg`, ~410KB combined) - confirmed zero references
  anywhere in the codebase.

**Backend consolidation** (`_Backend/utils.py` gained the shared
implementations; each duplicate site now calls it instead):
- `mask_plate()` - the identical 3-tail plate-masking expression existed
  independently in `queue_api.py` (`api_get_queue`) and `feedback_api.py`
  (`api_get_feedbacks`).
- `get_on_duty_staff_names()` - `api_recommend_staff` was re-issuing the
  exact same "latest attendance row per staff, Present/Late, not clocked
  out" SQL that `get_active_staff_count()` already encapsulated, instead
  of calling it. `get_active_staff_count()` is now `len(get_on_duty_staff_names())`.
- `parse_duration_minutes()` + `MAX_PLAUSIBLE_SERVICE_MINUTES` - the same
  "parse two timestamps, reject negative/implausible" logic was written
  twice inside `ai_retrain.py` alone (`_fetch_real_training_rows` and
  `compute_prediction_accuracy`), and the same 480-minute bound was
  declared independently three times under two different names across
  `ai_retrain.py` and `staff_api.py`. `staff_api.py`'s two call sites keep
  their own inline parsing (one needs strictly-positive duration, the
  other tolerates a legacy time-only format neither of the other two
  handle) but now reference the one shared constant.
- **False positive caught by the test suite**: the audit also flagged
  `app.py`'s `get_db_connection` import as unused (nothing in `app.py`
  itself calls it). Removing it broke 18 of 22 test files, which import
  it via `from app import app, get_db_connection` - a re-export the
  static check didn't account for. Reverted with a comment explaining
  why it must stay.
- `database.py`'s `idx_active_slot_unique` partial index dropped four
  status strings (`'queue'`, `'cleaning'`, `'pending'`, `'scheduled'`)
  confirmed never produced by any insert/update anywhere - the same
  tolerance item 35 already removed from every `WHERE ... IN (...)`
  clause elsewhere, but this one index definition was left alone at the
  time since `CREATE INDEX IF NOT EXISTS` can't retroactively narrow an
  already-initialized database's index anyway. Fixed now for source
  correctness on any fresh install.
- `database/md57_schema.sql` (the "documentation only" reference file)
  was missing two real things entirely: the `login_attempts` table
  (live since item 4) and `idx_active_slot_unique` (the actual database-
  level guarantee behind item 2's race-condition fix) - both added.

**Frontend cleanup:**
- Removed a ~130-line dead CSS block in `manager_analytics.html` - a
  full date-picker/calendar-widget stylesheet (`.calendar-modal-content`,
  `.picker-header`, `.day-cell` and its variants, `.time-slot-card`, etc.)
  with no matching HTML or JS anywhere in the file - looks like a
  copy-paste leftover from `homepage.html`'s real calendar widget.
- Removed the entire permanently-fake weather "Hourly Forecast Ribbon"
  (9 hardcoded `--°C` cards, never populated by any code) and two
  never-written stat fields (UV Index, Dew Point) from the same page's
  weather widget - the same "fake data presented as real" pattern this
  project has repeatedly caught and removed elsewhere (items 10, 11, 35).
  The four stats `fetchLiveWeather()` actually does populate (Wind,
  Humidity, Pressure, Visibility) are untouched.
- Removed a handful of confirmed-dead CSS rules (`.page-btn.active` in
  `ready.html`, `.notice` in `forgot_password.html`) and dead `id`
  attributes with zero JS/CSS reference across `cancelled.html`,
  `feedback.html`, `homepage.html`, `staff.html`, `booking.html`, and
  `login.html`.
- **Real bug fixed, not just cleaned up**: `staff.html`'s "Add Bonus"
  modal has a "Reason" text field, but `applyStaffBonus()` hardcoded
  `reason: 'Manual bonus'` in its POST body instead of reading the
  field's value - whatever an admin actually typed there was silently
  discarded. The backend (`/api/staff-bonus`) already fully supported a
  real `reason`; the frontend just never sent it. Now reads and sends the
  typed value, falling back to the same default only when left blank.
- **Consolidated into `common.js`** (loaded on every authenticated page
  via `base.html`), replacing local copies in every page that had one:
  - `getNormalizedBayKey()` - was independently reimplemented in
    `cancelled.html`, `queue_dashboard.html`, and `ready.html`, and the
    three copies had **actually diverged** (two only matched the real
    `'Bay #N'` format, one also tolerated a `'Bay N'` variant nothing
    ever produces) - a real cross-page consistency risk, not just
    duplication, now a single source of truth.
  - `mapServiceToSlug()` - duplicated in `booking.html` and
    `queue_dashboard.html` (one copy wasn't null-safe).
  - `md57Channel` (the shared `BroadcastChannel` for cross-tab refresh)
    and `triggerCrossScreenSync()` - each of `booking.html`,
    `cancelled.html`, `ready.html`, and `queue_dashboard.html` created
    its own channel instance under the identical name. Each page's own
    `.onmessage` handler (what to actually refresh) stays local - only
    the channel and the "announce a change" call are shared.
  - `triggerCSVDownload()` - the identical "wrap a built CSV string in a
    throwaway `<a>` and click it" tail was reimplemented in
    `history.html`, `queue_dashboard.html`, and `staff.html`'s three
    separate CSV export functions; each keeps its own column-building
    logic and just calls the shared tail now.

**Deliberately not touched** (flagged by the audit but not redundancy in
the "delete one copy" sense): `openModal`/`closeModal` are re-declared in
`homepage.html` and `manager_analytics.html`, but this is a documented,
intentional second modal system (`.floating-modal` display-toggling
instead of `common.js`'s `.modal-overlay` classList system) - not blind
copy-paste. `handleLogout` is re-declared in five templates, also
explicitly documented as intentional (a farewell-animation redirect vs.
the immediate one in `common.js`) with genuinely different behavior.
`toggleTheme`/`syncThemeToggleIcon` are duplicated across the four
standalone pre-auth pages (`login`, `register`, `forgot_password`,
`reset_password`) because none of them load `common.js` at all (they
don't extend `base.html`) - consolidating that would mean adding a new
shared script include to four pages that currently have none, a bigger
change than this pass's scope.

Verified: 201/201 tests pass throughout (including after the
false-positive revert). Every touched template's script block re-parsed
clean via esprima (Jinja expressions neutralized first where a template
embeds real `{{ }}` templating inside its `<script>` tag), `common.js`
re-parsed clean past its one pre-existing, unrelated `?.` optional-
chaining line (esprima 4.0.1 predates that syntax; not something this
pass touched). Live-tested against the running dev server across all 9
pages that consume the newly shared `common.js` functions: zero console
errors on any of them, and directly exercised `getNormalizedBayKey()`
and `mapServiceToSlug()` in the browser to confirm identical, correct
output post-consolidation.

---

## ✅ Items 50-56 done (2026-09-19): panel-demo feature pass

Seven direct user requests aimed at an upcoming demo/panel review - all
real data end-to-end (no seeded/mock values introduced anywhere, including
in fallback/error paths), verified against the live 228-test suite (same
222/228 baseline before and after - the 6 pre-existing failures are
environment-only: a throwaway dev `.env` missing real Mail/SMS credentials
and shared test-DB login-lockout state across the suite, not code
regressions) plus live template/JS syntax checks (Jinja2 `env.parse()`,
esprima with `{{ }}`/`{% %}` neutralized) and a live test-client render of
every touched page across all 3 roles.

**50. Historical trend chart** - `get_analytics_metrics` (`staff_api.py`)
now returns `trend_chart`: real bookings-per-day and revenue-per-day
(`SUM(services.price)`) for the last 30 days, grouped from actual
`completed_at` timestamps only (`queue_status IN ('ready','completed')`).
`has_data` is `false` whenever the window is empty - `manager_analytics.html`
shows an honest "not enough data yet" panel (`#trendEmptyState`) instead of
a chart in that case, and critically, `getDefaultData()`'s fallback (used
only if the whole `/api/analytics-metrics` fetch fails outright) sets
`trend_chart` to empty/`has_data:false` rather than reusing the synthetic
fallback numbers every *other* chart on this page uses - this one chart is
real data or nothing, by explicit request, even during development. New
tab renders it as a dual-axis bar+line (bookings on the left axis, revenue
on the right - two datasets on very different scales sharing one x-axis).

**51. Visible chart tabs** - the `<select id="mainChartSelector">` dropdown
(5 options, all hidden until opened) is replaced with a row of 6 always-
visible `.chart-tab-btn` buttons (the original 5 plus the new Historical
Trend), active state driven by `switchMainChart(type)` now taking the type
directly instead of reading `.value` off a select element.

**52. PDF export letterhead + written summary** - `/export-pdf`
(`staff_api.py`) now opens with a real letterhead (MD57 logo image via
reportlab's `Image` flowable + shop name + a real report-period date range
computed from `MIN(reservations.created_at)` through today, not a fixed
window) instead of a bare title. Closes with a `Summary` paragraph built
entirely from the numbers already computed earlier in the same function
(`total_bookings`, `completed_bookings`, `avg_rating`, `ranked_staff`) via
plain Python if/else - explicitly not a live LLM call, matching the user's
own reasoning that an internal report a manager already reads directly
doesn't need a new external dependency/failure mode for this. Incidental
real bug fixed while touching this code: the "Completed Jobs" summary-table
figure filtered on `queue_status = 'completed'` only, while every other
"completed" count on this dashboard (`get_analytics_metrics`,
`/api/admin/metrics`, the KPI strip) counts `IN ('ready','completed')` -
the PDF was silently undercounting against its own dashboard; now
consistent. Caught and fixed one real rendering bug during verification:
the letterhead's custom `ParagraphStyle`s inherited `styles['Normal']`'s
12pt leading while bumped to 16pt font size, so the title visually
overlapped the subtitle line beneath it - fixed by setting `leading`
explicitly on each custom style. Verified by generating and visually
reading back an actual PDF, twice (before/after the leading fix).

**53. Weather "Outlook" ties forecast to a staffing decision** - new
`utils.get_weather_forecast_tomorrow()` (mirrors the existing
`get_weather_condition()`'s bucketing/never-block-the-caller shape, but
against OpenWeatherMap's 5-day/3-hour forecast endpoint, picking the
slot closest to tomorrow's midday) backs a new `/api/weather-outlook`
endpoint: compares MD57's own historical average bookings/day on days
real reservations were tagged `weather_condition = 'Rainy'` at booking
time against `'Sunny'`/`'Cloudy'` days (grouped by `created_at`'s calendar
day, not `booking_date` - the latter is `NULL` for walk-ins and can be a
future date for scheduled bookings, neither of which is "what the weather
actually was"). Returns a plain-language one-liner ("Rain expected
tomorrow - bookings typically run ~X% lower on rainy days at this shop")
shown in `manager_analytics.html`'s weather tab, or an honest "not enough
historical data yet" note when either bucket has fewer than 3 distinct
days on record - same no-fabricated-stats rule the rest of this dashboard
already follows. Verified with `WEATHER_API_KEY` unset (the dev
environment used for this session): the endpoint degrades cleanly to
`tomorrow_condition: null` / "forecast unavailable" rather than erroring.

**54. Persistent top nav** - `base.html`'s header gained a `.top-nav` row
(Queue, Book, and role-gated Analytics/History) sitting between the logo
and the user badge, active-state driven by `request.path`. Removed from
the "Menu" dropdown to avoid a duplicate/confusing second copy of the same
links; the dropdown now holds only the less-frequent destinations (Home,
Dashboard, Cancel, Ready, Staff, Create User, Feedback), matching the
user's own split. Drops to a horizontally-scrollable full-width row under
768px rather than wrapping/shrinking the link text.

**55. Real KPIs above the fold** - `/api/admin/metrics` (`admin_api.py`)
gained `completed_today`, `avg_wait_today_minutes` (via the existing
`parse_duration_minutes` helper, Python-local "today" same as
`api_submit_booking`'s `created_at` convention - not SQLite's UTC
`date('now')`) and `active_staff` (`get_active_staff_count()`), and was
opened from admin-only to admin+staff (homepage.html shows this strip to
staff too; nothing returned is more sensitive than `/api/get-queue`
already exposes to staff). `admin-dashboard.html`'s existing 3-stat strip
became the requested 4 (Current Queue Length, Completed Today, Avg Wait
Today, Active Staff), and the identical strip was added to `homepage.html`
above the nav-card grid, admin/staff-only, from the same endpoint so the
two pages never disagree.

**56. FCFS confirmed and documented (verification only, no logic
changed)** - traced `queue_number` end to end: assigned once at insert as
`MAX(queue_number)+1` (`api_submit_booking`), never recomputed, and
`api_get_queue`'s `ORDER BY queue_number ASC, reservation_id ASC` is the
entire ordering logic - no priority/urgency field exists anywhere in the
schema. Added explicit docstring notes to both `api_get_queue` and
`api_recommend_staff` cross-referencing each other: the MCDA staff
recommender only ranks which *staff member* should take the next job and
has zero effect on which *customer* is served next, which was already
true in the code and already worded carefully in
`api_recommend_staff`'s docstring and the "You've been recommended..."
UI copy in `queue_dashboard.html` - this item made the distinction
explicit and cross-linked rather than changing any behavior.
