# AeroTax Backend — Safety-Net. Ein Befehl, der VOR jedem „fertig" grün sein muss.
#
#   make verify   → py_compile aller Python-Module + volle pytest-Suite (3360 Tests)
#   make compile  → nur der schnelle Syntax-Check (py_compile)
#   make test     → nur pytest
#
# Regel (CLAUDE.md): keine Aufgabe gilt als erledigt, bevor `make verify` grün ist.

PY := python3

.PHONY: verify compile test
compile:
	$(PY) -m py_compile app.py blueprints/*.py nas_harvester/*.py eu_scraper/*.py

# Der Unit-/Logik-Suite (schnell, offline). Die LIVE-E2E-Contract-Tests
# (tests/aerox/test_contract_ios_backend.py) sind AUSGENOMMEN: sie signieren gegen
# das echte Backend und laufen bei wiederholten lokalen Läufen in einen 429
# (too_many_signups) — sie gehören in CI mit Rate-Limit-Handling, nicht ins
# schnelle Gate. Separat: `make test-e2e`.
test:
	$(PY) -m pytest -q -p no:cacheprovider --ignore=tests/aerox/test_contract_ios_backend.py

test-e2e:
	$(PY) -m pytest -q -p no:cacheprovider tests/aerox/test_contract_ios_backend.py

verify: compile test
	@echo "✓ verify grün (compile + Unit-pytest)"
