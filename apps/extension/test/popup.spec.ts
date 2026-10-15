import { chromium, expect, test, type BrowserContext, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, realpathSync, rmSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import type { AddressInfo } from "node:net";

// The built extension in Chromium: the popup's two states, the hand-off to the web app's
// onboarding, unsupported pages, an unreachable web app, and the declared permissions.

const ROOT = path.resolve(__dirname, "..");
const NAME = "Example Product";
const TEAM = "https://fantasy.premierleague.com/entry/1234567/event/7";

// a stand-in for the web app: answers every path and records what it was asked
type Hit = { method: string; url: string; headers: http.IncomingHttpHeaders };
function standIn(): Promise<{ origin: string; hits: Hit[]; close: () => Promise<void> }> {
  const hits: Hit[] = [];
  const server = http.createServer((req, res) => {
    hits.push({ method: req.method ?? "", url: req.url ?? "", headers: req.headers });
    res.writeHead(200, { "content-type": "text/html" });
    res.end("<title>stand-in web app</title><p>ok</p>");
  });
  return new Promise((resolve) =>
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address() as AddressInfo;
      resolve({ origin: `http://127.0.0.1:${port}`, hits, close: () => new Promise((r) => server.close(() => r())) });
    }),
  );
}

// a port nothing listens on (taken, then released)
function closedPort(): Promise<number> {
  return new Promise((resolve) => {
    const s = http.createServer();
    s.listen(0, "127.0.0.1", () => {
      const { port } = s.address() as AddressInfo;
      s.close(() => resolve(port));
    });
  });
}

function build(url: string, out: string): string {
  execFileSync(path.join(ROOT, "build.sh"), ["--url", url, "--name", NAME, "--out", out], { stdio: "pipe" });
  return realpathSync(path.join(out, "unpacked"));
}

// Chrome derives an unpacked extension's id from its directory path: the first 16 bytes of
// SHA-256(path), each nibble written as a letter a–p
function extensionId(dir: string): string {
  const hex = createHash("sha256").update(dir).digest("hex").slice(0, 32);
  return [...hex].map((c) => String.fromCharCode("a".charCodeAt(0) + parseInt(c, 16))).join("");
}

async function launch(dir: string): Promise<BrowserContext> {
  return chromium.launchPersistentContext("", {
    channel: "chromium",
    args: [`--disable-extensions-except=${dir}`, `--load-extension=${dir}`],
  });
}

// the popup as Chrome opens it for a click on the toolbar button while `tabUrl` is the page
async function popup(context: BrowserContext, dir: string, tabUrl?: string): Promise<Page> {
  const page = await context.newPage();
  if (tabUrl !== undefined) {
    // what chrome.tabs.query answers for the clicked tab (activeTab grants its address)
    await page.addInitScript((url) => {
      const query = async () => [{ url }];
      const c = (window as unknown as { chrome: { tabs?: { query: unknown } } }).chrome;
      try {
        c.tabs!.query = query;
      } catch {
        /* defined below */
      }
      if (c.tabs?.query !== query) Object.defineProperty(c, "tabs", { value: { query }, configurable: true });
    }, tabUrl);
  }
  await page.goto(`chrome-extension://${extensionId(dir)}/popup.html`);
  await expect(page.locator("#action")).toBeVisible();
  return page;
}

let out: string;
let app: Awaited<ReturnType<typeof standIn>>;
let dir: string;
let context: BrowserContext;

test.beforeAll(async () => {
  out = mkdtempSync(path.join(tmpdir(), "fpl-ext-"));
  app = await standIn();
  dir = build(app.origin, out);
  context = await launch(dir);
});

test.afterAll(async () => {
  await context?.close();
  await app?.close();
  rmSync(out, { recursive: true, force: true });
});

test("the installed extension declares activeTab only and carries the product's name", async () => {
  const page = await popup(context, dir);
  const manifest = await page.evaluate(() => (window as unknown as { chrome: { runtime: { getManifest(): Record<string, unknown> } } }).chrome.runtime.getManifest());
  expect(manifest.permissions).toEqual(["activeTab"]);
  for (const k of ["host_permissions", "content_scripts", "background", "optional_permissions"]) expect(manifest).not.toHaveProperty(k);
  expect(manifest.name).toBe(NAME);
  await expect(page).toHaveTitle(NAME);
  await expect(page.locator("#product")).toHaveText(NAME);
  await expect(page.locator("#dest")).toHaveText(new URL(app.origin).host);
  await page.close();
});

