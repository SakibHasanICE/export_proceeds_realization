"""ERP automation (Playwright): fills the Export Proceeds Realization form. It NEVER clicks Save.

Used by the web UI through app/erp_worker/runner.py (run_entry), or from the command line:
    docker compose exec app python -m app.erp_worker.erp_data 18

Needs in .env:
    ERP_URL, ERP_USERNAME, ERP_PASSWORD
Set by docker-compose:
    ERP_HEADLESS, ERP_STATE_FILE, ERP_SHOT_DIR
"""
import builtins
import contextlib
import difflib
import os
import re
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

load_dotenv()

ERP_URL = os.getenv("ERP_URL", "http://192.168.21.238/erp/login.php")
ERP_USERNAME = os.getenv("ERP_USERNAME")
ERP_PASSWORD = os.getenv("ERP_PASSWORD")
HEADLESS = os.getenv("ERP_HEADLESS", "true").lower() == "true"
STATE_FILE = Path(os.getenv("ERP_STATE_FILE", "erp_state.json"))
SHOT_DIR = Path(os.getenv("ERP_SHOT_DIR", "erp_screens"))
RUN_TIMEOUT_S = int(os.getenv("ERP_RUN_TIMEOUT", "300"))          # one attempt may not take longer than this
DISCOVER_HEADS = os.getenv("ERP_DISCOVER_HEADS", "false").lower() == "true"   # debug: list the ERP's heads


# ---------------------------------------------------------------------------
# Reporting: every message / step / screenshot goes through REPORT, so the web UI can show a live
# tracker. The default reporter just prints (command-line use).
# ---------------------------------------------------------------------------
STEPS = [
    ("validate", "Check the data before starting"),
    ("browser", "Start the browser and log in to the ERP"),
    ("open_form", "Open Export Proceeds Realization"),
    ("header", "Select the Beneficiary"),
    ("bill", "Find and pick the Bill / Invoice"),
    ("received", "Set the Received Date"),
    ("deductions", "Fill Deductions at Source"),
    ("distributions", "Fill Distributions"),
    ("verify", "Verify every filled value"),
    ("screenshot", "Take the final screenshot (nothing is saved)"),
]


class Reporter:
    """Console reporter. runner.JobReporter subclasses this to feed the web UI."""

    def log(self, msg: str, level: str = "info") -> None:
        builtins.print(msg)

    def step(self, key: str, status: str, msg: str = "") -> None:   # status: running | done | failed | warn
        if status != "done":
            builtins.print(f"[{key}] {status} {msg}".rstrip())

    def shot(self, name: str, path: Path) -> None:
        pass

    def cancelled(self) -> bool:
        return False

    def reset_steps(self) -> None:
        pass


REPORT: Reporter = Reporter()


def set_reporter(r: Reporter | None) -> None:
    global REPORT
    REPORT = r or Reporter()


def _auto_level(msg: str) -> str:
    low = msg.lower()
    if "[browser alert]" in low or "fixing " in low or "check (erp changed" in low or low.strip().startswith("note:"):
        return "warn"
    if "left blank" in low or "kept clearing" in low or "no match" in low:
        return "warn"
    if msg.startswith("  "):
        return "detail"       # technical line: hidden in the UI unless "technical details" is on
    return "info"


def say(msg: str = "", level: str | None = None) -> None:
    REPORT.log(str(msg), level or _auto_level(str(msg)))


class ErpError(Exception):
    """A problem we understand, with a plain-language explanation for the user."""

    def __init__(self, code: str, message: str, hint: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.hint, self.details = code, message, hint, details or {}


class ErpCancelled(Exception):
    pass


def screenshot(page: Page, name: str) -> Path:
    """Save a screenshot so you can see what the headless browser saw."""
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOT_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{name}.png"
    # The ERP form sits in an iframe with its own scrollbar, so a plain full_page shot cuts off the
    # bottom. Grow every iframe to its content height for the shot, then put it back.
    grow_js = """
    () => {
      const saved = [];
      document.querySelectorAll('iframe').forEach(f => {
        try {
          const h = f.contentDocument.documentElement.scrollHeight;
          saved.push([f, f.style.height]);
          if (h > f.clientHeight) f.style.height = h + 'px';
        } catch (e) {}
      });
      window.__rbSaved = saved;
    }
    """
    restore_js = "() => { (window.__rbSaved || []).forEach(([f, h]) => { f.style.height = h; }); }"
    try:
        page.evaluate(grow_js)
        page.wait_for_timeout(300)
    except Exception:
        pass
    page.screenshot(path=str(path), full_page=True)
    try:
        page.evaluate(restore_js)
    except Exception:
        pass
    say(f"  Screenshot saved: {path}")
    REPORT.shot(name, path)
    return path


def _first_visible(page: Page, selectors: list[str], timeout: int = 5000):
    """Try several selectors, return the first one that appears."""
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except PWTimeout:
            continue
    raise RuntimeError(f"None of these selectors were found: {selectors}")


def is_logged_in(page: Page) -> bool:
    # After login the header shows "Logged in as ..." and a Logout button
    try:
        page.get_by_text("Logout").first.wait_for(state="visible", timeout=4000)
        return True
    except PWTimeout:
        return False


def login(page: Page) -> None:
    if not ERP_USERNAME or not ERP_PASSWORD:
        raise ErpError("CONFIG", "ERP_USERNAME / ERP_PASSWORD are not set.",
                       "Add them to the .env file next to docker-compose.yml, then run: docker compose up -d")

    page.goto(ERP_URL, wait_until="domcontentloaded")

    try:
        user_box = _first_visible(page, [
            'input[placeholder="User Name"]',
            'input[name="username"]',
            'input[name="user_name"]',
            'input[type="text"]',
        ])
        pass_box = _first_visible(page, [
            'input[placeholder="Password"]',
            'input[name="password"]',
            'input[type="password"]',
        ])

        user_box.fill(ERP_USERNAME)
        pass_box.fill(ERP_PASSWORD)

        login_btn = _first_visible(page, [
            'button:has-text("Login")',
            'input[type="submit"]',
            'text=Login',
        ])
        login_btn.click()

        page.wait_for_load_state("networkidle")
        if not is_logged_in(page):
            raise ErpError("LOGIN_FAILED", "The ERP did not accept the login.",
                           "Check ERP_USERNAME / ERP_PASSWORD in .env (has the ERP password changed?).")
    except ErpError:
        screenshot(page, "login_failed")
        raise
    except Exception as e:
        screenshot(page, "login_failed")
        if "None of these selectors" in str(e):
            raise ErpError("ERP_UNREACHABLE", "The ERP login page did not appear.",
                           f"Check that the ERP ({ERP_URL}) is reachable from this machine (VPN / network). "
                           "If it opens fine in your browser, the login page may have changed: open the screenshot.")
        raise


def get_logged_in_context(p) -> tuple[BrowserContext, Page]:
    """Reuse the saved session if still valid, otherwise log in fresh."""
    browser = p.chromium.launch(
        headless=HEADLESS,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )

    if STATE_FILE.exists():
        context = browser.new_context(storage_state=str(STATE_FILE), viewport={"width": 1280, "height": 1000})
        page = context.new_page()
        page.goto(ERP_URL.replace("login.php", "index.php"), wait_until="domcontentloaded")
        if is_logged_in(page):
            say("Reused saved session")
            return context, page
        context.close()

    context = browser.new_context(viewport={"width": 1280, "height": 1000})
    page = context.new_page()
    login(page)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(STATE_FILE))
    say("Logged in and saved session")
    return context, page


