// Sign-up with an invitation code: the API creates the account and a first session; the token is
// stored in the HttpOnly cookie exactly as at sign-in.

import { type NextRequest } from "next/server";

import { apiAuth, sessionCookie } from "../session";

export async function POST(req: NextRequest): Promise<Response> {
  const body = await req.text();
  const res = await apiAuth("/api/v1/auth/register", body);
  if (!res.ok) return res.passthrough;
  const headers = new Headers({ "content-type": "application/json" });
  headers.append("set-cookie", sessionCookie(req, res.json.session_token as string));
  return new Response(JSON.stringify({ user: res.json.user }), { status: 200, headers });
}

export const dynamic = "force-dynamic";
