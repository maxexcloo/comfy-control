# Deployment & Operations

## Container

GitHub Actions publishes separate control-plane and worker images from `main`:

```text
ghcr.io/OWNER/comfy-control:control
ghcr.io/OWNER/comfy-control:worker
```

Every build receives role-specific `sha-<short-sha>-control` and
`sha-<short-sha>-worker` tags. A published control image automatically deploys the
worker image built from the same source revision. An explicit `WORKER_IMAGE`
override can instead select another immutable build or digest. Comfy Control has
one current API and does not retain versioned or legacy contracts.

The lightweight control image contains the API, dashboard and provider control
plane. The CUDA-enabled worker image contains Comfy Control, pinned ComfyUI, the
bundled catalogue and model profiles. Run workers on CUDA-capable hosts with
persistent model and output storage. Both images use the pinned Python runtime;
the worker gets its CUDA 13 runtime from the pinned PyTorch wheels.

Deploy matching control and worker images. The controller uses the current
unversioned internal execution contract; compatibility with older worker APIs is
not retained.

For a local GPU host:

```bash
cp .env.example .env
# Set CONTROL_API_KEY, CONTROL_SECRET_KEY and CONTROL_UI_PASSWORD.
docker compose up --build --detach
docker compose ps
```

Pull requests build both images and smoke-test their packages.

Compose starts only the control image. Managed providers are enabled by their Modal,
RunPod, SaladCloud or Vast.ai management credential. The controller discovers
resources by their configured stable name and refreshes provider-assigned worker
URLs at runtime; do not store those URLs in deployment configuration. Add
`CLIPROXY_MANAGEMENT_KEY` to display CLI Proxy API usage.

Keep the controller's `/data` directory on persistent storage. It contains the
SQLite job history and controller-owned copies of generated media used by the
dashboard viewer. There is no automatic retention deletion.

## Providers

The controller builds Modal, RunPod, SaladCloud and Vast.ai requests from the
selected model policy and current provider capacity. Their deployment
implementations and shared worker defaults live together in `providers/deployment/`
rather than in copied JSON templates.

Managed providers are credential-driven. Unconfigured providers remain visible and
link to Settings. When no named resource exists, the dashboard shows `Not deployed`
and offers Deploy. A routed request can also deploy the resource, wait for its
discovered URL to become healthy, forward the request, and apply the configured idle
policy. Provider resource identifiers are saved in the controller database; serving
URLs are rediscovered because they may change after a start or scale event.

If RunPod reports that an existing Pod's host no longer has a free GPU, Comfy
Control creates the cheapest available compatible replacement from the configured
GPU classes. It keeps the old Pod until the replacement is healthy, then removes
the old resource. A failed replacement is removed and the old resource identifier
is restored. RunPod Pod volumes are local to their Pods, so model files are prepared
again on a replacement. Set `RUNPOD_NETWORK_VOLUME_ID` or the RunPod **Network
Volume ID** setting to attach portable model storage to both Pod and Serverless
deployments. Pods mount it at `/opt/ComfyUI/models`; Serverless workers use RunPod's
`/runpod-volume` mount. A network volume restricts capacity to its data centre, so
choose GPU and data-centre preferences that have stock in the same location.

Configure provider credentials, `HF_TOKEN`, model profiles and GPU preferences
through Settings. The packaged controller locks its worker image to the matching
source revision. Set `WORKER_IMAGE` only to override that default with another
immutable release tag or digest. SaladCloud also requires its organisation and
project because its public API uses both slugs as security boundaries. Comfy Control
discovers a suitable current GPU class for SaladCloud. RunPod and Vast.ai select
suitable available GPU capacity through their APIs. GPU classes, regions, worker
counts and minimum Vast.ai GPU memory are optional preferences rather than
prerequisites.

Automatic capacity selection applies workload policy before price ordering. FLUX.2
Klein 9B requires 24 GB VRAM (32 GB recommended), while Krea 2 Turbo requires and
recommends 48 GB. Both use an image capacity ceiling of USD 0.60/hour. MiniMax
Hailuo 2.3 requires 80 GB VRAM and
has a USD 2.00/hour ceiling. Known benchmarked combinations rank ahead of unverified
ones; when no proven option is available, compatible unverified capacity is allowed
and labelled experimental. A manually selected GPU may exceed the automatic spend
ceiling, but the choice remains visible rather than silently changed.

These requirements live in the catalogue profiles and are
shared by deployment filtering, the API and the interface.

Automatic cross-provider routing uses Modal first while current metered monthly
usage remains below its USD 30 included allowance. Once that allowance is consumed,
providers with known compatible prices are ordered by hourly cost. Unknown prices
remain usable after known offers rather than being assigned an invented estimate.

Image workers remain warm for 15 minutes after their last use. Readiness means both
ComfyUI and the configured image model have completed a real warm-up generation, so
the interface distinguishes cold capacity waiting from warm inference. Cold starts
are reported honestly and are not included in the under-20-second warm image target.