# ---------------------------------------------------------------------------
# Step 2: navigate to Commercial > Export > Export Proceeds Realization
# ---------------------------------------------------------------------------
FORM_TITLE = "Export Proceeds Realization Entry"


def _find_text(page: Page, text: str, timeout: int = 8000):
    """Look for visible text on the page and inside any iframe.

    Returns (frame_or_page, locator) so later steps can keep working in the
    same frame the form lives in.
    """
    deadline = timeout
    step = 500
    waited = 0
    while waited <= deadline:
        for frame in page.frames:
            loc = frame.get_by_text(text, exact=False).first
            try:
                if loc.is_visible():
                    return frame, loc
            except Exception:
                pass
        page.wait_for_timeout(step)
        waited += step
    return None, None


def open_export_proceeds_form(page: Page):
    """Open the Export Proceeds Realization entry form.

    Returns the page/frame that contains the form.
    """
    say("Opening Commercial menu")
    frame, tab = _find_text(page, "Commercial")
    if not tab:
        screenshot(page, "nav_no_commercial")
        raise ErpError("MENU_NOT_FOUND", "The 'Commercial' menu was not found in the ERP.",
                       "The ERP screen may have changed or the session expired. Open the screenshot to see what the robot saw.")
    tab.click()
    page.wait_for_load_state("networkidle")

    # Left menu: only click "Export" if the target item is not already showing
    frame, item = _find_text(page, "Export Proceeds Realization", timeout=2000)
    if not item:
        say("Expanding Export group")
        frame, grp = _find_text(page, "Export", timeout=4000)
        if not grp:
            screenshot(page, "nav_no_export_group")
            raise ErpError("MENU_NOT_FOUND", "The 'Export' menu group was not found in the ERP.",
                           "Open the screenshot to see what the robot saw.")
        grp.click()
        frame, item = _find_text(page, "Export Proceeds Realization", timeout=4000)
        if not item:
            screenshot(page, "nav_no_menu_item")
            raise ErpError("MENU_NOT_FOUND", "The menu item 'Export Proceeds Realization' was not found.",
                           "The robot's ERP user may not have permission for this screen.")

    say("Opening Export Proceeds Realization")
    item.click()
    page.wait_for_load_state("networkidle")

    ctx, title = _find_text(page, FORM_TITLE, timeout=10000)
    if not title:
        screenshot(page, "nav_form_not_loaded")
        raise ErpError("FORM_NOT_LOADED", "The Export Proceeds Realization form did not load.",
                       "The ERP may be slow. Try again; if it keeps happening, open the screenshot.")

    in_frame = ctx is not page.main_frame
    say(f"Form is open (inside iframe: {in_frame})")
    if in_frame:
        say(f"Frame URL: {ctx.url}")
    return ctx


# ---------------------------------------------------------------------------
# Step 3 (temporary): list every field in the form so selectors come from facts
# ---------------------------------------------------------------------------
_DUMP_JS = """
() => {
  const out = [];
  const els = document.querySelectorAll('input, select, textarea, button');
  els.forEach((e, i) => {
    const r = e.getBoundingClientRect();
    const row = {
      i, tag: e.tagName.toLowerCase(), type: e.type || '',
      id: e.id || '', name: e.name || '', cls: (e.className || '').toString().slice(0, 40),
      ph: e.placeholder || '', val: (e.value || '').toString().slice(0, 30),
      ro: !!e.readOnly, dis: !!e.disabled, vis: r.width > 0 && r.height > 0,
      list: e.getAttribute('list') || '',
      onclick: (e.getAttribute('onclick') || '').slice(0, 60),
      ondbl: (e.getAttribute('ondblclick') || '').slice(0, 60),
      onchange: (e.getAttribute('onchange') || '').slice(0, 60),
      label: ''
    };
    if (e.id) {
      const l = document.querySelector('label[for="' + e.id + '"]');
      if (l) row.label = l.innerText.trim().slice(0, 30);
    }
    if (e.tagName === 'SELECT') {
      row.nopts = e.options.length;
      row.opts = Array.from(e.options).slice(0, 6).map(o => o.text.trim().slice(0, 40));
    }
    out.push(row);
  });
  return out;
}
"""


def dump_form_fields(ctx) -> Path:
    """Write all inputs/selects/buttons of the form frame to a JSON file."""
    import json

    rows = ctx.evaluate(_DUMP_JS)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOT_DIR / "form_fields.json"
    path.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")

    say(f"\n{len(rows)} elements found (visible only shown):")
    for r in rows:
        if not r["vis"]:
            continue
        extra = ""
        if r["tag"] == "select":
            extra = f" options={r.get('nopts')} first={r.get('opts')}"
        if r["ondbl"]:
            extra += f" ondbl={r['ondbl']}"
        if r["onclick"]:
            extra += f" onclick={r['onclick']}"
        say(
            f"#{r['i']} <{r['tag']} {r['type']}> id={r['id']!r} name={r['name']!r} "
            f"ph={r['ph']!r} ro={r['ro']} val={r['val']!r}{extra}"
        )
    say(f"\nSaved: {path}")
    return path

# ---------------------------------------------------------------------------
# Step 4: fill header fields (Beneficiary, Buyer, Received Date)
# ---------------------------------------------------------------------------
def _norm(name: str | None) -> str:
    """Normalise company names so 'X LIMITED' matches 'X LTD.'."""
    s = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper())
    s = re.sub(r"\bLIMITED\b", "LTD", s)
    return re.sub(r"\s+", " ", s).strip()


def _options(ctx, selector: str) -> list[dict]:
    return ctx.eval_on_selector_all(
        f"{selector} option",
        "els => els.map(e => ({v: e.value, t: e.textContent.trim()}))",
    )


def _closest(opts: list[dict], wanted: str, n: int = 5) -> list[str]:
    """Rough 'did you mean' list for an unmatched name."""
    stop = {"LTD", "THE"}
    w = set(_norm(wanted).split()) - stop

    def score(o):
        t = set(_norm(o["t"]).split()) - stop
        jac = len(w & t) / len(w | t) if (w | t) else 0
        return jac + 0.5 * difflib.SequenceMatcher(None, _norm(wanted), _norm(o["t"])).ratio()

    return [o["t"] for o in sorted(opts, key=score, reverse=True)[:n]]


