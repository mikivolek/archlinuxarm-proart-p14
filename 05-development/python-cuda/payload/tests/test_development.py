"""Offline checks of where install_selected obtains the PyTorch wheels."""
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from h7607a import development

VENV = "/home/owner/.local/share/h7607a/venvs/torch"


class TorchSourceTest(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, root)
        self.target = root / "target"
        (self.target / "etc").mkdir(parents=True)
        (self.target / "etc/passwd").write_text("root:x:0:0::/root:/bin/bash\n"
                                                "owner:x:1000:1000::/home/owner:/bin/bash\n")
        (self.target / "home/owner").mkdir(parents=True)
        self.wheelhouse = root / "wheels"
        self.wheelhouse.mkdir()
        self.commands = []
        patcher = mock.patch.object(development, "fetch_object", side_effect=AssertionError("live download"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, **options):
        return development.install_selected(self.target, "owner", ["ai"], REPO,
                                            runner=lambda argv, check: self.commands.append(argv), **options)

    def test_online_install_runs_as_the_user_inside_the_target(self):
        result = self.install(online=True)
        [argv] = self.commands
        chroot = argv.index("arch-chroot")
        # Without -r, arch-chroot binds the live resolver configuration.
        self.assertEqual(argv[chroot + 1], str(self.target))
        self.assertEqual(argv[chroot + 2:], ["/usr/bin/runuser", "-u", "owner", "--", "/usr/local/bin/h7607a-dev",
                                             "install-torch", "--online", "--venv", VENV])
        self.assertTrue(result["ai_environment_installed"])
        self.assertTrue((self.target / "usr/local/bin/h7607a-dev").is_file())

    def test_no_wheelhouse_requires_explicit_online(self):
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual(self.commands, [])

    def test_incomplete_wheelhouse_never_downloads_on_the_live_system(self):
        with self.assertRaises(RuntimeError):
            self.install(wheelhouse=self.wheelhouse, online=True)
        self.assertEqual(self.commands, [])


if __name__ == "__main__":
    unittest.main()
