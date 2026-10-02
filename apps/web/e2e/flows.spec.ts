import { expect, test } from "@playwright/test";

// The core weekly workflow (§85): squad → recommendation with evidence → replacement → plan.

test("build and save a squad, then see the pitch", async ({ page }) => {
  await page.goto("/squad");
  await page.getByTestId("build-squad").click();
  await expect(page.getByTestId("squad-msg")).toContainText("Optimal squad proposed");
  await page.getByTestId("save-squad").click();
  await expect(page.getByTestId("squad-msg")).toContainText("Squad saved");
  await expect(page.getByTestId("pitch")).toBeVisible();
  await expect(page.getByTestId("pitch").locator(".shirt")).toHaveCount(11);
});

test("overview shows degraded freshness and a recommendation with evidence", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("freshness")).toContainText("Degraded mode");
  const generate = page.getByTestId("generate");
  if (await generate.isVisible()) await generate.click();
  await expect(page.getByTestId("decision-text")).toBeVisible();
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
  await page.getByRole("button", { name: "Forecast evaluation & calibration" }).click();
  await expect(page.getByTestId("report")).toContainText("Forecast evaluation");
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
