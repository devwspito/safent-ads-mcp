# ads-creative-comfyui

Local ComfyUI engine for Safent Ads creative generation, tuned for this
specific DGX Spark (GB10 Grace Blackwell, aarch64, sm_121, 121GB unified
memory shared with production workloads). Recipe verified against
[AEON-7/comfyui-aeon-spark](https://github.com/AEON-7/comfyui-aeon-spark) and
[Triplany/comfyui-dgx-spark](https://github.com/Triplany/comfyui-dgx-spark).
Full model/license research: `research/content-generation-stack.md`.

## Unpinned experimental tooling (read before trusting a build)

This `Containerfile` is **not** part of the release path (`Containerfile` at
the repo root, built by `ci.yml`/`release.yml`, is what ships) and its
supply chain is intentionally looser while the recipe is still being
verified on this specific box:

- `git clone` of `ComfyUI` itself and its six custom nodes
  (`ComfyUI-LTXVideo`, `ComfyUI-GGUF`, `ComfyUI-KJNodes`,
  `ComfyUI-VideoHelperSuite`, `ComfyUI_essentials`, `rgthree-comfy`) and of
  `SageAttention` (build stage) all float on their default branch —
  whatever is HEAD the day the image builds, not a commit this repo
  reviewed.
- `pip install` of PyTorch, the ComfyUI/custom-node `requirements.txt`
  files, and the base Python toolchain run without `--require-hashes`:
  whatever version resolves that day, not one this repo pinned.

Re-run and re-verify before trusting a rebuild — a floating clone can bring
in a breaking change, a malicious commit upstream, or simply a different
result than the one that was smoke-tested. Pin every clone to a commit SHA
(`git clone ... && git -C <dir> checkout <sha>`) and every `pip install` to
hashed, version-pinned requirements once this recipe is verified and stops
changing week to week; until then, treat any image built from this file as
disposable, single-box, non-reproducible tooling.

## Hard rules for this box (read before running anything)

1. **Only ONE model loaded at a time.** Never chain a Qwen-Image generation
   directly into a Wan 2.2 video job without unloading between them (ComfyUI
   unloads on its own between distinct model checkpoints in the same
   workflow, but do not run two separate workflows concurrently against this
   one container).
2. **~24GB is already committed** to a training job on this GPU. Everything
   this container does must fit in the remaining budget, and the operator
   should keep total *additional* memory (this container + downloads staged
   in RAM, if any) under ~40GB. `free -g` before every run; abort if
   available < 45GB.
3. **A unified-memory OOM hangs the whole box — there is no OOM-kill for CUDA
   managed memory the way there is for regular cgroup RAM.** `--reserve-vram
   2` in the container CMD is not cosmetic; do not remove it. If a workflow
   is going to be tight (e.g. Wan 2.2 14B fp16, ~61GB per research.md), do
   not run it opportunistically — plan the window, tell whoever owns the
   training job, and watch `nvidia-smi`/`free -g` live.
4. **Never touch any other container or process on this box.** `docker ps`
   before and after every action here; if something outside the
   `ads-creative-` prefix changed, stop and investigate — do not assume it
   was you.
5. **Clock cap is a host-level mitigation, not a container one.** A container
   cannot call `nvidia-smi` against a driver it doesn't administer this way;
   if repeated OOM/overcurrent reboots happen, the fix is on the host:
   ```
   sudo nvidia-smi -lgc 300,2100
   ```
   This is documented here so whoever runs this image knows it exists — it
   is **not** applied automatically by any file in this directory.

## Build

```bash
cd infra/creative/comfyui   # desde la raíz del repo
# --memory caps the *build* container's RAM (cgroup-enforced, unlike CUDA
# unified memory) — SageAttention's nvcc compile is the risky stage.
DOCKER_BUILDKIT=1 docker build --memory=32g --memory-swap=32g \
  -t ads-creative-comfyui:local .
```

Expect 15–35 min: PyTorch cu130 wheel install is fast, the SageAttention
source compile against sm_121a is the slow part. If the SageAttention wheel
build fails, the image still builds — `main.py` falls back to PyTorch's SDPA
math kernel (slower, not broken). Check the build log for the `WARN:
SageAttention wheel missing` line to know which path you got.

## Run

```bash
cd infra/creative/comfyui   # desde la raíz del repo
docker compose up -d
docker compose logs -f ads-creative-comfyui   # watch for "To see the GUI go to..."
```

Bound to `127.0.0.1:8188` only — this is a local worker, not exposed to the
LAN or internet. UI/API: `http://127.0.0.1:8188`.

Stop when not actively generating — it holds CUDA context even idle:

```bash
docker compose down
```

## What's inside

- CUDA 13.0.2 devel base, PyTorch 2.9.1+cu130, `TORCH_CUDA_ARCH_LIST=12.1a`.
- SageAttention compiled from source for sm_121a (FlashAttention-2 built for
  sm_120 would be binary-compatible but we don't bundle it; FA3 is not
  compatible with this arch at all — do not add it).
- `torch.compile`/Triton left **off** (`TORCH_COMPILE_DISABLE=1`,
  `TORCHDYNAMO_DISABLE=1`) — Triton on sm_121 needs the `TRITON_PTXAS_PATH`
  workaround and is still flaky per
  [triton#10331](https://github.com/triton-lang/triton/issues/10331).
- Custom nodes (only what our 4 target models need — see
  `../workflows/README.md` for which node belongs to which workflow):
  - `Lightricks/ComfyUI-LTXVideo` — required for LTX-2.5 (`LTXVLoader` /
    `LTXVConditioning` / `LTXVImgToVideo` family; core ComfyUI does not have
    LTX's temporal VAE/conditioning nodes).
  - `city96/ComfyUI-GGUF` — GGUF loader, kept for future quantized variants
    (research.md flags GGUF as a preferred format when available).
  - `kijai/ComfyUI-KJNodes` — Wan 2.2 example workflows commonly depend on a
    handful of these helper nodes (image/video resize, mask utilities).
  - `Kosinkadink/ComfyUI-VideoHelperSuite` — `VHS_VideoCombine` etc., needed
    to write video files out of Wan 2.2 / LTX frame batches.
  - `cubiq/ComfyUI_essentials`, `rgthree/rgthree-comfy` — general QoL used
    by several official example workflows (image ops, reroute/organizer
    nodes).
  - **Deliberately not included**: ComfyUI-Manager (no runtime pull of
    unpinned third-party code — nodes/models are supplied by this repo's
    IaC, not clicked in from a UI), Kijai's WanVideoWrapper (would duplicate
    node class names against the native Wan 2.2 support already in ComfyUI
    core — known collision source), the rest of AEON-7's bundle (Ollama
    nodes, frame interpolation, etc. — not needed by our 4 target models;
    add explicitly if a specific future workflow needs one).
- Qwen-Image-2512, FLUX.2-klein-4B and Wan 2.2 load through **native
  ComfyUI core nodes** (`UNETLoader`, `CLIPLoader`, `VAELoader`, `KSampler`
  family) — confirmed by decoding the `prompt` (API-format) metadata embedded
  in the official example images at
  `comfyanonymous.github.io/ComfyUI_examples/{qwen_image,flux2}/`. No custom
  node needed for those three beyond the LoRA loader already in core.

## Model paths

`extra_model_paths.yaml` maps `/models/creative/<model>/<type>/...` (as laid
out by `../models/download.sh`) onto ComfyUI's search paths. The container
mounts `$ADS_CREATIVE_MODELS_ROOT` (default `~/models/creative`) **read-only** — the download
script is the only writer, ComfyUI only reads.

## Pinning

This first build clones `comfyanonymous/ComfyUI` at `master` to pick up the
recent native Qwen-Image-2512 / FLUX.2 / Wan 2.2 support. **Re-pin to the
exact commit SHA that passed the smoke test** (`git -C /opt/ComfyUI rev-parse
HEAD` inside the built image) once verified, so future rebuilds are
reproducible and don't silently pick up a breaking upstream change. Track the
pin in this file once set — do not leave it floating indefinitely.
