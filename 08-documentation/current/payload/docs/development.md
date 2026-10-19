# Development environments

The Archinstall "Development tools" menu defaults to Python, C/C++, the PyTorch
CUDA environment and GPU containers, and needs an ordinary user account. A
failing development profile does not stop the installation; for PyTorch and
the CUDA SDK it prints the command that completes it after the first boot.
Wheels come from PyPI and the
[PyTorch CUDA 13.2 index](https://download.pytorch.org/whl/cu132), SDK archives
from NVIDIA's SBSA redistributables, and each is checked against the byte count
and SHA-256 in its lock file.

## PyTorch

The environment holds 31 pinned Python 3.14 wheels, including PyTorch
2.14.1+cu132 and Triton 3.8.0. The cuSPARSELt 0.8.1 wheel is retagged from
`manylinux2014_sbsa` to `manylinux2014_aarch64` with its RECORD updated; its
libraries are unchanged. The installer reads the wheels from a raw carrier
region that exists only on the maintainer's USB (`locks/usb-layout.json`), and
without it downloads them inside the target. It is a virtual environment on the
system's Python 3.14 interpreter:

```sh
source ~/.local/share/h7607a/venvs/torch/bin/activate
```

## CUDA SDK

The optional CUDA 13.4.3 SDK comes from 28 locked SBSA archives, including
`nvcc` 13.4.92, and needs about 20 GiB during installation. It skips the
driver, Nsight GUIs, Orin and NPU libraries and GPUDirect modules. Use it from a
separate shell so its library paths stay out of the PyTorch environment:

```sh
source ~/.local/opt/cuda-13.4.3/env.sh
```

## Containers

The container profile installs rootless Podman, `crun`, the NVIDIA container
toolkit with the absolute `/usr/bin/ldconfig` hook and CDI with specification
refresh, and assigns non-overlapping subordinate UID and GID ranges.
`h7607a-container run IMAGE COMMAND...` runs an ARM64 image with direct CDI
injection, `keep-id`, `keep-groups` and `no-new-privileges`; plain `podman`
keeps working.
