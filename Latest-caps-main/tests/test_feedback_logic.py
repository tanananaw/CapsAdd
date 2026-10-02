import unittest
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection

TEST_PLATES = ('FDB-0001', 'FDB-0002', 'FDB-0003')


class FeedbackLogicTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self._clear_test_rows()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT service_id FROM services LIMIT 1")
        service_id = cursor.fetchone()[0]

        # A completed reservation owned by customer_user (user_id 2, the
        # session set up below) - the one a review should be allowed on.
        cursor.execute("""
            INSERT INTO reservations (user_id, customer_name, vehicle_plate_number, vehicle_type, service_id, queue_status, completed_at)
            VALUES (2, 'Default Customer', ?, 'Car', ?, 'completed', '2026-01-01 10:00:00')
        """, (TEST_PLATES[0], service_id))
        self.own_completed_id = cursor.lastrowid

        # Still-waiting reservation owned by the same customer - not eligible.
        cursor.execute("""
            INSERT INTO reservations (user_id, customer_name, vehicle_plate_number, vehicle_type, service_id, queue_status)
            VALUES (2, 'Default Customer', ?, 'Car', ?, 'waiting')
        """, (TEST_PLATES[1], service_id))
        self.own_waiting_id = cursor.lastrowid

        # Completed reservation owned by a different user (user_id 1, the
        # seeded 'manager' account) - customer_user must not be able to
        # review it.
        cursor.execute("""
            INSERT INTO reservations (user_id, customer_name, vehicle_plate_number, vehicle_type, service_id, queue_status, completed_at)
            VALUES (1, 'Someone Else', ?, 'Car', ?, 'completed', '2026-01-01 10:00:00')
        """, (TEST_PLATES[2], service_id))
        self.other_completed_id = cursor.lastrowid

        conn.commit()
        conn.close()

        with self.client.session_transaction() as sess:
            sess['user'] = 'customer_user'
            sess['role'] = 'customer'
            sess['fullname'] = 'Default Customer'
            sess['user_id'] = 2

    def tearDown(self):
        self._clear_test_rows()

    def _clear_test_rows(self):
        conn = get_db_connection()
        conn.execute(
            f"DELETE FROM feedbacks WHERE plate_number IN ({','.join('?' * len(TEST_PLATES))})",
            TEST_PLATES,
        )
        conn.execute(
            f"DELETE FROM reservations WHERE vehicle_plate_number IN ({','.join('?' * len(TEST_PLATES))})",
            TEST_PLATES,
        )
        conn.commit()
        conn.close()

    def test_feedback_on_own_completed_reservation_succeeds(self):
        res = self.client.post('/api/submit-feedback', json={
            'reservation_id': self.own_completed_id,
            'rating': 5,
            'comment': 'Great service!',
        })
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        row = conn.execute(
            "SELECT reservation_id, user_id, plate_number, service_type FROM feedbacks WHERE reservation_id = ?",
            (self.own_completed_id,),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "feedback row should be linked to the reservation via reservation_id")
        self.assertEqual(row['user_id'], 2)
        self.assertEqual(row['plate_number'], TEST_PLATES[0])

    def test_feedback_on_not_yet_completed_reservation_rejected(self):
        """A booking still in the queue hasn't happened yet - nothing to review."""
        res = self.client.post('/api/submit-feedback', json={
            'reservation_id': self.own_waiting_id,
            'rating': 5,
        })
        self.assertEqual(res.status_code, 400)

    def test_feedback_on_someone_elses_reservation_rejected(self):
        """A logged-in customer must not be able to review another customer's visit."""
        res = self.client.post('/api/submit-feedback', json={
            'reservation_id': self.other_completed_id,
            'rating': 5,
        })
        self.assertEqual(res.status_code, 403)

    def test_duplicate_feedback_on_same_reservation_rejected(self):
        """One review per reservation - closes the unlimited-fake-feedback hole."""
        first = self.client.post('/api/submit-feedback', json={
            'reservation_id': self.own_completed_id,
            'rating': 4,
        })
        self.assertEqual(first.status_code, 200)

        second = self.client.post('/api/submit-feedback', json={
            'reservation_id': self.own_completed_id,
            'rating': 2,
        })
        self.assertEqual(second.status_code, 409)

    def test_feedback_on_nonexistent_reservation_rejected(self):
        res = self.client.post('/api/submit-feedback', json={
            'reservation_id': 999999,
            'rating': 5,
        })
        self.assertEqual(res.status_code, 404)


if __name__ == '__main__':
    unittest.main()
