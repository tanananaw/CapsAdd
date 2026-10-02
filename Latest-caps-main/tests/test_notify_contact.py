import unittest
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection


class NotifyContactTestCase(unittest.TestCase):
    """Opt-in email for shift_reminder.py - always the session's own
    account, never something admin sets on someone else's behalf. See
    ROADMAP item 49/50."""

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
        conn.close()

        self._reset_contact()

    def tearDown(self):
        self._reset_contact()

    def _reset_contact(self):
        conn = get_db_connection()
        conn.execute("UPDATE users SET notify_email = NULL WHERE user_id = ?", (self.staff_id,))
        conn.commit()
        conn.close()

    def _login_staff(self):
        with self.client.session_transaction() as sess:
            sess['user'] = self.staff_name
            sess['role'] = 'staff'
            sess['fullname'] = self.staff_name
            sess['user_id'] = self.staff_id

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess['user'] = 'manager'
            sess['role'] = 'admin'
            sess['fullname'] = 'System Manager'
            sess['user_id'] = 1

    def test_get_requires_authentication(self):
        res = self.client.get('/api/my-notify-contact')
        self.assertEqual(res.status_code, 401)

    def test_defaults_to_null_for_a_fresh_account(self):
        self._login_staff()
        res = self.client.get('/api/my-notify-contact')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIsNone(data['notify_email'])

    def test_staff_can_set_email(self):
        self._login_staff()
        res = self.client.post('/api/update-notify-contact', json={'notify_email': 'staff@example.com'})
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        row = conn.execute("SELECT notify_email FROM users WHERE user_id = ?", (self.staff_id,)).fetchone()
        conn.close()
        self.assertEqual(row['notify_email'], 'staff@example.com')

    def test_leaving_it_blank_is_allowed_opt_out(self):
        """No contact info at all is a valid, expected state - nobody is
        required to hand over an email just to have a staff account."""
        self._login_staff()
        res = self.client.post('/api/update-notify-contact', json={'notify_email': ''})
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        row = conn.execute("SELECT notify_email FROM users WHERE user_id = ?", (self.staff_id,)).fetchone()
        conn.close()
        self.assertIsNone(row['notify_email'])

    def test_malformed_email_rejected(self):
        self._login_staff()
        res = self.client.post('/api/update-notify-contact', json={'notify_email': 'not-an-email'})
        self.assertEqual(res.status_code, 400)

    def test_admin_cannot_use_this_endpoint(self):
        """No admin path exists to set another account's reminder contact
        - it's opt-in and self-service only."""
        self._login_admin()
        res = self.client.post('/api/update-notify-contact', json={'notify_email': 'x@example.com'})
        self.assertEqual(res.status_code, 403)

    def test_saved_contact_is_retrievable(self):
        self._login_staff()
        self.client.post('/api/update-notify-contact', json={'notify_email': 'staff@example.com'})

        res = self.client.get('/api/my-notify-contact')
        data = res.get_json()
        self.assertEqual(data['notify_email'], 'staff@example.com')

    def test_not_exposed_via_get_staff_list(self):
        """An email isn't something a coworker needs to see just to view
        the staff roster - kept off the general staff list entirely."""
        self._login_staff()
        self.client.post('/api/update-notify-contact', json={'notify_email': 'staff@example.com'})

        res = self.client.get('/api/get-staff-list')
        self.assertEqual(res.status_code, 200)
        body_text = res.get_data(as_text=True)
        self.assertNotIn('staff@example.com', body_text)


if __name__ == '__main__':
    unittest.main()
