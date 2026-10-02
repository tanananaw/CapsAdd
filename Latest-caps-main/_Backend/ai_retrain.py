from datetime import datetime, timedelta

from database import get_db_connection
from extensions import ai_predictor
from utils import parse_duration_minutes, start_periodic_worker

# Retraining doesn't need to react quickly the way the no-show sweep does -
# real completed bookings accumulate slowly, so checking every 6 hours is
# plenty. The first cycle runs immediately at startup (see
# start_ai_retrain_scheduler) in case enough real data already exists.
RETRAIN_INTERVAL_SECONDS = 6 * 60 * 60
WEEKLY_TREND_WEEKS = 8


def _fetch_real_training_rows():
    """Pulls completed reservations that have a full operational snapshot
    (queue_length_snapshot/active_staff_snapshot, captured at booking time
    by api_submit_booking) and an honest actual-wait duration
    (completed_at - created_at, both explicit local timestamps). Rows
    booked before this feature shipped have NULL snapshots and are
    excluded - there's no way to reconstruct what the queue/staff looked
    like for them after the fact.

    Rainfall comes from the booking's stored precipitation_mm; bookings
    made before it was recorded (NULL) use 0.0 rather than an invented
    value."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT r.queue_length_snapshot, r.active_staff_snapshot, r.precipitation_mm,
               r.created_at, r.completed_at, s.base_duration_minutes
        FROM reservations r
        JOIN services s ON r.service_id = s.service_id
        WHERE r.queue_status IN ('ready', 'completed')
          AND r.queue_length_snapshot IS NOT NULL
          AND r.active_staff_snapshot IS NOT NULL
          AND r.completed_at IS NOT NULL AND r.completed_at != ''
          AND r.created_at IS NOT NULL
    """)
    rows = cursor.fetchall()
    conn.close()

    training_rows = []
    for row in rows:
        actual_wait = parse_duration_minutes(row['created_at'], row['completed_at'])
        if actual_wait is None:
            continue

        training_rows.append({
            'queue_length': row['queue_length_snapshot'],
            'active_staff': row['active_staff_snapshot'],
            'precipitation': row['precipitation_mm'] or 0.0,
            'service_base_time': row['base_duration_minutes'],
            'actual_wait_time': actual_wait,
        })
    return training_rows


def compute_prediction_accuracy():
    """Real Mean Absolute Error / Root Mean Squared Error for the wait-time
    model, computed from actual bookings - not the old manager_analytics.html
    cards, which showed permanently frozen hardcoded numbers with no
    computation behind them at all.

    Compares each booking's predicted_wait_minutes (captured by
    api_submit_booking at the moment it was created, using the exact
    snapshot inputs then available) against what actually happened
    (completed_at - created_at). Only bookings with a real prediction and a
    plausible actual duration count - older bookings from before
    predicted_wait_minutes existed are NULL and excluded, same as
    _fetch_real_training_rows() above.

    Returns {mae_minutes, rmse_minutes, sample_count, weekly}; the two error
    figures are None when sample_count is 0 (nothing to average yet) rather
    than a misleading 0.0. weekly is the MAE per week (Monday start, by
    completion date) for the last WEEKLY_TREND_WEEKS weeks that have data -
    whether the model is actually getting better over time."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT predicted_wait_minutes, created_at, completed_at
        FROM reservations
        WHERE queue_status IN ('ready', 'completed')
          AND predicted_wait_minutes IS NOT NULL
          AND completed_at IS NOT NULL AND completed_at != ''
          AND created_at IS NOT NULL
    """)
    rows = cursor.fetchall()
    conn.close()

    errors = []
    errors_by_week = {}
    for row in rows:
        actual_wait = parse_duration_minutes(row['created_at'], row['completed_at'])
        if actual_wait is None:
            continue

        error = actual_wait - row['predicted_wait_minutes']
        errors.append(error)
        completed_day = datetime.strptime(row['completed_at'][:10], '%Y-%m-%d')
        week_start = (completed_day - timedelta(days=completed_day.weekday())).strftime('%Y-%m-%d')
        errors_by_week.setdefault(week_start, []).append(abs(error))

    if not errors:
        return {'mae_minutes': None, 'rmse_minutes': None, 'sample_count': 0, 'weekly': []}

    mae = sum(abs(e) for e in errors) / len(errors)
    rmse = (sum(e * e for e in errors) / len(errors)) ** 0.5
    weekly = [
        {'week_start': week, 'mae_minutes': round(sum(errs) / len(errs), 2), 'sample_count': len(errs)}
        for week, errs in sorted(errors_by_week.items())[-WEEKLY_TREND_WEEKS:]
    ]
    return {'mae_minutes': round(mae, 2), 'rmse_minutes': round(rmse, 2), 'sample_count': len(errors), 'weekly': weekly}


def run_retrain_cycle():
    """One retraining attempt: pull real data, hand it to the predictor.
    Safe to call any time - retrain_with_real_data() no-ops (returns False)
    below the minimum real-sample threshold."""
    rows = _fetch_real_training_rows()
    return ai_predictor.retrain_with_real_data(rows)


def start_ai_retrain_scheduler(app):
    start_periodic_worker(app, run_retrain_cycle, RETRAIN_INTERVAL_SECONDS, 'ai-retrain-scheduler')
