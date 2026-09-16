"""A raw TCP console onto the board, over the endpoint used for flashing.

Nothing here touches Qt: a socket, an incremental decoder, and the line ending
to send.

The transport is deliberately *raw* TCP rather than telnet, even though the
endpoint is the sort of thing people reach for ``telnet`` to talk to. The
address is a serial device server in TCP-server mode, where the socket is
simply a pipe onto the RS485 bus. A telnet client opens by sending IAC
negotiation bytes, which every board on the bus would see as noise, and would
in turn consume parts of their replies as if they were negotiation.

The bus is shared, so this is a broadcast medium: everything sent reaches every
board on the port, and their replies come back interleaved. The board address
used for flashing means nothing here.
"""

from __future__ import annotations

import codecs
import socket
import threading
from enum import Enum

__all__ = [
    "CONNECT_TIMEOUT",
    "READ_TIMEOUT",
    "ConsoleError",
    "LineEnding",
    "RawConsole",
]

#: How long to wait for the device server to accept a connection.
CONNECT_TIMEOUT = 5.0

#: How long `RawConsole.read` blocks before returning empty handed. It bounds
#: how long a reader thread takes to notice it has been asked to stop.
READ_TIMEOUT = 0.2

#: Enough for a burst of output without splitting it into needless updates.
_CHUNK = 4096


class ConsoleError(RuntimeError):
    """The console could not be opened, or failed while connected."""


class LineEnding(Enum):
    """What to append to a line that is sent.

    The D2AFE console terminates on CRLF, so that is the default rather than
    the newline a unix-shaped tool would send.
    """

    CR = "\r"
    LF = "\n"
    CRLF = "\r\n"

    @property
    def label(self) -> str:
        """The name to show in the UI."""
        return self.name


class RawConsole:
    """A line-oriented raw TCP connection to `ip`:`port`.

    Reading and writing may be done from different threads — a socket is
    full duplex, and `close` is safe to call while a read is in flight, which
    is what lets a GUI shut the connection down without waiting for the read
    timeout to expire.
    """

    def __init__(
        self,
        ip: str,
        port: int,
        *,
        connect_timeout: float = CONNECT_TIMEOUT,
        read_timeout: float = READ_TIMEOUT,
    ) -> None:
        self.ip = ip
        self.port = port
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        # Bytes can arrive split mid-character, so decoding has to carry state
        # from one read to the next rather than decode each chunk alone.
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    @property
    def endpoint(self) -> str:
        """The address, as it would be written in a command."""
        return f"{self.ip}:{self.port}"

    @property
    def is_open(self) -> bool:
        """Whether the connection is currently established."""
        with self._lock:
            return self._sock is not None

    def open(self) -> None:
        """Connect, or raise `ConsoleError` explaining why not."""
        if self.is_open:
            raise ConsoleError("The console is already connected")
        try:
            sock = socket.create_connection(
                (self.ip, self.port), timeout=self.connect_timeout
            )
        except OSError as e:
            raise ConsoleError(f"Could not connect to {self.endpoint}: {e}") from e
        # Console traffic is small and interactive; waiting to fill a packet
        # would add latency to every keystroke.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(self.read_timeout)
        with self._lock:
            self._sock = sock

    def read(self) -> str | None:
        """Text received since the last call.

        Returns ``""`` if nothing arrived before the read timeout, and `None`
        once the far end has closed the connection or the console has been
        closed from another thread.
        """
        with self._lock:
            sock = self._sock
        if sock is None:
            return None
        try:
            chunk = sock.recv(_CHUNK)
        except TimeoutError:
            return ""
        except OSError:
            # A close from another thread races us here; report it as an
            # ordinary end of stream rather than an error.
            return None
        if not chunk:
            return None
        return self._decoder.decode(chunk)

    def send(self, text: str, ending: LineEnding = LineEnding.CRLF) -> None:
        """Send `text`, terminated by `ending`."""
        with self._lock:
            sock = self._sock
        if sock is None:
            raise ConsoleError("The console is not connected")
        try:
            sock.sendall((text + ending.value).encode())
        except OSError as e:
            raise ConsoleError(f"Could not send to {self.endpoint}: {e}") from e

    def close(self) -> None:
        """Close the connection. Doing so twice is not an error."""
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            # Unblock a read waiting on this socket in another thread.
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # Already gone; nothing to unblock.
        sock.close()
