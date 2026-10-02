import unittest
import os
import sys
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection
from utils import log_activity


class AuditLogTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' ORDER BY user_id LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        self.staff_user_id = row['user_id']
        self.staff_fullname = row['name']

    def tearDown(self):
        conn = get_db_connection()
        conn.execute("DELETE FROM staff_attendance WHERE date_str = '2026-02-02'")
        conn.commit()
        conn.close()

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess['user'] = 'manager'
            sess['role'] = 'admin'
            sess['fullname'] = 'System Manager'
            sess['user_id'] = 1

    def test_audit_logs_requires_admin(self):
        res = self.client.get('/api/get-audit-logs')
        self.assertEqual(res.status_code, 401)

        with self.client.session_transaction() as sess:
            sess['user'] = self.staff_fullname
            sess['role'] = 'staff'
            sess['fullname'] = self.staff_fullname
            sess['user_id'] = self.staff_user_id

        res = self.client.get('/api/get-audit-logs')
        self.assertEqual(res.status_code, 403)

    def test_get_audit_logs_returns_newest_first(self):
        marker = uuid.uuid4().hex
        log_activity('TestUser', f'first-event-{marker}')
        log_activity('TestUser', f'second-event-{marker}')

        self._login_admin()
        res = self.client.get('/api/get-audit-logs?limit=10')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        actions = [row['action'] for row in data['data']]

        self.assertIn(f'second-event-{marker}', actions)
        self.assertIn(f'first-event-{marker}', actions)
        # Newest first: the second event must appear before the first.
        self.assertLess(actions.index(f'second-event-{marker}'), actions.index(f'first-event-{marker}'))

    def test_get_audit_logs_respects_limit(self):
        self._login_admin()
        res = self.client.get('/api/get-audit-logs?limit=3')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertLessEqual(len(data['data']), 3)

    def test_staff_attendance_clock_in_writes_to_real_log(self):
        """The old client-side fake log used to record this - now the
        backend must record it in the real, shared audit trail instead.
        Attendance is admin-only now (staff can't clock themselves in), so
        this records admin marking a real staff member present."""
        marker = uuid.uuid4().hex[:8]
        self._login_admin()

        res = self.client.post('/api/staff-attendance', json={
            'staff_name': self.staff_fullname,
            'date_str': '2026-02-02',
            'status': 'Present',
            'time_in': f'08:0{marker[0]} AM',
            'time_out': '--:--',
        })
        self.assertEqual(res.status_code, 200)

        res = self.client.get('/api/get-audit-logs?limit=20')
        actions = [row['action'] for row in res.get_json()['data']]
        matching = [a for a in actions if a.startswith('Time-In:') and self.staff_fullname in a]
        self.assertTrue(matching, f"expected a Time-In log entry for {self.staff_fullname}, got: {actions}")

if __name__ == '__main__':
    unittest.main()
