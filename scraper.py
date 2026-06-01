"""
scraper.py — Playwright-based JD scraper for Greenhouse, Lever, and Ashby.
"""

import logging
import re
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

import db

log = logging.getLogger(__name__)

# ── Per-board selectors ────────────────────────────────────────────────────────
# Each entry: (title_selector, body_selector)
# We try these in order and take the first that yields non-empty text.

SELECTORS = {
    "greenhouse": [
        ("#header h1", "#content"),
        (".app-title", ".job-post"),
        ("h1", "div.section-wrapper"),
    ],
    "lever": [
        (".posting-headline h2", ".posting-description"),
        ("h2", ".section-wrapper"),
    ],
    "ashby": [
        ("h1", "div.ashby-job-posting-brief-description"),
        ("h1", "div[class*='JobPostingLayout']"),
        ("h1", "main"),
    ],
    "unknown": [
        ("h1", "main"),
        ("h1", "article"),
        ("h1", "body"),
    ],
}


def _try_selectors(page: Page, source: str) -> tuple[str, str]:
    """Return (title, body_text) using board-specific selectors."""
    candidates = SELECTORS.get(source, SELECTORS["unknown"])

    for title_sel, body_sel in candidates:
        try:
            title_el = page.query_selector(title_sel)
            body_el = page.query_selector(body_sel)
            if title_el and body_el:
                title = (title_el.inner_text() or "").strip()
                body = (body_el.inner_text() or "").strip()
                if title and len(body) > 200:
                    return title, body
        except Exception:
            continue

    # Last-resort: grab all visible text
    title_el = page.query_selector("h1")
    title = (title_el.inner_text() if title_el else "").strip()
    body = page.evaluate("() => document.body.innerText")
    return title, (body or "").strip()


def _infer_employer(url: str, title: str) -> str:
    """Best-effort employer name from URL slug."""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if parts:
        slug = parts[0]
        # Convert kebab-case to Title Case
        return re.sub(r"[-_]", " ", slug).title()
    return "Unknown Employer"


def scrape_jobs():
    """Scrape all pending jobs and update the DB."""
    with db.get_conn() as conn:
        pending = db.get_pending_scrape(conn)

    if not pending:
        log.info("[scraper] No pending jobs to scrape.")
        return

    log.info(f"[scraper] Scraping {len(pending)} jobs...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )

        for row in pending:
            job_id = row["id"]
            url = row["url"]
            source = row["source"]

            log.info(f"[scraper] [{job_id}] {url}")

            try:
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=20_000)

                # Some boards lazy-load — wait a beat
                page.wait_for_timeout(1500)

                title, body = _try_selectors(page, source)
                page.close()

                if not body or len(body) < 100:
                    raise ValueError(f"Body too short ({len(body)} chars)")

                employer = _infer_employer(url, title)

                with db.get_conn() as conn:
                    db.update_job_scraped(conn, job_id, employer, title, body)

                log.info(f"[scraper] [{job_id}] ✓ {employer} — {title}")

            except PWTimeout:
                log.warning(f"[scraper] [{job_id}] Timeout: {url}")
                with db.get_conn() as conn:
                    db.mark_scrape_failed(conn, job_id)

            except Exception as e:
                log.warning(f"[scraper] [{job_id}] Error: {e}")
                with db.get_conn() as conn:
                    db.mark_scrape_failed(conn, job_id)

        browser.close()

    log.info("[scraper] Done.")