def select_by_name(ctx, selector: str, wanted: str, label: str) -> str | None:
    """Pick the ERP <option> for a name from our database.

    Returns the chosen ERP text, or None when nothing matched (a warning is printed).
    """
    mapped = wanted
    opts = [o for o in _options(ctx, selector) if o["v"] not in ("", "0")]
    hits = [o for o in opts if _norm(o["t"]) == _norm(mapped)]
    if len(hits) > 1:
        raise ErpError("AMBIGUOUS_MATCH", f"{label}: {len(hits)} ERP options match {mapped!r}.",
                       "Rename one of them in the ERP or tell the developer: " + ", ".join(o["t"] for o in hits[:5]))
    via = ""
    if not hits:
        # Fallback: ERP name is the leading whole words of our name
        # ('RAW GROUP' for 'RAW GROUP GLOBAL SOURCING LIMITED'); longest wins, ties stop.
        w_tokens = _norm(mapped).split()
        pref = []
        for o in opts:
            o_tokens = _norm(o["t"]).split()
            if o_tokens and len(_norm(o["t"])) >= 4 and w_tokens[:len(o_tokens)] == o_tokens:
                pref.append((len(o_tokens), o))
        if pref:
            best = max(n for n, _ in pref)
            top = [o for n, o in pref if n == best]
            if len(top) == 1:
                hits = top
                via = " (via prefix, verify)"
            else:
                say(f"  {label}: prefix is ambiguous for {wanted!r}: {[o['t'] for o in top]}")
    if not hits:
        say(f"  {label}: NO MATCH for {wanted!r}")
        say(f"    closest ERP names: {_closest(opts, mapped)}")
        return None
    ctx.select_option(selector, value=hits[0]["v"])
    shown = ctx.eval_on_selector(selector, "e => e.options[e.selectedIndex].text.trim()")
    if shown != hits[0]["t"]:
        raise ErpError("SELECT_FAILED", f"{label}: expected {hits[0]['t']!r}, the ERP shows {shown!r}.",
                       "The ERP did not accept the selection. Try again.")
    say(f"  {label}: {wanted!r} -> {shown!r}{via}  ok")
    return shown


def fill_beneficiary(ctx, page: Page, payload: dict) -> str:
    chosen = select_by_name(ctx, "#cbo_beneficiary_name", payload["Beneficiary"], "Beneficiary")
    if chosen is None:
        close = _closest([o for o in _options(ctx, "#cbo_beneficiary_name") if o["v"] not in ("", "0")],
                         payload["Beneficiary"])
        raise ErpError("BENEFICIARY_NOT_FOUND",
                       f"Beneficiary '{payload['Beneficiary']}' is not in the ERP's list.",
                       "Closest ERP names: " + (", ".join(close) or "none") +
                       ". If one of them is the same company, add the mapping or fix the name in the database.",
                       {"closest": close})
    page.wait_for_timeout(800)  # let the ERP finish loading after the beneficiary change
    return chosen


def fill_header(ctx, page: Page, payload: dict) -> None:
    say("Filling header")
    fill_beneficiary(ctx, page, payload)

    # Buyer, LC/SC No, Bill/Invoice Date, Currency and Amount are NOT filled here: the ERP fills them
    # itself when the bill is picked in the Bill/Invoice No search (double-click on the bill row).
    fill_received_date(ctx, payload)


def fill_received_date(ctx, payload: dict) -> None:
    received = payload.get("Received Date")
    if not received:
        raise ErpError("VALIDATION", "Received Date is empty in the database entry.", "Fix the entry first.")
    ctx.fill("#txt_received_date", received)
    ctx.press("#txt_received_date", "Tab")
    shown = ctx.input_value("#txt_received_date")
    if shown != received:
        raise ErpError("DATE_REJECTED", f"Received Date: we typed {received}, the ERP shows '{shown}'.",
                       "The ERP may restrict this date (closed period?). Check it by hand.")
    say(f"  Received Date: {received}  ok")


def _find_invoice_popup(page: Page):
    for f in page.frames:
        if "invoice_bill_popup_search" in f.url:
            return f
    return None


# Data rows of the result list: first cell is the running number (SL), 5+ cells.
# The header row ("SL", "Bill/ Invoice No", ...) has a non-numeric first cell, so it is skipped.
_ROWS_JS = """
() => Array.from(document.querySelectorAll('tr'))
  .filter(tr => !tr.querySelector('select,input'))
  .map(tr => Array.from(tr.querySelectorAll('td')).map(td => td.innerText.replace(/\\s+/g, ' ').trim()))
  .filter(c => c.length >= 5 && /^\\d+$/.test(c[0]))
"""


def _popup_rows(popup) -> list[list[str]]:
    """Data rows currently shown in the dialog (also looks inside child frames)."""
    rows: list[list[str]] = []
    for fr in [popup] + list(popup.child_frames):
        try:
            rows += fr.evaluate(_ROWS_JS)
        except Exception:
            pass
    return rows


