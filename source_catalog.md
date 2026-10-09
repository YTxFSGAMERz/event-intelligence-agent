# Event Intelligence Agent — Phase 1 source catalog

## Configured discovery feeds
These RSS feeds discover candidate announcements. Google News is an index, not the event organizer; use each result's original official page as the source of truth.

- Devpost hackathons: https://news.google.com/rss/search?q=site%3Adevpost.com%2Fhackathons+hackathon+registration+prize&hl=en-IN&gl=IN&ceid=IN%3Aen
- Major League Hacking: https://news.google.com/rss/search?q=site%3Amlh.io+hackathon+student+registration&hl=en-IN&gl=IN&ceid=IN%3Aen
- Funded travel and student programs: https://news.google.com/rss/search?q=%22travel+grant%22+OR+%22fully+funded%22+student+technology&hl=en-IN&gl=IN&ceid=IN%3Aen
- Indian government scholarships: https://news.google.com/rss/search?q=site%3Ascholarships.gov.in+scholarship+deadline&hl=en-IN&gl=IN&ceid=IN%3Aen
- GitHub Changelog RSS: https://github.blog/changelog/feed/

## Official pages to verify opportunities
- Devpost open hackathons: https://devpost.com/hackathons?status=open
- Major League Hacking: https://mlh.io/
- MLH Global Hack Week: https://ghw.mlh.com/
- National Scholarship Portal — all schemes: https://scholarships.gov.in/All-Scholarships
- National Scholarship Portal — student information: https://scholarships.gov.in/Students

## Verification and safe automation rules
1. Google News results are discovery leads only; verify dates, eligibility, fees, prizes, travel support, and availability on the organizer's official page.
2. A reachable URL does not mean registration is open or a ticket remains available.
3. Status is UNKNOWN unless explicit evidence is found. Feed-text heuristics can be stale or misleading.
4. Respect source terms, robots rules, and rate limits. Do not bypass logins, CAPTCHAs, or access controls.
5. Keep Telegram tokens only in GitHub Actions secrets, never in repository files.
