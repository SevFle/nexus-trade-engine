# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are produced automatically by
[release-please](https://github.com/googleapis/release-please) from
[Conventional Commits](https://www.conventionalcommits.org/) on `main`. Do not
edit this file by hand — it is regenerated as part of every release PR.

## [Unreleased]

### Security
- (auth) Issue #741 ("resolve 403 error on developer resource access")
  cannot be implemented as the originally-requested ``200`` response
  without re-opening SEV-741 (silent ``quant_dev`` → ``developer``
  privilege escalation, reverted in commit ``a81578f``). The
  ``test_quant_dev_accesses_developer_resource`` test in
  ``tests/test_auth_recent_integration.py`` is therefore pinned to
  ``403`` with a positive-control test verifying the explicit
  ``developer`` role still receives ``200``. See the module docstring
  for the full root-cause analysis.

### Internal
- (write_tests) Write tests for the most recently changed code to break the loop


### Internal
- (write_tests) Write tests for the most recently changed code to break the loop


### Internal
- (write_tests) Write tests for the most recently changed code to break the loop


### Internal
- (write_tests) Write tests for the most recently changed code to break the loop
- (subagent) SEV-267 — Plugin Sandbox with Security Isolation (Phase 2, Lane B)


The initial public 0.1.0 release line. Entries below this point are appended
automatically once release-please starts cutting tagged releases.
