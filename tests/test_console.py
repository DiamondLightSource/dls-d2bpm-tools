"""Tests for the raw TCP console, against a loopback server. No Qt involved."""

import socket
import threading
import time
from collections.abc import Iterator

import pytest

from dls_d2bpm_tools.console import (
    ConsoleError,
    LineEnding,
    RawConsole,
    TextDecoder,
    format_hex,
    parse_hex,
)


class FakeServer:
    """A one-connection loopback server standing in for the device server."""

    def __init__(self) -> None:
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port: int = self._listener.getsockname()[1]
        self.conn: socket.socket | None = None
        self.received = bytearray()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return  # Closed before anything connected.
        self.conn = conn
        # Closed here rather than left to the fixture: the garbage collector
        # would otherwise reclaim it at an arbitrary moment, and a
        # ResourceWarning raised under `filterwarnings = "error"` fails
        # whichever test happens to be running at the time.
        with conn:
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                self.received += chunk

    def send(self, data: bytes) -> None:
        """Push bytes towards the client, as a board would."""
        conn = self._wait_for_connection()
        conn.sendall(data)

    def hang_up(self) -> None:
        """Drop the connection, as a power cycle would.

        Shut down before closing: this thread is blocked reading the same
        socket, which holds it open in the kernel, so a bare close would not
        send the FIN the client is waiting for.
        """
        conn = self._wait_for_connection()
        conn.shutdown(socket.SHUT_RDWR)
        conn.close()

    def _wait_for_connection(self, timeout: float = 2.0) -> socket.socket:
        deadline = time.monotonic() + timeout
        while self.conn is None:
            if time.monotonic() > deadline:
                raise AssertionError("Nothing connected to the fake server")
            time.sleep(0.005)
        return self.conn

    def wait_for(self, expected: bytes, timeout: float = 2.0) -> None:
        """Block until `expected` has been received, or fail."""
        deadline = time.monotonic() + timeout
        while bytes(self.received) != expected:
            if time.monotonic() > deadline:
                raise AssertionError(f"Got {bytes(self.received)!r}, want {expected!r}")
            time.sleep(0.005)

    def close(self) -> None:
        # Unblock accept/recv before closing: close alone leaves an in-flight
        # call holding the socket open on Linux, including the listening port.
        try:
            self._listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._listener.close()
        # An accept already in flight may just have populated self.conn.
        # The server thread exits once its peer closes in the usual case.
        if self.conn is not None:
            try:
                self.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.conn.close()
        self._thread.join(timeout=2)


@pytest.fixture
def server() -> Iterator[FakeServer]:
    fake = FakeServer()
    yield fake
    fake.close()


@pytest.fixture
def console(server: FakeServer) -> Iterator[RawConsole]:
    con = RawConsole("127.0.0.1", server.port, read_timeout=0.05)
    yield con
    con.close()


def read_until(console: RawConsole, timeout: float = 2.0) -> bytes | None:
    """Read past the empty-handed timeouts to the next real data, or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = console.read()
        if data != b"":
            return data
    raise AssertionError("Nothing was received")


def test_endpoint_is_the_address_as_written(console: RawConsole) -> None:
    assert console.endpoint == f"127.0.0.1:{console.port}"


def test_opens_and_closes(console: RawConsole) -> None:
    assert not console.is_open
    console.open()
    assert console.is_open
    console.close()
    assert not console.is_open


def test_closing_twice_is_harmless(console: RawConsole) -> None:
    console.open()
    console.close()
    console.close()


def test_opening_twice_is_refused(console: RawConsole) -> None:
    console.open()
    with pytest.raises(ConsoleError, match="already connected"):
        console.open()


def test_a_refused_connection_is_explained() -> None:
    # Port 1 on loopback has nothing listening and needs no privileges to try.
    console = RawConsole("127.0.0.1", 1, connect_timeout=1.0)
    with pytest.raises(ConsoleError, match="Could not connect to 127.0.0.1:1"):
        console.open()


def test_send_appends_a_crlf_by_default(
    console: RawConsole, server: FakeServer
) -> None:
    """The D2AFE console terminates on CRLF, not the newline a unix tool sends."""
    console.open()
    console.send_text("status")
    server.wait_for(b"status\r\n")


@pytest.mark.parametrize(
    ("ending", "expected"),
    [
        (LineEnding.CR, b"go\r"),
        (LineEnding.LF, b"go\n"),
        (LineEnding.CRLF, b"go\r\n"),
    ],
)
def test_send_honours_the_line_ending(
    console: RawConsole, server: FakeServer, ending: LineEnding, expected: bytes
) -> None:
    console.open()
    console.send_text("go", ending)
    server.wait_for(expected)


def test_sending_while_closed_is_refused(console: RawConsole) -> None:
    with pytest.raises(ConsoleError, match="not connected"):
        console.send_text("status")


def test_send_bytes_appends_nothing(console: RawConsole, server: FakeServer) -> None:
    """Raw bytes go exactly as given: the terminator is the caller's business."""
    console.open()
    console.send_bytes(b"\x02\x41\x03")
    server.wait_for(b"\x02\x41\x03")


