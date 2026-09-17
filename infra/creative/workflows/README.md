# Workflows — ComfyUI `/prompt` API format

Each file here is a plain dict of `node_id -> {class_type, inputs, _meta}`,
exactly the shape ComfyUI's `POST /prompt` endpoint expects wrapped as
`{"prompt": <this dict>}`. Placeholders (`{{prompt}}`, `{{width}}`,
`{{height}}`, `{{seed}}`, `{{image}}`) are string tokens the caller must
substitute **before** JSON-parsing, not after — substitute the numeric ones
(`width`/`height`/`seed`) including their surrounding quotes so the result is
a JSON number, not a quoted string:

```python
import json

template = open("workflows/image_fast_klein.json").read()
filled = (template
    .replace('"{{prompt}}"', json.dumps(prompt_text))
    .replace('"{{width}}"', str(width))
    .replace('"{{height}}"', str(height))
    .replace('"{{seed}}"', str(seed)))
payload = {"prompt": json.loads(filled)}
# requests.post("http://127.0.0.1:8188/prompt", json=payload)
```

`{{image}}` (i2v_ltx.json only) is the filename of an image already uploaded
via ComfyUI's `POST /upload/image` — upload first, then substitute the
returned filename as a quoted string (`'"{{image}}"'.replace(..., json.dumps(name))`).

## Provenance — what's verified vs guessed

Per-file, from most to least confident. Where a specific node's parameters
were guessed, the JSON itself carries a `_meta.note` field explaining why —
those are the authoritative source; this table is a summary.

### `image_text_qwen.json` — Qwen-Image-2512-Lightning, 4-step (merged checkpoint)

**Postmortem (2026-09-09): this workflow produced pure noise in production
(reported by the coordinator, reproduced and root-caused against the real
container). Root cause + fix below — read before touching this file again.**

