"use client";
import { useRef, useState } from "react";
import { uploadFiles } from "../lib/upload";

// Manual upload: bank advice PDFs and EBL realization sheets (.xlsx). Same endpoint the old Streamlit tab used.
export default function UploadButton({ onDone, onNotice }) {
  const input = useRef(null);
  const [busy, setBusy] = useState(false);

  async function send(files) {
    if (!files || !files.length) return;
    setBusy(true);
    const { lines, lastId } = await uploadFiles(files);
    setBusy(false);
    if (input.current) input.current.value = "";
    onNotice(lines.join("\n"), 20000);
    onDone(lastId);
  }

  return (
    <>
      <input ref={input} type="file" accept=".pdf,.xlsx" multiple hidden onChange={(e) => send(e.target.files)} />
      <button className="btn btn-primary" disabled={busy} onClick={() => input.current && input.current.click()}>
        {busy ? "Reading document…" : "＋ Upload PDF / Excel"}
      </button>
    </>
  );
}
