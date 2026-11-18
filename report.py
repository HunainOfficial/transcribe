#!/usr/bin/env python3
"""Build a full timing + quality report for all jobs under out/."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from job import OUT_DIR, duration_of


DURATION_RE = re.compile(
    r"\| Before \(convert only\) \| ([^|]+) \|\n\| After \(`[^`]+`\) \| ([^|]+) \|\n\| Saved \| ([^(]+)\(([^)]+)%\) \|"
)
WORDS_RE = re.compile(
    r"\| Before \| (\d+) \| (\d+) \|\n\| After \| (\d+) \| (\d+) \|\n\| Word coverage[^\|]*\| ([0-9.]+)%"
)
VERDICT_RE = re.compile(r"## Verdict\n\n\*\*([^*]+)\*\*")


def parse_seconds_label(label: str) -> float | None:
    label = label.strip()
    # "11m 03s" or "1m 28s" or "0m 24s"
    m = re.match(r"(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?", label)
    if not m:
        return None
    h = int(m.group(1) or 0)
    mi = int(m.group(2) or 0)
    s = int(m.group(3) or 0)
    return h * 3600 + mi * 60 + s


def fmt_s(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def load_batch_results(out_dir: Path) -> dict[str, dict]:
    path = out_dir / "batch_results.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("results") if isinstance(data, dict) else data
    return {r["stem"]: r for r in rows if isinstance(r, dict) and "stem" in r}


def load_batch_start(out_dir: Path) -> datetime | None:
    path = out_dir / "batch_errors.jsonl"
    if not path.exists():
        return None
    last_start = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event") == "batch_start" and row.get("started_at"):
            last_start = datetime.fromisoformat(row["started_at"])
    return last_start


def collect_job(stem_dir: Path, batch_row: dict | None, batch_start: datetime | None) -> dict:
    stem = stem_dir.name
    comparison = stem_dir / "comparison.md"
    before_txt = stem_dir / "before" / "transcript.txt"
    after_txt = stem_dir / "after" / "transcript.txt"
    before_wav = next(stem_dir.joinpath("before").glob("*.wav"), None)
    after_wav = next(stem_dir.joinpath("after").glob("*.clean.wav"), None)

    row: dict = {"stem": stem, "status": "partial"}
    if batch_row:
        row.update(
            {
                "status": batch_row.get("status", row["status"]),
                "total_wall_s": batch_row.get("total_wall_s"),
                "preprocess_before_s": batch_row.get("preprocess_before_s"),
                "preprocess_after_s": batch_row.get("preprocess_after_s"),
                "api_before_s": batch_row.get("api_before_s"),
                "api_after_s": batch_row.get("api_after_s"),
                "api_wall_s": batch_row.get("api_wall_s"),
                "before_duration_s": batch_row.get("before_duration_s"),
                "after_duration_s": batch_row.get("after_duration_s"),
                "saved_pct": batch_row.get("saved_pct"),
                "attempts": batch_row.get("attempts"),
                "error": batch_row.get("error"),
                "started_at": batch_row.get("started_at"),
                "finished_at": batch_row.get("finished_at"),
            }
        )

    if comparison.exists():
        text = comparison.read_text(encoding="utf-8")
        m = DURATION_RE.search(text)
        if m:
            row.setdefault("before_duration_s", parse_seconds_label(m.group(1)))
            row.setdefault("after_duration_s", parse_seconds_label(m.group(2)))
            row.setdefault("saved_pct", float(m.group(4)))
        wm = WORDS_RE.search(text)
        if wm:
            row["before_words"] = int(wm.group(1))
            row["before_chars"] = int(wm.group(2))
            row["after_words"] = int(wm.group(3))
            row["after_chars"] = int(wm.group(4))
            row["coverage_pct"] = float(wm.group(5))
        vm = VERDICT_RE.search(text)
        if vm:
            row["verdict"] = vm.group(1).strip()
        row["status"] = row.get("status") if row.get("status") not in (None, "partial") else "ok"
        # Finish proxy from comparison mtime if no wall time
        if row.get("total_wall_s") is None and batch_start is not None:
            finished = datetime.fromtimestamp(comparison.stat().st_mtime, tz=timezone.utc)
            row["total_wall_s"] = max(0.0, (finished - batch_start).total_seconds())
            row["timing_source"] = "comparison_mtime_vs_batch_start"

    if before_txt.exists():
        row["before_txt_bytes"] = before_txt.stat().st_size
    if after_txt.exists():
        row["after_txt_bytes"] = after_txt.stat().st_size

    if before_wav and row.get("before_duration_s") is None:
        row["before_duration_s"] = duration_of(before_wav)
    if after_wav and row.get("after_duration_s") is None:
        row["after_duration_s"] = duration_of(after_wav)

    if (
        row.get("saved_pct") is None
        and row.get("before_duration_s")
        and row.get("after_duration_s")
        and row["before_duration_s"] > 0
    ):
        row["saved_pct"] = (
            (row["before_duration_s"] - row["after_duration_s"]) / row["before_duration_s"]
        ) * 100

    # billed minutes approx = after duration (smart) / 60
    if row.get("after_duration_s") is not None:
        row["billed_min_after"] = row["after_duration_s"] / 60.0
    if row.get("before_duration_s") is not None:
        row["billed_min_before"] = row["before_duration_s"] / 60.0

    return row


def build_report(out_dir: Path) -> str:
    batch_rows = load_batch_results(out_dir)
    batch_start = load_batch_start(out_dir)
    jobs = []
    for child in sorted(out_dir.iterdir()):
        if child.is_dir() and (child / "comparison.md").exists() or (
            child.is_dir() and child.name in batch_rows
        ):
            jobs.append(collect_job(child, batch_rows.get(child.name), batch_start))

    # Prefer stems that have data
    jobs = [j for j in jobs if j.get("stem")]
    jobs.sort(key=lambda j: j["stem"])

    wall_times = [j["total_wall_s"] for j in jobs if j.get("total_wall_s") is not None]
    audio_before = [j["before_duration_s"] for j in jobs if j.get("before_duration_s") is not None]
    audio_after = [j["after_duration_s"] for j in jobs if j.get("after_duration_s") is not None]
    saved = [j["saved_pct"] for j in jobs if j.get("saved_pct") is not None]

    # Parallel wall ≈ max(per-file); sequential estimate ≈ sum(per-file)
    parallel_est = max(wall_times) if wall_times else None
    sequential_est = sum(wall_times) if wall_times else None
    speedup = (
        (sequential_est / parallel_est) if parallel_est and sequential_est and parallel_est > 0 else None
    )

    lines = [
        "# Full transcription report",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
        f"Jobs found: **{len(jobs)}**",
        "",
        "## Executive summary",
        "",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Files | {len(jobs)} |",
        f"| Total audio before | {fmt_s(sum(audio_before) if audio_before else None)} |",
        f"| Total audio after (billed) | {fmt_s(sum(audio_after) if audio_after else None)} |",
        f"| Total dead-air saved | {fmt_s((sum(audio_before) - sum(audio_after)) if audio_before and audio_after else None)} |",
        f"| Avg save % | {(sum(saved) / len(saved)):.1f}% |" if saved else "| Avg save % | n/a |",
        f"| Est. sequential process time (sum of jobs) | {fmt_s(sequential_est)} |",
        f"| Est. parallel wall time (slowest job) | {fmt_s(parallel_est)} |",
        f"| Est. speedup | {speedup:.2f}x |" if speedup else "| Est. speedup | n/a |",
        "",
        "> Parallel wall time uses each job’s measured/estimated processing time; "
        "with `--workers 6` the batch wall clock should be close to the slowest file.",
        "",
        "## Per-file audio (cost)",
        "",
        "| File | Before | After | Saved | Save % | Billed min (after) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for j in jobs:
        b = j.get("before_duration_s")
        a = j.get("after_duration_s")
        saved_s = (b - a) if b is not None and a is not None else None
        lines.append(
            f"| `{j['stem']}` | {fmt_s(b)} | {fmt_s(a)} | {fmt_s(saved_s)} | "
            f"{j.get('saved_pct', float('nan')):.1f}% | "
            f"{j.get('billed_min_after', float('nan')):.2f} |"
            if j.get("saved_pct") is not None and j.get("billed_min_after") is not None
            else f"| `{j['stem']}` | {fmt_s(b)} | {fmt_s(a)} | {fmt_s(saved_s)} | n/a | n/a |"
        )

    lines += [
        "",
        "## Per-file processing time",
        "",
        "| File | Preprocess before | Preprocess after | API before | API after | API wall | Total job |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for j in jobs:
        lines.append(
            f"| `{j['stem']}` | {fmt_s(j.get('preprocess_before_s'))} | "
            f"{fmt_s(j.get('preprocess_after_s'))} | {fmt_s(j.get('api_before_s'))} | "
            f"{fmt_s(j.get('api_after_s'))} | {fmt_s(j.get('api_wall_s'))} | "
            f"{fmt_s(j.get('total_wall_s'))} |"
        )

    lines += [
        "",
        "## Transcript quality (before vs after)",
        "",
        "| File | Before words | After words | Coverage | Verdict |",
        "|---|---:|---:|---:|---|",
    ]
    for j in jobs:
        lines.append(
            f"| `{j['stem']}` | {j.get('before_words', 'n/a')} | {j.get('after_words', 'n/a')} | "
            f"{j.get('coverage_pct', float('nan')):.1f}% | {j.get('verdict', 'n/a')} |"
            if j.get("coverage_pct") is not None
            else f"| `{j['stem']}` | {j.get('before_words', 'n/a')} | {j.get('after_words', 'n/a')} | n/a | {j.get('verdict', 'n/a')} |"
        )

    lines += [
        "",
        "## Status / errors",
        "",
        "| File | Status | Attempts | Error |",
        "|---|---|---:|---|",
    ]
    for j in jobs:
        err = (j.get("error") or "").replace("|", "\\|").replace("\n", " ")[:100]
        lines.append(
            f"| `{j['stem']}` | {j.get('status', 'n/a')} | {j.get('attempts', '')} | {err} |"
        )

    lines += [
        "",
        "## Single vs all-6 (how to read)",
        "",
        "- **Single file time** = that row’s `Total job` (preprocess + API).",
        "- **All 6 sequential** ≈ sum of `Total job` (if you ran `foreach` one-by-one).",
        "- **All 6 parallel** ≈ max `Total job` (this batch with `--workers 6`), plus a little scheduling overhead.",
        "- **API cost** tracks **After** audio minutes (smart-compressed), not Before.",
        "- Open each `out/<id>/comparison.md` for missing-fact detail.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write full timing/quality report.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output markdown (default: out/full_report.md)",
    )
    args = parser.parse_args(argv)
    out = args.output or (OUT_DIR / "full_report.md")
    md = build_report(OUT_DIR)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(md)
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
