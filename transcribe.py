#!/usr/bin/env python3
"""Transcribe cleaned audio with Azure Speech MAI-Transcribe-2."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv


OUT_DIR = Path(__file__).resolve().parent / "out"
API_VERSION = "2025-10-15"
MODEL = "MAI-Transcribe-2"


def load_config() -> tuple[str, str]:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    key = os.getenv("AZURE_SPEECH_KEY", "").strip()
    endpoint = os.getenv("AZURE_SPEECH_ENDPOINT", "").strip().rstrip("/")
    if not key or not endpoint:
        raise SystemExit(
            "Missing AZURE_SPEECH_KEY or AZURE_SPEECH_ENDPOINT in .env"
        )
    return key, endpoint


def extract_transcript(payload: dict) -> str:
    """Pull readable text from Fast Transcription response shapes."""
    if isinstance(payload.get("combinedPhrases"), list):
        parts = [
            p.get("text", "").strip()
            for p in payload["combinedPhrases"]
            if isinstance(p, dict) and p.get("text")
        ]
        if parts:
            return "\n".join(parts).strip()

    if isinstance(payload.get("phrases"), list):
        parts = [
            p.get("text", "").strip()
            for p in payload["phrases"]
            if isinstance(p, dict) and p.get("text")
        ]
        if parts:
            return "\n".join(parts).strip()

    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    return json.dumps(payload, indent=2, ensure_ascii=False)


def transcribe(audio_path: Path, out_dir: Path | None = None) -> tuple[Path, Path]:
    if not audio_path.is_file():
        raise SystemExit(f"Audio not found: {audio_path}")

    key, endpoint = load_config()
    url = f"{endpoint}/speechtotext/transcriptions:transcribe"
    definition = {
        "enhancedMode": {
            "enabled": True,
            "model": MODEL,
            "modelOptions": {
                "transcribeStyle": "clean",
            },
        }
    }

    dest = out_dir or OUT_DIR
    dest.mkdir(parents=True, exist_ok=True)
    stem = audio_path.stem.replace(".clean", "") or audio_path.stem
    json_path = dest / f"{stem}.json"
    txt_path = dest / f"{stem}.txt"

    with audio_path.open("rb") as audio_file:
        response = requests.post(
            url,
            params={"api-version": API_VERSION},
            headers={"Ocp-Apim-Subscription-Key": key},
            files={
                "audio": (audio_path.name, audio_file, "audio/wav"),
                "definition": (None, json.dumps(definition), "application/json"),
            },
            timeout=300,
        )

    if response.status_code >= 400:
        raise SystemExit(
            f"Transcription failed ({response.status_code}):\n{response.text}"
        )

    try:
        payload = response.json()
    except ValueError:
        raise SystemExit(f"Non-JSON response:\n{response.text}") from None

    transcript = extract_transcript(payload)
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    txt_path.write_text(transcript + "\n", encoding="utf-8")

    print(f"Model:      {MODEL}")
    print(f"JSON:       {json_path}")
    print(f"Transcript: {txt_path}")
    preview = transcript if len(transcript) <= 500 else transcript[:500] + "…"
    print("---")
    print(preview)
    return txt_path, json_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Transcribe audio with Azure MAI-Transcribe-2."
    )
    parser.add_argument("audio", type=Path, help="Path to WAV/MP3/FLAC audio")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory (default: out/)",
    )
    args = parser.parse_args(argv)
    transcribe(args.audio, args.out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
