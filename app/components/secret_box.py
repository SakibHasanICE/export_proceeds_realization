"""Encrypt / decrypt the users' saved ERP passwords (Fernet, key from ERP_CREDENTIAL_KEY)."""
from app.config.settings import settings


class SecretBoxError(Exception):
    pass


def _fernet():
    if not settings.erp_credential_key:
        raise SecretBoxError("ERP_CREDENTIAL_KEY is not set in .env, so ERP passwords cannot be stored.")
    try:
        from cryptography.fernet import Fernet
    except ImportError as e:
        raise SecretBoxError("The 'cryptography' package is not installed (add it to requirements.txt and rebuild).") from e
    try:
        return Fernet(settings.erp_credential_key.encode())
    except Exception as e:
        raise SecretBoxError("ERP_CREDENTIAL_KEY is not a valid Fernet key.") from e


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise SecretBoxError("The saved ERP password cannot be read (the key changed). Enter it again.") from e
