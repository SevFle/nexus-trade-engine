# Nexus Trade Engine — Development Strategy

**Authoritative.** The engine follows this execution plan strictly. Phases gate merges; lanes within a phase run in parallel. Cross-phase delivery is permitted under the Exception Protocol (§Phase Gate Exceptions).

> **Drift advisory (ACTIVE - Critical):** Process drift has escalated. The declared development model is out of sync with repository reality. WIP hygiene measures failed to prevent emergency commits, and significant Phase 4+ (Paper/Live Trading) functionality has been implemented while the official roadmap states Phase 2 (Infra & Auth) is current. This revision formally restructures the execution plan to a **Gated-Parallel Model**, retroactively maps all orphaned work, and mandates strict blocking mechanisms for CI/CD and Architecture Decision Records.
>
> **Process amendment (retroactive-tracking rule):** Effective immediately, any merged feature without a pre-existing `[N.L.k]` tag must receive a retroactive mapping entry in §Shipped within one sprint of merge. Unmapped merges block the next phase gate until catalogued. See §Process Drift Correction below.

---

## Execution Method

Every issue is tagged `[N.L.k]`:
- **N** = Phase (1-7). Sequential gate logic: Phase N+1 gates open only after Phase N gates close (unless parallel lanes are formally activated).
- **L** = Lane (A, B, C...). Parallel within a phase. Pick any lane to staff.
- **k** = Position within lane. Sequential. Lower numbers first.

Cross-cutting concerns use `[XC.k]` and track against their own gate (ADR approval), not a phase gate.

**Issue counts are maintained as a live metric.** Historical baseline: ~80 open issues estimated 2025-01, ~65 active mapped. Post-streamline (commit 02b4465) and coverage-gate closure, current active mapped issue count is **~55** (pending deduplication pass — see §Issue Backlog Health). Exact tally requires deduplication pass; counts will be updated at each phase gate closure.

### Delivery Model: Gated Sequential with Acknowledged Parallelism

The declared model is now **gated-parallel execution**. In practice, parallel work categories are now formally recognised and integrated into the roadmap:

| Category | Governance | Examples |
|----------|-----------|----------|
| **Exception-gated** cross-phase delivery | Logged in §Phase Gate Exceptions; requires own test suite + ADR | EX-001 (Auth), EX-002 (Admin API) |
| **Retroactively-mapped** untracked delivery | Post-hoc mapping in §Shipped; triggers §Process Drift Correction review | Execution backend factory, slippage models, zero-quantity rejection, sandbox audit, legal-qa, sandbox CPU-timer |
| **Active Parallel Lanes** | Phase 4 work explicitly authorised while Phase 2/3 infra finalises | `[4.A.1]`, `[4.B.1]`, `[4.C.1]`, `[4.D.1]` |

**Rule amendment:** When the cumulative count of retroactively-mapped deliveries exceeds **3 per sprint**, the strategy document must be revised within one sprint to either (a) formally restructure the phase plan or (b) escalate to a gated-parallel model with per-lane entry criteria. Current count: **8 retroactively-mapped deliveries** — threshold exceeded; this revision constitutes the required restructuring.

### Development Stability Protocol

**Observed issue:** Frequent emergency commits (`wip: auto-save before ERR`) in recent commit history. Recent commits (3177f98, deb4722) continue this pattern. **8 of last 20 commits = 40% WIP ratio**. The previously declared corrective measures were **ineffective**.

**Corrective measures (Escalation - effective this revision):**

1. **CI Pipeline Hard Block (NEW):** All commits prefixed with `wip:`, `WIP:`, or containing `auto-save before ERR` will be strictly rejected by the `ci.yml` pipeline on the `main` branch.
2. **Pre-commit Hook Enforcement:** Local pre-commit hooks must validate conventional commit formatting. Bypassing hooks via `--no-verify` is prohibited for merges to `main`.
3. **Root-cause review:** If a developer logs >2 emergency WIP commits in a sprint, a brief root-cause analysis is required (environment instability, tooling gaps, or process issues).
4. **Stability metric:** WIP commit ratio tracked at each sprint audit. **Target: <5% of total commits. Current: 40% — CRITICAL.**

---

## Cross-Cutting Concerns `[XC.k]`

Infrastructure and tooling that spans all phases. Each cross-cutting concern requires an ADR for gate approval.

| Tag | Concern | Status | ADR | Workflows / Tooling | Phase Relevance |
|-----|---------|--------|-----|---------------------|-----------------|
| `[XC.1]` | **CI/CD Pipeline** — continuous integration, image publishing, release automation | ✓ Operational | ADR-0003 *(missing)* | `ci.yml`, `publish-images.yml`, `release-please.yml` | All phases |
| `[XC.2]` | **Security Scanning** — secret detection, vulnerability scanning | ✓ Operational | ADR-0004 *(missing)* | `security.yml`, `.gitleaks.toml` | All phases |
| `[XC.3]` | **Load Testing** — performance regression detection | ✓ Operational | ADR-0005 *(missing)* | `load-test.yml` | Phase 5 (Live Trading), Phase 7 (Scale) |
| `[XC.4]` | **Property-Based Testing** — generative coverage expansion via Hypothesis | ✓ Operational | — *(embedded in test policy)* | `.hypothesis/` persistent seed constants | All phases |
| `[XC.5]` | **Self-Hosted Runners** — dedicated `nexus` runner for all CI workflows | ✓ Operational | — *(infra config)* | Runner: `nexus` | All phases |
| `[XC.6]` | **AI-Assisted Design Skill** — local `.claude/skills/nothing-design` integration | ✓ Operational | — *(governed as standard tooling)* | `.claude/skills/nothing-design` | All phases |