Base graph decoded directly from the `prompt` (API-format) metadata embedded
in the official example image at
[comfyanonymous.github.io/ComfyUI_examples/qwen_image/qwen_image_basic_example.png](https://comfyanonymous.github.io/ComfyUI_examples/qwen_image/qwen_image_basic_example.png),
cross-checked against ModelTC/LightX2V-Qwen-Image-Lightning's own official
4-step workflow
([`workflows/qwen-image-4steps.json`](https://github.com/ModelTC/LightX2V-Qwen-Image-Lightning/blob/main/workflows/qwen-image-4steps.json)),
which is now the authoritative source for every sampler value here:
`seed, steps=4, cfg=1, sampler="euler", scheduler="simple", denoise=1` and
`ModelSamplingAuraFlow.shift=3`.

**What was wrong (root cause):** the original version loaded lightx2v's
plain `qwen_image_2512_fp8_e4m3fn_scaled.safetensors` (base only) through
`UNETLoader`, then applied the 4-step Lightning LoRA via a separate
`LoraLoaderModelOnly` node — the "keep the base swappable" design the task
originally asked for. The graph, sampler settings, and LoRA strength were
all otherwise correct (cfg=1.0, steps=4, shift≈3, LoRA strength_model=1.0,
CLIPLoader type="qwen_image" all matched the official example exactly), so
none of those were the bug. The actual bug: lightx2v's plain `_scaled`
export stores its fp8 dequantization factors as per-block `*.weight_scale`
tensors in a naming/layout ComfyUI's `UNETLoader` does not recognize for
this architecture. Confirmed directly in this container's own logs:

```
[WARNING] unet unexpected: ['transformer_blocks.0.img_mod.1.weight_scale', ... 720 keys total ...]
```

All 720 scale tensors get silently dropped. Without them the fp8 weights
are loaded uncalibrated (essentially the raw e4m3fn mantissa with no
multiplier), so the "denoised" output is numerically meaningless — pure
noise — no matter what the sampler/cfg/shift are set to. This is *worse*
than the grid-artifact bug in
[ModelTC/Qwen-Image-Lightning#32](https://github.com/ModelTC/Qwen-Image-Lightning/issues/32)
that motivated picking the "scaled" file in the first place; we traded a
cosmetic bug for total breakage.

**The fix:** lightx2v also publishes this exact base + 4-step LoRA already
merged, in a format their own filename promises is ComfyUI-native:
`qwen_image_2512_fp8_e4m3fn_scaled_comfyui_4steps_v1.0.safetensors`. Loading
that through the same plain `UNETLoader` produces **zero** "unexpected
keys" warnings and correct, coherent output — verified both at 512×512 and
at 1080×1350 with a Spanish ad prompt (headline text rendered perfectly
legible; see `../smoke/qwen_es_1080x1350.png`). `LoraLoaderModelOnly` is
gone from the graph — the LoRA is baked into the checkpoint, there is
nothing left to apply separately. `models/download.sh` fetches this file
now; the old plain `_scaled` one is not fetched by anything.

**Residual, expected limitation** (not a bug, not fixed): smaller secondary
ad copy can come out with 1-2 letter-level typos (e.g. "Preara" for
"Prepara", "gradies" for "gratis" in our test) while the large headline text
renders perfectly. This matches the model's own documented trade-off —
ModelTC's README shows the 4-step distilled variant losing accuracy on
dense/small text versus the 50-step base, "even just 10 steps seems to work
[better]" per their own maintainer note. If small-text accuracy becomes a
hard requirement, the escalation path is more steps (their 8-step LoRA,
`Qwen-Image-2512-Lightning-8steps-V1.0-bf16.safetensors`, applied over
Comfy-Org's plain `qwen_image_2512_fp8_e4m3fn.safetensors` via
`LoraLoaderModelOnly` — accepting the milder grid-artifact risk from
issue #32 above — or the full un-distilled base at 30-50 steps) — not
attempted here, out of scope for "fix the noise bug."

Two size presets to use via `{{width}}`/`{{height}}`: **1024×1024** (square)
and **1080×1350** (4:5, common feed-ad portrait format — note the actual
decoded output is 1080×1344, latent-stride rounding, not a bug).

### `image_fast_klein.json` — FLUX.2-klein-4B, 4-step

Base graph decoded from the `prompt` metadata embedded in
[comfyanonymous.github.io/ComfyUI_examples/flux2/flux2_example.png](https://comfyanonymous.github.io/ComfyUI_examples/flux2/flux2_example.png)
— note this official example is a **flux2-dev multi-reference editing**
workflow (two `LoadImage` + `VAEEncode` branches feeding a `FluxGuidance`),
not klein T2I. We stripped both reference-image branches (klein doesn't need
them for plain text-to-image) and kept the verified core:
`UNETLoader` / `CLIPLoader` / `VAELoader` / `RandomNoise` /
`KSamplerSelect` / `BasicGuider` / `SamplerCustomAdvanced` / `FluxGuidance` /
`Flux2Scheduler` / `EmptyFlux2LatentImage` / `VAEDecode` / `SaveImage` — all
node names and links confirmed from that image's embedded API graph.
**Guessed**: `FluxGuidance.guidance = 1.0` — no klein-specific official
example exists yet (klein just launched); the flux2-dev example uses 4.0,
but klein is a distilled/fast model where lower or even near-zero guidance
is typically correct. Flagged in the JSON's `_meta.note` — tune empirically.
Verified working at both 512×512 (~21.8s cold) and 1080×1350 (~28.1s warm;
decoded output 1072×1344, 8px latent-stride rounding).

### `i2v_ltx.json` — LTX-2.5, distilled, image-to-video, 9:16, 5s

**Materially more guessed than the other two — read before trusting this one
in production.** LTX-2.5 is a gated HF repo we could not inspect (no
accepted license / no `HF_TOKEN`), and no 2.5-specific example workflow is
published yet anywhere we could find. This file is adapted from
Lightricks' own official **LTX-2.3** single-stage distilled example,
fetched from
[Lightricks/ComfyUI-LTXVideo `example_workflows/2.3/LTX-2.3_T2V_I2V_Single_Stage_Distilled_Full.json`](https://github.com/Lightricks/ComfyUI-LTXVideo/blob/master/example_workflows/2.3/LTX-2.3_T2V_I2V_Single_Stage_Distilled_Full.json),
on the assumption LTX-2.5 shares LTX-2's joint audio/video architecture
(Gemma-3-12B text encoder, single AV latent) — reasonable given the naming,
**not confirmed**.

That official example is a 44-node canvas comparing two alternate
guider/sampler branches side by side (a `MultimodalGuider` path with
per-modality audio/video CFG weights, and a plain `CFGGuider` path) plus a
`GemmaAPITextEncode` subgraph that, per the actual links array, is **wired
to nothing** (its `conditioning` output has zero downstream links — an
orphaned/experimental node, not the active path). The real, active text
path is `LTXAVTextEncoderLoader` (outputs a standard `CLIP` object) feeding
ordinary `CLIPTextEncode` nodes. We reconstructed a single, minimal I2V
pipeline from the simpler `CFGGuider` branch:

`CheckpointLoaderSimple` → `LTXAVTextEncoderLoader` → `CLIPTextEncode` ×2 →
`LTXVConditioning` → `LoadImage` → `LTXVPreprocess` →
`EmptyLTXVLatentVideo` → `LTXVImgToVideoConditionOnly` →
`LTXVEmptyLatentAudio` → `LTXVConcatAVLatent` → `LTXVScheduler` →
`KSamplerSelect` → `RandomNoise` → `CFGGuider` → `SamplerCustomAdvanced` →
`LTXVSeparateAVLatent` → `LTXVTiledVAEDecode` → `CreateVideo` → `SaveVideo`.

Every node name above is confirmed present in the official example (types,
input/output slot names, and links were all read directly from its JSON —
not invented). What's genuinely guessed, each also flagged in the JSON's
`_meta.note` fields:

1. **Checkpoint filename** (`ltx-2.5-distilled.safetensors`) — gated repo,
   never inspected. Fix once `models/download.sh --with-ltx` actually runs.
2. **`LTXVEmptyLatentAudio` widget meaning** — mirrored positionally
   (`[97, 25, 1]` in the source), field names in our API-format JSON
   (`length`/`sample_rate_divisor`/`batch_size`) are our best inference,
   not confirmed against the node's Python signature.
3. **Dropped the `ResizeImageMaskNode` pre-scaling step** the official
   example has before `LTXVPreprocess` — simplification, may affect
   quality/aspect handling on non-square source images.
4. **Audio branch discarded**, not decoded/muxed into the output video —
   the architecture requires *sampling* a joint AV latent (hence
   `LTXVEmptyLatentAudio`/`LTXVConcatAVLatent`/`LTXVSeparateAVLatent` are
   still present), but we never wire `LTXVAudioVAEDecode` or an `audio`
   input into `CreateVideo`. If that input turns out to be required rather
   than optional, add it (see the note in node `19`).
5. **9:16 @ 5s**: `576×1024` (exact 9:16, both multiples of 32 — LTX's VAE
   spatial stride) and `length=121` frames at `frame_rate=24` (≈5.04s,
   matching the official example's own frame count, itself a valid
   `8n+1` latent-stride length) via the `{{width}}`/`{{height}}` slots.

**Before relying on this workflow**: get `HF_TOKEN`, run
`models/download.sh --with-ltx`, inspect the actual file layout (it will
almost certainly not match the `split_files/` convention the other three
models use — see the script's own warning), open this JSON in the ComfyUI
UI (paste as a `/prompt` body or convert to UI format), and validate/repair
node-by-node against whatever `ComfyUI-LTXVideo` version ships by then. Do
not trust this file blind for a real generation run.
