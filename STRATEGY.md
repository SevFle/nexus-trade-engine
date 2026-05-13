# Nexus Trade Engine — Development Strategy

**Authoritative.** The engine follows this execution plan strictly. Phases run sequentially. Lanes within a phase run in parallel.

---

## Execution Method

Every issue is tagged `[N.L.k]`:
- **N** = Phase (1-7). Sequential. Phase N+1 starts only after Phase N gates close.
- **L** = Lane (A, B, C...). Parallel within a phase. Pick any lane to staff.
- **k** = Position within lane. Sequential. Lower numbers first.

**Updated Active Map:** Tracking 75 active issues across 7 phases, reconciling CI infrastructure, security automation, and early deployments.

---

## Roadmap Progress Overview

```mermaid
gantt
    title Nexus Trade Engine — Execution Roadmap
    dateFormat  YYYY-MM-DD
    axisFormat  %b %Y
    
    section Phase 1 Foundations
    Backtest regression tests (SEV-217)       :done, 1a, 2024-01-01, 30d
    Property-based testing (Hypothesis)       :done, 1c, 2024-02-01, 20d
    80%+ coverage (SEV-264)                   :active, 1b, 2024-01-15, 90d

    section Phase 2 Safety & Legal
    Auth + RBAC (SEV-233)                     :active, 2a, 2024-04-01, 60d
    Plugin sandbox (SEV-267)                  : 2b, 2024-04-01, 60d
    Legal surfaces (SEV-206)                  : 2c, 2024-04-01, 60d

    section Phase 3 Engine Completeness
    Event Bus Subsystem                       :done, 3f, 2024-03-15, 30d
    Live Trading (SEV-258+)                   : 3a, 2024-06-01, 90d
    WebSocket API (SEV-275)                   : 3b, 2024-06-01, 60d
    MCP Server (SEV-223+)                     : 3c, 2024-06-01, 90d
    Multi-Asset (SEV-239+)                    : 3d, 2024-06-01, 90d
    Multi-Strategy (SEV-261+)                 : 3e, 2024-06-01, 90d

    section Phase 4 Production Readiness
    Security Scanning (gitleaks/sec.yml)      :active, 4s, 2024-05-01, 60d
    Load Testing (load-test.yml)              :active, 4l, 2024-06-01, 60d
    Release Automation (publish/release)      :active, 4r, 2024-07-01, 60d
    Docker Dev (SEV-260)                      : 4a, 2024-09-01, 40d
    Observability (SEV-251+)                  : 4b, 2024-09-01, 60d
    Blue/Green Deploy (SEV-216)               : 4c, 2024-09-01, 60d

    section Phase 5 Frontend Polish
    UI Screens (SEV-429+)                     : 5a, 2025-01-01, 120d
    Cross-Cutting UX (SEV-208+)               : 5b, 2025-01-01, 120d

    section Phase 6 Growth
    Marketplace
