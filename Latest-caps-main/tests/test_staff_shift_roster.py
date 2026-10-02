import unittest
import os
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection

TODAY = datetime.now().strftime('%Y-%m-%d')
TOMORROW = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
NEXT_WEEK = (datetime.now() + timedelta(days=10)).strftime('%Y-%m-%d')


class StaffShiftRosterTestCase(unittest.TestCase):
    """Item 26: a forward-looking schedule, separate from staff_attendance
    (which only ever records what already happened)."""

    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' ORDER BY user_id LIMIT 1")
        self.staff_name = cursor.fetchone()['name']
        conn.close()

        self._clear_test_rows()

    def tearDown(self):
        self._clear_test_rows()

    def _clear_test_rows(self):
        conn = get_db_connection()
        conn.execute(
            "DELETE FROM staff_shifts WHERE staff_name = ? AND shift_date IN (?, ?, ?)",
            (self.staff_name, TODAY, TOMORROW, NEXT_WEEK),
        )
        conn.commit()
        conn.close()

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess['user'] = 'manager'
            sess['role'] = 'admin'
            sess['fullname'] = 'System Manager'
            sess['user_id'] = 1

    def _login_staff(self):
        with self.client.session_transaction() as sess:
            sess['user'] = self.staff_name
            sess['role'] = 'staff'
            sess['fullname'] = self.staff_name
            sess['user_id'] = 3

    def test_get_shifts_requires_authentication(self):
        res = self.client.get('/api/staff-shifts')
        self.assertEqual(res.status_code, 401)

    def test_admin_can_schedule_a_shift(self):
        self._login_admin()
        res = self.client.post('/api/staff-shifts', json={
            'staff_name': self.staff_name, 'shift_date': TOMORROW, 'shift_note': 'Morning shift',
        })
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        row = conn.execute(
            "SELECT shift_note FROM staff_shifts WHERE staff_name = ? AND shift_date = ?",
            (self.staff_name, TOMORROW),
        ).fetchone()
        conn.close()
        self.assertEqual(row['shift_note'], 'Morning shift')

    def test_staff_cannot_schedule_a_shift(self):
        self._login_staff()
        res = self.client.post('/api/staff-shifts', json={
            'staff_name': self.staff_name, 'shift_date': TOMORROW,
        })
        self.assertEqual(res.status_code, 403)

    def test_unknown_staff_name_rejected(self):
        self._login_admin()
        res = self.client.post('/api/staff-shifts', json={
            'staff_name': 'Totally Made Up Person', 'shift_date': TOMORROW,
        })
        self.assertEqual(res.status_code, 400)

    def test_rescheduling_same_staff_and_date_updates_note_not_duplicates(self):
        self._login_admin()
        self.client.post('/api/staff-shifts', json={'staff_name': self.staff_name, 'shift_date': TOMORROW, 'shift_note': 'First note'})
        self.client.post('/api/staff-shifts', json={'staff_name': self.staff_name, 'shift_date': TOMORROW, 'shift_note': 'Updated note'})

        conn = get_db_connection()
        rows = conn.execute(
            "SELECT shift_note FROM staff_shifts WHERE staff_name = ? AND shift_date = ?",
            (self.staff_name, TOMORROW),
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1, "must upsert, not create a second row")
        self.assertEqual(rows[0]['shift_note'], 'Updated note')

    def test_get_shifts_returns_scheduled_entries_within_default_window(self):
        self._login_admin()
        self.client.post('/api/staff-shifts', json={'staff_name': self.staff_name, 'shift_date': TOMORROW})

        res = self.client.get('/api/staff-shifts')
        self.assertEqual(res.status_code, 200)
        names_dates = [(r['staff_name'], r['shift_date']) for r in res.get_json()['data']]
        self.assertIn((self.staff_name, TOMORROW), names_dates)

    def test_get_shifts_excludes_entries_outside_requested_range(self):
        self._login_admin()
        self.client.post('/api/staff-shifts', json={'staff_name': self.staff_name, 'shift_date': NEXT_WEEK})

        res = self.client.get('/api/staff-shifts')  # default window is only ~6 days ahead
        names_dates = [(r['staff_name'], r['shift_date']) for r in res.get_json()['data']]
        self.assertNotIn((self.staff_name, NEXT_WEEK), names_dates)

        res = self.client.get(f'/api/staff-shifts?start_date={TODAY}&end_date={NEXT_WEEK}')
        names_dates = [(r['staff_name'], r['shift_date']) for r in res.get_json()['data']]
        self.assertIn((self.staff_name, NEXT_WEEK), names_dates)

    def test_staff_can_view_the_roster(self):
        self._login_admin()
        self.client.post('/api/staff-shifts', json={'staff_name': self.staff_name, 'shift_date': TOMORROW})

        self._login_staff()
        res = self.client.get('/api/staff-shifts')
        self.assertEqual(res.status_code, 200)

    def test_admin_can_remove_a_scheduled_shift(self):
        self._login_admin()
        self.client.post('/api/staff-shifts', json={'staff_name': self.staff_name, 'shift_date': TOMORROW})

        res = self.client.post('/api/delete-staff-shift', json={'staff_name': self.staff_name, 'shift_date': TOMORROW})
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        row = conn.execute(
            "SELECT 1 FROM staff_shifts WHERE staff_name = ? AND shift_date = ?",
            (self.staff_name, TOMORROW),
        ).fetchone()
        conn.close()
        self.assertIsNone(row)

    def test_removing_nonexistent_shift_returns_404(self):
        self._login_admin()
        res = self.client.post('/api/delete-staff-shift', json={'staff_name': self.staff_name, 'shift_date': TOMORROW})
        self.assertEqual(res.status_code, 404)

    def test_staff_cannot_remove_a_shift(self):
        self._login_admin()
        self.client.post('/api/staff-shifts', json={'staff_name': self.staff_name, 'shift_date': TOMORROW})

        self._login_staff()
        res = self.client.post('/api/delete-staff-shift', json={'staff_name': self.staff_name, 'shift_date': TOMORROW})
        self.assertEqual(res.status_code, 403)


if __name__ == '__main__':
    unittest.main()
