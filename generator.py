"""
generator.py — For PASS jobs, generate a tailored resume + cover letter.
Outputs: Markdown + PDF versions of each.
"""

import json
import logging
import os
import re
from pathlib import Path

import anthropic
import markdown as md_lib

import db

log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent / "output"
RESUME_DIR = OUTPUT_DIR / "resumes"
COVER_DIR = OUTPUT_DIR / "cover_letters"


GENERATION_SYSTEM_PROMPT = """You are an expert resume writer and career coach. Given a candidate's source resume(s) and a job description (plus its ATS score report), produce a tailored resume and cover letter.

=== STRICT SOURCE FIDELITY — NON-NEGOTIABLE ===
Every technology, tool, framework, library, language, platform, and methodology that appears anywhere in the generated resume or cover letter MUST be explicitly present in the candidate's source resume(s). This constraint has no exceptions:

1. DO NOT add any skill, tool, or technology absent from the source resume — even if the JD requires it, even if it appears in the ATS missing_keywords list.
2. DO NOT use softening language to sneak in absent skills ("familiar with X", "exposure to X", "basic knowledge of X").
3. "missing_keywords" means the ATS did not detect that keyword in the resume. Your job is to check whether the candidate's existing, documented experience covers that concept under a different name or phrasing — and if so, reframe the existing language to make the match visible. If the concept is genuinely absent from the source resume, leave it absent. Do not add it.
4. The Core Competencies section must list ONLY items present verbatim or by clear implication in the source resume. You may reorder and curate for relevance, but never extend the list with new items.
5. Fabricating experience causes direct harm to the candidate. When in doubt, omit.

=== RULES ===
- The candidate may provide multiple resume versions (each labeled === RESUME: <filename> ===). Synthesize a single tailored resume drawing from all versions — select the most relevant experience and strongest phrasing from each source. Never add content not present in any version.
- Tailor by reordering, re-emphasizing, and reframing existing experience to align with the role. The summary and competencies sections may be rewritten for emphasis, but only using skills and accomplishments found in the source.
- Use the ATS optimization_suggestions for structural improvements (ordering, phrasing, emphasis) only — not as license to introduce absent skills.
- Resume: clean, ATS-friendly Markdown. No tables, no columns, no images.
- Resume structure: summary; professional experience (ALWAYS strictly chronological, never reordered); personal projects; core competencies; education.
- Cover letter: state clearly that you are an AI assistant writing on the applicant's behalf as part of a job-search pipeline the applicant built. Make the case for the human.
- Cover letter: ALWAYS refer to the applicant in the third person. Speak as the AI Assistant, not as the applicant. First-person ("I am writing...") refers to the AI.
- Cover letter: open with "I am writing to express [applicant name]'s strong interest in [position]" and close with the signature "{candidate_name}'s AI Assistant."
- Cover letter: professional, specific, concise, 3-4 paragraphs. Reference the company and role explicitly.
- Body text must contain NO em-dashes (—) or double-hyphens (--)
- Prefer prose over bullet points in the cover letter
- Return ONLY valid JSON — no preamble, no markdown fences

=== OUTPUT FORMAT ===
Return a single JSON object:
{
  "resume_md": "full resume in markdown",
  "cover_letter_md": "full cover letter in markdown"
}"""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50]


def _doc_slug(employer: str, title: str | None) -> str:
    """Build the company[-title] portion of the filename."""
    parts = [_slug(employer)]
    if title:
        parts.append(_slug(title))
    return "-".join(parts)


def _file_prefix() -> str:
    name = os.environ.get("CANDIDATE_NAME", "").strip()
    if name:
        return _slug(name) + "-"
    # legacy fallback
    p = os.environ.get("FILE_PREFIX", "").strip()
    return f"{p}-" if p else ""


def _unique_name(base: str) -> str:
    """Return base, or base-02/-03/... if a resume with that name already exists."""
    pfx = _file_prefix()
    candidate = base
    n = 2
    while (RESUME_DIR / f"{pfx}resume-{candidate}.md").exists():
        candidate = f"{base}-{n:02d}"
        n += 1
    return candidate


