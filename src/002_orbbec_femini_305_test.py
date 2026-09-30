#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TARGET_DEVICE_HINT = "Gemini 305"
DEFAULT_FPS = 30
DEFAULT_SENSOR = "stereo_rgb"
DUAL_COLOR_WORK_MODE = "Dual Color Streams"

COLOR_FORMAT_PRIORITY: dict[str, int] = {
    "RGB": 8,
    "BGR": 7,
    "RGBA": 6,
    "BGRA": 5,
    "MJPG": 4,
    "YUYV": 3,
    "UYVY": 2,
    "Y8": 1,
    "Y16": 0,
}

SENSOR_ENUM_CANDIDATES: dict[str, tuple[str, ...]] = {
    "color": ("COLOR_SENSOR",),
    "left_color": ("LEFT_COLOR_SENSOR",),
    "right_color": ("RIGHT_COLOR_SENSOR",),
    "ir": ("IR_SENSOR",),
    "left_ir": ("LEFT_IR_SENSOR", "IR_LEFT_SENSOR"),
    "right_ir": ("RIGHT_IR_SENSOR", "IR_RIGHT_SENSOR"),
    "depth": ("DEPTH_SENSOR",),
}

FRAME_GETTER_CANDIDATES: dict[str, tuple[str, ...]] = {
    "color": ("get_color_frame",),
    "left_color": ("get_left_color_frame",),
    "right_color": ("get_right_color_frame",),
    "ir": ("get_ir_frame",),
    "left_ir": ("get_left_ir_frame", "get_ir_frame"),
    "right_ir": ("get_right_ir_frame", "get_ir_frame"),
    "depth": ("get_depth_frame",),
}


@dataclass(frozen=True)
class ProfileInfo:
    index: int
    width: int
    height: int
    fps: int
    format_name: str
    profile: Any

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def label(self) -> str:
        return f"{self.width}x{self.height}@{self.fps} {self.format_name}"


@dataclass(frozen=True)
class TestResult:
    ok: bool
    profile: ProfileInfo
    frames: int = 0
    measured_fps: float = 0.0
    actual_width: int | None = None
    actual_height: int | None = None
    error: str = ""


@dataclass(frozen=True)
class StereoProfileInfo:
    left: ProfileInfo
    right: ProfileInfo

    @property
    def pixels(self) -> int:
        return self.left.pixels

    def label(self) -> str:
        return self.left.label()


@dataclass(frozen=True)
class StereoTestResult:
    ok: bool
    profile: StereoProfileInfo
    frame_pairs: int = 0
    measured_fps: float = 0.0
    error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            f"Open an Orbbec {TARGET_DEVICE_HINT} and find the largest single-stream "
            "or paired-camera resolution that can actually start at the requested FPS."
        )
    )
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS, help="FPS to test.")
    parser.add_argument(
        "--sensor",
        choices=("stereo_rgb", "stereo_ir", *SENSOR_ENUM_CANDIDATES.keys()),
        default=DEFAULT_SENSOR,
        help=(
            "Stream to test. Default: stereo_rgb, which switches one Gemini 305 "
            "to Dual Color Streams mode and opens both color lenses. "
            "Use stereo_ir to open the left and right infrared cameras together."
        ),
    )
    parser.add_argument("--serial", type=str, default=None, help="Optional Orbbec serial number.")
    parser.add_argument(
        "--seconds",
        type=float,
        default=2.0,
        help="Seconds to capture for each candidate profile.",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=1000,
        help="wait_for_frames timeout in milliseconds.",
    )
    parser.add_argument(
        "--try-all",
        action="store_true",
        help="Do not stop at the first successful max-resolution profile.",
    )
    parser.add_argument(
        "--preview",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Open a live OpenCV window with the best verified profile (default: on). "
            "Use --no-preview for a non-interactive test."
        ),
    )
    parser.add_argument(
        "--preview-seconds",
        type=float,
        default=0.0,
        help=(
            "Automatically close the preview after this many seconds. "
            "Default: 0 (keep running until q/Esc or the window is closed)."
        ),
    )
    parser.add_argument(
        "--window-width",
        type=int,
        default=1280,
        help="Initial OpenCV preview-window width. Default: 1280.",
    )
    return parser.parse_args()


