"""Unofficial, read-only MCP server for the Garmin Connect Personal API."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("garmin-health-mcp")
except PackageNotFoundError:  # pragma: no cover - running from an uninstalled checkout
    __version__ = "0.0.0"
