"""Real-time mailbox: IMAP IDLE. The server tells us the moment a new mail arrives, so it is processed at once.

The normal scheduler keeps running as a safety net (it also catches anything missed while the connection was down).
"""
import imaplib
import threading
import time
from typing import Callable

from app.common.logger import get_logger
from app.config.settings import settings

log = get_logger(__name__)


def wait_for_new_mail(conn: imaplib.IMAP4, timeout_s: float) -> bool:
    """Sit in IDLE until the server reports new mail (True). Raises TimeoutError when nothing happened for
    timeout_s: the caller then simply reconnects (a keep-alive, servers drop idle connections after ~30 min)."""
    tag = conn._new_tag()
    conn.send(tag + b" IDLE\r\n")
    conn.sock.settimeout(timeout_s)
    deadline = time.monotonic() + timeout_s
    got = idling = False
    while True:
        line = conn.readline()                     # TimeoutError if the server stays silent
        if not idling:
            if line.startswith(b"+"):
                idling = True
                if got:                            # new mail was announced right before IDLE started
                    break
            elif line.startswith(tag):
                raise imaplib.IMAP4.error("IDLE not accepted: " + line.decode(errors="replace").strip())
            elif b"EXISTS" in line:
                got = True
            continue
        if b"EXISTS" in line:
            got = True
            break
        if time.monotonic() > deadline:
            raise TimeoutError("idle keep-alive")
    conn.sock.settimeout(60)
    conn.send(b"DONE\r\n")
    while True:                                    # read until the server closes the IDLE command
        line = conn.readline()
        if line.startswith(tag):
            break
        if b"EXISTS" in line:
            got = True
    return got


def start(on_new_mail: Callable[[], object], stop: threading.Event) -> threading.Thread:
    def loop():
        backoff = 5
        while not stop.is_set():
            conn = None
            try:
                conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
                conn.login(settings.imap_user, settings.imap_password)
                conn.select(settings.imap_folder, readonly=True)
                log.info("IMAP IDLE connected: new mail is processed as soon as it arrives")
                backoff = 5
                _safe(on_new_mail)                 # catch up on anything that came while we were disconnected
                while not stop.is_set():
                    if wait_for_new_mail(conn, settings.imap_idle_minutes * 60):
                        stop.wait(1)               # let the message finish arriving
                        _safe(on_new_mail)
            except TimeoutError:
                pass                               # keep-alive: reconnect straight away
            except Exception as e:
                log.warning("IMAP IDLE connection lost: %s (retrying in %ss)", e, backoff)
                stop.wait(backoff)
                backoff = min(backoff * 2, 300)
            finally:
                if conn is not None:
                    try:
                        conn.shutdown()
                    except Exception:
                        pass

    t = threading.Thread(target=loop, name="imap-idle", daemon=True)
    t.start()
    return t


def _safe(fn) -> None:
    try:
        fn()
    except Exception:
        log.exception("mailbox cycle failed")