**ADR Backlog (CRITICAL BLOCKER):** `[XC.1]`, `[XC.2]`, and `[XC.3]` are operational but lack formal Architecture Decision Records. **The `docs/adr/` directory does not exist in the repository.** Creation of the directory and drafting/approval of ADR-0003, ADR-0004, and ADR-0005 is immediately required. **Blocking:** Phase 3 → Phase 4 gate formalisation.

---

## Phase Gate Exceptions

Documented violations of the sequential-phase rule. Every exception must record: what shipped early, why, residual risk, and remediation.

| Exception | What Shipped | Gate Bypassed | Justification | Residual Risk | Remediation |
|-----------|-------------|---------------|---------------|---------------|-------------|
| `EX-001` | `[2.A.1]` Auth + RBAC (SEV-233) | `[1.2]` 80%+ coverage (SEV-264) | Auth ADR-0002 was fully spec'd; implementation had its own test suite; security review needed early for Phase 3 broker adapter design | Core engine paths unmonitored by coverage gate at time of merge | ✓ **Closed** — coverage gate [1.2] now passed; SEV-264 closed |
| `EX-002` | Admin API (commits ec8754b, 5f46cb9) | `[1.2]` coverage gate + Phase 2 Lane D not formally established | Required for operational management of live-trading preparation; auth (EX-001) already shipped | Admin endpoints operated without formal coverage gate | ✓ **Closed** — coverage gate [1.2] now passed; Lane D formally mapped as `[2.D.1]` |
| `EX-003` | Execution Backend Factory, Slippage Models, CPU-Timer, Zero-Quantity Rejection | Phase 2 Infra gate; mapped to Phase 4 functionality | Core execution math and backend abstraction required immediate validation against broker sandbox environments. | Phase 4 execution logic exists without Phase 3 Broker Adapter formal closure. | **Active** — Acknowledged in gated-parallel model. Requires Phase 3 gate closure ASAP. |

**Rule amendment:** A Lane may ship ahead of its phase gate only if (1) it has its own independent test suite, (2) an ADR is approved, and (3) the exception is logged here. The gate still blocks all remaining lanes in the same and subsequent phases until the gate closes.

---

## Process Drift Correction

**Problem:** Eight features (Admin API, execution backend factory, slippage models, zero-quantity order rejection, sandbox audit logging, legal-qa infrastructure, sandbox CPU-timer hardening, execution backend refactoring) were implemented and merged without phase/lane tracking issues or `[N.L.k]` commit tags. While now retroactively documented, the underlying process allowed significant untracked work to accumulate.

**Correction (effective this revision):**

1. **Retroactive-mapping rule:** Any merged PR/commit introducing user-facing or architectural behaviour must be mapped to a `[N.L.k]` tag within one sprint. Unmapped merges block the next phase gate.
2. **Active Backlog Mapping:**
    - `[4.A.1]` Execution Backend Factory & Refactoring → **Mapped (Phase 4, Lane A)**
    - `[4.B.1]` Slippage Models → **Mapped (Phase 4, Lane B)**
    - `[4.C.1]` Sandbox CPU-Timer Hardening (Issue #510) → **Mapped (Phase 4, Lane C)** *Signal handling and thread safety implementations.*
    - `[4.D.1]` Zero-Quantity Order Rejection → **Mapped (Phase 4, Lane D)**

---

## Roadmap Status & Phases

### Shipped / Operational
*   **[1.x]** Phase 1: Foundations ✓
*   **[2.A.1]** Auth + RBAC ✓
*   **[2.D.1]** Admin API ✓
*   **[XC.1-6]** CI/CD, Security, Load Testing, Runners, AI Tooling ✓

### Current Active Development
*   **[2.B/C]** Phase 2 Infra & Auth completion (Pending formal closure)
*   **[4.A.1]** Execution Backend Factory
*   **[4.B.1]** Slippage Models
*   **[4.C.1]** Sandbox CPU-Timer Hardening (Issue #510 - Signal handling/thread safety)
*   **[4.D.1]** Zero-Quantity Order Rejection

### Blocked / Immediate Action Required
*   **Phase 3 Gate:** Blocked by missing ADR directory and ADR-0003, ADR-0004, ADR-0005.

---

```mermaid
flowchart LR
    P1["Phase 1<br/>Foundations ✓"] -->|gate closed| P2["Phase 2<br/>Infra & Auth"]
    P2 -->|gate open| P3["Phase 3<br/>Broker Adapters<br/>(BLOCKED by ADRs)"]
    P3 -->|gate open| P4["Phase 4<br/>Paper Trading<br/>(ACTIVE PARALLEL)"]
    P4 -->|gate closed| P5["Phase 5<br/>Live Trading"]
    P5 -->|gate closed| P6["Phase 6<br/>Compliance"]
    P6 -->|gate closed| P7["Phase 7<br/>Scale"]

    XC["XC.1–6<br/>Cross-Cutting<br/>Operational ✓"] -.->|spans all phases| P1
    XC -.-> P2
    XC -.-> P3
    XC -.-> P4
    XC -.-> P5
    XC -.-> P6
    XC -.-> P7

    LT["XC.3<br/>Load Testing"] -.->|baseline required| P5
    LT -.->|scale validation| P7

    style P1 fill:#4caf50,color:#fff
    style P2 fill:#4caf50,color:#fff
    style P3 fill:#ffeb3b,color:#000
    style P4 fill:#2196f3,color:#fff
    style P5 fill:#9e9e9e,color:#fff
    style P6 fill:#ff9800,color:#fff
    style P7 fill:#9e9e9e,color:#fff
    style XC fill:#7e57c2,color:#fff
    style LT fill:#7e57c2,color:#fff
```
