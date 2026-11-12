import { redirect } from "next/navigation";
import { getServerSession } from "next-auth";
import LoginButton from "../../components/LoginButton";
import { DOMAIN, authOptions } from "../../lib/auth";

export const dynamic = "force-dynamic";
export const metadata = { title: "Sign in · Export Proceeds Realization" };

export default async function LoginPage({ searchParams }) {
  const session = await getServerSession(authOptions);
  if (session && session.user && session.user.email) redirect("/");
  return (
    <main className="login-page">
      <LoginButton domain={DOMAIN} error={searchParams && searchParams.error} />
    </main>
  );
}
