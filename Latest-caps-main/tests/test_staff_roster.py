import unittest
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection

TEST_DATE = '2026-01-15'


class StaffRosterTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

        conn = get_db_connection()
        cursor = conn.cursor()
        # COALESCE matches how the real login flow populates
        # session['fullname'] (auth.py falls back to username when fullname
        # is unset) and how /api/get-staff-list resolves a display name.
        cursor.execute("SELECT user_id, COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' ORDER BY user_id LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        self.staff_user_id = row['user_id']
        self.staff_fullname = row['name']

        self._clear_test_rows()

    def tearDown(self):
        self._clear_test_rows()

    def _clear_test_rows(self):
        conn = get_db_connection()
        conn.execute("DELETE FROM staff_attendance WHERE date_str = ?", (TEST_DATE,))
        conn.commit()
        conn.close()

    def test_get_staff_list_returns_only_real_staff_accounts(self):
        with self.client.session_transaction() as sess:
            sess['user'] = self.staff_fullname
            sess['role'] = 'staff'
            sess['fullname'] = self.staff_fullname
            sess['user_id'] = self.staff_user_id

        res = self.client.get('/api/get-staff-list')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        names = [row['name'] for row in data['data']]
        self.assertIn(self.staff_fullname, names)
        self.assertNotIn('Juan Dela Cruz', names, "the old hardcoded fictional roster must not leak into the real endpoint")

    def test_staff_cannot_record_any_attendance(self):
        """Staff can no longer clock themselves (or anyone else) in/out -
        there was no PIN or other check that made a client-supplied
        identity trustworthy for self-service, so recording attendance is
        admin-only now."""
        with self.client.session_transaction() as sess:
            sess['user'] = self.staff_fullname
            sess['role'] = 'staff'
            sess['fullname'] = self.staff_fullname
            sess['user_id'] = self.staff_user_id

        res = self.client.post('/api/staff-attendance', json={
            'staff_name': self.staff_fullname,
            'date_str': TEST_DATE,
            'status': 'Present',
            'time_in': '08:00 AM',
            'time_out': '--:--',
        })
        self.assertEqual(res.status_code, 403)

        conn = get_db_connection()
        row = conn.execute(
            "SELECT 1 FROM staff_attendance WHERE date_str = ?", (TEST_DATE,)
        ).fetchone()
        conn.close()
        self.assertIsNone(row, "a forbidden attendance request must not be persisted")

    def test_admin_can_record_attendance_for_a_real_staff_member(self):
        with self.client.session_transaction() as sess:
            sess['user'] = 'manager'
            sess['role'] = 'admin'
            sess['fullname'] = 'System Manager'
            sess['user_id'] = 1

        res = self.client.post('/api/staff-attendance', json={
            'staff_name': self.staff_fullname,
            'date_str': TEST_DATE,
            'status': 'Present',
            'time_in': '08:00 AM',
            'time_out': '--:--',
        })
        self.assertEqual(res.status_code, 200)

    def test_admin_recording_attendance_for_unknown_name_rejected(self):
        with self.client.session_transaction() as sess:
            sess['user'] = 'manager'
            sess['role'] = 'admin'
            sess['fullname'] = 'System Manager'
            sess['user_id'] = 1

        res = self.client.post('/api/staff-attendance', json={
            'staff_name': 'Totally Made Up Person',
            'date_str': TEST_DATE,
            'status': 'Present',
            'time_in': '08:00 AM',
            'time_out': '--:--',
        })
        self.assertEqual(res.status_code, 400)

        conn = get_db_connection()
        row = conn.execute(
            "SELECT 1 FROM staff_attendance WHERE staff_name = 'Totally Made Up Person'"
        ).fetchone()
        conn.close()
        self.assertIsNone(row)

if __name__ == '__main__':
    unittest.main()
