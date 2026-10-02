import unittest
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection

TEST_PLATES = ('SEC-0001', 'SEC-0002')


class QueueConfidentialityTestCase(unittest.TestCase):
    """/api/get-queue and /api/booked-slots used to have no auth check at
    all - anyone, logged in or not, could pull the full live queue
    (customer names, plates, emails) system-wide. This covers both the
    auth requirement and the per-row masking that now applies to any row
    that isn't the requesting customer's own booking."""

    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT service_id FROM services LIMIT 1")
        self.service_id = cursor.fetchone()[0]
        conn.close()

        self._clear_test_rows()

    def tearDown(self):
        self._clear_test_rows()

    def _clear_test_rows(self):
        conn = get_db_connection()
        conn.execute(
            f"DELETE FROM reservations WHERE vehicle_plate_number IN ({','.join('?' * len(TEST_PLATES))})",
            TEST_PLATES,
        )
        conn.commit()
        conn.close()

    def _insert_reservation(self, plate, email, name):
        # No real user_id here on purpose - reservations.user_id has a FK
        # to users(user_id), and a made-up id would raise IntegrityError
        # (leaving the connection open with a dangling transaction, since
        # the exception skips conn.close() below). Ownership in
        # /api/get-queue already falls back to matching on customer_email,
        # so leaving user_id NULL still exercises the real masking logic.
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO reservations (vehicle_plate_number, vehicle_type, service_id, queue_status,
                    customer_email, customer_name)
                VALUES (?, 'Car', ?, 'waiting', ?, ?)
                """,
                (plate, self.service_id, email, name),
            )
            reservation_id = cursor.lastrowid
            conn.commit()
            return reservation_id
        finally:
            conn.close()

    def _login_customer(self, user_id, email):
        with self.client.session_transaction() as sess:
            sess['user'] = email
            sess['role'] = 'customer'
            sess['fullname'] = 'Test Customer'
            sess['user_id'] = user_id

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess['user'] = 'manager'
            sess['role'] = 'admin'
            sess['fullname'] = 'System Manager'
            sess['user_id'] = 1

    def test_get_queue_requires_authentication(self):
        res = self.client.get('/api/get-queue')
        self.assertEqual(res.status_code, 401)

    def test_booked_slots_requires_authentication(self):
        res = self.client.get('/api/booked-slots')
        self.assertEqual(res.status_code, 401)

    def test_customer_sees_own_booking_unmasked(self):
        rid = self._insert_reservation('SEC-0001', 'owner@example.com', 'Owner Customer')
        self._login_customer(99001, 'owner@example.com')

        res = self.client.get('/api/get-queue')
        self.assertEqual(res.status_code, 200)
        row = next(r for r in res.get_json()['data'] if r['id'] == rid)
        self.assertEqual(row['customer_name'], 'Owner Customer')
        self.assertEqual(row['email'], 'owner@example.com')
        self.assertEqual(row['plate_number'], 'SEC-0001')

    def test_customer_sees_other_customers_booking_masked(self):
        rid = self._insert_reservation('SEC-0002', 'other@example.com', 'Other Customer')
        self._login_customer(99001, 'owner@example.com')  # a different customer

        res = self.client.get('/api/get-queue')
        row = next(r for r in res.get_json()['data'] if r['id'] == rid)
        self.assertNotEqual(row['customer_name'], 'Other Customer')
        self.assertNotEqual(row['email'], 'other@example.com')
        self.assertNotEqual(row['plate_number'], 'SEC-0002')
        self.assertTrue(row['plate_number'].endswith('002'), "tail kept for the customer's own recognition")
        self.assertNotIn('user_id', row, "internal ownership key must not leak to the client")

    def test_staff_and_admin_see_everything_unmasked(self):
        rid = self._insert_reservation('SEC-0002', 'other@example.com', 'Other Customer')
        self._login_admin()

        res = self.client.get('/api/get-queue')
        row = next(r for r in res.get_json()['data'] if r['id'] == rid)
        self.assertEqual(row['customer_name'], 'Other Customer')
        self.assertEqual(row['plate_number'], 'SEC-0002')


class FeedbackConfidentialityTestCase(unittest.TestCase):
    """/api/get-feedbacks is an intentional public testimonials wall (every
    customer sees every review) - but the plate number is a more
    identifying, replayable piece of PII than a review comment, and only
    staff/admin actually need it to match a review back to a booking."""

    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self._clear_test_rows()

    def tearDown(self):
        self._clear_test_rows()

    def _clear_test_rows(self):
        conn = get_db_connection()
        conn.execute("DELETE FROM feedbacks WHERE plate_number = 'FB-SEC-001'")
        conn.commit()
        conn.close()

    def _insert_feedback(self):
        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO feedbacks (customer_name, plate_number, service_type, rating, comment)
            VALUES ('Reviewer Name', 'FB-SEC-001', 'Body Wash', 5, 'Great job')
            """
        )
        conn.commit()
        conn.close()

    def _login_customer(self):
        with self.client.session_transaction() as sess:
            sess['user'] = 'someone@example.com'
            sess['role'] = 'customer'
            sess['fullname'] = 'Someone'
            sess['user_id'] = 99003

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess['user'] = 'manager'
            sess['role'] = 'admin'
            sess['fullname'] = 'System Manager'
            sess['user_id'] = 1

    def test_customer_sees_masked_plate_on_public_feedback(self):
        self._insert_feedback()
        self._login_customer()

        res = self.client.get('/api/get-feedbacks')
        self.assertEqual(res.status_code, 200)
        row = next(r for r in res.get_json()['data'] if r['plate_number'].endswith('001'))
        self.assertNotEqual(row['plate_number'], 'FB-SEC-001')
        self.assertEqual(row['customer_name'], 'Reviewer Name', "the testimonial itself stays intact")

    def test_staff_and_admin_see_unmasked_plate_on_feedback(self):
        self._insert_feedback()
        self._login_admin()

        res = self.client.get('/api/get-feedbacks')
        row = next(r for r in res.get_json()['data'] if r['plate_number'] == 'FB-SEC-001')
        self.assertEqual(row['plate_number'], 'FB-SEC-001')


