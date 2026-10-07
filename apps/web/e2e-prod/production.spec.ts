import { expect, request, test, type APIRequestContext, type Page } from "@playwright/test";

// Runs against a deployed stack (playwright.prod.config.ts): every browser request goes to the web
// origin; the Next server adds the API key server-side. Direct API calls below exist only to
// check enforcement and to compare what the UI shows with what the API serves.

const API = process.env.E2E_API_URL ?? "http://127.0.0.1:8000";
const OPS_KEY = process.env.E2E_OPS_KEY ?? "";
const WEB_KEY = process.env.E2E_WEB_KEY ?? "";
const BAD_KEY_WEB = process.env.E2E_BADKEY_WEB_URL; // a web server configured with a wrong key
const NO_API_WEB = process.env.E2E_NOAPI_WEB_URL; // a web server whose API is unreachable
const MANAGER = `e2e-prod-${Date.now()}`;

test.describe.configure({ mode: "serial" });

async function asManager(page: Page) {
  await page.addInitScript((key) => {
    window.localStorage.setItem("fpl.settings.v1", JSON.stringify({ managerKey: key, profile: "default", horizon: 3 }));
  }, MANAGER);
}

let api: APIRequestContext;
test.beforeAll(async () => {
  expect(OPS_KEY, "E2E_OPS_KEY must be set").not.toBe("");
  expect(WEB_KEY, "E2E_WEB_KEY must be set").not.toBe("");
  api = await request.newContext({ baseURL: API });
});

test.afterAll(async () => {
  // the export/delete test erases this manager; this covers runs that stop before it
  await api.delete(`/api/v1/managers/${MANAGER}`, { headers: { "x-api-key": OPS_KEY } });
});

test("topology: the browser only talks to the web origin and never sees the proxy key", async ({ page, baseURL }) => {
  const origins = new Set<string>();
  const keyHeaders: string[] = [];
  const bodies: string[] = [];
  page.on("request", (r) => {
    origins.add(new URL(r.url()).origin);
    if (r.headers()["x-api-key"]) keyHeaders.push(r.url());
  });
  page.on("response", async (r) => {
    const t = r.headers()["content-type"] ?? "";
    if (/javascript|html|json/.test(t)) bodies.push(await r.text().catch(() => ""));
  });
  for (const path of ["/", "/players", "/health", "/settings", "/backtests"]) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
  }
  expect([...origins]).toEqual([new URL(baseURL!).origin]);
  expect(keyHeaders).toEqual([]);
  expect(bodies.length).toBeGreaterThan(10);
  expect(bodies.some((b) => b.includes(WEB_KEY))).toBe(false);
  expect(bodies.some((b) => b.includes(OPS_KEY))).toBe(false);
});

test("API key enforcement on the API itself (missing / invalid / valid)", async () => {
  const gen = { manager_key: MANAGER };
  expect((await api.post("/api/v1/recommendations/generate", { data: gen })).status()).toBe(401);
  expect((await api.post("/api/v1/recommendations/generate", { data: gen, headers: { "x-api-key": "not-a-key" } })).status()).toBe(403);
  for (const path of [`/api/v1/squad?manager_key=${MANAGER}`, `/api/v1/settings?manager_key=${MANAGER}`, `/api/v1/managers/${MANAGER}/export`]) {
    expect((await api.get(path)).status(), path).toBe(401);
    expect((await api.get(path, { headers: { "x-api-key": "not-a-key" } })).status(), path).toBe(403);
    expect([401, 403]).not.toContain((await api.get(path, { headers: { "x-api-key": OPS_KEY } })).status());
  }
  expect((await api.get("/api/v1/health")).status()).toBe(200); // public reference data stays open
});

