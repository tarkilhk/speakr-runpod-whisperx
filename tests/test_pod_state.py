import asyncio
import tempfile
import unittest
from pathlib import Path

from adapter.pod_state import ActivePodStore, DeployLock


class ActivePodStoreTests(unittest.TestCase):
    def test_configured_pod_id_takes_precedence_over_file_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "active-pod"
            path.write_text("file-pod\n", encoding="utf-8")

            store = ActivePodStore("fixed-pod", str(path))

            self.assertEqual(store.load(), "fixed-pod")

    def test_configured_pod_id_survives_clear_then_load(self) -> None:
        store = ActivePodStore("fixed-pod", "")

        store.clear("fixed-pod")

        self.assertEqual(store.load(), "fixed-pod")

    def test_template_store_round_trips_file_backed_pod_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "active-pod"
            store = ActivePodStore("", str(path))

            store.store("deployed-pod")

            self.assertEqual(store.load(), "deployed-pod")
            self.assertEqual(path.read_text(encoding="utf-8"), "deployed-pod")

    def test_clear_only_removes_matching_template_pod_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "active-pod"
            store = ActivePodStore("", str(path))
            store.store("current-pod")

            store.clear("other-pod")
            self.assertEqual(store.load(), "current-pod")
            self.assertTrue(path.exists())

            store.clear("current-pod")
            self.assertEqual(store.load(), "")
            self.assertFalse(path.exists())

    def test_configured_pod_id_is_not_overwritten_or_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "active-pod"
            store = ActivePodStore("fixed-pod", str(path))

            with self.assertLogs("whisperx-adapter.pod_state", level="WARNING"):
                store.store("template-pod")
            store.clear("fixed-pod")

            self.assertEqual(store.load(), "fixed-pod")
            self.assertFalse(path.exists())


class DeployLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_context_manager_hides_lock_file_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock = DeployLock(str(Path(tmp_dir) / "active-pod"))

            async with lock():
                self.assertTrue((Path(tmp_dir) / "active-pod.deploy.lock").exists())

            async with lock():
                self.assertTrue((Path(tmp_dir) / "active-pod.deploy.lock").exists())


if __name__ == "__main__":
    unittest.main()