class PasswordPolicyTestCase(unittest.TestCase):
    """Two account-creation paths (self-registration and admin create-user)
    used to accept any non-empty password, including a single character -
    the self-service and admin-reset password flows already enforced a
    6-character minimum, this closes the gap for the other two."""

    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self._clear_test_rows()

    def tearDown(self):
        self._clear_test_rows()

    def _clear_test_rows(self):
        conn = get_db_connection()
        conn.execute("DELETE FROM users WHERE username IN ('shortpw@example.com', 'shortpwstaff')")
        conn.commit()
        conn.close()

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess['user'] = 'manager'
            sess['role'] = 'admin'
            sess['fullname'] = 'System Manager'
            sess['user_id'] = 1

    def test_self_registration_rejects_short_password(self):
        res = self.client.post('/register', json={
            'email': 'shortpw@example.com', 'password': '123', 'fullname': 'Short PW',
        })
        self.assertEqual(res.status_code, 400)

        conn = get_db_connection()
        row = conn.execute("SELECT 1 FROM users WHERE username = 'shortpw@example.com'").fetchone()
        conn.close()
        self.assertIsNone(row, "a rejected registration must not create an account")

    def test_admin_create_user_rejects_short_password(self):
        self._login_admin()
        res = self.client.post('/api/admin/create-user', json={
            'username': 'shortpwstaff', 'password': 'abc', 'role': 'staff',
        })
        self.assertEqual(res.status_code, 400)

        conn = get_db_connection()
        row = conn.execute("SELECT 1 FROM users WHERE username = 'shortpwstaff'").fetchone()
        conn.close()
        self.assertIsNone(row, "a rejected create-user must not create an account")


class SessionCookieHardeningTestCase(unittest.TestCase):
    """SESSION_COOKIE_SECURE/HTTPONLY/SAMESITE used to rely entirely on
    Flask's implicit defaults, undocumented in this codebase's own config."""

    def test_session_cookie_flags_are_explicitly_hardened(self):
        self.assertTrue(app.config['SESSION_COOKIE_HTTPONLY'])
        self.assertEqual(app.config['SESSION_COOKIE_SAMESITE'], 'Lax')
        # SECURE is env-controlled (off by default for local HTTP dev) -
        # just confirm it's a real boolean, not left unset/None.
        self.assertIn(app.config['SESSION_COOKIE_SECURE'], (True, False))


if __name__ == '__main__':
    unittest.main()