def _md_to_pdf(md_text: str, output_path: Path):
    """Convert markdown → HTML → PDF via WeasyPrint."""
    try:
        from weasyprint import HTML, CSS

        html_body = md_lib.markdown(md_text, extensions=["extra"])
        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{
    font-family: Georgia, serif;
    font-size: 11pt;
    line-height: 1.5;
    color: #1a1a1a;
    max-width: 750px;
    margin: 40px auto;
    padding: 0 40px;
  }}
  h1 {{ font-size: 18pt; margin-bottom: 4px; }}
  h2 {{ font-size: 13pt; border-bottom: 1px solid #ccc; padding-bottom: 2px; margin-top: 18px; }}
  h3 {{ font-size: 11pt; margin-bottom: 2px; }}
  ul {{ margin: 4px 0 8px 20px; }}
  li {{ margin-bottom: 2px; }}
  p  {{ margin: 6px 0; }}
  a  {{ color: #1a1a1a; }}
</style>
</head>
<body>{html_body}</body>
</html>"""

        HTML(string=html).write_pdf(str(output_path))
        log.info(f"[generator] PDF written: {output_path}")

    except Exception as e:
        log.error(f"[generator] PDF generation failed: {e}")
        raise


def _generate_for_row(client, resume_text: str, row) -> None:
    """Generate and persist docs for a single job row."""
    job_id   = row["id"]
    employer = row["employer"] or "Unknown"
    title    = row["job_title"] or None
    log.info(f"[generator] [{job_id}] {employer} — {title or 'Unknown'}")

    score_data = json.loads(row["raw_json"]) if row["raw_json"] else {}

    # Strip missing_keywords from the score report sent to the generator.
    # Sending them verbatim caused the model to add absent skills to the resume.
    # The system prompt already explains the correct handling; we just don't dangle
    # the list in front of the model as an implicit checklist to satisfy.
    score_for_gen = {k: v for k, v in score_data.items() if k != "skills_match"}
    if "skills_match" in score_data:
        # Keep coverage_pct for context but drop the per-skill table and missing list
        score_for_gen["skills_coverage_pct"] = score_data["skills_match"].get("coverage_pct")

    user_msg = f"""=== SOURCE RESUME ===
{resume_text}

=== JOB DESCRIPTION ===
{row['jd_text']}

=== ATS SCORE REPORT ===
{json.dumps(score_for_gen, indent=2)}

Generate a tailored resume and cover letter for this role at {employer}.

REMINDER: every skill, tool, and technology in your output must appear in the SOURCE RESUME above. Do not add anything absent from that text, regardless of what the JD or ATS report requests."""

    candidate_name = os.environ.get("CANDIDATE_NAME", "").strip() or os.environ.get("FILE_PREFIX", "the applicant").strip()
    system = GENERATION_SYSTEM_PROMPT.replace("{candidate_name}", candidate_name)

    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=8192,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)

    data = json.loads(raw)
    resume_md = data["resume_md"]
    cover_md  = data["cover_letter_md"]

    name = _unique_name(_doc_slug(employer, title))
    pfx  = _file_prefix()

    resume_md_path  = RESUME_DIR / f"{pfx}resume-{name}.md"
    resume_pdf_path = RESUME_DIR / f"{pfx}resume-{name}.pdf"
    cover_md_path   = COVER_DIR  / f"{pfx}cover-letter-{name}.md"
    cover_pdf_path  = COVER_DIR  / f"{pfx}cover-letter-{name}.pdf"

    resume_md_path.write_text(resume_md, encoding="utf-8")
    cover_md_path.write_text(cover_md, encoding="utf-8")

    _md_to_pdf(resume_md, resume_pdf_path)
    _md_to_pdf(cover_md, cover_pdf_path)

    with db.get_conn() as conn:
        db.insert_generated_docs(
            conn, job_id,
            resume_md, str(resume_pdf_path),
            cover_md, str(cover_pdf_path),
        )

    log.info(f"[generator] [{job_id}] ✓ Docs saved for {employer}")


def generate_docs(resume_text: str):
    """Generate tailored resume + cover letter for all PASS jobs pending generation."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    RESUME_DIR.mkdir(parents=True, exist_ok=True)
    COVER_DIR.mkdir(parents=True, exist_ok=True)

    with db.get_conn() as conn:
        pending = db.get_pending_generation(conn)

    if not pending:
        log.info("[generator] No jobs pending generation.")
        return

    log.info(f"[generator] Generating docs for {len(pending)} jobs...")
    for row in pending:
        try:
            _generate_for_row(client, resume_text, row)
        except json.JSONDecodeError as e:
            log.error(f"[generator] [{row['id']}] JSON parse error: {e}")
        except Exception as e:
            log.error(f"[generator] [{row['id']}] Error: {e}")
    log.info("[generator] Done.")


def purge_job_docs(job_id: int) -> None:
    """Delete generated files and DB row for a job, if they exist."""
    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM generated_docs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not existing:
            return
        for pdf_path in (existing["resume_pdf_path"], existing["cover_letter_pdf_path"]):
            p = Path(pdf_path)
            for f in (p, p.with_suffix(".md")):
                if f.exists():
                    f.unlink()
        conn.execute("DELETE FROM generated_docs WHERE job_id=?", (job_id,))
        conn.commit()


def generate_single(job_id: int, resume_text: str, score_json: str | None = None) -> None:
    """Purge any existing docs for job_id, then regenerate.

    score_json: if provided (e.g. from a just-completed rescore), use it directly
    rather than re-querying the DB, avoiding any read-isolation edge cases.
    """
    purge_job_docs(job_id)

    if score_json is not None:
        with db.get_conn() as conn:
            row = conn.execute("""
                SELECT j.id, j.url, j.employer, j.job_title, j.jd_text
                FROM jobs j
                WHERE j.id = ?
            """, (job_id,)).fetchone()
        if not row:
            raise ValueError(f"Job {job_id} not found")
        # Attach the caller-supplied raw_json so _generate_for_row sees it
        row = dict(row)
        row["raw_json"] = score_json
    else:
        with db.get_conn() as conn:
            row = conn.execute("""
                SELECT j.id, j.url, j.employer, j.job_title, j.jd_text, s.raw_json
                FROM jobs j
                JOIN scores s ON s.job_id = j.id
                WHERE j.id = ? AND s.pass_fail = 'PASS'
            """, (job_id,)).fetchone()
        if not row:
            raise ValueError(f"Job {job_id} not found or did not PASS")

    RESUME_DIR.mkdir(parents=True, exist_ok=True)
    COVER_DIR.mkdir(parents=True, exist_ok=True)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    _generate_for_row(client, resume_text, row)
