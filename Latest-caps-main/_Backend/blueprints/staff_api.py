import base64
import csv as csv_module
import io
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request, session, send_file, Response
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, ListFlowable, ListItem

from config import LOGO_PATH
from database import get_db_connection
from extensions import ai_predictor, mcda_engine
from utils import (
    role_required, log_activity, get_active_staff_count, get_latest_attendance, is_date_staffed,
    ACTIVE_BOOKING_STATUSES,
    get_latest_attendance_by_staff, is_on_duty_record, current_staff_name,
    send_branded_email, get_on_duty_staff_ids,
    get_weather_forecast_tomorrow, get_weather_snapshot, get_weather_reading,
    parse_duration_minutes, create_notification, notify_role,
    SERVICE_CATEGORY_LABELS,
)

staff_api_bp = Blueprint('staff_api', __name__)


def _fetch_staff_accounts(cursor):
    cursor.execute(
        "SELECT user_id, COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' ORDER BY name"
    )
    return cursor.fetchall()


def _insert_attendance(staff_user_id, staff_name, date_str, status, time_in='--:--', time_out='--:--'):
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO staff_attendance (staff_user_id, staff_name, date_str, status, time_in, time_out) VALUES (?, ?, ?, ?, ?, ?)",
        (staff_user_id, staff_name, date_str, status, time_in, time_out),
    )
    conn.commit()
    conn.close()


def _staff_from_request(data):
    """(user_id, current display name) of the real staff account a request
    names - by staff_user_id (what the pages send) or by display name
    (older callers) - or (None, None) if it isn't one."""
    conn = get_db_connection()
    if data.get('staff_user_id'):
        row = conn.execute(
            "SELECT user_id, COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' AND user_id = ?",
            (data.get('staff_user_id'),),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT user_id, COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' AND COALESCE(fullname, username) = ? ORDER BY user_id LIMIT 1",
            ((data.get('staff_name') or '').strip(),),
        ).fetchone()
    conn.close()
    return (row['user_id'], row['name']) if row else (None, None)


@staff_api_bp.route('/api/get-staff-list', methods=['GET'])
@role_required('admin', 'staff')
def api_get_staff_list():
    """Real seeded staff accounts (role='staff'), replacing the frontend's
    old hardcoded fictional roster."""
    conn = get_db_connection()
    rows = [dict(row) for row in _fetch_staff_accounts(conn.cursor())]
    conn.close()
    return jsonify({'status': 'success', 'data': rows}), 200



def _compute_real_metrics_for_staff(cursor, staff_id):
    """Real Speed/Completion%/Rating for one staff member - shared by
    /api/staff-real-metrics and /api/recommend-staff so both read the exact
    same definition of each metric instead of drifting apart.

    - speed: average of min(100, benchmark_minutes / actual_minutes * 100)
      across jobs with both a real started_at and completed_at (both set by
      an actual staff action now, not a countdown timer) - None if the
      staff member has no such jobs yet.
    - completion: percent of a staff member's *resolved* jobs (completed or
      cancelled - a still-in-progress job hasn't been decided yet, so it's
      excluded rather than counted against them) that ended in completion.
    - rating: average customer feedback rating for reservations that staff
      member handled, joined via feedbacks.reservation_id ->
      reservations.assigned_staff_id.

    Any metric with no underlying data comes back as None, not a fabricated
    default - callers decide how to present "not enough data yet"."""
    cursor.execute(
        """
        SELECT r.started_at, r.completed_at, s.base_duration_minutes
        FROM reservations r
        JOIN services s ON r.service_id = s.service_id
        WHERE r.assigned_staff_id = ?
          AND r.queue_status IN ('ready', 'completed')
          AND r.started_at IS NOT NULL
          AND r.completed_at IS NOT NULL AND r.completed_at != ''
        """,
        (staff_id,),
    )
    speed_scores = []
    for row in cursor.fetchall():
        actual_minutes = parse_duration_minutes(row['started_at'], row['completed_at'])
        if not actual_minutes:  # None (unusable row) or 0 (can't divide by it)
            continue
        speed_scores.append(min(100, (row['base_duration_minutes'] / actual_minutes) * 100))
    speed = round(sum(speed_scores) / len(speed_scores), 1) if speed_scores else None

    cursor.execute(
        """
        SELECT
            SUM(CASE WHEN queue_status IN ('ready', 'completed') THEN 1 ELSE 0 END) AS completed_count,
            SUM(CASE WHEN queue_status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled_count
        FROM reservations
        WHERE assigned_staff_id = ?
        """,
        (staff_id,),
    )
    comp_row = cursor.fetchone()
    resolved_count = (comp_row['completed_count'] or 0) + (comp_row['cancelled_count'] or 0)
    completion = round((comp_row['completed_count'] or 0) / resolved_count * 100, 1) if resolved_count else None

    cursor.execute(
        """
        SELECT AVG(f.rating) AS avg_rating, COUNT(*) AS rating_count
        FROM feedbacks f
        JOIN reservations r ON f.reservation_id = r.reservation_id
        WHERE r.assigned_staff_id = ?
        """,
        (staff_id,),
    )
    rating_row = cursor.fetchone()
    rating = round(rating_row['avg_rating'], 1) if rating_row['rating_count'] else None

    return speed, completion, rating


@staff_api_bp.route('/api/staff-real-metrics', methods=['GET'])
@role_required('admin', 'staff')
def api_staff_real_metrics():
    """Real Speed/Completion%/Rating per staff member - replaces
    manager_analytics.html's "Performance Metrics Control" panel, which
    used to require typing all three in by hand for every staff member on
    every visit, starting from arbitrary hardcoded defaults."""
    conn = get_db_connection()
    cursor = conn.cursor()
    staff_rows = _fetch_staff_accounts(cursor)

    results = []
    for staff in staff_rows:
        speed, completion, rating = _compute_real_metrics_for_staff(cursor, staff['user_id'])
        results.append({
            'user_id': staff['user_id'],
            'name': staff['name'],
            'speed': speed,
            'completion': completion,
            'rating': rating,
        })

    conn.close()
    return jsonify({'status': 'success', 'data': results}), 200


@staff_api_bp.route('/api/recommend-staff', methods=['GET'])
@role_required('admin', 'staff')
def api_recommend_staff():
    """Suggests the best available washer for the next job, instead of pure
    first-come-first-served self-claim. "Available" means clocked in today
    (same on-duty definition as get_active_staff_count/staff.html) AND not
    already in the middle of another job. Ranking among available staff
    reuses the existing MCDA/SAW engine (Speed 40% / Completion 40% /
    Rating 20%) that already powers the manager's productivity leaderboard,
    so "who's recommended" and "who's ranked #1 on the leaderboard" are
    answers from the same formula, not two competing ones.

    This is advisory only - staff still self-claim jobs by clicking Start.
    Nothing here blocks a different staff member from taking the job
    instead; it just tells whoever's looking who the data says should.

    Distinct concern from queue ordering: this never changes which
    customer/car is next - that's strict FCFS by queue_number, decided
    entirely in api_get_queue's ORDER BY. This only ranks which *staff
    member* should pick up whatever the queue already says is next."""
    conn = get_db_connection()
    cursor = conn.cursor()
    staff_rows = _fetch_staff_accounts(cursor)

    on_duty_ids = get_on_duty_staff_ids()

    cursor.execute(
        "SELECT DISTINCT assigned_staff_id FROM reservations WHERE queue_status = 'in_progress' AND assigned_staff_id IS NOT NULL"
    )
    busy_staff_ids = {row['assigned_staff_id'] for row in cursor.fetchall()}

    off_duty_count = 0
    busy_count = 0
    candidates = []
    for staff in staff_rows:
        if staff['user_id'] not in on_duty_ids:
            off_duty_count += 1
            continue
        if staff['user_id'] in busy_staff_ids:
            busy_count += 1
            continue

        speed, completion, rating = _compute_real_metrics_for_staff(cursor, staff['user_id'])
        cursor.execute(
            "SELECT COUNT(*) AS total FROM reservations WHERE assigned_staff_id = ?",
            (staff['user_id'],),
        )
        total_jobs = cursor.fetchone()['total']
        candidates.append({
            'user_id': staff['user_id'],
            'name': staff['name'],
            'speed': speed,
            'completion': completion,
            'rating': rating,
            'total_jobs': total_jobs,
            'has_history': speed is not None or completion is not None or rating is not None,
        })

    conn.close()

    if not candidates:
        reason = 'no_staff_on_duty' if not on_duty_ids else 'all_on_duty_staff_busy'
        return jsonify({
            'status': 'success',
            'recommended': None,
            'ranked_available': [],
            'reason': reason,
            'available_count': 0,
            'busy_count': busy_count,
            'off_duty_count': off_duty_count,
        }), 200

    ranked = mcda_engine.evaluate_staff([
        {'name': c['name'], 'service_speed': c['speed'], 'completion': c['completion'], 'feedback': c['rating']}
        for c in candidates
    ])
    by_name = {c['name']: c for c in candidates}
    # MCDA ties at 0.0 whenever a candidate has no track record yet
    # (fillna(0) inside evaluate_staff). Breaking ties by fewest
    # lifetime jobs turns that tie into fair rotation - a brand-new
    # hire gets suggested ahead of someone who's already had 40 jobs,
    # rather than every untested staff member silently losing forever
    # to whoever happened to be seeded with better stats.
    ranked.sort(key=lambda r: (-r['saw_score'], by_name[r['name']]['total_jobs']))

    ranked_available = []
    for entry in ranked:
        c = by_name[entry['name']]
        ranked_available.append({
            'user_id': c['user_id'],
            'name': c['name'],
            'speed': c['speed'],
            'completion': c['completion'],
            'rating': c['rating'],
            'total_jobs': c['total_jobs'],
            'has_history': c['has_history'],
            'saw_score': entry['saw_score'],
        })

    top = ranked_available[0]
    return jsonify({
        'status': 'success',
        'recommended': top,
        'ranked_available': ranked_available,
        'reason': 'ranked_by_performance' if top['has_history'] else 'no_track_record_yet_rotation_pick',
        'available_count': len(ranked_available),
        'busy_count': busy_count,
        'off_duty_count': off_duty_count,
    }), 200