Optional provider and worker environment variables override their corresponding
SQLite settings on every start. The dashboard marks those fields as controlled by
the environment and does not allow them to be edited. Removing a variable restores
the previously stored value. Back up `CONTROL_SECRET_KEY` with the data volume: a
replacement key cannot decrypt existing saved credentials.

Deploy creates all provider-side compute dependencies: a Pod or container group for
Pod-style providers, a RunPod template and endpoint for RunPod Serverless, and a Vast.ai
template, endpoint and workergroup for Vast.ai Serverless. Stop preserves a reusable Pod
or container group. Terminate deletes its compute resource; provider-local disks may be
deleted with that resource. Generated media is copied to the controller's persistent
`/data` volume before idle shutdown and does not depend on provider storage.

Terminate is an explicit forced lifecycle action. It can delete provider compute
while a request is stuck so billing can be stopped; the interrupted request fails
normally when its provider connection closes. Stop remains guarded while requests
are active because it is intended as a recoverable idle transition.

Use persistent storage for model weights where the provider supports it. Video job
state and generated media are owned by the controller. The worker image defaults to
`WORKER_MODE=pod`; set `WORKER_MODE=serverless` when the ComfyUI frontend should not
be exposed. Vast.ai Serverless starts its request gateway directly for supported
PyWorker readiness and load reporting.

The dashboard displays RunPod credit, Vast.ai credit and spend, Modal billing-cycle
spend, SaladCloud replica quota and CLI Proxy API usage. SaladCloud monetary credit
and spend are currently portal-only, so its supported public API contributes quota
instead.
Comfy Control derives CLI Proxy API request totals from its durable history and reads
Grok allowances from CLI Proxy API's authenticated account data. CLI Proxy API v7
removed the legacy aggregate usage route, while its replacement is a destructive
collector queue and must not be polled by a dashboard. Keep
`usage-statistics-enabled` set to `true` for CLI Proxy API's own telemetry.

## Sizing and scale behaviour

A bounded live comparison on 14 August 2026 used Flux 2 Klein 9B, Krea 2 Turbo and
Real-ESRGAN on the same current worker image. These figures are observations rather
than provider guarantees:

| Provider           | Compute                         |              First ready image |                                Ready generation | Ready 2× upscale |
| ------------------ | ------------------------------- | -----------------------------: | ----------------------------------------------: | ---------------: |
| RunPod Pod         | A40 48 GB, USD 0.44/hour        |                    492 seconds | Flux 4 seconds; Krea 63 seconds on first switch |      6–9 seconds |
| RunPod Serverless  | A40 48 GB, scale to zero        | More than 7 minutes; cancelled |                                     Not reached |      Not reached |
| Vast.ai Pod        | RTX A6000 48 GB, USD 0.478/hour |                    315 seconds |            Flux 6–10 seconds; Krea 9–15 seconds |      5–6 seconds |
| Vast.ai Serverless | Dynamic verified 48 GB worker   | More than 6 minutes; cancelled |                                     Not reached |      Not reached |

The dominant interactive cost was image and model population, not GPU inference.
A RunPod Pod host-capacity replacement took about 389 seconds and lost its local
model cache. Prefer these operating modes:

1. For interactive work, keep one 48 GB Pod warm for a short burst window and use a
   portable model volume where supported. An A40 or A6000 is sufficient; paying for
   an H100 does not solve model-download latency.
2. For sporadic batch work, scale from zero only after splitting worker pools by
   model package or attaching pre-populated shared storage. The current all-model
   ephemeral Serverless worker is not suitable for interactive requests.
3. Generate final images natively at 768×1024 or 1024×1024. In the live comparison,
   native Flux 1024 took 6.6 seconds versus 6.5 seconds at 768, while producing more
   natural detail. Use Real-ESRGAN for accepted legacy or low-resolution images;
   its sharper reconstruction can look over-processed on faces and skin.

RunPod load-balancer endpoints do not expose the worker log API. Comfy Control labels
that limitation in the log modal and continues to stream controller lifecycle
events. Vast.ai Serverless reports that it is waiting for worker assignment until a
routable PyWorker exists; opening logs does not reserve a worker.

## Model Preparation

Managed workers default to the `flux-2-klein-9b` package. Install or remove pinned
packages in Settings, then redeploy a provider. Before starting ComfyUI, the
supervisor prepares the selected package's models and LoRAs and removes files
tracked only by deselected packages. It never removes unmanaged files. Hugging Face
and Civitai credentials are also configured there.

Profiles can also be baked into an image by passing `MODEL_PROFILE` as a build
argument with the corresponding build secret.

## Output Storage

The controller copies successful images and videos into `/data/media` before a
worker is stopped. Keep `/data` on persistent storage; API image URLs and completed
video content are then served by Comfy Control with normal authentication.

## Important Limits

- The project does not grant model redistribution rights.
- Publishing model weights may impose upstream licence obligations.
- Worker-local ComfyUI outputs are transient; the controller copies successful
  results into its persistent media store.
