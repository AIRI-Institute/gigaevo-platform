"""Service for system status and health information."""

from typing import Any, Dict, Optional

import requests
from config.settings import DEFAULT_TIMEOUTS, MASTER_API_URL, STATUS_COLORS
from loguru import logger


class StatusService:
    """Handles system status and health operations."""

    def __init__(self, base_url: Optional[str] = None):
        """Initialize the status service.

        Args:
            base_url: Base URL for the Master API. Falls back to environment variable.
        """
        self.base_url = base_url or MASTER_API_URL
        self.timeout = DEFAULT_TIMEOUTS["api_request"]

    def get_system_status(self) -> Dict[str, Any]:
        """Get comprehensive system status.

        Returns:
            System status dictionary
        """
        try:
            response = requests.get(f"{self.base_url}/api/v1/status/health", timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to get system status: {e}")
            return {"status": "unknown", "error": str(e)}

    def get_storage_status(self) -> Optional[str]:
        """Get storage bucket name from Master API.

        Returns:
            Bucket name or None if not available
        """
        try:
            response = requests.get(f"{self.base_url}/api/v1/status/storage", timeout=self.timeout)
            if response.ok:
                data = response.json()
                return data.get("bucket_name")
            return None

        except requests.RequestException as e:
            logger.error(f"Failed to get storage status: {e}")
            return None

    def create_status_blocks(self, status_data: Optional[Dict[str, Any]] = None) -> str:
        """Create visual HTML blocks for system status components.

        Args:
            status_data: Status data dictionary. If None, fetches fresh data.

        Returns:
            HTML string with status blocks
        """
        if not status_data:
            status_data = self.get_system_status()

        if not status_data:
            return """
            <div style='color:#666; text-align:center; padding:20px'>
                No status data available
            </div>
            """

        # Extract main system info
        system_status = status_data.get("status", "unknown")
        version = status_data.get("version", "unknown")

        # Get uptime if available
        uptime_text = "N/A"
        if "uptime_seconds" in status_data:
            uptime_seconds = status_data["uptime_seconds"]
            hours = uptime_seconds // 3600
            minutes = (uptime_seconds % 3600) // 60
            uptime_text = f"{hours}h {minutes}m"

        # Get components
        components = status_data.get("components", {})

        # Get system color
        system_color = STATUS_COLORS.get(system_status, STATUS_COLORS["unknown"])

        # Create status blocks HTML
        html = f"""
        <div style="display: flex; gap: 20px; margin-bottom: 20px; flex-wrap: wrap;">
            <div style="flex: 1; min-width: 200px; color: #ffffff !important; background: {system_color};
                        border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                <div style="font-size: 14px; color: #ffffff !important; opacity: 0.9; margin-bottom: 8px;">System Status</div>
                <div style="font-size: 24px; color: #ffffff !important; font-weight: bold; margin-bottom: 4px; text-transform: uppercase;">{system_status}</div>
                <div style="font-size: 12px; color: #ffffff !important; opacity: 0.8;">Overall system health</div>
            </div>

            <div style="flex: 1; min-width: 200px; color: #ffffff !important; background: #d1a584ff;
                        border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                <div style="font-size: 14px; color: #ffffff !important; opacity: 0.9; margin-bottom: 8px;">Version</div>
                <div style="font-size: 24px; color: #ffffff !important; font-weight: bold; margin-bottom: 4px;">{version}</div>
                <div style="font-size: 12px; color: #ffffff !important; opacity: 0.8;">Platform version</div>
            </div>

            <div style="flex: 1; min-width: 200px; color: #ffffff !important; background: #8c939cff;
                        border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                <div style="font-size: 14px; color: #ffffff !important; opacity: 0.9; margin-bottom: 8px;">Uptime</div>
                <div style="font-size: 24px; color: #ffffff !important; font-weight: bold; margin-bottom: 4px;">{uptime_text}</div>
                <div style="font-size: 12px; color: #ffffff !important; opacity: 0.8;">Time since start</div>
            </div>
        </div>

        <div style="margin-top: 30px;">
            <h3 style="margin-bottom: 15px; color: #333;">Component Status</h3>
            <div style="display: flex; gap: 15px; flex-wrap: wrap;">
        """

        # Add component blocks
        for component, comp_status in components.items():
            color = STATUS_COLORS.get(comp_status, STATUS_COLORS["unknown"])
            component_name = component.replace("_", " ").title()
            html += f"""
                <div style="flex: 1; min-width: 150px; background: {color}; border-radius: 12px; padding: 15px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                    <div style="font-size: 13px; opacity: 0.9; margin-bottom: 6px; color: white;">{component_name}</div>
                    <div style="font-size: 18px; font-weight: bold; text-transform: uppercase; color: white;">{comp_status}</div>
                </div>
            """

        html += """
            </div>
        </div>
        """

        return html

    def create_status_text(self, status_data: Optional[Dict[str, Any]] = None) -> str:
        """Create formatted text representation of system status.

        Args:
            status_data: Status data dictionary. If None, fetches fresh data.

        Returns:
            Formatted markdown text
        """
        if not status_data:
            status_data = self.get_system_status()

        # Build status text with proper formatting
        lines = [
            f"**System Status:** {status_data.get('status', 'unknown')}",
            f"**Version:** {status_data.get('version', 'unknown')}",
        ]

        # Add uptime if available
        if "uptime_seconds" in status_data:
            uptime_seconds = status_data["uptime_seconds"]
            hours = uptime_seconds // 3600
            minutes = (uptime_seconds % 3600) // 60
            lines.append(f"**Uptime:** {hours}h {minutes}m")

        lines.extend(["", "**Components:**"])

        components = status_data.get("components", {})
        emoji_map = {
            "healthy": "🟢",
            "degraded": "🟡",
            "unhealthy": "🔴",
            "configured": "🟦",
            "disabled": "⚪",
            "not_configured": "⚪",
            "no_instances": "⚠️",
        }

        for component, comp_status in components.items():
            emoji = emoji_map.get(comp_status, "❓")
            lines.append(f"{emoji} **{component.capitalize()}:** {comp_status}")

        # Add timestamp
        if "timestamp" in status_data:
            lines.extend(["", f"**Last Updated:** {status_data['timestamp'][:19].replace('T', ' ')}"])

        return "\n\n".join(lines)

    def create_fallback_status_html(self, error_message: str) -> str:
        """Create fallback HTML when status cannot be retrieved.

        Args:
            error_message: Error message to display

        Returns:
            HTML string for error display
        """
        return f"""
        <div style="background: #ef4444ff; border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <h3 style="margin-bottom: 10px;">Error fetching system status</h3>
            <div style="font-size: 16px; opacity: 0.9;">{error_message}</div>
            <div style="font-size: 12px; opacity: 0.8; margin-top: 10px;">Please check the Master API connection</div>
        </div>
        """