@staff_api_bp.route('/api/staff-attendance', methods=['POST'])
@role_required('admin')
def api_staff_attendance():
    """Admin-only: staff can no longer clock themselves in/out (there was
    no PIN or other check that made a client-supplied identity trustworthy
    for that), so attendance is now something admin records for whichever
    real staff member has actually arrived."""
    data = request.get_json() or {}
    date_str = data.get('date_str')
    status = data.get('status')
    time_in = data.get('time_in', '--:--')
    time_out = data.get('time_out', '--:--')

    # Must reference a real staff account, not an arbitrary string.
    staff_user_id, staff_name = _staff_from_request(data)
    if not staff_user_id:
        return jsonify({'status': 'error', 'message': 'Unknown staff member.'}), 400

    _insert_attendance(staff_user_id, staff_name, date_str, status, time_in, time_out)

    # time_out stays '--:--' on a time-in action (see staff.html's
    # staffSelfAction) and is only ever set on a time-out action, so it
    # doubles as a reliable signal for which of the two just happened.
    if time_out == '--:--':
        log_activity(session.get('user', staff_name), f'Time-In: {staff_name} at {time_in}')
        notif_message = f'Attendance recorded: Time-In at {time_in} ({status}).'
    else:
        log_activity(session.get('user', staff_name), f'Time-Out: {staff_name} at {time_out}')
        notif_message = f'Attendance recorded: Time-Out at {time_out}.'

    create_notification(staff_user_id, 'Attendance Recorded', notif_message, 'attendance')
    notify_role('admin', 'Staff Attendance', f'{staff_name} - {notif_message}', 'attendance', exclude_user_id=session.get('user_id'))

    return jsonify({'status': 'success', 'message': 'Attendance updated successfully!'}), 200


# Matches the late threshold the old (removed) client-side self-service
# clock-in used - staff no longer decide this themselves, but the business
# rule itself didn't change.
LATE_CUTOFF_HOUR, LATE_CUTOFF_MINUTE = 8, 15


@staff_api_bp.route('/api/staff-clock-in-request', methods=['POST'])
@role_required('staff')
def api_staff_clock_in_request():
    """Staff can't clock themselves in directly anymore, but they can ask -
    this records the real moment they showed up as a 'Pending' row; admin
    then confirms (/api/confirm-clock-in) or rejects
    (/api/reject-clock-in) it. The requested time_in is what's kept as the
    real clock-in time once confirmed, not whenever admin got to it."""
    staff_user_id = session.get('user_id')
    staff_name = current_staff_name()
    row = get_latest_attendance(staff_user_id)

    if is_on_duty_record(row):
        return jsonify({'status': 'error', 'message': 'You are already clocked in today.'}), 409
    if row and row['status'] == 'Pending':
        return jsonify({'status': 'error', 'message': 'Your clock-in request is already waiting for admin to confirm it.'}), 409

    now = datetime.now()
    time_in = now.strftime('%I:%M %p')
    _insert_attendance(staff_user_id, staff_name, now.strftime('%Y-%m-%d'), 'Pending', time_in)

    log_activity(session.get('user', staff_name), f'Requested clock-in at {time_in}')
    notify_role('admin', 'Clock-In Request', f'{staff_name} requested clock-in at {time_in}.', 'attendance')
    return jsonify({'status': 'success', 'message': 'Clock-in request sent - waiting for admin to confirm.'}), 200


@staff_api_bp.route('/api/clock-in-requests', methods=['GET'])
@role_required('admin')
def api_get_clock_in_requests():
    """Every staff member whose latest attendance row today is still
    'Pending' - the admin-facing approval queue."""
    pending = [
        {'staff_user_id': row['staff_user_id'], 'staff_name': row['staff_name'], 'time_in': row['time_in']}
        for row in get_latest_attendance_by_staff() if row['status'] == 'Pending'
    ]
    return jsonify({'status': 'success', 'data': pending}), 200


@staff_api_bp.route('/api/confirm-clock-in', methods=['POST'])
@role_required('admin')
def api_confirm_clock_in():
    data = request.get_json() or {}
    staff_user_id, staff_name = _staff_from_request(data)

    row = get_latest_attendance(staff_user_id) if staff_user_id else None
    if not row or row['status'] != 'Pending':
        return jsonify({'status': 'error', 'message': 'No pending clock-in request for that staff member.'}), 400

    time_in = row['time_in']
    try:
        requested_dt = datetime.strptime(time_in, '%I:%M %p')
        is_late = (requested_dt.hour, requested_dt.minute) > (LATE_CUTOFF_HOUR, LATE_CUTOFF_MINUTE)
    except (ValueError, TypeError):
        is_late = False
    new_status = 'Late' if is_late else 'Present'

    _insert_attendance(staff_user_id, staff_name, datetime.now().strftime('%Y-%m-%d'), new_status, time_in)

    log_activity(session.get('user', 'Admin'), f'Confirmed clock-in for {staff_name} at {time_in} ({new_status})')
    create_notification(
        staff_user_id, 'Clock-In Confirmed',
        f'Your clock-in at {time_in} was confirmed ({new_status}).', 'attendance',
    )
    return jsonify({'status': 'success', 'message': f'{staff_name} is now clocked in ({new_status}).'}), 200


@staff_api_bp.route('/api/reject-clock-in', methods=['POST'])
@role_required('admin')
def api_reject_clock_in():
    data = request.get_json() or {}
    staff_user_id, staff_name = _staff_from_request(data)

    row = get_latest_attendance(staff_user_id) if staff_user_id else None
    if not row or row['status'] != 'Pending':
        return jsonify({'status': 'error', 'message': 'No pending clock-in request for that staff member.'}), 400

    _insert_attendance(staff_user_id, staff_name, datetime.now().strftime('%Y-%m-%d'), 'Absent')

    log_activity(session.get('user', 'Admin'), f'Rejected clock-in request for {staff_name}')
    create_notification(
        staff_user_id, 'Clock-In Request Rejected',
        'Your clock-in request was not confirmed by admin. Please check with them.', 'attendance',
    )
    return jsonify({'status': 'success', 'message': f'Clock-in request for {staff_name} rejected.'}), 200


@staff_api_bp.route('/api/staff-status', methods=['GET'])
@role_required('admin', 'staff', 'customer')
def api_staff_status():
    """Backs the booking page's staff-availability banner. `active_staff`
    is how many are clocked in right now; `staff_available` answers for
    ?date=YYYY-MM-DD (default today) with the same rule
    /api/submit-booking enforces for customers (utils.is_date_staffed), so
    the banner and the booking outcome always agree."""
    date_str = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
    return jsonify({
        'status': 'success',
        'date': date_str,
        'active_staff': get_active_staff_count(),
        'staff_available': is_date_staffed(date_str),
    }), 200


