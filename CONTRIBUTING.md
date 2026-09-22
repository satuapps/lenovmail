# Contributing

## Setup

Requires Python 3.13, Node 22, Docker, and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                          # installs the dev dependency group too
docker compose up -d db redis    # Postgres 17 on :5433, Redis on :6380 (see docker-compose.yml)
cp .env.example .env
python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"  # -> LENOVMAIL_SECRET_KEY
uv run alembic upgrade head
npm --prefix web install
```

Run the API and worker in separate terminals:

```bash
uv run uvicorn lenovmail.api.app:app --reload --port 8080
uv run arq lenovmail.workers.main.WorkerSettings
npm --prefix web run dev         # GUI dev server, proxies API calls to :8080
```

`uv run lenovmail --help` lists the CLI (`bootstrap-admin`, `discover`, `sync`, `agent-token`,
`show-account`, `serve`, `janitor`).

## Checks a PR must pass

Run these before opening a PR; CI (`.github/workflows/ci.yml`) runs the same commands.

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
uv run pytest -q
npm --prefix web run typecheck
npm --prefix web run build
```

## Testing policy

- **Unit tests** (`tests/unit/`) that need a database use the `session`/`account`/`folder`
  fixtures in `tests/conftest.py`. Those fixtures call `pytest.skip(...)` — not fail — when
  Postgres isn't reachable, so `uv run pytest -q` without `docker compose up -d db` still runs
  cleanly; it just skips the DB-backed cases.
- **E2E tests** (`tests/e2e/`) exercise a real IMAP/SMTP server (GreenMail) and need:

  ```bash
  docker compose -f docker-compose.test.yml up -d
  uv run pytest -q tests/e2e
  ```

  GreenMail exposes IMAP on `localhost:3143`, IMAPS on `3993`, SMTP on `3025`, SMTPS on `3465`
  (see `docker-compose.test.yml`). It auto-creates a user from the delivery address, so mail
  sent to `demo@localhost` may land in that auto-created mailbox — that's GreenMail behavior,
  not something the test is asserting about Lenovmail.

## Code conventions

These are enforced by `ruff`/`mypy` where mechanical, and expected in review otherwise:

- Every source file (`.py`, `.ts`/`.tsx`, `.css`, `.yml`) starts with a
  `# Lenovmail — authored by satuapps` credit line in that language's comment syntax
  (`//` for TypeScript, `/* */` for CSS). Markdown files carry the maintainer line at the
  bottom instead.
- `ruff` line length is 100 (`[tool.ruff]` in `pyproject.toml`); lint rules are
  `E, F, I, UP, B, ASYNC, C4, SIM` with `B008` and `SIM108` ignored.
- Every module opens with `from __future__ import annotations`.
- `mypy` runs with `check_untyped_defs = true` against `src/lenovmail`; new code should type
  clean, not rely on `# type: ignore`.
- Logging goes through `structlog` (`lenovmail.logging.get_logger`). Event names are stable
  identifiers, not sentences — `snake_case`, matched against in tests and dashboards
  (e.g. `agent_audit_failed`, `janitor_account_disabled`). Don't rename an existing event name
  without checking for callers/tests that match on it.
- Comments explain *why*, not *what* — see `crypto.py` or `maintenance.py` for the house style:
  a short module docstring stating the design decision, then code with comments only where the
  reason isn't obvious from reading it.
- New database access goes through the existing session/query patterns in `sync/store.py` and
  `sync/queries.py` rather than ad hoc `select()` calls scattered across callers.

## Commit messages

Single-line, imperative, lower-case subject, no trailing period, optionally scoped by area:

```
janitor: stamp invalid_since on first rejection
api: reject moves into a different account's folder
web: virtualize the message list
```

Keep a commit to one logical change. Reference an issue with `Fixes #123` in the body when
applicable.

## Adding an Alembic migration

Revision ids in this repo are not Alembic's random hex — they're zero-padded sequential
integers (`0001`, `0002`, `0003`, ...), matching the files in `migrations/versions/`.

1. Generate the scaffold:

   ```bash
   uv run alembic revision -m "short description"
   ```

2. Alembic writes a file with a random hex revision id. Rename it to the next sequence number
   (check `migrations/versions/` for the current highest) and edit the generated file so:
   - `revision = "000N"` (the new zero-padded number)
   - `down_revision = "000N-1"` (the previous head)
   - the docstring states *why* the column/table exists, not just what changed — see
     `migrations/versions/0003_account_invalid_since.py` for the expected shape.
3. Write `upgrade()` and `downgrade()` by hand; this project does not use `--autogenerate`.
4. Apply and verify:

   ```bash
   uv run alembic upgrade head
   uv run alembic downgrade -1 && uv run alembic upgrade head   # downgrade round-trips
   ```

## Pull requests

Use the PR template. Keep PRs scoped to one change; unrelated formatting or refactors go in a
separate PR.

---

Maintained by [satuapps](https://github.com/satuapps).
