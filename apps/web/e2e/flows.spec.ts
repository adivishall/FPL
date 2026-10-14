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

test("transfer workbench compares legal whole-squad plans against holding", async ({ page }) => {
  await page.goto("/transfers");
  await expect(page.getByTestId("wb-state")).toContainText("free transfer");
  const first = page.locator("[data-testid^='out-']").first();
  await first.click();
  await expect(page.getByTestId("out-name")).toBeVisible();
  await page.getByTestId("evaluate").click();
  await expect(page.getByTestId("run-status")).toBeVisible();
  await expect(page.getByTestId("plan-hold")).toBeVisible({ timeout: 240_000 });
  await expect(page.getByTestId("plans")).toContainText("replacements screened");
  const rows = page.getByTestId("plans").locator("tbody tr");
  expect(await rows.count()).toBeGreaterThan(1);
  await expect(page.getByTestId("wb-trace")).toContainText("snapshot");
  await expect(page.getByTestId("plans")).not.toContainText("illegal");
  const rep = page.locator("[data-testid^='plan-replace-']").first();
  await expect(rep).toBeVisible();
  const label = (await rep.getAttribute("data-testid"))!.replace("plan-", "");
  await page.getByTestId(`details-${label}`).click();
  await expect(page.getByTestId("plan-details")).toContainText("Squad after");
  // a specific replacement through the picker is evaluated as an exact single transfer
  await page.getByTestId("mode-transfer").check();
  await page.getByTestId("wb-picker").getByTestId("picker-search").fill("a");
  await page.getByTestId("wb-picker").locator("[role=option]").first().click();
  await expect(page.getByTestId("incoming")).not.toContainText("pick a player");
  await page.getByTestId("include-best").uncheck();
  await page.getByTestId("evaluate").click();
  await expect(page.getByTestId("plan-your transfer")).toBeVisible({ timeout: 240_000 });
  await expect(page.getByTestId("plan-your transfer")).toContainText("TRANSFER");
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
  await expect(page.getByTestId("models")).toContainText("passed on past seasons 2023–26"); // not live evidence
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


// ---------------------------------------------------------------- result lifecycle (M1.3)
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

test("a saved squad makes the last workbench run historical with the state diff, and the run is shown again after a reload", async ({ page }) => {
  await page.goto("/transfers");
  await expect(page.getByTestId("wb-state")).toContainText("free transfer");
  await page.getByTestId("include-best").uncheck();
  await page.getByTestId("evaluate").click();
  await expect(page.getByTestId("plan-hold")).toBeVisible({ timeout: 240_000 });
  await expect(page.getByTestId("run-lifecycle")).toHaveAttribute("data-state", "current");
  const runId = (await page.getByTestId("run-status").textContent())?.match(/Run (\S+)/)?.[1] ?? "";
  expect(runId).not.toBe("");
  // the squad is saved again with a different bank: the run's inputs are no longer current
  const key = JSON.parse(await page.evaluate(() => window.localStorage.getItem("fpl.settings.v1")!)).managerKey as string;
  const st = await (await page.request.get(`/backend/squad?manager_key=${key}`)).json();
  const saved = await page.request.post("/backend/squad", {
    data: { manager_key: key, picks: st.state.squad.map((p: { player_code: number }) => ({ player_code: p.player_code })), bank: st.state.bank + 7, free_transfers: st.state.free_transfers },
  });
  expect(saved.ok(), await saved.text()).toBe(true);
  await page.reload();
  await expect(page.getByTestId("run-status")).toContainText(runId); // the last run is shown again…
  await expect(page.getByTestId("run-lifecycle")).toHaveAttribute("data-state", "stale"); // …but never as current
  await expect(page.getByTestId("lifecycle-reasons")).toContainText("your squad was saved again");
  await expect(page.getByTestId("plans")).toContainText("historical run");
  await page.getByTestId("what-changed-button").click();
  await expect(page.getByTestId("changed-state")).toContainText(/Bank .* → /);
  // re-evaluating for the current squad gives a current run; the old one stays historical
  await page.getByTestId("evaluate").click();
  await expect(page.getByTestId("run-lifecycle")).toHaveAttribute("data-state", "current", { timeout: 240_000 });
  expect((await page.getByTestId("run-status").textContent())!).not.toContain(runId);
  const old = await (await page.request.get(`/backend/workbench/runs/${runId}`)).json();
  expect(old.lifecycle.state).toBe("stale");
});

test("promoting a newer data snapshot makes the recommendation historical, explains what changed, and regeneration makes a current one", async ({ page }) => {
  const infoPath = join(__dirname, ".e2e-stack.json");
  test.skip(!existsSync(infoPath), "the e2e API stack did not publish its snapshot layout");
  const info = JSON.parse(readFileSync(infoPath, "utf8")) as { snapshots_root: string; artifact_dir: string; served_snapshot: string; staged_snapshot: string; staged_snapshot_id: string };
  await page.goto("/");
  const generate = page.getByTestId("generate");
  const decision = page.getByTestId("decision-text");
  await expect(generate.or(decision).first()).toBeVisible();
  if (!(await decision.isVisible())) await generate.click();
  await expect(decision).toBeVisible({ timeout: 240_000 });
  // an earlier flow may have saved the squad again: regenerate until the plan is current
  if ((await page.getByTestId("rec-lifecycle").getAttribute("data-state")) !== "current") {
    await page.getByTestId("regenerate").click();
  }
  await expect(page.getByTestId("rec-lifecycle")).toHaveAttribute("data-state", "current", { timeout: 240_000 });
  // --- promotion: snapshot B enters the snapshots root with a ready marker (what the live
  // refresh and the forecast job do); the API swaps within a second
  const target = join(info.snapshots_root, info.staged_snapshot_id);
  if (!existsSync(target)) cpSync(info.staged_snapshot, target, { recursive: true });
  mkdirSync(join(info.artifact_dir, "serving"), { recursive: true });
  writeFileSync(join(info.artifact_dir, "serving", `${info.staged_snapshot_id}.ready`), JSON.stringify({ forecast_key: "fc_e2e_b" }));
  await expect
    .poll(async () => (await (await page.request.get("/backend/gameweeks/current")).json()).freshness.data_snapshot_id, { timeout: 30_000 })
    .toBe(info.staged_snapshot_id);
  await page.reload();
  await expect(page.getByTestId("rec-lifecycle")).toHaveAttribute("data-state", "stale", { timeout: 60_000 });
  await expect(page.getByTestId("lifecycle-reasons")).toContainText(info.staged_snapshot_id);
  await page.getByTestId("what-changed-button").click();
  const changed = page.getByTestId("changed-snapshot");
  await expect(changed).toContainText(info.staged_snapshot_id, { timeout: 240_000 }); // the forecast for B may be computed on demand
  await expect(changed).toContainText("player_snapshots");
  // the Future Planner shows the same plan as historical, never as the recommended path
  await page.goto("/planner");
  await expect(page.getByTestId("planner-lifecycle")).toHaveAttribute("data-state", "stale");
  await expect(page.getByTestId("path")).toContainText("historical — not current");
  await page.goto("/");
  // --- regenerate against the newer snapshot: current again; the old record is historical
  await page.getByTestId("regenerate").click();
  await expect(page.getByTestId("rec-lifecycle")).toHaveAttribute("data-state", "current", { timeout: 240_000 });
  const key = JSON.parse(await page.evaluate(() => window.localStorage.getItem("fpl.settings.v1")!)).managerKey as string;
  const journal = await (await page.request.get(`/backend/decisions?manager_key=${key}`)).json();
  const statuses = (journal.journal as { status: string }[]).map((e) => e.status);
  expect(statuses).toContain("invalidated");
  expect(statuses.filter((s) => s === "active")).toHaveLength(1);
  await page.goto("/journal");
  await expect(page.getByTestId("journal")).toContainText("historical");
  // the journal's badges are the same derived verdict: exactly one current recommendation
  await expect(page.locator('[data-testid^="journal-status-"][data-state="current"]')).toHaveCount(1);
});

// ---------------------------------------------------------------- decision journal (M1.4)
test("a decision recorded on the Overview is kept in the journal and evaluated against the official results once they are final", async ({ page }) => {
  const infoPath = join(__dirname, ".e2e-stack.json");
  test.skip(!existsSync(infoPath), "the e2e API stack did not publish its snapshot layout");
  const info = JSON.parse(readFileSync(infoPath, "utf8")) as { snapshots_root: string; artifact_dir: string; staged_results_snapshot: string; staged_results_snapshot_id: string };
  await page.goto("/");
  const generate = page.getByTestId("generate");
  const decision = page.getByTestId("decision-text");
  await expect(generate.or(decision).first()).toBeVisible();
  if (!(await decision.isVisible())) await generate.click();
  await expect(decision).toBeVisible({ timeout: 240_000 });
  if ((await page.getByTestId("rec-lifecycle").getAttribute("data-state")) !== "current") await page.getByTestId("regenerate").click();
  await expect(page.getByTestId("rec-lifecycle")).toHaveAttribute("data-state", "current", { timeout: 240_000 });
  const snapshot = (await (await page.request.get("/backend/gameweeks/current")).json()).freshness.data_snapshot_id as string;
  // record: following Copilot, with a reason
  await page.getByTestId("record-rationale").fill("I trust the plan");
  await page.getByTestId("record-recommended").click();
  await expect(page.getByTestId("record-msg")).toContainText("Recorded in your journal");
  await page.getByTestId("record-link").click();
  await expect(page.getByTestId("entry-action")).toContainText("followed Copilot");
  await expect(page.getByTestId("outcome-status")).toHaveAttribute("data-state", "pending");
  await expect(page.getByTestId("evidence")).toContainText(snapshot);
  const entryUrl = page.url();
  // a decision migrated from pre-M1.4 feedback has no decision-time evidence: its page explains
  // that instead of failing, and nothing offers to evaluate it
  const entryId = entryUrl.split("/").pop()!;
  const entry = await (await page.request.get(`/backend/journal/${entryId}`)).json();
  const legacyId = execFileSync("uv", ["run", "python", "infra/scripts/e2e_api.py", "seed-legacy-decision", entry.manager_key, entry.source.id, entryId], {
    cwd: join(__dirname, "..", "..", ".."),
    encoding: "utf8",
  }).trim().split("\n").pop()!;
  await page.goto(`/journal/entry/${legacyId}`);
  await expect(page.getByTestId("entry-legacy")).toContainText("before the journal kept decision-time evidence");
  await expect(page.getByTestId("entry-superseded")).toContainText("Replaced by");
  await expect(page.getByTestId("outcome-none")).toContainText("no decision-time evidence");
  await expect(page.getByTestId("evaluate-now")).toHaveCount(0);
  // it is in the journal, also after a reload
  await page.goto("/journal");
  const row = page.locator('[data-testid^="decision-jd_"]').first();
  await expect(row).toContainText("followed Copilot");
  await page.reload();
  await expect(row).toContainText("followed Copilot");
  await expect(row).toContainText("awaiting results");
  // the official results arrive: their snapshot is promoted (copy + ready marker)
  const target = join(info.snapshots_root, info.staged_results_snapshot_id);
  if (!existsSync(target)) cpSync(info.staged_results_snapshot, target, { recursive: true });
  mkdirSync(join(info.artifact_dir, "serving"), { recursive: true });
  writeFileSync(join(info.artifact_dir, "serving", `${info.staged_results_snapshot_id}.ready`), JSON.stringify({ forecast_key: "fc_e2e_results" }));
  await expect
    .poll(async () => (await (await page.request.get("/backend/gameweeks/current")).json()).freshness.data_snapshot_id, { timeout: 30_000 })
    .toBe(info.staged_results_snapshot_id);
  await page.goto(entryUrl);
  await page.getByTestId("evaluate-now").click();
  await expect(page.getByTestId("outcome-status")).toHaveAttribute("data-state", "final", { timeout: 120_000 });
  await expect(page.getByTestId("outcome-scores")).toContainText("Holding");
  await expect(page.getByTestId("outcome-statements")).toContainText("Your recorded action scored");
  await expect(page.getByTestId("outcome-caveat")).toContainText("does not grade the decision");
  // the recommendation it was recorded against is historical now; the decision-time evidence is not rewritten
  await expect(page.getByTestId("entry-source-now")).toHaveAttribute("data-state", /stale|superseded/);
  await expect(page.getByTestId("evidence")).toContainText(snapshot);
  await expect(page.getByTestId("evidence")).not.toContainText(info.staged_results_snapshot_id);
  await page.goto("/journal");
  await expect(page.locator('[data-testid^="decision-jd_"]').first()).toContainText("final");
});

test("the season review keeps the live record, past seasons and the manager's own decisions apart and claims nothing early", async ({ page }) => {
  await page.goto("/season-review");
  // nothing frozen yet in this stack: no live gameweek, no evaluation, the empty state
  await expect(page.getByTestId("season-status")).toContainText("Live record: 0 of 8 gameweeks final");
  await expect(page.getByTestId("live-empty")).toContainText("No live gameweek is final yet");
  await expect(page.getByTestId("timeline")).toContainText("before the record");
  await expect(page.getByTestId("timeline-gw1")).toHaveAttribute("data-state", "before_record");
  // past seasons: the same method, labelled as context with its dates
  const ref = page.getByTestId("reference");
  for (const s of ["2023-24", "2024-25", "2025-26"]) await expect(ref).toContainText(s);
  await expect(page.getByTestId("past-seasons")).toContainText("selection bias");
  // the manager's own recorded decisions (the journal flow above recorded and evaluated one)
  await expect(page.getByTestId("your-decisions")).toContainText("decisions recorded");
  await expect(page.getByTestId("your-decisions")).toContainText("cannot show a trend");
  await expect(page.getByTestId("calibration-state")).toHaveAttribute("data-state", "deferred");
  await expect(page.getByTestId("calibration")).toContainText("No calibration adjustment has been made");
  await expect(page.getByTestId("ui-numbers")).toContainText("P(play)");
  // no evidence-class jargon, no grade
  const text = await page.locator("main").innerText();
  expect(text).not.toMatch(/prospective|class [HRPJ]\b/i);
  expect(text).not.toMatch(/% good|decision score|rating/i);
});
