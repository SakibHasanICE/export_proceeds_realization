// Browser -> Next.js -> FastAPI. The API key and the backend address stay on the server.
import { getServerSession } from "next-auth";
import { authOptions } from "../../../lib/auth";

export const dynamic = "force-dynamic";

const API = (process.env.API_URL || "http://localhost:8000").replace(/\/$/, "");

async function handler(req, ctx) {
  const session = await getServerSession(authOptions);
  const email = session && session.user && session.user.email ? session.user.email.toLowerCase() : null;
  if (!email) return Response.json({ detail: "Please sign in." }, { status: 401 });
  const { path } = await ctx.params;
  const url = `${API}/${(path || []).join("/")}${new URL(req.url).search}`;
  const headers = { "X-User-Email": email }; // the backend records who pressed Proceed to ERP
  if (process.env.API_KEY) headers["X-API-Key"] = process.env.API_KEY;
  const ct = req.headers.get("content-type");
  if (ct) headers["content-type"] = ct;
  const init = { method: req.method, headers, cache: "no-store" };
  if (!["GET", "HEAD"].includes(req.method)) init.body = await req.arrayBuffer();
  try {
    const r = await fetch(url, init);
    const out = new Headers();
    for (const h of ["content-type", "cache-control"]) {
      const v = r.headers.get(h);
      if (v) out.set(h, v);
    }
    return new Response(r.body, { status: r.status, headers: out });
  } catch (e) {
    return Response.json(
      { detail: `Cannot reach the backend (${API}). Is the app container running?` },
      { status: 502 }
    );
  }
}

export { handler as GET, handler as POST, handler as PUT, handler as DELETE };
