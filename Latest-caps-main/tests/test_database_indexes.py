import unittest
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from database import get_db_connection


class DatabaseIndexesTestCase(unittest.TestCase):
    """database/md57_schema.sql documented these indexes for a long time,
    but that file is never executed - only database.py's init_db() is the
    real, live schema. This confirms the indexes actually exist there now,
    not just on paper."""

    def _index_names(self):
        conn = get_db_connection()
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
        conn.close()
        return {row['name'] for row in rows}

    def test_reservations_status_index_exists(self):
        self.assertIn('idx_reservations_status', self._index_names())

    def test_reservations_plate_index_exists(self):
        self.assertIn('idx_reservations_plate', self._index_names())

    def test_reservations_date_index_exists(self):
        self.assertIn('idx_reservations_date', self._index_names())


if __name__ == '__main__':
    unittest.main()
