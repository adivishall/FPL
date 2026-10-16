"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { useUser } from "@/components/auth";
import { useProductName } from "@/components/product";
import { Card } from "@/components/ui";
import { sameSitePath } from "@/lib/nav";

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const { reload } = useUser();
  const product = useProductName();
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
      router.replace(sameSitePath(next, "/"));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth">
    <Card title="Sign in" testId="login" tone="accent">
      <p className="small">{product} is an invite-only beta: probabilistic forecasts, whole-squad planning and a journal that checks itself against the official results.</p>
      <form onSubmit={submit} className="kv">
        <label htmlFor="email">E-mail</label>
        <input id="email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        <label htmlFor="password">Password</label>
        <input id="password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        <span />
        <div className="row">
          <button type="submit" disabled={busy} data-testid="login-submit">{busy ? "Signing in…" : "Sign in"}</button>
          <Link href={next !== "/" ? `/register?next=${encodeURIComponent(next)}` : "/register"} className="small">Have an invitation? Create an account</Link>
        </div>
      </form>
      {err ? <p className="error" role="alert">{err}</p> : null}
      <p className="small">Sessions are stored in an HttpOnly cookie on this site and expire after 30 days.</p>
    </Card>
    </div>
  );
}

export default function Login() {
  return (
    <Suspense fallback={null}>
      <LoginForm />
    </Suspense>
  );
}
