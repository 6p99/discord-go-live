#!/usr/bin/env python3
"""Discord Go Live streamer + viewer (single file). Documentation: see the .md file next to this script."""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import json
import os
import random
import select
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import websockets
except ImportError:
    websockets = None

try:
    import davey
except ImportError:
    davey = None


# --------------------------------------------------------------------------
# dependencies
# --------------------------------------------------------------------------

def _install_package(name: str) -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", name],
            capture_output=True, text=True, timeout=600,
        )
    except Exception:
        return False
    return result.returncode == 0


def _load_crypto() -> bool:
    global AESGCM
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return True
    except ImportError:
        return False


HAS_CRYPTO = _load_crypto()
if not HAS_CRYPTO:
    print("Installing missing dependency: cryptography ...")
    if _install_package("cryptography"):
        import importlib
        importlib.invalidate_caches()
        HAS_CRYPTO = _load_crypto()
    print("cryptography installed." if HAS_CRYPTO
          else "cryptography could not be installed, continuing without encrypted audio frames.")


def ensure_watcher_deps() -> None:
    """Import (and if needed install) websockets + davey for the watcher."""
    global websockets, davey
    import importlib

    missing = []
    if websockets is None:
        try:
            import websockets  # noqa: F401
        except ImportError:
            missing.append("websockets")
    if davey is None:
        try:
            import davey  # noqa: F401
        except ImportError:
            missing.append("davey")

    for package in missing:
        print(f"Installing missing dependency for the watcher: {package} ...")
        if not _install_package(package):
            raise RuntimeError(f"could not install {package} (run: pip install {package})")
        importlib.invalidate_caches()

    if websockets is None:
        import websockets  # noqa: F401
    if davey is None:
        import davey  # noqa: F401


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

GATEWAY_URL = "gateway.discord.gg"
STREAM_ROTATE_SEC = 4 * 3600
STREAM_REJOIN_GAP_SEC = 3.0
WATCHER_LEAVE_GRACE_SEC = 8.0

CLIENT_LOCALE = "en-GB"
DESKTOP_CLIENT_VERSION = "1.0.9253"
DESKTOP_BUILD_IDENTIFIER = 594031
PLATFORM_BUILD_IDENTIFIER = 88414
PLATFORM_OS_REVISION = "10.0.26200"
USER_AGENT_IDENTIFIER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) discord/1.0.9253 Chrome/148.0.7778.280 "
    "Electron/42.7.1 Safari/537.36"
)
USER_AGENT = USER_AGENT_IDENTIFIER

OPUS_SILENCE = b"\xf8\xff\xfe"

STATE_PATH = Path(__file__).resolve().with_name("stream_session.json")


def generate_fallback_properties() -> dict:
    return {
        "os": "Windows",
        "browser": "Discord Client",
        "release_channel": "stable",
        "client_version": DESKTOP_CLIENT_VERSION,
        "os_version": PLATFORM_OS_REVISION,
        "os_arch": "x64",
        "app_arch": "x64",
        "system_locale": CLIENT_LOCALE,
        "has_client_mods": False,
        "browser_user_agent": USER_AGENT_IDENTIFIER,
        "browser_version": "42.7.1",
        "os_sdk_version": "26200",
        "client_build_number": DESKTOP_BUILD_IDENTIFIER,
        "native_build_number": PLATFORM_BUILD_IDENTIFIER,
        "client_event_source": None,
        "client_app_state": "focused",
    }


def extract_account_snowflake(auth_token: str) -> str:
    try:
        prefix = auth_token.split(".", 1)[0]
        padding = "=" * (-len(prefix) % 4)
        return base64.b64decode(prefix + padding).decode("utf-8")
    except Exception:
        return ""


# --------------------------------------------------------------------------
# console
# --------------------------------------------------------------------------

class Console:
    RED = "\x1b[91m"
    ORANGE = "\x1b[38;2;237;203;64m"
    GREEN = "\x1b[92m"
    YELLOW = "\x1b[93m"
    CYAN = "\x1b[96m"
    WHITE = "\x1b[97m"
    GREY = "\x1b[90m"
    MAGENTA = "\x1b[95m"
    RESET = "\x1b[0m"
    RULE = "─" * 48

    def __init__(self) -> None:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        if sys.platform == "win32":
            self._enable_ansi()

    @staticmethod
    def _enable_ansi() -> None:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)

    def _stamp(self) -> str:
        return f"{self.GREY}{time.strftime('%H:%M:%S')}{self.RESET}"

    def _line(self, glyph: str, target: str, detail: str, color: str) -> None:
        print(f"  {self._stamp()} {glyph} {self.WHITE}{target}{self.RESET} {self.GREY}·{self.RESET} {color}{detail}{self.RESET}")

    def ok(self, target: str, detail: str) -> None:
        self._line(f"{self.GREEN}✓{self.RESET}", target, detail, self.GREEN)

    def live(self, target: str, detail: str) -> None:
        self._line(f"{self.MAGENTA}●{self.RESET}", target, detail, self.MAGENTA)

    def warn(self, target: str, detail: str) -> None:
        self._line(f"{self.YELLOW}!{self.RESET}", target, detail, self.YELLOW)

    def err(self, target: str, detail: str) -> None:
        self._line(f"{self.RED}✗{self.RESET}", target, detail, self.RED)

    def info(self, detail: str) -> None:
        print(f"  {self._stamp()} {self.GREY}·{self.RESET} {self.WHITE}{detail}{self.RESET}")

    def rule(self) -> None:
        print(f"  {self.GREY}{self.RULE}{self.RESET}")


CONSOLE = Console()


# --------------------------------------------------------------------------
# minimal websocket client
# --------------------------------------------------------------------------

