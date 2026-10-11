import { expect, request, test, type APIRequestContext, type Cookie, type Page } from "@playwright/test";

// Portfolio screenshots (§89) of a running deployment — opt-in: SCREENSHOTS=1 with the same
// environment as production.spec.ts (E2E_BASE_URL, E2E_API_URL, E2E_OPS_KEY). Writes
// docs/images/*.png (generated, committed evidence). Runs as a throw-away beta user with a squad
// built by the initial-squad optimiser — no real manager's data — and erases everything after.
const OUT = "../../docs/images";
const API = process.env.E2E_API_URL ?? "http://127.0.0.1:8000";
const OPS_KEY = process.env.E2E_OPS_KEY ?? "";

let MANAGER = "";
let cookies: Cookie[] = [];
let web: APIRequestContext;

test.describe.configure({ mode: "serial" });
test.use({ viewport: { width: 1440, height: 1000 } });

test.beforeAll(async ({ baseURL }) => {
  expect(OPS_KEY, "E2E_OPS_KEY must be set").not.toBe("");
  const api = await request.newContext({ baseURL: API });
  const inv = await api.post("/api/v1/auth/invites", { data: { label: "screenshots", days: 1 }, headers: { "x-api-key": OPS_KEY } });
  expect(inv.ok(), await inv.text()).toBe(true);
  web = await request.newContext({ baseURL });
  const reg = await web.post("/auth/register", {
    data: { invite_code: (await inv.json()).invite_code, email: `screens-${Date.now()}@example.test`, password: "screenshots-only-password" },
  });
  expect(reg.ok(), await reg.text()).toBe(true);
  const mgr = await web.post("/backend/auth/managers", { data: { label: "demo squad" } });
  expect(mgr.ok(), await mgr.text()).toBe(true);
  MANAGER = (await mgr.json()).manager_key as string;
  cookies = (await web.storageState()).cookies;
});

test.afterAll(async () => {
  if (web) await web.delete("/backend/auth/me"); // the account and everything it owns
});

// a full-page capture repeats a sticky header in the middle of the image: unstick it for the
// capture only (the application is unchanged)
async function fullShot(page: Page, name: string) {
  await page.addStyleTag({ content: ".nav, nav, header { position: static !important; }" });
  await page.screenshot({ path: `${OUT}/${name}`, fullPage: true });
}

async function signedIn(page: Page) {
  await page.context().addCookies(cookies);
  await page.addInitScript((key) => {
    if (window.localStorage.getItem("fpl.settings.v1")) return;
    window.localStorage.setItem("fpl.settings.v1", JSON.stringify({ managerKey: key, profile: "default", horizon: 3 }));
  }, MANAGER);
}

test("capture the decision workflow", async ({ page }) => {
  await signedIn(page);
  await page.goto("/squad");
  await page.getByTestId("build-squad").click();
  await expect(page.getByTestId("squad-msg")).toContainText(/Squad proposed by the initial-squad optimiser/);
  await page.getByTestId("save-squad").click();
  await expect(page.getByTestId("squad-msg")).toContainText("Squad saved");
  await fullShot(page, "squad-planner.png");

  // Copilot Home with a worker-generated recommendation (current)
  await page.goto("/");
  await expect(page.getByTestId("captain-profiles")).toBeVisible({ timeout: 300_000 });
  await page.getByTestId("generate").click();
  await expect(page.getByTestId("decision-text")).toBeVisible({ timeout: 420_000 });
  await expect(page.getByTestId("rec-lifecycle")).toHaveAttribute("data-state", "current");
  await page.screenshot({ path: `${OUT}/copilot-home.png` }); // the top of the page
  await fullShot(page, "copilot-home-full.png");

  // Transfer Workbench: the engine's replacements for one player vs holding, as a job
  await page.goto("/transfers");
  await page.locator("[data-testid^='out-']").nth(5).click();
  await page.getByTestId("evaluate").click();
  await expect(page.getByTestId("plan-hold")).toBeVisible({ timeout: 420_000 });
  await fullShot(page, "transfer-workbench.png");

  // Result lifecycle: the squad is saved again, the run is historical and says what changed
  const st = await (await web.get(`/backend/squad?manager_key=${MANAGER}`)).json();
  const picks = st.state.squad.map((p: { player_code: number }) => ({ player_code: p.player_code }));
  await web.post("/backend/squad", { data: { manager_key: MANAGER, picks, bank: st.state.bank + 5, free_transfers: st.state.free_transfers } });
  await page.reload();
  await expect(page.getByTestId("run-lifecycle")).toHaveAttribute("data-state", "stale");
  await page.getByTestId("what-changed-button").click();
  await expect(page.getByTestId("changed-state")).toBeVisible();
  await page.getByTestId("run-lifecycle").scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${OUT}/what-changed.png` });
  await web.post("/backend/squad", { data: { manager_key: MANAGER, picks, bank: st.state.bank, free_transfers: st.state.free_transfers } });

  await page.goto("/journal");
  await page.getByTestId("journal").locator("tbody tr a").first().click();
  await expect(page.getByTestId("trace")).toContainText("chain complete");
  await fullShot(page, "traceability.png");

  await page.goto("/health");
  await expect(page.getByTestId("services")).toContainText("live_source");
  await fullShot(page, "data-health.png");

  await page.goto("/backtests");
  await page.waitForLoadState("networkidle");
  await page.locator("img[data-figure='backtest_cumulative_difference.svg']").scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${OUT}/backtest-lab.png` }); // the full report is ~11k px tall
});