def import_orbbec_sdk() -> Any:
    try:
        import pyorbbecsdk as ob  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "pyorbbecsdk is not installed in this Python environment.\n"
            "Install the official Python SDK first, for example:\n"
            "  python -m pip install --upgrade pyorbbecsdk2\n"
            "Then run this script again."
        ) from exc
    return ob


def orbbec_udev_install_hint(ob: Any) -> str:
    module_file = getattr(ob, "__file__", None)
    if not module_file:
        return (
            "Install Orbbec udev rules, reload udev, then replug the camera."
        )
    script = Path(module_file).resolve().parent / "shared" / "install_udev_rules.sh"
    if script.is_file():
        return (
            "Install Orbbec udev rules, then replug the camera:\n"
            f"  sudo bash {script}\n"
            "  # unplug/replug Gemini 305 after the command finishes"
        )
    return "Install Orbbec udev rules, reload udev, then replug the camera."


def enum_name(value: Any) -> str:
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    text = str(value)
    return text.split(".")[-1].split(":")[0].strip("<> ")


def resolve_sensor_type(ob: Any, sensor_key: str) -> Any:
    sensor_enum = getattr(ob, "OBSensorType")
    for name in SENSOR_ENUM_CANDIDATES[sensor_key]:
        if hasattr(sensor_enum, name):
            return getattr(sensor_enum, name)
    available = ", ".join(name for name in dir(sensor_enum) if name.isupper())
    raise RuntimeError(f"SDK does not expose sensor '{sensor_key}'. Available sensors: {available}")


def make_pipeline(ob: Any, device: Any | None) -> Any:
    if device is None:
        return ob.Pipeline()
    try:
        return ob.Pipeline(device)
    except TypeError:
        return ob.Pipeline()


def safe_call(obj: Any, method: str, *args: Any) -> Any | None:
    func = getattr(obj, method, None)
    if func is None:
        return None
    try:
        return func(*args)
    except Exception:
        return None


def select_device(ob: Any, serial: str | None) -> Any | None:
    if not hasattr(ob, "Context"):
        print("[device] SDK has no Context class; using Pipeline() default device.")
        return None

    context = ob.Context()
    devices = context.query_devices()
    count = int(devices.get_count())
    if count <= 0:
        raise RuntimeError("No Orbbec device found.")

    print(f"[device] found {count} Orbbec device(s)")
    for idx in range(count):
        name = safe_call(devices, "get_device_name_by_index", idx)
        sn = safe_call(devices, "get_device_serial_number_by_index", idx)
        uid = safe_call(devices, "get_device_uid_by_index", idx)
        conn = safe_call(devices, "get_device_connection_type_by_index", idx)
        print(f"  [{idx}] name={name} serial={sn} uid={uid} connection={conn}")

    if serial:
        return devices.get_device_by_serial_number(serial)
    return devices.get_device_by_index(0)


def print_device_info(device: Any | None) -> None:
    if device is None:
        return
    info = safe_call(device, "get_device_info")
    if info is None:
        return
    fields = (
        ("name", "get_name"),
        ("serial", "get_serial_number"),
        ("firmware", "get_firmware_version"),
        ("hardware", "get_hardware_version"),
        ("connection", "get_connection_type"),
        ("vid", "get_vid"),
        ("pid", "get_pid"),
    )
    values = []
    for label, method in fields:
        value = safe_call(info, method)
        if value is not None:
            values.append(f"{label}={value}")
    if values:
        print("[device] selected " + " ".join(values))


def get_depth_work_mode_names(device: Any | None) -> list[str]:
    if device is None:
        raise RuntimeError(
            "Dual RGB requires an SDK with explicit device selection and depth-work-mode support."
        )
    if not hasattr(device, "get_depth_work_mode_list"):
        raise RuntimeError(
            "This pyorbbecsdk build does not expose get_depth_work_mode_list(). "
            "Install a current pyorbbecsdk2/Orbbec SDK v2 release."
        )

    mode_list = device.get_depth_work_mode_list()
    count = int(mode_list.get_count())
    names: list[str] = []
    for index in range(count):
        mode = mode_list.get_depth_work_mode_by_index(index)
        name = getattr(mode, "name", mode)
        names.append(str(name))
    return names


