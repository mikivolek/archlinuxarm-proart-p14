"""Offline checks of the Archinstall adapter; archinstall itself is never imported."""
import contextlib
import hashlib
import importlib
import io
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock
from urllib.error import URLError

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from h7607a import archinstall_adapter as adapter
from h7607a import cuda_sdk, development, hardware, offline_locale, policy
from h7607a.archinstall_adapter import InstallError

DISK = "/dev/h7607a-test-ssd"


def temporary_directory(test):
    path = Path(tempfile.mkdtemp())
    test.addCleanup(shutil.rmtree, path)
    return path


class Stub:
    def __init__(self, *args, **kwargs):
        pass


def archinstall_stubs():
    """Stand-ins for the names make_native_components imports from archinstall."""
    names = {
        "archinstall.lib.global_menu": {"GlobalMenu": type("GlobalMenu", (Stub,), {})},
        "archinstall.lib.mirror.mirror_handler": {"MirrorListHandler": type("MirrorListHandler", (Stub,), {})},
        "archinstall.lib.mirror.mirror_menu": {"MirrorMenu": type("MirrorMenu", (Stub,), {})},
        "archinstall.lib.models.mirrors": {"MirrorStatusEntryV3": type("MirrorStatusEntryV3", (Stub,), {})},
        "archinstall.lib.models.bootloader": {"Bootloader": SimpleNamespace(Systemd="Systemd-boot"),
                                              "BootloaderConfiguration": Stub},
        "archinstall.lib.profile.profiles_handler": {"profile_handler": SimpleNamespace(profiles=[])},
        "archinstall.lib.plugins": {"plugins": {}},
        "archinstall.tui.components": {"tui": Stub()},
        "archinstall.lib.menu.helpers": {"Selection": Stub},
        "archinstall.tui.menu_item": {"MenuItem": Stub, "MenuItemGroup": Stub},
        "archinstall.tui.result": {"ResultType": SimpleNamespace()},
        "archinstall.lib.installer": {"Installer": type("Installer", (Stub,), {})},
        "archinstall.lib.command": {"SysCommand": Stub},
        "archinstall.scripts.guided": {},
    }
    modules = {}
    for dotted, attributes in names.items():
        parts = dotted.split(".")
        for index in range(1, len(parts) + 1):
            modules.setdefault(".".join(parts[:index]), ModuleType(".".join(parts[:index])))
        for name, value in attributes.items():
            setattr(modules[dotted], name, value)
    modules["archinstall.scripts"].guided = modules["archinstall.scripts.guided"]
    return modules


def hardware_plugin(test):
    """Register the adapter's plugin against stubbed archinstall modules."""
    modules = archinstall_stubs()
    patcher = mock.patch.dict(sys.modules, modules)
    patcher.start()
    test.addCleanup(patcher.stop)
    adapter.make_native_components(REPO, adapter.load_lock(REPO))
    return modules["archinstall.lib.plugins"].plugins["h7607a"]


class Partition:
    """The PartitionModification surface validate_configuration reads."""

    def __init__(self, mountpoint, fs_type, *, efi=False, root=False):
        self.mountpoint = Path(mountpoint)
        self.fs_type = fs_type
        self.dev_path = None
        self.btrfs_subvols = []
        self.length = None
        self.efi = efi
        self.root = root

    def is_exists_or_modify(self):
        return False

    def is_delete(self):
        return False

    def is_efi(self):
        return self.efi

    def is_boot(self):
        return self.efi

    def is_root(self):
        return self.root


def installable_config(**changes):
    partitions = [Partition("/boot", "fat32", efi=True), Partition("/", "ext4", root=True)]
    disk = SimpleNamespace(config_type="default_layout", lvm_config=None, disk_encryption=None,
                           device_modifications=[SimpleNamespace(device_path=DISK, partitions=partitions)])
    config = SimpleNamespace(
        disk_config=disk, kernels=["linux-n1x-nvidia-bin"], profile_config=None, mirror_config=None,
        bootloader_config=SimpleNamespace(bootloader="Systemd-boot", uki=False, plymouth=None),
        auth_config=SimpleNamespace(users=[SimpleNamespace(username="owner")], u2f_config=None),
        packages=[], development_profiles=[], network_config=None, app_config=None,
        locale_config=SimpleNamespace(kb_layout="us", sys_lang="en_US.UTF-8", sys_enc="UTF-8",
                                      console_font="default8x16"))
    for name, value in changes.items():
        setattr(config, name, value)
    return config


