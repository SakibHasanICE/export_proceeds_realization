"""One polling cycle (email -> PDFs -> extraction -> database) and the hourly scheduler."""
import threading
from pathlib import Path

from app.common.logger import get_logger
from app.components import database
from app.components.ai_agent import extract_document
from app.components.email_agent import EmailAgent
from app.config.settings import settings

log = get_logger(__name__)
_cycle_lock = threading.Lock()
_stop = threading.Event()


def process_file(path: Path | str, message_id: str | None = None) -> dict:
    """Extract one staged PDF and store the result."""
    path = Path(path)
    rec = extract_document(str(path))
    if rec.get("extractor") == "rules" and rec.get("doc_type") == "export_payment_advice" \
            and not rec.get("unsupported"):
        status, rid = database.save_realization(rec, message_id)
        log.info("%s -> %s (id %s, review=%s)", path.name, status, rid, rec["needs_review"])
        return {"file": path.name, "status": status, "id": rid, "needs_review": rec["needs_review"],
                "issues": rec["issues"]}
    doc_id = database.save_other_document(rec, message_id)
    log.info("%s -> other document %s (%s)", path.name, doc_id, rec.get("doc_type"))
    return {"file": path.name, "status": "other_document", "id": doc_id,
            "needs_review": True, "issues": rec.get("issues", [])}


def run_cycle() -> dict:
    """Poll the mailbox once. Safe to call from the scheduler and the API at the same time."""
    if not _cycle_lock.acquire(blocking=False):
        return {"skipped": "a cycle is already running"}
    stats = {"emails": 0, "attachments": 0, "saved": 0, "duplicates": 0,
             "other_documents": 0, "needs_review": 0, "errors": 0}
    try:
        with EmailAgent() as agent:
            for mail in agent.fetch_new():
                if database.email_already_processed(mail.message_id):
                    agent.mark_seen(mail.uid)
                    continue
                stats["emails"] += 1
                if not mail.attachments:                      # diagram: no attachment -> ignore email
                    database.log_email(mail.message_id, mail.sender, mail.subject, "no_pdf")
                    agent.mark_seen(mail.uid)
                    continue
                all_ok = True
                for path in mail.attachments:
                    stats["attachments"] += 1
                    try:
                        res = process_file(path, mail.message_id)
                        key = {"saved": "saved", "duplicate": "duplicates",
                               "other_document": "other_documents"}[res["status"]]
                        stats[key] += 1
                        stats["needs_review"] += int(bool(res["needs_review"]))
                    except Exception:
                        all_ok = False
                        stats["errors"] += 1
                        log.exception("failed processing %s", path.name)
                if all_ok:   # failed mail stays unseen and is retried next cycle
                    database.log_email(mail.message_id, mail.sender, mail.subject, "processed")
                    agent.mark_seen(mail.uid)
    finally:
        _cycle_lock.release()
    log.info("cycle done: %s", stats)
    return stats


def start_scheduler() -> threading.Thread:
    def loop():
        while not _stop.is_set():
            try:
                run_cycle()
            except Exception:
                log.exception("scheduled cycle failed")   # alert hook goes here
            _stop.wait(settings.poll_minutes * 60)

    t = threading.Thread(target=loop, name="poller", daemon=True)
    t.start()
    log.info("scheduler started: every %s min", settings.poll_minutes)
    return t


def stop_scheduler() -> None:
    _stop.set()
