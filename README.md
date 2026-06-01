# job-search-agent

An automated job search pipeline that discovers job postings, scores them against your resume using an ATS-style AI evaluation, and generates tailored resumes and cover letters for the roles that pass.

Built around DuckDuckGo search, Playwright scraping, the Anthropic Claude API, and a local Flask GUI.

---

## How it works

```
queries.txt
    │
    ▼
search.py      DuckDuckGo → extract job board URLs → SQLite (dedupe)
    │
    ▼
scraper.py     Playwright → scrape full JD text → SQLite
    │
    ▼
scorer.py      Claude API → ATS scoring (skills, experience, impact) → SQLite
    │
    ▼
generator.py   Claude API → tailored resume + cover letter → MD + PDF
    │
    ▼
app.py         Flask GUI → results table, doc downloads, pipeline controls
```

Supported job boards: Greenhouse, Lever, Ashby.

---

## Prerequisites

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com) with a credit balance
  - This is **separate** from a Claude.ai subscription — you need API credits at console.anthropic.com
- Chromium (installed via Playwright)

---

## Setup

```bash
git clone <repo>
cd job-search-agent
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
ANTHROPIC_API_KEY=sk-ant-...
FILE_PREFIX=YourInitials        # e.g. JSmith → JSmith-resume-google-swe.pdf
PASS_THRESHOLD=70               # composite ATS score cutoff (0–100)
SEARCH_RESULTS_PER_QUERY=10
```

### Add your resume(s)

Create a `resumes/` directory and add one or more Markdown-formatted resumes:

```bash
mkdir resumes
# Add resume.md (and optionally resume-v2.md, resume-ml.md, etc.)
```

When multiple resumes are present, the scorer and generator use all of them holistically — the scorer evaluates across all versions and the generator synthesizes the best tailored document from all sources.

### Configure search queries

```bash
cp queries.example.txt queries.txt
# Edit queries.txt with your target roles and job boards
```

Each line is a DuckDuckGo query. Blank lines and lines starting with `#` are ignored.

---

## Running

### GUI (recommended)

```bash
python run.py --gui
# Open http://localhost:5000
```

The GUI lets you:
- Run the full pipeline (search → scrape → score) with a single button
- Edit search queries in-browser
- Manage source resumes (add, edit, delete)
- Browse all scored jobs with ATS breakdowns
- Download generated resumes and cover letters
- Track which jobs you've applied to

### CLI

```bash
# Full pipeline
python run.py

# Individual steps
python run.py --search      # discover new URLs only
python run.py --scrape      # scrape pending jobs
python run.py --score       # score scraped jobs
python run.py --generate    # generate docs for PASS jobs

# Re-generate all docs (purges existing, regenerates from scratch)
python regen.py
```

---

## Output

| Path | Contents |
|---|---|
| `output/resumes/` | Tailored resumes as `.md` and `.pdf` |
| `output/cover_letters/` | Cover letters as `.md` and `.pdf` |
| `data/pipeline.db` | SQLite database — open with [DB Browser for SQLite](https://sqlitebrowser.org) |
| `logs/pipeline.log` | Pipeline run log |

Files are named `{FILE_PREFIX}-resume-{company}-{title}.pdf` (or without prefix if `FILE_PREFIX` is unset).

---

## Tuning

| Setting | Default | Description |
|---|---|---|
| `PASS_THRESHOLD` | `70` | Composite ATS score cutoff for PASS/FAIL |
| `SEARCH_RESULTS_PER_QUERY` | `10` | DuckDuckGo results fetched per query |
| `FILE_PREFIX` | _(empty)_ | Prefix for output filenames |

The scoring rubric weights: Skills Match 40%, Experience Alignment 25%, Impact Quantification 20%, Communication 10%, Formatting 5%.

---

## Project structure

```
.
├── run.py              # CLI entrypoint + pipeline orchestration
├── search.py           # DuckDuckGo search → job board URL extraction
├── scraper.py          # Playwright JD scraper
├── scorer.py           # Claude ATS scorer
├── generator.py        # Claude resume + cover letter generator
├── app.py              # Flask GUI + REST API
├── db.py               # SQLite schema + query helpers
├── regen.py            # Bulk re-generation utility
├── templates/
│   └── index.html      # GUI frontend
├── resumes/            # Your source resume(s) — gitignored
├── output/             # Generated docs — gitignored
├── data/               # SQLite DB — gitignored
├── queries.txt         # Your search queries — gitignored
├── queries.example.txt # Template to copy from
└── .env.example        # Environment variable template
```
