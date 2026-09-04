---
name: house-style
description: The editorial and finishing preferences this project's work is judged against — cut rhythm, shot selection, delivery conventions, and the corrections that have already been given. Load before assembling, restructuring, or refining any cut so the same note does not have to be given twice.
user-invocable: false
---

# House Style

The craft guides in `docs/guides/` describe editing in general. This file
describes **how this editor wants it done** — the accumulated, specific
corrections that would otherwise have to be repeated every session.

Read it before any edit task. Append to it whenever a correction is given.

## The capture protocol

This file is only worth what gets written into it. When the user corrects an
editorial decision — rejects a cut, changes a shot choice, adjusts a duration,
says "not like that" — do not just fix it. Fix it, then add the rule here.

A useful entry has three parts:

- **The rule**, stated as an instruction, not an observation.
- **Why**, in the user's terms — what it was in service of.
- **The trap**, if there is one: what makes it easy to get wrong.

Write rules that are falsifiable. "Cut on motion" is a rule; "make it feel
dynamic" is not. If a correction is one-off and situational, it does not belong
here — this file is for what generalizes.

When an entry turns out to be wrong or too broad, edit it. A stale rule
confidently followed is worse than no rule.

---

## Pacing and rhythm

<!-- Hold lengths, when to cut early, what "too long" means for this material. -->

_Not yet captured._

## Shot selection

<!-- What earns a place in the cut; what gets dropped even when it's a good shot. -->

_Not yet captured._

## Cut points

<!-- Cut on motion vs on rest, handles, how much air before and after a beat. -->

**Place every cut point inside measured silence, then verify all edges before
building.** Derive the candidate points from the transcript's word timings, but
validate each one against `silencedetect` run on the clip's own extracted audio
stem, and snap any edge that lands mid-speech to the nearest silence.

- **Why:** confirmed to work on the Ridge Wallet ad (2026-08-27) — the editor's
  note was "I like the way the cuts were made". Cutting on the picture, or on
  word boundaries alone, produces clipped words that read as cheap.
- **The trap:** Whisper emits *contiguous* word spans, so each word's `end` is
  the next word's `start`. Testing "is this point inside a word" against the
  transcript therefore flags almost every point and tells you nothing. Only the
  audio's own silence map is authoritative. On the first pass of that cut, 6 of
  18 edges landed mid-speech and had to be snapped and the timeline rebuilt.
- Threshold matters on quiet recordings: −32 dB cut into the talent's quieter
  words on a −40 LUFS take. Measure the stem first and pick the floor from it.

**When a snapped edge would open on a stray fragment, merge the segment into its
neighbour rather than keeping the fragment.** The CTA in that cut would have
started on an orphaned "of"; joining the two segments read better than the
extra jump cut.

## Structure and openings

<!-- How a piece starts, what the first frames have to do, how it lands. -->

_Not yet captured._

## Rejected by default

Things not to add unless explicitly asked. Seeded from the rough-cut deliverable
contract in the `resolve-rough-cut` skill, which exists because this work gets
thrown away:

- Titles, captions, and text cards
- Transitions (an assembly is hard cuts)
- Effects and speed ramps
- Music beds
- Grading on a cut that was asked for as an assembly

## Delivery conventions

<!-- Aspect ratios, timeline naming, versioning, where renders go. -->

**Check the project's timeline resolution before building any cut, and set it to
match the delivery shape.** Social verticals are **1080x1920**.

- **Why:** the project defaulted to 1920x1080 landscape while the source was
  2160x3840 vertical. With `timelineInputResMismatchBehavior: scaleToFit` the
  picture was pillarboxed into roughly 608 of 1920 pixels — 32% of frame — and a
  whole cut was assembled that way before anyone looked at the output.
- **The trap:** nothing errors, and source frames look correct because they are.
  The defect only exists in the *timeline's* output. Verify by rendering an
  actual timeline frame (`timeline_frame capture`, or `export_frame_as_still`
  with the playhead parked on a clip), not by reading clip metadata.
- Keys: `timelineResolutionWidth/Height` plus `timelineOutputResolutionWidth/Height`.
  All four are writable and read back. New timelines inherit them; rebuild rather
  than trying to re-shape an existing timeline.

**Loudness is a delivery-stage fix, not an edit-stage one.** Resolve exposes no
scriptable audio gain (`SetProperty('Volume')` returns False, and it reads back
`null`) and cannot normalise at render. Cut with the original linked audio, then
two-pass `loudnorm` the rendered file. Target the repo's named `web` standard —
**-16 LUFS +/-2, true peak <= -1 dBTP** — rather than inventing a number.