test("dashboard and data-health show the same freshness and live-source state as the API", async ({ page }) => {
  await asManager(page);
  const gw = await (await api.get("/api/v1/gameweeks/current")).json();
  const health = await (await api.get("/api/v1/health")).json();
  await page.goto("/");
  const banner = page.getByTestId("freshness");
  await expect(banner).toContainText(`${gw.season} GW${gw.gameweek}`);
  await expect(banner).toContainText(gw.freshness.data_snapshot_id);
  await expect(banner).toContainText(gw.freshness.degraded ? "Degraded mode" : "Data fresh");
  for (const reason of gw.freshness.degraded_reasons as string[]) await expect(banner).toContainText(reason);
  await expect(page.getByText(`No squad stored for`)).toBeVisible();
  await page.goto("/health");
  await expect(page.getByTestId("services")).toContainText("live_source");
  await expect(page.getByTestId("services")).toContainText(health.checks.live_source.note);
});

test("build and save a squad through the proxy", async ({ page }) => {
  await asManager(page);
  await page.goto("/squad");
  await page.getByTestId("build-squad").click();
  await expect(page.getByTestId("squad-msg")).toContainText("Optimal squad proposed");
  await page.getByTestId("save-squad").click();
  await expect(page.getByTestId("squad-msg")).toContainText("Squad saved");
  await expect(page.getByTestId("pitch").locator(".shirt")).toHaveCount(11);
  const st = await (await api.get(`/api/v1/squad?manager_key=${MANAGER}`, { headers: { "x-api-key": OPS_KEY } })).json();
  expect(st.state.squad).toHaveLength(15);
});

test("captain, vice-captain and bench order on the pitch are the ones the lineup optimiser serves", async ({ page }) => {
  await asManager(page);
  const hdr = { "x-api-key": OPS_KEY };
  const lu = await (await api.post("/api/v1/lineup", { data: { manager_key: MANAGER }, headers: hdr })).json();
  const names = (await (await api.get(`/api/v1/squad?manager_key=${MANAGER}`, { headers: hdr })).json()).names as Record<string, string>;
  const { starters, bench, captain, vice_captain } = lu.lineup as { starters: number[]; bench: number[]; captain: number; vice_captain: number };
  expect(starters).toHaveLength(11);
  expect(bench).toHaveLength(4);
  expect(starters).toContain(captain);
  expect(starters).toContain(vice_captain);
  expect(captain).not.toBe(vice_captain);
  await page.goto("/squad");
  const pitch = page.getByTestId("pitch");
  await expect(pitch.locator(".shirt")).toHaveCount(11);
  await expect(pitch.locator(".shirt", { hasText: "(C)" })).toHaveCount(1);
  await expect(pitch.locator(".shirt", { hasText: "(C)" })).toContainText(names[String(captain)]!);
  await expect(pitch.locator(".shirt", { hasText: "(V)" })).toContainText(names[String(vice_captain)]!);
  const benchText = (await pitch.locator(".bench").textContent()) ?? "";
  const order = bench.map((c) => benchText.indexOf(names[String(c)]!));
  expect(order.every((i) => i >= 0)).toBe(true);
  expect([...order].sort((a, b) => a - b)).toEqual(order); // bench shown in substitution order
  await expect(page.getByTestId("lineup")).toContainText("captain profiles");
});

