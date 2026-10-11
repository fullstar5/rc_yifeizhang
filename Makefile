.PHONY: up down migrate test lint

up:
	docker compose up --build

down:
	docker compose down

migrate:
	alembic upgrade head

test:
	pytest

lint:
	ruff check .
