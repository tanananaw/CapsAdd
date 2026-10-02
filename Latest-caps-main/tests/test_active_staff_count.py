import unittest
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from database import get_db_connection
from utils import get_active_staff_count

TEST_DATE = '2026-03-03'
# Attendance only counts for real staff accounts, so the test rows use the
# seeded ones (roles below), tagged by time_in so cleanup only removes them.
TEST_MARKER = 'ASC-test'


class ActiveStaffCountTestCase(unittest.TestCase):
    def setUp(self):
        conn = get_db_connection()
        names = [r[0] for r in conn.execute(
            "SELECT COALESCE(fullname, username) FROM users WHERE role = 'staff' ORDER BY user_id LIMIT 4"
        )]
        conn.close()
        self.staff = dict(zip(('Clocked In', 'Clocked Out', 'Late Still In', 'Absent'), names))
        self._clear_test_rows()

    def tearDown(self):
        self._clear_test_rows()

    def _clear_test_rows(self):
        conn = get_db_connection()
        conn.execute("DELETE FROM staff_attendance WHERE time_in = ?", (TEST_MARKER,))
        conn.commit()
        conn.close()

    def _insert_attendance(self, who, status, time_out, date_str=TEST_DATE):
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO staff_attendance (staff_name, date_str, status, time_in, time_out) VALUES (?, ?, ?, ?, ?)",
            (self.staff[who], date_str, status, TEST_MARKER, time_out),
        )
        conn.commit()
        conn.close()

    def test_counts_present_and_late_still_clocked_in(self):
        self._insert_attendance('Clocked In', 'Present', '--:--')
        self._insert_attendance('Late Still In', 'Late', '--:--')
        count = get_active_staff_count(TEST_DATE)
        self.assertEqual(count, 2)

    def test_excludes_clocked_out_staff(self):
        self._insert_attendance('Clocked Out', 'Present', '05:00 PM')
        count = get_active_staff_count(TEST_DATE)
        self.assertEqual(count, 0)

    def test_excludes_absent_staff(self):
        self._insert_attendance('Absent', 'Absent', '--:--')
        count = get_active_staff_count(TEST_DATE)
        self.assertEqual(count, 0)

    def test_only_latest_row_per_staff_counts(self):
        """A staff member who timed in, then out, then in again the same
        day has multiple rows - only their latest state should count."""
        self._insert_attendance('Clocked In', 'Present', '--:--')
        self._insert_attendance('Clocked In', 'Present', '12:00 PM')  # timed out at noon
        count = get_active_staff_count(TEST_DATE)
        self.assertEqual(count, 0, "latest row shows clocked out - must not count the earlier still-in row")

    def test_defaults_to_today_when_no_date_given(self):
        from datetime import datetime
        today = datetime.now().strftime('%Y-%m-%d')
        self._insert_attendance('Clocked In', 'Present', '--:--', date_str=today)
        try:
            count = get_active_staff_count()
            self.assertGreaterEqual(count, 1)
        finally:
            self._clear_test_rows()


if __name__ == '__main__':
    unittest.main()