class Response:
    def __init__(self, body):
        self.status = 200
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        return False

    def read(self, size):
        return self.body[:size]


class OnlineTest(unittest.TestCase):
    DE3, FL = adapter.MIRROR_PROBES
    UNREACHABLE = URLError(OSError(101, "Network is unreachable"))
    UNTRUSTED = URLError(ssl.SSLCertVerificationError(1, "certificate verify failed: certificate is not yet valid"))

    def probe(self, outcomes):
        def urlopen(url, timeout):
            outcome = outcomes[url]
            if isinstance(outcome, Exception):
                raise outcome
            return Response(outcome)
        with mock.patch.object(adapter, "urlopen", urlopen):
            return adapter.verify_online()

    def test_either_mirror_is_enough(self):
        self.assertTrue(self.probe({self.DE3: self.UNREACHABLE, self.FL: b"core.db"}))
        self.assertTrue(self.probe({self.DE3: self.UNTRUSTED, self.FL: b"core.db"}))

    def test_unreachable_mirrors_are_offline(self):
        self.assertFalse(self.probe({self.DE3: self.UNREACHABLE, self.FL: self.UNREACHABLE}))

    def test_certificate_failure_is_reported_as_clock_error(self):
        with self.assertRaises(InstallError):
            self.probe({self.DE3: self.UNTRUSTED, self.FL: self.UNREACHABLE})

    def test_clock_error_skips_network_setup(self):
        with mock.patch.object(adapter, "verify_online", side_effect=InstallError("clock")), \
                mock.patch.object(adapter.subprocess, "run") as run:
            with self.assertRaises(InstallError):
                adapter.connect_network()
        run.assert_not_called()


class ValidateConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.graph = adapter.BlockGraph({"blockdevices": [{"path": DISK, "type": "disk", "tran": "nvme"}]})
        self.lock = adapter.load_lock(REPO)
        patcher = mock.patch.object(offline_locale, "validate_choices")
        self.validate_choices = patcher.start()
        self.addCleanup(patcher.stop)

    def validate(self, config):
        return adapter.validate_configuration(config, self.graph, self.lock)

    def test_installable_configuration_passes(self):
        config = installable_config()
        self.assertEqual(self.validate(config), DISK)
        self.validate_choices.assert_called_once_with(Path("/"), config.locale_config)

    def test_unavailable_locale_is_refused_before_partitioning(self):
        self.validate_choices.side_effect = ValueError("no such locale")
        with self.assertRaises(InstallError):
            self.validate(installable_config())

    def power_management(self, choice):
        # PowerManagementConfiguration.power_management is a StrEnum member.
        return SimpleNamespace(power_management_config=SimpleNamespace(
            power_management=SimpleNamespace(value=choice)))

    def test_tuned_application_is_refused(self):
        with self.assertRaises(InstallError):
            self.validate(installable_config(app_config=self.power_management("tuned")))

    def test_power_profiles_daemon_application_passes(self):
        self.validate(installable_config(app_config=self.power_management("power-profiles-daemon")))

    def test_unconfigured_applications_pass(self):
        # The global menu's Applications item defaults to an empty list.
        self.validate(installable_config(app_config=[]))

    def test_shipped_development_locks_pass(self):
        self.validate(installable_config(development_profiles=["python", "ai", "cuda"]))

    def test_broken_development_lock_is_refused(self):
        with mock.patch.object(development, "read_lock", side_effect=RuntimeError("lock differs")):
            with self.assertRaises(InstallError):
                self.validate(installable_config(development_profiles=["python"]))

    def test_cuda_lock_is_read_only_for_cuda(self):
        with mock.patch.object(cuda_sdk, "read_lock", side_effect=RuntimeError("lock differs")) as read:
            self.validate(installable_config(development_profiles=["ai"]))
            read.assert_not_called()
            with self.assertRaises(InstallError):
                self.validate(installable_config(development_profiles=["cuda"]))


