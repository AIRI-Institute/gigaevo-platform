import unittest

from generate_runner_pool_compose import build_compose_config, generate


class GenerateRunnerPoolComposeTests(unittest.TestCase):
    def test_dev_pool_uses_one_shared_image_and_preserves_dev_settings(self) -> None:
        config = build_compose_config("dev", pool_size=3, db_start=1)
        services = config["services"]

        self.assertEqual(list(services), ["runner-api-1", "runner-api-2", "runner-api-3"])

        expected_image = "${RUNNER_IMAGE_NAME:?RUNNER_IMAGE_NAME is required}"
        images = {service["image"] for service in services.values()}
        self.assertEqual(images, {expected_image})

        for idx in range(1, 4):
            service = services[f"runner-api-{idx}"]
            self.assertNotIn("build", service)
            self.assertIn(f"REDIS__URL=redis://redis:6379/{idx}", service["environment"])
            self.assertIn(f"GIGAVOLVE__REDIS_URL=redis://redis-gigavolve:6379/{idx}", service["environment"])
            self.assertIn("MEMORY_API_URL=${MEMORY_API_URL:-http://host.docker.internal:8002}", service["environment"])
            self.assertEqual(service["extra_hosts"], ["host.docker.internal:host-gateway"])

        runner_one = services["runner-api-1"]
        self.assertEqual(runner_one["ports"], ["${RUNNER_API_HOST_PORT:-8001}:8001"])
        self.assertIn("HOST_UID=${HOST_UID:-1000}", runner_one["environment"])
        self.assertIn("HOST_GID=${HOST_GID:-1000}", runner_one["environment"])
        self.assertIn("./runner_api/src:/app/src", runner_one["volumes"])
        self.assertEqual(
            runner_one["command"],
            [
                "/app/.venv/bin/uvicorn",
                "src.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8001",
                "--reload",
                "--reload-dir",
                "/app/src",
            ],
        )

        rendered = generate("dev", pool_size=3, db_start=1)
        self.assertNotIn("build:", rendered)

    def test_deploy_pool_uses_one_shared_image_and_network(self) -> None:
        config = build_compose_config("deploy", pool_size=3, db_start=4)
        services = config["services"]

        self.assertIn("networks", config)
        self.assertEqual(
            config["networks"],
            {"gigaevo-network": {"external": True, "name": "${GIGAEVO_NETWORK_NAME:-gigaevo-network}"}},
        )

        for idx, redis_db in zip(range(1, 4), range(4, 7), strict=True):
            service = services[f"runner-api-{idx}"]
            self.assertEqual(service["image"], "${RUNNER_IMAGE_NAME:?RUNNER_IMAGE_NAME is required}")
            self.assertEqual(service["networks"], ["gigaevo-network"])
            self.assertNotIn("build", service)
            self.assertNotIn("command", service)
            self.assertIn(f"REDIS__URL=redis://redis:6379/{redis_db}", service["environment"])
            self.assertIn(f"GIGAVOLVE__REDIS_URL=redis://redis-gigavolve:6379/{redis_db}", service["environment"])
            self.assertIn("MEMORY_API_URL=${MEMORY_API_URL:-http://host.docker.internal:8002}", service["environment"])
            self.assertEqual(service["extra_hosts"], ["host.docker.internal:host-gateway"])


if __name__ == "__main__":
    unittest.main()