def _digits(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def search_invoice_popup(ctx, page: Page, bill_no: str, buyer_hint: str = ""):
    """Open the Bill/Invoice search dialog and try several searches until rows show up.

    Temporary discovery step: it does NOT pick a row yet.
    Tries, in order: Bill No = full number, Bill No = empty (lists everything for the buyer),
    Invoice No = full number, Bill No = last 6 characters.
    """
    say("Opening Bill/Invoice search")
    ctx.dblclick("#txt_invoice_bill_no")
    popup = None
    for _ in range(20):
        popup = _find_invoice_popup(page)
        if popup:
            break
        page.wait_for_timeout(500)
    if not popup:
        screenshot(page, "invoice_popup_missing")
        raise ErpError("DIALOG_NOT_OPEN", "The Bill/Invoice search window did not open.",
                       "Usually a slow ERP; try again. Open the screenshot to check.")
    popup.wait_for_selector("#txt_search_common", timeout=8000)

    buyer = popup.eval_on_selector("#cbo_buyer_name", "e => e.options[e.selectedIndex].text.trim()")
    say(f"  Dialog buyer: {buyer!r}")

    opts = _options(popup, "#cbo_search_by")
    say(f"  'Search By' options: {[o['t'] for o in opts]}")

    def pick_search_by(keyword: str) -> bool:
        hit = [o for o in opts if keyword in o["t"].upper()]
        if not hit:
            say(f"  No 'Search By' option containing {keyword!r}")
            return False
        popup.select_option("#cbo_search_by", value=hit[0]["v"])
        say(f"  Search By -> {hit[0]['t']!r}")
        return True

    def run_search(by: str, text: str, label: str) -> list[list[str]]:
        if not pick_search_by(by):
            return []
        popup.fill("#txt_search_common", text)
        say(f"  [{label}] searching {by!r} for {text!r}")
        popup.click("#search_button")
        rows: list[list[str]] = []
        for _ in range(10):  # up to 5 seconds
            page.wait_for_timeout(500)
            rows = _popup_rows(popup)
            if rows:
                break
        screenshot(page, f"invoice_search_{label}")
        say(f"  [{label}] data rows found: {len(rows)}")
        for r in rows[:15]:
            say(f"  [{label}] row: {r}")
        if len(rows) > 15:
            say(f"  [{label}] ... {len(rows) - 15} more rows not shown")
        return rows

    tail = _digits(bill_no)[-6:]
    attempts = [
        ("BILL", bill_no, "by_bill"),
        ("BILL", "", "all"),
        ("INVOICE", bill_no, "by_invoice_no"),
        ("BILL", tail, "by_tail"),
    ]
    found: list[list[str]] = []
    for by, text, label in attempts:
        rows = run_search(by, text, label)
        if not rows:
            continue
        found = rows
        want = _digits(bill_no)
        near = [r for r in rows if want and (want in _digits(r[1]) or _digits(r[1]) in want)]
        say(f"  Rows whose bill number matches {bill_no!r}: {len(near)}")
        for r in near[:5]:
            say(f"    match: {r}")
        break
    else:
        say("  No attempt produced any rows. Check the screenshots in the screens folder.")

    # Bill not under the dialog's buyer: try the other ERP buyers that start with the same word
    want = _digits(bill_no)
    if not any(_digits(r[1]) == want for r in found) and buyer_hint:
        first = (_norm(buyer_hint).split() or [""])[0]
        cur = popup.eval_on_selector("#cbo_buyer_name", "e => e.options[e.selectedIndex].value")
        related = [o for o in _options(popup, "#cbo_buyer_name")
                   if o["v"] not in ("", "0", cur) and _norm(o["t"]).split()[:1] == [first]]
        say(f"  Other ERP buyers starting with {first!r}: {[o['t'] for o in related]}")
        for n, o in enumerate(related, 1):
            popup.select_option("#cbo_buyer_name", value=o["v"])
            page.wait_for_timeout(800)
            rows = run_search("BILL", bill_no, f"buyer{n}")
            if any(_digits(r[1]) == want for r in rows):
                found = rows
                say(f"  Bill found under ERP buyer {o['t']!r}")
                break
            if not rows:
                rows = run_search("BILL", "", f"buyer{n}_all")
                if any(_digits(r[1]) == want for r in rows):
                    found = rows
                    say(f"  Bill found under ERP buyer {o['t']!r}")
                    break
    return popup, found


def pick_invoice_row(ctx, page: Page, popup, rows: list[list[str]], bill_no: str) -> list[str]:
    """Double-click the row whose Bill/Invoice No equals bill_no (exact match only)."""
    want = _digits(bill_no)
    exact = [r for r in rows if _digits(r[1]) == want]
    if len(exact) == 0:
        raise ErpError(
            "BILL_NOT_FOUND",
            f"Bill {bill_no} is not in the ERP.",
            "Enter this bill in the ERP first (Commercial > Export > Export Invoice), then run this entry again. "
            "Nothing was picked, so no wrong bill can be filled.",
            {"erp_bills_seen": [r[1] for r in rows][:20]},
        )
    if len(exact) > 1:
        raise ErpError("BILL_AMBIGUOUS", f"Bill {bill_no} appears {len(exact)} times in the ERP list.",
                       "Two ERP bills share this number; resolve it in the ERP.",
                       {"rows": [r for r in exact][:5]})
    row = exact[0]
    say(f"Picking ERP bill {row[1]!r} (double-click)")
    popup.locator("tr", has_text=row[1]).first.dblclick()
    for _ in range(20):
        page.wait_for_timeout(500)
        if _find_invoice_popup(page) is None:
            break
    else:
        screenshot(page, "invoice_popup_still_open")
        raise ErpError("BILL_PICK_FAILED", "The bill row was double-clicked but the search window stayed open.",
                       "Try again; if it repeats, open the screenshot.")
    page.wait_for_timeout(1500)  # form fills LC/SC, currency, date, amount
    say(f"  Bill/Invoice No field now: {ctx.input_value('#txt_invoice_bill_no')!r}")
    return row


# ---------------------------------------------------------------------------
# Step 5: Deductions at Source + Distributions (no field ids needed:
# inputs are found by their position between the section headings)
# ---------------------------------------------------------------------------
_TAG_JS = """
([startText, endText, prefix]) => {
  document.querySelectorAll('[data-rb-' + prefix + '-in],[data-rb-' + prefix + '-plus]').forEach(e => {
    e.removeAttribute('data-rb-' + prefix + '-in'); e.removeAttribute('data-rb-' + prefix + '-plus');
  });
  const seen = {start: false, end: false};
  let inside = false, nIn = 0, nPlus = 0;
  for (const e of document.querySelectorAll('*')) {
    // heading = element whose OWN text (not its children's) is the heading, tolerant of spaces/icons
    let own = '';
    for (const n of e.childNodes) if (n.nodeType === 3) own += n.textContent;
    own = own.replace(/[\\u00a0\\s]+/g, ' ').trim().toLowerCase();
    if (own) {
      const isHead = (h) => { h = h.toLowerCase(); return own === h || (own.includes(h) && own.length <= h.length + 6); };
      if (!inside && isHead(startText)) { inside = true; seen.start = true; continue; }
      if (inside && isHead(endText)) { seen.end = true; break; }
    }
    if (!inside) continue;
    const r = e.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const tag = e.tagName.toLowerCase();
    if (tag === 'input' && !['button', 'submit', 'hidden', 'checkbox', 'radio'].includes(e.type)) {
      e.setAttribute('data-rb-' + prefix + '-in', String(nIn++));
    } else if ((tag === 'button' || (tag === 'input' && e.type === 'button'))) {
      const label = ((e.value || e.textContent) || '').trim();
      if (label === '+') e.setAttribute('data-rb-' + prefix + '-plus', String(nPlus++));
    }
  }
  return {nIn, nPlus, ...seen};
}
"""


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, Decimal):
        return format(v, "f")
    return str(v)


def _same_number(a: str, b: str) -> bool:
    try:
        return abs(float(a.replace(",", "")) - float(b.replace(",", ""))) < 0.011
    except ValueError:
        return a.strip() == b.strip()


def _tag_section(ctx, start: str, end: str, prefix: str) -> dict:
    info = ctx.evaluate(_TAG_JS, [start, end, prefix])
    if not info["start"] or not info["end"]:
        texts = ctx.evaluate(
            "() => Array.from(document.querySelectorAll('td,th,div,span,b,h1,h2,h3,h4,legend,label'))"
            ".filter(e => e.children.length === 0 && /deduct|distrib|grand/i.test(e.textContent))"
            ".map(e => e.tagName + ':' + JSON.stringify(e.textContent.trim().slice(0, 50)))"
        )
        say(f"  heading-like elements: {texts}")
        raise ErpError("FORM_LAYOUT_CHANGED", f"Could not find the '{start}' table on the ERP form.",
                       "The ERP screen layout may have changed. Tell the developer.", {"info": info})
    return info


def _ensure_rows(ctx, page: Page, start: str, end: str, prefix: str, cols: int, rows_needed: int) -> int:
    """Click '+' until the section has enough rows. Returns the row count."""
    for _ in range(rows_needed + 2):
        info = _tag_section(ctx, start, end, prefix)
        n_rows, rest = divmod(info["nIn"] - 2, cols)  # last 2 inputs are the Total row
        if rest != 0:
            screenshot(page, f"{prefix}_layout_unexpected")
            raise ErpError("FORM_LAYOUT_CHANGED", f"The '{start}' table has an unexpected number of boxes.",
                           "The ERP screen layout may have changed. Tell the developer.", {"inputs": info["nIn"]})
        if n_rows >= rows_needed:
            return n_rows
        if info["nPlus"] == 0:
            raise ErpError("FORM_LAYOUT_CHANGED", f"No '+' button found to add a row in '{start}'.",
                           "The ERP screen layout may have changed. Tell the developer.")
        ctx.locator(f"[data-rb-{prefix}-plus='{info['nPlus'] - 1}']").click()
        page.wait_for_timeout(400)
    raise ErpError("FORM_LAYOUT_CHANGED", f"Could not add enough rows in '{start}'.",
                   "The '+' button did not add a row. Try again.")


