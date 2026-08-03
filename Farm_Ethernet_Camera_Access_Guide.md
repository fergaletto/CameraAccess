# Farm Ethernet Camera Access and Image Capture Guide

## 1. Purpose

This guide explains how students can access the Ethernet cameras installed in the hydroponic farm and capture still images with Python and OpenCV.

The normal student workflow is:

1. Connect a computer to the farm network.
2. Select a camera by its logical name.
3. Open the camera's RTSP stream only when an image is required.
4. Decode a fresh frame.
5. Save the frame as a JPEG image.
6. Release the RTSP connection immediately.

> **Network-use rule:** Student applications must not leave a live RTSP stream open continuously. Use still-image capture for normal data collection. A maximum sampling rate of approximately one image per second may be used for a short, bounded experiment, but the application must stop and release the camera afterward.

The previous implementation used ROS 2 to keep camera streams open and publish images continuously. That architecture is useful for a dedicated farm service, but it is not appropriate when many student computers independently connect to the same camera. The examples below use standard Python and OpenCV without ROS.

---

## 2. Current Camera Address Map

| Logical camera name | Farm location | IPv4 address | Default RTSP port |
|---|---|---:|---:|
| `level1_camera1` | Level 1, Camera 1 | `192.168.1.106` | `554` |
| `level1_camera2` | Level 1, Camera 2 | `192.168.1.108` | `554` |

Use the naming format below when more cameras are added:

```text
level<farm-level>_camera<camera-number>
```

Examples include `level2_camera1` and `level2_camera2`. Add every new camera and its assigned IP address to the `CAMERAS` dictionary in the Python code. Do not assume an IP address merely by incrementing the last octet; use the address assigned by the farm administrator.

---

## 3. Network Requirements

A student computer must be able to route to the `192.168.1.0/24` farm network. In a typical setup, this means that the computer has an address such as `192.168.1.x` with subnet mask `255.255.255.0`, or it is connected through a router/VPN that provides access to that subnet.

### 3.1 Basic reachability test

Linux/macOS:

```bash
ping -c 4 192.168.1.106
ping -c 4 192.168.1.108
```

Windows PowerShell:

```powershell
ping 192.168.1.106
ping 192.168.1.108
```

A failed ping does not always prove the camera is offline because some devices block ICMP. Test the RTSP TCP port as well.

Linux/macOS, when `nc` is installed:

```bash
nc -vz 192.168.1.106 554
nc -vz 192.168.1.108 554
```

Windows PowerShell:

```powershell
Test-NetConnection -ComputerName 192.168.1.106 -Port 554
Test-NetConnection -ComputerName 192.168.1.108 -Port 554
```

Expected result: TCP port `554` is reachable. If it is not, check the network connection, IP address, firewall, camera configuration, and whether RTSP is enabled in the camera administration page.

---

## 4. RTSP, RTP and RTPS

The camera protocol is **RTSP**, not RTPS.

- **RTSP — Real Time Streaming Protocol:** establishes and controls a media session. It handles operations such as opening, playing and closing a camera stream. Port `554` is the conventional RTSP port.
- **RTP — Real-time Transport Protocol:** normally carries the encoded video packets after RTSP establishes the session. The video is commonly encoded as H.264 or H.265.
- **RTPS — Real-Time Publish-Subscribe:** a different protocol associated with DDS and commonly encountered underneath ROS 2 communication. It is not the protocol used to request the camera video in this guide.

An RTSP URL normally has this structure:

```text
rtsp://<username>:<password>@<camera-ip>:<port>/<stream-path>
```

For example, the logical structure for Camera 1 is:

```text
rtsp://USERNAME:PASSWORD@192.168.1.106:554/STREAM_PATH
```

The exact `STREAM_PATH` is camera-model-specific. Some cameras use a blank path while others require a main-stream or sub-stream path. The previous ROS example used port `554` and allowed an empty path, but the correct path must be confirmed from the current camera configuration or manufacturer documentation.

### 4.1 TCP versus UDP transport

RTSP can negotiate RTP media over UDP or interleave the media over the RTSP TCP connection.

