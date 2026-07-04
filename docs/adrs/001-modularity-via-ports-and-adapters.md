# ADR-001 — Modularity via Ports & Adapters

**Status:** Accepted
**Date:** 2026-05-12

## Context

Verdict needs to support multiple model providers, storage backends, and
evaluation paths without tying the core domain logic to one vendor SDK or
deployment shape. Modularity is a core design constraint.

## Decision

We adopt a hexagonal (ports-and-adapters) architecture. The core domain (drift detection, scoring, clustering, statistical comparison) has zero direct dependencies on any external system. Every external system sits behind an interface defined in the core; concrete implementations are adapters loaded via a small factory at boot time.

We commit to **at least two concrete adapters per port from day one**, even if only one ships in production. The second is typically an in-memory implementation used in tests. This forces the interface to actually be vendor-neutral; a port with one adapter is not an abstraction.

## Ports (initial)

| Port | First adapter | Second adapter |
|---|---|---|
| `LLMProvider` | Anthropic | OpenAI |
| `Storage` | SQLite | in-memory |
| `EventBus` | in-process queue | (Pub/Sub later) |
| `VectorStore` | sklearn in-memory | (pgvector later) |
| `EvalJudge` | LLM judge via LLMProvider | rule-based stub |
| `BlobStore` | local filesystem | (GCS later) |
| `MetricsSink` | stdout | (Prometheus later) |

## Consequences

- ~10% more code per feature (port definition + ≥2 adapters + factory wiring).
- New providers/clouds are additive — implement one interface, register, done.
- We never import vendor SDKs in the core domain. Lint rules and code review enforce this.
- Domain vocabulary stays vendor-neutral: `chat_completion()` not `call_anthropic()`, `llm_requests` not `openai_requests`.
- Tests use in-memory adapters; integration tests use real ones behind feature flags.

## Anti-patterns explicitly forbidden

- `from anthropic import Anthropic` outside `packages/verdict/src/verdict/instrumentors/`
- Hardcoded provider names in switch statements (use the factory)
- Mutable singletons holding adapter instances (use dependency injection)
- Test code that mocks an adapter rather than using the in-memory adapter