def _show_head_options(ctx, page: Page, field, label: str) -> None:
    """Print the account heads the ERP offers (datalist or autocomplete), to build account_map.json."""
    try:
        opts = field.evaluate("e => e.list ? Array.from(e.list.options).map(o => o.value) : []")
        if opts:
            say(f"  {label}: {len(opts)} account heads in the ERP list, first 60: {opts[:60]}")
            return
        field.click()
        field.fill("a")  # most suggestion lists open after the first typed letter
        page.wait_for_timeout(900)
        items = ctx.eval_on_selector_all(
            "ul.ui-autocomplete li, .ui-menu-item, .autocomplete-suggestion, [role=option], datalist option, "
            "ul li, .suggestion, .dropdown-item",
            "els => els.filter(e => e.offsetWidth > 0 && e.textContent.trim().length > 2)"
            ".map(e => e.textContent.trim().slice(0, 80))",
        )
        items = [i for i in dict.fromkeys(items)][:60]
        say(f"  {label}: suggestions after typing 'a': {items}" if items else f"  {label}: no list seen, see screenshot")
        screenshot(page, "account_head_list")
        field.fill("")
    except Exception as e:
        say(f"  {label}: could not read account heads ({e})")


def _set_account_head(ctx, page: Page, field, head: str, label: str) -> None:
    field.click()
    field.fill(head)
    page.wait_for_timeout(600)
    item = ctx.locator("ul.ui-autocomplete li:visible, .ui-menu-item:visible").filter(has_text=head).first
    try:
        item.wait_for(state="visible", timeout=1500)
        item.click()
    except PWTimeout:
        field.press("Tab")
    shown = field.input_value()
    ok = shown.strip() == head.strip()
    say(f"  {label}: account head -> {shown!r}" + ("" if ok else "  CHECK (ERP changed it)"))
    if not ok:
        say(f"{label}: the ERP did not accept the Account Head '{head}' (it shows '{shown}'). "
            f"Check the exact name in the ERP list.", "warn")


def _popup_numbers(page: Page) -> list[str]:
    nums: list[str] = []
    for fr in page.frames:
        try:
            nums += fr.eval_on_selector_all(
                "td", "els => els.filter(e => e.offsetWidth > 0).map(e => e.textContent.trim())"
                ".filter(t => /^[0-9]{6,}$/.test(t))")
        except Exception:
            pass
    return list(dict.fromkeys(nums))


def _close_popup(page: Page) -> None:
    for fr in page.frames:
        for sel in (".ui-dialog-titlebar-close", "[title='Close']", "img[src*='close']", "[class*='close']"):
            try:
                loc = fr.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=1500)
                    return
            except Exception:
                continue
    with contextlib.suppress(Exception):
        page.keyboard.press("Escape")


def _set_ac_no(ctx, page: Page, field, ac_no: str, label: str, head: str = "") -> bool:
    """AC / Loan No. Some account heads leave the box free to type in (e.g. ERQ Account); others lock it and
    want you to double-click it and pick the account from the ERP's list (e.g. BTB Margin...).
    We type when the box is open and use the list when it is locked. Returns True when the number is in."""
    want = ac_no.strip()
    if field.is_editable():
        field.click()
        field.fill(want, timeout=5000)
        field.press("Tab")
        page.wait_for_timeout(500)
        if field.input_value().strip() == want:
            say(f"  {label}: {want!r} typed ok")
            return True
        say(f"  {label}: typed {want!r} but the ERP cleared it, trying the account list")
    else:
        say(f"  {label}: the ERP locked this box for head {head!r}, picking from its account list")
    field.dblclick()
    cell = None
    for _ in range(16):
        page.wait_for_timeout(500)
        for fr in page.frames:
            try:
                loc = fr.locator("td", has_text=want)
                if loc.count() and loc.first.is_visible():
                    cell = loc.first
                    break
            except Exception:
                continue
        if cell is not None:
            break
        if _popup_numbers(page):       # the list is open but our number is not in it
            break
    if cell is None:
        offered = _popup_numbers(page)
        _close_popup(page)
        say(f"{label}: the ERP does not offer account {want} for the head '{head}'. "
            f"It offers: {', '.join(offered) or 'nothing (the list did not open)'}. "
            "Check the account map: this account may belong to a different head.", "warn")
        return False
    cell.dblclick()
    for _ in range(10):
        page.wait_for_timeout(400)
        if field.input_value().strip() == want:
            break
    ok = field.input_value().strip() == want
    say(f"  {label}: picked {want!r} from the ERP list" if ok else f"  {label}: list click did not set it  CHECK (ERP changed it)")
    return ok


