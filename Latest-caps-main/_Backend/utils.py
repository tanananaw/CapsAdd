import os
import json
import threading
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from functools import wraps

from flask import session, request, jsonify, redirect, url_for
from flask_mail import Message
from markupsafe import escape

from config import ACTIVITY_LOG_FILE, LOGO_PATH
from database import get_db_connection
from extensions import mail
from live_updates import publish


# --- BRANDED EMAIL ---
_LOGO_CID = 'md57_logo'


def send_branded_email(subject, recipient, heading, plain_text_body, highlight=None):
    """Every real customer-facing email in this app (ready notifications,
    no-show releases, password reset codes) used to be bare plain text.
    This wraps the same content in an HTML layout carrying the MD57 logo,
    with the plain-text version kept as the `body` fallback for clients
    that don't render HTML. The logo is attached inline via Content-ID
    (cid:) rather than linked to this app's own /static/ URL, so it
    displays correctly in the recipient's inbox even when this app isn't
    publicly reachable (e.g. running on localhost during development).
    `highlight`, if given, renders as a large bold letter-spaced box (e.g.
    an OTP code) between the heading and the body paragraphs."""
    paragraphs = [p.strip() for p in plain_text_body.split('\n\n') if p.strip()]
    html_paragraphs = ''.join(
        f'<p style="margin:0 0 14px 0; color:#334155; font-size:14px; line-height:1.6; white-space:pre-line;">{escape(p)}</p>'
        for p in paragraphs
    )
    highlight_html = ''
    if highlight:
        highlight_html = (
            '<div style="margin:0 0 18px 0; padding:16px; background:#f8fafc; border:1px dashed #cbd5e1; '
            'border-radius:8px; text-align:center;">'
            f'<span style="font-size:28px; font-weight:700; letter-spacing:8px; color:#9c7a2e; '
            f'font-family:\'Courier New\',monospace;">{escape(highlight)}</span>'
            '</div>'
        )
    html_body = f"""
    <div style="max-width:480px; margin:0 auto; font-family:'Segoe UI',Arial,sans-serif; background:#f1f3f6; padding:24px;">
        <div style="background:#0d1b30; border-radius:12px 12px 0 0; padding:22px; text-align:center;">
            <img src="cid:{_LOGO_CID}" alt="MD57 Car Wash" width="64" height="64" style="width:64px; height:64px; border-radius:10px; object-fit:contain; background:#ffffff; padding:4px; display:inline-block;">
            <h1 style="margin:10px 0 0 0; color:#f3f0e8; font-size:16px; font-weight:700;">MD57 Car Wash &amp; Auto Detailing</h1>
        </div>
        <div style="background:#ffffff; border:1px solid #e2e8f0; border-top:none; border-radius:0 0 12px 12px; padding:24px;">
            <h2 style="margin:0 0 14px 0; color:#0d1b30; font-size:18px; font-weight:700;">{escape(heading)}</h2>
            {highlight_html}
            {html_paragraphs}
        </div>
        <p style="text-align:center; color:#94a3b8; font-size:11px; margin-top:16px;">MD57 Car Wash &amp; Auto Detailing &bull; This is an automated message.</p>
    </div>
    """

    msg = Message(subject=subject, recipients=[recipient], body=plain_text_body, html=html_body)
    try:
        with open(LOGO_PATH, 'rb') as f:
            msg.attach('md57logo.jpg', 'image/jpeg', f.read(), headers={'Content-ID': f'<{_LOGO_CID}>'})
    except OSError as e:
        print(f"[WARNING] Could not attach logo to email: {e}")
    mail.send(msg)


# Display order/labels for the 3 published pricing categories - shared by
# /api/services (booking form + Pricing modal) and the analytics service-mix
# chart, so a category never renders in a different order or under a
# different label in one place than another.
SERVICE_CATEGORY_LABELS = [
    ('general', 'Car Wash & General'),
    ('detailing', 'Detailing'),
    ('motorcycle', 'Motorcycle'),
]


# No car wash service plausibly takes longer than this - shared by every
# place that turns a created_at/completed_at (or started_at/completed_at)
# pair into a duration and needs to reject corrupt/implausible rows: the
# analytics wait-time chart, per-staff speed scoring, and the AI retraining
# pipeline's data fetch and accuracy calculation. Used to be declared
# independently (and inconsistently named) in three different places.
MAX_PLAUSIBLE_SERVICE_MINUTES = 480


