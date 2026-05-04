# Agent Guidelines

## Testing & Coverage

- **Run tests**: `uv run pytest --cov=engine -v --tb=long`
- **Run linting**: `uv run ruff check .`
- **Run type checking**: `uv run basedpyright`

### Coverage Configuration

- Coverage source is configured in `pyproject.toml` under `[tool.coverage.run]`
- The importable package is `engine` (directory `engine/`), NOT `nexus_trade-engine` (that's the PyPI project name)
- Coverage is invoked via pytest's `--cov=engine` in `addopts`
- Migration files are excluded from coverage: `engine/db/migrations/*`

### Known SQLAlchemy Default Pitfall

**Bug pattern**: `mapped_column(default=True)` only sets the SQL INSERT default, NOT the Python `__init__` default. When you create a model instance like `User(email="x")` without explicitly passing `is_active=True`, the attribute is `None` (falsy).

**Fix**: Always explicitly pass boolean defaults when creating model instances:
```python
# WRONG - is_active will be None
user = User(email=email, display_name=name, auth_provider="oidc")

# CORRECT - is_active is True
user = User(email=email, display_name=name, is_active=True, auth_provider="oidc")
```

**Affected files** (fixed as of 2025-05): `engine/api/auth/{oidc,google,github_oauth,local,ldap}.py`

**Regression test**: `tests/test_user_is_active_default.py`