- **The trap:** a straight gain to hit the target clips. On a take with an 18 LU
  peak-to-loudness ratio, normalising to -16 put peaks at +2.4 dBFS and produced
  610 clipped samples. Resolve's own *Normalize Audio Levels* in Loudness mode
  has the same failure. Use peak-aware normalisation, or Sample Peak mode as a
  safe interim while editing.

**Render a master from Resolve, then derive each platform file with one
controlled ffmpeg pass** that sets bitrate and applies loudnorm together.

- **Why:** `VideoQuality` is refused by `SetRenderSettings` on 21.0.4.5 — it is
  the one key that writes `False` — so bitrate cannot be pinned from a script.
  Resolve's own "TikTok - 1080p" preset rendered 1080x1920 at **130 Mbps /
  630 MB for 40 seconds**, over TikTok's 287.6 MB mobile upload cap. The same
  pass has to fix loudness anyway, so it costs nothing extra.
- Settled recipe for vertical social: `libx264 -preset slow -profile:v high
  -pix_fmt yuv420p -b:v 12M -maxrate 16M -bufsize 24M`, AAC 192k 48 kHz stereo,
  `-movflags +faststart`. Produced 58.9 MB at 12.2 Mbps, all QC fields passing.
- **Always pass `from_preset`.** The Deliver page keeps whatever state it last
  held: after capturing timeline stills it was sitting on `jpg / YUV420_8`, and
  every key not explicitly passed inherits from that. `GetRenderSettings` is
  unavailable, so the inherited state cannot be read — only pinned.
- **Verify the output file, never the job readback.** `EncodingProfile`,
  `MultiPassEncode` and `NetworkOptimization` all report success and cannot be
  confirmed. ffprobe for an actual `codec_type=video` stream, the frame count,
  and `moov` before `mdat` for faststart.
- Expect the audio stream to run ~1 frame longer than the video after AAC
  encoding. That is encoder tail padding, not drift — compare the video's
  `nb_frames` against the timeline to tell them apart.

**Deliver with `scripts/deliver_social.py`, not by hand.** It renders the master,
enhances and normalises audio, encodes the platform file, QCs every field against
the target's own projections, and writes a manifest. Hand-rolling the steps is how
the pillarbox, the 630 MB upload and the -40 LUFS audio each shipped once already.

    python scripts/deliver_social.py --target tiktok --out <dir>
    python scripts/deliver_social.py --target tiktok --dry-run

- **Why:** every number it asserts is measured off the finished file, and a QC
  failure is a non-zero exit rather than a cheerful summary. It refuses outright
  when the timeline raster does not match the target, which is the one defect
  that looks correct at every earlier stage.
- `--enhance natural` (default) / `aggressive` / `off`. Loudness normalisation is
  applied regardless: a deliverable off programme loudness is not a deliverable.
- The master is kept and reused; `--refresh-master` re-renders. Deliverables are
  disposable and platform-shaped, the master is what you re-version from.
- The gate is **review, never auto-clear.** All-pass means the measurements agree
  with the spec — it does not mean anyone watched it. Watch it before posting.

**Voice Isolation does nothing on the free edition, and lies about it.**
`SetVoiceIsolationState` accepts a state and `GetVoiceIsolationState` reads it
back, but a render with it at 100 null-tests against a render with it off at
**-66 dBFS** — re-encode noise. Do not offer it as a fix, and do not trust the
readback as evidence that it applied. Note the two setters are different scopes:
`timeline` sets the TRACK (`SetVoiceIsolationState(track_index, state)`),
`timeline_item` sets the CLIP. Setting one and reading the other proves nothing.

**Repair room tone between words, not on the voice.** The chain that worked on
phone-shot dialogue in a live room: highpass 85 Hz, `afftdn`, then an `agate`
whose only job is ducking the room tail in the gaps, then -3 dB at 320 Hz for
boxiness and a presence lift around 3.2 kHz. Measured effect: between-word bed
-23.9 -> -58.9 dB, voice-above-room spread 14.4 -> 46.9 dB.

- **The trap:** judging this from two hand-picked windows. A gap that is not
  actually a gap reported a 0.7 dB improvement for a chain that was in fact
  delivering ~35 dB. Measure block-RMS percentiles (p95 vs p10) over the whole
  programme instead.
- Nothing here separates direct sound from reflections. Reverb printed onto the
  voice stays. Say so rather than implying the recording was rescued.

---

## Where personal grading taste lives

Colour and look preferences are **not** kept here — this file travels with the
repository. Grading taste lives in the user-level `colorist-assistant` skill and
in persistent memory. Load those for look selection, grade transfer, and the
Resolve API traps around them.

If any entry below would be specific to one person rather than to this project's
work, it belongs in the user-level skill instead, and this file should be
gitignored rather than committed.