class ArchinstallVersionTest(unittest.TestCase):
    API = b"def main():\n    pass\n"

    def setUp(self):
        root = temporary_directory(self)
        self.package = root / "archinstall"
        self.package.mkdir()
        (self.package / "__init__.py").write_bytes(b"")
        (self.package / "main.py").write_bytes(self.API)
        self.metadata = root / "archinstall-4.5.dist-info"
        self.metadata.mkdir()
        self.write_version("4.5")
        sys.path.insert(0, str(root))
        self.addCleanup(sys.path.remove, str(root))
        self.lock = {"version": "4.5", "package_version": "4.5-1",
                     "api_files": [{"path": "main.py", "sha256": hashlib.sha256(self.API).hexdigest()}]}

    def write_version(self, version):
        (self.metadata / "METADATA").write_text(f"Metadata-Version: 2.1\nName: archinstall\nVersion: {version}\n")
        importlib.invalidate_caches()

    def test_pinned_release_is_located_without_import(self):
        self.assertEqual(adapter.verify_archinstall(self.lock), self.package)
        self.assertNotIn("archinstall", sys.modules)

    def test_other_version_is_refused(self):
        self.write_version("4.6")
        with self.assertRaises(InstallError):
            adapter.verify_archinstall(self.lock)

    def test_changed_api_file_is_refused(self):
        (self.package / "main.py").write_bytes(self.API + b"# local edit\n")
        with self.assertRaises(InstallError):
            adapter.verify_archinstall(self.lock)

    def test_missing_api_file_is_refused(self):
        (self.package / "main.py").unlink()
        with self.assertRaises(InstallError):
            adapter.verify_archinstall(self.lock)


class DeviceTest(unittest.TestCase):
    def setUp(self):
        root = temporary_directory(self)
        self.repo = root / "repo"
        (self.repo / "locks").mkdir(parents=True)
        device = {"sys_vendor": "ASUS", "product_name": "ProArt P14 H7407BA",
                  "board_name": "H7407BA", "bios_version": "H7407BA.303"}
        (self.repo / "locks/hardware.json").write_text(json.dumps({"device": device}))
        self.dmi = root / "dmi"
        self.dmi.mkdir()

    def identify(self, board, bios):
        (self.dmi / "board_name").write_text(board + "\n")
        (self.dmi / "bios_version").write_text(bios + "\n")

    def test_shipped_lock_accepts_h7407ba_303(self):
        self.identify("H7407BA", "H7407BA.303")
        adapter.verify_device(REPO, self.dmi)

    def test_shipped_lock_refuses_the_p16(self):
        self.identify("H7607BA", "H7607BA.300")
        with self.assertRaises(InstallError):
            adapter.verify_device(REPO, self.dmi)

    def test_other_firmware_is_refused(self):
        self.identify("H7407BA", "H7407BA.304")
        with self.assertRaises(InstallError):
            adapter.verify_device(self.repo, self.dmi)

    def test_other_board_is_refused(self):
        self.identify("H7607BA", "H7407BA.303")
        with self.assertRaises(InstallError):
            adapter.verify_device(self.repo, self.dmi)

    def test_unreadable_identity_is_refused(self):
        with self.assertRaises(InstallError):
            adapter.verify_device(self.repo, self.dmi)


