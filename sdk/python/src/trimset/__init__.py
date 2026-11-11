"""trimset: gate decision-model answers locally with live, calibrated policies."""

from importlib.metadata import version

from trimset.client import Client, GateResult

__version__ = version("trimset")
__all__ = ["Client", "GateResult", "__version__"]
