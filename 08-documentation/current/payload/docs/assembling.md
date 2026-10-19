# Assembling the inputs

Git holds the recipes, sources and pins but not most large inputs, such as
the ALARM rootfs and packages, the signed repository under
`07-build/locked-inputs/payload/build/`, the NVIDIA `.run` and the kernel
runtime tarball. `./assemble.sh --fetch` downloads them from the URLs in their
locks, upstream first. The last URL of each one is its asset on this
repository's `inputs-1` release, named by its SHA-256: ALARM keeps only the
current version of each package and replaces the rootfs in place, so once
upstream moves on, only the release copy matches the pins.
`07-build/locked-inputs/payload/locks/release-inputs.json` lists the release
files that no other lock covers.

On a fresh clone, [build the ACPI overrides](acpi-overrides.md) in the checkout
first. Then either [build the images](media-build.md#building) in one run, or
assemble the tree on its own:

```sh
./assemble.sh --fetch assemble "$HOME/h7607a-source"
```

`assemble.sh` checks every component file against its pin, copies the
component payloads into a new directory, applies the recorded file modes,
creates the repository links and writes the source manifest. It needs Bash,
`jq` and Python 3.13 or newer. The output must be an absolute path that does
not exist yet. A file that differs from its pin stops the run before anything
is written. Absent inputs are listed by directory. Without `--fetch` the run
stops and prints the command that downloads the ones with URLs; with
`--fetch`, `scripts/fetch.py` downloads and verifies them inside the output.
Files listed with a note, such as the ACPI overrides, have to be created in the
checkout before `build`.

To sign the packages with your own key, see
[Signing with your own key](maintainer.md#signing-with-your-own-key).
