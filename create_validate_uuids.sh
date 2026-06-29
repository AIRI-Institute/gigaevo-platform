# run - bash create_validate_uuids.sh

#!/bin/bash
set -euo pipefail

# Build UUID-named experiment folders for all specs in this directory,
# placing results into master_api/temp_experiments using the local builder.

DIR="$(cd "$(dirname "$0")" && cd master_api/data_examples && pwd)"
# repo root is the directory where this script lives
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
BUILDER="$REPO_ROOT/master_api/src/folder_constructor/uuid_experiment_builder.py"
OUT_ROOT="$REPO_ROOT/master_api/temp_experiments"

# Run a command via uv in the master_api project context, regardless of current cwd
uv_master_api() {
  (
    cd "$REPO_ROOT/master_api" && uv run "$@"
  )
}

run_build() {
  local SPEC_JSON="$1"
  local DATASET_PATH="$2"
  echo "[build] spec=$(basename "$SPEC_JSON") dataset=$(basename "$DATASET_PATH")"
  uv_master_api python "$BUILDER" \
    --spec-json "$SPEC_JSON" \
    --dataset-path "$DATASET_PATH" \
    --output-root "$OUT_ROOT"
}

# Resolve CSV corresponding to a given spec JSON.
resolve_csv_for_spec() {
  local SPEC_JSON="$1"

  # 1) Try to read dataset_path from JSON and map to examples by basename
  local CSV_BASENAME
  CSV_BASENAME=$(uv_master_api python - "$SPEC_JSON" << 'PY'
import json, os, sys
with open(sys.argv[1], 'r') as f:
    data = json.load(f)
dp = data.get('dataset_path') or ''
print(os.path.basename(dp))
PY
)
  if [[ -n "$CSV_BASENAME" && -f "$DIR/$CSV_BASENAME" ]]; then
    echo "$DIR/$CSV_BASENAME"
    return 0
  fi

  # 2) Try same-name CSV: foo_spec.json -> foo.csv
  local SPEC_FILE BASE_NO_EXT BASE_NO_SPEC CAND
  SPEC_FILE="$(basename "$SPEC_JSON")"
  BASE_NO_EXT="${SPEC_FILE%.json}"
  BASE_NO_SPEC="${BASE_NO_EXT%_spec}"
  CAND="$DIR/$BASE_NO_SPEC.csv"
  if [[ -f "$CAND" ]]; then
    echo "$CAND"
    return 0
  fi

  # 3) Heuristic: match tokens from spec name to CSV filenames
  local SPEC_LC TOKENS token csv_lc match
  SPEC_LC="${BASE_NO_SPEC,,}"
  # Split on underscores
  IFS='_' read -r -a TOKENS <<< "$SPEC_LC"
  for csv in "$DIR"/*.csv; do
    csv_lc="$(basename "${csv,,}")"
    match=1
    for token in "${TOKENS[@]}"; do
      case "$token" in
        spec|classification|regression|clustering|clusterization|from|sklearn)
          continue ;;
      esac
      if [[ "$csv_lc" != *"$token"* ]]; then
        match=0
        break
      fi
    done
    if [[ $match -eq 1 ]]; then
      echo "$csv"
      return 0
    fi
  done

  return 1
}

mkdir -p "$OUT_ROOT"

shopt -s nullglob
SPEC_FILES=("$DIR"/*_spec.json)
if [[ ${#SPEC_FILES[@]} -eq 0 ]]; then
  echo "No *_spec.json files found in $DIR" >&2
  exit 1
fi

for SPEC_JSON in "${SPEC_FILES[@]}"; do
  if CSV_PATH=$(resolve_csv_for_spec "$SPEC_JSON"); then
    run_build "$SPEC_JSON" "$CSV_PATH"
  else
    echo "[warn] CSV not found for spec: $(basename "$SPEC_JSON"). Skipping." >&2
  fi
done

patch_contexts() {
  echo "[patch] normalizing context.py to support PROBLEM_DIR fallback"
  shopt -s nullglob
  for ctx in "$OUT_ROOT"/*/context.py; do
    echo "  - patching $(basename "$(dirname "$ctx")")/context.py"
    uv_master_api python - "$ctx" << 'PY'
import sys, re
from pathlib import Path
import io

p = Path(sys.argv[1])
s = p.read_text()

# If already migrated templates (DATASET_BASE present), skip modifications
if 'DATASET_BASE =' in s:
    p.write_text(s)
    sys.exit(0)

# Ensure `import os` exists right after pathlib import
if 'import os' not in s:
    s = s.replace('from pathlib import Path', 'from pathlib import Path\nimport os')

# Replace DATASET_PATH definition with robust PROBLEM_DIR-aware version
pattern = r'^DATASET_PATH\s*=.*$'
replacement = (
    'DATASET_BASE = Path(os.getenv("PROBLEM_DIR") or '
    'Path(globals().get("__file__", ".")).resolve().parent)\n'
    'DATASET_PATH = DATASET_BASE / "dataset" / "data.csv"'
)
s = re.sub(pattern, replacement, s, flags=re.M)

p.write_text(s)
PY
  done
}

patch_contexts

echo "✓ All problems created and patched in $OUT_ROOT"

