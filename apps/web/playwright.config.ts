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
  // the flows run as one signed-in beta user: e2e/auth.setup.ts registers them and saves the
  // session cookie + active manager; the main project starts from that storage state
  projects: [
    { name: "setup", testMatch: /auth\.setup\.ts/ },
    { name: "chromium", testMatch: /flows\.spec\.ts/, dependencies: ["setup"], use: { storageState: "e2e/.auth/user.json" } },
  ],
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
      // production mode: the browser calls /backend/*, the Next server forwards with the key
      env: { FPL_API_INTERNAL_URL: "http://127.0.0.1:8000", FPL_API_KEY: "e2e-only-key" },
    },
  ],
});