class FinishRepositoryTest(unittest.TestCase):
    FINGERPRINT = "88464154799297F6EF8D465275EEE3E151B90D4A"
    CONFIG = ("[options]\nHoldPkg = pacman glibc\nIgnorePkg = vim\n\n"
              "[h7607a]\nSigLevel = Required DatabaseRequired\nServer = file:///opt/h7607a-arch/build/repo\n\n"
              "[core]\nInclude = /etc/pacman.d/mirrorlist\n")

    def setUp(self):
        root = temporary_directory(self)
        self.repo = root / "repo"
        (self.repo / "locks").mkdir(parents=True)
        (self.repo / "assets/keys").mkdir(parents=True)
        (self.repo / "assets/keys/h7607a-build.gpg").write_bytes(b"public key")
        (self.repo / "locks/packages.json").write_text(json.dumps({"repository": "h7607a", "signing_keys": {
            "local": {"path": "assets/keys/h7607a-build.gpg", "fingerprint": self.FINGERPRINT}}}))
        self.target = root / "target"
        (self.target / "etc").mkdir(parents=True)
        (self.target / "etc/pacman.conf").write_text(self.CONFIG)
        self.lock = {"hardware_packages": {"linux-n1x-nvidia-bin": "7.0.0.1022.23-3", "h7607a-hardware": "1.0.0-3"}}
        self.calls = []
        self.key_present = True

    def finish(self):
        def run(target, argv, **options):
            repository_listed = "[h7607a]" in (self.target / "etc/pacman.conf").read_text()
            self.calls.append((argv[1:], repository_listed))
            if argv[1] == "--list-keys" and not self.key_present:
                raise subprocess.CalledProcessError(2, argv)
            return ""
        with mock.patch.object(hardware, "_run", run):
            adapter.finish_target_repository(self.target, self.repo, self.lock)

    def test_build_key_is_deleted_after_the_repository_is_removed(self):
        self.finish()
        self.assertEqual(self.calls, [(["--init"], True), (["--populate", "archlinuxarm"], True),
                                      (["--list-keys", self.FINGERPRINT], False),
                                      (["--delete", self.FINGERPRINT], False)])
        config = (self.target / "etc/pacman.conf").read_text()
        self.assertIn("IgnorePkg = h7607a-hardware linux-n1x-nvidia-bin vim\n", config)
        self.assertIn("[core]\n", config)
        self.assertEqual((self.target / "usr/local/share/h7607a/package-signing.gpg").read_bytes(), b"public key")

    def test_absent_build_key_is_not_deleted(self):
        self.key_present = False
        self.finish()
        self.assertNotIn((["--delete", self.FINGERPRINT], False), self.calls)

    def test_key_listed_by_a_shipped_keyring_is_kept(self):
        keyrings = self.target / "usr/share/pacman/keyrings"
        keyrings.mkdir(parents=True)
        (keyrings / "archlinux.gpg").write_bytes(b"keyring")
        (keyrings / "archlinuxarm.gpg").write_bytes(b"keyring")
        listed = {"archlinux.gpg": {self.FINGERPRINT}, "archlinuxarm.gpg": set()}
        with mock.patch.object(adapter, "_keyring_fingerprints", lambda keyring: listed[keyring.name]):
            self.finish()
        self.assertEqual(self.calls, [(["--init"], True), (["--populate", "archlinuxarm"], True)])

    def test_unreadable_keyring_lists_nothing(self):
        keyring = self.target / "broken.gpg"
        keyring.write_bytes(b"not a keyring")
        self.assertEqual(adapter._keyring_fingerprints(keyring), set())

    @unittest.skipUnless(Path("/usr/share/pacman/keyrings/archlinux.gpg").is_file(), "needs archlinux-keyring")
    def test_shipped_keyring_fingerprints_are_read(self):
        fingerprints = adapter._keyring_fingerprints(Path("/usr/share/pacman/keyrings/archlinux.gpg"))
        self.assertTrue(fingerprints)
        self.assertTrue(all(len(f) == 40 and set(f) <= set("0123456789ABCDEF") for f in fingerprints))


