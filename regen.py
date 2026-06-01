"""
regen.py — Delete and re-generate docs for all PASS jobs.

Usage:
    python regen.py
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

Path("logs").mkdir(exist_ok=True)
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

OUTPUT_DIR = Path(__file__).parent / "output"
RESUME_DIR = OUTPUT_DIR / "resumes"
COVER_DIR  = OUTPUT_DIR / "cover_letters"


def purge_existing_docs():
    import db

    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT d.resume_pdf_path, d.cover_letter_pdf_path
            FROM generated_docs d
            JOIN scores s ON s.job_id = d.job_id
            WHERE s.pass_fail = 'PASS'
        """).fetchall()

        deleted_files = 0
        for row in rows:
            for pdf_path in (row["resume_pdf_path"], row["cover_letter_pdf_path"]):
                p = Path(pdf_path)
                for f in (p, p.with_suffix(".md")):
                    if f.exists():
                        f.unlink()
                        deleted_files += 1

        # remove any stray files not tracked in the DB
        for f in list(RESUME_DIR.glob("*")) + list(COVER_DIR.glob("*")):
            if f.is_file():
                f.unlink()
                deleted_files += 1

        deleted_rows = conn.execute("""
            DELETE FROM generated_docs
            WHERE job_id IN (SELECT job_id FROM scores WHERE pass_fail = 'PASS')
        """).rowcount
        conn.commit()

    log.info(f"[regen] Purged {deleted_files} files and {deleted_rows} DB rows.")


if __name__ == "__main__":
    import db
    from run import load_resumes
    from generator import generate_docs

    db.init_db()

    resume_text = load_resumes()
    purge_existing_docs()
    generate_docs(resume_text)

    log.info("[regen] Done.")
