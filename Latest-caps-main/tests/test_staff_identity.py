import unittest
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection
from utils import get_on_duty_staff_ids, is_staff_on_duty

TODAY = datetime.now().strftime('%Y-%m-%d')


class StaffIdentityTestCase(unittest.TestCase):
    """Attendance/roster/unavailability belong to a staff *account*
    (staff_user_id), not to a display name."""

    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess.update(user='manager', role='admin', fullname='System Manager', user_id=1)
        conn = get_db_connection()
        row = conn.execute("SELECT user_id, fullname FROM users WHERE role = 'staff' ORDER BY user_id LIMIT 1").fetchone()
        conn.close()
        self.staff_id, self.original_name = row['user_id'], row['fullname']
        self._clear()

    def tearDown(self):
        self._clear()
        conn = get_db_connection()
        conn.execute("UPDATE users SET fullname = ? WHERE user_id = ?", (self.original_name, self.staff_id))
        conn.execute("DELETE FROM users WHERE username = 'identity-dup'")
        conn.commit()
        conn.close()

    def _clear(self):
        conn = get_db_connection()
        conn.execute("DELETE FROM staff_attendance WHERE time_in = 'ID-test'")
        conn.commit()
        conn.close()

    def test_name_only_insert_gets_the_account_id(self):
        conn = get_db_connection()
        conn.execute("INSERT INTO staff_attendance (staff_name, date_str, status, time_in, time_out) VALUES (?, ?, 'Present', 'ID-test', '--:--')",
                     (self.original_name, TODAY))
        conn.commit()
        stored = conn.execute("SELECT staff_user_id FROM staff_attendance WHERE time_in = 'ID-test'").fetchone()[0]
        conn.close()
        self.assertEqual(stored, self.staff_id)

    def test_renamed_staff_stays_on_duty(self):
        res = self.client.post('/api/staff-attendance', json={
            'staff_user_id': self.staff_id, 'date_str': TODAY, 'status': 'Present', 'time_in': 'ID-test', 'time_out': '--:--',
        })
        self.assertEqual(res.status_code, 200)
        conn = get_db_connection()
        conn.execute("UPDATE users SET fullname = 'Renamed Washer' WHERE user_id = ?", (self.staff_id,))
        conn.commit()
        conn.close()
        self.assertIn(self.staff_id, get_on_duty_staff_ids())
        self.assertTrue(is_staff_on_duty(self.staff_id))
        records = self.client.get(f'/api/get-staff-attendance?date_str={TODAY}').get_json()['data']
        self.assertIn('Renamed Washer', [r['staff_name'] for r in records])

    def test_duplicate_staff_display_name_is_refused(self):
        res = self.client.post('/api/admin/create-user', json={
            'username': 'identity-dup', 'password': 'secret123', 'role': 'staff', 'fullname': self.original_name,
        })
        self.assertEqual(res.status_code, 400)


if __name__ == '__main__':
    unittest.main()
