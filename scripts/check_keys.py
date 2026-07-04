"""Check whether your provider API keys are valid — fast and (near) free.

Uses each SDK's `models.list()` endpoint, which validates authentication WITHOUT
generating any tokens, so this costs nothing and runs in a second or two. It does
NOT call verdict, capture anything, or generate completions — it only answers, per
provider: is the key present, is it valid, is the SDK installed.

Usage:
    export ANTHROPIC_API_KEY=...     # set whichever you have
    export OPENAI_API_KEY=...
    export GOOGLE_API_KEY=...         # (or GEMINI_API_KEY)
    python scripts/check_keys.py

Exit code 0 if every key that is SET is valid; 1 if any set key is invalid.
Missing keys and missing SDKs are reported but do not fail the run.
"""

from __future__ import annotations

import os
import sys

OK = "\033[32mVALID\033[0m"
BAD = "\033[31mINVALID\033[0m"
SKIP = "\033[33m—\033[0m"


def check_anthropic() -> str | None:
    """Return None=missing key, 'ok', 'sdk', or an error string."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return "sdk"
    try:
        anthropic.Anthropic().models.list(limit=1)
        return "ok"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def check_openai() -> str | None:
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    try:
        import openai
    except ImportError:
        return "sdk"
    try:
        openai.OpenAI().models.list()
        return "ok"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def check_google() -> str | None:
    if not (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")):
        return None
    try:
        from google import genai
    except ImportError:
        return "sdk"
    try:
        # Hold a reference to the client: models.list() returns a LAZY pager, so
        # a throwaway `genai.Client().models.list()` lets the client get GC'd and
        # closed before the pager actually sends the request ("client has been
        # closed"). Keep `client` alive until after we force the request.
        client = genai.Client()
        next(iter(client.models.list()), None)
        return "ok"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


CHECKS = {
    "anthropic": ("ANTHROPIC_API_KEY", check_anthropic),
    "openai": ("OPENAI_API_KEY", check_openai),
    "google": ("GOOGLE_API_KEY/GEMINI_API_KEY", check_google),
}


def main() -> int:
    any_invalid = False
    any_set = False
    print("Provider key check (models.list — no token cost):\n")
    for name, (env, fn) in CHECKS.items():
        result = fn()
        if result is None:
            print(f"  {SKIP}  {name:10s}  no key set ({env})")
        elif result == "ok":
            any_set = True
            print(f"  {OK}  {name:10s}  key works")
        elif result == "sdk":
            print(f"  {SKIP}  {name:10s}  key set but SDK not installed")
        else:
            any_set = True
            any_invalid = True
            print(f"  {BAD}  {name:10s}  {result}")
    print()
    if not any_set:
        print("No usable keys found. Set ANTHROPIC_API_KEY / OPENAI_API_KEY / "
              "GOOGLE_API_KEY and install the matching SDK(s).")
        return 0
    if any_invalid:
        print("At least one SET key is invalid (see above).")
        return 1
    print("All keys that are set are valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