def test_sending_bytes_while_closed_is_refused(console: RawConsole) -> None:
    with pytest.raises(ConsoleError, match="not connected"):
        console.send_bytes(b"\x00")


def test_read_returns_what_the_board_sent(
    console: RawConsole, server: FakeServer
) -> None:
    console.open()
    server.send(b"D2AFE> ")
    assert read_until(console) == b"D2AFE> "


def test_read_is_empty_handed_when_nothing_arrives(console: RawConsole) -> None:
    console.open()
    assert console.read() == b""


def test_read_reports_the_far_end_hanging_up(
    console: RawConsole, server: FakeServer
) -> None:
    console.open()
    server.hang_up()
    assert read_until(console) is None


def test_read_on_a_closed_console_is_the_end_of_the_stream(
    console: RawConsole,
) -> None:
    assert console.read() is None


def test_read_hands_back_raw_bytes(console: RawConsole, server: FakeServer) -> None:
    """Undecodable bytes reach the caller intact, for the hex view to show."""
    console.open()
    server.send(b"\xff\xfe\r\n")
    assert read_until(console) == b"\xff\xfe\r\n"


# ------------------------------------------------------------- TextDecoder


def test_a_character_split_across_chunks_is_not_mangled() -> None:
    """Bytes arrive when they arrive, so decoding must carry state over."""
    decoder = TextDecoder()
    degrees = "20 °C".encode()
    assert decoder.decode(degrees[:4]) == "20 "
    assert decoder.decode(degrees[4:]) == "°C"


def test_undecodable_bytes_do_not_break_the_stream() -> None:
    """Line noise on an RS485 bus must not take the console down with it."""
    assert TextDecoder().decode(b"\xff\xfe ok") == "�� ok"


def test_reset_forgets_a_half_finished_character() -> None:
    """Switching view mode strands a partial character; it must not resurface."""
    decoder = TextDecoder()
    assert decoder.decode("°".encode()[:1]) == ""
    decoder.reset()
    assert decoder.decode(b"ok") == "ok"


# ------------------------------------------------------------------- Hex


def test_format_hex_is_uppercase_and_spaced() -> None:
    assert format_hex(b"AB\r\n") == "41 42 0D 0A"


def test_format_hex_of_nothing_is_nothing() -> None:
    assert format_hex(b"") == ""


@pytest.mark.parametrize(
    "written",
    ["0d0a", "0D 0A", "0d 0a", "0x0d,0x0a", "\\x0d\\x0a", "0D-0A", "0D:0A"],
)
def test_parse_hex_accepts_how_people_write_it(written: str) -> None:
    """Hex gets copied out of datasheets, captures and code in every style."""
    assert parse_hex(written) == b"\r\n"


def test_parse_hex_round_trips_with_format_hex() -> None:
    assert parse_hex(format_hex(b"\x00\x7f\xff")) == b"\x00\x7f\xff"


def test_parse_hex_needs_something_to_parse() -> None:
    with pytest.raises(ValueError, match="Enter some hex bytes"):
        parse_hex("   ")


def test_parse_hex_names_the_characters_that_are_not_hex() -> None:
    with pytest.raises(ValueError, match="g z are not hex"):
        parse_hex("0g 0z")


def test_parse_hex_rejects_half_a_byte() -> None:
    with pytest.raises(ValueError, match="3 hex digits is not a whole number"):
        parse_hex("0d0")


def test_close_unblocks_a_read_in_another_thread(console: RawConsole) -> None:
    """The GUI must be able to disconnect without waiting on the read timeout."""
    console = RawConsole(console.ip, console.port, read_timeout=30.0)
    console.open()
    result: list[bytes | None] = []

    reader = threading.Thread(target=lambda: result.append(console.read()), daemon=True)
    reader.start()
    time.sleep(0.05)  # Let the reader get as far as blocking on the socket.
    console.close()

    reader.join(timeout=5.0)
    assert not reader.is_alive(), "close did not unblock the reader"
    assert result == [None]
