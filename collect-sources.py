#!/usr/bin/env python3
"""Build source bundles for the copyleft Arch Linux ARM packages that the locks pin.

Usage: collect-sources.py ROOTFS OUT > alarm-sources.json

ROOTFS is the pinned ArchLinuxARM rootfs tarball. Each copyleft package in it,
in the live-tools lock and among the Arch Linux ARM rows of packages.json gets
OUT/<pkgbase>-<version>.src.tar.gz holding its recipe and sources. The recipe is
the archlinuxarm/PKGBUILDs commit titled "<repo>/<pkgbase> to <version>" when
one exists, otherwise Arch's packaging tag for that version. Downloads are
checked against the recipe's checksums, and git sources are archived at the
pinned ref instead of cloned whole. SUBSTITUTES lists the few sources taken
from another host. Bundles already in OUT are reused. The lock
rows, ending in the release URL, go to stdout. Needs git, Mercurial and makepkg.
"""
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

COLLECTION = Path(__file__).resolve().parent
COPYLEFT = ("GPL", "MPL", "EPL", "CDDL", "EUPL", "OSL", "CPL", "APSL", "Sleepycat", "custom:vim")
ALARM_PKGBUILDS = "https://github.com/archlinuxarm/PKGBUILDs"
ARCH_PACKAGING = "https://gitlab.archlinux.org/archlinux/packaging/packages/"
ARCH = "aarch64"
CHECKSUMS = ("sha512", "b2", "sha384", "sha256", "sha224", "sha1", "md5")
# Recipe sources whose location no longer serves them, mapped to the same content elsewhere.
SUBSTITUTES = {
    # git.gnu.org.ua presents a certificate for wap.gnu.org.ua only. Savannah carries the same
    # repository, and its v1.26 tag verifies against the recipe's validpgpkeys.
    "git+https://git.gnu.org.ua/gdbm.git?signed#tag=v1.26":
        "git+https://git.savannah.gnu.org/git/gdbm.git#commit=8ca8f03ab6cc125139a395ec2d522aebb15f8060",
    # pagure.io no longer builds archives; this is the commit its tag r0-52-25 names.
    "https://pagure.io/newt/archive/r0-52-25/newt-r0-52-25.tar.gz":
        "newt-r0-52-25::git+https://pagure.io/newt.git#commit=9b9e7e8936f51f83a7259c5f0a9b4ff909cbd39a",
}


def locks():
    """Return every lock by its assembled path, read through manifest.yaml."""
    found = {}
    for entry in json.loads((COLLECTION / "manifest.yaml").read_text())["components"]:
        component = COLLECTION / entry["manifest"]
        for row in json.loads(component.read_text()).get("files", []):
            if row["destination"].startswith("locks/") and row["destination"].endswith(".json"):
                found[row["destination"]] = json.loads((component.parent / row["source"]).read_text())
    return found


def desc(text):
    fields, key = {}, None
    for line in text.splitlines():
        if line.startswith("%") and line.endswith("%"):
            key = line.strip("%")
            fields[key] = []
        elif line and key:
            fields[key].append(line)
    return fields


def descs(archive, prefix=""):
    for member in archive:
        if member.isfile() and member.name.lstrip("./").startswith(prefix) and member.name.endswith("/desc"):
            fields = desc(archive.extractfile(member).read().decode(errors="replace"))
            # The Arch Linux ARM databases carry occasional entries of NUL bytes.
            if fields.get("NAME"):
                yield fields


