"use client";
import { signOut, useSession } from "next-auth/react";

export default function UserMenu() {
  const { data } = useSession();
  const email = data && data.user && data.user.email;
  if (!email) return null;
  return (
    <div className="usermenu">
      <span className="muted small" title="Signed in with Google">{email}</span>
      <button className="btn btn-small" onClick={() => signOut({ callbackUrl: "/login" })}>Sign out</button>
    </div>
  );
}
