"""Account administration helpers."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import User

logger = logging.getLogger(__name__)


def promote_configured_admins(db: Session, emails: list[str]) -> list[str]:
    """Grant admin rights to any registered account listed in ADMIN_EMAILS.

    Returns the emails that were promoted by this call. Unknown addresses are
    ignored (the account simply becomes an admin when it is registered later).
    """
    if not emails:
        return []

    promoted: list[str] = []
    users = list(db.scalars(select(User).where(User.email.in_(emails))).all())
    for user in users:
        if not user.is_admin:
            user.is_admin = True
            promoted.append(user.email)
    if promoted:
        db.commit()
        logger.info("Promoted to administrator: %s", ", ".join(promoted))
    return promoted


def ensure_admin_exists(db: Session) -> str | None:
    """Guarantee the deployment always has at least one administrator.

    Without this, a cluster could lock itself out: for example a database
    created before roles existed, or an operator removing the last admin.
    The oldest account is promoted, and the action is logged loudly.
    """
    total_users = db.scalar(select(func.count(User.id))) or 0
    if total_users == 0:
        return None

    admin_count = db.scalar(
        select(func.count(User.id)).where(User.is_admin.is_(True))
    ) or 0
    if admin_count > 0:
        return None

    oldest = db.scalars(select(User).order_by(User.id)).first()
    if oldest is None:  # pragma: no cover - defensive
        return None

    oldest.is_admin = True
    db.commit()
    logger.warning(
        "No administrator account was found; promoted the oldest account (%s). "
        "Set ADMIN_EMAILS to control this explicitly.",
        oldest.email,
    )
    return oldest.email
