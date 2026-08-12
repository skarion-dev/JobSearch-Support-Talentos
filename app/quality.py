"""
One definition of "is this job usable", shared by every stage.

WHY THIS EXISTS
---------------
The 1500-character floor was duplicated in push_to_talentos, unlog_from_talentos,
enrich_talentos_descriptions and export_unpursued. Four copies of a threshold
that decides whether a real person gets a real application is three too many —
and the review UI didn't apply it at all, so an operator could tick 40 rows and
have 40 of them silently vanish at push time.

WHY 1500
--------
Measured, not guessed. At the old floor of 120 we pushed 186 jobs carrying
~457-char Adzuna snippets. The resume generator cannot tailor from those and an
AE cannot judge them. Real postings from Apify average 5,473 characters; 1500 is
comfortably below a genuine posting and well above a truncated teaser.

THE SOURCE THAT MATTERS
-----------------------
Measured over the live corpus (28,325 jobs):

    adzuna           25,938 jobs     224 usable    (1%)   avg   535 chars
    apify:linkedin    2,150 jobs   2,093 usable   (97%)   avg 5,473 chars
    apify:indeed        227 jobs     225 usable   (99%)   avg 5,622 chars

Adzuna is 92% of the corpus and 9% of what we can actually act on, because it
truncates at ~500 characters and gates its own links behind a login. It is a
discovery signal, not a source of applications.
"""

MIN_DESCRIPTION = 1500

# Links that can never be recovered: Adzuna's own redirect needs a login, and
# the link finder occasionally resolves to social/video junk.
DEAD_DOMAINS = ("adzuna.com", "youtube.com", "facebook.com", "twitter.com", "x.com")

# Sources that return the full posting text. Anything else needs enrichment
# before it can be pushed.
FULL_TEXT_SOURCES = ("apify:linkedin", "apify:indeed", "apify:google")


def desc_len(job) -> int:
    """Length of whatever description we hold. Accepts a dict or sqlite3.Row."""
    for key in ("description", "description_text", "raw_description"):
        try:
            v = job[key]
        except (KeyError, IndexError, TypeError):
            continue
        if v:
            return len(v)
    return 0


def is_pushable(job) -> bool:
    """True when the resume generator has enough text to tailor from."""
    return desc_len(job) >= MIN_DESCRIPTION


def best_link(job) -> str | None:
    for key in ("source_url", "apply_url", "job_url"):
        try:
            v = job[key]
        except (KeyError, IndexError, TypeError):
            continue
        if v:
            return v
    return None


def is_dead_link(url: str | None) -> bool:
    return bool(url) and any(d in url.lower() for d in DEAD_DOMAINS)


def blocked_reason(job) -> str | None:
    """
    Why this job cannot be pushed, in words an operator can act on.
    None means it is pushable.
    """
    if is_pushable(job):
        return None
    url = best_link(job)
    if not url:
        return "No link found"
    if is_dead_link(url):
        return "Link is gated/junk - needs manual search"
    return "Description too short, page not scrapeable"


# SQL fragment for the local SQLite corpus, so filtering happens in the query
# rather than in Python over thousands of rows.
PUSHABLE_SQL = f"length(coalesce(j.description,'')) >= {MIN_DESCRIPTION}"
THIN_SQL = f"length(coalesce(j.description,'')) <  {MIN_DESCRIPTION}"
