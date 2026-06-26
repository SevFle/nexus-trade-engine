# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are produced automatically by
[release-please](https://github.com/googleapis/release-please) from
[Conventional Commits](https://www.conventionalcommats.org/) on `main`. Do not
edit this file by hand — it is regenerated as part of every release PR.

## [Unreleased]

### Internal
- (fix) Fix release-please workflow failure and apply lint/format fixes across engine, tests, and SDK to make quality gate pass
- (fix) Fix the 3 review findings in coverage ramp tests: (1) correct misleading docstring in test_coverage_ramp.py, (2) add `--module-level` flag coverage in the ramp tests
- (fix) Add missing `users.processing_restricted` column to the test SQLite schema so the 25 `test_auth_e2e.py` tests match the `User` ORM model and pass
- (fix) Fix missing fakeredis dependency causing test collection failure in tests/test_rate_limit.py
- (write_tests) Write tests for the most recently changed code to break the loop
- (subagent) SEV-267 — Plugin Sandbox with Security Isolation (Phase 2, Lane B)

### Fixed
- (fix) Fix check ordering in `engine/core/execution/live.py` `execute()` and the two failing tests in `tests/test_execution_backends.py`


The initial public 0.1.0 release line. Entries below this point are appended
automatically once release-please starts cutting tagged releases.