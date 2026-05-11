# SiKAT Zamboanga Highlight Pipeline — Runbook

---

## Planned v2 — Shared Learning System

### Goal
The system learns from every finished game. Over time, calibration requires
fewer manual anchor points because the model already knows where stoppages
cluster for each league.

### How it works

**After each game**, the system automatically extracts derived metrics from
the confirmed CALIBRATION_ANCHORS and writes them to `game_stats/{game_id}.json`:

```json
{
  "game_id": "2836518",
  "league": "mpbl",
  "date": "2025-05-08",
  "quarter_breaks": [185, 710, 192],
  "segments": [
    {"quarter": 1, "gt_from": "09:44", "gt_to": "08:53", "rtf": 1.12},
    {"quarter": 1, "gt_from": "08:53", "gt_to": "07:22", "rtf": 1.31},
    ...
  ]
}
```

Each segment's RTF (real seconds ÷ game-clock seconds) reveals stoppages:
- RTF ~1.0 = pure running clock, no stoppages
- RTF ~1.5–2.0 = normal play with minor stoppages
- RTF ~4.0+ = timeout, challenge, or review inside that segment

**Sharing is via git — no server needed:**

```
finish game → game_stats/{game_id}.json written → git push
                                                        ↓
other users git pull → receive all contributed game stats
                                                        ↓
new_game.py reads game_stats/*.json → computes league averages
         → suggests smarter defaults for REAL_TIME_FACTOR,
           QUARTER_BREAK_SECONDS, HALFTIME_BREAK_SECONDS
```

Each game is its own file so there are never merge conflicts. The more
users contribute, the richer the dataset becomes — passively, through
normal git workflow.

**What the model learns per league over time:**

```
Q1 early (10:00→07:00)  avg RTF: 1.3   (few stoppages, running clock)
Q1 mid   (07:00→04:00)  avg RTF: 1.8
Q1 late  (04:00→00:00)  avg RTF: 2.4   (more fouls, timeouts pile up)
Q4 late  (02:00→00:00)  avg RTF: 3.5+  (intentional fouls, reviews)
```

**End state:** new games need only 1–2 anchors per quarter to confirm the
model, rather than 5–6 to derive it from scratch.

