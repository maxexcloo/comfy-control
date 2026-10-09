# AGENTS.md

## Structure

- Keep code in root `catalogue/`, `control/`, `providers/` and `worker/` packages;
  keep each runtime's image build with its package.
- Keep detailed operational documentation in `docs/`; keep `README.md` focused on
  purpose, architecture and the shortest working setup.
- Keep pinned model sources in `catalogue/profiles/`.
- Keep provider deployment implementations in `providers/deployment/`.
- Keep tests in `tests/`, grouped by the same runtime boundaries as the package.
- Keep local orchestration in root `compose.yaml`.
- Keep workflow manifests beside their API-format workflow JSON in `catalogue/`.

## Style

- Keep `main()` and execution guards last.
