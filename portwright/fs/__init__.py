"""Sandboxed filesystem access: named roots, write-only destinations, audited atomic operations."""
from .sandbox import FsError, Place, Sandbox, sha256_file

__all__ = ["FsError", "Place", "Sandbox", "sha256_file"]
