// Upload bank advice PDFs / EBL sheets. If the same bill is already in the table, ask before replacing it
// (only entries nobody has filled yet can be replaced) - useful after the extraction rules were improved.
async function post(file, replace) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(`/api/upload${replace ? "?replace=true" : ""}`, { method: "POST", body: fd, cache: "no-store" });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === "string" ? d : `HTTP ${r.status}`);
  }
  return data;
}

function describe(x, name, lines) {
  const items = x.results && x.results.length ? x.results : [x];
  for (const it of items) {
    const label = it.bill || it.file || name;
    const what =
      it.status === "other_document"
        ? "not an export payment advice, saved under Other documents"
        : it.status === "duplicate"
        ? `already in the table as #${it.id}`
        : `added as entry #${it.id}`;
    lines.push(`✓ ${label}: ${what}${it.needs_review ? " (needs review)" : ""}`);
    (it.issues || []).slice(0, 3).forEach((i) => lines.push(`   • ${i}`));
  }
}

export async function uploadFiles(files) {
  const lines = [];
  let lastId = null;
  for (const f of Array.from(files || [])) {
    try {
      let data = await post(f, false);
      if (data.status === "duplicate") {
        const ids = (data.results || [data]).filter((x) => x.status === "duplicate").map((x) => `#${x.id}`).join(", ");
        const again = window.confirm(
          `${f.name} is already in the table (${ids}).\n\nRead it again and replace the existing entry with the new data?\n` +
            "(Entries that were already filled in the ERP are never replaced.)"
        );
        if (again) data = await post(f, true);
      }
      describe(data, f.name, lines);
      const ids = (data.results || [data]).filter((x) => x.id && x.status !== "other_document").map((x) => x.id);
      if (ids.length) lastId = ids[0];
    } catch (e) {
      lines.push(`✗ ${f.name}: ${e.message}`);
    }
  }
  return { lines, lastId };
}
