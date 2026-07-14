"""Shared transport contract used by the desktop UI and send queue."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TransportPeer(Protocol):
    name: str
    host: str
    port: int
    service_name: str


@runtime_checkable
class TransportNode(Protocol):
    """The behavioral surface shared by LAN and relay transports."""

    cfg: object

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def send_file(self, local_path: str) -> bool: ...

    def peers(self) -> list[TransportPeer]: ...

    def selected_peer(self) -> str | None: ...

    def select_peer(self, name: str | None) -> None: ...

