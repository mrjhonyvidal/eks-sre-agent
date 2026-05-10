"""
sre_agent — EKS SRE AI Agent package.

Supports multiple LLM backends:
  - Anthropic Claude (direct API)
  - Amazon Bedrock (Claude / Titan / Nova via AWS-native SDK)

Set the LLM_PROVIDER environment variable to select the backend:
  LLM_PROVIDER=anthropic   (default, uses ANTHROPIC_API_KEY)
  LLM_PROVIDER=bedrock     (uses IAM role / instance profile)
"""

__version__ = "1.0.0"
__all__ = ["InteractiveEKSAgent", "ProactiveIncidentFlow", "SREAgent", "get_llm_client"]

from sre_agent.agent import SREAgent
from sre_agent.interactive import InteractiveEKSAgent
from sre_agent.llm_client import get_llm_client
from sre_agent.proactive import ProactiveIncidentFlow
