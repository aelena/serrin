"""serrin -- an opinionated, entropic noise generation tool."""

from .chain import Chain, Slot, default_chain
from .envelope import Envelope
from .export import MappingConfig, build_render
from .ingest import auto_seed, ingest_csv
from .stream import MAX_VOICES, Stream

__version__ = "0.1.0"

__all__ = [
    "Chain",
    "Envelope",
    "MAX_VOICES",
    "MappingConfig",
    "Slot",
    "Stream",
    "auto_seed",
    "build_render",
    "default_chain",
    "ingest_csv",
    "__version__",
]
