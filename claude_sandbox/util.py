"""util.py - tiny shared helpers: logging and subprocess wrapper."""
import contextlib
import signal
import subprocess
import sys
import time

from claude_sandbox import timing
def log(msg):
    print(f"\033[1;36m[sandbox]\033[0m {msg}", file=sys.stderr)


def run(cmd, check=True, capture=False, input=None):
    if timing.ACTIVE is None:
        return subprocess.run(cmd, check=check, text=True,
                              capture_output=capture, input=input)
    start = time.perf_counter()
    try:
        return subprocess.run(cmd, check=check, text=True,
                              capture_output=capture, input=input)
    finally:
        timing.count(timing.label_for(cmd),
                             time.perf_counter() - start)


@contextlib.contextmanager
def sigint_to_child():
    """Swallow Ctrl-C in this process so it reaches the interactive child.

    A no-op handler (not SIG_IGN) is used: handlers reset to default in the
    child on exec, so the child still sees SIGINT.
    """
    prev = signal.signal(signal.SIGINT, lambda *_: None)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, prev)
