# Data

Video and model files are **not committed**. This file records what we used so anyone can rebuild the setup.

## Test video (experiment input)

| Field | Value |
|---|---|
| File name | `data/test_video.mp4` |
| Source URL | _TODO (Andres)_ |
| License | _TODO: must allow reuse (e.g. Pexels license / CC0)_ |
| Resolution / fps / length | _TODO_ |
| SHA-256 | _TODO: `shasum -a 256 data/test_video.mp4`_ |
| Why chosen | Street scene with many COCO objects (people, cars, bikes), roughly 2–3 min, looped |

Playback for trials: fullscreen on the client display, looped, same player and settings every trial
(record player and version in `docs/testbed.md`).

## Model

`yolo11n.pt` (Ultralytics, COCO-pretrained). Downloaded automatically into `models/` on first server start.
The exact weights path and library version are recorded in every trial's `metadata.json`.

## Dev-only files

`data/dev_clip.mp4` is a 5 s pan over Ultralytics' bundled `zidane.jpg`, used only for local smoke tests.
Never report results from it.
