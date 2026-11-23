#!/usr/bin/env python3
"""One-command installer / launcher / doctor for the Prior Auth Copilot stack.

    python3 install.py              full run: prerequisites -> config -> start everything -> health checks -> open tabs
    python3 install.py --check      read-only diagnosis: finds problems, changes nothing
    python3 install.py --status     what is running + where the logins are
    python3 install.py --open       just open the web UIs in Chrome
    python3 install.py --stop       stop every container (data is kept)
    python3 install.py --help       all options

Standard library only - works before anything else is installed. Safe to re-run: it never overwrites
existing secrets, it only fills gaps, repairs mismatches between files, and (re)starts what is down.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import platform
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / ".setup" / "logs"
SYSTEM = platform.system()  # Darwin | Linux | Windows

# --------------------------------------------------------------------------------------- output helpers
_TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
if SYSTEM == "Windows":
    os.system("")  # enable ANSI on recent Windows terminals


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s


BOLD, DIM = (lambda s: _c("1", s)), (lambda s: _c("2", s))
GREEN, YELLOW, RED, CYAN = (lambda s: _c("32", s)), (lambda s: _c("33", s)), (lambda s: _c("31", s)), (lambda s: _c("36", s))

TODO: list[str] = []      # things the user must still do by hand
FIXED: list[str] = []     # inconsistencies this run repaired
FAILS: list[str] = []     # hard problems (exit code 1)
WARNS: list[str] = []


def head(title: str) -> None:
    print(f"\n{BOLD(CYAN('== ' + title + ' =='))}")


def ok(msg: str, todo: str | None = None) -> None:  # todo accepted (ignored) so ok/warn/fail are interchangeable
    print(f"  {GREEN('✓')} {msg}")


def info(msg: str) -> None:
    print(f"  {DIM('·')} {msg}")


def warn(msg: str, todo: str | None = None) -> None:
    WARNS.append(msg)
    print(f"  {YELLOW('!')} {msg}")
    if todo:
        TODO.append(todo)


def fail(msg: str, todo: str | None = None) -> None:
    FAILS.append(msg)
    print(f"  {RED('✗')} {msg}")
    if todo:
        TODO.append(todo)


def fixed(msg: str) -> None:
    FIXED.append(msg)
    print(f"  {GREEN('⟳')} fixed: {msg}")


def confirm(question: str, args) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        return False
    return input(f"  ? {question} [y/N] ").strip().lower() in ("y", "yes")


# --------------------------------------------------------------------------------------- process helpers
def run(cmd, cwd=None, timeout=None, input_text=None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, input=input_text)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timed out")


def run_logged(cmd, cwd, name: str, what: str) -> bool:
    """Run a long command, log full output to .setup/logs/<name>.log, show an elapsed ticker."""
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{name}.log"
    start = time.time()
    with open(log, "w") as fh:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT)
        while proc.poll() is None:
            time.sleep(1)
            if _TTY and int(time.time() - start) % 5 == 0:
                print(f"\r  {DIM('…')} {what} ({int(time.time() - start)}s)", end="", flush=True)
    if _TTY:
        print("\r" + " " * 70 + "\r", end="")
    if proc.returncode == 0:
        ok(f"{what} ({int(time.time() - start)}s)")
        return True
    fail(f"{what} failed - last log lines (full log: {log.relative_to(ROOT)}):")
    for line in log.read_text(errors="replace").splitlines()[-15:]:
        print(f"      {DIM(line[:160])}")
    return False


def http(url: str, timeout: float = 4.0, headers: dict | None = None, method: str = "GET", data: bytes | None = None):
    """Return (status, body) - status 0 if unreachable. 4xx/5xx still count as 'reachable'."""
    req = urllib.request.Request(url, headers=headers or {}, method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except ssl.SSLError as e:
        return -1, str(e)
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def wait_http(url: str, timeout: int = 120, any_response: bool = False, headers: dict | None = None) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        code, _ = http(url, headers=headers)
        if (any_response and code > 0) or 200 <= code < 300:
            return True
        time.sleep(2)
    return False


# --------------------------------------------------------------------------------------- the stack
# (name, compose dir, optional?, health checks [(label, url, any_response)])
STEPS = [
    ("ingestion + MCP servers", "ingestion_retriever", False, [("Qdrant MCP", "http://localhost:8100/mcp", True), ("SQLite MCP", "http://localhost:8101/mcp", True)]),
    ("gateway (LiteLLM + Langfuse)", "gateway", False, [("LiteLLM", "http://localhost:4000/health/readiness", False), ("Langfuse", "http://localhost:3000/api/public/health", False)]),
    ("Lens (optional trace UI)", "gateway/lens", True, [("Lens", "http://localhost:4318/", True)]),
    ("mem0 memory service", "mem0", False, [("mem0", "http://localhost:8200/health", False)]),
    ("ADK agents (web + A2A + AG-UI)", "adk_copilot", False, [("ADK web", "http://localhost:8000/list-apps", False), ("A2A server", "http://localhost:8001/", True), ("AG-UI endpoint", "http://localhost:8002/health", False)]),
    ("CopilotKit UI", "copilotkit_ui", False, [("CopilotKit UI", "http://localhost:3100/", False)]),
]

# Browser tabs, in the order they open
TABS = [
    ("CopilotKit UI (AG-UI / A2UI)", "http://localhost:3100"),
    ("ADK web", "http://localhost:8000"),
    ("LiteLLM admin", "http://localhost:4000/ui"),
    ("Langfuse", "http://localhost:3000"),
    ("Qdrant dashboard", "http://localhost:6333/dashboard"),
    ("Lens", "http://localhost:4318/ui/"),
    ("mem0 API docs", "http://localhost:8200/docs"),
]

# port -> (what, substring of the docker container that legitimately owns it)
PORTS = {3000: ("Langfuse", "langfuse-web"), 3100: ("CopilotKit UI", "copilotkit_ui"), 4000: ("LiteLLM", "litellm"),
         4318: ("Lens", "lens"), 6333: ("Qdrant", "qdrant"), 6334: ("Qdrant gRPC", "qdrant"), 8000: ("ADK web", "adk"),
         8001: ("A2A server", "adk"), 8002: ("AG-UI endpoint", "adk"), 8100: ("Qdrant MCP", "mcp"),
         8101: ("SQLite MCP", "mcp"), 8200: ("mem0", "mem0")}

# config files: target, template
ENV_FILES = {
    "gateway/.env": "gateway/.env.example",
    "gateway/.env.langfuse": "gateway/.env.langfuse.example",
    "adk_copilot/.env": "adk_copilot/.env.example",
    "ingestion_retriever/.env": "ingestion_retriever/.env.example",
    "mem0/.env": "mem0/.env.example",
}
# Values that MUST be identical across files:  canonical name -> [(file, key), ...]  (first file wins on conflict)
SHARED = {
    "GOOGLE_API_KEY": [("gateway/.env", "GOOGLE_API_KEY"), ("adk_copilot/.env", "GOOGLE_API_KEY"), ("ingestion_retriever/.env", "GOOGLE_API_KEY")],
    "MCP_API_TOKEN": [("gateway/.env", "MCP_API_TOKEN"), ("adk_copilot/.env", "MCP_API_TOKEN"), ("ingestion_retriever/.env", "MCP_API_TOKEN")],
    "LITELLM_MASTER_KEY": [("gateway/.env", "LITELLM_MASTER_KEY"), ("adk_copilot/.env", "LITELLM_MASTER_KEY"), ("adk_copilot/.env", "OPENAI_API_KEY"), ("mem0/.env", "LITELLM_MASTER_KEY")],
    "LANGFUSE_PUBLIC_KEY": [("gateway/.env.langfuse", "LANGFUSE_INIT_PROJECT_PUBLIC_KEY"), ("adk_copilot/.env", "LANGFUSE_PUBLIC_KEY")],
    "LANGFUSE_SECRET_KEY": [("gateway/.env.langfuse", "LANGFUSE_INIT_PROJECT_SECRET_KEY"), ("adk_copilot/.env", "LANGFUSE_SECRET_KEY")],
}
PLACEHOLDER = re.compile(r"^(\{\{.*\}\}|changeme|)$")


# --------------------------------------------------------------------------------------- env file helpers
def parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def set_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    for i, line in enumerate(lines):
        if re.match(rf"^\s*{re.escape(key)}\s*=", line):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def val(file: str, key: str) -> str:
    v = parse_env(ROOT / file).get(key, "")
    return "" if PLACEHOLDER.match(v) else v


def gen_hex(n: int = 24) -> str:
    return secrets.token_hex(n)


# --------------------------------------------------------------------------------------- 1. prerequisites
def have_cmd(name: str) -> bool:
    return shutil.which(name) is not None


def docker_up() -> bool:
    return run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30).returncode == 0


def start_docker_desktop() -> None:
    if SYSTEM == "Darwin":
        run(["open", "-a", "Docker"])
    elif SYSTEM == "Windows":
        for p in (os.environ.get("ProgramFiles", r"C:\Program Files") + r"\Docker\Docker\Docker Desktop.exe",):
            if os.path.exists(p):
                subprocess.Popen([p])
                break


def install_docker(args) -> None:
    if SYSTEM == "Darwin" and have_cmd("brew"):
        if confirm("Docker Desktop is not installed. Install it now with Homebrew (brew install --cask docker)?", args):
            run_logged(["brew", "install", "--cask", "docker"], ROOT, "install-docker", "Installing Docker Desktop (brew)")
            return
    elif SYSTEM == "Windows" and have_cmd("winget"):
        if confirm("Docker Desktop is not installed. Install it now with winget?", args):
            run_logged(["winget", "install", "-e", "--id", "Docker.DockerDesktop", "--accept-package-agreements", "--accept-source-agreements"], ROOT, "install-docker", "Installing Docker Desktop (winget)")
            return
    hint = {"Darwin": "Install Docker Desktop: https://www.docker.com/products/docker-desktop/ (or `brew install --cask docker`)",
            "Windows": "Install Docker Desktop: https://www.docker.com/products/docker-desktop/ (or `winget install -e --id Docker.DockerDesktop`)",
            }.get(SYSTEM, "Install Docker Engine + the compose plugin: https://docs.docker.com/engine/install/ then re-run this script")
    fail("Docker is not installed", todo=hint)


def check_prereqs(args, repair: bool) -> bool:
    head("1/6  Prerequisites")
    pv = sys.version_info
    (ok if pv >= (3, 9) else fail)(f"Python {pv.major}.{pv.minor}.{pv.micro}" + ("" if pv >= (3, 9) else " - need 3.9+"))
    ok(f"{platform.system()} {platform.machine()}")

    if not have_cmd("docker"):
        if repair:
            install_docker(args)
        else:
            fail("Docker is not installed", todo="Install Docker Desktop: https://www.docker.com/products/docker-desktop/")
    if have_cmd("docker"):
        ok("docker CLI found")
        if not docker_up():
            if repair:
                info("Docker daemon is not running - starting Docker Desktop (first launch may ask you to accept a license)...")
                start_docker_desktop()
                for i in range(90):
                    if docker_up():
                        break
                    if i % 10 == 9:
                        info("still waiting for Docker to start...")
                    time.sleep(3)
            if not docker_up():
                fail("Docker daemon is not running", todo="Open Docker Desktop, finish its first-run setup, wait for 'Engine running', then re-run: python3 install.py")
        if docker_up():
            ok("Docker daemon running")
            cv = run(["docker", "compose", "version", "--short"])
            (ok if cv.returncode == 0 else fail)(f"docker compose {cv.stdout.strip()}" if cv.returncode == 0 else "docker compose v2 plugin missing",
                                                  todo=None if cv.returncode == 0 else "Update Docker Desktop (it bundles compose v2)")
            mem = run(["docker", "info", "--format", "{{.MemTotal}}"]).stdout.strip()
            if mem.isdigit():
                gb = int(mem) / 1e9
                if gb < 7.5:
                    warn(f"Docker has only {gb:.1f} GB of memory (frontend build needs ~8 GB)",
                         todo="Docker Desktop -> Settings -> Resources -> Memory: set to 8 GB or more, Apply & restart, then re-run")
                else:
                    ok(f"Docker memory {gb:.1f} GB")
    free = shutil.disk_usage(ROOT).free / 1e9
    (ok if free >= 20 else warn)(f"{free:.0f} GB free disk" + ("" if free >= 20 else " (images + volumes need ~15-20 GB)"))

    chrome = find_chrome()
    if chrome:
        ok("Google Chrome found")
    elif repair and confirm("Google Chrome not found. Install it?", args) and install_chrome():
        ok("Google Chrome installed")
    else:
        warn("Google Chrome not found - tabs will open in your default browser", todo="(optional) Install Google Chrome: https://www.google.com/chrome/")

    if args.ca_cert:
        src = Path(args.ca_cert).expanduser()
        if src.is_file():
            for d in ("adk_copilot", "gateway", "ingestion_retriever", "mem0", "copilotkit_ui"):
                (ROOT / d / "certs").mkdir(exist_ok=True)
                shutil.copy(src, ROOT / d / "certs" / "corporate-ca.pem")
            ok(f"Corporate CA {src.name} copied into each certs/ folder (used at image build)")
        else:
            fail(f"--ca-cert file not found: {src}")
    return not FAILS


def find_chrome() -> str | None:
    if SYSTEM == "Darwin":
        p = "/Applications/Google Chrome.app"
        return p if os.path.exists(p) else None
    if SYSTEM == "Windows":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
            if base and os.path.exists(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")):
                return os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
        return None
    for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if have_cmd(n):
            return shutil.which(n)
    return None


def install_chrome() -> bool:
    if SYSTEM == "Darwin" and have_cmd("brew"):
        return run(["brew", "install", "--cask", "google-chrome"]).returncode == 0
    if SYSTEM == "Windows" and have_cmd("winget"):
        return run(["winget", "install", "-e", "--id", "Google.Chrome", "--accept-package-agreements", "--accept-source-agreements"]).returncode == 0
    return False


def containers() -> dict[str, tuple[str, str]]:
    """name -> (state, ports) for every container."""
    out = run(["docker", "ps", "-a", "--format", "{{.Names}}|{{.State}}|{{.Ports}}"]).stdout
    res = {}
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            res[parts[0]] = (parts[1], parts[2])
    return res


def publishes(ports: str, port: int) -> bool:
    """True if a `docker ps` Ports string (e.g. '0.0.0.0:6333-6334->6333-6334/tcp') publishes host `port`."""
    for lo, hi in re.findall(r":(\d+)(?:-(\d+))?->", ports):
        if int(lo) <= port <= int(hi or lo):
            return True
    return False


def check_ports() -> None:
    cs = containers()
    for port, (what, owner) in PORTS.items():
        with socket.socket() as s:
            s.settimeout(0.5)
            busy = s.connect_ex(("127.0.0.1", port)) == 0
        if not busy:
            continue
        ours = any(owner in n and st == "running" and publishes(ports, port) for n, (st, ports) in cs.items())
        if not ours:
            fail(f"port {port} ({what}) is already used by something else",
                 todo=f"Free port {port} (find the process: `lsof -i :{port}` on macOS/Linux), then re-run")


# --------------------------------------------------------------------------------------- 2. configuration
def ask(label: str, env_name: str, args, secret=True, default: str | None = None, required=True) -> str:
    v = os.environ.get(env_name, "").strip()
    if v:
        return v
    if not args.yes and sys.stdin.isatty():
        prompt = f"  ? {label}" + (f" [{default}]" if default else "") + (" (input hidden)" if secret else "") + ": "
        v = (getpass.getpass(prompt) if secret else input(prompt)).strip()
        if v:
            return v
    return default or ""


def validate_google_key(key: str) -> None:
    code, body = http(f"https://generativelanguage.googleapis.com/v1beta/models?pageSize=1&key={key}", timeout=10)
    if code == 200:
        ok("Google API key accepted")
    elif code in (400, 401, 403):
        fail("Google API key was rejected by Google", todo="Create a key at https://aistudio.google.com/apikey and put it in gateway/.env, adk_copilot/.env and ingestion_retriever/.env (GOOGLE_API_KEY=), or delete those lines and re-run")
    else:
        info("could not validate the Google key from this machine (corporate proxy?) - continuing")


def ensure_env(args) -> bool:
    head("2/6  Configuration (env files + shared secrets)")
    created = []
    # 1) values we may have to ask for / generate (only used when a file is created or a key is missing)
    gk = next((val(f, k) for f, k in SHARED["GOOGLE_API_KEY"] if val(f, k)), "") or ask("Google AI Studio API key (https://aistudio.google.com/apikey)", "GOOGLE_API_KEY", args)
    if not gk:
        fail("GOOGLE_API_KEY is required", todo="Get a free key at https://aistudio.google.com/apikey, then run:  GOOGLE_API_KEY=... python3 install.py")
        return False
    groq = val("gateway/.env", "GROQ_API_KEY")
    if not groq:
        groq = ask("Groq API key (optional - press Enter to skip; Gemini fallbacks cover it)", "GROQ_API_KEY", args, required=False) or "unused-placeholder"
    values = {
        "GOOGLE_API_KEY": gk,
        "GROQ_API_KEY": groq,
        "MCP_API_TOKEN": next((val(f, k) for f, k in SHARED["MCP_API_TOKEN"] if val(f, k)), "") or secrets.token_urlsafe(32),
        "LITELLM_MASTER_KEY": next((val(f, k) for f, k in SHARED["LITELLM_MASTER_KEY"] if val(f, k)), "") or "sk-" + gen_hex(24),
        "UI_USERNAME": val("gateway/.env", "UI_USERNAME") or "admin",
        "UI_PASSWORD": val("gateway/.env", "UI_PASSWORD") or secrets.token_urlsafe(12),
        "NEXTAUTH_SECRET": gen_hex(32), "LANGFUSE_SALT": gen_hex(32), "LANGFUSE_ENCRYPTION_KEY": gen_hex(32),
        "LANGFUSE_PUBLIC_KEY": next((val(f, k) for f, k in SHARED["LANGFUSE_PUBLIC_KEY"] if val(f, k)), "") or "pk-lf-" + gen_hex(12),
        "LANGFUSE_SECRET_KEY": next((val(f, k) for f, k in SHARED["LANGFUSE_SECRET_KEY"] if val(f, k)), "") or "sk-lf-" + gen_hex(24),
        "LANGFUSE_EMAIL": val("gateway/.env.langfuse", "LANGFUSE_INIT_USER_EMAIL") or ask("Email for the local Langfuse login (any address; stays on this machine)", "LANGFUSE_EMAIL", args, secret=False, default="admin@example.com"),
        "LANGFUSE_PASSWORD": val("gateway/.env.langfuse", "LANGFUSE_INIT_USER_PASSWORD") or secrets.token_urlsafe(12),
    }
    # 2) create missing files / fill missing keys, never overwrite existing values
    for target, tmpl in ENV_FILES.items():
        tpath, spath = ROOT / target, ROOT / tmpl
        if not spath.exists():
            fail(f"template missing: {tmpl}")
            continue
        text = re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), spath.read_text())
        if not tpath.exists():
            tpath.write_text(text)
            try:
                tpath.chmod(0o600)
            except OSError:
                pass
            created.append(target)
            continue
        have = parse_env(tpath)
        for k, v in parse_env_text(text).items():
            if k not in have or PLACEHOLDER.match(have[k]):
                set_env_value(tpath, k, v)
                fixed(f"{target}: added missing {k}")
    for c in created:
        ok(f"created {c}")
    if not created:
        ok("all env files already exist")
    # 3) keep shared values identical everywhere
    changed = sync_shared()
    validate = val("gateway/.env", "GOOGLE_API_KEY")
    if validate and not args.check:
        validate_google_key(validate)
    args._env_changed = changed
    return not FAILS


def parse_env_text(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def sync_shared(report_only: bool = False) -> set[str]:
    """Make every shared secret identical across files. Returns the set of compose dirs whose env changed."""
    changed: set[str] = set()
    for name, locs in SHARED.items():
        canonical = next((val(f, k) for f, k in locs if val(f, k)), "")
        if not canonical:
            continue
        for f, k in locs:
            cur = parse_env(ROOT / f).get(k)
            if cur != canonical:
                what = f"{f}:{k} differed from {locs[0][0]} ({name})" if cur else f"{f}:{k} was empty"
                if report_only:
                    fail(f"inconsistent: {what}", todo="Run `python3 install.py` to sync it automatically")
                else:
                    set_env_value(ROOT / f, k, canonical)
                    fixed(what + " -> synced")
                    changed.add(str(Path(f).parent))
    if not report_only and not changed:
        ok("shared secrets are consistent across all files")
    return changed


# --------------------------------------------------------------------------------------- 3. start everything
def start_qdrant() -> bool:
    cs = containers()
    if "qdrant" in cs:
        if cs["qdrant"][0] == "running":
            ok("Qdrant already running")
            return True
        run(["docker", "start", "qdrant"])
        ok("Qdrant started (existing container)")
        return True
    return run_logged(["docker", "run", "-d", "--name", "qdrant", "-p", "6333:6333", "-p", "6334:6334", "-v", "qdrant_storage:/qdrant/storage",
                       "--restart", "unless-stopped", "qdrant/qdrant:latest"], ROOT, "qdrant", "Starting Qdrant (vector DB)")


def ensure_litellm_db() -> None:
    gw = ROOT / "gateway"
    run(["docker", "compose", "up", "-d", "postgres"], cwd=gw)
    for _ in range(40):
        if run(["docker", "compose", "exec", "-T", "postgres", "pg_isready", "-U", "postgres"], cwd=gw).returncode == 0:
            break
        time.sleep(2)
    q = run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "postgres", "-tAc", "SELECT 1 FROM pg_database WHERE datname='litellm'"], cwd=gw)
    if q.stdout.strip() != "1":
        c = run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "postgres", "-c", "CREATE DATABASE litellm"], cwd=gw)
        (fixed if c.returncode == 0 else fail)("created the 'litellm' database in Postgres" if c.returncode == 0 else f"could not create litellm database: {c.stderr.strip()[:120]}")
    else:
        ok("LiteLLM database exists")


def start_stack(args) -> bool:
    head("3/6  Starting services (first run builds images: expect 10-20 minutes)")
    if not start_qdrant():
        return False
    changed = getattr(args, "_env_changed", set())
    for name, d, optional, checks in STEPS:
        info(name)
        if d == "gateway":
            ensure_litellm_db()
        flags = ["up", "-d", "--build"] + (["--force-recreate"] if d in changed else [])
        good = run_logged(["docker", "compose", *flags], ROOT / d, d.replace("/", "_"), f"{d}: docker compose {' '.join(flags)}")
        if not good:
            if optional:
                warn(f"{name} did not start (optional) - see .setup/logs/")
                continue
            return False
        for label, url, anyresp in checks:
            if wait_http(url, timeout=240 if d in ("gateway", "copilotkit_ui") else 120, any_response=anyresp):
                ok(f"{label} is up  ({url})")
            else:
                (warn if optional else fail)(f"{label} did not become healthy: {url}", todo=f"Inspect: cd {d} && docker compose logs --tail 50")
    return not FAILS


# --------------------------------------------------------------------------------------- 4. consistency checks
def langfuse_auth() -> str:
    pk, sk = val("adk_copilot/.env", "LANGFUSE_PUBLIC_KEY"), val("adk_copilot/.env", "LANGFUSE_SECRET_KEY")
    return "Basic " + base64.b64encode(f"{pk}:{sk}".encode()).decode()


def seed_prompts_if_needed(repair: bool) -> None:
    code, _ = http("http://localhost:3000/api/public/v2/prompts/preauth%2Fsupervisor?label=production", headers={"Authorization": langfuse_auth()})
    if code == 200:
        ok("Langfuse prompts (preauth/*) are seeded")
        return
    if code in (401, 403):
        fail("Langfuse rejected the API keys in adk_copilot/.env (its database was initialised with different keys)",
             todo="Langfuse UI -> Settings -> API Keys: create a key pair, put them in adk_copilot/.env (LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY), then `cd adk_copilot && docker compose up -d --force-recreate`")
        return
    if code == 0:
        warn("Langfuse not reachable - skipped prompt check")
        return
    if not repair:
        warn("Langfuse prompts not seeded yet", todo="Run `python3 install.py` to seed them")
        return
    seeded = run_logged(["docker", "compose", "run", "--rm", "--no-deps", "-v", f"{ROOT / 'adk_copilot' / 'scripts'}:/app/scripts:ro", "adk",
                         "python", "/app/scripts/seed_prompts.py"], ROOT / "adk_copilot", "seed_prompts", "Seeding agent prompts into Langfuse")
    if seeded:
        run(["docker", "compose", "restart"], cwd=ROOT / "adk_copilot")
        fixed("seeded Langfuse prompts and restarted the agents")


def doctor(args, repair: bool) -> None:
    head("4/6  Consistency checks" if repair else "Consistency checks")
    if not repair:
        sync_shared(report_only=True)
    # databases / data
    if (ROOT / "pa_copilot_dataset" / "pa_copilot.db").exists():
        ok("SQLite dataset present (pa_copilot_dataset/pa_copilot.db)")
    else:
        fail("pa_copilot_dataset/pa_copilot.db is missing", todo="Rebuild it: cd pa_copilot_dataset && python3 generate_dataset.py <out_dir> && python3 build_sqlite.py (see its README)")
    for coll in ("prior_auth_policies", "prior_auth_notes"):
        code, body = http(f"http://localhost:6333/collections/{coll}")
        pts = 0
        if code == 200:
            try:
                pts = json.loads(body)["result"]["points_count"]
            except Exception:  # noqa: BLE001
                pass
        if pts > 0:
            ok(f"Qdrant collection {coll}: {pts} points")
        else:
            warn(f"Qdrant collection {coll} is empty/missing (ingestion may still be embedding, or Gemini quota is exhausted)",
                 todo="Check ingestion: cd ingestion_retriever && docker compose logs --tail 30 ingestion")
    mk = val("gateway/.env", "LITELLM_MASTER_KEY")
    code, _ = http("http://localhost:4000/key/list", headers={"Authorization": f"Bearer {mk}"})
    if code == 200:
        ok("LiteLLM accepts the master key and is connected to its database")
    elif code == 0:
        warn("LiteLLM not reachable")
    else:
        fail(f"LiteLLM key/DB check failed (HTTP {code})", todo="cd gateway && docker compose logs --tail 40 litellm")
    seed_prompts_if_needed(repair)
    # expected containers up?
    cs = containers()
    want = ["qdrant", "gateway-litellm-1", "gateway-langfuse-web-1", "gateway-postgres-1", "mem0-mem0-1", "adk_copilot-adk-1", "adk_copilot-adk-a2a-1",
            "adk_copilot-adk-agui-1", "copilotkit_ui-ui-1"]
    down = [n for n in want if cs.get(n, ("missing",))[0] != "running"]
    (ok if not down else warn)("all expected containers are running" if not down else f"containers not running: {', '.join(down)}",
                               todo=None if not down else "Run `python3 install.py` to (re)start them")


# --------------------------------------------------------------------------------------- 5/6. browser + summary
def open_tabs(args) -> None:
    head("5/6  Opening web UIs")
    urls = [u for _, u in TABS]
    chrome = find_chrome()
    try:
        if chrome and SYSTEM == "Darwin":
            subprocess.Popen(["open", "-a", "Google Chrome", *urls])
        elif chrome and SYSTEM == "Windows":
            subprocess.Popen([chrome, *urls])
        elif chrome:
            subprocess.Popen([chrome, *urls], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            for u in urls:
                webbrowser.open_new_tab(u)
        ok(f"opened {len(urls)} tabs in " + ("Chrome" if chrome else "your default browser"))
    except Exception as e:  # noqa: BLE001
        warn(f"could not open the browser automatically: {e}")
    for label, u in TABS:
        info(f"{label:30s} {u}")


def credentials() -> list[tuple[str, str]]:
    return [
        ("Langfuse  (http://localhost:3000)", f"{val('gateway/.env.langfuse', 'LANGFUSE_INIT_USER_EMAIL')}  /  {val('gateway/.env.langfuse', 'LANGFUSE_INIT_USER_PASSWORD')}"),
        ("LiteLLM admin  (http://localhost:4000/ui)", f"{val('gateway/.env', 'UI_USERNAME')}  /  {val('gateway/.env', 'UI_PASSWORD')}"),
        ("LiteLLM master key (API calls)", "in gateway/.env  (LITELLM_MASTER_KEY)"),
        ("Lens  (http://localhost:4318/ui/)", "setup token: gateway/lens/.env -> LENS_ADMIN_TOKEN"),
    ]


def summary(args) -> None:
    head("6/6  Summary")
    cs = containers()
    running = sum(1 for _, (st, _) in cs.items() if st == "running")
    info(f"{running} containers running")
    print(f"\n  {BOLD('Logins')}")
    for k, v in credentials():
        print(f"    {k:44s} {v}")
    print(f"\n  {BOLD('Try it')}: open the CopilotKit UI (http://localhost:3100), pick a case, press Adjudicate.")
    print(f"           or in ADK web (http://localhost:8000) choose app 'preauth' and type:  Review case PA-001")
    if FIXED:
        print(f"\n  {BOLD('Repaired this run')} ({len(FIXED)})")
        for f in FIXED:
            print(f"    {GREEN('⟳')} {f}")
    todo = list(dict.fromkeys(TODO))
    todo.append("Gemini free-tier keys have daily request quotas; if answers stop with HTTP 429, wait for the reset or use a billed key (the LiteLLM gateway already falls back across several Gemini models).")
    print(f"\n  {BOLD('What is left for you')}")
    for i, t in enumerate(todo, 1):
        print(f"    {i}. {t}")
    print(f"\n  Stop everything:  python3 install.py --stop     Re-open tabs:  python3 install.py --open     Diagnose:  python3 install.py --check\n")


def status(args) -> int:
    head("Status")
    cs = containers()
    ours = {n: v for n, v in cs.items() if any(n.startswith(p) for p in ("qdrant", "gateway-", "mem0-", "adk_copilot-", "copilotkit_ui-", "lens-", "ingestion_retriever-"))}
    for n, (st, _) in sorted(ours.items()):
        (ok if st == "running" else warn)(f"{n:38s} {st}")
    if not ours:
        warn("no project containers found - run: python3 install.py")
    print()
    for label, u in TABS:
        code, _ = http(u, timeout=2)
        (ok if code > 0 else warn)(f"{label:30s} {u}  {'up' if code > 0 else 'down'}")
    print(f"\n  {BOLD('Logins')}")
    for k, v in credentials():
        print(f"    {k:44s} {v}")
    return 0


def stop_all() -> int:
    head("Stopping")
    for _, d, _, _ in reversed(STEPS):
        r = run(["docker", "compose", "stop"], cwd=ROOT / d, timeout=180)
        (ok if r.returncode == 0 else warn)(f"{d}: stopped" if r.returncode == 0 else f"{d}: {r.stderr.strip()[:100]}")
    run(["docker", "stop", "qdrant"])
    ok("qdrant: stopped (data volume kept)")
    info("Data is kept in Docker volumes. Start again with: python3 install.py")
    return 0


# --------------------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Install, start and verify the Prior Auth Copilot stack.", formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Env vars that skip prompts: GOOGLE_API_KEY, GROQ_API_KEY, LANGFUSE_EMAIL")
    ap.add_argument("--check", action="store_true", help="read-only diagnosis; change nothing")
    ap.add_argument("--status", action="store_true", help="show what is running and the logins")
    ap.add_argument("--open", action="store_true", help="only open the web UIs in Chrome")
    ap.add_argument("--stop", action="store_true", help="stop all containers (data kept)")
    ap.add_argument("--no-browser", action="store_true", help="do not open browser tabs")
    ap.add_argument("--env-only", action="store_true", help="only create/repair the env files, then exit")
    ap.add_argument("--yes", "-y", action="store_true", help="non-interactive: accept installs, never prompt")
    ap.add_argument("--ca-cert", metavar="FILE", help="corporate TLS-proxy root CA (.pem) to trust inside the Docker images")
    ap.add_argument("--root", metavar="DIR", help=argparse.SUPPRESS)
    args = ap.parse_args()

    global ROOT, LOGS
    if args.root:
        ROOT = Path(args.root).resolve()
        LOGS = ROOT / ".setup" / "logs"
    os.chdir(ROOT)

    if args.status:
        return status(args)
    if args.stop:
        return stop_all()
    if args.open:
        open_tabs(args)
        return 0

    print(BOLD("\nPrior Auth Copilot - installer"))
    repair = not args.check
    if args.env_only:
        ensure_env(args)
        return 1 if FAILS else 0
    if not check_prereqs(args, repair):
        summary(args)
        return 1
    if args.check:
        head("2/6  Configuration")
        for target in ENV_FILES:
            (ok if (ROOT / target).exists() else fail)(f"{target} " + ("exists" if (ROOT / target).exists() else "is missing"), todo=None if (ROOT / target).exists() else "Run `python3 install.py` to create it")
        check_ports()
        doctor(args, repair=False)
        summary(args)
        return 1 if FAILS else 0

    if not ensure_env(args):
        summary(args)
        return 1
    check_ports()
    if FAILS:
        summary(args)
        return 1
    if not start_stack(args):
        summary(args)
        return 1
    doctor(args, repair=True)
    if not args.no_browser:
        open_tabs(args)
    summary(args)
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
