#!/usr/bin/env python3
"""Preprocess audio then transcribe with MAI-Transcribe-2."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from preprocess import (
    DEFAULT_KEEP_SILENCE_S,
    DEFAULT_MIN_PAUSE_S,
    DEFAULT_MODE,
    DEFAULT_THRESHOLD_DB,
    preprocess,
)
from transcribe import transcribe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Cost + accuracy: compress long dead-air, keep speech, "
            "then transcribe with MAI-Transcribe-2. Default mode=smart."
        )
    )
    parser.add_argument("input", type=Path, help="Path to input audio file")
    parser.add_argument(
        "--skip-transcribe",
        action="store_true",
        help="Only preprocess; do not call the API",
    )
    parser.add_argument(
        "--mode",
        choices=["smart", "off", "edges", "long"],
        default=DEFAULT_MODE,
        help=(
            "smart=compress dead-air keep speech (default), "
            "off=no cut, edges=trim ends only, long=legacy"
        ),
    )
    parser.add_argument(
        "--threshold-db",
        type=float,
        default=DEFAULT_THRESHOLD_DB,
        help=f"Silence threshold in dB (default: {DEFAULT_THRESHOLD_DB})",
    )
    parser.add_argument(
        "--min-pause",
        type=float,
        default=DEFAULT_MIN_PAUSE_S,
        help=f"Only compress dead-air pauses >= seconds (default: {DEFAULT_MIN_PAUSE_S})",
    )
    parser.add_argument(
        "--keep-silence",
        type=float,
        default=DEFAULT_KEEP_SILENCE_S,
        help=f"Seconds of pause left after compressing a long gap (default: {DEFAULT_KEEP_SILENCE_S})",
    )
    args = parser.parse_args(argv)

    print("== Preprocess ==")
    cleaned = preprocess(
        args.input,
        mode=args.mode,
        threshold_db=args.threshold_db,
        min_pause_s=args.min_pause,
        keep_silence_s=args.keep_silence,
    )
    if args.skip_transcribe:
        print("Skipping transcription (--skip-transcribe).")
        return 0

    print("\n== Transcribe ==")
    transcribe(cleaned)
    return 0


if __name__ == "__main__":
    sys.exit(main())
