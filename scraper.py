"""
scraper.py — Playwright-based JD scraper for Greenhouse, Lever, and Ashby.
"""

import logging
import re
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

import db

# ── Location filter ────────────────────────────────────────────────────────────
# Explicit US indicators — if present the role is always kept.
_US_RE = re.compile(
    r'\b(?:'
    r'united states|u\.s\.a\.?'
    # State names (unambiguous)
    r'|california|new york|texas|florida|illinois|washington\s+state'
    r'|massachusetts|georgia|colorado|virginia|oregon|north carolina'
    r'|pennsylvania|ohio|michigan|arizona|tennessee|minnesota|maryland'
    r'|new jersey|nevada|utah|indiana|wisconsin|missouri|connecticut'
    # Distinctive US cities
    r'|san francisco|los angeles|new york city|nyc|chicago|seattle|boston'
    r'|austin|denver|atlanta|miami|portland|san jose|dallas|houston|phoenix'
    r'|silicon valley|bay area|nashville|philadelphia|san diego|minneapolis'
    r')\b'
    r'|remote\s*[-–(]\s*(?:u\.?s\.?|united states|north america)\b'
    r'|\bauthorized to work in the (?:u\.?s\.?|united states)\b',
    re.IGNORECASE,
)

# Non-US indicators — if present with no US indicator, the role is filtered.
_NON_US_RE = re.compile(
    r'\b(?:'
    # Countries
    r'united kingdom|u\.k\.|england|scotland|wales|northern ireland'
    r'|germany|france|netherlands|sweden|norway|denmark|spain|italy'
    r'|switzerland|austria|belgium|ireland|finland|portugal|poland'
    r'|czech republic|romania|greece|hungary|ukraine'
    r'|european union|europe only|eu only|emea only'
    r'|canada|australia|new zealand'
    r'|india|singapore|japan|south korea|hong kong|china|taiwan'
    r'|brazil|argentina|mexico|colombia|chile'
    r'|israel|uae|united arab emirates|south africa'
    # UK cities (uncommon as US place names in tech JDs)
    r'|london|manchester|birmingham|edinburgh|glasgow|leeds|liverpool|bristol'
    # Major EU cities
    r'|berlin|munich|münchen|hamburg|frankfurt|paris|amsterdam|stockholm'
    r'|oslo|copenhagen|madrid|barcelona|rome|milan|zurich|zürich|vienna|wien'
    r'|brussels|helsinki|lisbon|warsaw|prague'
    # Canada cities
    r'|toronto|vancouver|montreal|calgary|ottawa'
    # Australia cities
    r'|sydney|melbourne|brisbane|canberra|adelaide'
    # India cities
    r'|bangalore|bengaluru|mumbai|chennai|kolkata|hyderabad'
    r'|pune|gurugram|noida'
    # Asia cities
    r'|tokyo|osaka|seoul|beijing|shanghai|shenzhen|taipei'
    # LATAM cities
    r'|são paulo|buenos aires|bogotá'
    r'|tel aviv|dubai|abu dhabi'
    r')\b'
    r'|remote\s*[-–(,]\s*(?:uk|europe|eu|canada|australia|emea|apac|latam)\b',
    re.IGNORECASE,
)


def _is_us_eligible(text: str) -> bool:
    """Return False only when the role is clearly non-US with no US presence."""
    if _US_RE.search(text):
        return True           # explicit US signal always wins
    if _NON_US_RE.search(text):
        return False          # non-US with no offsetting US signal
    return True               # no location info → don't over-filter

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

                if not _is_us_eligible(body):
                    log.info(f"[scraper] [{job_id}] ✗ Not US-eligible: {employer} — {title}")
                    with db.get_conn() as conn:
                        db.mark_location_filtered(conn, job_id)
                    continue

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
