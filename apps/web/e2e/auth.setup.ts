import { expect, test as setup } from "@playwright/test";

// Signs a beta user in once for the whole run: an invitation from the operator key (direct API),
// registration through the web origin (which sets the HttpOnly session cookie), a manager key
// owned by that user, and the browser's active-manager setting. The main project reuses the
// saved storage state (cookie + localStorage).

const API = "http://127.0.0.1:8000";
const OPS_KEY = "e2e-ops-key"; // infra/scripts/e2e_api.py (throw-away stack only)
export const STORAGE = "e2e/.auth/user.json";

setup("register a beta user and a manager", async ({ page, request }) => {
  const inv = await request.post(`${API}/api/v1/auth/invites`, {
    data: { label: "e2e", days: 1 },
    headers: { "x-api-key": OPS_KEY },
  });
  expect(inv.ok(), await inv.text()).toBe(true);
  const code = (await inv.json()).invite_code as string;
  const reg = await page.request.post("/auth/register", {
    data: { invite_code: code, email: `e2e-${Date.now()}@example.test`, password: "e2e-password-123" },
  });
  expect(reg.ok(), await reg.text()).toBe(true);
  const mgr = await page.request.post("/backend/auth/managers", { data: { label: "e2e" } });
  expect(mgr.ok(), await mgr.text()).toBe(true);
  const key = (await mgr.json()).manager_key as string;
  await page.goto("/login");
  await page.evaluate((k) => {
    window.localStorage.setItem("fpl.settings.v1", JSON.stringify({ managerKey: k, profile: "default", horizon: 3 }));
  }, key);
  await page.context().storageState({ path: STORAGE });
});