This guide defaults to TCP because it is usually easier to operate through host firewalls and is less sensitive to packet loss on a busy network. UDP may provide slightly lower latency but can require additional dynamic ports and may show corruption when packets are lost.

OpenCV uses its FFmpeg backend to open and decode the RTSP stream. The examples set the FFmpeg option:

```text
rtsp_transport=tcp
```

---

## 5. Credentials and Security

Do not place the camera password directly in code committed to Git or shared publicly. Use environment variables instead.

Use a read-only camera account where the camera supports one. Students only need permission to view the stream; they do not need administrative permission.

Linux/macOS:

```bash
export FARM_CAMERA_USER='camera_viewer'
export FARM_CAMERA_PASSWORD='replace-with-the-assigned-password'
export FARM_CAMERA_RTSP_PATH='/'
```

Windows PowerShell:

```powershell
$env:FARM_CAMERA_USER='camera_viewer'
$env:FARM_CAMERA_PASSWORD='replace-with-the-assigned-password'
$env:FARM_CAMERA_RTSP_PATH='/'
```

Set `FARM_CAMERA_RTSP_PATH` to the actual path for this camera model. Use an empty value if the camera works without a path:

Linux/macOS:

```bash
export FARM_CAMERA_RTSP_PATH=''
```

Windows PowerShell:

```powershell
$env:FARM_CAMERA_RTSP_PATH=''
```

Security notes:

- Do not print the complete RTSP URL because it contains the password.
- Do not store credentials in screenshots, notebooks, reports or Git repositories.
- Do not expose camera port `554` directly to the public internet.
- Prefer a dedicated farm VLAN or protected laboratory network.
- Change factory-default passwords before providing student access.

---

## 6. Python and OpenCV Installation

Python 3.10 or newer is recommended.

Create a virtual environment:

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install opencv-python
```

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install opencv-python
```

Check that OpenCV was installed:

```bash
python -c "import cv2; print(cv2.__version__)"
```

For RTSP support, the OpenCV build must include FFmpeg. Check it with:

```bash
python -c "import cv2; print(cv2.getBuildInformation())"
```

Find the `Video I/O` section and confirm that `FFMPEG` is listed as `YES`.

> Use `opencv-python`, not `opencv-python-headless`, for the live-video test because the headless package does not provide GUI windows.

---

## 7. Recommended Code: Capture Still Images and Release the Camera

Save the following as `capture_farm_camera.py`.

```python
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
    username = os.getenv("FARM_CAMERA_USER", "")
    password = os.getenv("FARM_CAMERA_PASSWORD", "")
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
```

### 7.1 Capture one image from Camera 1

```bash
python capture_farm_camera.py level1_camera1
```

### 7.2 Capture one image from Camera 2

```bash
python capture_farm_camera.py level1_camera2
```

### 7.3 Capture one image from each camera

```bash
python capture_farm_camera.py all
```

### 7.4 Capture one image per second for a bounded test

The following requests 60 capture cycles with a target interval of one second:

```bash
python capture_farm_camera.py all --count 60 --interval 1
```

Important details:

- The interval is a target, not a guaranteed rate. Connection setup and decoding may take longer than one second.
- Each cycle opens the RTSP stream, obtains a fresh frame and closes it.
- Repeatedly opening a stream every second has connection overhead. Use this only for bounded data collection.
- Do not replace `--count 60` with an infinite loop.
- If many students need the same one-second images simultaneously, use the shared snapshot architecture described in Section 11 instead of having each student connect independently.

---

## 8. How the Still-Image Code Works

OpenCV cannot ask a generic RTSP camera for “just one JPEG.” RTSP is a stream protocol, so the code performs these operations:

1. `cv2.VideoCapture(...)` creates an RTSP session.
2. FFmpeg negotiates the encoded video stream.
3. `capture.read()` receives and decodes video frames.
4. A small number of initial frames are discarded to reduce the chance of saving a buffered or incomplete first frame.
5. `cv2.imwrite(...)` writes one decoded frame as a JPEG.
6. `capture.release()` closes the connection.

The student application uses a still image, even though a short RTSP video session is required internally to obtain that image.

### Why warm-up frames are used

