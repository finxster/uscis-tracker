#!/usr/bin/env python3
"""USCIS Silent Update Tracker.

Polls the internal my.uscis.gov case-service endpoint for each configured
receipt, detects silent updates via SHA-256 of the relevant fields,
persists per-case history in data/, and rebuilds dashboard.html.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

try:
    import browser_cookie3
except ImportError:
    print("ERROR: browser_cookie3 not installed. Run: pip install -r requirements.txt")
    sys.exit(1)


ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
DASHBOARD_PATH = ROOT / "dashboard.html"
PROFILE_MAP_PATH = ROOT / "data" / "_profile_map.json"
API_BASE = "https://my.uscis.gov/account/case-service/api/cases/"
STATUS_API_BASE = "https://my.uscis.gov/account/case-service/api/case_status/"
TIMEOUT = 20
REQUEST_DELAY = 0.4  # politeness between requests


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_hms()}] {msg}")


def chrome_base_dir() -> Path | None:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    if system == "Linux":
        return Path.home() / ".config/google-chrome"
    if system == "Windows":
        return Path.home() / "AppData/Local/Google/Chrome/User Data"
    return None


def discover_chrome_profiles() -> list[dict]:
    base = chrome_base_dir()
    if not base or not base.exists():
        return []

    info_cache: dict = {}
    local_state = base / "Local State"
    if local_state.exists():
        try:
            with local_state.open("r", encoding="utf-8") as f:
                ls = json.load(f)
            info_cache = ls.get("profile", {}).get("info_cache", {})
        except Exception:
            pass

    profiles = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        if child.name != "Default" and not child.name.startswith("Profile "):
            continue
        cookies = child / "Network" / "Cookies"
        if not cookies.exists():
            cookies = child / "Cookies"
        if not cookies.exists():
            continue
        info = info_cache.get(child.name, {})
        profiles.append({
            "dir": child.name,
            "cookie_file": str(cookies),
            "display": info.get("name") or child.name,
            "user_name": info.get("user_name", ""),
        })
    return profiles


def load_cookies_for_profile(profile: dict):
    return browser_cookie3.chrome(
        domain_name=".uscis.gov",
        cookie_file=profile["cookie_file"],
    )


def load_profile_map() -> dict:
    if not PROFILE_MAP_PATH.exists():
        return {}
    try:
        with PROFILE_MAP_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_profile_map(pmap: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with PROFILE_MAP_PATH.open("w", encoding="utf-8") as f:
        json.dump(pmap, f, ensure_ascii=False, indent=2)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"ERROR: config.json not found at {CONFIG_PATH}")
        sys.exit(1)
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def iter_cases(config: dict) -> list[dict]:
    """Flatten config into [{owner, label, name, receipt}, ...]."""
    out = []
    for person in config.get("people", []):
        owner = (person.get("name") or "").strip()
        for c in person.get("cases", []):
            label = (c.get("label") or "").strip()
            receipt = (c.get("receipt") or "").strip()
            display = f"{owner} {label}".strip() if label else owner
            out.append({
                "owner": owner,
                "label": label,
                "name": display,
                "receipt": receipt,
            })
    return out


def load_history(receipt: str) -> dict:
    path = DATA_DIR / f"{receipt}.json"
    if not path.exists():
        return {
            "receipt": receipt,
            "name": None,
            "last_sha": None,
            "cookie_expired": False,
            "checks": [],
        }
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_history(history: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{history['receipt']}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def compute_sha(data: dict) -> str:
    relevant = {
        "updatedAt": data.get("updatedAt") or data.get("lastUpdatedAt"),
        "status": data.get("status"),
        "events": data.get("events"),
        "closed": data.get("closed"),
        "actionRequired": data.get("actionRequired"),
    }
    serialized = json.dumps(relevant, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode()).hexdigest()


def compute_status_sha(data: dict) -> str:
    relevant = {
        "statusTitle": data.get("statusTitle"),
        "currentActionCode": data.get("currentActionCode"),
        "currentActionCodeDate": data.get("currentActionCodeDate"),
        "historicalLen": len(data.get("historicalCaseStatuses") or []),
    }
    serialized = json.dumps(relevant, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode()).hexdigest()


def looks_like_valid_case(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    return bool(data.get("receiptNumber") or data.get("formType"))


def looks_like_valid_status(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    return bool(data.get("statusTitle") or data.get("currentActionCode") or data.get("formType"))


_HTTP_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://my.uscis.gov/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}


def _get_json(url: str, cookiejar) -> tuple[Any, str | None]:
    """Low-level GET that unwraps {"data": ...}. Returns (payload, error_tag)."""
    try:
        resp = requests.get(url, headers=_HTTP_HEADERS, cookies=cookiejar, timeout=TIMEOUT)
    except requests.RequestException as e:
        return None, f"network:{e.__class__.__name__}"

    if resp.status_code in (401, 403):
        return None, "auth"
    if resp.status_code == 404:
        return None, "notfound"
    if resp.status_code >= 400:
        return None, f"http:{resp.status_code}"

    try:
        data = resp.json()
    except ValueError:
        return None, "auth"  # USCIS returns HTML login page when session dies

    if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
        data = data["data"]
    return data, None


def fetch_case(receipt: str, cookiejar) -> tuple[dict | None, str | None]:
    """Return (snapshot, error). error is None on success, or a short tag."""
    data, err = _get_json(API_BASE + receipt, cookiejar)
    if err:
        return None, err
    if not looks_like_valid_case(data):
        return None, "auth"
    return data, None


def fetch_case_status(receipt: str, cookiejar) -> tuple[dict | None, str | None]:
    data, err = _get_json(STATUS_API_BASE + receipt, cookiejar)
    if err:
        return None, err
    if not looks_like_valid_status(data):
        return None, "auth"
    return data, None


class ProfileCookieLoader:
    """Lazily loads cookiejars per Chrome profile, caching results."""

    def __init__(self, profiles: list[dict]):
        self.profiles = {p["dir"]: p for p in profiles}
        self._cache: dict = {}

    def get(self, profile_dir: str):
        if profile_dir in self._cache:
            return self._cache[profile_dir]
        prof = self.profiles.get(profile_dir)
        if not prof:
            self._cache[profile_dir] = None
            return None
        try:
            cj = load_cookies_for_profile(prof)
        except Exception as e:
            log(f"   (failed to read cookies from {profile_dir}: {e})")
            cj = None
        self._cache[profile_dir] = cj
        return cj


def try_case_against_profiles(
    receipt: str,
    profile_order: list[str],
    loader: ProfileCookieLoader,
) -> tuple[dict | None, str | None, str | None]:
    """Try each profile in order. Return (snapshot, last_error, profile_used)."""
    last_err = None
    for prof_dir in profile_order:
        cj = loader.get(prof_dir)
        if cj is None:
            continue
        snapshot, err = fetch_case(receipt, cj)
        time.sleep(REQUEST_DELAY)
        if snapshot is not None:
            return snapshot, None, prof_dir
        last_err = err
        # USCIS returns 500 when the logged-in account doesn't own the case,
        # and 401/403 ("auth") when not logged in at all. Both warrant trying
        # another profile. Only bail on network errors / 404.
        if err not in ("auth", "http:500"):
            return None, err, None
    return None, last_err or "auth", None


def cmd_check() -> int:
    config = load_config()
    cases = iter_cases(config)
    if not cases:
        print("ERROR: no cases configured in config.json")
        return 1

    DATA_DIR.mkdir(exist_ok=True)

    profiles = discover_chrome_profiles()
    if not profiles:
        print("ERROR: no Chrome profiles found.")
        print("Sign in to my.uscis.gov in Chrome first.")
        return 1
    log(f"Chrome profiles found: {', '.join(p['dir'] for p in profiles)}")

    loader = ProfileCookieLoader(profiles)
    pmap = load_profile_map()

    log(f"Checking {len(cases)} cases...")

    for case in cases:
        name = case.get("name", "?")
        receipt = case.get("receipt", "").strip()
        if not receipt:
            log(f"⚠️  {name} — empty receipt, skipping")
            continue

        history = load_history(receipt)
        history["name"] = name

        # Build profile order: cached preferred first, then everyone else
        preferred = pmap.get(receipt)
        order: list[str] = []
        if preferred and preferred in {p["dir"] for p in profiles}:
            order.append(preferred)
        for p in profiles:
            if p["dir"] not in order:
                order.append(p["dir"])

        snapshot, err, used = try_case_against_profiles(receipt, order, loader)

        if snapshot is None:
            if err in ("auth", "http:500"):
                history["cookie_expired"] = True
                history["checks"].append({
                    "timestamp": now_iso(), "sha": None, "changed": False,
                    "cookie_expired": True, "error": "auth", "snapshot": None,
                })
                save_history(history)
                hint = f" (tried: {', '.join(order)})"
                log(f"⚠️  {name} ({receipt}) — no authorized profile{hint}")
            else:
                history["checks"].append({
                    "timestamp": now_iso(), "sha": None, "changed": False,
                    "cookie_expired": False, "error": err, "snapshot": None,
                })
                save_history(history)
                log(f"⚠️  {name} ({receipt}) — error: {err}")
            continue

        pmap[receipt] = used
        sha = compute_sha(snapshot)
        case_changed = history.get("last_sha") is not None and sha != history["last_sha"]

        # Second source: case_status (rich narrative + currentActionCode).
        # Same profile that authorized /cases/ should authorize this too.
        cj_used = loader.get(used)
        status_snapshot, status_err = fetch_case_status(receipt, cj_used)
        time.sleep(REQUEST_DELAY)
        status_sha = compute_status_sha(status_snapshot) if status_snapshot else None
        status_changed = (
            status_sha is not None
            and history.get("last_status_sha") is not None
            and status_sha != history["last_status_sha"]
        )

        changed = case_changed or status_changed

        history["cookie_expired"] = False
        history["profile"] = used
        history["checks"].append({
            "timestamp": now_iso(),
            "sha": sha,
            "status_sha": status_sha,
            "changed": changed,
            "case_changed": case_changed,
            "status_changed": status_changed,
            "cookie_expired": False,
            "profile": used,
            "snapshot": snapshot,
            "status_snapshot": status_snapshot,
            "status_error": status_err,
        })
        history["last_sha"] = sha
        if status_sha is not None:
            history["last_status_sha"] = status_sha
        save_history(history)

        if changed:
            which = []
            if case_changed:
                which.append("case")
            if status_changed:
                which.append("status")
            marker = f"🔔 SILENT UPDATE ({'+'.join(which)})"
        else:
            marker = "first check" if len(history["checks"]) == 1 else "no change"
        log(f"{'🔔' if changed else '✓'} {name} ({receipt}) [{used}] — {marker}")

    save_profile_map(pmap)
    write_dashboard(cases)
    log(f"Dashboard written: {DASHBOARD_PATH}")
    return 0


def launch_chrome_profile(profile_dir: str) -> None:
    system = platform.system()
    url = "https://my.uscis.gov/"
    if system == "Darwin":
        subprocess.Popen([
            "open", "-a", "Google Chrome", "--args",
            f"--profile-directory={profile_dir}", url,
        ])
    elif system == "Linux":
        subprocess.Popen([
            "google-chrome", f"--profile-directory={profile_dir}", url,
        ])
    elif system == "Windows":
        subprocess.Popen([
            "cmd", "/c", "start", "chrome",
            f"--profile-directory={profile_dir}", url,
        ])


def cmd_setup() -> int:
    config = load_config()
    people = config.get("people", [])
    DATA_DIR.mkdir(exist_ok=True)

    profiles = discover_chrome_profiles()
    print(f"\nFound {len(profiles)} Chrome profile(s):")
    for p in profiles:
        acct = p["user_name"] or "(no Google sign-in)"
        print(f"  {p['dir']:14s}  {p['display']:30s}  {acct}")
    if not profiles:
        print("  (none)")

    loader = ProfileCookieLoader(profiles)
    pmap = load_profile_map()

    total_cases = sum(len(p.get("cases", [])) for p in people)
    print(f"\nProbing 1 representative case per person ({len(people)} person/people, {total_cases} case[s])...\n")

    unmapped_people = []
    for person in people:
        owner = (person.get("name") or "Account").strip()
        cases = person.get("cases", [])
        if not cases:
            continue

        mapped_profile = None
        last_err = None
        order = [p["dir"] for p in profiles]

        # Try cases in order: usually the first works, but if a receipt is
        # invalid/closed (404) we want to try the next one from the same owner
        # before declaring the whole group unmapped.
        for case in cases:
            receipt = (case.get("receipt") or "").strip()
            if not receipt:
                continue
            snapshot, err, used = try_case_against_profiles(receipt, order, loader)
            if snapshot is not None:
                mapped_profile = used
                for c in cases:
                    r = (c.get("receipt") or "").strip()
                    if r:
                        pmap[r] = used
                label = (case.get("label") or "").strip()
                via = f"{receipt}" + (f" ({label})" if label else "")
                print(f"  ✓ {owner:14s} via {via}  →  {used}  [mapped {len(cases)} case(s)]")
                break
            last_err = err
            # auth/http:500 means no profile is authorized for this owner at all;
            # trying sibling receipts won't change that. Stop early.
            if err in ("auth", "http:500"):
                break

        if mapped_profile is None:
            tag = f" (last error: {last_err})" if last_err and last_err != "auth" else ""
            print(f"  ✗ {owner:14s} → no authorized profile{tag}")
            unmapped_people.append(person)

    save_profile_map(pmap)

    if not unmapped_people:
        print("\nAll people mapped. Run: python check.py")
        return 0

    print(f"\n{len(unmapped_people)} person/people without an authorized profile.")
    print("Suggestion: one Chrome profile per USCIS account.\n")

    existing_dirs = {p["dir"] for p in profiles}
    for person in unmapped_people:
        owner = (person.get("name") or "Account").strip()
        cases = person.get("cases", [])
        suggested = owner
        i = 2
        while suggested in existing_dirs:
            suggested = f"{owner}{i}"
            i += 1
        print(f"  Account '{owner}' ({len(cases)} case[s]): suggested profile = \"{suggested}\"")
        for c in cases:
            label = (c.get("label") or "").strip()
            tag = f" — {label}" if label else ""
            print(f"     • {c.get('receipt', '')}{tag}")
        ans = input(f"     Open Chrome in profile '{suggested}' now? [y/N] ").strip().lower()
        if ans == "y":
            launch_chrome_profile(suggested)
            existing_dirs.add(suggested)
            print(f"     → Chrome opened. Sign in to '{owner}'s account, then run:")
            print(f"        python check.py setup")
        print()

    return 0


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "setup":
        return cmd_setup()
    return cmd_check()


def collect_dashboard_data(cases: list[dict]) -> list[dict]:
    out = []
    for case in cases:
        receipt = case.get("receipt", "").strip()
        if not receipt:
            continue
        path = DATA_DIR / f"{receipt}.json"
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                entry = json.load(f)
        else:
            entry = {
                "receipt": receipt,
                "name": case.get("name"),
                "last_sha": None,
                "cookie_expired": False,
                "checks": [],
            }
        # Annotate with config-level owner/label so the dashboard can mask
        # independently of the composed name string stored in history files.
        entry["owner"] = case.get("owner") or ""
        entry["label"] = case.get("label") or ""
        out.append(entry)
    return out


def write_dashboard(cases: list[dict]) -> None:
    data = collect_dashboard_data(cases)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    generated_at = now_iso()
    html = DASHBOARD_TEMPLATE.replace("__DATA__", payload).replace("__GENERATED__", generated_at)
    DASHBOARD_PATH.write_text(html, encoding="utf-8")


DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>USCIS Silent Update Tracker</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet" />
<style>
  :root {
    --bg: #0d0f14;
    --panel: #151921;
    --panel-2: #1c2129;
    --border: #252b36;
    --text: #d7dbe2;
    --muted: #7a818d;
    --ok: #4ade80;
    --warn: #f5a524;
    --err: #f87171;
    --accent: #ffb547;
  }
  * { box-sizing: border-box; }
  html, body {
    background: var(--bg);
    color: var(--text);
    margin: 0;
    font-family: 'IBM Plex Sans', system-ui, sans-serif;
    font-size: 14px;
    line-height: 1.45;
  }
  .mono { font-family: 'IBM Plex Mono', ui-monospace, monospace; }
  header {
    padding: 24px 32px 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
  }
  header h1 {
    font-size: 18px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin: 0;
    color: var(--accent);
  }
  header .meta {
    color: var(--muted);
    font-size: 12px;
    font-family: 'IBM Plex Mono', monospace;
  }
  header .global-badge {
    display: inline-block;
    margin-left: 12px;
    padding: 3px 10px;
    border: 1px solid var(--accent);
    color: var(--accent);
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
  }
  .cookie-banner {
    margin: 16px 32px 0;
    padding: 12px 16px;
    border: 1px solid var(--warn);
    background: rgba(245, 165, 36, 0.08);
    color: var(--warn);
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
  }
  main {
    padding: 24px 32px 48px;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(440px, 1fr));
    gap: 16px;
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    padding: 16px 18px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .card-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 12px;
  }
  .card-name {
    font-weight: 600;
    font-size: 15px;
    color: var(--text);
  }
  .card-receipt {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    color: var(--muted);
    margin-top: 2px;
    letter-spacing: 0.04em;
  }
  .badge {
    display: inline-block;
    padding: 3px 8px;
    border: 1px solid currentColor;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    white-space: nowrap;
  }
  .badge.ok { color: var(--ok); }
  .badge.update { color: var(--accent); animation: pulse 1.6s infinite; }
  .badge.err { color: var(--err); }
  .badge.idle { color: var(--muted); }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.45; }
  }
  .field-grid {
    display: grid;
    grid-template-columns: 110px 1fr;
    gap: 4px 12px;
    font-size: 13px;
  }
  .field-grid dt {
    color: var(--muted);
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    align-self: center;
  }
  .field-grid dd {
    margin: 0;
    color: var(--text);
    word-break: break-word;
  }
  .events {
    border-top: 1px solid var(--border);
    padding-top: 10px;
  }
  .events h4 {
    margin: 0 0 6px;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    font-weight: 500;
  }
  .events ul {
    list-style: none;
    margin: 0;
    padding: 0;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
  }
  .events li {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 2px 0;
    color: var(--text);
  }
  .events li span.ts { color: var(--muted); }
  .history {
    border-top: 1px solid var(--border);
    padding-top: 10px;
  }
  .history h4 {
    margin: 0 0 6px;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    font-weight: 500;
  }
  table.history-table {
    width: 100%;
    border-collapse: collapse;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
  }
  table.history-table th, table.history-table td {
    text-align: left;
    padding: 4px 8px;
    border-bottom: 1px solid var(--border);
  }
  table.history-table th {
    color: var(--muted);
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  table.history-table tr.changed td { color: var(--accent); }
  table.history-table tr.error td { color: var(--err); }
  .pill {
    display: inline-block;
    padding: 1px 6px;
    border: 1px solid currentColor;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .empty {
    color: var(--muted);
    font-style: italic;
    font-size: 12px;
  }
  footer {
    padding: 16px 32px 32px;
    border-top: 1px solid var(--border);
    color: var(--muted);
    font-size: 12px;
    font-family: 'IBM Plex Mono', monospace;
  }
  footer code {
    background: var(--panel-2);
    padding: 1px 6px;
    color: var(--text);
  }
  button.mask-toggle {
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--border);
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    padding: 6px 12px;
    cursor: pointer;
  }
  button.mask-toggle:hover { border-color: var(--accent); color: var(--accent); }
  button.mask-toggle.on { border-color: var(--accent); color: var(--accent); background: rgba(255, 181, 71, 0.08); }
</style>
</head>
<body>
<header>
  <div>
    <h1>USCIS Silent Update Tracker</h1>
    <div class="meta">Last run: <span id="generated">__GENERATED__</span><span id="globalBadge"></span></div>
  </div>
  <button id="maskBtn" class="mask-toggle" type="button">Mask: Off</button>
</header>
<div id="cookieBanner"></div>
<main id="cards"></main>
<footer>
  Refresh cookie: sign in to <code>my.uscis.gov</code> in Chrome &nbsp;·&nbsp;
  Re-run: <code>python check.py</code> &nbsp;·&nbsp;
  History under <code>data/{receipt}.json</code>
</footer>

<script>
const CASES = __DATA__;

let MASKED = localStorage.getItem('uscis_masked') === '1';

const OWNER_LETTERS = (() => {
  const seen = [];
  for (const c of CASES) {
    const o = (c.owner || '').trim();
    if (o && !seen.includes(o)) seen.push(o);
  }
  const map = {};
  seen.forEach((o, i) => {
    map[o] = String.fromCharCode(65 + (i % 26));
  });
  return map;
})();

function maskOwner(owner) {
  if (!MASKED) return owner || '';
  const letter = OWNER_LETTERS[owner] || '?';
  return `Person ${letter}`;
}

function maskReceipt(r) {
  if (!MASKED || !r) return r || '';
  if (r.length <= 7) return r.replace(/./g, '•');
  const head = r.substring(0, 3);
  const tail = r.substring(r.length - 4);
  return `${head}${'•'.repeat(r.length - 7)}${tail}`;
}

function maskName(caseData) {
  if (!MASKED) return caseData.name || '—';
  const owner = caseData.owner ? maskOwner(caseData.owner) : '';
  const label = caseData.label || '';
  if (owner && label) return `${owner} ${label}`;
  return owner || label || '—';
}

function maskText(s) {
  // Replace any IOE+digits occurrences inside free-form text (e.g. statusText).
  if (!MASKED || !s) return s || '';
  return s.replace(/IOE\d+/g, m => maskReceipt(m));
}

function fmtTs(s) {
  if (!s) return '—';
  try {
    const d = new Date(s);
    if (isNaN(d.getTime())) return s;
    return d.toLocaleString('en-CA', { hour12: false }).replace(',', '');
  } catch (e) { return s; }
}

function lastSuccessCheck(checks) {
  for (let i = checks.length - 1; i >= 0; i--) {
    if (checks[i].snapshot) return checks[i];
  }
  return null;
}

function changedSinceLastRun(checks) {
  if (!checks.length) return false;
  const last = checks[checks.length - 1];
  return !!last.changed;
}

function renderBadge(caseData) {
  if (caseData.cookie_expired) {
    return '<span class="badge err">⚠ Cookie Expired</span>';
  }
  if (!caseData.checks || !caseData.checks.length) {
    return '<span class="badge idle">● No data</span>';
  }
  const last = caseData.checks[caseData.checks.length - 1];
  if (last.error) return '<span class="badge err">⚠ Error</span>';
  if (last.changed) return '<span class="badge update">🔔 Silent Update</span>';
  return '<span class="badge ok">● No Change</span>';
}

function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function renderEvents(events) {
  if (!Array.isArray(events) || !events.length) {
    return '<div class="empty">No events</div>';
  }
  const sorted = [...events].sort((a, b) => {
    const ta = a.createdAtTimestamp || a.createdAt || a.timestamp || '';
    const tb = b.createdAtTimestamp || b.createdAt || b.timestamp || '';
    return tb.localeCompare(ta);
  }).slice(0, 5);
  return '<ul>' + sorted.map(ev => {
    const code = ev.code || ev.eventCode || ev.type || '?';
    const ts = ev.createdAtTimestamp || ev.createdAt || ev.timestamp || '';
    return `<li><span>${escapeHtml(code)}</span><span class="ts">${escapeHtml(fmtTs(ts))}</span></li>`;
  }).join('') + '</ul>';
}

function renderHistory(checks) {
  if (!checks || !checks.length) {
    return '<div class="empty">No checks yet</div>';
  }
  const rows = [...checks].slice(-20).reverse().map(c => {
    const cls = c.error ? 'error' : (c.changed ? 'changed' : '');
    const sha = c.sha ? c.sha.substring(0, 8) : '—';
    let mark;
    if (c.error) {
      mark = `<span class="pill">${escapeHtml(c.error)}</span>`;
    } else if (c.changed) {
      // For older entries without case_changed/status_changed fields, fall back to CHANGED.
      const tags = [];
      if (c.case_changed) tags.push('CASE');
      if (c.status_changed) tags.push('STATUS');
      const label = tags.length ? tags.join('+') : 'CHANGED';
      mark = `<span class="pill">${label}</span>`;
    } else {
      mark = '·';
    }
    return `<tr class="${cls}"><td>${escapeHtml(fmtTs(c.timestamp))}</td><td>${sha}</td><td>${mark}</td></tr>`;
  }).join('');
  return `<table class="history-table">
    <thead><tr><th>Timestamp</th><th>SHA</th><th>Change</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function stripHtml(s) {
  if (!s) return '';
  const div = document.createElement('div');
  div.innerHTML = s;
  return (div.textContent || div.innerText || '').replace(/\s+/g, ' ').trim();
}

function lastStatusSnapshot(checks) {
  for (let i = checks.length - 1; i >= 0; i--) {
    if (checks[i].status_snapshot) return checks[i].status_snapshot;
  }
  return null;
}

function renderCard(caseData) {
  const last = lastSuccessCheck(caseData.checks || []);
  const snap = last ? last.snapshot : null;
  const statusSnap = lastStatusSnapshot(caseData.checks || []);

  // Prefer the rich title from case_status; fall back to the /cases/ status.
  const statusTitle = statusSnap ? statusSnap.statusTitle : null;
  const statusFallback = snap ? (snap.statusText || snap.status || '—') : '—';
  const subStatus = snap ? (snap.subStatusText || '') : '';
  const updatedAt = snap ? (snap.updatedAtTimestamp || snap.lastUpdatedAtTimestamp || snap.updatedAt || snap.lastUpdatedAt || '') : '';
  const formType = (snap && snap.formType) || (statusSnap && statusSnap.formType) || '';
  const actionRequired = snap && snap.actionRequired;
  const closed = snap && snap.closed;

  const actionCode = statusSnap ? statusSnap.currentActionCode : null;
  const actionDate = statusSnap ? statusSnap.currentActionCodeDate : null;
  const statusTextRaw = statusSnap ? stripHtml(statusSnap.statusText) : '';
  const histLen = statusSnap && Array.isArray(statusSnap.historicalCaseStatuses)
    ? statusSnap.historicalCaseStatuses.length : 0;

  const statusDisplay = statusTitle || statusFallback;
  const statusTooltipText = statusTextRaw ? maskText(statusTextRaw) : '';
  const statusTooltip = statusTooltipText ? ` title="${escapeHtml(statusTooltipText)}"` : '';

  return `
    <article class="card">
      <div class="card-head">
        <div>
          <div class="card-name">${escapeHtml(maskName(caseData))}</div>
          <div class="card-receipt">${escapeHtml(maskReceipt(caseData.receipt))}${formType ? ' · ' + escapeHtml(formType) : ''}</div>
        </div>
        <div>${renderBadge(caseData)}</div>
      </div>
      <dl class="field-grid">
        <dt>Status</dt><dd${statusTooltip} style="cursor: ${statusTextRaw ? 'help' : 'default'}">${escapeHtml(statusDisplay)}${subStatus ? ' <span style="color:var(--muted)">· ' + escapeHtml(subStatus) + '</span>' : ''}</dd>
        ${actionCode ? `<dt>Action</dt><dd class="mono" style="font-size:12px">${escapeHtml(actionCode)}${actionDate ? ' <span style="color:var(--muted)">@ ' + escapeHtml(fmtTs(actionDate)) + '</span>' : ''}</dd>` : ''}
        <dt>Updated</dt><dd class="mono" style="font-size:12px">${escapeHtml(fmtTs(updatedAt))}</dd>
        ${histLen ? `<dt>History</dt><dd class="mono" style="font-size:12px">${histLen} prior status${histLen > 1 ? 'es' : ''}</dd>` : ''}
        ${actionRequired ? '<dt>Flag</dt><dd style="color:var(--accent)">Action Required</dd>' : ''}
        ${closed ? '<dt>Flag</dt><dd style="color:var(--muted)">Closed</dd>' : ''}
      </dl>
      <div class="events">
        <h4>Recent events</h4>
        ${renderEvents(snap ? snap.events : null)}
      </div>
      <div class="history">
        <h4>Checks (last 20)</h4>
        ${renderHistory(caseData.checks)}
      </div>
    </article>
  `;
}

function render() {
  const cookieExpired = CASES.some(c => c.cookie_expired);
  if (cookieExpired) {
    document.getElementById('cookieBanner').innerHTML =
      '<div class="cookie-banner">⚠ Session expired — sign in to my.uscis.gov in Chrome and re-run the script</div>';
  } else {
    document.getElementById('cookieBanner').innerHTML = '';
  }
  const updatesCount = CASES.filter(c => changedSinceLastRun(c.checks || [])).length;
  const globalBadge = document.getElementById('globalBadge');
  globalBadge.innerHTML = updatesCount > 0
    ? ` <span class="global-badge">${updatesCount} update${updatesCount > 1 ? 's' : ''} since last check</span>`
    : '';
  document.getElementById('cards').innerHTML = CASES.map(renderCard).join('');

  const btn = document.getElementById('maskBtn');
  btn.textContent = MASKED ? 'Mask: On' : 'Mask: Off';
  btn.classList.toggle('on', MASKED);
}

document.getElementById('maskBtn').addEventListener('click', () => {
  MASKED = !MASKED;
  localStorage.setItem('uscis_masked', MASKED ? '1' : '0');
  render();
});

render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
