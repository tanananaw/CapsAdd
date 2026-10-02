import unittest
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection
from utils import MAX_BOOKINGS_PER_BAY_PER_HOUR

DAY, UNSTAFFED_DAY, NEXT_DAY, LATER_DAY = '2027-05-03', '2027-05-01', '2027-05-02', '2027-05-04'
PLATE_PREFIX = 'CAP-'


class BayCapacityTestCase(unittest.TestCase):
    """A bay takes at most MAX_BOOKINGS_PER_BAY_PER_HOUR scheduled bookings
    per hourly slot; reschedule proposals skip full slots."""

    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        conn = get_db_connection()
        row = conn.execute(
            "SELECT s.service_id, s.default_bay FROM services s JOIN service_prices p ON p.service_id = s.service_id "
            "WHERE s.is_active = 1 AND p.vehicle_type = 'Medium' ORDER BY s.service_id LIMIT 1"
        ).fetchone()
        self.service_id, self.bay = row['service_id'], row['default_bay']
        self.staff_name = conn.execute("SELECT COALESCE(fullname, username) FROM users WHERE role = 'staff' LIMIT 1").fetchone()[0]
        conn.close()
        self._clear()
        for day in (DAY, NEXT_DAY, LATER_DAY):
            self._roster(day)

    def tearDown(self):
        self._clear()

    def _clear(self):
        conn = get_db_connection()
        conn.execute("DELETE FROM reservations WHERE vehicle_plate_number LIKE ?", (PLATE_PREFIX + '%',))
        conn.execute("DELETE FROM staff_shifts WHERE created_by = 'test-capacity'")
        conn.commit()
        conn.close()

    def _roster(self, day):
        conn = get_db_connection()
        conn.execute("INSERT INTO staff_shifts (staff_name, shift_date, created_by) VALUES (?, ?, 'test-capacity')", (self.staff_name, day))
        conn.commit()
        conn.close()

    def _login(self, role):
        with self.client.session_transaction() as sess:
            if role == 'customer':
                sess.update(user='customer_user', role='customer', fullname='Default Customer', user_id=2)
            else:
                sess.update(user='manager', role='admin', fullname='System Manager', user_id=1)

    def _book(self, n, day):
        return self.client.post('/api/submit-booking', json={
            'plate_number': f'{PLATE_PREFIX}{n:04d}', 'vehicle_type': 'Medium', 'service_id': self.service_id,
            'booking_date': day, 'booking_time': '10:00 AM',
        })

    def _fill(self, day):
        self._login('admin')
        for n in range(MAX_BOOKINGS_PER_BAY_PER_HOUR):
            self.assertEqual(self._book(n + (100 if day == NEXT_DAY else 0), day).status_code, 200)

    def test_slot_refuses_booking_beyond_capacity(self):
        self._fill(DAY)
        res = self._book(99, DAY)
        self.assertEqual(res.status_code, 400)
        self.assertIn('fully booked', res.get_json()['message'])

    def test_booked_slots_reports_counts_and_capacity(self):
        self._fill(DAY)
        data = self.client.get(f'/api/booked-slots?date={DAY}').get_json()
        self.assertEqual(data['capacity'], MAX_BOOKINGS_PER_BAY_PER_HOUR)
        slot = [s for s in data['data'] if s['booking_time'] == '10:00 AM' and s['assigned_bay'] == self.bay][0]
        self.assertEqual(slot['count'], MAX_BOOKINGS_PER_BAY_PER_HOUR)

    def test_reschedule_proposal_skips_a_full_day(self):
        self._fill(NEXT_DAY)
        self._login('customer')
        res = self._book(50, UNSTAFFED_DAY)
        data = res.get_json()
        self.assertEqual(data['status'], 'reschedule_required')
        self.assertEqual(data['proposed_date'], DAY, "the staffed-but-full day is skipped")


if __name__ == '__main__':
    unittest.main()
