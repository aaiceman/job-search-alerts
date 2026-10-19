"""We Work Remotely category RSS feeds (remote-only). Titles are 'Company: Job Title'."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from ..models import Job
from .base import SourceContext, note_skip, require, run_queries, text
from .http import get, html_to_text

NAME = "wwr"
FEED = "https://weworkremotely.com/categories/{}.rss"


def _posted(pub: str | None) -> str:
    try:
        return parsedate_to_datetime(pub).date().isoformat() if pub else ""
    except (TypeError, ValueError, IndexError):
        return ""


def _job(item: ET.Element) -> Job:
    raw = text(item.findtext("title"))
    company, sep, title = raw.partition(": ")
    if not sep:
        company, title = "", raw
    url, title = (item.findtext("link") or "").strip(), title.strip()
    require(title, url)
    return Job(
        source=NAME, url=url, title=title, company=company.strip(),
        location=text(item.findtext("region")).strip(), remote=True,
        employment_type=text(item.findtext("type")).strip(),
        posted=_posted(item.findtext("pubDate")),
        description=html_to_text(item.findtext("description")),
    )


def parse(xml_text, skipped: list | None = None) -> list[Job]:
    if not isinstance(xml_text, str) or not xml_text.strip():
        return []
    jobs = []
    for item in ET.fromstring(xml_text).iter("item"):  # malformed XML raises: counted as a failed feed
        try:
            jobs.append(_job(item))
        except Exception as e:
            note_skip(skipped, item, e)
    return jobs


def fetch(ctx: SourceContext) -> list[Job]:
    return run_queries(ctx, NAME, ctx.cfg.get("wwr_feeds", []),
                       lambda feed, skipped: parse(get(FEED.format(feed)).text, skipped))
