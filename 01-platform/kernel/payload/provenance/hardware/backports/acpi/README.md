# ACPI override allocation

The H7607BA boot recording `IMG_1427.MOV` shows a replacement DSDT at
`0x2050a00000`, length `0x12144`, crossing the EFI descriptor boundary
`0x2050a0d000`. The kernel first accepts the early table mapping, then
rejects its permanent mapping with "requested region covers multiple EFI
memory regions". ACPICA reports `AE_NO_ACPI_TABLES`; USB, NVMe and GPU
enumeration consequently cannot proceed.

The patch targets NVIDIA/NV-Kernels commit
`2234cd7f8f8a9fdbceef37f82c531016aab8cf73`. It adds an architecture allocation
hook. ARM64 reserves complete native pages inside a single usable,
write-back, non-runtime EFI conventional-memory descriptor. Existing
memblock reservations, NOMAP handling, and ACPI mapping protections remain
in force. No fitting descriptor means an explicit allocation failure;
there is no fallback across descriptors. Other architectures retain their
original allocator behavior. Successful allocation logs its address,
rounded size, and eligible region bounds (end exclusive).

Sources: [original allocator](https://github.com/NVIDIA/NV-Kernels/blob/2234cd7f8f8a9fdbceef37f82c531016aab8cf73/drivers/acpi/tables.c),
[ARM64 mapping restriction](https://github.com/NVIDIA/NV-Kernels/blob/2234cd7f8f8a9fdbceef37f82c531016aab8cf73/arch/arm64/kernel/acpi.c),
[EFI memory eligibility](https://github.com/NVIDIA/NV-Kernels/blob/2234cd7f8f8a9fdbceef37f82c531016aab8cf73/drivers/firmware/efi/efi-init.c),
[existing hardening rationale](https://www.openwall.com/lists/kernel-hardening/2020/09/29/9).

`python3 -B -m unittest discover -s tests -p test_acpi_allocation.py -v`
applies the production patch to exact pinned contexts, compiles the actual
resulting C allocator, and exercises 4 KiB and 64 KiB pages with EFI and
bounded/reservation-aware memblock stubs. This tests allocation decisions;
physical boot remains a separate verification. The kernel build must also
apply the patch without fuzz to the complete pinned sources.

Build natively in Ubuntu 24.04 ARM64 with GCC
`13.3.0-6ubuntu2~24.04.1`, binutils 2.42 and pahole 1.25:

```sh
sudo apt-get install build-essential gcc-13 bc bison flex libssl-dev \
  libelf-dev libdw-dev zlib1g-dev pkg-config pahole curl patch kmod \
  xz-utils zstd gawk
provenance/hardware/backports/acpi/build.sh /absolute/fresh-work 6
provenance/hardware/backports/acpi/package.sh \
  /absolute/fresh-work /absolute/fresh-runtime
```

`build.sh` checks the toolchain and source hashes, applies the Ubuntu source
delta and this patch without fuzz, and starts from the signed Ubuntu module
package's configuration. Only four unavailable Rust toolchain capability
values may differ; this kernel does not enable Rust. The resulting
configuration is `config-7.0.0-1022-nvidia`, whose SHA-256 matches
`rebuilt_sha256` in `build-receipt.json`. It runs ordinary
`make Image modules`. `SOURCE_CACHE` may name a directory containing the
three pinned upstream input archives to avoid downloading them again.

`package.sh` runs standard parallel `modules_install` (override with `JOBS`), strips and signs modules before
compressing them, and checks the signing certificate for every non-staging
module. Ubuntu's existing staging-module signing exclusion is retained.
The runtime archive includes matching generated headers, `Module.symvers`
and the kernel BTF base. It excludes the generated private signing key.
The normal pacman recipe overlays these onto the pinned upstream header
packages; it does not reuse stock modules coupled to the original BTF base.

Before accepting retained ASUS/NVIDIA modules, run `verify-modules.py` with
the new `Module.symvers` followed by the seven module paths from the hardware
and package locks. It checks all imported symbol CRCs, including imports
provided by other retained NVIDIA modules, and rejects retained modules that
need a matching BTF rebuild. Output hashes identify the verified artifacts;
separate builds generate new signing keys and are not byte-identical.

The resulting runtime archive is a normal PKGBUILD input. Updating its hash,
the kernel image hash and exact dependent package versions requires rebuilding
the local repository and clean installer. Neither the live image nor an
installed machine downloads or applies this patch at boot.