test("recommendation is generated by the worker and shows decision, evidence and downside", async ({ page }) => {
  await asManager(page);
  await page.goto("/");
  await page.getByTestId("generate").click();
  await expect(page.getByTestId("decision-text")).toBeVisible();
  await expect(page.getByTestId("decision")).toContainText(/HOLD|TRANSFER|HIT|CHIP/);
  await expect(page.getByTestId("why")).toBeVisible();
  await expect(page.getByTestId("downside")).toBeVisible();
  await expect(page.getByText("If you do nothing")).toBeVisible();
  await expect(page.getByTestId("why")).not.toContainText(/\[\d+/); // players named, not coded
  const rec = await (await api.get(`/api/v1/recommendations/current?manager_key=${MANAGER}`, { headers: { "x-api-key": OPS_KEY } })).json();
  await expect(page.getByTestId("decision")).toContainText(rec.decision.action);
});

test("player page shows engine and official price-change predictions separately, as served", async ({ page }) => {
  await asManager(page);
  const st = await (await api.get(`/api/v1/squad?manager_key=${MANAGER}`, { headers: { "x-api-key": OPS_KEY } })).json();
  const code = st.state.squad[0].player_code;
  const prof = await (await api.get(`/api/v1/players/${code}`)).json();
  await page.goto(`/players/${code}`);
  const risk = page.getByTestId("price-risk");
  await expect(risk).toContainText("Engine (calibrated)");
  if (prof.price_risk.engine) await expect(risk).toContainText("rise");
  else await expect(risk).toContainText("unavailable");
  if (prof.price_risk.official) await expect(risk).toContainText(`${prof.price_risk.official.price_change_percent.toFixed(1)}%`);
  else await expect(risk).toContainText("not in this snapshot");
  await expect(risk).toContainText("never blended");
});

test("transfer workflow: replacement picker → what-if vs hold, without mutating the saved squad", async ({ page }) => {
  await asManager(page);
  const before = await (await api.get(`/api/v1/squad?manager_key=${MANAGER}`, { headers: { "x-api-key": OPS_KEY } })).json();
  await page.goto("/transfers");
  const out = page.locator("[data-testid^='out-']").first();
  const outCode = (await out.getAttribute("data-testid"))!.replace("out-", "");
  await out.click();
  const firstIn = page.getByTestId("candidates").locator("tbody tr a").first();
  await expect(firstIn).toBeVisible();
  await expect(page.getByTestId("candidates")).toContainText("affordable, club-legal candidates screened");
  const inCode = (await firstIn.getAttribute("href"))!.split("/").pop()!;
  await page.goto("/what-if");
  await page.getByLabel("player").selectOption({ index: 1 });
  await page.getByLabel("sell").fill(outCode);
  await page.getByLabel("buy").fill(inCode);
  await page.getByTestId("run-scenario").click();
  await expect(page.getByTestId("scenario-result").first()).toContainText("holding scores");
  const after = await (await api.get(`/api/v1/squad?manager_key=${MANAGER}`, { headers: { "x-api-key": OPS_KEY } })).json();
  expect(after.state_id).toBe(before.state_id);
  expect(after.state.squad).toEqual(before.state.squad);
});

test("alerts are evaluated by the worker; re-evaluating unchanged data adds nothing", async ({ page }) => {
  await asManager(page);
  await page.goto("/alerts");
  await page.getByTestId("evaluate-alerts").click();
  await expect(page.getByTestId("alerts-msg")).toContainText("Evaluated");
  const first = (await page.getByTestId("alerts-msg").textContent()) ?? "";
  await page.getByTestId("evaluate-alerts").click();
  await expect(page.getByTestId("alerts-msg")).toContainText("Evaluated (alerts:0/");
  expect(first).toMatch(/Evaluated \(alerts:\d+\/\d+/);
});

const isSettings = (url: URL) => url.pathname === "/backend/settings";

test("settings: stored values load before editing; timezone persists; invalid zone and unsafe webhooks are rejected", async ({ page }) => {
  await asManager(page);
  const tz = page.getByLabel("Time zone (deadline reminders)");
  const hook = page.getByLabel("Webhook (HTTPS, allow-listed host)");
  const save = page.getByTestId("save-settings");
  const savePost = () => page.waitForRequest((r) => r.method() === "POST" && isSettings(new URL(r.url())));
  await page.goto("/settings");
  await tz.fill("Asia/Kolkata");
  await save.click();
  await expect(page.getByTestId("settings-msg")).toHaveText("Saved.");

  // Race regression: hold the stored settings in flight. Edits made before they arrived used to be
  // overwritten by the late response, so Save posted the stored value instead of the edit.
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  let held = 0;
  await page.route(isSettings, async (route) => {
    if (route.request().method() === "GET") {
      held += 1;
      await gate;
    }
    await route.continue();
  });
  await page.reload();
  await expect.poll(() => held, { message: "the settings GET is intercepted and held" }).toBe(1);
  await expect(page.getByTestId("notification-settings").getByText("Loading…")).toBeVisible();
  await expect(tz).toBeDisabled({ timeout: 10_000 });
  await expect(save).toBeDisabled({ timeout: 10_000 });
  const early = await tz.fill("Europe/Paris", { timeout: 1000 }).then(() => true, () => false);
  expect(early, "a server-backed field accepted an edit before the stored values arrived").toBe(false);
  release();
  await expect(tz).toBeEnabled();
  await expect(tz).toHaveValue("Asia/Kolkata"); // the stored value, not a default
  await tz.fill("America/New_York");
  const [posted] = await Promise.all([savePost(), save.click()]);
  expect(posted.postDataJSON().timezone).toBe("America/New_York");
  await expect(page.getByTestId("settings-msg")).toHaveText("Saved.");
  await page.reload();
  await expect(tz).toHaveValue("America/New_York");

  await tz.fill("Mars/Olympus_Mons");
  await save.click();
  await expect(page.getByTestId("settings-msg")).toContainText("server rejected");
  await tz.fill("America/New_York");
  for (const url of ["http://hooks.example.com/x", "https://169.254.169.254/latest", "https://10.0.0.5/hook", "https://127.0.0.1/hook", "https://evil.example.net/hook", "not a url"]) {
    await hook.fill(url);
    await save.click();
    await expect(page.getByTestId("settings-msg"), url).toContainText("server rejected");
  }
  const srv = await (await api.get(`/api/v1/settings?manager_key=${MANAGER}`, { headers: { "x-api-key": OPS_KEY } })).json();
  expect(srv.settings.timezone).toBe("America/New_York");
  expect(srv.settings.webhook_url ?? null).toBeNull(); // nothing unsafe was stored
});

test("settings: a failed load cannot save defaults over the stored values; retry recovers", async ({ page }) => {
  await asManager(page);
  const tz = page.getByLabel("Time zone (deadline reminders)");
  const save = page.getByTestId("save-settings");
  let fail = true;
  let posts = 0;
  await page.route(isSettings, async (route) => {
    if (route.request().method() === "POST") posts += 1;
    if (route.request().method() === "GET" && fail) {
      await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "API unavailable" }) });
      return;
    }
    await route.continue();
  });
  await page.goto("/settings");
  await expect(page.getByTestId("notification-settings").getByRole("alert")).toContainText("Stored settings could not be loaded");
  await expect(tz).toBeDisabled({ timeout: 10_000 });
  await expect(save).toBeDisabled({ timeout: 10_000 });
  fail = false;
  await page.getByTestId("retry-settings").click();
  await expect(tz).toBeEnabled();
  await expect(tz).toHaveValue("America/New_York");
  await expect(page.getByTestId("retry-settings")).toHaveCount(0);
  expect(posts).toBe(0);
  const srv = await (await api.get(`/api/v1/settings?manager_key=${MANAGER}`, { headers: { "x-api-key": OPS_KEY } })).json();
  expect(srv.settings.timezone).toBe("America/New_York");
});

