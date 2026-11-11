.PHONY: test-smoke test-fast test litmus privacy lint

# Smoke: unit tests of core packages, the envelope contract and the litmus test (about 5 s).
test-smoke:
	uv run pytest -q -m smoke

# Fast: everything except tests marked slow (performance, exhaustive shape variants), in parallel.
test-fast:
	uv run pytest -q -n auto -m "not slow"

# Full: every test, including performance, in parallel.
test:
	uv run pytest -q -n auto

litmus:
	uv run pytest -q tests/litmus

privacy:
	python3 scripts/privacy_scan.py

lint:
	uv run python -m compileall -q src tests
