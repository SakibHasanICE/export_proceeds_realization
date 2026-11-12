// Every page needs a signed-in user; unauthenticated visitors go to /login.
// (/api is not listed: the proxy route checks the session itself and answers 401 as JSON.)
//
// The redirect is built from NEXTAUTH_URL, the address people type in the browser. Inside Docker the server only
// knows its own internal address (port 3000), so a redirect built from the request would point to the wrong port.
import { NextResponse } from "next/server";
import { getToken } from "next-auth/jwt";

export async function middleware(req) {
  const token = await getToken({ req, secret: process.env.NEXTAUTH_SECRET });
  if (token) return NextResponse.next();
  return NextResponse.redirect(new URL("/login", process.env.NEXTAUTH_URL || req.url));
}

export const config = {
  matcher: ["/((?!api|login|_next/static|_next/image|favicon.ico).*)"],
};
