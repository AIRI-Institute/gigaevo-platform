"""System Status tab component."""

import gradio as gr
from loguru import logger

from .base import BaseComponent


class SystemStatusComponent(BaseComponent):
    """Component for displaying system status."""

    def build(self) -> gr.Column:
        """Build the system status tab.

        Returns:
            Gradio Column component
        """
        with gr.Column() as component:
            gr.Markdown("## System Health and Status")
            status_blocks_display = gr.HTML(label="Status Blocks")
            refresh_status_btn = gr.Button("Refresh Status")

            # Wire up event handlers
            refresh_status_btn.click(self._get_status_blocks, outputs=status_blocks_display)

            # Load initial status
            status_blocks_display.value = self._get_status_blocks()

        return component

    def _get_status_blocks(self) -> str:
        """Get system status as visual blocks."""
        try:
            status_data = self.status_service.get_system_status()
            return self.status_service.create_status_blocks(status_data)
        except Exception as e:
            logger.error(f"Failed to get system status: {e}")
            return self.status_service.create_fallback_status_html(str(e))