def switch_to_dual_color_mode(device: Any | None) -> str:
    """Switch a Gemini 305 into its one-device, two-color-sensor mode."""
    if device is None or not hasattr(device, "set_depth_work_mode"):
        raise RuntimeError(
            "This pyorbbecsdk build cannot switch the Gemini 305 work mode. "
            "Install a current pyorbbecsdk2/Orbbec SDK v2 release."
        )

    available = get_depth_work_mode_names(device)
    dual_mode = next(
        (name for name in available if name.casefold() == DUAL_COLOR_WORK_MODE.casefold()),
        None,
    )
    if dual_mode is None:
        available_text = ", ".join(available) or "none"
        raise RuntimeError(
            f"Device does not advertise '{DUAL_COLOR_WORK_MODE}'. Available modes: "
            f"{available_text}. Update Gemini 305 firmware and Orbbec SDK if needed."
        )

    current_mode = device.get_depth_work_mode()
    current_name = str(getattr(current_mode, "name", current_mode))
    print(f"[mode] current={current_name} available={available}")
    if current_name != dual_mode:
        print(f"[mode] switching to {dual_mode}; the camera will reinitialize")
        device.set_depth_work_mode(dual_mode)
        active_mode = device.get_depth_work_mode()
        active_name = str(getattr(active_mode, "name", active_mode))
        if active_name != dual_mode:
            raise RuntimeError(
                f"Requested work mode '{dual_mode}', but device reports '{active_name}'."
            )
    return current_name


