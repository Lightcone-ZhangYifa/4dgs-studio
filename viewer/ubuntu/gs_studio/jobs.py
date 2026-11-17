"""Latest-request scene reads and FIFO file commands with explicit ownership."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
import threading


@dataclass
class _Delivery:
    revision: int
    generation: int
    value: object
    error: Exception | None
    complete: object
    dispose: object


class LatestJob:
    """Keep one running operation and one replaceable pending operation.

    Call submit and close from the UI thread. dispatch must marshal a callable to
    that thread. Obsolete results are never delivered. dispose handles resources
    created by superseded requests (notably mmap-backed documents).
    """
    def __init__(self, dispatch, allow_intermediate=False):
        self.dispatch = dispatch
        self.allow_intermediate = allow_intermediate
        self.generation = 0
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gsv-scene")
        self.revision = 0
        self.running = False
        self.pending = None
        self.closed = False
        self._lock = threading.RLock()
        self._delivery = None
        self._shutdown = False

    def submit(self, work, complete, dispose=lambda result: None):
        with self._lock:
            if self.closed:
                return
            self.revision += 1
            self.pending = (self.revision, work, complete, dispose)
            self._start()

    def invalidate(self):
        with self._lock:
            self.revision += 1
            self.generation += 1
            self.pending = None

    def _start(self):
        with self._lock:
            # complete() is allowed to submit another request synchronously.
            # A second _start after that callback must not queue another worker.
            if self.running or self.closed or self.pending is None:
                return
            job = self.pending
            generation = self.generation
            self.pending = None
            self.running = True
            self.executor.submit(self._execute, job, generation)

    @staticmethod
    def _dispose(dispose, value):
        try:
            dispose(value)
        except Exception:
            logging.getLogger(__name__).exception("Failed to dispose obsolete worker result")

    def _stop_executor(self):
        # Caller holds _lock; all release tasks must have been queued first.
        if not self._shutdown:
            self._shutdown = True
            self.executor.shutdown(wait=False)

    def _execute(self, job, generation):
        revision, work, complete, dispose = job
        try:
            value, error = work(), None
        except Exception as exc:
            value, error = None, exc
        with self._lock:
            if self.closed:
                # The last window may already have stopped GTK's loop. Never
                # depend on an idle callback to release this result or shut down.
                self.running = False
                self._stop_executor()
                delivery = None
            else:
                delivery = _Delivery(revision, generation, value, error, complete, dispose)
                self._delivery = delivery
        if delivery is None:
            if value is not None:
                self._dispose(dispose, value)
            return
        self.dispatch(lambda: self._deliver(delivery))

    def _deliver(self, delivery):
        with self._lock:
            if self._delivery is not delivery:
                # close() already took ownership and scheduled its disposal.
                return False
            self._delivery = None
            self.running = False
            accepted = (not self.closed and delivery.generation == self.generation and
                        (self.allow_intermediate or delivery.revision == self.revision))
            if not accepted and delivery.value is not None:
                self.executor.submit(self._dispose, delivery.dispose, delivery.value)
        try:
            if accepted:
                delivery.complete(delivery.value, delivery.error)
        finally:
            self._start()
        return False

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self.invalidate()
            delivery, self._delivery = self._delivery, None
            if delivery is not None:
                if delivery.value is not None:
                    self.executor.submit(self._dispose, delivery.dispose, delivery.value)
                self.running = False
            if not self.running:
                self._stop_executor()
            # In-flight _execute performs disposal/shutdown itself, even when
            # the UI event loop no longer processes callbacks.


class CommandJobs:
    """Serialize every accepted file command, including across scene replacement.

    Submit, invalidate and close from the UI thread. Unlike scene-preview reads,
    a chosen file write must never be replaced by a newer request. Invalidating
    only suppresses old UI callbacks; closing drains queued commands without
    blocking the UI or requiring its event loop to keep running. Returned values
    retain ownership until their callback or worker disposal has completed.
    """
    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gsv-file")
        self.generation = self.revision = 0
        self.closed = False
        self._shutdown = False
        self._lock = threading.RLock()
        self._unfinished = 0
        self._deliveries = {}

    @property
    def running(self):
        with self._lock:
            return self._unfinished > 0

    def submit(self, work, complete, dispose=lambda result: None):
        with self._lock:
            if self.closed:
                return
            self.revision += 1
            revision, generation = self.revision, self.generation
            self._unfinished += 1
            self.executor.submit(self._execute, revision, generation, work, complete, dispose)

    def _release(self, delivery):
        try:
            if delivery.value is not None:
                LatestJob._dispose(delivery.dispose, delivery.value)
        finally:
            with self._lock:
                self._unfinished -= 1

    def _execute(self, revision, generation, work, complete, dispose):
        try:
            value, error = work(), None
        except Exception as exc:
            value, error = None, exc
        delivery = _Delivery(revision, generation, value, error, complete, dispose)
        with self._lock:
            obsolete = self.closed or generation != self.generation
            if not obsolete:
                self._deliveries[revision] = delivery
        if obsolete:
            # Already on the file worker. A closed GTK loop cannot retain an
            # accepted command's result, descriptor or executor lifetime.
            self._release(delivery)
        else:
            self.dispatch(lambda: self._deliver(delivery))

    def _deliver(self, delivery):
        with self._lock:
            if self._deliveries.pop(delivery.revision, None) is not delivery:
                # invalidate/close already owns disposal of this result.
                return False
            accepted = not self.closed and delivery.generation == self.generation
            if not accepted:
                self.executor.submit(self._release, delivery)
        if accepted:
            try:
                delivery.complete(delivery.value, delivery.error)
            finally:
                with self._lock:
                    self._unfinished -= 1
        return False

    def invalidate(self):
        with self._lock:
            if self.closed:
                return
            self.generation += 1
            self.revision += 1
            deliveries, self._deliveries = self._deliveries, {}
            # All accepted work remains in the executor queue. Only completed
            # values awaiting UI publication are transferred to worker disposal.
            for delivery in deliveries.values():
                self.executor.submit(self._release, delivery)

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self.generation += 1
            self.revision += 1
            deliveries, self._deliveries = self._deliveries, {}
            for delivery in deliveries.values():
                self.executor.submit(self._release, delivery)
            # cancel_futures stays False: accepted writes drain in FIFO order.
            # Their worker-side completion paths release resources directly.
            self.executor.shutdown(wait=False)
            self._shutdown = True