def parse_duration_minutes(start_str, end_str):
    """Parses two 'YYYY-MM-DD HH:MM:SS' timestamps and returns the minutes
    between them, or None if either is missing/malformed or the result
    isn't plausible (negative, or over MAX_PLAUSIBLE_SERVICE_MINUTES) -
    callers treat None as "exclude this row" rather than special-casing
    each failure mode themselves."""
    if not start_str or not end_str:
        return None
    try:
        start_dt = datetime.strptime(start_str, '%Y-%m-%d %H:%M:%S')
        end_dt = datetime.strptime(end_str.strip(), '%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError, AttributeError):
        return None

    diff = (end_dt - start_dt).total_seconds() / 60
    if diff < 0 or diff > MAX_PLAUSIBLE_SERVICE_MINUTES:
        return None
    return diff


def mask_plate(plate):
    """Reduces a plate number to a masked tail (e.g. 'ABC-1234' -> '****234')
    for any viewer who isn't staff/admin/the booking's own customer - a
    shared 3-character-tail convention so the queue board and the public
    feedback wall mask PII identically instead of drifting independently."""
    plate = plate or ''
    return ('*' * max(0, len(plate) - 3)) + plate[-3:] if len(plate) > 3 else plate


def start_periodic_worker(app, job, interval_seconds, name):
    """Runs job() inside an app context every interval_seconds on a daemon
    thread, starting immediately. A failed run is logged and retried next
    cycle instead of killing the thread. Call once at app startup."""
    def _loop():
        while True:
            try:
                with app.app_context():
                    job()
            except Exception as e:
                print(f"[WARNING] {name} run failed: {e}")
            time.sleep(interval_seconds)

    threading.Thread(target=_loop, daemon=True, name=name).start()


# --- ACTIVITY LOGGING FUNCTION (Audit Trail) ---
def log_activity(username, action):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = f'[{timestamp}] User: {username} - Action: {action}\n'
    try:
        with open(ACTIVITY_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_entry)
    except Exception as e:
        print(f'Error writing log: {e}')


# --- WEATHER LOOKUPS (server-side, so WEATHER_API_KEY never reaches a browser) ---
# Every public function below returns None on any failure (missing key,
# network issue, bad response) - a weather hiccup must never block a
# booking or break a dashboard.
def _openweather_get(endpoint, timeout):
    """GETs OpenWeatherMap's /data/2.5/<endpoint> for WEATHER_CITY, or
    returns None when no API key is configured."""
    api_key = os.environ.get('WEATHER_API_KEY')
    if not api_key:
        return None
    city = os.environ.get('WEATHER_CITY', 'Malabon')
    # ",PH" pins this to the Philippines - without a country code, the
    # city-name lookup can resolve to a same-named city elsewhere.
    query = urllib.parse.urlencode({'q': f'{city},PH', 'appid': api_key, 'units': 'metric'})
    url = f'https://api.openweathermap.org/data/2.5/{endpoint}?{query}'
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _bucket_weather(reading):
    """Buckets one OpenWeatherMap reading into 'Sunny', 'Cloudy', or 'Rainy'
    for analytics, or None if it carries no condition."""
    main = reading.get('weather', [{}])[0].get('main', '').lower()
    if main in ('rain', 'drizzle', 'thunderstorm'):
        return 'Rainy'
    if main == 'clear':
        return 'Sunny'
    return 'Cloudy' if main else None


# Every wait-time prediction and booking reads the current weather; one
# lookup is shared for this long instead of calling the API each time.
WEATHER_CACHE_SECONDS = 5 * 60
_weather_cache = {'at': 0.0, 'reading': None}


def get_weather_reading():
    """Current conditions as {'condition': 'Sunny'|'Cloudy'|'Rainy'|None,
    'rain_mm': rainfall in the last hour (0.0 if none or unknown)} - stored
    on each booking and fed to the wait-time model."""
    now = time.time()
    if _weather_cache['reading'] is not None and now - _weather_cache['at'] < WEATHER_CACHE_SECONDS:
        return _weather_cache['reading']
    reading = {'condition': None, 'rain_mm': 0.0}
    try:
        payload = _openweather_get('weather', timeout=3)
        if payload:
            reading = {
                'condition': _bucket_weather(payload),
                'rain_mm': float((payload.get('rain') or {}).get('1h', 0.0)),
            }
    except Exception as e:
        print(f'[WARNING] Weather lookup failed, using no weather data: {e}')
    _weather_cache.update(at=now, reading=reading)
    return reading


def get_weather_snapshot():
    """Full current conditions (temperature, description, wind, humidity,
    pressure, visibility) for manager_analytics.html's live weather widget."""
    try:
        payload = _openweather_get('weather', timeout=4)
        if not payload:
            return None

        main = payload.get('main', {})
        wind = payload.get('wind', {})
        weather_list = payload.get('weather', [{}])
        visibility_m = payload.get('visibility')

        return {
            'temp_c': round(main.get('temp')) if main.get('temp') is not None else None,
            'humidity': main.get('humidity'),
            'pressure': main.get('pressure'),
            'wind_speed': wind.get('speed'),
            'condition': weather_list[0].get('description') if weather_list else None,
            'visibility_km': round(visibility_m / 1000, 1) if visibility_m is not None else None,
            'rain_mm': (payload.get('rain') or {}).get('1h', 0.0),
        }
    except Exception as e:
        print(f'[WARNING] Weather snapshot lookup failed: {e}')
        return None


def get_weather_forecast_tomorrow():
    """Tomorrow's midday-ish forecast from the 5-day/3-hour endpoint,
    bucketed like get_weather_reading(). Backs the manager analytics
    "Outlook" note (see api_weather_outlook)."""
    try:
        payload = _openweather_get('forecast', timeout=4)
        if not payload:
            return None

        tomorrow_str = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        entries_tomorrow = [e for e in payload.get('list', []) if e.get('dt_txt', '').startswith(tomorrow_str)]
        if not entries_tomorrow:
            return None

        # The 3-hour slot closest to midday is the most representative
        # reading for "what's tomorrow like".
        def _hour_of(entry):
            try:
                return int(entry['dt_txt'][11:13])
            except (KeyError, ValueError, TypeError):
                return 12
        chosen = min(entries_tomorrow, key=lambda e: abs(_hour_of(e) - 12))
        return _bucket_weather(chosen)
    except Exception as e:
        print(f'[WARNING] Weather forecast lookup failed: {e}')
        return None


# --- IN-APP NOTIFICATION HELPERS ---
# Independent of email/Gmail delivery on purpose (send_branded_email above) -
# a user who opted into email reminders still gets one of these rows too,
# and a mail-server outage never prevents an in-app notification from being
# recorded. Every call here is fire-and-forget: it opens/closes its own
# short-lived connection and never raises, so a notification failure can
# never break the booking/attendance/queue action that triggered it.
def create_notification(user_id, title, message, category='general', related_reservation_id=None):
    """Inserts one in-app notification for exactly one account. user_id must
    be a real users.user_id - callers that only have a display name (e.g.
    staff_name) need to resolve it to a user_id first."""
    if not user_id:
        return
    try:
        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO notifications (user_id, title, message, category, related_reservation_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, title, message, category, related_reservation_id),
        )
        conn.commit()
        conn.close()
        publish('notifications', [user_id])
    except Exception as e:
        print(f"[WARNING] Failed to create notification for user {user_id}: {e}")