def restore_depth_work_mode(device: Any | None, previous_mode: str) -> None:
    if (
        device is None
        or previous_mode == DUAL_COLOR_WORK_MODE
        or not hasattr(device, "set_depth_work_mode")
    ):
        return
    try:
        # Give the just-stopped pipelines time to release their sensor handles.
        time.sleep(1.0)
        print(f"[mode] restoring {previous_mode}; the camera will reinitialize")
        device.set_depth_work_mode(previous_mode)
        time.sleep(1.0)
        restored_mode = device.get_depth_work_mode()
        restored_name = str(getattr(restored_mode, "name", restored_mode))
        if restored_name != previous_mode:
            raise RuntimeError(
                f"device reports '{restored_name}' after restoring '{previous_mode}'"
            )
    except Exception as exc:
        print(
            f"[warning] could not restore work mode '{previous_mode}': "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def enumerate_video_profiles(pipeline: Any, sensor_type: Any) -> list[ProfileInfo]:
    profile_list = pipeline.get_stream_profile_list(sensor_type)
    count = int(profile_list.get_count())
    profiles: list[ProfileInfo] = []
    for idx in range(count):
        profile = profile_list.get_stream_profile_by_index(idx)
        is_video = safe_call(profile, "is_video_stream_profile")
        if is_video is False:
            continue
        video_profile = safe_call(profile, "as_video_stream_profile") or profile
        profiles.append(
            ProfileInfo(
                index=idx,
                width=int(video_profile.get_width()),
                height=int(video_profile.get_height()),
                fps=int(video_profile.get_fps()),
                format_name=enum_name(video_profile.get_format()),
                profile=video_profile,
            )
        )
    return sorted(
        profiles,
        key=lambda p: (p.pixels, p.width, p.height, p.fps, p.format_name),
        reverse=True,
    )


def pair_stereo_profiles(
    left_profiles: list[ProfileInfo],
    right_profiles: list[ProfileInfo],
    fps: int,
    prefer_color: bool = False,
) -> list[StereoProfileInfo]:
    """Return unique left/right profiles with identical video parameters."""
    right_by_mode: dict[tuple[int, int, int, str], ProfileInfo] = {}
    for profile in right_profiles:
        key = (profile.width, profile.height, profile.fps, profile.format_name)
        right_by_mode.setdefault(key, profile)

    pairs: list[StereoProfileInfo] = []
    seen: set[tuple[int, int, int, str]] = set()
    for left in left_profiles:
        key = (left.width, left.height, left.fps, left.format_name)
        if left.fps != fps or key in seen or key not in right_by_mode:
            continue
        seen.add(key)
        pairs.append(StereoProfileInfo(left=left, right=right_by_mode[key]))

    return sorted(
        pairs,
        key=lambda pair: (
            pair.pixels,
            pair.left.width,
            pair.left.height,
            COLOR_FORMAT_PRIORITY.get(pair.left.format_name.upper(), -1)
            if prefer_color
            else 0,
            pair.left.format_name,
        ),
        reverse=True,
    )


def profile_selection_key(profile: ProfileInfo, sensor_key: str) -> tuple[Any, ...]:
    format_priority = (
        COLOR_FORMAT_PRIORITY.get(profile.format_name.upper(), -1)
        if sensor_key in {"color", "left_color", "right_color", "stereo_rgb"}
        else 0
    )
    return (
        profile.pixels,
        profile.width,
        profile.height,
        format_priority,
        profile.format_name,
    )


def get_expected_frame(frames: Any, sensor_key: str) -> Any | None:
    for method in FRAME_GETTER_CANDIDATES[sensor_key]:
        frame = safe_call(frames, method)
        if frame is not None:
            return frame

    frame_count = safe_call(frames, "get_frame_count")
    if frame_count is None:
        frame_count = safe_call(frames, "get_count")
    if frame_count:
        return safe_call(frames, "get_frame_by_index", 0)
    return None


def frame_shape(frame: Any) -> tuple[int | None, int | None]:
    video_frame = safe_call(frame, "as_video_frame") or frame
    width = safe_call(video_frame, "get_width")
    height = safe_call(video_frame, "get_height")
    return (
        int(width) if width is not None else None,
        int(height) if height is not None else None,
    )


def frame_to_preview_image(frame: Any) -> Any | None:
    import cv2
    import numpy as np

    frame = safe_call(frame, "as_video_frame") or frame
    width, height = frame_shape(frame)
    if width is None or height is None:
        return None

    fmt = enum_name(frame.get_format()).upper()
    data = np.asanyarray(frame.get_data())
    flat = data.reshape(-1)

    if fmt == "MJPG":
        return cv2.imdecode(flat, cv2.IMREAD_COLOR)
    if fmt == "RGB":
        rgb = flat.reshape(height, width, 3)
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    if fmt == "BGR":
        return flat.reshape(height, width, 3)
    if fmt == "RGBA":
        rgba = flat.reshape(height, width, 4)
        return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
    if fmt == "BGRA":
        bgra = flat.reshape(height, width, 4)
        return cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
    if fmt in {"YUYV", "YUYV_PACKED"}:
        yuyv = flat.reshape(height, width, 2)
        return cv2.cvtColor(yuyv, cv2.COLOR_YUV2BGR_YUY2)
    if fmt == "UYVY":
        uyvy = flat.reshape(height, width, 2)
        return cv2.cvtColor(uyvy, cv2.COLOR_YUV2BGR_UYVY)
    if fmt == "Y8":
        gray = flat.reshape(height, width)
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    if fmt in {"Y16", "Z16"}:
        if flat.dtype.itemsize == 1:
            flat = flat.view(np.uint16)
        gray = flat.reshape(height, width)
        gray8 = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
        return cv2.cvtColor(gray8, cv2.COLOR_GRAY2BGR)
    return None


def draw_text_with_outline(
    image: Any,
    text: str,
    position: tuple[int, int],
    scale: float = 0.75,
) -> None:
    import cv2

    cv2.putText(
        image,
        text,
        position,
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        (0, 0, 0),
        3,
        cv2.LINE_AA,
    )
    cv2.putText(
        image,
        text,
        position,
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )


def test_profile(
    ob: Any,
    device: Any | None,
    sensor_key: str,
    profile: ProfileInfo,
    seconds: float,
    timeout_ms: int,
) -> TestResult:
    pipeline = make_pipeline(ob, device)
    config = ob.Config()
    started = False
    timestamps: list[float] = []
    actual_width: int | None = None
    actual_height: int | None = None

    try:
        config.enable_stream(profile.profile)
        pipeline.start(config)
        started = True

        deadline = time.monotonic() + max(seconds, 0.1)
        while time.monotonic() < deadline:
            frames = pipeline.wait_for_frames(timeout_ms)
            if frames is None:
                continue
            frame = get_expected_frame(frames, sensor_key)
            if frame is None:
                continue
            timestamps.append(time.monotonic())
            actual_width, actual_height = frame_shape(frame)

        if not timestamps:
            return TestResult(False, profile, error="stream started but no frames arrived")

        elapsed = max(timestamps[-1] - timestamps[0], 1e-9)
        measured_fps = (len(timestamps) - 1) / elapsed if len(timestamps) > 1 else 0.0
        return TestResult(
            True,
            profile,
            frames=len(timestamps),
            measured_fps=measured_fps,
            actual_width=actual_width,
            actual_height=actual_height,
        )
    except Exception as exc:
        return TestResult(False, profile, error=f"{type(exc).__name__}: {exc}")
    finally:
        if started:
            try:
                pipeline.stop()
            except Exception:
                pass


def test_stereo_profile(
    ob: Any,
    device: Any | None,
    profile: StereoProfileInfo,
    seconds: float,
    timeout_ms: int,
    left_sensor_key: str = "left_ir",
    right_sensor_key: str = "right_ir",
    stream_label: str = "stereo IR",
) -> StereoTestResult:
    pipeline = make_pipeline(ob, device)
    config = ob.Config()
    config.enable_stream(profile.left.profile)
    config.enable_stream(profile.right.profile)
    started = False
    timestamps: list[float] = []

    try:
        pipeline.start(config)
        started = True
        deadline = time.monotonic() + max(seconds, 0.1)
        while time.monotonic() < deadline:
            frames = pipeline.wait_for_frames(timeout_ms)
            if frames is None:
                continue
            left_frame = get_expected_frame(frames, left_sensor_key)
            right_frame = get_expected_frame(frames, right_sensor_key)
            if left_frame is not None and right_frame is not None:
                timestamps.append(time.monotonic())

        if not timestamps:
            return StereoTestResult(
                False,
                profile,
                error=f"both {stream_label} streams started but no paired frames arrived",
            )

        elapsed = max(timestamps[-1] - timestamps[0], 1e-9)
        measured_fps = (len(timestamps) - 1) / elapsed if len(timestamps) > 1 else 0.0
        return StereoTestResult(
            True,
            profile,
            frame_pairs=len(timestamps),
            measured_fps=measured_fps,
        )
    except Exception as exc:
        return StereoTestResult(False, profile, error=f"{type(exc).__name__}: {exc}")
    finally:
        if started:
            try:
                pipeline.stop()
            except Exception:
                pass


def show_preview(
    ob: Any,
    device: Any | None,
    sensor_key: str,
    profile: ProfileInfo,
    timeout_ms: int,
    window_width: int,
    preview_seconds: float,
) -> None:
    try:
        import cv2
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "OpenCV is required for preview; run this script in the pyroki environment "
            "or pass --no-preview."
        ) from exc

    pipeline = make_pipeline(ob, device)
    config = ob.Config()
    config.enable_stream(profile.profile)
    window_name = f"Orbbec {TARGET_DEVICE_HINT} - {sensor_key} - {profile.label()}"
    started = False
    preview_started = time.monotonic()
    fps_started = preview_started
    fps_frames = 0
    displayed_fps = 0.0
    warned_unsupported_format = False

    try:
        pipeline.start(config)
        started = True
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
        initial_height = max(round(window_width * profile.height / profile.width), 1)
        cv2.resizeWindow(window_name, window_width, initial_height)
        print(
            f"[preview] showing {profile.label()} in OpenCV; "
            "press q/Esc or close the window to stop"
        )

        while True:
            frames = pipeline.wait_for_frames(timeout_ms)
            if frames is not None:
                frame = get_expected_frame(frames, sensor_key)
                if frame is not None:
                    try:
                        image = frame_to_preview_image(frame)
                    except (ValueError, cv2.error) as exc:
                        raise RuntimeError(
                            f"Cannot convert {enum_name(frame.get_format())} frame for OpenCV: {exc}"
                        ) from exc

                    if image is not None:
                        fps_frames += 1
                        now = time.monotonic()
                        fps_elapsed = now - fps_started
                        if fps_elapsed >= 1.0:
                            displayed_fps = fps_frames / fps_elapsed
                            fps_started = now
                            fps_frames = 0

                        draw_text_with_outline(
                            image,
                            f"{profile.width}x{profile.height} {profile.format_name}  {displayed_fps:.1f} FPS",
                            (16, 32),
                        )
                        cv2.imshow(window_name, image)
                    elif not warned_unsupported_format:
                        print(
                            f"[preview] format {enum_name(frame.get_format())} is not supported "
                            "by the OpenCV converter",
                            file=sys.stderr,
                        )
                        warned_unsupported_format = True

            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q"), ord("Q")):
                break
            try:
                if cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
                    break
            except cv2.error:
                break
            if preview_seconds > 0 and time.monotonic() - preview_started >= preview_seconds:
                break
    finally:
        if started:
            try:
                pipeline.stop()
            except Exception:
                pass
        cv2.destroyAllWindows()
        cv2.waitKey(1)
        print("[preview] stopped")


