"""Entry point: the FastAPI backend (+ hourly mailbox poller). The web UI is the separate Next.js service."""
import uvicorn

from app.config.settings import settings


def main() -> None:
    uvicorn.run("app.backend.api:app", host=settings.bind_host, port=settings.api_port)


if __name__ == "__main__":
    main()
