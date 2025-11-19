#!/usr/bin/env python3

import io
import math
import os
import tempfile
import time
from typing import Dict, Tuple

from loguru import logger
from PIL import Image, ImageDraw

from ..config import load_config
from .storage_service import StorageService


class VisualizationService:
    """Utility to generate placeholder visual assets and upload to S3 + local disk."""

    def __init__(self, storage_service: StorageService, base_local_dir: str = None):
        self.storage_service = storage_service
        # Use system temporary directory instead of /app which might be read-only
        self.base_local_dir = base_local_dir or os.path.join(tempfile.gettempdir(), "gigaevo_experiments")
        self.config = load_config()

    def _ensure_dir(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def _current_color(self) -> Tuple[int, int, int]:
        palette = [
            (220, 20, 60),
            (65, 105, 225),
            (34, 139, 34),
            (255, 140, 0),
            (148, 0, 211),
            (0, 206, 209),
        ]
        idx = int(time.time() // 20) % len(palette)
        return palette[idx]

    def _draw_flower(self, image_size: int = 512, petal_count: int = 8) -> Image.Image:
        img = Image.new("RGB", (image_size, image_size), (250, 250, 250))
        draw = ImageDraw.Draw(img)

        center = (image_size // 2, image_size // 2)
        radius_outer = int(image_size * 0.38)
        radius_inner = int(image_size * 0.18)
        petal_color = self._current_color()

        for k in range(petal_count):
            angle = (2 * math.pi * k) / petal_count
            cx = center[0] + int(math.cos(angle) * radius_inner)
            cy = center[1] + int(math.sin(angle) * radius_inner)
            bbox = [
                cx - radius_outer // 2,
                cy - radius_outer // 2,
                cx + radius_outer // 2,
                cy + radius_outer // 2,
            ]
            draw.ellipse(bbox, fill=petal_color, outline=(80, 80, 80))

        center_radius = int(image_size * 0.12)
        center_bbox = [
            center[0] - center_radius,
            center[1] - center_radius,
            center[0] + center_radius,
            center[1] + center_radius,
        ]
        draw.ellipse(center_bbox, fill=(255, 215, 0), outline=(120, 120, 120))

        ts_text = time.strftime("%H:%M:%S")
        draw.text((10, image_size - 24), f"{ts_text}", fill=(30, 30, 30))
        return img

    def _draw_metrics_plot(self, metrics: Dict[str, float], image_size: Tuple[int, int] = (640, 360)) -> Image.Image:
        width, height = image_size
        img = Image.new("RGB", (width, height), (255, 255, 255))
        draw = ImageDraw.Draw(img)

        left, top, right, bottom = 60, 30, width - 20, height - 50
        draw.rectangle([left, top, right, bottom], outline=(120, 120, 120), width=2)

        keys = list(metrics.keys())
        values = [float(metrics[k]) for k in keys]
        if not keys:
            draw.text((left + 10, top + 10), "No metrics", fill=(0, 0, 0))
            return img

        max_val = max(values) if values else 1.0
        bar_w = max(10, int((right - left - 20) / max(1, len(values))))

        for i, (k, v) in enumerate(zip(keys, values)):
            x0 = left + 10 + i * bar_w
            x1 = x0 + bar_w - 4
            bar_h = int((v / (max_val or 1.0)) * (bottom - top - 10))
            y0 = bottom - bar_h
            color = self._current_color()
            draw.rectangle([x0, y0, x1, bottom - 1], fill=color, outline=(80, 80, 80))
            draw.text((x0, bottom + 5), k[:6], fill=(0, 0, 0))

        ts_text = time.strftime("%H:%M:%S")
        draw.text((right - 80, top + 5), ts_text, fill=(0, 0, 0))
        return img

    def _to_bytes(self, img: Image.Image, fmt: str = "JPEG") -> bytes:
        buf = io.BytesIO()
        img.save(buf, format=fmt)
        return buf.getvalue()

    async def generate_and_store(
        self, experiment_id: str, metrics: Dict[str, float], seconds_since_start: int | None = None
    ) -> Dict[str, str]:
        """Generate assets, save locally, upload to S3 under experiments_results/{id}/.

        Returns mapping of logical names to S3 object paths.
        """
        exp_dir = os.path.join(self.base_local_dir, experiment_id)
        self._ensure_dir(exp_dir)

        base_prefix = f"experiments_results/{experiment_id}/"

        # Optional flower image (JPEG) for backward compatibility
        flower_img = self._draw_flower()
        flower_bytes = self._to_bytes(flower_img, fmt="JPEG")
        local_flower = os.path.join(exp_dir, "visual_results.jpeg")
        with open(local_flower, "wb") as f:
            f.write(flower_bytes)
        flower_object = f"{base_prefix}visual_results.jpeg"
        await self.storage_service.upload_bytes(
            flower_bytes, flower_object, metadata={"type": "visual_image", "experiment_id": experiment_id}
        )

        # Metrics plot image (PNG)
        plot_img = self._draw_metrics_plot(metrics)
        plot_bytes = self._to_bytes(plot_img, fmt="PNG")
        local_plot = os.path.join(exp_dir, "metrics_plot.png")
        with open(local_plot, "wb") as f:
            f.write(plot_bytes)

        plot_object = f"{base_prefix}metrics_plot.png"
        await self.storage_service.upload_bytes(
            plot_bytes, plot_object, metadata={"type": "metrics_plot", "experiment_id": experiment_id}
        )

        logger.info(
            f"Updated visuals for experiment {experiment_id}: {flower_object}, {plot_object} (and local copies)"
        )

        return {"visual_image": flower_object, "plot_image": plot_object}
