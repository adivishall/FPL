import { expect, test } from "@playwright/test";

// Portfolio screenshots (§89) of a running deployment — opt-in: SCREENSHOTS=1 with the same
// environment as production.spec.ts. Writes docs/images/*.png (generated, committed evidence).
const OUT = "../../docs/images";
const MANAGER = `screens-${Date.now()}`;

test.describe.configure({ mode: "serial" });
test.use({ viewport: { width: 1440, height: 1000 } });

test.beforeEach(async ({ page }) => {
  await page.addInitScript((key) => {
    window.localStorage.setItem("fpl.settings.v1", JSON.stringify({ managerKey: key, profile: "default", horizon: 3 }));
  }, MANAGER);
});

test("capture the decision workflow", async ({ page }) => {
  await page.goto("/squad");
  await page.getByTestId("build-squad").click();
  await expect(page.getByTestId("squad-msg")).toContainText("Optimal squad proposed");
  await page.getByTestId("save-squad").click();
  await expect(page.getByTestId("squad-msg")).toContainText("Squad saved");
  await page.screenshot({ path: `${OUT}/squad-planner.png`, fullPage: true });

  await page.goto("/");
  await page.getByTestId("generate").click();
  await expect(page.getByTestId("decision-text")).toBeVisible();
  await page.screenshot({ path: `${OUT}/overview-recommendation.png`, fullPage: true });

  await page.goto("/transfers");
  await page.locator("[data-testid^='out-']").nth(5).click();
  await expect(page.getByTestId("candidates").locator("tbody tr").first()).toBeVisible();
  await page.screenshot({ path: `${OUT}/transfer-lab.png`, fullPage: true });

  await page.goto("/journal");
  await page.getByTestId("journal").locator("tbody tr a").first().click();
  await expect(page.getByTestId("trace")).toContainText("chain complete");
  await page.screenshot({ path: `${OUT}/traceability.png`, fullPage: true });

  await page.goto("/health");
  await expect(page.getByTestId("services")).toContainText("live_source");
  await page.screenshot({ path: `${OUT}/data-health.png`, fullPage: true });

  await page.goto("/backtests");
  await page.waitForLoadState("networkidle");
  await page.locator("img[data-figure='backtest_cumulative_difference.svg']").scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${OUT}/backtest-lab.png` }); // the full report is ~11k px tall

  await page.goto("/settings");
  page.once("dialog", (d) => d.accept());
  await page.getByTestId("delete-data").click(); // leave no screenshot data behind
  await expect(page.getByTestId("settings-msg")).toContainText(/Deleted \d+ records/);
});
