"""
search.py — Run DuckDuckGo queries, extract job board URLs, dedupe against DB.
"""

import re
import time
import logging
from urllib.parse import urlparse

from ddgs import DDGS

import db

log = logging.getLogger(__name__)

# Patterns that identify supported job boards
BOARD_PATTERNS = {
    "greenhouse": re.compile(
        r"https?://(boards\.greenhouse\.io|job-boards\.greenhouse\.io)/\S+"
    ),
    "lever": re.compile(
        r"https?://jobs\.lever\.co/\S+"
    ),
    "ashby": re.compile(
        r"https?://jobs\.ashbyhq\.com/\S+"
    ),
}


def detect_source(url: str) -> str:
    for source, pattern in BOARD_PATTERNS.items():
        if pattern.match(url):
            return source
    return "unknown"


def is_job_url(url: str) -> bool:
    """Filter out search-result noise — company pages, blog posts, etc."""
    parsed = urlparse(url)

    # Must be a known board domain
    known_domains = {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "jobs.lever.co",
        "jobs.ashbyhq.com",
    }
    if parsed.netloc not in known_domains:
        return False

    # Greenhouse: /company/job-slug — needs at least 2 path segments
    # Lever: /company/uuid
    # Ashby: /company/job-slug
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return False

    return True


def clean_url(url: str) -> str:
    """Strip tracking params and fragments."""
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl()


def run_queries(queries: list[str], max_results: int = 10,
                delay: float = 1.5) -> dict[str, int]:
    """
    Execute each query, store new URLs in the DB.
    Returns stats: {found, new, skipped, unknown_board}
    """
    stats = {"found": 0, "new": 0, "skipped": 0, "skipped_pass": 0, "unknown_board": 0}

    with db.get_conn() as conn:
        passed_urls = db.get_passed_urls(conn)
        log.info(f"[search] {len(passed_urls)} URL(s) already passed — will skip")

        with DDGS() as ddgs:
            for query in queries:
                log.info(f"[search] Query: {query!r}")
                try:
                    results = ddgs.text(query, max_results=max_results)
                except Exception as e:
                    log.warning(f"[search] DDG error on {query!r}: {e}")
                    continue

                for r in results:
                    url = clean_url(r.get("href", ""))
                    if not url:
                        continue

                    stats["found"] += 1

                    if not is_job_url(url):
                        stats["unknown_board"] += 1
                        log.debug(f"[search] Skipping non-job URL: {url}")
                        continue

                    if url in passed_urls:
                        stats["skipped_pass"] += 1
                        log.debug(f"[search] Already passed, skipping: {url}")
                        continue

                    source = detect_source(url)
                    is_new = db.insert_job_url(conn, url, source)

                    if is_new:
                        stats["new"] += 1
                        log.info(f"[search] New job ({source}): {url}")
                    else:
                        stats["skipped"] += 1
                        log.debug(f"[search] Duplicate: {url}")

                time.sleep(delay)  # be polite to DDG

    log.info(f"[search] Done — {stats}")
    return stats
