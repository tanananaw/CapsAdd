import unittest
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection

OLD_DAY, STAFFED_DAY, UNSTAFFED_DAY, LATER_STAFFED_DAY = '2027-07-01', '2027-07-05', '2027-07-06', '2027-07-08'
PLATE, OTHER_PLATE = 'MYB-0001', 'MYB-0002'


class MyBookingsTestCase(unittest.TestCase):
    """Customers see and move their own bookings (/my-bookings,
    /api/reschedule-booking) under the same rules as a new booking."""

    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess.update(user='customer_user', role='customer', fullname='Default Customer', user_id=2)
        conn = get_db_connection()
        service_id = conn.execute("SELECT service_id FROM services WHERE is_active = 1 LIMIT 1").fetchone()[0]
        staff_name = conn.execute("SELECT COALESCE(fullname, username) FROM users WHERE role = 'staff' LIMIT 1").fetchone()[0]
        conn.close()
        self._clear()
        conn = get_db_connection()
        for plate, user_id in ((PLATE, 2), (OTHER_PLATE, None)):
            conn.execute(
                """INSERT INTO reservations (user_id, customer_email, vehicle_plate_number, vehicle_type, service_id,
                                             queue_status, booking_date, booking_time, assigned_bay)
                   VALUES (?, ?, ?, 'Medium', ?, 'waiting', ?, '10:00 AM', 'Bay #1')""",
                (user_id, None if user_id else 'someone@example.com', plate, service_id, OLD_DAY),
            )
        for day in (STAFFED_DAY, LATER_STAFFED_DAY):
            conn.execute("INSERT INTO staff_shifts (staff_name, shift_date, created_by) VALUES (?, ?, 'test-mybookings')", (staff_name, day))
        conn.commit()
        self.booking_id = conn.execute("SELECT reservation_id FROM reservations WHERE vehicle_plate_number = ?", (PLATE,)).fetchone()[0]
        self.other_id = conn.execute("SELECT reservation_id FROM reservations WHERE vehicle_plate_number = ?", (OTHER_PLATE,)).fetchone()[0]
        conn.close()

    def tearDown(self):
        self._clear()

    def _clear(self):
        conn = get_db_connection()
        conn.execute("DELETE FROM notifications WHERE related_reservation_id IN (SELECT reservation_id FROM reservations WHERE vehicle_plate_number IN (?, ?))", (PLATE, OTHER_PLATE))
        conn.execute("DELETE FROM reservations WHERE vehicle_plate_number IN (?, ?)", (PLATE, OTHER_PLATE))
        conn.execute("DELETE FROM staff_shifts WHERE created_by = 'test-mybookings'")
        conn.commit()
        conn.close()

    def _reschedule(self, reservation_id, day, time='10:00 AM'):
        return self.client.post('/api/reschedule-booking', json={'reservation_id': reservation_id, 'booking_date': day, 'booking_time': time})

    def test_page_is_customer_only(self):
        self.assertEqual(self.client.get('/my-bookings').status_code, 200)
        with self.client.session_transaction() as sess:
            sess.update(user='manager', role='admin', fullname='System Manager', user_id=1)
        self.assertEqual(self.client.get('/my-bookings').status_code, 302)

    def test_reschedule_to_a_staffed_day_records_where_it_came_from(self):
        res = self._reschedule(self.booking_id, STAFFED_DAY, '02:00 PM')
        self.assertEqual(res.status_code, 200)
        booking = [b for b in self.client.get('/api/my-bookings').get_json()['data'] if b['id'] == self.booking_id][0]
        self.assertEqual((booking['booking_date'], booking['booking_time']), (STAFFED_DAY, '02:00 PM'))
        self.assertEqual(booking['rescheduled_from'], f'{OLD_DAY} 10:00 AM')
        self.assertEqual(booking['reschedule_reason'], 'customer')

    def test_unstaffed_day_is_refused_with_a_suggestion(self):
        res = self._reschedule(self.booking_id, UNSTAFFED_DAY)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()['suggested_date'], LATER_STAFFED_DAY)

    def test_cannot_reschedule_someone_elses_booking(self):
        self.assertEqual(self._reschedule(self.other_id, STAFFED_DAY).status_code, 403)

    def test_outside_business_hours_is_refused(self):
        self.assertEqual(self._reschedule(self.booking_id, STAFFED_DAY, '11:00 PM').status_code, 400)


if __name__ == '__main__':
    unittest.main()
