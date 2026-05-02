"""Privacy / DSR (data-subject request) handling — GDPR & CCPA (gh#157).

Public surface intentionally minimal — see the submodules for details:

- :mod:`engine.privacy.dsr`       — registry: record, list, transition status.
- :mod:`engine.privacy.export`    — Art. 15 / 20: collect a user's data.
- :mod:`engine.privacy.deletion`  — Art. 17: initiate / cancel / grace window.

Async tarball generation, signed download URLs, and consent management
are explicit follow-ups; see ``docs/legal/processors.md`` and the
follow-up notes in the gh#157 PR.
"""

from engine.privacy.deletion import (
    DELETION_GRACE_DAYS,
    cancel_deletion,
    is_pending_deletion,
    request_deletion,
)
from engine.privacy.dsr import (
    DSR_KINDS,
    DSR_TERMINAL_STATUSES,
    SLA_DEFAULT_DAYS,
    list_user_requests,
    record_request,
)
from engine.privacy.export import collect_user_data

__all__ = [
    "DELETION_GRACE_DAYS",
    "DSR_KINDS",
    "DSR_TERMINAL_STATUSES",
    "SLA_DEFAULT_DAYS",
    "cancel_deletion",
    "collect_user_data",
    "is_pending_deletion",
    "list_user_requests",
    "record_request",
    "request_deletion",
]
