// Unit tests for the popup's decisions and the build (node --test; no browser).
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { decide, isFplPage, teamIdFrom } from "../src/lib.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const cfg = { appUrl: "https://app.example.test/", productName: "Example" };

test("a public team page gives its id, whatever the sub-page", () => {
  for (const [url, id] of [
    ["https://fantasy.premierleague.com/entry/1234567/event/7", "1234567"],
    ["https://fantasy.premierleague.com/entry/1234567/history", "1234567"],
    ["https://fantasy.premierleague.com/entry/1234567/transfers", "1234567"],
    ["https://fantasy.premierleague.com/entry/42", "42"],
    ["https://fantasy.premierleague.com/entry/42/", "42"],
    ["https://fantasy.premierleague.com/entry/42/event/7?x=1#top", "42"],
  ]) assert.equal(teamIdFrom(url), id, url);
});

test("unsupported pages, missing and invalid ids give no team", () => {
  for (const url of [
    "https://fantasy.premierleague.com/",
    "https://fantasy.premierleague.com/my-team", // the signed-in user's own pages carry no id
    "https://fantasy.premierleague.com/transfers",
    "https://fantasy.premierleague.com/leagues/123/standings/c",
    "https://fantasy.premierleague.com/entry/",
    "https://fantasy.premierleague.com/entry/0/event/1",
    "https://fantasy.premierleague.com/entry/-5",
    "https://fantasy.premierleague.com/entry/12ab/event/1",
    "https://fantasy.premierleague.com/entry/12345678901/event/1", // more than 10 digits
    "https://fantasy.premierleague.com/api/entry/42/",
    "http://fantasy.premierleague.com/entry/42/event/1", // not https
    "https://fantasy.premierleague.com.evil.test/entry/42/event/1",
    "https://evil.test/fantasy.premierleague.com/entry/42",
    "https://www.premierleague.com/entry/42",
    "chrome://extensions",
    "",
    undefined,
    null,
    "not a url",
  ]) assert.equal(teamIdFrom(url), null, String(url));
});

test("the decision carries the id to the web app's onboarding and nothing else", () => {
  const d = decide("https://fantasy.premierleague.com/entry/1234567/event/7", cfg);
  assert.deepEqual(d, {
    kind: "team",
    id: "1234567",
    href: "https://app.example.test/onboarding?entry=1234567",
    label: "Open team 1234567 in Example",
  });
});

test("elsewhere the decision is a plain link to the web app, with a hint that fits the page", () => {
  const onFpl = decide("https://fantasy.premierleague.com/leagues/1/standings/c", cfg);
  assert.equal(onFpl.kind, "site");
  assert.equal(onFpl.href, "https://app.example.test");
  assert.equal(onFpl.label, "Open Example");
  assert.match(onFpl.hint, /^This page shows no team ID/);
  const elsewhere = decide("https://example.org/", cfg);
  assert.equal(elsewhere.href, "https://app.example.test");
  assert.match(elsewhere.hint, /^On fantasy\.premierleague\.com/);
  assert.equal(isFplPage("https://fantasy.premierleague.com/leagues/1"), true);
  assert.equal(isFplPage("https://example.org/"), false);
});

test("the manifest asks for activeTab only: no hosts, no content scripts, no background", () => {
  const m = JSON.parse(readFileSync(path.join(here, "../src/manifest.json"), "utf8"));
  assert.equal(m.manifest_version, 3);
  assert.deepEqual(m.permissions, ["activeTab"]);
  for (const k of ["host_permissions", "optional_permissions", "optional_host_permissions", "content_scripts", "background", "web_accessible_resources", "content_security_policy"]) {
    assert.equal(k in m, false, k);
  }
  assert.ok(m.description.length <= 132);
});

test("the build is reproducible and substitutes only the address and the name", () => {
  const out1 = mkdtempSync(path.join(tmpdir(), "ext-"));
  const out2 = mkdtempSync(path.join(tmpdir(), "ext-"));
  try {
    const args = ["--url", "https://app.example.test", "--name", "Example Product"];
    execFileSync(path.join(here, "../build.sh"), [...args, "--out", out1], { stdio: "pipe" });
    execFileSync(path.join(here, "../build.sh"), [...args, "--out", out2], { stdio: "pipe" });
    const zip = "example-product-extension-0.1.0.zip";
    const h = (p) => createHash("sha256").update(readFileSync(p)).digest("hex");
    assert.equal(h(path.join(out1, zip)), h(path.join(out2, zip)));
    assert.equal(readFileSync(path.join(out1, `${zip}.sha256`), "utf8"), `${h(path.join(out1, zip))}  ${zip}\n`);
    const cfgJs = readFileSync(path.join(out1, "unpacked/config.js"), "utf8");
    assert.match(cfgJs, /APP_URL = "https:\/\/app\.example\.test";/);
    assert.match(cfgJs, /PRODUCT_NAME = "Example Product";/);
    const m = JSON.parse(readFileSync(path.join(out1, "unpacked/manifest.json"), "utf8"));
    assert.equal(m.name, "Example Product");
    assert.deepEqual(m.permissions, ["activeTab"]);
    assert.ok(!readFileSync(path.join(out1, "unpacked/popup.html"), "utf8").includes("FPL Copilot"));
    // nothing but the extension's files
    const listing = execFileSync("unzip", ["-Z1", path.join(out1, zip)], { encoding: "utf8" }).trim().split("\n").sort();
    assert.deepEqual(listing, [
      "config.js", "icons/icon-128.png", "icons/icon-16.png", "icons/icon-32.png", "icons/icon-48.png",
      "lib.js", "manifest.json", "popup.css", "popup.html", "popup.js",
    ]);
  } finally {
    rmSync(out1, { recursive: true, force: true });
    rmSync(out2, { recursive: true, force: true });
  }
});

test("the build refuses a name that would splice into the JSON or HTML", () => {
  const out = mkdtempSync(path.join(tmpdir(), "ext-"));
  try {
    for (const bad of ['Ex"ample', "Ex<b>ample", "Ex&ample", "a".repeat(46)]) {
      assert.throws(() => execFileSync(path.join(here, "../build.sh"), ["--url", "https://app.example.test", "--name", bad, "--out", out], { stdio: "pipe" }), bad);
    }
  } finally {
    rmSync(out, { recursive: true, force: true });
  }
});

test("the build refuses a plain-http production address and a path", () => {
  const out = mkdtempSync(path.join(tmpdir(), "ext-"));
  try {
    for (const bad of ["http://app.example.test", "https://app.example.test/some/path", "ftp://x"]) {
      assert.throws(() => execFileSync(path.join(here, "../build.sh"), ["--url", bad, "--out", out], { stdio: "pipe" }), bad);
    }
  } finally {
    rmSync(out, { recursive: true, force: true });
  }
});
