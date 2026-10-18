"""Environment verification for Community Copilot.

Stdlib-only by design: safe to run before (or without) the venv is ready.

    python scripts/check_prereqs.py

Exit code 0 = ready, 1 = something needs fixing (list printed).
"""

from __future__ import annotations

import ctypes
import json
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OK = "[ OK ]"
FAIL = "[FAIL]"
WARN = "[WARN]"

results: list[tuple[str, str, str]] = []  # (marker, label, detail)


def add(marker: str, label: str, detail: str = "") -> None:
    results.append((marker, label, detail))


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def http_json(url: str, *, payload: dict | None = None, timeout: float = 6.0) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"} if data else {}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def memory_gb() -> tuple[float | None, float | None]:
    if sys.platform != "win32":
        return None, None

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    try:
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return status.ullTotalPhys / 1e9, status.ullAvailPhys / 1e9
    except Exception:  # noqa: BLE001 - informational only
        return None, None


def main() -> int:
    env = load_env(ROOT / ".env")
    base_url = env.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    model = env.get("OLLAMA_MODEL", "gemma4:e2b")

    print("Community Copilot — environment check")
    print(f"project: {ROOT}")
    print("-" * 62)

    # Python ---------------------------------------------------------------
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    # Deliberate runtime guard: this script must be runnable on old Pythons to *report* them.
    if sys.version_info >= (3, 11):  # noqa: UP036
        add(OK, "Python", version)
    else:
        add(FAIL, "Python", f"{version} — 3.11+ required")

    # SQLite + FTS5 --------------------------------------------------------
    try:
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
        connection.close()
        add(OK, "SQLite FTS5", f"sqlite {sqlite3.sqlite_version}")
    except sqlite3.Error as exc:
        add(FAIL, "SQLite FTS5", f"unavailable: {exc}")

    # Ollama CLI + server --------------------------------------------------
    cli = shutil.which("ollama")
    add(OK if cli else WARN, "Ollama CLI", cli or "not found — install from https://ollama.com")

    server_version = None
    try:
        info = http_json(f"{base_url}/api/version")
        server_version = info.get("version", "?")
        add(OK, "Ollama server", f"v{server_version} at {base_url}")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        add(FAIL, "Ollama server", f"not reachable at {base_url} ({exc}) — start Ollama")

    # Model ----------------------------------------------------------------
    if server_version is not None:
        try:
            info = http_json(f"{base_url}/api/show", payload={"model": model})
            capabilities = ", ".join(info.get("capabilities", [])) or "unknown"
            context = None
            for key, value in (info.get("model_info") or {}).items():
                if key.endswith(".context_length") and isinstance(value, (int, float)):
                    context = int(value)
                    break
            detail = f"capabilities: {capabilities}"
            if context:
                detail += f"; context {context:,}"
            add(OK, "Model installed", f"{model} — {detail}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                add(FAIL, "Model installed", f"{model} NOT installed — run: ollama pull {model}")
            else:
                add(FAIL, "Model installed", f"HTTP {exc.code} checking {model}")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            add(FAIL, "Model installed", f"could not check: {exc}")
    else:
        add(WARN, "Model installed", f"skipped — server down (expected: {model})")

    # Configuration (.env) -------------------------------------------------
    if not (ROOT / ".env").exists():
        add(FAIL, ".env file", "missing — copy .env.example to .env and fill it in")
    else:
        add(OK, ".env file", "present")
        token = env.get("DISCORD_TOKEN", "")
        if token:
            add(OK, "DISCORD_TOKEN", f"configured (length {len(token)}, value never printed)")
        else:
            add(FAIL, "DISCORD_TOKEN", "empty — create a bot app and put the token in .env")
        if env.get("GUILD_ALLOWLIST", "").strip():
            add(OK, "GUILD_ALLOWLIST", "configured (empty would deny every server)")
        else:
            add(WARN, "GUILD_ALLOWLIST", "empty — the bot will refuse to operate anywhere")

    # Storage & hardware ---------------------------------------------------
    data_dir = ROOT / env.get("DATA_DIR", "data")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add(OK, "Data directory", f"writable: {data_dir}")
    except OSError as exc:
        add(FAIL, "Data directory", f"not writable: {data_dir} ({exc})")

    free_gb = shutil.disk_usage(ROOT).free / 1e9
    add(OK if free_gb >= 6 else WARN, "Disk space", f"{free_gb:.1f} GB free (model needs ~4.6 GB)")

    total_ram, free_ram = memory_gb()
    if total_ram is not None:
        marker = OK if total_ram >= 7.5 else FAIL
        detail = f"{total_ram:.1f} GB total, {free_ram or 0:.1f} GB free"
        if total_ram < 15:
            detail += " — expect CPU-limited generation on this machine"
        add(marker, "RAM", detail)

    # Report ---------------------------------------------------------------
    print()
    failures = 0
    for marker, label, detail in results:
        print(f"{marker} {label:16} {detail}")
        if marker == FAIL:
            failures += 1
    print("-" * 62)
    if failures:
        print(f"{failures} check(s) need attention before running the bot.")
        return 1
    print("Environment looks ready. Start the bot with: .venv\\Scripts\\python.exe main.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
