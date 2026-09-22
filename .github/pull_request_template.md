## Summary

<!-- What changed and why, in a sentence or two. -->

## Linked issue

<!-- Fixes #123, or "none" -->

## Verification

<!-- How you confirmed this works: command run, test added, screenshot/GUI check, etc. -->

## Checklist

- [ ] `uv run ruff check src tests` passes
- [ ] `uv run mypy` passes
- [ ] `uv run pytest -q` passes
- [ ] `npm --prefix web run typecheck` passes
- [ ] `npm --prefix web run build` passes
- [ ] New/changed env vars are reflected in `.env.example` and `src/lenovmail/config.py`
- [ ] New/changed database columns/tables have an Alembic migration under `migrations/versions/`
- [ ] New source files start with the `# Lenovmail — authored by satuapps` credit line
