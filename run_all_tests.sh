#!/bin/bash
# =============================================================================
# AeroX Test-Pyramid — single-command runner
# =============================================================================
# Runs every automated layer (1..11) of the iOS+backend test pyramid.
# Layer 12 (Manual QA) is a human checklist — see reminder at the end.
#
# Usage:
#   ./run_all_tests.sh                # run everything, fail-fast
#   ./run_all_tests.sh --keep-going   # continue after a layer fails
#   ./run_all_tests.sh --quick        # skip live-network layers (E2E)
#
# Exit-code 0 = all layers pass, non-zero = at least one layer failed.
# =============================================================================

set -u
FAIL_FAST=1
RUN_LIVE=1
for arg in "$@"; do
    case "$arg" in
        --keep-going) FAIL_FAST=0 ;;
        --quick)      RUN_LIVE=0 ;;
        -h|--help)
            grep '^#' "$0" | head -20
            exit 0
            ;;
    esac
done

BACKEND_ROOT="/Users/miguelschumann/Desktop/aerotax-backend"
IOS_TESTS="/Users/miguelschumann/Desktop/aeris-ios/AeroTax/tests"

LAYERS_PASSED=()
LAYERS_FAILED=()
LAYERS_SKIPPED=()

run_layer() {
    local name="$1"; shift
    local cmd="$*"
    echo
    echo "============================================================"
    echo "  $name"
    echo "============================================================"
    if eval "$cmd"; then
        LAYERS_PASSED+=("$name")
        echo "[PASS] $name"
    else
        local rc=$?
        LAYERS_FAILED+=("$name (rc=$rc)")
        echo "[FAIL] $name (rc=$rc)"
        if [ "$FAIL_FAST" -eq 1 ]; then
            print_summary
            exit "$rc"
        fi
    fi
}

skip_layer() {
    local name="$1"; shift
    local reason="$*"
    LAYERS_SKIPPED+=("$name — $reason")
    echo
    echo "[SKIP] $name — $reason"
}

print_summary() {
    echo
    echo "============================================================"
    echo "  SUMMARY"
    echo "============================================================"
    echo "Passed:  ${#LAYERS_PASSED[@]}"
    for l in "${LAYERS_PASSED[@]:-}";  do [ -n "$l" ] && echo "   + $l"; done
    echo "Failed:  ${#LAYERS_FAILED[@]}"
    for l in "${LAYERS_FAILED[@]:-}";  do [ -n "$l" ] && echo "   - $l"; done
    echo "Skipped: ${#LAYERS_SKIPPED[@]}"
    for l in "${LAYERS_SKIPPED[@]:-}"; do [ -n "$l" ] && echo "   ~ $l"; done
}

echo "============================================================"
echo "  AeroX Test-Pyramid — automated layers 1..11"
echo "  Fail-fast: $FAIL_FAST  |  Live-network: $RUN_LIVE"
echo "============================================================"

cd "$BACKEND_ROOT" || { echo "Cannot cd to $BACKEND_ROOT"; exit 2; }

# -----------------------------------------------------------------------------
# Layer 1 — Contract tests (iOS ↔ Backend JSON shapes)
# -----------------------------------------------------------------------------
run_layer "Layer 1: Contract Tests (iOS ↔ Backend)" \
    "pytest tests/aerox/test_contract_ios_backend.py -v --tb=short"

# -----------------------------------------------------------------------------
# Layer 2 — E2E Live Smoke (touches real cloud-run backend)
# -----------------------------------------------------------------------------
if [ "$RUN_LIVE" -eq 1 ]; then
    run_layer "Layer 2: E2E Live Smoke (7 journeys)" \
        "pytest tests/aerox/test_e2e_smoke_live.py -v --tb=short"
else
    skip_layer "Layer 2: E2E Live Smoke" "--quick mode (no live network)"
fi

# -----------------------------------------------------------------------------
# Layer 6 — iCal corpus (parsing 20+ real-world calendar samples)
# -----------------------------------------------------------------------------
run_layer "Layer 6: iCal Corpus (20 fixtures)" \
    "pytest tests/aerox/test_ical_corpus.py -v --tb=short"

# -----------------------------------------------------------------------------
# Layers 3..8 — dynamic discovery of Wave-2 audit scripts
# (Wave 2 lands incrementally: network/keychain/offline/perf/snapshot audits)
# -----------------------------------------------------------------------------
echo
echo "============================================================"
echo "  Discovering Wave-2 audit scripts under $IOS_TESTS"
echo "============================================================"
DISCOVERED_SCRIPTS=()
if [ -d "$IOS_TESTS" ]; then
    while IFS= read -r f; do
        case "$(basename "$f")" in
            security_audit.sh|appstore_readiness.sh)
                ;;  # handled explicitly below as Layer 9 + 11
            *)
                DISCOVERED_SCRIPTS+=("$f")
                ;;
        esac
    done < <(find "$IOS_TESTS" -maxdepth 2 -type f \( -name "*.sh" -o -name "*audit*.py" -o -name "*test*.py" \) | sort)
fi

if [ "${#DISCOVERED_SCRIPTS[@]}" -eq 0 ]; then
    skip_layer "Layers 3..8 (Wave-2 audits)" "no Wave-2 scripts discovered yet"
else
    for script in "${DISCOVERED_SCRIPTS[@]}"; do
        base="$(basename "$script")"
        case "$base" in
            *.sh) cmd="bash \"$script\"" ;;
            *.py) cmd="python3 \"$script\"" ;;
            *)    continue ;;
        esac
        run_layer "Wave-2 audit: $base" "$cmd"
    done
fi

# -----------------------------------------------------------------------------
# Layer 9 — Security audit (Keychain, ATS, TLS pinning, etc.)
# -----------------------------------------------------------------------------
if [ -x "$IOS_TESTS/security_audit.sh" ]; then
    run_layer "Layer 9: Security Audit" \
        "bash \"$IOS_TESTS/security_audit.sh\""
else
    skip_layer "Layer 9: Security Audit" "security_audit.sh not found / not executable"
fi

# -----------------------------------------------------------------------------
# Layer 11 — App-Store-Readiness (Info.plist, icons, privacy-manifest)
# -----------------------------------------------------------------------------
if [ -x "$IOS_TESTS/appstore_readiness.sh" ]; then
    run_layer "Layer 11: App-Store Readiness" \
        "bash \"$IOS_TESTS/appstore_readiness.sh\""
else
    skip_layer "Layer 11: App-Store Readiness" "appstore_readiness.sh not found / not executable"
fi

# -----------------------------------------------------------------------------
# Final summary + Layer-12 manual-QA reminder
# -----------------------------------------------------------------------------
print_summary

echo
echo "============================================================"
echo "  Layer 12 — Manual QA (NOT automated)"
echo "============================================================"
echo "  Run the human checklist before any App-Store submission:"
echo "    open $IOS_TESTS/MANUAL_QA_CHECKLIST.md"
echo "  Estimated time: ~30 min on real device(s)."
echo

if [ "${#LAYERS_FAILED[@]}" -gt 0 ]; then
    exit 1
fi
exit 0