### Files to build
- `game_stats/` — folder committed to repo, one JSON per game
- `analyze.py` — reads game_stats/*.json, computes per-league averages
- Updated `new_game.py` — pulls latest averages and pre-fills defaults
- Updated `vod_replay.py --calibrate` — writes game_stats file on completion

### Design decisions to confirm before building
- Should game_stats be on `main` or a dedicated `data` branch?
- Minimum number of anchors required before a game's stats are trusted?
- How to handle outlier games (broadcast delays, technical issues)?

---

## Planned v3 — Public / Multi-Team Architecture

### Current limitations at scale

The current git-based approach is not sustainable for public use:
- Everyone clones the same repo including `config.py` with team-specific data
- Multiple teams' config changes conflict on git
- `AUDIO_CONFIRM_KEYWORDS` is hardcoded per team — every user has to update manually
- Sharing game stats back requires write access to the same repo
- No separation between the tool and each team's data

### Target architecture

```
cliphigh/            ← public repo (the tool only, no team data)
  vod_replay.py
  new_game.py
  ...

my-team-data/        ← private, per team (their own repo or local folder)
  config.py
  game_stats/
  highlights/
```

The tool reads config from wherever the user points it. Game stats are
contributed to a separate shared data repo via pull request — no write
access to the main tool repo needed.

### Business model options

| Model | Effort | Learning |
|---|---|---|
| Free open source | Low | Fragmented — forks don't share back |
| Licensed tool + shared data pool | Medium | Teams contribute anonymously to pool |
| Hosted SaaS | High | Centralised — automatic, richest data |

The SaaS model is most sustainable for the learning system (v2) but is a
significantly larger build. The licensed tool + shared data pool is a
reasonable middle ground.

### Recommendation

Validate with a few more teams using the current approach before
committing to a public architecture. Once there is confirmed demand,
revisit this section and choose a model.

---

## FOR CLAUDE — READ THIS FIRST

Instructions are in `CLAUDE.md` (auto-loaded by Claude Code). Read this
entire RUNBOOK before doing any work — it contains confirmed calibration
values, workflow decisions, and lessons learned that are not obvious from
the code alone.

---

## Overview

Automated system that reads FIBA LiveStats play-by-play data and cuts highlight
clips from a YouTube VOD for every SiKAT Zamboanga scoring play, then compiles
per-quarter highlight reels.

---

## Machine Setup (one-time)

### Python
- Use Python 3.9+. All commands use `python3`.
- Install dependencies: `pip3 install -r requirements.txt`
- NumPy must be <2 for Whisper/PyTorch compatibility:
  `pip3 install "numpy<2"`

### yt-dlp
- Do NOT use pip-installed yt-dlp (requires Python 3.10+).
- Use the standalone macOS binary:
  ```
  curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos \
       -o /usr/local/bin/yt-dlp && chmod +x /usr/local/bin/yt-dlp
  ```

### ffmpeg
- If `brew install ffmpeg` fails (e.g. no full Xcode on macOS 13):
  Download the static binary from https://evermeet.cx/ffmpeg/ and place it in
  `/usr/local/bin/ffmpeg`.

---

## Folder Structure

```
sikat-highlights/
  config.py           <- all settings for the current game (change this per game)
  vod_replay.py       <- main script for completed/archived games
  pipeline.py         <- live game mode (polls feed in real time)
  fiba_poller.py      <- FIBA LiveStats feed reader
  clipper.py          <- clip cutter (used by pipeline.py)
  audio_verifier.py   <- Whisper-based audio verification
  stream_recorder.py  <- yt-dlp + ffmpeg stream recorder (live mode)
  publisher.py        <- Facebook upload or local save
  requirements.txt
  RUNBOOK.md          <- this file

  recording/
    stream.mp4        <- downloaded VOD (reused with --skip-download)

  clips/
    {opponent}/       <- individual scoring clips per game
      Q1_0938_J_Felicilda_2pt.mp4
      ...

  highlights/
    {opponent}/       <- compiled quarter reels per game
      1/
        SiKAT_Q1_20250508.mp4
      2/
      3/
      4/

  review/             <- clips that failed audio verification (inspect manually)
```

---

## Per-Game Workflow

### 1. Run new_game.py to set up config

```
python3 new_game.py
```

This prompts for all required details and writes a fresh `config.py`. It also
offers to clear clips, highlights, and the old recording file from the previous
game. Always start a new game with this script.

Fields it asks for:
- FIBA game ID
- Opponent name and short folder name
- Audio reject keywords (opponent city/nickname)
- YouTube URL
- Tip-off video time (can be skipped and filled in later)
- Clip lead/tail seconds
- Whether to include free throws in highlights

### 2. Update config.py for anything new_game.py does not cover

Change these values for every new game:

| Setting | What to change |
|---|---|
| `GAME_ID` | FIBA LiveStats game ID |
| `OPPONENT` | opponent name in lowercase, used for folder names |
| `YOUTUBE_STREAM_URL` | YouTube VOD or live stream URL |
| `TIPOFF_VIDEO_SECONDS` | seconds into the video where tip-off occurs |
| `CALIBRATION_ANCHORS` | confirmed (video_sec, quarter, game_clock) triples |
| `AUDIO_REJECT_KEYWORDS` | add opponent team name and nickname |

Finding the FIBA game ID: open the FIBA LiveStats URL for the game and note
the numeric ID in the URL or page source.

### 2. Download the VOD (first quarter only)

```
python3 vod_replay.py --quarters 1
```

This downloads to `recording/stream.mp4` automatically. For all subsequent
quarters always use `--skip-download` to reuse the same file.

### 3. Follow the 3-step flow for each quarter

**Step 1 — Dry-run:** see estimated timestamps
```
python3 vod_replay.py --dry-run --quarters 1
```

**Step 2 — Calibrate:** verify each play interactively, anchors auto-saved
```
python3 vod_replay.py --calibrate --quarters 1
```
For each play, press Enter if the estimate is correct, or type the actual
video time (e.g. `7:39` or `1:02:07`). Corrections are written to config.py
automatically. Re-run `--calibrate` as many times as needed until all plays
look right.

**Step 3 — Generate:** cut clips and compile the highlight reel
```
python3 vod_replay.py --quarters 1 --skip-download
```

The script prints the next command to run at the end of each step.

For Q4 when the stream ended early, add `--min-clock MM:SS` to step 3:
```
python3 vod_replay.py --quarters 4 --min-clock 03:05 --skip-download
```

If the stream ended before the quarter finished, use `--min-clock`:
```
python3 vod_replay.py --quarters 4 --min-clock 03:05 --skip-download
```

### 5. Cleanup rules

Each game has its own subfolder — you never need to delete another game's
highlights before starting a new one. The folder structure isolates everything:
`highlights/{league}/{team}/{opponent}/{quarter}/`

Only delete files for the specific quarter you are regenerating:
```
# Example: wipe only Q2 of the current game before a re-run
rm -rf highlights/mpbl/zamboanga/ilagan/2/
rm -f  clips/mpbl/zamboanga/ilagan/Q2_*
```

Never run `rm -rf clips/` or `rm -rf highlights/` — that wipes every game
across every league and team.

---

## Calibration Process

The system maps game clock to video timestamp using piecewise linear
interpolation between confirmed anchor points. More anchors = more accuracy.

### What is an anchor?

A triple: `(video_seconds, quarter, "MM:SS game clock")`

Confirm it by pausing the YouTube video at a recognisable play and noting:
- The video timestamp (e.g. `7:39` = `7*60+39 = 459`)
- The quarter number
- The game clock displayed on screen (`09:38`)

### Tip-off anchor

Always set `TIPOFF_VIDEO_SECONDS` first. Pause the video at the exact moment
the ball goes up. This is the base anchor everything else builds on.

### Adding calibration anchors

```python
CALIBRATION_ANCHORS = [
    (video_seconds, quarter, "MM:SS"),
    ...
]
```

Run `--dry-run` after each addition to see the effect on estimated timestamps.

### Recommended anchor density

- Minimum: 1 anchor per quarter
- Ideal: 1 anchor every 2-3 minutes of game clock, especially around
  long timeouts, challenges/reviews, and halftime
- Free throws make perfect dead-ball anchors (exact clock, no action
  ambiguity). Keep `INCLUDE_FREETHROWS = False` so they do not appear
  in the highlight reel but still calibrate the timeline.

### Quarter and halftime break values

These are NOT fixed — they depend on how much of the break the broadcast shows:

| Setting | Confirmed value (Iloilo game 2835583) |
|---|---|
| `QUARTER_BREAK_SECONDS` | 190s (~3 min broadcast break after Q1 and Q3) |
| `HALFTIME_BREAK_SECONDS` | 700s (~11 min broadcast halftime) |
| `REAL_TIME_FACTOR` | 2.2 (real seconds per game-clock second, extrapolation only) |

Re-derive from anchor data at the start of each new game if results look off.

### Calibration checklist per quarter

1. Run `--dry-run` for the quarter
2. For each play without a nearby confirmed anchor, check its estimated
   video timestamp in YouTube
3. If wrong, note the actual timestamp, add it as an anchor in `CALIBRATION_ANCHORS`
4. Re-run `--dry-run` and repeat until all plays look correct
5. Run the quarter for real

---

## Common Issues and Fixes

### Clips show timeouts, dead balls, or turnovers instead of baskets

Cause: anchors too sparse; linear interpolation drifts around a timeout or review.

Fix: add more anchors. Run dry-run, find the drifted play in the video, note
the actual timestamp, add the anchor, re-run dry-run.

### Clips show opponent plays at the end

Cause: `CLIP_TAIL_SECONDS` captures whatever happens after SiKAT scores,
including the opponent's next possession.

Fix: crop manually, or reduce `CLIP_TAIL_SECONDS` in config.
This is most common on the last play of a quarter (very little clock left).

### Two plays overlap in the highlight reel

Cause: two scoring plays are fewer than `CLIP_LEAD_SECONDS + CLIP_TAIL_SECONDS`
apart on the game clock (e.g. fast break followed immediately by a putback).

Fix: this is acceptable and expected for back-to-back plays. No action needed.

### Stream ends before the quarter finishes

Use `--min-clock MM:SS` to skip plays beyond the cutoff:
```
python3 vod_replay.py --quarters 4 --min-clock 03:05 --skip-download
```

If there is a continuation video (separate YouTube upload for the remaining
minutes), download it separately, calibrate its own tip-off and anchors, and
cut clips from it manually or treat it as a second pass. Note: plays that
fall in the gap between the two videos have no footage and cannot be recovered.

### Audio verification sends clips to `review/`

Verification uses **crowd noise energy detection** (replaced Whisper).
It measures RMS audio energy before and after the basket moment and
looks for a crowd noise spike in the tail window.

Tuning knobs in config.py:
- `CROWD_NOISE_SILENCE_THRESHOLD` — clips below this RMS are flagged silent (default 800)
- `CROWD_NOISE_SPIKE_RATIO` — tail must be this much louder than lead (default 1.2)
- `CROWD_NOISE_CONSISTENT_FACTOR` — if overall RMS > threshold × factor, pass regardless (default 4)

If too many real baskets are rejected: lower `CROWD_NOISE_SPIKE_RATIO` or
`CROWD_NOISE_CONSISTENT_FACTOR`. If bad clips are passing: raise them.

Note: **anchored plays skip verification entirely.** Only interpolated plays
are checked. To disable entirely: `AUDIO_VERIFY = False`

### yt-dlp downloads separate video and audio files (no ffmpeg at download time)

Merge them manually:
```
ffmpeg -i recording/stream.f399.mp4 -i recording/stream.f140.m4a \
       -c copy recording/stream.mp4
```

---

## Lessons Learned (Iloilo game — ID 2835583)

- **Tip-off time:** confirmed at 7m04s into the video, not the initially
  estimated 7:44. Always confirm the tip-off anchor first — everything else
  depends on it.

- **Halftime break:** broadcast halftime was ~700s (~11 min), not the standard
  10 min game rest. The broadcast cuts away and returns mid-break so the visible
  gap is longer than expected.

- **Anchor ordering must match game clock order:** a play at 05:07 remaining
  happens before a play at 01:24 remaining. If video timestamps appear reversed
  between two anchors, the wrong play was anchored — re-check which FIBA event
  matches which video moment. The code sorts by absolute game seconds so inverted
  anchors silently produce wrong interpolation.

- **Back-to-back fast breaks have a real-time factor near 1.0:** Gabat (Q2 01:24)
  and Felicilda (Q2 01:15) were 9 game-clock seconds apart with 9 real seconds
  between them in the video. No stoppages = no inflation. This is correct.

## Lessons Learned (Ilagan game — ID 2836518)

- **New calibrate flow worked:** Q1 was perfect on the first attempt — the dry-run → calibrate → generate flow prevented the bad-output-then-fix cycle that Iloilo Q1 required.

- **Old recording was reused:** `recording/stream.mp4` still contained the Iloilo video when the Ilagan game was set up. `new_game.py` asked to delete it but defaulted to No. Always delete the old recording before starting a new game.

- **Calibrate mode accepted duplicate/inverted timestamps:** Q4 anchors had the same video time for different game clocks, and some game clocks earlier in the period had higher video times than later ones. Fixed: calibrate mode now validates that each new timestamp is greater than the previous one and warns the user before accepting.

- **Audio verification fails frequently:** Whisper is not finding confirm keywords in many clips. Likely cause: commentary language varies by venue and broadcaster. Consider expanding `AUDIO_CONFIRM_KEYWORDS` after inspecting transcripts in `review/`, or set `AUDIO_VERIFY = False` for faster iteration and verify manually.

- **HALFTIME_BREAK_SECONDS varies by game:** Iloilo was ~700s; Ilagan appears to be ~986s based on anchor data. This default needs re-confirming each game from the Q2-last-anchor to Q3-first-anchor gap.

---

- **Next-step prompt showed wrong quarter:** was suggesting quarters before the ones just run instead of after. Fixed — now only suggests quarters with a higher number than the last one processed.

- **Q4 continuation video gap:** original broadcast ended at Q4 3:05. A second
  upload started at Q4 1:42. The play at Q4 2:35 (Salim) fell in the gap with
  no footage in either video. The play at Q4 0:11 (Are) was missing from the
  continuation upload. Q4 ended up with 7 of 9 plays.

- **REAL_TIME_FACTOR** only applies when extrapolating beyond the last anchor.
  With dense anchors it rarely matters, but 2.2 is a safe default for late-game
  when stoppages accumulate.

- **Free throw plays serve dual purpose:** they are precise dead-ball calibration
  anchors (exact clock moment) but are excluded from the highlight reel with
  `INCLUDE_FREETHROWS = False`.

---

## Config Quick-Reference for a New Game

```python
GAME_ID = "XXXXXXX"
OPPONENT = "opponent_name"          # lowercase, no spaces
YOUTUBE_STREAM_URL = "https://www.youtube.com/watch?v=XXXXXXX"
TIPOFF_VIDEO_SECONDS = 0            # fill in after watching the video

CALIBRATION_ANCHORS = [
    # (video_seconds, quarter, "MM:SS"),
]

AUDIO_REJECT_KEYWORDS = [
    "opponent_city", "opponent_nickname",
]

# These can stay at Iloilo-game defaults as a starting point:
# REAL_TIME_FACTOR = 2.2
# QUARTER_BREAK_SECONDS = 190
# HALFTIME_BREAK_SECONDS = 700
# CLIP_LEAD_SECONDS = 12
# CLIP_TAIL_SECONDS = 8
# INCLUDE_FREETHROWS = False
# AUDIO_VERIFY = True
# WHISPER_MODEL = "base"
```
