"""Experiment Results tab component."""

import html
import json
import re

import gradio as gr
from config.settings import DEFAULT_LIMITS
from loguru import logger
from utils.formatters import build_experiment_selector_choices

from .base import BaseComponent


def _carl_results_panel_flags(exp: dict | None) -> tuple[bool, bool]:
    """Return (show_chain_configuration, show_chain_feedback) for Experiment Results tab.

    Chain Configuration + feedback UI are only for experiments created via **Create CARL Experiment**.
    Feedback accordion is further gated by **enable_feedback** in that flow.
    """
    if not exp:
        return False, False
    params = (exp.get("config") or {}).get("parameters") or {}
    kind = params.get("ui_experiment_kind")
    if kind == "carl_chain":
        is_carl = True
    elif kind == "reasoning_chain":
        is_carl = False
    else:
        # Older CARL runs may have chain_llm_model without ui_experiment_kind
        is_carl = params.get("chain_llm_model") is not None and bool(params.get("base_chain_config"))
    if not is_carl:
        return False, False
    show_feedback = bool(params.get("enable_feedback", False))
    return True, show_feedback


def _extract_chain_config_from_program(program_code: str) -> dict | None:
    """Parse chain JSON from evolved program (CHAIN_CONFIG_JSON or BASE_CHAIN_CONFIG)."""
    if not program_code or not program_code.strip():
        return None
    for var in ("CHAIN_CONFIG_JSON", "BASE_CHAIN_CONFIG"):
        pattern = re.compile(
            rf"{re.escape(var)}\s*(?::\s*str\s*)?=\s*(?P<q>\"\"\"|'''|\"|')(?P<body>.*?)(?P=q)",
            re.DOTALL,
        )
        m = pattern.search(program_code)
        if not m:
            continue
        body = m.group("body")
        body = body.replace('\\"\\"\\"', '"""').replace("\\'\\'\\'", "'''")
        try:
            parsed = json.loads(body.strip())
            if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


_MISSING = object()

_CARL_DIFF_STYLE = """
<style>
.carl-chain-diff-wrap { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; line-height: 1.45; margin-top: 8px; }
.carl-chain-diff { margin: 0; white-space: pre-wrap; word-break: break-word; background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 6px; padding: 10px; }
.carl-chain-diff .carl-add { background-color: rgba(40, 167, 69, 0.28); border-radius: 2px; }
.carl-chain-diff .carl-del { background-color: rgba(220, 53, 69, 0.18); text-decoration: line-through; border-radius: 2px; opacity: 0.7; }
</style>
"""


def _step_map(steps: list | None) -> dict[int, dict]:
    m: dict[int, dict] = {}
    if not isinstance(steps, list):
        return m
    for s in steps:
        if isinstance(s, dict):
            n = s.get("number")
            if isinstance(n, (int, float)):
                m[int(n)] = s
    return m


def _indent_block(text: str, spaces: int) -> str:
    pad = " " * spaces
    lines = text.split("\n")
    out: list[str] = []
    for ln in lines:
        out.append(pad + ln if ln else ln)
    return "\n".join(out)


def _render_plain_json(obj, base_indent: int) -> str:
    raw = json.dumps(obj, indent=2, ensure_ascii=False)
    indented = _indent_block(raw, base_indent)
    return html.escape(indented, quote=False)


def _wrap_add(fragment: str) -> str:
    return f'<span class="carl-add">{fragment}</span>'


def _wrap_del(fragment: str) -> str:
    return f'<span class="carl-del">{fragment}</span>'


