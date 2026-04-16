from __future__ import annotations

import uuid
from datetime import UTC, datetime

from engine.db.models import Portfolio, User


def make_user(
    email: str = "test@example.com",
    display_name: str = "Test User",
    hashed_password: str = "hashed",  # noqa: S107
) -> User:
    return User(
        id=uuid.uuid4(),
        email=email,
        display_name=display_name,
        hashed_password=hashed_password,
        is_active=True,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )


def make_portfolio(
    user_id: uuid.UUID,
    name: str = "Test Portfolio",
    initial_capital: float = 100_000.0,
) -> Portfolio:
    return Portfolio(
        id=uuid.uuid4(),
        user_id=user_id,
        name=name,
        initial_capital=initial_capital,
        created_at=datetime.now(tz=UTC),
    )
