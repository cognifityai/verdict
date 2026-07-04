# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities **privately**. Do not open a public
GitHub issue for a suspected vulnerability.

Email: **security@cognifity.ai**

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
- **API keys are read from the environment** and are never committed by the
  SDK. Keep your keys out of source control.
- **Local SQLite databases and generated reports may contain sensitive trace
  data.** Treat those files, and any exposed dashboard, as sensitive and keep
  them out of source control and off untrusted networks.
- The **dashboard** (`ui/`) is a local, single-user tool. Binding it to a
  non-localhost host without the `VERDICT_USER`/`VERDICT_PASS` basic-auth
  environment variables is unsupported (the server warns when you do).
