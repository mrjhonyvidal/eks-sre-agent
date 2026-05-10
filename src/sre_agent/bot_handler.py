"""
Backward-compatible interactive Lambda entrypoint.

The implementation now lives in `sre_agent.interactive.handler`.
"""

from sre_agent.interactive.handler import _find_incident_from_thread as _find_incident_from_thread
from sre_agent.interactive.handler import _get_incident_table as _get_incident_table
from sre_agent.interactive.handler import _get_orchestrator as _get_orchestrator
from sre_agent.interactive.handler import _handle_block_action as _handle_block_action
from sre_agent.interactive.handler import _handle_mention as _handle_mention
from sre_agent.interactive.handler import _post_reply as _post_reply
from sre_agent.interactive.handler import _update_incident_status as _update_incident_status
from sre_agent.interactive.handler import _verify_slack_signature as _verify_slack_signature
from sre_agent.interactive.handler import handler as handler
from sre_agent.interactive.handler import incident_table as incident_table

__all__ = [
    "_find_incident_from_thread",
    "_get_incident_table",
    "_get_orchestrator",
    "_handle_block_action",
    "_handle_mention",
    "_post_reply",
    "_update_incident_status",
    "_verify_slack_signature",
    "handler",
    "incident_table",
]
