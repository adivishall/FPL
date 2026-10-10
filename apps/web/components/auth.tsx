"use client";

import { usePathname, useRouter } from "next/navigation";
import { type ReactNode, createContext, useCallback, useContext, useEffect, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { loadSettings, saveSettings } from "@/lib/settings";

export interface Manager {
  manager_key: string;
  label: string | null;
  created_at: string;
}

export interface User {
  id: string;
  email: string;
  created_at: string;
  analytics_opt_out: boolean;
  managers: Manager[];
}

interface AuthState {
  user: User | null;
  loading: boolean;
  error: string | null; // the session check failed for a reason other than "not signed in"
  reload: () => Promise<void>;
}

const AuthContext = createContext<AuthState>({ user: null, loading: true, error: null, reload: async () => undefined });
const PUBLIC = new Set(["/login", "/register"]);

/** Keeps the browser's active manager key one of the signed-in user's own (the API refuses any
 * other anyway); a user whose stored key is stale or missing gets their first manager. */
function reconcileActiveManager(user: User): void {
  const s = loadSettings();
  const keys = user.managers.map((m) => m.manager_key);
  const first = keys[0];
  if (first && !keys.includes(s.managerKey)) saveSettings({ ...s, managerKey: first });
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const path = usePathname();
  const router = useRouter();
  const reload = useCallback(async () => {
    try {
      const r = await api<{ user: User }>("/auth/me");
      reconcileActiveManager(r.user);
      setUser(r.user);
      setError(null);
    } catch (e) {
      setUser(null);
      if (e instanceof ApiError && e.status === 401) {
        setError(null);
        if (!PUBLIC.has(path)) router.replace(`/login?next=${encodeURIComponent(path)}`);
      } else {
        // the API is down, unreachable or refused the server's key: say so, never spin forever
        setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
      }
    } finally {
      setLoading(false);
    }
  }, [path, router]);
  useEffect(() => {
    void reload();
  }, [reload]);
  return <AuthContext.Provider value={{ user, loading, error, reload }}>{children}</AuthContext.Provider>;
}

export function useUser(): AuthState {
  return useContext(AuthContext);
}

/** Renders children only for a signed-in user; public pages render regardless. */
export function AuthGate({ children }: { children: ReactNode }) {
  const { user, loading, error } = useUser();
  const path = usePathname();
  if (PUBLIC.has(path)) return <>{children}</>;
  if (loading) return <p className="muted" role="status">Checking your session…</p>;
  if (error) return <p className="error" role="alert">Your session could not be verified: {error}</p>;
  if (!user) return <p className="muted">Redirecting to sign-in…</p>;
  return <>{children}</>;
}

export async function signOut(): Promise<void> {
  await fetch("/auth/logout", { method: "POST" });
  window.location.href = "/login";
}