test("traceability: the stored recommendation walks back to its pinned source", async ({ page }) => {
  await asManager(page);
  const rec = await (await api.get(`/api/v1/recommendations/current?manager_key=${MANAGER}`, { headers: { "x-api-key": OPS_KEY } })).json();
  await page.goto(`/journal/${rec.id}`);
  const trace = page.getByTestId("trace");
  await expect(trace).toContainText("chain complete");
  await expect(trace).toContainText(rec.optimizer_run_id);
  await expect(trace).toContainText(rec.snapshot_id);
  await expect(trace).toContainText("Source retrieval");
});

test("export downloads every stored record; deletion removes them", async ({ page }) => {
  await asManager(page);
  await page.goto("/settings");
  const [dl] = await Promise.all([page.waitForEvent("download"), page.getByTestId("export-data").click()]);
  const exported = JSON.parse(await (await import("node:fs/promises")).readFile((await dl.path())!, "utf8"));
  expect(exported.manager_key).toBe(MANAGER);
  expect(exported.counts.manager_state).toBeGreaterThan(0);
  expect(exported.counts.recommendations).toBeGreaterThan(0);
  expect(exported.counts.manager_settings).toBe(1);
  page.once("dialog", (d) => d.accept());
  await page.getByTestId("delete-data").click();
  await expect(page.getByTestId("settings-msg")).toContainText(/Deleted \d+ records/);
  const gone = await api.get(`/api/v1/managers/${MANAGER}/export`, { headers: { "x-api-key": OPS_KEY } });
  const counts = (await gone.json()).counts as Record<string, number>;
  expect(Object.values(counts).every((n) => n === 0)).toBe(true);
  await page.goto("/");
  await expect(page.getByText("No squad stored for")).toBeVisible();
});

