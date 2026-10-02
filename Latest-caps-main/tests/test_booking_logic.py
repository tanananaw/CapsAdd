import unittest
import os
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app, get_db_connection
from utils import is_within_business_hours, is_date_staffed

TEST_PLATES = ('BKT-0001', 'BKT-0002', 'BKT-0003', 'BKT-0004', 'BKT-0005')


class BookingLogicTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self._clear_test_rows()
        # These tests exercise booking ACCEPTANCE paths, which now also
        # requires a washer on duty today (see StaffedBookingGateTestCase
        # for the decline path) - force one on regardless of the shared
        # dev DB's ambient real attendance state, same override-row trick
        # used in test_recommend_staff.py.
        self._attendance_ids = []
        self._force_a_staff_member_on_duty()
        conn = get_db_connection()
        self.service_id = conn.execute(
            "SELECT s.service_id FROM services s JOIN service_prices p ON p.service_id = s.service_id WHERE s.is_active = 1 AND p.vehicle_type = 'Medium' ORDER BY s.service_id LIMIT 1"
        ).fetchone()['service_id']
        conn.close()
        with self.client.session_transaction() as sess:
            sess['user'] = 'customer_user'
            sess['role'] = 'customer'
            sess['fullname'] = 'Default Customer'
            sess['user_id'] = 2

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

    def _force_a_staff_member_on_duty(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' LIMIT 1")
        staff_name = cursor.fetchone()['name']
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute(
            "INSERT INTO staff_attendance (staff_name, date_str, status, time_in, time_out) VALUES (?, ?, 'Present', '08:00', '--:--')",
            (staff_name, today),
        )
        self._attendance_ids.append(cursor.lastrowid)
        # Customer bookings also need staff on the roster for the booked
        # day (see api_submit_booking's staffing rule) - every date these
        # tests book.
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        for day in ('2026-12-01', '2026-12-02', '2026-12-03', '2026-12-05', tomorrow):
            cursor.execute("INSERT OR IGNORE INTO staff_shifts (staff_name, shift_date, created_by) VALUES (?, ?, 'test-roster')", (staff_name, day))
        conn.commit()
        conn.close()

    def _clear_test_rows(self):
        conn = get_db_connection()
        conn.execute(
            f"DELETE FROM reservations WHERE vehicle_plate_number IN ({','.join('?' * len(TEST_PLATES))})",
            TEST_PLATES,
        )
        conn.execute("DELETE FROM staff_shifts WHERE created_by = 'test-roster'")
        conn.commit()
        conn.close()

    def test_double_booking_same_bay_slot_queues_instead_of_rejecting(self):
        """MD57 is a real first-come-first-served queue, not an appointment
        calendar with exclusive slots (see api_submit_booking's own note on
        this) - two customers wanting the same service around the same time
        is normal demand, so a second booking matching an existing one's
        exact bay/date/time must still succeed and simply queue behind the
        first (higher queue_number), not be rejected. Bay capacity is
        enforced separately, at start-service time (see
        test_washer_assignment's bay-occupancy tests)."""
        payload = {
            'plate_number': 'BKT-0001',
            'vehicle_type': 'Medium',
            'service_id': self.service_id,
            'customer_email': 'first@example.com',
            'booking_date': '2026-12-01',
            'booking_time': '10:00 AM',
            'assigned_bay': 'Bay #1',
        }
        res = self.client.post('/api/submit-booking', json=payload)
        self.assertEqual(res.status_code, 200)
        first_queue_number = res.get_json()['queue_number']

        second_payload = dict(payload, plate_number='BKT-0002', customer_email='second@example.com')
        res = self.client.post('/api/submit-booking', json=second_payload)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['status'], 'success')
        self.assertGreater(data['queue_number'], first_queue_number)

    def test_different_bay_same_slot_allowed(self):
        """Two bookings at the same date/time but different bays should both succeed."""
        base_payload = {
            'vehicle_type': 'Medium',
            'service_id': self.service_id,
            'booking_date': '2026-12-02',
            'booking_time': '11:00 AM',
        }
        res = self.client.post('/api/submit-booking', json=dict(
            base_payload, plate_number='BKT-0001', customer_email='a@example.com', assigned_bay='Bay #1'))
        self.assertEqual(res.status_code, 200)

        res = self.client.post('/api/submit-booking', json=dict(
            base_payload, plate_number='BKT-0002', customer_email='b@example.com', assigned_bay='Bay #2'))
        self.assertEqual(res.status_code, 200)

    def test_booking_requires_a_real_active_service(self):
        """A booking must name a real, active catalog service by id - a
        free-text service name alone, an unknown id, or a retired service
        is rejected rather than silently matched to some other service."""
        conn = get_db_connection()
        retired_id = conn.execute("SELECT service_id FROM services WHERE is_active = 0 LIMIT 1").fetchone()['service_id']
        conn.close()

        base_payload = {
            'plate_number': 'BKT-0003',
            'vehicle_type': 'Medium',
            'customer_email': 'fallback@example.com',
            # Explicit scheduled time rather than a walk-in - a walk-in's
            # business-hours check runs against the real wall clock
            # (datetime.now()), which made tests fail whenever actually
            # run outside 8AM-9PM even though they have nothing to do with
            # business hours. Every other test in this class that isn't
            # specifically testing the walk-in/business-hours path itself
            # uses a fixed future date/time for the same reason.
            'booking_date': '2026-12-01', 'booking_time': '10:00 AM',
        }
        for service_fields in ({'service_type': 'Body Wash'}, {'service_id': 999999}, {'service_id': retired_id}):
            with self.subTest(service_fields=service_fields):
                res = self.client.post('/api/submit-booking', json=dict(base_payload, **service_fields))
                self.assertEqual(res.status_code, 400)

        conn = get_db_connection()
        row = conn.execute("SELECT 1 FROM reservations WHERE vehicle_plate_number = 'BKT-0003'").fetchone()
        conn.close()
        self.assertIsNone(row, "a rejected booking must not create a reservation")

    def test_past_booking_datetime_rejected(self):
        """The calendar UI hides past dates client-side only - a direct API
        call must still be rejected server-side rather than silently
        creating a booking for a date/time that's already gone."""
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        res = self.client.post('/api/submit-booking', json={
            'plate_number': 'BKT-0001',
            'vehicle_type': 'Medium',
            'service_id': self.service_id,
            'customer_email': 'past@example.com',
            'booking_date': yesterday,
            'booking_time': '10:00 AM',
            'assigned_bay': 'Bay #1',
        })
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertEqual(data['status'], 'error')
        self.assertIn('past', data['message'].lower())

        conn = get_db_connection()
        row = conn.execute(
            "SELECT 1 FROM reservations WHERE vehicle_plate_number = ?", ('BKT-0001',)
        ).fetchone()
        conn.close()
        self.assertIsNone(row, "a rejected past-dated booking must not be persisted")

    def test_booking_before_opening_hour_rejected(self):
        """MD57 opens at 8:00 AM - a booking requested earlier than that
        (even for a real future date) must be rejected."""
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        res = self.client.post('/api/submit-booking', json={
            'plate_number': 'BKT-0004',
            'vehicle_type': 'Medium',
            'service_id': self.service_id,
            'customer_email': 'early@example.com',
            'booking_date': tomorrow,
            'booking_time': '06:00 AM',
            'assigned_bay': 'Bay #1',
        })
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertEqual(data['status'], 'error')
        self.assertIn('8:00 am', data['message'].lower())

    def test_booking_after_closing_hour_rejected(self):
        """MD57 closes at 9:00 PM - a booking requested later than that
        must be rejected, even though the date itself is a real future
        date and would otherwise pass the past-date check."""
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        res = self.client.post('/api/submit-booking', json={
            'plate_number': 'BKT-0005',
            'vehicle_type': 'Medium',
            'service_id': self.service_id,
            'customer_email': 'late@example.com',
            'booking_date': tomorrow,
            'booking_time': '10:00 PM',
            'assigned_bay': 'Bay #1',
        })
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertEqual(data['status'], 'error')
        self.assertIn('9:00 pm', data['message'].lower())

    def test_booking_at_exact_closing_hour_still_allowed(self):
        """9:00 PM itself is still a valid, bookable time - the window is
        inclusive of both ends, matching the last slot the booking UI
        actually offers."""
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        res = self.client.post('/api/submit-booking', json={
            'plate_number': 'BKT-0004',
            'vehicle_type': 'Medium',
            'service_id': self.service_id,
            'customer_email': 'boundary@example.com',
            'booking_date': tomorrow,
            'booking_time': '09:00 PM',
            'assigned_bay': 'Bay #1',
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['status'], 'success')

    def test_future_booking_datetime_still_allowed(self):
        """Sanity check alongside the past-date rejection: a real future
        booking must still succeed normally."""
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        res = self.client.post('/api/submit-booking', json={
            'plate_number': 'BKT-0002',
            'vehicle_type': 'Medium',
            'service_id': self.service_id,
            'customer_email': 'future@example.com',
            'booking_date': tomorrow,
            'booking_time': '10:00 AM',
            'assigned_bay': 'Bay #1',
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['status'], 'success')

    def test_malformed_booking_time_rejected(self):
        """An unparseable booking_time must be rejected with a clear error
        instead of silently skipping the past-date check or crashing."""
        res = self.client.post('/api/submit-booking', json={
            'plate_number': 'BKT-0003',
            'vehicle_type': 'Medium',
            'service_id': self.service_id,
            'customer_email': 'malformed@example.com',
            'booking_date': '2026-12-05',
            'booking_time': 'not-a-real-time',
            'assigned_bay': 'Bay #1',
        })
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()['status'], 'error')

    def test_garbage_plate_number_rejected(self):
        """Deliberately loose - only catches punctuation-only/too-short
        garbage, not anything shaped like a real plate (formats vary too
        much across old/new/motorcycle/conduction/personalized plates to
        validate strictly)."""
        for garbage_plate in ('---', 'A', '  ', '#$%'):
            res = self.client.post('/api/submit-booking', json={
                'plate_number': garbage_plate,
                'vehicle_type': 'Medium',
                'service_id': self.service_id,
                'customer_email': 'garbage@example.com',
            })
            self.assertEqual(res.status_code, 400, f"expected {garbage_plate!r} to be rejected")
            self.assertIn('plate number', res.get_json()['message'].lower())

    def test_realistic_plate_formats_still_accepted(self):
        """Sanity check alongside the garbage-plate rejection: real-looking
        plates in different formats must not get caught by the check."""
        for idx, real_plate in enumerate(('ABC 1234', 'NGP-1023', '1234 AB')):
            res = self.client.post('/api/submit-booking', json={
                'plate_number': real_plate,
                'vehicle_type': 'Medium',
                'service_id': self.service_id,
                'customer_email': f'realplate{idx}@example.com',
                # Explicit scheduled time - see the comment on
                # test_booking_requires_a_real_active_service on
                # why a walk-in (no date/time) makes this test depend on the
                # real wall clock's time of day for no reason relevant to
                # what it's actually testing (plate format matching). A
                # distinct bay per plate avoids the queueing behavior
                # test_double_booking_same_bay_slot_queues_instead_of_rejecting
                # already covers, keeping this test focused on plate formats.
                'booking_date': '2026-12-03', 'booking_time': '09:00 AM',
                'assigned_bay': f'Bay #{(idx % 3) + 1}',
            })
            self.assertEqual(res.status_code, 200, f"expected {real_plate!r} to be accepted")
            self.client.post('/api/cancel-booking', json={'plate_number': real_plate.strip().upper()})

    def test_mark_ready_route_no_longer_exists(self):
        """Dead duplicate route (`return api_complete_service()`, never
        called from any template) - removed."""
        res = self.client.post('/api/mark-ready', json={'plate_number': 'BKT-0001'})
        self.assertEqual(res.status_code, 404)

    def test_unrecognized_vehicle_type_rejected(self):
        """A vehicle_type that doesn't match any configured vehicle_types
        row must be rejected, not silently stored with an implicit
        0-minute duration multiplier."""
        res = self.client.post('/api/submit-booking', json={
            'plate_number': 'BKT-0001',
            'vehicle_type': 'Spaceship',
            'service_id': self.service_id,
            'customer_email': 'badtype@example.com',
            # See test_booking_requires_a_real_active_service's
            # comment - a scheduled time keeps this independent of the real
            # wall-clock time of day, which has nothing to do with what
            # this test actually checks (vehicle_type validation).
            'booking_date': '2026-12-01', 'booking_time': '10:00 AM', 'assigned_bay': 'Bay #1',
        })
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertEqual(data['status'], 'error')
        self.assertIn('vehicle type', data['message'].lower())

        conn = get_db_connection()
        row = conn.execute(
            "SELECT 1 FROM reservations WHERE vehicle_plate_number = ?", ('BKT-0001',)
        ).fetchone()
        conn.close()
        self.assertIsNone(row, "a rejected booking must not be persisted")

    def test_vehicle_type_matched_case_insensitively(self):
        """The booking UI always sends the exact configured casing, but a
        direct API call sending different casing (e.g. 'medium') should
        still resolve to the real 'Medium' row rather than being rejected
        or stored mismatched."""
        res = self.client.post('/api/submit-booking', json={
            'plate_number': 'BKT-0002',
            'vehicle_type': 'medium',
            'service_id': self.service_id,
            'customer_email': 'casing@example.com',
            # See test_booking_requires_a_real_active_service's
            # comment on why a scheduled time avoids depending on the real
            # wall-clock time of day for a test that's actually about
            # vehicle_type casing.
            'booking_date': '2026-12-02', 'booking_time': '10:00 AM', 'assigned_bay': 'Bay #1',
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['status'], 'success')

        conn = get_db_connection()
        row = conn.execute(
            "SELECT vehicle_type FROM reservations WHERE vehicle_plate_number = ?", ('BKT-0002',)
        ).fetchone()
        conn.close()
        self.assertEqual(row['vehicle_type'], 'Medium')

    def test_booking_captures_operational_snapshot_for_ai_retraining(self):
        """api_submit_booking must record queue_length_snapshot,
        active_staff_snapshot, and an explicit local created_at - the real
        training data ai_retrain.py later reads back."""
        before = datetime.now()
        res = self.client.post('/api/submit-booking', json={
            'plate_number': 'BKT-0001',
            'vehicle_type': 'Medium',
            'service_id': self.service_id,
            'customer_email': 'snapshot@example.com',
            # See test_booking_requires_a_real_active_service's
            # comment on why a scheduled time avoids depending on the real
            # wall-clock time of day - created_at (checked below) still
            # reflects the actual moment of submission either way.
            'booking_date': '2026-12-01', 'booking_time': '10:00 AM', 'assigned_bay': 'Bay #1',
        })
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        row = conn.execute(
            "SELECT queue_length_snapshot, active_staff_snapshot, created_at FROM reservations WHERE vehicle_plate_number = ?",
            ('BKT-0001',),
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row['queue_length_snapshot'])
        self.assertIsNotNone(row['active_staff_snapshot'])
        self.assertGreaterEqual(row['queue_length_snapshot'], 0)
        self.assertGreaterEqual(row['active_staff_snapshot'], 0)

        created_dt = datetime.strptime(row['created_at'], '%Y-%m-%d %H:%M:%S')
        # Explicit local time, not the UTC-evaluated SQL default - should
        # land within a few seconds of "now" in this process's own clock.
        self.assertLess(abs((created_dt - before).total_seconds()), 10)


class BusinessHoursHelperTestCase(unittest.TestCase):
    """Direct edge-case tests for is_within_business_hours() - covers the
    walk-in path (checked against datetime.now(), which can't be pinned to
    a specific hour through a live HTTP request the way a scheduled
    booking's explicit time can)."""

    def test_just_before_opening_rejected(self):
        self.assertFalse(is_within_business_hours(datetime(2026, 1, 1, 7, 59)))

    def test_exact_opening_hour_allowed(self):
        self.assertTrue(is_within_business_hours(datetime(2026, 1, 1, 8, 0)))

    def test_midday_allowed(self):
        self.assertTrue(is_within_business_hours(datetime(2026, 1, 1, 14, 30)))

    def test_exact_closing_hour_allowed(self):
        self.assertTrue(is_within_business_hours(datetime(2026, 1, 1, 21, 0)))

    def test_just_after_closing_rejected(self):
        self.assertFalse(is_within_business_hours(datetime(2026, 1, 1, 21, 1)))

    def test_middle_of_the_night_rejected(self):
        self.assertFalse(is_within_business_hours(datetime(2026, 1, 1, 2, 0)))


class CustomerStaffingRuleTestCase(unittest.TestCase):
    """A customer can only book a day someone can serve them: today needs a
    staff member clocked in or on today's roster (otherwise refused); a
    future day with nobody available on the roster is not saved - the next
    staffed day is proposed instead, for the customer to accept or cancel.
    Staff/admin booking on someone's behalf aren't restricted."""

    PLATE = 'STF-0001'
    # Far-future dates so no other test's roster rows land in between.
    UNSTAFFED_DAY, UNAVAILABLE_DAY, STAFFED_DAY = '2027-03-01', '2027-03-02', '2027-03-04'

    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        conn = get_db_connection()
        self.service_id = conn.execute(
            "SELECT s.service_id FROM services s JOIN service_prices p ON p.service_id = s.service_id "
            "WHERE s.is_active = 1 AND p.vehicle_type = 'Medium' ORDER BY s.service_id LIMIT 1"
        ).fetchone()['service_id']
        self.staff_name = conn.execute(
            "SELECT COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' ORDER BY user_id LIMIT 1"
        ).fetchone()['name']
        conn.close()
        self._clear()
        self._login('customer')

    def tearDown(self):
        self._clear()

    def _clear(self):
        conn = get_db_connection()
        conn.execute("DELETE FROM reservations WHERE vehicle_plate_number = ?", (self.PLATE,))
        conn.execute("DELETE FROM staff_shifts WHERE created_by = 'test-staffing'")
        conn.execute("DELETE FROM staff_unavailability WHERE reason = 'test-staffing'")
        conn.execute("DELETE FROM staff_attendance WHERE time_in = 'test-staffing'")
        conn.commit()
        conn.close()

    def _login(self, role):
        with self.client.session_transaction() as sess:
            sess['user'] = 'customer_user' if role == 'customer' else 'manager'
            sess['role'] = role
            sess['fullname'] = 'Default Customer' if role == 'customer' else 'System Manager'
            sess['user_id'] = 2 if role == 'customer' else 1

    def _roster(self, day):
        conn = get_db_connection()
        conn.execute("INSERT INTO staff_shifts (staff_name, shift_date, created_by) VALUES (?, ?, 'test-staffing')", (self.staff_name, day))
        conn.commit()
        conn.close()

    def _book(self, day):
        return self.client.post('/api/submit-booking', json={
            'plate_number': self.PLATE, 'vehicle_type': 'Medium', 'service_id': self.service_id,
            'customer_email': 'staffing@example.com', 'booking_date': day, 'booking_time': '10:00 AM',
        })

    def _saved(self):
        conn = get_db_connection()
        row = conn.execute("SELECT booking_date FROM reservations WHERE vehicle_plate_number = ?", (self.PLATE,)).fetchone()
        conn.close()
        return row['booking_date'] if row else None

    def test_future_day_without_staff_proposes_next_staffed_day_and_saves_nothing(self):
        self._roster(self.STAFFED_DAY)
        res = self._book(self.UNSTAFFED_DAY)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['status'], 'reschedule_required')
        self.assertEqual(data['requested_date'], self.UNSTAFFED_DAY)
        self.assertEqual(data['proposed_date'], self.STAFFED_DAY)
        self.assertIsNone(self._saved(), "nothing is booked until the customer accepts the new date")

    def test_accepting_the_proposed_day_books_it(self):
        self._roster(self.STAFFED_DAY)
        proposed = self._book(self.UNSTAFFED_DAY).get_json()['proposed_date']
        res = self._book(proposed)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self._saved(), self.STAFFED_DAY)

    def test_rostered_staff_marked_unavailable_does_not_count(self):
        self._roster(self.UNAVAILABLE_DAY)
        self._roster(self.STAFFED_DAY)
        conn = get_db_connection()
        conn.execute("INSERT INTO staff_unavailability (staff_name, unavailable_date, reason) VALUES (?, ?, 'test-staffing')",
                     (self.staff_name, self.UNAVAILABLE_DAY))
        conn.commit()
        conn.close()
        res = self._book(self.UNAVAILABLE_DAY)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['status'], 'reschedule_required')
        self.assertEqual(res.get_json()['proposed_date'], self.STAFFED_DAY)

    def test_no_staffed_day_at_all_is_refused(self):
        res = self._book('2099-06-01')  # nothing on the roster after this
        self.assertEqual(res.status_code, 400)
        self.assertIsNone(self._saved())

    def test_admin_booking_is_not_restricted(self):
        self._login('admin')
        res = self._book(self.UNSTAFFED_DAY)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self._saved(), self.UNSTAFFED_DAY)

    def test_today_needs_someone_clocked_in_or_rostered(self):
        today = datetime.now().strftime('%Y-%m-%d')
        self.assertFalse(is_date_staffed(today))

        conn = get_db_connection()
        conn.execute("INSERT INTO staff_attendance (staff_name, date_str, status, time_in, time_out) VALUES (?, ?, 'Present', 'test-staffing', '--:--')",
                     (self.staff_name, today))
        conn.commit()
        conn.close()
        self.assertTrue(is_date_staffed(today), "someone clocked in today counts")

        self._clear()
        self._roster(today)
        self.assertTrue(is_date_staffed(today), "someone on today's roster counts")

    @unittest.skipIf(datetime.now().hour >= 20, "no later same-day slot left to book today")
    def test_customer_booking_today_without_staff_is_refused(self):
        today = datetime.now().strftime('%Y-%m-%d')
        res = self.client.post('/api/submit-booking', json={
            'plate_number': self.PLATE, 'vehicle_type': 'Medium', 'service_id': self.service_id,
            'booking_date': today, 'booking_time': '09:00 PM',
        })
        self.assertEqual(res.status_code, 400)
        self.assertIsNone(self._saved())

    def test_staff_status_endpoint_follows_the_same_rule(self):
        self._roster(self.STAFFED_DAY)
        staffed = self.client.get(f'/api/staff-status?date={self.STAFFED_DAY}').get_json()
        unstaffed = self.client.get(f'/api/staff-status?date={self.UNSTAFFED_DAY}').get_json()
        self.assertTrue(staffed['staff_available'])
        self.assertFalse(unstaffed['staff_available'])


if __name__ == '__main__':
    unittest.main()
