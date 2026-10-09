# Catalogue & Models

## Workflows

Each directory under `catalogue/` contains a manifest beside its checksum-pinned
API-format ComfyUI workflow JSON. The manifest maps portable API fields to concrete
node inputs:

Directory names use `<profile>-<operation>`, where the operation identifies the
workflow implementation. Each manifest's `profile` must exactly match a
`catalogue/profiles/<profile>.yaml` file, and its ID uses
`<profile>/<operation-name>`.
Source-free profiles with zero minimum VRAM describe pipelines made entirely from
built-in ComfyUI nodes.

```yaml
id: flux-2-klein-9b/text-to-image
profile: flux-2-klein-9b
operation: image_generation
workflow: workflow.json
input_map:
  prompt: { node: "4", input: text }
  width: { node: "6", input: width }
  height: { node: "6", input: height }
  seed: { node: "7", input: noise_seed }
output: { node: "13", type: image }
```

A workflow is advertised only when all `required_files` exist and every workflow
`class_type` is registered by ComfyUI. Stale or incomplete workflows are hidden and
rejected when addressed directly.

To add a custom workflow, place its API-format `workflow.json` and `model.yaml`
manifest in a directory mounted through `CATALOGUE_DIR`, then restart the worker.
UI-format ComfyUI workflows are not accepted; export them using **Export (API)**.
Repository checks validate bundled catalogue and profile changes.

## Bundled Models

The catalogue includes FLUX.2 Klein 9B and Krea 2 Turbo image generation,
FLUX.2 Klein 9B single-image edit, and MiniMax Hailuo 2.3 video
workflows. Reference meaning belongs in the prompt; Comfy Control does not assign
character roles or rewrite prompts.

The studio exposes model-specific public IDs (`flux-2-klein-9b`,
`flux-2-klein-9b-edit`, `krea-2-turbo`, `minimax-h3` and `minimax-h3-image`). This
keeps model selection exact while allowing automatic fallback between compatible
providers. The 4B FLUX Klein variant is not installed or offered.

Image edits accept `image`, `prompt`, and optional `n`, `seed`, `steps` and
`response_format`. Their dimensions follow the uploaded image. MiniMax video
requests accept `prompt`, `size` and `seconds`; image-to-video also accepts the
first-frame `image`.

## Image Upscaling

`POST /v1/images/upscales` routes an uploaded image through the same configured
image-provider order as generation. The bundled
`image-upscale/realesrgan-x4plus` pipeline uses ComfyUI's native model-upscale
nodes and the pinned RealESRGAN x4plus model. It accepts a final `scale` from
greater than 1 through 4 and defaults to 2×:

```bash
curl -D response-headers.txt \
  -F image=@source.png \
  -F model=image-upscale \
  -F response_format=url \
  -F scale=2 \
  -H "Authorization: Bearer ${CONTROL_API_KEY}" \
  https://comfy-control.example/v1/images/upscales
```

The response includes `x-comfy-duration-seconds`, `x-comfy-history-id` and
`x-comfy-provider`. History stores the model and requested scale; Media records the
source and output dimensions and links both assets. Every request first performs
the native 4× AI enhancement. A 2× or 3× request then downsamples that enhanced
result to its requested final dimensions. This makes the runs directly comparable
by provider, elapsed time, file size and visual result.

## Profiles

Profiles under `catalogue/profiles/` pin weight sources independently from
workflow logic while keeping all model catalogue data in one place. A profile also
owns its stable ID, capabilities, VRAM limits, service class, idle policy, automatic
hourly cost ceiling and packaged benchmark observations. Workflow aliases supply
the public model IDs shown by the API and studio; consumers do not maintain parallel
model-card or routing metadata.
The control-plane Settings page defaults to `flux-2-klein-9b`; select one or more
profile names there to change the models prepared on managed workers.

Automation can read or replace the desired set through the typed current API at
`GET` or `PUT /ops/model-packages`; its schema is published in `/openapi.json`.
Changing the desired set takes effect when managed providers are next deployed.

Hugging Face sources require a pinned `revision` and may use `HF_TOKEN`. Civitai
sources use an immutable `version_id`, a ComfyUI-relative `destination`, and may
include `filename` and `sha256`.

Fetch only the profiles a deployment runs. Weight storage and GPU requirements are
independent costs; bundling more profiles does not make a request faster.

Publishing model weights may impose upstream licence obligations. Review each
model licence before enabling public package access.