def show_stereo_preview(
    ob: Any,
    device: Any | None,
    profile: StereoProfileInfo,
    timeout_ms: int,
    window_width: int,
    preview_seconds: float,
    left_sensor_key: str = "left_ir",
    right_sensor_key: str = "right_ir",
    stream_label: str = "stereo IR",
    left_overlay: str = "LEFT IR",
    right_overlay: str = "RIGHT IR",
) -> None:
    try:
        import cv2
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "OpenCV is required for preview; run this script in the pyroki environment "
            "or pass --no-preview."
        ) from exc

    pipeline = make_pipeline(ob, device)
    config = ob.Config()
    config.enable_stream(profile.left.profile)
    config.enable_stream(profile.right.profile)
    window_name = f"Orbbec {TARGET_DEVICE_HINT} - {stream_label} - {profile.label()}"
    started = False
    preview_started = time.monotonic()
    fps_started = preview_started
    fps_frame_pairs = 0
    displayed_fps = 0.0

    try:
        pipeline.start(config)
        started = True
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
        combined_width = profile.left.width + profile.right.width
        initial_height = max(
            round(window_width * profile.left.height / combined_width),
            1,
        )
        cv2.resizeWindow(window_name, window_width, initial_height)
        print(
            f"[preview] showing synchronized {stream_label} {profile.label()}; "
            "press q/Esc or close the window to stop"
        )

        while True:
            frames = pipeline.wait_for_frames(timeout_ms)
            if frames is not None:
                left_frame = get_expected_frame(frames, left_sensor_key)
                right_frame = get_expected_frame(frames, right_sensor_key)
                if left_frame is not None and right_frame is not None:
                    try:
                        left_image = frame_to_preview_image(left_frame)
                        right_image = frame_to_preview_image(right_frame)
                    except (ValueError, cv2.error) as exc:
                        raise RuntimeError(f"Cannot convert {stream_label} frames: {exc}") from exc

                    if left_image is None or right_image is None:
                        left_format = enum_name(left_frame.get_format())
                        right_format = enum_name(right_frame.get_format())
                        raise RuntimeError(
                            f"Unsupported {stream_label} formats for OpenCV: "
                            f"left={left_format}, right={right_format}"
                        )
                    if left_image.shape[0] != right_image.shape[0]:
                        raise RuntimeError(
                            "Left/right image heights differ: "
                            f"{left_image.shape[0]} != {right_image.shape[0]}"
                        )

                    fps_frame_pairs += 1
                    now = time.monotonic()
                    fps_elapsed = now - fps_started
                    if fps_elapsed >= 1.0:
                        displayed_fps = fps_frame_pairs / fps_elapsed
                        fps_started = now
                        fps_frame_pairs = 0

                    draw_text_with_outline(left_image, left_overlay, (16, 32))
                    draw_text_with_outline(right_image, right_overlay, (16, 32))
                    stereo_image = cv2.hconcat((left_image, right_image))
                    draw_text_with_outline(
                        stereo_image,
                        (
                            f"{profile.left.width}x{profile.left.height} "
                            f"{profile.left.format_name}  paired {displayed_fps:.1f} FPS"
                        ),
                        (16, stereo_image.shape[0] - 16),
                    )
                    cv2.imshow(window_name, stereo_image)

            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q"), ord("Q")):
                break
            try:
                if cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
                    break
            except cv2.error:
                break
            if preview_seconds > 0 and time.monotonic() - preview_started >= preview_seconds:
                break
    finally:
        if started:
            try:
                pipeline.stop()
            except Exception:
                pass
        cv2.destroyAllWindows()
        cv2.waitKey(1)
        print("[preview] stopped")


