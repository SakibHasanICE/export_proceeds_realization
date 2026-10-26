"""Entry point: starts the Streamlit UI (subprocess) and the FastAPI backend (+ hourly poller)."""
import subprocess
import sys
from pathlib import Path

import uvicorn

from app.common.logger import get_logger
from app.config.settings import settings

log = get_logger(__name__)


def main() -> None:
    ui_path = Path(__file__).parent / "frontend" / "ui.py"
    ui = subprocess.Popen([sys.executable, "-m", "streamlit", "run", str(ui_path),
                           "--server.port", str(settings.ui_port),
                           "--server.address", settings.bind_host,
                           "--server.headless", "true",
                           "--browser.gatherUsageStats", "false"])
    try:
        uvicorn.run("app.backend.api:app", host=settings.bind_host, port=settings.api_port)
    finally:
        ui.terminate()


if __name__ == "__main__":
    main()
