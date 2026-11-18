#!/usr/bin/env python3
"""Convert audio to mono 16 kHz WAV with speech-safe pause compression."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


OUT_DIR = Path(__file__).resolve().parent / "out"

# Practical default: compress long dead-air, keep all speech segments.
# Modes:
#   smart = detect silence, keep all speech, cap long dead-air (default)
#   off   = convert only (max retention, no cost save)
#   edges = trim leading/trailing dead air only
#   long  = older silenceremove mid-call trim (riskier)
DEFAULT_MODE = "smart"
DEFAULT_THRESHOLD_DB = -32.0
DEFAULT_MIN_PAUSE_S = 0.6  # compress dead-air at least this long
DEFAULT_KEEP_SILENCE_S = 0.25  # leave this much pause after compression
DEFAULT_START_DURATION_S = 0.2
OVERTRIM_WARN_PCT = 30.0

SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)")
DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def build_filter(
    mode: str = DEFAULT_MODE,
    threshold_db: float = DEFAULT_THRESHOLD_DB,
    min_pause_s: float = DEFAULT_MIN_PAUSE_S,
    keep_silence_s: float = DEFAULT_KEEP_SILENCE_S,
    start_duration_s: float = DEFAULT_START_DURATION_S,
) -> str | None:
    """Return ffmpeg -af for simple modes; None means custom smart path."""
    mode = mode.lower().strip()
    if mode == "smart":
        return None
    if mode == "off":
        return "highpass=f=80"

    thr = f"{threshold_db:g}dB"
    if mode == "edges":
        filt = (
            "highpass=f=80,"
            "silenceremove="
            f"start_periods=1:start_duration={start_duration_s:g}:start_threshold={thr}:"
            f"stop_periods=1:stop_duration={start_duration_s:g}:stop_threshold={thr}"
        )
    elif mode == "long":
        filt = (
            "highpass=f=80,"
            "silenceremove="
            f"start_periods=1:start_duration={start_duration_s:g}:start_threshold={thr}:"
            f"stop_periods=-1:stop_duration={min_pause_s:g}:stop_threshold={thr}"
        )
    else:
        raise SystemExit(f"Unknown mode {mode!r}. Use: smart | off | edges | long")

    if keep_silence_s > 0 and mode == "long":
        filt += f":stop_silence={keep_silence_s:g}"
    return filt


def resolve_tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    raise SystemExit(
        f"{name} not found. Install system ffmpeg (`sudo apt install -y ffmpeg`) "
        "or `pip install imageio-ffmpeg`."
    )


def probe_duration(path: Path, ffmpeg: str, ffprobe: str | None) -> float | None:
    if ffprobe:
        cmd = [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            duration = data.get("format", {}).get("duration")
            return float(duration) if duration is not None else None
        except (subprocess.CalledProcessError, ValueError, json.JSONDecodeError, TypeError):
            pass

    result = subprocess.run([ffmpeg, "-i", str(path)], capture_output=True, text=True)
    match = DURATION_RE.search(result.stderr or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def format_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    minutes, secs = divmod(seconds, 60)
    return f"{int(minutes)}m {secs:05.2f}s ({seconds:.2f}s)"


def convert_base_wav(ffmpeg: str, input_path: Path, output_path: Path) -> None:
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(input_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-af",
        "highpass=f=80",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            "ffmpeg failed to decode/convert this file.\n"
            "If the file has no extension, try renaming it to a known format "
            "(e.g. .webm, .m4a, .ogg, .mp3) and re-run.\n\n"
            f"{result.stderr.strip()}"
        )


def detect_silences(
    ffmpeg: str,
    wav_path: Path,
    threshold_db: float,
    min_silence_detect_s: float = 0.35,
) -> list[tuple[float, float]]:
    """Return list of (silence_start, silence_end) seconds."""
    cmd = [
        ffmpeg,
        "-i",
        str(wav_path),
        "-af",
        f"silencedetect=noise={threshold_db:g}dB:d={min_silence_detect_s:g}",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    log = (result.stderr or "") + (result.stdout or "")
    starts = [float(m.group(1)) for m in SILENCE_START_RE.finditer(log)]
    ends = [float(m.group(1)) for m in SILENCE_END_RE.finditer(log)]

    silences: list[tuple[float, float]] = []
    # Pair starts/ends in order; tolerate trailing open silence.
    ei = 0
    for start in starts:
        while ei < len(ends) and ends[ei] < start:
            ei += 1
        if ei < len(ends):
            silences.append((start, ends[ei]))
            ei += 1
    return silences


def build_keep_segments(
    duration: float,
    silences: list[tuple[float, float]],
    min_pause_s: float,
    keep_silence_s: float,
) -> list[tuple[float, float]]:
    """
    Keep every non-silence (speech) fully.
    For silence:
      - leading/trailing: keep at most keep_silence_s
      - mid gaps < min_pause_s: keep fully (natural turn gap)
      - mid gaps >= min_pause_s: keep only keep_silence_s (compress dead air)
    """
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    keep_silence_s = max(0.05, keep_silence_s)

    def add(a: float, b: float) -> None:
        a = max(0.0, min(a, duration))
        b = max(0.0, min(b, duration))
        if b - a > 0.001:
            if keep and abs(keep[-1][1] - a) < 0.001:
                keep[-1] = (keep[-1][0], b)
            else:
                keep.append((a, b))

    for s0, s1 in silences:
        s0 = max(0.0, min(s0, duration))
        s1 = max(0.0, min(s1, duration))
        if s1 <= s0:
            continue

        # speech before this silence
        if s0 > cursor:
            add(cursor, s0)

        gap = s1 - s0
        is_leading = s0 <= 0.01
        is_trailing = s1 >= duration - 0.01

        if is_leading or is_trailing:
            # Drop most edge dead-air; keep a tiny pad if speech exists on the inner side.
            if gap > keep_silence_s:
                if is_leading:
                    add(s1 - keep_silence_s, s1)
                else:
                    add(s0, s0 + keep_silence_s)
            else:
                add(s0, s1)
        elif gap >= min_pause_s:
            # Long mid dead-air: keep a short natural pause only.
            add(s0, s0 + keep_silence_s)
        else:
            # Short natural pause: keep all of it.
            add(s0, s1)

        cursor = s1

    if cursor < duration:
        add(cursor, duration)

    if not keep:
        keep = [(0.0, duration)]
    return keep


def render_keep_segments(
    ffmpeg: str,
    wav_in: Path,
    wav_out: Path,
    segments: list[tuple[float, float]],
) -> None:
    # Filter graph: atrim+asetpts per segment, then concat.
    parts: list[str] = []
    labels: list[str] = []
    for i, (start, end) in enumerate(segments):
        label = f"s{i}"
        labels.append(f"[{label}]")
        parts.append(
            f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[{label}]"
        )
    if len(segments) == 1:
        parts.append(f"{labels[0]}anull[outa]")
    else:
        parts.append(
            f"{''.join(labels)}concat=n={len(segments)}:v=0:a=1[outa]"
        )
    filter_complex = ";".join(parts)

    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(wav_in),
        "-filter_complex",
        filter_complex,
        "-map",
        "[outa]",
        "-c:a",
        "pcm_s16le",
        str(wav_out),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            "ffmpeg failed while compressing pauses.\n\n" + result.stderr.strip()
        )


def preprocess_smart(
    ffmpeg: str,
    ffprobe: str | None,
    input_path: Path,
    output_path: Path,
    threshold_db: float,
    min_pause_s: float,
    keep_silence_s: float,
) -> tuple[float | None, float | None, int]:
    with tempfile.TemporaryDirectory(prefix="transcribe_") as tmp:
        base = Path(tmp) / "base.wav"
        convert_base_wav(ffmpeg, input_path, base)
        before = probe_duration(base, ffmpeg, ffprobe)
        if before is None or before <= 0:
            raise SystemExit("Could not read converted audio duration.")

        silences = detect_silences(ffmpeg, base, threshold_db)
        segments = build_keep_segments(before, silences, min_pause_s, keep_silence_s)
        render_keep_segments(ffmpeg, base, output_path, segments)
        after = probe_duration(output_path, ffmpeg, ffprobe)
        return before, after, len(silences)


def preprocess(
    input_path: Path,
    output_path: Path | None = None,
    mode: str = DEFAULT_MODE,
    threshold_db: float = DEFAULT_THRESHOLD_DB,
    min_pause_s: float = DEFAULT_MIN_PAUSE_S,
    keep_silence_s: float = DEFAULT_KEEP_SILENCE_S,
) -> Path:
    ffmpeg = resolve_tool("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    if not input_path.is_file():
        raise SystemExit(f"Input not found: {input_path}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if output_path is None:
        stem = input_path.stem if input_path.suffix else input_path.name
        output_path = OUT_DIR / f"{stem}.clean.wav"
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)

    mode_norm = mode.lower().strip()
    silence_count = 0

    if mode_norm == "smart":
        before, after, silence_count = preprocess_smart(
            ffmpeg,
            ffprobe,
            input_path,
            output_path,
            threshold_db=threshold_db,
            min_pause_s=min_pause_s,
            keep_silence_s=keep_silence_s if keep_silence_s > 0 else DEFAULT_KEEP_SILENCE_S,
        )
    else:
        before = probe_duration(input_path, ffmpeg, ffprobe)
        audio_filter = build_filter(mode_norm, threshold_db, min_pause_s, keep_silence_s)
        assert audio_filter is not None
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(input_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-af",
            audio_filter,
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(
                "ffmpeg failed to decode/convert this file.\n"
                "If the file has no extension, try renaming it to a known format "
                "(e.g. .webm, .m4a, .ogg, .mp3) and re-run.\n\n"
                f"{result.stderr.strip()}"
            )
        after = probe_duration(output_path, ffmpeg, ffprobe)

    print(f"Input:  {input_path}")
    print(f"Output: {output_path}")
    print(f"Mode:   {mode_norm}")
    if mode_norm == "smart":
        print(
            f"Filter: speech-safe pause compress "
            f"(keep all speech; cap dead-air >= {min_pause_s:g}s to "
            f"{(keep_silence_s if keep_silence_s > 0 else DEFAULT_KEEP_SILENCE_S):g}s; "
            f"threshold {threshold_db:g} dB; silences detected={silence_count})"
        )
        print("Note:   cost + accuracy balance — only flat no-conversation stretches are shortened")
    elif mode_norm == "off":
        print("Filter: convert only (no pause removal — max word retention)")
        print("Note:   no cost save; use --mode smart for dead-air compression")
    elif mode_norm == "edges":
        print("Filter: trim leading/trailing dead air only (keeps all mid-call pauses)")
    else:
        print(
            f"Filter: compress flat pauses >= {min_pause_s:g}s "
            f"(threshold {threshold_db:g} dB)"
        )
        print("Note:   legacy long mode — riskier for soft/edge words; prefer smart")

    print(f"Before: {format_seconds(before)}")
    print(f"After:  {format_seconds(after)}")
    if before is not None and after is not None and before > 0:
        saved = before - after
        pct = (saved / before) * 100
        print(f"Saved:  {format_seconds(saved)} ({pct:.1f}% shorter)")
        if mode_norm != "off" and pct > OVERTRIM_WARN_PCT:
            print(
                f"WARNING: removed {pct:.1f}% of audio (> {OVERTRIM_WARN_PCT:g}%). "
                "Spot-check important details (payments, names). "
                "Use --mode off if anything critical is missing."
            )
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Convert audio to mono 16 kHz WAV. "
            "Default mode=smart keeps all speech and compresses long dead-air for cost."
        )
    )
    parser.add_argument("input", type=Path, help="Path to input audio file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output WAV path (default: out/<name>.clean.wav)",
    )
    parser.add_argument(
        "--mode",
        choices=["smart", "off", "edges", "long"],
        default=DEFAULT_MODE,
        help="smart=compress dead-air keep speech (default), off=no cut, edges=ends only, long=legacy",
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
        help=f"Only compress dead-air pauses >= this many seconds (default: {DEFAULT_MIN_PAUSE_S})",
    )
    parser.add_argument(
        "--keep-silence",
        type=float,
        default=DEFAULT_KEEP_SILENCE_S,
        help=f"Seconds of pause to leave after compressing a long gap (default: {DEFAULT_KEEP_SILENCE_S})",
    )
    args = parser.parse_args(argv)
    preprocess(
        args.input,
        args.output,
        mode=args.mode,
        threshold_db=args.threshold_db,
        min_pause_s=args.min_pause,
        keep_silence_s=args.keep_silence,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
