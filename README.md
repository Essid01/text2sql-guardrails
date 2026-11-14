# text2sql-guardrails

Natural-language questions → safe, verified SQL on PostgreSQL, with a confidence score.

> 🚧 Work in progress. Evaluation numbers will be published here once real evaluation runs exist.

## Quick start (dev)

```bash
cp .env.example .env          # then fill in ANTHROPIC_API_KEY and LLM_MODEL
docker compose up -d db       # PostgreSQL 16
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev,ui]"
python -m text2sql.db.seed    # create schema + load synthetic retail data (~40k orders)
pytest
```

## Use it

```bash
python -m text2sql.ask "Top 3 categories by net revenue in 2025"   # command line
uvicorn text2sql.api.app:app --port 8000     # API, OpenAPI docs on http://localhost:8000/docs
streamlit run src/text2sql/ui/app.py         # UI on http://localhost:8501 (uses API_URL)
python -m text2sql.feedback                  # export answers marked incorrect, for review
```

See [docs/PROGRESS.md](docs/PROGRESS.md) for the current state of the project.
