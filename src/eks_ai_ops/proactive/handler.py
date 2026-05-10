from __future__ import annotations

import logging
from typing import Any

from eks_ai_ops.proactive.flow import ProactiveIncidentFlow

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    logger.info("Received proactive event source=%s", event.get("source", "unknown"))
    return ProactiveIncidentFlow().process(event)
