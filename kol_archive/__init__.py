"""Local append-only archive for KOL post evidence."""

from kol_archive.database import connect_database, initialize_database
from kol_archive.service import Archive

__all__ = ["Archive", "connect_database", "initialize_database"]
