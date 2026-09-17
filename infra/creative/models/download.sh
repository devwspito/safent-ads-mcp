#!/usr/bin/env bash
# ads-creative model fetcher — idempotent, commercially-licensed weights only.
#
# Writes into $ADS_CREATIVE_MODELS_ROOT/<model>/<comfy-type>/ (default
# ~/models/creative) so
# comfyui/extra_model_paths.yaml can find each file under the ComfyUI model
# type it expects. Safe to re-run: `hf download` skips files that already
# match by size/etag.
#
# Usage:
#   ./download.sh                     # phase 1 (required, ~42GB): FLUX.2-klein-4B,
#                                      #   Qwen-Image-2512-Lightning (merged 4-step)
#   ./download.sh --with-edit         # + Qwen-Image-Edit-2511 (~30GB)
#   ./download.sh --with-wan          # + Wan2.2-TI2V-5B (~10GB, no lightning — see note)
#   ./download.sh --with-wan-a14b-lightning  # + Wan2.2 A14B lightning LoRAs (~2.5GB;
#                                      #   requires --with-wan-a14b, see note below)
#   ./download.sh --with-tts          # + Chatterbox-Multilingual es-es (~3GB)
#   ./download.sh --with-music        # + ACE-Step 1.5 turbo checkpoint (~7GB)
#   ./download.sh --with-ltx          # attempts LTX-2.5 — GATED, needs HF_TOKEN
#                                      #   with license accepted (see note below)
#   ./download.sh --all               # everything above except --with-ltx
#
# Sizes are documented per-model below (verified via the HF API tree listing
# on 2026-09-09; re-check if a model card changes).
set -euo pipefail

# Dónde viven los pesos en ESTA máquina. Nunca una ruta de nadie
# escrita a mano: el repo es público y la casa de quien lo escribió no
# es configuración de nadie más.
MODELS_ROOT="${ADS_CREATIVE_MODELS_ROOT:-${HOME}/models/creative}"
LOG_DIR="${ADS_CREATIVE_LOG_DIR:-${TMPDIR:-/tmp}/ads-creative-downloads}"
mkdir -p "$LOG_DIR"

WITH_EDIT=0
WITH_WAN=0
WITH_WAN_A14B_LIGHTNING=0
WITH_TTS=0
WITH_MUSIC=0
WITH_LTX=0

for arg in "$@"; do
  case "$arg" in
    --with-edit) WITH_EDIT=1 ;;
    --with-wan) WITH_WAN=1 ;;
    --with-wan-a14b-lightning) WITH_WAN_A14B_LIGHTNING=1 ;;
    --with-tts) WITH_TTS=1 ;;
    --with-music) WITH_MUSIC=1 ;;
    --with-ltx) WITH_LTX=1 ;;
    --all) WITH_EDIT=1; WITH_WAN=1; WITH_WAN_A14B_LIGHTNING=1; WITH_TTS=1; WITH_MUSIC=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 1 ;;
  esac
done

# -----------------------------------------------------------------------------
# hf CLI resolution — use the installed one, else build a throwaway venv
# under the scratchpad (never touch system/user Python for this).
# -----------------------------------------------------------------------------
if command -v hf >/dev/null 2>&1; then
  HF="hf"
elif command -v huggingface-cli >/dev/null 2>&1; then
  HF="huggingface-cli"
else
  echo "No hf/huggingface-cli found — bootstrapping a venv under scratchpad" >&2
  VENV="${SCRATCHPAD}/hf-venv"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -U pip huggingface_hub[hf_transfer] >/dev/null
  HF="$VENV/bin/hf"
fi

export HF_HUB_ENABLE_HF_TRANSFER=1

fetch() {
  local repo="$1" include="$2" dest="$3"
  mkdir -p "$dest"
  echo "==> $repo :: $include -> $dest"
  "$HF" download "$repo" --include "$include" --local-dir "$dest"
  # `hf download --local-dir` mirrors the repo-relative path (e.g. this
  # lands at $dest/split_files/vae/foo.safetensors, not $dest/foo.safetensors)
  # — flatten so it matches comfyui/extra_model_paths.yaml's flat per-type
  # layout. Never touch hf's own .cache/ bookkeeping (needed for resume).
  # Only safe for a single exact filename include — skip for wildcard/whole-repo
  # fetches (chatterbox/LTX `"*"`), where "flattening" a glob would be wrong.
  case "$include" in
    *'*'*|*'?'*|*/) return 0 ;;
  esac
  local base="${include##*/}"
  local nested
  nested=$(find "$dest" -path "$dest/.cache" -prune -o -type f -name "$base" -print | head -1)
  if [ -n "$nested" ] && [ "$nested" != "$dest/$base" ]; then
    mv "$nested" "$dest/$base"
    find "$dest" -depth -type d ! -name ".cache" -empty -delete 2>/dev/null || true
  fi
}

