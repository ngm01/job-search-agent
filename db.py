"""
db.py — SQLite schema + connection helpers
"""

import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "pipeline.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                url             TEXT    NOT NULL UNIQUE,
                employer        TEXT,
                job_title       TEXT,
                jd_text         TEXT,
                source          TEXT,           -- 'greenhouse' | 'lever' | 'ashby' | 'unknown'
                discovered_at   TEXT    DEFAULT (datetime('now')),
                scraped_at      TEXT,
                scrape_status   TEXT    DEFAULT 'pending'  -- 'pending' | 'done' | 'failed'
            );

            CREATE TABLE IF NOT EXISTS scores (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id                      INTEGER NOT NULL REFERENCES jobs(id),
                composite_score             REAL,
                fit_label                   TEXT,
                pass_fail                   TEXT,
                recommended_action          TEXT,
                relevance_score             INTEGER,
                skills_coverage_pct         REAL,
                experience_score            INTEGER,
                experience_implied_level    TEXT,
                impact_score                INTEGER,
                impact_density_pct          REAL,
                communication_score         INTEGER,
                formatting_score            INTEGER,
                strengths                   TEXT,   -- JSON array
                weaknesses                  TEXT,   -- JSON array
                optimization_suggestions    TEXT,   -- JSON array
                raw_json                    TEXT,   -- full response blob
                scored_at                   TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS generated_docs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id          INTEGER NOT NULL REFERENCES jobs(id),
                resume_md       TEXT,
                resume_pdf_path TEXT,
                cover_letter_md TEXT,
                cover_letter_pdf_path TEXT,
                generated_at    TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS run_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at      TEXT    DEFAULT (datetime('now')),
                queries     TEXT,   -- JSON array of query strings
                jobs_found  INTEGER DEFAULT 0,
                jobs_new    INTEGER DEFAULT 0,
                jobs_passed INTEGER DEFAULT 0,
                jobs_failed INTEGER DEFAULT 0,
                notes       TEXT
            );
        """)
    # migration: applied column
    with get_conn() as conn:
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN applied INTEGER DEFAULT 0")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # already exists

    print(f"[db] Initialized at {DB_PATH}")


# ── Helpers ────────────────────────────────────────────────────────────────────

def insert_job_url(conn: sqlite3.Connection, url: str, source: str) -> bool:
    """Insert a job URL. Returns True if new, False if duplicate."""
    try:
        conn.execute(
            "INSERT INTO jobs (url, source) VALUES (?, ?)",
            (url, source)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # duplicate


def update_job_scraped(conn: sqlite3.Connection, job_id: int, employer: str,
                        job_title: str, jd_text: str):
    conn.execute("""
        UPDATE jobs
        SET employer=?, job_title=?, jd_text=?, scraped_at=datetime('now'), scrape_status='done'
        WHERE id=?
    """, (employer, job_title, jd_text, job_id))
    conn.commit()


def mark_scrape_failed(conn: sqlite3.Connection, job_id: int):
    conn.execute("""
        UPDATE jobs SET scrape_status='failed', scraped_at=datetime('now') WHERE id=?
    """, (job_id,))
    conn.commit()


def mark_location_filtered(conn: sqlite3.Connection, job_id: int):
    conn.execute(
        "UPDATE jobs SET scrape_status='location_filtered', scraped_at=datetime('now') WHERE id=?",
        (job_id,)
    )
    conn.commit()


def insert_score(conn: sqlite3.Connection, job_id: int, data: dict):
    """Store parsed score JSON into the scores table."""
    sm = data.get("skills_match", {})
    ea = data.get("experience_alignment", {})
    iq = data.get("impact_quantification", {})
    cc = data.get("communication_collaboration", {})
    fr = data.get("formatting_readability", {})
    rs = data.get("relevance_score", {})

    conn.execute("""
        INSERT INTO scores (
            job_id, composite_score, fit_label, pass_fail, recommended_action,
            relevance_score, skills_coverage_pct,
            experience_score, experience_implied_level,
            impact_score, impact_density_pct,
            communication_score, formatting_score,
            strengths, weaknesses, optimization_suggestions, raw_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        job_id,
        data.get("composite_score"),
        data.get("fit_label"),
        data.get("pass_fail"),
        data.get("recommended_action"),
        rs.get("score") if isinstance(rs, dict) else data.get("relevance_score"),
        sm.get("coverage_pct"),
        ea.get("score"),
        ea.get("implied_level"),
        iq.get("score"),
        iq.get("impact_density_pct"),
        cc.get("score"),
        fr.get("score"),
        json.dumps(data.get("strengths", [])),
        json.dumps(data.get("weaknesses", [])),
        json.dumps(data.get("optimization_suggestions", [])),
        json.dumps(data),
    ))
    conn.commit()


