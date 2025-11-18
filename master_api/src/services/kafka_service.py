#!/usr/bin/env python3

import asyncio
import json
from typing import Any, Dict, Optional

import redis.asyncio as redis
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.errors import KafkaError
from loguru import logger


class KafkaService:
    """Service for managing Kafka operations in MasterAPI"""

    def __init__(self, config):
        self.config = config
        self.producer: Optional[AIOKafkaProducer] = None
        self.consumers: Dict[str, AIOKafkaConsumer] = {}
        self.redis_client: Optional[redis.Redis] = None
        self.consumer_tasks: Dict[str, asyncio.Task] = {}

    async def initialize(self):
        """Initialize Kafka producer and Redis connection"""
        try:
            # Initialize Redis connection first (required)
            self.redis_client = redis.from_url(self.config.redis_url)
            await self.redis_client.ping()
            logger.info("Redis connection established")

            # Initialize Kafka producer (optional for development)
            try:
                self.producer = AIOKafkaProducer(
                    bootstrap_servers=self.config.kafka.bootstrap_servers,
                    value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode("utf-8"),
                    key_serializer=lambda k: k.encode("utf-8") if k else None,
                )
                await self.producer.start()
                logger.info("Kafka producer started")
            except Exception as kafka_error:
                logger.warning(f"Kafka not available, running without message queuing: {kafka_error}")
                self.producer = None

        except Exception as e:
            logger.warning(f"Redis not available - running in degraded mode: {e}")
            # Don't raise the exception - allow the system to run without Redis for testing
            self.redis_client = None
            self.producer = None

    async def cleanup(self):
        """Cleanup Kafka producer and consumers"""
        if self.producer:
            await self.producer.stop()

        for task in self.consumer_tasks.values():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        for consumer in self.consumers.values():
            await consumer.stop()

        if self.redis_client:
            await self.redis_client.close()

    async def send_message(self, topic: str, message: Dict[str, Any], key: Optional[str] = None):
        """Send message to Kafka topic"""
        if not self.producer:
            logger.debug(f"Kafka not available, skipping message to topic {topic}: {message}")
            return

        try:
            await self.producer.send_and_wait(topic, message, key=key)
            logger.debug(f"Message sent to topic {topic}: {message}")
        except KafkaError as e:
            logger.error(f"Failed to send message to {topic}: {e}")
            raise

    async def publish_experiment_config(self, experiment_id: str, config_data: Dict[str, Any]):
        """Publish experiment configuration to Kafka"""
        message = {"experiment_id": experiment_id, "config": config_data, "timestamp": asyncio.get_event_loop().time()}
        await self.send_message(self.config.kafka.topics["experiment_config"], message, key=experiment_id)

    async def publish_experiment_start_command(self, experiment_id: str, runner_id: str):
        """Publish experiment start command"""
        message = {
            "experiment_id": experiment_id,
            "runner_id": runner_id,
            "action": "start",
            "timestamp": asyncio.get_event_loop().time(),
        }
        await self.send_message(self.config.kafka.topics["experiment_started"], message, key=experiment_id)

    async def publish_experiment_stop_command(self, experiment_id: str, runner_id: str):
        """Publish experiment stop command"""
        message = {
            "experiment_id": experiment_id,
            "runner_id": runner_id,
            "action": "stop",
            "timestamp": asyncio.get_event_loop().time(),
        }
        await self.send_message(self.config.kafka.topics["experiment_stopped"], message, key=experiment_id)

    async def start_consumer(self, topic_name: str, callback):
        """Start consumer for specific topic"""
        logger.info(f"Attempting to start consumer for topic: {topic_name}")

        # Check if Kafka is enabled - don't depend on producer being available
        if not self.config.kafka.enabled:
            logger.info(f"Kafka disabled, skipping consumer for topic: {topic_name}")
            return

        if topic_name in self.consumer_tasks:
            logger.info(f"Consumer already exists for topic: {topic_name}")
            return

        try:
            logger.info(f"Creating consumer for topic: {topic_name}")
            logger.info(f"Bootstrap servers: {self.config.kafka.bootstrap_servers}")
            logger.info(f"Group ID: {self.config.kafka.group_id}")

            consumer = AIOKafkaConsumer(
                topic_name,
                bootstrap_servers=self.config.kafka.bootstrap_servers,
                group_id=self.config.kafka.group_id,
                value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                key_deserializer=lambda k: k.decode("utf-8") if k else None,
            )

            logger.info(f"Starting consumer connection for topic: {topic_name}")
            await consumer.start()
            self.consumers[topic_name] = consumer
            logger.info(f"Consumer connection established for topic: {topic_name}")

            # Start consumer task
            logger.info(f"Creating consumer task for topic: {topic_name}")
            task = asyncio.create_task(self._consume_messages(consumer, callback))
            self.consumer_tasks[topic_name] = task
            logger.info(f"Successfully started consumer for topic: {topic_name}")
        except Exception as e:
            logger.error(f"Failed to start consumer for topic {topic_name}: {e}")
            import traceback

            logger.error(f"Consumer start traceback: {traceback.format_exc()}")
            raise

    async def _consume_messages(self, consumer: AIOKafkaConsumer, callback):
        """Consume messages from Kafka topic"""
        logger.info("Starting message consumption for consumer")
        try:
            async for message in consumer:
                try:
                    logger.info(f"Received message: {message.value} from topic: {message.topic}")
                    await callback(message.value, message.key, message.topic)
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    import traceback

                    logger.error(f"Message processing traceback: {traceback.format_exc()}")
        except Exception as e:
            logger.error(f"Consumer error: {e}")
            import traceback

            logger.error(f"Consumer traceback: {traceback.format_exc()}")
        finally:
            logger.info("Consumer task ended")

    async def cache_experiment_status(self, experiment_id: str, status: Dict[str, Any]):
        """Cache experiment status in Redis"""
        if not self.redis_client:
            logger.debug("Redis not available, skipping cache update")
            return
        key = f"experiment:{experiment_id}:status"
        await self.redis_client.setex(
            key,
            3600,  # 1 hour TTL
            json.dumps(status, ensure_ascii=False),
        )

    async def get_cached_experiment_status(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """Get cached experiment status from Redis"""
        if not self.redis_client:
            logger.debug("Redis not available, skipping cache lookup")
            return None
        key = f"experiment:{experiment_id}:status"
        data = await self.redis_client.get(key)
        if data:
            return json.loads(data.decode("utf-8"))
        return None
