# USCIS Silent Update Tracker

Monitors multiple USCIS cases (`IOE...`) via the internal `my.uscis.gov` API, detects *silent updates* (internal changes that haven't surfaced in the public status yet) by computing SHA-256 over the relevant fields, and renders a static HTML dashboard with each case's history.

No server, no database, no separate auth — it reuses your Chrome session cookies.

---

## How it works

1. Reads Chrome cookies straight from disk (via `browser_cookie3`).
2. For each configured case, fires **two** requests:
   - `GET https://my.uscis.gov/account/case-service/api/cases/{receipt}` — internal payload (events, status flags).
   - `GET https://my.uscis.gov/account/case-service/api/case_status/{receipt}` — public-facing status (statusTitle, currentActionCode, narrative text, status history).
3. Computes **two** independent SHA-256 hashes:
   - Case hash: `updatedAt`, `status`, `events`, `closed`, `actionRequired`.
   - Status hash: `statusTitle`, `currentActionCode`, `currentActionCodeDate`, `len(historicalCaseStatuses)`.
4. A silent update fires when **either** hash changes — divergence between the two sources is itself a signal.
5. Persists **every** check to `data/{receipt}.json` (nothing is ever discarded).
6. Regenerates a self-contained `dashboard.html` (data + CSS + JS inline) — just open it in a browser.

### Multiple USCIS accounts

Each session cookie only grants access to the cases owned by the logged-in account. To monitor cases from several accounts at once, the script uses **separate Chrome profiles** — one per account — and automatically figures out which profile is authorized for which case.

- Auto-discovers profiles in `~/Library/Application Support/Google/Chrome/`
- Caches the `receipt → profile` mapping in `data/_profile_map.json`
- Automatic fallback: if the cached profile fails, it tries the others
- Quirk: USCIS returns **HTTP 500** when the logged-in account doesn't own the requested case (not 401/403), so the script treats both as "try another profile"

---

## Setup

### 1. Dependencies

```bash
python3 -m pip install -r requirements.txt
```

Just `requests` and `browser_cookie3`.

### 2. Configure cases

Copy the template and edit:

```bash
cp config.example.json config.json
```

```json
{
  "people": [
    {
      "name": "Sam",
      "cases": [
        { "label": "I-485", "receipt": "IOE0936674431" },
        { "label": "EAD",   "receipt": "IOE0936674432" }
      ]
    },
    {
      "name": "Jordan",
      "cases": [
        { "label": "I-485", "receipt": "IOE0936674427" }
      ]
    }
  ]
}
```

- `people[].name`: USCIS account owner. Cases under the same person share a Chrome profile, so `setup` probes only one case per person.
- `cases[].label`: free-form tag shown in the dashboard (e.g. `I-485`, `EAD`, `AP`).
- `cases[].receipt`: full receipt number starting with `IOE`.
- `cases[].done` (optional): set to `true` once a case is approved/closed — it stops being polled but its history in `data/` is kept.

> `config.json` is in `.gitignore` and is never committed.

### 3. Chrome profiles (one USCIS account per profile)

For each USCIS account you want to monitor:

1. In Chrome: `chrome://settings/manageProfile` → **Add** → name it (e.g. "Jordan").
2. Open `https://my.uscis.gov` inside that profile and sign in fully (including login.gov if applicable).
3. Confirm that `/account/applicant` shows the cases for that account.

> **Important**: just being signed into Google in that profile is **not enough** — the `my.uscis.gov` session is independent and must be bootstrapped by navigating to `/account/applicant`.

### 4. Map cases to profiles

```bash
python3 check.py setup
```

Example output:

```
Found 2 Chrome profiles:
  Default         Sam      sam@gmail.com
  Profile 1       Jordan   jordan@gmail.com

Probing 1 representative case per person (3 person/people, 6 case[s])...

  ✓ Sam    via IOE0936674431 (I-485)  →  Default    [mapped 2 case(s)]
  ✓ Jordan via IOE0936674427 (I-485)  →  Profile 1  [mapped 3 case(s)]
  ✗ Pat    → no authorized profile

Account 'Pat' (1 case[s]): suggested profile = "Pat"
   Open Chrome in profile 'Pat' now? [y/N]
```

For unmapped cases, `setup` offers to open Chrome in a new profile so you can sign in. After signing in, run `setup` again until everything shows `✓`.

---

## Usage

```bash
python3 check.py
```

Output:

```
[09:30:01] Chrome profiles found: Default, Profile 1
[09:30:01] Checking 6 cases...
[09:30:02] ✓ Sam I-485 (IOE0936674431) [Default] — no change
[09:30:03] 🔔 Jordan EAD (IOE0936674428) [Profile 1] — 🔔 SILENT UPDATE (case+status)
[09:30:04] ⚠️  Pat I-485 (IOExxxxxxxxx) [Default, Profile 1] — no authorized profile
[09:30:05] Dashboard written: dashboard.html
```

Open the dashboard:

```bash
open dashboard.html
```

### Mask mode (for screenshots / sharing)

The dashboard has a **Mask** toggle in the top right. When on:

- Owners become `Person A`, `Person B`, … (stable across reloads via `localStorage`).
- Receipts collapse to `IOE•••••XXXX` (last 4 preserved so you can still cross-reference).
- Any `IOE…` substring inside the status narrative tooltip is masked too.

Useful for screenshots. Note this only masks the **rendered** view — the raw `dashboard.html` source still contains the receipts in the embedded JSON payload, so don't share the file itself.

### Frequency

Once a day (in the morning ET — when USCIS actually moves cases) is more than enough. Two requests per case with small backoffs stays well below WAF radar.

### Scheduling

To run automatically, set up `launchd` (macOS) or `cron`. The script is idempotent — if the session has expired, it flags `cookie_expired` in state and the dashboard shows a banner.

---

## Layout

```
uscis-tracker/
├── check.py                # main script (CLI)
├── config.json             # cases to monitor (gitignored)
├── config.example.json     # template
├── requirements.txt
├── data/                   # history (gitignored)
│   ├── IOE0936674431.json     # one file per receipt
│   ├── ...
│   └── _profile_map.json      # cached receipt → Chrome profile mapping
└── dashboard.html          # regenerated each run (gitignored)
```

### `data/{receipt}.json` format

```json
{
  "receipt": "IOE0936674431",
  "name": "Sam I-485",
  "last_sha": "a3f9b2...",
  "cookie_expired": false,
  "profile": "Default",
  "checks": [
    {
      "timestamp": "2026-05-26T17:40:48",
      "sha": "a3f9b2...",
      "changed": false,
      "cookie_expired": false,
      "profile": "Default",
      "snapshot": { "...": "raw API JSON" }
    }
  ]
}
```

Every check is preserved — the `checks` array grows over time.

---

## Dashboard

`dashboard.html` is a single, self-contained file. Dark / amber / monospace aesthetic, no external dependencies beyond Google Fonts (IBM Plex). Per case it shows:

- Current public status (when available)
- Internal `updatedAt`
- Recent events (last 5, with `eventCode` + timestamp)
- Check history (last 20, with SHA and `CHANGED` flag)
- Status badge: `● NO CHANGE`, `🔔 SILENT UPDATE` (pulsing), or `⚠️ COOKIE EXPIRED`

---

## Caveats

- **Undocumented endpoint**: `case-service/api/cases/{receipt}` is internal. It can change without notice.
- **Cloudflare**: the public `egov.uscis.gov/csol-api/case-tracking/` endpoint is protected by Cloudflare JS challenges, so the script can't enrich snapshots with the friendly status text — only the raw `eventCode`. A local code → label mapping can be added later.
- **Full Disk Access (macOS)**: `browser_cookie3` reads Chrome's cookie SQLite. You may need to grant *Full Disk Access* to your Terminal/iTerm/VS Code under **System Settings → Privacy & Security**.
- **Cookies still in RAM**: incognito cookies and freshly-set cookies in a running Chrome may not be flushed to disk yet. Flushes usually happen within seconds.
- **Session expiration**: the `my.uscis.gov` session expires after a few hours of inactivity. When that happens the case is marked `cookie_expired: true` until you reopen the profile and sign in again.

---

## Privacy

All data stays local. Nothing is sent anywhere outside of the requests to USCIS itself. `.gitignore` excludes:

- `config.json` (names + receipts)
- `data/` (full snapshots)
- `dashboard.html` (which renders the data)
