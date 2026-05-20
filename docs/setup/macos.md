# macOS setup

## Docker on macOS does not provide CUDA

macOS Docker runs containers inside a Linux VM with no GPU passthrough for
NVIDIA. Treat macOS as a CPU-only target.

## Running tests with the macOS override

```bash
docker compose -f docker-compose.yml -f docker-compose.mac.yml run --rm wolf
```

The `mac.yml` override explicitly sets `WOLF_BACKEND=cpu` and contains no
NVIDIA environment variables. It exists so that the same `docker compose`
flow works on macOS without leaking GPU expectations into the container.

## Apple Silicon and local inference

GPU-accelerated local inference on Apple Silicon (Metal / MPS) is out of
scope for the default Docker test container. When that backend is
implemented, it will be exposed as a separate service — likely a host-side
runner, since MPS is not available inside the Docker VM — not as a Docker
GPU device. Update this file when that work lands.

## Host-side fast inner loop

Docker is the canonical verification path, but host-side tests run
considerably faster on macOS and are recommended during active development:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t . -v
```
