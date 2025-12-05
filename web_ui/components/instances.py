"""Runner Instances tab component."""

import gradio as gr
from config.settings import DEFAULT_LIMITS
from utils.formatters import (
    build_instance_selector_choices,
    format_instances_table,
)

from .base import BaseComponent


class InstancesComponent(BaseComponent):
    """Component for managing runner instances."""

    def build(self) -> gr.Column:
        """Build the runner instances tab.

        Returns:
            Gradio Column component
        """
        with gr.Column() as component:
            gr.Markdown("## Runner API Instance Management")

            with gr.Tabs():
                # Sub-tab: Instance List
                with gr.Tab("Instance List"):
                    gr.Markdown("### All Runner Instances")

                    instances_df = gr.Dataframe(
                        value=self._get_instances_table(), label="Runner Instances", interactive=False
                    )

                    with gr.Row():
                        refresh_instances_btn = gr.Button("Refresh List")
                        initialize_all_btn = gr.Button("🚀 Initialize All", variant="primary")

                    with gr.Row():
                        instance_selector = gr.Dropdown(label="Select Instance", choices=[], interactive=True)

                        with gr.Column():
                            init_btn = gr.Button("Initialize", variant="primary", size="sm")
                            stop_btn = gr.Button("Stop", variant="stop", size="sm")
                            restart_btn = gr.Button("Restart", variant="secondary", size="sm")

                    instance_action_output = gr.Textbox(label="Action Status", interactive=False)

                # Sub-tab: Instance Details
                with gr.Tab("Instance Details"):
                    gr.Markdown("### Detailed Instance Information")

                    detail_instance_selector = gr.Dropdown(label="Select Instance", choices=[], interactive=True)
                    details_display = gr.Markdown(label="Instance Details")

                # Sub-tab: Instance Logs
                with gr.Tab("Instance Logs"):
                    gr.Markdown("### View Instance Logs")

                    logs_instance_selector = gr.Dropdown(label="Select Instance", choices=[], interactive=True)

                    with gr.Row():
                        log_lines_slider = gr.Slider(
                            minimum=DEFAULT_LIMITS["min_log_lines"],
                            maximum=DEFAULT_LIMITS["max_log_lines"],
                            value=DEFAULT_LIMITS["default_log_lines"],
                            step=10,
                            label="Number of Log Lines",
                        )
                        get_logs_btn = gr.Button("Get Logs", variant="primary")

                    logs_display = gr.Markdown(label="Instance Logs")

                # Sub-tab: Health Summary
                with gr.Tab("Health Summary"):
                    gr.Markdown("### Instance Health Overview")

                    health_display = gr.Markdown(self._get_health_summary())
                    refresh_health_btn = gr.Button("Refresh Health")

            # Wire up event handlers
            self._setup_event_handlers(
                instances_df,
                instance_selector,
                detail_instance_selector,
                logs_instance_selector,
                refresh_instances_btn,
                initialize_all_btn,
                init_btn,
                stop_btn,
                restart_btn,
                get_logs_btn,
                log_lines_slider,
                refresh_health_btn,
                instance_action_output,
                details_display,
                logs_display,
                health_display,
            )

        return component

    def _setup_event_handlers(self, *inputs):
        """Set up all event handlers for the component."""
        (
            instances_df,
            instance_selector,
            detail_instance_selector,
            logs_instance_selector,
            refresh_instances_btn,
            initialize_all_btn,
            init_btn,
            stop_btn,
            restart_btn,
            get_logs_btn,
            log_lines_slider,
            refresh_health_btn,
            instance_action_output,
            details_display,
            logs_display,
            health_display,
        ) = inputs

        # Refresh instances
        refresh_instances_btn.click(self._get_instances_table, outputs=instances_df)

        # Initialize all instances
        def initialize_all_and_refresh():
            """Initialize all instances and refresh the table."""
            result = self.inst_manager.initialize_all_instances()
            updated_table = self._get_instances_table()
            instances = self.inst_manager.list_instances()
            choices = build_instance_selector_choices(instances)

            status_msg = ""
            if "error" in result:
                status_msg = f"❌ {result['error']}"
            else:
                status_msg = f"✅ {result.get('message', 'All instances initialized')}"

            return (
                status_msg,
                updated_table,
                *[
                    gr.Dropdown(choices=choices, interactive=True)
                    for _ in range(3)  # Update three selectors
                ],
            )

        initialize_all_btn.click(
            initialize_all_and_refresh,
            outputs=[
                instance_action_output,
                instances_df,
                instance_selector,
                detail_instance_selector,
                logs_instance_selector,
            ],
        )

        # Update instance selectors when dataframe changes
        instances_df.change(
            self._update_instance_selectors,
            inputs=instances_df,
            outputs=[instance_selector, detail_instance_selector, logs_instance_selector],
        )

        # Instance actions
        init_btn.click(self._initialize_instance, inputs=instance_selector, outputs=instance_action_output)

        stop_btn.click(self._stop_instance, inputs=instance_selector, outputs=instance_action_output)

        restart_btn.click(self._restart_instance, inputs=instance_selector, outputs=instance_action_output)

        # Details display
        detail_instance_selector.change(
            self._get_instance_details, inputs=detail_instance_selector, outputs=details_display
        )

        # Logs display
        get_logs_btn.click(
            self._get_instance_logs, inputs=[logs_instance_selector, log_lines_slider], outputs=logs_display
        )

        # Health refresh
        refresh_health_btn.click(self._get_health_summary, outputs=health_display)

    def _get_instances_table(self):
        """Get instances as a pandas DataFrame."""
        instances = self.inst_manager.list_instances()
        return format_instances_table(instances)

    def _update_instance_selectors(self, df):
        """Update instance selector choices based on dataframe."""
        if df is not None and len(df) > 0:
            instances = self.inst_manager.list_instances()
            choices = build_instance_selector_choices(instances)
            return [
                gr.Dropdown(choices=choices, interactive=True)
                for _ in range(3)  # Update three selectors
            ]
        return [gr.Dropdown(choices=[], interactive=True) for _ in range(3)]

    def _initialize_instance(self, instance_id: str) -> str:
        """Initialize the selected instance."""
        if not instance_id:
            return "❌ No instance selected"

        result = self.inst_manager.initialize_instance(instance_id)
        if "error" in result:
            return self.handle_api_error(result, "initializing instance")

        return f"✅ {result.get('message', 'Instance initialized successfully')}"

    def _stop_instance(self, instance_id: str) -> str:
        """Stop the selected instance."""
        if not instance_id:
            return "❌ No instance selected"

        result = self.inst_manager.stop_instance(instance_id)
        if "error" in result:
            return self.handle_api_error(result, "stopping instance")

        return f"✅ {result.get('message', 'Instance stopped successfully')}"

    def _restart_instance(self, instance_id: str) -> str:
        """Restart the selected instance."""
        if not instance_id:
            return "❌ No instance selected"

        result = self.inst_manager.restart_instance(instance_id)
        if "error" in result:
            return self.handle_api_error(result, "restarting instance")

        return f"✅ {result.get('message', 'Instance restarted successfully')}"

    def _get_instance_details(self, instance_id: str) -> str:
        """Get detailed information about an instance."""
        if not instance_id:
            return "No instance selected"

        instance = self.inst_manager.get_instance(instance_id)
        if not instance:
            return "Instance not found"

        # Format details (simplified version)
        details = f"""
        **Instance Details**

        **ID:** {instance["id"]}
        **Name:** {instance["name"]}
        **Status:** {instance["status"]}
        **Endpoint URL:** {instance["endpoint_url"]}
        **Created:** {instance["created_at"][:19] if instance["created_at"] else "N/A"}
        **Last Heartbeat:** {instance["last_heartbeat"][:19] if instance["last_heartbeat"] else "Never"}

        **Current Experiment:** {instance.get("current_experiment_id", "None")}
        """

        return details

    def _get_instance_logs(self, instance_id: str, lines: int) -> str:
        """Get logs from the selected instance."""
        if not instance_id:
            return "No instance selected"

        result = self.inst_manager.get_instance_logs(instance_id, lines)
        if "error" in result:
            return self.handle_api_error(result, "getting instance logs")

        logs = result.get("logs", "")
        if not logs:
            return "No logs available"

        return f"**Logs for {instance_id} (last {lines} lines):**\n\n```\n{logs}\n```"

    def _get_health_summary(self) -> str:
        """Get health summary as formatted text."""
        health = self.inst_manager.get_health_summary()

        if "error" in health:
            return f"Error: {health['error']}"

        # Simplified health summary
        summary = f"""
        **Instance Health Summary**

        **Total Instances:** {health.get("total_instances", 0)}
        **Healthy Instances:** {health.get("healthy_instances", 0)}
        **Unhealthy Instances:** {health.get("unhealthy_instances", 0)}
        **Ready Instances:** {health.get("ready_instances", 0)}
        **Offline Instances:** {health.get("offline_instances", 0)}

        **Instance Details:**
        """

        instances_detail = health.get("instances_detail", [])
        for instance in instances_detail:
            status_emoji = (
                "🟢"
                if instance["status"] in ["ready", "online"]
                else "🔴"
                if instance["status"] in ["error", "offline"]
                else "🟡"
            )
            summary += f"\n{status_emoji} **{instance['id']}** - {instance['status'].upper()}"
            summary += f"\n   - Endpoint: {instance['endpoint_url']}"
            summary += (
                f"\n   - Last Heartbeat: {instance['last_heartbeat'][:19] if instance['last_heartbeat'] else 'Never'}"
            )
            if instance.get("current_experiment_id"):
                summary += f"\n   - Current Experiment: {instance['current_experiment_id']}"
            summary += "\n"

        return summary
