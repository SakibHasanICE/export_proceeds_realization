"use client";
import { useState } from "react";
import { del, put } from "../lib/api";

// "My ERP login": every user saves their own ERP username and password once. The server keeps the password
// encrypted and the robot logs in with it when this user presses Proceed to ERP.
export default function ErpLoginButton({ login, onChange, onNotice }) {
  const [open, setOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const mode = (login && login.mode) || "missing";

  function show() {
    setUsername((login && login.username) || "");
    setPassword("");
    setError(null);
    setOpen(true);
  }

  async function save() {
    if (!username.trim() || !password) {
      setError("Enter your ERP username and password.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await put("/erp/credentials/me", { username: username.trim(), password });
      setPassword("");
      setOpen(false);
      onNotice("Your ERP login is saved. It is used when you press Proceed to ERP.", 6000);
      onChange();
    } catch (e) {
      setError(e.message);
    }
    setBusy(false);
  }

  async function remove() {
    setBusy(true);
    try {
      await del("/erp/credentials/me");
      setOpen(false);
      onNotice("Your ERP login was removed.", 5000);
      onChange();
    } catch (e) {
      setError(e.message);
    }
    setBusy(false);
  }

  const missing = mode === "missing";
  return (
    <>
      <button
        className="btn"
        style={missing ? { borderColor: "#d1242f", color: "#d1242f", fontWeight: 600 } : undefined}
        title={missing ? "Save your ERP login before you press Proceed to ERP" : login && login.username ? `ERP user: ${login.username}` : "ERP login"}
        onClick={show}
      >
        {missing ? "My ERP login: not set" : mode === "personal" ? "My ERP login ✓" : "My ERP login"}
      </button>
      {open && (
        <div className="modal-backdrop" onClick={() => !busy && setOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3 style={{ margin: "0 0 6px" }}>My ERP login</h3>
            <p className="muted small" style={{ margin: "0 0 12px" }}>
              Enter your own ERP username and password once. When you press Proceed to ERP, the robot signs in to the
              ERP as you, so the entry is saved under your name. The password is stored encrypted and is never shown again.
            </p>
            <label className="small">ERP username</label>
            <input className="input" style={{ width: "100%", marginBottom: 10 }} value={username} autoComplete="off"
                   onChange={(e) => setUsername(e.target.value)} />
            <label className="small">ERP password</label>
            <input className="input" style={{ width: "100%", marginBottom: 10 }} type="password" value={password}
                   autoComplete="new-password" onChange={(e) => setPassword(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && save()} />
            {error && <div style={{ color: "#d1242f", marginBottom: 10 }}>{error}</div>}
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              {mode === "personal" && <button className="btn" disabled={busy} onClick={remove}>Remove</button>}
              <button className="btn" disabled={busy} onClick={() => setOpen(false)}>Cancel</button>
              <button className="btn btn-primary" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save"}</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