def fill_section(ctx, page: Page, title: str, end: str, prefix: str, cols: list[str], rows: list[dict],
                 keys: list[str]) -> dict:
    """Fill one section. cols = ERP column names in screen order, keys = matching payload keys.
    Returns {"rows": [{column: value shown by the ERP}], "totals": [document, domestic]}."""
    say(f"Filling {title} ({len(rows)} row(s))")
    if not rows:
        say("  nothing to fill")
        return {"rows": [], "totals": []}
    n_rows = _ensure_rows(ctx, page, title, end, prefix, len(cols), len(rows))
    if n_rows > len(rows):
        say(f"  note: the ERP already has {n_rows} rows, only the first {len(rows)} are filled")
    shown_heads = False
    ac_jobs: list[tuple[int, int, str, str]] = []
    for r, row in enumerate(rows):
        for c, (col, key) in enumerate(zip(cols, keys)):
            field = ctx.locator(f"[data-rb-{prefix}-in='{r * len(cols) + c}']")
            label = f"{title} row {r + 1} {col}"
            value = _fmt(row.get(key))
            if col == "Account Head":
                if value:
                    _set_account_head(ctx, page, field, value, label)
                else:
                    say(f"  {label}: Account Head not set - left empty (the ERP asks for it when saving)")
                    if DISCOVER_HEADS and not shown_heads:
                        _show_head_options(ctx, page, field, label)
                        shown_heads = True
                continue
            if not value:
                say(f"  {label}: EMPTY in database, left blank")
                continue
            if col == "AC / Loan No":
                row_head = _fmt(row.get("Account Head"))
                if row_head:
                    # head first (done above), then the account number, then the amounts
                    _set_ac_no(ctx, page, field, value, label, row_head)
                else:
                    ac_jobs.append((r, c, value, label))  # no head: written after the amounts, see below
                continue
            field.click()
            # paste the value, then a real key press so the ERP's keyup handlers (totals, balance)
            # run once with the complete number (typing digit by digit made it recalculate on '122.')
            field.fill(value, timeout=8000)
            field.press("End")
            field.press("Tab")  # lets the ERP run its own calculations
            page.wait_for_timeout(700)
            got = field.input_value()
            ok = _same_number(got, value)
            say(f"  {label}: {value!r} -> ERP shows {got!r}" + ("" if ok else "  CHECK (ERP changed it)"))

    # AC / Loan No: written by hand, row by row in order, AFTER the amounts. (Filling it first let the
    # ERP's "Account Head Not Found" check on the amount boxes wipe it.) No double-click popup is used.
    for r, c, value, label in ac_jobs:
        field = ctx.locator(f"[data-rb-{prefix}-in='{r * len(cols) + c}']")
        _set_ac_no(ctx, page, field, value, label, "")

    # The ERP recalculates neighbouring boxes (e.g. document = domestic / rate), so check every number
    # once more and put back any that the ERP altered.
    for _pass in range(3):
        ctx.evaluate("() => document.activeElement && document.activeElement.blur && document.activeElement.blur()")
        page.wait_for_timeout(900)  # the ERP validates on blur and may clear a box (e.g. AC / Loan No)
        fixed = 0
        for r, row in enumerate(rows):
            for c, (col, key) in enumerate(zip(cols, keys)):
                value = _fmt(row.get(key))
                if col == "Account Head" or not value:
                    continue
                field = ctx.locator(f"[data-rb-{prefix}-in='{r * len(cols) + c}']")
                got = field.input_value()
                same = got.strip() == value.strip() if col == "AC / Loan No" else _same_number(got, value)
                if not same:
                    say(f"  fixing {title} row {r + 1} {col}: ERP has {got!r}, ours {value!r}")
                    if col == "AC / Loan No":
                        if _set_ac_no(ctx, page, field, value, f"{title} row {r + 1} {col}", _fmt(row.get("Account Head"))):
                            fixed += 1
                        continue
                    field.click()
                    field.fill(value, timeout=8000)
                    field.press("End")
                    field.press("Tab")
                    page.wait_for_timeout(700)
                    fixed += 1
        if not fixed:
            break
    # Last resort for AC / Loan No boxes the ERP keeps emptying (its "Account Head Not Found" check):
    # put the number into the box without firing any ERP event, and say so. A person reviews the form
    # before saving, and the ERP will still ask for the Account Head.
    for r, row in enumerate(rows):
        for c, (col, key) in enumerate(zip(cols, keys)):
            value = _fmt(row.get(key))
            if col != "AC / Loan No" or not value:
                continue
            field = ctx.locator(f"[data-rb-{prefix}-in='{r * len(cols) + c}']")
            if not _fmt(row.get("Account Head")) and field.input_value().strip() != value.strip():
                field.evaluate("(e, v) => { e.value = v; }", value)
                say(f"  {title} row {r + 1} AC / Loan No: ERP kept clearing it, set directly WITHOUT ERP events "
                      f"(needs the Account Head to be accepted): {value!r}")
    info = _tag_section(ctx, title, end, prefix)
    read_rows: list[dict] = []
    for r in range(len(rows)):  # re-read: shows if the ERP reset anything after we filled it
        now = [ctx.locator(f"[data-rb-{prefix}-in='{r * len(cols) + c}']").input_value() for c in range(len(cols))]
        say(f"  {title} row {r + 1} now shows: {now}")
        read_rows.append(dict(zip(cols, now)))
    totals = [ctx.locator(f"[data-rb-{prefix}-in='{info['nIn'] - k}']").input_value() for k in (2, 1)]
    say(f"  {title} totals shown by ERP (document, domestic): {totals}")
    return {"rows": read_rows, "totals": totals}


DED_COLS = ["Account Head", "Document Currency", "Conversion Rate", "Domestic Currency"]
DIST_COLS = ["Account Head", "AC / Loan No", "Document Currency", "Conversion Rate", "Domestic Currency"]


def fill_deductions(ctx, page: Page, payload: dict) -> dict:
    return fill_section(ctx, page, "Deductions at Source", "Distributions", "ded", DED_COLS,
                        payload.get("Deductions at Source") or [], DED_COLS)


def fill_distributions(ctx, page: Page, payload: dict) -> dict:
    return fill_section(ctx, page, "Distributions", "Grand Total", "dist", DIST_COLS,
                        payload.get("Distributions") or [], DIST_COLS)


# ---------------------------------------------------------------------------
# Verification: compare what the ERP shows with what is in the database
# ---------------------------------------------------------------------------
_HEADER_IDS = [
    "cbo_beneficiary_name", "cbo_buyer_name", "txt_invoice_bill_no", "txt_received_date", "txt_lc_sc_no",
    "cbo_currency_name", "txt_bill_invoice_date", "txt_bill_invoice_amnt",
    "total_distribution_document_currency", "total_distribution_domestic_currency",
    "grand_total_document_currency", "cumilitive_bill_balance", "grand_total_domestic_currency",
]


def read_form_values(ctx) -> dict:
    return ctx.evaluate(
        """(ids) => { const out = {}; for (const id of ids) { const e = document.getElementById(id);
             out[id] = !e ? null : (e.tagName === 'SELECT'
               ? ((e.options[e.selectedIndex] || {}).text || '').trim() : (e.value || '').trim()); }
           return out; }""",
        _HEADER_IDS,
    )


def _num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _check(area: str, field: str, expected, erp, kind: str = "text", note: str = "") -> dict:
    """One verification line. kind: text | num | domestic | rate | info."""
    exp_s = "" if expected is None else _fmt(expected)
    erp_s = "" if erp is None else str(erp)
    status, why = "ok", note
    if kind == "info":
        status = "info"
    elif kind == "text":
        if not exp_s:
            status, why = "info", note or "not set in the database"
        elif _norm(exp_s) != _norm(erp_s) and _digits(exp_s) != _digits(erp_s):
            status = "error"
    else:
        e, g = _num(exp_s), _num(erp_s)
        if e is None:
            status, why = "info", note or "not set in the database"
        elif g is None:
            status, why = "error", "the ERP box is empty"
        else:
            diff = abs(e - g)
            tol = 0.0001 if kind == "rate" else 0.011
            if diff <= tol:
                status = "ok"
            elif kind == "domestic" and diff <= 0.1:
                status, why = "warn", "the ERP rounds this box differently (difference " + f"{diff:.2f})"
            else:
                status = "error"
    return {"area": area, "field": field, "expected": exp_s, "erp": erp_s, "status": status, "note": why}