# =============================================================================
# Phase 1 (default, auto-started) — required by the task
# =============================================================================

# --- FLUX.2-klein-4B (Apache-2.0, commercial) -------------------------------
# Comfy-Org repackage (split_files, fp4 text encoder for speed/VRAM):
#   diffusion_models/flux-2-klein-4b.safetensors        7.75 GB
#   text_encoders/qwen_3_4b_fp4_flux2.safetensors        3.85 GB
#   vae/flux2-vae.safetensors                            0.34 GB
#   total                                                ~11.9 GB
fetch "Comfy-Org/vae-text-encorder-for-flux-klein-4b" \
  "split_files/diffusion_models/flux-2-klein-4b.safetensors" \
  "${MODELS_ROOT}/flux2-klein-4b/diffusion_models"
fetch "Comfy-Org/vae-text-encorder-for-flux-klein-4b" \
  "split_files/text_encoders/qwen_3_4b_fp4_flux2.safetensors" \
  "${MODELS_ROOT}/flux2-klein-4b/text_encoders"
fetch "Comfy-Org/vae-text-encorder-for-flux-klein-4b" \
  "split_files/vae/flux2-vae.safetensors" \
  "${MODELS_ROOT}/flux2-klein-4b/vae"

# --- Qwen-Image-2512-Lightning, 4-step, MERGED comfyui-scaled fp8 ----------
# (Apache-2.0, commercial)
# FIXED 2026-09-09 — do not revert without reading this. We originally fetched
# lightx2v's plain qwen_image_2512_fp8_e4m3fn_scaled.safetensors (base only)
# + a separate LoraLoaderModelOnly node for the 4-step Lightning LoRA, on the
# theory that this "scaled" export avoids the grid-artifact bug documented in
# ModelTC/Qwen-Image-Lightning issue #32 (plain unscaled fp8 downcast +
# bf16-trained LoRA). It does avoid THAT bug, but introduces a worse one:
# lightx2v's plain "_scaled" file stores its fp8 dequantization factors as
# per-block `*.weight_scale` tensors in a layout ComfyUI's UNETLoader does not
# recognize for this architecture. Confirmed against a real run: ComfyUI logs
# "unet unexpected: [...hundreds of .weight_scale keys...]" and silently
# drops all of them — the fp8 weights then load uncalibrated and the model
# produces pure noise, no denoising, regardless of sampler/cfg/shift.
# lightx2v also ships this exact base + 4-step LoRA already merged, using
# their own "_comfyui_" naming to signal ComfyUI-native compatibility — which
# we verified: same run, same node graph, zero "unexpected keys", coherent
# output. Use this file. There is no separate LoRA node anymore — it's baked in.
#   diffusion_models/qwen_image_2512_fp8_e4m3fn_scaled_comfyui_4steps_v1.0.safetensors  20.44 GB
#   text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors                                   9.38 GB
#   vae/qwen_image_vae.safetensors                                                        0.25 GB
#   total                                                                                ~30.1 GB
fetch "lightx2v/Qwen-Image-2512-Lightning" \
  "qwen_image_2512_fp8_e4m3fn_scaled_comfyui_4steps_v1.0.safetensors" \
  "${MODELS_ROOT}/qwen-image-2512/diffusion_models"
fetch "Comfy-Org/Qwen-Image_ComfyUI" \
  "split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors" \
  "${MODELS_ROOT}/qwen-image-2512/text_encoders"
fetch "Comfy-Org/Qwen-Image_ComfyUI" \
  "split_files/vae/qwen_image_vae.safetensors" \
  "${MODELS_ROOT}/qwen-image-2512/vae"

echo
echo "=== Phase 1 done: FLUX.2-klein-4B + Qwen-Image-2512-Lightning (merged) ==="
du -sh "${MODELS_ROOT}/flux2-klein-4b" "${MODELS_ROOT}/qwen-image-2512" 2>/dev/null || true

# =============================================================================
# Optional extras
# =============================================================================

if [ "$WITH_EDIT" = "1" ]; then
  # Qwen-Image-Edit-2511 (Apache-2.0, commercial), fp8mixed variant. 20.53 GB.
  # This repo ships diffusion_models only — it reuses the same text encoder
  # (qwen_2.5_vl_7b) and VAE (qwen_image_vae) as the base Qwen-Image-2512
  # checkpoint fetched in phase 1, so we do not re-download them; requires
  # phase 1 to have already run.
  fetch "Comfy-Org/Qwen-Image-Edit_ComfyUI" \
    "split_files/diffusion_models/qwen_image_edit_2511_fp8mixed.safetensors" \
    "${MODELS_ROOT}/qwen-image-edit-2511/diffusion_models"
fi

