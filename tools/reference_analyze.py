#!/usr/bin/env python3
"""Reference-video analyzer — turn a video you like into a measurable blueprint.

Point it at a YouTube/TikTok/Reels/Shorts URL (via yt-dlp) or a local file and
it measures the things you'd otherwise eyeball:

  * format     — resolution, aspect class (vertical / landscape / square), fps
  * cutting    — shot boundaries (ffmpeg scene score), shot count, average
                 shot length, cuts per minute, pacing class
  * narration  — transcript (platform captions → local whisper → ElevenLabs
                 Scribe), words per minute, speech coverage, time-to-first-word
  * hook       — what happens in the first 3 seconds (cuts, words, first line)
  * beats      — transcript grouped at natural pauses, each with duration,
                 wpm, shot count and a word budget for re-scripting
  * audio      — integrated loudness (LUFS) and silence share
  * visuals    — one keyframe per shot + a labelled contact sheet

Outputs (default `references/<slug>/`, gitignored):

  analysis.json      machine-readable everything
  REFERENCE.md       human/Claude-readable brief (feed it to /reference)
  contact_sheet.jpg  keyframe grid with shot # and timestamps
  keyframes/         one JPG per (sampled) shot
  transcript.txt     plain transcript, when one was found

The numbers are measurements; the creative read (what to keep, what to
change) is the `/reference` command's job. Downloaded media is for analysis
only — don't reuse someone else's footage or audio in your render.

Usage:
    python3 tools/reference_analyze.py --input "https://youtube.com/shorts/ID"
    python3 tools/reference_analyze.py --input clip.mp4 --asr whisper
    python3 tools/reference_analyze.py --input clip.mp4 --transcript clip.srt
    python3 tools/reference_analyze.py --input URL --scene-threshold 0.25 --json

Requires ffmpeg/ffprobe. URLs need yt-dlp (`pip install yt-dlp`). Optional:
openai-whisper (local ASR), ELEVENLABS_API_KEY (Scribe ASR), Pillow (sheet).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from dotenv import load_dotenv

    load_dotenv(TOOLKIT_ROOT / ".env")
except ImportError:
    pass

# Standard narration pace used for word budgets (CLAUDE.md: ~150 wpm).
STANDARD_WPM = 150

HOOK_SECONDS = 3.0

# Average-shot-length bands (seconds) → pacing class. Heuristic, not a
# standard: tuned so TikTok-style edits read "frenetic"/"fast" and
# talking-head or documentary pieces read "moderate"/"slow".
ASL_BANDS = [(1.5, "frenetic"), (3.0, "fast"), (6.0, "moderate")]
ASL_SLOWEST = "slow"

# Speaking-rate tiers from CLAUDE.md.
WPM_TIERS = [(130, "slow"), (160, "standard"), (180, "fast")]
WPM_FASTEST = "very fast"

URL_RE = re.compile(r"^https?://", re.IGNORECASE)


# ─── helpers ────────────────────────────────────────────────

def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def require_bin(name: str, hint: str) -> str:
    path = shutil.which(name)
    if not path:
        sys.exit(f"error: `{name}` not found. {hint}")
    return path


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if check and proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        raise RuntimeError(f"command failed ({cmd[0]}):\n{tail}")
    return proc


def slugify(text: str, fallback: str = "reference") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60].strip("-") or fallback


def fmt_ts(seconds: float) -> str:
    m, s = divmod(max(seconds, 0.0), 60)
    return f"{int(m)}:{s:04.1f}"


def r2(x: float | None) -> float | None:
    return None if x is None else round(x, 2)


# ─── acquisition ────────────────────────────────────────────

def ytdlp_cmd() -> list[str]:
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    try:
        import yt_dlp  # noqa: F401

        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        sys.exit("error: URL input needs yt-dlp. Install it: pip install yt-dlp")


def _download_failed(stderr: str) -> None:
    errors = [ln for ln in stderr.strip().splitlines() if ln.startswith("ERROR")]
    last = (errors or stderr.strip().splitlines() or ["unknown error"])[-1]
    sys.exit(
        f"error: yt-dlp failed: {last[:300]}\n"
        "  Platforms often block downloads from servers/VPNs (403) or require login.\n"
        "  Try: pip install -U yt-dlp · --cookies-from-browser chrome · or download the\n"
        "  video yourself and pass the file: --input clip.mp4 (a .srt/.vtt next to it is used)."
    )


def fetch_url(url: str, out_dir: Path | None, max_minutes: float, lang: str,
              cookies_from_browser: str | None = None) -> tuple[Path, dict, Path | None]:
    """Download a URL with yt-dlp. Returns (video_path, metadata, captions_path)."""
    base = ytdlp_cmd()
    if cookies_from_browser:
        base += ["--cookies-from-browser", cookies_from_browser]
    log("→ reading metadata (yt-dlp)…")
    meta_proc = run(base + ["-J", "--no-playlist", url], check=False)
    if meta_proc.returncode != 0:
        _download_failed(meta_proc.stderr)
    info = json.loads(meta_proc.stdout)
    duration = info.get("duration") or 0
    if max_minutes and duration > max_minutes * 60:
        sys.exit(
            f"error: video is {duration / 60:.1f} min (limit {max_minutes} min). "
            "Pass --max-minutes to raise the limit."
        )
    meta = {
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "url": info.get("webpage_url") or url,
        "platform": info.get("extractor_key"),
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "description": (info.get("description") or "")[:1500],
        "tags": (info.get("tags") or [])[:20],
    }
    if out_dir is None:
        out_dir = TOOLKIT_ROOT / "references" / slugify(meta["title"] or info.get("id", ""))
    out_dir.mkdir(parents=True, exist_ok=True)

    log("→ downloading (≤720p)…")
    dl = run(base + [
        "--no-playlist", "-q", "--no-warnings",
        "-f", "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", str(out_dir / "source.%(ext)s"),
        url,
    ], check=False)
    if dl.returncode != 0:
        _download_failed(dl.stderr)
    # Captions are best-effort and fetched separately: platforms rate-limit
    # subtitle endpoints (HTTP 429) far more often than media, and a missing
    # caption track should fall through to ASR, not fail the run. Only the
    # original-language tracks — `en.*` would also pull machine translations.
    log("→ fetching captions (best-effort)…")
    subs_proc = run(base + [
        "--no-playlist", "-q", "--no-warnings", "--skip-download",
        "--write-subs", "--write-auto-subs", "--sub-format", "vtt",
        "--sub-langs", f"{lang},{lang}-orig,{lang}-US,{lang}-GB",
        "-o", str(out_dir / "source.%(ext)s"),
        url,
    ], check=False)
    if subs_proc.returncode != 0:
        log("  ! captions unavailable (" + (subs_proc.stderr.strip().splitlines() or ["?"])[-1][:120] + ")")
    videos = [p for p in out_dir.glob("source.*") if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]
    if not videos:
        sys.exit("error: yt-dlp finished but no video file was written.")
    subs = sorted(out_dir.glob("source*.vtt"))
    return videos[0], {**meta, "_out_dir": str(out_dir)}, (subs[0] if subs else None)


# ─── probing ────────────────────────────────────────────────

def probe(path: Path) -> dict:
    data = json.loads(run([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
    ]).stdout)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        sys.exit("error: input has no video stream.")
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = abs(int(sd["rotation"]))
    if (v.get("tags") or {}).get("rotate"):
        rot = abs(int(v["tags"]["rotate"]))
    if rot in (90, 270):
        w, h = h, w
    num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    ratio = w / h
    if ratio < 0.8:
        aspect = "vertical"
    elif ratio > 1.25:
        aspect = "landscape"
    else:
        aspect = "square"
    return {
        "width": w,
        "height": h,
        "aspect": aspect,
        "aspect_ratio": f"{w}:{h}",
        "fps": round(fps, 2),
        "duration": float(data["format"].get("duration") or v.get("duration") or 0),
        "has_audio": a is not None,
    }


# ─── shots ──────────────────────────────────────────────────

def detect_cuts(path: Path, threshold: float) -> list[float]:
    """Cut timestamps from ffmpeg's scene-change score on a downscaled stream."""
    proc = run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-an",
        "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ], check=False)
    return [float(m) for m in re.findall(r"pts_time:([\d.]+)", proc.stderr)]


