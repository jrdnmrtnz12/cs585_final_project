"""Application-layer protocol shared by the client and the edge server.

Inference runs over HTTP/1.1 on one persistent TCP connection (Kurose & Ross
Ch. 2: persistent HTTP). The client POSTs one JPEG per request and gets back
one JSON response.

    POST /infer
      Content-Type: image/jpeg
      X-Frame-Id:   <int>       required, echoed back in the response
      X-Trial-Id:   <str>       optional, for server-side debugging
      X-Profile:    <str>       optional, the profile name the client used
      body:         raw JPEG bytes

    200 -> InferResponse JSON (below)
    400 -> {"detail": "..."} for a missing/invalid header or an undecodable JPEG

Bounding boxes are absolute PIXEL coordinates [x1, y1, x2, y2] in the image
the server RECEIVED (the profile's resolution, not the original frame).

The network monitor uses a separate UDP echo (Kurose & Ross Ch. 3: UDP has no
retransmission, so a missing echo is a lost packet):

    datagram = PROBE_STRUCT(seq, send_ns) + zero padding to PROBE_SIZE bytes
    the server sends the exact same bytes back
"""
from __future__ import annotations

import struct

from pydantic import BaseModel, Field

INFER_PATH = "/infer"
HEALTH_PATH = "/health"
STATS_PATH = "/stats"
STATS_RESET_PATH = "/stats/reset"

HDR_FRAME_ID = "X-Frame-Id"
HDR_TRIAL_ID = "X-Trial-Id"
HDR_PROFILE = "X-Profile"
JPEG_CONTENT_TYPE = "image/jpeg"

PROBE_STRUCT = struct.Struct("!IQ")  # uint32 sequence number, uint64 client send time (ns)
PROBE_SIZE = 64                       # bytes of UDP payload per probe


class Detection(BaseModel):
    class_id: int
    label: str
    conf: float
    bbox_xyxy_px: list[float] = Field(min_length=4, max_length=4)


class Timing(BaseModel):
    decode_ms: float      # JPEG decode on the server
    infer_ms: float       # model forward pass + post-processing
    server_total_ms: float  # body fully received -> response object built (excludes upload time)


class InferResponse(BaseModel):
    frame_id: int
    image_wh: list[int] = Field(min_length=2, max_length=2)
    detections: list[Detection]
    timing: Timing
    mock: bool            # True if produced by MockDetector: NEVER use for accuracy


class HealthResponse(BaseModel):
    status: str
    detector: str
    mock: bool
    device: str
    imgsz: int
    conf: float
    weights: str | None
    library_version: str | None


def pack_probe(seq: int, send_ns: int) -> bytes:
    header = PROBE_STRUCT.pack(seq & 0xFFFFFFFF, send_ns)
    return header + bytes(PROBE_SIZE - len(header))


def unpack_probe(data: bytes) -> tuple[int, int]:
    """Return (seq, send_ns). Raises struct.error on a short datagram."""
    return PROBE_STRUCT.unpack_from(data)
