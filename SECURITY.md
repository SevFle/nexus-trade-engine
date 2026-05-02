# Security Policy

## Supported Versions

Only the `main` branch and the latest tagged release receive security fixes.
Older releases are best-effort. We recommend always running on a tag from the
last 90 days.

## Reporting a Vulnerability

**Do not file public GitHub issues for security vulnerabilities.**

Use one of these private channels:

1. **GitHub Security Advisories** — preferred. Open a draft advisory at
   <https://github.com/SevFle/nexus-trade-engine/security/advisories/new>.
   This keeps the discussion private until coordinated disclosure.
2. **Email** — `security@example.com` (operators forking this repo should
   replace with the address configured for their deployment). Encrypt with
   the maintainer's PGP key if available.

Include:

- A clear description of the vulnerability and its impact
- A minimal reproduction (commit hash, request, configuration)
- The version / commit you found it on
- Whether you would like credit in the advisory

We will acknowledge your report within **3 business days** and aim to issue
a fix or mitigation within **30 days** for critical issues. We will keep you
updated on progress and will coordinate disclosure timing with you.

## Scope

In scope:

- The Nexus Trade Engine API, frontend, plugins, MCP server, and
  infrastructure templates shipped from this repository.
- Authentication, authorization, MFA, rate limiting, CSRF / CSP / CORS handling.
- Data integrity issues that could mislead users about portfolio state, P&L,
  or trade fills.

Out of scope:

- Social engineering of maintainers.
- Denial of service that requires excessive request volume against
  unauthenticated endpoints — operators are expected to deploy a rate
  limiter / WAF.
- Vulnerabilities in third-party dependencies that we have not yet pulled
  in — please report those upstream first.
- Theoretical attacks without a working proof of concept.

## Safe Harbor

If you make a good-faith effort to comply with this policy, we will:

- Not pursue civil action or report you to law enforcement.
- Work with you to understand and resolve the issue quickly.
- Recognize your contribution if you wish.

## Disclosures

Past advisories: <https://github.com/SevFle/nexus-trade-engine/security/advisories>
