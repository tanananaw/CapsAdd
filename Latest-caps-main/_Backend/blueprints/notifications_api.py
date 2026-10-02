from flask import Blueprint, Response, jsonify, request, session

from database import get_db_connection
from live_updates import stream
from utils import role_required

notifications_api_bp = Blueprint('notifications_api', __name__)


@notifications_api_bp.route('/api/live', methods=['GET'])
@role_required('admin', 'staff', 'customer')
def api_live_updates():
    """Server-Sent Events stream for this session's open page (see
    live_updates.py) - replaces the pages' fast polling."""
    return Response(
        stream(session.get('user_id')),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@notifications_api_bp.route('/api/notifications', methods=['GET'])
@role_required('admin', 'staff', 'customer')
def api_get_notifications():
    """Every account (customer/staff/admin) gets its own private notification
    feed, scoped entirely by the session's own user_id - there is no way for
    one customer to see another's notifications, or for staff to see
    admin-only ones, since a notification is only ever inserted for one
    specific user_id (see utils.create_notification/notify_role) and this
    query never reads anyone else's rows."""
    user_id = session.get('user_id')
    limit = min(max(int(request.args.get('limit', 30)), 1), 100)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT notification_id, title, message, category, related_reservation_id, is_read, created_at
        FROM notifications
        WHERE user_id = ?
        ORDER BY notification_id DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE user_id = ? AND is_read = 0",
        (user_id,),
    )
    unread_count = cursor.fetchone()['c']
    conn.close()
    return jsonify({'status': 'success', 'data': rows, 'unread_count': unread_count}), 200


@notifications_api_bp.route('/api/notifications/mark-read', methods=['POST'])
@role_required('admin', 'staff', 'customer')
def api_mark_notification_read():
    """Ownership is enforced directly in the UPDATE's WHERE clause, not
    checked separately beforehand - a notification_id belonging to a
    different account simply matches zero rows here, the same
    read-it-from-the-record pattern the rest of this app uses rather than
    trusting a client-supplied owner."""
    data = request.get_json() or {}
    notification_id = data.get('notification_id')
    if not notification_id:
        return jsonify({'status': 'error', 'message': 'notification_id is required.'}), 400

    user_id = session.get('user_id')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE notifications SET is_read = 1 WHERE notification_id = ? AND user_id = ?",
        (notification_id, user_id),
    )
    updated = cursor.rowcount
    conn.commit()
    conn.close()

    if updated == 0:
        return jsonify({'status': 'error', 'message': 'Notification not found.'}), 404
    return jsonify({'status': 'success'}), 200


@notifications_api_bp.route('/api/notifications/mark-all-read', methods=['POST'])
@role_required('admin', 'staff', 'customer')
def api_mark_all_notifications_read():
    user_id = session.get('user_id')
    conn = get_db_connection()
    conn.execute(
        "UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0",
        (user_id,),
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'}), 200
