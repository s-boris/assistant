#!/usr/bin/env bash
# Offline tests for ~/bin/contracts. CONTRACTS_BIN=<path> tests another copy. Exit 0 = all pass.
cd "$(dirname "$0")" || exit 2
exec python3 -I test_contracts.py
