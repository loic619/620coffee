"""ICE certified-stocks scraper package.

Pulls the 10 ICE coffee certified-stock reports (1 arabica + 9 robusta sources),
parses each independently with per-source failure resilience, and writes two
static JSON files consumed by the frontend:

  - frontend/public/data/certified_stocks_arabica.json
  - frontend/public/data/certified_stocks_robusta.json

Architecture: each source has its own fetch + parse module returning a typed
dict. orchestrate.py composes them, preserves last-good values on per-source
failure, and writes atomically via safe_write_json with a real validate_fn.
"""
