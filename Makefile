run:
	uv run python serve.py

clean:
	rm -rf .novnc .venv __pycache__

.PHONY: run clean
