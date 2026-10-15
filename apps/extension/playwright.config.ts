import { defineConfig } from "@playwright/test";

// Browser tests: the extension built by build.sh is loaded into Chromium (persistent context,
// the only way Chromium loads extensions) and its popup is exercised against a stand-in web
// app started by the test. Nothing is started outside the test process.
export default defineConfig({
  testDir: "./test",
  testMatch: /(popup|store)\.spec\.ts/,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
});