def get(url, tries=3):
    request = urllib.request.Request(url, headers={"User-Agent": "h7607a-arch/1"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == tries - 1:
                raise
        except OSError:
            if attempt == tries - 1:
                raise
        time.sleep(2 ** attempt)


def run(*argv, cwd=None):
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout


def copyleft(licenses):
    return any(word in license for license in licenses for word in COPYLEFT)


def packages(rootfs, pins):
    """Map each copyleft (pkgbase, version) to its package names and licenses."""
    row = next(a for a in pins["locks/live-base.json"]["artifacts"] if a["path"].endswith(".tar.gz"))
    with open(rootfs, "rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        if (size, hashlib.file_digest(stream, "sha256").hexdigest()) != (row["bytes"], row["sha256"]):
            sys.exit(f"{rootfs} differs from the pinned rootfs")
    pairs = {}

    def add(name, version, base, licenses):
        entry = pairs.setdefault((base, version), {"packages": set(), "licenses": set()})
        entry["packages"].add(name)
        entry["licenses"].update(licenses)

    with tarfile.open(rootfs) as archive:
        for fields in descs(archive, "var/lib/pacman/local/"):
            base = fields.get("BASE", fields["NAME"])[0]
            add(fields["NAME"][0], fields["VERSION"][0], base, fields.get("LICENSE", []))
    tools = pins["locks/live-tools.json"]["packages"]
    mirror = tools[0]["url"].split(f"/{ARCH}/")[0]
    meta = {}
    for repo in ("core", "extra", "alarm"):
        with tarfile.open(fileobj=io.BytesIO(get(f"{mirror}/{ARCH}/{repo}/{repo}.db"))) as archive:
            for fields in descs(archive):
                meta[fields["NAME"][0]] = (fields.get("BASE", fields["NAME"])[0], fields.get("LICENSE", []))
    named = [(p["name"], p["version"]) for p in tools]
    for artifact in pins["locks/packages.json"]["artifacts"]:
        if any("archlinuxarm.org" in url for url in artifact.get("urls", [])) and ".pkg.tar." in artifact["id"] \
                and not artifact["id"].endswith(".sig"):
            name, version, release = artifact["id"].split(".pkg.tar.")[0].rsplit("-", 3)[:3]
            named.append((name, f"{version}-{release}"))
    for name, version in named:
        # A package the current databases no longer list counts as copyleft, so nothing is skipped unseen.
        base, licenses = meta.get(name, (name, ["unknown, assumed GPL"]))
        add(name, version, base, licenses)
    return {key: value for key, value in pairs.items() if copyleft(value["licenses"])}


def alarm_commits(clone):
    if not clone.exists():
        run("git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", ALARM_PKGBUILDS + ".git", str(clone))
    commits = {}
    for line in run("git", "-C", str(clone), "log", "--format=%H %s").splitlines():
        commit, subject = line.split(" ", 1)
        match = re.fullmatch(r"(core|extra|alarm|aur)/(\S+) to (\S+)", subject)
        if match:
            commits.setdefault((match[2], match[3]), (commit, f"{match[1]}/{match[2]}"))
    return commits


def recipe(base, version, commits):
    if (base, version) in commits:
        commit, path = commits[(base, version)]
        return {"repository": ALARM_PKGBUILDS, "commit": commit, "path": path}
    return {"repository": f"{ARCH_PACKAGING}{base}.git", "tag": version.replace(":", "-")}


def checkout(source, clone, directory):
    """Write the recipe files to directory and return its .SRCINFO text."""
    if "commit" in source:
        listing = run("git", "-C", str(clone), "ls-tree", "-r", "--name-only", source["commit"], source["path"] + "/")
        for name in listing.splitlines():
            target = directory / Path(name).relative_to(source["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(subprocess.run(["git", "-C", str(clone), "show", f"{source['commit']}:{name}"],
                                              check=True, capture_output=True).stdout)
        info = run("makepkg", "--printsrcinfo", cwd=directory)
        (directory / ".SRCINFO").write_text(info)
        return info
    run("git", "clone", "--quiet", "--depth", "1", "--branch", source["tag"], source["repository"], str(directory))
    run("rm", "-rf", "--", str(directory / ".git"))
    # Tags from before Arch's move to git carry no .SRCINFO.
    if not (directory / ".SRCINFO").is_file():
        (directory / ".SRCINFO").write_text(run("makepkg", "--printsrcinfo", cwd=directory))
    return (directory / ".SRCINFO").read_text()


def sources(info):
    """Yield (name, url, algorithm, digest) for the generic and aarch64 sources."""
    arrays = {}
    for line in info.splitlines():
        if line.startswith("pkgname"):
            break
        key, _, value = line.strip().partition(" = ")
        arrays.setdefault(key, []).append(value)
    for suffix in ("", "_" + ARCH):
        entries = arrays.get("source" + suffix, [])
        if not entries:
            continue
        algorithm = next((a for a in CHECKSUMS if len(arrays.get(f"{a}sums{suffix}", [])) == len(entries)), None)
        if algorithm is None:
            raise ValueError(f"no checksum array matches source{suffix}")
        for entry, digest in zip(entries, arrays[f"{algorithm}sums{suffix}"]):
            name, _, url = SUBSTITUTES.get(entry, entry).rpartition("::")
            yield name, url, algorithm, digest


def check(data, algorithm, digest):
    if digest == "SKIP":
        return
    actual = hashlib.blake2b(data).hexdigest() if algorithm == "b2" else hashlib.new(algorithm, data).hexdigest()
    if actual != digest:
        raise ValueError(f"{algorithm} differs from the recipe")


def vcs(url):
    """Split a makepkg VCS source into (remote, fragment kind, ref); '?signed' may precede or follow '#'."""
    rest = url.split("+", 1)[1]
    fragment = rest.split("#", 1)[1].split("?")[0] if "#" in rest else ""
    kind, _, ref = fragment.partition("=")
    return rest.split("#")[0].split("?")[0], kind, ref


def git_fetch(remote, kind, ref, repository):
    """Fetch one ref of remote into repository and return its commit."""
    wanted = {"tag": f"refs/tags/{ref}", "commit": ref, "branch": f"refs/heads/{ref}"}.get(kind, "HEAD")
    try:
        run("git", "init", "--quiet", str(repository))
        run("git", "-C", str(repository), "fetch", "--quiet", "--depth", "1", remote, wanted)
        target = "FETCH_HEAD"
    except subprocess.CalledProcessError:
        # Servers that refuse a commit by hash still serve it from a blobless clone.
        run("rm", "-rf", "--", str(repository))
        run("git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", remote, str(repository))
        target = ref
    return run("git", "-C", str(repository), "rev-parse", target + "^{commit}").strip()


def git_archive(repository, commit, directory, name):
    with open(directory / f"{name}.tar", "wb") as stream:
        subprocess.run(["git", "-C", str(repository), "archive", "--format=tar", f"--prefix={name}/", commit],
                       check=True, stdout=stream)


def gitlink(fetched, name):
    """Return the commit a fetched repository records for its submodule called name."""
    for repository, commit in fetched:
        try:
            config = run("git", "-C", str(repository), "config", "--blob", f"{commit}:.gitmodules",
                         "--get-regexp", r"^submodule\..*\.(path|url)$")
        except subprocess.CalledProcessError:
            continue
        modules = {}
        for line in config.splitlines():
            key, value = line.split(" ", 1)
            section, field = key.rsplit(".", 1)
            modules.setdefault(section, {})[field] = value
        for module in modules.values():
            if name in (Path(module.get("url", "")).name.removesuffix(".git"), Path(module.get("path", "")).name):
                mode, kind, sha = run("git", "-C", str(repository), "ls-tree", commit, module["path"]).split()[:3]
                if kind == "commit":
                    return sha
    return None


def pack(work, directory, path):
    """Write directory as a gzip tarball with fixed owners, modes and times."""
    partial = path.with_name(path.name + ".partial")
    with open(partial, "wb") as raw, gzip.GzipFile("", "wb", fileobj=raw, mtime=0) as packed, \
            tarfile.open(fileobj=packed, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for file in [directory, *sorted(directory.rglob("*"))]:
            member = archive.gettarinfo(str(file), str(file.relative_to(work)))
            member.mtime = member.uid = member.gid = 0
            member.uname = member.gname = ""
            member.mode = 0o755 if file.is_dir() or os.access(file, os.X_OK) else 0o644
            if member.isreg():
                with open(file, "rb") as stream:
                    archive.addfile(member, stream)
            else:
                archive.addfile(member)
    partial.rename(path)


def bundle(source, base, clone, path):
    with tempfile.TemporaryDirectory(dir=path.parent) as work:
        directory = Path(work) / base
        directory.mkdir()
        info = checkout(source, clone, directory)
        fetched, submodules = [], []
        for name, url, algorithm, digest in sources(info):
            scheme = url.split("://")[0]
            if "://" not in url:
                if not (directory / url).is_file():
                    raise ValueError(f"local source {url} is not in the recipe")
            elif scheme.startswith("git+"):
                remote, kind, ref = vcs(url)
                name = name or Path(remote).name.removesuffix(".git")
                if not kind:
                    # makepkg checks these out at the commit a parent repository records for them.
                    submodules.append((name, remote))
                    continue
                repository = Path(work) / "git" / name
                commit = git_fetch(remote, kind, ref, repository)
                git_archive(repository, commit, directory, name)
                fetched.append((repository, commit))
            elif scheme.startswith("hg+"):
                remote, kind, ref = vcs(url)
                name = name or Path(remote).name
                repository = Path(work) / "hg" / name
                # A clone cut at a tag lacks the later .hgtags commit that names it, so use the node.
                identify = run("hg", "identify", "--debug", "--id", "--rev", ref or "default", remote)
                node = re.search(r"^[0-9a-f]{40}$", identify, re.M)[0]
                run("hg", "clone", "--quiet", "--noupdate", "--rev", node, remote, str(repository))
                run("hg", "--repository", str(repository), "archive", "--rev", node, "--type", "tar",
                    "--prefix", name, str(directory / f"{name}.tar"))
            elif "+" in scheme:
                raise ValueError(f"unsupported VCS source {url}")
            else:
                data = get(url)
                check(data, algorithm, digest)
                (directory / (name or Path(url.split("?")[0]).name)).write_bytes(data)
        for name, remote in submodules:
            commit = gitlink(fetched, name)
            if commit is None:
                raise ValueError(f"no repository in the recipe pins {name}")
            repository = Path(work) / "git" / name
            git_archive(repository, git_fetch(remote, "commit", commit, repository), directory, name)
        pack(Path(work), directory, path)


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__.split("\n\n")[1])
    rootfs, out = Path(sys.argv[1]), Path(sys.argv[2]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    pins = locks()
    release = pins["locks/release-inputs.json"]["release"]
    prefix = f"https://github.com/{release['repository']}/releases/download/{release['tag']}/"
    pairs = packages(rootfs, pins)
    clone = out / ".alarm-pkgbuilds"
    commits = alarm_commits(clone)
    rows, failed = [], []
    for (base, version), entry in sorted(pairs.items()):
        source, path = recipe(base, version, commits), out / f"{base}-{version}.src.tar.gz"
        if not path.exists():
            print(f"{base} {version}", file=sys.stderr, flush=True)
            try:
                bundle(source, base, clone, path)
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                failed.append(f"{base} {version}: {getattr(error, 'stderr', None) or error}")
                continue
        with open(path, "rb") as stream:
            sha = hashlib.file_digest(stream, "sha256").hexdigest()
        rows.append({"id": f"source-{base}-{version}", "path": f".artifacts/sources/alarm/{path.name}",
                     "bytes": path.stat().st_size, "sha256": sha, "urls": [prefix + sha],
                     "pkgbase": base, "version": version, "packages": sorted(entry["packages"]),
                     "licenses": sorted(entry["licenses"]), "recipe": source})
    json.dump({"schema": "h7607a-alarm-sources-v1", "artifacts": rows}, sys.stdout, indent=2)
    print()
    if failed:
        sys.exit("\n".join(failed))


if __name__ == "__main__":
    main()
