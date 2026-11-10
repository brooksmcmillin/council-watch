# PDF request origins

The scraper's production client checks every request **before transport dispatch**,
including HEAD pre-checks, streaming GETs and every redirect hop. Only HTTPS URLs
without userinfo and with an exact configured scheme/host/port are allowed:

- `CITY_BASE_URL`: Campbell defaults to `https://www.campbellca.gov`.
- The enabled eSCRIBE source: Campbell defaults to
  `https://pub-campbell.escribemeetings.com`. `CITY_ESCRIBE_BASE_URL` overrides it;
  an empty value disables it.

Relative PDF links and relative redirect locations resolve normally. Absolute
links and redirects between the two configured HTTPS origins are supported.
An upstream href or Location cannot add an origin. HTTP-only source settings are
not supported by this boundary. Settings are operator-controlled trust inputs;
this is not a DNS-rebinding defense or a production network-policy audit.

## Link provenance and regression fixtures

On 2026-11-10 UTC, the public
[Campbell CivicPlus listing](https://www.campbellca.gov/AgendaCenter/City-Council-10)
published these September 16, 2025 links (meeting 3145):

- `/AgendaCenter/ViewFile/Agenda/_09162025-3145`
- `/AgendaCenter/ViewFile/Minutes/_09162025-3145`

`tests/fixtures/civicplus-pdf-links.html` retains the minimal parser-relevant
excerpt. `tests/test_pdf_origins.py` parses it and exercises both PDF helpers
with relative and absolute links. It also models a same-origin relative
redirect and a multi-hop city-to-enabled-eSCRIBE redirect using the existing
`FileStream.ashx?DocumentId=2377` / `2373` portal fixture paths.

The redirects and PDF bytes in these tests are **synthetic**, not observed live
redirect chains or proof of current download availability. Public listing HTML
was read for link provenance; all download/security verification uses
`httpx.MockTransport`, including hostile address strings. No requests are made
to metadata services or internal hosts.

No additional Campbell PDF CDN origin was established by the listing or existing
repository evidence. Do not infer a wildcard CivicPlus/CDN trust boundary from
asset URLs, or label a synthetic redirect as a live observation. If the city
changes its download origin, verify the published destination and update the
bounded source configuration/fixtures deliberately; arbitrary redirect hosts
remain disallowed.
