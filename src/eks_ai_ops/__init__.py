"""
eks_ai_ops — EKS AI Ops Toolkit package.

Bundles the proactive incident flow and the interactive Slack bot,
backed by multiple LLM providers:
  - Anthropic Claude (direct API)
  - Amazon Bedrock (Claude / Titan / Nova via AWS-native SDK)

Set the LLM_PROVIDER environment variable to select the backend:
  LLM_PROVIDER=anthropic   (default, uses ANTHROPIC_API_KEY)
  LLM_PROVIDER=bedrock     (uses IAM role / instance profile)
"""

__version__ = "1.0.0"
__all__ = ["InteractiveEKSAgent", "ProactiveIncidentFlow", "SREAgent", "get_llm_client"]

from eks_ai_ops.agent import SREAgent
from eks_ai_ops.interactive import InteractiveEKSAgent
from eks_ai_ops.llm_client import get_llm_client
from eks_ai_ops.proactive import ProactiveIncidentFlow
