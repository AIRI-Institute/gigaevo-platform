# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS core-builder

ARG GIGAEVO_CORE_REF=main
ARG GIGAEVO_CORE_REPO_URL=https://github.com/FusionBrainLab/gigaevo-core
ARG GIGAEVO_CORE_STAGE_TIMEOUT=3600

WORKDIR /opt

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    ca-certificates \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=secret,id=github_pat,required=false \
    set -eu; \
    CLONE_URL="${GIGAEVO_CORE_REPO_URL}"; \
    if [ -s /run/secrets/github_pat ]; then \
      PAT="$(cat /run/secrets/github_pat)"; \
      CLONE_URL="https://x-access-token:${PAT}@${GIGAEVO_CORE_REPO_URL#https://}"; \
    fi; \
    git clone "${CLONE_URL}" /opt/gigaevo-core; \
    unset PAT CLONE_URL || true; \
    cd /opt/gigaevo-core; \
    git checkout --force "${GIGAEVO_CORE_REF}"; \
    git remote set-url origin "${GIGAEVO_CORE_REPO_URL}"

RUN python -m venv /opt/gigaevo-core/.venv
ENV PATH=/opt/gigaevo-core/.venv/bin:${PATH}

RUN python -m pip install --upgrade pip && pip install --no-cache-dir uv

RUN set -eux; \
    cd /opt/gigaevo-core; \
    if [ -f pyproject.toml ]; then \
      uv pip install --python /opt/gigaevo-core/.venv/bin/python .; \
    elif [ -f requirements.txt ]; then \
      uv pip install --python /opt/gigaevo-core/.venv/bin/python -r requirements.txt; \
    else \
      echo "No pyproject.toml or requirements.txt found in gigaevo-core" >&2; \
      exit 1; \
    fi; \
    uv pip install --python /opt/gigaevo-core/.venv/bin/python \
      gigaevo-memory \
      scikit-learn \
      lightautoml \
      catboost \
      evaluate \
      rouge_score \
      bert_score \
      "mmar-carl>=0.1.0" \
      "httpx>=0.25.0"; \
    PY=/opt/gigaevo-core/.venv/bin/python; \
    uv pip install --python "$PY" "statsmodels==0.14.6"; \
    "$PY" -m pip --disable-pip-version-check check || true

RUN GIGAEVO_CORE_STAGE_TIMEOUT="${GIGAEVO_CORE_STAGE_TIMEOUT}" python - <<'PY'
import os
import re
from pathlib import Path

clone_path = Path("/opt/gigaevo-core")
timeout = int(os.environ.get("GIGAEVO_CORE_STAGE_TIMEOUT", "3600") or "3600")

constants_file = clone_path / "gigaevo" / "entrypoint" / "constants.py"
if constants_file.exists():
    content = constants_file.read_text(encoding="utf-8")
    patched = re.sub(r"DEFAULT_STAGE_TIMEOUT\s*=\s*\d+", f"DEFAULT_STAGE_TIMEOUT = {timeout}", content)
    patched = re.sub(r"DEFAULT_DAG_TIMEOUT\s*=\s*\d+", f"DEFAULT_DAG_TIMEOUT = {timeout}", patched)
    constants_file.write_text(patched, encoding="utf-8")

exec_runner_path = clone_path / "tools" / "exec_runner.py"
if exec_runner_path.exists():
    raw = exec_runner_path.read_text(encoding="utf-8")
    if "mod.__dict__['__file__']" not in raw and 'mod.__dict__["__file__"]' not in raw:
        pattern = re.compile(r"(\n\s*)(exec\(code_obj,\s*mod\.__dict__\))")
        match = pattern.search(raw)
        if match:
            indent = match.group(1)
            injection = "\n".join(
                [
                    f"{indent}_os = __import__('os')",
                    f"{indent}from pathlib import Path as _Path",
                    (
                        f"{indent}mod.__dict__['__file__'] = "
                        "str(_Path(_os.environ.get('PROBLEM_DIR') or _os.getcwd()) / 'user_code.py')"
                    ),
                    f"{indent}{match.group(2)}",
                ]
            )
            patched = raw[: match.start()] + injection + raw[match.end() :]
            exec_runner_path.write_text(patched, encoding="utf-8")
PY

RUN set -eux; \
    cd /opt/gigaevo-core; \
    CORE_COMMIT="$(git rev-parse HEAD)"; \
    CORE_BRANCH="$(git rev-parse --abbrev-ref HEAD || true)"; \
    CORE_REMOTE="$(git config --get remote.origin.url || true)"; \
    CORE_REF="${GIGAEVO_CORE_REF}"; \
    CORE_COMMIT="${CORE_COMMIT}" CORE_BRANCH="${CORE_BRANCH}" CORE_REMOTE="${CORE_REMOTE}" CORE_REF="${CORE_REF}" \
      python - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

payload = {
    "schema": 1,
    "core_ref": os.environ.get("CORE_REF", ""),
    "core_commit": os.environ.get("CORE_COMMIT", ""),
    "core_branch": os.environ.get("CORE_BRANCH", ""),
    "core_repo_url": os.environ.get("CORE_REMOTE", ""),
    "built_at_utc": datetime.now(timezone.utc).isoformat(),
}
Path("/opt/gigaevo-core/.buildinfo.json").write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
PY

RUN rm -rf /opt/gigaevo-core/.git


FROM python:3.12-slim AS runner-base

WORKDIR /app

ENV PYTHONPATH=/app
ENV GIGAVOLVE__CLONE_PATH=/opt/gigaevo-core

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    curl \
    ca-certificates \
    gosu \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

COPY runner_api/pyproject.toml ./pyproject.toml
COPY runner_api/uv.lock ./uv.lock

# Install dependencies before copying source so code edits don't invalidate them.
RUN uv sync --frozen --no-install-project --no-cache

COPY runner_api/src/ ./src/
COPY common/ ./common/
# Ship the root pyproject.toml beside common/ so common.version can resolve
# the single-source version at runtime (the package itself isn't installed).
COPY pyproject.toml ./common/pyproject.toml

COPY --from=core-builder /opt/gigaevo-core /opt/gigaevo-core
COPY runner_api/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh && \
    useradd -m -u 1000 gigaevouser && \
    chown -R gigaevouser:gigaevouser /app /opt/gigaevo-core

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1


FROM runner-base AS dev

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8001", "--reload"]


FROM runner-base AS prod

CMD ["uv", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8001"]
