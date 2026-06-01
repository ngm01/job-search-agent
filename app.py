"""
app.py — Flask GUI for the job pipeline.
"""

import json
import logging
import os
import threading
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, render_template, jsonify, send_file, abort, request

load_dotenv()

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/pipeline.log"),
    ],
)

import db

log = logging.getLogger(__name__)

app = Flask(__name__)
db.init_db()

OUTPUT_DIR   = Path(__file__).parent / "output"
RESUMES_DIR  = Path(__file__).parent / "resumes"
QUERIES_FILE = Path(__file__).parent / "queries.txt"

# ── Pipeline background runner ──────────────────────────────────────────────

_pipeline = {"running": False, "step": "", "error": None, "last_log": ""}


class _TailHandler(logging.Handler):
    """Captures the most recent log line into the pipeline status dict."""
    def emit(self, record):
        _pipeline["last_log"] = self.format(record)


_tail_handler = _TailHandler()
_tail_handler.setFormatter(logging.Formatter("%(message)s"))


def _load_resumes() -> str:
    RESUMES_DIR.mkdir(exist_ok=True)
    files = sorted(RESUMES_DIR.glob("*.md"))
    if not files:
        legacy = Path(os.environ.get("RESUME_PATH", "resume.md"))
        if legacy.exists():
            return legacy.read_text(encoding="utf-8").strip()
        raise FileNotFoundError("No resumes found in resumes/ and no resume.md fallback")
    if len(files) == 1:
        return files[0].read_text(encoding="utf-8").strip()
    parts = [
        f"=== RESUME: {f.name} ===\n{f.read_text(encoding='utf-8').strip()}"
        for f in files
    ]
    return "\n\n".join(parts)


def _load_query_lines() -> list[str]:
    if not QUERIES_FILE.exists():
        return []
    lines = QUERIES_FILE.read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def _run_pipeline_bg(queries, resume_text, pass_threshold, max_results):
    root = logging.getLogger()
    root.addHandler(_tail_handler)
    try:
        from search import run_queries
        from scraper import scrape_jobs
        from scorer import score_jobs

        _pipeline["step"] = "searching"
        run_queries(queries, max_results=max_results)

        _pipeline["step"] = "scraping"
        scrape_jobs()

        _pipeline["step"] = "scoring"
        score_jobs(resume_text, pass_threshold=pass_threshold)

        _pipeline["step"] = "done"
    except Exception as e:
        log.exception("[pipeline] background run failed")
        _pipeline["error"] = str(e)
        _pipeline["step"] = "error"
    finally:
        _pipeline["running"] = False
        logging.getLogger().removeHandler(_tail_handler)


# ── Standard routes ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/results")
def api_results():
    with db.get_conn() as conn:
        rows = db.get_all_results(conn)

    results = []
    for r in rows:
        score_data = json.loads(r["raw_json"]) if r["raw_json"] else {}
        results.append({
            "id": r["id"],
            "url": r["url"],
            "employer": r["employer"] or "—",
            "job_title": r["job_title"] or "—",
            "source": r["source"],
            "discovered_at": r["discovered_at"],
            "composite_score": r["composite_score"],
            "fit_label": r["fit_label"],
            "pass_fail": r["pass_fail"],
            "recommended_action": r["recommended_action"],
            "relevance_score": r["relevance_score"],
            "skills_coverage_pct": r["skills_coverage_pct"],
            "experience_score": r["experience_score"],
            "experience_implied_level": r["experience_implied_level"],
            "impact_score": r["impact_score"],
            "communication_score": r["communication_score"],
            "formatting_score": r["formatting_score"],
            "strengths": json.loads(r["strengths"]) if r["strengths"] else [],
            "weaknesses": json.loads(r["weaknesses"]) if r["weaknesses"] else [],
            "optimization_suggestions": json.loads(r["optimization_suggestions"]) if r["optimization_suggestions"] else [],
            "has_docs": bool(r["resume_pdf_path"]),
            "applied": bool(r["applied"]),
            "resume_pdf": r["resume_pdf_path"],
            "cover_pdf": r["cover_letter_pdf_path"],
            "resume_md": r["resume_md"],
            "cover_md": r["cover_letter_md"],
            "skills_table": score_data.get("skills_match", {}).get("skills_table", []),
            "missing_keywords": score_data.get("skills_match", {}).get("missing_keywords", []),
        })

    return jsonify(results)


@app.route("/api/stats")
def api_stats():
    with db.get_conn() as conn:
        total     = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        scraped   = conn.execute("SELECT COUNT(*) FROM jobs WHERE scrape_status='done'").fetchone()[0]
        scored    = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
        passed    = conn.execute("SELECT COUNT(*) FROM scores WHERE pass_fail='PASS'").fetchone()[0]
        failed    = conn.execute("SELECT COUNT(*) FROM scores WHERE pass_fail='FAIL'").fetchone()[0]
        generated = conn.execute("SELECT COUNT(*) FROM generated_docs").fetchone()[0]
    return jsonify({
        "total": total, "scraped": scraped, "scored": scored,
        "passed": passed, "failed": failed, "generated": generated,
    })


