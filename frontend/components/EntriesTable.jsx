"use client";
import { Fragment, useRef, useState } from "react";
import { DbStatus, ErpBadge } from "./Badges";
import { dmy, money, rate, stamp } from "../lib/format";

function Head({ row }) {
  if (row.account_head) {
    return (
      <span>
        {row.account_head}
        {row.account_head_source === "account map" && <span className="src" title="Taken from account_map.json">map</span>}
      </span>
    );
  }
  return <span className="muted">(empty)</span>;
}

function Lines({ e, saveEnabled }) {
  const t = e.totals || {};
  return (
    <div className="detail">
      <div className="detail-note">
        <b>What the robot will do:</b> select the Beneficiary, double-click Bill/Invoice No and pick the bill, then type
        the Received Date and the tables below. The ERP fills {e.erp_fills_itself.join(", ")} by itself from the bill.
        {saveEnabled ? (
          <b> Then it reads every value back and clicks Save only if everything matches.</b>
        ) : (
          <b> Nothing is saved.</b>
        )}
      </div>

      {(e.blockers.length > 0 || e.warnings.length > 0) && (
        <div className="chips">
          {e.blockers.map((b, i) => (
            <span key={"b" + i} className="chip chip-fail">✗ {b}</span>
          ))}
          {e.warnings.map((w, i) => (
            <span key={"w" + i} className="chip chip-warn">⚠ {w}</span>
          ))}
        </div>
      )}

      <div className="grid2">
        <div>
          <h4>Deductions at Source</h4>
          <table className="sub">
            <thead>
              <tr><th>Account Head</th><th>Document Currency</th><th>Conversion Rate</th><th>Domestic Currency</th></tr>
            </thead>
            <tbody>
              {e.deductions.length === 0 && <tr><td colSpan={4} className="muted">No deductions for this document</td></tr>}
              {e.deductions.map((d, i) => (
                <tr key={i}>
                  <td><Head row={d} />{d.label && <div className="muted small">{d.label}</div>}</td>
                  <td className="num">{money(d.document_currency)}</td>
                  <td className="num">{rate(d.conversion_rate)}</td>
                  <td className="num">{money(d.domestic_currency)}</td>
                </tr>
              ))}
              <tr className="total">
                <td>Total</td><td className="num">{money(t.deductions_document)}</td><td />
                <td className="num">{money(t.deductions_domestic)}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div>
          <h4>Distributions</h4>
          <table className="sub">
            <thead>
              <tr>
                <th>Account Head</th><th>AC / Loan No</th><th>Document Currency</th><th>Conversion Rate</th><th>Domestic Currency</th>
              </tr>
            </thead>
            <tbody>
              {e.distributions.map((d, i) => (
                <tr key={i}>
                  <td><Head row={d} /></td>
                  <td className="mono">{d.ac_loan_no || <span className="muted">(empty)</span>}</td>
                  <td className="num">{money(d.document_currency)}</td>
                  <td className="num">{rate(d.conversion_rate)}</td>
                  <td className="num">{money(d.domestic_currency)}</td>
                </tr>
              ))}
              <tr className="total">
                <td colSpan={2}>Total</td><td className="num">{money(t.distributions_document)}</td><td />
                <td className="num">{money(t.distributions_domestic)}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <table className="sub grand">
        <thead>
          <tr><th>Grand Total · Document Currency</th><th>Cumulative Bill Balance</th><th>Grand Total · Domestic Currency</th></tr>
        </thead>
        <tbody>
          <tr>
            <td className="num">{money(t.grand_document)}</td>
            <td className={"num " + (Math.abs(t.cumulative_bill_balance || 0) > 0.005 ? "bad" : "good")}>
              {money(t.cumulative_bill_balance, 5)}
            </td>
            <td className="num">{money(t.grand_domestic)}</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

// A table cell the user can correct by double-clicking it. A value that was changed shows "(edited)" under it.
function EditCell({ e, field, kind = "text", value, canEdit, onEdit, className = "", children }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const cancelled = useRef(false);
  const info = (e.edited || {})[field];
  const current = kind === "date" ? String(value || "").slice(0, 10) : value === null || value === undefined ? "" : String(value);

  function start() {
    cancelled.current = false;
    setDraft(current);
    setEditing(true);
  }

  async function commit() {
    if (cancelled.current || busy) return;
    if (draft.trim() === current.trim()) {
      setEditing(false);
      return;
    }
    setBusy(true);
    const ok = await onEdit(e.id, field, draft.trim());
    setBusy(false);
    if (ok !== false) setEditing(false);
  }

  const tip = info
    ? `Edited${info.by ? " by " + info.by : ""}. Processed value was: ${info.original === "" ? "(empty)" : info.original}`
    : canEdit
    ? "Double-click to edit"
    : undefined;

  return (
    <td className={(className + (canEdit ? " editable" : "")).trim()} title={tip} onDoubleClick={canEdit && !editing ? start : undefined}>
      {editing ? (
        <input
          className="cell-input"
          autoFocus
          disabled={busy}
          type={kind === "date" ? "date" : "text"}
          inputMode={kind === "number" ? "decimal" : undefined}
          value={draft}
          onChange={(ev) => setDraft(ev.target.value)}
          onBlur={commit}
          onKeyDown={(ev) => {
            if (ev.key === "Enter") commit();
            if (ev.key === "Escape") {
              cancelled.current = true;
              setEditing(false);
            }
          }}
        />
      ) : (
        children
      )}
      {info && !editing && <span className="edited-tag">(edited)</span>}
    </td>
  );
}

// Amount an entry distributes to one account (sum, in case the same account appears twice)
function acctAmount(e, no) {
  const rows = e.distributions.filter((d) => d.ac_loan_no === no);
  if (rows.length === 0) return null;
  return rows.reduce((t, d) => t + Number(d.document_currency || 0), 0);
}

// One very short reason: what blocks the entry, what the last run reported, or what is uncertain
function remarkFor(e) {
  const r = e.last_run;
  if (r && r.state === "queued") return { level: "info", text: "Waiting in the queue" };
  if (r && r.state === "running") return { level: "info", text: `Working: ${r.current_label || "starting"}` };
  if (e.remark && e.remark.level === "error") return e.remark;
  if (r && r.remark) return r.remark;
  return e.remark || null;
}

export default function EntriesTable({ entries, accounts, showLines, activeIds, highlightId, onRun, onOpenRun, expanded, onToggle,
  selected, onSelect, onSelectAll, saveEnabled, onResolve, showOwner, onEdit }) {
  if (entries.length === 0) return <div className="empty">No documents match the filter.</div>;
  const pickable = entries.filter((e) => e.can_run && !activeIds.has(e.id));
  const pickedCount = pickable.filter((e) => selected.has(e.id)).length;
  return (
    <div className="table-wrap">
      <table className="main">
        <thead>
          <tr>
            <th className="stick-l">
              <div className="firstcell">
                <input
                  type="checkbox"
                  title="Select every document that is ready to send"
                  disabled={pickable.length === 0}
                  checked={pickable.length > 0 && pickedCount === pickable.length}
                  ref={(el) => { if (el) el.indeterminate = pickedCount > 0 && pickedCount < pickable.length; }}
                  onChange={onSelectAll}
                />
              </div>
            </th>
            <th>ID</th>
            {showOwner && <th title="The user who sent this document (master only)">Owner</th>}
            <th>Bank</th>
            <th>Bill/Invoice No</th>
            <th>Beneficiary</th>
            <th title="From the bank advice; the ERP fills its own Buyer from the bill">Buyer</th>
            <th title="From the bank advice; the ERP fills its own LC/SC No from the bill">LC/SC No</th>
            <th>Received Date</th>
            <th>Bill/Invoice Date</th>
            <th>CCY</th>
            <th className="num">Bill Amount</th>
            <th className="num">Rate</th>
            <th className="num">Deductions</th>
            {accounts.map((a) => (
              <th key={a.no} className="num acct-h" title={a.heads.length ? a.heads.join(" / ") : "No account head set"}>
                {a.no}
              </th>
            ))}
            <th className="num">Distributions</th>
            <th className="num">Cum. Balance</th>
            <th>Status</th>
            <th>ERP run</th>
            <th>Remarks</th>
            <th title="When this document was stored and appeared in the table">Arrived</th>
            <th title="When someone last pressed Proceed to ERP / Run again for this document">Proceed clicked</th>
            <th title="The signed-in user who pressed it">Clicked by</th>
            <th className="stick-r">Action</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => {
            const isActive = activeIds.has(e.id);
            const open = showLines || expanded.has(e.id);
            const bal = e.totals.cumulative_bill_balance;
            const reason = !e.can_run ? e.blockers.join("; ") : isActive ? "Already queued or running" : "";
            const canPick = e.can_run && !isActive;
            const canEdit = !!onEdit && e.status === "pending" && !isActive;   // only entries not yet saved in the ERP
            const ed = { e, canEdit, onEdit };
            return (
              <Fragment key={e.id}>
                <tr
                  className={[isActive ? "row-active" : e.id === highlightId ? "row-new" : "", selected.has(e.id) ? "row-selected" : ""].join(" ").trim()}
                  onClick={(ev) => {
                    // click anywhere on the row to select it (grey); click again to unselect.
                    // Buttons, links and the checkbox keep their own behaviour.
                    if (ev.target.closest("button, a, input, select, label")) return;
                    onSelect(e.id);
                  }}
                >
                  <td className="stick-l">
                    <div className="firstcell">
                      <input
                        type="checkbox"
                        checked={selected.has(e.id)}
                        title={canPick ? "Select this row" : "Select this row (it can be followed, but it is not sent: " + (reason || "not ready") + ")"}
                        onChange={() => onSelect(e.id)}
                      />
                      <button className="chev" onClick={() => onToggle(e.id)} title="Show / hide the line details">
                        {open ? "▾" : "▸"}
                      </button>
                    </div>
                  </td>
                  <td className="mono">{e.id}</td>
                  {showOwner && <td className="small">{e.owner_email || <span className="muted">—</span>}</td>}
                  <td>{e.bank}</td>
                  <EditCell {...ed} field="bill_invoice_no" value={e.bill_invoice_no} className="mono strong">{e.bill_invoice_no}</EditCell>
                  <EditCell {...ed} field="beneficiary" value={e.beneficiary}>{e.beneficiary || <span className="muted">—</span>}</EditCell>
                  <EditCell {...ed} field="buyer" value={e.buyer}>{e.buyer || <span className="muted">—</span>}</EditCell>
                  <EditCell {...ed} field="lc_sc_no" value={e.lc_sc_no}>{e.lc_sc_no || <span className="muted">—</span>}</EditCell>
                  <EditCell {...ed} field="received_date" kind="date" value={e.received_date}>{dmy(e.received_date)}</EditCell>
                  <EditCell {...ed} field="bill_invoice_date" kind="date" value={e.bill_invoice_date}>{dmy(e.bill_invoice_date)}</EditCell>
                  <td>{e.currency}</td>
                  <EditCell {...ed} field="bill_invoice_amount" kind="number" value={e.bill_invoice_amount} className="num">{money(e.bill_invoice_amount)}</EditCell>
                  <EditCell {...ed} field="conversion_rate" kind="number" value={e.conversion_rate} className="num">{rate(e.conversion_rate)}</EditCell>
                  <EditCell
                    {...ed}
                    canEdit={canEdit && e.deductions.length === 1}
                    field="deductions" kind="number" value={e.totals.deductions_document} className="num"
                  >
                    {money(e.totals.deductions_document)}
                  </EditCell>
                  {accounts.map((a) => {
                    const v = acctAmount(e, a.no);
                    const lines = e.distributions.filter((d) => d.ac_loan_no === a.no).length;
                    return (
                      <EditCell {...ed} key={a.no} canEdit={canEdit && lines === 1} field={"dist:" + a.no} kind="number" value={v} className="num">
                        {v === null ? <span className="muted">—</span> : money(v)}
                      </EditCell>
                    );
                  })}
                  <td className="num">{money(e.totals.distributions_document)}</td>
                  <td className={"num " + (bal !== null && bal !== undefined && Math.abs(bal) > 0.005 ? "bad" : "good")}>
                    {money(bal)}
                  </td>
                  <td><DbStatus status={e.status} /></td>
                  <td><ErpBadge run={e.last_run} onClick={() => e.last_run && onOpenRun(e.last_run)} /></td>
                  <td className="remark">
                    {(() => {
                      const rk = remarkFor(e);
                      return rk ? <span className={"rk rk-" + rk.level} title={rk.text}>{rk.text}</span> : null;
                    })()}
                  </td>
                  <td className="mono small">{stamp(e.created_at)}</td>
                  <td className="mono small">{e.erp_clicked_at ? stamp(e.erp_clicked_at) : <span className="muted">—</span>}</td>
                  <td className="small">{e.erp_clicked_by || <span className="muted">—</span>}</td>
                  <td className="stick-r">
                    {e.status === "filled" ? (
                      <span className="saved">✓ Saved in ERP</span>
                    ) : (
                      <button
                        className="btn btn-primary"
                        disabled={!e.can_run || isActive}
                        title={reason || (saveEnabled ? "Fill this document in the ERP and click Save when every value matches" : "Fill this document in the ERP (nothing is saved)")}
                        onClick={() => onRun([e.id])}
                      >
                        {isActive
                          ? "In progress…"
                          : e.last_run && e.last_run.state !== "cancelled"
                          ? "Run again"
                          : "Proceed to ERP"}
                      </button>
                    )}
                  </td>
                </tr>
                {open && (
                  <tr className="detail-row">
                    <td colSpan={22 + accounts.length + (showOwner ? 1 : 0)}><Lines e={e} saveEnabled={saveEnabled} /></td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
