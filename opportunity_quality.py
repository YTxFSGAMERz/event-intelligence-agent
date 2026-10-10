"""Conservative headline-quality gate for opportunity discovery.

This module deliberately uses transparent rules rather than claiming ML confidence.
It rejects obvious roundups and non-actionable news while preserving named programs,
deadline changes, and application/registration announcements.
"""
from __future__ import annotations

import re
from typing import Iterable


RULE_VERSION = 5

ROUNDUP_PATTERNS = (
    re.compile(
        r"^\s*(?:top|best|ultimate|complete|definitive)\s+"
        r"(?:(?:\d+|[a-z]+)\s+)?(?:fully funded\s+)?"
        r"(?:scholarships?|fellowships?|internships?|grants?|opportunities|"
        r"programs?|competitions?|hackathons?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b\d+\s+(?:(?:best|top|fully funded)\s+)?"
        r"(?:scholarships|fellowships|internships|grants|opportunities|"
        r"programs|competitions|hackathons)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b\d+\s+(?:scholarship|fellowship|internship|grant)\s+"
        r"(?:opportunities|programs|options|schemes)\b",
        re.IGNORECASE,
    ),
    # Headlines often insert a broad subject (for example "tech") between
    # a ranking word and the opportunity type: "10 best tech internships".
    re.compile(
        r"\b(?:top|best)\s+\d+\s+.{0,45}\b"
        r"(?:scholarships?|fellowships?|internships?|grants?|opportunities|"
        r"programs?|competitions?|hackathons?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b\d+\s+(?:best|top)\s+.{0,45}\b"
        r"(?:scholarships?|fellowships?|internships?|grants?|opportunities|"
        r"programs?|competitions?|hackathons?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:government and private schemes|multiple scholarships|various scholarships|"
        r"different scholarships|scholarships you should know|opportunities you should know)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:complete guide|ultimate guide|round[- ]?up|list of scholarships|"
        r"best opportunities|top opportunities|all you need to know)\b",
        re.IGNORECASE,
    ),
)

NON_ACTIONABLE_NEWS = re.compile(
    r"\b(?:new rules|rules explained|results announced|winners? announced|"
    r"what is|explained|how to check|check your status|everything you need to know)\b",
    re.IGNORECASE,
)
ACTIONABLE_UPDATE = re.compile(
    r"\b(?:deadline extended|deadline postponed|application deadline|registration deadline|"
    r"last date(?: to apply)?|applications? (?:are )?(?:now )?open|"
    r"applications? (?:till|until)\b|opens? applications?|apply now|register now|"
    r"registration (?:is )?open|call for applications?|call for proposals?|"
    r"submissions? (?:are )?open|apply by|applications? close|registration closes?)\b",
    re.IGNORECASE,
)
TITLE_OPPORTUNITY_SIGNAL = re.compile(
    r"\b(?:hackathons?|buildathons?|coding challenges?|competitions?|contests?|"
    r"olympiads?|scholarships?|fellowships?|bursaries|stipends?|internships?|"
    r"apprenticeships?|travel grants?|travel scholarships?|student ambassador|"
    r"summer of code|campus experts?|conference|summit|bootcamp|grant|"
    r"call for entries|fully funded|funded travel|exchange program|exchange programme)\b",
    re.IGNORECASE,
)

def assess_discovery_quality(
    title: str,
    summary: str = "",
    categories: Iterable[str] = (),
    adapter_type: str = "",
) -> dict:
    """Return an explainable accept/reject decision for a feed item.

    The caller should first run its opportunity-keyword matcher. This function then
    reduces low-value listicles, evergreen guides and results-only announcements.
    A curated event catalogue may omit event-type words from its title, so that
    source gets a narrow allowance when the category matcher has already matched.
    """
    clean_title = re.sub(r"\s+", " ", str(title or "")).strip()
    clean_summary = re.sub(r"\s+", " ", str(summary or "")).strip()
    category_list = list(dict.fromkeys(
        str(category) for category in categories if str(category).strip()
    ))
    normalized = clean_title.casefold()
    actionable = bool(ACTIONABLE_UPDATE.search(clean_title))
    title_signal = bool(TITLE_OPPORTUNITY_SIGNAL.search(clean_title))

    def result(accepted: bool, status: str, reason: str) -> dict:
        return {
            "accepted": accepted,
            "status": status,
            "reason": reason,
            "rule_version": RULE_VERSION,
            "title_signal": title_signal,
            "actionable_update": actionable,
            "matched_categories": category_list,
        }

    if not clean_title:
        return result(False, "missing_title", "Feed item has no usable title.")
    if not category_list:
        return result(False, "no_opportunity_category", "No opportunity category matched.")

    # A multi-destination guide is normally a round-up, not one distinct opportunity.
    if (
        "to study in" in normalized
        and len(re.findall(r",", clean_title)) >= 2
        and re.search(r"\b(?:scholarships?|fellowships?|grants?)\b", normalized)
    ):
        return result(
            False, "roundup_or_listicle",
            "Headline aggregates destinations rather than identifying one application.",
        )
    if any(pattern.search(clean_title) for pattern in ROUNDUP_PATTERNS):
        return result(
            False, "roundup_or_listicle",
            "Headline looks like a listicle or general opportunity guide.",
        )

    if NON_ACTIONABLE_NEWS.search(clean_title) and not actionable:
        return result(
            False, "non_actionable_news",
            "Headline appears to describe results, rules, or general information rather than an open opportunity.",
        )

    # Do not treat broad words from an article body as sufficient when the headline
    # itself says nothing about a program, application, event or award.
    if not title_signal and not actionable and adapter_type not in {"hackalendar_rss", "mlh_events_html", "devfolio_html", "nsp_scholarships_html", "unstop_html"}:
        return result(
            False, "low_specificity",
            "Opportunity keywords only appear in supporting text; headline lacks an opportunity signal.",
        )

    # Keep date changes and application notices, even when their title reads like news.
    if actionable:
        return result(
            True, "actionable_update",
            "Headline contains a concrete application, registration, or deadline action.",
        )

    if title_signal or adapter_type in {"hackalendar_rss", "mlh_events_html", "devfolio_html", "nsp_scholarships_html", "unstop_html"}:
        return result(
            True, "specific_opportunity",
            "Headline identifies an opportunity or a curated event listing.",
        )

    return result(
        False, "low_specificity",
        "The feed item does not identify a specific opportunity.",
    )
