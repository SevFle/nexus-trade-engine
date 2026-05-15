"""Root conftest — intentionally empty.

Session-scoped asyncio loop configuration lives in pyproject.toml under
``[tool.pytest.ini_options]`` (asyncio_mode, asyncio_default_fixture_loop_scope,
asyncio_default_test_loop_scope).  No manual ``event_loop`` or
``event_loop_policy`` fixture is needed in any conftest file.
"""
