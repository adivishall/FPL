# Chrome Web Store listing (draft; fill `<product>` and `<site>` from the release)

**Name:** `<product>` (≤ 45 characters; the manifest's `name`)

**Summary (≤ 132 characters):** One click from a fantasy.premierleague.com team page to the
squad import in `<product>`. Only the page address is read.

**Category:** Productivity → Tools. **Language:** English.

**Description:**

> `<product>` is an invite-only Fantasy Premier League planning web app: probabilistic forecasts,
> whole-squad transfer plans compared with holding, and a decision journal checked against the
> official results.
>
> This extension is the shortest way in. Click its button on a team page of
> fantasy.premierleague.com (an address containing /entry/<ID>/ — a team's Points, History or
> Transfers page) and it opens `<site>` with that team's ID filled in for the import. On any
> other page it simply opens `<site>`.
>
> It reads nothing from the page except its address, and only when you click. It never touches
> your FPL password, cookies or session, holds no API key, runs on no website, and collects no
> data. Sign-in (invitation required) and confirmation of the team happen in the web app.
>
> Not affiliated with the Premier League or Fantasy Premier League.

**Single purpose:** open the current fantasy.premierleague.com team page's public team ID in the
`<product>` web app's import flow (or open the web app).

**Permission justification:**

| Permission | Why |
|---|---|
| `activeTab` | To read the address of the tab you clicked the button on, so the team ID in it can be passed to the web app. Granted per click; no other tab or page content is accessible. |

No host permissions, content scripts, background service worker, remote code, or `storage`.

**Data usage disclosure (Privacy practices tab):** the extension does **not** collect or
transmit user data. It does not handle personally identifiable information, health, financial,
authentication, personal communications, location, web history, user activity or website
content. The only network request it causes is the navigation your browser makes to `<site>`
when you click the button, which carries the public team ID from the page address.

**Privacy policy URL:** `https://github.com/adivishall/fpl-copilot/blob/develop/docs/EXTENSION_PRIVACY.md`
**Support URL / homepage:** `https://github.com/adivishall/fpl-copilot` (issues).

**Assets:** icon 128×128 (`src/icons/icon-128.png`); at least one screenshot 1280×800 or
640×400 (`dist/store/popup-team.png`, `dist/store/popup-site.png`, made by
`tools/store_shots.sh`); no promo tiles required.

**Distribution:** public listing or unlisted (link only) — unlisted fits an invite-only beta.

**Before submitting:** build with the production `--url` and the chosen `--name`, run both test
suites, install the ZIP's contents with *Load unpacked* once, and record the ZIP's SHA-256 next
to the submission ID in `docs/BUILD_STATUS.md`.
