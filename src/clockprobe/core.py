"""The probe: baseline, scenarios, flips, flip points."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .runner import FAIL, PASS, RunResult
from .scenarios import Scenario

# runner(instant, tz, only) -> RunResult ; only = test ids to run (None = all)
Runner = Callable[[dt.datetime, Optional[str], Optional[List[str]]], RunResult]


@dataclass
class Flip:
    test: str
    scenario: str
    at: str                              # local time of the scenario
    tz: Optional[str]
    first_red: Optional[str] = None      # local time of a red moment at a flip found by bisection (not necessarily the first)
    last_green: Optional[str] = None
    steps: int = 0
    output: str = ""


@dataclass
class Report:
    base: str
    tz: Optional[str]
    baseline: Dict[str, str]
    unstable: List[str] = field(default_factory=list)       # passed in one baseline run, did not pass (failed, skipped, missing) in another
    baseline_red: List[str] = field(default_factory=list)   # red already at the base time: not judged
    harness_red: List[str] = field(default_factory=list)    # of those: green without libfaketime at the real current time, so possibly broken by the harness (or just by the base date)
    flips: List[Flip] = field(default_factory=list)
    artefacts: List[Flip] = field(default_factory=list)      # red only in one file-time mode: most likely a file-time artefact (or a flaky test)
    scenarios: List[Dict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict:
        return {
            "base": self.base, "tz": self.tz,
            "tests": len(self.baseline), "baseline_red": self.baseline_red, "harness_red": self.harness_red, "unstable": self.unstable,
            "scenarios": self.scenarios,
            "flips": [f.__dict__ for f in self.flips],
            "file_time_artefacts": [f.__dict__ for f in self.artefacts],
            "errors": self.errors,
        }


def bisect(runner: Runner, test: str, lo: dt.datetime, hi: dt.datetime, tz: Optional[str],
           resolution: dt.timedelta) -> tuple:
    """Narrow down a green -> red change between lo (green) and hi (red) for one test.

    Returns (last_green, first_red, steps). A resolution below one second is raised to one second. Between a green and a red point there is always at least one change; if the test goes red
    and green again several times in between, this finds one of the changes, not necessarily the first.
    """
    lo, hi = lo.astimezone(dt.timezone.utc), hi.astimezone(dt.timezone.utc)   # real elapsed time, not wall time (DST)
    resolution = max(resolution, dt.timedelta(seconds=1))   # whole-second steps: a finer resolution could never be reached
    steps = 0
    while hi - lo > resolution:
        mid = lo + (hi - lo) / 2
        mid = mid.replace(microsecond=0)
        if mid <= lo:   # truncation fell back onto lo (sub-second lo/hi): step one second, which lies below hi since hi - lo > resolution >= 1 s
            mid = lo + dt.timedelta(seconds=1)
        r = runner(mid, tz, [test])
        steps += 1
        if r.results.get(test) == FAIL:
            hi = mid
        else:
            lo = mid
    return lo, hi, steps


def judged(runner: Runner, confirm: Optional[Runner]) -> Callable:
    """Wrap runner: a test that fails is re-run with the other file-time mode (confirm). If it passes there, it counts as passed and is
    returned in the artefact list (most likely a file-time artefact of libfaketime, possibly a flaky test)."""
    def run(instant, tz, only):
        r = runner(instant, tz, only)
        red = [t for t, st in r.results.items() if st == FAIL]
        art: List[str] = []
        if confirm and red:
            c = confirm(instant, tz, red)
            art = [t for t in red if c.results.get(t) == PASS]
            for t in art:
                r.results[t] = PASS
        return r, art
    return run


def probe(runner: Runner, base: dt.datetime, tz: Optional[str], scenarios: List[Scenario],
          baseline_runs: int = 2, resolution: Optional[dt.timedelta] = dt.timedelta(hours=1),
          log: Callable[[str], None] = lambda s: None, confirm: Optional[Runner] = None,
          plain: Optional[Callable] = None) -> Report:
    """confirm: the same runner with the other file-time mode (libfaketime shifts stat() results or not). A test that is red in one
    mode and green in the other most likely fails because of how file times are faked (possibly it is flaky); in baseline runs every test
    of this kind is reported apart (whatever its baseline state), in scenario runs only baseline-green ones not already reported for the
    baseline; in bisection steps it counts as green."""
    run = judged(runner, confirm)
    runs: List[RunResult] = []
    base_art: set = set()
    for i in range(max(1, baseline_runs)):
        log(f"baseline run {i + 1}/{max(1, baseline_runs)} at {base.isoformat()} ({tz or 'local zone'})")
        r, art = run(base, tz, None)
        runs.append(r)
        base_art.update(art)
    rep = Report(base=base.isoformat(), tz=tz, baseline=dict(runs[0].results))
    for t in sorted(base_art):
        rep.artefacts.append(Flip(test=t, scenario="baseline", at=base.isoformat(), tz=tz))
    for r in runs:
        rep.errors.extend(r.errors)
    ids = sorted(set().union(*(r.results for r in runs)))
    green = []
    for t in ids:
        states = {r.results.get(t) for r in runs}
        if states == {PASS}:
            green.append(t)
        elif PASS in states:
            rep.unstable.append(t)
        elif FAIL in states:
            rep.baseline_red.append(t)

    if plain and rep.baseline_red:
        log(f"checking {len(rep.baseline_red)} test(s) red at base time without libfaketime")
        pr = plain(rep.baseline_red)
        rep.harness_red = [t for t in rep.baseline_red if pr.results.get(t) == PASS]

    def single(instant, tz_, only):
        return run(instant, tz_, only)[0]

    known: Dict[str, tuple] = {}   # test -> (last_green, first_red, steps) from an earlier scenario

    for sc in scenarios:
        log(f"scenario {sc.label}: {sc.local().isoformat()} ({sc.tz or 'local zone'})")
        r, art = run(sc.instant, sc.tz, None)
        rep.errors.extend(r.errors)
        red = [t for t in green if r.results.get(t) == FAIL]
        art = [t for t in art if t in green and t not in base_art]
        for t in art:
            rep.artefacts.append(Flip(test=t, scenario=sc.label, at=sc.local().isoformat(), tz=sc.tz))
        # Green at the base time but no result here (missing JUnit output, collection error, a renamed test): not a flip, but named.
        missing = [t for t in green if t not in r.results]
        rep.scenarios.append({"label": sc.label, "at": sc.local().isoformat(), "tz": sc.tz, "flipped": red,
                              "file_time_artefacts": art, "no_result": missing})
        for t in red:
            f = Flip(test=t, scenario=sc.label, at=sc.local().isoformat(), tz=sc.tz, output=r.output.get(t, ""))
            if resolution and sc.bisectable and sc.tz == tz and sc.instant > base.astimezone(dt.timezone.utc):
                if t in known and known[t][1] <= sc.instant:
                    lg, fr, _ = known[t]
                    steps = 0
                else:
                    lg, fr, steps = bisect(single, t, base, sc.instant, tz, resolution)
                    known[t] = (lg, fr, steps)
                z = sc.local().tzinfo
                f.last_green, f.first_red, f.steps = lg.astimezone(z).isoformat(), fr.astimezone(z).isoformat(), steps
            rep.flips.append(f)
    return rep


def format_report(rep: Report) -> str:
    lines = [f"clockprobe: base {rep.base} ({rep.tz or 'local zone'}), {len(rep.baseline)} test(s)"]
    if rep.baseline_red:
        names = ", ".join(rep.baseline_red[:10]) + (f", ... ({len(rep.baseline_red) - 10} more, all in --json)" if len(rep.baseline_red) > 10 else "")
        lines.append(f"  red at base time (not judged): {len(rep.baseline_red)}: {names}")
    if rep.harness_red:
        lines.append(f"  of these green without libfaketime at the real current time (possibly broken by the clock harness, or just red on the base date; not judged): {', '.join(rep.harness_red)}")
    if rep.unstable:
        lines.append(f"  unstable at base time (not judged): {', '.join(rep.unstable)}")
    for sc in rep.scenarios:
        mark = f"{len(sc['flipped'])} flipped" if sc["flipped"] else "ok"
        if sc.get("file_time_artefacts"):
            mark += f", {len(sc['file_time_artefacts'])} file-time artefact(s)"
        if sc.get("no_result"):
            mark += f", {len(sc['no_result'])} without result"
        lines.append(f"  {sc['label']:<28} {sc['at']:<32} {sc['tz'] or '':<20} {mark}")
    if not rep.flips:
        lines.append("No test flips from green to red under a shifted clock.")
    else:
        lines.append("")
        lines.append("Tests that are green at the base time and red under a shifted clock:")
        order: List[str] = []
        for f in rep.flips:
            if f.test not in order:
                order.append(f.test)
        for t in order:
            fl = [f for f in rep.flips if f.test == t]
            lines.append(f"- {t}")
            for f in fl:
                lines.append(f"    red at {f.scenario}: {f.at}{' (' + f.tz + ')' if f.tz else ''}")
            first = next((f for f in fl if f.first_red), None)
            if first:
                lines.append(f"    flips between {first.last_green} (green) and {first.first_red} (red), {first.steps} bisection steps")
            out = next((f.output for f in fl if f.output), "")
            if out:
                lines.extend("    | " + s for s in out.splitlines()[-5:])
    if rep.artefacts:
        lines.append("")
        lines.append("Red at the base time or under a shifted clock only with one file-time mode (most likely a file-time artefact or a flaky test; not counted):")
        for f in rep.artefacts:
            lines.append(f"- {f.test}: {f.scenario} ({f.at})")
    missing = [sc for sc in rep.scenarios if sc.get("no_result")]
    if missing:
        lines.append("")
        lines.append("Green at the base time, without a result under a shifted clock (not judged; check these by hand):")
        for sc in missing:
            names = sc["no_result"]
            more = f", ... ({len(names) - 10} more, all in --json)" if len(names) > 10 else ""
            lines.append(f"- {sc['label']} ({sc['at']}): {', '.join(names[:10])}{more}")
    for e in rep.errors:
        lines.append(f"error: {e}")
    return "\n".join(lines)
