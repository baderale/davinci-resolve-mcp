#!/usr/bin/env python3
"""Timeline to a verified social deliverable, in one command.

    python scripts/deliver_social.py --target tiktok --out ~/Movies/RidgeWallet
    python scripts/deliver_social.py --target reels --dry-run

Renders a master out of Resolve, derives the platform file from it, and refuses
to call the result delivered until it has been measured against the target's own
spec. Every number it asserts is read back off the finished file.

## Why a master and a separate deliverable

Resolve cannot render this deliverable on its own, for two unrelated reasons
that happen to have the same fix:

  - **Bitrate is unreachable.** `SetRenderSettings` refuses `VideoQuality` on
    21.0.4.5 — it is the one key that writes False. Resolve's own
    "TikTok - 1080p" preset produced 1080x1920 at **130 Mbps / 630 MB for 40
    seconds**, over TikTok's 287.6 MB mobile upload cap.
  - **Loudness is unreachable.** There is no scriptable audio gain
    (`SetProperty('Volume')` returns False and reads back `null`) and no
    render-side normalisation. Resolve's Voice Isolation is worse than absent on
    the free edition: it stores its state, reads it back, and does nothing —
    measured against a null test at -66 dBFS, which is re-encode noise.

So Resolve renders picture at high quality and this script does the rest. That
split is what a finishing workflow does anyway: the master is the thing you keep
and re-version from; a deliverable is disposable and platform-shaped.

## The audio chain

Built for hand-held phone footage shot into a room, which is the failure mode
this repository keeps meeting: a quiet, reverberant, presence-shy recording that
is clean but 24 dB too low. Measured on the reference material, `natural` moved
the between-word room bed from -23.9 dB to -58.9 dB while holding programme
loudness on target.

It cannot remove reverb that is *on* the voice — nothing here separates direct
sound from reflections. It removes room *between* words and restores presence
the microphone rolled off. Fix the microphone; this makes what you already shot
usable.

`--enhance off` runs loudness normalisation alone, which is always applied: a
deliverable that misses programme loudness is not a deliverable.

## What it will not do

It will not overwrite a master that already exists — pass `--refresh-master` to
re-render. It will not touch source media. It will not report success on an
unverified file: QC failures set a non-zero exit and the manifest records them.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.utils import delivery_targets as _dt  # noqa: E402

#: Audio chains, in filter order. `natural` is the default because a fully dead
#: gap reads as artificial on a talking head; `aggressive` is for material where
#: the room is worse than the performance.
#:
#: The stages, and why each one is here for this material:
#:   highpass     rumble and handling noise; a male fundamental sits near 100 Hz
#:                so 85 Hz costs nothing
#:   afftdn       broadband reduction against a measured -58 dB floor
#:   agate        ducks the room tail BETWEEN words — the single biggest win,
#:                because the room bed is what makes a recording sound cheap
#:   equalizer    -3 dB at 320 Hz (small-room boxiness), then presence and air
#:                to replace what a cardioid rolls off when it is not aimed well
#:   acompressor  steadies a delivery that drifts
#:   alimiter     catches transients before normalisation rather than after
ENHANCE_PROFILES: Dict[str, str] = {
    "natural": (
        "highpass=f=85:poles=2,"
        "afftdn=nr=10:nf=-45:tn=1,"
        "agate=threshold=0.006:ratio=2:range=0.08:attack=10:release=250:knee=6,"
        "equalizer=f=320:t=q:w=1.2:g=-3,"
        "equalizer=f=3200:t=q:w=1.0:g=4,"
        "equalizer=f=6000:t=q:w=1.4:g=2.5,"
        "acompressor=threshold=0.05:ratio=2.5:attack=12:release=220:makeup=1,"
        "alimiter=limit=0.9"
    ),
    "aggressive": (
        "highpass=f=85:poles=2,"
        "afftdn=nr=12:nf=-45:tn=1,"
        "agate=threshold=0.006:ratio=2.5:attack=8:release=180:knee=4,"
        "equalizer=f=320:t=q:w=1.2:g=-3,"
        "equalizer=f=3200:t=q:w=1.0:g=5,"
        "equalizer=f=6000:t=q:w=1.4:g=3,"
        "acompressor=threshold=0.05:ratio=2.5:attack=12:release=220:makeup=1,"
        "alimiter=limit=0.9"
    ),
    "off": "",
}

#: Video encode for vertical social. Not a bitrate plucked from the air: 12 Mbps
#: at 1080x1920/30 measured 58.9 MB for 40 seconds — comfortably inside the
#: 287.6 MB mobile cap with room for a piece three times longer.
VIDEO_ENCODE: List[str] = [
    "-c:v", "libx264", "-preset", "slow", "-profile:v", "high", "-level", "4.2",
    "-pix_fmt", "yuv420p", "-b:v", "12M", "-maxrate", "16M", "-bufsize", "24M",
]

#: TikTok's mobile upload ceiling. Desktop allows more, but a file that only
#: uploads from one of the two is a trap, not a deliverable.
MOBILE_UPLOAD_CAP_MB = 287.6

#: Resolve render preset pinned as the master's base state. `SetRenderSettings`
#: applies keys *on top of* whatever the Deliver page holds, and that state
#: cannot be read back — an inherited still-image preset has been measured to
#: produce an mp4 with no video stream at all.
DEFAULT_MASTER_PRESET = "TikTok - 1080p"


def _run(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def require_tools() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        raise SystemExit(
            f"Missing required tool(s): {', '.join(missing)}.\n"
            "ffmpeg is GPL and not bundled; install it and put it on PATH."
        )


def probe(path: Path) -> Dict[str, Any]:
    out = _run(["ffprobe", "-v", "error", "-show_entries",
                "format=duration,size,bit_rate,format_name",
                "-show_entries", "stream=codec_type,codec_name,width,height,"
                "r_frame_rate,sample_rate,channels,nb_frames",
                "-of", "json", str(path)])
    if out.returncode != 0:
        raise SystemExit(f"ffprobe failed on {path}:\n{out.stderr.strip()}")
    return json.loads(out.stdout)


def measure_loudness(path: Path) -> Dict[str, float]:
    """Integrated loudness, true peak and LRA, read off the file."""
    out = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
                "-af", "ebur128=peak=true", "-f", "null", "-"])
    tail = out.stderr[out.stderr.rfind("Summary"):]

    def grab(pattern: str) -> Optional[float]:
        m = re.search(pattern, tail)
        return float(m.group(1)) if m else None

    return {
        "integrated_lufs": grab(r"I:\s*(-?[\d.]+)\s*LUFS"),
        "true_peak_dbfs": grab(r"Peak:\s*(-?[\d.]+)\s*dBFS"),
        "lra_lu": grab(r"LRA:\s*(-?[\d.]+)\s*LU"),
    }


def loudnorm_measure(path: Path, target_i: float, target_tp: float) -> Dict[str, str]:
    """Pass one of two-pass loudnorm. Single-pass drifts; this does not."""
    out = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
                f"loudnorm=I={target_i}:TP={target_tp}:LRA=11:print_format=json",
                "-f", "null", "-"])
    blob = out.stderr[out.stderr.rfind("{"):out.stderr.rfind("}") + 1]
    if not blob:
        raise SystemExit(f"loudnorm measurement produced no JSON for {path}")
    return json.loads(blob)


def block_levels(path: Path, ms: int = 200) -> Optional[Dict[str, float]]:
    """Voice-above-room spread, as block RMS percentiles.

    Two hand-picked windows cannot answer "does the voice stand out from the
    room" — pick the wrong gap and the number is meaningless. Percentiles over
    the whole programme can. numpy is optional; without it this is skipped
    rather than approximated.
    """
    try:
        import numpy as np
    except ImportError:
        return None
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1",
                          "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True).stdout
    if not raw:
        return None
    x = np.frombuffer(raw, dtype=np.float32)
    n = int(16000 * ms / 1000)
    if len(x) < n:
        return None
    x = x[:len(x) // n * n].reshape(-1, n).astype(np.float64)
    db = 20 * np.log10(np.sqrt((x ** 2).mean(axis=1)) + 1e-12)
    p95, p10 = (float(v) for v in np.percentile(db, [95, 10]))
    return {"loud_p95_db": round(p95, 1), "quiet_p10_db": round(p10, 1),
            "spread_db": round(p95 - p10, 1)}


def connect_resolve_handle():
    from src.utils.platform import discover_scripting_lib  # noqa: F401
    from src.utils.resolve_connection import connect_resolve
    try:
        import DaVinciResolveScript as dvr
    except ImportError:
        dvr = None
    resolve = connect_resolve(dvr)
    if resolve is None:
        raise SystemExit(
            "Could not reach DaVinci Resolve.\n"
            "On the free edition start the in-app bridge first:\n"
            "  python scripts/start_resolve_bridge.py"
        )
    return resolve


def render_master(resolve, target, out_dir: Path, name: str, preset: str,
                  timeline_name: Optional[str]) -> Tuple[Path, Dict[str, Any]]:
    """Render the picture master, then verify the file rather than the job.

    The job readback is not evidence. `GetRenderSettings` does not exist, so an
    inherited preset survives into the job unseen; the only honest check is that
    the written file has a video stream and the frame count the timeline says.
    """
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        raise SystemExit("No project is open in Resolve.")
    if timeline_name:
        for i in range(1, (project.GetTimelineCount() or 0) + 1):
            tl = project.GetTimelineByIndex(i)
            if tl and tl.GetName() == timeline_name:
                project.SetCurrentTimeline(tl)
                break
        else:
            raise SystemExit(f"No timeline named {timeline_name!r} in this project.")
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        raise SystemExit("No current timeline.")

    expected_frames = int(timeline.GetEndFrame()) - int(timeline.GetStartFrame())

    # Refuse a raster mismatch rather than deliver a pillarboxed file. This is
    # the defect that produced a whole cut inside black bars: nothing errors,
    # and the source frames look correct because they are.
    settings = _dt.to_render_settings(target, timeline_fps=None)
    want_w, want_h = settings.get("FormatWidth"), settings.get("FormatHeight")
    tl_w = project.GetSetting("timelineResolutionWidth")
    tl_h = project.GetSetting("timelineResolutionHeight")
    raster = {"timeline": f"{tl_w}x{tl_h}", "target": f"{want_w}x{want_h}"}
    if want_w and want_h and str(tl_w) and str(tl_h):
        if (str(want_w), str(want_h)) != (str(tl_w), str(tl_h)):
            raise SystemExit(
                f"Timeline is {tl_w}x{tl_h} but {target.id} delivers {want_w}x{want_h}.\n"
                "Rendering this would letterbox or pillarbox the picture. Set the "
                "project's timelineResolutionWidth/Height to match and rebuild the "
                "timeline (new timelines inherit; existing ones do not re-shape)."
            )

    project.LoadRenderPreset(preset)

    # Pin format and codec explicitly rather than inheriting them from the
    # preset. The preset is the *base state*, not the contract: point this at a
    # different target or a different preset and the container silently follows
    # the preset instead of the target. Resolved against the LIVE maps, so an
    # install that cannot render the pair fails with the available list rather
    # than queueing a job in whatever was already loaded.
    formats = project.GetRenderFormats() or {}
    fmt_id = _dt.select_available(target.format_candidates, formats)
    if not fmt_id:
        raise SystemExit(
            f"{target.id} needs one of {list(target.format_candidates)}; this "
            f"install offers {sorted(formats)}."
        )
    codecs = project.GetRenderCodecs(fmt_id) or {}
    codec_id = _dt.select_available(target.codec_candidates, codecs)
    if not codec_id:
        raise SystemExit(
            f"{target.id} needs one of {list(target.codec_candidates)} for format "
            f"{fmt_id}; this install offers {sorted(codecs)}."
        )
    if not project.SetCurrentRenderFormatAndCodec(fmt_id, codec_id):
        raise SystemExit(f"Resolve refused format/codec {fmt_id}/{codec_id}.")

    project.SetRenderSettings({**settings, "TargetDir": str(out_dir), "CustomName": name})
    job_id = project.AddRenderJob()
    project.StartRendering(job_id)
    while project.IsRenderingInProgress():
        time.sleep(2)

    produced = sorted(out_dir.glob(f"{name}.*"))
    if not produced:
        raise SystemExit(f"Render reported done but no file matching {name}.* appeared.")
    master = produced[0]

    info = probe(master)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise SystemExit(
            f"{master.name} has NO video stream. The Deliver page's inherited "
            "state overrode the job — re-run with --master-preset naming a video preset."
        )
    frames = int(video.get("nb_frames") or 0)
    if frames and abs(frames - expected_frames) > 1:
        raise SystemExit(
            f"{master.name} has {frames} frames; the timeline has {expected_frames}. "
            "The render did not cover the timeline."
        )
    return master, {"expected_frames": expected_frames, "master_frames": frames,
                    "raster": raster, "preset": preset,
                    "format": fmt_id, "codec": codec_id}


def build_audio(master: Path, work: Path, profile: str,
                target_i: float, target_tp: float) -> Tuple[Path, Dict[str, Any]]:
    """Enhance (optionally) then normalise, and measure both ends."""
    work.mkdir(parents=True, exist_ok=True)
    stem = work / "master_audio.wav"
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(master),
          "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s24le", str(stem)])
    if not stem.exists():
        raise SystemExit("Could not extract audio from the master.")

    before = measure_loudness(stem)
    before_blocks = block_levels(stem)

    chain = ENHANCE_PROFILES[profile]
    enhanced = work / "enhanced.wav"
    if chain:
        r = _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(stem),
                  "-af", chain, "-c:a", "pcm_s24le", str(enhanced)])
        if r.returncode != 0:
            raise SystemExit(f"Audio enhancement failed:\n{r.stderr.strip()}")
    else:
        enhanced = stem

    m = loudnorm_measure(enhanced, target_i, target_tp)
    final = work / "final_audio.wav"
    af = (f"loudnorm=I={target_i}:TP={target_tp}:LRA=11:"
          f"measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
          f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
          f"offset={m['target_offset']}:linear=true")
    r = _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(enhanced),
              "-af", af, "-c:a", "pcm_s24le", str(final)])
    if r.returncode != 0:
        raise SystemExit(f"Loudness normalisation failed:\n{r.stderr.strip()}")

    return final, {
        "profile": profile,
        "before": before, "before_blocks": before_blocks,
        "after": measure_loudness(final), "after_blocks": block_levels(final),
        "loudnorm_pass1": m,
    }


def encode_deliverable(master: Path, audio: Path, out: Path, bitrate: Optional[str]) -> None:
    encode = list(VIDEO_ENCODE)
    if bitrate:
        encode[encode.index("-b:v") + 1] = bitrate
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(master), "-i", str(audio),
           "-map", "0:v:0", "-map", "1:a:0", *encode,
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
           "-shortest", "-movflags", "+faststart", str(out)]
    r = _run(cmd)
    if r.returncode != 0:
        raise SystemExit(f"Deliverable encode failed:\n{r.stderr.strip()}")


def qc(path: Path, spec: Dict[str, Any], loud: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-field verdict against the target's own projections. Never auto-clears."""
    info = probe(path)
    fmt = info["format"]
    V = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    A = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    rows: List[Dict[str, Any]] = []

    def check(field: str, got: Any, want: Any, ok: bool) -> None:
        rows.append({"field": field, "got": got, "want": want,
                     "verdict": "PASS" if ok else "FAIL"})

    if spec.get("container"):
        got = fmt["format_name"].split(",")[0]
        check("container", got, spec["container"], got == spec["container"])
    vs = spec.get("video") or {}
    if V is not None:
        if vs.get("codec"):
            check("video.codec", V["codec_name"], vs["codec"], V["codec_name"] == vs["codec"])
        for key in ("width", "height"):
            if vs.get(key):
                check(f"video.{key}", V[key], vs[key], V[key] == vs[key])
        if vs.get("fps"):
            num, den = V["r_frame_rate"].split("/")
            fps = int(num) / int(den)
            check("video.fps", round(fps, 3), vs["fps"], abs(fps - vs["fps"]) < 0.01)
    else:
        check("video stream", "ABSENT", "present", False)
    aus = spec.get("audio") or {}
    if A is not None:
        if aus.get("channels"):
            check("audio.channels", A["channels"], aus["channels"], A["channels"] == aus["channels"])
        if aus.get("sampleRate"):
            got = int(A["sample_rate"])
            check("audio.sampleRate", got, aus["sampleRate"], got == aus["sampleRate"])
    else:
        check("audio stream", "ABSENT", "present", False)

    if loud:
        t = loud["target"]
        meas = measure_loudness(path)
        if t.get("integrated") is not None and meas["integrated_lufs"] is not None:
            tol = t.get("integratedTol", 2)
            ok = abs(meas["integrated_lufs"] - t["integrated"]) <= tol
            check("loudness.integrated", meas["integrated_lufs"],
                  f"{t['integrated']} +/-{tol}", ok)
        if t.get("truePeakMax") is not None and meas["true_peak_dbfs"] is not None:
            check("loudness.truePeak", meas["true_peak_dbfs"],
                  f"<= {t['truePeakMax']}", meas["true_peak_dbfs"] <= t["truePeakMax"])

    mb = int(fmt["size"]) / 1024 / 1024
    check("size (mobile upload cap)", f"{mb:.1f} MB",
          f"<= {MOBILE_UPLOAD_CAP_MB} MB", mb <= MOBILE_UPLOAD_CAP_MB)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", default="tiktok",
                        help="Delivery target or alias (tiktok, reels, shorts, ...).")
    parser.add_argument("--out", required=False, default=None,
                        help="Output directory for the master and deliverable.")
    parser.add_argument("--name", default=None, help="Base filename; defaults to the timeline name.")
    parser.add_argument("--timeline", default=None, help="Timeline to deliver; defaults to the current one.")
    parser.add_argument("--enhance", choices=sorted(ENHANCE_PROFILES), default="natural",
                        help="Audio enhancement profile. 'off' still normalises loudness.")
    parser.add_argument("--loudness", default="web",
                        help="Named loudness standard (web, podcast, ebu_r128, atsc_a85, ...).")
    parser.add_argument("--bitrate", default=None, help="Override the video bitrate, e.g. 10M.")
    parser.add_argument("--master-preset", default=DEFAULT_MASTER_PRESET,
                        help="Resolve render preset pinned as the master's base state.")
    parser.add_argument("--refresh-master", action="store_true",
                        help="Re-render the master even if one is already there.")
    parser.add_argument("--dry-run", action="store_true", help="Report the plan and stop.")
    args = parser.parse_args()

    require_tools()

    standard = _dt.normalize_loudness_standard(args.loudness)
    if standard is None:
        raise SystemExit(f"Unknown loudness standard {args.loudness!r}. "
                         f"Known: {', '.join(sorted(_dt.LOUDNESS_STANDARDS))}")
    target = _dt.resolve_target(args.target, {"loudness_standard": standard})
    if target is None:
        raise SystemExit(f"Unknown delivery target {args.target!r}.")
    loud = _dt.to_loudness_target(target)
    resolve = connect_resolve_handle()
    project = resolve.GetProjectManager().GetCurrentProject()
    timeline = project.GetCurrentTimeline() if project else None
    try:
        timeline_fps = float(project.GetSetting("timelineFrameRate")) if project else None
    except (TypeError, ValueError):
        timeline_fps = None
    spec = _dt.to_qc_spec(target, timeline_fps=timeline_fps)
    if spec is None:
        raise SystemExit(f"{target.id} has no single-file QC projection; "
                         "this pipeline delivers one file.")
    base = args.name or (timeline.GetName().replace(" ", "_") if timeline else "deliverable")
    out_dir = Path(args.out).expanduser() if args.out else Path.home() / "Movies" / base
    stamp = datetime.now().strftime("%Y%m%d")
    master_name = f"{base}_MASTER"
    deliverable = out_dir / f"{base}_{target.id}_{stamp}.mp4"

    if args.dry_run:
        print(json.dumps({
            "target": target.id, "loudness_standard": standard,
            "qc_spec": spec, "loudness_target": loud,
            "enhance_profile": args.enhance,
            "master": str(out_dir / f"{master_name}.mp4"),
            "deliverable": str(deliverable),
            "video_encode": " ".join(VIDEO_ENCODE),
        }, indent=2))
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / ".work"

    existing = sorted(out_dir.glob(f"{master_name}.*"))
    if existing and not args.refresh_master:
        master, render_meta = existing[0], {"reused": True}
        print(f"[1/4] master     reusing {master.name} (--refresh-master to re-render)")
    else:
        print(f"[1/4] master     rendering from Resolve ...")
        master, render_meta = render_master(resolve, target, out_dir, master_name,
                                            args.master_preset, args.timeline)
        print(f"                 {master.name}  ({render_meta['master_frames']} frames)")

    t = loud["target"] if loud else {"integrated": -16, "truePeakMax": -1}
    print(f"[2/4] audio      profile={args.enhance}  -> {t['integrated']} LUFS / {t['truePeakMax']} dBTP")
    audio, audio_meta = build_audio(master, work, args.enhance,
                                    t["integrated"], t.get("truePeakMax", -1) - 0.5)
    b, a = audio_meta["before"], audio_meta["after"]
    print(f"                 {b['integrated_lufs']} -> {a['integrated_lufs']} LUFS, "
          f"peak {b['true_peak_dbfs']} -> {a['true_peak_dbfs']} dBFS")
    if audio_meta["before_blocks"] and audio_meta["after_blocks"]:
        print(f"                 voice-above-room spread "
              f"{audio_meta['before_blocks']['spread_db']} -> "
              f"{audio_meta['after_blocks']['spread_db']} dB")

    print(f"[3/4] encode     {deliverable.name}")
    encode_deliverable(master, audio, deliverable, args.bitrate)

    print(f"[4/4] qc         against {target.id}")
    rows = qc(deliverable, spec, loud)
    width = max(len(r["field"]) for r in rows)
    for r in rows:
        print(f"      {r['field'].ljust(width)}  {str(r['got']):>20}  "
              f"{str(r['want']):>18}  {r['verdict']}")
    failed = [r for r in rows if r["verdict"] == "FAIL"]

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timeline": timeline.GetName() if timeline else None,
        "target": target.id, "loudness_standard": standard,
        "master": str(master), "deliverable": str(deliverable),
        "render": render_meta, "audio": audio_meta,
        "video_encode": " ".join(VIDEO_ENCODE),
        "qc": rows, "gate": "review", "passed": not failed,
    }
    (out_dir / f"{base}_{target.id}_{stamp}.manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")

    shutil.rmtree(work, ignore_errors=True)
    if failed:
        print(f"\nGATE: review - {len(failed)} FAILURE(S). Not deliverable as-is.")
        return 1
    print(f"\nGATE: review - all {len(rows)} fields pass. Deliverable: {deliverable}")
    print("      A gate is a report, not a clearance. Watch it before you post it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
