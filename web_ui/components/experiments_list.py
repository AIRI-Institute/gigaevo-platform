"""Experiments List tab component."""

import gradio as gr
from loguru import logger
from utils.formatters import (
    build_experiment_selector_choices,
    format_experiments_table,
)

from .base import BaseComponent


class ExperimentsListComponent(BaseComponent):
    """Component for listing and managing experiments."""

    def build(self) -> gr.Column:
        """Build the experiments list tab.

        Returns:
            Gradio Column component
        """
        with gr.Column() as component:
            gr.Markdown("## Active and Historical Experiments")

            # Don't call API during initialization - load data asynchronously
            # Use empty dataframe initially to avoid blocking interface startup
            try:
                initial_table = self._get_experiments_table()
            except Exception as e:
                logger.warning(f"Failed to load experiments table during initialization: {e}")
                initial_table = None
            
            experiments_df = gr.Dataframe(value=initial_table, label="Experiments", interactive=False)

            with gr.Row():
                refresh_btn = gr.Button("Refresh")
                drop_all_btn = gr.Button("🗑️ Drop All Experiments", variant="stop", size="sm")

            with gr.Row():
                experiment_selector = gr.Dropdown(label="Select Experiment", choices=[], interactive=True)

                with gr.Column():
                    start_btn = gr.Button("Start", variant="primary")
                    stop_btn = gr.Button("Stop", variant="stop")

            action_output = gr.Textbox(label="Action Status", interactive=False)

            # Wire up event handlers
            self._setup_event_handlers(
                experiments_df, experiment_selector, refresh_btn, drop_all_btn, start_btn, stop_btn, action_output
            )

        return component

    def _setup_event_handlers(self, *inputs):
        """Set up all event handlers for the component."""
        (experiments_df, experiment_selector, refresh_btn, drop_all_btn, start_btn, stop_btn, action_output) = inputs

        # Refresh data and update selector
        def refresh_and_update_selector():
            """Refresh experiments table and update selector."""
            updated_table = self._get_experiments_table()
            updated_selector = self._update_experiment_selector(updated_table)
            return updated_table, updated_selector

        refresh_btn.click(refresh_and_update_selector, outputs=[experiments_df, experiment_selector])

        # Drop all experiments
        def drop_all_and_refresh():
            """Drop all experiments and refresh the table."""
            result = self.exp_manager.drop_all_experiments()
            updated_table = self._get_experiments_table()
            selector_choices = []

            if "error" in result:
                status_msg = f"❌ {result['error']}"
            else:
                deleted_experiments = result.get("deleted_experiments", 0)
                deleted_objects = result.get("deleted_storage_objects", 0)
                message = result.get("message", "Operation completed")
                status_msg = (
                    f"✅ {message}\n📊 Deleted {deleted_experiments} experiments and {deleted_objects} storage objects"
                )
                selector_choices = build_experiment_selector_choices(self.exp_manager.list_experiments())

            return status_msg, updated_table, gr.Dropdown(choices=selector_choices, interactive=True)

        drop_all_btn.click(drop_all_and_refresh, outputs=[action_output, experiments_df, experiment_selector])

        # Start experiment
        start_btn.click(self._start_experiment, inputs=experiment_selector, outputs=action_output)

        # Stop experiment
        stop_btn.click(self._stop_experiment, inputs=experiment_selector, outputs=action_output)

        # Update experiment selector when dataframe changes
        experiments_df.change(self._update_experiment_selector, inputs=experiments_df, outputs=experiment_selector)

    def _get_experiments_table(self):
        """Get experiments as a pandas DataFrame."""
        experiments = self.exp_manager.list_experiments()
        return format_experiments_table(experiments)

    def _update_experiment_selector(self, df):
        """Update experiment selector choices based on dataframe."""
        if df is not None and len(df) > 0:
            choices = build_experiment_selector_choices(self.exp_manager.list_experiments())
            return gr.Dropdown(choices=choices, interactive=True)
        return gr.Dropdown(choices=[], interactive=True)

    def _start_experiment(self, experiment_selector: str) -> str:
        """Start the selected experiment."""
        if not experiment_selector:
            return "❌ No experiment selected"

        experiment_id = self.extract_experiment_id(experiment_selector)
        result = self.exp_manager.start_experiment(experiment_id)

        if "error" in result:
            return self.handle_api_error(result, "starting experiment")

        return f"✅ Experiment {experiment_id} started successfully"

    def _stop_experiment(self, experiment_selector: str) -> str:
        """Stop the selected experiment."""
        if not experiment_selector:
            return "❌ No experiment selected"

        experiment_id = self.extract_experiment_id(experiment_selector)
        result = self.exp_manager.stop_experiment(experiment_id)

        if "error" in result:
            return self.handle_api_error(result, "stopping experiment")

        return f"✅ Experiment {experiment_id} stopped successfully"
