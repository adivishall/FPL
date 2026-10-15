# Browser extension privacy notice

This notice covers the Chrome extension published for the product (source:
`apps/extension/` in this repository). The web app's own notice is shown at registration.

**What the extension does.** When you click its toolbar button, it looks at the address of the
page you are on. If that page is a public team page on fantasy.premierleague.com (an address
containing `/entry/<ID>/`), it opens the product's web app with that team ID filled in for the
squad import; otherwise it opens the web app's front page.

**What it reads.** The address of the tab you clicked on, at the moment you click, under
Chrome's `activeTab` permission. Nothing on the page, nothing from any other tab, and nothing at
any other time.

**What it never reads or stores.** Your FPL password, cookies, session tokens, private account
data, browsing history or any page content. The extension has no storage, no background process,
no permission to run on any website, and no API key or credential of any kind.

**What it sends.** Nothing of its own. Clicking the button makes your browser open the web
app's address; a public team ID (the same number anyone sees in that page's address) is included
in that address. What the web app does with it is covered by the web app's notice: the ID is
only used to look up public data from the official FPL API, and nothing is stored until you
sign in and confirm the team.

**Third parties.** None. No analytics, advertising or tracking.

**Changes.** This notice is versioned with the source code; the published extension's version
is recorded in `docs/BUILD_STATUS.md`.

**Contact.** Issues at https://github.com/adivishall/fpl-copilot/issues.
