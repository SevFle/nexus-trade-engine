from engine.plugins.sandbox import StrategySandbox

__all__ = ["StrategySandbox"]


def __getattr__(name: str):
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
