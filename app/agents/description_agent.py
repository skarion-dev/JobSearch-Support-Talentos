"""
Extract a full job description from a posting URL.

Why not ScrapeGraphAI/Playwright for everything
-----------------------------------------------
The previous enricher drove a real Chromium instance per job. That caps
concurrency at ~8-10 (50 workers crashed the Node driver with EPIPE) and the
bottleneck was the BROWSER, not the model.

Most job pages are server-rendered: a plain HTTP GET returns the description in
the initial HTML. So: fetch with requests, strip to text, hand to
deepseek-v4-flash. No browser, so this scales as wide as the LLM allows
(100+ workers). Playwright stays available only for pages that genuinely need
JS, which is a small minority.
"""
import json
import re
import html as html_mod

import requests
from openai import OpenAI

from app.config import LLM_CONFIG

client = OpenAI(api_key=LLM_CONFIG["api_key"], base_url=LLM_CONFIG["base_url"])
MODEL = LLM_CONFIG["model"].removeprefix("openai/")

TIMEOUT = 20
MIN_USEFUL_TEXT = 400        # page text shorter than this is a shell/JS app
MIN_DESCRIPTION = 550        # Adzuna snippets cap ~500; below this is no gain
MAX_TEXT_TO_MODEL = 14000

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

_SCRIPT_STYLE = re.compile(r"<(script|style|noscript|svg)\b.*?</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")


def html_to_text(raw: str) -> str:
    text = _SCRIPT_STYLE.sub(" ", raw)
    text = _TAGS.sub(" ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


PROMPT = """From this job posting page text, extract:

1. "description": the FULL job description — responsibilities, requirements,
   and qualifications, verbatim as far as possible. Do NOT summarize or
   truncate. If the page is not actually a job posting (a search results page,
   a listing index, an error or login page), return null.

2. "posted_date": the date the job was posted, in YYYY-MM-DD, ONLY if the page
   states it explicitly ("Posted on ...", "Date posted ...", or an unambiguous
   relative date such as "Posted 3 days ago"). Never guess, never infer from
   context, never use today's date.

3. "posted_date_evidence": a short verbatim quote of the text proving the date,
   or null. If you cannot quote it, posted_date must be null.

PAGE TEXT:
{page_text}

Return JSON only: {{"description": <string|null>, "posted_date": <string|null>, "posted_date_evidence": <string|null>}}"""


def fetch_page_text(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return None
    if resp.status_code != 200 or not resp.text:
        return None
    text = html_to_text(resp.text)
    return text if len(text) >= MIN_USEFUL_TEXT else None


def extract(url: str) -> tuple[str | None, str | None]:
    """Returns (full_description, verified_posted_date). Either may be None."""
    page_text = fetch_page_text(url)
    if not page_text:
        return None, None

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": PROMPT.format(page_text=page_text[:MAX_TEXT_TO_MODEL])}],
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
    except Exception:
        return None, None

    desc = data.get("description")
    if not desc or len(desc) < MIN_DESCRIPTION:
        desc = None

    posted = data.get("posted_date")
    evidence = data.get("posted_date_evidence")
    if not posted or not evidence:
        posted = None
    elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(posted)):
        posted = None

    return desc, posted