def build_verification(payload: dict, header: dict, ded: dict, dist: dict) -> dict:
    checks: list[dict] = []
    H = lambda f, exp, erp, kind="text", note="": checks.append(_check("Header", f, exp, erp, kind, note))
    H("Beneficiary", payload.get("Beneficiary"), header.get("cbo_beneficiary_name"))
    if checks[-1]["status"] == "error" and header.get("cbo_beneficiary_name"):
        # the name was matched to the ERP's own spelling earlier in the run
        checks[-1].update(status="ok", note="matched to the ERP's spelling")
    H("Bill/Invoice No", payload.get("Bill/Invoice No"), header.get("txt_invoice_bill_no"))
    H("Received Date", payload.get("Received Date"), header.get("txt_received_date"))
    H("Currency", payload.get("Currency"), header.get("cbo_currency_name"))
    H("Bill/Invoice Amount", payload.get("Bill/Invoice Amount"), header.get("txt_bill_invoice_amnt"), "num",
      "")
    if checks[-1]["status"] == "error":
        checks[-1]["note"] = "the bill amount in the ERP differs from the amount in our database"
    H("Buyer (filled by the ERP)", payload.get("Buyer"), header.get("cbo_buyer_name"), "info",
      "the ERP fills the buyer from the bill")
    H("LC/SC No (filled by the ERP)", payload.get("LC/SC No"), header.get("txt_lc_sc_no"), "info",
      "the ERP fills this from the bill")
    H("Bill/Invoice Date (filled by the ERP)", payload.get("Bill/Invoice Date"), header.get("txt_bill_invoice_date"),
      "info", "the ERP fills this from the bill")

    def section(area: str, cols: list[str], exp_rows: list[dict], got: dict) -> None:
        for i, exp in enumerate(exp_rows):
            erp = (got.get("rows") or [])[i] if i < len(got.get("rows") or []) else {}
            for col in cols:
                f = f"Row {i + 1} - {col}"
                if col == "Account Head":
                    c = _check(area, f, exp.get(col), erp.get(col, ""), "text")
                    if not exp.get(col):
                        c["note"] = "no head set for this account (the ERP asks for it when saving)"
                elif col == "AC / Loan No":
                    c = _check(area, f, exp.get(col), erp.get(col, ""), "text")
                elif col == "Conversion Rate":
                    c = _check(area, f, exp.get(col), erp.get(col, ""), "rate")
                elif col == "Domestic Currency":
                    c = _check(area, f, exp.get(col), erp.get(col, ""), "domestic")
                else:
                    c = _check(area, f, exp.get(col), erp.get(col, ""), "num")
                checks.append(c)

    section("Deductions at Source", DED_COLS, payload.get("Deductions at Source") or [], ded)
    section("Distributions", DIST_COLS, payload.get("Distributions") or [], dist)

    gt = payload.get("Grand Total") or {}
    checks.append(_check("Totals", "Grand Total - Document Currency", gt.get("Document Currency"),
                         header.get("grand_total_document_currency"), "num"))
    bal = _check("Totals", "Cumulative Bill Balance (must be 0)", 0, header.get("cumilitive_bill_balance"), "num")
    if bal["status"] == "error":
        bal["note"] = "the bill is not fully distributed: check the bill amount and the distribution rows"
    checks.append(bal)

    # Independent balance rule, worked out from what the ERP itself shows (not from our database):
    # Bill/Invoice Amount (filled by the ERP when the bill is picked) = Deductions + Distributions (document currency)
    def _erp_total(section: dict) -> float:
        tot = (section.get("totals") or [None])[0]
        if _num(tot) is not None:
            return _num(tot)
        return sum(_num(r.get("Document Currency")) or 0.0 for r in (section.get("rows") or []))

    erp_bill = _num(header.get("txt_bill_invoice_amnt"))
    erp_sum = round(_erp_total(ded) + _erp_total(dist), 2)
    rule = _check("Totals", "Bill/Invoice Amount = Deductions + Distributions", erp_bill, f"{erp_sum:.2f}", "num")
    if erp_bill is None:
        rule.update(status="error", note="the ERP did not fill the Bill/Invoice Amount, so the balance cannot be checked")
    elif rule["status"] == "error":
        rule["note"] = (f"deductions + distributions in the ERP are {erp_sum:,.2f} but the bill amount the ERP filled in "
                        f"is {erp_bill:,.2f} (difference {erp_bill - erp_sum:,.2f})")
    checks.append(rule)

    checks.append(_check("Totals", "Grand Total - Domestic Currency", gt.get("Domestic Currency"),
                         header.get("grand_total_domestic_currency"), "domestic"))

    counts = {k: sum(1 for c in checks if c["status"] == k) for k in ("ok", "warn", "error", "info")}
    return {"checks": checks, "counts": counts, "ok": counts["error"] == 0 and counts["warn"] == 0}


# ---------------------------------------------------------------------------
# Errors -> plain language
# ---------------------------------------------------------------------------
_TITLES = {
    "VALIDATION": "The entry is not ready",
    "CONFIG": "Robot settings are missing",
    "LOGIN_FAILED": "ERP login failed",
    "MENU_NOT_FOUND": "ERP menu not found",
    "FORM_NOT_LOADED": "ERP form did not load",
    "BENEFICIARY_NOT_FOUND": "Beneficiary not found in the ERP",
    "BILL_NOT_FOUND": "Bill is not in the ERP",
    "BILL_AMBIGUOUS": "Bill found more than once",
    "BILL_PICK_FAILED": "Could not pick the bill",
    "DIALOG_NOT_OPEN": "Search window did not open",
    "DATE_REJECTED": "Received Date was rejected",
    "FORM_LAYOUT_CHANGED": "ERP screen looks different",
    "AMBIGUOUS_MATCH": "More than one ERP match",
    "SELECT_FAILED": "ERP did not accept a selection",
    "TIMEOUT": "The ERP was too slow",
    "BROWSER_CLOSED": "The browser stopped unexpectedly",
    "ERP_UNREACHABLE": "ERP cannot be reached",
    "FIELD_LOCKED": "An ERP box is locked",
    "UNEXPECTED": "Unexpected problem",
}
_RETRYABLE = {"TIMEOUT", "BROWSER_CLOSED", "ERP_UNREACHABLE", "FORM_NOT_LOADED", "DIALOG_NOT_OPEN",
              "BILL_PICK_FAILED", "MENU_NOT_FOUND"}


def explain(exc: BaseException) -> dict:
    if isinstance(exc, ErpError):
        code, msg, hint, details = exc.code, exc.message, exc.hint, exc.details
    elif isinstance(exc, PWTimeout) and "not editable" in str(exc).lower():
        code, details = "FIELD_LOCKED", {"raw": str(exc)[:400]}
        msg = "An ERP box is locked (read-only), so the robot could not type into it."
        hint = "The ERP locks some boxes depending on the Account Head. Tell the developer which row; the screenshot shows it."
    elif isinstance(exc, PWTimeout):
        code, msg, details = "TIMEOUT", "The ERP did not answer in time.", {"raw": str(exc)[:300]}
        hint = "The ERP may be busy. The robot retries once automatically; if it still fails, try again later."
    else:
        raw = str(exc)
        low = raw.lower()
        details = {"raw": raw[:500], "type": type(exc).__name__}
        if "net::err" in low or "connection refused" in low or "name_not_resolved" in low or "unreachable" in low:
            code, msg = "ERP_UNREACHABLE", "The ERP server could not be reached from the robot."
            hint = f"Check that the ERP ({ERP_URL}) is reachable from this machine (VPN / network) and try again."
        elif "has been closed" in low or "target closed" in low or "browser has been closed" in low or "crashed" in low:
            code, msg = "BROWSER_CLOSED", "The robot's browser closed or crashed."
            hint = "This is usually temporary. Try again."
        else:
            code, msg = "UNEXPECTED", raw.splitlines()[0][:300] if raw else type(exc).__name__
            hint = "Open the screenshot and the technical details; send them to the developer."
    return {"code": code, "title": _TITLES.get(code, "Problem"), "message": msg, "hint": hint,
            "retryable": code in _RETRYABLE, "details": details}


