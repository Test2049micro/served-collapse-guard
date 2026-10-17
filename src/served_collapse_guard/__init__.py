"""Zero-dependency auditor for OpenAI-compatible gateways: which model actually served you?"""

from .core import JudgeReport, ProbeResult, judge_seats, probe_model, probe_once
from .vendors import VendorMap

__all__ = [
    "ProbeResult",
    "JudgeReport",
    "probe_once",
    "probe_model",
    "judge_seats",
    "VendorMap",
    "__version__",
]

__version__ = "0.1.0"
