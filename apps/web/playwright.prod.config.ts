import { defineConfig } from "@playwright/test";

// Production-topology verification: the browser talks only to the deployed web server, which
// proxies /backend/* to the API with a server-held key (Browser → Next proxy → API key → API →
// PostgreSQL / Redis / worker). Nothing is started here: point it at a running deployment, e.g.
//   docker compose up -d   (see docs/DEPLOYMENT.md)
//   E2E_BASE_URL=http://127.0.0.1:3000 E2E_API_URL=http://127.0.0.1:8000 \
//   E2E_OPS_KEY=<operator key> E2E_WEB_KEY=<the proxy's key> npx playwright test -c playwright.prod.config.ts
// E2E_WEB_KEY is only used to assert that the proxy's key never reaches the browser.
export default defineConfig({
  testDir: "./e2e-prod",
  // screenshots.spec.ts only captures docs/images (SCREENSHOTS=1); it is not a test
  testIgnore: process.env.SCREENSHOTS ? [] : ["**/screenshots.spec.ts"],
  timeout: 600_000,
  expect: { timeout: 180_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["json", { outputFile: "prod-results.json" }], ["html", { open: "never", outputFolder: "playwright-report-prod" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:3000",
    trace: "retain-on-failure",
    acceptDownloads: true,
    launchOptions: process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {},
  },
});
