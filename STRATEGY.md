# Nexus Trade Engine — Development Strategy

**Authoritative.** The engine follows this execution plan strictly. Phases gate merges; lanes within a phase run in parallel. Cross-phase delivery is permitted under the Exception Protocol (§Phase Gate Exceptions).

> **Drift advisory (resolved):** Phase 2 Lane A (Auth, SEV-233) and multiple untracked features shipped before Phase 1 gate (SEV-264 coverage) formally closed. All exceptions are documented below in §Phase Gate Exceptions. Coverage gate `[1.2]` has been **closed** following extensive test additions (commits bc89f1e, a253064, 5bc1f0d, 5f46cb9). Remaining Phase 2+ lanes are unblocked.
>
> **Process amendment (retroactive-tracking rule):** Effective immediately, any merged feature without a pre-existing `[N.L.k]` tag must receive a retroactive mapping entry in §Shipped within one sprint of merge. Unmapped merges block the next phase gate until catalogued. See §Process Drift Correction below.
>
> **Enforcement advisory (active):** The Development Stability Protocol (§Development Stability Protocol) prohibiting `wip:`-prefixed commits on `main` has not been enforced. **6 of the latest 20 commits (30%) remain `wip:` commits.** This revision escalates enforcement; see §Development Stability Protocol for corrective actions.

---

## Execution Method

Every issue is tagged `[N.L.k]`:
- **N** = Phase (1-7). Sequential gate logic: Phase N+1 gates open only after Phase N gates close.
- **L** = Lane (A, B, C...). Parallel within a phase. Pick any lane to staff.
- **k** = Position within lane. Sequential. Lower numbers first.

Cross-cutting concerns use `[XC.k]` and track against their own gate (ADR approval), not a phase gate.

**Issue counts are maintained as a live metric.** Historical baseline: ~80 open issues estimated 2025-01, ~65 active mapped. Post-streamline (commit 02b4465) and coverage-gate closure, current active mapped issue count is **52** (deduplication pass completed 2025-01; down from ~55 pre-dedup). Tally will be re-verified at each phase gate closure.

### Delivery Model: Gated Sequential with Acknowledged Parallelism

The declared model is **sequential phase execution**. In practice, two categories of parallel work are now formally recognised:

| Category | Governance | Examples |
|----------|-----------|----------|
| **Exception-gated** cross-phase delivery | Logged in §Phase Gate Exceptions; requires own test suite + ADR | EX-001 (Auth), EX-002 (Admin API), EX-003 (Execution Backend) |
| **Retroactively-mapped** untracked delivery | Post-hoc mapping in §Shipped; triggers §Process Drift Correction review | Slippage models, sandbox audit, legal-qa |

**Rule amendment:** When the cumulative count of retroactively-mapped deliveries exceeds **3 per sprint**, the strategy document must be revised within one sprint to either (a) formally restructure the phase plan or (b) escalate to a gated-parallel model with per-lane entry criteria. Current count: **8 retroactively-mapped deliveries** — threshold exceeded; this revision constitutes the required restructuring.

### Development Stability Protocol

**Observed issue:** Frequent emergency commits (`wip: auto-save before ERR`) in recent commit history. Despite the prohibition enacted in the prior revision, **6 of the last 20 commits on `main` (30% WIP ratio)** remain `wip:`-prefixed, indicating the corrective measure was not enforced.

**Corrective measures (effective this revision — escalated):**

1. **WIP commit hygiene — enforced via pre-receive hook:** Emergency WIP commits must be squashed or amended before merge to `main`. No `wip:` prefixed commits permitted on the main branch. **Enforcement mechanism:** A pre-receive or CI check must reject any commit message matching `/^wip:/i` on `main`. Hook/CI rule to be implemented as `[XC.6]` (see Cross-Cutting Concerns). Until the hook is active, no PR containing a `wip:` commit may be merged; reviewers must block.
2. **Root-cause review:** If a developer logs >2 emergency WIP commits in a sprint, a brief root-cause analysis is required (environment instability, tooling gaps, or process issues).
3. **Stability metric:** WIP commit ratio tracked at each sprint audit. **Target: <5% of total commits. Current: 30% (6/20) — action required. Trend: improving from 40% baseline.**

