/** The destination of a post-sign-in redirect must be a path on this site: one leading "/",
 * never "//host" or "/\host" (protocol-relative addresses the browser would follow elsewhere). */
export function sameSitePath(next: string | null | undefined, fallback: string): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return fallback;
  return next;
}
