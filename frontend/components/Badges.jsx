"use client";

export function DbStatus({ status }) {
  return <span className={`pill pill-${status || "none"}`}>{status || "—"}</span>;
}

// What happened the last time the robot ran this entry
export function ErpBadge({ run, onClick }) {
  if (!run) return <span className="pill pill-none">Not run yet</span>;
  const sv = run.save_state;
  const map = {
    queued: ["pill-queued", "Queued"],
    running: ["pill-running", `Running${run.current_label ? " · " + run.current_label : ""}`],
    succeeded: sv === "saved" ? ["pill-ok", "Saved in ERP"] : ["pill-ok", "Filled · not saved"],
    attention:
      sv === "unconfirmed" ? ["pill-fail", "Save not confirmed"]
      : sv === "rejected" ? ["pill-fail", "ERP refused to save"]
      : ["pill-warn", `Filled · ${run.counts ? run.counts.error + run.counts.warn : "some"} to check`],
    failed: run.save_clicked ? ["pill-fail", "Stopped after Save"] : ["pill-fail", `Failed${run.error ? " · " + run.error.title : ""}`],
    cancelled: run.save_clicked ? ["pill-fail", "Stopped after Save"] : ["pill-none", "Cancelled"],
  };
  const [cls, label] = map[run.state] || ["pill-none", run.state];
  return (
    <button className={`pill pill-btn ${cls}`} onClick={onClick} title="Open the tracker for this run">
      {run.state === "running" && <span className="spinner" />}
      {label}
    </button>
  );
}
