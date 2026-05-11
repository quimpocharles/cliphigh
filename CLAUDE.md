# Claude Instructions — SiKAT Highlight Pipeline

## Read before doing anything

1. Read `RUNBOOK.md` in full — it contains confirmed calibration values,
   workflow decisions, lessons learned from real games, and a planned v2.
   Acting without reading it will repeat already-solved mistakes.

2. Read `config.py` — it shows the current game's state: which game is
   loaded, which anchors are confirmed, and current settings.

## Key facts

- All commands use `python3`, never `python`
- yt-dlp: standalone binary at `/usr/local/bin/yt-dlp` (not pip-installed)
- ffmpeg: static binary at `/usr/local/bin/ffmpeg`
- NumPy must be `<2` for Whisper compatibility: `pip3 install "numpy<2"`
- Output folders: `highlights/{league}/{team}/{opponent}/{quarter}/`
- Never `rm -rf clips/` or `rm -rf highlights/` — wipes all games

## Workflow per quarter (VOD/completed games)

Single command — dry-run + calibrate + generate happen in one session:
```
python3 vod_replay.py --quarters N [--skip-download]
```

Step-by-step flags (for re-running a specific phase only):
```
python3 vod_replay.py --dry-run --quarters N       # show timestamps only
python3 vod_replay.py --calibrate --quarters N     # calibrate only, no clips
python3 vod_replay.py --quarters N --skip-download # full flow, reuse VOD
```

## Starting a new game

```
python3 new_game.py
```

## Update RUNBOOK.md whenever something new is learned.
