#!/usr/bin/env python3

import asyncio
from typing import Dict, List, Optional

from loguru import logger

from ..config import RunnerInstanceConfig


class DockerService:
    """Service for managing Docker containers for RunnerAPI instances"""

    def __init__(self, config: RunnerInstanceConfig):
        self.config = config
        self.docker_host = config.docker_host
        self.container_name = f"gigaevo-runner-{config.host.replace('.', '-').replace(':', '-')}"
        self.endpoint_url = f"http://{config.host}:{config.port}"

    async def initialize_container(
        self,
        image_name: str,
        network_name: str,
        environment_vars: Optional[Dict[str, str]] = None,
        volumes: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Initialize and start a Docker container for RunnerAPI"""
        try:
            logger.info(f"Initializing Docker container for {self.config.host}:{self.config.port}")

            # Check if container already exists and remove it
            await self._stop_and_remove_container()

            # Prepare Docker command
            docker_cmd = self._build_docker_run_command(
                image_name,
                network_name,
                environment_vars or {},
                volumes or {},
            )

            # Execute Docker command
            if self.config.is_local:
                success = await self._execute_local_command(docker_cmd)
            else:
                success = await self._execute_remote_command(docker_cmd)

            if success:
                logger.info(f"Successfully started container {self.container_name}")
                return True
            else:
                logger.error(f"Failed to start container {self.container_name}")
                return False

        except Exception as e:
            logger.error(f"Error initializing container for {self.config.host}: {e}")
            return False

    async def stop_container(self) -> bool:
        """Stop and remove the Docker container"""
        try:
            return await self._stop_and_remove_container()
        except Exception as e:
            logger.error(f"Error stopping container for {self.config.host}: {e}")
            return False

    async def get_container_status(self) -> Optional[str]:
        """Get the status of the Docker container (disabled in containerized environment)"""
        try:
            # In containerized environments, Docker commands won't work from within containers
            # So we return a mock status and rely on HTTP health checks instead
            logger.debug(f"Container status check disabled for {self.config.host} (containerized environment)")
            return "Up (HTTP-based health check)"

        except Exception as e:
            logger.error(f"Error getting container status for {self.config.host}: {e}")
            return None

    async def check_container_health(self) -> bool:
        """Check if the container is healthy and responding via HTTP"""
        try:
            # Since Master API and Runner API run in separate containers,
            # we rely on HTTP health checks instead of Docker commands
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                # Try the standard health endpoint first
                try:
                    health_url = f"{self.endpoint_url.rstrip('/')}/health"
                    logger.debug(f"Trying health endpoint: {health_url}")
                    response = await client.get(health_url)
                    if response.status_code == 200:
                        logger.info(f"Health check successful for {self.config.host} via /health")
                        return True
                    else:
                        logger.debug(f"Health endpoint returned status {response.status_code}")
                except Exception as e:
                    logger.debug(f"Health endpoint failed: {e}")

                # Try alternative health endpoints
                health_endpoints = [
                    "/api/v1/status",
                    "/api/v1/health",
                    "/",
                ]

                for endpoint in health_endpoints:
                    try:
                        endpoint_url = f"{self.endpoint_url.rstrip('/')}{endpoint}"
                        logger.debug(f"Trying alternative endpoint: {endpoint_url}")
                        response = await client.get(endpoint_url)
                        if response.status_code == 200:
                            logger.info(f"Health check successful for {self.config.host} via {endpoint}")
                            return True
                        else:
                            logger.debug(f"Endpoint {endpoint} returned status {response.status_code}")
                    except Exception as e:
                        logger.debug(f"Endpoint {endpoint} failed: {e}")
                        continue

            logger.error(f"All health checks failed for {self.config.host} at {self.endpoint_url}")
            return False

        except Exception as e:
            logger.error(f"Health check failed for {self.config.host}: {e}")
            return False

    async def get_container_logs(self, lines: int = 50) -> str:
        """Get recent logs from the container (disabled in containerized environment)"""
        try:
            # In containerized environments, Docker commands won't work from within containers
            # So we return a helpful message directing users to use Docker logs directly
            logger.debug(f"Container logs retrieval disabled for {self.config.host} (containerized environment)")
            return (
                f"Container logs are not available from within containerized environment. "
                f"Use 'docker logs {self.container_name}' from the host machine to view logs."
            )

        except Exception as e:
            logger.error(f"Error getting container logs for {self.config.host}: {e}")
            return f"Error retrieving logs: {e}"

    def _build_docker_run_command(
        self,
        image_name: str,
        network_name: str,
        environment_vars: Dict[str, str],
        volumes: Dict[str, str],
    ) -> List[str]:
        """Build the docker run command"""
        cmd = ["docker", "run", "-d", "--name", self.container_name]

        # Add port mapping
        if self.config.is_local:
            cmd.extend(["-p", f"{self.config.port}:8001"])
        else:
            # For remote instances, we don't need port mapping on the host
            cmd.extend(["-p", "8001"])

        # Add network
        if network_name:
            cmd.extend(["--network", network_name])

        # Add environment variables
        for key, value in environment_vars.items():
            cmd.extend(["-e", f"{key}={value}"])

        # Add volumes
        for host_path, container_path in volumes.items():
            cmd.extend(["-v", f"{host_path}:{container_path}"])

        # Add restart policy
        cmd.extend(["--restart", "unless-stopped"])

        # Add image
        cmd.append(image_name)

        return cmd

    async def _execute_local_command(self, cmd: List[str]) -> bool:
        """Execute command locally"""
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                logger.debug(f"Command succeeded: {' '.join(cmd)}")
                return True
            else:
                logger.error(f"Command failed with code {process.returncode}: {stderr.decode()}")
                return False

        except Exception as e:
            logger.error(f"Error executing local command: {e}")
            return False

    async def _execute_local_command_with_output(self, cmd: List[str]) -> Optional[str]:
        """Execute command locally and return output"""
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                return stdout.decode().strip()
            else:
                logger.error(f"Command failed with code {process.returncode}: {stderr.decode()}")
                return None

        except Exception as e:
            logger.error(f"Error executing local command: {e}")
            return None

    async def _execute_remote_command(self, cmd: List[str]) -> bool:
        """Execute command via SSH on remote host"""
        if not self.config.ssh_user or not self.config.ssh_key_path:
            logger.error("SSH configuration missing for remote execution")
            return False

        try:
            # Build SSH command
            ssh_cmd = [
                "ssh",
                "-i",
                self.config.ssh_key_path,
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "BatchMode=yes",
                f"{self.config.ssh_user}@{self.config.host}",
                " ".join(cmd),
            ]

            process = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                logger.debug(f"Remote command succeeded: {' '.join(cmd)}")
                return True
            else:
                logger.error(f"Remote command failed with code {process.returncode}: {stderr.decode()}")
                return False

        except Exception as e:
            logger.error(f"Error executing remote command: {e}")
            return False

    async def _execute_remote_command_with_output(self, cmd: List[str]) -> Optional[str]:
        """Execute command via SSH on remote host and return output"""
        if not self.config.ssh_user or not self.config.ssh_key_path:
            logger.error("SSH configuration missing for remote execution")
            return None

        try:
            # Build SSH command
            ssh_cmd = [
                "ssh",
                "-i",
                self.config.ssh_key_path,
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "BatchMode=yes",
                f"{self.config.ssh_user}@{self.config.host}",
                " ".join(cmd),
            ]

            process = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                return stdout.decode().strip()
            else:
                logger.error(f"Remote command failed with code {process.returncode}: {stderr.decode()}")
                return None

        except Exception as e:
            logger.error(f"Error executing remote command: {e}")
            return None

    async def _stop_and_remove_container(self) -> bool:
        """Stop and remove the container if it exists"""
        try:
            # Stop container
            stop_cmd = ["docker", "stop", self.container_name]
            if self.config.is_local:
                await self._execute_local_command(stop_cmd)
            else:
                await self._execute_remote_command(stop_cmd)

            # Remove container
            remove_cmd = ["docker", "rm", self.container_name]
            if self.config.is_local:
                return await self._execute_local_command(remove_cmd)
            else:
                return await self._execute_remote_command(remove_cmd)

        except Exception as e:
            logger.debug(f"Error stopping/removing container (may not exist): {e}")
            # Don't treat this as an error since the container might not exist
            return True
