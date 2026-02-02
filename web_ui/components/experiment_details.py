"""Experiment Details and Visualization tab component."""

import gradio as gr
from loguru import logger
from utils.formatters import build_experiment_selector_choices
from utils.ui_helpers import (
    build_image_html,
    create_metric_cards,
    create_pie_chart_for_programs,
)

from .base import BaseComponent


class ExperimentDetailsComponent(BaseComponent):
    """Component for experiment details and visualization."""

    def __init__(self, *args, **kwargs):
        """Initialize experiment details component."""
        super().__init__(*args, **kwargs)
        self.bucket_name = None

    def build(self) -> gr.Column:
        """Build the experiment details and visualization tab.

        Returns:
            Gradio Column component
        """
        with gr.Column() as component:
            gr.Markdown("## Experiment Details and Visualization")

            results_selector = gr.Dropdown(label="Select Experiment", choices=[], interactive=True)

            gr.Markdown("### Key Metrics")
            metrics_cards_display = gr.HTML(label="Metrics Cards")

            with gr.Row():
                with gr.Column(scale=1):
                    pie_chart_display = gr.HTML(label="Program Completion Chart")

                with gr.Column(scale=2):
                    visualization_display = gr.HTML(label="Experiment Visualization")

            refresh_results_btn = gr.Button("Refresh Results")

            def update_all_visualizations(experiment_selector_value):
                """Update visualization components."""
                return self._load_experiment_visualizations(experiment_selector_value)

            vis_timer = gr.Timer(10)
            vis_timer.tick(
                update_all_visualizations,
                inputs=results_selector,
                outputs=[metrics_cards_display, pie_chart_display, visualization_display],
            )

            def refresh_selector(current_value):
                """Refresh experiment selector choices."""
                exps = self.exp_manager.list_experiments()
                choices = build_experiment_selector_choices(exps)
                value = current_value if current_value in choices else (choices[0] if choices else None)
                return gr.Dropdown(choices=choices, value=value, interactive=True)

            selector_timer = gr.Timer(10)
            selector_timer.tick(
                refresh_selector,
                inputs=results_selector,
                outputs=results_selector,
            )

            results_selector.change(
                update_all_visualizations,
                inputs=results_selector,
                outputs=[metrics_cards_display, pie_chart_display, visualization_display],
            )

            refresh_results_btn.click(
                update_all_visualizations,
                inputs=results_selector,
                outputs=[metrics_cards_display, pie_chart_display, visualization_display],
            )

            results_selector.change(
                self._load_experiment_visualizations,
                inputs=results_selector,
                outputs=[metrics_cards_display, pie_chart_display, visualization_display],
            )

        return component

    def _load_selector_choices(self):
        """Load experiment selector choices."""
        exps = self.exp_manager.list_experiments()
        choices = build_experiment_selector_choices(exps)
        return gr.Dropdown(choices=choices, interactive=True)

    def _load_experiment_visualizations(self, experiment_selector_value):
        """Load experiment visualizations for the selected experiment."""
        if not experiment_selector_value:
            return (
                create_metric_cards({}),
                create_pie_chart_for_programs(0, 0),
                "<div style='color:#666'>Select an experiment to view visualization</div>",
            )

        experiment_id = self.extract_experiment_id(experiment_selector_value)
        if not experiment_id:
            return (
                create_metric_cards({}),
                create_pie_chart_for_programs(0, 0),
                "<div style='color:#666'>Select an experiment to view visualization</div>",
            )

        try:
            if not self.bucket_name:
                self.bucket_name = self.status_service.get_storage_status()

            summary_data = self.exp_manager.get_experiment_summary(experiment_id)

            metric_cards_html = create_metric_cards(summary_data)

            total_programs = summary_data.get("total_programs", 0)
            total_programs_complete = summary_data.get("total_programs_complete", 0)
            pie_chart_html = create_pie_chart_for_programs(total_programs, total_programs_complete)

            image_html = "<div style='color:#666'>Visualization not available</div>"
            if self.bucket_name:
                image_html = build_image_html(experiment_id, self.bucket_name)

            return metric_cards_html, pie_chart_html, image_html

        except Exception as e:
            logger.error(f"Error loading experiment visualizations: {e}")
            error_html = "<div style='color:#a00'>Error loading visualizations</div>"
            return error_html, error_html, error_html
