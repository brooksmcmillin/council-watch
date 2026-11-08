# council-watch

Monitors Campbell's [eSCRIBE portal](https://pub-campbell.escribemeetings.com/)
and its legacy [CivicPlus](https://www.civicplus.com/) Agenda Center for new
city-council agendas and minutes, summarizes documents with Claude, and publishes
the results to a small public website plus email and Bluesky notifications.

The deployed instance tracks the City of Campbell, CA at
**[council.brooksmcmillin.com](https://council.brooksmcmillin.com)**.

> Summaries are AI-generated and may contain errors or omissions — the original
> documents remain the authoritative source. This project is not affiliated with any
> city.

## What it does

A single FastAPI process serves the site and runs an in-process
[APScheduler](https://apscheduler.readthedocs.io/) pipeline
(`src/council_meetings/scheduler.py`) every `SCRAPE_INTERVAL_MINUTES`:

1. **Scrape** (`scraper.py`, `escribe.py`) — reads all Council meeting types from
   eSCRIBE's public calendar for the current year (past and upcoming), alongside
   the legacy `AgendaCenter` archive. Full agenda-package PDFs are preferred over
   cover sheets. When PDF retrieval fails, accessible agenda/minutes HTML is
   extracted into plain text; challenge pages are not treated as documents.
   A saved PDF from the same source is retained on a transient refresh failure;
   its summary is not replaced just because HTML is available. At source cutover,
   HTML may replace the active document, while old PDF bytes remain on disk.
   PDF hashes and HTML text changes invalidate old summaries and notification
   flags. A `HEAD` size pre-check avoids unchanged PDF downloads.
2. **Summarize** (`summarizer.py`) — sends each un-summarized PDF or extracted
   HTML text to the Anthropic API (`SUMMARIZATION_MODEL`, default `claude-sonnet-5`)
   and stores a plain-language summary. HTML-based summaries explicitly exclude
   linked attachments/staff reports; they are not full-package PDF summaries.
3. **Notify** (`notifier.py`) — emails confirmed subscribers over SMTP and posts
   to Bluesky. Each channel is skipped unless it's configured.

A second, slower job re-scrapes the previous `BACKFILL_YEARS` years through
both eSCRIBE calendar-year ranges and CivicPlus's `UpdateCategoryList` endpoint,
so minutes posted long after a meeting are still picked up. Calendar-year results
are not limited to the paginated, per-type PastMeetings listing. Failed years,
malformed records and unreadable documents are recorded in `scrape_log.errors`.

The verified overlapping October 7, 2025 regular meeting keeps CivicPlus id 3159
and its existing public row; new portal meetings use `escribe-<UUID>` in the
existing `civicplus_id` column. Legacy records and public meeting IDs are retained.

### Data model

SQLite (`data/council.db`, schema managed by Alembic) with PDFs on disk under
`data/pdfs/`:

| Table | Purpose |
| --- | --- |
| `meetings` | One row per meeting, with legacy numeric or namespaced eSCRIBE source id |
| `documents` | PDF paths/hashes or extracted HTML text, summaries, and notify flags |
| `subscribers` | Email subscribers with confirmation/unsubscribe tokens |
| `scrape_log` | Per-run counts and errors |

### Routes

`/` (recent meetings), `/meeting/{id}`, `/about`, `/health`, and the subscription
flow: `/subscribe`, `/confirm/{token}`, `/unsubscribe/{token}`. Email signup is
**confirmed opt-in** — an address receives nothing until its confirmation link is
clicked — and the public subscription routes are rate-limited per client IP.

## Local development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # install deps (--all-groups also gets the dev tools)
cp .env.example .env         # then set ANTHROPIC_API_KEY at minimum
uv run alembic upgrade head  # create data/council.db
uv run uvicorn council_meetings.main:app --reload
```

The site is then at <http://localhost:8000>. Starting the app also starts the
scheduler, so the first pipeline run happens one interval later. To run the
scraper on its own without waiting:

```bash
uv run python -m council_meetings.scraper
```

Or with Docker — reads `.env`, mounts `./data`, serves on `:8000`:

```bash
docker compose up --build
```

### Checks

These mirror CI (`.github/workflows/ci.yml`); `pre-commit install` wires the
first three up as commit hooks.

```bash
uv run ruff check src/ && uv run ruff format --check src/
uv run pyright src/
uv run pytest -q
```

## Configuration

Configuration is entirely environment variables, read via pydantic-settings from
`.env` — see `.env.example` for the full annotated list. `ANTHROPIC_API_KEY` is
the only value the pipeline can't run without; every notification channel is
optional and is skipped when left blank.

| Variable | Default | Notes |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | — | Required for summarization |
| `SUMMARIZATION_MODEL` | `claude-sonnet-5` | Any Claude model id |
| `DATABASE_URL` | `sqlite:///data/council.db` | |
| `PDF_STORAGE_DIR` | `data/pdfs` | |
| `APP_BASE_URL` | `http://localhost:8000` | Used to build links in emails |
| `SCRAPE_INTERVAL_MINUTES` | `60` | Pipeline cadence |
| `BACKFILL_YEARS` / `BACKFILL_INTERVAL_HOURS` | `2` / `168` | `0` years disables the backfill job |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM` | blank | Email turns on once `SMTP_HOST` and `EMAIL_FROM` are both set |
| `EMAIL_TO` | blank | Optional comma-separated static/admin recipients (always notified, no unsubscribe link) |
| `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD` | blank | Both required to enable Bluesky posts |

### Pointing it at a different city

City display values and CivicPlus archive settings live in `CityConfig`
(`config.py`) and use `CITY_`-prefixed environment variables. Campbell's current
portal is enabled automatically only for its default legacy base/category.
`CITY_ESCRIBE_BASE_URL=""` explicitly disables it; another portal URL can be set
explicitly, but only Campbell's published format/overlap has been verified.

```bash
CITY_NAME=Riverton                              # short name (email subjects, User-Agent)
CITY_LOCATION=Riverton, Utah                    # how the city reads in summarizer prompts
CITY_DISPLAY_NAME=Riverton City Council         # display name in notifications
CITY_BASE_URL=https://www.example.gov           # CivicPlus site root, no trailing slash
CITY_AGENDA_PATH=City-Council-7                 # AgendaCenter category path segment
CITY_CATEGORY_ID=7                              # CivicPlus catID for the backfill endpoint
CITY_ESCRIBE_BASE_URL=                          # disable Campbell eSCRIBE for this city
CITY_USER_AGENT=RivertonCouncilMonitor/1.0 (+https://example.com)
```

To find `CITY_AGENDA_PATH` and `CITY_CATEGORY_ID`, open the city's
`/AgendaCenter` page and read the council category's link: it has the form
`/AgendaCenter/<AGENDA_PATH>`, and the trailing number in that segment is the
`catID`. Use a fresh database when switching cities — meeting ids are only unique
within a single CivicPlus site.

## Deployment

`.github/workflows/docker-publish.yml` publishes
`ghcr.io/brooksmcmillin/council-watch:latest` (plus a `:${sha}` tag) on every
push to `main`. The image runs as uid 1000 against a read-only root filesystem
and applies `alembic upgrade head` before starting uvicorn.

It is deployed to k3s from a private infra repo
(`terraform/workloads/council-watch.tf`): a dedicated `council-watch` namespace,
a PVC mounted at `/app/data` for the SQLite file and PDFs, and configuration
synced from Bitwarden Secrets Manager via external-secrets into a
`council-watch-env` Secret.

Design notes and background live on the internal wiki page
[Campbell Council Meeting Monitor — Design](https://nexus.brooksmcmillin.com/wiki/council-meeting-monitor-design)
(not publicly accessible).

### Repair rollout notes

Apply the additive `text_content` migration (`uv run alembic upgrade head`) before
running the updated app. No existing archive rows are deleted. PDF access from
the diagnostic client returned HTTP 403; HTML agendas/minutes were accessible.
Production access and actual model credentials still require separate verification.
A first scrape/backfill can discover many new documents and the normal pipeline
will summarize and notify them if configured. Deployment, paid model execution,
and notification delivery need separate authorization; do not use a pipeline run
as a harmless verification command.
