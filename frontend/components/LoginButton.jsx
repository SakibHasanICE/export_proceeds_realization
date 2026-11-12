"use client";
import { signIn } from "next-auth/react";

const MESSAGES = {
  AccessDenied: (d) => `That Google account is not allowed. Sign in with your @${d} work account.`,
  Configuration: () => "Sign-in is not set up on the server yet (check GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, NEXTAUTH_SECRET and NEXTAUTH_URL in .env).",
};

export default function LoginButton({ domain, error }) {
  const msg = error ? (MESSAGES[error] || (() => "Sign-in failed. Please try again."))(domain) : null;
  return (
    <div className="login-card">
      <h1>Export Proceeds Realization</h1>
      <p className="muted">Sign in with your company Google account (@{domain}).</p>
      {msg && <div className="banner banner-fail">{msg}</div>}
      <button className="btn btn-primary btn-big" onClick={() => signIn("google", { callbackUrl: "/" })}>
        Sign in with Google
      </button>
    </div>
  );
}
