from __future__ import annotations

from dataclasses import dataclass
import os
import secrets
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib


DESKTOP_BUS_NAME = "org.freedesktop.portal.Desktop"
DESKTOP_OBJECT_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_INTERFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
SESSION_INTERFACE = "org.freedesktop.portal.Session"

SOURCE_TYPE_MONITOR = 1
CURSOR_MODE_EMBEDDED = 2


class PortalError(RuntimeError):
    pass


@dataclass(slots=True)
class StreamDescriptor:
    node_id: int
    source_type: int | None
    size: tuple[int, int] | None
    position: tuple[int, int] | None
    stream_id: str | None
    pipewire_serial: int | None

    def summary(self) -> str:
        size_text = "unknown size"
        if self.size is not None:
            size_text = f"{self.size[0]}x{self.size[1]}"

        source_text = {
            1: "monitor",
            2: "window",
            4: "virtual monitor",
        }.get(self.source_type, "unknown source")

        return f"{source_text}, {size_text}, node {self.node_id}"


@dataclass(slots=True)
class ScreenCastSession:
    session_handle: str
    streams: list[StreamDescriptor]
    restore_token: str | None

    def summary_lines(self) -> list[str]:
        lines = [f"{len(self.streams)} stream selected"]
        for index, stream in enumerate(self.streams, start=1):
            lines.append(f"{index}. {stream.summary()}")
        if self.restore_token:
            lines.append("Session restore token received")
        return lines


