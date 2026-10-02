import unittest
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, '_Backend'))

from ai_engine import StaffMCDAEngine, WaitTimePredictorAI


class WaitTimePredictorTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.predictor = WaitTimePredictorAI()

    def test_predicted_wait_never_below_floor(self):
        """predict_ewt clips to a 5-minute floor regardless of input."""
        result = self.predictor.predict_ewt(1, 6, 0.0, 20)
        self.assertGreaterEqual(result, 5.0)

    def test_larger_queue_with_same_staff_increases_or_holds_wait(self):
        """A much longer queue with the same staffing should not predict a
        shorter wait than a short queue - sanity check on the trained model,
        not a strict monotonicity guarantee."""
        short_queue = self.predictor.predict_ewt(1, 2, 0.0, 20)
        long_queue = self.predictor.predict_ewt(10, 2, 0.0, 20)
        self.assertGreaterEqual(long_queue, short_queue)

    def test_missing_service_duration_falls_back_to_default(self):
        """A missing/invalid service duration must not raise - it falls back
        to DEFAULT_SERVICE_MINUTES rather than crash a booking."""
        for minutes in (None, 0, 'not-a-number'):
            with self.subTest(minutes=minutes):
                result = self.predictor.predict_ewt(3, 2, 0.0, minutes)
                self.assertEqual(result, self.predictor.predict_ewt(3, 2, 0.0, self.predictor.DEFAULT_SERVICE_MINUTES))

    def test_longer_service_predicts_a_longer_wait(self):
        """The real service duration is a model input - a 180-minute
        detailing job with the same queue must not predict a shorter wait
        than a 15-minute wash."""
        self.assertGreater(self.predictor.predict_ewt(5, 2, 0.0, 180), self.predictor.predict_ewt(5, 2, 0.0, 15))


class WaitTimeRetrainingTestCase(unittest.TestCase):
    def setUp(self):
        self.predictor = WaitTimePredictorAI()

    def test_below_threshold_real_samples_does_not_retrain(self):
        """Too few real rows relative to noise - the existing model must be
        left untouched rather than retrained on a handful of samples."""
        original_model = self.predictor.model
        few_rows = [
            {'queue_length': 3, 'active_staff': 2, 'precipitation': 0.0, 'service_base_time': 20, 'actual_wait_time': 45}
            for _ in range(self.predictor.MIN_REAL_SAMPLES_TO_RETRAIN - 1)
        ]
        retrained = self.predictor.retrain_with_real_data(few_rows)
        self.assertFalse(retrained)
        self.assertIs(self.predictor.model, original_model)

    def test_at_threshold_real_samples_retrains(self):
        """Enough real rows - retraining should actually swap in a new
        model and record how many real samples it used."""
        original_model = self.predictor.model
        enough_rows = [
            {'queue_length': i % 10 + 1, 'active_staff': 3, 'precipitation': 0.0, 'service_base_time': 20, 'actual_wait_time': 30 + i}
            for i in range(self.predictor.MIN_REAL_SAMPLES_TO_RETRAIN)
        ]
        retrained = self.predictor.retrain_with_real_data(enough_rows)
        self.assertTrue(retrained)
        self.assertIsNot(self.predictor.model, original_model)
        self.assertEqual(self.predictor.last_retrain_sample_count, self.predictor.MIN_REAL_SAMPLES_TO_RETRAIN)

    def test_enough_real_data_trains_on_real_only(self):
        few = [
            {'queue_length': i % 10 + 1, 'active_staff': 3, 'precipitation': 0.0, 'service_base_time': 20, 'actual_wait_time': 30 + i}
            for i in range(self.predictor.MIN_REAL_SAMPLES_TO_RETRAIN)
        ]
        self.predictor.retrain_with_real_data(few)
        self.assertEqual(self.predictor.trained_on, 'blended')
        many = few * (self.predictor.REAL_ONLY_MIN_SAMPLES // len(few) + 1)
        self.predictor.retrain_with_real_data(many)
        self.assertEqual(self.predictor.trained_on, 'real')

    def test_retrained_model_still_predicts_normally(self):
        """A retrain must not leave the predictor in a broken state - it
        should keep answering predict_ewt() calls afterward."""
        enough_rows = [
            {'queue_length': i % 10 + 1, 'active_staff': 3, 'precipitation': 0.0, 'service_base_time': 20, 'actual_wait_time': 30 + i}
            for i in range(self.predictor.MIN_REAL_SAMPLES_TO_RETRAIN)
        ]
        self.predictor.retrain_with_real_data(enough_rows)
        result = self.predictor.predict_ewt(3, 2, 0.0, 20)
        self.assertIsInstance(result, float)
        self.assertGreaterEqual(result, 5.0)


class StaffMCDAEngineTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = StaffMCDAEngine()

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(self.engine.evaluate_staff([]), [])

    def test_ranks_highest_performer_first(self):
        staff_metrics = [
            {'name': 'Low Performer', 'service_speed': 40, 'completion': 60, 'feedback': 3.0},
            {'name': 'Top Performer', 'service_speed': 95, 'completion': 98, 'feedback': 5.0},
            {'name': 'Mid Performer', 'service_speed': 70, 'completion': 80, 'feedback': 4.0},
        ]
        ranked = self.engine.evaluate_staff(staff_metrics)

        self.assertEqual(len(ranked), 3)
        self.assertEqual(ranked[0]['name'], 'Top Performer')
        self.assertEqual(ranked[0]['rank'], 1)
        self.assertEqual(ranked[-1]['name'], 'Low Performer')
        # Scores must be in strictly descending order.
        scores = [r['saw_score'] for r in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(self.engine.weights.values()), 1.0)


if __name__ == '__main__':
    unittest.main()
