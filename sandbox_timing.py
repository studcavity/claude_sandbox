"""sandbox_timing.py - where does a launch spend its time? (`--timings`)

A tiny, stdlib-only stopwatch for the launcher. Two kinds of measurement:

  stages    named, nestable spans of the launch (`with stage("clone"): ...`),
  counters  how many subprocess/GitHub calls were made and what they cost in
            total, fed from the two real I/O edges (sandbox_util.run and
            sandbox_github._gh), so the numbers are what actually ran.

Off by default and free when off: every helper first checks ACTIVE, and the
edges don't even read the clock. `mark("prompt")` is the headline number: how
long until the interactive session started.
"""
import collections
import contextlib
import json
import os
import time

Row = collections.namedtuple("Row", "name depth start seconds")
ACTIVE = None   # the Timings in use, or None


class Timings:
    def __init__(self, clock=time.perf_counter):
        self.clock = clock
        self.t0 = clock()
        self.rows = []                # finished stages, in completion order
        self.counters = {}            # label -> [calls, seconds]
        self.marks = {}               # name -> seconds since start (first wins)
        self._depth = 0

    @contextlib.contextmanager
    def stage(self, name):
        start, depth = self.clock(), self._depth
        self._depth += 1
        try:
            yield
        finally:      # also on exceptions and Ctrl-C: the report must survive them
            self._depth -= 1
            self.rows.append(Row(name, depth, start - self.t0, self.clock() - start))

    def count(self, label, seconds):
        c = self.counters.setdefault(label, [0, 0.0])
        c[0] += 1
        c[1] += seconds

    def mark(self, name):
        self.marks.setdefault(name, self.clock() - self.t0)

    def total(self):
        return self.clock() - self.t0

    def to_dict(self):
        return {
            "version": 1,
            "total_seconds": round(self.total(), 3),
            "marks": {k: round(v, 3) for k, v in self.marks.items()},
            "stages": [{"name": r.name, "depth": r.depth, "start": round(r.start, 3),
                        "seconds": round(r.seconds, 3)}
                       for r in sorted(self.rows, key=lambda r: (r.start, r.depth))],
            "counters": {k: {"calls": c[0], "seconds": round(c[1], 3)}
                         for k, c in sorted(self.counters.items(),
                                            key=lambda kv: -kv[1][1])},
        }

    def report(self):
        d = self.to_dict()
        total = d["total_seconds"] or 1e-9
        lines = ["timings", f"  {'stage':<42}{'secs':>8}{'share':>8}"]
        for r in d["stages"]:
            share = f"{100 * r['seconds'] / total:5.0f}%" if r["depth"] == 0 else ""
            lines.append(f"  {'  ' * r['depth'] + r['name']:<42}{r['seconds']:>8.2f}{share:>8}")
        prompt = d["marks"].get("prompt")
        if prompt is not None:
            lines.append(f"  -> the session prompt appeared after {prompt:.2f} s "
                         f"({100 * prompt / total:.0f}% of {total:.2f} s total)")
        if d["counters"]:
            lines.append("  calls (count, total seconds; nested calls overlap stages):")
            lines += [f"    {k:<30}{v['calls']:>5}{v['seconds']:>9.2f}"
                      for k, v in d["counters"].items()]
        return "\n".join(lines)


# --- module-level helpers: no-ops unless a Timings is installed -------------- #
def install(t):
    global ACTIVE
    ACTIVE = t


def stage(name):
    return ACTIVE.stage(name) if ACTIVE else contextlib.nullcontext()


def mark(name):
    if ACTIVE:
        ACTIVE.mark(name)


def count(label, seconds):
    if ACTIVE:
        ACTIVE.count(label, seconds)


def label_for(cmd):
    """Counter label for a subprocess argv: `incus exec`, `incus launch`, `git`..."""
    exe = os.path.basename(str(cmd[0])) if cmd else "?"
    if exe in ("incus", "lxc") and len(cmd) > 1:
        return f"{exe} {cmd[1]}"
    return exe


def write_json(t, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(t.to_dict(), f, indent=2)
        f.write("\n")
