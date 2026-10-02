import unittest
import os
import sys
import uuid
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from werkzeug.security import generate_password_hash, check_password_hash

from app import app, get_db_connection

TEST_USERNAME = 'resettest@example.com'
TEST_GOOGLE_USERNAME = 'resettest-google@example.com'
OLD_PASSWORD = 'oldpassword123'


class PasswordResetTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self._clear_test_rows()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (username, password_hash, role, fullname) VALUES (?, ?, 'customer', 'Reset Test User')",
            (TEST_USERNAME, generate_password_hash(OLD_PASSWORD)),
        )
        self.user_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO users (username, password_hash, role, fullname, auth_provider) VALUES (?, ?, 'customer', 'Reset Test Google User', 'google')",
            (TEST_GOOGLE_USERNAME, generate_password_hash(uuid.uuid4().hex)),
        )
        self.google_user_id = cursor.lastrowid
        conn.commit()
        conn.close()

    def tearDown(self):
        self._clear_test_rows()

    def _clear_test_rows(self):
        conn = get_db_connection()
        conn.execute(
            "DELETE FROM password_resets WHERE user_id IN (SELECT user_id FROM users WHERE username IN (?, ?))",
            (TEST_USERNAME, TEST_GOOGLE_USERNAME),
        )
        conn.execute("DELETE FROM users WHERE username IN (?, ?)", (TEST_USERNAME, TEST_GOOGLE_USERNAME))
        conn.commit()
        conn.close()

    def _create_valid_otp(self, user_id, minutes_left=10, code='123456', attempts=0):
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO password_resets (user_id, token, expires_at, attempts) VALUES (?, ?, ?, ?)",
            (user_id, code, (datetime.now() + timedelta(minutes=minutes_left)).strftime('%Y-%m-%d %H:%M:%S'), attempts),
        )
        conn.commit()
        conn.close()
        return code

    def _reset_row_count(self, user_id):
        conn = get_db_connection()
        row = conn.execute("SELECT COUNT(*) FROM password_resets WHERE user_id = ?", (user_id,)).fetchone()
        conn.close()
        return row[0]

    # --- /forgot-password ---

    def test_forgot_password_shows_same_verify_step_for_unknown_username(self):
        res = self.client.post('/forgot-password', data={'username': 'no-such-user@example.com'})
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Enter Reset Code', res.data)

    def test_forgot_password_creates_otp_for_real_account(self):
        res = self.client.post('/forgot-password', data={'username': TEST_USERNAME})
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Enter Reset Code', res.data)
        self.assertEqual(self._reset_row_count(self.user_id), 1)

    def test_forgot_password_does_not_create_otp_for_google_account(self):
        res = self.client.post('/forgot-password', data={'username': TEST_GOOGLE_USERNAME})
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Enter Reset Code', res.data, "must look identical to the real-account case")
        self.assertEqual(self._reset_row_count(self.google_user_id), 0)

    def test_forgot_password_throttles_repeated_requests(self):
        self.client.post('/forgot-password', data={'username': TEST_USERNAME})
        self.client.post('/forgot-password', data={'username': TEST_USERNAME})
        self.assertEqual(self._reset_row_count(self.user_id), 1, "a second request within the cooldown must not issue a new code")

    # --- /reset-password ---

    def test_reset_password_rejects_bogus_code(self):
        self._create_valid_otp(self.user_id, code='123456')
        res = self.client.post('/reset-password', data={
            'username': TEST_USERNAME, 'otp': '999999',
            'password': 'brandnewpassword456', 'confirm_password': 'brandnewpassword456',
        })
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'invalid or expired code', res.data.lower())

    def test_reset_password_rejects_expired_code(self):
        code = self._create_valid_otp(self.user_id, minutes_left=-5)
        res = self.client.post('/reset-password', data={
            'username': TEST_USERNAME, 'otp': code,
            'password': 'brandnewpassword456', 'confirm_password': 'brandnewpassword456',
        })
        self.assertIn(b'invalid or expired code', res.data.lower())

    def test_reset_password_rejects_unknown_username(self):
        res = self.client.post('/reset-password', data={
            'username': 'no-such-user@example.com', 'otp': '123456',
            'password': 'brandnewpassword456', 'confirm_password': 'brandnewpassword456',
        })
        self.assertIn(b'invalid or expired code', res.data.lower())

    def test_reset_password_succeeds_with_valid_code(self):
        code = self._create_valid_otp(self.user_id)
        res = self.client.post('/reset-password', data={
            'username': TEST_USERNAME, 'otp': code,
            'password': 'brandnewpassword456', 'confirm_password': 'brandnewpassword456',
        }, follow_redirects=False)
        self.assertEqual(res.status_code, 302)

        conn = get_db_connection()
        row = conn.execute("SELECT password_hash FROM users WHERE user_id = ?", (self.user_id,)).fetchone()
        conn.close()
        self.assertTrue(check_password_hash(row['password_hash'], 'brandnewpassword456'))

    def test_reset_password_code_cannot_be_reused(self):
        code = self._create_valid_otp(self.user_id)
        self.client.post('/reset-password', data={
            'username': TEST_USERNAME, 'otp': code,
            'password': 'firstnewpass1', 'confirm_password': 'firstnewpass1',
        })
        res = self.client.post('/reset-password', data={
            'username': TEST_USERNAME, 'otp': code,
            'password': 'secondnewpass2', 'confirm_password': 'secondnewpass2',
        })
        self.assertIn(b'invalid or expired code', res.data.lower())

    def test_reset_password_rejects_mismatched_confirmation(self):
        code = self._create_valid_otp(self.user_id)
        res = self.client.post('/reset-password', data={
            'username': TEST_USERNAME, 'otp': code,
            'password': 'onepassword1', 'confirm_password': 'differentpassword2',
        })
        self.assertIn(b'do not match', res.data.lower())

        conn = get_db_connection()
        row = conn.execute("SELECT password_hash FROM users WHERE user_id = ?", (self.user_id,)).fetchone()
        conn.close()
        self.assertTrue(check_password_hash(row['password_hash'], OLD_PASSWORD), "password must be unchanged after a rejected attempt")

    def test_reset_password_rejects_short_password(self):
        code = self._create_valid_otp(self.user_id)
        res = self.client.post('/reset-password', data={
            'username': TEST_USERNAME, 'otp': code,
            'password': 'abc', 'confirm_password': 'abc',
        })
        self.assertIn(b'6 characters', res.data)

    def test_reset_password_wrong_code_increments_attempts(self):
        self._create_valid_otp(self.user_id, code='123456')
        self.client.post('/reset-password', data={
            'username': TEST_USERNAME, 'otp': '000000',
            'password': 'x', 'confirm_password': 'x',
        })
        conn = get_db_connection()
        row = conn.execute("SELECT attempts FROM password_resets WHERE user_id = ?", (self.user_id,)).fetchone()
        conn.close()
        self.assertEqual(row['attempts'], 1)

    def test_reset_password_locks_out_after_max_attempts(self):
        code = self._create_valid_otp(self.user_id, code='123456', attempts=5)
        res = self.client.post('/reset-password', data={
            'username': TEST_USERNAME, 'otp': code,
            'password': 'brandnewpassword456', 'confirm_password': 'brandnewpassword456',
        })
        self.assertIn(b'too many incorrect attempts', res.data.lower())

        conn = get_db_connection()
        row = conn.execute("SELECT password_hash FROM users WHERE user_id = ?", (self.user_id,)).fetchone()
        conn.close()
        self.assertTrue(check_password_hash(row['password_hash'], OLD_PASSWORD), "a locked-out code must not reset the password even if correct")


class AdminPasswordResetTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self._clear_test_rows()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (username, password_hash, role, fullname) VALUES (?, ?, 'customer', 'Admin Reset Target')",
            (TEST_USERNAME, generate_password_hash(OLD_PASSWORD)),
        )
        self.user_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO users (username, password_hash, role, fullname, auth_provider) VALUES (?, ?, 'customer', 'Admin Reset Google Target', 'google')",
            (TEST_GOOGLE_USERNAME, generate_password_hash(uuid.uuid4().hex)),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._clear_test_rows()

    def _clear_test_rows(self):
        conn = get_db_connection()
        conn.execute(
            "DELETE FROM password_resets WHERE user_id IN (SELECT user_id FROM users WHERE username IN (?, ?))",
            (TEST_USERNAME, TEST_GOOGLE_USERNAME),
        )
        conn.execute("DELETE FROM users WHERE username IN (?, ?)", (TEST_USERNAME, TEST_GOOGLE_USERNAME))
        conn.commit()
        conn.close()

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess['user'] = 'manager'
            sess['role'] = 'admin'
            sess['fullname'] = 'System Manager'
            sess['user_id'] = 1

    def test_admin_can_reset_a_real_user_password(self):
        self._login_admin()
        res = self.client.post('/api/admin/reset-user-password', json={
            'username': TEST_USERNAME, 'new_password': 'adminresetpass1',
        })
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        row = conn.execute("SELECT password_hash FROM users WHERE user_id = ?", (self.user_id,)).fetchone()
        conn.close()
        self.assertTrue(check_password_hash(row['password_hash'], 'adminresetpass1'))

    def test_admin_reset_rejected_for_google_account(self):
        self._login_admin()
        res = self.client.post('/api/admin/reset-user-password', json={
            'username': TEST_GOOGLE_USERNAME, 'new_password': 'somepassword1',
        })
        self.assertEqual(res.status_code, 400)

    def test_admin_reset_rejected_for_unknown_username(self):
        self._login_admin()
        res = self.client.post('/api/admin/reset-user-password', json={
            'username': 'nobody-here@example.com', 'new_password': 'somepassword1',
        })
        self.assertEqual(res.status_code, 404)

    def test_non_admin_cannot_reset_password(self):
        with self.client.session_transaction() as sess:
            sess['user'] = TEST_USERNAME
            sess['role'] = 'customer'
            sess['fullname'] = 'Reset Test User'
            sess['user_id'] = self.user_id

        res = self.client.post('/api/admin/reset-user-password', json={
            'username': TEST_USERNAME, 'new_password': 'somepassword1',
        })
        self.assertEqual(res.status_code, 403)


if __name__ == '__main__':
    unittest.main()