@app.route("/download/<int:job_id>/<doc_type>")
def download(job_id: int, doc_type: str):
    if doc_type not in ("resume_pdf", "cover_pdf", "resume_md", "cover_md"):
        abort(400)

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM generated_docs WHERE job_id=?", (job_id,)
        ).fetchone()

    if not row:
        abort(404)

    if doc_type == "resume_md":
        return row["resume_md"], 200, {
            "Content-Type": "text/markdown",
            "Content-Disposition": f'attachment; filename="resume-{job_id}.md"',
        }
    if doc_type == "cover_md":
        return row["cover_letter_md"], 200, {
            "Content-Type": "text/markdown",
            "Content-Disposition": f'attachment; filename="cover-{job_id}.md"',
        }

    col = "resume_pdf_path" if doc_type == "resume_pdf" else "cover_letter_pdf_path"
    path = Path(row[col])
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True)


# ── Pipeline ─────────────────────────────────────────────────────────────────

@app.route("/api/run", methods=["POST"])
def api_run():
    if _pipeline["running"]:
        return jsonify({"error": "pipeline already running"}), 409

    try:
        resume_text = _load_resumes()
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 500

    queries = _load_query_lines()
    if not queries:
        return jsonify({"error": "no queries configured"}), 400

    pass_threshold = int(os.environ.get("PASS_THRESHOLD", 70))
    max_results    = int(os.environ.get("SEARCH_RESULTS_PER_QUERY", 10))

    _pipeline["running"] = True
    _pipeline["step"]    = "starting"
    _pipeline["error"]   = None

    threading.Thread(
        target=_run_pipeline_bg,
        args=(queries, resume_text, pass_threshold, max_results),
        daemon=True,
    ).start()

    return jsonify({"ok": True})


@app.route("/api/run/status")
def api_run_status():
    return jsonify(_pipeline)


# ── Queries ───────────────────────────────────────────────────────────────────

@app.route("/api/queries", methods=["GET"])
def api_queries_get():
    return jsonify(_load_query_lines())


@app.route("/api/queries", methods=["POST"])
def api_queries_post():
    lines = request.json.get("lines", [])
    content = (
        "# Search queries — one per line. Lines starting with # are ignored.\n\n"
        + "\n".join(l.strip() for l in lines if l.strip())
        + "\n"
    )
    QUERIES_FILE.write_text(content, encoding="utf-8")
    return jsonify({"ok": True, "count": len([l for l in lines if l.strip()])})


# ── Resumes ───────────────────────────────────────────────────────────────────

@app.route("/api/resumes", methods=["GET"])
def api_resumes_list():
    RESUMES_DIR.mkdir(exist_ok=True)
    files = sorted(RESUMES_DIR.glob("*.md"))
    return jsonify([{"name": f.name, "chars": f.stat().st_size} for f in files])


@app.route("/api/resumes/<name>", methods=["GET"])
def api_resume_get(name: str):
    path = RESUMES_DIR / name
    if not path.exists() or path.suffix != ".md" or path.parent != RESUMES_DIR:
        abort(404)
    return jsonify({"name": name, "content": path.read_text(encoding="utf-8")})


@app.route("/api/resumes/<name>", methods=["POST"])
def api_resume_save(name: str):
    if not name.endswith(".md") or "/" in name or "\\" in name:
        abort(400)
    content = request.json.get("content", "")
    RESUMES_DIR.mkdir(exist_ok=True)
    (RESUMES_DIR / name).write_text(content, encoding="utf-8")
    return jsonify({"ok": True})


@app.route("/api/resumes/<name>", methods=["DELETE"])
def api_resume_delete(name: str):
    path = RESUMES_DIR / name
    if not path.exists() or path.suffix != ".md" or path.parent != RESUMES_DIR:
        abort(404)
    path.unlink()
    return jsonify({"ok": True})


# ── Generate / Applied ────────────────────────────────────────────────────────

@app.route("/api/generate/<int:job_id>", methods=["POST"])
def api_generate(job_id: int):
    try:
        resume_text = _load_resumes()
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 500

    from generator import generate_single
    try:
        generate_single(job_id, resume_text)
        return jsonify({"ok": True})
    except Exception as e:
        log.exception(f"[api_generate] job {job_id} failed")
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/rescore/<int:job_id>", methods=["POST"])
def api_rescore(job_id: int):
    try:
        resume_text = _load_resumes()
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 500

    pass_threshold = int(os.environ.get("PASS_THRESHOLD", 70))

    from scorer import score_single
    from generator import generate_single, purge_job_docs

    # ── Step 1: score ── if this fails, nothing in the DB has changed yet
    try:
        data = score_single(job_id, resume_text, pass_threshold)
    except Exception as e:
        log.exception(f"[api_rescore] scoring job {job_id} failed")
        return jsonify({"error": f"Scoring failed: {type(e).__name__}: {e}"}), 500

    # ── Step 2: docs ── score is already committed; generation failure is non-fatal
    warning = None
    if data["pass_fail"] == "PASS":
        try:
            import json as _json
            generate_single(job_id, resume_text, score_json=_json.dumps(data))
        except Exception as e:
            log.exception(f"[api_rescore] generation for job {job_id} failed after PASS score")
            warning = f"Score updated to {data['composite_score']:.0f} but document generation failed: {e}"
    else:
        purge_job_docs(job_id)

    resp = {"ok": True, "pass_fail": data["pass_fail"], "composite_score": data["composite_score"]}
    if warning:
        resp["warning"] = warning
    return jsonify(resp)


@app.route("/api/applied/<int:job_id>", methods=["POST"])
def api_applied(job_id: int):
    applied = request.json.get("applied", False)
    with db.get_conn() as conn:
        db.mark_applied(conn, job_id, applied)
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