test("on a page that is not a team page the popup offers the web app and says what would work", async () => {
  // opened as a tab: the active tab is the popup itself — the real chrome.tabs.query, no stub
  const page = await popup(context, dir);
  await expect(page.locator("#status")).toHaveText("No team ID in this page's address.");
  const action = page.locator("#action");
  await expect(action).toHaveAttribute("data-kind", "site");
  await expect(action).toHaveAttribute("href", app.origin);
  await expect(action).toHaveText(`Open ${NAME}`);
  await expect(page.locator("#hint")).toContainText("fantasy.premierleague.com");
  await page.close();
});

test("on a team page the popup hands the team id to the web app's onboarding, and nothing else", async () => {
  const page = await popup(context, dir, TEAM);
  await expect(page.locator("#status")).toHaveText("Team 1234567 found in this page's address.");
  const action = page.locator("#action");
  await expect(action).toHaveAttribute("data-kind", "team");
  await expect(action).toHaveAttribute("href", `${app.origin}/onboarding?entry=1234567`);
  await expect(action).toHaveText(`Open team 1234567 in ${NAME}`);
  await expect(page.locator("#hint")).toBeHidden();
  app.hits.length = 0;
  const [opened] = await Promise.all([context.waitForEvent("page"), action.click()]);
  await opened.waitForLoadState();
  expect(opened.url()).toBe(`${app.origin}/onboarding?entry=1234567`);
  const hit = app.hits.find((h) => h.url === "/onboarding?entry=1234567");
  expect(hit?.method).toBe("GET");
  // the request carries no credential, no FPL cookie and nothing from the FPL page
  expect(hit?.headers.cookie).toBeUndefined();
  expect(hit?.headers.authorization).toBeUndefined();
  expect(Object.keys(hit?.headers ?? {}).filter((k) => k.startsWith("x-"))).toEqual([]);
  await opened.close();
  await page.close();
});

test("unsupported FPL pages, missing and invalid ids fall back to the plain link", async () => {
  for (const url of [
    "https://fantasy.premierleague.com/",
    "https://fantasy.premierleague.com/my-team",
    "https://fantasy.premierleague.com/leagues/123/standings/c",
    "https://fantasy.premierleague.com/entry/",
    "https://fantasy.premierleague.com/entry/0/event/1",
    "https://fantasy.premierleague.com/entry/12ab/event/1",
    "http://fantasy.premierleague.com/entry/42/event/1",
    "https://fantasy.premierleague.com.evil.test/entry/42/event/1",
    "https://example.org/",
  ]) {
    const page = await popup(context, dir, url);
    await expect(page.locator("#action"), url).toHaveAttribute("data-kind", "site");
    await expect(page.locator("#action"), url).toHaveAttribute("href", app.origin);
    await expect(page.locator("#hint"), url).toBeVisible();
    await page.close();
  }
});

test("when the web app is unreachable the popup still works and the link shows where it would go", async () => {
  const port = await closedPort();
  const down = mkdtempSync(path.join(tmpdir(), "fpl-ext-down-"));
  const downDir = build(`http://127.0.0.1:${port}`, down);
  const ctx = await launch(downDir);
  try {
    const page = await popup(ctx, downDir, TEAM);
    await expect(page.locator("#dest")).toHaveText(`127.0.0.1:${port}`);
    await expect(page.locator("#action")).toHaveAttribute("href", `http://127.0.0.1:${port}/onboarding?entry=1234567`);
    const requested: string[] = [];
    ctx.on("request", (r) => requested.push(r.url()));
    const [opened] = await Promise.all([ctx.waitForEvent("page"), page.locator("#action").click()]);
    await opened.waitForLoadState().catch(() => undefined);
    // the browser tried the intended address and shows its own "cannot connect" page; the popup
    // did not break and sent the user nowhere else
    await expect.poll(() => requested).toContain(`http://127.0.0.1:${port}/onboarding?entry=1234567`);
    expect(requested.filter((u) => !u.startsWith("chrome-extension://"))).toEqual([`http://127.0.0.1:${port}/onboarding?entry=1234567`]);
    expect(opened.url()).toBe("chrome-error://chromewebdata/");
    await expect(page.locator("#action")).toBeVisible();
  } finally {
    await ctx.close();
    rmSync(down, { recursive: true, force: true });
  }
});