def _render_dict_diff(new_d: dict, old_d: dict, base_indent: int) -> str:
    old_d = old_d if isinstance(old_d, dict) else {}
    all_keys = list(dict.fromkeys(list(new_d.keys()) + list(old_d.keys())))
    pad = " " * base_indent
    inner = base_indent + 2
    ip = " " * inner

    entries: list[str] = []
    for k in all_keys:
        key_lit = json.dumps(k, ensure_ascii=False)
        key_esc = html.escape(key_lit, quote=False)
        in_new = k in new_d
        in_old = k in old_d

        if in_new and not in_old:
            val_plain = _render_plain_json(new_d[k], inner)
            if "\n" not in val_plain:
                entries.append(_wrap_add(f"{ip}{key_esc}: {val_plain}"))
            else:
                block = f"{ip}{key_esc}:\n{val_plain}"
                entries.append(_wrap_add(block))
        elif not in_new and in_old:
            val_plain = _render_plain_json(old_d[k], inner)
            if "\n" not in val_plain:
                entries.append(_wrap_del(f"{ip}{key_esc}: {val_plain}"))
            else:
                block = f"{ip}{key_esc}:\n{val_plain}"
                entries.append(_wrap_del(block))
        else:
            nv, ov = new_d[k], old_d[k]
            val = _render_value_diff(nv, ov, inner, k)
            if "\n" not in val:
                entries.append(f"{ip}{key_esc}: {val}")
            else:
                parts = [f"{ip}{key_esc}:"]
                for vl in val.split("\n"):
                    parts.append(vl if vl else "")
                entries.append("\n".join(parts))

    lines: list[str] = [pad + "{"]
    for i, entry in enumerate(entries):
        comma = "," if i < len(entries) - 1 else ""
        entry_lines = entry.split("\n")
        entry_lines[-1] += comma
        lines.extend(entry_lines)
    lines.append(pad + "}")
    return "\n".join(lines)


def _render_steps_array(new_steps: list, old_steps: list, base_indent: int) -> str:
    old_m = _step_map(old_steps)
    new_m = _step_map(new_steps)
    pad = " " * base_indent
    inner = base_indent + 2

    entries: list[str] = []
    for step in new_steps:
        if not isinstance(step, dict):
            entries.append(_render_value_diff(step, _MISSING, inner, None))
        else:
            num = step.get("number")
            old_s = old_m.get(int(num)) if isinstance(num, (int, float)) else None
            if old_s is None:
                entries.append(_wrap_add(_render_plain_json(step, inner)))
            elif step == old_s:
                entries.append(_render_plain_json(step, inner))
            else:
                entries.append(_render_dict_diff(step, old_s, inner))

    for num in sorted(set(old_m) - set(new_m)):
        entries.append(_wrap_del(_render_plain_json(old_m[num], inner)))

    lines: list[str] = [pad + "["]
    for i, entry in enumerate(entries):
        comma = "," if i < len(entries) - 1 else ""
        entry_lines = entry.split("\n")
        for bi, bline in enumerate(entry_lines):
            suf = comma if bi == len(entry_lines) - 1 else ""
            lines.append(bline + suf)
    lines.append(pad + "]")
    return "\n".join(lines)


def _render_value_diff(nv, ov, base_indent: int, key: str | None) -> str:
    if key == "steps" and isinstance(nv, list):
        old_list = ov if isinstance(ov, list) else []
        return _render_steps_array(nv, old_list, base_indent)
    if ov is _MISSING:
        return _wrap_add(_render_plain_json(nv, base_indent))
    if nv == ov:
        return _render_plain_json(nv, base_indent)
    if type(nv) is not type(ov):
        return _wrap_add(_render_plain_json(nv, base_indent))
    if isinstance(nv, dict):
        return _render_dict_diff(nv, ov, base_indent)
    if isinstance(nv, list):
        return _wrap_add(_render_plain_json(nv, base_indent))
    return _wrap_add(_render_plain_json(nv, base_indent))


def render_chain_plain_html(chain: dict | None) -> str:
    """Render chain config as formatted HTML without diff highlighting."""
    if not chain:
        return f"{_CARL_DIFF_STYLE}<p style='color:#666'>No chain config.</p>"
    body = _render_plain_json(chain, 0)
    pre = f'<pre class="carl-chain-diff">{body}</pre>'
    return f"{_CARL_DIFF_STYLE}<div class='carl-chain-diff-wrap'>{pre}</div>"


