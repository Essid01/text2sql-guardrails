# Shortcuts. On Windows without `make`, run the commands directly.
.PHONY: test lint eval eval-quick

test:
	pytest -m "not embeddings"

lint:
	ruff check src tests && ruff format --check src tests

# Full benchmark: real LLM + database (~$0.10 the first run, cached afterwards).
eval:
	python -m text2sql.eval

# Benchmark without the injected bad queries (cheaper).
eval-quick:
	python -m text2sql.eval --no-injected
