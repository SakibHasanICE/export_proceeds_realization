"""IMAP polling: find unseen mail from whitelisted bank senders and stage their PDF attachments."""
import email
import hashlib
import imaplib
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from app.common.custom_exception import CustomException
from app.common.logger import get_logger
from app.components import database
from app.config.settings import settings

log = get_logger(__name__)


@dataclass
class IncomingMail:
    uid: str
    message_id: str
    sender: str
    subject: str
    date: datetime | None
    attachments: list[Path] = field(default_factory=list)
    names: list[str] = field(default_factory=list)       # the attachments' original file names, same order
    owner_email: str | None = None                       # the staff member who sent it (None = a bank wrote directly)


def sender_allowed(address: str, allowed: list[str]) -> bool:
    """Whitelist check. Entries: full address, '@domain' or 'domain'. Empty whitelist rejects all."""
    address = address.lower().strip()
    for entry in allowed:
        if "@" in entry and not entry.startswith("@"):
            if address == entry:
                return True
        else:
            domain = entry.lstrip("@")
            if address.endswith("@" + domain):
                return True
    return False


def is_user_sender(address: str) -> bool:
    """A staff address of the signed-in domain (ALLOWED_EMAIL_DOMAIN): their mail belongs to them."""
    domain = settings.allowed_email_domain
    address = address.lower().strip()
    return bool(domain) and address.endswith("@" + domain) and address != settings.imap_user.lower()


def accepted_sender(address: str) -> bool:
    """Only staff addresses: every entry belongs to the person who sent it. Bank mail is no longer read directly."""
    return is_user_sender(address)


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_checked: set[str] = set()      # uids already looked at and not wanted (not a staff/bank sender, DMARC fail, done)


def extract_pdf_attachments(msg) -> list[tuple[str, bytes]]:
    """Every attachment whose bytes really are a PDF (the file name/extension is not trusted)."""
    out = []
    for part in msg.walk():
        if part.is_multipart() or not part.get_filename():
            continue
        data = part.get_payload(decode=True)
        if data and data[:4] == b"%PDF":
            out.append((part.get_filename(), data))
    return out


def _is_xlsx(data: bytes) -> bool:
    if data[:4] != b"PK\x03\x04":
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return "xl/workbook.xml" in z.namelist()
    except zipfile.BadZipFile:
        return False


def extract_attachments(msg) -> list[tuple[str, bytes]]:
    """PDFs plus Excel workbooks (EBL realization sheets), judged by content, not by file name."""
    out = list(extract_pdf_attachments(msg))
    for part in msg.walk():
        if part.is_multipart() or not part.get_filename():
            continue
        data = part.get_payload(decode=True)
        if data and _is_xlsx(data):
            out.append((part.get_filename(), data))
    return out


def save_attachments(message_id: str, attachments: list[tuple[str, bytes]], staging_dir: Path) -> list[Path]:
    staging_dir.mkdir(parents=True, exist_ok=True)
    tag = hashlib.sha1(message_id.encode()).hexdigest()[:10]
    paths = []
    for i, (name, data) in enumerate(attachments):
        safe = re.sub(r"[^\w.\-]+", "_", Path(name).name) or "attachment.pdf"
        path = staging_dir / f"{tag}_{i}_{safe}"
        path.write_bytes(data)
        paths.append(path)
    return paths


class EmailAgent:
    def __init__(self):
        self.conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self) -> None:
        if not (settings.imap_user and settings.imap_password):
            raise CustomException("IMAP_USER / IMAP_PASSWORD are not set")
        try:
            self.conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
            self.conn.login(settings.imap_user, settings.imap_password)
            self.conn.select(settings.imap_folder)  # read-write so processed mail can be marked \Seen
        except imaplib.IMAP4.error as e:
            raise CustomException("IMAP login/select failed", e)

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
                self.conn.logout()
            except Exception:
                pass
            self.conn = None

    def _since_uids(self) -> list[str]:
        """Every mail of the last IMAP_LOOKBACK_DAYS days, seen or unseen: whether it was read does not matter."""
        d = datetime.now() - timedelta(days=max(1, settings.imap_lookback_days))
        since = f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"
        typ, data = self.conn.uid("SEARCH", None, "SINCE", since)
        if typ != "OK" or not data or not data[0]:
            return []
        return sorted((u.decode() for u in data[0].split()), key=int)

    def _uidvalidity(self) -> str:
        try:
            return self.conn.response("UIDVALIDITY")[1][0].decode()
        except Exception:
            return "0"

    def _header(self, uid: str):
        typ, data = self.conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM MESSAGE-ID)])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return None
        return email.message_from_bytes(data[0][1], policy=policy.default)

    def fetch_new(self):
        """Yield IncomingMail for every not-yet-processed mail from a staff address.

        Seen/unseen is ignored. Nothing is marked: a mail is done once its Message-ID is in processed_emails."""
        if not settings.allowed_email_domain:
            log.warning("ALLOWED_EMAIL_DOMAIN is empty: ignoring all mail")
            return
        validity = self._uidvalidity()
        for uid in self._since_uids():
            key = f"{validity}:{uid}"
            if key in _checked:
                continue
            head = self._header(uid)
            if head is None:
                continue
            sender = parseaddr(str(head.get("From", "")))[1].lower()
            if not accepted_sender(sender):
                _checked.add(key)
                continue
            hmid = str(head.get("Message-ID") or "").strip()
            if hmid and database.email_already_processed(hmid):
                _checked.add(key)
                continue
            typ, data = self.conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            raw = data[0][1]
            msg = email.message_from_bytes(raw, policy=policy.default)
            auth = str(msg.get("Authentication-Results", "")).lower()
            if settings.reject_dmarc_fail and "dmarc=fail" in auth:
                log.warning("uid %s: DMARC fail for %s, ignored (possible spoof)", uid, sender)
                _checked.add(key)
                continue
            mid = hmid or "sha1-" + hashlib.sha1(raw).hexdigest()
            try:
                date = parsedate_to_datetime(str(msg.get("Date"))) if msg.get("Date") else None
            except (TypeError, ValueError):
                date = None
            atts = extract_attachments(msg)
            paths = save_attachments(mid, atts, settings.staging_dir)
            yield IncomingMail(uid, mid, sender, str(msg.get("Subject", "")), date, paths,
                               [n for n, _ in atts], sender)

    def mark_seen(self, uid: str) -> None:
        self.conn.uid("STORE", uid, "+FLAGS", r"(\Seen)")
