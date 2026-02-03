#!/usr/bin/env python3

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from common.llm_registry import get_default_llm_model_id, get_default_prompt_llm_model_id, get_llm_runtime_required
from redis.asyncio import Redis

from ..config import load_config

logger = logging.getLogger(__name__)


REPO_EXTRA_PIP_PACKAGES = [
    "scikit-learn",
    "lightautoml",
    "catboost",
    "evaluate",
    "rouge_score",
    "bert_score",
    "mmar-carl>=0.0.13",
    "httpx>=0.25.0",
]


def _extract_prompt_template(program_code: str) -> Optional[str]:
    """Extract PROMPT_TEMPLATE content from program code.

    Returns the extracted prompt template string, or None if extraction fails
    (indicating this is not a prompt experiment).
    """
    if not program_code:
        return None

    # Match PROMPT_TEMPLATE: str = """...""" or PROMPT_TEMPLATE = """..."""
    # Supports with/without type hint, triple/single quotes
    pattern = re.compile(
        r"PROMPT_TEMPLATE\s*(?::\s*str\s*)?=\s*(?P<q>\"\"\"|'''|\"|')(?P<body>.*?)(?P=q)",
        re.DOTALL,
    )
    m = pattern.search(program_code)
    if m:
        # Unescape escaped triple quotes from baseline creation
        body = m.group("body")
        body = body.replace('\\"\\"\\"', '"""').replace("\\'\\'\\'", "'''")
        return body
    return None


