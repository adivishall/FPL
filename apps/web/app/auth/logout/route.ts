// Sign-out: revoke the session at the API, then clear the cookie whatever the API answered.

import { type NextRequest } from "next/server";

import { SESSION_COOKIE, apiAuth, clearedCookie } from "../session";

export async function POST(req: NextRequest): Promise<Response> {
  const session = req.cookies.get(SESSION_COOKIE)?.value;
  if (session) await apiAuth("/api/v1/auth/logout", "{}", session);
  const headers = new Headers({ "content-type": "application/json" });
  headers.append("set-cookie", clearedCookie());
  return new Response(JSON.stringify({ signed_out: true }), { status: 200, headers });
}

export const dynamic = "force-dynamic";
