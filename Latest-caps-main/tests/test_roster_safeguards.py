import unittest
import os
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection
from shift_reminder import process_missing_rostered_staff

TODAY = datetime.now().strftime('%Y-%m-%d')
AFTER_CUTOFF = datetime.now().replace(hour=9, minute=0)
BEFORE_CUTOFF = datetime.now().replace(hour=8, minute=0)
UNSTAFFED_DAY = '2027-09-01'
ALERT_TITLE = 'Rostered Staff Not Clocked In'


class RosterSafeguardsTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess.update(user='manager', role='admin', fullname='System Manager', user_id=1)
        conn = get_db_connection()
        row = conn.execute("SELECT user_id, COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' ORDER BY user_id LIMIT 1").fetchone()
        conn.close()
        self.staff_id, self.staff_name = row['user_id'], row['name']
        self._clear()

    def tearDown(self):
        self._clear()

    def _clear(self):
        conn = get_db_connection()
        conn.execute("DELETE FROM staff_shifts WHERE staff_user_id = ? AND (created_by = 'manager' OR created_by = 'test-safeguards')", (self.staff_id,))
        conn.execute("DELETE FROM staff_attendance WHERE time_in = 'SG-test'")
        conn.execute("DELETE FROM notifications WHERE title = ?", (ALERT_TITLE,))
        conn.execute("DELETE FROM reservations WHERE vehicle_plate_number = 'SG-0001'")
        conn.commit()
        conn.close()

    def _alerts(self):
        conn = get_db_connection()
        n = conn.execute("SELECT COUNT(*) FROM notifications WHERE title = ? AND user_id = 1", (ALERT_TITLE,)).fetchone()[0]
        conn.close()
        return n

    def _roster_today(self):
        conn = get_db_connection()
        conn.execute("INSERT INTO staff_shifts (staff_name, shift_date, created_by) VALUES (?, ?, 'test-safeguards')", (self.staff_name, TODAY))
        conn.commit()
        conn.close()

    def test_repeat_weekly_schedules_the_following_weeks(self):
        start = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
        res = self.client.post('/api/staff-shifts', json={'staff_user_id': self.staff_id, 'shift_date': start, 'repeat_weeks': 3})
        self.assertEqual(res.status_code, 200)
        expected = [(datetime.strptime(start, '%Y-%m-%d') + timedelta(weeks=w)).strftime('%Y-%m-%d') for w in range(4)]
        self.assertEqual(res.get_json()['dates'], expected)
        conn = get_db_connection()
        stored = [r[0] for r in conn.execute("SELECT shift_date FROM staff_shifts WHERE staff_user_id = ? AND shift_date >= ? ORDER BY shift_date", (self.staff_id, start))]
        conn.close()
        self.assertEqual(stored, expected)

    def test_unstaffed_booking_days_lists_booked_days_without_roster(self):
        conn = get_db_connection()
        service_id = conn.execute("SELECT service_id FROM services LIMIT 1").fetchone()[0]
        conn.execute("INSERT INTO reservations (vehicle_plate_number, vehicle_type, service_id, queue_status, booking_date, booking_time) VALUES ('SG-0001', 'Medium', ?, 'waiting', ?, '10:00 AM')",
                     (service_id, UNSTAFFED_DAY))
        conn.commit()
        conn.close()
        days = self.client.get('/api/unstaffed-booking-days').get_json()['data']
        self.assertIn({'date': UNSTAFFED_DAY, 'bookings': 1}, days)

    def test_missing_rostered_staff_alerts_admin_once(self):
        self._roster_today()
        process_missing_rostered_staff(BEFORE_CUTOFF)
        self.assertEqual(self._alerts(), 0, "no alert before the cutoff")
        process_missing_rostered_staff(AFTER_CUTOFF)
        process_missing_rostered_staff(AFTER_CUTOFF)
        self.assertEqual(self._alerts(), 1, "exactly one alert per shift")

    def test_no_alert_when_staff_clocked_in(self):
        self._roster_today()
        conn = get_db_connection()
        conn.execute("INSERT INTO staff_attendance (staff_name, date_str, status, time_in, time_out) VALUES (?, ?, 'Present', 'SG-test', '--:--')", (self.staff_name, TODAY))
        conn.commit()
        conn.close()
        process_missing_rostered_staff(AFTER_CUTOFF)
        self.assertEqual(self._alerts(), 0)


if __name__ == '__main__':
    unittest.main()
