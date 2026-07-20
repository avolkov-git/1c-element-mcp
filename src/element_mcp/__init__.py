from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("1c-element-mcp")
except PackageNotFoundError:  # pragma: no cover - package metadata exists in normal installs
    __version__ = "0.0.0"


__all__ = ["__version__"]
