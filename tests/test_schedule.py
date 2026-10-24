import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kilatdl as k  # noqa: E402


class Item:
    def __init__(self, status):
        self.status = status


def sched(**schedule):
    return k.Scheduler({"schedule": {"enabled": True, "start": "23:00", "stop": "", **schedule}, "after": "none"})


class WindowTest(unittest.TestCase):
    def test_disabled_is_always_open(self):
        s = sched(enabled=False)
        self.assertTrue(s.is_open("12:00", True))

    def test_start_and_stop_window(self):
        s = sched(start="09:00", stop="17:00")
        self.assertEqual([s.is_open(t, True) for t in ("08:59", "09:00", "16:59", "17:00")], [False, True, True, False])

    def test_window_wrapping_midnight(self):
        s = sched(start="23:00", stop="06:00")
        self.assertEqual([s.is_open(t, True) for t in ("22:59", "23:00", "02:00", "05:59", "06:00")],
                         [False, True, True, True, False])

    def test_start_only_releases_when_time_is_crossed_and_rearms_when_empty(self):
        s = sched(start="23:00")
        self.assertFalse(s.is_open("22:59", True))
        self.assertTrue(s.is_open("23:00", True))
        self.assertTrue(s.is_open("23:30", True), "stays released while work remains")
        self.assertFalse(s.is_open("23:31", False), "queue empty: armed again")
        self.assertFalse(s.is_open("23:32", True), "late additions wait for the next start time")

    def test_start_only_crossing_midnight(self):
        s = sched(start="00:00")
        s.is_open("23:59", True)
        self.assertTrue(s.is_open("00:00", True))

    def test_enabling_after_start_time_waits_for_next_day(self):
        s = sched(start="23:00")
        self.assertFalse(s.is_open("23:40", True))


class AfterActionTest(unittest.TestCase):
    def make(self, after="poweroff"):
        return k.Scheduler({"schedule": {"enabled": False, "start": "23:00", "stop": ""}, "after": after})

    def test_fires_once_when_batch_completes(self):
        s = self.make()
        self.assertIsNone(s.finished_action([Item("downloading"), Item("queued")]))
        self.assertEqual(s.finished_action([Item("done"), Item("done")]), "poweroff")
        self.assertIsNone(s.finished_action([Item("done"), Item("done")]))

    def test_manual_pause_never_powers_off(self):
        s = self.make()
        s.finished_action([Item("downloading"), Item("downloading")])
        self.assertIsNone(s.finished_action([Item("done"), Item("paused")]))

    def test_nothing_completed_means_no_action(self):
        s = self.make()
        s.finished_action([Item("downloading")])
        self.assertIsNone(s.finished_action([Item("error")]))

    def test_none_means_no_action(self):
        s = self.make("none")
        s.finished_action([Item("downloading")])
        self.assertIsNone(s.finished_action([Item("done")]))


if __name__ == "__main__":
    unittest.main()
