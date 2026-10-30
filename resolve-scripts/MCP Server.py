#!/usr/bin/env python
r"""
MCP Server — runs INSIDE DaVinci Resolve and serves over HTTP.

This is the route for the **free edition** of DaVinci Resolve, where the
normal stdio server cannot work. Studio users do not need it.

Why
---
Resolve refuses `scriptapp("Resolve")` to foreign processes unless you are on
Studio, so an external MCP server has nothing to talk to. A script launched
from Resolve's own Scripts menu, however, is handed a live `resolve` object.
So the server is hosted inside such a script, and the MCP client connects to
it over HTTP instead of spawning it over stdio.

Install
-------
1. Set PROJECT below to your clone of this repository.
2. Copy this file into Resolve's Utility scripts folder:
     Windows : %APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility
     macOS   : ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility
     Linux   : ~/.local/share/DaVinciResolve/Fusion/Scripts/Utility
3. Point your MCP client at the HTTP endpoint instead of the stdio command:
     {"resolve": {"type": "http", "url": "http://127.0.0.1:8765/mcp"}}

Use
---
Launch from Workspace > Scripts > MCP Server after opening a project. The
script does not return — it serves until you stop it. Resolve has to stay
open, and the script has to be launched again each session.
"""

import glob
import io
import os
import site
import sys
import tempfile
import traceback

# ── EDIT THIS: your clone of resolve-claude-mcp ──
PROJECT = os.environ.get("RESOLVE_CLAUDE_MCP_HOME") or r"C:\path\to\resolve-claude-mcp"

HOST = os.environ.get("RESOLVE_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("RESOLVE_MCP_PORT", "8765"))


def _log_path():
    """Log beside this script when possible, otherwise in the temp dir."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        here = None
    for d in (here, tempfile.gettempdir()):
        if d and os.path.isdir(d):
            p = os.path.join(d, "mcp_server.log")
            try:
                open(p, "a").close()
                return p
            except Exception:
                continue
    return None


LOG = _log_path()


def log(msg):
    line = "%s\n" % msg
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except Exception:
        pass
    if LOG:
        try:
            with open(LOG, "a") as f:
                f.write(line)
        except Exception:
            pass


# ── the connection Resolve injected into this script ──
# This is the whole point: it exists only because Resolve started us.
_INJECTED = globals().get("resolve")

if LOG:
    try:
        open(LOG, "w").close()
    except Exception:
        pass

log("=" * 60)
log("Resolve MCP host starting")
log("pid           : %s" % os.getpid())
log("interpreter   : %s %s" % (sys.executable, sys.version.split()[0]))
log("injected      : %r" % (_INJECTED,))

if _INJECTED is None:
    log("")
    log("FATAL: no `resolve` object in globals.")
    log("Launch this from Resolve's Workspace > Scripts menu, not a terminal.")
    raise SystemExit(1)

try:
    log("product       : %s %s" % (_INJECTED.GetProductName(),
                                   _INJECTED.GetVersionString()))
except Exception as e:
    log("product       : FAILED: %s" % e)


# ── make the project's own code importable in Resolve's interpreter ──
def _site_packages(venv):
    cands = [os.path.join(venv, "Lib", "site-packages")]
    cands += sorted(glob.glob(os.path.join(venv, "lib", "python3.*", "site-packages")))
    return [c for c in cands if os.path.isdir(c)]


VENV_SITE = _site_packages(os.path.join(PROJECT, ".venv"))
SRC = os.path.join(PROJECT, "src")

if not VENV_SITE:
    log("")
    log("FATAL: no virtualenv found under %s" % os.path.join(PROJECT, ".venv"))
    log("Set PROJECT at the top of this script to your clone, then run `uv sync`.")
    raise SystemExit(1)

# Prepend rather than only addsitedir: Resolve's interpreter already carries
# the system Python's site-packages, and the project's pinned versions should
# win. addsitedir as well, so editable-install .pth files get processed.
for sp in VENV_SITE:
    site.addsitedir(sp)
for p in VENV_SITE + [SRC]:
    while p in sys.path:
        sys.path.remove(p)
    sys.path.insert(0, p)

try:
    import resolve_claude_mcp.connection as conn_mod
except Exception:
    log("FATAL: cannot import resolve_claude_mcp from %s" % SRC)
    log(traceback.format_exc())
    raise SystemExit(1)


# ── hand the injected object to the connection layer ──
# Everything else in ResolveConnection (the RLock, the fresh accessors,
# execute_code) stays as it is; only the way it acquires `resolve` changes,
# because scriptapp() is exactly what the free edition denies us.
def _connect_via_injection(self):
    with self._lock:
        if self.resolve is None:
            self.resolve = _INJECTED
        return self.resolve is not None


conn_mod.ResolveConnection.connect = _connect_via_injection

try:
    conn = conn_mod.get_resolve_connection()
    log("connection    : ok, alive=%s" % conn.is_alive())
except Exception as e:
    log("connection    : FAILED: %s" % e)
    log(traceback.format_exc())
    raise SystemExit(1)


# ── make Resolve's stdout look like a real stream ──
# Resolve swaps sys.stdout/stderr for its `fu_stdout` module, which has
# write() but none of the rest of the file protocol. uvicorn's log formatter
# calls isatty(), so wrap both streams in a thin proxy.
class _StreamShim:
    def __init__(self, wrapped):
        self._w = wrapped

    def write(self, s):
        try:
            return self._w.write(s)
        except Exception:
            return len(s)

    def flush(self):
        try:
            self._w.flush()
        except Exception:
            pass

    def isatty(self):
        return False

    def fileno(self):
        raise io.UnsupportedOperation("fileno")

    def writable(self):
        return True

    def readable(self):
        return False

    def seekable(self):
        return False

    @property
    def encoding(self):
        return getattr(self._w, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self._w, "errors", "replace")

    @property
    def closed(self):
        return False

    def close(self):
        pass


for _name in ("stdout", "stderr"):
    _stream = getattr(sys, _name, None)
    if _stream is None or not hasattr(_stream, "isatty"):
        setattr(sys, _name,
                _StreamShim(_stream if _stream is not None else io.StringIO()))

# ── start the server ──
try:
    from resolve_claude_mcp.server import mcp
except Exception:
    log("FATAL: cannot import the MCP server")
    log(traceback.format_exc())
    raise SystemExit(1)

mcp.settings.host = HOST
mcp.settings.port = PORT
# Stateless: each request stands alone, so a client restarting does not leave
# the server holding a session id that no longer exists.
mcp.settings.stateless_http = True

log("tools         : %s" % len(getattr(mcp._tool_manager, "_tools", {})))
log("mcp from      : %s" % getattr(sys.modules.get("mcp"), "__file__", "?"))
log("listening     : http://%s:%s/mcp" % (HOST, PORT))
log("log           : %s" % LOG)
log("=" * 60)
log("Serving. Close this window to stop.")

try:
    mcp.run(transport="streamable-http")
except KeyboardInterrupt:
    log("stopped by interrupt")
except Exception:
    log("server crashed:")
    log(traceback.format_exc())
