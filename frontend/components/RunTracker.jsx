"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, post } from "../lib/api";
import { ACTIVE, FINAL, clock, elapsed } from "../lib/format";

const STATE_TEXT = {
  queued: "Waiting in the queue",
  running: "Running",
  succeeded: "Filled — nothing saved",
  attention: "Filled — needs your attention",
  failed: "Stopped by an error",
  cancelled: "Cancelled",
};

function useNow(active) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return undefined;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [active]);
  return now;
}

// Polls one run; merges the new log lines as they arrive.
function useJob(jobId, onFinished) {
  const [job, setJob] = useState(null);
  const [events, setEvents] = useState([]);
  const [error, setError] = useState(null);
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;

  useEffect(() => {
    let stop = false;
    let since = 0;
    let timer = null;
    setJob(null);
    setEvents([]);
    setError(null);

    async function tick() {
      if (stop || !jobId) return;
      try {
        const j = await api(`/erp/runs/${jobId}?since=${since}`);
        if (stop) return;
        since = j.next_since;
        setJob(j);
        setError(null);
        if (j.events.length) setEvents((old) => old.concat(j.events));
        if (FINAL.includes(j.state)) {
          if (finishedRef.current) finishedRef.current(j);
          return; // done: no more polling
        }
        timer = setTimeout(tick, j.state === "running" ? 700 : 1500);
      } catch (e) {
        if (stop) return;
        setError(e.message);
        timer = setTimeout(tick, 3000); // backend busy or restarting: keep trying
      }
    }
    tick();
    return () => {
      stop = true;
      if (timer) clearTimeout(timer);
    };
  }, [jobId]);

  return { job, events, error };
}

function StepIcon({ status }) {
  if (status === "running") return <span className="spinner big" />;
  const m = { done: "✓", failed: "✗", skipped: "–", pending: "○", warn: "!" };
  return <span className={`step-icon s-${status}`}>{m[status] || "○"}</span>;
}

function Stepper({ steps, now }) {
  return (
    <ol className="stepper">
      {steps.map((s) => (
        <li key={s.key} className={`step st-${s.status}`}>
          <StepIcon status={s.status} />
          <div className="step-body">
            <div className="step-title">
              {s.label}
              {s.started_at && (
                <span className="muted small"> · {elapsed(s.started_at, s.finished_at, now)}</span>
              )}
            </div>
            {s.message && <div className={"step-msg" + (s.status === "failed" ? " bad" : "")}>{s.message}</div>}
          </div>
        </li>
      ))}
    </ol>
  );
}

function ErrorCard({ error, shots }) {
  const [open, setOpen] = useState(false);
  const last = shots.length ? shots[shots.length - 1] : null;
  return (
    <div className="card card-fail">
      <div className="card-title">✗ {error.title}</div>
      <p>{error.message}</p>
      {error.hint && <p className="hint">What to do: {error.hint}</p>}
      <div className="row-gap">
        <span className="muted small">Code: {error.code}</span>
        {last && (
          <a href={`/api/erp/screens/${last.file}`} target="_blank" rel="noreferrer" className="link">
            See what the robot saw
          </a>
        )}
        <button className="link" onClick={() => setOpen(!open)}>{open ? "Hide" : "Show"} technical details</button>
      </div>
      {open && <pre className="pre">{JSON.stringify(error.details || {}, null, 2)}</pre>}
    </div>
  );
}

const ICON = { ok: "✓", warn: "⚠", error: "✗", info: "ⓘ" };

