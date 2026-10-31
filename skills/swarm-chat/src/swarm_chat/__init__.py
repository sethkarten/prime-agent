"""SwarmChat team room: one shared chat channel for a whole agent tree.

`room()` lazily starts a family-local SwarmChat server (the vendored upstream
package, MIT) on the machine and registers this session as a user; every session
that shares the same prime-agent agent directory — in the eval sandbox that is
the whole rollout: the root session and every spawned subagent — joins the same
public ``#swarm`` channel. Team members post findings, claims and results there
instead of relaying them pairwise through the parent, and the orchestrator reads
the channel instead of polling each child.

The server binds 127.0.0.1 only and lives under
``$PRIME_AGENT_CODING_AGENT_DIR/swarmchat`` (its lifetime is the sandbox's, and
the data vanishes with it). Everything is usable with no network access.

Call from a session kernel:

    room = await swarm_chat.room(name="recon-1")   # idempotent; identity per session
    await swarm_chat.send("claimed bug #12 in parser.py")
    msgs = await swarm_chat.read()                 # the channel, newest last
    await swarm_chat.wait(timeout=30)              # block for a new message
    cmd = await swarm_chat.wait_command()          # background-bash wait instead

`wait_command()` returns a ready-to-run `bash()` command that blocks until a new
channel message arrives and then exits, so it fits the turn discipline: start it
with `bash()`, end the turn, and the `[bash-done]` notification wakes the session
with the message in the command output. Prefer it over in-kernel `wait()`.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from swarmchat.client import Client, ClientError  # vendored upstream, unmodified

#: The kernel exports the session tree's shared agent directory under this name;
#: every session in one prime-agent install (in the eval sandbox: one rollout)
#: sees the same value, so the room spans the whole tree.
FAMILY_DIR_ENV = "PRIME_AGENT_CODING_AGENT_DIR"
#: The public channel every room member joins.
DEFAULT_CHANNEL = "swarm"
#: The vendored server holds one wait request at most 300s; stay under it.
WAIT_HARD_CAP_S = 280.0
_BOOT_TIMEOUT_S = 15.0


def _family_dir() -> Path:
    raw = os.environ.get(FAMILY_DIR_ENV, "").strip()
    if not raw:
        raise RuntimeError(
            "swarm_chat needs a session kernel (no " + FAMILY_DIR_ENV + " in the environment)"
        )
    return Path(raw)


def _data_dir() -> Path:
    return _family_dir() / "swarmchat"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _healthy(server: str | None) -> bool:
    if not server:
        return False
    try:
        with urllib.request.urlopen(f"{server}/v1/health", timeout=2.0) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


@contextmanager
def _boot_lock(data: Path):
    """Serialize first-server-start across sessions of one family (best effort)."""
    handle = None
    try:
        handle = open(data / "boot.lock", "a+b")
        import fcntl  # POSIX only; fall back to lockless elsewhere

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        yield
    finally:
        if handle is not None:
            handle.close()


def _kernel_python() -> str:
    """The session kernel's interpreter, where this skill is installed."""
    return os.environ.get("PRIME_AGENT_KERNEL_PYTHON", "").strip() or sys.executable


