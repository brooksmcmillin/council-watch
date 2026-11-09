# Dependency audit reconciliation — 2026-11-09

Investigation: [task 7920](https://nexus.brooksmcmillin.com/task/7920).
Baseline: `bf6a8958290b198e824c53b10530fb3b2e7a36fd`.

## certifi: historical failure was corrected advisory data

[CI run 40628680984](https://github.com/brooksmcmillin/council-watch/actions/runs/40628680984)
on October 28 reported `certifi 2026.10.18 PYSEC-2026-4320 2026.10.19`.
The same locked certifi version now audits cleanly; this is not evidence of a
package upgrade or a reason to suppress the advisory.

The advisory database's October 30
[range correction](https://github.com/pypa/advisory-database/commit/3d2b0544e88e638ddb3cfa06f71d9a14e82c894e)
changed the fixed version to **2026.10.18** and removed it from affected versions.
That release removed the affected emSign roots. Version 2026.10.19 restored
unaffected roots. The current [OSV record](https://api.osv.dev/v1/vulns/PYSEC-2026-4320)
identifies 2026.10.18 as fixed, although its prose still recommends 2026.10.19.
The corrected machine-readable range explains the historical/current scan
discrepancy; no certifi change is needed to resolve this advisory.

## cryptography: remove the obsolete exception

[GHSA-537c-gmf6-5ccf](https://github.com/advisories/GHSA-537c-gmf6-5ccf)
affects PyPI cryptography wheels before **48.0.1**, which bundle vulnerable
OpenSSL. Source builds depend on their separately supplied OpenSSL.

The lock already contains **cryptography 50.0.0** and **atproto 0.0.72**.
Installed atproto 0.0.72 metadata allows `cryptography >=41.0.7,<51`, so the old
CI comment claiming `cryptography<47` is no longer true. The locked production
export includes 50.0.0, and a hash-checked audit **without any ignored advisories**
reports `No known vulnerabilities found`. Remove the exception rather than
force an incompatible dependency override. No package constraints or lockfile
changes are necessary.

## Reproduction and evidence limits

```bash
uv export --locked --no-dev --no-header --no-emit-project > /tmp/council-requirements.txt
uvx pip-audit --require-hashes --no-deps -r /tmp/council-requirements.txt
```

The baseline's [main CI run 42522224153](https://github.com/brooksmcmillin/council-watch/actions/runs/42522224153)
has a successful Dependency Audit job at the exact baseline SHA. That historical
job still used the exception; the local unsuppressed audit establishes that the
exception is unnecessary. The removal must also pass its own exact-head CI.

On November 9, the GitHub API reported automated security fixes `enabled: true`,
`paused: false`, vulnerability alerts enabled (HTTP 204), and zero open Dependabot
alerts. No security setting was changed and no auto-merge was requested. These
are point-in-time observations, not guarantees about future advisory data.
A green audit or published image does not certify deployment or live application
behavior.
