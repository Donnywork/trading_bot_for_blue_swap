.PHONY: check format test run status dashboard

check:
	uv run ruff check --extend-exclude research .
	uv run ruff format --check --extend-exclude research .

format:
	uv run ruff check --fix --extend-exclude research .
	uv run ruff format --extend-exclude research .

test:
	uv run pytest

run:
	uv run python main.py

status:
	uv run python status.py

dashboard:
	uv run python dashboard.py
