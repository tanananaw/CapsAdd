import unittest
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection

PLATE = 'DUP-0001'


class QueueActionsByReservationIdTestCase(unittest.TestCase):
    """Queue actions target exactly one reservation - by reservation_id, or
    (older callers) a plate's most recent matching booking - never every
    booking that shares the plate."""

    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess.update(user='manager', role='admin', fullname='System Manager', user_id=1)
        self._clear()
        conn = get_db_connection()
        service_id = conn.execute("SELECT service_id FROM services LIMIT 1").fetchone()[0]
        self.ids = []
        for _ in range(2):
            cur = conn.execute(
                "INSERT INTO reservations (vehicle_plate_number, vehicle_type, service_id, queue_status) VALUES (?, 'Medium', ?, 'waiting')",
                (PLATE, service_id),
            )
            self.ids.append(cur.lastrowid)
        conn.commit()
        conn.close()

    def tearDown(self):
        self._clear()

    def _clear(self):
        conn = get_db_connection()
        conn.execute("DELETE FROM notifications WHERE related_reservation_id IN (SELECT reservation_id FROM reservations WHERE vehicle_plate_number = ?)", (PLATE,))
        conn.execute("DELETE FROM reservations WHERE vehicle_plate_number = ?", (PLATE,))
        conn.commit()
        conn.close()

    def _statuses(self):
        conn = get_db_connection()
        rows = dict(conn.execute("SELECT reservation_id, queue_status FROM reservations WHERE vehicle_plate_number = ?", (PLATE,)).fetchall())
        conn.close()
        return [rows[i] for i in self.ids]

    def test_cancel_by_id_leaves_the_other_booking_for_the_same_plate(self):
        res = self.client.post('/api/cancel-booking', json={'reservation_id': self.ids[0]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self._statuses(), ['cancelled', 'waiting'])

    def test_plate_only_caller_acts_on_the_most_recent_booking_only(self):
        res = self.client.post('/api/cancel-booking', json={'plate_number': PLATE})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self._statuses(), ['waiting', 'cancelled'])

    def test_unknown_or_wrong_state_booking_is_404(self):
        self.assertEqual(self.client.post('/api/cancel-booking', json={'reservation_id': 999999}).status_code, 404)
        self.assertEqual(self.client.post('/api/confirm-complete', json={'reservation_id': self.ids[0]}).status_code, 404)

    def test_missing_booking_reference_is_400(self):
        self.assertEqual(self.client.post('/api/archive-ready', json={}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
