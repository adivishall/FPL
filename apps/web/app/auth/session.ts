// Shared by the sign-in, sign-up and sign-out route handlers: they are the only code that reads
// the API's session token, and they keep it in an HttpOnly cookie scoped to this origin.

import { type NextRequest } from "next/server";

export const SESSION_COOKIE = "fpl_session";
const ORIGIN = process.env.FPL_API_INTERNAL_URL ?? "http://localhost:8000";
const MAX_AGE_SECONDS = 30 * 24 * 3600; // matches the API's default session lifetime

export interface AuthResult {
  ok: boolean;
  status: number;
  json: Record<string, unknown>;
  passthrough: Response;
}

/** POST a JSON body to an auth route on the API with the server-held key (and the current
 * session as bearer when one exists). */
export async function apiAuth(path: string, body: string, session?: string): Promise<AuthResult> {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (process.env.FPL_API_KEY) headers["x-api-key"] = process.env.FPL_API_KEY;
  if (session) headers.authorization = `Bearer ${session}`;
  let res: Response;
  try {
    res = await fetch(new URL(path, ORIGIN), {
      method: "POST",
      headers,
      body,
      cache: "no-store",
      signal: AbortSignal.timeout(20_000),
    });
  } catch {
    const down = Response.json({ detail: "API unreachable" }, { status: 502 });
    return { ok: false, status: 502, json: {}, passthrough: down };
  }
  const text = await res.text();
  let json: Record<string, unknown> = {};
  try {
    json = JSON.parse(text) as Record<string, unknown>;
  } catch {
    json = { detail: text };
  }
  // never forward the session token to the browser: the passthrough carries only the error
  const safe = res.ok ? { user: json.user } : json;
  const passthrough = new Response(JSON.stringify(safe), {
    status: res.status,
    headers: { "content-type": "application/json" },
  });
  return { ok: res.ok, status: res.status, json, passthrough };
}

export function sessionCookie(req: NextRequest, token: string): string {
  const secure = req.nextUrl.protocol === "https:" || req.headers.get("x-forwarded-proto") === "https";
  return `${SESSION_COOKIE}=${token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${MAX_AGE_SECONDS}${secure ? "; Secure" : ""}`;
}

export function clearedCookie(): string {
  return `${SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0`;
}
