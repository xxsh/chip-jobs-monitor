# chip-jobs-monitor

A daily job watcher for semiconductor career sites that scores newly-posted roles
against **your** resume using an LLM, then emits a grouped-by-company report
(Markdown / JSON / Telegram).

It currently monitors **NVIDIA, AMD, Intel, and Arm** (all filtered to a location,
Shanghai by default) and is easy to extend to more companies. Despite the legacy
internal name `nvidia-...` in a few places, it is multi-company.

> Personal-tool heads-up: this scrapes public career sites and shells out to the
> `codex` CLI for scoring. Career sites change their markup/APIs without notice, so
> an adapter may need fixing from time to time. No warranty — see [LICENSE](LICENSE).

## How it works

```
fetch.py / sources/* ──► snapshots/{date}[_{source}]_{slug}.{json,md,csv}
            │
daily.py ──► diff vs previous snapshot   (only newly-added jobs are scored)
            ├─► scorer.score.score_job() ──► codex exec (LLM, ChatGPT OAuth)
            ├─► db.py  ──► optional MySQL persistence
            └─► reports/latest_{slug}.{json,md,_telegram.md}  (grouped by company)
```

Only *newly-added* jobs are scored each day, so LLM cost is proportional to actual
change. A brand-new company "baselines" on its first run (existing backlog is not
scored) unless you seed it (see [Add a company](#add-a-company)).

## Requirements

- **Python 3.12+** with a virtualenv (`fetch.py`/`daily.py` use Playwright + PyMySQL).
- **[Playwright](https://playwright.dev/python/)** chromium (the NVIDIA scraper). Other
  sources are plain HTTP and need no browser.
- **The [`codex` CLI](https://github.com/openai/codex)**, authenticated via ChatGPT
  login (no API key) — the scorer shells out to `codex exec`. **Each user needs their
  own login.**
- **Node.js 18+** — *optional*, only for MySQL admin scripts (`db.mjs`) and the local
  dashboard. The daily pipeline itself is pure Python.
- **MySQL** — *optional*; without it the run still produces reports and just skips
  persistence.

## Setup

```bash
# 1. Clone, then create the main venv
python3 -m venv .venv
.venv/bin/python -m pip install playwright pymysql
.venv/bin/python -m playwright install chromium

# 2. Create the scorer venv (stdlib-only, no pip deps)
python3 -m venv scorer/.venv

# 3. Install + authenticate the codex CLI (one-time ChatGPT login)
#    See https://github.com/openai/codex — then `codex login`.

# 4. Point the tool at your resume (markdown), then build the profile cache (required)
cp /path/to/your_resume.md ../resume.md      # or set RESUME_PATH (see below)
cd scorer && PYTHONPATH=src .venv/bin/python -m scorer.profile --resume "../../resume.md"
cd ..
```

The **profile cache must exist before the first scoring run** — `daily.py` will stop
with a clear error pointing at the command above if it is missing. Rebuild it with
`--force` whenever your resume changes.

## Configure

Copy `.env.example` to `.env`, edit it, and `source .env` (the app reads plain env
vars; it does not auto-load `.env`). Most-used knobs:

| Variable | Purpose | Default |
|---|---|---|
| `RESUME_PATH` | Resume markdown, relative to this dir | `../resume.md` |
| `MONITOR_SOURCES` | Comma list of companies to run | `nvidia` + all of `sources/` |
| `NVIDIA_LOCATION` | NVIDIA search/location filter | `Shanghai, China` |
| `MYSQL_USER` / `MYSQL_SOCKET_PATH` / `MYSQL_DATABASE` (or `MYSQL_DSN`) | Enable MySQL persistence | unset → skipped |
| `NVIDIA_SKIP_FETCH=1` | Re-render/score from existing snapshots, no scraping | off |
| `NVIDIA_CHROMIUM_PATH` | Override the chromium binary | bundled chromium |

See `.env.example` for the full list (scorer concurrency, timeouts, etc.).

## Run

```bash
# Full daily pipeline: fetch → diff → score → grouped report → (persist)
.venv/bin/python daily.py

# With MySQL persistence
MYSQL_USER=root MYSQL_SOCKET_PATH=/tmp/mysql.sock .venv/bin/python daily.py

# Only some companies
MONITOR_SOURCES=nvidia,amd .venv/bin/python daily.py

# Re-render from existing snapshots without scraping
NVIDIA_SKIP_FETCH=1 .venv/bin/python daily.py
```

Reports are written to `reports/latest_{slug}.{json,md,_telegram.md}` (plus dated
per-source files). `run-daily.sh` is a convenience entry point that resolves the
interpreter, sets MySQL defaults, and strips proxy vars.

## Add a company

Adapters live in `sources/`. Each is a `fetch(max_jobs=None) -> list[normalized_job]`
returning the same schema as `fetch.py`, so scoring/persistence/rendering are untouched.
Many ATS platforms are already covered: Phenom (`amd.py`), Workday (`workday.py`,
parameterized — Intel/NXP/etc.), Radancy (`arm.py`).

1. Add a registry line in `sources/__init__.py` (e.g. another Workday tenant via
   `make_workday_fetcher(host, tenant, site, location_keyword)`).
2. Seed its existing backlog once so you don't miss current openings:
   ```bash
   SEED_SOURCES=<name> .venv/bin/python daily.py
   ```

## Scheduling

- **macOS**: copy `com.example.chip-jobs-monitor.plist` to `~/Library/LaunchAgents/`,
  replace `{{REPO_DIR}}` with this checkout's absolute path, then
  `launchctl load ~/Library/LaunchAgents/com.example.chip-jobs-monitor.plist`.
- **Linux/cron**: schedule `run-daily.sh` (e.g. `0 9 * * * /path/to/run-daily.sh`).

## Tests

```bash
.venv/bin/python -m unittest daily_test test_arm_source   # pipeline + source unit tests
.venv/bin/python verify_fetch_render.py                   # NVIDIA render is byte-stable
node --test                                               # db.mjs suite (needs Node)
.venv/bin/python verify_db.py                             # db.py == db.mjs (needs MySQL)
```

## Layout

```
daily.py            orchestrator (live entry point)
fetch.py            NVIDIA scraper (Playwright)
sources/            HTTP adapters for other companies (amd, arm, intel, ...)
scorer/             LLM scoring + resume-profile extraction (calls codex)
db.py / db.mjs      MySQL persistence (Python live; Node still used by scripts/dashboard)
scripts/, web/      MySQL admin scripts + a local dashboard (Node, optional)
reports/, snapshots/  generated output (gitignored)
```

## License

[MIT](LICENSE)
