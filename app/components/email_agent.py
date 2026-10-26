"""IMAP polling: find unseen mail from whitelisted bank senders and stage their PDF attachments."""
import email
import hashlib
import imaplib
import re
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from app.common.custom_exception import CustomException
from app.common.logger import get_logger
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

    def _unseen_uids(self) -> list[str]:
        uids: set[str] = set()
        for sender in settings.known_senders:
            typ, data = self.conn.uid("SEARCH", None, "UNSEEN", "FROM", f'"{sender}"')
            if typ == "OK" and data and data[0]:
                uids.update(u.decode() for u in data[0].split())
        return sorted(uids, key=int)

    def fetch_new(self):
        """Yield IncomingMail for unseen, whitelisted mail. Does NOT mark it seen (BODY.PEEK)."""
        if not settings.known_senders:
            log.warning("KNOWN_BANK_SENDERS is empty: ignoring all mail")
            return
        for uid in self._unseen_uids():
            typ, data = self.conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            raw = data[0][1]
            msg = email.message_from_bytes(raw, policy=policy.default)
            sender = parseaddr(str(msg.get("From", "")))[1].lower()
            if not sender_allowed(sender, settings.known_senders):
                log.info("uid %s: sender %s not whitelisted, ignored", uid, sender)
                continue
            auth = str(msg.get("Authentication-Results", "")).lower()
            if settings.reject_dmarc_fail and "dmarc=fail" in auth:
                log.warning("uid %s: DMARC fail for %s, ignored (possible spoof)", uid, sender)
                continue
            mid = str(msg.get("Message-ID") or "").strip() or "sha1-" + hashlib.sha1(raw).hexdigest()
            try:
                date = parsedate_to_datetime(str(msg.get("Date"))) if msg.get("Date") else None
            except (TypeError, ValueError):
                date = None
            paths = save_attachments(mid, extract_pdf_attachments(msg), settings.staging_dir)
            yield IncomingMail(uid, mid, sender, str(msg.get("Subject", "")), date, paths)

    def mark_seen(self, uid: str) -> None:
        self.conn.uid("STORE", uid, "+FLAGS", r"(\Seen)")
