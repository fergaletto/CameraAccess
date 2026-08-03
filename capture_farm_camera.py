#!/usr/bin/env python3
"""Capture still images from the farm RTSP cameras.

Default behaviour:
- Connect to the selected camera's main RTSP stream (/stream1).
- Decode a few frames so the saved image is fresh.
- Save one full-resolution image.
- Release the camera connection and exit.

A bounded polling mode is available through --count and --interval. Do not use
this program as an indefinite video client. Every capture cycle opens the
selected stream only long enough to obtain an image and then releases it.

Farm camera streams discovered during testing:
- /stream1: main stream, normally 2880 x 1616
- /stream2: secondary stream, normally 704 x 576
"""

from __future__ import annotations

import argparse
import getpass
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
    """Network configuration for one farm camera."""

    name: str
    ip: str
    port: int = 554


@dataclass(frozen=True)
class StreamProfile:
    """Known RTSP stream path and expected decoded resolution."""

    name: str
    path: str
    expected_width: int | None = None
    expected_height: int | None = None


CAMERAS: dict[str, Camera] = {
    "level1_camera1": Camera("level1_camera1", "192.168.1.106"),
    "level1_camera2": Camera("level1_camera2", "192.168.1.108"),
    # Add future cameras here, for example:
    # "level2_camera1": Camera("level2_camera1", "192.168.1.110"),
}

STREAM_PROFILES: dict[str, StreamProfile] = {
    "main": StreamProfile(
        name="main",
        path="/stream1",
        expected_width=2880,
        expected_height=1616,
    ),
    "secondary": StreamProfile(
        name="secondary",
        path="/stream2",
        expected_width=704,
        expected_height=576,
    ),
}

OPEN_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 8.0
WARMUP_FRAMES = 3
JPEG_QUALITY = 90
MINIMUM_POLL_INTERVAL_SECONDS = 1.0


def get_credentials() -> tuple[str, str]:
    """Read credentials without placing the password in the source code.

    FARM_CAMERA_USER defaults to "admin".
    FARM_CAMERA_PASSWORD is preferred. If it is absent, the program prompts
    securely and does not display the typed password.
    """
    username = os.getenv("FARM_CAMERA_USER", "admin").strip()
    password = os.getenv("FARM_CAMERA_PASSWORD")

    if not username:
        raise RuntimeError("FARM_CAMERA_USER must not be empty.")

    if password is None:
        password = getpass.getpass(
            "Camera password (input will not be displayed): "
        )

    if not password:
        raise RuntimeError(
            "No camera password was supplied. Set FARM_CAMERA_PASSWORD "
            "or enter it when prompted."
        )

    return username, password


def normalise_rtsp_path(path: str) -> str:
    """Return an RTSP path beginning with a slash."""
    path = path.strip()

    if not path:
        raise ValueError("The RTSP path must not be empty.")

    if not path.startswith("/"):
        path = "/" + path

    return path


def build_rtsp_url(
    camera: Camera,
    username: str,
    password: str,
    path: str,
) -> str:
    """Build the RTSP URL without logging or displaying the password."""
    encoded_user = quote(username, safe="")
    encoded_password = quote(password, safe="")
    normalised_path = normalise_rtsp_path(path)

    return (
        f"rtsp://{encoded_user}:{encoded_password}"
        f"@{camera.ip}:{camera.port}{normalised_path}"
    )


def configure_ffmpeg_timeouts() -> None:
    """Configure OpenCV's FFmpeg RTSP transport before opening a stream."""
    open_timeout_us = int(OPEN_TIMEOUT_SECONDS * 1_000_000)
    read_timeout_us = int(READ_TIMEOUT_SECONDS * 1_000_000)

    # TCP is preferred on the farm LAN because it avoids incomplete frames
    # caused by lost UDP packets. These options are interpreted by OpenCV's
    # FFmpeg backend when supported by the installed build.
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
        "rtsp_transport;tcp"
        f"|stimeout;{open_timeout_us}"
        f"|rw_timeout;{read_timeout_us}"
    )


def create_video_capture(url: str) -> cv2.VideoCapture:
    """Create a VideoCapture with timeout properties where supported."""
    parameters: list[int] = []

    if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
        parameters.extend(
            [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
                int(OPEN_TIMEOUT_SECONDS * 1000),
            ]
        )

    if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
        parameters.extend(
            [
                cv2.CAP_PROP_READ_TIMEOUT_MSEC,
                int(READ_TIMEOUT_SECONDS * 1000),
            ]
        )

    # The three-argument constructor is supported by current OpenCV releases.
    # Fall back to the older constructor for installations that do not expose it.
    if parameters:
        try:
            return cv2.VideoCapture(url, cv2.CAP_FFMPEG, parameters)
        except (TypeError, cv2.error):
            pass

    return cv2.VideoCapture(url, cv2.CAP_FFMPEG)


