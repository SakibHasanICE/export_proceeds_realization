"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import EntriesTable from "../components/EntriesTable";
import RunTracker from "../components/RunTracker";
import StatusGuide from "../components/StatusGuide";
import UploadButton from "../components/UploadButton";
import { uploadFiles } from "../lib/upload";
import { api, post } from "../lib/api";
import { ACTIVE } from "../lib/format";

export default function Page() {
  const [entries, setEntries] = useState([]);
  const [worker, setWorker] = useState({ busy: false, queued: 0 });
  const [erpConfigured, setErpConfigured] = useState(true);
  const [loadError, setLoadError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState("all");
  const [search, setSearch] = useState("");
  const [onlyReady, setOnlyReady] = useState(false);
  const [showLines, setShowLines] = useState(false);
  const [expanded, setExpanded] = useState(new Set());
  const [tracker, setTracker] = useState(null); // { batchId, jobId }
  const [notice, setNotice] = useState(null);
  const [highlightId, setHighlightId] = useState(null);
  const [dragging, setDragging] = useState(false);
  const [showGuide, setShowGuide] = useState(false);
  const noticeTimer = useRef(null);

  const flash = useCallback((msg, ms = 7000) => {
    setNotice(msg);
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    if (ms) noticeTimer.current = setTimeout(() => setNotice(null), ms);
  }, []);

  const load = useCallback(async () => {
    try {
      const d = await api("/erp/entries");
      setEntries(d.entries);
      setWorker(d.worker);
      setErpConfigured(d.erp_configured);
      setLoadError(null);
    } catch (e) {
      setLoadError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

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

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase();
    return entries.filter((e) => {
      if (status !== "all" && e.status !== status) return false;
      if (onlyReady && !e.can_run) return false;
      if (!q) return true;
      return [e.id, e.bank, e.bill_invoice_no, e.beneficiary, e.buyer, e.lc_sc_no]
        .map((x) => String(x || "").toLowerCase())
        .some((x) => x.includes(q));
    });
  }, [entries, status, search, onlyReady]);

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

  async function run(ids) {
    try {
      const r = await post("/erp/runs", { entry_ids: ids });
      if (r.skipped && r.skipped.length) {
        flash("Not started: " + r.skipped.map((s) => `#${s.entry_id} (${s.reason})`).join("; "), 12000);
      }
      if (r.jobs.length) {
        setTracker({ batchId: r.jobs.length > 1 ? r.batch_id : null, jobId: r.jobs[0] });
      }
      load();
    } catch (e) {
      flash(`Could not start: ${e.message}`, 12000);
    }
  }

  function runAll() {
    if (runnable.length === 0) return;
    const ok = window.confirm(
      `Fill ${runnable.length} document(s) in the ERP, one after another?\n\n` +
        "The robot only fills the forms and takes screenshots. It never clicks Save."
    );
    if (ok) run(runnable.map((e) => e.id));
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
      setHighlightId(id);
      setTimeout(() => setHighlightId(null), 15000);
    }
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
        <header className="top">
          <h1>Export Proceeds Realization</h1>
          <div className="muted">
            Review what will be filled, then send it to the ERP. <b>Test mode: nothing is ever saved in the ERP.</b>
          </div>
        </header>

        {!erpConfigured && (
          <div className="banner banner-warn">
            ERP_USERNAME / ERP_PASSWORD are not set in .env, so runs will fail at login.
          </div>
        )}
        {loadError && <div className="banner banner-fail">Cannot load the documents: {loadError}</div>}
        {notice && <div className="banner" style={{ whiteSpace: "pre-line" }}>{notice}</div>}

        <div className="toolbar">
          <input className="input" placeholder="Search bill, bank, beneficiary, buyer…" value={search} onChange={(e) => setSearch(e.target.value)} />
          <select className="input" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="all">All statuses</option>
            <option value="pending">pending</option>
            <option value="filled">filled</option>
          </select>
          <label className="check"><input type="checkbox" checked={onlyReady} onChange={(e) => setOnlyReady(e.target.checked)} /> only ready to run</label>
          <label className="check"><input type="checkbox" checked={showLines} onChange={(e) => setShowLines(e.target.checked)} /> show line details</label>
          <span className="spacer" />
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
            onRun={run}
            onOpenRun={(r) => setTracker({ batchId: null, jobId: r.id })}
          />
        )}
        <div className="spacer-bottom" />
      </div>

      <RunTracker
        tracker={tracker}
        onSelectJob={(jobId) => setTracker((t) => ({ ...t, jobId }))}
        onClose={() => setTracker(null)}
        onRetry={(id) => run([id])}
        onJobFinished={load}
        onNotice={flash}
      />

      <footer className="footbar">
        <div>
          <b>{runnable.length}</b> of {visible.length} shown document(s) are ready to send.
          <span className="muted"> The robot works one by one and never saves.</span>
        </div>
        <button className="btn btn-primary btn-big" disabled={runnable.length === 0} onClick={runAll}>
          Proceed all to ERP ({runnable.length})
        </button>
      </footer>
    </main>
  );
}
