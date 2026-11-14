"""Read Gemini and Claude/GPT allowance data from the local Antigravity Language Server."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("gemini-usage-mini-widget")

_SERVICE_PREFIX = "/exa.language_server_pb.LanguageServerService"
_GET_USER_STATUS_PATH = f"{_SERVICE_PREFIX}/GetUserStatus"
_RETRIEVE_QUOTA_SUMMARY_PATH = f"{_SERVICE_PREFIX}/RetrieveUserQuotaSummary"
_PROCESS_PREFIXES = ("language_server", "agy", "antigravity")
_CSRF_TOKEN_RE = re.compile(r'--csrf[_-]token(?:=|\s+)(?:"([^"]+)"|\'([^\']+)\'|(\S+))')
_SERVER_PORT_RE = re.compile(
    r"--(?:hub[-_]port|https[-_]server[-_]port|http[-_]server[-_]port|server[-_]port|connect[-_]port|port)(?:=|\s+)(\d+)"
)
_EXT_PORT_RE = re.compile(r"--extension[-_]server[-_]port(?:=|\s+)(\d+)")
_URL_PORT_RE = re.compile(r"https?://(?:127\.0\.0\.1|localhost|\[::1\]):(\d+)", re.IGNORECASE)

_EMPTY_BODY = b"{}"
_METADATA_BODY = json.dumps(
    {
        "metadata": {
            "ideName": "antigravity",
            "extensionName": "antigravity",
            "ideVersion": "unknown",
            "locale": "en",
        }
    },
    separators=(",", ":"),
).encode("utf-8")

_CACHE_LOCK = threading.Lock()
_ENDPOINT_CACHE: dict[str, Any] = {}


class UsageError(RuntimeError):
    """Raised when Antigravity usage data cannot be retrieved or parsed."""


class AuthRequiredError(UsageError):
    """Raised when Antigravity authentication is missing or expired."""


@dataclass(frozen=True)
class LimitWindow:
    used_percent: int
    resets_at: int | None

    @property
    def remaining_percent(self) -> int:
        return max(0, min(100, 100 - self.used_percent))


@dataclass(frozen=True)
class UsageSnapshot:
    plan: str
    account_email: str | None
    five_hour: LimitWindow | None
    seven_day: LimitWindow | None
    claude_gpt_five_hour: LimitWindow | None
    claude_gpt_seven_day: LimitWindow | None
    credit_balance: int | None
    fetched_at: float


@dataclass(frozen=True)
class _LanguageServerProcess:
    pid: int
    name: str
    command_line: str
    csrf_token: str
    server_ports: tuple[int, ...] = ()
    extension_port: int | None = None
    priority: int = 1


def find_antigravity_executables() -> list[str]:
    """Find Antigravity desktop executables on Windows."""
    candidates: list[Path] = []
    override = os.environ.get("GEMINI_USAGE_WIDGET_ANTIGRAVITY")
    if override:
        candidates.append(Path(override).expanduser())

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "Antigravity" / "Antigravity.exe")

    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(variable)
        if root:
            candidates.append(Path(root) / "Antigravity" / "Antigravity.exe")

    for command in ("Antigravity.exe", "antigravity", "agy"):
        found = shutil.which(command)
        if found:
            candidates.append(Path(found))

    available: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = str(candidate.resolve(strict=True))
        except OSError:
            continue
        key = os.path.normcase(resolved)
        if key not in seen:
            seen.add(key)
            available.append(resolved)
    return available


def _append_valid_port(ports: list[int], raw_port: Any) -> None:
    if raw_port is None or isinstance(raw_port, bool):
        return
    try:
        port = int(raw_port)
    except (ValueError, TypeError):
        return
    if 0 < port <= 65535 and port not in ports:
        ports.append(port)


def parse_command_line_ports(command_line: str) -> tuple[list[int], int | None]:
    """Extract (server_ports, extension_server_port) from an Antigravity process command line."""
    if not command_line:
        return [], None
    server_ports: list[int] = []
    for match in _SERVER_PORT_RE.finditer(command_line):
        _append_valid_port(server_ports, match.group(1))

    ext_match = _EXT_PORT_RE.search(command_line)
    ext_port: int | None = None
    if ext_match:
        try:
            candidate = int(ext_match.group(1))
            if 0 < candidate <= 65535:
                ext_port = candidate
        except ValueError:
            ext_port = None
    return server_ports, ext_port


def parse_language_server_command_line(command_line: str) -> tuple[str | None, int | None]:
    """Extract (csrf_token, port) from a language_server or agy command line."""
    if not command_line:
        return None, None
    token_match = _CSRF_TOKEN_RE.search(command_line)
    csrf_token: str | None = None
    if token_match:
        csrf_token = next((group for group in token_match.groups() if group), None)

    server_ports, ext_port = parse_command_line_ports(command_line)
    fallback_port = ext_port if ext_port is not None else (server_ports[0] if server_ports else None)
    return csrf_token, fallback_port


def _is_antigravity_process_name(exe_name: str, command_line: str) -> bool:
    name_lower = exe_name.lower()
    if any(name_lower.startswith(prefix) for prefix in _PROCESS_PREFIXES):
        return True
    cmd_lower = command_line.lower()
    return name_lower == "node.exe" and ("antigravity" in cmd_lower or "agy" in cmd_lower)


def _process_priority(exe_name: str, command_line: str) -> int:
    name_lower = exe_name.lower()
    cmd_lower = command_line.lower()
    if "--standalone" in cmd_lower or "--hub" in cmd_lower or name_lower.startswith("agy"):
        return 0
    if "antigravity" in cmd_lower or name_lower.startswith("antigravity"):
        return 1
    return 2


def _build_process_candidate(pid: int, name: str, cmd: str) -> _LanguageServerProcess | None:
    if not _is_antigravity_process_name(name, cmd):
        return None
    csrf_token, _ = parse_language_server_command_line(cmd)
    if not csrf_token:
        return None
    server_ports, ext_port = parse_command_line_ports(cmd)
    return _LanguageServerProcess(
        pid=pid,
        name=name,
        command_line=cmd,
        csrf_token=csrf_token,
        server_ports=tuple(server_ports),
        extension_port=ext_port,
        priority=_process_priority(name, cmd),
    )


def _discover_daemon_candidates(daemon_dir: Path | None = None) -> list[_LanguageServerProcess]:
    """Read local Antigravity daemon metadata files (e.g. ls_*.json) if accessible."""
    root = daemon_dir or (Path.home() / ".gemini" / "antigravity" / "daemon")
    try:
        if not root.is_dir():
            return []
        files = sorted(root.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return []

    found: list[_LanguageServerProcess] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue

        csrf_token: str | None = None
        for key in ("csrfToken", "csrf_token", "token"):
            raw = data.get(key)
            if isinstance(raw, str) and raw.strip():
                csrf_token = raw.strip()
                break
        if not csrf_token:
            continue

        ports: list[int] = []
        for key in (
            "httpsPort",
            "https_port",
            "port",
            "httpPort",
            "http_port",
            "hubPort",
            "hub_port",
            "serverPort",
            "server_port",
            "connectPort",
            "connect_port",
        ):
            _append_valid_port(ports, data.get(key))
        for url_key in ("url", "endpoint", "baseUrl", "base_url"):
            raw_url = data.get(url_key)
            if isinstance(raw_url, str):
                match = _URL_PORT_RE.search(raw_url)
                if match:
                    _append_valid_port(ports, match.group(1))

        pid = 0
        raw_pid = data.get("pid") if "pid" in data else data.get("processId")
        if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) and raw_pid > 0:
            pid = raw_pid

        if not ports and pid <= 0:
            continue

        found.append(
            _LanguageServerProcess(
                pid=pid,
                name=path.name,
                command_line="",
                csrf_token=csrf_token,
                server_ports=tuple(ports),
                extension_port=None,
                priority=0,
            )
        )
    return found


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", ctypes.c_void_p),
    ]


class _MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class _MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("ucLocalAddr", ctypes.c_ubyte * 16),
        ("dwLocalScopeId", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("ucRemoteAddr", ctypes.c_ubyte * 16),
        ("dwRemoteScopeId", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwState", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


def _read_process_command_line_win32(pid: int) -> str | None:
    """Query a process command line using NtQueryInformationProcess(ProcessCommandLineInformation)."""
    if os.name != "nt" or pid <= 0:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    ntdll.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG),
    ]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long

    process_query_limited_information = 0x1000
    process_command_line_information = 60

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        needed = wintypes.ULONG(0)
        ntdll.NtQueryInformationProcess(
            handle,
            process_command_line_information,
            None,
            0,
            ctypes.byref(needed),
        )
        buf_size = max(int(needed.value), 4096)
        buf = ctypes.create_string_buffer(buf_size)
        status = ntdll.NtQueryInformationProcess(
            handle,
            process_command_line_information,
            buf,
            buf_size,
            ctypes.byref(needed),
        )
        if status != 0:
            return None
        ustr = _UNICODE_STRING.from_buffer_copy(buf)
        if not ustr.Buffer or ustr.Length == 0:
            return None
        return ctypes.wstring_at(ustr.Buffer, ustr.Length // ctypes.sizeof(wintypes.WCHAR))
    except OSError:
        return None
    finally:
        kernel32.CloseHandle(handle)


def _discover_processes_win32() -> list[_LanguageServerProcess]:
    if os.name != "nt":
        return []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    th32cs_snapprocess = 0x00000002
    invalid_handle = ctypes.c_void_p(-1).value
    snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
    if not snapshot or snapshot == invalid_handle:
        return []

    found: list[_LanguageServerProcess] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        has_entry = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while has_entry:
            exe_name = entry.szExeFile
            name_lower = exe_name.lower()
            if any(name_lower.startswith(prefix) for prefix in _PROCESS_PREFIXES) or name_lower == "node.exe":
                pid = int(entry.th32ProcessID)
                cmd = _read_process_command_line_win32(pid) or ""
                candidate = _build_process_candidate(pid, exe_name, cmd)
                if candidate is not None:
                    found.append(candidate)
            has_entry = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)

    found.sort(key=lambda proc: (proc.priority, -proc.pid))
    return found


def _discover_processes_powershell() -> list[_LanguageServerProcess]:
    if os.name != "nt":
        return []
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    ps_command = (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name LIKE 'language_server%' OR Name LIKE 'agy%' OR Name LIKE 'antigravity%' "
        "OR (Name = 'node.exe' AND CommandLine LIKE '%antigravity%')\" "
        "| Select-Object ProcessId,Name,CommandLine "
        "| ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_command],
            capture_output=True,
            text=True,
            timeout=5.0,
            creationflags=creation_flags,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    raw = (completed.stdout or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []

    items = parsed if isinstance(parsed, list) else [parsed]
    found: list[_LanguageServerProcess] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        pid_raw = item.get("ProcessId")
        cmd = str(item.get("CommandLine") or "")
        name = str(item.get("Name") or "")
        if not isinstance(pid_raw, int) or not cmd:
            continue
        candidate = _build_process_candidate(pid_raw, name, cmd)
        if candidate is not None:
            found.append(candidate)
    found.sort(key=lambda proc: (proc.priority, -proc.pid))
    return found


def _discover_language_server_processes() -> list[_LanguageServerProcess]:
    candidates = _discover_daemon_candidates()
    processes = _discover_processes_win32()
    if not processes:
        processes = _discover_processes_powershell()
    return candidates + processes


def _query_tcp_table_ports(
    iphlpapi: ctypes.WinDLL,
    family: int,
    row_cls: type[ctypes.Structure],
    target_pid: int,
) -> list[int]:
    tcp_table_owner_pid_listener = 3
    error_insufficient_buffer = 122

    size = wintypes.DWORD(0)
    res = iphlpapi.GetExtendedTcpTable(
        None,
        ctypes.byref(size),
        False,
        family,
        tcp_table_owner_pid_listener,
        0,
    )
    if res not in (0, error_insufficient_buffer) or size.value == 0:
        return []

    buf = ctypes.create_string_buffer(int(size.value))
    res = iphlpapi.GetExtendedTcpTable(
        buf,
        ctypes.byref(size),
        False,
        family,
        tcp_table_owner_pid_listener,
        0,
    )
    if res != 0:
        return []

    num_entries = wintypes.DWORD.from_buffer_copy(buf, 0).value
    row_size = ctypes.sizeof(row_cls)
    offset = ctypes.sizeof(wintypes.DWORD)
    ports: list[int] = []
    for _ in range(int(num_entries)):
        if offset + row_size > len(buf):
            break
        row = row_cls.from_buffer_copy(buf, offset)
        offset += row_size
        if int(row.dwOwningPid) == target_pid:
            raw_port = int(row.dwLocalPort) & 0xFFFF
            port = ((raw_port & 0xFF) << 8) | ((raw_port >> 8) & 0xFF)
            _append_valid_port(ports, port)
    return ports


def _get_listening_ports_win32(target_pid: int) -> list[int]:
    """Return local IPv4/IPv6 TCP listening ports owned by target_pid via GetExtendedTcpTable."""
    if os.name != "nt" or target_pid <= 0:
        return []
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
        iphlpapi.GetExtendedTcpTable.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
            wintypes.ULONG,
            ctypes.c_int,
            wintypes.ULONG,
        ]
        iphlpapi.GetExtendedTcpTable.restype = wintypes.DWORD
    except OSError:
        return []

    ports: list[int] = []
    for family, row_cls in (
        (2, _MIB_TCPROW_OWNER_PID),
        (23, _MIB_TCP6ROW_OWNER_PID),
    ):
        for port in _query_tcp_table_ports(iphlpapi, family, row_cls, target_pid):
            if port not in ports:
                ports.append(port)
    return ports


def _get_listening_ports_netstat(target_pid: int) -> list[int]:
    if os.name != "nt" or target_pid <= 0:
        return []
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=4.0,
            creationflags=creation_flags,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    ports: list[int] = []
    pid_str = str(target_pid)
    for line in (completed.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        if not parts[0].upper().startswith("TCP") or parts[3].upper() != "LISTENING" or parts[4] != pid_str:
            continue
        local_addr = parts[1]
        if ":" not in local_addr:
            continue
        port_part = local_addr.rsplit(":", 1)[-1]
        _append_valid_port(ports, port_part)
    return ports


def _candidate_ports_for_process(proc: _LanguageServerProcess) -> list[int]:
    ports: list[int] = list(proc.server_ports)
    if proc.pid > 0:
        os_ports = _get_listening_ports_win32(proc.pid)
        if not os_ports:
            os_ports = _get_listening_ports_netstat(proc.pid)
        for port in os_ports:
            if port not in ports:
                ports.append(port)
    if proc.extension_port and proc.extension_port not in ports:
        ports.append(proc.extension_port)
    return ports


def _call_language_server_rpc(
    scheme: str,
    port: int,
    path: str,
    csrf_token: str,
    timeout: float,
) -> dict[str, Any]:
    bodies = (
        (_EMPTY_BODY, _METADATA_BODY)
        if path == _RETRIEVE_QUOTA_SUMMARY_PATH
        else (_METADATA_BODY, _EMPTY_BODY)
    )

    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
    if scheme == "https":
        ssl_ctx = ssl._create_unverified_context()
        handlers.append(urllib.request.HTTPSHandler(context=ssl_ctx))
    opener = urllib.request.build_opener(*handlers)

    last_exc: Exception | None = None
    for host in ("127.0.0.1", "[::1]"):
        url = f"{scheme}://{host}:{port}{path}"
        host_unreachable = False
        for body in bodies:
            request = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Connect-Protocol-Version": "1",
                    "X-Codeium-Csrf-Token": csrf_token,
                },
                method="POST",
            )
            try:
                with opener.open(request, timeout=timeout) as response:
                    parsed = json.loads(response.read().decode("utf-8"))
                if not isinstance(parsed, dict):
                    raise UsageError("Antigravity 상태 응답 형식이 올바르지 않습니다.")
                return parsed
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code == 400 and body is not bodies[-1]:
                    exc.close()
                    continue
                raise
            except (urllib.error.URLError, OSError) as exc:
                last_exc = exc
                host_unreachable = True
                break
        if not host_unreachable:
            break

    if last_exc is not None:
        raise last_exc
    raise UsageError("Antigravity 로컬 서버에 연결하지 못했습니다.")


_RESET_TIME_KEYS = (
    "resetTime",
    "reset_time",
    "resetsAt",
    "resets_at",
    "resetAt",
    "reset_at",
    "nextResetTime",
    "next_reset_time",
    "windowEndTime",
    "window_end_time",
    "endTime",
    "end_time",
    "refreshTime",
    "refresh_time",
    "expiresAt",
    "expires_at",
)
_RELATIVE_RESET_KEYS = (
    "resetInSeconds",
    "reset_in_seconds",
    "secondsUntilReset",
    "seconds_until_reset",
    "resetDuration",
    "reset_duration",
)
_ISO_FRAC_OVER6_RE = re.compile(r"\.(\d{6})\d+")
_NUMERIC_STR_RE = re.compile(r"-?\d+(?:\.\d+)?")
_DURATION_SEC_RE = re.compile(r"^(-?\d+(?:\.\d+)?)s$", re.IGNORECASE)


def _normalize_epoch_seconds(value: float) -> int | None:
    if value <= 0:
        return None
    if value > 10_000_000_000:
        value /= 1000.0
    return int(value)


def _parse_reset_time(raw_reset: Any) -> int | None:
    if raw_reset is None or isinstance(raw_reset, bool):
        return None
    if isinstance(raw_reset, dict):
        for sec_key in ("seconds", "epochSeconds", "unixSeconds"):
            if sec_key in raw_reset and raw_reset[sec_key] is not None and not isinstance(raw_reset[sec_key], bool):
                try:
                    return _normalize_epoch_seconds(float(str(raw_reset[sec_key]).strip()))
                except ValueError:
                    pass
        for nested_key in ("iso", "timestamp", "value"):
            if nested_key in raw_reset:
                parsed = _parse_reset_time(raw_reset[nested_key])
                if parsed is not None:
                    return parsed
        return None
    if isinstance(raw_reset, (int, float)):
        return _normalize_epoch_seconds(float(raw_reset))
    if isinstance(raw_reset, str):
        text = raw_reset.strip()
        if not text:
            return None
        if _NUMERIC_STR_RE.fullmatch(text):
            try:
                return _normalize_epoch_seconds(float(text))
            except ValueError:
                return None
        dur_match = _DURATION_SEC_RE.match(text)
        if dur_match:
            try:
                seconds = float(dur_match.group(1))
                return int(time.time() + max(0.0, seconds))
            except ValueError:
                return None
        try:
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            text = _ISO_FRAC_OVER6_RE.sub(r".\1", text)
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            return None
    return None


def _extract_reset_time(quota_obj: dict[str, Any]) -> int | None:
    containers = [quota_obj]
    for nested_key in ("remaining", "quotaInfo", "quota_info"):
        nested = quota_obj.get(nested_key)
        if isinstance(nested, dict):
            containers.append(nested)

    for container in containers:
        for key in _RESET_TIME_KEYS:
            if key in container:
                parsed = _parse_reset_time(container.get(key))
                if parsed is not None:
                    return parsed

    for container in containers:
        for key in _RELATIVE_RESET_KEYS:
            raw_val = container.get(key)
            if raw_val is None or isinstance(raw_val, bool):
                continue
            if isinstance(raw_val, (int, float)):
                return int(time.time() + max(0.0, float(raw_val)))
            if isinstance(raw_val, str):
                text = raw_val.strip()
                dur_match = _DURATION_SEC_RE.match(text)
                num_text = dur_match.group(1) if dur_match else text
                try:
                    return int(time.time() + max(0.0, float(num_text)))
                except ValueError:
                    pass
    return None


def _decimal_to_used_percent(fraction: Decimal) -> int:
    clamped = max(Decimal("0"), min(Decimal("1"), fraction))
    used_percent = int(
        ((Decimal("1") - clamped) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    return max(0, min(100, used_percent))


def _parse_used_percent_from_quota_obj(quota_obj: dict[str, Any]) -> int | None:
    if not isinstance(quota_obj, dict) or not quota_obj:
        return None

    source = quota_obj
    for nested_key in ("quotaInfo", "quota_info"):
        nested = source.get(nested_key)
        if isinstance(nested, dict) and nested:
            source = nested
            break

    rem_container = source.get("remaining")
    rem_dict = rem_container if isinstance(rem_container, dict) else {}

    rem_frac_raw = None
    for key in ("remainingFraction", "remaining_fraction"):
        if key in rem_dict:
            rem_frac_raw = rem_dict.get(key)
            break
        if key in source:
            rem_frac_raw = source.get(key)
            break
    if rem_frac_raw is None and isinstance(rem_container, (int, float)) and not isinstance(rem_container, bool):
        rem_frac_raw = rem_container

    if rem_frac_raw is not None and not isinstance(rem_frac_raw, bool):
        try:
            rem = Decimal(str(rem_frac_raw))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise UsageError("Antigravity가 반환한 사용량 데이터가 올바르지 않습니다.") from exc
        return _decimal_to_used_percent(rem)

    for key in ("usedFraction", "used_fraction"):
        raw = rem_dict.get(key) if key in rem_dict else source.get(key)
        if raw is not None and not isinstance(raw, bool):
            try:
                used = Decimal(str(raw))
                return _decimal_to_used_percent(Decimal("1") - used)
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise UsageError("Antigravity가 반환한 사용량 데이터가 올바르지 않습니다.") from exc

    for key in ("remainingPercent", "remaining_percent", "remainingPercentage", "remaining_percentage"):
        raw = rem_dict.get(key) if key in rem_dict else source.get(key)
        if raw is not None and not isinstance(raw, bool):
            try:
                rem_pct = Decimal(str(raw)) / Decimal("100")
                return _decimal_to_used_percent(rem_pct)
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise UsageError("Antigravity가 반환한 사용량 데이터가 올바르지 않습니다.") from exc

    for key in ("usedPercent", "used_percent", "usedPercentage", "used_percentage"):
        raw = rem_dict.get(key) if key in rem_dict else source.get(key)
        if raw is not None and not isinstance(raw, bool):
            try:
                used_pct = Decimal(str(raw)) / Decimal("100")
                return _decimal_to_used_percent(Decimal("1") - used_pct)
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise UsageError("Antigravity가 반환한 사용량 데이터가 올바르지 않습니다.") from exc

    rem_amount_raw = (
        rem_dict.get("remainingAmount")
        if "remainingAmount" in rem_dict
        else source.get("remainingAmount", source.get("remaining_amount"))
    )

    if (
        source.get("isExhausted") is True
        or source.get("is_exhausted") is True
        or rem_amount_raw == 0
        or str(rem_amount_raw) == "0"
        or _extract_reset_time(quota_obj) is not None
        or (isinstance(rem_container, dict) and not rem_container)
    ):
        # Proto3 JSON omits remainingFraction when it is 0.0 (100% used).
        return 100

    return None


def _window_from_quota_obj(quota_obj: Any) -> LimitWindow | None:
    if not isinstance(quota_obj, dict) or not quota_obj:
        return None
    used_percent = _parse_used_percent_from_quota_obj(quota_obj)
    if used_percent is None:
        return None
    resets_at = _extract_reset_time(quota_obj)
    return LimitWindow(used_percent=used_percent, resets_at=resets_at)


def _pick_more_constrained(current: LimitWindow | None, candidate: LimitWindow | None) -> LimitWindow | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    current_key = (current.used_percent, 1 if current.resets_at is not None else 0, -(current.resets_at or 0))
    candidate_key = (candidate.used_percent, 1 if candidate.resets_at is not None else 0, -(candidate.resets_at or 0))
    return candidate if candidate_key > current_key else current


def _extract_credits_from_obj(obj: Any) -> int | None:
    if not isinstance(obj, dict):
        return None

    for list_key in ("availableCredits", "credits"):
        raw_list = obj.get(list_key)
        if isinstance(raw_list, list):
            total = 0
            found = False
            for item in raw_list:
                if not isinstance(item, dict):
                    continue
                amount = item.get("creditAmount") if "creditAmount" in item else item.get("amount")
                if amount is not None and not isinstance(amount, bool):
                    try:
                        total += int(Decimal(str(amount)))
                        found = True
                    except (InvalidOperation, ValueError):
                        pass
            if found:
                return total
        elif isinstance(raw_list, (int, float)) and not isinstance(raw_list, bool):
            return int(raw_list)

    for num_key in ("creditBalance", "g1Credits", "aiCredits", "availablePromptCredits"):
        val = obj.get(num_key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return int(val)
        if isinstance(val, str) and val.strip():
            try:
                return int(Decimal(val.strip()))
            except (InvalidOperation, ValueError):
                pass
    return None


def _collect_tag_text(obj: dict[str, Any]) -> str:
    keys = (
        "groupId",
        "group_id",
        "bucketId",
        "bucket_id",
        "familyId",
        "family_id",
        "id",
        "category",
        "modelFamily",
        "model_family",
        "displayName",
        "display_name",
        "name",
        "title",
        "label",
    )
    parts = [str(obj.get(key) or "") for key in keys]
    return " ".join(part for part in parts if part).lower()


def _classify_model_family(tag: str) -> str | None:
    if any(
        token in tag
        for token in (
            "3p",
            "third-party",
            "third_party",
            "third party",
            "claude",
            "gpt",
            "anthropic",
            "openai",
            "sonnet",
            "opus",
            "haiku",
            "partner",
            "external",
        )
    ):
        return "third_party"
    if any(token in tag for token in ("gemini", "argon", "carbon", "hydrogen", "1p", "first-party", "first_party")):
        return "gemini"
    return None


def _classify_bucket_cadence(bucket: dict[str, Any], resets_at: int | None) -> str | None:
    for key in ("window", "windowType", "window_type", "cadence", "period", "limitWindow", "limit_window"):
        raw = bucket.get(key)
        if isinstance(raw, str) and raw.strip():
            norm = raw.strip().lower()
            if any(token in norm for token in ("weekly", "week", "7d", "7-day", "7_day", "seven_day", "168h", "604800")):
                return "weekly"
            if any(token in norm for token in ("5h", "5-hour", "5_hour", "five_hour", "session", "short", "rolling", "18000")):
                return "5h"

    for key in ("windowSeconds", "window_seconds", "durationSeconds", "duration_seconds"):
        raw = bucket.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            if raw >= 86400:
                return "weekly"
            if 0 < raw <= 43200:
                return "5h"

    bucket_tag = _collect_tag_text(bucket)
    if any(token in bucket_tag for token in ("weekly", "7d", "7-day", "7_day", "7 day", "week", "주간", "일주일", "7일")):
        return "weekly"
    if any(token in bucket_tag for token in ("5h", "5-hour", "5_hour", "5 hour", "five-hour", "five hour", "short", "session", "5시간")):
        return "5h"

    if resets_at is not None:
        return "weekly" if (resets_at - int(time.time())) > 6 * 3600 else "5h"
    return None


def _unwrap_summary_groups(summary_obj: dict[str, Any]) -> list[dict[str, Any]]:
    queue_objs: list[dict[str, Any]] = [summary_obj]
    visited = 0
    wrapper_keys = (
        "response",
        "quotaSummary",
        "quota_summary",
        "userQuotaSummary",
        "user_quota_summary",
        "summary",
        "data",
        "result",
    )
    group_keys = ("groups", "quotaGroups", "quota_groups", "families", "modelGroups", "model_groups")
    bucket_keys = ("buckets", "quotaBuckets", "quota_buckets")

    while queue_objs and visited < 12:
        current = queue_objs.pop(0)
        visited += 1
        for key in group_keys:
            groups = current.get(key)
            if isinstance(groups, list):
                return [item for item in groups if isinstance(item, dict)]
        for key in bucket_keys:
            buckets = current.get(key)
            if isinstance(buckets, list):
                return [{"buckets": buckets}]
        for wkey in wrapper_keys:
            nested = current.get(wkey)
            if isinstance(nested, dict):
                queue_objs.append(nested)
    return []


def _parse_summary_groups(
    summary_obj: dict[str, Any],
) -> tuple[LimitWindow | None, LimitWindow | None, LimitWindow | None, LimitWindow | None]:
    gemini_5h: LimitWindow | None = None
    gemini_7d: LimitWindow | None = None
    claude_5h: LimitWindow | None = None
    claude_7d: LimitWindow | None = None

    groups = _unwrap_summary_groups(summary_obj)
    if not groups:
        return gemini_5h, gemini_7d, claude_5h, claude_7d

    for group in groups:
        group_family = _classify_model_family(_collect_tag_text(group))
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            buckets = group.get("quotaBuckets")
        if not isinstance(buckets, list):
            buckets = group.get("items")
        if not isinstance(buckets, list):
            continue

        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            family = group_family or _classify_model_family(_collect_tag_text(bucket))
            if family is None:
                continue

            window = _window_from_quota_obj(bucket)
            if window is None:
                continue

            cadence = _classify_bucket_cadence(bucket, window.resets_at)
            is_weekly = cadence == "weekly"

            if family == "gemini":
                if is_weekly:
                    gemini_7d = _pick_more_constrained(gemini_7d, window)
                else:
                    gemini_5h = _pick_more_constrained(gemini_5h, window)
            elif family == "third_party":
                if is_weekly:
                    claude_7d = _pick_more_constrained(claude_7d, window)
                else:
                    claude_5h = _pick_more_constrained(claude_5h, window)

    return gemini_5h, gemini_7d, claude_5h, claude_7d


def _parse_client_model_configs(
    configs: list[Any],
) -> tuple[LimitWindow | None, LimitWindow | None, LimitWindow | None, LimitWindow | None]:
    gemini_5h: LimitWindow | None = None
    gemini_7d: LimitWindow | None = None
    claude_5h: LimitWindow | None = None
    claude_7d: LimitWindow | None = None

    now = int(time.time())
    for entry in configs:
        if not isinstance(entry, dict):
            continue

        label_raw = str(entry.get("label") or "")
        model_or_alias = entry.get("modelOrAlias")
        alias_raw = ""
        if isinstance(model_or_alias, dict):
            alias_raw = str(model_or_alias.get("model") or model_or_alias.get("alias") or "")
        elif isinstance(model_or_alias, str):
            alias_raw = model_or_alias
        model_id_raw = str(entry.get("modelId") or "")
        combined = f"{label_raw} {alias_raw} {model_id_raw}".lower()

        if "embedding" in combined or "aqa" in combined:
            continue

        is_gemini = any(token in combined for token in ("gemini", "argon", "carbon", "hydrogen"))
        is_third_party = any(token in combined for token in ("claude", "gpt", "sonnet", "opus", "haiku", "o1", "o3"))
        if not is_gemini and not is_third_party:
            continue

        short_obj = entry.get("quotaInfo") if isinstance(entry.get("quotaInfo"), dict) else entry
        weekly_obj = (
            entry.get("weeklyQuotaInfo")
            if isinstance(entry.get("weeklyQuotaInfo"), dict)
            else (entry.get("weekly_quota_info") if isinstance(entry.get("weekly_quota_info"), dict) else None)
        )

        has_inline_quota = any(
            key in entry
            for key in (
                "remainingFraction",
                "remaining_fraction",
                "remainingAmount",
                "resetTime",
                "reset_time",
            )
        )
        short_win = _window_from_quota_obj(short_obj) if short_obj is not entry or has_inline_quota else None
        weekly_win = _window_from_quota_obj(weekly_obj) if weekly_obj else None

        # A 5-hour rolling window cannot reset more than 6 hours in the future;
        # if weeklyQuotaInfo is absent and reset is > 6h out, quotaInfo represents the weekly window.
        if (
            short_win is not None
            and weekly_win is None
            and short_win.resets_at is not None
            and (short_win.resets_at - now) > 6 * 3600
        ):
            weekly_win = short_win
            short_win = None

        if is_gemini:
            gemini_5h = _pick_more_constrained(gemini_5h, short_win)
            gemini_7d = _pick_more_constrained(gemini_7d, weekly_win)
        elif is_third_party:
            claude_5h = _pick_more_constrained(claude_5h, short_win)
            claude_7d = _pick_more_constrained(claude_7d, weekly_win)

    return gemini_5h, gemini_7d, claude_5h, claude_7d


def parse_user_quota(
    status_or_quota: dict[str, Any],
    summary_result: dict[str, Any] | None = None,
    account_email: str | None = None,
) -> UsageSnapshot:
    """Convert Antigravity GetUserStatus and/or RetrieveUserQuotaSummary into a UsageSnapshot."""
    if not isinstance(status_or_quota, dict):
        raise UsageError("Antigravity 응답에 사용량 한도 정보가 없습니다.")

    user_status = (
        status_or_quota.get("userStatus")
        if isinstance(status_or_quota.get("userStatus"), dict)
        else status_or_quota
    )

    # 1. Parse summary groups from RetrieveUserQuotaSummary or status_or_quota if present
    g5_sum, g7_sum, c5_sum, c7_sum = (None, None, None, None)
    if isinstance(summary_result, dict):
        g5_sum, g7_sum, c5_sum, c7_sum = _parse_summary_groups(summary_result)
    if g5_sum is None and g7_sum is None and c5_sum is None and c7_sum is None:
        g5_sum, g7_sum, c5_sum, c7_sum = _parse_summary_groups(user_status)

    # 2. Parse per-model configs from cascadeModelConfigData.clientModelConfigs or buckets
    cascade = (
        user_status.get("cascadeModelConfigData")
        if isinstance(user_status.get("cascadeModelConfigData"), dict)
        else {}
    )
    configs = cascade.get("clientModelConfigs")
    if not isinstance(configs, list):
        configs = user_status.get("buckets") if isinstance(user_status.get("buckets"), list) else []

    g5_cfg, g7_cfg, c5_cfg, c7_cfg = _parse_client_model_configs(configs)

    five_hour = g5_sum or g5_cfg
    seven_day = g7_sum or g7_cfg
    claude_gpt_five_hour = c5_sum or c5_cfg
    claude_gpt_seven_day = c7_sum or c7_cfg

    if (
        five_hour is None
        and seven_day is None
        and claude_gpt_five_hour is None
        and claude_gpt_seven_day is None
    ):
        raise UsageError("Antigravity 응답에 사용량 한도 정보가 없습니다.")

    # 3. Extract Account Email
    email = account_email
    for candidate_email in (
        user_status.get("email"),
        user_status.get("accountEmail"),
        (summary_result or {}).get("email") if isinstance(summary_result, dict) else None,
    ):
        if isinstance(candidate_email, str) and candidate_email.strip():
            email = candidate_email.strip()
            break

    # 4. Extract Plan / Tier Name
    plan = "Antigravity"
    user_tier = user_status.get("userTier") if isinstance(user_status.get("userTier"), dict) else {}
    paid_tier = user_status.get("paidTier") if isinstance(user_status.get("paidTier"), dict) else {}
    current_tier = user_status.get("currentTier") if isinstance(user_status.get("currentTier"), dict) else {}
    plan_status = user_status.get("planStatus") if isinstance(user_status.get("planStatus"), dict) else {}
    plan_info = plan_status.get("planInfo") if isinstance(plan_status.get("planInfo"), dict) else {}

    plan_candidate = (
        user_tier.get("preferredName")
        or user_tier.get("name")
        or paid_tier.get("name")
        or current_tier.get("name")
        or plan_info.get("planName")
        or plan_info.get("teamsTier")
        or user_tier.get("id")
        or paid_tier.get("id")
    )
    if isinstance(plan_candidate, str) and plan_candidate.strip():
        plan = plan_candidate.strip()

    # 5. Extract AI Credits Balance
    credit_balance = (
        _extract_credits_from_obj(user_tier)
        or _extract_credits_from_obj(paid_tier)
        or _extract_credits_from_obj(plan_status)
        or _extract_credits_from_obj(user_status)
        or (_extract_credits_from_obj(summary_result) if isinstance(summary_result, dict) else None)
    )

    return UsageSnapshot(
        plan=plan,
        account_email=email,
        five_hour=five_hour,
        seven_day=seven_day,
        claude_gpt_five_hour=claude_gpt_five_hour,
        claude_gpt_seven_day=claude_gpt_seven_day,
        credit_balance=credit_balance,
        fetched_at=time.time(),
    )


def _has_weekly_limits(snapshot: UsageSnapshot) -> bool:
    return snapshot.seven_day is not None or snapshot.claude_gpt_seven_day is not None


def _fetch_endpoint_payloads(
    scheme: str,
    port: int,
    csrf_token: str,
    timeout: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    status_payload: dict[str, Any] | None = None
    summary_payload: dict[str, Any] | None = None
    last_exc: Exception | None = None

    for path, is_summary in (
        (_RETRIEVE_QUOTA_SUMMARY_PATH, True),
        (_GET_USER_STATUS_PATH, False),
    ):
        try:
            result = _call_language_server_rpc(
                scheme=scheme,
                port=port,
                path=path,
                csrf_token=csrf_token,
                timeout=timeout,
            )
            if is_summary:
                summary_payload = result
            else:
                status_payload = result
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if is_summary:
                LOGGER.debug("RetrieveUserQuotaSummary HTTP %s on %s://port=%s", exc.code, scheme, port)
        except Exception as exc:
            last_exc = exc
            if not is_summary and summary_payload is None:
                raise

    if status_payload is None and summary_payload is None:
        if last_exc is not None:
            raise last_exc
        raise UsageError("Antigravity 로컬 서버 응답이 없습니다.")

    if isinstance(summary_payload, dict) and not _unwrap_summary_groups(summary_payload):
        LOGGER.warning(
            "RetrieveUserQuotaSummary returned unexpected keys on port=%s: %s",
            port,
            sorted(summary_payload.keys()),
        )

    return status_payload, summary_payload


def _build_snapshot_from_payloads(
    status_payload: dict[str, Any] | None,
    summary_payload: dict[str, Any] | None,
    fallback_status_payload: dict[str, Any] | None = None,
) -> UsageSnapshot:
    effective_status = status_payload if status_payload is not None else fallback_status_payload
    return parse_user_quota(
        effective_status or summary_payload or {},
        summary_result=summary_payload if effective_status is not None else None,
    )


def _fetch_from_endpoint(
    scheme: str,
    port: int,
    csrf_token: str,
    timeout: float,
    fallback_status_payload: dict[str, Any] | None = None,
) -> UsageSnapshot:
    status_payload, summary_payload = _fetch_endpoint_payloads(
        scheme=scheme,
        port=port,
        csrf_token=csrf_token,
        timeout=timeout,
    )
    return _build_snapshot_from_payloads(
        status_payload=status_payload,
        summary_payload=summary_payload,
        fallback_status_payload=fallback_status_payload,
    )


def query_usage(timeout: float = 15.0) -> UsageSnapshot:
    """Fetch current Gemini & Claude/GPT quota from the local Antigravity Language Server."""
    with _CACHE_LOCK:
        cached_endpoint = dict(_ENDPOINT_CACHE) if _ENDPOINT_CACHE else None

    if cached_endpoint and cached_endpoint.get("has_weekly") is True:
        try:
            snapshot = _fetch_from_endpoint(
                scheme=str(cached_endpoint["scheme"]),
                port=int(cached_endpoint["port"]),
                csrf_token=str(cached_endpoint["csrf_token"]),
                timeout=min(timeout, 3.5),
                fallback_status_payload=cached_endpoint.get("fallback_status"),
            )
            if _has_weekly_limits(snapshot):
                return snapshot
        except Exception:
            pass
        with _CACHE_LOCK:
            _ENDPOINT_CACHE.clear()

    processes = _discover_language_server_processes()
    if not processes:
        raise UsageError(
            "실행 중인 Antigravity를 찾을 수 없습니다. Antigravity를 실행해 주세요."
        )

    per_attempt_timeout = max(1.5, min(timeout / 4.0, 3.5))
    saw_auth_error = False
    last_error: Exception | None = None
    fallback_status_payload: dict[str, Any] | None = None
    best_partial: tuple[UsageSnapshot, dict[str, Any]] | None = None

    for proc in processes:
        ports = _candidate_ports_for_process(proc)
        for port in ports:
            for scheme in ("https", "http"):
                try:
                    status_payload, summary_payload = _fetch_endpoint_payloads(
                        scheme=scheme,
                        port=port,
                        csrf_token=proc.csrf_token,
                        timeout=per_attempt_timeout,
                    )
                    if status_payload is not None and fallback_status_payload is None:
                        fallback_status_payload = status_payload

                    snapshot = _build_snapshot_from_payloads(
                        status_payload=status_payload,
                        summary_payload=summary_payload,
                        fallback_status_payload=fallback_status_payload,
                    )
                    cache_entry = {
                        "pid": proc.pid,
                        "scheme": scheme,
                        "port": port,
                        "csrf_token": proc.csrf_token,
                        "has_weekly": _has_weekly_limits(snapshot),
                        "fallback_status": fallback_status_payload if status_payload is None else None,
                    }
                    if _has_weekly_limits(snapshot):
                        with _CACHE_LOCK:
                            _ENDPOINT_CACHE.clear()
                            _ENDPOINT_CACHE.update(cache_entry)
                        return snapshot

                    if best_partial is None:
                        best_partial = (snapshot, cache_entry)
                    LOGGER.info(
                        "Endpoint port=%s (%s) returned 5H only; continuing probe for weekly limits",
                        port,
                        proc.name,
                    )
                    # Stop trying http on the same port once https succeeded, and check next port/process.
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code in (401, 403):
                        saw_auth_error = True
                    last_error = exc
                except AuthRequiredError:
                    raise
                except Exception as exc:
                    last_error = exc

    if best_partial is not None:
        snapshot, cache_entry = best_partial
        with _CACHE_LOCK:
            _ENDPOINT_CACHE.clear()
            _ENDPOINT_CACHE.update(cache_entry)
        return snapshot

    if saw_auth_error:
        raise AuthRequiredError(
            "Antigravity 인증에 실패했습니다. Antigravity에서 로그인 상태를 확인해 주세요."
        )
    if last_error is not None:
        raise UsageError(f"Antigravity 로컬 서버에 연결하지 못했습니다: {last_error}") from last_error
    raise UsageError("Antigravity 로컬 서버 포트를 찾지 못했습니다.")
