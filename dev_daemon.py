"""Daemonize the Streamlit dev server so it survives between tool calls."""
import os
import sys
import time

LOG = "/tmp/rbxscout.log"


def daemonize() -> None:
    if os.fork() > 0:
        sys.exit(0)  # parent exits
    os.setsid()  # new session -> escapes process-group kills
    if os.fork() > 0:
        sys.exit(0)
    sys.stdout.flush()
    sys.stderr.flush()
    devnull = os.open(os.devnull, os.O_RDONLY)
    out = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    os.dup2(devnull, 0)
    os.dup2(out, 1)
    os.dup2(out, 2)


def wait_http(port: int, timeout: float = 40.0) -> bool:
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://localhost:{port}", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(1)
    return False


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8601
    daemonize()
    os.execvp(
        sys.executable,
        [sys.executable, "-m", "streamlit", "run", "app.py",
         "--server.port", str(port), "--server.headless", "true"],
    )
