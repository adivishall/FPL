"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { useUser } from "@/components/auth";
import { Card } from "@/components/ui";

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const { reload } = useUser();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const next = params.get("next") || "/";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const res = await fetch("/auth/login", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      const body = (await res.json().catch(() => ({}))) as { detail?: string };
      if (!res.ok) throw new Error(body.detail ?? `sign-in failed (${res.status})`);
      await reload();
      router.replace(next.startsWith("/") ? next : "/");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Sign in" testId="login">
      <form onSubmit={submit} className="kv">
        <label htmlFor="email">E-mail</label>
        <input id="email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        <label htmlFor="password">Password</label>
        <input id="password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        <span />
        <div className="row">
          <button type="submit" disabled={busy} data-testid="login-submit">{busy ? "Signing in…" : "Sign in"}</button>
          <Link href="/register" className="small">Have an invitation? Create an account</Link>
        </div>
      </form>
      {err ? <p className="error" role="alert">{err}</p> : null}
      <p className="small">This is an invite-only beta. Sessions are stored in an HttpOnly cookie on this site and expire after 30 days.</p>
    </Card>
  );
}

export default function Login() {
  return (
    <Suspense fallback={null}>
      <LoginForm />
    </Suspense>
  );
}