@staff_api_bp.route('/api/export-attendance-csv', methods=['GET'])
@role_required('admin')
def api_export_attendance_csv():
    """Admin-only CSV export of attendance records, generated straight from
    staff_attendance - every column is a real column already stored by the
    system (staff_name, date_str, status, time_in, time_out, created_at),
    nothing invented. Optional start_date/end_date (YYYY-MM-DD, inclusive)
    narrow the export; omitted, every record on file is included. A UTF-8
    byte-order mark is prefixed so Excel (which otherwise guesses the
    system codepage) opens it correctly instead of mangling it."""
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    conn = get_db_connection()
    cursor = conn.cursor()
    query = ("SELECT COALESCE(u.fullname, u.username, a.staff_name) AS staff_name, a.date_str, a.status, "
             "a.time_in, a.time_out, a.created_at FROM staff_attendance a LEFT JOIN users u ON u.user_id = a.staff_user_id")
    clauses = []
    params = []
    if start_date:
        clauses.append("a.date_str >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("a.date_str <= ?")
        params.append(end_date)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY a.date_str DESC, a.attendance_id DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    output.write('﻿')  # UTF-8 BOM - keeps Excel/Google Sheets from mis-decoding the file
    writer = csv_module.writer(output)
    writer.writerow(['Staff Name', 'Date', 'Status', 'Time In', 'Time Out', 'Recorded At'])
    for row in rows:
        writer.writerow([
            row['staff_name'], row['date_str'], row['status'],
            row['time_in'], row['time_out'], row['created_at'],
        ])

    log_activity(session.get('user', 'Admin'), 'Exported attendance logs to CSV')
    filename = f"MD57_Attendance_Logs_{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(
        output.getvalue().encode('utf-8'),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


@staff_api_bp.route('/api/get-staff-attendance', methods=['GET'])
@role_required('admin', 'staff')
def api_get_staff_attendance():
    """Returns the latest attendance record per staff member for a given date
    (defaults to today). Single source of truth replacing localStorage caching."""
    date_str = request.args.get('date_str', datetime.now().strftime('%Y-%m-%d'))
    rows = [dict(row) for row in get_latest_attendance_by_staff(date_str)]
    return jsonify({'status': 'success', 'date_str': date_str, 'data': rows}), 200


# Forward-looking schedule, distinct from staff_attendance (which only
# ever records what already happened). Default window matches the
# "shift roster" framing - a week out, not the whole history.
ROSTER_DEFAULT_DAYS_AHEAD = 6


@staff_api_bp.route('/api/staff-shifts', methods=['GET'])
@role_required('admin', 'staff')
def api_get_staff_shifts():
    today = datetime.now().strftime('%Y-%m-%d')
    start_date = request.args.get('start_date', today)
    end_date = request.args.get(
        'end_date',
        (datetime.now() + timedelta(days=ROSTER_DEFAULT_DAYS_AHEAD)).strftime('%Y-%m-%d'),
    )
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT s.shift_id, s.staff_user_id, COALESCE(u.fullname, u.username, s.staff_name) AS staff_name,
               s.shift_date, s.shift_note, s.created_by
        FROM staff_shifts s
        LEFT JOIN users u ON u.user_id = s.staff_user_id
        WHERE s.shift_date BETWEEN ? AND ?
        ORDER BY s.shift_date ASC, staff_name ASC
        """,
        (start_date, end_date),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify({'status': 'success', 'data': rows}), 200


@staff_api_bp.route('/api/staff-shifts', methods=['POST'])
@role_required('admin')
def api_add_staff_shift():
    """Admin-only, same "must reference a real staff account" validation
    api_staff_attendance already uses. One row per staff/day - scheduling
    the same person for a date they're already scheduled just updates the
    note instead of erroring, so re-submitting the roster form is harmless.
    repeat_weeks (0-12) also schedules the same weekday for that many
    following weeks, so a regular schedule isn't re-entered day by day."""
    data = request.get_json() or {}
    shift_date = (data.get('shift_date') or '').strip()
    shift_note = (data.get('shift_note') or '').strip() or None
    try:
        repeat_weeks = min(max(int(data.get('repeat_weeks') or 0), 0), 12)
        first_day = datetime.strptime(shift_date, '%Y-%m-%d') if shift_date else None
    except ValueError:
        return jsonify({'status': 'error', 'message': 'Invalid date or repeat count.'}), 400

    if not shift_date or not (data.get('staff_user_id') or data.get('staff_name')):
        return jsonify({'status': 'error', 'message': 'Staff member and date are required.'}), 400

    staff_user_id, staff_name = _staff_from_request(data)
    if not staff_user_id:
        return jsonify({'status': 'error', 'message': 'Unknown staff member.'}), 400

    dates = [(first_day + timedelta(weeks=week)).strftime('%Y-%m-%d') for week in range(repeat_weeks + 1)]
    conn = get_db_connection()
    conn.executemany(
        """
        INSERT INTO staff_shifts (staff_user_id, staff_name, shift_date, shift_note, created_by)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(staff_name, shift_date) DO UPDATE SET shift_note = excluded.shift_note, created_by = excluded.created_by
        """,
        [(staff_user_id, staff_name, day, shift_note, session.get('user', 'Admin')) for day in dates],
    )
    conn.commit()
    conn.close()

    when = shift_date if not repeat_weeks else f'{shift_date} and the same weekday for {repeat_weeks} more week(s)'
    log_activity(session.get('user', 'Admin'), f'Scheduled {staff_name} for shift on {when}')
    return jsonify({'status': 'success', 'message': f'{staff_name} scheduled for {when}.', 'dates': dates}), 200


@staff_api_bp.route('/api/unstaffed-booking-days', methods=['GET'])
@role_required('admin')
def api_unstaffed_booking_days():
    """Upcoming days that already have bookings but no available staff on
    the roster (or, for today, nobody clocked in either) - what admin should
    schedule someone for before the no-show sweep has to move them."""
    conn = get_db_connection()
    rows = conn.execute(
        f"""
        SELECT booking_date, COUNT(*) AS bookings FROM reservations
        WHERE booking_date >= ? AND queue_status IN ({','.join('?' * len(ACTIVE_BOOKING_STATUSES))})
        GROUP BY booking_date ORDER BY booking_date
        """,
        (datetime.now().strftime('%Y-%m-%d'), *ACTIVE_BOOKING_STATUSES),
    ).fetchall()
    conn.close()
    days = [{'date': r['booking_date'], 'bookings': r['bookings']} for r in rows if not is_date_staffed(r['booking_date'])]
    return jsonify({'status': 'success', 'data': days}), 200


@staff_api_bp.route('/api/delete-staff-shift', methods=['POST'])
@role_required('admin')
def api_delete_staff_shift():
    data = request.get_json() or {}
    shift_date = (data.get('shift_date') or '').strip()

    if not shift_date or not (data.get('staff_user_id') or data.get('staff_name')):
        return jsonify({'status': 'error', 'message': 'Staff member and date are required.'}), 400

    staff_user_id, staff_name = _staff_from_request(data)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM staff_shifts WHERE staff_user_id = ? AND shift_date = ?",
        (staff_user_id, shift_date),
    )
    removed = cursor.rowcount
    conn.commit()
    conn.close()

    if removed == 0:
        return jsonify({'status': 'error', 'message': 'No matching shift found.'}), 404

    log_activity(session.get('user', 'Admin'), f'Removed {staff_name} from the roster for {shift_date}')
    return jsonify({'status': 'success', 'message': f'{staff_name} removed from {shift_date}.'}), 200


@staff_api_bp.route('/api/staff-unavailability', methods=['GET'])
@role_required('admin', 'staff')
def api_get_staff_unavailability():
    """Admin sees every staff member's marked-unavailable dates in the
    window (needs the full picture to schedule around it). A staff session
    sees only their own - a coworker's reason for being unavailable isn't
    anyone else's business, only admin's."""
    today = datetime.now().strftime('%Y-%m-%d')
    start_date = request.args.get('start_date', today)
    end_date = request.args.get(
        'end_date',
        (datetime.now() + timedelta(days=ROSTER_DEFAULT_DAYS_AHEAD)).strftime('%Y-%m-%d'),
    )
    conn = get_db_connection()
    cursor = conn.cursor()
    if session.get('role') == 'admin':
        cursor.execute(
            """
            SELECT n.unavailability_id, n.staff_user_id, COALESCE(u.fullname, u.username, n.staff_name) AS staff_name,
                   n.unavailable_date, n.reason
            FROM staff_unavailability n
            LEFT JOIN users u ON u.user_id = n.staff_user_id
            WHERE n.unavailable_date BETWEEN ? AND ?
            ORDER BY n.unavailable_date ASC, staff_name ASC
            """,
            (start_date, end_date),
        )
    else:
        cursor.execute(
            """
            SELECT unavailability_id, staff_user_id, staff_name, unavailable_date, reason
            FROM staff_unavailability
            WHERE unavailable_date BETWEEN ? AND ? AND staff_user_id = ?
            ORDER BY unavailable_date ASC
            """,
            (start_date, end_date, session.get('user_id')),
        )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify({'status': 'success', 'data': rows}), 200