def delete_score(conn: sqlite3.Connection, job_id: int):
    conn.execute("DELETE FROM scores WHERE job_id=?", (job_id,))
    conn.commit()


def delete_job(conn: sqlite3.Connection, job_id: int):
    """Delete a job and all associated scores/doc records. Call purge_job_docs first to remove files."""
    conn.execute("DELETE FROM generated_docs WHERE job_id=?", (job_id,))
    conn.execute("DELETE FROM scores WHERE job_id=?", (job_id,))
    conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    conn.commit()


def mark_applied(conn: sqlite3.Connection, job_id: int, applied: bool):
    conn.execute("UPDATE jobs SET applied=? WHERE id=?", (int(applied), job_id))
    conn.commit()


def insert_generated_docs(conn: sqlite3.Connection, job_id: int,
                           resume_md: str, resume_pdf: str,
                           cover_md: str, cover_pdf: str):
    conn.execute("""
        INSERT INTO generated_docs
            (job_id, resume_md, resume_pdf_path, cover_letter_md, cover_letter_pdf_path)
        VALUES (?,?,?,?,?)
    """, (job_id, resume_md, resume_pdf, cover_md, cover_pdf))
    conn.commit()


def get_all_results(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Fetch everything needed for the results table in the GUI."""
    return conn.execute("""
        SELECT
            j.id, j.url, j.employer, j.job_title, j.source, j.discovered_at, j.applied,
            s.composite_score, s.fit_label, s.pass_fail, s.recommended_action,
            s.relevance_score, s.skills_coverage_pct,
            s.experience_score, s.experience_implied_level,
            s.impact_score, s.communication_score, s.formatting_score,
            s.strengths, s.weaknesses, s.optimization_suggestions, s.raw_json,
            d.resume_md, d.resume_pdf_path, d.cover_letter_md, d.cover_letter_pdf_path
        FROM jobs j
        LEFT JOIN scores s ON s.job_id = j.id
        LEFT JOIN generated_docs d ON d.job_id = j.id
        WHERE j.scrape_status != 'location_filtered'
        ORDER BY s.composite_score DESC NULLS LAST
    """).fetchall()


def get_passed_urls(conn: sqlite3.Connection) -> set[str]:
    """Return URLs that already have a PASS score — skip re-inserting these."""
    rows = conn.execute("""
        SELECT j.url FROM jobs j
        JOIN scores s ON s.job_id = j.id
        WHERE s.pass_fail = 'PASS'
    """).fetchall()
    return {row["url"] for row in rows}


def get_pending_scrape(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, url, source FROM jobs WHERE scrape_status='pending'"
    ).fetchall()


def get_pending_score(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT j.id, j.url, j.employer, j.job_title, j.jd_text
        FROM jobs j
        LEFT JOIN scores s ON s.job_id = j.id
        WHERE j.scrape_status='done' AND s.id IS NULL
    """).fetchall()


def get_pending_generation(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT j.id, j.url, j.employer, j.job_title, j.jd_text, s.raw_json
        FROM jobs j
        JOIN scores s ON s.job_id = j.id
        LEFT JOIN generated_docs d ON d.job_id = j.id
        WHERE s.pass_fail='PASS' AND d.id IS NULL
    """).fetchall()