def _start_server(data: Path) -> str:
    """Run `python -m swarmchat serve --port 0` detached; the server writes
    its actual address to <data>/address. Returns that address."""
    log_path = data / "serve.log"
    with open(log_path, "ab") as log:
        subprocess.Popen(
            [_kernel_python(), "-m", "swarmchat", "serve",
             "--data", str(data), "--host", "127.0.0.1", "--port", "0"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True,
        )
    deadline = time.monotonic() + _BOOT_TIMEOUT_S
    while time.monotonic() < deadline:
        server = _read_text(data / "address")
        if _healthy(server):
            return server or ""
        time.sleep(0.1)
    raise RuntimeError(f"the swarm room server did not come up; see {log_path}")


def _server(data: Path) -> str:
    server = _read_text(data / "address")
    if _healthy(server):
        return server or ""
    with _boot_lock(data):
        server = _read_text(data / "address")
        if _healthy(server):
            return server or ""
        return _start_server(data)


def _user_id() -> str:
    """A stable, room-safe user id for this session: `s-<session id prefix>` for
    spawned/saved sessions, `root[-<kernel owner pid>]` otherwise."""
    session = os.environ.get("RLM_SESSION_ID", "").strip().lower()
    if session:
        raw = "s-" + session[:24]
    else:
        pid = os.environ.get("PRIME_AGENT_KERNEL_OWNER_PID", "").strip()
        raw = "root-" + pid if pid else "root"
    cleaned = "".join(
        c if (c.isascii() and (c.isalnum() or c in "._-")) else "-" for c in raw
    ).lstrip("-")[:40] or "agent"
    if not (cleaned[0].isalnum()):
        cleaned = "a" + cleaned[1:]
    return cleaned


def _identity(server: str, data: Path, user: str, name: str | None) -> str:
    """This session's room token, minted once per user and cached on disk."""
    tokens = data / "tokens"
    tokens.mkdir(parents=True, exist_ok=True)
    cached = tokens / f"{user}.token"
    token = _read_text(cached)
    if token:
        return token
    admin_token = _read_text(data / "admin.token") or ""
    admin = Client(server, admin_token)
    try:
        token = admin.user_add(id=user, name=name or user)["token"]
    except ClientError:
        token = admin.token_rotate(id=user)["token"]  # the user exists already
    cached.write_text(token + "\n", encoding="utf-8")
    os.chmod(cached, 0o600)
    return token


def _client() -> Client:
    data = _data_dir()
    server = _server(data)
    token = _identity(server, data, _user_id(), None)
    _export_env(server, token)
    return Client(server, token)


def _export_env(server: str, token: str) -> None:
    """Let bash()-spawned `python -m swarmchat.cli ...` commands reuse the room
    without flags: the shell inherits the kernel process environment."""
    os.environ["SWARMCHAT_SERVER"] = server
    os.environ["SWARMCHAT_TOKEN"] = token


async def room(name: str | None = None, channel: str = DEFAULT_CHANNEL) -> dict:
    """Join (idempotently) the family room: start the local server if needed,
    mint this session's identity, and join the public channel. `name` labels
    this session's user the first time it is created. Returns the room status."""
    data = _data_dir()
    data.mkdir(parents=True, exist_ok=True)
    server = _server(data)
    user = _user_id()
    token = _identity(server, data, user, name)
    _export_env(server, token)
    client = Client(server, token)
    try:
        try:
            client.channel_create(name=channel, public=True,
                                  description="Findings, claims and results of this agent tree.")
        except ClientError:
            pass  # already created by another member
        try:
            client.channel_join(chat=f"#{channel}")
        except ClientError:
            pass  # already a member
        who = client.whoami()
    finally:
        client.close()
    return {
        "server": server,
        "user": user,
        "name": who["user"]["name"],
        "channel": f"#{channel}",
        "data_dir": str(data),
    }


def _select(chat: str | None, select: dict) -> dict:
    """The server-side message filters, with `from_` spelling the reserved word."""
    out = {"chat": chat or f"#{DEFAULT_CHANNEL}", **select}
    if "from_" in out:
        out["from"] = out.pop("from_")
    return out


async def send(text: str, chat: str | None = None, reply_to: str | None = None, attach=()) -> dict:
    """Post `text` to the room channel (or another chat) from this session."""
    client = _client()
    try:
        return client.send(chat=chat or f"#{DEFAULT_CHANNEL}", text=text,
                           attach=attach, reply_to=reply_to)
    finally:
        client.close()


async def read(chat: str | None = None, last: int | None = None, all_: bool = False,
               **select) -> dict:
    """Read this session's channel messages, oldest first, and mark them read.
    No arguments: the messages new for this session (unread). `last=N`: the
    newest N of history instead. `all_=True`: the whole history. `from_=...`,
    `mentions=True`, `since=...` and `before=...` filter further."""
    client = _client()
    try:
        params = _select(chat, select)
        if last is not None:
            params["last"] = last
        if all_:
            params["all"] = True
        return client.read(**params)
    finally:
        client.close()


async def wait(timeout: float = 60.0, chat: str | None = None, **select) -> dict:
    """Block up to `timeout` (hard-capped at 280s) until a new channel message
    arrives; returns `{"messages": [...]}` with an empty list on timeout.
    Prefer `wait_command()` in background bash for anything longer."""
    timeout = min(float(timeout), WAIT_HARD_CAP_S)
    client = _client()
    try:
        return client.wait(timeout=timeout, **_select(chat, select))
    finally:
        client.close()


async def wait_command(chat: str | None = None, timeout: float = 300.0,
                       from_: str | None = None) -> str:
    """A `bash()` command string that blocks until a new channel message arrives
    and prints it as JSON (`exit 0`), or exits 4 on timeout. Start it with
    `bash(cmd)`, end the turn, and the `[bash-done]` notification delivers the
    message — the notification-shaped wait the turn discipline asks for."""
    _client().close()  # boot the room and export SWARMCHAT_* for the CLI
    argv = [_kernel_python(), "-m", "swarmchat", "wait",
            "--timeout", str(float(timeout)), "--json"]
    if chat:
        argv += ["--chat", chat]
    if from_:
        argv += ["--from", from_]
    return " ".join(shlex.quote(a) for a in argv)
