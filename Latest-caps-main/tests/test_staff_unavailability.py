import unittest
import os
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection

TOMORROW = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
IN_5_DAYS = (datetime.now() + timedelta(days=5)).strftime('%Y-%m-%d')
NEXT_WEEK = (datetime.now() + timedelta(days=10)).strftime('%Y-%m-%d')


class StaffUnavailabilityTestCase(unittest.TestCase):
    """A staff-initiated "I can't work this day" flag - separate from
    staff_shifts (admin's plan), so admin sees it before scheduling rather
    than finding out after. See ROADMAP item 49/50 for the full rationale."""

    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' ORDER BY user_id LIMIT 1")
        row = cursor.fetchone()
        self.staff_id = row['user_id']
        self.staff_name = row['name']
        cursor.execute("SELECT user_id, COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' ORDER BY user_id LIMIT 1 OFFSET 1")
        row2 = cursor.fetchone()
        self.other_staff_id = row2['user_id']
        self.other_staff_name = row2['name']
        conn.close()

        self._clear_test_rows()

    def tearDown(self):
        self._clear_test_rows()

    def _clear_test_rows(self):
        conn = get_db_connection()
        conn.execute(
            "DELETE FROM staff_unavailability WHERE staff_name IN (?, ?) AND unavailable_date IN (?, ?, ?)",
            (self.staff_name, self.other_staff_name, TOMORROW, IN_5_DAYS, NEXT_WEEK),
        )
        conn.commit()
        conn.close()

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess['user'] = 'manager'
            sess['role'] = 'admin'
            sess['fullname'] = 'System Manager'
            sess['user_id'] = 1

    def _login_staff(self, user_id=None, name=None):
        with self.client.session_transaction() as sess:
            sess['user'] = name or self.staff_name
            sess['role'] = 'staff'
            sess['fullname'] = name or self.staff_name
            sess['user_id'] = user_id or self.staff_id

    def test_get_requires_authentication(self):
        res = self.client.get('/api/staff-unavailability')
        self.assertEqual(res.status_code, 401)

    def test_staff_can_mark_own_unavailability(self):
        self._login_staff()
        res = self.client.post('/api/staff-unavailability', json={
            'unavailable_date': TOMORROW, 'reason': 'Doctor appointment',
        })
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        row = conn.execute(
            "SELECT reason FROM staff_unavailability WHERE staff_name = ? AND unavailable_date = ?",
            (self.staff_name, TOMORROW),
        ).fetchone()
        conn.close()
        self.assertEqual(row['reason'], 'Doctor appointment')

    def test_marking_unavailable_always_uses_the_session_identity(self):
        """Staff can only mark themselves unavailable - there's no
        staff_name field in the request body a staff session could use to
        speak for a coworker, unlike the admin-only staff_name parameter
        on staff-shifts/staff-attendance."""
        self._login_staff()
        self.client.post('/api/staff-unavailability', json={'unavailable_date': TOMORROW, 'reason': 'x'})

        conn = get_db_connection()
        row = conn.execute(
            "SELECT staff_name FROM staff_unavailability WHERE unavailable_date = ?", (TOMORROW,)
        ).fetchone()
        conn.close()
        self.assertEqual(row['staff_name'], self.staff_name)

    def test_admin_cannot_mark_unavailability(self):
        """Admin isn't a washer, and this channel is specifically for
        staff to speak for themselves - admin has no path to set this on
        anyone's behalf."""
        self._login_admin()
        res = self.client.post('/api/staff-unavailability', json={'unavailable_date': TOMORROW})
        self.assertEqual(res.status_code, 403)

    def test_missing_date_rejected(self):
        self._login_staff()
        res = self.client.post('/api/staff-unavailability', json={'reason': 'no date given'})
        self.assertEqual(res.status_code, 400)

    def test_marking_same_date_twice_updates_reason_not_duplicates(self):
        self._login_staff()
        self.client.post('/api/staff-unavailability', json={'unavailable_date': TOMORROW, 'reason': 'First reason'})
        self.client.post('/api/staff-unavailability', json={'unavailable_date': TOMORROW, 'reason': 'Updated reason'})

        conn = get_db_connection()
        rows = conn.execute(
            "SELECT reason FROM staff_unavailability WHERE staff_name = ? AND unavailable_date = ?",
            (self.staff_name, TOMORROW),
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1, "must upsert, not create a second row")
        self.assertEqual(rows[0]['reason'], 'Updated reason')

    def test_staff_sees_only_their_own_entries(self):
        """A coworker's reason for being unavailable isn't anyone else's
        business - only admin gets the full picture."""
        self._login_staff(self.staff_id, self.staff_name)
        self.client.post('/api/staff-unavailability', json={'unavailable_date': TOMORROW, 'reason': 'mine'})

        self._login_staff(self.other_staff_id, self.other_staff_name)
        self.client.post('/api/staff-unavailability', json={'unavailable_date': TOMORROW, 'reason': 'theirs'})

        res = self.client.get('/api/staff-unavailability')
        names = [r['staff_name'] for r in res.get_json()['data']]
        self.assertIn(self.other_staff_name, names)
        self.assertNotIn(self.staff_name, names)

    def test_admin_sees_everyones_entries(self):
        self._login_staff(self.staff_id, self.staff_name)
        self.client.post('/api/staff-unavailability', json={'unavailable_date': TOMORROW, 'reason': 'mine'})

        self._login_staff(self.other_staff_id, self.other_staff_name)
        self.client.post('/api/staff-unavailability', json={'unavailable_date': TOMORROW, 'reason': 'theirs'})

        self._login_admin()
        res = self.client.get('/api/staff-unavailability')
        names = [r['staff_name'] for r in res.get_json()['data']]
        self.assertIn(self.staff_name, names)
        self.assertIn(self.other_staff_name, names)

    def test_get_excludes_entries_outside_requested_range(self):
        self._login_staff()
        self.client.post('/api/staff-unavailability', json={'unavailable_date': NEXT_WEEK})

        res = self.client.get('/api/staff-unavailability')  # default window is only ~6 days ahead
        dates = [r['unavailable_date'] for r in res.get_json()['data']]
        self.assertNotIn(NEXT_WEEK, dates)

        today = datetime.now().strftime('%Y-%m-%d')
        res = self.client.get(f'/api/staff-unavailability?start_date={today}&end_date={NEXT_WEEK}')
        dates = [r['unavailable_date'] for r in res.get_json()['data']]
        self.assertIn(NEXT_WEEK, dates)

    def test_staff_can_clear_their_own_entry(self):
        self._login_staff()
        self.client.post('/api/staff-unavailability', json={'unavailable_date': TOMORROW})

        res = self.client.post('/api/delete-staff-unavailability', json={'unavailable_date': TOMORROW})
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        row = conn.execute(
            "SELECT 1 FROM staff_unavailability WHERE staff_name = ? AND unavailable_date = ?",
            (self.staff_name, TOMORROW),
        ).fetchone()
        conn.close()
        self.assertIsNone(row)

    def test_clearing_nonexistent_entry_returns_404(self):
        self._login_staff()
        res = self.client.post('/api/delete-staff-unavailability', json={'unavailable_date': TOMORROW})
        self.assertEqual(res.status_code, 404)

    def test_staff_cannot_clear_a_coworkers_entry(self):
        """Deleting is scoped to the session's own staff_name the same way
        creating is - a staff session can't clear someone else's flag even
        if it somehow knew the date to target."""
        self._login_staff(self.staff_id, self.staff_name)
        self.client.post('/api/staff-unavailability', json={'unavailable_date': TOMORROW})

        self._login_staff(self.other_staff_id, self.other_staff_name)
        res = self.client.post('/api/delete-staff-unavailability', json={'unavailable_date': TOMORROW})
        self.assertEqual(res.status_code, 404)

        conn = get_db_connection()
        row = conn.execute(
            "SELECT 1 FROM staff_unavailability WHERE staff_name = ? AND unavailable_date = ?",
            (self.staff_name, TOMORROW),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "the original staff member's entry must survive")


if __name__ == '__main__':
    unittest.main()
