#!/usr/bin/env bash
# Stage the pinned inputs hosted as release assets under their SHA-256 names
# and print, or with --upload run, the gh commands that publish them.
set -euo pipefail

usage() {
    printf 'Usage: %s [--upload] STAGE [SEARCH...]\n' "$0" >&2
    printf 'SEARCH directories are checked for each file under its assembled path,\n' >&2
    printf 'its file name, its SHA-256 and the Git LFS object layout.\n' >&2
    exit 2
}

upload=no
if [[ ${1-} == --upload ]]; then
    upload=yes
    shift
fi
[[ $# -ge 1 && ${1-} != -* ]] || usage
collection=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

python3 -I - "$collection" "$upload" "$@" <<'EOF'
import hashlib, json, os, shlex, shutil, subprocess, sys

collection, upload, stage, *search = sys.argv[1:]
stage = os.path.abspath(stage)
BATCH = 50


def pin(path):
    with open(path, "rb") as stream:
        return os.fstat(stream.fileno()).st_size, hashlib.file_digest(stream, "sha256").hexdigest()


def read(relative):
    with open(os.path.join(collection, relative), "rb") as stream:
        return stream.read()


sources, locks = {}, []
for entry in json.loads(read("manifest.yaml"))["components"]:
    data = read(entry["manifest"])
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        sys.exit(entry["manifest"] + ": SHA-256 differs from manifest.yaml")
    base = os.path.dirname(entry["manifest"])
    for row in json.loads(data).get("files", []):
        source = os.path.normpath(os.path.join(base, row["source"]))
        sources[row["destination"]] = source
        if row["destination"].startswith("locks/") and row["destination"].endswith(".json"):
            if pin(os.path.join(collection, source)) != (row["bytes"], row["sha256"]):
                sys.exit(source + ": differs from its pin")
            locks.append(json.loads(read(source)))
releases = [lock["release"] for lock in locks if isinstance(lock, dict) and "release" in lock]
if len(releases) != 1:
    sys.exit("Exactly one lock must name the release")
repository, tag = releases[0]["repository"], releases[0]["tag"]
prefix = f"https://github.com/{repository}/releases/download/{tag}/"

errors, published = [], {}
for lock in locks:
    for artifact in lock.get("artifacts", []) if isinstance(lock, dict) else []:
        hosted = [url for url in artifact.get("urls", []) if url.startswith(prefix)]
        if not hosted:
            continue
        if hosted != [prefix + artifact["sha256"]] or artifact["urls"][-1] != hosted[0]:
            errors.append(artifact["path"] + ": the release URL must come last and end in the SHA-256")
        entry = published.setdefault(artifact["sha256"], {"bytes": artifact["bytes"], "paths": set()})
        if entry["bytes"] != artifact["bytes"]:
            errors.append(artifact["path"] + ": same SHA-256 pinned with another size")
        entry["paths"].add(artifact["path"])
if errors:
    sys.exit("\n".join(errors))


def candidates(sha, paths):
    for path in paths:
        if path in sources:
            yield os.path.join(collection, sources[path])
    for directory in search:
        for path in paths:
            yield os.path.join(directory, path)
            yield os.path.join(directory, os.path.basename(path))
        yield os.path.join(directory, sha)
        yield os.path.join(directory, sha[:2], sha[2:4], sha)


os.makedirs(stage, exist_ok=True)
staged, absent = [], []
for sha, entry in sorted(published.items()):
    paths, target, expected = sorted(entry["paths"]), os.path.join(stage, sha), (entry["bytes"], sha)
    if os.path.lexists(target):
        if os.path.islink(target) or not os.path.isfile(target) or pin(target) != expected:
            errors.append(target + ": differs from its name")
        else:
            staged.append(sha)
        continue
    for candidate in candidates(sha, paths):
        if not os.path.isfile(candidate):
            continue
        if pin(candidate) != expected:
            errors.append(f"{candidate}: differs from the pin of {paths[0]}")
            break
        try:
            os.link(candidate, target)
        except OSError:
            partial = target + ".partial"
            shutil.copyfile(candidate, partial)
            if pin(partial) != expected:
                os.unlink(partial)
                errors.append(candidate + ": changed while copying")
                break
            os.replace(partial, target)
        staged.append(sha)
        break
    else:
        absent.extend(paths[:1])

size = sum(published[sha]["bytes"] for sha in staged)
print(f"Staged {len(staged)} of {len(published)} release assets ({size / 1e6:,.0f} MB) in {stage}", file=sys.stderr)
if absent:
    print("Not found locally:", file=sys.stderr)
    last = None
    for path in sorted(absent, key=os.path.split):
        directory, name = os.path.split(path)
        if directory != last:
            print(f"  {directory}/", file=sys.stderr)
            last = directory
        print(f"    {name}", file=sys.stderr)
if errors:
    sys.exit("\n".join(errors) + "\nNothing to upload until these files match their pins.")

notes = ("Pinned build inputs, each named by its SHA-256. The locks in the repository "
         "give the file name, size and upstream URLs of every asset.")
create = ["gh", "release", "create", tag, "--repo", repository, "--draft", "--title", tag, "--notes", notes]
files = [os.path.join(stage, sha) for sha in staged]
if upload == "no":
    print(shlex.join(create))
    for start in range(0, len(files), BATCH):
        print(shlex.join(["gh", "release", "upload", tag, "--repo", repository, *files[start:start + BATCH]]))
    sys.exit()
if not shutil.which("gh"):
    sys.exit("--upload needs the gh command")
view = subprocess.run(["gh", "release", "view", tag, "--repo", repository, "--json", "assets"],
                      capture_output=True, text=True)
if view.returncode and subprocess.run(create).returncode:
    sys.exit("gh release create failed")
done = set()
for asset in json.loads(view.stdout)["assets"] if not view.returncode else []:
    sha = asset["name"]
    if sha not in published:
        continue
    if (asset.get("state"), asset.get("size")) == ("uploaded", published[sha]["bytes"]) \
            and asset.get("digest") in (None, "sha256:" + sha):
        done.add(sha)
    else:
        errors.append(f"Release asset {sha} is incomplete or differs from its name; delete it and run again.")
if errors:
    sys.exit("\n".join(errors))
files = [os.path.join(stage, sha) for sha in staged if sha not in done]
for start in range(0, len(files), BATCH):
    if subprocess.run(["gh", "release", "upload", tag, "--repo", repository, *files[start:start + BATCH]]).returncode:
        sys.exit("gh release upload failed; run again to upload the rest")
print(f"Uploaded {len(files)} assets to {tag}; publish the draft once all {len(published)} are present.",
      file=sys.stderr)
EOF
