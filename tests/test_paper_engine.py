import unittest
from datetime import datetime, timedelta

from paper_engine import (
    COHORT_CONTROL,
    COHORT_PRIMARY,
    EXIT_STOP_LOSS,
    EXIT_TIME,
    EXIT_TRAILING_STOP,
    MAX_HOLD_HOURS,
    SIGNAL_CONFIRMED,
    SIGNAL_NEUTRAL,
    SIGNAL_OVERHEAT,
    SIGNAL_WATCH,
    SIGNAL_WEAK,
    STOP_LOSS_PERCENT,
    TRAILING_DISTANCE_PERCENT,
    TRAILING_START_PERCENT,
    calculate_drawdown_from_max_percent,
    calculate_profit_percent,
    classify_signal_type,
    decide_baseline_exit,
    elapsed_hours,
    is_active_tracking,
    is_expired_window,
    is_fresh_24h,
    observation_bucket,
    project_baseline,
    score_cohort,
)


ENTRY = datetime(2026, 10, 1, 12, 0, 0)


def baseline_position(**overrides):
    position = {
        "id": 1,
        "pair_id": 10,
        "entry_price": 1.0,
        "entry_time": ENTRY,
        "max_price": 1.0,
        "max_profit_percent": 0,
        "stop_loss_percent": STOP_LOSS_PERCENT,
        "trailing_start_percent": TRAILING_START_PERCENT,
        "trailing_distance_percent": TRAILING_DISTANCE_PERCENT,
        "max_hold_hours": MAX_HOLD_HOURS,
        "status": "OPEN",
    }
    position.update(overrides)
    return position


class SignalAndCohortTest(unittest.TestCase):
    def test_score_cohorts(self):
        cases = (
            (None, None),
            (69, None),
            (70, COHORT_CONTROL),
            (79, COHORT_CONTROL),
            (80, COHORT_PRIMARY),
            (100, COHORT_PRIMARY),
        )

        for final_score, expected in cases:
            with self.subTest(final_score=final_score):
                self.assertEqual(score_cohort(final_score), expected)

    def test_signal_bands(self):
        cases = (
            (None, None),
            (-0.01, SIGNAL_WEAK),
            (0, SIGNAL_NEUTRAL),
            (2.99, SIGNAL_NEUTRAL),
            (3, SIGNAL_CONFIRMED),
            (5.99, SIGNAL_CONFIRMED),
            (6, SIGNAL_WATCH),
            (9.99, SIGNAL_WATCH),
            (10, SIGNAL_OVERHEAT),
            (25, SIGNAL_OVERHEAT),
        )

        for change_24h, expected in cases:
            with self.subTest(change_24h=change_24h):
                self.assertEqual(classify_signal_type(change_24h), expected)

    def test_fresh_24h_window(self):
        checked_at = ENTRY
        self.assertTrue(is_fresh_24h(checked_at, ENTRY))
        self.assertTrue(is_fresh_24h(checked_at, ENTRY + timedelta(minutes=30)))
        self.assertFalse(
            is_fresh_24h(checked_at, ENTRY + timedelta(minutes=30, seconds=1))
        )
        self.assertFalse(is_fresh_24h(checked_at, ENTRY - timedelta(seconds=1)))


class PriceMathTest(unittest.TestCase):
    def test_profit_percent(self):
        self.assertEqual(calculate_profit_percent(1, 1.1), 10)
        self.assertEqual(calculate_profit_percent(2, 1.5), -25)
        self.assertEqual(calculate_profit_percent(1, 1), 0)
        self.assertIsNone(calculate_profit_percent(0, 1))
        self.assertIsNone(calculate_profit_percent(None, 1))

    def test_drawdown_from_max(self):
        self.assertEqual(calculate_drawdown_from_max_percent(1.1, 1.1), 0)
        self.assertEqual(calculate_drawdown_from_max_percent(1.1, 1.045), 5)
        self.assertEqual(calculate_drawdown_from_max_percent(1, 1.2), 0)

    def test_new_high_resets_drawdown_before_exit_check(self):
        projected = project_baseline(
            baseline_position(),
            1.2,
            ENTRY + timedelta(hours=1),
        )

        self.assertEqual(projected["max_price"], 1.2)
        self.assertEqual(projected["max_profit_percent"], 20)
        self.assertEqual(projected["drawdown_from_max_percent"], 0)
        self.assertIsNone(projected["exit_reason"])

    def test_observation_buckets(self):
        cases = (
            (datetime(2026, 10, 7, 10, 0, 0), "2026-10-07 10:00:00"),
            (datetime(2026, 10, 7, 10, 14, 59), "2026-10-07 10:00:00"),
            (datetime(2026, 10, 7, 10, 15, 0), "2026-10-07 10:15:00"),
            (datetime(2026, 10, 7, 10, 59, 59), "2026-10-07 10:45:00"),
            (datetime(2026, 10, 7, 11, 0, 0), "2026-10-07 11:00:00"),
        )

        for moment, expected in cases:
            with self.subTest(moment=moment):
                self.assertEqual(observation_bucket(moment), expected)


class BaselineExitTest(unittest.TestCase):
    def test_stop_loss_boundary(self):
        self.assertEqual(
            decide_baseline_exit(-15, 0, 15, 1, 15, 10, 5, 168),
            EXIT_STOP_LOSS,
        )
        self.assertIsNone(
            decide_baseline_exit(-14.99, 0, 14.99, 1, 15, 10, 5, 168)
        )

    def test_trailing_boundary(self):
        self.assertEqual(
            decide_baseline_exit(4.5, 10, 5, 1, 15, 10, 5, 168),
            EXIT_TRAILING_STOP,
        )
        self.assertIsNone(
            decide_baseline_exit(4.5, 9.99, 5, 1, 15, 10, 5, 168)
        )

    def test_time_exit_boundary(self):
        self.assertEqual(
            decide_baseline_exit(1, 1, 0, 168, 15, 10, 5, 168),
            EXIT_TIME,
        )
        self.assertIsNone(
            decide_baseline_exit(1, 1, 0, 167.9, 15, 10, 5, 168)
        )

    def test_stop_wins_over_trailing_and_time(self):
        self.assertEqual(
            decide_baseline_exit(-20, 20, 33, 168, 15, 10, 5, 168),
            EXIT_STOP_LOSS,
        )

    def test_trailing_wins_over_time(self):
        self.assertEqual(
            decide_baseline_exit(4, 12, 7, 168, 15, 10, 5, 168),
            EXIT_TRAILING_STOP,
        )

    def test_window_flags(self):
        deadline = ENTRY + timedelta(hours=168)
        self.assertTrue(is_active_tracking(ENTRY, deadline, 168))
        self.assertFalse(is_expired_window(ENTRY, deadline, 168))
        later = deadline + timedelta(seconds=1)
        self.assertFalse(is_active_tracking(ENTRY, later, 168))
        self.assertTrue(is_expired_window(ENTRY, later, 168))
        self.assertEqual(elapsed_hours(ENTRY, deadline), 168)


if __name__ == "__main__":
    unittest.main()
