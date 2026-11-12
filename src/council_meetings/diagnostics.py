"""Content-free process memory diagnostics for the in-process pipeline."""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def log_memory(stage: str, document_id: int | None = None) -> None:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                key, value = line.split(":", 1)
                values[key] = int(value.split()[0])
    except (OSError, ValueError):
        # Non-Linux hosts or unavailable procfs must not stop the pipeline.
        values = {}
    logger.info(
        "Memory stage=%s document_id=%s rss_kib=%s peak_rss_kib=%s",
        stage,
        document_id,
        values.get("VmRSS"),
        values.get("VmHWM"),
    )
