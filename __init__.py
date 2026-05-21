"""signal-group-chat — Signal platform plugin for Hermes Agent.

Subclasses the upstream Signal adapter and adds access control, delete-watch,
file staging, and a /single ↔ /group mode system. Registers as platform
"signal", overriding the built-in adapter.

``register`` is exposed lazily so importing this package (e.g. to reach a single
submodule, or during test collection) doesn't pull in the full adapter stack.
"""

__all__ = ["register"]
__version__ = "0.1.0"


def __getattr__(name):
    if name == "register":
        from .register import register

        return register
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
