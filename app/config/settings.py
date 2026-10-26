"""Central configuration. Secrets come from the environment / .env (never commit .env)."""
import json
import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")


def _csv(name: str, default: str = "") -> list[str]:
    return [x.strip().lower() for x in os.getenv(name, default).split(",") if x.strip()]


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _load_account_map() -> dict:
    """Account lookup: HSBC masks numbers, so key by the last 6 digits; SCB by full number."""
    path = Path(os.getenv("ACCOUNT_MAP_PATH", Path(__file__).with_name("account_map.json")))
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _database_url() -> str:
    """DATABASE_URL if set, else built from POSTGRES_* (same variables as docker-compose), else SQLite."""
    if os.getenv("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    user, pw, db = os.getenv("POSTGRES_USER"), os.getenv("POSTGRES_PASSWORD", ""), os.getenv("POSTGRES_DB")
    if (user or db or pw) and not (user and db):
        # a half-filled .env must not silently fall back to a throw-away SQLite file
        raise RuntimeError("POSTGRES_USER and POSTGRES_DB must both be set in .env "
                           f"(POSTGRES_USER set: {bool(user)}, POSTGRES_DB set: {bool(db)})")
    if user and db:
        host, port = os.getenv("POSTGRES_HOST", "localhost"), os.getenv("POSTGRES_PORT", "5432")
        return f"postgresql+psycopg2://{quote_plus(user)}:{quote_plus(pw)}@{host}:{port}/{db}"
    return f"sqlite:///{BASE_DIR / 'local.db'}"


class Settings:
    # --- mailbox (Google Workspace IMAP: use an app password or OAuth, not the normal password)
    imap_host = os.getenv("IMAP_HOST", "imap.gmail.com")
    imap_port = int(os.getenv("IMAP_PORT", "993"))
    imap_user = os.getenv("IMAP_USER", "")            # e.g. something@urmigroup.net
    imap_password = os.getenv("IMAP_PASSWORD", "")
    imap_folder = os.getenv("IMAP_FOLDER", "INBOX")
    # whitelist: full addresses ("a@b.com") or domains ("hsbc.com.bd" / "@hsbc.com.bd")
    known_senders = _csv("KNOWN_BANK_SENDERS")
    reject_dmarc_fail = _bool("REJECT_DMARC_FAIL", True)

    # --- scheduler
    poll_minutes = int(os.getenv("POLL_MINUTES", "60"))
    scheduler_enabled = _bool("SCHEDULER_ENABLED", True)
    staging_dir = Path(os.getenv("STAGING_DIR", BASE_DIR / "staging"))

    # --- database: Postgres via POSTGRES_* (SQLite fallback if they are not set)
    database_url = _database_url()

    # --- LLM fallback
    openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_model = os.getenv("OPENROUTER_MODEL", "mistralai/ministral-8b-2512")

    # --- business rules
    bill_amount_basis = os.getenv("BILL_AMOUNT_BASIS", "proceeds")  # "proceeds" | "stated"
    account_map = _load_account_map()

    # --- servers
    bind_host = os.getenv("BIND_HOST", "127.0.0.1")
    api_port = int(os.getenv("API_PORT", "8000"))
    ui_port = int(os.getenv("UI_PORT", "8501"))
    api_key = os.getenv("API_KEY", "")   # if set, API requires header X-API-Key
    log_dir = Path(os.getenv("LOG_DIR", BASE_DIR / "logs"))


settings = Settings()
