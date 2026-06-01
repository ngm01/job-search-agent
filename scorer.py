"""
scorer.py — Call Claude to score each scraped JD against the resume.
"""

import json
import logging
import os
import re

import anthropic

import db

log = logging.getLogger(__name__)

SCORING_SYSTEM_PROMPT = """Act as an advanced AI resume screening and scoring system modeled after enterprise ATS + recruiter assistants (e.g., Workday, Greenhouse, Lever, HireVue, Jobscan). Your task is to analyze the candidate's resume against a specific job description and produce a structured JSON report with numeric scores and written insights.

=== INSTRUCTIONS ===
Analyze the candidate's resume against the job description across all categories below. Return ONLY valid JSON — no preamble, no markdown fences, no commentary.
- The candidate may submit multiple resume versions (each labeled === RESUME: <filename> ===). Evaluate holistically — treat all versions as a unified candidate profile and draw on the strongest representation from any version for each scoring dimension.

=== SCORING CATEGORIES ===

1. relevance_score (object)
   - Evaluate overall fit: 40% technical skills, 25% experience alignment, 20% quantifiable impact, 10% communication, 5% formatting
   - Fields: score (0-100 integer), summary (1 paragraph string), top_reasons (array of 3 strings)

2. skills_match (object, weight: 40%)
   - Extract all technical skills/frameworks/tools from the resume, match against JD
   - Fields: coverage_pct (0-100), skills_table (array of {skill, found: "Yes"|"No"|"Partial", context}), missing_keywords (array of strings)

3. experience_alignment (object, weight: 25%)
   - Compare years of experience, role progression, industry context
   - Fields: score (0-100 integer), implied_level ("junior"|"mid"|"senior"), summary (1-2 sentences)

4. impact_quantification (object, weight: 20%)
   - Identify measurable impact statements
   - Fields: score (0-100 integer), impact_density_pct (0-100), instances (array of {statement, category: "efficiency_gain"|"revenue_growth"|"cost_reduction"|"ux_improvement"|"automation"})

5. communication_collaboration (object, weight: 10%)
   - Evidence of cross-functional work, stakeholder communication, leadership
   - Fields: score (0-100 integer), examples (array of strings)

6. formatting_readability (object, weight: 5%)
   - Machine readability, consistent headers, bullet structure, date ranges
   - Fields: score (0-100 integer), notes (string)

=== COMPOSITE SCORING ===
composite_score = (skills_match.coverage_pct * 0.40) + (experience_alignment.score * 0.25) + (impact_quantification.score * 0.20) + (communication_collaboration.score * 0.10) + (formatting_readability.score * 0.05)

fit_label: "Strong Fit" if >= 85, "Moderate Fit" if >= 70, "Weak Fit" if < 70
pass_fail: "PASS" if composite_score >= {threshold}, "FAIL" otherwise

=== OUTPUT FORMAT ===
Return ONLY a single valid JSON object:
{{
  "employer": "",
  "job_title": "",
  "relevance_score": {{"score": 0, "summary": "", "top_reasons": []}},
  "skills_match": {{"coverage_pct": 0, "skills_table": [], "missing_keywords": []}},
  "experience_alignment": {{"score": 0, "implied_level": "", "summary": ""}},
  "impact_quantification": {{"score": 0, "impact_density_pct": 0, "instances": []}},
  "communication_collaboration": {{"score": 0, "examples": []}},
  "formatting_readability": {{"score": 0, "notes": ""}},
  "composite_score": 0,
  "fit_label": "",
  "pass_fail": "",
  "strengths": [],
  "weaknesses": [],
  "recommended_action": "",
  "optimization_suggestions": []
}}

Be CONCISE but analytical. Numeric scores must be integers. Do not include any text outside the JSON object."""


def build_user_message(resume_text: str, jd_text: str) -> str:
    return f"""=== RESUME ===
{resume_text}

=== JOB DESCRIPTION ===
{jd_text}"""


def _score_row(client, system: str, resume_text: str, row) -> dict:
    """Call Claude to score a single job row. Returns parsed score dict."""
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        system=system,
        messages=[
            {
                "role": "user",
                "content": build_user_message(resume_text, row["jd_text"]),
            }
        ],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)

    data = json.loads(raw)

    if not data.get("employer"):
        data["employer"] = row["employer"] or "Unknown"
    if not data.get("job_title"):
        data["job_title"] = row["job_title"] or "Unknown"

    return data


def score_jobs(resume_text: str, pass_threshold: int = 70):
    """Score all scraped-but-unscored jobs."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    with db.get_conn() as conn:
        pending = db.get_pending_score(conn)

    if not pending:
        log.info("[scorer] No jobs to score.")
        return

    log.info(f"[scorer] Scoring {len(pending)} jobs...")

    system = SCORING_SYSTEM_PROMPT.replace("{threshold}", str(pass_threshold))

    for row in pending:
        job_id = row["id"]
        log.info(f"[scorer] [{job_id}] {row['employer'] or row['url']} — {row['job_title']}")

        try:
            data = _score_row(client, system, resume_text, row)

            with db.get_conn() as conn:
                db.insert_score(conn, job_id, data)

            verdict = data.get("pass_fail", "?")
            score = data.get("composite_score", "?")
            log.info(f"[scorer] [{job_id}] {verdict} ({score}) — {data.get('fit_label')}")

        except json.JSONDecodeError as e:
            log.error(f"[scorer] [{job_id}] JSON parse error: {e}")
        except Exception as e:
            log.error(f"[scorer] [{job_id}] Error: {e}")

    log.info("[scorer] Done.")


def score_single(job_id: int, resume_text: str, pass_threshold: int = 70) -> dict:
    """Delete any existing score for job_id, re-score, persist, and return score dict."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, url, employer, job_title, jd_text FROM jobs WHERE id=? AND scrape_status='done'",
            (job_id,),
        ).fetchone()

    if not row:
        raise ValueError(f"Job {job_id} not found or not yet scraped")

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    system = SCORING_SYSTEM_PROMPT.replace("{threshold}", str(pass_threshold))

    log.info(f"[scorer] Rescoring [{job_id}] {row['employer'] or row['url']}")
    data = _score_row(client, system, resume_text, row)

    with db.get_conn() as conn:
        db.delete_score(conn, job_id)
        db.insert_score(conn, job_id, data)

    verdict = data.get("pass_fail", "?")
    score = data.get("composite_score", "?")
    log.info(f"[scorer] [{job_id}] rescore → {verdict} ({score})")
    return data