class DevelopmentInstallTest(unittest.TestCase):
    def setUp(self):
        root = temporary_directory(self)
        self.target = root / "target"
        self.target.mkdir()
        self.wheelhouse = root / "wheels"
        self.wheelhouse.mkdir()
        self.installed = []
        self.failing = set()
        self.mounts = []

        def install_selected(target, username, profiles, repo, **options):
            self.installed.append((username, profiles, options))
            if set(profiles) & self.failing:
                raise RuntimeError("download failed")

        def run(argv, check=False):
            self.mounts.append(argv[:2])
            return subprocess.CompletedProcess(argv, 0)
        for owner, name, replacement in ((development, "install_selected", install_selected),
                                         (adapter.subprocess, "run", run)):
            patcher = mock.patch.object(owner, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def install(self, profiles, wheelhouse=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return adapter.install_development(self.target, REPO, profiles, ["owner"], wheelhouse)

    def test_failed_profile_names_its_after_boot_command(self):
        self.failing = {"ai"}
        failures = self.install(["python", "ai", "containers", "cuda"], self.wheelhouse)
        self.assertEqual(failures, [{"profiles": ["ai"], "user": "owner",
                                     "command": "h7607a-dev install-torch --online"}])
        self.assertEqual([profiles for _, profiles, _ in self.installed],
                         [["python", "containers"], ["ai"], ["cuda"]])

    def test_carrier_wheelhouse_is_bound_read_only_below_the_target(self):
        self.assertEqual(self.install(["ai"], self.wheelhouse), [])
        wheelhouse = self.installed[0][2]["wheelhouse"]
        self.assertEqual(wheelhouse.parent, self.target / "var/cache")
        self.assertEqual(self.mounts, [["mount", "--bind"], ["mount", "-o"], ["umount", str(wheelhouse)]])
        self.assertFalse(wheelhouse.exists())

    def test_failed_bind_reports_the_profile(self):
        def run(argv, check=False):
            raise subprocess.CalledProcessError(32, argv)
        with mock.patch.object(adapter.subprocess, "run", run):
            failures = self.install(["ai"], self.wheelhouse)
        self.assertEqual([failure["profiles"] for failure in failures], [["ai"]])
        self.assertEqual(self.installed, [])
        self.assertEqual(list((self.target / "var/cache").iterdir()), [])

    def test_failed_sdk_download_is_not_fatal(self):
        self.failing = {"cuda"}
        failures = self.install(["cuda"])
        self.assertEqual(failures, [{"profiles": ["cuda"], "user": "owner",
                                     "command": "h7607a-cuda install-sdk --online"}])

    def test_without_carrier_pytorch_installs_online_in_the_target(self):
        self.assertEqual(self.install(["ai"]), [])
        self.assertEqual(self.installed, [("owner", ["ai"], {"online": True})])
        self.assertEqual(self.mounts, [])


class WheelhouseTest(unittest.TestCase):
    def stage(self, outcome):
        with mock.patch.object(adapter, "prepare_wheelhouse", mock.Mock(side_effect=outcome)), \
                contextlib.redirect_stdout(io.StringIO()):
            return adapter.stage_wheelhouse(REPO, None, Path("/run/h7607a-wheels-test"))

    def test_verified_carrier_is_used(self):
        self.assertEqual(self.stage([{"wheelhouse": "/run/h7607a-wheels-test"}]), Path("/run/h7607a-wheels-test"))

    def test_missing_carrier_falls_back_to_online(self):
        from h7607a.carrier import CarrierError
        for error in (CarrierError("Opened USB differs from installer parent identity"),
                      InstallError("Offline wheels must come from the protected installer USB"),
                      OSError(5, "Input/output error")):
            self.assertIsNone(self.stage(error))


def pin(path, data):
    return {"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class SpeakerTuningTest(unittest.TestCase):
    PREFIX = b"MZ" + bytes(510)
    MEMBERS = {"XU_Ext/fw/a.wmfw": b"wmfw\n" * 64, "XU_Ext/tn/b.bin": b"tuning\n" * 32}

    def setUp(self):
        root = temporary_directory(self)
        source = root / "source"
        for member, data in self.MEMBERS.items():
            (source / member).parent.mkdir(parents=True, exist_ok=True)
            (source / member).write_bytes(data)
        archive = root / "a.7z"
        subprocess.run(["bsdtar", "--format", "7zip", "-cf", archive, "-C", source, "XU_Ext"], check=True)
        self.container = self.PREFIX + archive.read_bytes()
        driver = {**pin(".artifacts/asus/driver.exe", self.container), "urls": []}
        self.rows = [{**pin("assets/hardware/firmware/cirrus/" + Path(member).name, data),
                      "extract": {"from": driver, "offset": len(self.PREFIX), "member": member},
                      "target": "usr/lib/firmware/cirrus/" + Path(member).name}
                     for member, data in self.MEMBERS.items()]
        self.repo = root / "repo"
        (self.repo / "locks").mkdir(parents=True)
        (self.repo / "locks/hardware.json").write_text(json.dumps({"artifacts": [
            {**pin("assets/hardware/acpi/h7607a-acpi.cpio", b"acpi"), "boot": "h7607a-acpi.cpio"}, *self.rows]}))
        self.cache = root / "cache"
        self.target = root / "target"
        (self.target / "usr/lib/firmware/cirrus").mkdir(parents=True)
        patcher = mock.patch.object(adapter.os, "chown")
        self.chown = patcher.start()
        self.addCleanup(patcher.stop)

    def seed_driver_package(self):
        (self.cache / ".artifacts/asus").mkdir(parents=True)
        (self.cache / ".artifacts/asus/driver.exe").write_bytes(self.container)

    def install(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return adapter.install_speaker_tuning(self.target, self.repo, self.cache)

    def test_extracted_files_are_installed_root_owned(self):
        self.seed_driver_package()
        installed = self.install()
        self.assertEqual(sorted(installed), ["/" + row["target"] for row in self.rows])
        for row, data in zip(self.rows, self.MEMBERS.values()):
            path = self.target / row["target"]
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.stat().st_mode & 0o7777, 0o644)
            self.chown.assert_any_call(path, 0, 0)

    def test_identical_existing_file_is_accepted(self):
        self.seed_driver_package()
        (self.target / self.rows[0]["target"]).write_bytes(self.MEMBERS["XU_Ext/fw/a.wmfw"])
        self.assertEqual(len(self.install()), 2)

    def test_different_existing_file_is_kept(self):
        self.seed_driver_package()
        (self.target / self.rows[1]["target"]).write_bytes(b"newer upstream tuning")
        with self.assertRaises(InstallError):
            self.install()
        self.assertEqual((self.target / self.rows[1]["target"]).read_bytes(), b"newer upstream tuning")
        self.assertFalse((self.target / self.rows[0]["target"]).exists())

    def test_failed_download_leaves_the_target_untouched(self):
        self.assertIsNone(self.install())
        self.assertEqual(list((self.target / "usr/lib/firmware/cirrus").iterdir()), [])


class MkinitcpioTest(unittest.TestCase):
    def test_speaker_tuning_precedes_the_initramfs_build(self):
        plugin = hardware_plugin(self)
        steps = []
        for owner, name in ((adapter, "install_speaker_tuning"), (policy, "configure_system_policy"),
                            (hardware, "install_hardware")):
            patcher = mock.patch.object(owner, name, mock.Mock(
                side_effect=lambda *args, name=name, **kwargs: steps.append(name)))
            patcher.start()
            self.addCleanup(patcher.stop)
        target = temporary_directory(self)
        installation = SimpleNamespace(target=target, _hooks=["base", "udev"], append_mod=lambda module: None,
                                       mkinitcpio=lambda flags: steps.append("mkinitcpio") or True)
        self.assertTrue(plugin.on_mkinitcpio(installation))
        self.assertEqual(steps, ["install_speaker_tuning", "configure_system_policy", "install_hardware",
                                 "mkinitcpio"])
        adapter.install_speaker_tuning.assert_called_once_with(target, REPO)


class GenfstabTest(unittest.TestCase):
    def setUp(self):
        self.plugin = hardware_plugin(self)
        self.target = temporary_directory(self)
        self.plugin.boot_parameters = ["root=UUID=0b6c0f8e-8a54-4c55-9c38-8c1f3c1d2f01"]
        self.plugin.root_uuid = "0b6c0f8e-8a54-4c55-9c38-8c1f3c1d2f01"
        self.plugin.configuration = SimpleNamespace(
            network_config=None, disk_config=None, development_profiles=["ai"],
            auth_config=SimpleNamespace(users=[SimpleNamespace(username="owner")]))
        self.steps = []
        for owner, name, result in ((hardware, "write_boot_files", None),
                                    (adapter, "finish_target_repository", None),
                                    (adapter, "install_development", [{"profiles": ["ai"]}])):
            patcher = mock.patch.object(owner, name, mock.Mock(
                side_effect=lambda *args, name=name, result=result, **kwargs: self.steps.append(name) or result))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_holds_precede_development_installs(self):
        with mock.patch("builtins.input") as pause:
            self.assertFalse(self.plugin.on_genfstab(SimpleNamespace(target=self.target)))
        self.assertEqual(self.steps, ["write_boot_files", "finish_target_repository", "install_development"])
        adapter.install_development.assert_called_once_with(self.target, REPO, ["ai"], ["owner"], None)
        self.assertEqual(pause.call_count, 1)


if __name__ == "__main__":
    unittest.main()
