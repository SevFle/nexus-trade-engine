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
- (fix) Constrain MagicMock objects in tests/test_app_coverage.py with spec= to prevent auto-attribute false positives, remove r


### Internal
- (write_tests) Write tests for engine/ws/bridge.py uncovered lines (95-118, 131-145, 163-165, 232-257, 282-306, 344-387) to raise cover


### Fixed
- (fix) Narrow broad `except Exception` in `engine/core/order_manager.py` (fill-event publish path) to the expected bus failure types and add a `fill_event_publish_failures` metric counter so swallowed errors are observable instead of silent.
- (fix) Replace fragile dotted-to-underscore string normalization in `engine/api/ws/event_bridge.py` by keying `_EVENT_TO_CHANNEL` on actual `EventType` enum values, eliminating a class of channel-resolution bugs from string mangling.
- (fix) Fix HIGH-severity eager-evaluation bug in `engine/api/routes/marketplace.py` where `getattr(catalog, 'get', _fallback_strategy_get(catalog))` constructed the fallback adapter on every request regardless of whether `catalog.get` existed; replaced with a lazy callable so the adapter is only built when actually needed. Fix MEDIUM-severity security issue where raw `strategy_id` was reflected verbatim in 404 error `detail`, enabling information leakage; error responses now return a generic message.
- (fix) Fix critical sandbox security bypasses: add `__dict__` and missing escape primitives (`__reduce__`, `__reduce_ex__`, `__wrapped__`, `__self__`, `__loader__`, `__spec__`, `__objclass__`, `__defaults__`, `__kwdefaults__`) to `_BLOCKED_ATTRS` in `engine/plugins/sandbox/__init__.py`; add `@pytest.mark.asyncio` to async tests in `tests/test_sandbox_blocked_attrs.py`.
- (write_tests) Add a no-retry guard in `LiveExecutionBackend._request` so an transport error on the non-idempotent `POST /v2/orders` (order submission) raises `BrokerConnectionError` immediately instead of retrying, preventing duplicate orders; add unit tests covering the guard and confirming reads still retry.
- (fix) Fix check ordering in `engine/core/execution/live.py` `execute()` and the two failing tests in `tests/test_execution_backends.py`

### Internal
- (fix) Fix missing fakeredis dependency causing test collection failure in tests/test_rate_limit.py
