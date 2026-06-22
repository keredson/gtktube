from __future__ import annotations

import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import thread as futures_thread


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor variant whose workers do not block interpreter exit."""

    def _adjust_thread_count(self) -> None:
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads >= self._max_workers:
            return

        thread_name = "%s_%d" % (self._thread_name_prefix or self, num_threads)
        thread = threading.Thread(
            name=thread_name,
            target=futures_thread._worker,
            args=(
                weakref.ref(self, weakref_cb),
                self._create_worker_context(),
                self._work_queue,
            ),
            daemon=True,
        )
        thread.start()
        self._threads.add(thread)
