import unittest
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection

TEST_PLATES = ('HIST-0001', 'HIST-0002', 'HIST-0003')


class HistoryTestCase(unittest.TestCase):
    """/api/get-history is the single automatic record of everything that
    finished - completed (archived from Ready) or cancelled - so nothing
    requires a separate manual step to show up there."""

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
        with self.client.session_transaction() as sess:
            sess['user'] = 'manager'
            sess['role'] = 'admin'
            sess['fullname'] = 'System Manager'
            sess['user_id'] = 1

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

    def _insert_reservation(self, plate, status, completed_at=None, cancelled_at=None, cancel_reason=None):
        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO reservations (
                vehicle_plate_number, vehicle_type, service_id, queue_status,
                completed_at, cancelled_at, cancel_reason
            ) VALUES (?, 'Car', ?, ?, ?, ?, ?)
            """,
            (plate, self.service_id, status, completed_at, cancelled_at, cancel_reason),
        )
        conn.commit()
        conn.close()

    def _find(self, data, plate):
        return next((row for row in data if row['plate_number'] == plate), None)

    def test_completed_booking_appears_in_history(self):
        self._insert_reservation('HIST-0001', 'completed', completed_at='2026-01-01 10:00:00')
        res = self.client.get('/api/get-history')
        self.assertEqual(res.status_code, 200)
        row = self._find(res.get_json()['data'], 'HIST-0001')
        self.assertIsNotNone(row)
        self.assertEqual(row['status'], 'completed')

    def test_cancelled_booking_appears_in_history_automatically(self):
        """A cancelled booking must show up in History without any extra
        archiving step - cancelling it is the terminal action."""
        self._insert_reservation(
            'HIST-0002', 'cancelled',
            cancelled_at='2026-01-01 11:00:00', cancel_reason='no_show',
        )
        res = self.client.get('/api/get-history')
        self.assertEqual(res.status_code, 200)
        row = self._find(res.get_json()['data'], 'HIST-0002')
        self.assertIsNotNone(row)
        self.assertEqual(row['status'], 'cancelled')
        self.assertEqual(row['cancel_reason'], 'no_show')

    def test_still_active_booking_excluded_from_history(self):
        self._insert_reservation('HIST-0003', 'in_progress')
        res = self.client.get('/api/get-history')
        self.assertIsNone(self._find(res.get_json()['data'], 'HIST-0003'))

    def test_history_ordered_newest_outcome_first(self):
        """Completed and cancelled entries are arranged together by
        whichever timestamp actually closed the booking out, not two
        separate lists stitched together in insertion order."""
        self._insert_reservation('HIST-0001', 'completed', completed_at='2026-01-01 09:00:00')
        self._insert_reservation('HIST-0002', 'cancelled', cancelled_at='2026-01-01 15:00:00', cancel_reason='customer_request')
        res = self.client.get('/api/get-history')
        data = res.get_json()['data']
        idx1 = next(i for i, row in enumerate(data) if row['plate_number'] == 'HIST-0001')
        idx2 = next(i for i, row in enumerate(data) if row['plate_number'] == 'HIST-0002')
        # HIST-0002 was cancelled later (15:00) than HIST-0001 completed (09:00),
        # so it must sort ahead (newest first).
        self.assertLess(idx2, idx1)

    def test_requires_authentication(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        res = self.client.get('/api/get-history')
        self.assertEqual(res.status_code, 401)


if __name__ == '__main__':
    unittest.main()