def run_paired_mode(
    ob: Any,
    device: Any | None,
    args: argparse.Namespace,
    *,
    left_sensor_key: str,
    right_sensor_key: str,
    stream_label: str,
    left_overlay: str,
    right_overlay: str,
    prefer_color: bool = False,
) -> int:
    left_sensor_type = resolve_sensor_type(ob, left_sensor_key)
    right_sensor_type = resolve_sensor_type(ob, right_sensor_key)
    enum_pipeline = make_pipeline(ob, device)
    left_profiles = enumerate_video_profiles(enum_pipeline, left_sensor_type)
    right_profiles = enumerate_video_profiles(enum_pipeline, right_sensor_type)

    candidates = pair_stereo_profiles(
        left_profiles,
        right_profiles,
        args.fps,
        prefer_color=prefer_color,
    )
    if not candidates:
        print(f"[result] no matching {stream_label} profile exists at {args.fps} fps")
        return 2

    print(f"[test] trying largest simultaneous {stream_label} profiles at {args.fps} fps")
    successes: list[StereoTestResult] = []
    failures: list[StereoTestResult] = []
    for candidate in candidates:
        print(f"  try LEFT + RIGHT {candidate.label()} ...", flush=True)
        result = test_stereo_profile(
            ob=ob,
            device=device,
            profile=candidate,
            seconds=args.seconds,
            timeout_ms=args.timeout_ms,
            left_sensor_key=left_sensor_key,
            right_sensor_key=right_sensor_key,
            stream_label=stream_label,
        )
        if result.ok:
            successes.append(result)
            print(
                f"    OK frame_pairs={result.frame_pairs} "
                f"measured_fps={result.measured_fps:.2f}"
            )
            if not args.try_all:
                break
        else:
            failures.append(result)
            print(f"    FAIL {result.error}")

    if not successes:
        print(f"[result] no {stream_label} profile at {args.fps} fps could be opened")
        for failure in failures:
            print(f"  {failure.profile.label()}: {failure.error}")
        return 3

    best = max(
        successes,
        key=lambda result: (
            result.profile.pixels,
            result.profile.left.width,
            result.profile.left.height,
            COLOR_FORMAT_PRIORITY.get(result.profile.left.format_name.upper(), -1)
            if prefer_color
            else 0,
            result.profile.left.format_name,
        ),
    )
    print(
        f"[result] max verified simultaneous {stream_label} resolution at "
        f"{args.fps} fps: {best.profile.left.width}x{best.profile.left.height} "
        f"format={best.profile.left.format_name} "
        f"measured_fps={best.measured_fps:.2f} frame_pairs={best.frame_pairs}"
    )
    if args.preview:
        show_stereo_preview(
            ob=ob,
            device=device,
            profile=best.profile,
            timeout_ms=args.timeout_ms,
            window_width=args.window_width,
            preview_seconds=args.preview_seconds,
            left_sensor_key=left_sensor_key,
            right_sensor_key=right_sensor_key,
            stream_label=stream_label,
            left_overlay=left_overlay,
            right_overlay=right_overlay,
        )
    return 0


