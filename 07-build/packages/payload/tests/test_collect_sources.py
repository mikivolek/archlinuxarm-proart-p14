"""Offline checks of collect-sources.py recipe parsing, git archiving and packing."""
import hashlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

HERE = Path(__file__).resolve()
SCRIPT = HERE.parents[4] / "collect-sources.py"

SRCINFO = """pkgbase = demo
\tpkgver = 1.0
\tpkgrel = 2
\tsource = https://example.org/demo-1.0.tar.gz
\tsource = local.patch
\tsource_aarch64 = arm.bin::https://example.org/arm-1.0.bin
\tsource_x86_64 = https://example.org/x86-1.0.bin
\tsha256sums = aaa
\tsha256sums = bbb
\tb2sums = ccc
\tb2sums = ddd
\tsha256sums_aarch64 = eee
\tsha256sums_x86_64 = fff

pkgname = demo
\tsource = https://example.org/ignored.tar.gz
"""


def git(*argv, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *argv], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, message, parent=None):
    """Record the index as a commit with plumbing, which runs no hooks and signs nothing."""
    tree = git("write-tree", cwd=repo)
    sha = git("commit-tree", "--no-gpg-sign", "-m", message, *(["-p", parent] if parent else []), tree, cwd=repo)
    git("update-ref", "HEAD", sha, cwd=repo)
    return sha


@unittest.skipUnless(HERE.parents[1].name == "payload" and SCRIPT.is_file(), "needs the collection checkout")
class CollectSourcesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("collect_sources", SCRIPT)
        cls.cs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cs)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_sources_keep_generic_and_aarch64_entries_with_the_strongest_checksum(self):
        self.assertEqual(list(self.cs.sources(SRCINFO)), [
            ("", "https://example.org/demo-1.0.tar.gz", "b2", "ccc"),
            ("", "local.patch", "b2", "ddd"),
            ("arm.bin", "https://example.org/arm-1.0.bin", "sha256", "eee"),
        ])

    def test_check_rejects_a_changed_download(self):
        data = b"upstream tarball\n"
        self.cs.check(data, "b2", hashlib.blake2b(data).hexdigest())
        self.cs.check(data, "sha256", "SKIP")
        with self.assertRaises(ValueError):
            self.cs.check(data + b"x", "sha256", hashlib.sha256(data).hexdigest())

    def test_copyleft_selection(self):
        self.assertTrue(self.cs.copyleft(["LGPL-2.1-or-later", "MIT"]))
        self.assertTrue(self.cs.copyleft(["MPL-2.0"]))
        self.assertFalse(self.cs.copyleft(["MIT", "Apache-2.0 WITH LLVM-exception"]))

    def test_recipe_prefers_the_alarm_commit_and_maps_epochs_to_arch_tags(self):
        commits = {("glibc", "2.44-1"): ("c0ffee", "core/glibc")}
        self.assertEqual(self.cs.recipe("glibc", "2.44-1", commits)["commit"], "c0ffee")
        self.assertEqual(self.cs.recipe("mesa", "1:26.2.5-1", commits)["tag"], "1-26.2.5-1")

    def test_vcs_accepts_the_signed_query_on_either_side_of_the_fragment(self):
        for url in ("git+https://h/r.git?signed#tag=v1", "git+https://h/r.git#tag=v1?signed"):
            self.assertEqual(self.cs.vcs(url), ("https://h/r.git", "tag", "v1"))
        self.assertEqual(self.cs.vcs("git+https://h/gnulib.git"), ("https://h/gnulib.git", "", ""))

    def upstream(self):
        upstream = self.tmp / "upstream"
        upstream.mkdir()
        git("init", "-q", cwd=upstream)
        (upstream / "file").write_text("tagged\n")
        git("add", "file", cwd=upstream)
        tagged = commit(upstream, "one")
        git("update-ref", "refs/tags/v1", tagged, cwd=upstream)
        (upstream / "file").write_text("later\n")
        git("add", "file", cwd=upstream)
        commit(upstream, "two", tagged)
        return upstream, tagged

    def test_git_archive_exports_the_tagged_tree_only(self):
        (upstream, _), out = self.upstream(), self.tmp / "out"
        out.mkdir()
        commit = self.cs.git_fetch(f"file://{upstream}", "tag", "v1", self.tmp / "fetched")
        self.cs.git_archive(self.tmp / "fetched", commit, out, "demo")
        with tarfile.open(out / "demo.tar") as archive:
            self.assertEqual(archive.extractfile("demo/file").read(), b"tagged\n")

    def test_gitlink_reads_the_submodule_commit_of_the_parent(self):
        _, pinned = self.upstream()
        parent = self.tmp / "parent"
        parent.mkdir()
        git("init", "-q", cwd=parent)
        (parent / ".gitmodules").write_text('[submodule "lib"]\n\tpath = lib\n\turl = https://h/gnulib.git\n')
        git("add", ".gitmodules", cwd=parent)
        git("update-index", "--add", "--cacheinfo", f"160000,{pinned},lib", cwd=parent)
        head = commit(parent, "pin")
        self.assertEqual(self.cs.gitlink([(parent, head)], "gnulib"), pinned)
        self.assertIsNone(self.cs.gitlink([(parent, head)], "paxutils"))

    def test_pack_is_byte_identical_for_the_same_tree(self):
        tree = self.tmp / "work" / "demo"
        tree.mkdir(parents=True)
        (tree / "PKGBUILD").write_text("pkgname=demo\n")
        (tree / "run.sh").write_text("#!/bin/sh\n")
        (tree / "run.sh").chmod(0o700)
        first, second = self.tmp / "a.src.tar.gz", self.tmp / "b.src.tar.gz"
        self.cs.pack(self.tmp / "work", tree, first)
        (tree / "PKGBUILD").touch()
        self.cs.pack(self.tmp / "work", tree, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with tarfile.open(first) as archive:
            self.assertEqual([m.name for m in archive], ["demo", "demo/PKGBUILD", "demo/run.sh"])
            self.assertEqual(archive.getmember("demo/run.sh").mode, 0o755)
            self.assertEqual(archive.getmember("demo/PKGBUILD").uid, 0)


if __name__ == "__main__":
    unittest.main()
