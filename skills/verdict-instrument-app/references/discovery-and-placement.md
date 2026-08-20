# Repository discovery and placement

The goal is to find live LLM call paths and their lifecycle owners, not merely text
that resembles a provider call.

## Start with repository evidence

1. Read repository and directory-level agent instructions.
2. Identify languages, package managers, entry points, deployment manifests,
   processes, storage conventions, secret mechanisms, observability code, test
   commands, and existing schedulers.
3. Resolve `<skill-root>` to the directory containing `SKILL.md`, then run:

   ```bash
   python3 <skill-root>/scripts/scan_repository.py \
     /absolute/repository --format json
   ```

4. Review every match in its surrounding source and call graph. The report exposes
   file paths and line numbers but intentionally omits source snippets.
   Python comments and string literals are masked for provider/lifecycle rules.
   If `python_parse_fallback_files` is nonzero, filter findings for
   `rule_id: python-parse-fallback` and manually inspect every named path.
5. Search with the repository's own tools (`rg` when available) for wrapper functions,
   dependency aliases, factory-created clients, background queues, and dynamic imports
   that a pattern scanner may miss.

## Classify live provider paths

Record for each call site:

- language and provider SDK distribution/version;
- exact invoked method;
- sync, async, iterator, or stream behavior;
- wrapper and retry layers;
- process type and all entry points that reach it;
- whether requests can be duplicated or replayed;
- provider error/cancellation behavior; and
- current tests that execute it.

Use the release compatibility map to mark supported, constrained, unverified-adapter,
or unsupported. A Python wrapper is not automatically supported: prove that the
released instrumentor intercepts the concrete SDK method underneath it.

## Choose an initialization owner

For every supported process, locate the earliest stable lifecycle point after config
is available and before the first provider call. Typical owners are:

- application factory or startup hook for a web process;
- worker bootstrap for a queue consumer;
- command entry point for a CLI or batch process; and
- handler-module initialization for a warm serverless process, after confirming the
  platform lifecycle.

Reject these placements:

- inside an HTTP/request handler;
- inside the LLM wrapper for every call;
- after importing and invoking startup code that already calls the provider;
- in only the web process when workers also make LLM calls; or
- in a parent process when the provider calls occur only after a fork and have not
  been verified.

Map normal exit, signal/cancellation, graceful shutdown, and forced termination.
Buffered writes are not acceptable until shutdown is wired and tested for every
relevant lifecycle.

## Resolve storage ownership

For SQLite, resolve the literal absolute file path after environment expansion. Check:

- parent directory creation and write permissions;
- container/host persistence and volume mounts;
- process working directories;
- whether multiple writers are expected;
- which OS identity owns the file; and
- whether the dashboard reads that exact same file.

For shared or multi-instance capture, prefer Postgres and identify database, schema,
credentials, migrations, connection limits, and tenant boundary. The bundled
dashboard can read the same Verdict schema in a read-only transaction; mount it
behind the host application's authorization and verify that it cannot migrate or
mutate the store.

## Design stable context without sensitive identifiers

Use low-cardinality operational fields such as deployment version, feature flag,
route family, or pseudonymous tenant key only when the user approves them. Do not
store raw email, name, account token, session secret, or unrestricted metadata.
Define cardinality and retention for each added field.

## Report discovery before editing

Provide a compact table with process, entry point, provider call, support class,
initialization owner, storage owner, relevant lifecycle risks, and test path. List
uncertain dynamic paths explicitly. The absence of scanner findings is not proof that
the repository makes no LLM calls.