```mermaid
flowchart LR
    subgraph WIP Enforcement
        A["Commit pushed<br/>to main"] --> B{"Pre-receive hook:<br/>message matches /^wip:/i?"}
        B -->|Yes| C["❌ Reject commit"]
        B -->|No| D["✅ Accept commit"]
        C --> E["Developer squashes<br/>or amends"]
        E --> A
    end
```

---

## Cross-Cutting Concerns `[XC.k]`

Infrastructure and tooling that spans all phases. Each cross-cutting concern requires an ADR for gate approval.

| Tag | Concern | Status | ADR | Workflows / Tooling | Phase Relevance |
|-----|---------|--------|-----|---------------------|-----------------|
| `[XC.1]` | **CI/CD Pipeline** — continuous integration, image publishing, release automation | ✓ Operational | ADR-0003 *(required)* | `ci.yml`, `publish-images.yml`, `release-please.yml` | All phases |
| `[XC.2]` | **Security Scanning** — secret detection, vulnerability scanning | ✓ Operational | ADR-0004 *(required)* | `security.yml`, `.gitleaks.toml` | All phases |
| `[XC.3]` | **Load Testing** — performance regression detection | ✓ Operational | ADR-0005 *(required)* | `load-test.yml` | Phase 5 (Live Trading), Phase 7 (Scale) |
| `[XC.4]` | **Property-Based Testing** — generative coverage expansion via Hypothesis | ✓ Operational | — *(embedded in test policy)* | `.hypothesis/` persistent seed constants | All phases |
| `[XC.5]` | **Self-Hosted Runners** — dedicated `nexus` runner for all CI workflows | ✓ Operational | — *(infra config)* | Runner: `nexus` | All phases |
| `[XC.6]` | **Commit Hygiene Enforcement** — reject `wip:` commits on `main`, stability metric reporting | ⏳ Pending implementation | — *(CI policy)* | Pre-receive hook or CI gate (§Development Stability Protocol) | All phases |
| `[XC.7]` | **Design Skill Integration** — `.claude/skills/nothing-design` directory for structured design-to-implementation workflow | ✓ Operational | — *(tooling config)* | `.claude/skills/nothing-design/` | All phases |
| `[XC.8]` | **Issue & PR Templates** — standardised GitHub templates for bug reports, feature requests, config, and pull requests | ✓ Operational | — *(process tooling)* | `.github/ISSUE_TEMPLATE/bug.yml`, `feature.yml`, `config.yml`; `.github/PULL_REQUEST_TEMPLATE.md` | All phases |

**ADR backlog:** `[XC.1]`, `[XC.2]`, and `[XC.3]` are operational but lack formal Architecture Decision Records. **ADRs must be drafted and approved before Phase 3 gate closure.** Blocking: Phase 3 → Phase 4 transition.

```mermaid
flowchart LR
    P1["Phase 1<br/>Foundations ✓"] -->|gate closed| P2["Phase 2<br/>Infra & Auth"]
    P2 -->|gate closed| P3["Phase 3<br/>Broker Adapters"]
    P3 -->|gate closed| P4["Phase 4<br/>Paper Trading"]
    P4 -->|gate closed| P5["Phase 5<br/>Live Trading"]
    P5 -->|gate closed| P6["Phase 6<br/>Compliance"]
    P6 -->|gate closed| P7["Phase 7<br/>Scale"]

    XC["XC.1–8<br/>Cross-Cutting"] -.->|spans all phases| P1
    XC -.-> P2
    XC -.-> P3
    XC -.-> P4
    XC -.-> P5
    XC -.-> P6
    XC -.-> P7

    LT["XC.3<br/>Load Testing"] -.->|baseline required| P5
    LT -.->|scale validation| P7

    style P1 fill:#4caf50,color:#fff
    style P2 fill:#2196f3,color:#fff
    style P3 fill:#9e9e9e,color:#fff
    style P4 fill:#9e9e9e,color:#fff
    style P5 fill:#9e9e9e,color:#fff
    style P6 fill:#ff9800,color:#fff
    style P7 fill:#9e9e9e,color:#fff
    style XC fill:#7e57c2,color:#fff
    style LT fill:#7e57c2,color:#fff
```

