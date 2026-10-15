// Pure decisions for the popup (no browser APIs): is the current page a public FPL team page,
// and where should the button send the user. Tested in test/lib.test.mjs.

export const FPL_HOST = "fantasy.premierleague.com";
// a public entry (team) id: /entry/<id>, /entry/<id>/event/<gw>, /entry/<id>/history, …
const ENTRY_PATH = /^\/entry\/([1-9][0-9]{0,9})(?:\/|$)/;

/** The FPL team id in a fantasy.premierleague.com team-page address, or null. */
export function teamIdFrom(url) {
  let u;
  try {
    u = new URL(String(url ?? ""));
  } catch {
    return null;
  }
  if (u.protocol !== "https:" || u.hostname !== FPL_HOST) return null;
  const m = ENTRY_PATH.exec(u.pathname);
  return m ? m[1] : null;
}

/** True when the address is on the official FPL site (any page). */
export function isFplPage(url) {
  try {
    const u = new URL(String(url ?? ""));
    return u.protocol === "https:" && u.hostname === FPL_HOST;
  } catch {
    return false;
  }
}

/**
 * What the popup offers for the current page.
 *   { kind: "team", id, href, label }  — a team page: open the web app's import with that id
 *   { kind: "site", href, label, hint } — anything else: open the web app; say what would work
 * The web app decides the rest: it asks the user to sign in (invite-only accounts) and to confirm
 * the team before anything is imported — the extension cannot bypass either.
 */
export function decide(url, { appUrl, productName }) {
  const base = String(appUrl).replace(/\/+$/, "");
  const id = teamIdFrom(url);
  if (id) {
    return { kind: "team", id, href: `${base}/onboarding?entry=${id}`, label: `Open team ${id} in ${productName}` };
  }
  return {
    kind: "site",
    href: base,
    label: `Open ${productName}`,
    hint: isFplPage(url)
      ? "This page shows no team ID. Open a team's Points, History or Transfers page (the address contains /entry/<ID>/) to import it with one click."
      : "On fantasy.premierleague.com, open a team's Points, History or Transfers page (the address contains /entry/<ID>/) to import it with one click.",
  };
}
