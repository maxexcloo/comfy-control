# Architecture

## Runtime

Each Comfy Control worker runs two supervised processes in one container:

- **Comfy Control worker** exposes authenticated internal execution and, in Pod
  mode, separately authenticated native ComfyUI routes on port 8000.
- **ComfyUI** executes workflows locally on port 8188 and is not modified by this
  project.

The `control` command is the public OpenAI-compatible service. It owns public model
IDs, ordered fallback, provider lifecycle, durable video jobs, account telemetry,
media provenance and the operations dashboard. Workers validate canonical
execution requests, render catalogue workflows and execute them in local ComfyUI.
Managed providers are selected by stable configured names. Their resource IDs and
serving URLs are discovered through provider APIs and kept in controller runtime
state, so provider-assigned addresses can change across starts without changing
deployment configuration.

Provider integrations are isolated by platform. Modal, RunPod, SaladCloud, Vast.ai
and CLI Proxy API each own their discovery, status, lifecycle additions and telemetry in
separate modules behind a small common adapter contract. The controller coordinates
adapters without containing provider API URLs or response-shape branching.

The root `control/` and `worker/` packages own their respective runtime behaviour
and image builds. Catalogue loading lives in `catalogue/`, and provider integrations
and deployment implementations live in `providers/`. Dashboard, inference,
operation and provider-deployment implementations are further split within their
owning package; filenames do not encode their parent package.

## Configuration

The control plane has a deliberately small bootstrap environment:

- `CONTROL_API_KEY` authenticates inference clients;
- `CONTROL_SECRET_KEY` encrypts saved credentials;
- `CONTROL_UI_PASSWORD` and `CONTROL_UI_USERNAME` authenticate administrators.

Managed Pod workers receive those same credentials for their ComfyUI frontend;
there is no separate ComfyUI credential setting.

Provider credentials, deployment preferences, routing, limits and worker settings
are versioned in SQLite. Secrets use authenticated encryption and are never returned
by the settings API. The administrator dashboard applies a validated configuration
snapshot atomically; updates are rejected while inference requests are active.
Explicit environment variables override their corresponding SQLite settings on
every start. Environment-controlled fields are marked as locked in the settings API
and dashboard and cannot be changed there. Removing an environment variable reveals
the previously stored value.

Catalogue profile YAML defines workload requirements, cost ceilings and service
classes; workflow manifest aliases define exact public model IDs. The packaged
Python registry combines that catalogue with provider capabilities and safe
control-plane requests. SQLite stores ordered provider-and-model route targets and
operator preferences, while the UI exposes only known typed values. Route children
inherit the Images or Videos order and skip a selected package when it does not
implement that operation. Arbitrary provider URLs, headers and request bodies are
not editable through the dashboard. `CONTROL_CONFIG` remains an advanced explicit
override for development and private provider catalogues.

Automation can read or atomically replace both route families through `GET` or
`PUT /ops/provider-routes`; the live contract is published at `/openapi.json`.

## Request Flow

1. The control plane authenticates the request and selects configured targets.
   Exact public model IDs use ordered compatible-provider fallback. A qualified
   `provider/model` ID pins one provider and disables fallback for that request;
   provider aliases are accepted.
2. It starts a stopped provider when lifecycle controls are configured.
3. The worker checks the requested catalogue model against installed files and
   registered ComfyUI node types.
4. Portable request fields are copied into a fresh API-format workflow graph.
5. The controller submits a canonical request to the worker's unversioned
   `/internal/executions` endpoint. Workers do not expose the public API.
6. The request is admitted to the bounded queue.
7. GPU execution is serialised and submitted to local ComfyUI.
8. On a retriable failure, the control plane tries the next provider configured for
   that exact model. Grok image and video models remain available only through their
   explicit public IDs; they are never silent substitutes.
9. Results return as base64 or an authenticated URL.

A timed-out workflow is removed from the ComfyUI queue or interrupted when already
running.

## History & Media