@staff_api_bp.route('/api/staff-unavailability', methods=['POST'])
@role_required('staff')
def api_add_staff_unavailability():
    """Staff-only, and always for the session's own identity - the whole
    point is giving staff a real, first-party way to say "I can't work
    this day" that admin sees before building the roster, not a channel
    for anyone to speak for someone else. Mirrors the self-only shape of
    /api/staff-clock-in-request."""
    data = request.get_json() or {}
    unavailable_date = (data.get('unavailable_date') or '').strip()
    reason = (data.get('reason') or '').strip() or None
    if not unavailable_date:
        return jsonify({'status': 'error', 'message': 'A date is required.'}), 400

    staff_name = current_staff_name()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO staff_unavailability (staff_user_id, staff_name, unavailable_date, reason)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(staff_name, unavailable_date) DO UPDATE SET reason = excluded.reason
        """,
        (session.get('user_id'), staff_name, unavailable_date, reason),
    )
    conn.commit()
    conn.close()

    log_activity(session.get('user', staff_name), f'Marked unavailable on {unavailable_date}')
    return jsonify({'status': 'success', 'message': f'Marked unavailable on {unavailable_date}.'}), 200


@staff_api_bp.route('/api/delete-staff-unavailability', methods=['POST'])
@role_required('staff')
def api_delete_staff_unavailability():
    data = request.get_json() or {}
    unavailable_date = (data.get('unavailable_date') or '').strip()
    if not unavailable_date:
        return jsonify({'status': 'error', 'message': 'A date is required.'}), 400

    staff_name = current_staff_name()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM staff_unavailability WHERE staff_user_id = ? AND unavailable_date = ?",
        (session.get('user_id'), unavailable_date),
    )
    removed = cursor.rowcount
    conn.commit()
    conn.close()

    if removed == 0:
        return jsonify({'status': 'error', 'message': 'No matching entry found.'}), 404

    log_activity(session.get('user', staff_name), f'Cleared unavailability on {unavailable_date}')
    return jsonify({'status': 'success', 'message': f'Cleared for {unavailable_date}.'}), 200


@staff_api_bp.route('/api/my-notify-contact', methods=['GET'])
@role_required('staff')
def api_get_my_notify_contact():
    """Returns only the session's own opt-in contact info - kept off
    /api/get-staff-list entirely, since that endpoint is visible to every
    staff/admin session and this isn't something a coworker needs to see
    just to view the roster."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT notify_email FROM users WHERE user_id = ?",
        (session.get('user_id'),),
    )
    row = cursor.fetchone()
    conn.close()
    return jsonify({
        'status': 'success',
        'notify_email': row['notify_email'] if row else None,
    }), 200


@staff_api_bp.route('/api/update-notify-contact', methods=['POST'])
@role_required('staff')
def api_update_notify_contact():
    """Staff-only opt-in contact info for shift reminders (shift_reminder.py).
    Always the session's own account - there's no admin path to set this
    for someone else, since the whole point is that handing over an email
    is the staff member's own choice, not something imposed on them. Can
    be cleared by sending an empty string."""
    data = request.get_json() or {}
    email = (data.get('notify_email') or '').strip() or None

    if email and '@' not in email:
        return jsonify({'status': 'error', 'message': 'That email address doesn\'t look right.'}), 400

    user_id = session.get('user_id')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET notify_email = ? WHERE user_id = ?",
        (email, user_id),
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'message': 'Notification contact info updated.'}), 200


# /api/get-staff-bonuses and /api/staff-bonus (commission/bonus/payout
# tracking) were removed here per explicit request - staff.html's
# "Washers Directory & Task Tracking" table now tracks real completed/
# in-progress job counts instead, no payout figure anywhere on that page.
# The staff_bonuses table itself is left in the schema (historical bonus
# rows, if any exist in a live deployment, aren't destroyed by removing
# the routes that wrote to it) but nothing in this app reads or writes it
# anymore.


