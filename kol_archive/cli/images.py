"""Image evidence commands: download, OCR, and vision descriptions."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
from typing import Any

from kol_archive.collector import HEADERS
from kol_archive.config import load_config, resolve_cookie
from kol_archive.image_enrich import load_vision_settings, run_image_enrichment
from kol_archive.images import ImageDownloader, ImageDownloadSettings
from kol_archive.obs import http_client
from kol_archive.ocr import run_ocr, select_engine

from .common import connect_existing_archive, print_json, resolve_db_path


def _images_section(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("images") or {}
    if not isinstance(value, dict):
        raise ValueError("images must be a mapping")
    return value


def _download_images_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    images = _images_section(config)
    cookie, _ = resolve_cookie(config)
    settings = ImageDownloadSettings(
        request_min_interval_seconds=float(images.get("request_min_interval_seconds") or 1.0),
        request_jitter_seconds=float(images.get("request_jitter_seconds") or 1.0),
        max_image_bytes=int(images.get("max_image_bytes") or 8 * 1024 * 1024),
        max_batch_bytes=int(images.get("max_batch_bytes") or 256 * 1024 * 1024),
    )
    connection, archive = connect_existing_archive(resolve_db_path(args.path, config))
    # Images are static CDN assets, fetched directly (not through the feed's WAF
    # path); a dead/blocked link is recorded as a failed attempt, not raised.
    client = http_client(headers=HEADERS, timeout=30.0, follow_redirects=True)
    if cookie:
        client.headers["cookie"] = cookie
    try:
        downloader = ImageDownloader(archive, client, settings)
        row_ids = downloader.download_pending(post_id=args.post_id, limit=args.limit)
        print_json({"download_attempts": len(row_ids)})
    finally:
        client.close()
        connection.close()


def _ocr_images_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    connection, archive = connect_existing_archive(resolve_db_path(args.path, config))
    try:
        engine = select_engine()
        row_ids = run_ocr(archive, engine, post_id=args.post_id, limit=args.limit)
        print_json(
            {
                "engine": engine.name,
                "engine_version": engine.version,
                "ocr_added": len(row_ids),
            }
        )
    finally:
        connection.close()


def _enrich_images_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    settings = load_vision_settings(config)
    if args.prompt_version:
        settings = replace(settings, prompt_version=args.prompt_version)
    connection, archive = connect_existing_archive(resolve_db_path(args.path, config))
    try:
        row_ids = run_image_enrichment(archive, settings, post_id=args.post_id, limit=args.limit)
        print_json(
            {
                "model": settings.model,
                "prompt_version": settings.prompt_version,
                "described": len(row_ids),
            }
        )
    finally:
        connection.close()


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    download_images_parser = subparsers.add_parser(
        "download-images", help="fetch and store image bytes for archived versions"
    )
    download_images_parser.add_argument("--post-id", type=int, help="restrict to one post")
    download_images_parser.add_argument("--limit", type=int, help="cap images fetched this run")
    download_images_parser.add_argument("--path", type=Path)
    download_images_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    download_images_parser.set_defaults(handler=_download_images_command)
    ocr_images_parser = subparsers.add_parser(
        "ocr-images", help="transcribe stored images (winocr, tesseract fallback)"
    )
    ocr_images_parser.add_argument("--post-id", type=int, help="restrict to one post")
    ocr_images_parser.add_argument("--limit", type=int, help="cap images transcribed this run")
    ocr_images_parser.add_argument("--path", type=Path)
    ocr_images_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    ocr_images_parser.set_defaults(handler=_ocr_images_command)
    enrich_images_parser = subparsers.add_parser(
        "enrich-images", help="describe stored images with a vision model (inference, not evidence)"
    )
    enrich_images_parser.add_argument("--post-id", type=int, help="restrict to one post")
    enrich_images_parser.add_argument("--limit", type=int, help="cap images described this run")
    enrich_images_parser.add_argument(
        "--prompt-version", help="override llm.vision_prompt_version for this run"
    )
    enrich_images_parser.add_argument("--path", type=Path)
    enrich_images_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    enrich_images_parser.set_defaults(handler=_enrich_images_command)
