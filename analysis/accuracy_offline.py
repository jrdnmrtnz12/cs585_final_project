"""Per-profile detection quality, measured offline and deterministically.

    python analysis/accuracy_offline.py --video data/test_video.mp4 --every-n 15
    python analysis/accuracy_offline.py --images path/to/frames/        # any folder of images

For each sampled frame:
  reference = YOLO on the UNCOMPRESSED frame at its native resolution
  for each profile: crop/resize + JPEG exactly as the client does (edge/client/encoder.py)
                    -> YOLO -> boxes scaled back to the reference frame's coordinates
                    -> greedy match to reference boxes (same class, IoU >= --iou)

Output (results/accuracy_by_profile.csv), one row per profile:
  precision, recall, f1, tp/fp/fn, mean_jpeg_bytes, bitrate_mbps_at_fps
The bitrate column (mean bytes * 8 * profile fps) is what the scenario
rate caps are calibrated from (docs/spec.md, Part 1 #4).

IMPORTANT: this is AGREEMENT WITH A FULL-RESOLUTION REFERENCE produced by the same
model, NOT accuracy against human ground truth. Report it that way.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import cv2
import numpy as np

from edge.client.encoder import crop_to_aspect, encode_frame
from edge.config import REPO_ROOT, load_profiles
from edge.server.detector import Det, YoloDetector


def iou(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def match(ref: list[Det], pred: list[Det], thr: float) -> tuple[int, int, int]:
    """Greedy one-to-one matching, highest-confidence predictions first. Returns (tp, fp, fn)."""
    used: set[int] = set()
    tp = 0
    for p in sorted(pred, key=lambda d: -d.conf):
        best, best_i = thr, -1
        for i, r in enumerate(ref):
            if i in used or r.class_id != p.class_id:
                continue
            v = iou(p.bbox_xyxy_px, r.bbox_xyxy_px)
            if v >= best:
                best, best_i = v, i
        if best_i >= 0:
            used.add(best_i)
            tp += 1
    return tp, len(pred) - tp, len(ref) - tp


def frames_from_video(path: str, every_n: int, limit: int | None):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise FileNotFoundError(path)
    i = 0
    n = 0
    while True:
        ok, frame = cap.read()
        if not ok or (limit and n >= limit):
            break
        if i % every_n == 0:
            yield frame
            n += 1
        i += 1
    cap.release()


def frames_from_dir(path: str, limit: int | None):
    files = sorted(p for p in Path(path).iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp"))
    for p in files[:limit]:
        img = cv2.imread(str(p))
        if img is not None:
            yield img


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--video")
    src.add_argument("--images")
    ap.add_argument("--every-n", type=int, default=15)
    ap.add_argument("--limit", type=int, help="max frames")
    ap.add_argument("--weights", default="yolo11n.pt")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--iou", type=float, default=0.5)
    ap.add_argument("--out", default=str(REPO_ROOT / "results" / "accuracy_by_profile.csv"))
    args = ap.parse_args(argv)

    profiles, order = load_profiles()
    det = YoloDetector(args.weights, device=args.device, conf=args.conf)
    stats = {p: {"tp": 0, "fp": 0, "fn": 0, "bytes": 0} for p in order}
    n_frames = n_ref = 0
    frames = frames_from_video(args.video, args.every_n, args.limit) if args.video \
        else frames_from_dir(args.images, args.limit)

    for frame in frames:
        ref_frame = crop_to_aspect(frame, 16, 9)  # same crop the encoder applies
        rh, rw = ref_frame.shape[:2]
        ref = det.detect(ref_frame)
        n_frames += 1
        n_ref += len(ref)
        for name in order:
            p = profiles[name]
            enc = encode_frame(frame, p.width, p.height, p.jpeg_quality)
            img = cv2.imdecode(np.frombuffer(enc.data, np.uint8), cv2.IMREAD_COLOR)
            sx, sy = rw / p.width, rh / p.height
            pred = [Det(d.class_id, d.label, d.conf,
                        (d.bbox_xyxy_px[0] * sx, d.bbox_xyxy_px[1] * sy, d.bbox_xyxy_px[2] * sx, d.bbox_xyxy_px[3] * sy))
                    for d in det.detect(img)]
            tp, fp, fn = match(ref, pred, args.iou)
            s = stats[name]
            s["tp"] += tp
            s["fp"] += fp
            s["fn"] += fn
            s["bytes"] += len(enc.data)

    if n_frames == 0:
        print("no frames read")
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["profile", "n_frames", "ref_boxes", "tp", "fp", "fn", "precision", "recall", "f1",
                    "mean_jpeg_bytes", "bitrate_mbps_at_fps", "metric_note"])
        for name in order:
            s, p = stats[name], profiles[name]
            prec = s["tp"] / (s["tp"] + s["fp"]) if s["tp"] + s["fp"] else float("nan")
            rec = s["tp"] / (s["tp"] + s["fn"]) if s["tp"] + s["fn"] else float("nan")
            f1 = 2 * prec * rec / (prec + rec) if prec + rec else float("nan")
            mean_b = s["bytes"] / n_frames
            w.writerow([name, n_frames, n_ref, s["tp"], s["fp"], s["fn"], f"{prec:.4f}", f"{rec:.4f}", f"{f1:.4f}",
                        f"{mean_b:.0f}", f"{mean_b * 8 * p.fps / 1e6:.3f}", "agreement_with_fullres_reference"])
            print(f"{name:7s} P={prec:.3f} R={rec:.3f} F1={f1:.3f}  mean {mean_b / 1e3:.1f} kB"
                  f"  -> {mean_b * 8 * p.fps / 1e6:.2f} Mbit/s at {p.fps:g} fps")
    print(f"{n_frames} frames, {n_ref} reference boxes -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
