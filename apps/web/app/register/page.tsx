"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { useUser } from "@/components/auth";
import { Card } from "@/components/ui";

export default function Register() {
  const router = useRouter();
  const { reload } = useUser();
  const [invite, setInvite] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const res = await fetch("/auth/register", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ invite_code: invite.trim(), email, password }),
      });
      const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
      if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `sign-up failed (${res.status})`);
      await reload();
      router.replace("/onboarding");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Create your account" testId="register">
      <form onSubmit={submit} className="kv">
        <label htmlFor="invite">Invitation code</label>
        <input id="invite" required value={invite} onChange={(e) => setInvite(e.target.value)} autoComplete="off" />
        <label htmlFor="email">E-mail</label>
        <input id="email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        <label htmlFor="password">Password (10+ characters)</label>
        <input id="password" type="password" autoComplete="new-password" minLength={10} required value={password} onChange={(e) => setPassword(e.target.value)} />
        <span />
        <div className="row">
          <button type="submit" disabled={busy} data-testid="register-submit">{busy ? "Creating…" : "Create account"}</button>
          <Link href="/login" className="small">Already registered? Sign in</Link>
        </div>
      </form>
      {err ? <p className="error" role="alert">{err}</p> : null}
      <p className="small">
        We store your e-mail (as your login), a salted hash of your password, your FPL squad data and,
        unless you opt out in Settings, a few anonymous usage events to improve the beta. You can export
        or erase everything from Settings at any time.
      </p>
    </Card>
  );
}
