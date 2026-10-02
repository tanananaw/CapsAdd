import unittest
import os
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection
from no_show import process_no_show_bookings, staff_were_absent

PLATE = 'ABS-0001'
YESTERDAY = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
TODAY = datetime.now().strftime('%Y-%m-%d')
IN_THREE_DAYS = (datetime.now() + timedelta(days=3)).strftime('%Y-%m-%d')


class StaffAbsenceTestCase(unittest.TestCase):
    """A booking still 'waiting' past its 30-minute grace period is only a
    customer no-show if staff were actually there. With no staff on duty
    it's the shop's absence: moved to the next day with available rostered
    staff, or - with none rostered ahead - cancelled as 'staff_absent'.
    Test bookings carry no customer_email, so no real email is sent."""

    def setUp(self):
        conn = get_db_connection()
        self.service_id = conn.execute("SELECT service_id FROM services WHERE is_active = 1 LIMIT 1").fetchone()['service_id']
        self.staff_name = conn.execute(
            "SELECT COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' ORDER BY user_id LIMIT 1"
        ).fetchone()['name']
        conn.close()
        self._clear()

    def tearDown(self):
        self._clear()

    def _clear(self):
        conn = get_db_connection()
        conn.execute("DELETE FROM notifications WHERE related_reservation_id IN (SELECT reservation_id FROM reservations WHERE vehicle_plate_number = ?)", (PLATE,))
        conn.execute("DELETE FROM reservations WHERE vehicle_plate_number = ?", (PLATE,))
        conn.execute("DELETE FROM staff_shifts WHERE created_by = 'test-absence'")
        conn.execute("DELETE FROM staff_attendance WHERE time_in = 'test-absence'")
        conn.commit()
        conn.close()

    def _book_yesterday(self):
        conn = get_db_connection()
        cur = conn.execute(
            """INSERT INTO reservations (user_id, vehicle_plate_number, vehicle_type, service_id, queue_status,
                                         booking_date, booking_time)
               VALUES (2, ?, 'Medium', ?, 'waiting', ?, '10:00 AM')""",
            (PLATE, self.service_id, YESTERDAY),
        )
        conn.commit()
        conn.close()
        return cur.lastrowid

    def _attend(self, day):
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO staff_attendance (staff_name, date_str, status, time_in, time_out) VALUES (?, ?, 'Present', 'test-absence', '--:--')",
            (self.staff_name, day),
        )
        conn.commit()
        conn.close()

    def _roster(self, day):
        conn = get_db_connection()
        conn.execute("INSERT INTO staff_shifts (staff_name, shift_date, created_by) VALUES (?, ?, 'test-absence')", (self.staff_name, day))
        conn.commit()
        conn.close()

    def _booking(self):
        conn = get_db_connection()
        row = conn.execute(
            "SELECT reservation_id, queue_status, booking_date, cancel_reason, rescheduled_from, reschedule_reason FROM reservations WHERE vehicle_plate_number = ?",
            (PLATE,),
        ).fetchone()
        titles = [r['title'] for r in conn.execute(
            "SELECT title FROM notifications WHERE user_id = 2 AND related_reservation_id = ?", (row['reservation_id'],)
        )]
        conn.close()
        return row, titles

    def _sweep(self):
        with app.app_context():
            process_no_show_bookings()

    def test_no_staff_that_day_reschedules_to_next_staffed_day(self):
        self._book_yesterday()
        self._roster(IN_THREE_DAYS)
        self._sweep()
        row, titles = self._booking()
        self.assertEqual(row['queue_status'], 'waiting')
        self.assertEqual(row['booking_date'], IN_THREE_DAYS)
        self.assertEqual(row['rescheduled_from'], f'{YESTERDAY} 10:00 AM')
        self.assertEqual(row['reschedule_reason'], 'staff_absent')
        self.assertIn('Booking Rescheduled', titles)

    def test_no_staff_and_none_rostered_ahead_cancels_as_staff_absent(self):
        self._book_yesterday()
        self._sweep()
        row, titles = self._booking()
        self.assertEqual(row['queue_status'], 'cancelled')
        self.assertEqual(row['cancel_reason'], 'staff_absent')
        self.assertIn('Booking Cancelled', titles)

    def test_staff_present_is_still_a_customer_no_show(self):
        self._book_yesterday()
        self._attend(YESTERDAY)
        self._roster(IN_THREE_DAYS)
        self._sweep()
        row, _ = self._booking()
        self.assertEqual(row['queue_status'], 'cancelled')
        self.assertEqual(row['cancel_reason'], 'no_show')

    def test_today_counts_as_absent_only_while_nobody_is_on_duty(self):
        now = datetime.now()
        self.assertTrue(staff_were_absent(TODAY, now))
        self._attend(TODAY)
        self.assertFalse(staff_were_absent(TODAY, now))


if __name__ == '__main__':
    unittest.main()
