from __future__ import annotations

import threading
import unittest
from concurrent.futures import thread as futures_thread

from gtktube.daemon_executor import DaemonThreadPoolExecutor


class DaemonThreadPoolExecutorTests(unittest.TestCase):
    def test_worker_threads_are_daemon_and_not_registered_for_exit_join(self) -> None:
        with DaemonThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(threading.current_thread)
            worker = future.result(timeout=1)

        self.assertTrue(worker.daemon)
        self.assertNotIn(worker, futures_thread._threads_queues)


if __name__ == "__main__":
    unittest.main()