The control plane records every image generation, image edit and video request in
SQLite. Explicit schema migrations preserve current data. History retains
sanitised parameters, every provider attempt, status, errors and timestamps. Input
and output media are content-addressed by SHA-256, include intrinsic dimensions or
duration where available, and are associated with their generation as ordered
inputs or outputs. The media library uses FTS-backed fuzzy prompt lookup, structured
parameter filters and source-to-derivative links. Assets remain viewable after a
worker is stopped or destroyed.
A controller-owned media service handles bounded remote input resolution, private
network protection, archive downloads, durable paths and media persistence. Provider
orchestration does not perform media network or filesystem operations directly.
The dashboard uses a normal sign-in form backed by an HTTP-only signed session
cookie; it does not use browser Basic authentication. It queries each provider
control plane for current state without routing work to serverless capacity and
provides confirmed lifecycle controls, sanitised control logs, direct
provider-console links and a bounded image-generation test request. It opens images
and videos in a keyboard-accessible popup viewer after sign-in. History and events
are searched, filtered, counted and paginated directly in SQLite. The server-rendered
interface uses a shared Jinja layout, Basecoat components and HTMX interactions.
The default Generate page is model-first; operational detail remains compartmentalised
under Deployments, Activity and Settings. History, jobs, provider resources and generated media remain
in the persistent `data` volume; controller events are also persisted there and
bounded to the latest 2,000 records. Provider actions record their start, success or
failure, and display live controller logs in a closeable dialog.

Video jobs and input media are durable controller records. HTTP image inputs are
downloaded with private-network protection and bounded size; controller media URLs
and base64 data URLs can also be used as video inputs. Completed outputs are copied
from the worker before its compute can be released. Controller-owned history and
media remain available in the `data` volume until that volume is explicitly pruned.
Generate-page submissions are durable SQLite requests rather than in-memory browser
jobs. A controller restart records interrupted work explicitly and retains its
status for the interface.

## Runtime Modes

The control image runs the central router and dashboard. The worker image uses
`WORKER_MODE=pod` to serve the ComfyUI frontend with basic authentication, or
`WORKER_MODE=serverless` to block the frontend. Both worker modes provide:

- native ComfyUI proxy routes and WebSockets;
- `GET /health/live`, `/health/ready`, `/health` and `/metrics`;
- authenticated `/internal/info` and `/internal/executions` routes.

The gateway uses one API key. Terminate TLS and enforce client identity, quotas,
rate limits and deployment selection in an external trusted gateway. Health and
metrics endpoints intentionally remain unauthenticated and expose no generated
content.

`GENERATION_QUEUE_LIMIT` defaults to `2`: one active interactive generation and one
queued generation. Video jobs use a single durable FIFO runner. `MAXIMUM_REQUEST_MIB`
defaults to `100`.
`COMFYUI_REQUEST_TIMEOUT` bounds individual ComfyUI calls and defaults to 60
seconds; `GENERATION_TIMEOUT` bounds a complete workflow and defaults to 900
seconds.

Vast.ai Serverless runs its supported PyWorker in front of the normal Serverless
worker. PyWorker verifies routed requests, benchmarks the backend, reports load and
readiness, and embeds generated outputs in the Serverless response.

Control and worker publish their live OpenAPI documents at `/openapi.json` and
interactive documentation at `/docs`. Controller operations are under `/ops`,
worker execution is under `/internal`, and both are unversioned current contracts.
Operations publish named request and response schemas for generated clients.
Route groups are registered as focused routers; history, media, provider logs and
settings operations are independent from dashboard pages and inference handling.

## Provider Telemetry

Account telemetry is cached for one minute and cannot take inference offline.
CLI Proxy API reports request and token usage and obtains sanitised remaining Grok
allowances through its per-credential Management API proxy. RunPod reports Pod or
Endpoint billing history, Vast.ai reports account credit, Modal reports billing-cycle
spend and credits used, and SaladCloud reports public replica usage and quota.
