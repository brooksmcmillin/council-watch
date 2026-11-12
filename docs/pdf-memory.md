# PDF memory profile and production limits

Task: <https://nexus.brooksmcmillin.com/task/7995>

## Offline evidence (2026-11-12)

The existing local archive fixture `data/pdfs/2025-09-16_agenda_3145.pdf` is a
190-page, 164,906,726-byte public agenda. Its SHA-256 is
`6b57cd32c40475dcf3373ac23b8315cee1de3f678b228cf2475cb8a04beab955`.
It was the largest PDF in the inspected local archive, selected as a realistic
large-package stress case rather than a synthetic blank-page fixture. `pdfinfo`
identified its title as “Agenda - Tuesday, September 16, 2025” and subject as
“Campbell City CA - Regular Meeting.” It is not claimed to represent typical
frequency or to be the production OOM culprit. The PDF is not committed here.

`scripts/profile_pdf_memory.py` runs the actual summarizer and synchronous
Anthropic/httpx multipart implementation against a custom **offline transport**.
Uploads are drained in chunks; no request reaches the network, database,
scheduler or notification channels. Unlike `httpx.MockTransport`, the transport
does not eagerly read the multipart body into memory. Its optional byte-size
rejection forces the existing oversized-input split path; it is **not** a claim
about the provider's actual token limit or this production document's rejection.

Fresh Linux/Python 3.13 processes used Anthropic 0.84.0, httpx 0.28.1 and pypdf
7.0.1. These versions and input pins describe this run, not supported-use policy.
RSS includes interpreter/import overhead. Each process had a 90-second CPU limit;
the default address-space limit was 1,536 MiB.

| Case | Before peak RSS (KiB) | After peak RSS (KiB) | Uploads / deletes, before and after |
| --- | ---: | ---: | ---: |
| Fake provider accepts original PDF | 254,628 | 92,508 | 1 / 1 |
| Fake provider rejects uploads above 32 MiB | 1,515,932 | 238,476 | 13 / 13 |

The forced-split case fell from **1,480.4 MiB to 232.9 MiB (84.3% less)**.
Both successful split runs made 45 fake HTTP requests, including synthesis and
cleanup. A separate **512 MiB address-space-limited** replay failed before the
change (`memory_limit_exceeded`, peak RSS 491,744 KiB, two uploads/deletes) and
completed afterward (238,748 KiB, all 13 uploads/deletes). Address-space limits
are not Kubernetes cgroup limits; this does not certify a 512 MiB production
budget or identify the original OOM's exact document/stage.

The baseline source was commit `553b5d140cd5f8d32d67ef6da52ac350b01ce73a`.
Summarizer source hashes recorded by the comparable runs:

- Before: `79d5384cf8259f9745f2008e84ebf82f2b170985767826f9df591401e172a27d`
- After: `a9f810e84b767860dbf663f660c859de0143db2c214eea339c7866aa9697ccab`

The change streams open file handles, writes split children beside the source
PDF, and discards/collects parent parser/writer graphs before recursive work.
`PdfWriter.close()` is a no-op in the measured pypdf; context exit alone would
not release its cyclic page graph. Original page-count preflight still avoids
uploading more than 600 pages. No pages are intentionally dropped, and a single
page rejected as oversized still fails visibly.

## Reproduce without paid calls

From the repository root with the fixture available, run each case as a separate
process (or use another PDF and record its printed size/hash):

```bash
uv run python scripts/profile_pdf_memory.py data/pdfs/2025-09-16_agenda_3145.pdf
uv run python scripts/profile_pdf_memory.py data/pdfs/2025-09-16_agenda_3145.pdf --reject-above-mib 32
uv run python scripts/profile_pdf_memory.py data/pdfs/2025-09-16_agenda_3145.pdf --reject-above-mib 32 --memory-limit-mib 512
```

To compare the old implementation using the same harness/environment:

```bash
git worktree add --detach .scratchpad/pdf-memory-baseline 553b5d140cd5f8d32d67ef6da52ac350b01ce73a
PYTHONPATH="$PWD/.scratchpad/pdf-memory-baseline/src" uv run python scripts/profile_pdf_memory.py "$PWD/data/pdfs/2025-09-16_agenda_3145.pdf" --reject-above-mib 32
```

The script prints sanitized JSON and exits 2 on a caught `MemoryError`. It uses
an offline-only key/client; it never invokes `run_pipeline`. Tests separately
verify nested page ordering, original-file preservation, temporary-part cleanup
on success/error, uploaded-file deletion, and unrelated-provider-error behavior.

## Production assessment and remaining verification

Task 7972 observed the application's latest termination as `OOMKilled` / exit
137 at 2026-11-12 04:09:06 UTC under a 512 MiB memory limit, with two total
restarts. That proves a process-level failure, not that every previously pending
document failed for this reason. The 03:08 UTC operator snapshot had 176 summaries
and 62 eligible pending documents; it predates the latest termination.

An authorized pod metric at 05:02:33 UTC showed 71,196 KiB idle memory. This is
neither a pipeline peak nor available node capacity. The exposed tools do not
provide node-memory metrics or deployment mutation; exact host discovery for
`k3s_read_node_metrics` loaded no tool, broker search for `node_metrics` found
none, and the requestable catalogue contained no node/capacity/write capability.
No memory-limit change was made or specific larger cap deemed safe.

A temporary increase remains an operator option if capacity permits, but merely
raising the ceiling masks this measured allocation pattern. Before changing a
limit, inspect fresh node `MemAvailable`, placement and competing workload
budgets. Use the reviewed infrastructure workflow; this deployment is a single
replica with a Recreate strategy and RWO storage. The old infrastructure comment
saying 512 MiB covers bursts is historical, not current adequacy evidence.

After deployment, use the new pipeline/document boundary logs to correlate
natural-run progress and RSS/high-water marks with eligible-summary counts and
restart status. No deployment, paid model authentication test, pipeline trigger,
subscriber send or production recovery is established by this offline profile.
Boundary logs can miss transient peaks; `peak_rss_kib` is process-lifetime, not
per-document. Concurrent pipeline/backfill jobs and other PDFs may need more
memory. File-backed parsing is not an absolute memory bound for arbitrary PDFs.

Splits require temporary **disk** capacity beside the source PDF. Normal returns
and exceptions remove temporary parts; an uncatchable kill may leave `summary-*`
directories. Do not delete the original archive or active split directories as
part of an unreviewed cleanup attempt.
