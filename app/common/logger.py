import logging
from logging.handlers import RotatingFileHandler

from app.config.settings import settings

_FMT = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
_configured = False


def _configure_root() -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(_FMT)
    root.addHandler(console)
    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(settings.log_dir / "app.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(_FMT)
        root.addHandler(fh)
    except OSError:
        pass  # read-only filesystem: console logging only
    _configured = True


def get_logger(name: str) -> logging.Logger:
    _configure_root()
    return logging.getLogger(name)
