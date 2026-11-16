"""Latest-request publication and resource lifetime without a GTK dependency."""
import queue
from pathlib import Path
import tempfile
import threading
import time
import unittest

from gs_studio.jobs import CommandJobs, LatestJob


class JobTests(unittest.TestCase):
    def setUp(self):
        self.dispatch = queue.Queue()
        self.job = LatestJob(self.dispatch.put)
        self.addCleanup(self.job.close)
        self.releases = []

    def deliver(self):
        self.dispatch.get(timeout=3)()

    def test_only_latest_pending_request_is_executed(self):
        release = threading.Event()
        self.addCleanup(release.set)
        entered = threading.Event()
        ran, published = [], []

        def first():
            ran.append(1)
            entered.set()
            release.wait(3)
            return 1

        self.job.submit(first, lambda value, error: published.append(value))
        self.assertTrue(entered.wait(3))
        self.job.submit(lambda: ran.append(2) or 2, lambda value, error: published.append(value))
        self.job.submit(lambda: ran.append(3) or 3, lambda value, error: published.append(value))
        release.set()
        self.deliver()
        self.deliver()
        self.assertEqual(ran, [1, 3])
        self.assertEqual(published, [3])

    def test_completed_superseded_result_disposes_on_worker(self):
        disposed = threading.Event()
        seen = []
        main_thread = threading.get_ident()

        def dispose(value):
            seen.append((value, threading.get_ident()))
            disposed.set()

        self.job.submit(lambda: "obsolete", lambda *_: self.fail("obsolete result published"), dispose)
        callback = self.dispatch.get(timeout=3)
        self.job.invalidate()
        callback()
        self.assertTrue(disposed.wait(3))
        self.assertEqual(seen[0][0], "obsolete")
        self.assertNotEqual(seen[0][1], main_thread)

    def test_close_releases_completed_result_without_running_dispatch(self):
        disposed = threading.Event()
        self.job.submit(lambda: "document", lambda *_: self.fail("closed result published"), lambda value: disposed.set())
        callback = self.dispatch.get(timeout=3)
        self.job.close()
        self.assertTrue(disposed.wait(3))
        callback()
        self.assertFalse(self.job.running)
        self.assertTrue(self.job._shutdown)

    def test_close_during_work_releases_without_dispatch(self):
        entered, release, disposed = [threading.Event() for _ in range(3)]
        self.addCleanup(release.set)

        def work():
            entered.set()
            release.wait(3)
            return object()

        self.job.submit(work, lambda *_: self.fail("closed result published"), lambda value: disposed.set())
        self.assertTrue(entered.wait(3))
        self.job.close()
        release.set()
        self.assertTrue(disposed.wait(3))
        self.assertTrue(self.dispatch.empty())
        self.assertTrue(self.job._shutdown)

    def test_continuous_playback_can_publish_intermediate_frames(self):
        self.job.allow_intermediate = True
        frames = []
        self.job.submit(lambda: 1, lambda value, error: frames.append(value))
        callback = self.dispatch.get(timeout=3)
        # A new playback tick arrives before delivery of the previous frame.
        self.job.submit(lambda: 2, lambda value, error: frames.append(value))
        callback()
        self.deliver()
        self.assertEqual(frames, [1, 2])

    def test_intermediate_publication_still_respects_document_generation(self):
        self.job.allow_intermediate = True
        disposed = threading.Event()
        self.job.submit(lambda: "previous scene", lambda *_: self.fail("foreign scene published"), lambda value: disposed.set())
        callback = self.dispatch.get(timeout=3)
        self.job.invalidate()
        callback()
        self.assertTrue(disposed.wait(3))

    def test_reentrant_submissions_preserve_single_pending_slot(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        ran, published = [], []

        def second():
            ran.append(2)
            entered.set()
            release.wait(3)
            return 2

        def complete(value, error):
            published.append(value)
            self.job.submit(second, lambda value, error: published.append(value))
            self.job.submit(lambda: ran.append(3) or 3, lambda value, error: published.append(value))

        self.job.submit(lambda: ran.append(1) or 1, complete)
        self.deliver()
        self.assertTrue(entered.wait(3))
        self.job.submit(lambda: ran.append(4) or 4, lambda value, error: published.append(value))
        release.set()
        self.deliver()
        self.deliver()
        self.assertEqual(ran, [1, 2, 4])
        self.assertEqual(published, [1, 4])

    def test_work_error_is_delivered_and_next_work_runs(self):
        results = []
        def fail():
            raise ValueError("bad scene")
        self.job.submit(fail, lambda value, error: results.append((value, error)))
        self.deliver()
        self.assertIsNone(results[0][0])
        self.assertIsInstance(results[0][1], ValueError)
        self.job.submit(lambda: 2, lambda value, error: results.append((value, error)))
        self.deliver()
        self.assertEqual(results[1], (2, None))

    def test_completion_can_close_executor(self):
        seen = []
        def complete(value, error):
            seen.append(value)
            self.job.close()
        self.job.submit(lambda: 3, complete)
        self.deliver()
        self.assertEqual(seen, [3])
        self.assertTrue(self.job._shutdown)
        self.job.submit(lambda: self.fail("work accepted after close"), complete)
        self.assertIsNone(self.job.pending)


class FileCommandTests(unittest.TestCase):
    def setUp(self):
        self.dispatch = queue.Queue()
        self.job = CommandJobs(self.dispatch.put)
        self.addCleanup(self.job.close)

    def wait_until(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if condition():
                return
            threading.Event().wait(.001)
        self.fail("File command did not finish")

    def test_accepted_writes_survive_source_replacement_and_new_command(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        written, published, disposed = [], [], []
        with tempfile.TemporaryDirectory() as directory:
            def write(number):
                if number == 1:
                    entered.set()
                    release.wait(3)
                (Path(directory) / f"chosen-{number}.png").write_bytes(bytes([number]))
                written.append(number)
                return number
            self.job.submit(lambda: write(1), lambda value, error: published.append(value), disposed.append)
            self.assertTrue(entered.wait(3))
            self.job.submit(lambda: write(2), lambda value, error: published.append(value), disposed.append)
            # Changing the source must fence stale UI state without silently
            # throwing away the user's already chosen second export.
            self.job.invalidate()
            self.job.submit(lambda: write(3), lambda value, error: published.append(value), disposed.append)
            release.set()
            callback = self.dispatch.get(timeout=3)
            self.assertTrue(self.job.running, "A completed value still awaits its UI owner")
            callback()
            self.wait_until(lambda: not self.job.running)
            self.assertEqual(written, [1, 2, 3])
            self.assertEqual(published, [3])
            self.assertEqual(disposed, [1, 2])
            for number in (1, 2, 3):
                self.assertEqual((Path(directory) / f"chosen-{number}.png").read_bytes(), bytes([number]))

    def test_close_drains_every_accepted_command_without_gtk_callbacks(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        written, disposed = [], []
        main = threading.get_ident()
        def write(number):
            if number == 1:
                entered.set()
                release.wait(3)
            written.append(number)
            return number
        def dispose(value):
            disposed.append((value, threading.get_ident()))
        for number in (1, 2, 3):
            self.job.submit(lambda n=number: write(n), lambda *_: self.fail("Closed UI callback ran"), dispose)
        self.assertTrue(entered.wait(3))
        self.job.close()
        self.assertFalse(release.is_set())
        self.assertTrue(self.job.running)
        release.set()
        self.wait_until(lambda: not self.job.running)
        self.assertEqual(written, [1, 2, 3])
        self.assertEqual([value for value, _ in disposed], [1, 2, 3])
        self.assertTrue(all(thread != main for _, thread in disposed))
        self.assertTrue(self.dispatch.empty())
        self.assertTrue(self.job._shutdown)
        self.job.submit(lambda: self.fail("Accepted a write after closing"), lambda *_: None)

    def test_invalidation_disposes_completed_values_without_delivering_idle(self):
        disposed = threading.Event()
        self.job.submit(lambda: object(), lambda *_: self.fail("Stale UI callback ran"), lambda value: disposed.set())
        callback = self.dispatch.get(timeout=3)
        self.job.invalidate()
        self.assertTrue(disposed.wait(3))
        self.wait_until(lambda: not self.job.running)
        callback()
        self.assertFalse(self.job.running)

    def test_close_claims_already_dispatched_value_exactly_once(self):
        disposed = []
        self.job.submit(lambda: 7, lambda *_: self.fail("Closed UI callback ran"), disposed.append)
        callback = self.dispatch.get(timeout=3)
        self.job.close()
        self.wait_until(lambda: not self.job.running)
        callback()
        self.assertEqual(disposed, [7])

    def test_errors_do_not_abort_queued_commands_and_publication_stays_fifo(self):
        results = []
        def fail():
            raise OSError("Cannot save chosen file")
        self.job.submit(fail, lambda value, error: results.append((value, error)))
        self.job.submit(lambda: 2, lambda value, error: results.append((value, error)))
        first, second = self.dispatch.get(timeout=3), self.dispatch.get(timeout=3)
        first()
        self.assertTrue(self.job.running)
        second()
        self.assertFalse(self.job.running)
        self.assertIsInstance(results[0][1], OSError)
        self.assertEqual(results[1], (2, None))

    def test_callback_can_close_and_queued_write_still_finishes(self):
        results, disposed = [], []
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def first_done(value, error):
            results.append(value)
            self.job.close()
        def second():
            entered.set()
            release.wait(3)
            return 2
        self.job.submit(lambda: 1, first_done)
        self.job.submit(second, lambda *_: self.fail("Closed callback ran"), disposed.append)
        self.assertTrue(entered.wait(3))
        self.dispatch.get(timeout=3)()
        release.set()
        self.wait_until(lambda: not self.job.running)
        self.assertEqual(results, [1])
        self.assertEqual(disposed, [2])


if __name__ == "__main__":
    unittest.main()
