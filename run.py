"""
run.py — Main pipeline entrypoint.

Usage:
    python run.py              # full pipeline (search → scrape → score → generate)
    python run.py --scrape     # scrape only (no new search queries)
    python run.py --score      # score only
    python run.py --generate   # generate docs only
    python run.py --gui        # launch Flask GUI only
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

Path("logs").mkdir(exist_ok=True)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/pipeline.log"),
    ],
)
log = logging.getLogger(__name__)

RESUMES_DIR = Path(__file__).parent / "resumes"


def load_resumes() -> str:
    """Load all .md files from resumes/, combined with separators when multiple."""
    RESUMES_DIR.mkdir(exist_ok=True)
    files = sorted(RESUMES_DIR.glob("*.md"))

    if not files:
        legacy = Path(os.environ.get("RESUME_PATH", "resume.md"))
        if legacy.exists():
            log.warning(f"[main] resumes/ is empty — falling back to {legacy}")
            return legacy.read_text(encoding="utf-8").strip()
        log.error("[main] No resumes found. Add .md files to resumes/.")
        sys.exit(1)

    if len(files) == 1:
        text = files[0].read_text(encoding="utf-8").strip()
        log.info(f"[main] Loaded 1 resume: {files[0].name} ({len(text)} chars)")
        return text

    log.info(f"[main] Loaded {len(files)} resumes: {', '.join(f.name for f in files)}")
    parts = [
        f"=== RESUME: {f.name} ===\n{f.read_text(encoding='utf-8').strip()}"
        for f in files
    ]
    return "\n\n".join(parts)


def load_queries() -> list[str]:
    queries_path = Path(os.environ.get("QUERIES_FILE", "queries.txt"))
    if not queries_path.exists():
        log.warning(f"[main] No queries file found at {queries_path}")
        return []
    lines = queries_path.read_text(encoding="utf-8").splitlines()
    queries = [l.strip() for l in lines if l.strip() and not l.startswith("#")]
    log.info(f"[main] Loaded {len(queries)} search queries")
    return queries


def run_pipeline(steps: list[str]):
    import db

    db.init_db()

    resume_text = load_resumes()
    pass_threshold = int(os.environ.get("PASS_THRESHOLD", 70))
    max_results = int(os.environ.get("SEARCH_RESULTS_PER_QUERY", 10))

    if "search" in steps:
        queries = load_queries()
        if queries:
            from search import run_queries
            log.info("[main] ── Step 1: Search ──")
            stats = run_queries(queries, max_results=max_results)
            log.info(f"[main] Search complete: {stats}")
        else:
            log.info("[main] Skipping search — no queries loaded")

    if "scrape" in steps:
        from scraper import scrape_jobs
        log.info("[main] ── Step 2: Scrape ──")
        scrape_jobs()

    if "score" in steps:
        from scorer import score_jobs
        log.info("[main] ── Step 3: Score ──")
        score_jobs(resume_text, pass_threshold=pass_threshold)

    if "generate" in steps:
        from generator import generate_docs
        log.info("[main] ── Step 4: Generate ──")
        generate_docs(resume_text)

    log.info("[main] Pipeline complete.")

    # Print summary table to terminal
    with db.get_conn() as conn:
        rows = db.get_all_results(conn)

    if rows:
        print("\n" + "─" * 80)
        print(f"{'EMPLOYER':<25} {'ROLE':<30} {'SCORE':>6}  {'RESULT':<6}  {'ACTION'}")
        print("─" * 80)
        for r in rows:
            score = f"{r['composite_score']:.1f}" if r['composite_score'] is not None else "  —  "
            pf = r['pass_fail'] or "—"
            action = r['recommended_action'] or "—"
            employer = (r['employer'] or "—")[:24]
            title = (r['job_title'] or "—")[:29]
            print(f"{employer:<25} {title:<30} {score:>6}  {pf:<6}  {action}")
        print("─" * 80 + "\n")


def run_gui():
    log.info("[main] Starting GUI at http://localhost:5000")
    import db
    db.init_db()
    from app import app
    app.run(debug=False, port=5000)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Job application pipeline")
    parser.add_argument("--search",   action="store_true", help="Run search step only")
    parser.add_argument("--scrape",   action="store_true", help="Run scrape step only")
    parser.add_argument("--score",    action="store_true", help="Run score step only")
    parser.add_argument("--generate", action="store_true", help="Run generation step only")
    parser.add_argument("--gui",      action="store_true", help="Launch GUI only")
    args = parser.parse_args()

    Path("logs").mkdir(exist_ok=True)

    if args.gui:
        run_gui()
    elif any([args.search, args.scrape, args.score, args.generate]):
        steps = []
        if args.search:   steps.append("search")
        if args.scrape:   steps.append("scrape")
        if args.score:    steps.append("score")
        if args.generate: steps.append("generate")
        run_pipeline(steps)
    else:
        # Default: full pipeline
        run_pipeline(["search", "scrape", "score", "generate"])
