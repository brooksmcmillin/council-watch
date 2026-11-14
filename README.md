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
   PDFs are uploaded from open files. Oversized PDFs are split into consecutive
   page ranges on the PDF storage volume (without dropping pages), releasing the
   parent parser before processing each child; text inputs are split when the
   provider rejects their size. Partial summaries
   are combined into one final summary, requiring additional API calls. A single
   PDF page that still exceeds the input limit remains a visible failure.
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
**confirmed opt-in** — opening the email link displays a form; the address receives
summaries only after submitting its confirmation button. GET and POST confirmation
requests share the existing per-client-IP rate limit.

## Local development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # install deps (--all-groups also gets the dev tools)
cp .env.example .env         # then set ANTHROPIC_API_KEY at minimum
uv run alembic upgrade head  # create data/council.db
uv run uvicorn council_meetings.main:app --reload --no-proxy-headers
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

### Subscription proxy trust

Direct/local Docker access needs no proxy configuration: by default rate limits
use the socket peer and ignore visitor headers. For Cloudflare → Traefik → app
(or another trusted forwarding chain), set `TRUSTED_PROXY_NETWORKS` to a JSON
array of the **app-visible proxy peer** IPs/CIDRs, for example
`["192.0.2.10/32", "2001:db8:1::10/128"]` (documentation addresses, not deployment
values). Restart the app after changing it. Invalid networks fail startup.

Only allow peers you control, with ingress restricted to the trusted edge and
`CF-Connecting-IP` overwritten there; a trusted peer must never relay a
client-chosen value from a bypass path. Do not trust all private/pod networks or
`0.0.0.0/0`. Observe the actual peer addresses in your deployment before setting
the list. The production ingress's Cloudflare-only middleware is an upstream
prerequisite, not an application peer allowlist. Until configured, proxied
visitors share the proxy's bucket; configure trust before rolling out behind a
proxy to retain separate normalized visitor buckets.

Keep Uvicorn's `--no-proxy-headers` in custom launch commands, as in the image:
the application must see the real socket peer, not a value rewritten from
`X-Forwarded-For` (including via `FORWARDED_ALLOW_IPS`). Only `CF-Connecting-IP`
from configured peers is used; missing/malformed values fall back to the peer.
These are deployment prerequisites, not a claim of live deployment verification.

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
| `TRUSTED_PROXY_NETWORKS` | `[]` | JSON array of trusted app-visible proxy CIDRs; see proxy trust above |
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

### Memory diagnostics

Application INFO logs include pipeline stages, document IDs, current RSS and
process-lifetime peak RSS (`rss_kib` / `peak_rss_kib`, Linux procfs). Memory records
contain no document text, credentials or subscriber data. A final `.start` without
its matching `.complete` helps locate an interrupted stage; `/health` alone does
not prove pipeline progress. These boundary samples can miss transient peaks.

PDF splitting needs writable disk space beside the source PDF; temporary parts
are removed on normal success or exception. An uncatchable process kill may leave
`summary-*` directories behind. Parser memory is not hard-capped by file-backed
I/O. See [the offline profile and production limits](docs/pdf-memory.md) before
changing resource limits or claiming production recovery.

### Empty summary diagnostics

If a provider response has no usable text, the summarizer logs a WARNING with
`stop_reason`, SDK content-block class names, and input/output token counts before
raising the existing error. Unknown stop reasons become `unknown`; absent or
non-integer token counts become `None`. Refusals also report an allowlisted
`refusal_category` from the provider's `stop_details`: `cyber`, `bio`,
`frontier_llm`, `reasoning_extraction`, or `general_harms`. Missing categories
become `unavailable`, explicit null becomes `unspecified`, and unrecognized
values become `unknown`; other stop reasons use `not_applicable`. A provider
`request_id` is logged only when it matches `req_` plus ASCII letters, digits,
underscores or hyphens, with at most 128 characters total; otherwise it is
`unavailable`. It can be supplied to provider support for investigation.

Each document-start log includes its public `/meeting/<id>` path and selected
`pdf` or `html` source kind. Document IDs and meeting IDs are different: use that
mapping to find the original agenda/minutes link on the public meeting page.
Source URLs, local filenames, response bodies, refusal explanations, thinking,
tool inputs, stop sequences and arbitrary extra fields are not logged.
Successful responses do not emit the empty-response diagnostic. These fields
add no requests or retries and do not change summary eligibility or persistence.

Inspect the warning alongside the following document failure in an already
scheduled run. HTTP 200 and scheduler success do not prove a summary was saved.
For example, `max_tokens` with only thinking blocks suggests output exhaustion;
it does not by itself prove that increasing the limit will fix the document.
Do not trigger the paid summarize/notify pipeline just to obtain diagnostics.

### Repair rollout notes

Apply the additive `text_content` migration (`uv run alembic upgrade head`) before
running the updated app. No existing archive rows are deleted. PDF access from
the diagnostic client returned HTTP 403; HTML agendas/minutes were accessible.
Production access and actual model credentials still require separate verification.
A first scrape/backfill can discover many new documents and the normal pipeline
will summarize and notify them if configured. Deployment, paid model execution,
and notification delivery need separate authorization; do not use a pipeline run
as a harmless verification command.
