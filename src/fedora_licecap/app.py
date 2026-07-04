from __future__ import annotations

import os
from pathlib import Path
import socket
import threading
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk, Gdk

from fedora_licecap.portal import PortalError, ScreenCastPortal, ScreenCastSession
from fedora_licecap.recorder import GifRecorder, RecorderError, RecordingResult


CONTROL_SOCKET_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "loopcap.sock"
APP_CSS = """
window {
  background: transparent;
}

#surface {
  background: rgba(244, 247, 251, 0.94);
  border: 1px solid rgba(216, 225, 236, 0.95);
  border-radius: 22px;
}

#surface label {
  color: #223043;
}

#fps-tag {
  color: #66788d;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
}

spinbutton {
  background: rgba(255, 255, 255, 0.96);
  border: 1px solid rgba(198, 210, 224, 0.98);
  border-radius: 12px;
  color: #1f2b3a;
  padding: 2px 8px;
}

button.control {
  background: rgba(230, 237, 247, 0.96);
  border: none;
  border-radius: 12px;
  min-width: 38px;
  min-height: 38px;
  padding: 0;
}

button.control:hover {
  background: rgba(219, 230, 244, 1.0);
}

button.control:disabled {
  background: rgba(231, 236, 243, 0.75);
}

button.stop {
  background: rgba(220, 70, 70, 0.96);
}

button.stop:hover {
  background: rgba(199, 53, 53, 1.0);
}

button.stop label {
  color: white;
}

#icon-glyph {
  font-size: 18px;
  font-weight: 700;
}

#status {
  color: #324559;
  font-size: 12px;
}
"""


def send_control_command(command: str) -> tuple[bool, str]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(CONTROL_SOCKET_PATH))
            client.sendall(f"{command}\n".encode("utf-8"))
            response = client.recv(1024).decode("utf-8").strip() or "ok"
    except OSError as exc:
        return False, f"Unable to reach LoopCap: {exc}"
    return True, response


class ControlServer:
    def __init__(self, on_stop_requested: Callable[[], bool]) -> None:
        self._on_stop_requested = on_stop_requested
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        CONTROL_SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
        if CONTROL_SOCKET_PATH.exists():
            CONTROL_SOCKET_PATH.unlink()

        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(CONTROL_SOCKET_PATH))
        self._server.listen(1)
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if CONTROL_SOCKET_PATH.exists():
            try:
                CONTROL_SOCKET_PATH.unlink()
            except OSError:
                pass

    def _serve(self) -> None:
        assert self._server is not None
        while self._running:
            try:
                connection, _address = self._server.accept()
            except OSError:
                break

            with connection:
                try:
                    payload = connection.recv(1024).decode("utf-8").strip()
                except OSError:
                    continue

                if payload == "stop":
                    GLib.idle_add(self._on_stop_requested)
                    reply = "stop-requested"
                else:
                    reply = "unknown-command"

                try:
                    connection.sendall(reply.encode("utf-8"))
                except OSError:
                    pass


class CaptureWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application) -> None:
        super().__init__(application=app)
        self.set_title("LoopCap")
        self.set_default_size(242, 90)
        self.set_size_request(242, 90)
        self.set_resizable(False)
        self.set_decorated(False)

        self._portal_pending = False
        self._portal: ScreenCastPortal | None = None
        self._recorder = GifRecorder(fps=10)
        self._current_output_path: Path | None = None
        self._current_session: ScreenCastSession | None = None
        self._control_server = ControlServer(self._handle_remote_stop)

        self._install_css()

        self._fps_adjustment = Gtk.Adjustment(value=10, lower=1, upper=30, step_increment=1)
        self._fps_spin = Gtk.SpinButton(adjustment=self._fps_adjustment, climb_rate=1, digits=0)
        self._fps_spin.set_numeric(True)
        self._fps_spin.set_width_chars(2)
        self._fps_spin.connect("value-changed", self._handle_fps_changed)

        self._play_button = self._build_glyph_button("▶", "Start recording", self._handle_start_clicked)
        self._pause_button = self._build_glyph_button("❚❚", "Pause recording", self._handle_pause_clicked)
        self._stop_button = self._build_glyph_button("■", "Stop recording", self._handle_stop_clicked)
        self._stop_button.add_css_class("stop")

        self._status_label = Gtk.Label(label="Ready")
        self._status_label.set_name("status")
        self._status_label.set_xalign(0)

        self._idle_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        fps_label = Gtk.Label(label="FPS")
        fps_label.set_name("fps-tag")
        fps_label.set_xalign(0)
        self._idle_controls.append(fps_label)
        self._idle_controls.append(self._fps_spin)
        self._idle_controls.append(self._play_button)

        self._recording_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._recording_controls.append(self._pause_button)
        self._recording_controls.append(self._stop_button)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.append(self._idle_controls)
        content.append(self._recording_controls)
        content.append(self._status_label)

        drag_handle = Gtk.WindowHandle()
        drag_handle.set_child(content)

        surface = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        surface.set_name("surface")
        surface.set_margin_top(8)
        surface.set_margin_bottom(8)
        surface.set_margin_start(8)
        surface.set_margin_end(8)
        surface.append(drag_handle)

        self.connect("close-request", self._handle_close_request)
        self.set_child(surface)
        self._initialize_portal()
        self._control_server.start()
        self._refresh_controls()

    def _install_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(APP_CSS.encode("utf-8"))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def _build_glyph_button(
        self,
        glyph: str,
        tooltip: str,
        callback: Callable[[Gtk.Button], None],
    ) -> Gtk.Button:
        label = Gtk.Label(label=glyph)
        label.set_name("icon-glyph")
        button = Gtk.Button()
        button.add_css_class("control")
        button.set_tooltip_text(tooltip)
        button.set_child(label)
        button.connect("clicked", callback)
        return button

    def _set_button_glyph(self, button: Gtk.Button, glyph: str, tooltip: str) -> None:
        label = Gtk.Label(label=glyph)
        label.set_name("icon-glyph")
        button.set_child(label)
        button.set_tooltip_text(tooltip)

    def _initialize_portal(self) -> None:
        try:
            self._portal = ScreenCastPortal()
        except PortalError as exc:
            self._play_button.set_sensitive(False)
            self._status_label.set_label(str(exc))
            return

        self._status_label.set_label("Ready")

    def _refresh_controls(self) -> None:
        recording = self._recorder.is_recording
        paused = self._recorder.is_paused

        self._idle_controls.set_visible(not recording)
        self._recording_controls.set_visible(recording)
        self._fps_spin.set_sensitive(not recording and not self._portal_pending)
        self._play_button.set_sensitive(not self._portal_pending)
        self._pause_button.set_sensitive(recording)
        self._stop_button.set_sensitive(recording)

        pause_glyph = "▶" if paused else "❚❚"
        pause_tip = "Resume recording" if paused else "Pause recording"
        self._set_button_glyph(self._pause_button, pause_glyph, pause_tip)

    def _sync_fps_from_input(self) -> None:
        self._fps_spin.update()
        self._recorder.set_fps(self._fps_spin.get_value_as_int())

    def _handle_fps_changed(self, _spin: Gtk.SpinButton) -> None:
        try:
            self._sync_fps_from_input()
        except RecorderError:
            return

        if not self._recorder.is_recording and not self._portal_pending:
            self._status_label.set_label("Ready")

    def _handle_start_clicked(self, _button: Gtk.Button) -> None:
        if self._portal is None or self._portal_pending or self._recorder.is_recording:
            return

        try:
            self._sync_fps_from_input()
        except RecorderError:
            return

        self._portal_pending = True
        self._status_label.set_label("Select source")
        self._refresh_controls()
        self._portal.start(self._handle_session_ready, self._handle_portal_error)

    def _handle_pause_clicked(self, _button: Gtk.Button) -> None:
        if not self._recorder.is_recording:
            return

        try:
            if self._recorder.is_paused:
                if self._portal is None or self._current_session is None:
                    raise RecorderError("No active session to resume.")
                pipewire_fd = self._portal.open_pipewire_remote()
                self._recorder.resume(pipewire_fd)
                self._status_label.set_label("Recording")
            else:
                self._recorder.pause()
                self._status_label.set_label("Paused")
        except RecorderError as exc:
            self._status_label.set_label(str(exc))
            return

        self._refresh_controls()

    def _handle_stop_clicked(self, _button: Gtk.Button) -> None:
        if self._recorder.is_recording:
            self._request_stop_recording("Finalizing GIF")

    def _request_stop_recording(self, status_text: str) -> None:
        self._status_label.set_label(status_text)
        self._pause_button.set_sensitive(False)
        self._stop_button.set_sensitive(False)
        self._recorder.stop()

    def _handle_remote_stop(self) -> bool:
        if self._recorder.is_recording:
            self._request_stop_recording("Stopping...")
        return False

    def _handle_session_ready(self, session: ScreenCastSession) -> None:
        GLib.idle_add(self._apply_session_ready, session)

    def _apply_session_ready(self, session: ScreenCastSession) -> bool:
        self._portal_pending = False
        if self._portal is None:
            self._apply_portal_error("Portal unavailable")
            return False

        try:
            pipewire_fd = self._portal.open_pipewire_remote()
            output_path = self._recorder.start(session, pipewire_fd, self._handle_recording_finished)
        except (PortalError, RecorderError) as exc:
            self._portal.stop()
            self._apply_portal_error(str(exc))
            return False

        self._current_output_path = output_path
        self._current_session = session
        self._status_label.set_label("Recording")
        self._refresh_controls()
        return False

    def _handle_portal_error(self, message: str) -> None:
        GLib.idle_add(self._apply_portal_error, message)

    def _apply_portal_error(self, message: str) -> bool:
        self._portal_pending = False
        self._current_output_path = None
        self._status_label.set_label(message)
        self._refresh_controls()
        return False

    def _handle_recording_finished(self, result: RecordingResult) -> None:
        GLib.idle_add(self._apply_recording_finished, result)

    def _apply_recording_finished(self, result: RecordingResult) -> bool:
        if self._portal is not None:
            self._portal.stop()

        self._current_output_path = None
        self._current_session = None
        if result.success:
            self._status_label.set_label("Saved")
        else:
            self._status_label.set_label("Recording failed")

        self._refresh_controls()
        return False

    def _handle_close_request(self, *_args: object) -> bool:
        self._control_server.stop()
        if self._recorder.is_recording:
            self._recorder.stop()
        if self._portal is not None:
            self._portal.stop()
        return False


class LoopCapApplication(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id="com.uebis.LoopCap",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            window = CaptureWindow(self)
        window.present()


def run() -> int:
    app = LoopCapApplication()
    return app.run(None)
