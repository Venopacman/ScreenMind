"""
An executor whose one worker thread never holds up the process exit.

asyncio.run() waits for the default executor's threads when it ends, and the
interpreter joins every ThreadPoolExecutor thread at exit, both with no
useful timeout. A Gemma call can run for minutes, so a normal stop waited
for the call in flight (about 40 s on the Windows laptop; over 45 s the
shutdown deadline forced exit code 3).

DaemonExecutor runs jobs one at a time on a single daemon thread, started on
the first job. shutdown(wait=False) drops the queued jobs and leaves the
running one behind: its result is never used, and the thread dies with the
process.
"""

import queue
import threading
from concurrent.futures import Executor, Future


class DaemonExecutor(Executor):
    def __init__(self, name: str):
        self._name = name
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._shutdown = False

    def submit(self, fn, /, *args, **kwargs) -> Future:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            future: Future = Future()
            self._jobs.put((future, fn, args, kwargs))
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, name=self._name, daemon=True)
                self._thread.start()
            return future

    def _work(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            future, fn, args, kwargs = job
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as e:
                future.set_exception(e)
            else:
                future.set_result(result)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False):
        """Stop taking jobs. cancel_futures cancels the queued ones, else they
        still run. wait=True waits for the thread to finish; wait=False
        returns at once and abandons the running job."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            while cancel_futures:
                try:
                    job = self._jobs.get_nowait()
                except queue.Empty:
                    break
                job[0].cancel()
            self._jobs.put(None)
            thread = self._thread
        if wait and thread is not None:
            thread.join()
