import { expect, test } from "@playwright/test";

// The core weekly workflow (§85): squad → recommendation with evidence → replacement → plan.

test("build and save a squad, then see the pitch", async ({ page }) => {
  await page.goto("/squad");
  await page.getByTestId("build-squad").click();
  await expect(page.getByTestId("squad-msg")).toContainText(/Squad proposed by the initial-squad optimiser \((proven optimal|best found within the solver limit.*not proven optimal)\)/);
  await page.getByTestId("save-squad").click();
  await expect(page.getByTestId("squad-msg")).toContainText("Squad saved");
  await expect(page.getByTestId("pitch")).toBeVisible();
  await expect(page.getByTestId("pitch").locator(".shirt")).toHaveCount(11);
});

test("overview shows degraded freshness and a recommendation with evidence", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("freshness")).toContainText("Degraded mode");
  const generate = page.getByTestId("generate");
  const decision = page.getByTestId("decision-text");
  // wait for the page to settle on one of the two states; a one-shot isVisible() raced the
  // API under load and skipped the click (flaky: later tests then had no recommendation)
  await expect(generate.or(decision).first()).toBeVisible();
  if (!(await decision.isVisible())) await generate.click(); // waits until it is enabled
  await expect(decision).toBeVisible();
  await expect(page.getByTestId("decision")).toContainText(/HOLD|TRANSFER|HIT|CHIP/);
  await expect(page.getByTestId("why")).toBeVisible();
  await expect(page.getByTestId("downside")).toBeVisible();
  await expect(page.getByText("If you do nothing")).toBeVisible();
});

test("transfer lab returns legal replacement candidates", async ({ page }) => {
  await page.goto("/transfers");
  const first = page.locator("[data-testid^='out-']").first();
  await first.click();
  await expect(page.getByTestId("candidates").locator("tbody tr").first()).toBeVisible();
  await expect(page.getByTestId("candidates")).toContainText("affordable, club-legal candidates screened");
});

test("future planner, player lab, backtest lab and data health render real data", async ({ page }) => {
  await page.goto("/planner");
  await expect(page.getByTestId("path").locator("tbody tr").first()).toBeVisible();
  await page.goto("/players");
  const firstPlayer = page.getByTestId("players").locator("tbody tr a").first();
  await expect(firstPlayer).toBeVisible();
  await firstPlayer.click();
  await expect(page.getByTestId("forecast").locator("tbody tr").first()).toBeVisible();
  await page.goto("/backtests");
  const report = page.getByTestId("report");
  await expect(report.locator("table").first()).toBeVisible(); // rendered, not raw Markdown
  await expect(report).not.toContainText("|---");
  for (const f of ["backtest_cumulative_2023-24.svg", "backtest_cumulative_difference.svg"]) {
    const img = report.locator(`img[data-figure="${f}"]`);
    await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth), { message: f }).toBeGreaterThan(0);
  }
  await page.getByRole("button", { name: "Forecast evaluation & calibration" }).click();
  await expect(report).toContainText("Forecast evaluation");
  await page.goto("/health");
  await expect(page.getByTestId("services")).toContainText("live_source");
  await expect(page.getByTestId("models")).toContainText("passed");
});

test("what-if scenario re-simulates the squad", async ({ page }) => {
  await page.goto("/what-if");
  await page.getByLabel("player").selectOption({ index: 1 });
  await page.getByTestId("run-scenario").click();
  await expect(page.getByTestId("scenario-result")).toContainText("holding scores");
});

test("alerts evaluate, and a recommendation traces back to its source revision", async ({ page }) => {
  await page.goto("/alerts");
  await page.getByTestId("evaluate-alerts").click();
  await expect(page.getByTestId("alerts-msg")).toContainText("Evaluated");
  await page.goto("/journal");
  await page.getByTestId("journal").locator("tbody tr a").first().click();
  await expect(page.getByTestId("trace")).toContainText("chain complete");
  await expect(page.getByTestId("trace")).toContainText("Source retrieval");
});

