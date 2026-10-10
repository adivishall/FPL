// Sign-in: the Next server exchanges e-mail + password for a session token at the API (with the
// server-held web key) and stores the token in an HttpOnly cookie. The browser's JavaScript never
// sees the token; every later /backend/* call carries it server-side (see backend/[...path]).

import { type NextRequest } from "next/server";

import { apiAuth, sessionCookie } from "../session";

export async function POST(req: NextRequest): Promise<Response> {
  const body = await req.text();
  const res = await apiAuth("/api/v1/auth/login", body);
  if (!res.ok) return res.passthrough;
  const headers = new Headers({ "content-type": "application/json" });
  headers.append("set-cookie", sessionCookie(req, res.json.session_token as string));
  return new Response(JSON.stringify({ user: res.json.user }), { status: 200, headers });
}

export const dynamic = "force-dynamic";
