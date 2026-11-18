"""Notification mail to the staff member whose documents were just processed."""
import smtplib
from email.message import EmailMessage
from html import escape

from app.common.logger import get_logger
from app.components import database
from app.config.settings import settings

log = get_logger(__name__)


def _describe(res: dict) -> tuple[str, str]:
    """(plain text, level) for one processed attachment. level: ok | warn | skip"""
    status = res.get("status")
    bills = res.get("results")
    if bills:                                   # an Excel sheet with several bills
        new = [b["bill"] for b in bills if b["status"] == "saved"]
        dup = [b["bill"] for b in bills if b["status"] == "duplicate"]
        parts = []
        if new:
            parts.append(f"{len(new)} bill(s) ready: " + ", ".join(new))
        if dup:
            parts.append(f"{len(dup)} already in the system: " + ", ".join(dup))
        level = "warn" if res.get("needs_review") else ("ok" if new else "skip")
        return "; ".join(parts) or "no bills found", level
    if status == "saved":
        bill = ""
        try:
            rec = database.get_realization(res["id"])
            bill = f" (Bill {rec['bill_invoice_no']})" if rec else ""
        except Exception:
            pass
        return "ready to proceed in the ERP" + bill + (" - please check the highlighted values" if res.get("needs_review") else ""), \
            "warn" if res.get("needs_review") else "ok"
    if status == "duplicate":
        return "already in the system (not added again)", "skip"
    return "could not be read automatically - needs a person to look at it", "warn"


def notify_processed(to: str, names: list[str], results: list[dict], subject: str) -> bool:
    """One mail per received mail, listing every PDF/Excel file name. Never raises: a mail problem must not undo the data."""
    if not settings.notify_enabled:
        return False
    if not (settings.smtp_user and settings.smtp_password):
        log.warning("notification not sent: SMTP_USER/SMTP_PASSWORD (or IMAP_USER/IMAP_PASSWORD) not set")
        return False
    rows = [(names[i] if i < len(names) else r.get("file", "file"),) + _describe(r) for i, r in enumerate(results)]
    ready = sum(1 for _, _, lvl in rows if lvl in ("ok", "warn"))
    msg = EmailMessage()
    msg["From"] = settings.notify_from or settings.smtp_user
    msg["To"] = to
    msg["Subject"] = ("Your data is processed and ready to proceed in ERP" if ready
                      else "Your documents were received") + f" - {len(rows)} file(s)"
    link = settings.app_url
    text = ["Hello,", "",
            f"We received your mail \"{subject[:120]}\" and processed these file(s):", ""]
    text += [f" - {n}: {d}" for n, d, _ in rows]
    if link:
        text += ["", f"Open the ERP automation page: {link}"]
    text += ["", "This is an automatic message."]
    msg.set_content("\n".join(text))
    li = "".join(f"<li><b>{escape(n)}</b>: {escape(d)}</li>" for n, d, _ in rows)
    btn = f'<p><a href="{escape(link)}">Open the ERP automation page</a></p>' if link else ""
    msg.add_alternative(f"<p>Hello,</p><p>We received your mail &quot;{escape(subject[:120])}&quot; and processed "
                        f"these file(s):</p><ul>{li}</ul>{btn}<p style='color:#888'>This is an automatic message.</p>",
                        subtype="html")
    try:
        if settings.smtp_port == 465:
            srv = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30)
        else:
            srv = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
            srv.starttls()
        with srv:
            srv.login(settings.smtp_user, settings.smtp_password)
            srv.send_message(msg)
        log.info("notification sent to %s (%d file(s))", to, len(rows))
        return True
    except Exception as e:
        log.warning("notification to %s failed: %s", to, e)
        return False