The first decoded frame may be old, incomplete or delayed while the decoder waits for an H.264/H.265 keyframe. Reading several frames before saving usually produces a more recent and complete image. Increase `WARMUP_FRAMES` only when necessary because it increases connection duration and data use.

### Why `CAP_PROP_BUFFERSIZE` is set to one

A small buffer may reduce latency and stale frames. It is only a request to the backend; not every OpenCV/FFmpeg build or camera honours it.

---

## 9. Test-Only Code: Live Video from Both Cameras

> **WARNING — TESTING ONLY:** The following program keeps two RTSP streams open continuously until the user exits. Do not use it for normal student data collection. Do not leave it running unattended. Do not ask a class of students to run it simultaneously. Every additional client can cause the camera and network to transmit another complete stream, so bandwidth and camera connection usage grow approximately with the number of clients.

Save the following as `test_two_camera_live_video.py`.

```python
#!/usr/bin/env python3
"""TEST ONLY: display live video from both farm cameras.

This program holds both RTSP connections open. Press Q or Escape to exit.
Do not use it as a permanent monitoring application.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote

import cv2


@dataclass(frozen=True)
class Camera:
    name: str
    ip: str
    port: int = 554


CAMERAS = [
    Camera("Level 1 - Camera 1", "192.168.1.106"),
    Camera("Level 1 - Camera 2", "192.168.1.108"),
]

DISPLAY_HEIGHT = 480


def build_rtsp_url(camera: Camera) -> str:
    username = os.getenv("FARM_CAMERA_USER", "")
    password = os.getenv("FARM_CAMERA_PASSWORD", "")
    path = os.getenv("FARM_CAMERA_RTSP_PATH", "")

    if not username or not password:
        raise RuntimeError(
            "Set FARM_CAMERA_USER and FARM_CAMERA_PASSWORD before running."
        )

    if path and not path.startswith("/"):
        path = "/" + path

    return (
        f"rtsp://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{camera.ip}:{camera.port}{path}"
    )


def open_stream(camera: Camera) -> cv2.VideoCapture:
    url = build_rtsp_url(camera)
    capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    if not capture.isOpened():
        capture.release()
        raise ConnectionError(f"Could not open {camera.name} at {camera.ip}")

    return capture


def resize_to_height(frame, height: int):
    original_height, original_width = frame.shape[:2]
    scale = height / original_height
    width = max(1, int(original_width * scale))
    return cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)


def add_label(frame, text: str):
    cv2.putText(
        frame,
        text,
        (10, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return frame


def main() -> None:
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
        "rtsp_transport;tcp|stimeout;5000000"
    )

    captures: list[cv2.VideoCapture] = []

    try:
        print("TEST ONLY: opening two continuous RTSP streams.")
        print("Press Q or Escape to close both streams.")

        for camera in CAMERAS:
            captures.append(open_stream(camera))

        while True:
            displayed_frames = []

            for camera, capture in zip(CAMERAS, captures):
                ok, frame = capture.read()
                if not ok or frame is None:
                    raise RuntimeError(f"Frame read failed for {camera.name}")

                frame = resize_to_height(frame, DISPLAY_HEIGHT)
                displayed_frames.append(add_label(frame, camera.name))

            combined = cv2.hconcat(displayed_frames)
            cv2.imshow("Farm cameras - TEST ONLY", combined)

            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q"), ord("Q")):
                break

    finally:
        for capture in captures:
            capture.release()
        cv2.destroyAllWindows()
        print("All camera streams released.")


if __name__ == "__main__":
    main()
```

Run it with:

```bash
python test_two_camera_live_video.py
```

Exit using `Q` or the `Escape` key. Confirm in the terminal that the program prints `All camera streams released.`

### Live-video test limitations

- The camera normally sends its configured stream bitrate regardless of the display-window size.
- Resizing the image locally reduces screen and processing requirements but does not necessarily reduce network traffic.
- A camera sub-stream, when available, is preferable for testing because it can be configured for lower resolution and bitrate.
- Closing the OpenCV window using the window manager may not always trigger a clean shutdown on every platform; use `Q` or `Escape`.
- If the program crashes, the operating system will normally close the socket, but students should still use `try/finally` as shown.

