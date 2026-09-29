#!/usr/bin/env bash
# DMN generation-quality benchmark runner (RHAIENG-6455).
#
# Usage:
#   ./run-benchmark.sh --suite all       --corpus hypertension   # creator + reviewer
#   ./run-benchmark.sh --suite creator   --corpus hypertension
#   ./run-benchmark.sh --suite reviewer  --corpus hypertension
#   ./run-benchmark.sh --suite creator   --no-compile            # skip /jit/dmn checks
#
# Drives the REAL creator/reviewer nodes, so it needs the LLM env
# (LITELLM_URL / LLM_MODEL / LITELLM_API_KEY), exactly as the pipeline reads it.
# Uses the cpg-ingester venv. Override the interpreter with
# BENCH_PYTHON=/path/to/python if your venv lives elsewhere.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# cpg-ingester/tests/benchmarks/dmn -> cpg-ingester
CPG_INGESTER_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
PY="${BENCH_PYTHON:-${CPG_INGESTER_DIR}/.venv/bin/python}"

if [[ ! -x "${PY}" ]]; then
  echo "ERROR: Python interpreter not found at ${PY}" >&2
  echo "Set BENCH_PYTHON to your venv python, or create the venv." >&2
  exit 1
fi

exec "${PY}" "${SCRIPT_DIR}/run_benchmark.py" "$@"
