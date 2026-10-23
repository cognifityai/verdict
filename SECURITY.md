# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities **privately**. Do not open a public
GitHub issue for a suspected vulnerability.

Email: **info@cognifity.ai** with "Security vulnerability" in the subject line.

Include, if you can: a description of the issue, the affected component
(e.g. `packages/verdict` instrumentation, storage adapter, `ui/` dashboard),
steps to reproduce, and any potential impact. A minimal proof of concept helps
us triage quickly.

We aim to acknowledge a report within **3 business days** and will keep you
updated as we investigate. Please give us reasonable time to release a fix
before any public disclosure.

## Supported versions

This is an early public release. Security fixes are applied to the latest tagged
release and the default branch only. There is no long-term-support branch yet.

## Scope notes

A few things that are by design rather than vulnerabilities:

- **Content redaction is best-effort** (regex + Luhn), not a compliance
  guarantee. Do not rely on it as your only control for regulated data.
- **API keys are read from the process environment.** Inject them at process
  startup from a managed secret store or OS credential manager. Do not keep a
  plaintext `.env*`, `*.env*`, `*.envrc*`, `.direnv`, or provider-key file
  anywhere under a Verdict working tree: ignored files still leak into
  snapshots, support bundles, and misconfigured build contexts.
- **Local SQLite databases and generated reports may contain sensitive trace
  data.** Treat those files, and any exposed dashboard, as sensitive and keep
  them out of source control and off untrusted networks.
- **Container images contain no captured database by design.** Supply
  `VERDICT_STORAGE` at runtime from a customer-managed store; do not put a
  SQLite file, trace export, or provider key in the build context. The image's
  default SQLite schema is empty and its filesystem is not a durable store.
- **Dashboard navigation identifiers remain exact.** Run, turn, event, and
  linked trace IDs in dashboard API responses and URLs may contain source
  supplied text. Treat API responses, URLs, browser history, and access logs
  as sensitive even though display content is redacted.
- The **dashboard** (`ui/`) serves one configured tenant and is intended for
  localhost or a trusted network. Binding it to a
  non-localhost host without the `VERDICT_USER`/`VERDICT_PASS` basic-auth
  environment variables and an explicit `VERDICT_ALLOWED_HOSTS` allowlist is
  refused by the CLI. With credentials configured, Basic Auth covers every
  dashboard API route except the public `/api/health` endpoint; a setup
  capability alone does not bypass it, including when the dashboard is mounted
  under a host application path. Terminate TLS at a trusted ingress.
- A dashboard instance is configured for one tenant. Monitor policies can be
  activated only in that instance's tenant scope. Sharing a database across
  instances does not turn the dashboard into a hosted multi-tenant service;
  each instance still needs its own access boundary.
