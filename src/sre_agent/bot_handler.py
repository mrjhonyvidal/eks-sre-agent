"""
Backward-compatible interactive Lambda entrypoint.

This module is a thin re-export shim. The actual implementation lives in
`sre_agent.interactive.handler` (Lambda handler), `sre_agent.interactive.orchestrator`
(intent routing), and `sre_agent.interactive.strands_agent` (specialist agent).
The SAM template references `bot_handler.handler` here so that existing
deployments and integrations keep working without code changes.

If you are adding new behaviour, edit the modules under `sre_agent/interactive/`
rather than this file.
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
