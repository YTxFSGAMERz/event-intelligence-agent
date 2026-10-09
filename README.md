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

## Source adapter registry (Phase 2)

Configured URLs in `sources.txt` are routed through the adapter registry in `source_adapters.py`. The registry currently supports:

- **Google News RSS:** feed fetching plus best-effort resolution of article redirects or canonical URL tags. If resolution fails, the original discovery URL is retained and canonical URL remains unknown.
- **Hackalendar RSS:** a human-curated upcoming-hackathon discovery feed; the feed's event page is not automatically treated as the organizer's official page.
- **GitHub Blog RSS:** recognized as a named official-blog feed.
- **Generic RSS/Atom:** shared fetching and parsing for other configured public feeds.

Each configured URL receives a stable source ID and a source-health record in `data/seen_events.json`. Health includes the access method, expected feed fields, pagination model, request timeout, minimum polling interval, rate-limit policy, last attempt and successful fetch, status, duration, entry count, matching count, queued count, and failure streak. A failed run retains the last successful timestamp and last successful counts. GitHub Actions merges these health records alongside the deduplication records when persisting state.

The agent enforces a **15-minute minimum polling interval per feed**, including manual and code-push runs. Sources skipped by this guard are marked `skipped_minimum_interval`; skipping does not overwrite their last real attempt or last successful-fetch time. RSS/Atom feeds are publisher-managed, so the current adapters do not implement client-side page-number pagination.

### URL provenance and duplicate handling

- `discovered_url` stores the URL found in the feed.
- `canonical_url` stores a normalized resolved publisher-page URL when one is available. It is **not** automatically considered the organizer's official page.
- `verification.official_url` remains unknown until a separate official-source verification step is implemented.
- Resolved URLs are used for cross-feed deduplication where possible. Specific exact-title matches are also merged when the title is sufficiently descriptive and the existing record was first seen within the last 90 days; short/generic titles and older records are not deduplicated by title alone.
- When sources converge on one record, source IDs, source labels, feed URLs, discovery URLs and resolved URLs are retained in deduplicated alias lists. Existing legacy records remain compatible and are not re-alerted just because their ID predates URL resolution.
- If a Google News redirect cannot be resolved, the system keeps the discovery link and records the resolution status instead of fabricating a canonical URL.

Google News URL decoding is bounded to **four attempts per workflow run**, with at most **one legacy-record backfill** so historical records cannot use the entire budget before new discoveries. If the budget is exhausted, the event is still retained with its discovery URL and a `resolution_budget_deferred` status; later runs can retry it. Ordinary direct RSS links do not consume this resolution budget.

The scanner continues through the feed after reaching its per-source alert cap, so matching counts and last-seen timestamps cover the full feed while new alerts remain limited. The `new_items` source-health field represents items queued for alerting, not a guarantee Telegram delivery succeeded.

The current registry uses public RSS/Atom feeds and ordinary redirect/canonical-page checks. It does not bypass access controls or anti-bot mechanisms.

## Official-page verification (Phase 3)

The verification module is `page_verification.py`. It fetches public HTML pages with a maximum 512 KB per page, a short timeout, a four-hop redirect ceiling, per-redirect URL checks, and no more than four page fetches across two records per workflow run. Google News wrapper URLs are not treated as event pages while they remain unresolved; the system waits for a publisher URL instead.

### What it extracts

