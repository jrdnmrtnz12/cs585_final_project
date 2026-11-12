"""Start the edge server.

    python -m edge.server --detector mock               # pipeline testing, no model
    python -m edge.server --detector yolo               # real detection (MPS/CUDA/CPU auto)
    python -m edge.server --detector yolo --device cpu --weights yolo11n.pt
"""
from __future__ import annotations

import argparse
import logging

import uvicorn

from edge.config import load_experiment
from edge.server.app import create_app
from edge.server.detector import MockDetector, YoloDetector


def main(argv: list[str] | None = None) -> None:
    exp = load_experiment()
    ap = argparse.ArgumentParser(description="CS585 edge inference server")
    ap.add_argument("--host", default="0.0.0.0", help="interface to bind (default: all)")
    ap.add_argument("--port", type=int, default=exp.http_port)
    ap.add_argument("--udp-port", type=int, default=exp.udp_port)
    ap.add_argument("--detector", choices=["mock", "yolo"], required=True)
    ap.add_argument("--weights", default="yolo11n.pt")
    ap.add_argument("--device", default="auto", help="auto | mps | cpu | cuda:0")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--mock-delay-ms", type=float, default=15.0)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.detector == "mock":
        detector = MockDetector(delay_ms=args.mock_delay_ms, imgsz=args.imgsz, conf=args.conf)
    else:
        detector = YoloDetector(weights=args.weights, device=args.device, imgsz=args.imgsz, conf=args.conf)
    logging.info("detector=%s device=%s imgsz=%d conf=%.2f mock=%s",
                 detector.name, detector.device, detector.imgsz, detector.conf, detector.is_mock)

    app = create_app(detector, udp_port=args.udp_port, udp_host=args.host)
    # One worker so the model is loaded exactly once; access log off to keep per-request overhead low.
    uvicorn.run(app, host=args.host, port=args.port, workers=1, access_log=False, log_level="info")


if __name__ == "__main__":
    main()
