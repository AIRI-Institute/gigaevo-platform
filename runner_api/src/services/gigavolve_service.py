#!/usr/bin/env python3

import asyncio
import logging
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from ..config import load_config

logger = logging.getLogger(__name__)


class GigaEvolveService:
    def __init__(self):
        self.config = load_config().gigavolve
        self._ensure_repos_directory()

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

    async def clone_repository(self, force_refresh: bool = False) -> bool:
        """Clone the GigaEvolve repository"""
        try:
            clone_path = Path(self.config.clone_path).resolve()

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
                        # Ensure LLM config exists even when repo already present
                        try:
                            self._ensure_llm_config_file(clone_path)
                        except Exception as cfg_err:
                            logger.warning(f"Failed to ensure LLM config file after update: {cfg_err}")
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

            # Ensure local LLM config is present in the cloned repo
            try:
                self._ensure_llm_config_file(clone_path)
            except Exception as cfg_err:
                logger.warning(f"Failed to ensure LLM config file: {cfg_err}")

            return True

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
        except Exception as e:
            logger.error(f"Unexpected error during repository cloning: {e}")
            return False

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

            subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True, text=True)

            if venv_dir.exists():
                logger.info(f"Created virtualenv at {venv_dir}")
            else:
                logger.error(f"Failed to create virtualenv at {venv_dir}")
                return False

            if pyproject.exists():
                # Install project (non-editable to minimize file churn)
                subprocess.run([str(venv_pip), "install", "."], cwd=str(clone_path), check=True, text=True)
                subprocess.run([str(venv_pip), "install", "scikit-learn"], cwd=str(clone_path), check=False, text=True)
                logger.info("Installed repo dependencies into dedicated venv (pyproject)")
            elif requirements.exists():
                subprocess.run(
                    [str(venv_pip), "install", "-r", "requirements.txt"], cwd=str(clone_path), check=True, text=True
                )
                logger.info("Installed repo requirements into dedicated venv")
            else:
                logger.info("No pyproject.toml or requirements.txt found; skipping dependency install")
                return False

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
        """Check if the repository is ready for use"""
        try:
            clone_path = Path(self.config.clone_path).resolve()
            if not clone_path.exists():
                return False

            # Check if it's a git repository
            git_dir = clone_path / ".git"
            if git_dir.exists():
                # Check if we can run git commands
                try:
                    subprocess.run(["git", "status"], cwd=clone_path, capture_output=True, text=True, check=True)
                    return True
                except subprocess.CalledProcessError:
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
                    "commit_hash": "mock",
                    "remote_url": self.config.repo_url,
                    "branch": "mock",
                    "is_ready": True,
                    "is_mock": True,
                }

            # Get current commit hash
            result = self._run_git_command(["git", "rev-parse", "HEAD"], clone_path)
            commit_hash = result.stdout.strip()

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
                "commit_hash": commit_hash,
                "remote_url": remote_url,
                "branch": branch,
                "is_ready": True,
                "is_mock": False,
            }

        except Exception as e:
            logger.error(f"Error getting repository info: {e}")
            return {"error": str(e)}

    def _ensure_llm_config_file(self, clone_path: Path) -> None:
        """
        Create/update local LLM Hydra config expected by GigaEvo Core:
          <repo>/config/llm/custom.yaml
        The config references runtime environment variables, so we do not bake secrets here.
        """
        try:
            llm_dir = clone_path / "config" / "llm"
            llm_dir.mkdir(parents=True, exist_ok=True)
            cfg_path = llm_dir / "custom.yaml"
            content = (
                "# @package _global_\n\n"
                "llm:\n"
                "  _target_: gigaevo.llm.models.MultiModelRouter\n"
                "  _convert_: all\n"
                "  models:\n"
                "    - _target_: langchain_openai.ChatOpenAI\n"
                "      model: ${oc.env:LLM__MODEL,gigachat-max-2}\n"
                "      api_key: ${oc.env:LLM__API_KEY}\n"
                "      temperature: ${temperature}\n"
                "      max_tokens: ${oc.env:LLM__MAX_TOKENS,2048}\n"
                "      top_p: ${top_p}\n"
                "      base_url: ${oc.env:LLM__BASE_URL}\n"
                "      request_timeout: ${request_timeout}\n"
                "  probabilities: [1.0]\n"
            )
            # Always write/overwrite to keep deterministic
            cfg_path.write_text(content, encoding="utf-8")
            logger.info(f"Wrote LLM config at {cfg_path}")
        except Exception as e:
            raise RuntimeError(f"Error writing LLM config: {e}")

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

            # If the repo-local venv exists, prefer its interpreter for this run
            venv_python = clone_path / ".venv" / "bin" / "python"
            if venv_python.exists():
                python_path = str(venv_python)
            else:
                python_path = self.config.python_path

            logger.info(f"Using Python path: {python_path}")

            cfg = load_config()

            # Optionally inject a development-only SSL verification bypass, controlled via config
            script_to_run = run_script
            if getattr(self.config, "ssl_bypass_enabled", False):
                try:
                    raw = run_script.read_text()
                    ssl_bypass = (
                        "# DEV ONLY: Disable SSL verification for HTTP clients\n"
                        "import ssl\n"
                        "try:\n"
                        "    ssl._create_default_https_context = ssl._create_unverified_context\n"
                        "    _orig_create_default_context = ssl.create_default_context\n"
                        "    def _insecure_ctx(*args, **kwargs):\n"
                        "        ctx = _orig_create_default_context(*args, **kwargs)\n"
                        "        try:\n"
                        "            ctx.check_hostname = False\n"
                        "            ctx.verify_mode = ssl.CERT_NONE\n"
                        "        except Exception:\n"
                        "            pass\n"
                        "        return ctx\n"
                        "    ssl.create_default_context = _insecure_ctx\n"
                        "except Exception:\n"
                        "    pass\n\n"
                    )
                    if "ssl._create_default_https_context" not in raw:
                        raw = ssl_bypass + raw
                    patched_path = clone_path / "run_patched.py"
                    patched_path.write_text(raw)
                    script_to_run = patched_path
                except Exception as patch_err:
                    logger.warning(
                        f"Failed to patch run.py for LLM settings: {patch_err}. Proceeding with original script."
                    )

            cmd = [
                python_path,
                str(script_to_run),
                f"problem.name={experiment_id}",
                f"redis_storage.config.redis_url={cfg.gigavolve.redis_url}",
                f"problem.dir={problem_dir}",
                f"max_generations={config.get('max_iterations', None)}",
                "llm=custom",
            ]

            # Prepare environment (LLM base URL and API key)
            env = os.environ.copy()
            # Provide problem directory for exec runner to resolve __file__ when executing user code
            env["PROBLEM_DIR"] = str(problem_dir)
            env["OPENAI_API_KEY"] = cfg.llm.api_key

            # Run subprocess asynchronously to avoid blocking the event loop
            proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(clone_path), env=env)

            # Determine per-experiment timeout (seconds)
            timeout_limit = config.get("timeout_seconds", self.config.experiment_timeout)

            # Periodically check for timeout and optional cancellation
            stdout_b = b""
            stderr_b = b""
            try:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout_limit
                while True:
                    try:
                        # Wait in 1s slices, but do not exceed remaining timeout
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            raise asyncio.TimeoutError()
                        await asyncio.wait_for(proc.wait(), timeout=min(1, remaining))
                        break
                    except asyncio.TimeoutError:
                        # Check global timeout first
                        if loop.time() >= deadline:
                            try:
                                proc.kill()
                            except ProcessLookupError:
                                pass
                            # Treat reaching the time limit as a successful (but time-bounded) completion
                            return {"output": "Experiment reached time limit", "success": True, "timed_out": True}
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
                # Fallback: also treat this path as a time-bounded successful completion
                return {"output": "Experiment reached time limit", "success": True, "timed_out": True}

            stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
            stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""

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
            # Resolve python path (prefer repo .venv)
            venv_python = clone_path / ".venv" / "bin" / "python"
            python_path = str(venv_python) if venv_python.exists() else self.config.python_path

            # Parse redis host/port/db from config
            cfg = load_config()
            candidate_url = (cfg.gigavolve.redis_url or cfg.redis.url or "").strip()
            host, port, db = self._parse_redis_url(candidate_url)

            # tools.comparison expects run spec <prefix>@<db>[:label]
            run_spec = f"{experiment_id}@{db}:Run"

            # Ensure output folder exists
            out_dir = clone_path / output_subfolder
            out_dir.mkdir(parents=True, exist_ok=True)

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

            proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(clone_path))
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
            # Resolve python path (prefer repo .venv)
            venv_python = clone_path / ".venv" / "bin" / "python"
            python_path = str(venv_python) if venv_python.exists() else self.config.python_path

            # Parse redis host/port/db from config
            cfg = load_config()
            candidate_url = (cfg.gigavolve.redis_url or cfg.redis.url or "").strip()
            host, port, db = self._parse_redis_url(candidate_url)

            # Ensure output folder exists
            out_dir = clone_path / output_subfolder
            out_dir.mkdir(parents=True, exist_ok=True)
            out_csv = out_dir / "evolution_report.csv"
            out_json = out_dir / "evolution_report.json"

            # Build the command to run module tools.redis2pd
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
                f"{experiment_id}",
                "--output-file",
                str(out_csv),
            ]

            proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(clone_path))
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
            valid_completed = df[_state_ok & (df["metric_is_valid"] >= 1) & (df["is_complete"] == True)]
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
                    _completed_mask = df["is_complete"] == True
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
                "best_program": (best_row.get("code") if best_row else None),
            }

            # Write JSON summary
            try:
                import json as _json

                out_json.write_text(_json.dumps(summary, indent=2), encoding="utf-8")
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