---

## 10. Troubleshooting

### `Could not open ...`

Check:

1. The computer can reach the camera IP.
2. TCP port `554` is reachable.
3. RTSP is enabled in the camera settings.
4. The username and password are correct.
5. The RTSP path is correct.
6. No VPN or firewall is blocking the farm subnet.
7. The camera has not reached its maximum number of simultaneous clients.

### The stream opens but no frame is decoded

Possible causes include:

- The RTSP path refers to an unavailable stream.
- The camera uses a codec unsupported by the installed FFmpeg/OpenCV build.
- The decoder is waiting for a keyframe.
- The network is dropping packets.
- The camera is overloaded by too many clients.

Try the lower-resolution sub-stream, increase the read timeout modestly, and make sure other test clients are closed.

### Images are old or delayed

- Keep `CAP_PROP_BUFFERSIZE` at `1`.
- Use RTSP-over-TCP on a congested or lossy network.
- Read and discard several warm-up frames before saving.
- Do not leave a capture object open while only reading occasionally; buffered frames may accumulate depending on the backend.

### H.265 does not display

The installed OpenCV/FFmpeg build may not include the required decoder. Configure the camera to provide an H.264 sub-stream for student access, or install a compatible FFmpeg/OpenCV build according to university IT policy.

### Authentication fails when the password contains symbols

The example URL-encodes the username and password using `urllib.parse.quote`. Use the provided builder rather than manually concatenating credentials.

### Two cameras use different credentials or RTSP paths

Move `username`, `password` and `path` into the `Camera` configuration or use camera-specific environment variables. Do not duplicate passwords directly in the source code.

---

## 11. Recommended Architecture for a Whole Class

Direct student-to-camera access does not scale well. If 20 students independently open Camera 1, the camera may have to serve 20 sessions and the network may carry many copies of the same stream.

For class-scale use, deploy one trusted snapshot collector on the farm network:

1. The collector opens at most one RTSP connection per camera.
2. It keeps only the newest frame.
3. Once per second, it writes or serves a current JPEG such as `level1_camera1_latest.jpg`.
4. Students request that JPEG from a web endpoint or shared data service instead of opening RTSP directly.
5. The service applies authentication, rate limiting, timestamps and logging.

This changes the load from “one stream per student per camera” to “one stream per camera,” while still allowing all students to retrieve the latest image. It is the preferred long-term design for Leafy AI experiments, dashboards and computer-vision assignments.

If the camera itself provides an authenticated HTTP snapshot endpoint, that endpoint may also be preferable to opening RTSP for each still image. The exact endpoint is manufacturer-specific and must be confirmed before use.

---

## 12. Student Usage Rules

1. Use `capture_farm_camera.py` for normal work.
2. Capture only the camera required by the experiment.
3. Use one image per second only for short, bounded tests.
4. Do not create infinite polling loops.
5. Do not run the live-video example except during supervised testing.
6. Always call `release()` by using `try/finally` or a context-management pattern.
7. Do not share or commit camera credentials.
8. Report repeated connection failures instead of continuously reconnecting at high frequency.
9. Store the camera name and timestamp with collected data.
10. Use the shared snapshot service when it becomes available.

---

## 13. Suggested Image Metadata

A captured image should be accompanied by metadata similar to:

```json
{
  "camera_name": "level1_camera1",
  "camera_ip": "192.168.1.106",
  "captured_at": "2026-08-03T10:03:00+10:00",
  "source_protocol": "RTSP",
  "image_format": "JPEG",
  "experiment_id": "student-defined-experiment-id"
}
```

Use an ISO 8601 timestamp with timezone information. For synchronisation with sensor readings, the system clock on the capture computer should be synchronised using the university's approved time service.

---

## 14. Summary

- Camera 1: `192.168.1.106`
- Camera 2: `192.168.1.108`
- Default RTSP port: `554`
- Normal method: open, decode one fresh frame, save, release
- Maximum suggested short-term sampling rate: approximately `1 Hz`
- Continuous two-camera viewer: testing only
- Preferred class-scale solution: one shared snapshot collector per camera
