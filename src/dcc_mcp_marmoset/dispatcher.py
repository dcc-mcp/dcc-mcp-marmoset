"""Execution adapter for Toolbag bridge-backed skill scripts."""

from __future__ import annotations

from typing import Any, Callable


class MarmosetBridgeDispatcher:
    """Run wrappers inline; the Toolbag plugin performs host work on its main thread."""

    def dispatch_callable(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        for key in (
            "affinity",
            "context",
            "action_name",
            "skill_name",
            "execution",
            "timeout_hint_secs",
            "thread_affinity",
        ):
            kwargs.pop(key, None)
        return func(*args, **kwargs)