def build_shots(cuts: list[float], duration: float, min_shot: float) -> list[dict]:
    bounds = [0.0]
    for t in sorted(cuts):
        if t - bounds[-1] >= min_shot and duration - t >= min_shot:
            bounds.append(t)
    bounds.append(duration)
    return [
        {"index": i + 1, "start": r2(s), "end": r2(e), "duration": r2(e - s)}
        for i, (s, e) in enumerate(zip(bounds, bounds[1:]))
    ]


def pacing_class(asl: float) -> str:
    for limit, label in ASL_BANDS:
        if asl < limit:
            return label
    return ASL_SLOWEST


def wpm_tier(wpm: float | None) -> str | None:
    if wpm is None:
        return None
    for limit, label in WPM_TIERS:
        if wpm < limit:
            return label
    return WPM_FASTEST


# ─── transcript ─────────────────────────────────────────────
# Normalised form: list of {"text", "start", "end"} words. Segment-level
# sources get evenly interpolated word times — good enough for pacing.

_TS = r"(\d+:)?\d{1,2}:\d{2}[.,]\d{3}"
_CUE_RE = re.compile(rf"^\s*({_TS})\s*-->\s*({_TS})")
_INLINE_RE = re.compile(r"<(\d+:)?(\d{1,2}):(\d{2})[.,](\d{3})>")
_TAG_RE = re.compile(r"<[^>]+>")


