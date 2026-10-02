// Server-side proxy to the API (§75): when the API requires keys, the key lives only in the Next
// server's environment (FPL_API_KEY) and is never shipped to the browser. Paths are confined to
// /api/v1 on the configured internal origin — the proxy cannot be pointed at arbitrary hosts.

import { type NextRequest } from "next/server";

const ORIGIN = process.env.FPL_API_INTERNAL_URL ?? "http://localhost:8000";
const SAFE = /^[A-Za-z0-9_.\-]+$/;

async function forward(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const { path } = await ctx.params;
  if (!path.every((p) => SAFE.test(p) && p !== "..")) {
    return Response.json({ detail: "invalid path" }, { status: 400 });
  }
  const url = new URL(`/api/v1/${path.join("/")}`, ORIGIN);
  url.search = req.nextUrl.search;
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (process.env.FPL_API_KEY) headers["x-api-key"] = process.env.FPL_API_KEY;
  const rid = req.headers.get("x-request-id");
  if (rid) headers["x-request-id"] = rid;
  const res = await fetch(url, {
    method: req.method,
    headers,
    body: req.method === "GET" || req.method === "DELETE" ? undefined : await req.text(),
    cache: "no-store",
    redirect: "manual",
  });
  const out = new Headers({ "content-type": res.headers.get("content-type") ?? "application/json" });
  const retry = res.headers.get("retry-after");
  if (retry) out.set("retry-after", retry);
  return new Response(await res.arrayBuffer(), { status: res.status, headers: out });
}

export const GET = forward;
export const POST = forward;
export const DELETE = forward;
export const dynamic = "force-dynamic";
