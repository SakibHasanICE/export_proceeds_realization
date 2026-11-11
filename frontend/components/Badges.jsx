"use client";

export function DbStatus({ status }) {
  return <span className={`pill pill-${status || "none"}`}>{status || "—"}</span>;
}

// What happened the last time the robot ran this entry
export function ErpBadge({ run, onClick }) {
  if (!run) return <span className="pill pill-none">Not run yet</span>;
  const map = {
    queued: ["pill-queued", "Queued"],
    running: ["pill-running", `Running${run.current_label ? " · " + run.current_label : ""}`],
    succeeded: ["pill-ok", "Filled · not saved"],
    attention: ["pill-warn", `Filled · ${run.counts ? run.counts.error + run.counts.warn : "some"} to check`],
    failed: ["pill-fail", `Failed${run.error ? " · " + run.error.title : ""}`],
    cancelled: ["pill-none", "Cancelled"],
  };
  const [cls, label] = map[run.state] || ["pill-none", run.state];
  return (
    <button className={`pill pill-btn ${cls}`} onClick={onClick} title="Open the tracker for this run">
      {run.state === "running" && <span className="spinner" />}
      {label}
    </button>
  );
}
