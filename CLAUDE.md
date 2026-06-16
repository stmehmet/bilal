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

## CI/CD & Local Dev

Local-first model (fleet-wide, see `../../RELEASE-WORKFLOW.md`):

- **Pre-commit** (lefthook): fast format/lint/secret scan on staged files.
- **Pre-push** (lefthook): runs `just check` — the full gate. Push only when it's green.
- **Remote CI is a thin backstop**, not the primary gate. You push already-verified code.

`just check` for this repo runs (Python stack — no typecheck/lint configured here):

- test: `pytest tests/ -v --tb=short`
- build: `docker build --target web .` + `docker build --target scheduler .`

> `justfile` + `lefthook.yml` are **not present yet** — rollout pending Mac Studio (2026-06-17). See `../../RELEASE-WORKFLOW.md`.

CI gating (already applied to `.github/workflows/build-push.yml` — do not re-edit):

- **test job**: `pull_request` (drafts skipped) + `merge_group`, with `paths-ignore` for docs/images and `concurrency` cancel-in-progress on PRs.
- **build/push job**: only on `push` to `main` + `v*` tags and `workflow_dispatch` — never on `merge_group`. Pushes `bilal-web` and `bilal-scheduler` (amd64 + arm64) to GHCR.
- Watchtower on each Pi pulls new images from GHCR within the hour.

Fleet playbook: `../../RELEASE-WORKFLOW.md` · CI audit: `../../CI-CD-AUDIT.md`
