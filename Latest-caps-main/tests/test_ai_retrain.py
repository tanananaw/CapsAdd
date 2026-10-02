import unittest
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from database import get_db_connection
from ai_retrain import _fetch_real_training_rows

TEST_PLATES = ('AIR-0001', 'AIR-0002', 'AIR-0003', 'AIR-0004', 'AIR-0005')


class FetchRealTrainingRowsTestCase(unittest.TestCase):
    def setUp(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT service_id, base_duration_minutes FROM services LIMIT 1")
        self.service_id, self.base_duration = cursor.fetchone()
        self._clear_test_rows()
        conn.close()

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

    def _insert_reservation(self, plate, status, created_at, completed_at,
                             queue_length_snapshot, active_staff_snapshot):
        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO reservations (
                vehicle_plate_number, vehicle_type, service_id, queue_status,
                created_at, completed_at, queue_length_snapshot, active_staff_snapshot
            ) VALUES (?, 'Car', ?, ?, ?, ?, ?, ?)
            """,
            (plate, self.service_id, status, created_at, completed_at,
             queue_length_snapshot, active_staff_snapshot),
        )
        conn.commit()
        conn.close()

    def test_complete_row_with_snapshot_is_included(self):
        self._insert_reservation(
            'AIR-0001', 'completed',
            '2026-01-01 10:00:00', '2026-01-01 10:40:00',
            queue_length_snapshot=97, active_staff_snapshot=53,
        )
        rows = _fetch_real_training_rows()
        matching = [r for r in rows if r['queue_length'] == 97 and r['active_staff'] == 53]
        self.assertEqual(len(matching), 1)
        self.assertAlmostEqual(matching[0]['actual_wait_time'], 40.0)
        self.assertEqual(matching[0]['service_base_time'], self.base_duration)
        self.assertEqual(matching[0]['precipitation'], 0.0)

    def test_row_missing_snapshot_is_excluded(self):
        """Bookings placed before this feature shipped have NULL snapshots -
        there's no honest way to reconstruct historical queue/staff state."""
        self._insert_reservation(
            'AIR-0002', 'completed',
            '2026-01-01 10:00:00', '2026-01-01 10:40:00',
            queue_length_snapshot=None, active_staff_snapshot=None,
        )
        rows = _fetch_real_training_rows()
        plates_represented = [r for r in rows if r.get('actual_wait_time') == 40.0 and r['queue_length'] is None]
        self.assertEqual(plates_represented, [])

    def test_row_not_completed_or_ready_is_excluded(self):
        # Distinctive marker values so this test only checks for its own
        # row, not the total row count - the shared dev/test database
        # accumulates other genuinely-qualifying rows from live smoke
        # testing over time, so asserting a global count of 0 is fragile.
        self._insert_reservation(
            'AIR-0003', 'waiting',
            '2026-01-01 10:00:00', None,
            queue_length_snapshot=997, active_staff_snapshot=998,
        )
        rows = _fetch_real_training_rows()
        matching = [r for r in rows if r['queue_length'] == 997 and r['active_staff'] == 998]
        self.assertEqual(matching, [])

    def test_implausible_duration_is_excluded(self):
        """A completed_at earlier than created_at (negative duration) or an
        absurdly long one must be dropped, not silently averaged in."""
        self._insert_reservation(
            'AIR-0004', 'completed',
            '2026-01-01 10:00:00', '2026-01-01 09:00:00',  # negative
            queue_length_snapshot=997, active_staff_snapshot=999,
        )
        rows = _fetch_real_training_rows()
        matching = [r for r in rows if r['queue_length'] == 997 and r['active_staff'] == 999]
        self.assertEqual(matching, [])

    def test_ready_status_is_included_same_as_completed(self):
        self._insert_reservation(
            'AIR-0005', 'ready',
            '2026-01-01 10:00:00', '2026-01-01 10:25:00',
            queue_length_snapshot=61, active_staff_snapshot=42,
        )
        rows = _fetch_real_training_rows()
        matching = [r for r in rows if r['queue_length'] == 61 and r['active_staff'] == 42]
        self.assertEqual(len(matching), 1)
        self.assertAlmostEqual(matching[0]['actual_wait_time'], 25.0)


if __name__ == '__main__':
    unittest.main()