test("settings reject an unsafe webhook URL (SSRF guard)", async ({ page }) => {
  await page.goto("/settings");
  await page.getByLabel("Webhook (HTTPS, allow-listed host)").fill("https://169.254.169.254/latest");
  await page.getByTestId("save-settings").click();
  await expect(page.getByTestId("settings-msg")).toContainText("allow-list");
});

test("onboarding validates the FPL ID and surfaces an unavailable live source with the manual fallback", async ({ page }) => {
  await page.goto("/onboarding");
  await expect(page.getByTestId("onboarding-id")).toBeVisible();
  await page.getByLabel("FPL ID").fill("abc");
  await page.getByTestId("lookup-entry").click();
  await expect(page.getByTestId("onboarding-error")).toContainText("not a valid FPL ID");
  await page.getByLabel("FPL ID").fill("1234567");
  await page.getByTestId("lookup-entry").click();
  // this stack has no route to the FPL API: the user is told, and pointed at manual entry
  await expect(page.getByTestId("onboarding-error")).toContainText(/cannot be reached|disabled/);
  await expect(page.getByRole("link", { name: "Enter it manually" })).toBeVisible();
});

test("player picker searches the real pool, filters, and picks with the keyboard", async ({ page }) => {
  await page.goto("/squad");
  const picker = page.getByTestId("player-picker");
  const search = picker.getByTestId("picker-search");
  await expect(picker.locator("li").first()).toBeVisible(); // the pool loaded
  const firstName = (await picker.locator("li strong").first().textContent())!.trim();
  await search.fill(firstName.slice(0, 4));
  await expect(picker.locator("li").first()).toContainText(firstName.slice(0, 4));
  await search.fill("");
  await picker.getByLabel("position").selectOption("GK");
  await expect(picker.locator("li").first()).toContainText("GK");
  await picker.getByLabel("position").selectOption("");
  await search.fill("zzzzqqqq");
  await expect(picker.getByTestId("picker-empty")).toBeVisible();
  await search.fill("");
  await search.press("ArrowDown");
  await search.press("Enter");
  await expect(page.getByTestId("picked").locator("li")).toHaveCount(1);
  await expect(page.getByTestId("save-picked")).toBeDisabled(); // 15 needed
  await page.getByRole("button", { name: /^remove / }).click();
  await expect(page.getByTestId("picked")).toHaveCount(0);
});

test("copilot home shows the situation, squad health, captain profiles, the squad and fixtures from the precomputed analysis", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("situation")).toContainText("free transfer");
  await expect(page.getByTestId("analysis-status")).toContainText("computed");
  await expect(page.getByTestId("health")).toContainText("affordability"); // always present, names its source
  await expect(page.getByTestId("health")).toContainText("source:");
  await expect(page.getByTestId("captain-profiles").locator("tbody tr")).toHaveCount(3);
  await expect(page.getByTestId("home-squad").locator("tbody tr")).toHaveCount(15);
  await expect(page.getByTestId("home-squad")).toContainText("Sources: forecast");
  await expect(page.getByTestId("fixture-outlook").locator("tbody tr").first()).toBeVisible();
  // the captain shown on Home is the one the lineup endpoint serves
  const lu = await (await page.request.post("/backend/lineup", { data: { manager_key: JSON.parse(await page.evaluate(() => window.localStorage.getItem("fpl.settings.v1") ?? "{}")).managerKey } })).json();
  const names = (await (await page.request.get(`/backend/squad?manager_key=${JSON.parse(await page.evaluate(() => window.localStorage.getItem("fpl.settings.v1") ?? "{}")).managerKey}`)).json()).names as Record<string, string>;
  await expect(page.getByTestId("captain-profiles").locator("tbody tr").first()).toContainText(names[String(lu.captaincy.expected)]!);
});

