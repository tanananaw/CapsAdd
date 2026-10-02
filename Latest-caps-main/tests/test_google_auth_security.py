import unittest
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from werkzeug.security import generate_password_hash

from app import app, get_db_connection

TEST_USERNAME = 'googletest@example.com'


class GoogleAuthSecurityTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self._clear_test_rows()

        # Simulates an account created by google_auth_callback: a real
        # (but unknown-to-anyone) password hash, marked auth_provider='google'.
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO users (username, password_hash, role, fullname, auth_provider) VALUES (?, ?, 'customer', 'Google Test User', 'google')",
            (TEST_USERNAME, generate_password_hash('some-random-unknowable-value')),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._clear_test_rows()

    def _clear_test_rows(self):
        conn = get_db_connection()
        conn.execute("DELETE FROM users WHERE username = ?", (TEST_USERNAME,))
        conn.execute("DELETE FROM login_attempts WHERE username = ?", (TEST_USERNAME,))
        conn.commit()
        conn.close()

    def test_google_account_rejects_the_old_known_shared_password(self):
        """The historical bug: every Google-created account used to share
        the fixed password 'google_auth_secure_2026'. Even if some
        pre-existing row in a live database still has that hash, the
        auth_provider check must refuse password login for it outright."""
        res = self.client.post('/login', data={
            'username': TEST_USERNAME,
            'password': 'google_auth_secure_2026',
        })
        self.assertEqual(res.status_code, 200)  # re-renders login.html with an error, no redirect
        self.assertIn(b'Google Sign-In', res.data)

    def test_google_account_rejects_any_password_attempt(self):
        res = self.client.post('/login', data={
            'username': TEST_USERNAME,
            'password': 'literally anything',
        })
        self.assertIn(b'Google Sign-In', res.data)

        with self.client.session_transaction() as sess:
            self.assertNotIn('user', sess)

    def test_regular_non_google_account_still_logs_in_normally(self):
        """Regression guard: the auth_provider check must not accidentally
        block ordinary username/password accounts."""
        res = self.client.post('/login', data={
            'username': 'customer_user',
            'password': 'customer123',
        }, follow_redirects=False)
        self.assertEqual(res.status_code, 302)


if __name__ == '__main__':
    unittest.main()