def render_chain_diff_vs_initial_html(initial: dict | None, best: dict | None) -> str:
    """HTML: best chain with inline green (added/changed) and red (removed) vs initial."""
    if not best:
        return f"{_CARL_DIFF_STYLE}<p style='color:#666'>No best chain config.</p>"
    ini = initial if isinstance(initial, dict) else {}
    body = _render_dict_diff(best, ini, 0)
    pre = f'<pre class="carl-chain-diff">{body}</pre>'
    return f"{_CARL_DIFF_STYLE}<div class='carl-chain-diff-wrap'>{pre}</div>"


class ExperimentResultsComponent(BaseComponent):
    """Component for experiment results and best program."""

    def build(self) -> gr.Column:
        """Build the experiment results tab.

        Returns:
            Gradio Column component
        """
        with gr.Column() as component:
            gr.Markdown("## Experiment Results")

            res_selector = gr.Dropdown(label="Select Experiment", choices=[], interactive=True)

            with gr.Column():
                best_program_code = gr.Code(label="Best Program (.py)", language="python", lines=32)

                # CARL wizard only (visibility updated when experiment changes)
                with gr.Accordion("Chain Configuration (CARL)", open=False, visible=False) as chain_config_accordion:
                    with gr.Row():
                        with gr.Column():
                            gr.Markdown("### Initial Chain Config")
                            initial_chain_html = gr.HTML(
                                value="<div style='color:#888;font-size:12px'>No initial chain config loaded.</div>"
                            )
                        with gr.Column():
                            gr.Markdown("### Best (Evolved) Chain Config")
                            best_chain_diff_html = gr.HTML(
                                value="<div style='color:#888;font-size:12px'>No best chain config loaded.</div>"
                            )

                with gr.Accordion("Chain Feedback", open=False, visible=False) as feedback_accordion:
                    with gr.Row():
                        feedback_iteration = gr.Dropdown(
                            label="Iteration (Latest = most recent uploaded)",
                            choices=["Latest"],
                            value="Latest",
                            allow_custom_value=True,
                            interactive=True,
                        )
                        load_feedback_btn = gr.Button("Load Feedback", size="sm")
                    feedback_status = gr.HTML("")

                    gr.Markdown("### 🔄 Chain Execution Feedback")
                    feedback_markdown = gr.Markdown(
                        "Select an experiment to load feedback.\n\n"
                        "Feedback shows performance analysis, input/output pairs, "
                        "failed examples, and improvement suggestions."
                    )

                refresh_res_btn = gr.Button("Refresh Results")
                download_btn = gr.DownloadButton(label="Download Results", value=None, variant="primary")
                download_hint = gr.HTML("<div style='color:#666'>Select an experiment to enable download</div>")

                # Memory upload (visible after experiment completes, for all experiment types)
                with gr.Column(visible=False) as memory_upload_row:
                    gr.HTML(
                        "<div style='border-top:1px solid #e0e0e0;margin-top:8px;padding-top:12px;'>"
                        "<span style='font-size:1.1em;font-weight:600;'>🧠 Upload to Memory</span>"
                        "<p style='color:#666;margin:4px 0 0;font-size:0.9em;'>"
                        "Export experiment insights to the <b>Memory Platform</b>. "
                        "Ideas Tracker will analyze evolution results, extract best ideas, "
                        "and save them as <b>Memory Cards</b> for future experiments."
                        "</p></div>"
                    )
                    with gr.Row():
                        upload_memory_btn = gr.Button(
                            "🧠 Start Upload to Memory",
                            variant="primary",
                            size="lg",
                        )
                    memory_upload_status = gr.HTML("")

            _all_outputs = [
                best_program_code,
                initial_chain_html,
                best_chain_diff_html,
                feedback_markdown,
                feedback_iteration,
                chain_config_accordion,
                feedback_accordion,
                memory_upload_row,
            ]

            # Wire up event handlers
            def update_results_and_iteration_bounds(experiment_selector_value: str):
                """Update results, feedback, and iteration dropdown choices."""
                return self._fetch_all_results(experiment_selector_value)

            res_selector.change(
                update_results_and_iteration_bounds,
                inputs=res_selector,
                outputs=_all_outputs,
            )

            refresh_res_btn.click(
                update_results_and_iteration_bounds,
                inputs=res_selector,
                outputs=_all_outputs,
            )

            download_btn.click(self._prepare_download_file, inputs=res_selector, outputs=[download_btn, download_hint])

            # Load feedback for specific iteration
            load_feedback_btn.click(
                self._load_feedback_for_iteration,
                inputs=[res_selector, feedback_iteration],
                outputs=[feedback_markdown, feedback_status],
            )

            # Memory upload handler (namespace = experiment_id automatically)
            upload_memory_btn.click(
                self._upload_to_memory,
                inputs=[res_selector],
                outputs=[memory_upload_status],
            )

            # Auto-refresh components
            def refresh_selector(current_value):
                """Refresh experiment selector choices."""
                exps = self.exp_manager.list_experiments()
                choices = build_experiment_selector_choices(exps)
                value = current_value if current_value in choices else (choices[0] if choices else None)
                return gr.Dropdown(choices=choices, value=value, interactive=True)

            # Auto-refresh selector periodically
            res_selector_timer = gr.Timer(DEFAULT_LIMITS["refresh_interval"])
            res_selector_timer.tick(refresh_selector, inputs=res_selector, outputs=res_selector)

            # Auto-refresh code periodically
            code_timer = gr.Timer(DEFAULT_LIMITS["refresh_interval"])
            code_timer.tick(self._fetch_best_program_code, inputs=res_selector, outputs=best_program_code)

            # Auto-refresh feedback iterations dropdown periodically
            feedback_timer = gr.Timer(DEFAULT_LIMITS["refresh_interval"])
            feedback_timer.tick(
                self._refresh_feedback_iterations,
                inputs=[res_selector, feedback_iteration],
                outputs=[feedback_iteration, feedback_accordion],
            )

            # Set up event handlers
            res_selector.change(
                self._fetch_best_program_code,
                inputs=res_selector,
                outputs=best_program_code,
            )
            # Also load initial data
            res_selector.change(
                self._load_selector_choices,
                outputs=res_selector,
            )

        return component

    def _load_selector_choices(self):
        """Load experiment selector choices."""
        exps = self.exp_manager.list_experiments()
        choices = build_experiment_selector_choices(exps)
        return gr.Dropdown(choices=choices, interactive=True)

    def _fetch_best_program_code(self, experiment_selector_value: str) -> str:
        """Fetch best program code for the selected experiment."""
        if not experiment_selector_value:
            return ""

        experiment_id = self.extract_experiment_id(experiment_selector_value)
        if not experiment_id:
            return ""

        try:
            evolution_report = self.exp_manager.get_evolution_report(experiment_id)
            if evolution_report:
                return evolution_report.get("best_program", "")
            return ""
        except Exception as e:
            logger.error(f"Error fetching best program code: {e}")
            return f"Error fetching code: {str(e)}"

    def _fetch_all_results(self, experiment_selector_value: str) -> tuple:
        """Fetch results for Gradio: program, CARL panels (if applicable), visibility updates."""

        def _empty_row(show_chain: bool = False, show_fb: bool = False):
            return (
                "",
                "<div style='color:#888;font-size:12px'>No initial chain config loaded.</div>",
                "<div style='color:#888;font-size:12px'>No best chain config loaded.</div>",
                "Select an experiment to load feedback.\n\n"
                "Feedback shows performance analysis, input/output pairs, "
                "failed examples, and improvement suggestions.",
                gr.update(choices=["Latest"], value="Latest"),
                gr.update(visible=show_chain),
                gr.update(visible=show_fb),
                gr.update(visible=False),
            )

        if not experiment_selector_value:
            return _empty_row()

        experiment_id = self.extract_experiment_id(experiment_selector_value)
        if not experiment_id:
            return _empty_row()

        exp_record = None
        try:
            exp_record = self.exp_manager.get_experiment(experiment_id)
        except Exception as e:
            logger.warning(f"Could not load experiment record for {experiment_id}: {e}")

        show_chain, show_feedback = _carl_results_panel_flags(exp_record)

        best_program = ""
        initial_chain_html_val = "<div style='color:#888;font-size:12px'>No initial chain config loaded.</div>"
        best_diff_html = "<div style='color:#888;font-size:12px'>No best chain config loaded.</div>"
        feedback_md = (
            "Chain feedback is disabled for this experiment (Enable Chain Feedback was off when created)."
            if show_chain and not show_feedback
            else (
                "Select an experiment to load feedback.\n\n"
                "Feedback shows performance analysis, input/output pairs, "
                "failed examples, and improvement suggestions."
            )
        )
        evolution_report = None
        avail_iters: list[int] = []

        # --- Best program code ---
        try:
            evolution_report = self.exp_manager.get_evolution_report(experiment_id)
            if evolution_report:
                best_program = evolution_report.get("best_program", "") or ""
        except Exception as e:
            logger.error(f"Error fetching best program code: {e}")
            best_program = f"Error fetching code: {str(e)}"

        best_chain_data = None
        initial_chain_data = None

        if show_chain:
            try:
                best_chain_data = self.exp_manager.get_best_chain_config(experiment_id)
                if best_chain_data and isinstance(best_chain_data, dict):
                    if best_chain_data.get("error"):
                        best_chain_data = None
                    elif not best_chain_data.get("steps"):
                        best_chain_data = None
            except Exception as e:
                logger.error(f"Error fetching best chain config: {e}")

            if best_chain_data is None and evolution_report:
                report_bc = evolution_report.get("best_chain_config")
                if isinstance(report_bc, dict) and report_bc.get("steps"):
                    best_chain_data = report_bc
                    logger.debug(f"Resolved best chain from evolution_report for {experiment_id}")

            if best_chain_data is None and best_program and not best_program.startswith("Error "):
                extracted = _extract_chain_config_from_program(best_program)
                if extracted:
                    best_chain_data = extracted
                    logger.debug(f"Resolved best chain from best_program for {experiment_id}")

            try:
                initial_chain_data = self.exp_manager.get_initial_chain_config(experiment_id)
                if initial_chain_data and isinstance(initial_chain_data, dict):
                    if initial_chain_data.get("error"):
                        initial_chain_data = None
                    else:
                        initial_chain_html_val = render_chain_plain_html(initial_chain_data)
            except Exception as e:
                logger.error(f"Error fetching initial chain config: {e}")

            if best_chain_data and isinstance(best_chain_data, dict):
                ini_for_diff = initial_chain_data if isinstance(initial_chain_data, dict) else {}
                best_diff_html = render_chain_diff_vs_initial_html(ini_for_diff, best_chain_data)

        if show_feedback:
            try:
                feedback_data = self.exp_manager.get_chain_feedback(experiment_id, iteration=None)
                avail_raw = (feedback_data or {}).get("available_iterations")
                if isinstance(avail_raw, list):
                    for x in avail_raw:
                        try:
                            avail_iters.append(int(x))
                        except (TypeError, ValueError):
                            continue
                if feedback_data and feedback_data.get("has_feedback"):
                    feedback_md = feedback_data.get("feedback", "")
                else:
                    msg = "No feedback available for this experiment."
                    if feedback_data and feedback_data.get("message"):
                        msg = feedback_data["message"]
                    feedback_md = msg
            except Exception as e:
                logger.error(f"Error fetching feedback: {e}")
                feedback_md = f"Error loading feedback: {str(e)}"

        choices = ["Latest"] + [str(i) for i in sorted(set(avail_iters), reverse=True)]
        iter_update = gr.update(choices=choices, value="Latest")

        # Show memory upload button for eligible terminal experiments
        show_memory_upload = False
        if exp_record:
            status = str((exp_record.get("status") or "")).lower()
            show_memory_upload = status in ("completed", "terminated", "cancelled")

        return (
            best_program,
            initial_chain_html_val,
            best_diff_html,
            feedback_md,
            iter_update,
            gr.update(visible=show_chain),
            gr.update(visible=show_feedback),
            gr.update(visible=show_memory_upload),
        )

    def _prepare_download_file(self, experiment_selector_value: str):
        """Prepare download file for the selected experiment."""
        if not experiment_selector_value:
            return None, "<div style='color:#666'>No experiment selected</div>"

        experiment_id = self.extract_experiment_id(experiment_selector_value)
        if not experiment_id:
            return None, "<div style='color:#666'>No experiment selected</div>"

        try:
            import tempfile

            archive_content = self.exp_manager.get_download_archive(experiment_id)

            if archive_content:
                tmp = tempfile.NamedTemporaryFile(prefix="results_", suffix=".zip", delete=False)
                tmp.write(archive_content)
                tmp.flush()
                tmp.close()
                return tmp.name, "<div>Ready to download results.zip</div>"
            else:
                return None, "<div style='color:#a00'>Failed to fetch archive</div>"

        except Exception as e:
            logger.error(f"Error preparing download file: {e}")
            return None, "<div style='color:#a00'>Failed to prepare download</div>"

    def _refresh_feedback_iterations(
        self,
        experiment_selector_value: str,
        current_iteration_choice: str,
    ) -> tuple:
        """Periodically refresh the feedback iteration dropdown without changing displayed content."""
        if not experiment_selector_value:
            return gr.update(), gr.update()

        experiment_id = self.extract_experiment_id(experiment_selector_value)
        if not experiment_id:
            return gr.update(), gr.update()

        exp_record = None
        try:
            exp_record = self.exp_manager.get_experiment(experiment_id)
        except Exception:
            return gr.update(), gr.update()

        _, show_feedback = _carl_results_panel_flags(exp_record)
        if not show_feedback:
            return gr.update(), gr.update(visible=False)

        try:
            feedback_data = self.exp_manager.get_chain_feedback(experiment_id, iteration=None)
            avail_iters: list[int] = []
            avail_raw = (feedback_data or {}).get("available_iterations")
            if isinstance(avail_raw, list):
                for x in avail_raw:
                    try:
                        avail_iters.append(int(x))
                    except (TypeError, ValueError):
                        continue
            choices = ["Latest"] + [str(i) for i in sorted(set(avail_iters), reverse=True)]
            value = current_iteration_choice if current_iteration_choice in choices else "Latest"
            return gr.update(choices=choices, value=value), gr.update(visible=True)
        except Exception:
            return gr.update(), gr.update()

    def _load_feedback_for_iteration(
        self,
        experiment_selector_value: str,
        iteration_choice: str,
    ) -> tuple:
        """Load feedback for a specific iteration (dropdown value or numeric string).

        Args:
            experiment_selector_value: Selected experiment from dropdown
            iteration_choice: \"Latest\" or iteration index as string

        Returns:
            Tuple of (feedback_markdown, status_html)
        """
        if not experiment_selector_value:
            return "No experiment selected", ""

        experiment_id = self.extract_experiment_id(experiment_selector_value)
        if not experiment_id:
            return "Invalid experiment", ""

        exp_rec = self.exp_manager.get_experiment(experiment_id)
        show_chain, show_fb = _carl_results_panel_flags(exp_rec)
        if not show_chain or not show_fb:
            return (
                "Chain feedback is not enabled for this experiment.",
                "<div style='color:#888'>Only CARL experiments with “Enable Chain Feedback” show this panel.</div>",
            )

        iteration_param = None
        if iteration_choice is not None and str(iteration_choice).strip().lower() not in ("", "latest"):
            try:
                iteration_param = int(float(iteration_choice))
            except (TypeError, ValueError):
                return (
                    "Invalid iteration",
                    "<div style='color:#a00'>Enter a number or choose Latest</div>",
                )

        try:
            feedback_data = self.exp_manager.get_chain_feedback(
                experiment_id,
                iteration=iteration_param,
            )

            if feedback_data and feedback_data.get("has_feedback"):
                feedback_md = feedback_data.get("feedback", "")

                iter_num = feedback_data.get("iteration")
                if iter_num is not None:
                    status = (
                        f"<div style='color:#4CAF50'>✅ Loaded feedback for "
                        f"iteration {iter_num} ({feedback_data.get('feedback_length')} chars)</div>"
                    )
                else:
                    status = (
                        f"<div style='color:#4CAF50'>✅ Loaded latest feedback "
                        f"({feedback_data.get('feedback_length')} chars)</div>"
                    )

                return feedback_md, status
            else:
                message = (
                    feedback_data.get("message", "No feedback available for this iteration")
                    if feedback_data
                    else "No feedback data"
                )
                status = f"<div style='color:#FFA500'>⚠️ {message}</div>"
                return message, status

        except Exception as e:
            logger.error(f"Error loading feedback for iteration {iteration_choice}: {e}")
            error_msg = f"Error loading feedback: {str(e)}"
            status = f"<div style='color:#a00'>❌ {error_msg}</div>"
            return error_msg, status

    def _upload_to_memory(self, experiment_selector_value: str, progress=gr.Progress()) -> str:
        """Upload experiment results to the memory platform via Master API."""
        if not experiment_selector_value:
            return "<div style='color:#a00'>No experiment selected</div>"
        experiment_id = self.extract_experiment_id(experiment_selector_value)
        if not experiment_id:
            return "<div style='color:#a00'>Invalid experiment</div>"
        progress(0.1, desc="Submitting upload request...")
        result = self.exp_manager.upload_to_memory(experiment_id)

        if not isinstance(result, dict):
            return "<div style='color:#a00'>Unexpected response from upload_to_memory</div>"

        if "error" in result:
            progress(1.0, desc="Upload failed")
            return (
                "<div style='padding:12px 16px;background:linear-gradient(135deg,#ffebee,#fff8f8);"
                "border-left:4px solid #d32f2f;border-radius:8px;margin-top:8px;'>"
                "<div style='display:flex;align-items:center;gap:8px;'>"
                "<span style='font-size:1.3em;'>❌</span>"
                "<strong style='color:#b71c1c;font-size:1.05em;'>Upload Failed</strong>"
                "</div>"
                f"<div style='margin-top:8px;color:#555;font-size:0.92em;'>"
                f"<b>Experiment:</b> <code style='background:#fff;padding:2px 6px;"
                f"border-radius:3px;'>{html.escape(experiment_id)}</code><br>"
                f"<b>Error:</b> {html.escape(str(result.get('error') or 'Unknown error'))}"
                "</div></div>"
            )

        progress(1.0, desc="Upload complete")
        namespace = str(result.get("namespace") or experiment_id)
        return (
            "<div style='padding:12px 16px;background:linear-gradient(135deg,#e8f5e9,#f1f8e9);"
            "border-left:4px solid #4CAF50;border-radius:8px;margin-top:8px;'>"
            "<div style='display:flex;align-items:center;gap:8px;'>"
            "<span style='font-size:1.3em;'>✅</span>"
            "<strong style='color:#2e7d32;font-size:1.05em;'>Upload Complete</strong>"
            "</div>"
            f"<div style='margin-top:8px;color:#555;font-size:0.92em;'>"
            f"<b>Experiment:</b> <code style='background:#fff;padding:2px 6px;"
            f"border-radius:3px;'>{html.escape(experiment_id)}</code><br>"
            f"<b>Memory Namespace:</b> <code style='background:#fff;padding:2px 6px;"
            f"border-radius:3px;'>{html.escape(namespace)}</code>"
            "</div></div>"
        )