def validate_payload(payload: dict) -> list[str]:
    problems = []
    for f in ("Beneficiary", "Bill/Invoice No", "Received Date"):
        if not payload.get(f):
            problems.append(f"{f} is empty")
    dists = payload.get("Distributions") or []
    if not dists:
        problems.append("there are no distribution rows")
    for i, d in enumerate(dists, 1):
        if not d.get("AC / Loan No"):
            problems.append(f"distribution {i} has no AC / Loan No")
        if not d.get("Document Currency"):
            problems.append(f"distribution {i} has no amount")
    return problems


# ---------------------------------------------------------------------------
# One full run for one entry
# ---------------------------------------------------------------------------
def _checkpoint(t0: float) -> None:
    if REPORT.cancelled():
        raise ErpCancelled()
    if time.time() - t0 > RUN_TIMEOUT_S:
        raise ErpError("TIMEOUT", f"The run took longer than {RUN_TIMEOUT_S} seconds and was stopped.",
                       "The ERP may be very slow. Try again.")


@contextlib.contextmanager
def _step(key: str, msg: str = ""):
    REPORT.step(key, "running", msg)
    note: dict = {}
    try:
        yield note
    except ErpCancelled:
        REPORT.step(key, "skipped", "cancelled")
        raise
    except BaseException as e:
        REPORT.step(key, "failed", (getattr(e, "message", None) or str(e))[:200])
        raise
    REPORT.step(key, "done", note.get("msg", ""))


def _attempt(payload: dict, t0: float) -> dict:
    alerts: list[str] = []
    target = payload["Bill/Invoice No"]
    with sync_playwright() as p:
        with _step("browser", "Starting a headless browser and logging in to the ERP"):
            context, page = get_logged_in_context(p)
        try:
            def on_dialog(d):
                alerts.append(d.message)
                say(f"  [browser alert] {d.message!r}")
                d.accept()

            page.on("dialog", on_dialog)

            _checkpoint(t0)
            with _step("open_form", "Opening Commercial > Export > Export Proceeds Realization"):
                ctx = open_export_proceeds_form(page)

            _checkpoint(t0)
            with _step("header", f"Selecting beneficiary {payload['Beneficiary']}") as n:
                chosen = fill_beneficiary(ctx, page, payload)
                n["msg"] = f"ERP name: {chosen}"
                fill_received_date(ctx, payload)

            _checkpoint(t0)
            with _step("bill", f"Searching the ERP for bill {target}") as n:
                popup, rows = search_invoice_popup(ctx, page, target, payload.get("Buyer") or "")
                pick_invoice_row(ctx, page, popup, rows, target)
                n["msg"] = "Bill picked; the ERP filled Buyer, LC/SC No, Currency, Date and Amount"
                screenshot(page, "bill_picked")

            _checkpoint(t0)
            with _step("received", "Typing the Received Date again (the ERP clears it when a bill is picked)"):
                fill_received_date(ctx, payload)

            _checkpoint(t0)
            n_ded = len(payload.get("Deductions at Source") or [])
            with _step("deductions", f"Filling {n_ded} deduction row(s)" if n_ded else "No deductions for this entry") as n:
                ded = fill_deductions(ctx, page, payload)
                if not n_ded:
                    n["msg"] = "nothing to fill"

            _checkpoint(t0)
            n_dist = len(payload.get("Distributions") or [])
            with _step("distributions", f"Filling {n_dist} distribution row(s)"):
                dist = fill_distributions(ctx, page, payload)

            _checkpoint(t0)
            with _step("verify", "Reading every box back from the ERP and comparing with the database") as n:
                header = read_form_values(ctx)
                ver = build_verification(payload, header, ded, dist)
                c = ver["counts"]
                n["msg"] = f"{c['ok']} match, {c['warn']} warning(s), {c['error']} mismatch(es)"
                for chk in ver["checks"]:
                    if chk["status"] in ("warn", "error"):
                        say(f"{chk['area']} / {chk['field']}: database {chk['expected']!r}, ERP {chk['erp']!r}"
                            + (f" ({chk['note']})" if chk["note"] else ""),
                            "error" if chk["status"] == "error" else "warn")

            with _step("screenshot", "Saving a full-page screenshot of the filled form") as n:
                shot = screenshot(page, "final_form")
                n["msg"] = "Nothing was saved in the ERP: the browser closes without clicking Save"
        except BaseException:
            with contextlib.suppress(Exception):
                screenshot(page, "error")
            raise
        finally:
            with contextlib.suppress(Exception):
                context.browser.close()

    heads_alerts = [a for a in alerts if "account head" in a.lower()]
    if heads_alerts and all((r.get("Account Head") for r in (payload.get("Distributions") or []))):
        say(f"The ERP warned {len(heads_alerts)} time(s): {heads_alerts[0]!r} although the heads were set.", "warn")
    return {
        "outcome": "succeeded" if ver["ok"] else "attention",
        "verification": ver,
        "erp_values": header,
        "alerts": alerts,
    }


def run_entry(payload: dict, reporter: Reporter | None = None, max_attempts: int = 2) -> dict:
    """Fill the ERP form for one entry (never saves). Always returns a result dict; never raises.

    outcome: succeeded | attention (filled, but something differs) | failed | cancelled
    """
    set_reporter(reporter)
    started = time.time()

    def done(res: dict, attempts: int) -> dict:
        res["attempts"] = attempts
        res["duration_s"] = round(time.time() - started, 1)
        return res

    try:
        with _step("validate", "Checking that the entry has everything the ERP form needs") as n:
            problems = validate_payload(payload)
            if problems:
                raise ErpError("VALIDATION", "This entry cannot be sent yet: " + "; ".join(problems) + ".",
                               "Fix the entry in the database, then run it again.")
            n["msg"] = "Entry is complete"
    except ErpCancelled:
        return done({"outcome": "cancelled"}, 0)
    except Exception as e:
        err = explain(e)
        say(f"{err['title']}: {err['message']}", "error")
        return done({"outcome": "failed", "error": err}, 0)

    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            say(f"Run attempt {attempt} of {max_attempts}" if attempt > 1 else "Starting the ERP run")
            return done(_attempt(payload, time.time()), attempt)
        except ErpCancelled:
            say("Cancelled by the user. Nothing was saved in the ERP.", "warn")
            return done({"outcome": "cancelled"}, attempt)
        except Exception as e:
            err = last_err = explain(e)
            say(f"{err['title']}: {err['message']}", "error")
            if err["retryable"] and attempt < max_attempts:
                say("This looks temporary. Trying once more with a fresh browser session.", "warn")
                with contextlib.suppress(Exception):
                    STATE_FILE.unlink()      # drop a possibly stale saved login
                REPORT.reset_steps()
                time.sleep(3)
                continue
            break
    return done({"outcome": "failed", "error": last_err}, attempt)


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m app.erp_worker.erp_data <entry_id>")
    from app.components import database
    from app.erp_worker.view import build_run_payload

    rid = int(sys.argv[1])
    rec = database.get_realization(rid)
    if not rec:
        raise SystemExit(f"Entry {rid} not found in the database")
    result = run_entry(build_run_payload(rec))
    builtins.print(json.dumps({k: v for k, v in result.items() if k != "erp_values"}, indent=2, default=str)[:4000])