def _parse_ts(ts: str) -> float:
    parts = ts.replace(",", ".").split(":")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def _spread(text: str, start: float, end: float) -> list[dict]:
    toks = text.split()
    if not toks:
        return []
    step = max(end - start, 0.01) / len(toks)
    return [
        {"text": t, "start": start + i * step, "end": start + (i + 1) * step}
        for i, t in enumerate(toks)
    ]


def parse_subtitles(path: Path) -> list[dict]:
    """Parse VTT/SRT into words. Handles YouTube rolling auto-captions."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    cues: list[tuple[float, float, list[str]]] = []
    # Cues end at a truly empty line; YouTube auto-captions open each cue with
    # a whitespace-only line, so `.strip()` would end the cue too early.
    lines = [ln.rstrip("\r") for ln in raw.splitlines()]
    i = 0
    while i < len(lines):
        m = _CUE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        start, end = _parse_ts(m.group(1)), _parse_ts(m.group(3))
        body = []
        i += 1
        while i < len(lines) and lines[i] != "":
            body.append(lines[i])
            i += 1
        cues.append((start, end, body))

    has_inline = any(_INLINE_RE.search(line) for _, _, body in cues for line in body)
    words: list[dict] = []
    if has_inline:
        # YouTube auto-captions: the line carrying <hh:mm:ss.mmm> tags holds the
        # newly spoken words with real timings; other lines are repeats.
        for start, end, body in cues:
            for line in body:
                if not _INLINE_RE.search(line):
                    continue
                pieces = _INLINE_RE.split(line)
                # split yields: text, (h, m, s, ms), text, ...
                cursor, text = start, pieces[0]
                timed: list[tuple[float, str]] = [(cursor, text)]
                for j in range(1, len(pieces), 5):
                    h, mm, ss, ms = pieces[j:j + 4]
                    t = (int(h[:-1]) if h else 0) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000
                    timed.append((t, pieces[j + 4] if j + 4 < len(pieces) else ""))
                for k, (t, chunk) in enumerate(timed):
                    clean = _TAG_RE.sub("", chunk).strip()
                    if not clean:
                        continue
                    nxt = timed[k + 1][0] if k + 1 < len(timed) else end
                    words.extend(_spread(clean, t, max(nxt, t + 0.05)))
        return words

    prev = ""
    for start, end, body in cues:
        text = " ".join(_TAG_RE.sub("", ln).strip() for ln in body if not ln.strip().isdigit()).strip()
        if not text or text == prev:
            continue
        # Rolling captions repeat the previous line as a prefix — keep the new tail.
        new = text[len(prev):].strip() if prev and text.startswith(prev) else text
        prev = text
        words.extend(_spread(new, start, end))
    return words


def extract_audio(video: Path, out: Path) -> Path:
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
         "-vn", "-ac", "1", "-ar", "16000", str(out)])
    return out


def transcribe_whisper(audio: Path, model: str) -> list[dict]:
    import whisper

    log(f"→ transcribing with whisper ({model})…")
    result = whisper.load_model(model).transcribe(str(audio), word_timestamps=True)
    words = []
    for seg in result.get("segments", []):
        for w in seg.get("words") or []:
            words.append({"text": w["word"].strip(), "start": w["start"], "end": w["end"]})
        if not seg.get("words"):
            words.extend(_spread(seg["text"], seg["start"], seg["end"]))
    return words


def transcribe_scribe(audio: Path) -> list[dict]:
    from align_captions import transcribe_words

    log("→ transcribing with ElevenLabs Scribe…")
    return transcribe_words(str(audio), os.environ["ELEVENLABS_API_KEY"])


def get_transcript(args, video: Path, captions: Path | None, work: Path, has_audio: bool) -> tuple[list[dict], str | None, str | None]:
    """Returns (words, source, note)."""
    if args.transcript:
        p = Path(args.transcript)
        if p.suffix.lower() in (".vtt", ".srt"):
            return parse_subtitles(p), "file", None
        text = p.read_text(encoding="utf-8", errors="replace")
        return [{"text": t, "start": None, "end": None} for t in text.split()], "file-untimed", \
            "Plain-text transcript has no timings: wpm is averaged over the whole video, beats are unavailable."

    order = [args.asr] if args.asr != "auto" else ["captions", "whisper", "scribe"]
    if args.asr == "none" or not has_audio:
        return [], None, None if has_audio else "No audio stream."

    for source in order:
        try:
            if source == "captions":
                if captions:
                    words = parse_subtitles(captions)
                    if words:
                        if captions.name.startswith("source"):
                            return words, "platform-captions", \
                                "Platform captions (often auto-generated): wording can be imperfect, timing approximate."
                        return words, "sidecar-captions", None
            elif source == "whisper":
                try:
                    import whisper  # noqa: F401
                except ImportError:
                    if args.asr == "whisper":
                        sys.exit("error: --asr whisper needs `pip install openai-whisper`.")
                    continue
                return transcribe_whisper(extract_audio(video, work / "audio.wav"), args.whisper_model), \
                    f"whisper-{args.whisper_model}", None
            elif source == "scribe":
                if not os.environ.get("ELEVENLABS_API_KEY"):
                    if args.asr == "scribe":
                        sys.exit("error: --asr scribe needs ELEVENLABS_API_KEY in .env.")
                    continue
                return transcribe_scribe(extract_audio(video, work / "audio.wav")), "elevenlabs-scribe", None
        except Exception as e:  # one backend failing shouldn't kill the analysis
            log(f"  ! {source} transcription failed: {e}")
            if args.asr != "auto":
                raise
    return [], None, ("No transcript: no platform captions, and neither openai-whisper nor "
                      "ELEVENLABS_API_KEY is available. Narration metrics are missing.")


# ─── beats ──────────────────────────────────────────────────

def build_beats(words: list[dict], shots: list[dict], gap: float, max_len: float) -> list[dict]:
    """Group timed words into beats at pauses ≥ gap (or when a beat hits max_len)."""
    timed = [w for w in words if w.get("start") is not None]
    groups: list[list[dict]] = []
    for w in timed:
        if groups:
            last = groups[-1]
            pause = w["start"] - last[-1]["end"]
            too_long = w["end"] - last[0]["start"] > max_len
            sentence_end = last[-1]["text"].rstrip().endswith((".", "!", "?"))
            if pause < gap and not (too_long and sentence_end) and not (too_long and pause > 0.25):
                last.append(w)
                continue
        groups.append([w])

    beats = []
    for i, g in enumerate(groups):
        start, end = g[0]["start"], g[-1]["end"]
        dur = max(end - start, 0.01)
        n = len(g)
        n_shots = sum(1 for s in shots if s["start"] < end and s["end"] > start)
        beats.append({
            "index": i + 1,
            "start": r2(start),
            "end": r2(end),
            "duration": r2(dur),
            "words": n,
            "wpm": round(n / dur * 60) if dur >= 1.5 else None,
            "shots": n_shots,
            "word_budget_standard": round(dur * STANDARD_WPM / 60),
            "text": " ".join(w["text"] for w in g),
        })
    return beats


# ─── audio ──────────────────────────────────────────────────

def audio_stats(path: Path, duration: float) -> dict:
    loud = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
                "-af", "ebur128=framelog=quiet", "-f", "null", "-"], check=False).stderr
    lufs = re.findall(r"I:\s+(-?[\d.]+) LUFS", loud)
    sil = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
               "-af", "silencedetect=noise=-40dB:d=0.4", "-f", "null", "-"], check=False).stderr
    silent = sum(float(x) for x in re.findall(r"silence_duration: ([\d.]+)", sil))
    return {
        "integrated_lufs": float(lufs[-1]) if lufs else None,
        "silence_pct": round(100 * silent / duration, 1) if duration else None,
    }


# ─── keyframes ──────────────────────────────────────────────

def sample_shots(shots: list[dict], limit: int) -> list[dict]:
    if len(shots) <= limit:
        return shots
    step = len(shots) / limit
    return [shots[int(i * step)] for i in range(limit)]


def extract_keyframes(video: Path, shots: list[dict], out_dir: Path, width: int) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("shot_*.jpg"):
        old.unlink()
    paths = []
    for s in shots:
        t = s["start"] + s["duration"] / 2
        p = out_dir / f"shot_{s['index']:03d}_{t:07.2f}s.jpg"
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}",
             "-i", str(video), "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(p)],
            check=False)
        if p.exists():
            s["keyframe"] = str(p.relative_to(out_dir.parent))
            paths.append(p)
    return paths


def contact_sheet(shots: list[dict], root: Path, out: Path, vertical: bool) -> Path | None:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        log("  ! Pillow not installed — skipping contact sheet")
        return None
    items = [(s, root / s["keyframe"]) for s in shots if s.get("keyframe")]
    if not items:
        return None
    cols = min(6 if vertical else 4, len(items))
    tw = 220 if vertical else 320
    first = Image.open(items[0][1])
    th = round(tw * first.height / first.width)
    label_h, pad = 22, 6
    rows = (len(items) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (tw + pad) + pad, rows * (th + label_h + pad) + pad), (18, 18, 18))
    draw = ImageDraw.Draw(sheet)
    for n, (s, p) in enumerate(items):
        x = pad + (n % cols) * (tw + pad)
        y = pad + (n // cols) * (th + label_h + pad)
        sheet.paste(Image.open(p).convert("RGB").resize((tw, th)), (x, y))
        draw.text((x + 4, y + th + 4),
                  f"#{s['index']}  {fmt_ts(s['start'])}  ({s['duration']:.1f}s)", fill=(230, 230, 230))
    sheet.save(out, quality=85)
    return out


# ─── report ─────────────────────────────────────────────────

def suggest_template(fmt: dict) -> str:
    if fmt["aspect"] == "vertical":
        return "concept-explainer-short"
    return "product-demo"


def write_markdown(a: dict, path: Path) -> None:
    f, c, n, h = a["format"], a["cutting"], a["narration"], a["hook"]
    src = a["source"]
    L = [f"# Reference: {src.get('title') or Path(src['input']).name}", ""]
    if src.get("url"):
        L.append(f"- **Source:** {src['url']}" + (f" — {src['uploader']}" if src.get("uploader") else ""))
    if src.get("view_count") is not None:
        L.append(f"- **Views:** {src['view_count']:,}" + (f" · likes {src['like_count']:,}" if src.get("like_count") else ""))
    L += [
        f"- **Analyzed:** {a['analyzed_at']}",
        "",
        "> Measurements, not judgments. Downloaded media is for analysis only — don't reuse its footage or audio.",
        "",
        "## At a glance",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Duration | {f['duration']:.1f}s |",
        f"| Format | {f['aspect']} {f['width']}×{f['height']} @ {f['fps']}fps |",
        f"| Shots | {c['shot_count']} (avg {c['avg_shot_length']:.2f}s, median {c['median_shot_length']:.2f}s) |",
        f"| Cuts per minute | {c['cuts_per_minute']:.1f} |",
        f"| Pacing class | **{c['pacing_class']}** |",
    ]
    if n["source"]:
        L += [
            f"| Narration | {n['word_count']} words · {n['wpm'] or '—'} wpm ({n['wpm_tier'] or '—'}) |",
            f"| Speech coverage | {n['speech_coverage_pct'] if n['speech_coverage_pct'] is not None else '—'}% of runtime |",
            f"| First word at | {n['time_to_first_word'] if n['time_to_first_word'] is not None else '—'}s |",
        ]
    else:
        L.append("| Narration | none detected / no transcript |")
    au = a["audio"]
    if au:
        L.append(f"| Loudness | {au['integrated_lufs'] if au['integrated_lufs'] is not None else '—'} LUFS · silence {au['silence_pct']}% |")
    L += [f"| Suggested template | `{a['suggested_template']}` |", ""]

    if n.get("note"):
        L += [f"_Transcript note: {n['note']}_", ""]

    L += ["## Hook (first 3s)", "",
          f"- Cuts: **{h['cuts']}** · words spoken: **{h['words']}**",
          f"- Opening line: {('“' + h['opening_line'] + '”') if h['opening_line'] else '_(no speech in first 3s)_'}",
          ""]

    if a["beats"]:
        L += ["## Beats", "",
              "Narration grouped at natural pauses. **Budget** = words at the toolkit's standard 150 wpm "
              "for the same duration; the reference's own pace is in the wpm column.", "",
              "| # | Time | Dur | Shots | Words | WPM | Budget@150 | Text |",
              "|---|---|---|---|---|---|---|---|"]
        for b in a["beats"]:
            text = b["text"].replace("|", "/")
            L.append(f"| {b['index']} | {fmt_ts(b['start'])} | {b['duration']:.1f}s | {b['shots']} | "
                     f"{b['words']} | {b['wpm'] or '—'} | {b['word_budget_standard']} | {text} |")
        L.append("")

    L += ["## Shots", "", "See `contact_sheet.jpg` for the keyframe grid.", "",
          "| # | Start | Dur | Keyframe |", "|---|---|---|---|"]
    for s in a["shots"]:
        L.append(f"| {s['index']} | {fmt_ts(s['start'])} | {s['duration']:.2f}s | {s.get('keyframe', '')} |")
    L.append("")

    if a.get("transcript_text"):
        L += ["## Transcript", "", a["transcript_text"], ""]
    path.write_text("\n".join(L), encoding="utf-8")


# ─── main ───────────────────────────────────────────────────

def analyze(args) -> dict:
    require_bin("ffmpeg", "Install FFmpeg (brew/apt install ffmpeg).")
    require_bin("ffprobe", "Install FFmpeg (brew/apt install ffmpeg).")

    out_dir = Path(args.output_dir).resolve() if args.output_dir else None
    captions = None
    if URL_RE.match(args.input):
        video, meta, captions = fetch_url(args.input, out_dir, args.max_minutes, args.lang,
                                          args.cookies_from_browser)
        out_dir = Path(meta.pop("_out_dir"))
    else:
        video = Path(args.input).resolve()
        if not video.exists():
            sys.exit(f"error: input not found: {video}")
        meta = {"title": video.stem}
        out_dir = out_dir or TOOLKIT_ROOT / "references" / slugify(video.stem)
        out_dir.mkdir(parents=True, exist_ok=True)
        for ext in (".vtt", ".srt"):
            sidecar = video.with_suffix(ext)
            if sidecar.exists():
                captions = sidecar
                break

    log(f"→ analyzing {video.name} → {out_dir}")
    fmt = probe(video)
    duration = fmt["duration"]

    log("→ detecting shots…")
    shots = build_shots(detect_cuts(video, args.scene_threshold), duration, args.min_shot)
    lens = sorted(s["duration"] for s in shots)
    asl = duration / len(shots)
    cutting = {
        "shot_count": len(shots),
        "avg_shot_length": r2(asl),
        "median_shot_length": r2(lens[len(lens) // 2]),
        "shortest_shot": r2(lens[0]),
        "longest_shot": r2(lens[-1]),
        "cuts_per_minute": r2((len(shots) - 1) / duration * 60) if duration else 0,
        "pacing_class": pacing_class(asl),
        "scene_threshold": args.scene_threshold,
    }

    log("→ transcript…")
    words, t_source, t_note = get_transcript(args, video, captions, out_dir, fmt["has_audio"])
    timed = [w for w in words if w.get("start") is not None]
    if timed:
        speech_span = timed[-1]["end"] - timed[0]["start"]
        beats = build_beats(words, shots, args.beat_gap, args.beat_max)
        spoken = sum(b["duration"] for b in beats)
        wpm = round(len(timed) / spoken * 60) if spoken >= 3 else None
        first_word = r2(timed[0]["start"])
    else:
        speech_span, beats, spoken, first_word = 0, [], 0, None
        wpm = round(len(words) / duration * 60) if words and duration else None
    narration = {
        "source": t_source,
        "note": t_note,
        "word_count": len(words),
        "wpm": wpm,
        "wpm_tier": wpm_tier(wpm),
        "speech_coverage_pct": round(100 * spoken / duration, 1) if timed and duration else None,
        "time_to_first_word": first_word,
        "speech_span": r2(speech_span) if timed else None,
    }
    hook_words = [w for w in timed if w["start"] < HOOK_SECONDS]
    hook = {
        "seconds": HOOK_SECONDS,
        "cuts": sum(1 for s in shots[1:] if s["start"] < HOOK_SECONDS),
        "words": len(hook_words),
        "opening_line": beats[0]["text"] if beats and beats[0]["start"] < HOOK_SECONDS else
                        (" ".join(w["text"] for w in hook_words) or None),
    }

    audio = None
    if fmt["has_audio"]:
        log("→ audio levels…")
        audio = audio_stats(video, duration)

    log("→ keyframes…")
    sampled = sample_shots(shots, args.max_keyframes)
    extract_keyframes(video, sampled, out_dir / "keyframes", args.keyframe_width)
    sheet = contact_sheet(sampled, out_dir, out_dir / "contact_sheet.jpg", fmt["aspect"] == "vertical")

    transcript_text = " ".join(w["text"] for w in words) or None
    if transcript_text:
        (out_dir / "transcript.txt").write_text(transcript_text + "\n", encoding="utf-8")

    analysis = {
        "tool": "reference_analyze",
        "analyzed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {"input": args.input, "file": str(video), **meta},
        "format": fmt,
        "cutting": cutting,
        "narration": narration,
        "hook": hook,
        "audio": audio,
        "beats": beats,
        "shots": shots,
        "suggested_template": suggest_template(fmt),
        "outputs": {
            "dir": str(out_dir),
            "report": str(out_dir / "REFERENCE.md"),
            "contact_sheet": str(sheet) if sheet else None,
            "keyframes": str(out_dir / "keyframes"),
        },
        "transcript_text": transcript_text,
    }
    (out_dir / "analysis.json").write_text(json.dumps(analysis, indent=2), encoding="utf-8")
    write_markdown(analysis, out_dir / "REFERENCE.md")

    if args.delete_source and URL_RE.match(args.input):
        video.unlink(missing_ok=True)
        analysis["source"]["file"] = None
    return analysis


def main() -> None:
    p = argparse.ArgumentParser(
        description="Analyze a reference video (URL or file): shots, pacing, narration, hook, beats, keyframes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1] if "Usage:" in __doc__ else None,
    )
    p.add_argument("--input", "-i", required=True, help="URL (YouTube/TikTok/Reels/…) or local video file")
    p.add_argument("--output-dir", "-o", help="Output directory (default: references/<slug>/)")
    p.add_argument("--transcript", help="Use this transcript (.vtt/.srt timed, or .txt untimed) instead of ASR")
    p.add_argument("--asr", choices=["auto", "captions", "whisper", "scribe", "none"], default="auto",
                   help="Transcript source. auto = platform captions → whisper → Scribe (default)")
    p.add_argument("--whisper-model", default="base", help="Whisper model size (default: base)")
    p.add_argument("--lang", default="en", help="Caption language to fetch for URLs (default: en)")
    p.add_argument("--scene-threshold", type=float, default=0.3,
                   help="ffmpeg scene-change threshold 0-1; lower = more cuts detected (default: 0.3)")
    p.add_argument("--min-shot", type=float, default=0.4, help="Merge shots shorter than this (s, default 0.4)")
    p.add_argument("--beat-gap", type=float, default=0.6, help="Pause that splits narration beats (s, default 0.6)")
    p.add_argument("--beat-max", type=float, default=12.0, help="Soft max beat length (s, default 12)")
    p.add_argument("--max-keyframes", type=int, default=48, help="Cap keyframes / contact-sheet tiles (default 48)")
    p.add_argument("--keyframe-width", type=int, default=480, help="Keyframe width in px (default 480)")
    p.add_argument("--cookies-from-browser", metavar="BROWSER",
                   help="Pass browser cookies to yt-dlp (chrome, firefox, safari…) for login/bot walls")
    p.add_argument("--max-minutes", type=float, default=20, help="Refuse URL downloads longer than this (default 20)")
    p.add_argument("--delete-source", action="store_true", help="Delete the downloaded video after analysis")
    p.add_argument("--json", action="store_true", help="Print the analysis JSON (minus shots/transcript) to stdout")
    args = p.parse_args()

    try:
        a = analyze(args)
    except RuntimeError as e:
        sys.exit(f"error: {e}")

    if args.json:
        slim = {k: v for k, v in a.items() if k not in ("shots", "transcript_text")}
        print(json.dumps(slim, indent=2))
    else:
        c, n = a["cutting"], a["narration"]
        print(f"✓ {a['format']['duration']:.1f}s {a['format']['aspect']} · {c['shot_count']} shots "
              f"(avg {c['avg_shot_length']:.2f}s, {c['pacing_class']}) · "
              f"{n['wpm'] or '—'} wpm · {len(a['beats'])} beats")
        print(f"  report: {a['outputs']['report']}")
        if a["outputs"]["contact_sheet"]:
            print(f"  sheet:  {a['outputs']['contact_sheet']}")


if __name__ == "__main__":
    main()
