import unittest
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection

TODAY = datetime.now().strftime('%Y-%m-%d')


class ClockInRequestTestCase(unittest.TestCase):
    """Staff can no longer clock themselves in directly (admin-only, see
    test_staff_roster.py) - but they can send a request, which lands as a
    'Pending' attendance row until admin confirms (-> Present/Late) or
    rejects (-> Absent) it."""

    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' ORDER BY user_id LIMIT 1")
        row = cursor.fetchone()
        self.staff_user_id = row['user_id']
        self.staff_fullname = row['name']
        conn.close()

        self._attendance_ids = []
        self._clear_test_rows()

    def tearDown(self):
        self._clear_test_rows()

    def _clear_test_rows(self):
        conn = get_db_connection()
        conn.execute(
            "DELETE FROM staff_attendance WHERE staff_name = ? AND date_str = ?",
            (self.staff_fullname, TODAY),
        )
        conn.commit()
        conn.close()

    def _insert_attendance(self, status, time_in='--:--', time_out='--:--'):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO staff_attendance (staff_name, date_str, status, time_in, time_out) VALUES (?, ?, ?, ?, ?)",
            (self.staff_fullname, TODAY, status, time_in, time_out),
        )
        conn.commit()
        conn.close()

    def _login_staff(self):
        with self.client.session_transaction() as sess:
            sess['user'] = self.staff_fullname
            sess['role'] = 'staff'
            sess['fullname'] = self.staff_fullname
            sess['user_id'] = self.staff_user_id

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess['user'] = 'manager'
            sess['role'] = 'admin'
            sess['fullname'] = 'System Manager'
            sess['user_id'] = 1

    def _latest_status(self):
        conn = get_db_connection()
        row = conn.execute(
            "SELECT status, time_in, time_out FROM staff_attendance WHERE staff_name = ? AND date_str = ? ORDER BY attendance_id DESC LIMIT 1",
            (self.staff_fullname, TODAY),
        ).fetchone()
        conn.close()
        return row

    def test_staff_can_request_clock_in(self):
        self._login_staff()
        res = self.client.post('/api/staff-clock-in-request')
        self.assertEqual(res.status_code, 200)

        row = self._latest_status()
        self.assertEqual(row['status'], 'Pending')
        self.assertNotEqual(row['time_in'], '--:--')

    def test_staff_cannot_double_request_while_pending(self):
        self._login_staff()
        self.client.post('/api/staff-clock-in-request')
        res = self.client.post('/api/staff-clock-in-request')
        self.assertEqual(res.status_code, 409)

    def test_staff_cannot_request_while_already_clocked_in(self):
        self._insert_attendance('Present', time_in='08:00 AM')
        self._login_staff()
        res = self.client.post('/api/staff-clock-in-request')
        self.assertEqual(res.status_code, 409)

    def test_admin_can_request_clock_in_is_staff_only(self):
        self._login_admin()
        res = self.client.post('/api/staff-clock-in-request')
        self.assertEqual(res.status_code, 403)

    def test_pending_request_shows_up_for_admin(self):
        self._login_staff()
        self.client.post('/api/staff-clock-in-request')

        self._login_admin()
        res = self.client.get('/api/clock-in-requests')
        self.assertEqual(res.status_code, 200)
        names = [r['staff_name'] for r in res.get_json()['data']]
        self.assertIn(self.staff_fullname, names)

    def test_staff_cannot_view_clock_in_requests(self):
        self._login_staff()
        res = self.client.get('/api/clock-in-requests')
        self.assertEqual(res.status_code, 403)

    def test_admin_confirm_moves_pending_to_present(self):
        self._insert_attendance('Pending', time_in='08:00 AM')
        self._login_admin()

        res = self.client.post('/api/confirm-clock-in', json={'staff_name': self.staff_fullname})
        self.assertEqual(res.status_code, 200)

        row = self._latest_status()
        self.assertEqual(row['status'], 'Present')
        self.assertEqual(row['time_in'], '08:00 AM')
        self.assertEqual(row['time_out'], '--:--')

    def test_admin_confirm_after_cutoff_marks_late(self):
        self._insert_attendance('Pending', time_in='09:30 AM')
        self._login_admin()

        res = self.client.post('/api/confirm-clock-in', json={'staff_name': self.staff_fullname})
        self.assertEqual(res.status_code, 200)

        row = self._latest_status()
        self.assertEqual(row['status'], 'Late')

    def test_admin_confirm_keeps_the_requested_time_not_confirm_time(self):
        """The real clock-in time is when staff asked, not whenever admin
        got around to approving it."""
        self._insert_attendance('Pending', time_in='07:45 AM')
        self._login_admin()
        self.client.post('/api/confirm-clock-in', json={'staff_name': self.staff_fullname})

        row = self._latest_status()
        self.assertEqual(row['time_in'], '07:45 AM')

    def test_admin_reject_marks_absent(self):
        self._insert_attendance('Pending', time_in='08:00 AM')
        self._login_admin()

        res = self.client.post('/api/reject-clock-in', json={'staff_name': self.staff_fullname})
        self.assertEqual(res.status_code, 200)

        row = self._latest_status()
        self.assertEqual(row['status'], 'Absent')

    def test_confirm_without_pending_request_rejected(self):
        self._login_admin()
        res = self.client.post('/api/confirm-clock-in', json={'staff_name': self.staff_fullname})
        self.assertEqual(res.status_code, 400)

    def test_reject_without_pending_request_rejected(self):
        self._login_admin()
        res = self.client.post('/api/reject-clock-in', json={'staff_name': self.staff_fullname})
        self.assertEqual(res.status_code, 400)

    def test_staff_cannot_confirm_or_reject(self):
        self._insert_attendance('Pending', time_in='08:00 AM')
        self._login_staff()

        res = self.client.post('/api/confirm-clock-in', json={'staff_name': self.staff_fullname})
        self.assertEqual(res.status_code, 403)
        res = self.client.post('/api/reject-clock-in', json={'staff_name': self.staff_fullname})
        self.assertEqual(res.status_code, 403)

    def test_confirmed_staff_can_now_start_a_service(self):
        """End-to-end tie-in with the on-duty gate on /api/start-service."""
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT service_id FROM services LIMIT 1")
        service_id = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO reservations (vehicle_plate_number, vehicle_type, service_id, queue_status) VALUES ('CLOCK-0001', 'Car', ?, 'waiting')",
            (service_id,),
        )
        conn.commit()
        conn.close()

        try:
            self._login_staff()
            self.client.post('/api/staff-clock-in-request')

            self._login_admin()
            self.client.post('/api/confirm-clock-in', json={'staff_name': self.staff_fullname})

            self._login_staff()
            res = self.client.post('/api/start-service', json={'plate_number': 'CLOCK-0001'})
            self.assertEqual(res.status_code, 200)
        finally:
            conn = get_db_connection()
            conn.execute("DELETE FROM reservations WHERE vehicle_plate_number = 'CLOCK-0001'")
            conn.commit()
            conn.close()


if __name__ == '__main__':
    unittest.main()
