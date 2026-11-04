"""One polling cycle (email -> PDFs -> extraction -> database) and the hourly scheduler."""
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.common.logger import get_logger
from app.components import database, ledger
from app.components.ai_agent import extract_document
from app.components.ebl_parser import is_workbook, parse_workbook
from app.components.email_agent import EmailAgent
from app.config.settings import settings

log = get_logger(__name__)
_cycle_lock = threading.Lock()
_stop = threading.Event()


def arrival_date(mail_date: datetime | None) -> date:
    """The day the bank mail arrived, in local (Bangladesh) time; today for manual uploads."""
    tz = timezone(timedelta(hours=settings.local_utc_offset_hours))
    if mail_date is None:
        return datetime.now(tz).date()
    return (mail_date.astimezone(tz) if mail_date.tzinfo else mail_date).date()


def process_workbook(path: Path, message_id: str | None = None, mail_date: datetime | None = None) -> dict:
    """An EBL realization sheet holds many bills: store each one, report them individually."""
    try:
        recs = parse_workbook(path)
    except Exception as e:      # not an EBL sheet, or its layout changed: keep it for a human
        doc_id = database.save_other_document(
            {"source_file": path.name, "extractor": "none", "doc_type": None, "needs_review": True,
             "issues": [f"spreadsheet not recognised as an EBL realization sheet: {e}"]}, message_id)
        log.warning("%s -> other document %s (%s)", path.name, doc_id, e)
        return {"file": path.name, "status": "other_document", "id": doc_id, "needs_review": True,
                "issues": [str(e)]}
    results = []
    arrived = arrival_date(mail_date).isoformat()
    for rec in recs:
        rec["meta"]["arrived_on"] = arrived
        status, rid = database.save_realization(rec, message_id)
        log.info("%s [%s] -> %s (id %s, review=%s)", path.name, rec["bill_invoice_no"], status, rid, rec["needs_review"])
        results.append({"file": path.name, "bill": rec["bill_invoice_no"], "status": status, "id": rid,
                        "needs_review": rec["needs_review"], "issues": rec["issues"]})
    ledger.sync()                                    # one Excel update for the whole workbook
    return {"file": path.name, "status": "saved" if any(r["status"] == "saved" for r in results) else "duplicate",
            "bills": len(results), "saved": sum(r["status"] == "saved" for r in results),
            "duplicates": sum(r["status"] == "duplicate" for r in results),
            "needs_review": any(r["needs_review"] for r in results), "results": results}


def process_file(path: Path | str, message_id: str | None = None, mail_date: datetime | None = None) -> dict:
    """Extract one staged PDF (or EBL .xlsx) and store the result. mail_date = when the bank mail arrived."""
    path = Path(path)
    if is_workbook(path):
        return process_workbook(path, message_id, mail_date)
    rec = extract_document(str(path))
    if rec.get("extractor") == "rules" and rec.get("doc_type") == "export_payment_advice" \
            and not rec.get("unsupported"):
        rec.setdefault("meta", {})["arrived_on"] = arrival_date(mail_date).isoformat()
        status, rid = database.save_realization(rec, message_id)
        ledger.sync()
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
                        res = process_file(path, mail.message_id, mail.date)
                        for one in res.get("results") or [res]:     # a workbook reports one result per bill
                            key = {"saved": "saved", "duplicate": "duplicates",
                                   "other_document": "other_documents"}[one["status"]]
                            stats[key] += 1
                            stats["needs_review"] += int(bool(one["needs_review"]))
                    except Exception:
                        all_ok = False
                        stats["errors"] += 1
                        log.exception("failed processing %s", path.name)
                if all_ok:   # failed mail stays unseen and is retried next cycle
                    database.log_email(mail.message_id, mail.sender, mail.subject, "processed")
                    agent.mark_seen(mail.uid)
        ledger.sync()                                 # catch-up if an earlier write was refused
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
