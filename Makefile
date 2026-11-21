.PHONY: check format test test-browser run status dashboard events

check:
	uv run ruff check --extend-exclude research .
	uv run ruff format --check --extend-exclude research .

format:
	uv run ruff check --fix --extend-exclude research .
	uv run ruff format --extend-exclude research .

test:
	uv run pytest

test-browser:
	PHASE2_BROWSER_TESTS=1 uv run pytest tests/test_phase2_browser.py

run:
	uv run python main.py

status:
	uv run python status.py

dashboard:
	uv run python dashboard.py

watchdog:
	uv run python watchdog.py

events: run