class GigaEvolveService:
    def __init__(self):
        cfg = load_config()
        self.config = cfg.gigavolve
        self.extras = cfg.extras
        self._ensure_repos_directory()

    def _ensure_prompt_layout_present(self, clone_path: Path) -> None:
        """
        Ensure gigaevo/problems/prompt_layout.py exists inside the checked-out repo.

        If missing, we try to copy it from:
        - extras.prompt_layout_source (EXTRAS__PROMPT_LAYOUT_SOURCE)
        - /app/master_api/src/folder_constructor/prompt_layout.py (Docker)
        - <repo_root>/master_api/src/folder_constructor/prompt_layout.py (local dev)
        """
        dest = clone_path / "gigaevo" / "problems" / "prompt_layout.py"
        if dest.exists():
            return

        repo_root = Path(__file__).resolve().parents[3]
        candidates = [
            self.extras.prompt_layout_source,
            "/app/master_api/src/folder_constructor/prompt_layout.py",
            str(repo_root / "master_api" / "src" / "folder_constructor" / "prompt_layout.py"),
        ]

        src_path: Optional[str] = None
        for c in candidates:
            if not c:
                continue
            p = Path(c)
            if p.exists():
                src_path = c
                break

        if not src_path:
            tried = ", ".join([str(c) for c in candidates if c])
            raise FileNotFoundError(
                f"Required prompt_layout.py is missing in repo (expected at {dest}) and no source was found. "
                f"Tried: {tried}"
            )

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_path, dest)
            logger.info("Copied prompt_layout.py from %s to %s", src_path, str(dest))
        except Exception as e:
            raise RuntimeError(f"Failed to copy prompt_layout.py from {src_path} to {dest}: {e}") from e

        if not dest.exists():
            raise RuntimeError(f"prompt_layout.py copy completed but file is still missing at {dest}")

    def _resolve_python_path(self, clone_path: Path) -> str:
        """Prefer repo-local venv python if present; otherwise fall back to configured python_path."""
        venv_python = clone_path / ".venv" / "bin" / "python"
        return str(venv_python) if venv_python.exists() else self.config.python_path

    @staticmethod
    def _normalize_problem_name(experiment_id: str) -> str:
        """Normalize experiment id into problem.name (used for Redis keys)."""
        return experiment_id.replace("exp_", "") if experiment_id.startswith("exp_") else experiment_id

    @staticmethod
    def _env_with_problem_dir(problem_dir: Path, *, base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """
        Return env with PROBLEM_DIR set and PYTHONPATH prepended with problem dir (for helper imports).
        """
        env: Dict[str, str] = dict(base_env) if base_env is not None else dict(os.environ)
        problem_dir_str = str(problem_dir.resolve())

        # Provide problem directory for exec runner to resolve __file__ when executing user code,
        # and for tools that need to import helper modules from the problem directory.
        env["PROBLEM_DIR"] = problem_dir_str

        # Only prepend PYTHONPATH if the directory exists (preserve prior behavior).
        if problem_dir.exists():
            existing_pythonpath = env.get("PYTHONPATH", "")
            if existing_pythonpath:
                env["PYTHONPATH"] = f"{problem_dir_str}{os.pathsep}{existing_pythonpath}"
            else:
                env["PYTHONPATH"] = problem_dir_str
        return env

    def _redis_host_port_db(self) -> tuple[str, int, int]:
        """Resolve redis host/port/db from config (supports redis_url or legacy config)."""
        cfg = load_config()
        candidate_url = (cfg.gigavolve.redis_url or cfg.redis.url or "").strip()
        return self._parse_redis_url(candidate_url)

    def _patch_stage_timeout(self, clone_path: Path) -> None:
        """Patch DEFAULT_STAGE_TIMEOUT and DEFAULT_DAG_TIMEOUT in constants.py to 3600."""
        try:
            constants_file = clone_path / "gigaevo" / "entrypoint" / "constants.py"
            if not constants_file.exists():
                logger.warning(f"constants.py not found at {constants_file}, skipping timeout patch")
                return

            content = constants_file.read_text(encoding="utf-8")
            # Replace DEFAULT_STAGE_TIMEOUT = <any number> with DEFAULT_STAGE_TIMEOUT = 3600
            patched = re.sub(
                r"DEFAULT_STAGE_TIMEOUT\s*=\s*\d+",
                "DEFAULT_STAGE_TIMEOUT = 3600",
                content,
            )
            # Replace DEFAULT_DAG_TIMEOUT = <any number> with DEFAULT_DAG_TIMEOUT = 3600
            patched = re.sub(
                r"DEFAULT_DAG_TIMEOUT\s*=\s*\d+",
                "DEFAULT_DAG_TIMEOUT = 3600",
                patched,
            )
            if patched != content:
                constants_file.write_text(patched, encoding="utf-8")
                logger.info(f"✓ Patched timeouts to 3600 in {constants_file}")
            else:
                logger.debug(f"Timeouts already set correctly in {constants_file}")
        except Exception as e:
            logger.warning(f"Failed to patch timeouts: {e}")

    @staticmethod
    def _sanitize_url(url: str) -> str:
        """Remove invisible/format control chars and normalize the URL."""
        try:
            norm = unicodedata.normalize("NFKC", url)
            cleaned = "".join(ch for ch in norm if unicodedata.category(ch) not in {"Cf", "Cc", "Cs"})
            return cleaned.strip()
        except Exception:
            return (url or "").strip()

    def _ensure_repos_directory(self):
        """Ensure the repos directory exists"""
        repos_dir = Path(self.config.clone_path).parent
        repos_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Ensured repos directory exists at: {repos_dir}")

    @staticmethod
    def _redact(text: str) -> str:
        """Redact sensitive tokens from logs."""
        try:
            redacted = re.sub(r"(Authorization:\s*Bearer\s+)[^\s)]+", r"\1[REDACTED]", text)
            redacted = re.sub(r"(x-access-token:)[^@]+@", r"\1[REDACTED]@", redacted)
            return redacted
        except Exception:
            return text

    def _get_pat(self) -> str:
        """Get required GitHub PAT or raise."""
        pat = (os.getenv("GITHUB_PAT") or self.config.github_pat or "").strip()
        if not pat:
            raise RuntimeError("GitHub PAT is required but not configured (env:GITHUB_PAT or config.github_pat)")
        return pat

    def _build_token_url(self, base_url: str, pat: str) -> str:
        """Build https token-in-URL using x-access-token user."""
        try:
            parts = urlsplit(base_url)
            token_netloc = f"x-access-token:{pat}@{(parts.netloc or '')}"
            return urlunsplit((parts.scheme, token_netloc, parts.path, parts.query, parts.fragment))
        except Exception:
            return f"https://x-access-token:{pat}@{base_url.split('://', 1)[-1]}"

    def _git_clone_with_token(self, repo_url: str, dest_path: Path, pat: str) -> None:
        token_url = self._build_token_url(repo_url, pat)
        self._run_git_command(["git", "clone", token_url, str(dest_path)])

    def _git_fetch_tags_with_token(self, repo_url: str, cwd: Path, pat: str) -> None:
        token_url = self._build_token_url(repo_url, pat)
        self._run_git_command(["git", "fetch", token_url, "--tags"], cwd)

    def _git_checkout_ref(self, cwd: Path, ref: str) -> None:
        try:
            self._run_git_command(["git", "checkout", "--force", ref], cwd)
        except subprocess.CalledProcessError:
            self._run_git_command(["git", "checkout", "--force", f"refs/tags/{ref}"], cwd)

    def _clone_repository_blocking(self, force_refresh: bool = False) -> bool:
        """
        Clone/update the GigaEvolve repository (blocking).

        NOTE: This function performs blocking filesystem and subprocess operations.
        It must NOT run on the event loop thread.
        """
        try:
            clone_path = Path(self.config.clone_path).resolve()

            # If clone_path points to an existing directory that looks like a valid repo,
            # and it's not a git repo but has run.py, treat it as a local development repo
            if clone_path.exists() and clone_path.is_dir():
                run_script = clone_path / "run.py"
                if run_script.exists():
                    # Check if it's a git repo or a local development directory
                    git_dir = clone_path / ".git"
                    if not git_dir.exists():
                        logger.info(
                            f"Using local development repository at {clone_path} (not a git repo, but has run.py)"
                        )
                        # Patch stage timeout to match pipeline.yaml
                        self._patch_stage_timeout(clone_path)
                        return True
                    else:
                        # It's a git repo, continue with normal flow
                        logger.info(f"Found git repository at {clone_path}, proceeding with normal update flow")

            # Start fresh if requested or directory is not a valid repo
            if clone_path.exists():
                if force_refresh or not (clone_path / ".git").exists():
                    logger.info(f"Removing existing directory at {clone_path}")
                    subprocess.run(["rm", "-rf", str(clone_path)], check=True)
                else:
                    # Repo exists: update and optionally checkout ref with mandatory PAT
                    logger.info(f"Repository already exists at {clone_path}")
                    repo_url = self._sanitize_url(self.config.repo_url or "")
                    pat = self._get_pat()
                    ref = (self.config.repo_ref or "").strip()
                    try:
                        self._git_fetch_tags_with_token(repo_url, clone_path, pat)
                        if ref:
                            self._git_checkout_ref(clone_path, ref)
                            logger.info(f"Checked out ref {ref}")
                        else:
                            logger.info("No repo_ref configured; leaving current checkout as-is")
                        try:
                            self._install_repo_dependencies(clone_path)
                        except Exception as dep_err:
                            logger.warning(f"Dependency installation after pull/checkout failed: {dep_err}")
                        # Patch stage timeout to match pipeline.yaml
                        self._patch_stage_timeout(clone_path)
                        return True
                    except subprocess.CalledProcessError as e:
                        logger.warning(f"Failed to update existing repo, will reclone: {self._redact(str(e))}")
                        subprocess.run(["rm", "-rf", str(clone_path)], check=True)

            # Clone the repository
            # Normalize and sanitize URL (strip hidden/invisible characters)
            repo_url = self._sanitize_url(self.config.repo_url or "")
            logger.info(f"Cloning repository from {repo_url} to {clone_path}")

            # Ensure parent directory exists
            clone_path.parent.mkdir(parents=True, exist_ok=True)

            # Mandatory PAT-only token-in-URL clone
            pat = self._get_pat()
            result = self._run_git_command(["git", "clone", self._build_token_url(repo_url, pat), str(clone_path)])
            logger.info(
                f"Successfully cloned repository: {result.stdout if hasattr(result, 'stdout') else 'Clone completed'}"
            )

            # Verify the clone was successful
            if not clone_path.exists():
                raise RuntimeError("Repository clone completed but directory not found")

            git_dir = clone_path / ".git"
            if not git_dir.exists():
                raise RuntimeError("Cloned directory is not a valid git repository")

            # Configure git user in the cloned repository
            if self.config.git_user_name and self.config.git_user_email:
                self._run_git_command(["git", "config", "user.name", self.config.git_user_name], clone_path)
                self._run_git_command(["git", "config", "user.email", self.config.git_user_email], clone_path)
                logger.info("Configured git user in cloned repository")

            # If a specific ref is configured, fetch tags and checkout
            try:
                ref = (self.config.repo_ref or "").strip()
                if ref:
                    self._git_fetch_tags_with_token(repo_url, clone_path, pat)
                    self._git_checkout_ref(clone_path, ref)
                    logger.info(f"Checked out ref {ref}")
            except subprocess.CalledProcessError as e:
                logger.warning(f"Failed to checkout ref {ref}: {self._redact(str(e))}")

            # Install repository dependencies
            try:
                self._install_repo_dependencies(clone_path)
            except Exception as dep_err:
                logger.warning(f"Dependency installation after clone failed: {dep_err}")

            # Patch stage timeout to match pipeline.yaml
            self._patch_stage_timeout(clone_path)

            return True

        except (FileNotFoundError, RuntimeError) as e:
            # Fatal requirement: prompt_layout.py must be present
            logger.error(str(e))
            raise
        except subprocess.CalledProcessError as e:
            logger.error("Failed to clone repository")
            if hasattr(e, "stderr") and e.stderr:
                logger.error(f"Git stderr: {self._redact(e.stderr)}")

            # If authentication failed, create a mock repository for development
            if (
                hasattr(e, "stderr")
                and e.stderr
                and ("could not read Username" in str(e.stderr) or "Authentication failed" in str(e.stderr))
            ):
                logger.error("Repository requires authentication.!")

            return False

    async def clone_repository(self, force_refresh: bool = False) -> bool:
        """
        Clone/update the GigaEvolve repository without blocking the event loop.

        The underlying work is executed in a worker thread because it uses
        subprocess/file operations extensively.
        """
        return await asyncio.to_thread(self._clone_repository_blocking, force_refresh)

    async def _configure_git_auth(self):
        """Configure git authentication"""
        if self.config.github_pat:
            # Configure git to use PAT for HTTPS authentication
            try:
                self._run_git_command(["git", "config", "--global", "credential.helper", "store"])

                # Use subprocess to write to git credentials file
                _ = subprocess.run(
                    ["git", "config", "--global", "credential.helper"], capture_output=True, text=True, check=True
                )

                logger.info("Configured git authentication with GitHub PAT")
            except subprocess.CalledProcessError as e:
                logger.warning(f"Failed to configure git authentication: {e}")

    def _run_git_command(self, cmd: list, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
        """Run a git command with proper error handling"""
        try:
            env = os.environ.copy()
            if self.config.github_pat:
                # Set GIT_ASKPASS to avoid interactive prompts
                env["GIT_ASKPASS"] = "echo"
                env["GIT_TERMINAL_PROMPT"] = "0"

            result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True, env=env)
            return result
        except subprocess.CalledProcessError as e:
            # Add more context to the error
            e.cmd = cmd
            e.cwd = str(cwd) if cwd else None
            raise

    def _install_repo_dependencies(self, clone_path: Path) -> bool:
        """Install repository dependencies into a dedicated venv under the repo.

        Steps:
        - Create venv at <clone_path>/.venv if missing
        - Use that venv's pip to install project deps (pyproject or requirements)
        - Point python_path to that venv's python for subsequent runs
        """
        try:
            pyproject = clone_path / "pyproject.toml"
            requirements = clone_path / "requirements.txt"

            venv_dir = clone_path / ".venv"
            venv_python = venv_dir / "bin" / "python"
            venv_pip = venv_dir / "bin" / "pip"
            deps_marker = venv_dir / ".deps_installed"

            def _deps_fingerprint() -> Optional[str]:
                """Quick fingerprint for requirements + our extra dependencies."""
                spec_bytes: Optional[bytes] = None
                if pyproject.exists():
                    spec_bytes = pyproject.read_bytes()
                elif requirements.exists():
                    spec_bytes = requirements.read_bytes()
                else:
                    return None

                h = hashlib.sha256()
                h.update(spec_bytes)
                h.update(b"\n--extra--\n")
                h.update("\n".join(REPO_EXTRA_PIP_PACKAGES).encode("utf-8"))
                return h.hexdigest()

            # Reuse venv only if marker fingerprint matches current deps spec.
            if venv_python.exists() and venv_pip.exists() and deps_marker.exists():
                expected_fp = _deps_fingerprint()
                if expected_fp:
                    try:
                        marker_obj = json.loads(deps_marker.read_text(encoding="utf-8"))
                        marker_fp = marker_obj.get("fingerprint")
                    except Exception:
                        marker_fp = None

                    if marker_fp == expected_fp:
                        self.config.python_path = str(venv_python)
                        logger.info(f"Reusing existing repo venv (deps marker matches) at {venv_dir}")
                        return True

            # Create venv only if missing (re-creating it will blow away deps and cause churn).
            if not venv_python.exists() or not venv_pip.exists():
                subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True, text=True)

            if venv_dir.exists():
                logger.info(f"Created virtualenv at {venv_dir}")
            else:
                logger.error(f"Failed to create virtualenv at {venv_dir}")
                return False

            if pyproject.exists():
                # Install project (non-editable to minimize file churn)
                subprocess.run([str(venv_pip), "install", "."], cwd=str(clone_path), check=True, text=True)
                subprocess.run(
                    [str(venv_pip), "install", *REPO_EXTRA_PIP_PACKAGES],
                    cwd=str(clone_path),
                    check=True,
                    text=True,
                )
                logger.info("Installed repo dependencies into dedicated venv (pyproject)")
            elif requirements.exists():
                subprocess.run(
                    [str(venv_pip), "install", "-r", "requirements.txt"], cwd=str(clone_path), check=True, text=True
                )
                subprocess.run(
                    [str(venv_pip), "install", *REPO_EXTRA_PIP_PACKAGES],
                    cwd=str(clone_path),
                    check=True,
                    text=True,
                )
                logger.info("Installed repo requirements into dedicated venv")
            else:
                logger.info("No pyproject.toml or requirements.txt found; skipping dependency install")
                return False

            # Strict verification: ensure all installed requirements are consistent.
            subprocess.run([str(venv_python), "-m", "pip", "check"], cwd=str(clone_path), check=True, text=True)

            # Write a minimal "cached proof" marker atomically: fingerprint + schema.
            fp = _deps_fingerprint()
            if not fp:
                logger.warning("Failed to compute deps fingerprint; refusing to mark deps as installed")
                return False
            payload = {"schema": 1, "fingerprint": fp}
            tmp = deps_marker.with_suffix(deps_marker.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
            os.replace(tmp, deps_marker)
            logger.info(f"Wrote deps marker at {deps_marker}")

            # Point service to use the venv's python for running experiments
            self.config.python_path = str(venv_python)
            return True
        except subprocess.CalledProcessError as e:
            logger.warning(f"Dependency installation failed: {e}")
            if e.stderr:
                logger.debug(f"installer stderr: {e.stderr}")
            return False
        except Exception as e:
            logger.warning(f"Unexpected error during dependency installation: {e}")
            return False

    def is_repository_ready(self) -> bool:
        """Check if the repository is ready for use.

        We rely on a small "cached proof" marker written only after a
        successful install + `pip check`, and validate it against a fingerprint of the
        current dependency spec.
        """
        try:
            clone_path = Path(self.config.clone_path).resolve()
            if not clone_path.exists():
                return False

            # Check if it's a git repository
            git_dir = clone_path / ".git"
            if git_dir.exists():
                pyproject = clone_path / "pyproject.toml"
                requirements = clone_path / "requirements.txt"

                venv_dir = clone_path / ".venv"
                venv_python = venv_dir / "bin" / "python"
                venv_pip = venv_dir / "bin" / "pip"
                deps_marker = venv_dir / ".deps_installed"

                if not (venv_python.exists() and venv_pip.exists() and deps_marker.exists()):
                    return False

                def _expected_fingerprint() -> Optional[str]:
                    spec_bytes: Optional[bytes] = None
                    if pyproject.exists():
                        spec_bytes = pyproject.read_bytes()
                    elif requirements.exists():
                        spec_bytes = requirements.read_bytes()
                    else:
                        return None

                    h = hashlib.sha256()
                    h.update(spec_bytes)
                    h.update(b"\n--extra--\n")
                    h.update("\n".join(REPO_EXTRA_PIP_PACKAGES).encode("utf-8"))
                    return h.hexdigest()

                try:
                    marker_obj = json.loads(deps_marker.read_text(encoding="utf-8"))
                    marker_fp = marker_obj.get("fingerprint")
                except Exception:
                    return False

                expected_fp = _expected_fingerprint()
                if not expected_fp or marker_fp != expected_fp:
                    return False

                # Check if we can run git commands as a final sanity check.
                try:
                    subprocess.run(["git", "status"], cwd=clone_path, capture_output=True, text=True, check=True)
                    return True
                except subprocess.CalledProcessError as e:
                    logger.warning(f"'git status' failed for repo at {clone_path}: {e}")
                    return False
            else:
                # Check if it's a mock repository (has run_experiment.py)
                experiment_script = clone_path / "run_experiment.py"
                return experiment_script.exists()

        except Exception as e:
            logger.error(f"Error checking repository status: {e}")
            return False

    async def get_repository_info(self) -> Dict[str, Any]:
        """Get information about the cloned repository"""
        try:
            clone_path = Path(self.config.clone_path).resolve()

            if not self.is_repository_ready():
                return {"error": "Repository not ready"}

            # Check if it's a mock repository
            git_dir = clone_path / ".git"
            if not git_dir.exists():
                return {
                    "path": str(clone_path),
                    "tag": "mock",
                    "commit_hash": "mock",
                    "remote_url": self.config.repo_url,
                    "branch": "mock",
                    "is_ready": True,
                    "is_mock": True,
                }

            # Get current commit hash
            result = self._run_git_command(["git", "rev-parse", "HEAD"], clone_path)
            commit_hash = result.stdout.strip()

            # Try to get exact tag for current commit
            tag = None
            try:
                result = self._run_git_command(["git", "describe", "--tags", "--exact-match"], clone_path)
                tag = result.stdout.strip() if result.stdout else None
            except subprocess.CalledProcessError:
                tag = None

            # Get remote URL
            result = self._run_git_command(["git", "config", "--get", "remote.origin.url"], clone_path)
            remote_url = result.stdout.strip()
            # Redact any credentials embedded in the URL before returning
            try:
                parts = urlsplit(remote_url)
                if parts.username or "@" in parts.netloc:
                    # Strip userinfo if present
                    netloc = parts.hostname or ""
                    if parts.port:
                        netloc = f"{netloc}:{parts.port}"
                    remote_url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
            except Exception:
                # Best-effort redaction; if parsing fails, fall back to original string
                pass

            # Get current branch
            result = self._run_git_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], clone_path)
            branch = result.stdout.strip()

            return {
                "path": str(clone_path),
                "tag": tag,
                "commit_hash": commit_hash,
                "remote_url": remote_url,
                "branch": branch,
                "is_ready": True,
                "is_mock": False,
            }

        except Exception as e:
            logger.error(f"Error getting repository info: {e}")
            return {"error": str(e)}

    def _write_llm_config_for_model(self, clone_path: Path, model_id: str) -> None:
        """Write Hydra LLM config with runtime values from llm_models.yml for the selected model."""
        llm_dir = clone_path / "config" / "llm"
        llm_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = llm_dir / "custom.yaml"

        runtime = get_llm_runtime_required(
            model_id,
            required_keys={
                "model",
                "api_key",
                "base_url",
                "temperature",
                "max_tokens",
                "top_p",
                "max_retries",
                "timeout",
            },
        )

        payload = {
            "llm": {
                "_target_": "gigaevo.llm.models.MultiModelRouter",
                "_convert_": "all",
                "models": [
                    {
                        "_target_": "langchain_openai.ChatOpenAI",
                        "model": runtime.get("model"),
                        "api_key": runtime.get("api_key"),
                        "temperature": float(runtime.get("temperature", 0.7)),
                        "max_tokens": int(runtime.get("max_tokens", 2048)),
                        "top_p": float(runtime.get("top_p", 1.0)),
                        "base_url": runtime.get("base_url"),
                        "request_timeout": int(runtime.get("request_timeout", 600)),
                        "max_retries": int(runtime.get("max_retries", 3)),
                        "timeout": int(runtime.get("timeout", 600)),
                    }
                ],
                "probabilities": [1.0],
                # TODO: Re-enable when writer param is supported by gigaevo-core
                # "writer": "${writer}",
            }
        }

        import yaml  # type: ignore

        text = "# @package _global_\n\n" + yaml.safe_dump(payload, sort_keys=False)
        cfg_path.write_text(text, encoding="utf-8")

    async def run_experiment(
        self,
        experiment_id: str,
        config: Dict[str, Any],
        cancel_check: Optional[Callable[[], Awaitable[bool]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Run a GigaEvolve experiment with timeout and optional cancel-aware execution."""
        clone_path = Path(self.config.clone_path).resolve()
        try:
            # Ensure exec runner supports __file__ for dynamic code
            try:
                self._patch_exec_runner_support(clone_path)
            except Exception as _patch_err:
                logger.debug(f"exec_runner patch skipped/failed: {_patch_err}")

            run_script = Path(clone_path) / "run.py"
            if not run_script.exists():
                raise FileNotFoundError("Run script not found")

            problem_dir = clone_path / "problems" / str(experiment_id)

            python_path = self._resolve_python_path(clone_path)

            logger.info(f"Using Python path: {python_path}")

            cfg = load_config()

            # Always create a patched version to fix imports and add SSL bypass (matching original run.py)
            script_to_run = run_script
            try:
                raw = run_script.read_text()
                original_raw = raw
                needs_patch = False

                # Fix import: gigaevo.runner.runner -> gigaevo.runner.dag_runner
                # Replace RunnerConfig -> DagRunnerConfig, RunnerManager -> DagRunner
                if "from gigaevo.runner.runner import" in raw:
                    raw = raw.replace(
                        "from gigaevo.runner.runner import RunnerConfig, RunnerManager",
                        "from gigaevo.runner.dag_runner import DagRunnerConfig as RunnerConfig, DagRunner as RunnerManager",
                    )
                    needs_patch = True
                    logger.info("Fixed import: gigaevo.runner.runner -> gigaevo.runner.dag_runner")

                # Add SSL verification bypass logic (matching original run.py)
                # This checks LLM_SSL_NO_VERIFY or PROMPT_SSL_NO_VERIFY environment variables.
                # NOTE: do not gate on "import ssl" presence; run.py may already import ssl.
                if "ssl._create_default_https_context" not in raw:
                    # Find the main() function and add SSL bypass before load_dotenv()
                    ssl_bypass = (
                        "    # Optional: Dev-only SSL verification bypass for local/self-signed HTTPS endpoints.\n"
                        "    # Enable via env: LLM_SSL_NO_VERIFY=1 (or reuse PROMPT_SSL_NO_VERIFY=1)\n"
                        "    try:\n"
                        "        import os\n"
                        '        _ssl_flag = (os.getenv("LLM_SSL_NO_VERIFY") or os.getenv("PROMPT_SSL_NO_VERIFY") or "0").strip().lower()\n'
                        '        if _ssl_flag in {"1", "true", "yes"}:\n'
                        "            ssl._create_default_https_context = ssl._create_unverified_context  # type: ignore[attr-defined]\n"
                        "            _orig_create_default_context = ssl.create_default_context  # type: ignore[assignment]\n"
                        "\n"
                        "            def _insecure_ctx(*args, **kwargs):\n"
                        "                ctx = _orig_create_default_context(*args, **kwargs)\n"
                        "                try:\n"
                        "                    ctx.check_hostname = False\n"
                        "                    ctx.verify_mode = ssl.CERT_NONE\n"
                        "                except Exception:\n"
                        "                    pass\n"
                        "                return ctx\n"
                        "\n"
                        "            ssl.create_default_context = _insecure_ctx  # type: ignore[assignment]\n"
                        '            logger.warning("SSL verification disabled (dev): using unverified HTTPS context for LLM requests.")\n'
                        "    except Exception as _e:\n"
                        '        logger.warning(f"Failed to apply SSL bypass setting: {_e}")\n'
                    )
                    # Add import ssl if not present
                    if "import ssl" not in raw:
                        # Find the first import statement and add ssl after it
                        import_match = None
                        for line in raw.split("\n"):
                            if line.strip().startswith("import ") and "ssl" not in line:
                                import_match = line
                                break
                        if import_match:
                            # Add import ssl after the first import
                            raw = raw.replace(import_match, import_match + "\nimport ssl", 1)
                            needs_patch = True

                    # Add SSL bypass logic in main() function after load_dotenv()
                    if "def main(" in raw and "load_dotenv()" in raw:
                        # Insert SSL bypass after load_dotenv()
                        raw = raw.replace("    load_dotenv()\n", f"    load_dotenv()\n\n{ssl_bypass}\n")
                        needs_patch = True
                        logger.info("Added SSL bypass logic (matching original run.py)")

                # Write patched version if any changes were made
                if needs_patch:
                    patched_path = clone_path / "run_patched.py"
                    patched_path.write_text(raw)
                    script_to_run = patched_path
                    logger.info(f"Created patched run script at {patched_path}")
            except Exception as patch_err:
                logger.warning(f"Failed to patch run.py: {patch_err}. Proceeding with original script.")

            # Build command arguments: run.py uses Hydra, not argparse
            # Hydra overrides use format: param=value or group.param=value
            # Extract problem name from experiment_id (remove 'exp_' prefix if present)
            # CRITICAL: run.py uses problem.name for Redis keys, NOT the directory name.
            # For prompt experiments with IDs like "exp_<uuid>", we must pass the name WITHOUT
            # the "exp_" prefix so Redis keys match between storage and retrieval.
            problem_name = self._normalize_problem_name(experiment_id)

            cmd = [
                python_path,
                str(script_to_run),
                f"problem.name={problem_name}",  # Used by run.py for Redis keys
                f"problem.dir={problem_dir}",  # Full path with exp_ prefix for file system
            ]

            # Add Redis configuration as Hydra overrides
            if cfg.gigavolve.redis_url:
                # Parse Redis URL: redis://host:port/db
                parsed = urlsplit(cfg.gigavolve.redis_url)
                redis_host = parsed.hostname or "localhost"
                redis_port = parsed.port or 6379
                redis_db = int(parsed.path.lstrip("/")) if parsed.path else 0
                cmd.extend(
                    [
                        f"redis.host={redis_host}",
                        f"redis.port={redis_port}",
                        f"redis.db={redis_db}",
                    ]
                )
            else:
                # Fallback to individual Redis settings if URL not provided
                redis_host = getattr(cfg.gigavolve, "redis_host", "localhost")
                redis_port = getattr(cfg.gigavolve, "redis_port", 6379)
                redis_db = getattr(cfg.gigavolve, "redis_db", 0)
                cmd.extend(
                    [
                        f"redis.host={redis_host}",
                        f"redis.port={redis_port}",
                        f"redis.db={redis_db}",
                    ]
                )

            # Add max generations if specified
            max_iterations = config.get("max_iterations")
            if max_iterations:
                cmd.append(f"max_generations={max_iterations}")

            # Use custom.yaml for LLM configuration (instead of default single.yaml)
            # This ensures LLM__* environment variables are used correctly
            cmd.append("llm=custom")

            # Add LLM config (if run.py supports it via argparse)
            # Note: LLM config is typically handled via config/llm/custom.yaml (already created)
            # and environment variables, not via command line

            # Prepare environment (LLM base URL and API key)
            env = self._env_with_problem_dir(problem_dir)

            # Add dataset configuration to environment variables
            dataset_size = config.get("dataset_size")
            if dataset_size is not None:
                env["DATASET_SIZE"] = str(int(dataset_size))
            test_size = config.get("test_size")
            if test_size is not None:
                env["TEST_SIZE"] = str(float(test_size))
            
            # Limit validation samples for chain experiments to speed up validation
            # Use max 2 samples for train and 1 for val to keep validation very fast
            # This significantly reduces validation time from hours to under 2 minutes
            env["CARL__MAX_TRAIN_SAMPLES"] = "2"
            env["CARL__MAX_VAL_SAMPLES"] = "1"
            # Reduce chain timeout per example to fail faster on problematic cases
            env["CARL__CHAIN_TIMEOUT"] = "15"  # 15 seconds per chain execution
            
            # Resolve selected model id from experiment config and load runtime values from llm_models.yml
            selected_model_id = str(config.get("llm_model") or "").strip() or get_default_llm_model_id()
            runtime = get_llm_runtime_required(
                selected_model_id,
                required_keys={"model", "api_key", "base_url"},
            )
            llm_base_url = str(runtime.get("base_url") or "")
            llm_api_key = str(runtime.get("api_key") or "")
            llm_model = str(runtime.get("model") or selected_model_id)
            llm_max_tokens = str(runtime.get("max_tokens", 2048))
            llm_request_timeout = str(runtime.get("request_timeout", 600.0))
            llm_max_retries = str(runtime.get("max_retries", 3))
            llm_timeout = str(runtime.get("timeout", 600.0))

            # PROMPT_* variables use a separate, independently configurable model id.
            prompt_model_id = str(config.get("prompt_llm_model") or "").strip() or get_default_prompt_llm_model_id()
            prompt_runtime = get_llm_runtime_required(
                prompt_model_id,
                required_keys={"model", "api_key", "base_url"},
            )
            prompt_base_url = str(prompt_runtime.get("base_url") or "")
            prompt_api_key = str(prompt_runtime.get("api_key") or "")
            prompt_model = str(prompt_runtime.get("model") or prompt_model_id)

            # CRITICAL: Ensure LLM config file exists before running experiment
            # This creates/updates config/llm/custom.yaml that will be used via llm=custom
            try:
                # Write per-model Hydra config (single source of truth: llm_models.yml)
                self._write_llm_config_for_model(clone_path, selected_model_id)
                cfg_path = clone_path / "config" / "llm" / "custom.yaml"
                if cfg_path.exists():
                    logger.info(f"✓ LLM config file exists at {cfg_path}")
                    logger.debug(f"LLM config file size: {cfg_path.stat().st_size} bytes")
                else:
                    logger.error(f"✗ LLM config file was not created at {cfg_path}")
            except Exception as cfg_err:
                logger.error(f"✗ Failed to ensure LLM config file before experiment run: {cfg_err}", exc_info=True)
                raise RuntimeError(f"Cannot proceed without LLM config: {cfg_err}") from cfg_err

            # Set env vars for compatibility with existing codepaths/templates (values come from llm_models.yml)
            env["LLM__BASE_URL"] = llm_base_url
            env["LLM__API_KEY"] = llm_api_key
            env["LLM__MODEL"] = llm_model
            env["LLM__MAX_TOKENS"] = llm_max_tokens
            env["LLM__REQUEST_TIMEOUT"] = llm_request_timeout
            env["LLM__MAX_RETRIES"] = llm_max_retries
            env["LLM__TIMEOUT"] = llm_timeout
            env["LLM__TEMPERATURE"] = str(runtime.get("temperature", 0.7))
            env["LLM__TOP_P"] = str(runtime.get("top_p", 1.0))

            # Also set OPENAI_API_KEY for compatibility (langchain_openai may use it)
            env["OPENAI_API_KEY"] = llm_api_key
            os.environ["OPENAI_LOG"] = "debug"

            # Set SSL bypass if configured in llm_models.yml (or via non-LLM gigavolve setting)
            if (
                bool(runtime.get("ssl_no_verify"))
                or bool(prompt_runtime.get("ssl_no_verify"))
                or cfg.gigavolve.ssl_bypass_enabled
            ):
                env["LLM_SSL_NO_VERIFY"] = "1"
                env["PROMPT_SSL_NO_VERIFY"] = "1"
                logger.info("SSL verification bypass enabled for LLM requests")

            # Provide PROMPT_* env vars for prompt templates (values come from llm_models.yml)
            env["PROMPT_MODEL_NAME"] = prompt_model
            env["PROMPT_MODEL"] = prompt_model
            env["PROMPT_BASE_URL"] = prompt_base_url
            env["PROMPT_API_KEY"] = prompt_api_key
            # Optional extras (kept for compatibility, but sourced from YAML if present)
            if "openrouter_api_key" in prompt_runtime and str(prompt_runtime.get("openrouter_api_key") or "").strip():
                env["PROMPT_OPENROUTER_API_KEY"] = str(prompt_runtime.get("openrouter_api_key"))

            logger.info(
                f"Passing LLM env vars to subprocess: BASE_URL={env.get('LLM__BASE_URL', '')[:50]}..., MODEL={env.get('LLM__MODEL', '')}, TIMEOUT={llm_timeout}, MAX_RETRIES={llm_max_retries}"
            )
            logger.debug(
                f"LLM env vars: BASE_URL={llm_base_url[:50] if llm_base_url else 'NOT SET'}..., API_KEY={'SET' if llm_api_key else 'NOT SET'}, MODEL={llm_model}"
            )

            # Run subprocess asynchronously to avoid blocking the event loop
            logger.info(f"Starting run.py for experiment {experiment_id} with command: {' '.join(cmd)}")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(clone_path),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            timeout_limit = config.get("timeout_seconds")
            if timeout_limit == 0:
                timeout_limit = None
            elif timeout_limit is None:
                max_iterations = config.get("max_iterations", 100)
                calculated_timeout = max(3600, int(max_iterations * 90 * 1.3))
                timeout_limit = calculated_timeout
                logger.info(
                    f"Auto-calculated timeout for {experiment_id}: {timeout_limit}s "
                    f"based on {max_iterations} iterations"
                )
            else:
                max_iterations = config.get("max_iterations", 100)
                min_recommended = max(3600, int(max_iterations * 90 * 1.3))
                if timeout_limit < min_recommended:
                    logger.warning(
                        f"Provided timeout {timeout_limit}s may be insufficient for {max_iterations} iterations. "
                        f"Recommended minimum: {min_recommended}s"
                    )

            # Periodically check for timeout and optional cancellation
            stdout_b = b""
            stderr_b = b""
            try:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout_limit if timeout_limit else None
                while True:
                    try:
                        # Wait in 1s slices, but do not exceed remaining timeout
                        if deadline is not None:
                            remaining = deadline - loop.time()
                            if remaining <= 0:
                                raise asyncio.TimeoutError()
                            wait_timeout = min(1, remaining)
                        else:
                            # No timeout, wait 1 second at a time
                            wait_timeout = 1
                        await asyncio.wait_for(proc.wait(), timeout=wait_timeout)
                        break
                    except asyncio.TimeoutError:
                        # Check global timeout first (only if timeout is enabled)
                        if deadline is not None and loop.time() >= deadline:
                            try:
                                proc.kill()
                            except ProcessLookupError:
                                pass
                            # Treat reaching the time limit as a failed completion
                            return {"output": "Experiment reached time limit", "success": False, "timed_out": True}
                        # If a cancel_check is provided, consult it
                        if cancel_check is not None:
                            try:
                                if await cancel_check():
                                    try:
                                        proc.kill()
                                    except ProcessLookupError:
                                        pass
                                    return {"error": "Experiment cancelled", "success": False}
                            except Exception:
                                # Ignore cancel check errors
                                pass
                # Process finished; collect remaining output
                out, err = await proc.communicate()
                stdout_b = out or b""
                stderr_b = err or b""
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                # Fallback: also treat this path as a failed completion due to timeout
                return {"output": "Experiment reached time limit", "success": False, "timed_out": True}

            stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
            stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""

            # Save full logs to file in experiment workspace
            try:
                log_dir = problem_dir / "logs"
                log_dir.mkdir(exist_ok=True)
                log_file = log_dir / "evolution.log"
                with open(log_file, "w", encoding="utf-8") as f:
                    f.write(f"=== Experiment {experiment_id} Evolution Log ===\n")
                    f.write(f"Command: {' '.join(cmd)}\n")
                    f.write(f"Return code: {proc.returncode}\n")
                    f.write("\n=== STDOUT ===\n")
                    f.write(stdout)
                    if stderr:
                        f.write("\n=== STDERR ===\n")
                        f.write(stderr)
                logger.info(f"Saved full evolution log to {log_file}")
            except Exception as log_err:
                logger.warning(f"Failed to save evolution log to file: {log_err}")

            # Log output for debugging (first 2000 chars in logger)
            if stdout:
                logger.info(f"run.py stdout for {experiment_id}:\n{stdout[:2000]}")  # First 2000 chars
            if stderr:
                logger.error(f"run.py stderr for {experiment_id}:\n{stderr[:2000]}")  # First 2000 chars
            logger.info(f"run.py finished for {experiment_id} with returncode={proc.returncode}")

            if proc.returncode == 0:
                return {"output": stdout, "success": True}
            else:
                return {"error": stderr or stdout, "success": False}
        except Exception as e:
            return {"error": str(e), "success": False}
        finally:
            try:
                if "script_to_run" in locals() and script_to_run != run_script and script_to_run.exists():
                    script_to_run.unlink()
            except Exception:
                pass

    def _patch_exec_runner_support(self, clone_path: Path) -> None:
        """Ensure tools/exec_runner.py sets __file__ before exec so user code can resolve dataset paths."""
        exec_runner_path = clone_path / "tools" / "exec_runner.py"
        if not exec_runner_path.exists():
            return
        raw = exec_runner_path.read_text()
        # Already patched
        if "mod.__dict__['__file__']" in raw or 'mod.__dict__["__file__"]' in raw:
            return
        # Inject __file__ assignment before exec(..., mod.__dict__) without touching file header
        import re as _re

        pattern = _re.compile(r"(\n\s*)(exec\(code_obj,\s*mod\.__dict__\))")
        m = pattern.search(raw)
        if not m:
            return
        indent = m.group(1)
        # Inline minimal imports here to avoid violating future-import placement rules at file top
        injection_lines = [
            f"{indent}_os = __import__('os')",
            f"{indent}from pathlib import Path as _Path",  # local import is allowed anywhere
            f"{indent}mod.__dict__['__file__'] = str(_Path(_os.environ.get('PROBLEM_DIR') or _os.getcwd()) / 'user_code.py')",
            f"{indent}" + m.group(2),
        ]
        injection = "\n".join(injection_lines)
        patched = raw[: m.start()] + injection + raw[m.end() :]
        exec_runner_path.write_text(patched)

    async def generate_code_from_llm(self, prompt: str) -> Optional[str]:
        """Generate code using LLM (placeholder)"""
        # TODO: Implement LLM integration for code generation
        # This would connect to an LLM service to generate experiment code
        return None

    @staticmethod
    def _parse_redis_url(url_str: str) -> tuple[str, int, int]:
        """Parse a redis:// URL into (host, port, db). Raises ValueError if invalid."""
        if not url_str:
            raise ValueError("Empty Redis URL")
        parts = urlsplit(url_str)
        host = parts.hostname
        port = parts.port
        path_num = (parts.path or "").lstrip("/")
        if not path_num.isdigit():
            raise ValueError(f"Invalid Redis URL (db index missing): '{url_str}'")
        db = int(path_num)
        if not host or port is None:
            raise ValueError(f"Invalid Redis URL (host/port missing): '{url_str}'")
        return host, port, db

    @staticmethod
    def _redis_value_to_float(value: Any, field_name: str = "value") -> Optional[float]:
        """Convert Redis value to float. Returns None for empty/invalid values."""
        if value is None:
            return None

        # Convert to string and strip whitespace
        value_str = str(value).strip()
        if not value_str:
            return None

        try:
            # Convert to float to preserve precision and handle both integers and decimals
            return float(value_str)
        except (ValueError, TypeError, OverflowError) as e:
            logger.warning(f"Failed to convert Redis {field_name} '{value!r}': {e}")
            return None

    async def cleanup_experiment(self, experiment_id: str) -> bool:
        """Clean up experiment artifacts"""
        try:
            # TODO: Implement cleanup logic
            return True
        except Exception as e:
            print(f"Failed to cleanup experiment {experiment_id}: {e}")
            return False

    async def generate_evolution_plot(
        self,
        experiment_id: str,
        output_subfolder: str,
        *,
        iteration_col: str = "generation",
    ) -> dict:
        """
        Run tools.comparison to generate evolution plot for a single running experiment.
        Saves plot to <repo>/{output_subfolder}/evolution_runs_comparison.(png|pdf)
        """
        clone_path = Path(self.config.clone_path).resolve()
        try:
            python_path = self._resolve_python_path(clone_path)

            host, port, db = self._redis_host_port_db()

            # Extract problem name from experiment_id (remove 'exp_' prefix if present)
            # CRITICAL: Must match the problem.name used by run.py (without exp_ prefix)
            # run.py stores data in Redis using problem.name, so we must use the same name here
            problem_name = self._normalize_problem_name(experiment_id)

            # tools.comparison expects run spec <prefix>@<db>[:label]
            # The prefix must match what run.py used when storing data (problem.name without exp_)
            run_spec = f"{problem_name}@{db}:Run"

            # Ensure output folder exists
            out_dir = clone_path / output_subfolder
            out_dir.mkdir(parents=True, exist_ok=True)

            problem_dir = clone_path / "problems" / experiment_id

            # Build the command to run module tools.comparison
            cmd = [
                python_path,
                "-m",
                "tools.comparison",
                "--redis-host",
                host,
                "--redis-port",
                str(port),
                "--run",
                run_spec,
                "--iteration-col",
                iteration_col,
                "--output-folder",
                str(out_dir),
            ]

            env = self._env_with_problem_dir(problem_dir)
            logger.info(f"Setting PYTHONPATH to include problem directory: {env.get('PROBLEM_DIR')}")

            proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(clone_path), env=env)
            # Enforce a timeout using experiment_timeout to avoid hanging results collection
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=self.config.experiment_timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return {"success": False, "error": "Results collection timed out"}
            stdout = (stdout_b or b"").decode("utf-8", errors="replace")
            stderr = (stderr_b or b"").decode("utf-8", errors="replace")

            success = proc.returncode == 0
            return {
                "success": success,
                "output_dir": str(out_dir),
                "stdout": stdout,
                "stderr": stderr,
                "output_png_file": str(out_dir / "evolution_runs_comparison.png"),
                "output_pdf_file": str(out_dir / "evolution_runs_comparison.pdf"),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def generate_evolution_report(
        self,
        experiment_id: str,
        output_subfolder: str,
    ) -> dict:
        """
        Export comprehensive evolution data to CSV using tools.redis2pd, then build a JSON
        summary with main stats and the best_program code.
        Writes files under <repo>/{output_subfolder}/:
          - evolution_report.csv (intermediate)
          - evolution_report.json (final summary with best_program)
        """
        clone_path = Path(self.config.clone_path).resolve()
        try:
            python_path = self._resolve_python_path(clone_path)

            host, port, db = self._redis_host_port_db()

            # Extract problem name from experiment_id (remove 'exp_' prefix if present)
            # CRITICAL: Must match the problem.name used by run.py (without exp_ prefix)
            # run.py stores data in Redis using problem.name, so we must use the same name here
            problem_name = self._normalize_problem_name(experiment_id)

            # Ensure output folder exists
            out_dir = clone_path / output_subfolder
            out_dir.mkdir(parents=True, exist_ok=True)
            out_csv = out_dir / "evolution_report.csv"
            out_json = out_dir / "evolution_report.json"

            problem_dir = clone_path / "problems" / experiment_id

            # Build the command to run module tools.redis2pd
            # The redis-prefix must match what run.py used when storing data (problem.name without exp_)
            cmd = [
                python_path,
                "-m",
                "tools.redis2pd",
                "--redis-host",
                host,
                "--redis-port",
                str(port),
                "--redis-db",
                str(db),
                "--redis-prefix",
                f"{problem_name}",  # Must match problem.name from run.py (without exp_ prefix)
                "--output-file",
                str(out_csv),
            ]

            env = self._env_with_problem_dir(problem_dir)
            logger.info(f"Setting PYTHONPATH to include problem directory: {env.get('PROBLEM_DIR')}")

            proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(clone_path), env=env)
            # Enforce a timeout using experiment_timeout
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=self.config.experiment_timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return {"success": False, "error": "Stats export timed out"}
            stdout = (stdout_b or b"").decode("utf-8", errors="replace")
            stderr = (stderr_b or b"").decode("utf-8", errors="replace")

            success = proc.returncode == 0
            if not success:
                return {"success": False, "stdout": stdout, "stderr": stderr}

            # Build JSON summary with pandas (required)
            try:
                import pandas as pd  # type: ignore
            except Exception as _e:
                return {"success": False, "error": f"pandas is required to build evolution_report.json: {_e}"}

            df = pd.read_csv(out_csv)
            # Coerce types
            if "metric_is_valid" in df.columns:
                df["metric_is_valid"] = pd.to_numeric(df["metric_is_valid"], errors="coerce").fillna(0.0)
            else:
                df["metric_is_valid"] = 0.0
            if "is_complete" in df.columns:
                df["is_complete"] = df["is_complete"].astype(str).str.lower().isin(["true", "1"])
            else:
                df["is_complete"] = False
            if "metric_fitness" in df.columns:
                df["metric_fitness"] = pd.to_numeric(df["metric_fitness"], errors="coerce")
            else:
                df["metric_fitness"] = float("nan")
            if "generation" in df.columns:
                df["generation"] = pd.to_numeric(df["generation"], errors="coerce").fillna(0).astype(int)
            else:
                df["generation"] = 0
            if "created_at" in df.columns:
                df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")

            # Consider only successfully completed programs
            if "state" in df.columns:
                _state_ok = df["state"].astype(str).str.lower().eq("dag_processing_completed")
            else:
                # If no state column, treat all rows as acceptable state
                _state_ok = pd.Series([True] * len(df), index=df.index)
            valid_completed = df[_state_ok & (df["metric_is_valid"] >= 1) & (df["is_complete"] == True)]  # noqa: E712
            # Determine fitness direction from workspace metrics.yml (higher_is_better)
            higher_is_better = True
            try:
                problem_dir = clone_path / "problems" / str(experiment_id)
                metrics_yaml = problem_dir / "metrics.yml"
                if metrics_yaml.exists():
                    try:
                        import yaml  # type: ignore

                        _cfg = yaml.safe_load(metrics_yaml.read_text(encoding="utf-8")) or {}
                        _specs = _cfg.get("specs", {}) or {}
                        _fitness = _specs.get("fitness", {}) or {}
                        hib = _fitness.get("higher_is_better", True)
                        if isinstance(hib, bool):
                            higher_is_better = hib
                    except Exception:
                        pass
            except Exception:
                pass
            best_row = None
            if not valid_completed.empty:
                sort_cols = []
                ascending = []
                if "metric_fitness" in valid_completed.columns:
                    sort_cols.append("metric_fitness")
                    ascending.append(not higher_is_better)
                if "generation" in valid_completed.columns:
                    sort_cols.append("generation")
                    ascending.append(False)
                if "created_at" in valid_completed.columns:
                    sort_cols.append("created_at")
                    ascending.append(False)
                ranked = valid_completed.sort_values(sort_cols, ascending=ascending) if sort_cols else valid_completed
                best_row = ranked.head(1).iloc[0].to_dict()

            # Derived counts
            # Total iterations: metadata_iteration is 0-based index, so report max + 1
            total_iterations = 0
            if "metadata_iteration" in df.columns:
                _iter_series = pd.to_numeric(df["metadata_iteration"], errors="coerce")
                if not _iter_series.empty and _iter_series.notna().any():
                    try:
                        total_iterations = int(_iter_series.max()) + 1
                    except Exception:
                        total_iterations = 0
            total_programs = int(df["program_id"].nunique()) if "program_id" in df.columns else 0
            if "program_id" in df.columns and "is_complete" in df.columns:
                try:
                    _completed_mask = df["is_complete"]
                    # Apply state filter
                    _completed_mask = _completed_mask & _state_ok
                    total_programs_complete = int(df[_completed_mask]["program_id"].nunique())
                except Exception:
                    total_programs_complete = 0
            else:
                total_programs_complete = 0

            summary: Dict[str, Any] = {
                "experiment_id": experiment_id,
                "total_iterations": total_iterations,
                "total_programs": total_programs,
                "total_programs_complete": total_programs_complete,
                "best_program_id": (best_row.get("program_id") if best_row else None),
                "best_fitness": (
                    float(best_row.get("metric_fitness"))
                    if best_row and best_row.get("metric_fitness") is not None
                    else None
                ),
                "best_generation": (
                    int(best_row.get("generation")) if best_row and best_row.get("generation") is not None else None
                ),
                "best_created_at": (
                    str(best_row.get("created_at")) if best_row and best_row.get("created_at") is not None else None
                ),
                "best_program": (
                    _extract_prompt_template(best_row.get("code")) or best_row.get("code") if best_row else None
                ),
            }

            # Add token counters
            try:
                cfg_path = clone_path / "problems" / str(experiment_id) / "config.json"
                if cfg_path.exists():
                    run_cfg = json.loads(cfg_path.read_text(encoding="utf-8") or "{}")
                    model_id = str(run_cfg.get("llm_model") or "").strip()
                    if model_id:
                        try:
                            redis_host, redis_port, redis_db = self._redis_host_port_db()
                            redis_url = f"redis://{redis_host}:{redis_port}/{redis_db}"
                            runtime = get_llm_runtime_required(model_id, required_keys={"model"})
                            model_name = str(runtime.get("model") or model_id).replace("/", "_")
                            problem_name = self._normalize_problem_name(experiment_id)
                            async with Redis.from_url(redis_url, decode_responses=True) as r:
                                hash_key = f"{problem_name}:metrics:latest"
                                total_raw, ctx_raw = await r.hmget(
                                    hash_key,
                                    f"llm/tokens/default/{model_name}/cumulative_total_tokens",
                                    f"llm/tokens/default/{model_name}/cumulative_context_tokens",
                                )
                                total_tokens = self._redis_value_to_float(total_raw, "cumulative_total_tokens")
                                if total_tokens is not None:
                                    summary["cumulative_total_tokens"] = total_tokens

                                ctx_tokens = self._redis_value_to_float(ctx_raw, "cumulative_context_tokens")
                                if ctx_tokens is not None:
                                    summary["cumulative_context_tokens"] = ctx_tokens
                        except Exception as redis_err:
                            logger.warning(f"Failed to fetch token metrics for {experiment_id}: {redis_err}")
            except Exception as e:
                logger.debug(f"Token metrics fetch failed for {experiment_id}: {e}")

            # Write JSON summary
            try:
                out_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            except Exception as _e:
                return {"success": False, "error": f"Failed to write JSON summary: {_e}"}

            return {
                "success": True,
                "output_dir": str(out_dir),
                "output_json_file": str(out_json),
                "stdout": stdout,
                "stderr": stderr,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
