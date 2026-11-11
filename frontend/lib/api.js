export async function api(path, opts = {}) {
  const r = await fetch(`/api${path}`, {
    cache: "no-store",
    ...opts,
    headers: { "content-type": "application/json", ...(opts.headers || {}) },
  });
  let data = null;
  try {
    data = await r.json();
  } catch (e) {
    data = null;
  }
  if (!r.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === "string" ? d : d ? JSON.stringify(d) : `HTTP ${r.status}`);
  }
  return data;
}

export const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });
