"use client";

// "What does each status mean?" - plain-language help panel
export default function StatusGuide({ onClose }) {
  return (
    <div className="guide">
      <div className="guide-head">
        <b>What the statuses and buttons mean</b>
        <button className="btn btn-small" onClick={onClose}>Close ✕</button>
      </div>
      <div className="guide-grid">
        <div>
          <h4>Status <span className="muted small">(the document itself)</span></h4>
          <table className="sub">
            <tbody>
              <tr><td><span className="pill pill-pending">pending</span></td><td>Read from the PDF / Excel and ready to send to the ERP. Every new document starts here. It stays here after a test run, because nothing is saved in the ERP.</td></tr>
              <tr><td><span className="pill pill-filled">filled</span></td><td>Saved in the ERP. It appears only when the robot really saves an entry (not switched on yet). The button is then replaced by <b>✓ Saved in ERP</b> and the row cannot be run again.</td></tr>
            </tbody>
          </table>
        </div>
        <div>
          <h4>ERP run <span className="muted small">(what the robot did last time)</span></h4>
          <table className="sub">
            <tbody>
              <tr><td><span className="pill pill-none">Not run yet</span></td><td>The robot has not tried this document.</td></tr>
              <tr><td><span className="pill pill-queued">Queued</span></td><td>Waiting its turn. The robot does one document at a time.</td></tr>
              <tr><td><span className="pill pill-running">Running · step</span></td><td>The robot is working. Click it to watch every step.</td></tr>
              <tr><td><span className="pill pill-ok">Filled · not saved</span></td><td>The whole ERP form was filled and every value matches the database. Nothing was saved.</td></tr>
              <tr><td><span className="pill pill-warn">Filled · N to check</span></td><td>The form was filled but N values differ or look uncertain. The Remarks column says which. Check before anyone saves.</td></tr>
              <tr><td><span className="pill pill-fail">Failed · reason</span></td><td>The robot stopped. Nothing was saved. The reason is in Remarks and in the tracker. Fix it, then press Run again.</td></tr>
              <tr><td><span className="pill pill-none">Cancelled</span></td><td>You cancelled the run.</td></tr>
            </tbody>
          </table>
        </div>
        <div>
          <h4>Action button</h4>
          <table className="sub">
            <tbody>
              <tr><td><b>Proceed to ERP</b></td><td>Never run before: fill this document in the ERP.</td></tr>
              <tr><td><b>Run again</b></td><td>It was run before (any result): try again, for example after fixing a problem.</td></tr>
              <tr><td><b>In progress…</b></td><td>Queued or running right now.</td></tr>
              <tr><td>Greyed button</td><td>The entry cannot be sent yet. Hover to see why; the Remarks column shows the same reason.</td></tr>
              <tr><td><b>✓ Saved in ERP</b></td><td>The entry is saved in the ERP. No button.</td></tr>
            </tbody>
          </table>
          <h4>Remarks</h4>
          <p className="muted">One short reason, only when something failed or is uncertain: red = blocked or failed, amber = please check, grey = information. Hover to read it in full.</p>
        </div>
      </div>
    </div>
  );
}