def notify_role(role, title, message, category='general', related_reservation_id=None, exclude_user_id=None):
    """Fans a notification out to every account with the given role - the
    admin-wide/staff-wide broadcast case ('New Booking', 'Staff Attendance',
    etc.). exclude_user_id skips the acting user's own account (e.g. an
    admin who just performed the action doesn't need to be told about it)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE role = ?", (role,))
        user_ids = [row['user_id'] for row in cursor.fetchall() if row['user_id'] != exclude_user_id]
        conn.close()
    except Exception as e:
        print(f"[WARNING] Failed to look up {role} accounts for notification fan-out: {e}")
        return
    for uid in user_ids:
        create_notification(uid, title, message, category, related_reservation_id)


# --- ROBUST SERVER-SIDE RBAC DECORATOR ---
def role_required(*allowed_roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user' not in session:
                if request.path.startswith('/api/'):
                    return jsonify({'status': 'error', 'message': 'Authentication required. Please log in.'}), 401
                return redirect(url_for('auth.login_page', next=request.url))

            user_role = session.get('role')
            if not user_role or user_role not in allowed_roles:
                if request.path.startswith('/api/'):
                    return jsonify({
                        'status': 'error',
                        'message': f'Access denied: {user_role} role is not permitted for this resource.'
                    }), 403
                return redirect(get_redirect_for_role(user_role))

            return f(*args, **kwargs)
        return decorated_function
    return decorator


def get_redirect_for_role(role):
    if role == 'admin':
        return url_for('pages.analytics_page')
    elif role == 'staff':
        return url_for('pages.queue_page')
    else:
        return url_for('pages.home')


def current_staff_name():
    """The session's display name, in the same COALESCE(fullname, username)
    form staff_attendance/staff_shifts/staff_unavailability store."""
    return session.get('fullname') or session.get('user')


def get_latest_attendance_by_staff(date_str=None):
    """One row per staff account for the given date (defaults to today): the
    row that's "true right now". staff_attendance is insert-only (never
    updated in place), so the highest attendance_id wins - SQLite fills the
    bare columns from that MAX() row. staff_name is the account's current
    display name."""
    if date_str is None:
        date_str = datetime.now().strftime('%Y-%m-%d')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT a.staff_user_id, COALESCE(u.fullname, u.username) AS staff_name,
               a.date_str, a.status, a.time_in, a.time_out, MAX(a.attendance_id) AS latest_id
        FROM staff_attendance a
        JOIN users u ON u.user_id = a.staff_user_id
        WHERE a.date_str = ?
        GROUP BY a.staff_user_id
        """,
        (date_str,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_latest_attendance(staff_user_id, date_str=None):
    """Same as get_latest_attendance_by_staff, narrowed to one staff account.
    Returns None if they have no row at all for the day."""
    if date_str is None:
        date_str = datetime.now().strftime('%Y-%m-%d')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT status, time_in, time_out, MAX(attendance_id) AS latest_id
        FROM staff_attendance
        WHERE date_str = ? AND staff_user_id = ?
        GROUP BY staff_user_id
        """,
        (date_str, staff_user_id),
    )
    row = cursor.fetchone()
    conn.close()
    return row


def is_on_duty_record(row):
    """The one definition of "on duty": clocked in (Present/Late) and not
    yet clocked out. Mirrored client-side by staff.html's
    renderAvailableStaff()."""
    return bool(row) and row['status'] in ('Present', 'Late') and row['time_out'] == '--:--'


def get_on_duty_staff_ids(date_str=None):
    return {row['staff_user_id'] for row in get_latest_attendance_by_staff(date_str) if is_on_duty_record(row)}


def get_active_staff_count(date_str=None):
    return len(get_on_duty_staff_ids(date_str))


def is_date_staffed(date_str):
    """Whether a customer can be served on date_str: at least one staff
    member is on the admin's shift roster for that day without having
    marked themselves unavailable - or, for today only, someone is already
    clocked in (staff can be on duty without a roster entry)."""
    conn = get_db_connection()
    rostered = conn.execute(
        """
        SELECT 1 FROM staff_shifts s
        WHERE s.shift_date = ? AND s.staff_user_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM staff_unavailability u
              WHERE u.staff_user_id = s.staff_user_id AND u.unavailable_date = s.shift_date
          )
        LIMIT 1
        """,
        (date_str,),
    ).fetchone()
    conn.close()
    if rostered:
        return True
    return date_str == datetime.now().strftime('%Y-%m-%d') and get_active_staff_count() > 0


# How many scheduled bookings one bay takes per hourly slot - a bay works
# one car at a time, so beyond a few per hour the queue only grows. Walk-ins
# (already physically here) aren't counted against it.
MAX_BOOKINGS_PER_BAY_PER_HOUR = int(os.environ.get('MAX_BOOKINGS_PER_BAY_PER_HOUR', 3))
ACTIVE_BOOKING_STATUSES = ('waiting', 'in_progress', 'pending_confirmation')


def slot_booking_counts(date_str=None):
    """{(booking_date, booking_time, assigned_bay): active scheduled
    bookings} for one day - or, without date_str, every day from today on."""
    conn = get_db_connection()
    date_clause, date_value = ('booking_date = ?', date_str) if date_str else ('booking_date >= ?', datetime.now().strftime('%Y-%m-%d'))
    rows = conn.execute(
        f"""
        SELECT booking_date, booking_time, assigned_bay, COUNT(*) AS cnt FROM reservations
        WHERE {date_clause} AND booking_time IS NOT NULL
          AND queue_status IN ({','.join('?' * len(ACTIVE_BOOKING_STATUSES))})
        GROUP BY booking_date, booking_time, assigned_bay
        """,
        (date_value, *ACTIVE_BOOKING_STATUSES),
    ).fetchall()
    conn.close()
    return {(r['booking_date'], r['booking_time'], r['assigned_bay']): r['cnt'] for r in rows}


def slot_has_room(date_str, time_str, bay, exclude_reservation_id=None):
    """Whether bay can take another scheduled booking at date_str/time_str
    (exclude_reservation_id: a booking being moved doesn't count against its
    own new slot)."""
    conn = get_db_connection()
    taken = conn.execute(
        f"""
        SELECT COUNT(*) FROM reservations
        WHERE booking_date = ? AND booking_time = ? AND assigned_bay = ? AND reservation_id != ?
          AND queue_status IN ({','.join('?' * len(ACTIVE_BOOKING_STATUSES))})
        """,
        (date_str, time_str, bay, exclude_reservation_id or -1, *ACTIVE_BOOKING_STATUSES),
    ).fetchone()[0]
    conn.close()
    return taken < MAX_BOOKINGS_PER_BAY_PER_HOUR


def next_bookable_date(after_date_str, time_str, bay, exclude_reservation_id=None):
    """The earliest date after after_date_str that has an available rostered
    staff member (see is_date_staffed) AND room in `bay` at `time_str`, or
    None if the roster has no such day yet."""
    conn = get_db_connection()
    dates = [r[0] for r in conn.execute(
        """
        SELECT DISTINCT s.shift_date FROM staff_shifts s
        WHERE s.shift_date > ? AND s.staff_user_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM staff_unavailability u
              WHERE u.staff_user_id = s.staff_user_id AND u.unavailable_date = s.shift_date
          )
        ORDER BY s.shift_date
        LIMIT 60
        """,
        (after_date_str,),
    )]
    conn.close()
    for day in dates:
        if slot_has_room(day, time_str, bay, exclude_reservation_id):
            return day
    return None


def is_staff_on_duty(staff_user_id, date_str=None):
    """Used to stop a staff account from starting a job while admin never
    actually clocked them in."""
    return is_on_duty_record(get_latest_attendance(staff_user_id, date_str))


# MD57 operates 8:00 AM - 9:00 PM. Kept as shared constants (not scattered
# literals) since both the booking-time validation and the walk-in check
# below need to agree on exactly the same window.
BUSINESS_OPEN_HOUR = 8
BUSINESS_CLOSE_HOUR = 21  # 9:00 PM, inclusive - the last bookable hour


def is_within_business_hours(dt):
    """True if dt's time-of-day (its date is ignored) falls within MD57's
    8:00 AM - 9:00 PM operating window, inclusive of both ends - 9:00 PM
    itself is still a valid time, matching the last slot the booking UI
    offers."""
    minutes = dt.hour * 60 + dt.minute
    return (BUSINESS_OPEN_HOUR * 60) <= minutes <= (BUSINESS_CLOSE_HOUR * 60)


def parse_booking_datetime(booking_date, booking_time):
    """Parses a reservation's stored booking_date ('YYYY-MM-DD') and
    booking_time ('HH:MM AM/PM' from the booking UI, or 'HH:MM' from any
    24h-format source) into a single datetime. Returns None if either value
    is missing or doesn't match a known format, rather than raising -
    callers should treat that as "can't determine a scheduled time" and
    decide what that means for them."""
    if not booking_date or not booking_time:
        return None
    for fmt in ('%Y-%m-%d %I:%M %p', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(f'{booking_date} {booking_time}', fmt)
        except (ValueError, TypeError):
            continue
    return None
