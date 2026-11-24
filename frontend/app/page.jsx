"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import EntriesTable from "../components/EntriesTable";
import ErpLoginButton from "../components/ErpLoginButton";
import RunTracker from "../components/RunTracker";
import StatusGuide from "../components/StatusGuide";
import UploadButton from "../components/UploadButton";
import UserMenu from "../components/UserMenu";
import { uploadFiles } from "../lib/upload";
import { api, post, put } from "../lib/api";
import { ACTIVE } from "../lib/format";

function skippedShown(n) {
  return n > 0 ? ` (${n} not ready, will be skipped)` : "";
}

export default function Page() {
  const [entries, setEntries] = useState([]);
  const [worker, setWorker] = useState({ busy: false, queued: 0 });
  const [erpConfigured, setErpConfigured] = useState(true);
  const [saveEnabled, setSaveEnabled] = useState(false); // true = the robot clicks Save in the ERP (ERP_SAVE_ENABLED)
  const [viewer, setViewer] = useState({ email: null, is_master: false }); // is_master: sees every user's data
  const [owners, setOwners] = useState([]);
  const [erpLogin, setErpLogin] = useState({ mode: "shared", username: null }); // this user's own ERP login (personal | shared | missing)
  const [ownerFilter, setOwnerFilter] = useState("all");
  const [loadError, setLoadError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState("all");
  const [search, setSearch] = useState("");
  const [dSearch, setDSearch] = useState("");           // the search text the server is asked for (after a short pause)
  const [page, setPage] = useState(1);                  // 100 entries per page
  const [showHidden, setShowHidden] = useState(false);  // false: my normal list; true: only the entries I hid
  const [hiddenCount, setHiddenCount] = useState(0);
  const [pageInfo, setPageInfo] = useState({ pages: 1, total: 0 });
  const [onlyReady, setOnlyReady] = useState(false);
  const [showLines, setShowLines] = useState(false);
  const [expanded, setExpanded] = useState(new Set());
  const [tracker, setTracker] = useState(null); // { batchId, jobId }
  const [notice, setNotice] = useState(null);
  const [highlightId, setHighlightId] = useState(null);
  const [dragging, setDragging] = useState(false);
  const [showGuide, setShowGuide] = useState(false);
  const [selected, setSelected] = useState(new Set()); // ids ticked for "Proceed selected to ERP"
  const noticeTimer = useRef(null);

  const flash = useCallback((msg, ms = 7000) => {
    setNotice(msg);
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    if (ms) noticeTimer.current = setTimeout(() => setNotice(null), ms);
  }, []);

  const load = useCallback(async () => {
    try {
      const qs = new URLSearchParams({ page: String(page) });
      if (showHidden) qs.set("hidden", "true");
      if (status !== "all") qs.set("status", status);
      if (dSearch.trim()) qs.set("search", dSearch.trim());
      if (ownerFilter !== "all") qs.set("owner", ownerFilter);
      if (onlyReady) qs.set("only_ready", "true");
      const d = await api("/erp/entries?" + qs.toString());
      if (d.page && d.page !== page) setPage(d.page);   // the server moved us (e.g. the last page became empty)
      setPageInfo({ pages: d.pages || 1, total: d.total || 0 });
      setEntries(d.entries);
      setWorker(d.worker);
      setErpConfigured(d.erp_configured);
      setSaveEnabled(!!d.save_enabled);
      setViewer(d.viewer || { email: null, is_master: false });
      setOwners(d.owners || []);
      setHiddenCount(d.hidden_count || 0);
      setErpLogin(d.erp_login || { mode: "shared", username: null });
      setLoadError(null);
    } catch (e) {
      setLoadError(e.message);
    } finally {
      setLoading(false);
    }
  }, [showHidden, page, status, dSearch, ownerFilter, onlyReady]);

  useEffect(() => {
    const t = setTimeout(() => {
      if (search !== dSearch) {
        setDSearch(search);
        setPage(1);
        setSelected(new Set());
      }
    }, 350);
    return () => clearTimeout(t);
  }, [search, dSearch]);

  const anyActive = entries.some((e) => e.last_run && ACTIVE.includes(e.last_run.state));
  useEffect(() => {
    load();
    const t = setInterval(() => {
      if (!document.hidden) load();
    }, anyActive ? 2500 : 8000);
    return () => clearInterval(t);
  }, [load, anyActive]);

  const activeIds = useMemo(
    () => new Set(entries.filter((e) => e.last_run && ACTIVE.includes(e.last_run.state)).map((e) => e.id)),
    [entries]
  );

  // search / status / owner / ready are applied by the server on ALL pages, so what we got is what is shown
  const visible = entries;

  // One column per account number found in the shown documents (value = amount distributed to it)
  const accounts = useMemo(() => {
    const map = new Map();
    for (const e of visible) {
      for (const d of e.distributions) {
        if (!d.ac_loan_no) continue;
        const a = map.get(d.ac_loan_no) || { no: d.ac_loan_no, heads: [] };
        if (d.account_head && !a.heads.includes(d.account_head)) a.heads.push(d.account_head);
        map.set(d.ac_loan_no, a);
      }
    }
    return [...map.values()].sort((x, y) => x.no.localeCompare(y.no));
  }, [visible]);

  const runnable = visible.filter((e) => e.can_run && !activeIds.has(e.id));
  // only rows that are still visible and still ready count, so nothing hidden by a filter is sent by accident
  const picked = runnable.filter((e) => selected.has(e.id));
  const selectedShown = visible.filter((e) => selected.has(e.id)).length;
  const skippedSelected = selectedShown - picked.length; // selected, but not ready (running, filled, blocked)

  async function run(ids) {
    try {
      const r = await post("/erp/runs", { entry_ids: ids });
      if (r.skipped && r.skipped.length) {
        flash("Not started: " + r.skipped.map((s) => `#${s.entry_id} (${s.reason})`).join("; "), 12000);
      }
      // the Run tracker is not opened automatically: click the value in the "ERP run" column to see it
      if (r.jobs.length) flash(`Started ${r.jobs.length} run${r.jobs.length > 1 ? "s" : ""}. Click the ERP run status to see the steps.`, 6000);
      load();
    } catch (e) {
      flash(`Could not start: ${e.message}`, 12000);
    }
  }

  function toggleSelect(id) {
    setSelected((old) => {
      const n = new Set(old);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }

  function toggleSelectAll() {
    setSelected((old) => {
      const n = new Set(old);
      if (runnable.every((e) => n.has(e.id))) runnable.forEach((e) => n.delete(e.id));
      else runnable.forEach((e) => n.add(e.id));
      return n;
    });
  }

  // one document (row button, tracker Retry): run straight away, no popup
  function requestRun(ids) {
    run(ids);
  }

  function runSelected() {
    if (picked.length === 0) return;
    run(picked.map((e) => e.id));
    setSelected(new Set());
  }

  function runAll() {
    if (runnable.length === 0) return;
    run(runnable.map((e) => e.id));
  }

  // After a Save the robot could not confirm, a person looks in the ERP and says what is true
  async function resolveSave(id, resolution) {
    const q =
      resolution === "saved"
        ? `Entry #${id}: you checked the ERP and this entry IS saved there?\n\nIt will be marked as saved.`
        : `Entry #${id}: you checked the ERP and this entry is NOT saved there?\n\nIt will become runnable again.`;
    if (!window.confirm(q)) return;
    try {
      await post(`/erp/entries/${id}/save-resolution`, { resolution });
      flash(resolution === "saved" ? `Entry #${id} marked as saved.` : `Entry #${id} can be run again.`);
      load();
    } catch (e) {
      flash(`Could not update entry #${id}: ${e.message}`, 12000);
    }
  }

  async function poll() {
    try {
      flash("Checking the mailbox…", 0);
      const r = await post("/poll");
      flash(`Mailbox checked: ${JSON.stringify(r)}`, 8000);
      load();
    } catch (e) {
      flash(`Mailbox check failed: ${e.message}`, 10000);
    }
  }

  async function dropFiles(files) {
    const { lines, lastId } = await uploadFiles(files);
    flash(lines.join("\n"), 15000);
    uploaded(lastId);
  }

  function uploaded(id) {
    load();
    if (id) {
      setStatus("all");
      setSearch("");
      setDSearch("");
      setPage(1);
      setOwnerFilter("all");
      setHighlightId(id);
      setTimeout(() => setHighlightId(null), 15000);
    }
  }

  // a user corrects one processed value; the table then shows "(edited)" under it
  async function editCell(id, field, value) {
    try {
      await put(`/erp/entries/${id}/cell`, { field, value });
      await load();
      return true;
    } catch (e) {
      flash(`Could not save the change: ${e.message}`);
      return false;
    }
  }

  async function hideSelected() {
    const ids = visible.filter((e) => selected.has(e.id)).map((e) => e.id);
    if (ids.length === 0) return;
    try {
      await post(showHidden ? "/erp/entries/unhide" : "/erp/entries/hide", { entry_ids: ids });
      flash(showHidden
        ? `${ids.length} entr${ids.length > 1 ? "ies are" : "y is"} back in your list.`
        : `Hid ${ids.length} entr${ids.length > 1 ? "ies" : "y"}. Tick "show hidden" to bring them back.`, 7000);
      setSelected(new Set());
      load();
    } catch (e) {
      flash(`Could not ${showHidden ? "unhide" : "hide"}: ${e.message}`);
    }
  }

  function toggleShowHidden(v) {
    setShowHidden(v);
    setPage(1);
    setSelected(new Set());
    setExpanded(new Set());
  }

  function goPage(p) {
    setPage(Math.max(1, Math.min(pageInfo.pages, p)));
    setSelected(new Set());
    setExpanded(new Set());
  }

  function toggle(id) {
    setExpanded((old) => {
      const n = new Set(old);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }

  return (
    <main
      className={"page" + (tracker ? " with-tracker" : "") + (dragging ? " dragging" : "")}
      onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
      onDragLeave={(e) => { if (e.currentTarget === e.target) setDragging(false); }}
      onDrop={(e) => { e.preventDefault(); setDragging(false); if (e.dataTransfer.files.length) dropFiles(e.dataTransfer.files); }}
    >
      <div className="content">
        <header className="top top-row">
          <div>
            <h1>Export Proceeds Realization</h1>
            <div className="muted">
              {viewer.is_master ? "Master view: documents of all users. " : "Your documents (sent from your own mail address). "}
              Review what will be filled, then send it to the ERP.{" "}
              {saveEnabled ? (
                <b>Live mode: clean entries are saved in the ERP.</b>
              ) : (
                <b>Test mode: nothing is ever saved in the ERP.</b>
              )}
            </div>
          </div>
          <UserMenu />
        </header>

        {/* {saveEnabled && (
          <div className="banner banner-live">
            <b>LIVE MODE.</b> The robot fills the ERP form, reads every value back, and clicks <b>Save</b> only when
            everything matches the database. If the ERP does not clearly confirm a save, the entry is blocked until a
            person checks the ERP and answers “It was saved” or “Not saved”.
          </div>
        )} */}
        {!erpConfigured && (
          <div className="banner banner-warn">
            ERP_USERNAME / ERP_PASSWORD are not set in .env, so runs will fail at login.
          </div>
        )}
        {loadError && <div className="banner banner-fail">Cannot load the documents: {loadError}</div>}
        {notice && <div className="banner" style={{ whiteSpace: "pre-line" }}>{notice}</div>}

        <div className="toolbar">
          <input className="input" placeholder="Search bill, bank, beneficiary, buyer…" value={search} onChange={(e) => setSearch(e.target.value)} />
          <select className="input" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); setSelected(new Set()); }}>
            <option value="all">All statuses</option>
            <option value="pending">pending</option>
            <option value="filled">filled</option>
          </select>
          {viewer.is_master && (
            <select className="input" value={ownerFilter} onChange={(e) => { setOwnerFilter(e.target.value); setPage(1); setSelected(new Set()); }}>
              <option value="all">All users</option>
              {owners.map((o) => <option key={o} value={o}>{o}</option>)}
              <option value="none">No owner (old data / bank mail)</option>
            </select>
          )}
          <label className="check"><input type="checkbox" checked={onlyReady} onChange={(e) => { setOnlyReady(e.target.checked); setPage(1); setSelected(new Set()); }} /> only ready to run</label>
          {(hiddenCount > 0 || showHidden) && (
            <label className="check">
              <input type="checkbox" checked={showHidden} onChange={(e) => toggleShowHidden(e.target.checked)} /> show hidden ({hiddenCount})
            </label>
          )}
          <label className="check"><input type="checkbox" checked={showLines} onChange={(e) => setShowLines(e.target.checked)} /> show line details</label>
          <span className="spacer" />
          {viewer.email && <ErpLoginButton login={erpLogin} onChange={load} onNotice={flash} />}
          <UploadButton onDone={uploaded} onNotice={flash} />
          <span className="muted small">{worker.busy ? "Robot is working" : "Robot is idle"}{worker.queued ? ` · ${worker.queued} queued` : ""}</span>
          <button className="btn" onClick={() => setShowGuide(!showGuide)}>Status guide</button>
          <button className="btn" onClick={load}>Refresh</button>
          <button className="btn" onClick={poll}>Poll mailbox</button>
        </div>

        {showGuide && <StatusGuide onClose={() => setShowGuide(false)} />}

        {loading ? (
          <div className="empty">Loading…</div>
        ) : (
          <EntriesTable
            entries={visible}
            accounts={accounts}
            showLines={showLines}
            expanded={expanded}
            onToggle={toggle}
            activeIds={activeIds}
            highlightId={highlightId}
            onRun={requestRun}
            saveEnabled={saveEnabled}
            showOwner={viewer.is_master}
            onEdit={editCell}
            onResolve={resolveSave}
            onOpenRun={(r) => setTracker({ batchId: null, jobId: r.id })}
            selected={selected}
            onSelect={toggleSelect}
            onSelectAll={toggleSelectAll}
          />
        )}
        <div className="pager">
          <button className="btn" disabled={page <= 1} onClick={() => goPage(page - 1)}>‹ Prev</button>
          <span className="muted">
            Page {page} of {pageInfo.pages} · {pageInfo.total} entr{pageInfo.total === 1 ? "y" : "ies"}
          </span>
          <button className="btn" disabled={page >= pageInfo.pages} onClick={() => goPage(page + 1)}>Next ›</button>
        </div>
        <div className="spacer-bottom" />
      </div>

      <RunTracker
        tracker={tracker}
        onSelectJob={(jobId) => setTracker((t) => ({ ...t, jobId }))}
        onClose={() => setTracker(null)}
        onRetry={(id) => requestRun([id])}
        onJobFinished={load}
        onNotice={flash}
      />

      <footer className="footbar">
        <div>
          {showHidden ? (
            <span><b>Hidden entries</b>: {visible.length} shown. Tick rows and press Unhide to bring them back.</span>
          ) : (
            <>
              <b>{runnable.length}</b> of {visible.length} shown document(s) are ready to send.
              <span className="muted">
                {saveEnabled ? " The robot works one by one and saves only entries that match." : " The robot works one by one and never saves."}
              </span>
            </>
          )}
        </div>
        <div className="footbtns">
          {selectedShown > 0 && (
            <>
              <span className="muted small">
                {selectedShown} selected{skippedShown(skippedSelected)}
              </span>
              <button className="link" onClick={() => setSelected(new Set())}>Clear selection</button>
            </>
          )}
          {selectedShown > 0 && (
            <button className="btn btn-big" onClick={hideSelected}
                    title={showHidden ? "Put the selected entries back in your list" : "Hide the selected entries from your list (nothing is deleted)"}>
              {showHidden ? "Unhide" : "Hide"} selected ({selectedShown})
            </button>
          )}
          {!showHidden && picked.length > 0 && (
            <button className="btn btn-primary btn-big" onClick={runSelected}>
              Selected Proceed to ERP ({picked.length})
            </button>
          )}
          {!showHidden && (
            <button
              className={"btn btn-big " + (picked.length > 0 ? "" : "btn-primary")}
              disabled={runnable.length === 0}
              onClick={runAll}
            >
              All Proceed to ERP ({runnable.length})
            </button>
          )}
        </div>
      </footer>
    </main>
  );
}
