#!/usr/bin/env bash
# Materialize component payloads and invoke the ARM64 image builder.
# Absent pinned inputs are downloaded only with --fetch.
set -euo pipefail

usage() {
    printf 'Usage: %s [--fetch] {assemble|build} /absolute/output\n' "$0" >&2
    printf '%s\n' '--fetch downloads absent pinned inputs from their lock URLs and verifies them.' >&2
    exit 2
}

fetch=no
if [[ ${1-} == --fetch ]]; then
    fetch=yes
    shift
fi
[[ $# == 2 ]] || usage
case $1 in assemble|build) action=$1 ;; *) usage ;; esac
case $2 in /*) output=$2 ;; *) usage ;; esac
collection=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
locks=(locks/live-base.json locks/live-tools.json locks/packages.json locks/release-inputs.json)
if [[ -e $output || -L $output ]]; then
    printf '%s: %s already exists\n' "$0" "$output" >&2
    exit 1
fi

# Exit status 3 means the action can proceed once absent inputs are fetched.
check() {
    python3 -I - "$collection" "$action" "$fetch" "$0" "$output" "${locks[@]}" <<'EOF'
import hashlib, json, os, shlex, sys

collection, action, fetch, program, output, *locks = sys.argv[1:]
LFS = b"version https://git-lfs.github.com/spec/v1"


def read(relative):
    with open(os.path.join(collection, relative), "rb") as stream:
        return stream.read()


def files(rows):
    return f"{len(rows)} file" + ("" if len(rows) == 1 else "s")


def stop(errors):
    if errors:
        sys.exit("\n".join(errors) + f"\n{files(errors)} failed verification")


rows, errors, missing = [], [], []
for entry in json.loads(read("manifest.yaml"))["components"]:
    data = read(entry["manifest"])
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        errors.append(entry["manifest"] + ": SHA-256 differs from manifest.yaml")
        continue
    base = os.path.dirname(entry["manifest"])
    rows += [(os.path.normpath(os.path.join(base, row["source"])), row) for row in json.loads(data).get("files", [])]
for source, row in rows:
    path = os.path.join(collection, source)
    if not os.path.lexists(path):
        missing.append((source, row))
    elif os.path.islink(path) or not os.path.isfile(path):
        errors.append(source + ": not a regular file")
    else:
        with open(path, "rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if stream.read(len(LFS)) == LFS:
                errors.append(source + ": Git LFS pointer instead of the pinned file")
            elif size != row["bytes"]:
                errors.append(f"{source}: {size} bytes, pinned {row['bytes']}")
            else:
                stream.seek(0)
                if hashlib.file_digest(stream, "sha256").hexdigest() != row["sha256"]:
                    errors.append(source + ": SHA-256 differs from its pin")
stop(errors)
sources = {row["destination"]: source for source, row in rows}
remote = {}
for lock in locks:
    if lock in sources and os.path.isfile(os.path.join(collection, sources[lock])):
        for artifact in json.loads(read(sources[lock]))["artifacts"]:
            if artifact.get("urls"):
                remote[artifact["path"]] = artifact
fetchable, provided, unsourced = [], [], []
# Lock rows that no component ships are never in the checkout; --fetch
# always downloads them.
lock_only = [artifact for path, artifact in remote.items() if path not in sources]
for source, row in missing:
    artifact = remote.get(row["destination"])
    if artifact and (artifact["bytes"], artifact["sha256"]) != (row["bytes"], row["sha256"]):
        errors.append(source + ": lock pin differs from its component pin")
    elif artifact:
        fetchable.append(row)
    elif row.get("provided_by"):
        provided.append(row)
    else:
        unsourced.append(row)
stop(errors)

if missing:
    print(f"{files(missing)} absent from {collection}:", file=sys.stderr)
    last = None
    for source, row in sorted(missing, key=lambda item: os.path.split(item[0])):
        directory, name = os.path.split(source)
        if directory != last:
            print(f"  {directory}/", file=sys.stderr)
            last = directory
        note = row.get("provided_by") or ("" if row["destination"] in remote else "no URL")
        print(f"    {name}" + (f": {note}" if note else ""), file=sys.stderr)
download = fetchable + lock_only
size = f"{files(download)}, {sum(row['bytes'] for row in download) / 1e6:,.0f} MB"
problems = []
if unsourced:
    problems.append(f"Add the {files(unsourced)} marked 'no URL' to this checkout.")
if fetchable and fetch == "no":
    command = shlex.join([program, "--fetch", action, output])
    problems.append(f"Download and verify the absent inputs with URLs ({size}): {command}")
if provided and action == "build":
    problems.append(f"Provide the {files(provided)} listed with a note before building.")
if problems:
    sys.exit("\n".join(problems))
if provided:
    print(f"Provide the {files(provided)} listed with a note before building.", file=sys.stderr)
if download and fetch == "yes":
    print(f"Fetching {size} after copying.", file=sys.stderr)
    sys.exit(3)
EOF
}

status=0
check || status=$?
case $status in
    0) fetch=no ;;
    3) ;;
    *) exit "$status" ;;
esac
mkdir -- "$output"

jq -r '.components[].manifest' "$collection/manifest.yaml" |
while IFS= read -r manifest; do
    component="$collection/$manifest"
    directory=$(dirname -- "$component")
    jq -r '.files[] | [.source, .destination, .mode] | @tsv' "$component" |
    while IFS=$'\t' read -r source destination mode; do
        [[ -e $directory/$source ]] || continue
        mkdir -p -- "$(dirname -- "$output/$destination")"
        install -m "$(printf '%o' "$mode")" "$directory/$source" "$output/$destination"
    done
    jq -r '.links[]? | [.destination, .target] | @tsv' "$component" |
    while IFS=$'\t' read -r destination target; do
        mkdir -p -- "$(dirname -- "$output/$destination")"
        ln -s -- "$target" "$output/$destination"
    done
done

cd -- "$output"
if [[ $fetch == yes ]] && ! python3 -B scripts/fetch.py "${locks[@]}"; then
    resume="python3 -B scripts/fetch.py ${locks[*]} && python3 -B scripts/release.py seal"
    if [[ $action == build ]]; then
        resume="$resume && python3 -B scripts/build-live.py build"
    fi
    printf '%s: fetch stopped; to resume, run in %s: %s\n' "$0" "$output" "$resume" >&2
    exit 1
fi
python3 -B scripts/release.py seal
if [[ $action == build ]]; then
    python3 -B scripts/build-live.py build
fi
printf 'Source directory: %s\n' "$output"