---

## Phase Gate Exceptions

Documented violations of the sequential-phase rule. Every exception must record: what shipped early, why, residual risk, and remediation.

| Exception | What Shipped | Gate Bypassed | Justification | Residual Risk | Remediation |
|-----------|-------------|---------------|---------------|---------------|-------------|
| `EX-001` | `[2.A.1]` Auth + RBAC (SEV-233) | `[1.2]` 80%+ coverage (SEV-264) | Auth ADR-0002 was fully spec'd; implementation had its own test suite; security review needed early for Phase 3 broker adapter design | Core engine paths unmonitored by coverage gate at time of merge | ✓ **Closed** — coverage gate [1.2] now passed; SEV-264 closed |
| `EX-002` | Admin API (commits ec8754b, 5f46cb9) | `[1.2]` coverage gate + Phase 2 Lane D not formally established | Required for operational management of live-trading preparation; auth (EX-001) already shipped | Admin endpoints operated without formal coverage gate | ✓ **Closed** — coverage gate [1.2] now passed; Lane D formally mapped as `[2.D.1]` |
| `EX-003` | Execution backend features: zero-quantity order rejection, execution backend factory tests, execution backend refactoring | `[1.2]` coverage gate; Phase 2 not formally closed | Execution backend factory (ADR-0006) was independently spec'd with own test suite; zero-quantity rejection is a safety guard required before any order-flow work in Phase 3+; factory tests provide contractual coverage | Execution backend architecture shipped without Phase 2 closure confirming infra stability; safety rejection logic not validated against formal coverage gate at merge time | ⏳ **Open** — Phase 2 gate closure must confirm no regression; execution backend features added to Phase 2 Lane C as `[2.C.2]`–`[2.C.4]` retroactively |

**Rule amendment:** A Lane may ship ahead of its phase gate only if (1) it has its own independent test suite, (2) an ADR is approved, and (3) the exception is logged here. The gate still blocks all remaining lanes in the same and subsequent phases until the gate closes.

---

## Process Drift Correction

**Problem:** Eight features (Admin API, execution backend factory, slippage models, zero-quantity order rejection, sandbox audit logging, legal-qa infrastructure, sandbox CPU timer, execution backend refactoring) were implemented and merged without phase/lane tracking issues or `[N.L.k]` commit tags. While now retroactively documented, the underlying process allowed significant untracked work to accumulate.

**Correction (effective this revision):**

1. **Retroactive-mapping rule:** Any merged PR/commit introducing user-facing or architectural behaviour must be mapped to a `[N.L.k]` tag within one sprint. Unmapped merges block the next phase gate.
2. **Execution backend features formally captured:** Zero-quantity order rejection, execution backend factory tests, and execution backend refactoring are mapped to Phase 2 Lane C as `[2.C.2]`–`[2.C.4]`. Exception EX-003 logged above.
3. **`wip:` commit enforcement failure acknowledged:** The prior revision's WIP prohibition was declared but not enforced through any automated gate. This revision adds `[XC.6]` as a tracked cross-cutting concern with an implementation ticket. Reviewers are instructed to block `wip:`-prefixed commits effective immediately, pending the automated hook.
4. **Deduplication pass completed:** The backlog deduplication pass referenced in the prior revision has been completed. Active mapped issue count reduced from ~55 to **52**. Next deduplication audit scheduled at Phase 2 gate closure.

---

## Issue Backlog Health

| Metric | Baseline (2025-01) | Post-Streamline (02b4465) | Post-Dedup (Current) | Target |
|--------|-------------------|---------------------------|----------------------|--------|
| Total open issues | ~80 | ~65 | **52** | <40 at Phase 3 gate |
| Mapped with `[N.L.k]` | ~30 | ~55 | **52** | 100% of active issues |
| Unmapped / triage | ~50 | ~10 | **0** | 0 at all times |
| WIP commit ratio | 40% (8/20) | 30% (6/20) | **30% (6/20)** | <5% |

**Next scheduled audit:** Phase 2 gate closure.
