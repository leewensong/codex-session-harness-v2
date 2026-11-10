"""Public Harness adaptor contract (v1); no provider SDK required to import."""
from .contracts import AgentAdaptor, AdaptorError, Capabilities
from .registry import AdaptorRegistry

__all__ = ["AgentAdaptor", "AdaptorError", "Capabilities", "AdaptorRegistry"]