@staff_api_bp.route('/api/predict-ewt', methods=['POST'])
@role_required('admin', 'staff', 'customer')
def api_predict_ewt():
    data = request.get_json() or {}
    try:
        # active_staff is computed server-side from real attendance data,
        # not trusted from the client - the frontend used to hardcode a
        # guess here (queue_dashboard.html/booking.html both sent a fixed
        # "3, not tracked client-side"), which was fake in exactly the way
        # this whole prediction feature is supposed to avoid.
        # The service's real duration (by service_id, or minutes given
        # directly) and the current rainfall are looked up here too, so a
        # preview uses exactly what a real booking would.
        service_minutes = data.get('service_minutes')
        if data.get('service_id'):
            conn = get_db_connection()
            row = conn.execute("SELECT base_duration_minutes FROM services WHERE service_id = ?", (data.get('service_id'),)).fetchone()
            conn.close()
            service_minutes = row[0] if row else None
        ewt_result = ai_predictor.predict_ewt(
            int(data.get('queue_length', 1)),
            get_active_staff_count(),
            get_weather_reading()['rain_mm'],
            service_minutes,
        )
        return jsonify({'status': 'success', 'predicted_ewt_minutes': ewt_result}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400


@staff_api_bp.route('/api/staff-evaluation', methods=['POST'])
@role_required('admin')
def api_staff_evaluation():
    data = request.get_json() or {}
    ranked_staff = mcda_engine.evaluate_staff(data.get('staff_metrics', []))
    return jsonify({'status': 'success', 'ranked_productivity_report': ranked_staff}), 200


@staff_api_bp.route('/api/model-accuracy', methods=['GET'])
@role_required('admin')
def api_model_accuracy():
    """Real MAE/RMSE for the wait-time predictor, computed from actual
    bookings (see ai_retrain.py's compute_prediction_accuracy) - replaces
    the old manager_analytics.html cards that showed permanently frozen
    hardcoded numbers."""
    from ai_retrain import compute_prediction_accuracy
    return jsonify({
        'status': 'success',
        **compute_prediction_accuracy(),
        'model_trained_on': ai_predictor.trained_on,
        'model_real_samples': ai_predictor.last_retrain_sample_count,
    }), 200


@staff_api_bp.route('/api/analytics-metrics', methods=['GET'])
@role_required('admin')
def get_analytics_metrics():
    conn = get_db_connection()
    cursor = conn.cursor()

    # --- Service mix: non-cancelled bookings per published service
    # category (same categories/labels as /api/services) ---
    cursor.execute("""
        SELECT s.service_category, COUNT(*) AS cnt
        FROM reservations r
        JOIN services s ON r.service_id = s.service_id
        WHERE r.queue_status != 'cancelled'
        GROUP BY s.service_category
    """)
    bookings_by_category = {row['service_category']: row['cnt'] for row in cursor.fetchall()}
    service_labels = [label for _, label in SERVICE_CATEGORY_LABELS]
    service_shares = [bookings_by_category.get(key, 0) for key, _ in SERVICE_CATEGORY_LABELS]

    # --- Workforce chart: AI-recommended staff headcount per weekday,
    # cross-referenced against what's actually on the shift roster.
    # Replaces the old "Predicted vs Actual demand" line chart, which
    # compared two values on different scales (a per-occurrence average
    # vs. an all-time cumulative total) - it always looked like the AI
    # was wildly wrong, when the two numbers were never meant to be
    # compared directly. This is a real capacity-planning calculation
    # instead: workload -> recommended headcount, actionable against the
    # shift roster rather than just a chart to look at.
    demand_labels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    weekday_totals = [0] * 7          # total bookings ever made on that weekday
    weekday_date_sets = [set() for _ in range(7)]  # distinct calendar dates seen per weekday

    cursor.execute("SELECT booking_date FROM reservations WHERE booking_date IS NOT NULL AND booking_date != ''")
    for row in cursor.fetchall():
        try:
            d = datetime.strptime(row['booking_date'], '%Y-%m-%d')
            idx = d.weekday()  # Monday = 0 ... Sunday = 6
            weekday_totals[idx] += 1
            weekday_date_sets[idx].add(row['booking_date'])
        except (ValueError, TypeError):
            continue  # skip malformed dates rather than let one bad row break the chart

    # Expected bookings for "a typical Monday/Tuesday/etc.", and how many
    # distinct calendar dates that average is actually based on - a
    # weekday seen only once or twice gets flagged as low-confidence on
    # the frontend rather than presented as a firm recommendation.
    avg_bookings = [
        weekday_totals[i] / len(weekday_date_sets[i]) if weekday_date_sets[i] else 0
        for i in range(7)
    ]
    sample_days = [len(weekday_date_sets[i]) for i in range(7)]

    # Real empirical throughput: how many jobs did one present staff
    # member actually get through per shift, historically? Falls back to
    # a conservative fixed estimate (roughly one job per hour of the
    # 13-hour business day) only when there's no attendance history yet
    # to compute a real figure from.
    FALLBACK_JOBS_PER_STAFF_PER_DAY = 8
    cursor.execute("SELECT COUNT(*) as cnt FROM reservations WHERE queue_status IN ('ready', 'completed')")
    total_completed_jobs = cursor.fetchone()['cnt'] or 0
    cursor.execute(
        "SELECT COUNT(DISTINCT staff_user_id || '|' || date_str) as cnt FROM staff_attendance WHERE status IN ('Present', 'Late')"
    )
    total_staff_days = cursor.fetchone()['cnt'] or 0
    jobs_per_staff_per_day = (
        total_completed_jobs / total_staff_days if total_staff_days > 0 else FALLBACK_JOBS_PER_STAFF_PER_DAY
    )

    cursor.execute("SELECT COUNT(*) as cnt FROM users WHERE role = 'staff'")
    total_staff_on_roster = cursor.fetchone()['cnt'] or 0

    recommended_staff = [
        min(total_staff_on_roster, -(-round(avg_bookings[i]) // max(1, round(jobs_per_staff_per_day))))
        if avg_bookings[i] > 0 else 0
        for i in range(7)
    ]

    # Scheduled: the nearest upcoming shift-roster date for each weekday
    # (the roster is a rolling ~7-day window, so there's normally at
    # most one relevant future date per weekday to compare against).
    today_str = datetime.now().strftime('%Y-%m-%d')
    cursor.execute(
        "SELECT shift_date, COUNT(DISTINCT staff_user_id) as cnt FROM staff_shifts WHERE shift_date >= ? GROUP BY shift_date",
        (today_str,),
    )
    nearest_by_weekday = {}
    for row in cursor.fetchall():
        try:
            d = datetime.strptime(row['shift_date'], '%Y-%m-%d')
        except (ValueError, TypeError):
            continue
        idx = d.weekday()
        if idx not in nearest_by_weekday or row['shift_date'] < nearest_by_weekday[idx][0]:
            nearest_by_weekday[idx] = (row['shift_date'], row['cnt'])
    scheduled_staff = [nearest_by_weekday[i][1] if i in nearest_by_weekday else 0 for i in range(7)]

    # --- Peak hours chart: real bucketed counts from booking_time ---
    # MD57 is open 8 AM-9 PM (see is_within_business_hours) - the
    # bucket list must cover that whole window. It previously stopped
    # at 6 PM, so every booking from 7-9 PM had no bucket closer than
    # "6 PM" and got folded into it via the nearest-bucket snap below,
    # silently inflating that one bucket by 3 hours' worth of evening
    # bookings and skewing which hour looked "busiest".
    peak_labels = ['8 AM', '10 AM', '12 PM', '2 PM', '4 PM', '6 PM', '8 PM']
    bucket_hours = [8, 10, 12, 14, 16, 18, 20]  # 24h reference points for each label
    peak_counts = [0] * len(peak_labels)

    cursor.execute("SELECT booking_time FROM reservations WHERE booking_time IS NOT NULL AND booking_time != ''")
    for row in cursor.fetchall():
        bt = row['booking_time']
        parsed_hour = None
        for fmt in ('%I:%M %p', '%H:%M'):
            try:
                parsed_hour = datetime.strptime(bt.strip(), fmt).hour
                break
            except (ValueError, TypeError):
                continue
        if parsed_hour is None:
            continue
        # snap to the nearest bucket
        nearest_idx = min(range(len(bucket_hours)), key=lambda i: abs(bucket_hours[i] - parsed_hour))
        peak_counts[nearest_idx] += 1

    # --- Wait-Time chart: real average wait time (created_at ->
    # completed_at), bucketed by the hour the booking was *created* -
    # same hour buckets as Peak Hours, for visual/mental consistency
    # between the two tabs. Replaces the old per-service-category
    # actual-vs-predicted comparison: that answered "which service is
    # slower", this answers "how long has it historically taken
    # depending on what time you showed up", which the old view
    # couldn't speak to at all. sample_counts lets the frontend dim a
    # bucket with too few real data points, same low-confidence
    # treatment the demand chart already gives sparse weekdays.
    wait_labels = list(peak_labels)
    wait_bucket_totals = [0.0] * len(bucket_hours)
    wait_bucket_counts = [0] * len(bucket_hours)

    cursor.execute(
        """
        SELECT created_at, completed_at FROM reservations
        WHERE queue_status IN ('ready', 'completed')
          AND completed_at IS NOT NULL AND completed_at != ''
          AND created_at IS NOT NULL
        """
    )
    wait_skipped = 0
    for row in cursor.fetchall():
        diff = parse_duration_minutes(row['created_at'], row['completed_at'])
        if diff is None:
            wait_skipped += 1
            continue
        try:
            created_hour = datetime.strptime(row['created_at'], '%Y-%m-%d %H:%M:%S').hour
        except (ValueError, TypeError):
            wait_skipped += 1
            continue
        nearest_idx = min(range(len(bucket_hours)), key=lambda i: abs(bucket_hours[i] - created_hour))
        wait_bucket_totals[nearest_idx] += diff
        wait_bucket_counts[nearest_idx] += 1

    if wait_skipped:
        print(f"[WARNING] Skipped {wait_skipped} malformed/implausible row(s) "
              f"computing hourly wait-time analytics")

    wait_actual = [
        round(wait_bucket_totals[i] / wait_bucket_counts[i]) if wait_bucket_counts[i] else 0
        for i in range(len(bucket_hours))
    ]
    wait_sample_counts = wait_bucket_counts

    # --- Weather chart: real counts, only available for bookings made since this feature shipped ---
    weather_labels = ['Sunny', 'Cloudy', 'Rainy']
    weather_demand = [0, 0, 0]
    cursor.execute(
        "SELECT weather_condition, COUNT(*) as cnt FROM reservations WHERE weather_condition IS NOT NULL GROUP BY weather_condition"
    )
    for row in cursor.fetchall():
        if row['weather_condition'] in weather_labels:
            weather_demand[weather_labels.index(row['weather_condition'])] = row['cnt']

    # --- Historical trend & weather-vs-volume: both built from the
    # same 30-day window of real completed jobs, so one query covers
    # both - bookings/avg-wait-time per day (Historical Trend tab) and
    # bookings-with-dominant-weather-condition per day (Weather tab's
    # line chart, added alongside the existing live-conditions widget,
    # not replacing it). No seeded/mock values at any point, including
    # during development, for either - `has_data` lets the frontend
    # show an honest "not enough data yet" state instead of a
    # placeholder chart when the window is empty.
    #
    # Deliberately does NOT report revenue/sales figures - this system
    # has no POS/payment/transaction record anywhere (no discounts,
    # refunds, partial payments, or payment method ever captured), so a
    # peso figure here would only ever be service list-price x
    # completed-job-count, not real sales data. Displaying it under a
    # "Revenue" label would misrepresent what the system actually
    # tracks, and contradicts this project's own documented limitation
    # that POS/sales tracking is out of scope.
    TREND_WINDOW_DAYS = 30
    trend_start_day = (datetime.now() - timedelta(days=TREND_WINDOW_DAYS - 1)).strftime('%Y-%m-%d')
    cursor.execute(
        """
        SELECT substr(r.completed_at, 1, 10) AS day, r.created_at, r.completed_at,
               r.weather_condition
        FROM reservations r
        WHERE r.queue_status IN ('ready', 'completed')
          AND r.completed_at IS NOT NULL AND r.completed_at != ''
          AND substr(r.completed_at, 1, 10) >= ?
        """,
        (trend_start_day,),
    )
    by_day = {}
    for row in cursor.fetchall():
        entry = by_day.setdefault(row['day'], {'bookings': 0, 'durations': [], 'conditions': []})
        entry['bookings'] += 1
        diff = parse_duration_minutes(row['created_at'], row['completed_at'])
        if diff is not None:
            entry['durations'].append(diff)
        if row['weather_condition'] in weather_labels:
            entry['conditions'].append(row['weather_condition'])

    trend_labels, trend_bookings, trend_avg_wait = [], [], []
    weather_trend_labels, weather_trend_bookings, weather_trend_conditions = [], [], []
    for i in range(TREND_WINDOW_DAYS):
        day = (datetime.now() - timedelta(days=TREND_WINDOW_DAYS - 1 - i)).strftime('%Y-%m-%d')
        entry = by_day.get(day)

        trend_labels.append(day)
        trend_bookings.append(entry['bookings'] if entry else 0)
        trend_avg_wait.append(
            round(sum(entry['durations']) / len(entry['durations'])) if entry and entry['durations'] else 0
        )

        weather_trend_labels.append(day)
        weather_trend_bookings.append(entry['bookings'] if entry else 0)
        # Dominant (most frequent) condition among that day's bookings -
        # a day can technically see more than one if weather changed
        # mid-day, since it's captured live per booking, not once daily.
        dominant = None
        if entry and entry['conditions']:
            dominant = max(set(entry['conditions']), key=entry['conditions'].count)
        weather_trend_conditions.append(dominant)

    conn.close()

    return jsonify({
        "status": "success",
        "trend_chart": {
            "labels": trend_labels,
            "bookings": trend_bookings,
            "avg_wait_minutes": trend_avg_wait,
            "has_data": sum(trend_bookings) > 0,
            "window_days": TREND_WINDOW_DAYS
        },
        "weather_trend_chart": {
            "labels": weather_trend_labels,
            "bookings": weather_trend_bookings,
            "conditions": weather_trend_conditions,
            "has_data": sum(weather_trend_bookings) > 0,
            "window_days": TREND_WINDOW_DAYS
        },
        "demand_chart": {
            "labels": demand_labels,
            "recommended": recommended_staff,
            "scheduled": scheduled_staff,
            "sample_days": sample_days
        },
        "peak_chart": {
            "labels": peak_labels,
            "counts": peak_counts
        },
        "service_pie": {
            "labels": service_labels,
            "shares": service_shares
        },
        "wait_time_chart": {
            "labels": wait_labels,
            "actual": wait_actual,
            "sample_counts": wait_sample_counts
        },
        "weather_chart": {
            "labels": weather_labels,
            "demand": weather_demand
        }
    }), 200



@staff_api_bp.route('/api/weather-current', methods=['GET'])
@role_required('admin', 'staff', 'customer')
def api_weather_current():
    """Server-side proxy for manager_analytics.html's live weather widget.
    Replaces the old approach of embedding WEATHER_API_KEY directly in the
    rendered page and letting the browser call OpenWeatherMap itself -
    that left the key readable to anyone who viewed the page source. Now
    the key never leaves the server; the frontend just gets the handful of
    display fields it actually needs."""
    snapshot = get_weather_snapshot()
    if snapshot is None:
        return jsonify({'status': 'error', 'message': 'Weather data unavailable right now.'}), 200
    return jsonify({'status': 'success', **snapshot}), 200


# A day needs at least this many distinct dates on record for a weather
# bucket before its average is trusted - same "don't present a guess as a
# firm number" rule the demand chart already applies via its sample_days
# low-confidence styling.
MIN_WEATHER_DAYS_PER_BUCKET = 3


@staff_api_bp.route('/api/weather-outlook', methods=['GET'])
@role_required('admin', 'staff')
def api_weather_outlook():
    """Ties tomorrow's forecast to an actual staffing decision instead of
    displaying it for its own sake: fetches tomorrow's bucketed forecast
    (get_weather_forecast_tomorrow), then compares MD57's own historical
    average bookings/day on days real reservations were tagged 'Rainy'
    (weather_condition, captured live at booking time - see
    get_weather_reading and api_submit_booking) against days tagged
    'Sunny'/'Cloudy'. Grouped by the calendar day each reservation was
    actually created (created_at), not booking_date - booking_date is
    NULL for walk-ins and can be a future date for scheduled bookings,
    neither of which is "what the weather was when this row's
    weather_condition was captured".

    Returns has_enough_data=False (and a plain-language note instead of a
    percentage) when either bucket has fewer than MIN_WEATHER_DAYS_PER_BUCKET
    distinct days on record - same honesty rule as the rest of this
    dashboard: no fabricated stats from a handful of rows."""
    tomorrow_condition = get_weather_forecast_tomorrow()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT substr(created_at, 1, 10) AS day, weather_condition
        FROM reservations
        WHERE weather_condition IS NOT NULL
          AND created_at IS NOT NULL AND created_at != ''
        """
    )
    day_conditions = {}  # day -> {condition -> count}
    for row in cursor.fetchall():
        day_conditions.setdefault(row['day'], {})
        day_conditions[row['day']][row['weather_condition']] = day_conditions[row['day']].get(row['weather_condition'], 0) + 1
    conn.close()

    bucket_totals = {}  # condition -> total reservations tagged with it
    bucket_days = {}    # condition -> set of distinct days that saw it
    for day, conds in day_conditions.items():
        for cond, cnt in conds.items():
            bucket_totals[cond] = bucket_totals.get(cond, 0) + cnt
            bucket_days.setdefault(cond, set()).add(day)

    rainy_days = len(bucket_days.get('Rainy', set()))
    non_rainy_days = len(bucket_days.get('Sunny', set()) | bucket_days.get('Cloudy', set()))
    rainy_avg = (bucket_totals.get('Rainy', 0) / rainy_days) if rainy_days else None
    non_rainy_total = bucket_totals.get('Sunny', 0) + bucket_totals.get('Cloudy', 0)
    non_rainy_avg = (non_rainy_total / non_rainy_days) if non_rainy_days else None

    has_enough_data = (
        rainy_days >= MIN_WEATHER_DAYS_PER_BUCKET and non_rainy_days >= MIN_WEATHER_DAYS_PER_BUCKET
    )
    pct_diff = None
    if has_enough_data and non_rainy_avg:
        pct_diff = round(((non_rainy_avg - rainy_avg) / non_rainy_avg) * 100)

    if tomorrow_condition is None:
        message = 'Tomorrow\'s forecast is unavailable right now.'
    elif tomorrow_condition == 'Rainy':
        if has_enough_data and pct_diff is not None:
            if pct_diff > 0:
                message = f"Rain expected tomorrow — bookings typically run ~{pct_diff}% lower on rainy days at this shop."
            elif pct_diff < 0:
                message = f"Rain expected tomorrow — historically that hasn't slowed bookings here (~{abs(pct_diff)}% higher on rainy days)."
            else:
                message = "Rain expected tomorrow — historically that hasn't changed booking volume much at this shop."
        else:
            message = "Rain expected tomorrow. Not enough historical data yet to estimate the effect on booking volume here."
    else:
        message = f"{tomorrow_condition} conditions expected tomorrow — no rain-related slowdown anticipated."

    return jsonify({
        'status': 'success',
        'tomorrow_condition': tomorrow_condition,
        'message': message,
        'has_enough_data': has_enough_data,
        'rainy_avg_bookings_per_day': round(rainy_avg, 1) if rainy_avg is not None else None,
        'non_rainy_avg_bookings_per_day': round(non_rainy_avg, 1) if non_rainy_avg is not None else None,
        'rainy_days_sampled': rainy_days,
        'non_rainy_days_sampled': non_rainy_days,
    }), 200


@staff_api_bp.route('/api/send-email', methods=['POST'])
@role_required('admin')
def send_email_notification():
    """Notifies a customer their vehicle is ready, by email - looked up
    from the reservation itself rather than trusted from the client, same
    "read it from the record, not the request" pattern item 6's feedback
    fix established."""
    data = request.get_json() or {}

    reservation_id = data.get('reservation_id')
    plate_number = data.get('plate_number')
    service_type = data.get('service_type', 'Car Wash Service')
    custom_subject = data.get('subject')
    custom_message = data.get('message')

    if not reservation_id and not plate_number:
        return jsonify({'status': 'error', 'message': 'Which booking? reservation_id is required.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    if reservation_id:
        cursor.execute(
            "SELECT vehicle_plate_number, customer_name, customer_email FROM reservations WHERE reservation_id = ?",
            (reservation_id,),
        )
    else:
        cursor.execute(
            """
            SELECT vehicle_plate_number, customer_name, customer_email FROM reservations
            WHERE vehicle_plate_number = ?
            ORDER BY (queue_status = 'ready') DESC, reservation_id DESC
            LIMIT 1
            """,
            (plate_number,),
        )
    row = cursor.fetchone()
    conn.close()

    if not row:
        return jsonify({'status': 'error', 'message': 'No matching reservation found.'}), 404
    plate_number = row['vehicle_plate_number']

    customer_name = row['customer_name'] or 'Customer'
    recipient_email = row['customer_email']

    if not recipient_email:
        return jsonify({'status': 'error', 'message': 'Could not notify the customer (no email on file).'}), 400

    try:
        subject_to_use = custom_subject if custom_subject else 'MD57 Car Wash - Your Vehicle is Ready!'
        body_to_use = custom_message if custom_message else f"""Hello {customer_name}!

We are pleased to inform you that your vehicle with plate number {plate_number} (Service: {service_type}) is now complete and ready for pickup at MD57 Car Wash & Auto Detailing.

Thank you very much for your trust!
- MD57 Team"""
        send_branded_email(
            subject=subject_to_use,
            recipient=recipient_email,
            heading='Your Vehicle is Ready!',
            plain_text_body=body_to_use,
        )
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Could not notify the customer (email failed: {e}).'}), 400

    log_activity(session.get('user', 'System'), f'Notified customer via email for plate: {plate_number}')
    return jsonify({'status': 'success', 'message': 'Notification sent via email.', 'email_sent': True}), 200


def _chart_image_flowable(data_url, width):
    """Decodes a 'data:image/png;base64,...' string (or None) into a
    reportlab Image flowable sized to the given width with its aspect
    ratio preserved - returns None on anything missing or malformed rather
    than raising, so one bad/absent chart image never fails the whole
    export."""
    if not data_url or not isinstance(data_url, str) or 'base64,' not in data_url:
        return None
    try:
        raw = base64.b64decode(data_url.split('base64,', 1)[1])
        img_buf = io.BytesIO(raw)
        img_reader = Image(img_buf)
        aspect = img_reader.drawHeight / float(img_reader.drawWidth)
        img_reader.drawWidth = width
        img_reader.drawHeight = width * aspect
        return img_reader
    except Exception:
        return None


@staff_api_bp.route('/export-pdf', methods=['GET', 'POST'])
@role_required('admin')
def export_pdf():
    # Ranked staff list comes from the browser - it's the exact same
    # MCDA/SAW scores the manager is looking at on screen when they
    # click Export. A plain GET (e.g. someone opening the URL directly)
    # falls back to an empty rankings section instead of fake names.
    ranked_staff = []
    analytics_charts = {}
    chart_images = {}
    if request.method == 'POST':
        payload = request.get_json(silent=True) or {}
        ranked_staff = payload.get('ranked_productivity_report', []) or []
        # Same on-screen data the manager is already looking at (the
        # same dbData the chart tabs render from) - not a fresh query,
        # so the report matches whatever's on screen at export time.
        analytics_charts = payload.get('analytics_charts', {}) or {}
        # PNG data URLs the browser rendered off-screen with Chart.js
        # (see captureChartImage in manager_analytics.html) - the PDF
        # can't run Chart.js itself, so the image is built client-side
        # and shipped up alongside the raw numbers.
        chart_images = payload.get('analytics_chart_images', {}) or {}

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) as c FROM reservations")
    total_bookings = cursor.fetchone()['c']

    # 'ready' counts as done here too, same as every other "completed"
    # figure on this dashboard (get_analytics_metrics, /api/admin/metrics,
    # the KPI strip) - a plain 'completed' filter would silently
    # undercount against every other number this report sits next to.
    cursor.execute("SELECT COUNT(*) as c FROM reservations WHERE queue_status IN ('ready', 'completed')")
    completed_bookings = cursor.fetchone()['c']

    cursor.execute("SELECT COUNT(*) as c FROM reservations WHERE queue_status = 'cancelled'")
    cancelled_bookings = cursor.fetchone()['c']

    cursor.execute("SELECT AVG(rating) as avg_rating, COUNT(*) as c FROM feedbacks")
    feedback_row = cursor.fetchone()
    avg_rating = round(feedback_row['avg_rating'], 2) if feedback_row['avg_rating'] is not None else None
    feedback_count = feedback_row['c']

    # Real report period for the letterhead: earliest booking on record
    # through today - not a fixed/fake window.
    cursor.execute("SELECT MIN(created_at) as first_date FROM reservations")
    first_date_row = cursor.fetchone()
    first_date_raw = first_date_row['first_date'] if first_date_row else None

    conn.close()

    if first_date_raw:
        try:
            first_date_text = datetime.strptime(first_date_raw[:10], '%Y-%m-%d').strftime('%B %d, %Y')
        except ValueError:
            first_date_text = first_date_raw[:10]
        date_range_text = f"{first_date_text} &ndash; {datetime.now().strftime('%B %d, %Y')}"
    else:
        date_range_text = f"No bookings on record yet as of {datetime.now().strftime('%B %d, %Y')}"

    # --- Build the actual PDF ---
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter,
                             topMargin=0.6 * inch, bottomMargin=0.6 * inch)
    styles = getSampleStyleSheet()
    footer_style = ParagraphStyle('MD57Footer', parent=styles['Normal'], fontSize=8, textColor=colors.grey)
    # leading (line height) set explicitly on each - styles['Normal']'s
    # inherited leading (12pt) is smaller than the 16pt name style's own
    # font size, which made the title overlap the subtitle line below it
    # when this was first tried.
    letterhead_name_style = ParagraphStyle('MD57LetterheadName', parent=styles['Normal'],
                                            fontSize=16, leading=19, fontName='Helvetica-Bold',
                                            textColor=colors.HexColor('#0d1b30'), spaceAfter=2)
    letterhead_sub_style = ParagraphStyle('MD57LetterheadSub', parent=styles['Normal'],
                                           fontSize=9, leading=12, textColor=colors.grey, spaceAfter=2)
    letterhead_range_style = ParagraphStyle('MD57LetterheadRange', parent=styles['Normal'],
                                             fontSize=8, leading=11, textColor=colors.HexColor('#9c7a2e'))

    elements = []

    # --- Letterhead: shop logo + name + real report date range, not
    # just a plain title - what a distributable report actually needs. ---
    try:
        logo_flowable = Image(LOGO_PATH, width=0.55 * inch, height=0.55 * inch)
    except Exception:
        logo_flowable = Paragraph('', styles['Normal'])

    letterhead_text = [
        Paragraph("MD57 Car Wash &amp; Auto Detailing", letterhead_name_style),
        Paragraph("AI-Driven Workforce Demand &amp; MCDA Analytics Report", letterhead_sub_style),
        Paragraph(f"Report period: {date_range_text}", letterhead_range_style),
    ]
    letterhead_table = Table([[logo_flowable, letterhead_text]], colWidths=[0.75 * inch, 5.75 * inch])
    letterhead_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (0, 0), 0),
        ('LEFTPADDING', (1, 0), (1, 0), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(letterhead_table)
    elements.append(Spacer(1, 8))
    rule_table = Table([['']], colWidths=[6.5 * inch], rowHeights=[1])
    rule_table.setStyle(TableStyle([('LINEBELOW', (0, 0), (-1, -1), 1.5, colors.HexColor('#9c7a2e'))]))
    elements.append(rule_table)
    elements.append(Spacer(1, 12))

    elements.append(Paragraph(
        f"Generated: {datetime.now().strftime('%B %d, %Y %I:%M %p')} &bull; "
        f"By: {session.get('user', 'Manager')}", styles['Normal']))
    elements.append(Spacer(1, 16))

    elements.append(Paragraph("System Summary", styles['Heading2']))
    summary_data = [
        ['Total Bookings', str(total_bookings)],
        ['Completed Jobs', str(completed_bookings)],
        ['Cancelled Jobs', str(cancelled_bookings)],
        ['Customer Feedback Received', str(feedback_count)],
        ['Average Rating', f"{avg_rating} / 5.0" if avg_rating is not None else 'No ratings yet'],
    ]
    summary_table = Table(summary_data, colWidths=[3 * inch, 2 * inch])
    summary_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 14))

    elements.append(Paragraph("Staff Productivity Rankings (MCDA / SAW Evaluation)", styles['Heading2']))
    if ranked_staff:
        table_data = [['Rank', 'Name', 'Speed', 'Completion', 'Feedback', 'SAW Score']]
        for s in ranked_staff:
            table_data.append([
                str(s.get('rank', '')),
                str(s.get('name', '')),
                f"{s.get('service_speed', '')}/100",
                f"{s.get('completion', '')}%",
                f"{s.get('feedback', '')}/5.0",
                f"{float(s.get('saw_score', 0)):.4f}",
            ])
        staff_table = Table(table_data, colWidths=[0.5*inch, 1.6*inch, 0.9*inch, 1*inch, 0.9*inch, 1*inch])
        staff_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1e293b')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8fafc')]),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        elements.append(staff_table)
    else:
        elements.append(Paragraph(
            "No staff ranking data was available at export time. "
            "Open the Staff Evaluation tab, let it finish loading, then export again.",
            styles['Normal']))

    elements.append(Spacer(1, 14))

    # --- Analytics Charts: the same Peak Hours/Service Mix/Wait-Time/
    # Weather charts as images, laid out 2-up like the numbers-only
    # Key Analytics Insights bullets right below them - rendered
    # off-screen by the browser (Chart.js can't run server-side) and
    # shipped up as PNG data URLs. Skipped entirely if none came
    # through (e.g. an older client, or a plain GET), rather than
    # printing four "unavailable" placeholders. ---
    chart_heading_style = ParagraphStyle('MD57ChartHeading', parent=styles['Normal'],
                                          fontSize=10, fontName='Helvetica-Bold',
                                          textColor=colors.HexColor('#0d1b30'), spaceAfter=4)

    def _chart_cell(title, img_key):
        img = _chart_image_flowable(chart_images.get(img_key), 2.9 * inch)
        if img:
            return [Paragraph(title, chart_heading_style), img]
        return [Paragraph(title, chart_heading_style),
                Paragraph("Chart unavailable at export time.", styles['Normal'])]

    if any(chart_images.get(k) for k in ('peak_chart', 'service_pie', 'wait_time_chart', 'weather_chart')):
        elements.append(Paragraph("Analytics Charts", styles['Heading2']))
        chart_grid = Table([
            [_chart_cell("Peak Hours (Vehicles by Hour)", 'peak_chart'),
             _chart_cell("Service Mix (Customers per Service)", 'service_pie')],
            [_chart_cell("Wait-Time (Avg Minutes by Hour)", 'wait_time_chart'),
             _chart_cell("Weather Impact (Bookings by Condition)", 'weather_chart')],
        ], colWidths=[3.1 * inch, 3.1 * inch])
        chart_grid.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ]))
        elements.append(chart_grid)
        elements.append(Spacer(1, 14))

    # --- Key Analytics Insights: one plain-language line per chart tab
    # (Peak Hours, Service Mix, Wait-Time, Weather) derived from the
    # exact dbData the manager's screen is already rendering from - not
    # a fresh query, and not the raw per-hour/per-service tables
    # themselves (those stay on screen), just the single number from
    # each that actually matters for a report. Same "no fabricated
    # stats from thin data" rule as the rest of this dashboard: a chart
    # with no usable data is skipped, not padded with a fake line. ---
    insight_lines = []

    peak = analytics_charts.get('peak_chart') or {}
    peak_labels = peak.get('labels') or []
    peak_counts = peak.get('counts') or []
    if peak_labels and peak_counts and len(peak_labels) == len(peak_counts) and any((c or 0) > 0 for c in peak_counts):
        busiest_i = max(range(len(peak_counts)), key=lambda i: peak_counts[i])
        quietest_i = min(range(len(peak_counts)), key=lambda i: peak_counts[i])
        insight_lines.append(
            f"<b>Peak Hours:</b> busiest around {peak_labels[busiest_i]} ({peak_counts[busiest_i]} vehicles); "
            f"quietest around {peak_labels[quietest_i]} ({peak_counts[quietest_i]} vehicles)."
        )

    service = analytics_charts.get('service_pie') or {}
    service_labels = service.get('labels') or []
    # Despite the key name, these are raw booking counts per service
    # category, not pre-computed percentages (same numbers the Service
    # Mix bar chart's x-axis plots) - the % here is computed from them.
    service_shares = service.get('shares') or []
    service_total = sum(service_shares or [0])
    if service_labels and service_shares and len(service_labels) == len(service_shares) and service_total > 0:
        top_i = max(range(len(service_shares)), key=lambda i: service_shares[i])
        top_pct = round(service_shares[top_i] / service_total * 100)
        insight_lines.append(
            f"<b>Service Mix:</b> {service_labels[top_i]} is the most requested category, "
            f"making up {top_pct}% of bookings ({service_shares[top_i]} of {service_total})."
        )

    wait = analytics_charts.get('wait_time_chart') or {}
    wait_labels = wait.get('labels') or []
    wait_actual = wait.get('actual') or []
    wait_samples = wait.get('sample_counts') or []
    if wait_labels and wait_actual and len(wait_labels) == len(wait_actual) and any(wait_actual):
        # Same >=2-sample confidence bar the chart itself uses (see
        # renderMainChart's waitPointColors) - an hour with fewer real
        # completed jobs than that isn't a confident number to lead
        # the report with, so it's only used if every hour is that thin.
        confident_idx = [i for i in range(len(wait_actual)) if (wait_samples[i] if i < len(wait_samples) else 0) >= 2]
        pool = confident_idx or list(range(len(wait_actual)))
        if pool:
            longest_i = max(pool, key=lambda i: wait_actual[i])
            insight_lines.append(
                f"<b>Wait-Time:</b> the longest average wait is around {wait_labels[longest_i]} "
                f"at {wait_actual[longest_i]:.0f} minutes."
            )

    weather = analytics_charts.get('weather_chart') or {}
    weather_labels = weather.get('labels') or []
    # Despite the key name, this is a total booking count per weather
    # condition tagged at booking time, not a per-day average - phrased
    # accordingly below (a true daily-average comparison already lives
    # in /api/weather-outlook, gated behind its own
    # MIN_WEATHER_DAYS_PER_BUCKET confidence check).
    weather_demand = weather.get('demand') or []
    weather_total = sum(weather_demand or [0])
    if weather_labels and weather_demand and len(weather_labels) == len(weather_demand) and weather_total > 0:
        top_i = max(range(len(weather_demand)), key=lambda i: weather_demand[i])
        top_pct = round(weather_demand[top_i] / weather_total * 100)
        insight_lines.append(
            f"<b>Weather Impact:</b> most bookings on record ({top_pct}%) were made under "
            f"{weather_labels[top_i]} conditions."
        )

    elements.append(Paragraph("Key Analytics Insights", styles['Heading2']))
    if insight_lines:
        elements.append(ListFlowable(
            [ListItem(Paragraph(line, styles['Normal']), spaceAfter=4) for line in insight_lines],
            bulletType='bullet', start='•', leftIndent=14,
        ))
    else:
        elements.append(Paragraph(
            "Not enough chart data was available at export time to summarize Peak Hours, "
            "Service Mix, Wait-Time, or Weather trends.",
            styles['Normal']))
    elements.append(Spacer(1, 14))

    # --- Written summary: template/rules over the real numbers already
    # computed above (total_bookings, completed_bookings, avg_rating,
    # ranked_staff) - not a live LLM call, which would add a new
    # external dependency and failure mode for an internal report a
    # manager is already reading directly. Every clause below is a
    # plain if/else over data this function already fetched. ---
    elements.append(Paragraph("Summary", styles['Heading2']))
    summary_sentences = []

    if total_bookings:
        completion_rate = round((completed_bookings / total_bookings) * 100)
        summary_sentences.append(
            f"Of {total_bookings} total booking(s) on record, {completed_bookings} were completed "
            f"({completion_rate}% completion rate) and {cancelled_bookings} were cancelled."
        )
    else:
        summary_sentences.append("No bookings are on record yet for this period.")

    if avg_rating is not None:
        summary_sentences.append(
            f"Customers rated the shop {avg_rating}/5.0 on average across {feedback_count} review(s)."
        )
    else:
        summary_sentences.append("No customer feedback has been recorded yet.")

    if ranked_staff:
        top = ranked_staff[0]

        def _to_float(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        avg_completion = sum(_to_float(s.get('completion')) for s in ranked_staff) / len(ranked_staff)
        top_speed = _to_float(top.get('service_speed'))
        top_completion = _to_float(top.get('completion'))
        top_feedback = _to_float(top.get('feedback'))
        max_speed = max(_to_float(s.get('service_speed')) for s in ranked_staff)
        max_completion = max(_to_float(s.get('completion')) for s in ranked_staff)
        max_feedback = max(_to_float(s.get('feedback')) for s in ranked_staff)

        # Which metric actually explains the #1 rank - the first of
        # Speed/Completion/Feedback (in that MCDA weight order) where
        # the top performer ties or leads the whole ranked field.
        if len(ranked_staff) > 1 and top_speed >= max_speed:
            driver_text = ", driven by leading Speed"
        elif len(ranked_staff) > 1 and top_completion >= max_completion:
            driver_text = ", driven by leading Completion"
        elif len(ranked_staff) > 1 and top_feedback >= max_feedback:
            driver_text = ", driven by leading Feedback"
        else:
            driver_text = ""

        summary_sentences.append(
            f"{top.get('name', 'The top-ranked staff member')} ranked #1 with a SAW score of "
            f"{_to_float(top.get('saw_score')):.2f}{driver_text}; "
            f"average Completion across {len(ranked_staff)} staff member(s) was {avg_completion:.0f}%."
        )
    else:
        summary_sentences.append("No staff ranking data was available at export time.")

    elements.append(Paragraph(' '.join(summary_sentences), styles['Normal']))
    elements.append(Spacer(1, 20))

    elements.append(Paragraph(
        "This is an official system-generated report for MD57 management.",
        footer_style))

    doc.build(elements)
    buffer.seek(0)

    log_activity(session.get('user', 'Manager'), 'Exported system analytics report to PDF')
    return send_file(
        buffer,
        as_attachment=True,
        download_name='MD57_Manager_Analytics_Report.pdf',
        mimetype='application/pdf',
    )
