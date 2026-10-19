"""Offline checks of the CUDA SDK free-space gate; nothing is downloaded."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from h7607a import cuda_sdk


class Downloading(Exception):
    pass


class FreeSpaceTest(unittest.TestCase):
    GiB = 1024**3

    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.home)
        self.lock = cuda_sdk.read_lock()
        for name, replacement in (("user_home", lambda: self.home),
                                  ("component_objects", mock.Mock(side_effect=Downloading))):
            patcher = mock.patch.object(cuda_sdk, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def install(self, prefix_free, cache_free, *, same_filesystem=False):
        prefix = self.home / ".local/opt"

        def filesystem(path):
            if same_filesystem or Path(path).is_relative_to(prefix):
                return prefix_free, 1
            return cache_free, 2
        with mock.patch.object(cuda_sdk, "filesystem", filesystem):
            cuda_sdk.install_sdk(online=True)

    def test_small_prefix_filesystem_is_refused_before_download(self):
        with self.assertRaises(RuntimeError):
            self.install(self.lock["reserve_bytes"] - 1, 100 * self.GiB)
        cuda_sdk.component_objects.assert_not_called()
        self.assertFalse((self.home / ".local").exists())

    def test_small_separate_cache_filesystem_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.install(self.lock["reserve_bytes"], self.lock["download_bytes"] - 1)
        cuda_sdk.component_objects.assert_not_called()

    def test_enough_space_reaches_download(self):
        with self.assertRaises(Downloading):
            self.install(self.lock["reserve_bytes"], self.lock["download_bytes"])

    def test_shared_filesystem_needs_only_the_reserve(self):
        with self.assertRaises(Downloading):
            self.install(self.lock["reserve_bytes"], 0, same_filesystem=True)

    def test_missing_path_uses_nearest_existing_directory(self):
        free, device = cuda_sdk.filesystem(self.home / "a/b/c")
        self.assertEqual(device, os.stat(self.home).st_dev)
        self.assertGreaterEqual(free, 0)


if __name__ == "__main__":
    unittest.main()
