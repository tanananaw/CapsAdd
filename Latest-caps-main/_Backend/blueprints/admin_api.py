import os
import re
from datetime import datetime

from flask import Blueprint, jsonify, request, session
from werkzeug.security import generate_password_hash

from config import ACTIVITY_LOG_FILE
from database import get_db_connection
from utils import role_required, log_activity, get_active_staff_count, parse_duration_minutes

admin_api_bp = Blueprint('admin_api', __name__)

# Matches lines written by utils.log_activity(): "[2026-09-11 14:32:00]
# User: manager - Action: Logged in as admin"
_LOG_LINE_RE = re.compile(r'^\[(?P<timestamp>[^\]]+)\] User: (?P<username>.*?) - Action: (?P<action>.*)$')


@admin_api_bp.route('/api/admin/create-user', methods=['POST'])
@role_required('admin')
def api_create_user():
    data = request.get_json() if request.is_json else request.form
    username = (data.get('username') or '').strip()
    password = data.get('password')
    role = (data.get('role') or 'staff').strip().lower()
    fullname = (data.get('fullname') or username).strip()

    if not username or not password:
        return jsonify({'status': 'error', 'message': 'Username and password are required!'}), 400

    if len(password) < 6:
        return jsonify({'status': 'error', 'message': 'Password must be at least 6 characters.'}), 400

    if role not in ('admin', 'staff', 'customer'):
        return jsonify({'status': 'error', 'message': 'Invalid role. Must be admin, staff, or customer.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
    existing = cursor.fetchone()
    if existing:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Username already exists in the system!'}), 400

    # Staff are picked by display name in the admin's forms and shown by it
    # everywhere - two staff with the same name would be indistinguishable.
    if role == 'staff':
        cursor.execute("SELECT 1 FROM users WHERE role = 'staff' AND COALESCE(fullname, username) = ?", (fullname,))
        if cursor.fetchone():
            conn.close()
            return jsonify({'status': 'error', 'message': f'A staff account named "{fullname}" already exists - use a different full name.'}), 400

    password_hash = generate_password_hash(password)
    cursor.execute(
        'INSERT INTO users (username, password_hash, role, fullname) VALUES (?, ?, ?, ?)',
        (username, password_hash, role, fullname)
    )
    conn.commit()
    conn.close()

    log_activity(session.get('user', 'Admin'), f'Created new user account: {username} ({role})')
    return jsonify({'status': 'success', 'message': f'Successfully created account for {username}!'}), 200



@admin_api_bp.route('/api/admin/reset-user-password', methods=['POST'])
@role_required('admin')
def api_admin_reset_user_password():
    """Complements the self-service /forgot-password email flow (auth.py)
    for cases where an admin needs to reset an account directly - email
    delivery isn't configured/working, or the customer asks in person."""
    data = request.get_json() if request.is_json else request.form
    username = (data.get('username') or '').strip()
    new_password = data.get('new_password')

    if not username or not new_password:
        return jsonify({'status': 'error', 'message': 'Username and new password are required!'}), 400
    if len(new_password) < 6:
        return jsonify({'status': 'error', 'message': 'Password must be at least 6 characters.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
    user = cursor.fetchone()

    if not user:
        conn.close()
        return jsonify({'status': 'error', 'message': 'No account found with that username.'}), 404

    if user['auth_provider'] == 'google':
        conn.close()
        return jsonify({'status': 'error', 'message': 'This account uses Google Sign-In and has no password to reset.'}), 400

    new_hash = generate_password_hash(new_password)
    cursor.execute('UPDATE users SET password_hash = ? WHERE user_id = ?', (new_hash, user['user_id']))
    # Consistent with what a successful self-service reset does - an
    # outstanding emailed link for this account shouldn't remain valid
    # after an admin has already reset the password directly.
    cursor.execute('UPDATE password_resets SET used = 1 WHERE user_id = ?', (user['user_id'],))
    conn.commit()
    conn.close()

    log_activity(session.get('user', 'Admin'), f'Reset password for user: {username}')
    return jsonify({'status': 'success', 'message': f'Password reset successfully for {username}.'}), 200


@admin_api_bp.route('/api/admin/metrics', methods=['GET'])
@role_required('admin', 'staff')
def api_admin_metrics():
    """Shared source for the "lead with real KPIs" stat strips on both
    admin-dashboard.html and homepage.html (admin/staff view) - one set of
    real numbers rather than each page computing its own. Opened up to
    staff (previously admin-only) since homepage.html is the first page
    staff land on too and active-staff/queue-length are operationally
    useful to them, not just managers; nothing returned here is more
    sensitive than what /api/get-queue already exposes to staff."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM reservations WHERE queue_status IN ('waiting', 'in_progress')")
    queue_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM reservations WHERE queue_status IN ('ready', 'completed')")
    ready_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM reservations WHERE queue_status = 'cancelled'")
    cancelled_count = cursor.fetchone()[0]

    # Real "today" numbers a shop owner actually checks first, computed
    # in Python-local time (not SQLite's UTC date('now')) for the same
    # reason created_at is set explicitly elsewhere in this app - see
    # api_submit_booking's created_at comment.
    today_str = datetime.now().strftime('%Y-%m-%d')

    cursor.execute(
        """
        SELECT created_at, completed_at FROM reservations
        WHERE queue_status IN ('ready', 'completed')
          AND completed_at IS NOT NULL AND substr(completed_at, 1, 10) = ?
        """,
        (today_str,),
    )
    completed_today_rows = cursor.fetchall()
    completed_today = len(completed_today_rows)
    today_durations = [
        d for d in (parse_duration_minutes(row['created_at'], row['completed_at']) for row in completed_today_rows)
        if d is not None
    ]
    avg_wait_today_minutes = round(sum(today_durations) / len(today_durations)) if today_durations else None

    active_staff = get_active_staff_count()

    conn.close()
    return jsonify({
        'status': 'success',
        'queue_count': queue_count,
        'ready_count': ready_count,
        'cancelled_count': cancelled_count,
        'completed_today': completed_today,
        'avg_wait_today_minutes': avg_wait_today_minutes,
        'active_staff': active_staff,
    }), 200


@admin_api_bp.route('/api/get-audit-logs', methods=['GET'])
@role_required('admin')
def api_get_audit_logs():
    """Serves the real server-side activity log (utils.log_activity writes
    every login, booking, cancellation, feedback submission, staff time-
    clock/bonus action, etc. to this file) - replaces the old client-only
    localStorage "Audit Logs" in staff.html, which only ever saw two kinds
    of event and nothing anyone else did. No delete/clear endpoint exists
    for this on purpose: an audit trail that the people being audited can
    erase isn't much of one."""
    limit = min(max(int(request.args.get('limit', 100)), 1), 500)
    entries = []
    if os.path.exists(ACTIVITY_LOG_FILE):
        with open(ACTIVITY_LOG_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        # Newest first, capped to `limit` - the file itself is never
        # rotated or truncated here, this just avoids shipping the
        # whole thing over the wire on every modal open.
        for line in reversed(lines[-limit:]):
            line = line.rstrip('\n')
            match = _LOG_LINE_RE.match(line)
            if match:
                entries.append(match.groupdict())
            elif line:
                entries.append({'timestamp': '', 'username': '', 'action': line})

    return jsonify({'status': 'success', 'data': entries}), 200


# --- SERVICE PRICING MANAGEMENT (admin-only) ---
# Backs a dedicated admin page for editing the real service catalog /api/services
# reads from - the whole point of storing pricing in the database instead of
# hardcoded frontend numbers is that an admin can update it here without
# touching any template or code.

_VALID_BAYS = ('Bay #1', 'Bay #2', 'Bay #3')
_VALID_CATEGORIES = ('general', 'detailing', 'motorcycle')


@admin_api_bp.route('/api/admin/services-all', methods=['GET'])
@role_required('admin')
def api_admin_get_all_services():
    """Every service including retired/inactive ones (unlike the
    customer/staff-facing /api/services, which only ever returns active
    ones) - an admin managing the catalog needs to see and potentially
    reactivate a retired service, not just the ones currently bookable."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT service_id, service_name, service_category, default_bay,
               base_duration_minutes, is_active
        FROM services
        ORDER BY is_active DESC, service_category, service_id
        """
    )
    service_rows = cursor.fetchall()

    cursor.execute("SELECT service_id, vehicle_type, price FROM service_prices")
    prices_by_service = {}
    for row in cursor.fetchall():
        prices_by_service.setdefault(row['service_id'], {})[row['vehicle_type']] = row['price']

    cursor.execute("SELECT type_name FROM vehicle_types ORDER BY type_id")
    vehicle_types = [row['type_name'] for row in cursor.fetchall()]
    conn.close()

    data = []
    for row in service_rows:
        entry = dict(row)
        entry['prices'] = prices_by_service.get(row['service_id'], {})
        data.append(entry)

    return jsonify({'status': 'success', 'data': data, 'vehicle_types': vehicle_types}), 200


@admin_api_bp.route('/api/admin/update-service-price', methods=['POST'])
@role_required('admin')
def api_admin_update_service_price():
    """Updates (or creates) one service's price for one vehicle size -
    editing a single cell in the pricing management table, not the whole
    service at once, so a typo in one size doesn't risk resubmitting every
    other size's price along with it."""
    data = request.get_json() or {}
    service_id = data.get('service_id')
    vehicle_type = (data.get('vehicle_type') or '').strip()
    price = data.get('price')

    if not service_id or not vehicle_type:
        return jsonify({'status': 'error', 'message': 'service_id and vehicle_type are required.'}), 400
    try:
        price = float(price)
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'Price must be a number.'}), 400
    if price < 0:
        return jsonify({'status': 'error', 'message': 'Price cannot be negative.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT service_name FROM services WHERE service_id = ?", (service_id,))
    service_row = cursor.fetchone()
    if not service_row:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Unknown service.'}), 404
    cursor.execute("SELECT 1 FROM vehicle_types WHERE type_name = ?", (vehicle_type,))
    if not cursor.fetchone():
        conn.close()
        return jsonify({'status': 'error', 'message': f"Unknown vehicle size '{vehicle_type}'."}), 400

    cursor.execute(
        """
        INSERT INTO service_prices (service_id, vehicle_type, price) VALUES (?, ?, ?)
        ON CONFLICT(service_id, vehicle_type) DO UPDATE SET price = excluded.price
        """,
        (service_id, vehicle_type, price),
    )
    conn.commit()
    conn.close()

    log_activity(session.get('user', 'Admin'), f"Updated price: {service_row['service_name']} ({vehicle_type}) -> PHP {price}")
    return jsonify({'status': 'success', 'message': 'Price updated.'}), 200


@admin_api_bp.route('/api/admin/toggle-service-active', methods=['POST'])
@role_required('admin')
def api_admin_toggle_service_active():
    """Retires or reactivates a service from new bookings - never deletes
    one, since past reservations/feedback may still reference it by
    foreign key (see database.py's migration comment on the 3 original
    services for exactly this scenario)."""
    data = request.get_json() or {}
    service_id = data.get('service_id')
    is_active = data.get('is_active')

    if not service_id or is_active is None:
        return jsonify({'status': 'error', 'message': 'service_id and is_active are required.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT service_name FROM services WHERE service_id = ?", (service_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Unknown service.'}), 404

    cursor.execute("UPDATE services SET is_active = ? WHERE service_id = ?", (1 if is_active else 0, service_id))
    conn.commit()
    conn.close()

    log_activity(session.get('user', 'Admin'), f"{'Activated' if is_active else 'Deactivated'} service: {row['service_name']}")
    return jsonify({'status': 'success', 'message': 'Service updated.'}), 200


@admin_api_bp.route('/api/admin/add-service', methods=['POST'])
@role_required('admin')
def api_admin_add_service():
    """Adds a brand-new service to the catalog with a starting price for
    every vehicle size - the same shape /api/services and the booking form
    already expect, so a newly-added service is immediately bookable with
    no other change needed anywhere."""
    data = request.get_json() or {}
    service_name = (data.get('service_name') or '').strip()
    service_category = (data.get('service_category') or '').strip()
    default_bay = (data.get('default_bay') or '').strip()
    base_duration_minutes = data.get('base_duration_minutes')
    prices = data.get('prices') or {}

    if not service_name:
        return jsonify({'status': 'error', 'message': 'Service name is required.'}), 400
    if service_category not in _VALID_CATEGORIES:
        return jsonify({'status': 'error', 'message': f"service_category must be one of: {', '.join(_VALID_CATEGORIES)}."}), 400
    if default_bay not in _VALID_BAYS:
        return jsonify({'status': 'error', 'message': f"default_bay must be one of: {', '.join(_VALID_BAYS)}."}), 400
    try:
        base_duration_minutes = int(base_duration_minutes)
        if base_duration_minutes <= 0:
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'base_duration_minutes must be a positive whole number.'}), 400
    if not prices or not isinstance(prices, dict):
        return jsonify({'status': 'error', 'message': 'At least one vehicle-size price is required.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT type_name FROM vehicle_types")
    valid_sizes = {row['type_name'] for row in cursor.fetchall()}
    clean_prices = {}
    for size, price in prices.items():
        if size not in valid_sizes:
            conn.close()
            return jsonify({'status': 'error', 'message': f"Unknown vehicle size '{size}'."}), 400
        try:
            clean_prices[size] = float(price)
        except (TypeError, ValueError):
            conn.close()
            return jsonify({'status': 'error', 'message': f"Price for '{size}' must be a number."}), 400

    cursor.execute("SELECT 1 FROM services WHERE service_name = ?", (service_name,))
    if cursor.fetchone():
        conn.close()
        return jsonify({'status': 'error', 'message': 'A service with that name already exists.'}), 400

    first_price = next(iter(clean_prices.values()))
    cursor.execute(
        """
        INSERT INTO services (service_name, service_type, base_duration_minutes, price, is_active, service_category, default_bay)
        VALUES (?, ?, ?, ?, 1, ?, ?)
        """,
        (service_name, service_category, base_duration_minutes, first_price, service_category, default_bay),
    )
    service_id = cursor.lastrowid
    for size, price in clean_prices.items():
        cursor.execute(
            "INSERT INTO service_prices (service_id, vehicle_type, price) VALUES (?, ?, ?)",
            (service_id, size, price),
        )
    conn.commit()
    conn.close()

    log_activity(session.get('user', 'Admin'), f"Added new service: {service_name} ({service_category}, {default_bay})")
    return jsonify({'status': 'success', 'message': f'{service_name} added to the catalog.', 'service_id': service_id}), 200