def open_camera(
    camera: Camera,
    username: str,
    password: str,
    stream_path: str,
) -> cv2.VideoCapture:
    """Open one RTSP camera using OpenCV's FFmpeg backend."""
    configure_ffmpeg_timeouts()

    url = build_rtsp_url(camera, username, password, stream_path)
    capture = create_video_capture(url)

    # A buffer size of one is a best-effort request. Some FFmpeg/OpenCV builds
    # ignore this property for RTSP inputs.
    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    if not capture.isOpened():
        capture.release()
        raise ConnectionError(
            f"Could not open {camera.name} at {camera.ip}:{camera.port}"
            f"{normalise_rtsp_path(stream_path)}. Check network access, "
            "credentials, RTSP settings and stream path."
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

    raise TimeoutError(
        "The RTSP connection opened, but no complete frame was decoded."
    )


def report_resolution(
    camera: Camera,
    profile: StreamProfile,
    frame: "cv2.typing.MatLike",
) -> None:
    """Print the decoded resolution and warn when it differs from expectations."""
    height, width = frame.shape[:2]

    print(
        f"Received {width} x {height} from "
        f"{camera.name} using {profile.path}"
    )

    if (
        profile.expected_width is not None
        and profile.expected_height is not None
        and (
            width != profile.expected_width
            or height != profile.expected_height
        )
    ):
        print(
            "WARNING: The decoded resolution differs from the resolution "
            f"previously observed for the {profile.name} stream "
            f"({profile.expected_width} x {profile.expected_height}).",
            file=sys.stderr,
        )


def capture_one_image(
    camera: Camera,
    profile: StreamProfile,
    output_directory: Path,
    username: str,
    password: str,
) -> Path:
    """Open, capture, save and release one camera image."""
    capture: cv2.VideoCapture | None = None

    try:
        print(
            f"Connecting to {camera.name} ({camera.ip}) "
            f"using {profile.name} stream {profile.path} ..."
        )

        capture = open_camera(
            camera=camera,
            username=username,
            password=password,
            stream_path=profile.path,
        )

        frame = read_fresh_frame(capture)
        report_resolution(camera, profile, frame)

        output_directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().strftime(
            "%Y%m%d_%H%M%S_%f"
        )

        output_path = output_directory / (
            f"{camera.name}_{profile.name}_{timestamp}.jpg"
        )

        parameters = [
            cv2.IMWRITE_JPEG_QUALITY,
            JPEG_QUALITY,
        ]

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
    """Resolve one camera name or all configured cameras."""
    if selection == "all":
        return list(CAMERAS.values())

    return [CAMERAS[selection]]


def selected_stream_profile(
    stream_name: str,
    path_override: str | None,
) -> StreamProfile:
    """Resolve the known profile or create one for a custom RTSP path."""
    if path_override is None:
        return STREAM_PROFILES[stream_name]

    return StreamProfile(
        name="custom",
        path=normalise_rtsp_path(path_override),
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture still images from the farm RTSP cameras. "
            "The main stream (/stream1) is used by default."
        )
    )

    parser.add_argument(
        "camera",
        choices=[*CAMERAS.keys(), "all"],
        help="Camera name, or 'all' to capture each configured camera.",
    )

    parser.add_argument(
        "--stream",
        choices=STREAM_PROFILES.keys(),
        default="main",
        help=(
            "Known stream profile. Default: main. "
            "main=/stream1; secondary=/stream2"
        ),
    )

    parser.add_argument(
        "--path",
        help=(
            "Override the RTSP path, for example /stream1. "
            "When supplied, this takes precedence over --stream."
        ),
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
        help=(
            "Target seconds between cycle starts when --count is greater "
            "than 1. Minimum: 1.0 second."
        ),
    )

    return parser.parse_args()


def main() -> int:
    args = parse_arguments()

    if args.count < 1:
        print("--count must be at least 1.", file=sys.stderr)
        return 2

    if (
        args.count > 1
        and args.interval < MINIMUM_POLL_INTERVAL_SECONDS
    ):
        print(
            "For shared-camera use, --interval must be at least "
            f"{MINIMUM_POLL_INTERVAL_SECONDS:.1f} second.",
            file=sys.stderr,
        )
        return 2

    try:
        username, password = get_credentials()
        profile = selected_stream_profile(args.stream, args.path)
    except (RuntimeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    cameras = selected_cameras(args.camera)
    failed_captures = 0

    if args.count > 1:
        print(
            "NOTICE: Polling is bounded by --count. Do not leave this process "
            "running continuously on shared cameras."
        )

    for cycle in range(args.count):
        cycle_start = time.monotonic()
        print(f"\nCapture cycle {cycle + 1}/{args.count}")

        for camera in cameras:
            try:
                capture_one_image(
                    camera=camera,
                    profile=profile,
                    output_directory=args.output,
                    username=username,
                    password=password,
                )
            except Exception as error:
                failed_captures += 1
                # Continue so a failure on one camera does not prevent the
                # other configured camera from being tested.
                print(
                    f"ERROR: {camera.name}: {error}",
                    file=sys.stderr,
                )

        if cycle + 1 < args.count:
            elapsed = time.monotonic() - cycle_start
            remaining = args.interval - elapsed

            if remaining > 0:
                time.sleep(remaining)
            else:
                print(
                    f"WARNING: This cycle took {elapsed:.2f} seconds, "
                    f"which exceeds the requested {args.interval:.2f}-second "
                    "interval. The next cycle will start immediately.",
                    file=sys.stderr,
                )

    if failed_captures:
        print(
            f"\nCompleted with {failed_captures} failed capture(s).",
            file=sys.stderr,
        )
        return 1

    print("\nAll requested captures completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
