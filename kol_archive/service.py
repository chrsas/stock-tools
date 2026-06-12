"""Backwards-compatible import path for the archive (now ``kol_archive.archive``)."""

from __future__ import annotations

from kol_archive.archive import Archive, is_healthy_feed_run, is_healthy_probe_run

__all__ = ["Archive", "is_healthy_feed_run", "is_healthy_probe_run"]