test("backtest lab renders reports as formatted content with every figure the report references", async ({ page }) => {
  const tabs: [string, string][] = [
    ["Walk-forward backtest", "backtest"],
    ["Forecast evaluation & calibration", "forecast_eval"],
    ["Price-change model", "price_change"],
    ["Optimiser benchmark", "optimizer_benchmark"],
  ];
  const seen: string[] = [];
  await page.goto("/backtests");
  for (const [label, name] of tabs) {
    const rep = await (await page.request.get(`/backend/reports/${name}`)).json();
    const figs = [...(rep.markdown as string).matchAll(/!\[[^\]]*\]\(figures\/([^)]+)\)/g)].map((m) => m[1]!);
    const tables = (rep.markdown as string).split("\n").filter((l) => /^\|\s*:?-+/.test(l.trim())).length;
    await page.getByRole("button", { name: label, exact: true }).click();
    const report = page.getByTestId("report");
    await expect(report.locator("h2").first()).toBeVisible();
    await expect(report.locator("table")).toHaveCount(tables); // every Markdown table is a real table
    await expect(report).not.toContainText("|---"); // no raw Markdown syntax
    await expect(report).not.toContainText("**");
    for (const f of figs) {
      const img = report.locator(`img[data-figure="${f}"]`);
      await expect(img, f).toBeVisible();
      await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth), { message: f }).toBeGreaterThan(0);
      seen.push(f);
    }
  }
  expect(seen).toEqual(expect.arrayContaining(["backtest_cumulative_2023-24.svg", "backtest_cumulative_difference.svg"]));
});

test("API error states are shown, not swallowed", async ({ page }) => {
  await page.goto("/players/999999999");
  await expect(page.getByRole("alert").first()).toBeVisible();
});

test("authentication error from the API is surfaced by the UI", async ({ page }) => {
  test.skip(!BAD_KEY_WEB, "E2E_BADKEY_WEB_URL not provided");
  await page.goto(`${BAD_KEY_WEB}/settings`);
  // the stored settings cannot be read: the rejection is shown on load and nothing can be saved
  await expect(page.getByTestId("notification-settings").getByRole("alert")).toContainText("invalid API key");
  await expect(page.getByTestId("save-settings")).toBeDisabled({ timeout: 10_000 });
});

test("API outage is surfaced by the UI", async ({ page }) => {
  test.skip(!NO_API_WEB, "E2E_NOAPI_WEB_URL not provided");
  await page.goto(`${NO_API_WEB}/players`);
  await expect(page.getByRole("alert").first()).toBeVisible();
});

test("rate limiting reaches the browser through the proxy with Retry-After", async ({ page }) => {
  await page.goto("/");
  const statuses: number[] = [];
  let retryAfter: string | null = null;
  for (let i = 0; i < 400 && !retryAfter; i++) {
    const r = await page.request.get("/backend/gameweeks/current");
    statuses.push(r.status());
    if (r.status() === 429) retryAfter = r.headers()["retry-after"] ?? null;
  }
  expect(statuses).toContain(429);
  expect(Number(retryAfter)).toBeGreaterThan(0);
});