def run_stereo_mode(
    ob: Any,
    device: Any | None,
    args: argparse.Namespace,
) -> int:
    return run_paired_mode(
        ob,
        device,
        args,
        left_sensor_key="left_ir",
        right_sensor_key="right_ir",
        stream_label="stereo IR",
        left_overlay="LEFT IR",
        right_overlay="RIGHT IR",
    )


def run_stereo_rgb_mode(
    ob: Any,
    device: Any | None,
    args: argparse.Namespace,
) -> int:
    previous_mode = switch_to_dual_color_mode(device)
    try:
        return run_paired_mode(
            ob,
            device,
            args,
            left_sensor_key="left_color",
            right_sensor_key="right_color",
            stream_label="dual RGB",
            left_overlay="LEFT RGB",
            right_overlay="RIGHT RGB",
            prefer_color=True,
        )
    finally:
        restore_depth_work_mode(device, previous_mode)


def main() -> int:
    args = parse_args()
    if args.fps <= 0:
        print("[error] --fps must be greater than zero", file=sys.stderr)
        return 2
    if args.seconds <= 0:
        print("[error] --seconds must be greater than zero", file=sys.stderr)
        return 2
    if args.timeout_ms <= 0:
        print("[error] --timeout-ms must be greater than zero", file=sys.stderr)
        return 2
    if args.preview_seconds < 0:
        print("[error] --preview-seconds cannot be negative", file=sys.stderr)
        return 2
    if args.window_width <= 0:
        print("[error] --window-width must be greater than zero", file=sys.stderr)
        return 2

    ob = import_orbbec_sdk()
    print(f"[sdk] pyorbbecsdk version={safe_call(ob, 'get_version') or 'unknown'}")

    try:
        device = select_device(ob, args.serial)
        print_device_info(device)
        if args.sensor == "stereo_rgb":
            return run_stereo_rgb_mode(ob, device, args)
        if args.sensor == "stereo_ir":
            return run_stereo_mode(ob, device, args)

        sensor_type = resolve_sensor_type(ob, args.sensor)

        enum_pipeline = make_pipeline(ob, device)
        profiles = enumerate_video_profiles(enum_pipeline, sensor_type)

        candidates = [profile for profile in profiles if profile.fps == args.fps]
        candidates.sort(
            key=lambda profile: profile_selection_key(profile, args.sensor),
            reverse=True,
        )
        if not candidates:
            print(f"[result] no {args.sensor} profile advertises exactly {args.fps} fps")
            return 2

        print(f"[test] trying largest {args.sensor} profiles at {args.fps} fps")
        successes: list[TestResult] = []
        failures: list[TestResult] = []
        for candidate in candidates:
            print(f"  try {candidate.label()} ...", flush=True)
            result = test_profile(
                ob=ob,
                device=device,
                sensor_key=args.sensor,
                profile=candidate,
                seconds=args.seconds,
                timeout_ms=args.timeout_ms,
            )
            if result.ok:
                successes.append(result)
                actual = (
                    f"{result.actual_width}x{result.actual_height}"
                    if result.actual_width and result.actual_height
                    else "unknown"
                )
                print(
                    f"    OK frames={result.frames} measured_fps={result.measured_fps:.2f} "
                    f"actual_frame={actual}"
                )
                if not args.try_all:
                    break
            else:
                failures.append(result)
                print(f"    FAIL {result.error}")

        if successes:
            best = max(
                successes,
                key=lambda result: profile_selection_key(result.profile, args.sensor),
            )
            print(
                "[result] max verified "
                f"{args.sensor} resolution at {args.fps} fps: {best.profile.width}x{best.profile.height} "
                f"format={best.profile.format_name} measured_fps={best.measured_fps:.2f} "
                f"frames={best.frames}"
            )
            if args.preview:
                show_preview(
                    ob=ob,
                    device=device,
                    sensor_key=args.sensor,
                    profile=best.profile,
                    timeout_ms=args.timeout_ms,
                    window_width=args.window_width,
                    preview_seconds=args.preview_seconds,
                )
            return 0

        print(f"[result] no advertised {args.fps} fps profile could be opened")
        for failure in failures:
            print(f"  {failure.profile.label()}: {failure.error}")
        return 3
    except KeyboardInterrupt:
        print("\n[stop] interrupted")
        return 130
    except Exception as exc:
        if "openUsbDevice failed" in str(exc):
            print("[hint] " + orbbec_udev_install_hint(ob).replace("\n", "\n[hint] "))
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
