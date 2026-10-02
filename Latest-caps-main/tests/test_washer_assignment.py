import unittest
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection

TEST_PLATES = ('WASH-0001', 'WASH-0002', 'WASH-0003', 'WASH-0004', 'WASH-0005')


class WasherAssignmentTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT service_id FROM services LIMIT 1")
        self.service_id = cursor.fetchone()[0]
        cursor.execute("SELECT user_id, COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' ORDER BY user_id LIMIT 1")
        row = cursor.fetchone()
        self.staff_user_id = row['user_id']
        self.staff_fullname = row['name']
        conn.close()

        self._attendance_ids = []
        self._clear_test_rows()
        # Starting a service now requires the staff member to actually be
        # clocked in (see test_start_service_requires_clock_in below for
        # the case this fixture is guarding against) - force it on for the
        # tests here that are about attribution, not attendance itself.
        self._force_staff_on_duty()

    def tearDown(self):
        self._clear_test_rows()
        if self._attendance_ids:
            conn = get_db_connection()
            conn.execute(
                f"DELETE FROM staff_attendance WHERE attendance_id IN ({','.join('?' * len(self._attendance_ids))})",
                self._attendance_ids,
            )
            conn.commit()
            conn.close()

    def _clear_test_rows(self):
        conn = get_db_connection()
        conn.execute(
            f"DELETE FROM reservations WHERE vehicle_plate_number IN ({','.join('?' * len(TEST_PLATES))})",
            TEST_PLATES,
        )
        conn.commit()
        conn.close()

    def _force_staff_on_duty(self, status='Present'):
        conn = get_db_connection()
        cursor = conn.cursor()
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute(
            "INSERT INTO staff_attendance (staff_name, date_str, status, time_in, time_out) VALUES (?, ?, ?, '08:00', '--:--')",
            (self.staff_fullname, today, status),
        )
        self._attendance_ids.append(cursor.lastrowid)
        conn.commit()
        conn.close()

    def _insert_waiting_reservation(self, plate, booking_date=None, booking_time=None):
        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO reservations (vehicle_plate_number, vehicle_type, service_id, queue_status, booking_date, booking_time)
            VALUES (?, 'Car', ?, 'waiting', ?, ?)
            """,
            (plate, self.service_id, booking_date, booking_time),
        )
        conn.commit()
        conn.close()

    def _login_staff(self):
        with self.client.session_transaction() as sess:
            sess['user'] = self.staff_fullname
            sess['role'] = 'staff'
            sess['fullname'] = self.staff_fullname
            sess['user_id'] = self.staff_user_id

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess['user'] = 'manager'
            sess['role'] = 'admin'
            sess['fullname'] = 'System Manager'
            sess['user_id'] = 1

    def test_staff_starting_service_records_assigned_staff(self):
        self._insert_waiting_reservation('WASH-0001')
        self._login_staff()

        res = self.client.post('/api/start-service', json={'plate_number': 'WASH-0001'})
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        row = conn.execute(
            "SELECT assigned_staff_id, started_at FROM reservations WHERE vehicle_plate_number = ?", ('WASH-0001',)
        ).fetchone()
        conn.close()
        self.assertEqual(row['assigned_staff_id'], self.staff_user_id)
        self.assertIsNotNone(row['started_at'], "started_at must be recorded so Speed can be computed from real work time, not queue wait time")

    def test_admin_starting_service_leaves_assignment_unattributed(self):
        """Admins aren't washers - no commission attribution should be
        invented just because an admin clicked the button."""
        self._insert_waiting_reservation('WASH-0002')
        self._login_admin()

        res = self.client.post('/api/start-service', json={'plate_number': 'WASH-0002'})
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        row = conn.execute(
            "SELECT assigned_staff_id FROM reservations WHERE vehicle_plate_number = ?", ('WASH-0002',)
        ).fetchone()
        conn.close()
        self.assertIsNone(row['assigned_staff_id'])

    def test_get_ready_returns_real_washer_name(self):
        """No more round-robin fabrication - /api/get-ready must return the
        actual staff member who started the job, or nothing at all. A
        staff completion needs admin's confirm-complete before it reaches
        the Ready list (see test_completion_confirmation.py for that
        workflow itself)."""
        self._insert_waiting_reservation('WASH-0003')
        self._login_staff()
        self.client.post('/api/start-service', json={'plate_number': 'WASH-0003'})

        res = self.client.post('/api/complete-service', json={'plate_number': 'WASH-0003'})
        self.assertEqual(res.status_code, 200)

        self._login_admin()
        res = self.client.post('/api/confirm-complete', json={'plate_number': 'WASH-0003'})
        self.assertEqual(res.status_code, 200)

        res = self.client.get('/api/get-ready')
        self.assertEqual(res.status_code, 200)
        matching = [row for row in res.get_json()['data'] if row['plate_number'] == 'WASH-0003']
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['washer'], self.staff_fullname)

    def test_get_ready_washer_is_null_when_unassigned(self):
        self._insert_waiting_reservation('WASH-0001')
        self._login_admin()
        self.client.post('/api/start-service', json={'plate_number': 'WASH-0001'})
        self.client.post('/api/complete-service', json={'plate_number': 'WASH-0001'})

        res = self.client.get('/api/get-ready')
        matching = [row for row in res.get_json()['data'] if row['plate_number'] == 'WASH-0001']
        self.assertEqual(len(matching), 1)
        self.assertIsNone(matching[0]['washer'])

    def test_restarting_an_already_started_job_does_not_reassign(self):
        """The UPDATE's WHERE clause only matches not-yet-started jobs, so
        a job once claimed by a real staff member can't be silently
        reassigned (e.g. to NULL) by a later admin action on the same
        plate."""
        self._insert_waiting_reservation('WASH-0002')
        self._login_staff()
        self.client.post('/api/start-service', json={'plate_number': 'WASH-0002'})

        self._login_admin()
        self.client.post('/api/start-service', json={'plate_number': 'WASH-0002'})

        conn = get_db_connection()
        row = conn.execute(
            "SELECT assigned_staff_id FROM reservations WHERE vehicle_plate_number = ?", ('WASH-0002',)
        ).fetchone()
        conn.close()
        self.assertEqual(row['assigned_staff_id'], self.staff_user_id)

    def test_start_service_requires_clock_in(self):
        """A staff account that hasn't been clocked in by admin today must
        not be able to start (and get attributed on) a job - this was a
        real gap: once attendance became admin-only, nothing actually
        enforced it was ever recorded, so an account could start jobs
        indefinitely without ever being marked present."""
        # Override setUp's on-duty fixture - a later attendance_id always
        # wins in the "latest row for today" resolution used elsewhere.
        self._force_staff_on_duty(status='Absent')
        self._insert_waiting_reservation('WASH-0001')
        self._login_staff()

        res = self.client.post('/api/start-service', json={'plate_number': 'WASH-0001'})
        self.assertEqual(res.status_code, 403)

        conn = get_db_connection()
        row = conn.execute(
            "SELECT queue_status, assigned_staff_id, started_at FROM reservations WHERE vehicle_plate_number = ?", ('WASH-0001',)
        ).fetchone()
        conn.close()
        self.assertEqual(row['queue_status'], 'waiting', "a blocked start must not change anything")
        self.assertIsNone(row['assigned_staff_id'])
        self.assertIsNone(row['started_at'])

    def test_start_service_blocked_for_staff_never_clocked_in_at_all(self):
        """Same as above but for a staff account with zero attendance
        history for today at all (not even an explicit Absent row) - the
        more common real-world case. Uses a brand-new throwaway staff
        account rather than the class's shared one, so "zero attendance
        rows" is guaranteed rather than depending on deleting whatever a
        real account happens to carry (real usage/demos can leave real
        attendance rows on the shared seeded accounts at any time)."""
        conn = get_db_connection()
        cursor = conn.cursor()
        temp_name = f"Temp Never Clocked In {datetime.now().timestamp()}"
        cursor.execute(
            "INSERT INTO users (username, password_hash, role, fullname) VALUES (?, 'x', 'staff', ?)",
            (temp_name, temp_name),
        )
        temp_user_id = cursor.lastrowid
        conn.commit()
        conn.close()

        try:
            self._insert_waiting_reservation('WASH-0001')
            with self.client.session_transaction() as sess:
                sess['user'] = temp_name
                sess['role'] = 'staff'
                sess['fullname'] = temp_name
                sess['user_id'] = temp_user_id

            res = self.client.post('/api/start-service', json={'plate_number': 'WASH-0001'})
            self.assertEqual(res.status_code, 403)
        finally:
            conn = get_db_connection()
            conn.execute("DELETE FROM users WHERE user_id = ?", (temp_user_id,))
            conn.commit()
            conn.close()

    def test_admin_can_start_service_without_being_clocked_in(self):
        """The clock-in requirement is specific to staff accounts being
        real washers - admin covering a shift isn't a washer and was never
        required to clock in."""
        self._insert_waiting_reservation('WASH-0002')
        self._login_admin()

        res = self.client.post('/api/start-service', json={'plate_number': 'WASH-0002'})
        self.assertEqual(res.status_code, 200)

    def test_starting_a_future_scheduled_booking_is_rejected(self):
        """A booking scheduled for tomorrow (or any other day but today)
        must not be startable early - real bug caught live: an admin was
        able to start a booking scheduled for the next day."""
        from datetime import timedelta
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        self._insert_waiting_reservation('WASH-0001', booking_date=tomorrow, booking_time='10:00 AM')
        self._login_admin()

        res = self.client.post('/api/start-service', json={'plate_number': 'WASH-0001'})
        self.assertEqual(res.status_code, 400)
        self.assertIn(tomorrow, res.get_json()['message'])

        conn = get_db_connection()
        row = conn.execute(
            "SELECT queue_status, started_at FROM reservations WHERE vehicle_plate_number = ?", ('WASH-0001',)
        ).fetchone()
        conn.close()
        self.assertEqual(row['queue_status'], 'waiting', "a blocked start must not change anything")
        self.assertIsNone(row['started_at'])

    def test_starting_a_past_scheduled_booking_is_also_rejected(self):
        """Same rule from the other side - a stale 'waiting' row scheduled
        for a day that's already passed (e.g. the no-show sweeper hasn't
        caught it yet) isn't "today" either."""
        from datetime import timedelta
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        self._insert_waiting_reservation('WASH-0002', booking_date=yesterday, booking_time='10:00 AM')
        self._login_admin()

        res = self.client.post('/api/start-service', json={'plate_number': 'WASH-0002'})
        self.assertEqual(res.status_code, 400)

    def test_starting_a_booking_scheduled_for_today_succeeds(self):
        today = datetime.now().strftime('%Y-%m-%d')
        self._insert_waiting_reservation('WASH-0003', booking_date=today, booking_time='10:00 AM')
        self._login_admin()

        res = self.client.post('/api/start-service', json={'plate_number': 'WASH-0003'})
        self.assertEqual(res.status_code, 200)

    def test_starting_a_walkin_with_no_scheduled_date_still_succeeds(self):
        """Walk-ins have no booking_date at all - nothing to check against,
        same exemption every other date-based rule in this codebase gives
        them (no-show sweep, past-date validation, business hours)."""
        self._insert_waiting_reservation('WASH-0001')
        self._login_admin()

        res = self.client.post('/api/start-service', json={'plate_number': 'WASH-0001'})
        self.assertEqual(res.status_code, 200)

    def test_second_car_cannot_start_in_an_already_occupied_bay(self):
        """Only one vehicle may actually be in_progress (or done and
        awaiting admin's sign-off) per bay at a time - booking itself is
        never blocked by capacity (see api_submit_booking), but starting
        work is where "wait your turn for the bay" is actually enforced.
        Both WASH-0004/WASH-0005 default to the same bay (Bay #1, the
        column default) since _insert_waiting_reservation doesn't set one."""
        self._insert_waiting_reservation('WASH-0004')
        self._insert_waiting_reservation('WASH-0005')
        self._login_admin()

        res = self.client.post('/api/start-service', json={'plate_number': 'WASH-0004'})
        self.assertEqual(res.status_code, 200)

        res = self.client.post('/api/start-service', json={'plate_number': 'WASH-0005'})
        self.assertEqual(res.status_code, 409)
        data = res.get_json()
        self.assertEqual(data['status'], 'error')
        self.assertIn('occupied', data['message'].lower())

        conn = get_db_connection()
        row = conn.execute(
            "SELECT queue_status FROM reservations WHERE vehicle_plate_number = ?", ('WASH-0005',)
        ).fetchone()
        conn.close()
        self.assertEqual(row['queue_status'], 'waiting', "a blocked start must leave the booking waiting in the queue, not lost")

    def test_bay_frees_up_once_the_occupying_car_reaches_ready(self):
        """Once the car occupying a bay is marked 'ready' (picked up), the
        bay is free again and the next queued car for it can start."""
        self._insert_waiting_reservation('WASH-0004')
        self._insert_waiting_reservation('WASH-0005')
        self._login_admin()

        self.client.post('/api/start-service', json={'plate_number': 'WASH-0004'})
        blocked = self.client.post('/api/start-service', json={'plate_number': 'WASH-0005'})
        self.assertEqual(blocked.status_code, 409)

        # Admin completes WASH-0004 directly -> goes straight to 'ready'
        # (no pending_confirmation hand-off needed for an admin-completed job).
        res = self.client.post('/api/complete-service', json={'plate_number': 'WASH-0004'})
        self.assertEqual(res.status_code, 200)

        res = self.client.post('/api/start-service', json={'plate_number': 'WASH-0005'})
        self.assertEqual(res.status_code, 200, "the bay should be free now that WASH-0004 is 'ready', not still occupying it")


if __name__ == '__main__':
    unittest.main()
