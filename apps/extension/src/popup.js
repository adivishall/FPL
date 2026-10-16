import { APP_URL, PRODUCT_NAME } from "./config.js";
import { decide } from "./lib.js";

// The address of the tab the user clicked the button on. "activeTab" grants it for that click
// only; nothing else about the page is read, and no other tab is visible to the extension.
async function activeTabUrl() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    // a navigation still in flight is the page the user means; otherwise the committed one
    return tab?.pendingUrl ?? tab?.url ?? "";
  } catch {
    return "";
  }
}

function render(d) {
  const action = document.getElementById("action");
  const hint = document.getElementById("hint");
  const status = document.getElementById("status");
  action.href = d.href;
  action.textContent = d.label;
  action.dataset.kind = d.kind;
  action.hidden = false;
  if (d.kind === "team") {
    status.textContent = `Team ${d.id} found in this page's address.`;
    hint.hidden = true;
  } else {
    status.textContent = "No team ID in this page's address.";
    hint.textContent = d.hint;
    hint.hidden = false;
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  document.getElementById("product").textContent = PRODUCT_NAME;
  document.title = PRODUCT_NAME;
  document.getElementById("dest").textContent = new URL(APP_URL).host;
  render(decide(await activeTabUrl(), { appUrl: APP_URL, productName: PRODUCT_NAME }));
});
