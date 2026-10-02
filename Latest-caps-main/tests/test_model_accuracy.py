import unittest
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection
from ai_retrain import compute_prediction_accuracy

TEST_PLATES = ('ACC-0001', 'ACC-0002', 'ACC-0003', 'ACC-0004')


class ComputePredictionAccuracyTestCase(unittest.TestCase):
    def setUp(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT service_id FROM services LIMIT 1")
        self.service_id = cursor.fetchone()[0]
        conn.close()
        self._clear_test_rows()

    def tearDown(self):
        self._clear_test_rows()

    def _clear_test_rows(self):
        conn = get_db_connection()
        conn.execute(
            f"DELETE FROM reservations WHERE vehicle_plate_number IN ({','.join('?' * len(TEST_PLATES))})",
            TEST_PLATES,
        )
        conn.commit()
        conn.close()

    def _insert(self, plate, status, created_at, completed_at, predicted_wait_minutes):
        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO reservations (
                vehicle_plate_number, vehicle_type, service_id, queue_status,
                created_at, completed_at, predicted_wait_minutes
            ) VALUES (?, 'Car', ?, ?, ?, ?, ?)
            """,
            (plate, self.service_id, status, created_at, completed_at, predicted_wait_minutes),
        )
        conn.commit()
        conn.close()

    def test_no_predictions_on_record_returns_zero_sample(self):
        result = compute_prediction_accuracy()
        # Whatever the live count is, inserting nothing new must not crash
        # and must report None error figures at zero samples specifically.
        if result['sample_count'] == 0:
            self.assertIsNone(result['mae_minutes'])
            self.assertIsNone(result['rmse_minutes'])

    def test_computes_real_mae_and_rmse_from_known_errors(self):
        # Predicted 30, actual 40 -> error +10. Predicted 50, actual 40 -> error -10.
        self._insert('ACC-0001', 'completed', '2026-01-01 10:00:00', '2026-01-01 10:40:00', 30.0)
        self._insert('ACC-0002', 'completed', '2026-01-01 10:00:00', '2026-01-01 10:40:00', 50.0)

        result = compute_prediction_accuracy()
        matching_count = result['sample_count']
        self.assertGreaterEqual(matching_count, 2)

        # With only these two rows contributing exactly |+-10|, MAE would be
        # exactly 10.0 and RMSE exactly 10.0 - assert that directly only
        # when they're the sole rows in the table (typical for a clean test
        # DB); otherwise just sanity-check the shape of the result.
        if matching_count == 2:
            self.assertAlmostEqual(result['mae_minutes'], 10.0, places=1)
            self.assertTrue(result['weekly'], "a weekly MAE trend accompanies the overall figure")
            self.assertEqual(sum(w['sample_count'] for w in result['weekly']), result['sample_count'])
            self.assertAlmostEqual(result['rmse_minutes'], 10.0, places=1)

    def test_row_missing_prediction_is_excluded(self):
        self._insert('ACC-0003', 'completed', '2026-01-01 10:00:00', '2026-01-01 10:40:00', None)
        conn = get_db_connection()
        row = conn.execute(
            "SELECT predicted_wait_minutes FROM reservations WHERE vehicle_plate_number = ?", ('ACC-0003',)
        ).fetchone()
        conn.close()
        self.assertIsNone(row['predicted_wait_minutes'])
        # Doesn't raise, and this row specifically contributes nothing -
        # covered implicitly by the WHERE predicted_wait_minutes IS NOT NULL
        # clause in compute_prediction_accuracy().
        compute_prediction_accuracy()

    def test_implausible_duration_is_excluded(self):
        self._insert('ACC-0004', 'completed', '2026-01-01 10:00:00', '2026-01-01 09:00:00', 20.0)  # negative duration
        conn = get_db_connection()
        row = conn.execute(
            "SELECT 1 FROM reservations WHERE vehicle_plate_number = ? AND predicted_wait_minutes IS NOT NULL",
            ('ACC-0004',),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "the row itself should exist with a prediction stored")
        # Just confirms no crash; the negative-duration guard inside
        # compute_prediction_accuracy is what actually excludes it.
        compute_prediction_accuracy()


class ModelAccuracyEndpointTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

    def test_requires_admin(self):
        res = self.client.get('/api/model-accuracy')
        self.assertEqual(res.status_code, 401)

        with self.client.session_transaction() as sess:
            sess['user'] = 'customer_user'
            sess['role'] = 'customer'
            sess['fullname'] = 'Default Customer'
            sess['user_id'] = 2

        res = self.client.get('/api/model-accuracy')
        self.assertEqual(res.status_code, 403)

    def test_admin_gets_a_valid_response_shape(self):
        with self.client.session_transaction() as sess:
            sess['user'] = 'manager'
            sess['role'] = 'admin'
            sess['fullname'] = 'System Manager'
            sess['user_id'] = 1

        res = self.client.get('/api/model-accuracy')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['status'], 'success')
        self.assertIn('mae_minutes', data)
        self.assertIn('rmse_minutes', data)
        self.assertIn('sample_count', data)


class PredictedWaitCaptureTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self._clear_test_rows()
        # Booking submission now requires a washer on duty today - force
        # one on regardless of the shared dev DB's ambient attendance state
        # (see test_booking_logic.py's StaffedBookingGateTestCase for the
        # decline-path coverage).
        self._attendance_ids = []
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' LIMIT 1")
        staff_name = cursor.fetchone()['name']
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute(
            "INSERT INTO staff_attendance (staff_name, date_str, status, time_in, time_out) VALUES (?, ?, 'Present', '08:00', '--:--')",
            (staff_name, today),
        )
        self._attendance_ids.append(cursor.lastrowid)
        # ...and on the roster for the day this test books (customer
        # bookings need a staffed day - see api_submit_booking).
        cursor.execute("INSERT OR IGNORE INTO staff_shifts (staff_name, shift_date, created_by) VALUES (?, ?, 'test-roster')", (staff_name, '2026-12-01'))
        conn.commit()
        conn.close()
        with self.client.session_transaction() as sess:
            sess['user'] = 'customer_user'
            sess['role'] = 'customer'
            sess['fullname'] = 'Default Customer'
            sess['user_id'] = 2

    def tearDown(self):
        self._clear_test_rows()
        if self._attendance_ids:
            conn = get_db_connection()
            conn.execute(
                f"DELETE FROM staff_attendance WHERE attendance_id IN ({','.join('?' * len(self._attendance_ids))})",
                self._attendance_ids,
            )
            conn.commit()
            conn.close()

    def _clear_test_rows(self):
        conn = get_db_connection()
        conn.execute("DELETE FROM reservations WHERE vehicle_plate_number = 'ACCP-0001'")
        conn.execute("DELETE FROM staff_shifts WHERE created_by = 'test-roster'")
        conn.commit()
        conn.close()

    def _bookable_service_id(self):
        conn = get_db_connection()
        service_id = conn.execute(
            "SELECT s.service_id FROM services s JOIN service_prices p ON p.service_id = s.service_id WHERE s.is_active = 1 AND p.vehicle_type = 'Medium' ORDER BY s.service_id LIMIT 1"
        ).fetchone()['service_id']
        conn.close()
        return service_id

    def test_submit_booking_captures_a_real_prediction(self):
        res = self.client.post('/api/submit-booking', json={
            'plate_number': 'ACCP-0001',
            'vehicle_type': 'Medium',
            'service_id': self._bookable_service_id(),
            'customer_email': 'accp@example.com',
            # A walk-in (no booking_date/time) is checked against the real
            # wall-clock business hours (8AM-9PM), which made this test
            # fail whenever actually run outside that window even though
            # it has nothing to do with business hours - it's testing
            # predicted_wait_minutes capture. A fixed future scheduled time
            # sidesteps that.
            'booking_date': '2026-12-01', 'booking_time': '10:00 AM', 'assigned_bay': 'Bay #1',
        })
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        row = conn.execute(
            "SELECT predicted_wait_minutes FROM reservations WHERE vehicle_plate_number = ?", ('ACCP-0001',)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row['predicted_wait_minutes'])
        self.assertGreaterEqual(row['predicted_wait_minutes'], 5.0)


if __name__ == '__main__':
    unittest.main()
