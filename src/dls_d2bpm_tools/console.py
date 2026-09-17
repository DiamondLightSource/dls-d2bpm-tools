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
import re
import socket
import threading
from enum import Enum

__all__ = [
    "CONNECT_TIMEOUT",
    "READ_TIMEOUT",
    "ConsoleError",
    "LineEnding",
    "RawConsole",
    "TextDecoder",
    "format_hex",
    "parse_hex",
]

#: How long to wait for the device server to accept a connection.
CONNECT_TIMEOUT = 5.0

#: How long `RawConsole.read` blocks before returning empty handed. It bounds
#: how long a reader thread takes to notice it has been asked to stop.
READ_TIMEOUT = 0.2

#: Enough for a burst of output without splitting it into needless updates.
_CHUNK = 4096

#: Separators and prefixes people write hex with, all of which are ignored.
_HEX_NOISE_RE = re.compile(r"0x|\\x|[\s,;:_-]")


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


def format_hex(data: bytes) -> str:
    """Render `data` as space separated uppercase hex, e.g. ``41 42 0D 0A``.

    A flat stream rather than an offset-and-ASCII hexdump: bytes arrive in
    whatever chunks the network hands over, so there are no meaningful lines
    to number.
    """
    return " ".join(f"{byte:02X}" for byte in data)


def parse_hex(text: str) -> bytes:
    """The bytes written as hex in `text`, or raise `ValueError`.

    Liberal about how they are written, since people copy hex out of all
    sorts of places: ``0d0a``, ``0D 0A``, ``0x0d,0x0a`` and ``\\x0d\\x0a``
    all mean the same two bytes.
    """
    cleaned = _HEX_NOISE_RE.sub("", text)
    if not cleaned:
        raise ValueError("Enter some hex bytes, for example 0D 0A")
    stray = sorted(set(cleaned) - set("0123456789abcdefABCDEF"))
    if stray:
        strays = " ".join(stray)
        is_are = "is" if len(stray) == 1 else "are"
        raise ValueError(f"{strays} {is_are} not hex")
    if len(cleaned) % 2:
        raise ValueError(f"{len(cleaned)} hex digits is not a whole number of bytes")
    return bytes.fromhex(cleaned)


class TextDecoder:
    """Decodes a byte stream to text, one arbitrary chunk at a time.

    Bytes can arrive split mid-character, so this has to carry state from one
    chunk to the next rather than decode each alone. Undecodable bytes become
    replacement characters: line noise on an RS485 bus must not take the
    console down with it.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Forget any half-finished character."""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def decode(self, data: bytes) -> str:
        """The text `data` completes, which may be none of it."""
        return self._decoder.decode(data)


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

    def read(self) -> bytes | None:
        """Bytes received since the last call.

        Raw bytes rather than text, because the caller may want to show them
        as hex — where a termination character is the whole point of looking.

        Returns ``b""`` if nothing arrived before the read timeout, and `None`
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
            return b""
        except OSError:
            # A close from another thread races us here; report it as an
            # ordinary end of stream rather than an error.
            return None
        if not chunk:
            return None
        return chunk

    def send_text(self, text: str, ending: LineEnding = LineEnding.CRLF) -> None:
        """Send `text`, terminated by `ending`."""
        self.send_bytes((text + ending.value).encode())

    def send_bytes(self, data: bytes) -> None:
        """Send `data` exactly as given, with nothing appended.

        Nothing is appended deliberately: someone sending raw bytes is usually
        doing it to control the terminator themselves.
        """
        with self._lock:
            sock = self._sock
        if sock is None:
            raise ConsoleError("The console is not connected")
        try:
            sock.sendall(data)
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
