"""Auto-instrumentors. Each one targets a specific LLM SDK and patches its call
sites at import time via `wrapt`.

1. `available()` returns True if the target SDK is installed in this process.
2. `install()` calls `wrapt.wrap_function_wrapper` on every call site we care about.
3. `uninstall()` reverses it (used in tests).
4. Wrappers convert SDK-shaped calls into vendor-neutral `Trace` records
   and hand them to `client.storage`.
"""

from verdict.instrumentors.anthropic import AnthropicInstrumentor
from verdict.instrumentors.base import BaseInstrumentor
from verdict.instrumentors.google import GoogleInstrumentor
from verdict.instrumentors.openai import OpenAIInstrumentor

__all__ = [
    "AnthropicInstrumentor",
    "BaseInstrumentor",
    "GoogleInstrumentor",
    "OpenAIInstrumentor",
]
