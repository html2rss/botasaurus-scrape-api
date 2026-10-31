"""Installed dependency versions for health and startup telemetry."""

from importlib.metadata import PackageNotFoundError, version


def botasaurus_version() -> str:
    try:
        return version("botasaurus")
    except PackageNotFoundError:
        return "unknown"
