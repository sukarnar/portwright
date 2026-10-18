"""Directory adapters (Strategy): Active Directory over LDAPS (ldap3) or the PowerShell AD module.

Both expose the same small interface, so group loads do not care how AD is reached.
"""
from .base import Directory, DirectoryError, DirEntry, open_directory

__all__ = ["Directory", "DirectoryError", "DirEntry", "open_directory"]