class MiniWebSocket:

    def __init__(self, host: str, port: int = 443, path: str = "/") -> None:
        host_clean = host.replace("wss://", "").replace("https://", "").rstrip("/")
        if ":" in host_clean:
            h, p = host_clean.split(":", 1)
            self.host = h
            try:
                self.port = int(p)
            except ValueError:
                self.port = port
        else:
            self.host = host_clean
            self.port = port

        self.path = path
        self.sock: ssl.SSLSocket | None = None
        self.closed = True
        self._lock = threading.RLock()

    def connect(self) -> None:
        raw_sock = socket.create_connection((self.host, self.port), timeout=15)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw_sock, server_hostname=self.host)
        sec_key = base64.b64encode(os.urandom(16)).decode("ascii")

        host_hdr = self.host if self.port == 443 else f"{self.host}:{self.port}"
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {host_hdr}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: {USER_AGENT}\r\n\r\n"
        )
        self.sock.sendall(req.encode("utf-8"))

        header_data = b""
        while b"\r\n\r\n" not in header_data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("failed handshake")
            header_data += chunk

        first_line = header_data.split(b"\r\n")[0].decode("utf-8", "ignore")
        if "101" not in first_line:
            raise ConnectionError(f"handshake rejected: {first_line}")
        self.closed = False

    def send(self, data: str) -> None:
        with self._lock:
            if self.closed or not self.sock:
                raise ConnectionError("socket closed")
            try:
                payload = data.encode("utf-8")
                length = len(payload)
                mask_key = os.urandom(4)

                header = bytearray([0x81])
                if length <= 125:
                    header.append(0x80 | length)
                elif length <= 65535:
                    header.append(0x80 | 126)
                    header.extend(length.to_bytes(2, "big"))
                else:
                    header.append(0x80 | 127)
                    header.extend(length.to_bytes(8, "big"))

                header.extend(mask_key)
                masked = bytearray(payload[i] ^ mask_key[i % 4] for i in range(length))
                self.sock.sendall(header + masked)
            except Exception as e:
                self.close()
                raise ConnectionError(f"socket write error: {e}") from e

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("socket closed during read")
            buf.extend(chunk)
        return bytes(buf)

    def recv(self, timeout: float = 5.0) -> dict | None:
        if self.closed or not self.sock:
            return None
        try:
            r, _, _ = select.select([self.sock], [], [], timeout)
            if not r:
                return None
            head = self._recv_exact(2)
        except Exception:
            self.close()
            return None

        opcode = head[0] & 0x0F
        if opcode == 0x8:
            code = 0
            reason = ""
            try:
                c_len = head[1] & 0x7F
                if c_len >= 2:
                    p = self._recv_exact(c_len)
                    code = struct.unpack(">H", p[:2])[0]
                    reason = p[2:].decode("utf-8", "ignore")
            except Exception:
                pass
            self.close()
            return {"op": "close", "code": code, "reason": reason}
        if opcode == 0x9:
            try:
                with self._lock:
                    if self.sock and not self.closed:
                        pong = bytearray([0x8A, 0x80])
                        pong.extend(os.urandom(4))
                        self.sock.sendall(pong)
            except Exception:
                self.close()
            return {"op": "ping"}

        if opcode == 0xA:
            try:
                self._read_payload(head)
            except Exception:
                self.close()
            return {"op": "pong"}

        try:
            fin = bool(head[0] & 0x80)
            payload = self._read_payload(head)
            while not fin:
                nxt = self._recv_exact(2)
                chunk = self._read_payload(nxt)
                if (nxt[0] & 0x0F) in (0x8, 0x9, 0xA):
                    continue
                payload += chunk
                fin = bool(nxt[0] & 0x80)
            return json.loads(payload.decode("utf-8"))
        except Exception:
            self.close()
            return None

    def _read_payload(self, head: bytes) -> bytes:
        length = head[1] & 0x7F
        if length == 126:
            length = int.from_bytes(self._recv_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(self._recv_exact(8), "big")
        return self._recv_exact(length)

    def close(self) -> None:
        self.closed = True
        with self._lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None


# --------------------------------------------------------------------------
# voice connection
# --------------------------------------------------------------------------

class VoiceConnection:

    def __init__(
        self,
        endpoint: str,
        server_id: str,
        user_id: str,
        session_id: str,
        token: str,
        console: Console,
        label: str = "voice",
        is_stream: bool = False,
        stream_key: str = "",
    ) -> None:
        cleaned = endpoint.replace("wss://", "").replace("https://", "").rstrip("/")
        if ":" in cleaned:
            self.host, port_str = cleaned.split(":", 1)
            try:
                self.port = int(port_str)
            except ValueError:
                self.port = 443
        else:
            self.host = cleaned
            self.port = 443
        self.endpoint = f"{self.host}:{self.port}"
        self.server_id = server_id
        self.user_id = user_id
        self.session_id = session_id
        self.token = token
        self.console = console
        self.label = label
        self.is_stream = is_stream
        self.stream_key = stream_key

        self.ws: MiniWebSocket | None = None
        self.udp_sock: socket.socket | None = None
        self.ssrc: int = 0
        self.voice_ip: str = ""
        self.voice_port: int = 0
        self.external_ip: str = ""
        self.external_port: int = 0
        self.secret_key: bytes = b""
        self.encryption_mode: str = ""
        self.heartbeat_interval: float = 13.75
        self.running: bool = False
        self.nonce_counter: int = 0
        self._hb_thread: threading.Thread | None = None
        self._audio_thread: threading.Thread | None = None

    def connect(self) -> bool:
        C = self.console

        try:
            self.ws = MiniWebSocket(self.host, port=self.port, path="/?v=7")
            self.ws.connect()
        except Exception as exc:
            C.err(self.label, f"ws connect failed: {exc}")
            return False

        msg = self._recv_op(8, timeout=10)
        if not msg:
            C.err(self.label, "no Hello from voice server")
            return False
        self.heartbeat_interval = msg.get("d", {}).get("heartbeat_interval", 13750) / 1000.0

        identify_d: dict[str, Any] = {
            "server_id": self.server_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "token": self.token,
            "max_dave_protocol_version": 1,
        }
        if self.is_stream:
            if self.stream_key:
                identify_d["stream_key"] = self.stream_key
            identify_d["video"] = True
            identify_d["streams"] = [
                {"type": "screen", "rid": "100", "quality": 100}
            ]
        self.ws.send(json.dumps({"op": 0, "d": identify_d}))

        msg = self._recv_op(2, timeout=10)
        if not msg:
            C.err(self.label, "no Ready from voice server")
            return False

        d = msg.get("d", {})
        self.ssrc = d.get("ssrc", 0)
        self.voice_ip = d.get("ip", "")
        self.voice_port = d.get("port", 0)
        modes = d.get("modes", [])

        preferred = [
            "aead_aes256_gcm_rtpsize",
            "aead_xchacha20_poly1305_rtpsize",
            "xsalsa20_poly1305_lite",
            "xsalsa20_poly1305",
        ]
        for mode in preferred:
            if mode in modes:
                self.encryption_mode = mode
                break
        if not self.encryption_mode and modes:
            self.encryption_mode = modes[0]

        if not self._udp_discovery():
            C.warn(self.label, "UDP discovery failed, using fallback IP")
            self.external_ip = "127.0.0.1"
            self.external_port = random.randint(10000, 60000)

        self.ws.send(json.dumps({
            "op": 1,
            "d": {
                "protocol": "udp",
                "data": {
                    "address": self.external_ip,
                    "port": self.external_port,
                    "mode": self.encryption_mode,
                },
            },
        }))

        msg = self._recv_op(4, timeout=10)
        if msg:
            d = msg.get("d", {})
            self.secret_key = bytes(d.get("secret_key", []))
            self.encryption_mode = d.get("mode", self.encryption_mode)

        self.running = True
        return True

    def _recv_op(self, target_op: int, timeout: float = 10) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            msg = self.ws.recv(timeout=min(remaining, 2.0))
            if msg is None:
                if self.ws.closed:
                    return None
                continue
            if msg.get("op") == "close":
                C = self.console
                code = msg.get("code")
                reason = msg.get("reason")
                C.err(self.label, f"voice server closed: {code} ({reason})")
                return None
            if msg.get("op") == target_op:
                return msg
            if msg.get("op") == 3:
                self._send_heartbeat()
        return None

    def _send_heartbeat(self) -> None:
        if self.ws and not self.ws.closed:
            try:
                nonce = int(time.time() * 1000)
                self.ws.send(json.dumps({"op": 3, "d": nonce}))
            except Exception:
                pass

    def _udp_discovery(self) -> bool:
        try:
            self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_sock.settimeout(5)

            packet = bytearray(74)
            struct.pack_into(">HHI", packet, 0, 0x0001, 70, self.ssrc)

            self.udp_sock.sendto(bytes(packet), (self.voice_ip, self.voice_port))

            data, _ = self.udp_sock.recvfrom(256)
            if len(data) < 74:
                return False

            ip_start = 8
            ip_end = data.index(0, ip_start)
            self.external_ip = data[ip_start:ip_end].decode("utf-8")
            self.external_port = struct.unpack_from(">H", data, 72)[0]

            return bool(self.external_ip)
        except Exception:
            if self.udp_sock:
                try:
                    self.udp_sock.close()
                except Exception:
                    pass
                self.udp_sock = None
            return False

    def start_heartbeat(self) -> None:
        def _loop():
            while self.running and self.ws and not self.ws.closed:
                self._send_heartbeat()
                try:
                    msg = self.ws.recv(timeout=0.5)
                    if msg and msg.get("op") == 3:
                        self._send_heartbeat()
                except Exception:
                    pass
                time.sleep(max(1.0, self.heartbeat_interval - 0.5))

        self._hb_thread = threading.Thread(target=_loop, daemon=True)
        self._hb_thread.start()

    def send_speaking(self, speaking: bool = True) -> None:
        if self.ws and not self.ws.closed:
            try:
                self.ws.send(json.dumps({
                    "op": 5,
                    "d": {
                        "speaking": 1 if speaking else 0,
                        "delay": 0,
                        "ssrc": self.ssrc,
                    },
                }))
            except Exception:
                pass

    def start_silence_pump(self) -> None:
        if not self.udp_sock or not self.secret_key:
            return

        def _pump():
            seq = 0
            ts = 0
            while self.running:
                try:
                    if HAS_CRYPTO and "aes256_gcm" in self.encryption_mode:
                        self._send_encrypted_silence_gcm(seq, ts)
                    else:
                        keepalive = struct.pack(">I", seq) + b"\xc9"
                        try:
                            self.udp_sock.sendto(keepalive, (self.voice_ip, self.voice_port))
                        except Exception:
                            pass
                    seq = (seq + 1) & 0xFFFF
                    ts = (ts + 960) & 0xFFFFFFFF
                    time.sleep(1.0)
                except Exception:
                    break

        self._audio_thread = threading.Thread(target=_pump, daemon=True)
        self._audio_thread.start()

    def _send_encrypted_silence_gcm(self, seq: int, ts: int) -> None:
        if not self.udp_sock or not self.secret_key:
            return
        try:
            rtp_header = struct.pack(
                ">BBHII",
                0x80,
                0x78,
                seq & 0xFFFF,
                ts & 0xFFFFFFFF,
                self.ssrc,
            )

            self.nonce_counter += 1
            nonce = struct.pack(">I", self.nonce_counter) + b"\x00" * 8

            cipher = AESGCM(self.secret_key[:32])
            encrypted = cipher.encrypt(nonce, OPUS_SILENCE, rtp_header)

            packet = rtp_header + encrypted + struct.pack(">I", self.nonce_counter)
            self.udp_sock.sendto(packet, (self.voice_ip, self.voice_port))
        except Exception:
            pass

    def close(self) -> None:
        self.running = False
        if self.udp_sock:
            try:
                self.udp_sock.close()
            except Exception:
                pass
        if self.ws:
            self.ws.close()


# --------------------------------------------------------------------------
# small state / context objects
# --------------------------------------------------------------------------

@dataclass
class StreamSession:
    """Only what the engine needs from AccountSessionState."""
    token: str = ""
    total_stream_hours_claimed: float = 0.0
    total_stream_sessions: int = 0

    def save(self) -> None:
        try:
            existing = {}
            if STATE_PATH.exists():
                existing = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            existing.update(asdict(self))
            STATE_PATH.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        except Exception:
            pass


@dataclass(frozen=True)
class ClientContext:
    """Only what the engine needs from RuntimeClientContext."""
    props_dict: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# gateway stream engine
# --------------------------------------------------------------------------

class GatewayStreamEngine:

    def __init__(self, state: StreamSession, session: ClientContext, console: Console) -> None:
        self.state = state
        self.session = session
        self.console = console
        self.ws: MiniWebSocket | None = None
        self.voice_conn: VoiceConnection | None = None
        self.stream_conn: VoiceConnection | None = None
        self.running = False
        self.heartbeat_interval = 41.25
        self.sequence: int | None = None
        self.start_time = 0.0

        self.gateway_session_id = ""
        self.user_id = ""
        self.stream_key = ""

        self.voice_session_id = ""
        self.voice_token = ""
        self.voice_endpoint = ""
        self.voice_connected = False

        self.stream_token = ""
        self.stream_endpoint = ""
        self.stream_connected = False
        self.stream_ever_active = False
        self._stream_recreate_attempts = 0
        self._user_left_vc = False
        self._rotating = False
        self._segment_start = 0.0
        self.on_live = None
        self.on_offline = None
        self._finalized = False

        self._status_thread: threading.Thread | None = None

    def start_stream(self, guild_id: str, channel_id: str, max_seconds: float = 0.0) -> None:
        C = self.console
        C.rule()
        C.info(f"{C.MAGENTA}Headless Gateway Live Streamer{C.WHITE}")
        C.rule()
        C.info("connecting to discord gateway (wss://gateway.discord.gg)...")

        self.ws = MiniWebSocket(GATEWAY_URL, path="/?v=9&encoding=json")
        try:
            self.ws.connect()
        except Exception as exc:
            C.err("gateway", f"connection failed: {exc}")
            C.rule()
            return

        C.ok("gateway", "connected")
        self.running = True
        self._finalized = False
        self.start_time = time.time()
        self.user_id = extract_account_snowflake(self.state.token) or ""
        self.stream_ever_active = False
        self._stream_recreate_attempts = 0
        self._user_left_vc = False
        self._rotating = False
        self._segment_start = 0.0

        event_thread = threading.Thread(
            target=self._event_loop,
            args=(guild_id, channel_id),
            daemon=True,
        )
        event_thread.start()

        self._status_thread = threading.Thread(
            target=self._status_display,
            daemon=True,
        )
        self._status_thread.start()

        print()
        C.info(f"{C.GREY}press Ctrl+C at any time to stop streaming...{C.RESET}")
        deadline = (self.start_time + max_seconds) if max_seconds > 0 else 0.0
        if deadline:
            C.info(f"{C.GREY}auto stop after {max_seconds / 3600.0:g}h{C.RESET}")
        C.rule()
        try:
            while self.running:
                if deadline and time.time() >= deadline:
                    C.info("requested stream time reached, stopping...")
                    break
                time.sleep(0.2)
        except KeyboardInterrupt:
            C.info("Ctrl+C received, stopping...")

        C.info("leaving the stream and the voice channel first (streaming account)...")
        self._cleanup(guild_id)

    def _status_display(self) -> None:
        while self.running:
            time.sleep(60)
            if not self.running:
                break
            elapsed = time.time() - self.start_time
            hours = elapsed / 3600.0
            mins = int(elapsed / 60)
            if self.stream_connected:
                status = f"{self.console.MAGENTA}🔴 LIVE{self.console.RESET}"
            elif self.voice_connected:
                status = f"{self.console.CYAN}⏳ starting stream{self.console.RESET}"
            else:
                status = f"{self.console.YELLOW}⏳ reconnecting{self.console.RESET}"
            self.console.live(
                "stream",
                f"{status} · {mins}m ({hours:.2f}h) elapsed"
            )

    def _reconnect(self, guild_id: str, channel_id: str, can_resume: bool = True) -> bool:
        C = self.console
        if not self.running:
            return False

        self.stream_connected = False
        self.voice_connected = False

        if self.stream_conn:
            try:
                self.stream_conn.close()
            except Exception:
                pass
            self.stream_conn = None

        if self.voice_conn:
            try:
                self.voice_conn.close()
            except Exception:
                pass
            self.voice_conn = None

        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

        # The streaming account is now fully disconnected (sockets closed), so it
        # always leaves before the watching account does.
        if self.on_offline:
            try:
                self.on_offline()
            except Exception:
                pass

        attempts = 0
        backoff = 3
        while self.running:
            attempts += 1
            C.warn("gateway", f"reconnecting to gateway in {backoff}s (attempt {attempts})...")
            for _ in range(backoff * 10):
                if not self.running:
                    return False
                time.sleep(0.1)

            try:
                self.ws = MiniWebSocket(GATEWAY_URL, path="/?v=9&encoding=json")
                self.ws.connect()

                hello_msg = None
                for _ in range(15):
                    m = self.ws.recv(timeout=1.0)
                    if isinstance(m, dict) and m.get("op") == "close":
                        code = m.get("code")
                        reason = m.get("reason", "")
                        if code == 4004:
                            C.err("auth", "fatal gateway authentication failure (code 4004: invalid token)")
                            C.err("auth", "pass a fresh token with --token or delete stream_session.json.")
                            self.running = False
                            return False
                    if m and m.get("op") == 10:
                        hello_msg = m
                        break
                    time.sleep(0.1)

                if not hello_msg:
                    C.warn("gateway", "did not receive hello from gateway, retrying...")
                    self.ws.close()
                    self.ws = None
                    backoff = min(15, backoff + 2)
                    continue

                d_hello = hello_msg.get("d", {})
                self.heartbeat_interval = d_hello.get("heartbeat_interval", 41250) / 1000.0

                self.ws.send(json.dumps({"op": 1, "d": self.sequence}))

                resumed = False
                if can_resume and self.gateway_session_id and self.sequence is not None:
                    C.info("attempting to resume gateway session (op 6)...")
                    try:
                        self.ws.send(json.dumps({
                            "op": 6,
                            "d": {
                                "token": self.state.token,
                                "session_id": self.gateway_session_id,
                                "seq": self.sequence,
                            },
                        }))

                        res_msg = self.ws.recv(timeout=4.0)
                        if res_msg:
                            op_res = res_msg.get("op")
                            t_res = res_msg.get("t")
                            if op_res == 0 and t_res == "RESUMED":
                                C.ok("gateway", "session resumed successfully")
                                resumed = True
                            elif op_res == 9:
                                C.warn("gateway", "resume failed (invalid session), falling back to identify")
                                time.sleep(1.0)
                            elif op_res == 0 and t_res == "READY":
                                self._handle_dispatch("READY", res_msg.get("d", {}), guild_id, channel_id)
                                resumed = True
                    except Exception:
                        resumed = False

                if not resumed:
                    C.info("sending identify (op 2)...")
                    self.ws.send(json.dumps({
                        "op": 2,
                        "d": {
                            "token": self.state.token,
                            "capabilities": 16381,
                            "properties": self.session.props_dict,
                            "presence": {
                                "status": "online",
                                "since": 0,
                                "activities": [],
                                "afk": False,
                            },
                        },
                    }))

                    test_msg = self.ws.recv(timeout=2.0)
                    if isinstance(test_msg, dict) and test_msg.get("op") == "close":
                        code = test_msg.get("code")
                        if code == 4004:
                            C.err("auth", "fatal gateway authentication failure (code 4004: invalid token)")
                            C.err("auth", "pass a fresh token with --token or delete stream_session.json.")
                            self.running = False
                            return False
                    elif isinstance(test_msg, dict) and test_msg.get("op") == 0 and test_msg.get("t") == "READY":
                        self._handle_dispatch("READY", test_msg.get("d", {}), guild_id, channel_id)
                        resumed = True

                if resumed:
                    self.ws.send(json.dumps({
                        "op": 4,
                        "d": {
                            "guild_id": guild_id,
                            "channel_id": channel_id,
                            "self_mute": False,
                            "self_deaf": False,
                            "self_video": False,
                        },
                    }))
                    C.ok("voice", f"re-joining voice channel {C.ORANGE}{channel_id}{C.RESET}")

                C.ok("gateway", "gateway reconnected successfully")
                return True

            except Exception as exc:
                C.warn("gateway", f"reconnect attempt {attempts} failed: {exc}")
                if self.ws:
                    self.ws.close()
                    self.ws = None
                backoff = min(15, backoff + 2)

        return False

    def _event_loop(self, guild_id: str, channel_id: str) -> None:
        last_hb = time.time()

        while self.running:
            try:
                if not self.ws or self.ws.closed:
                    if not self._reconnect(guild_id, channel_id):
                        break
                    last_hb = time.time()
                    continue

                now = time.time()

                if now - last_hb > self.heartbeat_interval:
                    if self.ws and not self.ws.closed:
                        try:
                            self.ws.send(json.dumps({"op": 1, "d": self.sequence}))
                            last_hb = now
                        except Exception as exc:
                            self.console.warn("gateway", f"heartbeat failed ({exc})")
                            if not self._reconnect(guild_id, channel_id):
                                break
                            last_hb = time.time()
                            continue

                if (self.stream_connected and not self._rotating and self._segment_start
                        and now - self._segment_start >= STREAM_ROTATE_SEC):
                    self._rotate_stream(guild_id, channel_id)
                    last_hb = time.time()
                    continue

                msg = self.ws.recv(timeout=1.0)
                if not msg:
                    continue

                if isinstance(msg, dict) and msg.get("op") == "close":
                    code = msg.get("code")
                    reason = msg.get("reason", "")
                    self.console.warn("gateway", f"gateway closed connection (code {code}: {reason})")
                    if code == 4004:
                        self.console.err("auth", "fatal: gateway authentication failed (code 4004: invalid token)")
                        self.console.err("auth", "pass a fresh token with --token or delete stream_session.json.")
                        self.running = False
                        break
                    elif code in (4010, 4011, 4012, 4013, 4014):
                        self.console.err("gateway", f"fatal gateway error (code {code}: {reason}) — stopping.")
                        self.running = False
                        break

                    if not self._reconnect(guild_id, channel_id):
                        break
                    last_hb = time.time()
                    continue

                op = msg.get("op")
                seq = msg.get("s")
                t = msg.get("t")
                d = msg.get("d") or {}

                if seq is not None:
                    self.sequence = seq

                if op == 10:
                    self.heartbeat_interval = d.get("heartbeat_interval", 41250) / 1000.0
                    last_hb = time.time()

                    self.ws.send(json.dumps({"op": 1, "d": self.sequence}))

                    self.ws.send(json.dumps({
                        "op": 2,
                        "d": {
                            "token": self.state.token,
                            "capabilities": 16381,
                            "properties": self.session.props_dict,
                            "presence": {
                                "status": "online",
                                "since": 0,
                                "activities": [],
                                "afk": False,
                            },
                        },
                    }))

                elif op == 1:
                    self.ws.send(json.dumps({"op": 1, "d": self.sequence}))
                    last_hb = now

                elif op == 11:
                    pass

                elif op == 0:
                    if t == "RESUMED":
                        self.console.ok("gateway", "gateway session resumed successfully")
                        self.ws.send(json.dumps({
                            "op": 4,
                            "d": {
                                "guild_id": guild_id,
                                "channel_id": channel_id,
                                "self_mute": False,
                                "self_deaf": False,
                                "self_video": False,
                            },
                        }))
                    else:
                        self._handle_dispatch(t, d, guild_id, channel_id)

                elif op == 7:
                    self.console.warn("gateway", "discord requested gateway reconnect (op 7)")
                    if not self._reconnect(guild_id, channel_id, can_resume=True):
                        break
                    last_hb = time.time()

                elif op == 9:
                    resumable = bool(d)
                    self.console.warn("gateway", f"session invalidated (op 9, resumable={resumable})")
                    if not self._reconnect(guild_id, channel_id, can_resume=resumable):
                        break
                    last_hb = time.time()

            except Exception as exc:
                if not self.running:
                    break
                self.console.warn("gateway", f"connection dropped: {exc}")
                if not self._reconnect(guild_id, channel_id):
                    break
                last_hb = time.time()

    def _rotate_stream(self, guild_id: str, channel_id: str) -> None:
        C = self.console
        C.warn("stream", "4h reached, leaving and rejoining before Discord's 5h limit...")
        self._rotating = True
        self._segment_start = time.time()
        self.stream_connected = False
        self.voice_connected = False
        if self.stream_key:
            self.ws.send(json.dumps({"op": 19, "d": {"stream_key": self.stream_key}}))
        self.ws.send(json.dumps({
            "op": 4,
            "d": {"guild_id": guild_id, "channel_id": None, "self_mute": False, "self_deaf": False, "self_video": False},
        }))
        for conn in (self.stream_conn, self.voice_conn):
            if conn:
                conn.close()
        self.stream_conn = None
        self.voice_conn = None
        time.sleep(STREAM_REJOIN_GAP_SEC)
        self.ws.send(json.dumps({
            "op": 4,
            "d": {"guild_id": guild_id, "channel_id": channel_id, "self_mute": False, "self_deaf": False, "self_video": False},
        }))
        C.ok("voice", f"rejoining voice channel {C.ORANGE}{channel_id}{C.RESET}")

    def _handle_dispatch(self, event_type: str, d: dict, guild_id: str, channel_id: str) -> None:
        C = self.console

        if event_type == "READY":
            self.gateway_session_id = d.get("session_id", "")
            user_data = d.get("user", {})
            username = user_data.get("username", "?")
            self.user_id = str(user_data.get("id", self.user_id))
            C.ok("auth", f"gateway authenticated ({username})")

            self.ws.send(json.dumps({
                "op": 4,
                "d": {
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "self_mute": False,
                    "self_deaf": False,
                    "self_video": False,
                },
            }))
            C.ok("voice", f"joining voice channel {C.ORANGE}{channel_id}{C.RESET}")

        elif event_type == "VOICE_STATE_UPDATE":
            if str(d.get("user_id")) == str(self.user_id):
                active_channel = d.get("channel_id")
                new_session = d.get("session_id", "")
                if new_session:
                    self.voice_session_id = new_session

                if self._rotating:
                    if active_channel is None:
                        return
                    if str(active_channel) == str(channel_id):
                        self._rotating = False

                if (self.voice_connected or self.stream_connected or self.stream_ever_active):
                    if active_channel is None:
                        C.warn("voice", "user manually disconnected from voice channel in Discord app")
                        C.info("gracefully stopping stream engine and saving accumulated stream hours...")
                        self._user_left_vc = True
                        self.voice_connected = False
                        self.stream_connected = False
                        self.running = False
                        return

                    if str(active_channel) != str(channel_id):
                        C.warn("voice", f"user moved to another voice channel ({active_channel}) in Discord app")
                        C.info("stopping stream on old channel and saving accumulated stream hours...")
                        self._user_left_vc = True
                        self.voice_connected = False
                        self.stream_connected = False
                        self.running = False
                        return

                C.ok("voice", f"voice state updated (session: {self.voice_session_id[:8]}...)")

        elif event_type == "VOICE_SERVER_UPDATE":
            token = d.get("token", "")
            endpoint = d.get("endpoint") or ""
            v_guild = d.get("guild_id", guild_id)

            if not endpoint or not token:
                C.err("voice", "empty voice server endpoint/token")
                return

            if self.voice_connected:
                C.warn("voice", "voice server update received, re-establishing voice connection...")
                if self.voice_conn:
                    self.voice_conn.close()
                self.voice_connected = False

            self.voice_token = token
            self.voice_endpoint = endpoint
            C.ok("voice", f"voice server: {endpoint}")

            session_id = self.voice_session_id or self.gateway_session_id
            self.voice_conn = VoiceConnection(
                endpoint=self.voice_endpoint,
                server_id=v_guild,
                user_id=self.user_id,
                session_id=session_id,
                token=self.voice_token,
                console=C,
                label="voice",
                is_stream=False,
            )

            C.info("performing voice WebSocket handshake...")
            if self.voice_conn.connect():
                self.voice_conn.start_heartbeat()
                self.voice_conn.send_speaking(False)
                self.voice_conn.start_silence_pump()
                self.voice_connected = True
                C.ok("voice", f"{C.GREEN}voice connection established (ssrc={self.voice_conn.ssrc}){C.RESET}")

                time.sleep(0.5)
                C.info("requesting Go Live stream (op 18 Stream Create)...")
                try:
                    self.ws.send(json.dumps({
                        "op": 18,
                        "d": {
                            "type": "guild",
                            "guild_id": guild_id,
                            "channel_id": channel_id,
                            "preferred_region": None,
                        },
                    }))
                except Exception as exc:
                    C.warn("stream", f"stream create send error: {exc}")
            else:
                C.err("voice", "voice WebSocket handshake FAILED, retrying in 3s...")
                def _retry_voice():
                    time.sleep(3.0)
                    if self.running and self.ws and not self.ws.closed:
                        try:
                            self.ws.send(json.dumps({
                                "op": 4,
                                "d": {
                                    "guild_id": guild_id,
                                    "channel_id": channel_id,
                                    "self_mute": False,
                                    "self_deaf": False,
                                    "self_video": False,
                                },
                            }))
                        except Exception:
                            pass
                threading.Thread(target=_retry_voice, daemon=True).start()

        elif event_type == "STREAM_CREATE":
            self.stream_key = d.get("stream_key", "")
            C.ok("stream", f"Go Live created! stream_key: {C.CYAN}{self.stream_key}{C.RESET}")
            first_live = not self.stream_connected
            self.stream_connected = True
            self.stream_ever_active = True
            self._stream_recreate_attempts = 0
            self._rotating = False
            self._segment_start = time.time()
            if first_live and self.on_live:
                try:
                    self.on_live()
                except Exception as exc:
                    C.warn("watcher", f"could not start the watcher: {exc}")

            if self.voice_conn and self.voice_conn.ws and not self.voice_conn.ws.closed:
                try:
                    video_ssrc = self.voice_conn.ssrc + 1
                    rtx_ssrc = self.voice_conn.ssrc + 2
                    self.voice_conn.ws.send(json.dumps({
                        "op": 12,
                        "d": {
                            "audio_ssrc": self.voice_conn.ssrc,
                            "video_ssrc": video_ssrc,
                            "rtx_ssrc": rtx_ssrc,
                            "streams": [
                                {
                                    "type": "screen",
                                    "rid": "100",
                                    "ssrc": video_ssrc,
                                    "rtx_ssrc": rtx_ssrc,
                                    "active": True,
                                    "quality": 100,
                                    "max_bitrate": 8000000,
                                    "max_framerate": 30,
                                    "max_resolution": {"type": "fixed", "width": 1920, "height": 1080},
                                }
                            ],
                        },
                    }))
                    self.voice_conn.send_speaking(True)
                except Exception:
                    pass

            print()
            C.rule()
            C.live("stream", f"{C.MAGENTA}● BROADCASTING LIVE{C.RESET}, discord shows your account as {C.MAGENTA}LIVE{C.RESET}")
            C.rule()
            print(f"  {C.YELLOW}⚠️  CRITICAL REQUIREMENTS FOR STREAM TIME TO ACCUMULATE:{C.RESET}")
            print(f"  {C.ORANGE}1.{C.RESET} {C.WHITE}An alt account (or friend) {C.YELLOW}MUST be in the voice channel{C.RESET}{C.WHITE} sitting or watching.{C.RESET}")
            print(f"     {C.GREY}› Discord will NOT count streaming hours if you stream alone in an empty channel!{C.RESET}")
            print(f"  {C.ORANGE}2.{C.RESET} {C.WHITE}Ensure {C.YELLOW}'Use data to personalize my Discord experience'{C.RESET}{C.WHITE} is turned {C.GREEN}ON{C.RESET}{C.WHITE}.{C.RESET}")
            print(f"     {C.GREY}› Check in Discord: User Settings (⚙) → Data & Privacy → Personalization.{C.RESET}")
            C.rule()
            if HAS_CRYPTO and self.voice_conn and self.voice_conn.secret_key:
                C.ok("audio", "encrypted silence pump active (cryptography lib found)")
            else:
                C.info(f"{C.GREY}tip: pip install cryptography (enables encrypted audio frames){C.RESET}")

        elif event_type == "STREAM_SERVER_UPDATE":
            token = d.get("token", "")
            endpoint = d.get("endpoint") or ""
            if endpoint and token:
                self.stream_token = token
                self.stream_endpoint = endpoint
                C.ok("stream", f"stream media route: {endpoint}")

        elif event_type == "STREAM_DELETE":
            self.stream_connected = False
            if self.stream_conn:
                self.stream_conn.close()
                self.stream_conn = None

            if not self.running or not self.voice_connected or self._user_left_vc or self._rotating:
                return

            if self._stream_recreate_attempts < 3:
                self._stream_recreate_attempts += 1
                C.warn("stream", f"stream stopped by server, re-requesting Go Live in 2s (attempt {self._stream_recreate_attempts}/3)...")
                def _recreate():
                    time.sleep(2.0)
                    if self.running and self.ws and not self.ws.closed and self.voice_connected and not self._user_left_vc:
                        try:
                            self.ws.send(json.dumps({
                                "op": 18,
                                "d": {
                                    "type": "guild",
                                    "guild_id": guild_id,
                                    "channel_id": channel_id,
                                    "preferred_region": None,
                                },
                            }))
                        except Exception:
                            pass
                threading.Thread(target=_recreate, daemon=True).start()
            else:
                C.err("stream", "stream stopped repeatedly by server — stopping session to prevent rate limits.")
                self.running = False

        elif event_type == "VOICE_CONNECTION_STATUS":
            status = d.get("state", "")
            if status:
                C.info(f"voice connection status: {status}")

    def _cleanup(self, guild_id: str) -> None:
        C = self.console
        if self._finalized:
            return
        self._finalized = True
        self.running = False

        if self.ws and not self.ws.closed and self.stream_key:
            try:
                self.ws.send(json.dumps({
                    "op": 19,
                    "d": {"stream_key": self.stream_key},
                }))
            except Exception:
                pass

        if self.ws and not self.ws.closed and not self._user_left_vc:
            try:
                self.ws.send(json.dumps({
                    "op": 4,
                    "d": {
                        "guild_id": guild_id,
                        "channel_id": None,
                        "self_mute": False,
                        "self_deaf": False,
                        "self_video": False,
                    },
                }))
            except Exception:
                pass

        if self.stream_conn:
            self.stream_conn.close()
        if self.voice_conn:
            self.voice_conn.close()
        if self.ws:
            time.sleep(0.3)
            self.ws.close()

        if (self.stream_connected or self.stream_ever_active) and self.start_time > 0:
            elapsed = time.time() - self.start_time
            hours = elapsed / 3600.0
            if elapsed >= 10:
                self.state.total_stream_hours_claimed += hours
                self.state.total_stream_sessions += 1
                self.state.save()
                print()
                C.ok("stream", f"ended · {elapsed:.0f}s ({hours:.2f}h) streamed live")
                C.info(f"{C.GREY}note: badge progression typically syncs within 24-48 hours.{C.RESET}")
        else:
            print()
            C.info("stream session ended.")

        C.rule()


# ==========================================================================
# WATCHER (viewer account)
# ==========================================================================

WATCH_GATEWAY_URL = "wss://gateway.discord.gg/?v=9&encoding=json"
ORIGIN = "https://discord.com"
LOCATE_WAIT_SECONDS = 15
STEP_TIMEOUT = 15
TEARDOWN_PAUSE = 1.5
STABLE_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 8
MAX_SESSION_SECONDS = 9.5 * 3600

CAPABILITIES = 16381
CLIENT_PROPERTIES = {
    "os": "Windows",
    "browser": "Chrome",
    "device": "",
    "system_locale": "en-US",
    "browser_user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "browser_version": "124.0.0.0",
    "os_version": "10",
    "referrer": "",
    "referring_domain": "",
    "release_channel": "stable",
    "client_build_number": 0,
    "client_event_source": None,
}

GATEWAY_CLOSE_CODES = {
    4004: "authentication failed (bad or expired token)",
    4003: "not authenticated",
    4002: "payload not understood",
    4005: "already authenticated",
    4007: "invalid sequence",
    4008: "rate limited",
    4009: "session timed out",
    4010: "invalid shard",
    4011: "sharding required",
    4012: "invalid API version",
    4013: "invalid intents",
    4014: "disallowed intents",
}
FATAL_GATEWAY_CODES = {4004, 4010, 4011, 4012, 4013, 4014}

VOICE_CLOSE_CODES = {
    4001: "unknown opcode",
    4002: "failed to decode payload",
    4003: "not authenticated",
    4004: "authentication failed",
    4005: "already authenticated",
    4006: "session no longer valid",
    4009: "session timeout",
    4011: "server not found",
    4012: "unknown protocol",
    4014: "disconnected",
    4015: "voice server crashed",
    4016: "unknown encryption mode",
    4017: "end-to-end encryption (DAVE) required",
    4020: "bad request",
    4021: "rate limited",
    4022: "call terminated",
}

MODES = ("aead_xchacha20_poly1305_rtpsize", "aead_aes256_gcm_rtpsize")
SECRET_KEYS = {"token", "secret_key"}
QUIET_EVENTS = {"READY", "GUILD_CREATE", "READY_SUPPLEMENTAL", "SESSIONS_REPLACE"}


def clear_watch_token() -> None:
    try:
        existing = {}
        if STATE_PATH.exists():
            existing = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if existing.pop("watch_token", None) is not None:
            STATE_PATH.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except Exception:
        pass


class DebugLog:
    """Writes a redacted trace to a file. Secrets never reach the file."""

    def __init__(self):
        self.fh = None
        self.path = None
        self.secrets = set()

    def open(self, path):
        self.path = os.path.abspath(path)
        try:
            self.fh = open(self.path, "w", encoding="utf-8")
        except OSError:
            self.fh = None
        self.write("env", f"python {getattr(sys, 'version', '?')}, "
                          f"websockets {getattr(websockets, '__version__', '?')}, "
                          f"davey {getattr(davey, '__version__', '?')}")

    def add_secret(self, value):
        if value and len(str(value)) > 6:
            self.secrets.add(str(value))

    def clean_text(self, text):
        for secret in self.secrets:
            text = text.replace(secret, "<redacted>")
        return text

    def redact(self, obj):
        if isinstance(obj, dict):
            out = {}
            for key, value in obj.items():
                if key in SECRET_KEYS:
                    self.add_secret(value if isinstance(value, str) else None)
                    out[key] = "<redacted>"
                else:
                    out[key] = self.redact(value)
            return out
        if isinstance(obj, list):
            return [self.redact(v) for v in obj]
        return obj

    def write(self, kind, text):
        if not self.fh:
            return
        try:
            self.fh.write(f"{time.strftime('%H:%M:%S')} {kind:<8} {self.clean_text(text)}\n")
            self.fh.flush()
        except (OSError, ValueError):
            pass

    def frame(self, kind, direction, obj):
        try:
            text = json.dumps(self.redact(obj), separators=(",", ":"))
        except (TypeError, ValueError):
            text = repr(obj)
        if len(text) > 1500:
            text = text[:1500] + "...(cut)"
        self.write(kind, f"{direction} {text}")


DBG = DebugLog()


def wlog(msg):
    CONSOLE.info(f"{CONSOLE.GREY}watch{CONSOLE.RESET} {msg}")
    DBG.write("log", msg)


def close_code(exc):
    return getattr(getattr(exc, "rcvd", None), "code", None) or getattr(exc, "code", None)


def ask_digits(prompt, allow_blank=False):
    while True:
        try:
            raw = input(prompt).strip()
        except EOFError:
            return ""
        if not raw and allow_blank:
            return ""
        if raw.isdigit():
            return raw
        print("Enter a numeric ID.")


def async_input(prompt):
    """input() in a daemon thread so Ctrl+C is never blocked by it."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def worker():
        try:
            value = input(prompt)
        except BaseException as exc:
            loop.call_soon_threadsafe(lambda e=exc: fut.done() or fut.set_exception(e))
            return
        loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(value))

    threading.Thread(target=worker, daemon=True).start()
    return fut


async def wait_any(events, timeout):
    tasks = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
    return [e for e in events if e.is_set()]


async def wait_all(events, timeout):
    deadline = time.time() + timeout
    for e in events:
        remaining = deadline - time.time()
        if remaining <= 0:
            return False
        try:
            await asyncio.wait_for(e.wait(), remaining)
        except asyncio.TimeoutError:
            return False
    return True


class VoiceLink:
    def __init__(self, label, endpoint, server_id, user_id, session_id, token,
                 group_id, keypair, video=False, keep_port=True):
        self.keep_port = keep_port
        self.label = label
        self.endpoint = endpoint
        self.server_id = server_id
        self.user_id = user_id
        self.session_id = session_id
        self.token = token
        self.group_id = group_id
        self.keypair = keypair
        self.video = video

        DBG.add_secret(token)
        self.ws = None
        self.task = None
        self.seq_ack = -1
        self.ssrc = None
        self.mode = None
        self.sock = None
        self.bg = []
        self.last_ack = time.time()

        self.ready = asyncio.Event()
        self.dave_ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.closed_code = None
        self.closed_reason = ""

        self.dave = None
        self.dave_version = 0
        self.pending = {}

    def say(self, msg):
        wlog(f"[{self.label}] {msg}")

    def spawn(self, coro):
        task = asyncio.create_task(coro)

        def done(t):
            if not t.cancelled() and t.exception():
                self.say(f"background task error: {t.exception()!r}")

        task.add_done_callback(done)
        self.bg.append(task)
        return task

    async def send_json(self, op, data):
        DBG.frame(self.label, ">", {"op": op, "d": data})
        await self.ws.send(json.dumps({"op": op, "d": data}))

    async def send_binary(self, op, payload):
        DBG.write(self.label, f"> bin op={op} len={len(payload)}")
        await self.ws.send(bytes([op]) + payload)

    def url(self):
        host, _, port = self.endpoint.partition(":")
        if self.keep_port and port and port != "80":
            return f"wss://{host}:{port}/?v=8"
        return f"wss://{host}/?v=8"

    async def run(self):
        try:
            self.say(f"connecting to {self.url().split('/?')[0]}")
            async with websockets.connect(self.url(), max_size=None) as ws:
                self.ws = ws
                await self.identify()
                async for raw in ws:
                    if isinstance(raw, (bytes, bytearray)):
                        await self.on_binary(bytes(raw))
                    else:
                        await self.on_text(json.loads(raw))
                self.closed_code = getattr(ws, "close_code", None)
        except websockets.ConnectionClosed as exc:
            self.closed_code = close_code(exc)
        except Exception as exc:
            self.closed_reason = repr(exc)
        finally:
            for t in self.bg:
                t.cancel()
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass
            meaning = VOICE_CLOSE_CODES.get(self.closed_code, "")
            if self.closed_reason:
                self.say(f"connection failed: {self.closed_reason}")
            else:
                self.say(f"closed: {self.closed_code} {meaning}".rstrip())
            self.closed.set()

    async def identify(self):
        data = {
            "server_id": str(self.server_id),
            "user_id": str(self.user_id),
            "session_id": self.session_id,
            "token": self.token,
            "max_dave_protocol_version": davey.DAVE_PROTOCOL_VERSION,
        }
        if self.video:
            data["video"] = True
            data["streams"] = [{"type": "screen", "rid": "100", "quality": 100}]
        await self.send_json(0, data)

    async def heartbeat(self, interval):
        while True:
            if time.time() - self.last_ack > max(interval * 4, 20):
                self.say("no heartbeat reply, dropping the connection")
                await self.ws.close(code=4000)
                return
            await self.send_json(3, {"t": int(time.time() * 1000), "seq_ack": self.seq_ack})
            await asyncio.sleep(interval)

    async def udp_keepalive(self):
        counter = 0
        while True:
            await asyncio.sleep(5)
            counter += 1
            try:
                self.sock.send(struct.pack(">Q", counter))
            except OSError:
                pass


    async def on_text(self, msg):
        DBG.frame(self.label, "<", msg)
        op = msg.get("op")
        data = msg.get("d") or {}
        if "seq" in msg:
            self.seq_ack = msg["seq"]
        if op == 8:
            interval = min(data.get("heartbeat_interval", 5000) / 1000.0, 5.0)
            self.last_ack = time.time()
            self.spawn(self.heartbeat(interval))
        elif op == 6:
            self.last_ack = time.time()
        elif op == 2:
            await self.on_ready(data)
        elif op == 4:
            await self.on_session(data)
        elif self.dave is not None:
            if op == 21:
                await self.dave_prepare_transition(data)
            elif op == 22:
                self.dave_execute(data.get("transition_id"))
            elif op == 24:
                await self.dave_prepare_epoch(data)

    def discover_blocking(self, ip, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5)
        sock.connect((ip, port))
        packet = bytearray(74)
        struct.pack_into(">H", packet, 0, 1)
        struct.pack_into(">H", packet, 2, 70)
        struct.pack_into(">I", packet, 4, self.ssrc)
        for _ in range(3):
            sock.send(bytes(packet))
            try:
                reply = sock.recv(2048)
            except socket.timeout:
                continue
            if len(reply) == 74 and reply[1] == 2:
                my_ip = reply[8:reply.index(0, 8)].decode("ascii")
                my_port = struct.unpack_from(">H", reply, 72)[0]
                sock.setblocking(False)
                return sock, my_ip, my_port
        sock.close()
        raise socket.timeout("no reply to UDP discovery")

    async def on_ready(self, data):
        self.ssrc = data["ssrc"]
        modes = data.get("modes", [])
        mode = next((m for m in MODES if m in modes), modes[0] if modes else MODES[0])
        loop = asyncio.get_running_loop()
        try:
            self.sock, my_ip, my_port = await loop.run_in_executor(
                None, self.discover_blocking, data["ip"], data["port"])
        except (OSError, ValueError) as exc:
            self.say(f"UDP handshake failed ({exc}). Your firewall or network may block UDP.")
            return
        await self.send_json(1, {
            "protocol": "udp",
            "address": my_ip,
            "port": my_port,
            "mode": mode,
            "data": {"address": my_ip, "port": my_port, "mode": mode},
            "codecs": [
                {"name": "opus", "type": "audio", "priority": 1000, "payload_type": 120},
                {"name": "H264", "type": "video", "priority": 1000,
                 "payload_type": 101, "rtx_payload_type": 102},
            ],
            "rtc_connection_id": str(uuid.uuid4()),
            "experiments": [],
        })
        self.spawn(self.udp_keepalive())

    async def on_session(self, data):
        self.mode = data.get("mode")
        self.dave_version = data.get("dave_protocol_version", 0)
        self.ready.set()
        self.say(f"connected, mode {self.mode}, DAVE v{self.dave_version}")
        if self.dave_version > 0:
            await self.reinit_dave()
        else:
            self.dave_ready.set()


    async def reinit_dave(self):
        if self.dave_version > 0:
            uid, gid = int(self.user_id), int(self.group_id)
            if self.dave is None:
                self.dave = davey.DaveSession(self.dave_version, uid, gid, self.keypair)
            else:
                self.dave.reinit(self.dave_version, uid, gid, self.keypair)
            await self.send_binary(26, self.dave.get_serialized_key_package())
            self.say("sent MLS key package")
        elif self.dave is not None:
            self.dave.reset()
            self.dave.set_passthrough_mode(True, 10)

    def check_dave_ready(self):
        if self.dave is not None and self.dave.ready and not self.dave_ready.is_set():
            self.dave_ready.set()
            self.say("end-to-end encryption established")

    async def dave_prepare_transition(self, data):
        tid = data.get("transition_id")
        self.pending[tid] = data.get("protocol_version", 0)
        if tid == 0:
            self.dave_execute(0)
        else:
            if self.pending[tid] == 0:
                self.dave.set_passthrough_mode(True, 120)
            await self.send_json(23, {"transition_id": tid})

    def dave_execute(self, tid):
        if tid not in self.pending:
            return
        old = self.dave_version
        self.dave_version = self.pending.pop(tid)
        if old != self.dave_version and self.dave_version == 0:
            self.say("call downgraded to non-E2EE")
            self.dave_ready.set()
        self.check_dave_ready()

    async def dave_prepare_epoch(self, data):
        if data.get("epoch") == 1:
            self.dave_version = data.get("protocol_version", self.dave_version)
            await self.reinit_dave()

    async def apply_mls(self, fn, payload, tid, what):
        try:
            fn(payload)
        except Exception as exc:
            self.say(f"MLS {what} failed ({exc}), asking for a fresh start")
            await self.send_json(31, {"transition_id": tid})
            await self.reinit_dave()
            return
        self.say(f"MLS {what} processed")
        if tid != 0:
            self.pending[tid] = self.dave_version
            await self.send_json(23, {"transition_id": tid})
        self.check_dave_ready()

    async def on_binary(self, msg):
        if len(msg) < 3:
            return
        self.seq_ack = struct.unpack_from(">H", msg, 0)[0]
        op = msg[2]
        DBG.write(self.label, f"< bin op={op} len={len(msg)}")
        if self.dave is None:
            return
        try:
            if op == 25:
                self.dave.set_external_sender(msg[3:])
            elif op == 27:
                kind = (davey.ProposalsOperationType.append if msg[3] == 0
                        else davey.ProposalsOperationType.revoke)
                result = self.dave.process_proposals(kind, msg[4:])
                if isinstance(result, davey.CommitWelcome):
                    body = result.commit + result.welcome if result.welcome else result.commit
                    await self.send_binary(28, body)
            elif op == 29:
                tid = struct.unpack_from(">H", msg, 3)[0]
                await self.apply_mls(self.dave.process_commit, msg[5:], tid, "commit")
            elif op == 30:
                tid = struct.unpack_from(">H", msg, 3)[0]
                await self.apply_mls(self.dave.process_welcome, msg[5:], tid, "welcome")
        except Exception as exc:
            self.say(f"DAVE opcode {op} failed: {exc}")


class StreamWatcher:
    def __init__(self, token, target_id, max_attempts=DEFAULT_MAX_ATTEMPTS, preset=None):
        self.token = token
        DBG.add_secret(token)
        self.target_id = target_id
        self.max_attempts = max_attempts
        self.keypair = davey.generate_p256_keypair()

        self.ws = None
        self.seq = None
        self.me = None
        self.session_id = None
        self.resume_url = None
        self.acked = True
        self.connected = asyncio.Event()
        self.fatal = False
        self.ready_count = 0
        self.locator_started = False

        self.target_loc = preset
        self.target_live = bool(preset)
        self.live_event = asyncio.Event()
        if preset:
            self.live_event.set()
        self.voice_session_id = None

        self.main_idx = 0
        self.stream_idx = 0
        self.history = []
        self.supervisor = None
        self.tasks = []
        self.stream_ready = asyncio.Event()

        self.reset_attempt_state()

    def reset_attempt_state(self):
        self.joined_channel = None
        self.joined_event = asyncio.Event()
        self.voice_server = None
        self.voice_server_event = asyncio.Event()
        self.stream_key = None
        self.rtc_server_id = None
        self.rtc_channel_id = None
        self.stream_token = None
        self.stream_endpoint = None
        self.stream_create_event = asyncio.Event()
        self.stream_server_event = asyncio.Event()
        self.stream_deleted = asyncio.Event()
        self.stream_delete_reason = None
        self.main_link = None
        self.stream_link = None

    def spawn(self, coro):
        task = asyncio.create_task(coro)

        def done(t):
            if not t.cancelled() and t.exception():
                wlog(f"Background task error: {t.exception()!r}")
                DBG.write("trace", "".join(traceback.format_exception(
                    type(t.exception()), t.exception(), t.exception().__traceback__)))

        task.add_done_callback(done)
        self.tasks.append(task)
        return task


    async def send_raw(self, op, data):
        DBG.frame("gateway", ">", {"op": op, "d": data})
        await self.ws.send(json.dumps({"op": op, "d": data}))

    async def send(self, op, data):
        await asyncio.wait_for(self.connected.wait(), 30)
        await self.send_raw(op, data)

    async def try_send(self, op, data, timeout=4):
        try:
            await asyncio.wait_for(self.send(op, data), timeout)
        except Exception:
            pass

    async def heartbeat_loop(self, ws, interval):
        await asyncio.sleep(interval * random.random())
        while True:
            if not self.acked:
                wlog("Gateway stopped answering, reconnecting.")
                await ws.close(code=4000)
                return
            self.acked = False
            await ws.send(json.dumps({"op": 1, "d": self.seq}))
            await asyncio.sleep(interval)

    async def identify(self):
        await self.send_raw(2, {
            "token": self.token,
            "capabilities": CAPABILITIES,
            "properties": CLIENT_PROPERTIES,
            "presence": {"status": "online", "since": 0, "activities": [], "afk": False},
            "compress": False,
            "client_state": {"guild_versions": {}},
        })

    async def run(self):
        failures = 0
        try:
            while not self.fatal:
                try:
                    outcome = await self.connect_once()
                    failures = 0
                except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as exc:
                    failures += 1
                    outcome = "network error"
                    wlog(f"Gateway connection error: {exc!r}")
                if self.fatal:
                    break
                if failures >= 8:
                    wlog("Could not reach Discord after 8 tries. Check your internet connection.")
                    break
                delay = min(2 ** failures, 30) if failures else 1
                wlog(f"Reconnecting to the gateway in {delay}s ({outcome}).")
                await asyncio.sleep(delay)
        finally:
            await self.cleanup()

    async def connect_once(self):
        resuming = bool(self.session_id and self.seq is not None)
        url = self.resume_url or WATCH_GATEWAY_URL
        hb = None
        async with websockets.connect(url, max_size=None, origin=ORIGIN) as ws:
            self.ws = ws
            self.acked = True
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    op = msg.get("op")
                    if msg.get("s") is not None:
                        self.seq = msg["s"]
                    if op == 10:
                        interval = msg["d"]["heartbeat_interval"] / 1000.0
                        hb = asyncio.create_task(self.heartbeat_loop(ws, interval))
                        if resuming:
                            wlog("Resuming the gateway session.")
                            await self.send_raw(6, {"token": self.token,
                                                    "session_id": self.session_id,
                                                    "seq": self.seq})
                        else:
                            await self.identify()
                    elif op == 11:
                        self.acked = True
                    elif op == 1:
                        await ws.send(json.dumps({"op": 1, "d": self.seq}))
                    elif op == 7:
                        wlog("Gateway asked for a reconnect.")
                        return "reconnect requested"
                    elif op == 9:
                        if not msg.get("d"):
                            self.session_id = self.seq = self.resume_url = None
                        await asyncio.sleep(1 + random.random() * 4)
                        return "invalid session"
                    elif op == 0:
                        await self.dispatch(msg.get("t"), msg.get("d") or {})
                return "closed"
            except websockets.ConnectionClosed as exc:
                code = close_code(exc)
                wlog(f"Gateway closed the connection: {code} "
                     f"{GATEWAY_CLOSE_CODES.get(code, '')}".rstrip())
                if code in FATAL_GATEWAY_CODES:
                    self.fatal = True
                    if code == 4004:
                        clear_watch_token()
                        wlog("Saved watcher token removed. Start again with --watch-token.")
                elif code in (4007, 4009):
                    self.session_id = self.seq = self.resume_url = None
                return f"closed {code}"
            finally:
                if hb:
                    hb.cancel()
                self.connected.clear()
                self.ws = None

    async def cleanup(self):
        await self.teardown(pause=0)
        for t in self.tasks:
            t.cancel()


    async def dispatch(self, name, d):
        if name in QUIET_EVENTS:
            DBG.write("gateway", f"< {name} (omitted)")
        else:
            DBG.frame("gateway", "<", {"t": name, "d": d})

        if name == "READY":
            first = self.ready_count == 0
            self.me = d["user"]["id"]
            self.session_id = d.get("session_id")
            base = d.get("resume_gateway_url")
            self.resume_url = f"{base}/?v=9&encoding=json" if base else None
            wlog(f"Logged in as {d['user'].get('username', '?')} ({self.me})")
            if self.me == self.target_id:
                wlog("The target user ID is the same as this account. Use the OTHER account's ID.")
                self.fatal = True
                await self.ws.close()
                return
            self.ready_count += 1
            if not first:
                await self.restart_after_new_session()
            self.connected.set()
            for guild in d.get("guilds", []):
                self.scan_guild(guild)
            self.start_supervisor()
            if not self.locator_started:
                self.locator_started = True
                self.spawn(self.locator())
        elif name == "RESUMED":
            self.connected.set()
            wlog("Gateway session resumed.")
        elif name == "GUILD_CREATE":
            self.scan_guild(d)
        elif name == "VOICE_STATE_UPDATE":
            self.on_voice_state(d)
        elif name == "VOICE_SERVER_UPDATE":
            if (self.joined_channel and d.get("guild_id") == self.joined_channel[0]
                    and d.get("endpoint")):
                DBG.add_secret(d.get("token"))
                self.voice_server = d
                self.voice_server_event.set()
        elif name == "STREAM_CREATE":
            if d.get("stream_key") == self.stream_key:
                self.rtc_server_id = d.get("rtc_server_id")
                self.rtc_channel_id = d.get("rtc_channel_id")
                wlog(f"Stream found, region {d.get('region', '?')} "
                     f"(fields: {', '.join(sorted(d.keys()))})")
                self.stream_create_event.set()
        elif name == "STREAM_SERVER_UPDATE":
            if d.get("stream_key") == self.stream_key:
                DBG.add_secret(d.get("token"))
                self.stream_token = d.get("token")
                self.stream_endpoint = d.get("endpoint")
                self.stream_server_event.set()
        elif name == "STREAM_DELETE":
            if d.get("stream_key") == self.stream_key:
                self.stream_delete_reason = d.get("reason")
                wlog(f"Stream deleted by Discord (reason: {self.stream_delete_reason}).")
                self.stream_deleted.set()

    async def restart_after_new_session(self):
        wlog("Discord started a fresh session, restarting the watch.")
        if self.supervisor:
            self.supervisor.cancel()
            self.supervisor = None
        self.reset_attempt_state()
        self.voice_session_id = None

    def scan_guild(self, guild):
        if guild.get("unavailable"):
            return
        for vs in guild.get("voice_states", []):
            if vs.get("user_id") == self.target_id:
                self.note_target(guild.get("id"), vs.get("channel_id"), bool(vs.get("self_stream")))

    def note_target(self, guild_id, channel_id, live):
        if not channel_id:
            if self.target_loc:
                wlog("Target left voice.")
            self.target_loc = None
            self.target_live = False
            self.live_event.clear()
            return
        first = self.target_loc is None
        self.target_loc = (guild_id, channel_id)
        self.target_live = live
        if first:
            wlog(f"Target is in voice channel {channel_id} (guild {guild_id}), "
                 f"{'streaming' if live else 'not streaming yet'}.")
        if live:
            self.live_event.set()
        else:
            self.live_event.clear()

    def on_voice_state(self, d):
        uid = d.get("user_id")
        if uid == self.target_id:
            self.note_target(d.get("guild_id"), d.get("channel_id"), bool(d.get("self_stream")))
        elif uid == self.me:
            if d.get("session_id"):
                self.voice_session_id = d["session_id"]
            if (d.get("channel_id") and self.joined_channel
                    and d["channel_id"] == self.joined_channel[1]):
                self.joined_event.set()

    async def locator(self):
        await asyncio.sleep(LOCATE_WAIT_SECONDS)
        if self.target_loc:
            return
        wlog("Could not find that user in any voice channel the watcher account can see.")
        try:
            guild_id = (await async_input("Guild ID: ")).strip()
            if not guild_id:
                wlog("Waiting for the target to appear in voice.")
                return
            channel_id = (await async_input("Voice channel ID: ")).strip()
        except EOFError:
            return
        if not (guild_id.isdigit() and channel_id.isdigit()):
            wlog("Those IDs are not numeric. Waiting for the target to appear in voice.")
            return
        self.target_loc = (guild_id, channel_id)
        self.target_live = True
        self.live_event.set()


    def start_supervisor(self):
        if self.supervisor is None or self.supervisor.done():
            self.supervisor = self.spawn(self.supervise())

    def session_candidates(self):
        values = []
        for value in (self.voice_session_id, self.session_id):
            if value and value not in values:
                values.append(value)
        return values

    def stream_strategy(self):
        sids = self.session_candidates()
        sid_names = {}
        for name, value in (("voice", self.voice_session_id), ("gateway", self.session_id)):
            if value:
                sid_names.setdefault(value, name)
        rtc = self.rtc_channel_id or self.rtc_server_id
        minus_one = None
        if self.rtc_server_id and str(self.rtc_server_id).isdigit():
            minus_one = str(int(self.rtc_server_id) - 1)
        groups = []
        for name, value in (("rtc_server-1", minus_one), ("rtc", rtc),
                            ("channel", self.joined_channel[1]),
                            ("rtc_server", self.rtc_server_id)):
            if value and value not in [g[1] for g in groups]:
                groups.append((name, value))
        port = str(self.stream_endpoint or "").partition(":")[2]
        port_options = [True, False] if port and port not in ("80", "443") else [True]
        combos = []
        for video in (True, False):
            for gname, gval in groups:
                for sid in sids:
                    for keep in port_options:
                        combos.append({
                            "session_id": sid, "group_id": gval, "video": video, "keep_port": keep,
                            "desc": (f"session={sid_names[sid]}, group={gname}, video={video}, "
                                     f"port={'kept' if keep else 'stripped'}")})
        return combos[self.stream_idx % len(combos)]

    async def supervise(self):
        attempts = 0
        while True:
            await self.live_event.wait()
            if not self.target_loc:
                self.live_event.clear()
                continue
            attempts += 1
            guild_id, channel_id = self.target_loc
            started = time.time()
            try:
                outcome = await self.attempt(attempts, guild_id, channel_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                DBG.write("trace", traceback.format_exc())
                outcome = ("failed", f"unexpected error {exc!r}", None, None)
            held = time.time() - started
            await self.teardown()

            if outcome[0] in ("ended", "cycle"):
                attempts = 0
                if outcome[0] == "ended":
                    wlog("Stream ended. Waiting for the target to go live again.")
                    self.live_event.clear()
                continue

            _, reason, which, code = outcome
            self.history.append(f"attempt {attempts}: {reason}")
            if held >= STABLE_SECONDS:
                attempts = 1
            elif which == "voice":
                self.main_idx += 1
            elif which == "stream":
                self.stream_idx += 1
            if attempts >= self.max_attempts:
                await self.give_up()
                return
            delay = min(2 * attempts, 15)
            wlog(f"Attempt failed ({reason}). Retrying in {delay}s with another approach.")
            await asyncio.sleep(delay)

    async def give_up(self):
        wlog(f"Giving up after {self.max_attempts} attempts. What happened:")
        for line in self.history[-self.max_attempts:]:
            wlog(f"  {line}")
        if DBG.path:
            wlog(f"Debug log (tokens removed): {DBG.path}")
        self.fatal = True
        if self.ws:
            await self.ws.close()

    async def attempt(self, n, guild_id, channel_id):
        session_deadline = time.time() + MAX_SESSION_SECONDS
        self.reset_attempt_state()
        self.joined_channel = (guild_id, channel_id)
        wlog(f"Attempt {n}/{self.max_attempts}: joining voice channel (muted).")
        await self.send(4, {"guild_id": guild_id, "channel_id": channel_id,
                            "self_mute": True, "self_deaf": False, "self_video": False})
        if not await wait_all([self.joined_event, self.voice_server_event], STEP_TIMEOUT):
            return ("failed", "Discord never returned voice server details "
                              "(no permission to connect, or channel full?)", "join", None)

        candidates = self.session_candidates()
        DBG.write("ids", f"voice_session={self.voice_session_id} gateway_session={self.session_id}")
        main_sid = candidates[self.main_idx % len(candidates)]
        sid_label = "voice" if main_sid == self.voice_session_id else "gateway"
        vs = self.voice_server
        link = VoiceLink("voice", vs["endpoint"], guild_id, self.me, main_sid,
                         vs["token"], channel_id, self.keypair)
        self.main_link = link
        link.task = self.spawn(link.run())
        wlog(f"Main voice connection using {sid_label} session id.")
        await wait_any([link.ready, link.closed], STEP_TIMEOUT)
        if link.closed.is_set():
            return ("failed", f"main voice closed {link.closed_code} "
                              f"{VOICE_CLOSE_CODES.get(link.closed_code, '')}".strip(),
                    "voice", link.closed_code)
        if not link.ready.is_set():
            return ("failed", "main voice connection never completed", "voice", None)
        await wait_any([link.dave_ready, link.closed], STEP_TIMEOUT)
        if link.closed.is_set():
            return ("failed", f"main voice closed {link.closed_code} "
                              f"{VOICE_CLOSE_CODES.get(link.closed_code, '')}".strip(),
                    "voice", link.closed_code)
        if not link.dave_ready.is_set():
            wlog("Voice encryption not confirmed yet, continuing anyway.")

        self.stream_key = f"guild:{guild_id}:{channel_id}:{self.target_id}"
        wlog("Requesting to watch the stream.")
        await self.send(20, {"stream_key": self.stream_key})
        deadline = time.time() + STEP_TIMEOUT
        while True:
            if self.stream_deleted.is_set():
                return ("failed", f"Discord refused the stream request "
                                  f"({self.stream_delete_reason})", "request", None)
            if self.stream_create_event.is_set() and self.stream_server_event.is_set():
                break
            if time.time() > deadline:
                return ("failed", "no stream details received", "request", None)
            if link.closed.is_set():
                return ("failed", f"main voice closed {link.closed_code}", "voice",
                        link.closed_code)
            await asyncio.sleep(0.1)

        strat = self.stream_strategy()
        wlog(f"Stream connection: {strat['desc']}")
        slink = VoiceLink("stream", self.stream_endpoint, self.rtc_server_id, self.me,
                          strat["session_id"], self.stream_token, strat["group_id"],
                          self.keypair, video=strat["video"], keep_port=strat["keep_port"])
        self.stream_link = slink
        slink.task = self.spawn(slink.run())
        await wait_any([slink.ready, slink.closed], STEP_TIMEOUT)
        if slink.closed.is_set() or not slink.ready.is_set():
            code = slink.closed_code
            return ("failed", f"stream closed {code} {VOICE_CLOSE_CODES.get(code, '')} "
                              f"[{strat['desc']}]".replace("  ", " "), "stream", code)
        await wait_any([slink.dave_ready, slink.closed], STEP_TIMEOUT)
        if slink.closed.is_set():
            code = slink.closed_code
            return ("failed", f"stream closed {code} {VOICE_CLOSE_CODES.get(code, '')} "
                              f"[{strat['desc']}]".replace("  ", " "), "stream", code)
        if not slink.dave_ready.is_set():
            wlog("Stream encryption not confirmed yet, staying connected.")
        self.stream_ready.set()
        wlog("Watching the stream. Press Ctrl+C to stop.")

        remaining = session_deadline - time.time()
        await wait_any([slink.closed, link.closed, self.stream_deleted], max(remaining, 0))
        if self.stream_deleted.is_set():
            return ("ended",)
        if slink.closed.is_set():
            return ("failed", f"stream connection dropped ({slink.closed_code})",
                    "stream", slink.closed_code)
        if link.closed.is_set():
            return ("failed", f"voice connection dropped ({link.closed_code})",
                    "voice", link.closed_code)
        wlog(f"Session hit the {MAX_SESSION_SECONDS / 3600:g}h cap, cycling the connection.")
        return ("cycle",)

    async def teardown(self, pause=TEARDOWN_PAUSE):
        if self.stream_key:
            await self.try_send(19, {"stream_key": self.stream_key})
        if self.joined_channel:
            await self.try_send(4, {"guild_id": self.joined_channel[0], "channel_id": None,
                                    "self_mute": True, "self_deaf": False, "self_video": False})
        for link in (self.stream_link, self.main_link):
            if link is None:
                continue
            if link.ws is not None:
                try:
                    await link.ws.close()
                except Exception:
                    pass
            if getattr(link, "task", None):
                link.task.cancel()
        if pause and (self.stream_key or self.joined_channel):
            await asyncio.sleep(pause)
        self.reset_attempt_state()


# --------------------------------------------------------------------------
# watcher thread (runs the asyncio watcher next to the streamer)
# --------------------------------------------------------------------------

class WatcherRunner:
    """Runs StreamWatcher on its own thread + event loop, started on go-live.

    It can be started again after being stopped (for example when the gateway
    reconnects and the stream comes back), and stop() never holds the lock
    while joining, so it can never deadlock against the watcher thread.
    """

    def __init__(self, token: str, target_id: str, guild_id: str, channel_id: str,
                 log_path: str = "watcher_debug.log") -> None:
        self.token = token
        self.target_id = target_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.log_path = log_path

        self._lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None
        self.stop_event = threading.Event()
        self.started = False
        self.restarts = 0
        self.max_restarts = 5

    def start(self) -> bool:
        if not self.token or not self.target_id:
            return False
        with self._lock:
            if self.started:
                return False
            self.started = True
            self.stop_event = threading.Event()
            self.restarts = 0
            self.thread = threading.Thread(
                target=self._thread_main,
                args=(self.stop_event,),
                name="watcher",
                daemon=True,
            )
            thread = self.thread
        thread.start()
        return True

    def _finish(self) -> None:
        with self._lock:
            self.loop = None
            self.task = None
            self.thread = None
            self.started = False

    def _thread_main(self, stop_event: threading.Event) -> None:
        try:
            ensure_watcher_deps()
        except Exception as exc:
            CONSOLE.err("watcher", f"{exc}")
            self._finish()
            return

        DBG.open(self.log_path)
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main(stop_event))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            CONSOLE.err("watcher", f"stopped: {exc!r}")
            DBG.write("trace", traceback.format_exc())
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            self._finish()
            CONSOLE.info(f"{CONSOLE.GREY}watcher stopped{CONSOLE.RESET}")

    async def _main(self, stop_event: threading.Event) -> None:
        self.task = asyncio.current_task()
        while not stop_event.is_set():
            watcher = StreamWatcher(
                self.token, self.target_id,
                preset=(self.guild_id, self.channel_id),
            )
            await watcher.run()
            if stop_event.is_set() or watcher.fatal:
                break
            self.restarts += 1
            if self.restarts > self.max_restarts:
                CONSOLE.err("watcher", "gave up after repeated restarts")
                break
            wlog(f"Watcher exited, restarting ({self.restarts}/{self.max_restarts}) ...")
            await asyncio.sleep(5)

    def stop(self, wait: float = 8.0, quiet: bool = False) -> None:
        """Leave the channel and stop the watcher thread. Never holds the lock."""
        with self._lock:
            self.stop_event.set()
            loop, task, thread = self.loop, self.task, self.thread
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except Exception:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=wait)
            if thread.is_alive() and not quiet:
                CONSOLE.warn("watcher", "it did not close in time, but the streamer already left")


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

_CONSOLE_HOOK = None


def install_console_exit_hook(callback) -> None:
    """Windows only: also run the ordered shutdown when the console window is
    closed, or when Windows logs off / shuts down."""
    global _CONSOLE_HOOK
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def _handler(event: int) -> bool:
            if event in (2, 5, 6):  # close window, logoff, shutdown
                try:
                    callback()
                except Exception:
                    pass
                return True
            return False

        _CONSOLE_HOOK = handler_type(_handler)
        if not ctypes.windll.kernel32.SetConsoleCtrlHandler(_CONSOLE_HOOK, True):
            _CONSOLE_HOOK = None
    except Exception:
        _CONSOLE_HOOK = None


def load_saved() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def remember(key: str, value: str) -> None:
    try:
        existing = load_saved()
        existing[key] = value
        STATE_PATH.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        try:
            os.chmod(STATE_PATH, 0o600)
        except OSError:
            pass
    except Exception as exc:
        print(f"Could not save to stream_session.json ({exc}).")


def looks_like_token(token: str) -> bool:
    return len(token) >= 50 and token.count(".") >= 2 and " " not in token


def ask_token(label: str, flag: str, env: str, key: str, reset: bool = False) -> str:
    token = "" if reset else os.environ.get(env, "").strip()
    if not token and not reset:
        token = str(load_saved().get(key, "")).strip()
        if token:
            CONSOLE.info(f"{label} token loaded from stream_session.json")
    for _ in range(3):
        if not token:
            try:
                token = getpass.getpass(
                    f"{label} token (hidden, saved to stream_session.json): ").strip()
            except EOFError:
                return ""
            token = token.strip('"').strip("'")
            if not token:
                return ""
            remember(key, token)
        if looks_like_token(token):
            return token
        CONSOLE.warn(label.lower(), f"that does not look like a user token "
                                    f"(needs 3 dot-separated parts, {flag} to pass it)")
        token = ""
    return ""


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


# --------------------------------------------------------------------------
# stream duration menu
# --------------------------------------------------------------------------

STREAM_TIERS = [
    (1, "Newcomer", 1),
    (2, "Fledgling", 5),
    (3, "Breakout", 20),
    (4, "Standout", 75),
    (5, "Trendsetter", 150),
    (6, "Headliner", 300),
    (7, "Star", 500),
    (8, "Sensation", 1000),
    (9, "Visionary", 2000),
    (10, "Phenomenon", 5000),
]


def _fmt_hours(value: float) -> str:
    return f"{value:,.0f}h" if float(value).is_integer() else f"{value:,g}h"


def read_key() -> str:
    """Arrow-key reader (Windows), with w/s/j/k fallbacks."""
    try:
        import msvcrt
    except ImportError:
        return ""

    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "none")
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\x03":
        raise KeyboardInterrupt
    low = ch.lower()
    if low in ("w", "k"):
        return "up"
    if low in ("s", "j"):
        return "down"
    return "none"


def arrow_menu(title: str, rows: list[tuple[str, str, str]], start: int = 0) -> int:
    """Interactive up/down menu. Returns the selected index."""
    C = CONSOLE
    if not sys.stdout.isatty() or os.name != "nt":
        return _number_menu(title, rows, start)

    left_w = max(len(r[0]) for r in rows) + 2
    mid_w = max(len(r[1]) for r in rows) + 2

    print(f"  {C.WHITE}{title}{C.RESET}")
    print(f"  {C.GREY}↑/↓ move · enter select{C.RESET}")

    idx = start
    drawn = 0
    while True:
        lines = []
        for i, (left, mid, right) in enumerate(rows):
            selected = i == idx
            arrow = "›" if selected else " "
            body = f"{left:<{left_w}}{mid:>{mid_w}}{right:>10}"
            color = C.WHITE if selected else C.GREY
            lines.append(f"  {C.ORANGE}{arrow}{C.RESET} {color}○ {body}{C.RESET}")

        if drawn:
            sys.stdout.write(f"\x1b[{drawn}A")
        for line in lines:
            sys.stdout.write("\x1b[2K" + line + "\n")
        sys.stdout.flush()
        drawn = len(lines)

        key = read_key()
        if key == "up":
            idx = (idx - 1) % len(rows)
        elif key == "down":
            idx = (idx + 1) % len(rows)
        elif key == "enter":
            return idx


def _number_menu(title: str, rows: list[tuple[str, str, str]], start: int = 0) -> int:
    print(f"  {title}")
    for i, (left, mid, right) in enumerate(rows):
        mark = "›" if i == start else " "
        print(f"  {mark} {i + 1:>2}) {left:<28}{mid:>9}{right:>10}")
    while True:
        raw = ask("  number: ")
        if raw.isdigit() and 1 <= int(raw) <= len(rows):
            return int(raw) - 1


def choose_stream_hours(already_claimed: float) -> float:
    """Shows the tier menu and returns hours to stream (0 = until Ctrl+C)."""
    rows = []
    for number, name, hours in STREAM_TIERS:
        delta = max(0.0, float(hours) - already_claimed)
        rows.append((f"Tier {number:>2} · {name}", _fmt_hours(hours), f"+{delta:,.1f}h"))
    rows.append(("Custom hours...", "", ""))

    try:
        choice = arrow_menu("Stream to which tier?", rows)
    except EOFError:
        return 0.0

    if choice < len(STREAM_TIERS):
        return float(STREAM_TIERS[choice][2])

    while True:
        raw = ask("  Hours to stream (0 = until Ctrl+C): ").replace(",", "")
        if not raw:
            return 0.0
        try:
            value = float(raw)
        except ValueError:
            print("  enter a number, for example 3 or 2.5")
            continue
        if value < 0:
            print("  cannot be negative")
            continue
        return value


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Complete standalone Discord Go Live system: streamer + watcher account.")
    ap.add_argument("--token", help="streaming account token (else DISCORD_TOKEN / saved / prompt)")
    ap.add_argument("--watch-token", help="watching account token (else DISCORD_WATCH_TOKEN / saved / prompt)")
    ap.add_argument("--guild", help="server (guild) ID")
    ap.add_argument("--channel", help="voice channel ID")
    ap.add_argument("--hours", type=float, default=None,
                    help="skip the menu and stream N hours (0 = until Ctrl+C)")
    ap.add_argument("--no-watch", action="store_true", help="stream only, do not start the watcher")
    ap.add_argument("--reset-tokens", action="store_true", help="forget the saved tokens and ask again")
    ap.add_argument("--log", default="watcher_debug.log", help="watcher debug log path")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    C = CONSOLE

    C.rule()
    C.info(f"{C.MAGENTA}Discord Go Live{C.WHITE}  ·  streamer + watcher")
    C.rule()

    token = (args.token or "").strip()
    if not token:
        token = ask_token("Streaming", "--token", "DISCORD_TOKEN", "token", args.reset_tokens)
    elif not looks_like_token(token):
        C.warn("token", "this does not look like a user token (needs 3 dot-separated parts)")
    if not token:
        C.err("token", "no streaming token provided")
        return 1

    watch_token = ""
    if not args.no_watch:
        watch_token = (args.watch_token or "").strip()
        if not watch_token:
            watch_token = ask_token("Watching", "--watch-token", "DISCORD_WATCH_TOKEN",
                                    "watch_token", args.reset_tokens)
        elif not looks_like_token(watch_token):
            C.warn("watcher", "the watching token does not look like a user token")
        if not watch_token:
            C.err("watcher", "no watching token provided (use --no-watch to stream only)")
            return 1
        if watch_token == token:
            C.err("watcher", "the watching token is the same as the streaming token; use a second account")
            return 1
        try:
            ensure_watcher_deps()
        except Exception as exc:
            C.err("watcher", str(exc))
            C.warn("watcher", "continuing without the watcher (stream time will not count)")
            watch_token = ""

    guild_id = (args.guild or "").strip()
    channel_id = (args.channel or "").strip()
    if not guild_id:
        guild_id = ask("Server (guild) ID: ")
    if not channel_id:
        channel_id = ask("Voice channel ID: ")
    if not (guild_id.isdigit() and channel_id.isdigit()):
        C.err("input", "guild ID and channel ID must be numeric")
        return 1

    streamer_id = extract_account_snowflake(token)
    if not streamer_id:
        C.warn("token", "could not read the account id from the token; the watcher may not find the stream")
    if watch_token:
        watcher_id = extract_account_snowflake(watch_token)
        if watcher_id and watcher_id == streamer_id:
            C.err("watcher", "both tokens belong to the same account; use a second account")
            return 1
    C.ok("accounts", f"streamer {streamer_id or '?'}"
                     + (f" · watcher {extract_account_snowflake(watch_token) or '?'}"
                        if watch_token else " · no watcher"))

    state = StreamSession(token=token)
    saved = load_saved()

    def _total(source: dict, key: str, cast=float):
        try:
            return cast(source.get(key, 0) or 0)
        except Exception:
            return cast(0)

    state.total_stream_hours_claimed = _total(saved, "total_stream_hours_claimed")
    state.total_stream_sessions = _total(saved, "total_stream_sessions", int)

    remember("token", token)
    if watch_token:
        remember("watch_token", watch_token)

    if args.hours is None:
        hours = choose_stream_hours(state.total_stream_hours_claimed)
    else:
        hours = max(0.0, args.hours)
    print()
    if hours > 0:
        C.ok("duration", f"{_fmt_hours(hours)} selected · it stops by itself after that")
    else:
        C.ok("duration", "no limit · only Ctrl+C stops the stream")
    C.rule()

    runner = WatcherRunner(watch_token, streamer_id, guild_id, channel_id, args.log) if watch_token else None
    engine = GatewayStreamEngine(state, ClientContext(generate_fallback_properties()), C)

    shutdown_lock = threading.Lock()
    shutdown_done = threading.Event()
    offline_timer: dict[str, threading.Timer | None] = {"timer": None}

    def cancel_offline_timer() -> None:
        timer = offline_timer["timer"]
        if timer is not None:
            offline_timer["timer"] = None
            try:
                timer.cancel()
            except Exception:
                pass

    def on_live() -> None:
        if not runner:
            return
        cancel_offline_timer()
        if runner.started:
            return
        if runner.start():
            C.ok("watcher", "started, joining the voice channel to watch the stream")

    def watcher_offline_check() -> None:
        offline_timer["timer"] = None
        if shutdown_done.is_set() or not runner or not runner.started:
            return
        if engine.stream_connected:
            return
        C.warn("watcher", "the streaming account is offline, so the watcher leaves after it...")
        runner.stop(wait=6.0)

    def on_offline() -> None:
        """The streamer's sockets are already closed at this point."""
        if not runner or shutdown_done.is_set():
            return
        cancel_offline_timer()
        timer = threading.Timer(WATCHER_LEAVE_GRACE_SEC, watcher_offline_check)
        timer.daemon = True
        offline_timer["timer"] = timer
        timer.start()
        C.warn("watcher", f"streamer lost its connection; the watcher leaves in "
                          f"{WATCHER_LEAVE_GRACE_SEC:g}s unless the stream comes back")

    def shutdown(wait: float = 8.0, quiet: bool = False) -> None:
        """Ordered stop: streaming account leaves first, watcher second."""
        if not shutdown_lock.acquire(timeout=1.0):
            return
        try:
            if shutdown_done.is_set():
                return
            shutdown_done.set()
            cancel_offline_timer()
            if not runner:
                engine._cleanup(guild_id)
                return
            if not quiet:
                C.rule()
                C.info("stopping: the streaming account leaves first, the watcher after it...")
            engine._cleanup(guild_id)
            if runner.started:
                if not quiet:
                    C.info("streamer left, now closing the watching account...")
                runner.stop(wait=wait)
        finally:
            shutdown_lock.release()

    if runner:
        engine.on_live = on_live
        engine.on_offline = on_offline
        install_console_exit_hook(lambda: shutdown(wait=3.0, quiet=True))
    else:
        engine.on_offline = None
        C.warn("watcher", "disabled (--no-watch): stream time will not count without a viewer")

    if runner:
        C.info(f"{C.GREY}watcher debug log: {os.path.abspath(args.log)}{C.RESET}")
        C.info(f"{C.GREY}order on exit: streaming account first, then the watcher{C.RESET}")

    try:
        engine.start_stream(guild_id, channel_id, hours * 3600.0)
    except KeyboardInterrupt:
        C.info("stopping...")
    finally:
        shutdown()

    if state.total_stream_sessions:
        C.info(f"{C.GREY}total: {state.total_stream_hours_claimed:.2f}h over "
               f"{state.total_stream_sessions} session(s){C.RESET}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
