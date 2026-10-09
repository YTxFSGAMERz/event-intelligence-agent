# Event Intelligence Agent

A lightweight Python starter that checks configured public RSS/Atom feeds every 15 minutes via GitHub Actions and sends new matching opportunities to Telegram.

## What it does
- Reads public RSS/Atom feeds you configure in `sources.txt`.
- Filters for hackathons, student events, prizes, swag, gadgets, scholarships and funded travel.
- Deduplicates events using a stable hash of the normalized URL (with a title fallback).
- Saves seen-event state in `data/seen_events.json` and commits updates back to the repository.
- Extracts likely deadlines from event text when they are explicitly written.
- Checks the event URL is reachable and labels that check separately from registration availability.
- Sends Telegram alerts for newly discovered matching entries.

## Canonical opportunity schema (v1)

The tracker uses an additive, versioned schema. Legacy keys such as `url`, `deadline`, `registration_status`, `image_url`, and `source` remain temporarily for compatibility with the Telegram workflow and dashboard.

New normalized fields include:

- **Identity and provenance:** `id`, `canonical_url`, `discovered_url`, `source_feed_url`, `organizer`, and `summary`.
- **Separate timelines:** `application_open_at`, `application_deadline`, `application_deadline_raw`, `deadline_status`, `deadline_timezone`, `event_start_at`, `event_end_at`, `event_timezone`, and `date_precision`.
- **Location:** raw location, mode (`in_person`, `remote`, `hybrid`, or `unknown`), venue, city, region, country, country code, and remote restrictions.
- **Eligibility and value:** structured eligibility requirements, reward amounts/currency, and travel-support details for flights, transport reimbursement, accommodation, meals, visa support, conditions, and evidence URLs.
- **Verification:** official URL, evidence URLs, last-checked timestamps, content hash, and material-change timestamp.

### Data integrity rules

- Missing dates stay `null`; no synthetic deadline or event date is generated.
- A deadline extracted from a feed is **unverified** until checked against an official source.
- A Google News redirect is retained as `discovered_url`, not mislabelled as the event's canonical/official URL.
- Unknown location, eligibility, prize value, and travel support remain explicitly unknown; absence of evidence does not mean support is unavailable.
- Existing records are migrated additively. The legacy fields remain available, and the `schema_version` field identifies the normalized record version.
- `last_seen_utc` means the item was observed in a feed; it does **not** mean its deadline, eligibility, or funding was verified at that time.

## Important limits
- GitHub Actions scheduled workflows are best-effort, not exact timers; runs may be delayed. GitHub documents a minimum schedule interval of 5 minutes, and scheduled workflows run from the default branch. See https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- RSS feeds are only as good as their publishers. Add official feeds and public announcement sources you trust.
- `URL reachable` does **not** mean registration is open or a free ticket is available. The starter labels registration status `UNKNOWN` unless explicit text signals are found in the feed item. It does not log in, solve CAPTCHAs, register you, pay, or bypass restrictions.
- Deadline extraction is heuristic. Verify dates on the official event page before acting.
- Respect each site's terms, robots rules, API limits and rate limits. Do not add private or access-controlled feeds without permission.

## Setup

### 1. Create a GitHub repository
Create a repository (for example `event-intelligence-agent`) and upload these files. Keep it private if you prefer.

### 2. Create a Telegram bot
1. In Telegram, open the official `@BotFather` account and create a bot.
2. Copy the bot token into a GitHub Actions secret named `TELEGRAM_BOT_TOKEN`.
3. Open your bot and send it `/start`.
4. Obtain your chat ID using the Telegram Bot API `getUpdates` endpoint after messaging the bot. Keep the token private.
5. Add a repository Actions secret named `TELEGRAM_CHAT_ID`.

Never put the token directly in source code or commit it to Git.

### 3. Add repository secrets
In GitHub: **Settings → Secrets and variables → Actions → New repository secret**
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### 4. Configure sources
Edit `sources.txt`. One public RSS/Atom URL per line; lines beginning with `#` are comments. Start with official organizer feeds and add more sources over time. If a site has no RSS/API, add it in a future version only using a permitted method.

### 5. Enable Actions
- Push the files to the default branch (usually `main`).
- Open **Actions** and enable workflows if prompted.
- Run **Event Intelligence Agent** manually once using **Run workflow**.
- The workflow then runs every 15 minutes (`*/15 * * * *`, UTC), subject to GitHub scheduling delays and usage limits.

## Local test (Windows PowerShell)
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:TELEGRAM_BOT_TOKEN="your-token"
$env:TELEGRAM_CHAT_ID="your-chat-id"
python main.py
```

For a dry run without Telegram, set `DRY_RUN=true`.

## Event data and statuses
Each alert shows:
- **Link check**: reachable / unavailable / could not verify
- **Registration status**: OPEN / CLOSED / UNKNOWN, based only on explicit phrases in feed content
- **Deadline**: extracted date if clearly stated, otherwise `Not found`
- **Evidence**: the short feed text that triggered the status

Do not treat automated status detection as definitive. Always verify on the organizer's official page.
