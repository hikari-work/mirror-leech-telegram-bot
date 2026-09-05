"""Generic /bypass dispatcher.

Routes a URL to the right thread scraper by domain. Currently semprot.com /
senang.top is registered; add another domain to `_SCRAPER_DOMAINS` plus a branch
to support more sites.

Shortlink resolvers live in url_shortener_bypass.py, not here: they answer with
a single target URL rather than a page of links, so /bypass resolves them first
and only lands here when the target is a thread worth scraping.
"""

from urllib.parse import urlparse

from ..util.exceptions import DirectDownloadLinkException
from .semprot_scraper import scrape_pages

_SCRAPER_DOMAINS = ("semprot.com", "senang.top")


def is_scrape_target(link):
    """True when a thread scraper is registered for this URL's host."""
    domain = urlparse(link).hostname or ""
    return any(h in domain for h in _SCRAPER_DOMAINS)


async def bypass_scrape(link, page_list="1", filter_host=""):
    """Return (title, links, total_pages).

    Delegates pageList & filter to the backend.
    """
    domain = urlparse(link).hostname or ""
    if any(h in domain for h in _SCRAPER_DOMAINS):
        return await scrape_pages(link, page_list, filter_host)
    raise DirectDownloadLinkException(f"ERROR: No bypass scraper for {domain}")
