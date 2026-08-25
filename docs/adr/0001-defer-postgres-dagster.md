# ADR-0001: Defer PostgreSQL + Dagster until deal volume triggers

**Status:** Accepted (re-affirmed Aug 25, 2026 during remediation tiers A–D)
**Deciders:** HERMES-REO maintainers
**Original context:** PRIOR_FINDINGS.md "discard JSON storage" recommendation; SYSTEM_REPORT §11 gap table

## Context

Two prior reviews recommended replacing the JSON-file + subprocess architecture with PostgreSQL (CPR persistence) and Dagster (pipeline orchestration). The dataset is 338 records (~420 KB); the entire working set fits comfortably in memory, and every artifact is already written through atomic `src.io_utils` helpers with crash-safety semantics that previously only a database would provide.

## Decision

**Keep flat-file storage and script orchestration for now. Do not adopt PostgreSQL or Dagster until any of the following triggers fire:**

| Trigger | Threshold | Rationale |
|---|---|---|
| Dataset scale | > 10,000 live CPR records | Point-query latency + concurrent-writer contention become real above this |
| Concurrent operators | ≥ 3 people/processes writing parcels simultaneously | File locking is per-artifact; row-level concurrency needs a DB |
| Audit/compliance demand | External requirement for SQL audit trails / row-level history | The hash-chained ledger satisfies internal tamper-evidence but not third-party DB-audit mandates |
| Multi-county expansion | ≥ 6 counties or non-TX jurisdictions | Orchestration DAGs pay off once pipeline topologies branch |

## Consequences

**Positive:** zero infrastructure to run (`make verify` is the whole system); trivially portable; every artifact human-readable; no ORM drift between code and schema.

**Negative / accepted risks:** no cross-process concurrent writes today (single-operator assumption, enforced by convention); no partial queries — analysis loads full datasets; historical provenance lives inside CPR ledgers rather than a queryable event store.

**Migration sketch (when triggered):**
1. Introduce `src/store.py` interface (`save_cpr`, `load_cpr`, `query`) with a JSON backend first — all call sites move behind it while behavior stays identical.
2. Add a Postgres backend of the same interface (CPR rows keyed by parcel_id; ledger entries as an append-only child table; GeoJSON becomes a view).
3. Move scheduling into Dagster only after step 2, one asset per pipeline stage, reusing existing module self-checks as asset checks.
4. `data/*.json` remain export/import format forever — dashboards and HITL keep working unchanged.
