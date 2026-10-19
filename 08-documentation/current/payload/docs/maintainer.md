# Maintainer builds

Paths starting with a numbered directory such as `01-platform/` are in the
checkout; paths such as `scripts/` and `locks/` are in the assembled tree, and
commands using them run from there.

## Component metadata

Each numbered directory holds components. A component's `component.yaml` lists
its files with source and destination paths, byte counts, SHA-256 values and
modes, and `manifest.yaml` pins every `component.yaml` by hash. The files use
JSON syntax, which is valid YAML 1.2, so `jq` reads them. When you change a
file, update its byte count and SHA-256 in `component.yaml` and the component
hash in `manifest.yaml`.

- `type` is `patch` for modified upstream code, `custom` for code written here
  and `direct` for upstream artifacts and configuration used as they are.
- `repo_url` and `checkout_hash` name the upstream source, and `sparse` the
  checkout paths; they are `null` where no upstream checkout exists.
- `dependencies` records tool and package versions, `depends_on` names other
  components, and items may list further requirements.
- `files[].source` is relative to the component directory and
  `files[].destination` to the assembled tree. Item-level script, patch and
  artifact references and `links` resolve inside the assembled tree.

## Rebuilding packages and binaries

`scripts/build-packages.py` rebuilds the custom packages with `makepkg` in a
native Arch Linux ARM build root and publishes the signed repository in
`build/repo`. Run the build step (`--build-only`) as an unprivileged user that
cannot read the signing home, and sign separately with
`--assemble-only --signing-home DIR`, so package build code never sees the key.
The signing step accepts only packages that the build step recorded in
`build-record.json` with matching sizes and SHA-256 values.

This rebuilds packages from their recipes and pinned inputs. The kernel
recipe packages a precompiled `h7607a-kernel-runtime.tar.zst`, and the hardware
recipes include prebuilt modules. To change those binaries, first follow the
build scripts in their `provenance/` directories, update the affected artifact,
recipe and lock pins, and then rebuild the package repository and images.
[GPL source](gpl-source.md) locates the corresponding sources and scripts.

## Signing with your own key

1. Create a key in a private GnuPG home and export its public key to
   `07-build/packages/payload/assets/keys/h7607a-build.gpg`.
2. In `07-build/packages/payload/locks/packages.json`, set `fingerprint`,
   `bytes` and `sha256` for the new file in both `signing_keys.local` and the
   `local-key` artifact row.
3. In `07-build/packages/component.yaml`, update `bytes` and `sha256` for both
   `payload/assets/keys/h7607a-build.gpg` and `payload/locks/packages.json`,
   then that component's hash in `manifest.yaml`.
4. [Assemble the inputs](assembling.md) with
   `./assemble.sh --fetch assemble DIR`. Enter `DIR` and move the downloaded
   `build/repo` aside; the signing step requires that destination to be absent.
5. In a native Arch Linux ARM build root, run
   `python3 scripts/build-packages.py --build-only` as an unprivileged user
   without access to the signing home. The assembled tree and build output
   must be writable by that user.
6. With access to the same assembled tree and build output, run
   `python3 scripts/build-packages.py --assemble-only --signing-home /absolute/private/gnupg`
   as the signing user. Keep that GnuPG home outside the assembled tree.
7. In the assembled tree, run `python3 scripts/release.py seal`, then
   `sudo python3 scripts/build-live.py build`.

## Publishing the inputs

`publish-inputs.sh STAGE [SEARCH...]` collects every pinned file whose last URL
is on the release, checks it against its pin and links or copies it into
`STAGE` under its SHA-256 name. It looks in the checkout and in each `SEARCH`
directory, including a Git LFS object store, and lists the files it found
nowhere. Any file that differs from its pin stops it; otherwise it prints the
`gh release create --draft` and `gh release upload` commands, and `--upload`
runs them, skipping assets the draft already holds.

The Arch Linux ARM source bundles that [GPL source](gpl-source.md) lists need
rebuilding whenever the rootfs, live package or `packages.json` pins change.
Run on an Arch Linux host with git, Mercurial and makepkg:

```sh
./collect-sources.py ROOTFS DIR > 07-build/locked-inputs/payload/locks/alarm-sources.json
```

`ROOTFS` is the pinned rootfs tarball. Each package version gets
`DIR/<pkgbase>-<version>.src.tar.gz` with its exact recipe, patches and
upstream sources, git and Mercurial sources archived at their pinned revision;
`SUBSTITUTES` in the script lists the few sources taken from another host. A
rerun with the same tool versions gives the same files. Update the lock's pins
in `component.yaml` and `manifest.yaml`, then pass `DIR` to `publish-inputs.sh`
as a `SEARCH` directory.
