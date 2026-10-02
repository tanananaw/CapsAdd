import unittest
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection
from login_security import MAX_FAILED_ATTEMPTS


class LoginSecurityTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self._clear_attempts()

    def tearDown(self):
        self._clear_attempts()

    def _clear_attempts(self):
        # The whole table is scoped to this test class only (nothing else in
        # the app reads it), and every request from Flask's test client
        # shares the same simulated IP - so the per-IP lockout check would
        # bleed between test methods (and between usernames within one
        # method) unless the table is fully cleared, not just filtered by
        # username, between tests.
        conn = get_db_connection()
        conn.execute("DELETE FROM login_attempts")
        conn.commit()
        conn.close()

    def test_lockout_after_repeated_failures_blocks_even_correct_password(self):
        """After MAX_FAILED_ATTEMPTS wrong passwords for a username, the next
        attempt must be blocked by the lockout message - even if the
        attacker then guesses the actually-correct password - rather than
        allowing unlimited guesses."""
        for _ in range(MAX_FAILED_ATTEMPTS):
            res = self.client.post('/login', data={
                'username': 'manager', 'password': 'definitely-wrong-password',
            })
            self.assertEqual(res.status_code, 200)
            self.assertIn(b'Invalid username or password', res.data)

        # The real password would normally succeed - but the account is now
        # locked, so it must still be rejected with the lockout message.
        res = self.client.post('/login', data={
            'username': 'manager', 'password': 'md57admin2026',
        })
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Too many failed login attempts', res.data)

    def test_unlimited_attempts_against_nonexistent_username_also_locks_out(self):
        """The lockout must apply the same way whether or not the username
        actually exists - otherwise the differing behavior itself would leak
        which usernames are real accounts."""
        for _ in range(MAX_FAILED_ATTEMPTS):
            res = self.client.post('/login', data={
                'username': 'locktest_user', 'password': 'whatever',
            })
            self.assertIn(b'Invalid username or password', res.data)

        res = self.client.post('/login', data={
            'username': 'locktest_user', 'password': 'whatever-else',
        })
        self.assertIn(b'Too many failed login attempts', res.data)

    def test_attempts_below_threshold_are_not_locked_out(self):
        """A handful of failed attempts (fewer than the threshold) must not
        trigger a lockout - only sustained brute-forcing should."""
        for _ in range(MAX_FAILED_ATTEMPTS - 1):
            res = self.client.post('/login', data={
                'username': 'locktest_other', 'password': 'wrong',
            })
            self.assertIn(b'Invalid username or password', res.data)
            self.assertNotIn(b'Too many failed login attempts', res.data)


if __name__ == '__main__':
    unittest.main()
