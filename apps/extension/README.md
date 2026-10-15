# Browser extension (Chrome, Manifest V3)

The smallest useful companion to the web app: a toolbar button that opens the web app, and —
on a public team page of `fantasy.premierleague.com` (an address containing `/entry/<ID>/`,
such as a team's Points, History or Transfers page) — opens the web app's **squad import with
that team's ID** already filled in. The web app then asks for sign-in (invite-only accounts)
and for confirmation of the team before anything is imported; the extension cannot bypass
either.

What it does **not** do: read anything from the page but its address; touch FPL passwords,
cookies, session tokens or any private account data; hold any API key or credential; run on
any other site; collect or send any data anywhere (the only request it causes is the one your
browser makes to the web app when you click the button).

## Layout

| Path | Purpose |
|---|---|
| `src/manifest.json` | Manifest V3; permission `activeTab` only (no host permissions, content scripts or background worker) |
| `src/lib.js` | Pure decisions: team id from an address; where the button goes |
| `src/popup.html`, `popup.css`, `popup.js` | The toolbar popup |
| `src/config.js` | The web app's origin and the product name (defaults: a local stack, "FPL Copilot") |
| `src/icons/` | Toolbar icons, drawn by `tools/make_icons.py` (standard library) |
| `build.sh` → `tools/build.py` | Substitutes origin and name, writes `dist/unpacked/` and a reproducible ZIP + SHA-256 |
| `test/lib.test.mjs` | Unit tests (`node --test`): address patterns, decisions, manifest, build reproducibility |
| `test/popup.spec.ts` | Browser tests: the built extension loaded in Chromium against a stand-in web app |
| `STORE_LISTING.md` | Chrome Web Store listing text, permission justification and privacy disclosure |

## Build

```bash
cd apps/extension
./build.sh --url https://<the web app's origin> --name "<product name>"
# → dist/unpacked/                        load this directory in Chrome
# → dist/<name>-extension-<version>.zip   the store upload, byte-identical for the same inputs
# → dist/<name>-extension-<version>.zip.sha256
```

`--url` must be an origin (no path); `http://` is accepted for `localhost` / `127.0.0.1` only.
The version is `src/manifest.json` → `version`.

## Install by hand ("Load unpacked")

1. Build as above (or use `src/` directly for a local stack on `http://localhost:3000`).
2. Open `chrome://extensions`, switch on **Developer mode** (top right).
3. **Load unpacked** → choose `apps/extension/dist/unpacked` (or `apps/extension/src`).
4. Pin the extension from the puzzle-piece menu. Open a team page on
   fantasy.premierleague.com (for example `https://fantasy.premierleague.com/entry/<ID>/event/7`)
   and click the button: it offers *Open team <ID> in <product>*. On any other page it offers
   *Open <product>* and says which pages carry a team ID.

To update after a rebuild: `chrome://extensions` → the extension's **reload** button.

## Test

```bash
cd apps/extension
npm ci                 # the Playwright test runner only (browsers are shared with apps/web)
npm test               # unit tests, incl. two builds compared byte for byte
npm run test:browser   # Chromium with the built extension: popup states, hand-off, errors
```

The browser tests start a stand-in web app on a free port, build the extension for it, load the
build into Chromium and check: the manifest declares `activeTab` only; a non-team page offers the
plain link with a hint; a team page offers `…/onboarding?entry=<ID>` and clicking it sends one
GET with no cookie, authorisation or custom header; unsupported FPL pages, missing and invalid
IDs (`/entry/0/…`, `/entry/12ab/…`, plain `http://`, look-alike hosts) fall back to the plain
link; and with the web app unreachable the popup still renders and the browser's own error page
is all the user sees.

## Privacy and permissions

* `activeTab` grants the extension the address of the current tab only when you click its button,
  for that click; it is the only permission. No host permission, so the extension never runs on
  any site, and no `tabs` permission, so it never sees your other tabs.
* The team ID in a public FPL page address is public information (the same number anyone sees
  in that address); it is passed to the web app as `?entry=<ID>` and nowhere else.
* No analytics, no storage, no network requests of its own.
