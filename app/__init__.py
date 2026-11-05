"""Voice-Satellite – schlanker Voice-Assistent (2.0: Indexing via Master)."""
try:      # Build-Artefakt (scripts/gen_version.py): laeuft automatisch mit
    from ._version import __version__      # jedem Deploy weiter
except ImportError:
    __version__ = "2.0.0"                  # Quellstand ohne Build
