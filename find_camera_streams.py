#!/usr/bin/env python3
"""
find_camera_streams.py

Find and verify RTSP streams on the farm's Ethernet cameras.

Default targets:
    192.168.1.106  Level 1 Camera 1
    192.168.1.108  Level 1 Camera 2

Discovery strategy, one camera at a time:
1. Check whether TCP port 554 (RTSP) is reachable.
2. If python-onvif-zeep is installed, query common ONVIF service ports and
   request the RTSP URI for every media profile.
3. If ONVIF does not return a usable URI, test a small, bounded list of common
   RTSP paths.
4. Verify each candidate briefly, report codec/resolution, and immediately
   close the connection.

The script does not keep streams open and does not scan the whole subnet.

Recommended installation:
    python -m pip install onvif-zeep opencv-python

For the most reliable stream verification, also install FFmpeg so that
"ffprobe" is available on PATH.

Credentials should normally be supplied through environment variables:

Linux/macOS:
    export CAMERA_USERNAME='admin'
    export CAMERA_PASSWORD='your-password'

Windows PowerShell:
    $env:CAMERA_USERNAME='admin'
    $env:CAMERA_PASSWORD='your-password'

Usage:
    python find_camera_streams.py
    python find_camera_streams.py 192.168.1.106
    python find_camera_streams.py --timeout 5 192.168.1.106 192.168.1.108
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from typing import Iterable, Optional
from urllib.parse import quote, urlsplit, urlunsplit


DEFAULT_HOSTS = ["192.168.1.106", "192.168.1.108"]
DEFAULT_ONVIF_PORTS = [80, 8000, 8080, 8899, 2020]

# A deliberately short list. This is not an exhaustive or aggressive scan.
COMMON_RTSP_PATHS = [
    ("Hikvision-style main", "/Streaming/Channels/101"),
    ("Hikvision-style sub", "/Streaming/Channels/102"),
    ("Dahua/Amcrest-style main", "/cam/realmonitor?channel=1&subtype=0"),
    ("Dahua/Amcrest-style sub", "/cam/realmonitor?channel=1&subtype=1"),
    ("Reolink-style main", "/h264Preview_01_main"),
    ("Reolink-style sub", "/h264Preview_01_sub"),
    ("Generic stream 1", "/stream1"),
    ("Generic stream 2", "/stream2"),
    ("Generic live main", "/live/main"),
    ("Generic live sub", "/live/sub"),
    ("Generic channel main", "/live/ch00_0"),
    ("Generic channel sub", "/live/ch00_1"),
    ("Generic main channel", "/11"),
    ("Generic sub channel", "/12"),
]


@dataclass
class StreamResult:
    host: str
    source: str
    profile: str
    url: str
    verified: bool
    codec: str = ""
    width: int = 0
    height: int = 0
    error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover and briefly verify RTSP streams on specified IP cameras."
    )
    parser.add_argument(
        "hosts",
        nargs="*",
        default=DEFAULT_HOSTS,
        help="Camera IP addresses. Defaults to 192.168.1.106 and 192.168.1.108.",
    )
    parser.add_argument(
        "--username",
        default=os.getenv("CAMERA_USERNAME", "admin"),
        help="Camera username. Default: CAMERA_USERNAME or 'admin'.",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("CAMERA_PASSWORD"),
        help=(
            "Camera password. Prefer CAMERA_PASSWORD instead because command-line "
            "arguments may be visible to other local users."
        ),
    )
    parser.add_argument(
        "--rtsp-port",
        type=int,
        default=554,
        help="RTSP TCP port. Default: 554.",
    )
    parser.add_argument(
        "--onvif-ports",
        default=",".join(str(p) for p in DEFAULT_ONVIF_PORTS),
        help="Comma-separated ONVIF service ports to try.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=4.0,
        help="Timeout in seconds for each brief connection attempt. Default: 4.",
    )
    parser.add_argument(
        "--transport",
        choices=["tcp", "udp"],
        default="tcp",
        help="RTSP transport used for verification. Default: tcp.",
    )
    parser.add_argument(
        "--skip-onvif",
        action="store_true",
        help="Skip ONVIF profile discovery.",
    )
    parser.add_argument(
        "--skip-common",
        action="store_true",
        help="Skip probing the bounded list of common RTSP paths.",
    )
    parser.add_argument(
        "--show-urls",
        action="store_true",
        help="Print complete URLs. By default credentials are masked.",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        help="Optionally save all results to a JSON file.",
    )
    return parser.parse_args()


def parse_ports(value: str) -> list[int]:
    ports: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        port = int(item)
        if not 1 <= port <= 65535:
            raise ValueError(f"Invalid port: {port}")
        ports.append(port)
    return ports


def tcp_port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def add_credentials_and_normalise_host(
    url: str,
    host: str,
    username: str,
    password: str,
) -> str:
    """Add URL-escaped credentials and repair unusable hostnames from cameras."""
    parsed = urlsplit(url)

    stream_host = parsed.hostname or host
    if stream_host in {"0.0.0.0", "127.0.0.1", "localhost"}:
        stream_host = host

    port = parsed.port
    safe_user = quote(username, safe="")
    safe_password = quote(password, safe="")
    auth = f"{safe_user}:{safe_password}@"

    netloc = f"{auth}{stream_host}"
    if port is not None:
        netloc += f":{port}"

    scheme = parsed.scheme or "rtsp"
    return urlunsplit((scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def build_rtsp_url(
    host: str,
    port: int,
    path: str,
    username: str,
    password: str,
) -> str:
    safe_user = quote(username, safe="")
    safe_password = quote(password, safe="")
    if not path.startswith("/"):
        path = "/" + path
    return f"rtsp://{safe_user}:{safe_password}@{host}:{port}{path}"


def masked_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.port is not None:
        host += f":{parsed.port}"
    netloc = f"***:***@{host}" if parsed.username is not None else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def verify_with_ffprobe(
    url: str,
    timeout: float,
    transport: str,
) -> tuple[bool, str, int, int, str]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False, "", 0, 0, "ffprobe is not installed"

    timeout_us = max(1, int(timeout * 1_000_000))
    command = [
        ffprobe,
        "-v",
        "error",
        "-rtsp_transport",
        transport,
        "-rw_timeout",
        str(timeout_us),
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height",
        "-of",
        "json",
        url,
    ]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout + 2.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "", 0, 0, f"ffprobe timed out after {timeout:g} s"
    except OSError as exc:
        return False, "", 0, 0, f"ffprobe could not run: {exc}"

    if completed.returncode != 0:
        message = completed.stderr.strip() or "ffprobe rejected the stream"
        return False, "", 0, 0, message.splitlines()[-1][:300]

    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams", [])
        if not streams:
            return False, "", 0, 0, "No video stream metadata returned"
        stream = streams[0]
        return (
            True,
            str(stream.get("codec_name", "")),
            int(stream.get("width", 0) or 0),
            int(stream.get("height", 0) or 0),
            "",
        )
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return False, "", 0, 0, f"Could not parse ffprobe output: {exc}"


def verify_with_opencv(
    url: str,
    timeout: float,
    transport: str,
) -> tuple[bool, str, int, int, str]:
    try:
        import cv2  # type: ignore
    except ImportError:
        return False, "", 0, 0, "OpenCV is not installed"

    # These are consumed by OpenCV's FFmpeg backend when supported.
    timeout_us = max(1, int(timeout * 1_000_000))
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
        f"rtsp_transport;{transport}|stimeout;{timeout_us}|rw_timeout;{timeout_us}"
    )

    cap = None
    try:
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)

        # Supported by newer OpenCV builds; harmlessly ignored otherwise.
        if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(timeout * 1000))
        if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(timeout * 1000))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if not cap.isOpened():
            return False, "", 0, 0, "OpenCV could not open the stream"

        # Discard a few frames because some cameras begin with stale/incomplete data.
        frame = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ok, candidate = cap.read()
            if ok and candidate is not None:
                frame = candidate
                break

        if frame is None:
            return False, "", 0, 0, "OpenCV opened the URL but did not receive a frame"

        height, width = frame.shape[:2]
        return True, "decoded by OpenCV", int(width), int(height), ""
    except Exception as exc:
        return False, "", 0, 0, f"OpenCV error: {exc}"
    finally:
        if cap is not None:
            cap.release()


def verify_stream(
    url: str,
    timeout: float,
    transport: str,
) -> tuple[bool, str, int, int, str]:
    # ffprobe is preferable because it reads stream metadata and terminates cleanly.
    if shutil.which("ffprobe"):
        return verify_with_ffprobe(url, timeout, transport)
    return verify_with_opencv(url, timeout, transport)


def encoder_details(profile: object) -> tuple[str, int, int]:
    name = str(getattr(profile, "Name", "") or getattr(profile, "name", "") or "")
    config = getattr(profile, "VideoEncoderConfiguration", None)
    resolution = getattr(config, "Resolution", None) if config is not None else None
    width = int(getattr(resolution, "Width", 0) or 0) if resolution is not None else 0
    height = int(getattr(resolution, "Height", 0) or 0) if resolution is not None else 0
    return name, width, height


def discover_onvif(
    host: str,
    ports: Iterable[int],
    username: str,
    password: str,
    timeout: float,
    transport: str,
    show_urls: bool,
) -> list[StreamResult]:
    try:
        from onvif import ONVIFCamera  # type: ignore
    except ImportError:
        print("  ONVIF: package not installed; skipping.")
        print("         Install with: python -m pip install onvif-zeep")
        return []

    results: list[StreamResult] = []

    for port in ports:
        if not tcp_port_open(host, port, min(timeout, 1.5)):
            continue

        print(f"  ONVIF: trying service port {port} ...")
        try:
            camera = ONVIFCamera(host, port, username, password)
            media = camera.create_media_service()
            profiles = media.GetProfiles()
        except Exception as exc:
            print(f"    No usable ONVIF media service on port {port}: {exc}")
            continue

        print(f"    ONVIF media service responded with {len(profiles)} profile(s).")

        for index, profile in enumerate(profiles, start=1):
            token = (
                getattr(profile, "token", None)
                or getattr(profile, "Token", None)
                or ""
            )
            profile_name, declared_width, declared_height = encoder_details(profile)
            label = profile_name or f"profile-{index}"

            if declared_width and declared_height:
                print(
                    f"    Profile {index}: {label!r}, token={token!r}, "
                    f"declared={declared_width}x{declared_height}"
                )
            else:
                print(f"    Profile {index}: {label!r}, token={token!r}")

            try:
                request = media.create_type("GetStreamUri")
                request.StreamSetup = {
                    "Stream": "RTP-Unicast",
                    "Transport": {"Protocol": "RTSP"},
                }
                request.ProfileToken = token
                response = media.GetStreamUri(request)
                raw_uri = str(response.Uri)
                uri = add_credentials_and_normalise_host(
                    raw_uri, host, username, password
                )
            except Exception as exc:
                results.append(
                    StreamResult(
                        host=host,
                        source=f"ONVIF port {port}",
                        profile=label,
                        url="",
                        verified=False,
                        width=declared_width,
                        height=declared_height,
                        error=f"GetStreamUri failed: {exc}",
                    )
                )
                print(f"      GetStreamUri failed: {exc}")
                continue

            shown = uri if show_urls else masked_url(uri)
            print(f"      URI: {shown}")
            ok, codec, width, height, error = verify_stream(
                uri, timeout, transport
            )

            result = StreamResult(
                host=host,
                source=f"ONVIF port {port}",
                profile=label,
                url=uri,
                verified=ok,
                codec=codec,
                width=width or declared_width,
                height=height or declared_height,
                error=error,
            )
            results.append(result)

            if ok:
                print(
                    f"      VERIFIED: {result.codec or 'video'}, "
                    f"{result.width}x{result.height}"
                )
            else:
                print(f"      Not verified: {error}")

        # A responding ONVIF media service is authoritative enough; do not try
        # the same camera on every remaining HTTP port.
        return results

    print("  ONVIF: no responding media service found on the selected ports.")
    return results


def probe_common_paths(
    host: str,
    rtsp_port: int,
    username: str,
    password: str,
    timeout: float,
    transport: str,
    show_urls: bool,
) -> list[StreamResult]:
    results: list[StreamResult] = []
    print("  Common-path probe: testing a small list sequentially ...")

    for label, path in COMMON_RTSP_PATHS:
        uri = build_rtsp_url(host, rtsp_port, path, username, password)
        shown = uri if show_urls else masked_url(uri)
        print(f"    {label}: {shown}")

        ok, codec, width, height, error = verify_stream(uri, timeout, transport)
        result = StreamResult(
            host=host,
            source="common-path probe",
            profile=label,
            url=uri,
            verified=ok,
            codec=codec,
            width=width,
            height=height,
            error=error,
        )
        results.append(result)

        if ok:
            print(f"      VERIFIED: {codec or 'video'}, {width}x{height}")
        else:
            print(f"      No stream: {error}")

    return results


def print_summary(results: list[StreamResult], show_urls: bool) -> None:
    verified = [result for result in results if result.verified]
    print("\n=== VERIFIED STREAMS ===")
    if not verified:
        print("No RTSP stream was verified.")
        return

    for result in verified:
        url = result.url if show_urls else masked_url(result.url)
        print(
            f"{result.host} | {result.profile} | "
            f"{result.codec or 'video'} | {result.width}x{result.height}"
        )
        print(f"  {url}")


def main() -> int:
    args = parse_args()

    try:
        onvif_ports = parse_ports(args.onvif_ports)
    except ValueError as exc:
        print(f"Argument error: {exc}", file=sys.stderr)
        return 2

    password = args.password
    if password is None:
        password = getpass.getpass(
            "Camera password (input will not be displayed): "
        )

    if not password:
        print("A camera password is required.", file=sys.stderr)
        return 2

    all_results: list[StreamResult] = []

    print(
        "This tool makes only brief, sequential connections and releases each "
        "stream immediately.\n"
    )

    for host in args.hosts:
        print(f"\n=== CAMERA {host} ===")
        if tcp_port_open(host, args.rtsp_port, min(args.timeout, 2.0)):
            print(f"  RTSP TCP port {args.rtsp_port}: reachable")
        else:
            print(
                f"  RTSP TCP port {args.rtsp_port}: not reachable. "
                "The port may be different, RTSP may be disabled, or a firewall "
                "may be blocking it."
            )

        host_results: list[StreamResult] = []

        if not args.skip_onvif:
            host_results.extend(
                discover_onvif(
                    host=host,
                    ports=onvif_ports,
                    username=args.username,
                    password=password,
                    timeout=args.timeout,
                    transport=args.transport,
                    show_urls=args.show_urls,
                )
            )

        has_verified_onvif = any(
            result.verified and result.source.startswith("ONVIF")
            for result in host_results
        )

        if not args.skip_common and not has_verified_onvif:
            host_results.extend(
                probe_common_paths(
                    host=host,
                    rtsp_port=args.rtsp_port,
                    username=args.username,
                    password=password,
                    timeout=args.timeout,
                    transport=args.transport,
                    show_urls=args.show_urls,
                )
            )
        elif has_verified_onvif:
            print(
                "  Common-path probing skipped because ONVIF returned at least "
                "one verified stream."
            )

        all_results.extend(host_results)

    print_summary(all_results, args.show_urls)

    if args.json_path:
        output_path = os.path.abspath(args.json_path)
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(
                [asdict(result) for result in all_results],
                handle,
                indent=2,
            )
        print(f"\nSaved results to: {output_path}")

    return 0 if any(result.verified for result in all_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
