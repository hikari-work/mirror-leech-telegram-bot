"""Generic /bypass dispatcher.

Routes a URL to the right thread scraper by domain. Currently semprot.com / senang.top is
registered; add another `if domain` branch to support more sites.
"""

from urllib.parse import urlparse

from ...ext_utils.exceptions import DirectDownloadLinkException
from .semprot_scraper import scrape_pages


async def bypass_scrape(link, page_list="1", filter_host=""):
    """Return (title, links, total_pages). Delegates pageList & filter to the backend."""
    domain = urlparse(link).hostname or ""
    if any(h in domain for h in ("semprot.com", "senang.top")):
        return await scrape_pages(link, page_list, filter_host)
    raise DirectDownloadLinkException(f"ERROR: No bypass scraper for {domain}")
