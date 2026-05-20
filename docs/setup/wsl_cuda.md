# WSL2 + NVIDIA CUDA setup

## Prerequisites

- Windows 11, or Windows 10 with current updates
- NVIDIA GPU with a current NVIDIA Windows driver that supports WSL2 CUDA
- WSL2 enabled: `wsl --install` (first install) or `wsl --update` (existing)
- A WSL2 Ubuntu distribution (Ubuntu 22.04 LTS recommended)
- Docker Desktop with the WSL2 backend enabled, or NVIDIA Container Toolkit
  installed inside the WSL2 distro

## Critical: do not install a Linux NVIDIA display driver inside WSL2

The Windows NVIDIA driver is exposed into WSL2. Installing a Linux NVIDIA
display driver inside the WSL2 distro will break CUDA in WSL2.

If you need the CUDA toolkit inside WSL2 for `nvcc` or headers, use the
WSL-Ubuntu CUDA toolkit package path, not the standard Linux package that
bundles a display driver.

## Repository location

Clone this repository inside the WSL2 filesystem, e.g., `/home/<user>/wolf`,
not on a Windows drive mount such as `/mnt/c/...`. Filesystem performance
through `/mnt/c` is significantly slower and can cause Docker build and test
slowdowns.

## Verifying GPU access

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml run --rm wolf nvidia-smi
```

You should see the GPU listed with the driver version reported by Windows,
not a Linux driver version. If `nvidia-smi` is not found or no GPU is
listed, the GPU is not visible to the container — re-check Docker Desktop
GPU support or the NVIDIA Container Toolkit installation.

## Running tests on GPU

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml run --rm wolf
```

The current unit test suite does not require a GPU; the tests are pure
Python with fake providers. The GPU compose file exists so that future
model-backed tests can opt into CUDA without changing the default image.