function Verification({ ver }) {
  const [onlyIssues, setOnlyIssues] = useState(ver.counts.error + ver.counts.warn > 0);
  const rows = ver.checks.filter((c) => !onlyIssues || c.status === "warn" || c.status === "error");
  const areas = [...new Set(rows.map((c) => c.area))];
  return (
    <div className="card">
      <div className="card-title">
        Check: database vs what the ERP shows
        <span className="chips inline">
          <span className="chip chip-ok">{ver.counts.ok} match</span>
          {ver.counts.warn > 0 && <span className="chip chip-warn">{ver.counts.warn} warning</span>}
          {ver.counts.error > 0 && <span className="chip chip-fail">{ver.counts.error} mismatch</span>}
          <span className="chip">{ver.counts.info} info</span>
        </span>
      </div>
      <label className="check">
        <input type="checkbox" checked={onlyIssues} onChange={(e) => setOnlyIssues(e.target.checked)} /> Show only
        problems
      </label>
      {rows.length === 0 && <p className="muted">Everything matches.</p>}
      {areas.map((a) => (
        <table key={a} className="sub ver">
          <thead>
            <tr><th colSpan={4}>{a}</th></tr>
          </thead>
          <tbody>
            {rows.filter((c) => c.area === a).map((c, i) => (
              <tr key={i} className={"v-" + c.status}>
                <td className="v-ic">{ICON[c.status]}</td>
                <td>{c.field}</td>
                <td><span className="muted small">DB</span> {c.expected || "—"}</td>
                <td><span className="muted small">ERP</span> {c.erp || "—"}{c.note && <div className="muted small">{c.note}</div>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ))}
    </div>
  );
}

function LogView({ events }) {
  const [tech, setTech] = useState(false);
  const box = useRef(null);
  const shown = useMemo(() => events.filter((e) => tech || e.level !== "detail"), [events, tech]);
  useEffect(() => {
    const el = box.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [shown.length]);
  return (
    <div className="card">
      <div className="card-title">
        Live log
        <label className="check right">
          <input type="checkbox" checked={tech} onChange={(e) => setTech(e.target.checked)} /> technical details
        </label>
      </div>
      <div className="log" ref={box}>
        {shown.map((e) => (
          <div key={e.i} className={`log-line lv-${e.level}`}>
            <span className="log-t">{clock(e.t)}</span>
            <span className="log-m">{e.msg}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function JobView({ jobId, onFinished, onRetry, onNotice }) {
  const { job, events, error } = useJob(jobId, onFinished);
  const active = job && ACTIVE.includes(job.state);
  const now = useNow(active);

  async function cancel() {
    try {
      await post(`/erp/runs/${jobId}/cancel`);
    } catch (e) {
      onNotice(e.message);
    }
  }

  if (!job) return <div className="muted pad">{error ? `Waiting for the backend… (${error})` : "Loading…"}</div>;
  const res = job.result || {};
  return (
    <div className="job">
      <div className="job-head">
        <div>
          <div className="job-title">
            Entry #{job.entry_id} · <span className="mono">{job.bill_no}</span> {job.bank && <span className="muted">({job.bank})</span>}
          </div>
          <div className={`job-state js-${job.state}`}>
            {job.state === "running" && <span className="spinner" />} {STATE_TEXT[job.state]}
            <span className="muted small"> · {elapsed(job.started_at || job.created_at, job.finished_at, now)}</span>
            {job.attempts > 1 && <span className="muted small"> · attempt {job.attempts}</span>}
          </div>
        </div>
        <div className="row-gap">
          {active && <button className="btn" onClick={cancel}>Cancel</button>}
          {FINAL.includes(job.state) && job.state !== "succeeded" && (
            <button className="btn btn-primary" onClick={() => onRetry(job.entry_id)}>Retry</button>
          )}
        </div>
      </div>

      <div className="banner">
        TEST MODE — the robot fills the form, takes a screenshot and closes the browser. It never clicks Save.
      </div>
      {error && <div className="banner banner-warn">Lost contact with the backend ({error}). Retrying…</div>}

      {job.state === "succeeded" && (
        <div className="card card-ok"><b>✓ Filled and verified.</b> Every value in the ERP form matches the database. Nothing was saved.</div>
      )}
      {job.state === "attention" && (
        <div className="card card-warn"><b>⚠ Filled, but check the highlighted values below</b> before anyone saves this in the ERP. Nothing was saved.</div>
      )}
      {job.error && <ErrorCard error={job.error} shots={job.shots} />}

      <Stepper steps={job.steps} now={now} />
      {res.verification && <Verification ver={res.verification} />}

      {job.shots.length > 0 && (
        <div className="card">
          <div className="card-title">Screenshots</div>
          <div className="shots">
            {job.shots.map((s, i) => (
              <a key={i} href={`/api/erp/screens/${s.file}`} target="_blank" rel="noreferrer" className="shot">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={`/api/erp/screens/${s.file}`} alt={s.label} loading="lazy" />
                <span>{s.label.replace(/_/g, " ")}</span>
              </a>
            ))}
          </div>
        </div>
      )}
      <LogView events={events} />
    </div>
  );
}

function BatchView({ batchId, jobId, onSelect, onFinished }) {
  const [batch, setBatch] = useState(null);
  useEffect(() => {
    let stop = false;
    let timer = null;
    async function tick() {
      try {
        const b = await api(`/erp/batches/${batchId}`);
        if (stop) return;
        setBatch(b);
        if (b.finished) {
          onFinished && onFinished(b);
          return;
        }
      } catch (e) {
        /* keep trying */
      }
      timer = setTimeout(tick, 1500);
    }
    tick();
    return () => {
      stop = true;
      if (timer) clearTimeout(timer);
    };
  }, [batchId]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!batch) return null;
  const pct = batch.total ? Math.round((batch.done / batch.total) * 100) : 0;
  async function cancelAll() {
    if (window.confirm("Cancel the runs that have not finished yet? Nothing is saved either way.")) {
      await post(`/erp/batches/${batchId}/cancel`).catch(() => {});
    }
  }
  return (
    <div className="batch">
      <div className="batch-head">
        <b>{batch.finished ? "Batch finished" : "Processing one by one"}</b>
        <span className="muted"> · {batch.done} / {batch.total} done</span>
        {!batch.finished && <button className="btn btn-small right" onClick={cancelAll}>Cancel remaining</button>}
      </div>
      <div className="bar"><div className="bar-fill" style={{ width: `${pct}%` }} /></div>
      <div className="chips">
        {batch.counts.succeeded > 0 && <span className="chip chip-ok">{batch.counts.succeeded} filled</span>}
        {batch.counts.attention > 0 && <span className="chip chip-warn">{batch.counts.attention} to check</span>}
        {batch.counts.failed > 0 && <span className="chip chip-fail">{batch.counts.failed} failed</span>}
        {batch.counts.cancelled > 0 && <span className="chip">{batch.counts.cancelled} cancelled</span>}
        {batch.counts.running + batch.counts.queued > 0 && <span className="chip">{batch.counts.running + batch.counts.queued} waiting</span>}
      </div>
      {batch.jobs.length > 1 && (
        <ul className="joblist">
          {batch.jobs.map((j) => (
            <li key={j.id}>
              <button className={"jobbtn" + (j.id === jobId ? " sel" : "")} onClick={() => onSelect(j.id)}>
                <span>#{j.entry_id}</span>
                <span className={`pill pill-${j.state === "succeeded" ? "ok" : j.state === "attention" ? "warn" : j.state === "failed" ? "fail" : j.state === "running" ? "running" : "queued"}`}>
                  {j.state === "running" ? (j.current_label || "running") : STATE_TEXT[j.state]}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function RunTracker({ tracker, onSelectJob, onClose, onRetry, onJobFinished, onNotice }) {
  if (!tracker) return null;
  return (
    <aside className="tracker">
      <div className="tracker-head">
        <b>Run tracker</b>
        <button className="btn btn-small" onClick={onClose}>Close ✕</button>
      </div>
      {tracker.batchId && (
        <BatchView batchId={tracker.batchId} jobId={tracker.jobId} onSelect={onSelectJob} onFinished={onJobFinished} />
      )}
      {tracker.jobId && (
        <JobView jobId={tracker.jobId} onFinished={onJobFinished} onRetry={onRetry} onNotice={onNotice} />
      )}
    </aside>
  );
}
