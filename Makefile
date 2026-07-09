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
	$(PY) -m py_compile app.py blueprints/*.py nas_harvester/*.py

test:
	$(PY) -m pytest -q -p no:cacheprovider

verify: compile test
	@echo "✓ verify grün (compile + pytest)"
