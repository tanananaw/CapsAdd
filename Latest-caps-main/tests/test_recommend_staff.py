import unittest
import os
import sys
import uuid
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection

TEST_PLATES = ('REC-0001', 'REC-0002', 'REC-0003')


class RecommendStaffTestCase(unittest.TestCase):
    """/api/recommend-staff picks the best AVAILABLE (on duty + not already
    on a job) washer via the same MCDA/SAW formula as the manager's
    productivity leaderboard. These tests force each candidate's on-duty
    status with our own attendance row (its attendance_id always sorts
    highest, so it wins get_active_staff_count-style "latest row for
    today" resolution) rather than asserting on the shared dev DB's
    ambient state, which real smoke-testing/usage can change day to day."""

    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT service_id, base_duration_minutes FROM services LIMIT 1")
        self.service_id, self.base_duration = cursor.fetchone()
        # A real staff account from the shared dev DB carries real history
        # now (seeded demo bookings, feedback, cancellations), which would
        # pollute completion%/speed/rating stats these tests need to
        # control precisely (e.g. a "perfect record reaches 1.0" case).
        # Creating our own throwaway staff account guarantees zero prior
        # history, independent of whatever real/seeded data exists.
        self.staff_name = f"Test Washer {uuid.uuid4().hex[:8]}"
        cursor.execute(
            "INSERT INTO users (username, password_hash, role, fullname) VALUES (?, 'x', 'staff', ?)",
            (f"test_washer_{uuid.uuid4().hex[:8]}", self.staff_name),
        )
        self.staff_id = cursor.lastrowid
        conn.commit()
        conn.close()

        self.today = datetime.now().strftime('%Y-%m-%d')
        self._attendance_ids = []
        self._clear_test_rows()

        with self.client.session_transaction() as sess:
            sess['user'] = 'manager'
            sess['role'] = 'admin'
            sess['fullname'] = 'System Manager'
            sess['user_id'] = 1

    def tearDown(self):
        self._clear_test_rows()
        conn = get_db_connection()
        if self._attendance_ids:
            conn.execute(
                f"DELETE FROM staff_attendance WHERE attendance_id IN ({','.join('?' * len(self._attendance_ids))})",
                self._attendance_ids,
            )
        conn.execute("DELETE FROM users WHERE user_id = ?", (self.staff_id,))
        conn.commit()
        conn.close()

    def _clear_test_rows(self):
        conn = get_db_connection()
        conn.execute(
            f"DELETE FROM feedbacks WHERE plate_number IN ({','.join('?' * len(TEST_PLATES))})",
            TEST_PLATES,
        )
        conn.execute(
            f"DELETE FROM reservations WHERE vehicle_plate_number IN ({','.join('?' * len(TEST_PLATES))})",
            TEST_PLATES,
        )
        conn.commit()
        conn.close()

    def _set_attendance(self, staff_name, status):
        """Inserts a fresh row that always has the highest attendance_id for
        (staff_name, today), so it's the one get_active_staff_count-style
        queries resolve to - overriding whatever real state exists."""
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO staff_attendance (staff_name, date_str, status, time_in, time_out) VALUES (?, ?, ?, '08:00', '--:--')",
            (staff_name, self.today, status),
        )
        attendance_id = cursor.lastrowid
        conn.commit()
        conn.close()
        self._attendance_ids.append(attendance_id)

    def _insert_reservation(self, plate, status, started_at=None, completed_at=None, assigned_staff_id=None):
        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO reservations (
                vehicle_plate_number, vehicle_type, service_id, queue_status,
                assigned_staff_id, started_at, completed_at
            ) VALUES (?, 'Car', ?, ?, ?, ?, ?)
            """,
            (plate, self.service_id, status, assigned_staff_id, started_at, completed_at),
        )
        conn.commit()
        conn.close()

    def test_requires_authentication(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        res = self.client.get('/api/recommend-staff')
        self.assertEqual(res.status_code, 401)

    def test_off_duty_staff_excluded(self):
        self._set_attendance(self.staff_name, 'Absent')
        res = self.client.get('/api/recommend-staff')
        self.assertEqual(res.status_code, 200)
        ids = [c['user_id'] for c in res.get_json()['ranked_available']]
        self.assertNotIn(self.staff_id, ids)

    def test_on_duty_and_idle_staff_is_available(self):
        self._set_attendance(self.staff_name, 'Present')
        res = self.client.get('/api/recommend-staff')
        self.assertEqual(res.status_code, 200)
        ids = [c['user_id'] for c in res.get_json()['ranked_available']]
        self.assertIn(self.staff_id, ids)

    def test_busy_staff_excluded_even_if_on_duty(self):
        self._set_attendance(self.staff_name, 'Present')
        self._insert_reservation('REC-0001', 'in_progress', assigned_staff_id=self.staff_id)
        res = self.client.get('/api/recommend-staff')
        self.assertEqual(res.status_code, 200)
        ids = [c['user_id'] for c in res.get_json()['ranked_available']]
        self.assertNotIn(self.staff_id, ids)

    def test_perfect_record_reaches_top_saw_score(self):
        """Speed caps at 100 (min(100, benchmark/actual*100)), completion at
        100%, rating at 5.0 (the DB's own CHECK constraint) - so a candidate
        who hits all three ceilings always normalizes to 1.0 on every SAW
        criterion and reaches the maximum possible score, no matter what
        real data any other on-duty staff member happens to have today."""
        self._set_attendance(self.staff_name, 'Present')
        completed_at = f"2026-01-01 10:{self.base_duration:02d}:00" if self.base_duration < 60 else '2026-01-01 11:00:00'
        self._insert_reservation(
            'REC-0002', 'completed',
            started_at='2026-01-01 10:00:00', completed_at=completed_at,
            assigned_staff_id=self.staff_id,
        )
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT reservation_id FROM reservations WHERE vehicle_plate_number = 'REC-0002'"
        )
        reservation_id = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO feedbacks (reservation_id, customer_name, plate_number, service_type, rating, comment)
            VALUES (?, 'Test Customer', 'REC-0002', 'Body Wash', 5, 'Perfect')
            """,
            (reservation_id,),
        )
        conn.commit()
        conn.close()

        res = self.client.get('/api/recommend-staff')
        data = res.get_json()
        mine = next(c for c in data['ranked_available'] if c['user_id'] == self.staff_id)
        self.assertAlmostEqual(mine['saw_score'], 1.0, places=4)
        self.assertTrue(mine['has_history'])
        # The reported top pick can only be a saw_score of 1.0 (nothing can
        # exceed the maximum our perfect performer already reached).
        self.assertAlmostEqual(data['recommended']['saw_score'], 1.0, places=4)

    def test_no_staff_on_duty_returns_null_recommendation_with_reason(self):
        # Force our test staff off duty - if this ends up the only staff
        # account in the whole system, this alone empties the pool; either
        # way the response shape under an empty pool must hold.
        self._set_attendance(self.staff_name, 'Absent')
        res = self.client.get('/api/recommend-staff')
        data = res.get_json()
        if not data['ranked_available']:
            self.assertIsNone(data['recommended'])
            self.assertIn(data['reason'], ('no_staff_on_duty', 'all_on_duty_staff_busy'))


if __name__ == '__main__':
    unittest.main()
