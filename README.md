# text2sql-guardrails

Natural-language questions → safe, verified SQL on PostgreSQL, with a confidence score.

> 🚧 Work in progress. Evaluation numbers will be published here once real evaluation runs exist.

## Quick start (dev)

```bash
cp .env.example .env          # then fill in ANTHROPIC_API_KEY and LLM_MODEL
docker compose up -d db       # PostgreSQL 16
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m text2sql.db.seed    # create schema + load synthetic retail data (~40k orders)
pytest
```

See [docs/PROGRESS.md](docs/PROGRESS.md) for the current state of the project.
