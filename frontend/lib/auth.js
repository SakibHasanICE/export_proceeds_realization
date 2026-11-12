// Google sign-in, limited to one Google Workspace domain (default urmigroup.net).
import GoogleProvider from "next-auth/providers/google";

export const DOMAIN = (process.env.ALLOWED_EMAIL_DOMAIN || "urmigroup.net").trim().toLowerCase().replace(/^@/, "");

export const authOptions = {
  providers: [
    GoogleProvider({
      clientId: process.env.GOOGLE_CLIENT_ID || "",
      clientSecret: process.env.GOOGLE_CLIENT_SECRET || "",
      // "hd" only makes Google's account picker show work accounts. It is a hint, not security: the check below is.
      authorization: { params: { hd: DOMAIN, prompt: "select_account" } },
    }),
  ],
  session: { strategy: "jwt", maxAge: 12 * 60 * 60 }, // signed in for 12 hours
  pages: { signIn: "/login", error: "/login" },
  callbacks: {
    // The real gate. Needs a verified address on the company domain AND Google's own hosted-domain claim, so a
    // personal Google account that merely uses an @urmigroup.net address is refused too.
    async signIn({ account, profile }) {
      if (account?.provider !== "google") return false;
      const email = String(profile?.email || "").toLowerCase();
      const hd = String(profile?.hd || "").toLowerCase();
      return profile?.email_verified === true && hd === DOMAIN && email.endsWith("@" + DOMAIN);
    },
  },
};
