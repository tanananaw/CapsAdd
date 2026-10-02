import unittest
import json
import os
import sys

# Ensure _Backend is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection

class RBACTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        conn = get_db_connection()
        conn.execute("DELETE FROM reservations WHERE vehicle_plate_number = 'TST-9999'")
        conn.commit()
        conn.close()

    def tearDown(self):
        conn = get_db_connection()
        # feedbacks.reservation_id references reservations - must delete the
        # referencing row first or the FK constraint blocks the reservation
        # delete (and leaves the connection open, locking the db file for
        # every test that runs after this one).
        conn.execute("DELETE FROM feedbacks WHERE plate_number IN ('XYZ-1234', 'HCK-0000')")
        conn.execute("DELETE FROM reservations WHERE vehicle_plate_number IN ('TST-9999', 'XYZ-1234')")
        conn.execute("DELETE FROM staff_attendance WHERE date_str = '2026-01-01' AND staff_name = 'TJ Barcebal'")
        conn.execute("DELETE FROM staff_shifts WHERE created_by = 'test-roster'")
        conn.commit()
        conn.close()

    def test_unauthenticated_api_access(self):
        """Unauthenticated requests to protected APIs should receive 401 JSON error"""
        res = self.client.get('/api/admin/metrics')
        self.assertEqual(res.status_code, 401)
        data = res.get_json()
        self.assertEqual(data['status'], 'error')

        res = self.client.post('/api/admin/create-user', json={'username': 'test', 'password': '123'})
        self.assertEqual(res.status_code, 401)

        res = self.client.post('/api/cancel-booking', json={'plate_number': 'ABC-1000'})
        self.assertEqual(res.status_code, 401)

        res = self.client.post('/api/submit-booking', json={'plate_number': 'TST-9999'})
        self.assertEqual(res.status_code, 401)

        res = self.client.post('/api/predict-ewt', json={'queue_length': 1})
        self.assertEqual(res.status_code, 401)

    def test_unauthenticated_page_access(self):
        """Unauthenticated requests to protected pages should redirect to login"""
        res = self.client.get('/admin')
        self.assertEqual(res.status_code, 302)
        self.assertIn('/login', res.headers['Location'])

        res = self.client.get('/analytics')
        self.assertEqual(res.status_code, 302)
        self.assertIn('/login', res.headers['Location'])

    def test_customer_role_permissions(self):
        """Customer should be restricted from admin/staff endpoints"""
        with self.client.session_transaction() as sess:
            sess['user'] = 'customer_user'
            sess['role'] = 'customer'
            sess['fullname'] = 'Default Customer'
            sess['user_id'] = 2

        # Forbidden from admin metrics
        res = self.client.get('/api/admin/metrics')
        self.assertEqual(res.status_code, 403)

        # Forbidden from creating users
        res = self.client.post('/api/admin/create-user', json={'username': 'hacker', 'password': '123'})
        self.assertEqual(res.status_code, 403)

        # Forbidden from admin page
        res = self.client.get('/admin')
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers['Location'], '/')

        # Allowed to submit booking (logged in as customer) - on a day with
        # staff on the roster, which customer bookings require.
        conn = get_db_connection()
        conn.execute("INSERT OR IGNORE INTO staff_shifts (staff_name, shift_date, created_by) VALUES (?, ?, 'test-roster')", ('TJ Barcebal', '2026-10-10'))
        conn.commit()
        service_id = conn.execute(
            "SELECT s.service_id FROM services s JOIN service_prices p ON p.service_id = s.service_id WHERE s.is_active = 1 AND p.vehicle_type = 'Medium' ORDER BY s.service_id LIMIT 1"
        ).fetchone()['service_id']
        conn.close()
        booking_payload = {
            'plate_number': 'TST-9999',
            'vehicle_type': 'Medium',
            'service_id': service_id,
            'customer_email': 'customer_user@example.com',
            'booking_source': 'Online Booking',
            'booking_date': '2026-10-10',
            'booking_time': '10:00 AM',
            'assigned_bay': 'Bay #1'
        }
        res = self.client.post('/api/submit-booking', json=booking_payload)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['plate_number'], 'TST-9999')

        # Customer can cancel their own booking
        res = self.client.post('/api/cancel-booking', json={'plate_number': 'TST-9999'})
        self.assertEqual(res.status_code, 200)

    def test_staff_role_permissions(self):
        """Staff should access queue and cancelled list (read-only), but be
        forbidden from admin tasks - including the lifecycle-closing actions
        (restore, attendance, notify-customer, archive-to-history) that used
        to be staff-accessible too."""
        with self.client.session_transaction() as sess:
            sess['user'] = 'TJ Barcebal'
            sess['role'] = 'staff'
            sess['fullname'] = 'TJ Barcebal'
            sess['user_id'] = 3

        # Staff can access queue page
        res = self.client.get('/queue')
        self.assertEqual(res.status_code, 200)

        # Staff can access cancelled list (read-only)
        res = self.client.get('/api/get-cancelled')
        self.assertEqual(res.status_code, 200)

        # Staff can no longer restore a cancelled booking - admin only
        res = self.client.post('/api/restore-booking', json={'plate_number': 'TST-9999'})
        self.assertEqual(res.status_code, 403)

        # Staff can no longer record attendance (self or anyone else's) -
        # admin only clocks staff in/out now
        res = self.client.post('/api/staff-attendance', json={
            'staff_name': 'TJ Barcebal', 'date_str': '2026-01-01',
            'status': 'Present', 'time_in': '08:00', 'time_out': '--:--',
        })
        self.assertEqual(res.status_code, 403)

        # Staff can no longer send the "car is ready" customer notification
        res = self.client.post('/api/send-email', json={
            'recipient': 'customer@example.com', 'subject': 'Ready', 'message': 'Ready',
            'plate_number': 'TST-9999', 'customer_name': 'Test',
        })
        self.assertEqual(res.status_code, 403)

        # Staff can no longer archive a ready vehicle to History
        res = self.client.post('/api/archive-ready', json={'plate_number': 'TST-9999'})
        self.assertEqual(res.status_code, 403)

        # Staff is forbidden from creating users
        res = self.client.post('/api/admin/create-user', json={'username': 'newstaff', 'password': '123'})
        self.assertEqual(res.status_code, 403)

    def test_admin_retains_lifecycle_closing_actions(self):
        """The actions just taken away from staff must still work for
        admin - restore, attendance, notify, archive."""
        with self.client.session_transaction() as sess:
            sess['user'] = 'manager'
            sess['role'] = 'admin'
            sess['fullname'] = 'System Manager'
            sess['user_id'] = 1

        # A real cancelled booking to restore (actions on a booking that
        # doesn't exist answer 404, not a silent 200).
        conn = get_db_connection()
        service_id = conn.execute("SELECT service_id FROM services LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO reservations (vehicle_plate_number, vehicle_type, service_id, queue_status) VALUES ('TST-9999', 'Medium', ?, 'cancelled')",
            (service_id,),
        )
        conn.commit()
        conn.close()

        res = self.client.post('/api/restore-booking', json={'plate_number': 'TST-9999'})
        self.assertEqual(res.status_code, 200)

        res = self.client.post('/api/staff-attendance', json={
            'staff_name': 'TJ Barcebal', 'date_str': '2026-01-01',
            'status': 'Present', 'time_in': '08:00', 'time_out': '--:--',
        })
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        conn.execute("UPDATE reservations SET queue_status = 'ready' WHERE vehicle_plate_number = 'TST-9999'")
        conn.commit()
        conn.close()
        res = self.client.post('/api/archive-ready', json={'plate_number': 'TST-9999'})
        self.assertEqual(res.status_code, 200)

    def test_admin_role_permissions(self):
        """Admin has full access to management endpoints"""
        with self.client.session_transaction() as sess:
            sess['user'] = 'manager'
            sess['role'] = 'admin'
            sess['fullname'] = 'System Manager'
            sess['user_id'] = 1

        # Admin can view metrics
        res = self.client.get('/api/admin/metrics')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn('queue_count', data)

        # Admin can view history
        res = self.client.get('/api/get-history')
        self.assertEqual(res.status_code, 200)

        # Admin can view analytics page
        res = self.client.get('/analytics')
        self.assertEqual(res.status_code, 200)

    def test_feedback_system(self):
        """Feedback can be submitted and retrieved from database (requires
        login, and requires a real completed reservation owned by the
        submitter - see tests/test_feedback_logic.py for the full set of
        ownership/status/duplicate checks)."""
        # Unauthenticated requests must be rejected
        payload = {'reservation_id': 999999, 'rating': 5, 'comment': 'Fast and friendly service!'}
        res = self.client.post('/api/submit-feedback', json=payload)
        self.assertEqual(res.status_code, 401)

        res = self.client.get('/api/get-feedbacks')
        self.assertEqual(res.status_code, 401)

        # Logged-in customer can submit feedback for their own completed visit
        with self.client.session_transaction() as sess:
            sess['user'] = 'customer_user'
            sess['role'] = 'customer'
            sess['fullname'] = 'Default Customer'
            sess['user_id'] = 2

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT service_id FROM services LIMIT 1")
        service_id = cursor.fetchone()[0]
        cursor.execute("""
            INSERT INTO reservations (user_id, customer_name, vehicle_plate_number, vehicle_type, service_id, queue_status, completed_at)
            VALUES (2, 'Default Customer', 'XYZ-1234', 'Car', ?, 'completed', '2026-01-01 10:00:00')
        """, (service_id,))
        reservation_id = cursor.lastrowid
        conn.commit()
        conn.close()

        payload['reservation_id'] = reservation_id
        res = self.client.post('/api/submit-feedback', json=payload)
        self.assertEqual(res.status_code, 200)

        res = self.client.get('/api/get-feedbacks')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        # Still logged in as a customer here, so the plate number on the
        # public feedback wall is masked (only staff/admin see it in full,
        # last 3 characters kept) - match on the kept tail instead of the
        # exact plate.
        self.assertTrue(any(fb['plate_number'].endswith('234') for fb in data['data']))

    def test_csrf_protection_enforcement(self):
        """When CSRF is enabled, requests without valid token should be blocked with 400"""
        self.app.config['WTF_CSRF_ENABLED'] = True
        try:
            res = self.client.post('/api/submit-feedback', json={'customer_name': 'Hacker', 'plate_number': 'HCK-0000'})
            self.assertEqual(res.status_code, 400)
            data = res.get_json()
            self.assertEqual(data['status'], 'error')
            self.assertIn('CSRF', data['message'])
        finally:
            self.app.config['WTF_CSRF_ENABLED'] = False

if __name__ == '__main__':
    unittest.main()