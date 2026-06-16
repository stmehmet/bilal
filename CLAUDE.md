# Bilal — Home Adhan System

Python service that plays the call to prayer on Google Nest/Home speakers from a Raspberry Pi.
Two containers, one multi-stage `Dockerfile`, deployed as multi-arch images on `ghcr.io`.

## Layout

- `scheduler/` — APScheduler + pychromecast; computes prayer times, plays adhan (`main.py` entry).
- `web/` — Flask + Gunicorn dashboard (`app.py`).
- `tests/` — pytest suite (`test_*.py`), shared fixtures in `conftest.py`.
- `audio/` — `.mp3` adhan files only (not a build target; gitignored except `.gitkeep`).
- `Dockerfile` — multi-stage: `base` → `scheduler` / `web` targets.

## Commands

- Tests: `pip install -r tests/requirements.txt && pip install pytz && pytest tests/ -v --tb=short`
- Single test: `pytest tests/test_prayer_times.py -v`
- Build images: `docker build --target web -t bilal-web .` and `docker build --target scheduler -t bilal-scheduler .`
- Local stack: `docker compose up -d`

No typecheck/lint tooling is wired in this repo (no ruff/mypy/pyright config). Tests are the gate.

## CI/CD

CI is `.github/workflows/build-push.yml`:

- **test job** — runs on `pull_request` (drafts skipped) and `merge_group`, with `paths-ignore` for docs/images and `concurrency` cancel-in-progress on PRs. Runs the pytest suite.
- **build/push job** — runs on `push` to `main`, `v*` tags, and `workflow_dispatch` (never on `merge_group`). Builds `bilal-web` and `bilal-scheduler` (amd64 + arm64) and pushes them to GHCR. The `push:main` trigger has no `paths-ignore`, so docs-only pushes to `main` also rebuild images.

Tests are the gate. Before pushing, run `pytest tests/ -v --tb=short`; for image changes also run the local `docker build` targets above.
