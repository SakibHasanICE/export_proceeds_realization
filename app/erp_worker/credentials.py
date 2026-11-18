"""Which ERP login a run uses: the user's own (saved encrypted in the database), or the shared one from .env."""
import contextlib
import hashlib
import os
from pathlib import Path

from app.components import database, secret_box
from app.config.settings import settings


class CredentialMissing(Exception):
    """The user has no usable personal ERP login (and the shared login is not allowed for them)."""


def _state_base() -> Path:
    return Path(os.getenv("ERP_STATE_FILE", "erp_state.json"))


def state_file_for(email: str) -> Path:
    """Every user keeps their own saved browser session, so one user's session is never used for another."""
    base = _state_base()
    tag = hashlib.sha1(email.lower().encode()).hexdigest()[:12]
    return base.with_name(f"{base.stem}_{tag}{base.suffix}")


def forget_session(email: str) -> None:
    with contextlib.suppress(Exception):
        state_file_for(email).unlink()


def _shared_allowed() -> bool:
    return bool(os.getenv("ERP_USERNAME") and os.getenv("ERP_PASSWORD"))


def mode_for(email: str | None) -> dict:
    """What the page shows: personal (own login saved) | shared (.env login) | missing (must add one)."""
    if not settings.allowed_email_domain or not email:
        return {"mode": "shared", "username": None}
    cred = database.get_erp_credential(email)
    if cred:
        return {"mode": "personal", "username": cred[0]}
    if settings.erp_fallback_shared and _shared_allowed():
        return {"mode": "shared", "username": None}
    return {"mode": "missing", "username": None}


def login_for(email: str | None) -> dict | None:
    """None = use the shared login from .env. Otherwise {'username','password','state_file','personal'}."""
    if mode_for(email)["mode"] == "shared":
        return None
    cred = database.get_erp_credential(email or "")
    if not cred:
        raise CredentialMissing("You have not saved your ERP login yet. Click 'My ERP login' at the top of the page "
                                "and enter your ERP username and password once.")
    try:
        password = secret_box.decrypt(cred[1])
    except secret_box.SecretBoxError as e:
        raise CredentialMissing(str(e)) from e
    return {"username": cred[0], "password": password, "state_file": str(state_file_for(email)), "personal": True}
