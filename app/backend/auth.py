"""Who is calling the API. The Next.js server signs the user in with Google and forwards the address in X-User-Email.

Only the Next.js server knows API_KEY, so the header is trusted the same way the key is: keep API_KEY set and do not
expose the API port to the network.
"""
import re
from typing import Optional

from fastapi import Header, HTTPException

from app.config.settings import settings

_EMAIL = re.compile(r"[^@\s]{1,64}@[a-z0-9.-]{1,190}")


def current_user(x_user_email: Optional[str] = Header(default=None)) -> Optional[str]:
    """The signed-in user's e-mail (lower case), or None when sign-in is not enforced (ALLOWED_EMAIL_DOMAIN empty)."""
    email = (x_user_email or "").strip().lower()
    domain = settings.allowed_email_domain
    if domain:
        if not _EMAIL.fullmatch(email) or not email.endswith("@" + domain):
            raise HTTPException(401, f"sign in with your @{domain} Google account")
    return email if _EMAIL.fullmatch(email) else None


def is_master(email: Optional[str]) -> bool:
    """Master accounts (MASTER_EMAILS) see every user's data. With sign-in switched off everybody does."""
    return not settings.allowed_email_domain or (email or "").strip().lower() in settings.master_emails


def owner_scope(email: Optional[str]) -> Optional[str]:
    """What to filter the data by: None = no filter (master), otherwise the user's own address."""
    return None if is_master(email) else (email or "").strip().lower()


def can_access(owner_email: Optional[str], email: Optional[str]) -> bool:
    scope = owner_scope(email)
    return scope is None or (owner_email or "").lower() == scope
