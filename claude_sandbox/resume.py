"""resume.py - what the session is shown when the final check fails, and how the check output is run and trimmed."""

import collections
import re
import subprocess
import sys

from claude_sandbox import mergecheck


# The final check (`check:` in the deps file): see Sandbox.final_check.
CHECK_OUTPUT_CHARS = 6000   # per failing script: what the resumed session is shown


_CONTROL_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]")


def tail_text(text, limit=CHECK_OUTPUT_CHARS):
    """The last `limit` characters of `text`, minus terminal escapes and
    control characters (a test run's colours and progress bars), with a marker
    when something was cut."""
    text = _CONTROL_RE.sub("", text.replace("\r\n", "\n")).strip("\n")
    if len(text) <= limit:
        return text
    return "[... output cut ...]\n" + text[-limit:]


def check_failure_text(returncode):
    """Why one check script failed."""
    return ("timed out" if returncode == 124
            else f"exited with status {returncode}")


def resume_prompt(failures, can_push=True, merge_findings=()):
    """The prompt that reopens the session after a failed final check.

    `failures`: [(check entry, returncode, output[, branch])], in run order;
    `branch` is set when the scripts ran on each session branch. The text
    is the project's own test output, handed back to the session that wrote
    the code, in the container it ran in. `can_push`: whether the session
    has a remote to push to (not with --local, where committed branches are
    copied out when it exits). `merge_findings`: [(branch, problem)] from
    mergecheck, listed after the scripts' output.
    """
    if can_push:
        intro = ("The launcher's final check ran after you exited, and it "
                 "FAILED. The container and your push access are still alive, "
                 "but only until you exit again. Fix the failures below, re-run "
                 "each failing script until it passes, then commit and push "
                 "(new commits; never bypass or weaken the check) and stop.\n")
    else:
        intro = ("The launcher's final check ran after you exited, and it "
                 "FAILED. The container is still alive, but only until you "
                 "exit again. Fix the failures below, re-run each failing "
                 "script until it passes, then commit (new commits; never "
                 "bypass or weaken the check) and stop. There is no push: your "
                 "committed branches are copied out when you exit.\n")
    parts = [intro]
    for s, code, out, *rest in failures:
        branch = rest[0] if rest and rest[0] else None
        on = f" on branch `{branch}`" if branch else ""
        switch = f"`git switch {branch}`, then run" if branch else "Run"
        parts.append(f"## `{s['path']}` ({s['name']}){on}: "
                     f"{check_failure_text(code)}\n"
                     f"{switch} it from the repo root: `./{s['path']}`\n"
                     f"```\n{tail_text(out)}\n```\n")
    if merge_findings:
        parts.append(mergecheck.resume_text(merge_findings, can_push))
    return "\n".join(parts)


def run_streaming(cmd, limit=CHECK_OUTPUT_CHARS * 2, echo=None):
    """Run `cmd` with no stdin, echoing its combined output as it arrives and
    keeping the last `limit` characters. Returns (returncode, output)."""
    echo = echo or sys.stderr
    kept: collections.deque = collections.deque()
    size = 0
    with subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace") as proc:
        assert proc.stdout is not None
        for line in proc.stdout:
            echo.write(line)
            echo.flush()
            kept.append(line)
            size += len(line)
            while size > limit and len(kept) > 1:
                size -= len(kept.popleft())
        return proc.wait(), "".join(kept)


def ask_resume(names):
    """Interactive yes/no (default yes) to resume Claude on a failed final
    check; no answer (EOF) or no terminal is no."""
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"\nFinal check failed ({', '.join(names)}). Resume the "
                     "Claude session to fix it? [Y/n] "
                     ).strip().lower() in ("", "y", "yes")
    except EOFError:
        return False
