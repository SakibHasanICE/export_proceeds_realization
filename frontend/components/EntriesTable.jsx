"use client";
import { Fragment } from "react";
import { DbStatus, ErpBadge } from "./Badges";
import { dmy, money, rate } from "../lib/format";

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

function Lines({ e }) {
  const t = e.totals || {};
  return (
    <div className="detail">
      <div className="detail-note">
        <b>What the robot will do:</b> select the Beneficiary, double-click Bill/Invoice No and pick the bill, then type
        the Received Date and the tables below. The ERP fills {e.erp_fills_itself.join(", ")} by itself from the bill.
        <b> Nothing is saved.</b>
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

export default function EntriesTable({ entries, accounts, showLines, activeIds, highlightId, onRun, onOpenRun, expanded, onToggle }) {
  if (entries.length === 0) return <div className="empty">No documents match the filter.</div>;
  return (
    <div className="table-wrap">
      <table className="main">
        <thead>
          <tr>
            <th className="stick-l" />
            <th>ID</th>
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
            <th className="stick-r">Action</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => {
            const isActive = activeIds.has(e.id);
            const open = showLines || expanded.has(e.id);
            const bal = e.totals.cumulative_bill_balance;
            const reason = !e.can_run ? e.blockers.join("; ") : isActive ? "Already queued or running" : "";
            return (
              <Fragment key={e.id}>
                <tr className={isActive ? "row-active" : e.id === highlightId ? "row-new" : ""}>
                  <td className="stick-l">
                    <button className="chev" onClick={() => onToggle(e.id)} title="Show / hide the line details">
                      {open ? "▾" : "▸"}
                    </button>
                  </td>
                  <td className="mono">{e.id}</td>
                  <td>{e.bank}</td>
                  <td className="mono strong">{e.bill_invoice_no}</td>
                  <td>{e.beneficiary || <span className="muted">—</span>}</td>
                  <td>{e.buyer || <span className="muted">—</span>}</td>
                  <td>{e.lc_sc_no || <span className="muted">—</span>}</td>
                  <td>{dmy(e.received_date)}</td>
                  <td>{dmy(e.bill_invoice_date)}</td>
                  <td>{e.currency}</td>
                  <td className="num">{money(e.bill_invoice_amount)}</td>
                  <td className="num">{rate(e.conversion_rate)}</td>
                  <td className="num">{money(e.totals.deductions_document)}</td>
                  {accounts.map((a) => {
                    const v = acctAmount(e, a.no);
                    return (
                      <td key={a.no} className="num">
                        {v === null ? <span className="muted">—</span> : money(v)}
                      </td>
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
                  <td className="stick-r">
                    {e.status === "filled" ? (
                      <span className="saved">✓ Saved in ERP</span>
                    ) : (
                      <button
                        className="btn btn-primary"
                        disabled={!e.can_run || isActive}
                        title={reason || "Fill this document in the ERP (nothing is saved)"}
                        onClick={() => onRun([e.id])}
                      >
                        {isActive ? "In progress…" : e.last_run && e.last_run.state !== "cancelled" ? "Run again" : "Proceed to ERP"}
                      </button>
                    )}
                  </td>
                </tr>
                {open && (
                  <tr className="detail-row">
                    <td colSpan={19 + accounts.length}><Lines e={e} /></td>
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
