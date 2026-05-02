# Contributing to Nexus Trade Engine

Thanks for your interest in improving Nexus Trade Engine. This document
covers everything you need to land a change quickly and predictably.

## Quick Start

```bash
# 1. Fork, then clone your fork
git clone git@github.com:<you>/nexus-trade-engine.git
cd nexus-trade-engine

# 2. Set up the dev environment
make setup            # installs Python deps, pre-commit hooks, etc.
cp .env.example .env  # fill in any local secrets

# 3. Run the test suite to confirm a clean baseline
make test
```

If `make` is not available, see the equivalent commands in
[`docs/development.md`](docs/development.md) (or the Makefile itself).

## Branching

- Branch from `main`.
- Use a descriptive prefix: `feat/`, `fix/`, `chore/`, `docs/`, `refactor/`,
  `perf/`, `ci/`, or `test/`, followed by a short slug and (when relevant)
  the GitHub issue number, e.g. `feat/oms-shadow-mode-111`.
- Keep branches focused — one logical change per PR is much easier to review
  than a sprawling branch.

## Development Workflow

We follow a TDD-leaning workflow:

1. **Write or update tests first.** New behaviour without a test will be
   asked to add one in review.
2. **Implement** the smallest change that makes the test pass.
3. **Refactor** with the tests green.
4. **Run the full local check** before pushing:

   ```bash
   make lint     # ruff / black / mypy where configured
   make test    # pytest
   make build   # docker build, sanity check
   ```

5. Open the PR — see the template for the expected fields.

For larger features, please open an issue first or start a draft PR so we
can align on the approach before you spend a lot of time.

## Coding Standards

- **Python:** 3.14+, formatted with `ruff format` (Black-compatible). Imports
  are sorted by `ruff`. Type hints are expected on public functions.
- **Async first:** server code uses async SQLAlchemy / FastAPI. Avoid mixing
  blocking I/O into async paths.
- **Migrations:** add an Alembic revision in `engine/db/migrations/versions/`
  for any schema change. The chain is numbered (e.g. `010_webhooks.py`) —
  pick the next number.
- **Logging:** use `structlog`. The `event` kwarg is reserved by structlog;
  pass domain-event names as `event_type=` instead.
- **Secrets:** never commit secrets. The CI secret scan (gitleaks) will
  block the PR if it finds any.

## Testing

- We aim for >= 80% coverage on new code. Use `pytest --cov` locally to
  check.
- Prefer integration tests against the real DB layer for anything that
  touches SQL — mock-only tests have masked migration bugs in the past.
- Long-running or external-network tests should be marked and skipped by
  default.

## Pull Requests

- Fill in the PR template completely (`.github/PULL_REQUEST_TEMPLATE.md`).
- Reference the GitHub issue (`Closes #123`) when applicable.
- Keep the PR title under 70 characters and use the conventional-commits
  style: `feat:`, `fix:`, `docs:`, `chore:`, etc.
- Self-review the diff before requesting review.
- Make sure CI is green — if a check is flaky, mention it explicitly so
  reviewers know.

## Reporting Bugs and Requesting Features

Use the issue forms in `.github/ISSUE_TEMPLATE/`. The forms exist to make
sure we capture enough context to triage quickly. Security issues do **not**
go in public issues — see [`SECURITY.md`](SECURITY.md).

## Code of Conduct

By participating you agree to follow our
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md). Reports of violations go through
the channels in [`SECURITY.md`](SECURITY.md).

## Licensing

Contributions are accepted under the project's existing license (see
[`LICENSE`](LICENSE)). Submitting a pull request signals that you have the
right to contribute the code under that license.
