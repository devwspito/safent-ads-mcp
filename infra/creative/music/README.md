# ads-creative-music — ACE-Step 1.5 (MIT, commercial-safe)

Not started yet — this is the install recipe, ready to run when needed.
Background music tracks for campaign spots (60–120s per campaign, trimmed
per cut). MIT-licensed, ~15GB resident, measured ~8s/track on this class of
hardware regardless of track length (research.md §4, forum 378352).

ACE-Step-1.5's own `pyproject.toml` already pins the right PyTorch wheel for
this box — `torch==2.10.0+cu130` under a `platform_machine == 'aarch64'`
marker (same CUDA 13 / aarch64 combination as `../comfyui/Containerfile`) —
so a plain `uv sync` resolves correctly here, no manual torch override
needed like some other Spark recipes require.

## Install

```bash
# 1. uv (skip if already installed — check `which uv` first). Download,
#    inspect, then run — never pipe curl straight into a shell.
curl -LsSf -o /tmp/uv-install.sh https://astral.sh/uv/install.sh
less /tmp/uv-install.sh   # read it before running it
sh /tmp/uv-install.sh
rm /tmp/uv-install.sh

# 2. Clone into this directory (not vendored into this repo — upstream owns
#    its own release cadence; reuse-before-writing)
cd infra/creative/music   # desde la raíz del repo
git clone https://github.com/ACE-Step/ACE-Step-1.5.git
cd ACE-Step-1.5
uv sync
```

## Run

```bash
cd infra/creative/music/ACE-Step-1.5   # desde la raíz del repo

# REST API (what an agent/orchestrator should talk to)
uv run acestep-api    # http://127.0.0.1:8001 — bind behind a reverse proxy
                       # or firewall rule limiting it to 127.0.0.1, same as
                       # every other ads-creative-* service on this box.

# or the Gradio UI for manual auditioning
uv run acestep         # http://127.0.0.1:7860
```

Models auto-download on first run into ACE-Step-1.5's own cache — **do not**
run this alongside `ads-creative-comfyui` or `ads-creative-tts` (rule #1 in
`../comfyui/README.md`: one model loaded at a time on this shared GPU).

## DGX Spark cautions (same box, same rules as everywhere else in this repo)

- Check `free -g` before starting; if available < 45GB, do not start.
- `nvidia-smi` before and after — this must be the only GPU-resident model
  from our side when it's running.
- If it needs to run unattended (e.g. batch-generating tracks overnight),
  wrap it the same way as the ComfyUI smoke test: log to
  a scratch directory of your own, never a location production
  reads from live (see the "ficheros que producción lee en vivo" caution
  that applies to this whole box — nothing here should touch a path any
  other service depends on).
- Prefix any container wrapper you add around this with `ads-creative-`, per
  the naming convention for everything in this directory.

## Model weights (optional pre-stage)

`../models/download.sh --with-music` fetches the turbo all-in-one checkpoint
(`Comfy-Org/ace_step_1.5_ComfyUI_files` → `checkpoints/ace_step_1.5_turbo_aio.safetensors`,
~7GB) into `$ADS_CREATIVE_MODELS_ROOT/ace-step-1.5/checkpoints/`
for inventory/reuse from ComfyUI's own ACE-Step nodes if that path is
preferred over the standalone `acestep-api` server. The standalone server
above manages its own cache independently and does not need this pre-stage
step to work.
