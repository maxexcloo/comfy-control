# Development

## Setup

```bash
mise run setup
mise run check
```

Setup creates the bootstrap `.env` from its template when missing and installs Prek
hooks. Set the three blank secrets before starting Compose. Checks use an isolated
Python environment; a repository-local `.venv` is not required.

Use `mise run fmt` for supported formatting and `mise run cleanup` to remove test,
lint and bytecode caches.

## Checks

`mise run check` runs repository hygiene, structured-file validation, GitHub
Actions linting, Dockerfile linting, formatting, Python linting and the pytest
suite on the latest Python version pinned by Mise. Run it before handoff.

The container workflow runs these checks before building images. Pull requests also
smoke-test the images; publishing requires successful checks.

## Project Boundaries

- Keep deterministic workflow selection in `catalogue/`, not prompt prose or
  deployment code.
- Keep external field names unchanged; use Australian English for project-owned
  prose and identifiers.
- Keep pinned weight sources in `catalogue/profiles/`.
- Keep catalogue behaviour and data together in `catalogue/`.
- Keep control-plane code and its image build in `control/`.
- Keep provider behaviour in `providers/`.
- Keep provider deployment implementations in `providers/deployment/`.
- Keep built-in provider capabilities, lifecycle and telemetry in the provider
  registry and adapters.
- Keep each provider's API discovery, status and telemetry in its own
  `providers/<name>.py` module.
- Keep safe user-editable preferences in `ControlPreferences` and SQLite.
- Keep tests grouped by the same catalogue, control, provider and worker boundaries.
- Keep worker code and its image build in `worker/`.
- Keep OpenAI-compatible routing in the controller and canonical execution in workers.

## API Contracts

Running control and worker services are the source of truth for API contracts.
Read `/openapi.json` for machine consumption or `/docs` for the interactive view.
Do not commit generated OpenAPI snapshots or add project-owned version prefixes.
