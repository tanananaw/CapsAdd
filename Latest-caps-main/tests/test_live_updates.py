import unittest
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from app import app
import live_updates


class LiveUpdatesTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self._heartbeat = live_updates.HEARTBEAT_SECONDS
        live_updates.HEARTBEAT_SECONDS = 0.2

    def tearDown(self):
        live_updates.HEARTBEAT_SECONDS = self._heartbeat

    def test_stream_delivers_data_to_everyone_and_notifications_to_their_owner(self):
        mine, other = live_updates.stream(7), live_updates.stream(8)
        self.assertTrue(next(mine).startswith('retry:'))
        self.assertTrue(next(other).startswith('retry:'))

        live_updates.publish('data')
        self.assertEqual(next(mine), 'event: data\ndata: {}\n\n')
        self.assertEqual(next(other), 'event: data\ndata: {}\n\n')

        live_updates.publish('notifications', [7])
        self.assertEqual(next(mine), 'event: notifications\ndata: {}\n\n')
        self.assertEqual(next(other), ': keep-alive\n\n', "someone else's notification isn't sent")

    def test_bursts_are_coalesced(self):
        s = live_updates.stream(7)
        next(s)
        for _ in range(5):
            live_updates.publish('data')
        self.assertEqual(next(s), 'event: data\ndata: {}\n\n')
        self.assertEqual(next(s), ': keep-alive\n\n', "five changes -> one refresh")

    def test_only_real_writes_are_announced(self):
        self.assertTrue(live_updates.is_announced_write('POST', '/api/cancel-booking'))
        self.assertFalse(live_updates.is_announced_write('GET', '/api/get-queue'))
        self.assertFalse(live_updates.is_announced_write('POST', '/api/predict-ewt'))
        self.assertFalse(live_updates.is_announced_write('POST', '/api/notifications/mark-read'))

    def test_successful_api_write_publishes_and_failed_one_does_not(self):
        with self.client.session_transaction() as sess:
            sess.update(user='manager', role='admin', fullname='System Manager', user_id=1)
        before = live_updates._sequence
        self.client.post('/api/cancel-booking', json={})  # 400 - nothing changed
        self.assertEqual(live_updates._sequence, before)
        self.client.post('/api/admin/update-service-price', json={'service_id': 1, 'vehicle_type': 'Medium', 'price': 150})
        self.assertEqual(live_updates._sequence, before + 1)

    def test_endpoint_requires_login_and_streams_events(self):
        self.assertEqual(self.client.get('/api/live').status_code, 401)
        with self.client.session_transaction() as sess:
            sess.update(user='customer_user', role='customer', fullname='Default Customer', user_id=2)
        res = self.client.get('/api/live')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, 'text/event-stream')
        res.close()


if __name__ == '__main__':
    unittest.main()
