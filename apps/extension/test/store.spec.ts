import { chromium, expect, test, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { realpathSync } from "node:fs";
import path from "node:path";

// Not a test: renders the built popup (EXT_DIR) at store size for the listing's screenshots.
// Run through tools/store_shots.sh (STORE_SHOTS=1); skipped otherwise.
test.skip(!process.env.STORE_SHOTS, "store screenshots only (tools/store_shots.sh)");

function extensionId(dir: string): string {
  const hex = createHash("sha256").update(dir).digest("hex").slice(0, 32);
  return [...hex].map((c) => String.fromCharCode("a".charCodeAt(0) + parseInt(c, 16))).join("");
}

async function stage(page: Page, caption: string) {
  // a backdrop and a caption around the real popup, for the store only
  await page.addStyleTag({
    content: `
      html { background: #37003c; }
      body { width: 320px; margin: 140px auto 0; border-radius: 12px; overflow: hidden; box-shadow: 0 24px 60px rgba(0,0,0,.45); }
      body::after { content: "${caption.replace(/"/g, '\\"')}"; position: fixed; left: 0; right: 0; bottom: 90px; text-align: center;
        color: #fff; font: 600 26px/1.3 system-ui, sans-serif; }
    `,
  });
}

test("store screenshots", async () => {
  const dir = realpathSync(process.env.EXT_DIR!);
  const out = process.env.OUT_DIR!;
  const context = await chromium.launchPersistentContext("", {
    channel: "chromium",
    viewport: { width: 1280, height: 800 },
    args: [`--disable-extensions-except=${dir}`, `--load-extension=${dir}`],
  });
  try {
    const team = await context.newPage();
    await team.addInitScript(() => {
      Object.defineProperty((window as unknown as { chrome: object }).chrome, "tabs", {
        value: { query: async () => [{ url: "https://fantasy.premierleague.com/entry/1234567/event/7" }] },
        configurable: true,
      });
    });
    await team.goto(`chrome-extension://${extensionId(dir)}/popup.html`);
    await expect(team.locator("#action")).toHaveAttribute("data-kind", "team");
    await stage(team, "On a team page: one click to the squad import, ID filled in");
    await team.screenshot({ path: path.join(out, "popup-team.png"), fullPage: false });

    const site = await context.newPage();
    await site.goto(`chrome-extension://${extensionId(dir)}/popup.html`);
    await expect(site.locator("#action")).toHaveAttribute("data-kind", "site");
    await stage(site, "Anywhere else: open the web app, and see which pages carry a team ID");
    await site.screenshot({ path: path.join(out, "popup-site.png"), fullPage: false });
  } finally {
    await context.close();
  }
});
