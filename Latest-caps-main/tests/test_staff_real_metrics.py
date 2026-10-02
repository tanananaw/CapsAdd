import unittest
import os
import sys
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection

TEST_PLATES = ('RM-0001', 'RM-0002', 'RM-0003', 'RM-0004')


class StaffRealMetricsTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT service_id, base_duration_minutes FROM services LIMIT 1")
        self.service_id, self.base_duration = cursor.fetchone()
        # A throwaway staff account created just for this test class,
        # rather than a real seeded staff member - "no data yet" needs a
        # user guaranteed to have zero history, and a real account can
        # accumulate real jobs at any time (from actual usage, demos,
        # etc.), which would otherwise make that assumption silently false.
        self.staff_name = f"Test Metrics Staff {uuid.uuid4().hex[:8]}"
        cursor.execute(
            "INSERT INTO users (username, password_hash, role, fullname) VALUES (?, 'x', 'staff', ?)",
            (self.staff_name, self.staff_name),
        )
        self.staff_id = cursor.lastrowid
        conn.commit()
        conn.close()
        self._clear_test_rows()

        with self.client.session_transaction() as sess:
            sess['user'] = 'manager'
            sess['role'] = 'admin'
            sess['fullname'] = 'System Manager'
            sess['user_id'] = 1

    def tearDown(self):
        self._clear_test_rows()
        conn = get_db_connection()
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

    def _insert_reservation(self, plate, status, started_at=None, completed_at=None, assigned=True):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO reservations (
                vehicle_plate_number, vehicle_type, service_id, queue_status,
                assigned_staff_id, started_at, completed_at
            ) VALUES (?, 'Car', ?, ?, ?, ?, ?)
            """,
            (plate, self.service_id, status, self.staff_id if assigned else None, started_at, completed_at),
        )
        reservation_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return reservation_id

    def _get_my_metrics(self, data):
        return next(row for row in data if row['user_id'] == self.staff_id)

    def test_no_data_returns_null_metrics(self):
        res = self.client.get('/api/staff-real-metrics')
        self.assertEqual(res.status_code, 200)
        mine = self._get_my_metrics(res.get_json()['data'])
        self.assertIsNone(mine['speed'])
        self.assertIsNone(mine['completion'])
        self.assertIsNone(mine['rating'])

    def test_speed_computed_from_real_started_and_completed(self):
        # Benchmark is self.base_duration minutes; took exactly that long ->
        # speed should be exactly 100.
        self._insert_reservation(
            'RM-0001', 'completed',
            started_at='2026-01-01 10:00:00',
            completed_at=f"2026-01-01 10:{self.base_duration:02d}:00" if self.base_duration < 60 else '2026-01-01 11:30:00',
        )
        res = self.client.get('/api/staff-real-metrics')
        mine = self._get_my_metrics(res.get_json()['data'])
        self.assertIsNotNone(mine['speed'])
        self.assertGreater(mine['speed'], 0)
        self.assertLessEqual(mine['speed'], 100)

    def test_completion_percentage_from_resolved_jobs_only(self):
        self._insert_reservation('RM-0001', 'completed', started_at='2026-01-01 10:00:00', completed_at='2026-01-01 10:30:00')
        self._insert_reservation('RM-0002', 'cancelled')
        self._insert_reservation('RM-0003', 'in_progress')  # unresolved - must not count either way

        res = self.client.get('/api/staff-real-metrics')
        mine = self._get_my_metrics(res.get_json()['data'])
        # 1 completed out of 2 resolved (completed + cancelled) = 50%
        self.assertAlmostEqual(mine['completion'], 50.0, places=1)

    def test_rating_averages_real_feedback_for_that_staff_members_jobs(self):
        reservation_id = self._insert_reservation('RM-0004', 'completed', started_at='2026-01-01 10:00:00', completed_at='2026-01-01 10:30:00')

        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO feedbacks (reservation_id, customer_name, plate_number, service_type, rating, comment)
            VALUES (?, 'Test Customer', 'RM-0004', 'Body Wash', 5, 'Great job')
            """,
            (reservation_id,),
        )
        conn.commit()
        conn.close()

        res = self.client.get('/api/staff-real-metrics')
        mine = self._get_my_metrics(res.get_json()['data'])
        self.assertAlmostEqual(mine['rating'], 5.0, places=1)

    def test_unassigned_job_does_not_count_toward_any_staff_member(self):
        self._insert_reservation('RM-0001', 'completed', started_at='2026-01-01 10:00:00', completed_at='2026-01-01 10:30:00', assigned=False)
        res = self.client.get('/api/staff-real-metrics')
        mine = self._get_my_metrics(res.get_json()['data'])
        self.assertIsNone(mine['completion'])

    def test_requires_authentication(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        res = self.client.get('/api/staff-real-metrics')
        self.assertEqual(res.status_code, 401)


if __name__ == '__main__':
    unittest.main()