class ScreenCastPortal:
    def __init__(self) -> None:
        try:
            self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._proxy = Gio.DBusProxy.new_sync(
                self._connection,
                Gio.DBusProxyFlags.NONE,
                None,
                DESKTOP_BUS_NAME,
                DESKTOP_OBJECT_PATH,
                SCREENCAST_INTERFACE,
                None,
            )
        except GLib.Error as exc:
            raise PortalError(f"Unable to reach xdg-desktop-portal: {exc.message}") from exc

        self._session_handle: str | None = None
        self._session_closed_subscription: int | None = None
        self._busy = False

    def start(
        self,
        on_success: Callable[[ScreenCastSession], None],
        on_error: Callable[[str], None],
    ) -> None:
        if self._busy:
            on_error("A portal request is already in progress.")
            return

        self._busy = True
        handle_token = self._make_token("create")
        session_token = self._make_token("session")
        expected_request_path = self._request_path(handle_token)

        def after_create(results: dict[str, object]) -> None:
            session_handle = str(results["session_handle"])
            self._select_sources(session_handle, on_success, on_error)

        self._request_response(
            method_name="CreateSession",
            parameters=GLib.Variant(
                "(a{sv})",
                ({
                    "handle_token": GLib.Variant("s", handle_token),
                    "session_handle_token": GLib.Variant("s", session_token),
                },),
            ),
            request_path=expected_request_path,
            on_success=after_create,
            on_error=on_error,
        )

    def open_pipewire_remote(self) -> int:
        if self._session_handle is None:
            raise PortalError("There is no active screencast session.")

        try:
            result, fd_list = self._proxy.call_with_unix_fd_list_sync(
                "OpenPipeWireRemote",
                GLib.Variant("(oa{sv})", (self._session_handle, {})),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
                None,
            )
        except GLib.Error as exc:
            raise PortalError(f"Unable to open the PipeWire remote: {exc.message}") from exc

        fd_index = int(result.unpack()[0])
        raw_fd = fd_list.get(fd_index)
        return os.dup(raw_fd)

    def stop(self) -> None:
        self._busy = False
        if self._session_closed_subscription is not None:
            self._connection.signal_unsubscribe(self._session_closed_subscription)
            self._session_closed_subscription = None

        if self._session_handle is None:
            return

        session_handle = self._session_handle
        self._session_handle = None

        try:
            session_proxy = Gio.DBusProxy.new_sync(
                self._connection,
                Gio.DBusProxyFlags.NONE,
                None,
                DESKTOP_BUS_NAME,
                session_handle,
                SESSION_INTERFACE,
                None,
            )
            session_proxy.call_sync(
                "Close",
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except GLib.Error:
            pass

    def _select_sources(
        self,
        session_handle: str,
        on_success: Callable[[ScreenCastSession], None],
        on_error: Callable[[str], None],
    ) -> None:
        handle_token = self._make_token("select")
        request_path = self._request_path(handle_token)

        self._request_response(
            method_name="SelectSources",
            parameters=GLib.Variant(
                "(oa{sv})",
                (
                    session_handle,
                    {
                        "handle_token": GLib.Variant("s", handle_token),
                        "types": GLib.Variant("u", SOURCE_TYPE_MONITOR),
                        "multiple": GLib.Variant("b", False),
                        "cursor_mode": GLib.Variant("u", CURSOR_MODE_EMBEDDED),
                    },
                ),
            ),
            request_path=request_path,
            on_success=lambda _results: self._start_session(
                session_handle,
                on_success,
                on_error,
            ),
            on_error=on_error,
        )

    def _start_session(
        self,
        session_handle: str,
        on_success: Callable[[ScreenCastSession], None],
        on_error: Callable[[str], None],
    ) -> None:
        handle_token = self._make_token("start")
        request_path = self._request_path(handle_token)

        def finish(results: dict[str, object]) -> None:
            self._busy = False
            self._session_handle = session_handle
            self._subscribe_session_closed(session_handle)

            streams = [self._parse_stream(item) for item in list(results.get("streams", []))]
            restore_token = self._optional_str(results.get("restore_token"))
            on_success(
                ScreenCastSession(
                    session_handle=session_handle,
                    streams=streams,
                    restore_token=restore_token,
                )
            )

        self._request_response(
            method_name="Start",
            parameters=GLib.Variant(
                "(osa{sv})",
                (session_handle, "", {"handle_token": GLib.Variant("s", handle_token)}),
            ),
            request_path=request_path,
            on_success=finish,
            on_error=on_error,
        )

    def _request_response(
        self,
        method_name: str,
        parameters: GLib.Variant,
        request_path: str,
        on_success: Callable[[dict[str, object]], None],
        on_error: Callable[[str], None],
    ) -> None:
        subscription_id: int | None = None

        def unsubscribe() -> None:
            nonlocal subscription_id
            if subscription_id is not None:
                self._connection.signal_unsubscribe(subscription_id)
                subscription_id = None

        def response_callback(
            _connection: Gio.DBusConnection,
            _sender_name: str,
            _object_path: str,
            _interface_name: str,
            _signal_name: str,
            response_parameters: GLib.Variant,
            _user_data: object,
        ) -> None:
            unsubscribe()

            response_code = int(response_parameters.get_child_value(0).unpack())
            results = self._unpack(response_parameters.get_child_value(1))

            if response_code == 0:
                on_success(results)
                return

            self._busy = False
            if response_code == 1:
                on_error("Screen sharing was cancelled.")
                return

            on_error(f"Portal request failed with response code {response_code}.")

        subscription_id = self._connection.signal_subscribe(
            DESKTOP_BUS_NAME,
            REQUEST_INTERFACE,
            "Response",
            request_path,
            None,
            Gio.DBusSignalFlags.NONE,
            response_callback,
            None,
        )

        try:
            reply = self._proxy.call_sync(
                method_name,
                parameters,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except GLib.Error as exc:
            unsubscribe()
            self._busy = False
            on_error(f"Portal call {method_name} failed: {exc.message}")
            return

        actual_request_path = str(reply.unpack()[0])
        if actual_request_path == request_path:
            return

        unsubscribe()
        subscription_id = self._connection.signal_subscribe(
            DESKTOP_BUS_NAME,
            REQUEST_INTERFACE,
            "Response",
            actual_request_path,
            None,
            Gio.DBusSignalFlags.NONE,
            response_callback,
            None,
        )

    def _subscribe_session_closed(self, session_handle: str) -> None:
        if self._session_closed_subscription is not None:
            self._connection.signal_unsubscribe(self._session_closed_subscription)

        def closed_callback(
            _connection: Gio.DBusConnection,
            _sender_name: str,
            _object_path: str,
            _interface_name: str,
            _signal_name: str,
            _parameters: GLib.Variant,
            _user_data: object,
        ) -> None:
            self._busy = False
            self._session_handle = None
            if self._session_closed_subscription is not None:
                self._connection.signal_unsubscribe(self._session_closed_subscription)
                self._session_closed_subscription = None

        self._session_closed_subscription = self._connection.signal_subscribe(
            DESKTOP_BUS_NAME,
            SESSION_INTERFACE,
            "Closed",
            session_handle,
            None,
            Gio.DBusSignalFlags.NONE,
            closed_callback,
            None,
        )

    def _make_token(self, prefix: str) -> str:
        return f"loopcap_{prefix}_{secrets.token_hex(6)}"

    def _request_path(self, token: str) -> str:
        sender = self._connection.get_unique_name()
        if not sender:
            raise PortalError("D-Bus connection did not provide a unique name.")
        sender_path = sender[1:].replace(".", "_")
        return f"/org/freedesktop/portal/desktop/request/{sender_path}/{token}"

    def _parse_stream(self, item: object) -> StreamDescriptor:
        node_id, properties = item  # type: ignore[misc]
        props = dict(properties)

        return StreamDescriptor(
            node_id=int(node_id),
            source_type=self._optional_int(props.get("source_type")),
            size=self._optional_pair(props.get("size")),
            position=self._optional_pair(props.get("position")),
            stream_id=self._optional_str(props.get("id")),
            pipewire_serial=self._optional_int(props.get("pipewire-serial")),
        )

    def _optional_int(self, value: object) -> int | None:
        if value is None:
            return None
        return int(value)

    def _optional_str(self, value: object) -> str | None:
        if value is None:
            return None
        return str(value)

    def _optional_pair(self, value: object) -> tuple[int, int] | None:
        if value is None:
            return None
        first, second = value  # type: ignore[misc]
        return int(first), int(second)

    def _unpack(self, value: object) -> object:
        if isinstance(value, GLib.Variant):
            return self._unpack(value.unpack())
        if isinstance(value, dict):
            return {key: self._unpack(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._unpack(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._unpack(item) for item in value)
        return value
