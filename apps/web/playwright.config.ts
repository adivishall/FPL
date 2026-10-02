import { defineConfig } from "@playwright/test";

// End-to-end flows against the real stack: API (real data excerpt + PostgreSQL) and the built UI.
export default defineConfig({
  testDir: "./e2e",
  timeout: 240_000,
  expect: { timeout: 120_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: "http://127.0.0.1:3000",
    trace: "retain-on-failure",
    launchOptions: process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {},
  },
  webServer: [
    {
      command: "uv run python infra/scripts/e2e_api.py",
      cwd: "../..",
      url: "http://127.0.0.1:8000/api/v1/gameweeks/current",
      timeout: 300_000,
      reuseExistingServer: !process.env.CI,
    },
    {
      command: "npx next start -p 3000 -H 127.0.0.1",
      url: "http://127.0.0.1:3000",
      timeout: 120_000,
      reuseExistingServer: !process.env.CI,
      env: { NEXT_PUBLIC_API_BASE: "http://127.0.0.1:8000" },
    },
  ],
});
