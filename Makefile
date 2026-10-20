.PHONY: lint type test smoke up down
lint:
	uv run ruff check . && uv run ruff format --check .
type:
	uv run mypy
test:
	uv run pytest
smoke:
	uv run pytest -m smoke tests/smoke
up:
	./scripts/init-secrets.sh && docker compose up -d --build
down:
	docker compose down
