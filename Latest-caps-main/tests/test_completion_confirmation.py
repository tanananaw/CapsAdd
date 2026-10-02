import unittest
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection

TEST_PLATES = ('CONF-0001', 'CONF-0002', 'CONF-0003', 'CONF-0004', 'CONF-0005')


class CompletionConfirmationTestCase(unittest.TestCase):
    """Staff marking a job done no longer sends it straight to the Ready
    list - it waits in 'pending_confirmation' until admin confirms it (or
    sends it back). Admin completing a job directly is unaffected - there's
    no one else for admin to get sign-off from."""

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
        # Starting a service now requires the staff member to be clocked
        # in - these tests are about the completion/confirmation workflow,
        # not attendance, so force it on.
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

    def _force_staff_on_duty(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute(
            "INSERT INTO staff_attendance (staff_name, date_str, status, time_in, time_out) VALUES (?, ?, 'Present', '08:00', '--:--')",
            (self.staff_fullname, today),
        )
        self._attendance_ids.append(cursor.lastrowid)
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

    def _insert_waiting_reservation(self, plate):
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO reservations (vehicle_plate_number, vehicle_type, service_id, queue_status) VALUES (?, 'Car', ?, 'waiting')",
            (plate, self.service_id),
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

    def _status_of(self, plate):
        conn = get_db_connection()
        row = conn.execute(
            "SELECT queue_status, completed_at FROM reservations WHERE vehicle_plate_number = ?", (plate,)
        ).fetchone()
        conn.close()
        return row

    def test_staff_completing_job_sets_pending_confirmation_not_ready(self):
        self._insert_waiting_reservation('CONF-0001')
        self._login_staff()
        self.client.post('/api/start-service', json={'plate_number': 'CONF-0001'})

        res = self.client.post('/api/complete-service', json={'plate_number': 'CONF-0001'})
        self.assertEqual(res.status_code, 200)

        row = self._status_of('CONF-0001')
        self.assertEqual(row['queue_status'], 'pending_confirmation')
        self.assertIsNotNone(row['completed_at'], "the real finish time should still be recorded immediately")

    def test_admin_completing_job_goes_straight_to_ready(self):
        self._insert_waiting_reservation('CONF-0002')
        self._login_admin()
        self.client.post('/api/start-service', json={'plate_number': 'CONF-0002'})

        res = self.client.post('/api/complete-service', json={'plate_number': 'CONF-0002'})
        self.assertEqual(res.status_code, 200)

        row = self._status_of('CONF-0002')
        self.assertEqual(row['queue_status'], 'ready')

    def test_pending_confirmation_excluded_from_ready_list_until_confirmed(self):
        self._insert_waiting_reservation('CONF-0001')
        self._login_staff()
        self.client.post('/api/start-service', json={'plate_number': 'CONF-0001'})
        self.client.post('/api/complete-service', json={'plate_number': 'CONF-0001'})

        res = self.client.get('/api/get-ready')
        matching = [r for r in res.get_json()['data'] if r['plate_number'] == 'CONF-0001']
        self.assertEqual(matching, [])

    def test_pending_confirmation_still_visible_in_live_queue(self):
        """The car stays visible where staff/admin are already looking
        (the live bay view), not hidden until confirmed."""
        self._insert_waiting_reservation('CONF-0001')
        self._login_staff()
        self.client.post('/api/start-service', json={'plate_number': 'CONF-0001'})
        self.client.post('/api/complete-service', json={'plate_number': 'CONF-0001'})

        res = self.client.get('/api/get-queue')
        matching = [r for r in res.get_json()['data'] if r['plate_number'] == 'CONF-0001']
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['status'], 'pending_confirmation')
        self.assertEqual(matching[0]['washer'], self.staff_fullname)

    def test_get_queue_includes_started_at(self):
        self._insert_waiting_reservation('CONF-0003')
        self._login_staff()
        self.client.post('/api/start-service', json={'plate_number': 'CONF-0003'})

        res = self.client.get('/api/get-queue')
        matching = [r for r in res.get_json()['data'] if r['plate_number'] == 'CONF-0003']
        self.assertEqual(len(matching), 1)
        self.assertIsNotNone(matching[0]['started_at'])

    def test_admin_confirm_moves_pending_to_ready(self):
        self._insert_waiting_reservation('CONF-0001')
        self._login_staff()
        self.client.post('/api/start-service', json={'plate_number': 'CONF-0001'})
        self.client.post('/api/complete-service', json={'plate_number': 'CONF-0001'})

        self._login_admin()
        res = self.client.post('/api/confirm-complete', json={'plate_number': 'CONF-0001'})
        self.assertEqual(res.status_code, 200)

        row = self._status_of('CONF-0001')
        self.assertEqual(row['queue_status'], 'ready')

        res = self.client.get('/api/get-ready')
        matching = [r for r in res.get_json()['data'] if r['plate_number'] == 'CONF-0001']
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['washer'], self.staff_fullname)

    def test_staff_cannot_confirm_completion(self):
        self._insert_waiting_reservation('CONF-0001')
        self._login_staff()
        self.client.post('/api/start-service', json={'plate_number': 'CONF-0001'})
        self.client.post('/api/complete-service', json={'plate_number': 'CONF-0001'})

        res = self.client.post('/api/confirm-complete', json={'plate_number': 'CONF-0001'})
        self.assertEqual(res.status_code, 403)

        row = self._status_of('CONF-0001')
        self.assertEqual(row['queue_status'], 'pending_confirmation', "a forbidden confirm must not change anything")

    def test_admin_reject_sends_job_back_to_in_progress(self):
        self._insert_waiting_reservation('CONF-0004')
        self._login_staff()
        self.client.post('/api/start-service', json={'plate_number': 'CONF-0004'})
        self.client.post('/api/complete-service', json={'plate_number': 'CONF-0004'})

        self._login_admin()
        res = self.client.post('/api/reject-complete', json={'plate_number': 'CONF-0004'})
        self.assertEqual(res.status_code, 200)

        row = self._status_of('CONF-0004')
        self.assertEqual(row['queue_status'], 'in_progress')
        self.assertIsNone(row['completed_at'], "no longer complete - it was sent back")

        res = self.client.get('/api/get-ready')
        matching = [r for r in res.get_json()['data'] if r['plate_number'] == 'CONF-0004']
        self.assertEqual(matching, [])

    def test_rejected_job_can_be_completed_again(self):
        """The same staff member picks the job back up and can mark it
        done again - the reject isn't a dead end."""
        self._insert_waiting_reservation('CONF-0004')
        self._login_staff()
        self.client.post('/api/start-service', json={'plate_number': 'CONF-0004'})
        self.client.post('/api/complete-service', json={'plate_number': 'CONF-0004'})

        self._login_admin()
        self.client.post('/api/reject-complete', json={'plate_number': 'CONF-0004'})

        self._login_staff()
        res = self.client.post('/api/complete-service', json={'plate_number': 'CONF-0004'})
        self.assertEqual(res.status_code, 200)

        row = self._status_of('CONF-0004')
        self.assertEqual(row['queue_status'], 'pending_confirmation')

    def test_staff_cannot_reject_completion(self):
        self._insert_waiting_reservation('CONF-0004')
        self._login_staff()
        self.client.post('/api/start-service', json={'plate_number': 'CONF-0004'})
        self.client.post('/api/complete-service', json={'plate_number': 'CONF-0004'})

        res = self.client.post('/api/reject-complete', json={'plate_number': 'CONF-0004'})
        self.assertEqual(res.status_code, 403)

        row = self._status_of('CONF-0004')
        self.assertEqual(row['queue_status'], 'pending_confirmation')

    def test_confirm_and_reject_require_authentication(self):
        res = self.client.post('/api/confirm-complete', json={'plate_number': 'CONF-0005'})
        self.assertEqual(res.status_code, 401)
        res = self.client.post('/api/reject-complete', json={'plate_number': 'CONF-0005'})
        self.assertEqual(res.status_code, 401)

    def test_history_includes_washer_for_completed_booking(self):
        self._insert_waiting_reservation('CONF-0001')
        self._login_staff()
        self.client.post('/api/start-service', json={'plate_number': 'CONF-0001'})
        self.client.post('/api/complete-service', json={'plate_number': 'CONF-0001'})

        self._login_admin()
        self.client.post('/api/confirm-complete', json={'plate_number': 'CONF-0001'})
        self.client.post('/api/archive-ready', json={'plate_number': 'CONF-0001'})

        res = self.client.get('/api/get-history')
        matching = [r for r in res.get_json()['data'] if r['plate_number'] == 'CONF-0001']
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['washer'], self.staff_fullname)


if __name__ == '__main__':
    unittest.main()