- Schema.org Event JSON-LD: event name, start/end dates, explicit timezone offsets, organizer, online/in-person/hybrid mode, structured location and event status. Field meanings follow [Schema.org Event](https://schema.org/Event) and [Google's event structured-data guidance](https://developers.google.com/search/docs/appearance/structured-data/event).
- Explicit application/registration deadline labels and deadline metadata. A normalized deadline is saved as verified only when the page passes the official-page checks and the date includes an explicit year. A date without a year remains raw/unknown; the system does not infer a year or timezone.
- Explicit eligibility text and travel-support wording for flights, transport, accommodation, meals and visa support. Component-level outcomes are recorded as `confirmed`, `possible`, `not_offered` or `unknown`, with evidence snippets and source URLs.
- Explicit registration phrases such as “applications are open” or “registration closed”.

### Verification states and trust boundary

- `official_page_verified`: the page is identified by an explicit organizer/official-site link or a matching structured Event that points to an organizer URL on the same site. Extracted facts are merged into the canonical fields with evidence.
- `structured_event_source_page_checked` and `source_page_checked`: the discovery page was readable, but the official organizer page was not established. Extracted content stays under `verification.observed_facts`; it does not silently become verified deadline/location/funding data.
- `official_link_candidate_found` / `official_link_candidate_unverified`: an outbound CTA was found but the destination did not satisfy the official-page check.
- `awaiting_source_resolution`: the only URL is an unresolved Google News wrapper; no page fetch is spent on the wrapper.
- HTTP, redirect and parse failures are recorded separately. Successful checks are scheduled for recheck after seven days; fetch failures can retry after a day. A verification-version change schedules older records for a bounded recheck.

The page parser does not log in, solve CAPTCHAs, bypass anti-bot checks, submit applications or infer official status from reachability alone. Unsupported facts remain unknown. In a live test, Hackalendar's structured listing was parsed and its “Enter on the organiser's site” link was identified. The linked Devpost page returned an HTTP error to the automation runner, so the link is retained as an unverified candidate and the listing's dates/location remain observations rather than verified organizer facts. Candidate fetch status and HTTP error details are stored to guide later rechecks.

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

## Discovery quality and source coverage (Phase 5)

The agent now applies an explainable quality gate before spending a Google News URL-resolution request. It rejects obvious opportunity roundup/listicle headlines, non-actionable results/rules stories, and items whose opportunity keywords only appear in generic supporting text. Named opportunities and concrete application, registration, and deadline updates are retained. The decision and reason are stored as `discovery_quality`, and the latest per-feed `quality_rejected_count` is visible in source-health data. This is a transparent rule-based triage system, not an AI accuracy score.

The matcher no longer treats generic words such as standalone `winner`, `reward`, `flight`, `laptop`, or `early bird` as sufficient evidence for prize, travel, gadget, or free-ticket categories. The resolver budget is six URL attempts per workflow run, reserving up to two for legacy Google News records while allowing four for newly found records; individual legacy records can receive up to five bounded attempts before the agent stops retrying them.

Two targeted discovery searches were added: one restricted to National Scholarship Portal pages and one to Google's Student Programs announcements. They are still **Google News discovery feeds**, not direct official feeds, so discovered URLs must pass normal resolution and verification. The National Scholarship Portal currently lists AY 2026–27 scholarship application timelines, and Google describes its Student Programs page as covering scholarships, internships, events and programs. See [National Scholarship Portal](https://scholarships.gov.in/Students) and [Google Student Programs](https://blog.google/company-news/outreach-and-initiatives/student-programs/).

## Web dashboard (Phase 4)

The live dashboard is available at https://event-intelligence-dashboard-indol.vercel.app and is served from `dashboard/index.html`. It reads the public tracker at `data/seen_events.json` and refreshes automatically every five minutes.

### Dashboard features

- **Trust-aware verification:** records are labelled as officially verified, official-link candidates needing review, checked discovery pages, waiting for source resolution, or not checked. A record counts as officially verified only when the verifier reports `official_page_verified` and provides an HTTP(S) `official_url`.
- **Opportunity cards:** source images, discovery summaries, event schedule signals, registration status, location, reward mentions, and an expandable view of eligibility, travel-support components, verification notes, and evidence links.
- **Search and filters:** categories, registration status, verification state, event format, country, travel-support state, eligibility detail availability, and sorting by verified deadline, event date, or title.
- **Multi-category browsing:** each event is searchable from every category assigned by the tracker, and category counts reflect all assigned labels rather than just the primary category.
- **Noise triage:** an explicitly estimated headline shape separates likely specific opportunities from roundup/listicle posts, news updates, and unclear titles. This heuristic never represents official verification.
- **Versioned discovery-quality gate:** the agent re-evaluates saved records when its rules change, preserves officially verified records, and marks weak records rather than deleting them. The dashboard shows accepted records by default while allowing flagged, unassessed, or all records to be inspected; exports include the quality decision and reason.
- **Information coverage:** cards show how many of seven key detail groups are populated, with a sort option for records that contain more details. Coverage indicates field presence, not accuracy.
- **Paged results and exports:** results are paginated in groups of 12. CSV and JSON exports include the current filtered result set, provenance and trust labels; CSV fields beginning with spreadsheet formula characters are neutralized.
- **Source health:** per-feed latest known status, last attempt and last success, adapter type, entries seen, matches, queued items, failure streak, and reported error.
- **Saved opportunities:** saved cards are kept in the visitor's local browser storage; they are not uploaded to the agent or shared between devices.

Unverified discovery-page facts are explicitly labelled as unverified. A page being readable, an outbound organizer-link candidate being found, or a source image being present does not establish official ownership. Missing deadline, location, eligibility, reward, or travel-support data remain unknown rather than being guessed. The dashboard is a research aid, not an application or booking service.

The dashboard test checks its live-feed binding, trust-state controls, evidence rendering, filtering controls, and source-health panel: `python -m unittest discover -s tests -v`.

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
