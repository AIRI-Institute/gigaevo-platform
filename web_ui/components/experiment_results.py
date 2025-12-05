"""Experiment Results tab component."""

import gradio as gr
from loguru import logger
from utils.formatters import build_experiment_selector_choices

from .base import BaseComponent


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
                refresh_res_btn = gr.Button("Refresh Results")
                download_btn = gr.DownloadButton(label="Download Results", value=None, variant="primary")
                download_hint = gr.HTML("<div style='color:#666'>Select an experiment to enable download</div>")

            # Wire up event handlers
            res_selector.change(self._fetch_best_program_code, inputs=res_selector, outputs=best_program_code)

            refresh_res_btn.click(self._fetch_best_program_code, inputs=res_selector, outputs=best_program_code)

            download_btn.click(self._prepare_download_file, inputs=res_selector, outputs=[download_btn, download_hint])

            # Auto-refresh components
            def refresh_selector(current_value):
                """Refresh experiment selector choices."""
                exps = self.exp_manager.list_experiments()
                choices = build_experiment_selector_choices(exps)
                value = current_value if current_value in choices else (choices[0] if choices else None)
                return gr.Dropdown(choices=choices, value=value, interactive=True)

            # Auto-refresh selector every 10 seconds
            res_selector_timer = gr.Timer(10)
            res_selector_timer.tick(refresh_selector, inputs=res_selector, outputs=res_selector)

            # Auto-refresh code every 10 seconds
            code_timer = gr.Timer(10)
            code_timer.tick(self._fetch_best_program_code, inputs=res_selector, outputs=best_program_code)

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
