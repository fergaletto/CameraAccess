#!/usr/bin/env python3
"""Capture still images from the farm RTSP cameras.

Normal use captures one image and exits. The camera connection is released after
that image. A bounded polling mode is included for experiments that need images
at a fixed interval. Do not run polling indefinitely.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import cv2


@dataclass(frozen=True)
class Camera:
    name: str
    ip: str
    port: int = 554


CAMERAS: dict[str, Camera] = {
    "level1_camera1": Camera("level1_camera1", "192.168.1.106"),
    "level1_camera2": Camera("level1_camera2", "192.168.1.108"),
    # Add future cameras here, for example:
    # "level2_camera1": Camera("level2_camera1", "192.168.1.xxx"),
}

OPEN_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 5.0
WARMUP_FRAMES = 3
JPEG_QUALITY = 90


def build_rtsp_url(camera: Camera) -> str:
    """Build the RTSP URL without logging or displaying the password."""
    username = "admin"
    password = "123456"
    path = os.getenv("FARM_CAMERA_RTSP_PATH", "")

    if not username or not password:
        raise RuntimeError(
            "Set FARM_CAMERA_USER and FARM_CAMERA_PASSWORD before running."
        )

    # Encode credentials so characters such as @, : and / do not break the URL.
    encoded_user = quote(username, safe="")
    encoded_password = quote(password, safe="")

    if path and not path.startswith("/"):
        path = "/" + path

    return (
        f"rtsp://{encoded_user}:{encoded_password}"
        f"@{camera.ip}:{camera.port}{path}"
    )


def open_camera(camera: Camera) -> cv2.VideoCapture:
    """Open one RTSP camera using OpenCV's FFmpeg backend."""
    # These options must be set before VideoCapture is created.
    # stimeout is expressed in microseconds in common FFmpeg builds.
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
        "rtsp_transport;tcp"
        f"|stimeout;{int(OPEN_TIMEOUT_SECONDS * 1_000_000)}"
    )

    url = build_rtsp_url(camera)
    capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)

    # A buffer size of one is a best-effort request. Some backends ignore it.
    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    if not capture.isOpened():
        capture.release()
        raise ConnectionError(
            f"Could not open {camera.name} at {camera.ip}:{camera.port}. "
            "Check network access, credentials, RTSP path and camera settings."
        )

    return capture


def read_fresh_frame(capture: cv2.VideoCapture) -> "cv2.typing.MatLike":
    """Read several frames and return a recently decoded frame."""
    deadline = time.monotonic() + READ_TIMEOUT_SECONDS
    latest_frame = None
    successful_frames = 0

    while time.monotonic() < deadline:
        ok, frame = capture.read()
        if ok and frame is not None:
            latest_frame = frame
            successful_frames += 1
            if successful_frames >= WARMUP_FRAMES:
                return latest_frame
        else:
            time.sleep(0.05)

    if latest_frame is not None:
        return latest_frame

    raise TimeoutError("The RTSP connection opened, but no frame was decoded.")


def capture_one_image(camera: Camera, output_directory: Path) -> Path:
    """Open, capture, save and release one camera image."""
    capture: cv2.VideoCapture | None = None

    try:
        print(f"Connecting to {camera.name} ({camera.ip}) ...")
        capture = open_camera(camera)
        frame = read_fresh_frame(capture)

        output_directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
        output_path = output_directory / f"{camera.name}_{timestamp}.jpg"

        parameters = [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
        saved = cv2.imwrite(str(output_path), frame, parameters)
        if not saved:
            raise OSError(f"OpenCV could not write {output_path}")

        print(f"Saved {output_path}")
        return output_path

    finally:
        if capture is not None:
            capture.release()
        print(f"Released {camera.name}")


def selected_cameras(selection: str) -> list[Camera]:
    if selection == "all":
        return list(CAMERAS.values())
    return [CAMERAS[selection]]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture still images from the farm RTSP cameras."
    )
    parser.add_argument(
        "camera",
        choices=[*CAMERAS.keys(), "all"],
        help="Camera name, or 'all' to capture each configured camera.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("camera_images"),
        help="Output directory. Default: camera_images",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="Number of capture cycles. Default: 1",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Target seconds between cycles when count is greater than 1.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()

    if args.count < 1:
        print("--count must be at least 1", file=sys.stderr)
        return 2
    if args.interval < 1.0 and args.count > 1:
        print(
            "For shared-camera use, --interval must be at least 1.0 second.",
            file=sys.stderr,
        )
        return 2

    cameras = selected_cameras(args.camera)

    for cycle in range(args.count):
        cycle_start = time.monotonic()
        print(f"Capture cycle {cycle + 1}/{args.count}")

        for camera in cameras:
            try:
                capture_one_image(camera, args.output)
            except Exception as error:
                # Continue so a failure on one camera does not prevent testing another.
                print(f"ERROR: {camera.name}: {error}", file=sys.stderr)

        if cycle + 1 < args.count:
            elapsed = time.monotonic() - cycle_start
            time.sleep(max(0.0, args.interval - elapsed))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())