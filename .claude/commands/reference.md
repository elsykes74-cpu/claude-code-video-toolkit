---
description: Start from a video you love - analyze a reference, get concepts + cost before production
---

# Reference

Turn a reference video (YouTube, Short, Reel, TikTok, or a local clip) into a grounded
production plan. Wraps `tools/reference_analyze.py`, which *measures* the reference; this
command does the *creative read* and hands off to `/video`.

```
URL/file → measure (shots, pacing, wpm, hook, beats, keyframes) → read → keep/change
        → 2-3 concepts with tool path + cost → user picks → /video with a pre-filled plan
```

**Ground rule:** the reference is a study, not a source. Never put its footage, audio, music,
or verbatim script into a render. Keep *structure and craft* (pacing, hook mechanics, beat
shape, shot rhythm); write new words and generate new visuals.

## Step 1: Get the reference

If the user gave a URL or path, use it. Otherwise ask for one, plus the **new topic** ("make
something like this, but about X"). The topic can come later — the analysis doesn't need it.

## Step 2: Measure it

```bash
cd /path/to/claude-code-video-toolkit && python3 tools/reference_analyze.py \
  --input "URL_OR_PATH" --json
```

- Output lands in `references/<slug>/` (gitignored): `REFERENCE.md`, `analysis.json`,
  `contact_sheet.jpg`, `keyframes/`, `transcript.txt`.
- **Download blocked** (403, "logged-in", bot check): suggest `--cookies-from-browser chrome`,
  or have the user download the clip and re-run with `--input clip.mp4` (a `.srt`/`.vtt` with
  the same name next to it is picked up automatically).
- **No transcript**: the tool tries platform captions → local whisper → ElevenLabs Scribe.
  If all are missing it still measures cutting/hook/visuals; say narration metrics are
  unavailable and offer `pip install openai-whisper` or `--transcript file.srt`.
- **Cuts look wrong** (one 40s "shot" in an obviously cut video, or dozens of 0.4s shots in a
  slow piece): re-run with `--scene-threshold 0.2` (more sensitive) or `0.45` (less).
  Fades and dissolves under-count at the default.

## Step 3: Look, don't just read

1. Read `references/<slug>/REFERENCE.md`.
2. **View `contact_sheet.jpg`** (and individual keyframes for the hook shots). The numbers
   say *how fast*; the frames say *what* — framing (talking head / b-roll / screen / text
   cards / animation), on-screen text style, color, subject.

## Step 4: Present the breakdown

Keep it tight. Use the measured numbers; label anything inferred from frames as inferred.

```
REFERENCE: "<title>" — <duration>s <aspect>, <shots> shots (avg <ASL>s, <pacing_class>),
<wpm> wpm (<tier>), first word at <t>s, speech covers <pct>% of runtime.

Hook (0-3s): <cuts> cuts, <words> words — "<opening line>". <what's on screen, from frames>
Structure: <beat summary: e.g. hook → 3 numbered points → payoff → CTA>
Visual language: <from frames: e.g. full-bleed b-roll + bold caption pills, no presenter>

WHAT TO KEEP          WHAT TO CHANGE
- <pacing/hook/beat>  - <topic/visual treatment/voice/length>
```

Interpretation guide (what the numbers mean for a remake):

| Signal | Implication |
|---|---|
| `pacing_class` frenetic/fast (ASL < 3s) | Needs lots of visual assets — budget 1 asset per ~2s, or reuse stills with Ken Burns/cuts |
| `wpm` > 170 | Reference is rushed; matching it with TTS needs a fast voice — or deliberately slow to 155-165 and trim words |
| `time_to_first_word` < 0.5s | Cold-open narration — script the first line as the hook |
| `speech_coverage_pct` < 50% | Music/visual-led — narration is sparse; invest in music + visuals |
| Hook `cuts` ≥ 2 | Hard visual hook — plan 2-3 distinct shots in the first 3s |
| `silence_pct` high + low LUFS | Quiet/ambient piece; low silence + ~-14 LUFS = loud, compressed social mix |

## Step 5: Propose 2-3 concepts

Each concept must be **differentiated** (not three variants of one idea) and **honest about
the toolkit's real capabilities**. For each:

```
CONCEPT A — "<working title>"
Angle:       <one line>
Keeps:       <which reference mechanics>
Changes:     <what's new>
Template:    <concept-explainer-short | product-demo | sprint-review | moviepy build.py>
Length:      <s>  ·  <n> scenes  ·  ~<words> words @ <wpm>
Visual path: <e.g. 6 Ideogram cards + 4 LTX-2 b-roll clips + FLUX bg>
Audio path:  <voiceover.py (ElevenLabs/Qwen3) + music_gen preset>
Est. cost:   ~$<x>  (<itemized>)
Risk:        <what might not land — e.g. LTX-2 can't render legible text; fast cutting = many assets>
```

**Template fit:** vertical → `concept-explainer-short` (or moviepy for sub-30s ad spots);
landscape marketing → `product-demo`; landscape team update → `sprint-review`. The tool's
`suggested_template` is only an aspect/length hint; over ~3 min it reports no match.

**Costing:** count assets from the beat/shot plan × the per-unit estimates in the README's
Cloud GPU table and CLAUDE.md (Ideogram 4 ~$0.03-0.09/img; LTX-2 ~$0.23/clip; Qwen3-TTS
~$0.01; music_gen free via acemusic). ElevenLabs costs depend on the user's plan — say so
rather than inventing a number. Show the arithmetic; round up; call it an estimate.

**Honesty rules:** if the reference relies on something the toolkit can't do well (live-action
people, real product footage, licensed music, celebrity voices), say so and offer the
nearest achievable treatment instead of promising a match.

## Step 6: Hand off

When the user picks a concept (or mixes them):

1. Draft the scene plan from the reference's **beats** — one scene per beat (merge tiny
   beats, split beats >15s). Per scene: duration, word budget (`duration × 2.5` at 150 wpm,
   or the chosen pace), visual asset, on-screen text.
2. Write the new script to that budget. Fresh wording — never paraphrase the reference
   line-by-line.
3. Run `/video` to create the project with the chosen template, passing the scene plan.
   Record the reference in `project.json` (e.g. a `reference` block with the source URL and
   `references/<slug>/analysis.json` path) so later sessions can compare pacing.
4. After voiceover, compare the new cut against the reference: run
   `python3 tools/reference_analyze.py --input projects/NAME/out/video.mp4` and diff
   ASL / wpm / hook numbers against the original.
