#!/usr/bin/env python3
"""Refactored Gradio web interface for GigaEvo platform."""

import gradio as gr
from components import (
    CreateExperimentComponent,
    CreatePromptExperimentComponent,
    CreateChainExperimentComponent,
    ExperimentDetailsComponent,
    ExperimentResultsComponent,
    ExperimentsListComponent,
    InstancesComponent,
    SystemStatusComponent,
)
from config import AIRI_CSS
from loguru import logger
from services import ExperimentManager, InstanceManager, StatusService


def create_interface():
    """Create the main Gradio interface with modular components."""
    # Initialize service managers
    exp_manager = ExperimentManager()
    inst_manager = InstanceManager()
    status_service = StatusService()

    # Initialize components
    create_experiment_comp = CreateExperimentComponent(
        experiment_manager=exp_manager,
        instance_manager=inst_manager,
        status_service=status_service,
    )
    create_prompt_experiment_comp = CreatePromptExperimentComponent(
        experiment_manager=exp_manager,
        instance_manager=inst_manager,
        status_service=status_service,
    )
    create_chain_experiment_comp = CreateChainExperimentComponent(
        experiment_manager=exp_manager,
        instance_manager=inst_manager,
        status_service=status_service,
    )
    experiments_list_comp = ExperimentsListComponent(
        experiment_manager=exp_manager,
        instance_manager=inst_manager,
        status_service=status_service,
    )
    experiment_details_comp = ExperimentDetailsComponent(
        experiment_manager=exp_manager,
        instance_manager=inst_manager,
        status_service=status_service,
    )
    experiment_results_comp = ExperimentResultsComponent(
        experiment_manager=exp_manager,
        instance_manager=inst_manager,
        status_service=status_service,
    )
    instances_comp = InstancesComponent(
        experiment_manager=exp_manager,
        instance_manager=inst_manager,
        status_service=status_service,
    )
    system_status_comp = SystemStatusComponent(
        experiment_manager=exp_manager,
        instance_manager=inst_manager,
        status_service=status_service,
    )

    # Build the main interface
    with gr.Blocks(title="GigaEvo") as interface:
        # Inject CSS for styling (compatible with Gradio versions without `css` kwarg)
        gr.HTML(f"<style>{AIRI_CSS}</style>")
        gr.Markdown("# GigaEvo Platform")

        with gr.Tabs():
            # Tab 1: Create Prompt Experiment
            with gr.Tab("Create Prompt Experiment"):
                create_prompt_experiment_comp.build()

            # Tab 2: Create Chain Experiment
            with gr.Tab("Create Chain Experiment"):
                create_chain_experiment_comp.build()

            # Tab 3: Create Experiment
            with gr.Tab("Create ML Experiment"):
                create_experiment_comp.build()

            # Tab 4: Experiments List
            with gr.Tab("Experiments"):
                experiments_list_comp.build()

            # Tab 5: Experiment Details and Visualization
            with gr.Tab("Experiment Details and Visualization"):
                experiment_details_comp.build()

            # Tab 6: Experiment Results
            with gr.Tab("Experiment Results"):
                experiment_results_comp.build()

            # Tab 7: Runner Instances
            with gr.Tab("Runner Instances"):
                instances_comp.build()

            # Tab 8: System Status
            with gr.Tab("System Status"):
                system_status_comp.build()

        # Global data refresh timer (refresh every 30 seconds)
        def refresh_global_data():
            """Refresh global data that all components might need."""
            try:
                # This can be expanded to refresh shared data
                # For now, components handle their own refreshing
                return
            except Exception as e:
                logger.error(f"Error in global data refresh: {e}")

        global_timer = gr.Timer(30)
        global_timer.tick(refresh_global_data)

    return interface


if __name__ == "__main__":
    logger.info("Starting GigaEvo Web UI")

    try:
        logger.info("Creating interface...")
        interface = create_interface()
        logger.info("Interface created, launching server...")
        logger.info("=" * 80)
        logger.info("🌐 GigaEvo Web UI is starting...")
        logger.info("📋 Access the web interface at: http://localhost:7860")
        logger.info("=" * 80)
        interface.launch(
            server_name="0.0.0.0",
            server_port=7860,
            share=False,
            show_error=True,
            inbrowser=False,  # Don't try to open browser automatically in container
        )
        logger.info("Server launched successfully")
    except Exception as e:
        logger.error(f"Failed to start interface: {e}", exc_info=True)
        raise
