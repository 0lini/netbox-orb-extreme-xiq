# AGENTS.md

## Cursor Cloud specific instructions

This repo is a single Python package: `orb_extreme_platformone`, an Extreme
Platform ONE discovery worker for the NetBox Labs Orb Agent (extract →
transform → Diode → NetBox). There is no in-repo frontend/backend service; the
"application" is the worker package plus its standalone dry-run entrypoint.

`.devcontainer/` holds an optional local NetBox (Diode plugin) compose stack, a
compose-based Dev Container, and Diode via the **official Diode quickstart**
(downloaded by `setup.sh` into `.devcontainer/diode/`). Codespaces uses
`dockerComposeFile` — no Docker-in-Docker required.

Standard commands are documented in `README.md`, `pyproject.toml`
(`[tool.pytest.ini_options]`, `[tool.ruff]`) and `.github/workflows/ci.yml`.
Dev dependencies are installed by the startup update script (`pip install -e
".[dev]"`).

Non-obvious notes:

- Console scripts (`pytest`, `ruff`) install to `~/.local/bin`, which is NOT on
  PATH here. Run tools as modules instead: `python3 -m pytest`,
  `python3 -m ruff check .`, `python3 -m ruff format --check .`.
- `pytest` deselects `contract` tests by default (see `addopts` in
  `pyproject.toml`). Contract tests need locally downloaded Platform ONE
  OpenAPI spec files (`PLATFORMONE_ASSETS_SPEC` / `PLATFORMONE_CONFIGSTATE_SPEC`)
  and are the only tests that touch external spec files.
- The whole extract→transform pipeline is exercised offline by the test suite:
  `tests/` mock the Platform ONE HTTP API with `responses`, so no credentials
  or network are needed for `python3 -m pytest`.
- The standalone dry-run entrypoint `python3 -m orb_extreme_platformone` hits
  the live Platform ONE cloud APIs and therefore needs real credentials
  (`PLATFORMONE_USERNAME`/`PLATFORMONE_PASSWORD` or `PLATFORMONE_API_TOKEN`,
  read from env or a local `.env`). There is no offline/demo mode for it; use
  the pytest suite to validate pipeline logic without credentials.
- Full E2E (`./.devcontainer/setup.sh` + compose / Dev Container) requires
  Docker plus real Platform ONE credentials; it is optional for package-level
  development.
