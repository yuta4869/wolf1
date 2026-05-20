# Docker development environment

## Why

Wolf uses Docker as the default development and verification environment to
keep behavior reproducible across Ubuntu Linux native, WSL2 Ubuntu + NVIDIA
CUDA, and macOS CPU-only hosts. Host-side test runs are still supported as a
fast inner loop, but the canonical verification path is Docker.

## Default CPU test

```bash
docker compose build
docker compose run --rm wolf
```

The default `wolf` service runs the full unit test suite. It uses
`network_mode: none` and read-only volume mounts of `src/`, `tests/`, and
`pyproject.toml` only — the project root is not mounted wholesale.

## GPU test (Ubuntu / WSL2)

Requires an NVIDIA driver on the host, NVIDIA Container Toolkit (Linux) or
Docker Desktop GPU support (WSL2), and `nvidia-smi` working inside the GPU
container.

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml run --rm wolf nvidia-smi
docker compose -f docker-compose.yml -f docker-compose.gpu.yml run --rm wolf
```

See `wsl_cuda.md` for the WSL2-specific setup.

## macOS CPU fallback

macOS Docker has no CUDA support. The `mac.yml` override forces the CPU
backend so the same `docker compose` flow works without leaking GPU
expectations into the container.

```bash
docker compose -f docker-compose.yml -f docker-compose.mac.yml run --rm wolf
```

See `macos.md`.

## Why `network_mode: none`

Default unit tests must not touch the network. Setting `network_mode: none`
at the container level provides a defense-in-depth boundary independent of
code: even if a test inadvertently triggers a network call, the kernel
rejects it. This enforces the project-wide "no hidden cloud calls" rule.

## Why no secrets in the image

Per CLAUDE.md security requirements, `.env`, `.env.*`, `secrets/`,
`credentials/`, `tokens/`, `private/`, local model weights, vector
databases, photo libraries, mail caches, and raw sensor data are excluded
from the build context via `.dockerignore` and are not copied by the
Dockerfile. A leaked image must not contain any user-private material.

## Why robot control is not in the default Docker container

Robot transport, motor control, camera capture, microphone capture,
USB / serial device access, and LiDAR access require host hardware
permissions that the default test container deliberately does not have.
Hardware-bound services must be defined as separate, explicitly named
services with explicit device mounts and permissions — never folded into the
default test image.