if [ "$WITH_WAN" = "1" ]; then
  # Wan2.2-TI2V-5B fp16 (Apache-2.0, commercial) — the Spark-VERIFIED variant
  # per research.md (single 5B model, not the 14B MoE).
  #   diffusion_models/wan2.2_ti2v_5B_fp16.safetensors                 9.4 GB
  #   text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors             6.74 GB
  #   vae/wan2.2_vae.safetensors                                       1.41 GB
  #   total                                                           ~17.5 GB
  #
  # NOTE (assumption, documented not silently applied): lightx2v/Wan2.2-Lightning
  # only ships 4-step LoRAs for the 14B A14B MoE model (separate high/low-noise
  # expert LoRAs), not for TI2V-5B. Applying a 14B-shaped LoRA to the 5B model
  # is not a compatible operation. workflows/i2v_wan.json is therefore the
  # non-distilled, full-step (30-step) path — no Lightning variant exists for
  # this checkpoint. Use --with-wan-a14b-lightning only if you switch the
  # pipeline to the full A14B checkpoints (much larger — ~28.6GB fp8 diffusion
  # models alone, on top of this download; do not run both loaded at once,
  # see comfyui/README.md rule #1).
  fetch "Comfy-Org/Wan_2.2_ComfyUI_Repackaged" \
    "split_files/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors" \
    "${MODELS_ROOT}/wan2.2-ti2v-5b/diffusion_models"
  fetch "Comfy-Org/Wan_2.2_ComfyUI_Repackaged" \
    "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors" \
    "${MODELS_ROOT}/wan2.2-ti2v-5b/text_encoders"
  fetch "Comfy-Org/Wan_2.2_ComfyUI_Repackaged" \
    "split_files/vae/wan2.2_vae.safetensors" \
    "${MODELS_ROOT}/wan2.2-ti2v-5b/vae"
fi

if [ "$WITH_WAN_A14B_LIGHTNING" = "1" ]; then
  # 4-step Lightning LoRAs for Wan2.2 A14B I2V (high+low noise experts).
  # ~2.46 GB. Only useful if you also fetch the A14B fp8 diffusion models
  # (not done by this script — add explicitly, ~28.6GB, if you go this route).
  fetch "lightx2v/Wan2.2-Lightning" \
    "Wan2.2-I2V-A14B-4steps-lora-rank64-Seko-V1/high_noise_model.safetensors" \
    "${MODELS_ROOT}/wan2.2-lightning/loras"
  fetch "lightx2v/Wan2.2-Lightning" \
    "Wan2.2-I2V-A14B-4steps-lora-rank64-Seko-V1/low_noise_model.safetensors" \
    "${MODELS_ROOT}/wan2.2-lightning/loras"
fi

if [ "$WITH_TTS" = "1" ]; then
  # ResembleAI Chatterbox Multilingual es-ES (MIT, commercial, watermarked).
  # Fetched here for inventory/reuse; the TTS container manages its own
  # model_cache — see ../tts/compose.yaml.
  fetch "ResembleAI/Chatterbox-Multilingual-es-es" "*" \
    "${MODELS_ROOT}/chatterbox-multilingual-es-es"
fi

if [ "$WITH_MUSIC" = "1" ]; then
  # ACE-Step 1.5 turbo all-in-one checkpoint (MIT, commercial). ~7 GB.
  fetch "Comfy-Org/ace_step_1.5_ComfyUI_files" \
    "checkpoints/ace_step_1.5_turbo_aio.safetensors" \
    "${MODELS_ROOT}/ace-step-1.5/checkpoints"
fi

if [ "$WITH_LTX" = "1" ]; then
  # LTX-2.5 is GATED on Hugging Face (auto-gated, license must be accepted
  # while logged in). This script does NOT bypass that — it will fail loudly
  # if HF_TOKEN is unset or the license hasn't been accepted:
  #   1. Accept the license at https://huggingface.co/Lightricks/LTX-2.5
  #   2. export HF_TOKEN=hf_xxx   (never hardcode it here or in any file)
  #   3. re-run with --with-ltx
  if [ -z "${HF_TOKEN:-}" ]; then
    echo "ERROR: --with-ltx requires HF_TOKEN (LTX-2.5 is a gated repo)." >&2
    echo "       export HF_TOKEN=hf_xxx after accepting the license at" >&2
    echo "       https://huggingface.co/Lightricks/LTX-2.5" >&2
    exit 1
  fi
  echo "==> Comfy-Org does not (yet, as of 2026-09-09) publish a repackaged"
  echo "    LTX-2.5 checkpoint — only LoRAs for LTX-2.3. Falling back to the"
  echo "    gated upstream repo directly; verify file layout once fetched,"
  echo "    it will NOT match the split_files/ convention used elsewhere."
  fetch "Lightricks/LTX-2.5" "*" "${MODELS_ROOT}/ltx-2.5"
fi

echo
echo "=== Disk usage per model ==="
du -sh "${MODELS_ROOT}"/*/ 2>/dev/null || true
