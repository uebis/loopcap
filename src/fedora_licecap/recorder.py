from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
from typing import Callable

from fedora_licecap.portal import ScreenCastSession


class RecorderError(RuntimeError):
    pass


@dataclass(slots=True)
class RecordingResult:
    output_path: Path
    success: bool
    details: str


class GifRecorder:
    def __init__(self, fps: int = 10) -> None:
        self._fps = fps
        self._process: subprocess.Popen[str] | None = None
        self._pipewire_fd: int | None = None
        self._output_path: Path | None = None
        self._frames_dir: Path | None = None
        self._paused = False
        self._finishing = False
        self._on_finished: Callable[[RecordingResult], None] | None = None
        self._next_frame_index = 0
        self._segment_target_object: str | None = None
        self._segment_path: int | None = None

    @property
    def is_recording(self) -> bool:
        return self._process is not None or self._paused

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def fps(self) -> int:
        return self._fps

    def set_fps(self, fps: int) -> None:
        if self.is_recording:
            raise RecorderError("Cannot change FPS while recording.")
        self._fps = max(1, min(30, int(fps)))

    def start(
        self,
        session: ScreenCastSession,
        pipewire_fd: int,
        on_finished: Callable[[RecordingResult], None],
    ) -> Path:
        if self.is_recording or self._finishing:
            raise RecorderError("A recording is already running.")
        if not session.streams:
            raise RecorderError("No PipeWire stream was selected.")

        stream = session.streams[0]
        self._output_path = self._build_output_path()
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._frames_dir = Path(tempfile.mkdtemp(prefix="fedora-licecap-frames-"))
        self._on_finished = on_finished
        self._next_frame_index = 0
        self._segment_target_object = str(stream.pipewire_serial) if stream.pipewire_serial is not None else None
        self._segment_path = stream.node_id
        self._paused = False
        self._finishing = False

        self._start_segment(pipewire_fd)
        return self._output_path

    def pause(self) -> None:
        if self._process is None:
            raise RecorderError("No recording is running.")
        if self._paused:
            return

        self._stop_segment()
        self._paused = True

    def resume(self, pipewire_fd: int) -> None:
        if not self._paused:
            raise RecorderError("Recording is not paused.")
        self._paused = False
        self._start_segment(pipewire_fd)

    def stop(self) -> None:
        if not self.is_recording:
            return
        if self._frames_dir is None or self._output_path is None or self._on_finished is None:
            raise RecorderError("Recording state is incomplete.")

        if self._process is not None:
            self._stop_segment()

        self._paused = False
        self._finishing = True
        frames_dir = self._frames_dir
        output_path = self._output_path
        callback = self._on_finished

        def finalize() -> None:
            encode_result = self._encode_gif(frames_dir, output_path)
            shutil.rmtree(frames_dir, ignore_errors=True)
            self._reset_state()
            callback(encode_result)

        threading.Thread(target=finalize, daemon=True).start()

    def _start_segment(self, pipewire_fd: int) -> None:
        if self._frames_dir is None:
            raise RecorderError("Recording frames directory is unavailable.")

        frame_pattern = self._frames_dir / "frame_%06d.png"
        command = ["gst-launch-1.0", "-e"]
        command.extend([
            "pipewiresrc",
            f"fd={pipewire_fd}",
            "do-timestamp=true",
            "keepalive-time=1000",
        ])
        if self._segment_target_object is not None:
            command.append(f"target-object={self._segment_target_object}")
        elif self._segment_path is not None:
            command.append(f"path={self._segment_path}")

        command.extend([
            "!",
            "videorate",
            "!",
            f"video/x-raw,framerate={self._fps}/1",
            "!",
            "videoconvert",
            "!",
            "video/x-raw,format=RGBA",
            "!",
            "pngenc",
            "compression-level=2",
            "snapshot=false",
            "!",
            "multifilesink",
            f"location={frame_pattern}",
            f"index={self._next_frame_index}",
        ])

        try:
            self._process = subprocess.Popen(
                command,
                pass_fds=(pipewire_fd,),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            os.close(pipewire_fd)
            raise RecorderError(f"Unable to start gst-launch-1.0: {exc}") from exc

        self._pipewire_fd = pipewire_fd

    def _stop_segment(self) -> None:
        if self._process is None:
            return

        process = self._process
        if self._pipewire_fd is not None:
            fd_to_close = self._pipewire_fd
        else:
            fd_to_close = None

        process.send_signal(signal.SIGINT)
        stdout_text, stderr_text = process.communicate()
        self._process = None

        if fd_to_close is not None:
            os.close(fd_to_close)
            self._pipewire_fd = None

        self._next_frame_index = self._count_frames()

        if process.returncode not in (0, 130):
            details = stderr_text.strip() or stdout_text.strip() or "Capture segment stopped unexpectedly."
            raise RecorderError(details)

    def _count_frames(self) -> int:
        if self._frames_dir is None:
            return 0
        return len(list(self._frames_dir.glob("frame_*.png")))

    def _encode_gif(self, frames_dir: Path, output_path: Path) -> RecordingResult:
        first_frame = frames_dir / "frame_000000.png"
        if not first_frame.exists():
            return RecordingResult(
                output_path=output_path,
                success=False,
                details="No frames were captured from the selected source.",
            )

        palette_path = frames_dir / "palette.png"
        input_pattern = frames_dir / "frame_%06d.png"

        palette_cmd = [
            "ffmpeg",
            "-y",
            "-framerate",
            str(self._fps),
            "-i",
            str(input_pattern),
            "-vf",
            "palettegen=stats_mode=diff",
            str(palette_path),
        ]
        palette_run = subprocess.run(
            palette_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if palette_run.returncode != 0 or not palette_path.exists():
            details = palette_run.stderr.strip() or palette_run.stdout.strip() or "Unable to generate GIF palette."
            return RecordingResult(output_path=output_path, success=False, details=details)

        gif_cmd = [
            "ffmpeg",
            "-y",
            "-framerate",
            str(self._fps),
            "-i",
            str(input_pattern),
            "-i",
            str(palette_path),
            "-lavfi",
            "paletteuse=dither=sierra2_4a",
            str(output_path),
        ]
        gif_run = subprocess.run(
            gif_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        success = gif_run.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0
        details = gif_run.stderr.strip() or gif_run.stdout.strip() or (
            "GIF saved successfully." if success else "GIF encoding failed."
        )
        return RecordingResult(output_path=output_path, success=success, details=details)

    def _reset_state(self) -> None:
        self._process = None
        self._pipewire_fd = None
        self._output_path = None
        self._frames_dir = None
        self._paused = False
        self._finishing = False
        self._on_finished = None
        self._next_frame_index = 0
        self._segment_target_object = None
        self._segment_path = None

    def _build_output_path(self) -> Path:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base_dir = Path.home() / "Videos" / "LoopCap"
        return base_dir / f"capture_{timestamp}.gif"
