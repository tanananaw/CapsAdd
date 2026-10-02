import unittest
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from database import get_db_connection
from shift_reminder import process_shift_reminders

TOMORROW = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
TODAY = datetime.now().strftime('%Y-%m-%d')
IN_3_DAYS = (datetime.now() + timedelta(days=3)).strftime('%Y-%m-%d')


class ProcessShiftRemindersTestCase(unittest.TestCase):
    """Mocks send_branded_email, the one external-gateway boundary this
    module calls - rather than actually hitting SMTP from a test run."""

    def setUp(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' ORDER BY user_id LIMIT 1")
        row = cursor.fetchone()
        self.staff_id = row['user_id']
        self.staff_name = row['name']
        conn.close()
        self._clear_test_rows()

    def tearDown(self):
        self._clear_test_rows()

    def _clear_test_rows(self):
        conn = get_db_connection()
        conn.execute(
            "DELETE FROM staff_shifts WHERE staff_name = ? AND shift_date IN (?, ?, ?)",
            (self.staff_name, TODAY, TOMORROW, IN_3_DAYS),
        )
        conn.execute("UPDATE users SET notify_email = NULL WHERE user_id = ?", (self.staff_id,))
        conn.commit()
        conn.close()

    def _set_contact(self, email=None):
        conn = get_db_connection()
        conn.execute("UPDATE users SET notify_email = ? WHERE user_id = ?", (email, self.staff_id))
        conn.commit()
        conn.close()

    def _insert_shift(self, shift_date, note=None):
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO staff_shifts (staff_name, shift_date, shift_note, created_by) VALUES (?, ?, ?, 'test')",
            (self.staff_name, shift_date, note),
        )
        conn.commit()
        conn.close()

    def _reminded_at(self, shift_date):
        conn = get_db_connection()
        row = conn.execute(
            "SELECT reminded_at FROM staff_shifts WHERE staff_name = ? AND shift_date = ?",
            (self.staff_name, shift_date),
        ).fetchone()
        conn.close()
        return row['reminded_at'] if row else None

    @patch('shift_reminder.send_branded_email')
    def test_email_sent_for_staff_with_email_on_file(self, mock_email):
        self._set_contact(email='staff@example.com')
        self._insert_shift(TOMORROW)

        process_shift_reminders()

        mock_email.assert_called_once()
        self.assertEqual(mock_email.call_args.kwargs.get('recipient'), 'staff@example.com')
        self.assertIsNotNone(self._reminded_at(TOMORROW))

    @patch('shift_reminder.send_branded_email')
    def test_no_contact_on_file_sends_nothing_and_stays_unreminded(self, mock_email):
        """Opting in later, before the shift date, should still get them
        reminded on a subsequent cycle - reminded_at must stay NULL, not
        be marked handled just because there was nothing to send yet."""
        self._insert_shift(TOMORROW)

        process_shift_reminders()

        mock_email.assert_not_called()
        self.assertIsNone(self._reminded_at(TOMORROW))

    @patch('shift_reminder.send_branded_email')
    def test_already_reminded_shift_is_not_resent(self, mock_email):
        self._set_contact(email='staff@example.com')
        self._insert_shift(TOMORROW)
        process_shift_reminders()
        mock_email.reset_mock()

        process_shift_reminders()  # a second cycle, same shift
        mock_email.assert_not_called()

    @patch('shift_reminder.send_branded_email')
    def test_shift_not_tomorrow_is_untouched(self, mock_email):
        self._set_contact(email='staff@example.com')
        self._insert_shift(TODAY)
        self._insert_shift(IN_3_DAYS)

        process_shift_reminders()

        mock_email.assert_not_called()
        self.assertIsNone(self._reminded_at(TODAY))
        self.assertIsNone(self._reminded_at(IN_3_DAYS))


if __name__ == '__main__':
    unittest.main()
