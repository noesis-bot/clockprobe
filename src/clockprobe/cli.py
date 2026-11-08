"""Command line: clockprobe [options] -- <test command>"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import shlex
import sys
import traceback
from typing import List, Optional

from . import __version__
from .core import format_report, probe
from .runner import Case, clock_env, find_libfaketime, run_cases
from .scenarios import DEFAULT_SPECS, EXTREME_ZONES, default_matrix, parse_base, zone

EPILOG = """examples:
  clockprobe --junit -- pytest -q --junitxml={junit}
  clockprobe --each 'tests/test_*.py' --runner 'python3 {}' --tz Europe/Berlin
  clockprobe --base 2026-10-29T12:00 --at +1d --at midnight -- make test

clock specs for --at: +1d, +7d, -3h, +2w (units s, m, h, d, w), midnight, before-midnight, month-end, new-year, dst, or an ISO datetime.
Exit code: 0 no flips, 1 at least one test flipped, 2 usage or setup error found before the run, or a first baseline run without test results (checked after all scenarios ran), 3 unexpected error (with a traceback)."""


_NEG_SPEC = re.compile(r"^-\d+(?:\.\d+)?[smhdw]$")


def _allow_negative_specs(argv: List[str]) -> List[str]:
    """argparse reads `--at -3h` as a missing value followed by an unknown option. Join such pairs into `--at=-3h` (only before `--`)."""
    out: List[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            out.extend(argv[i:])
            break
        if arg == "--at" and i + 1 < len(argv) and _NEG_SPEC.match(argv[i + 1]):
            out.append(f"--at={argv[i + 1]}")
            i += 2
            continue
        out.append(arg)
        i += 1
    return out


def local_zone_name(localtime: str = "/etc/localtime") -> Optional[str]:
    """IANA name of the system zone from the /etc/localtime symlink (Linux, macOS), or None (also for names
    without a slash other than UTC, such as CET). Without a name the local zone is only a
    fixed UTC offset, and the DST scenario cannot be built."""
    try:
        target = os.path.realpath(localtime)
    except OSError:
        return None
    marker = "zoneinfo/"
    if marker not in target:
        return None
    name = target.split(marker, 1)[1]
    return name if "/" in name or name.upper() == "UTC" else None


def _duration(text: str) -> Optional[dt.timedelta]:
    text = text.strip()
    if not text:
        raise ValueError("--resolution needs a value such as 1h, 10m or 0")
    if text in ("0", "off", "none"):
        return None
    unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}[text[-1]] if text[-1] in "smhd" else 1
    num = float(text[:-1]) if text[-1] in "smhd" else float(text)
    if num * unit < 1:   # bisection steps are whole seconds; a resolution below one second would never be reached
        raise ValueError(f"--resolution {text}: use at least one second (or 0 to skip bisection)")
    return dt.timedelta(seconds=num * unit)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clockprobe", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG,
                                description="Run a test suite under shifted clocks and report the tests that only fail there.")
    p.add_argument("--version", action="version", version=f"clockprobe {__version__}")
    p.add_argument("--base", help="baseline time (ISO; default: now). Naive values are read in --tz")
    p.add_argument("--tz", default=os.environ.get("TZ") or None, help="time zone of the baseline (IANA name; default: $TZ, else the zone /etc/localtime resolves to if that name contains a slash or is UTC; otherwise the current local UTC offset without DST)")
    p.add_argument("--at", action="append", metavar="SPEC", help=f"clock scenario (repeatable; default: {' '.join(DEFAULT_SPECS)})")
    p.add_argument("--zone", action="append", metavar="TZ",
                   help=f"also run at the base time in this zone (repeatable; skipped if it is the base zone; default: {' '.join(EXTREME_ZONES)}); --zone none for none")
    p.add_argument("--junit", action="store_true",
                   help="the command after -- writes JUnit XML to {junit}; each distinct classname::name of a <testcase> is one test (duplicates are merged, red wins; not for --cmd/--each)")
    p.add_argument("--each", metavar="GLOB", help="one test per matching file, run with --runner")
    p.add_argument("--runner", default="{}", help="command for --each, {} is the file (default: run the file)")
    p.add_argument("--cmd", action="append", metavar="CMD", help="a test command, exit 0 = pass (repeatable)")
    p.add_argument("--cwd", default=None, help="working directory for the tests")
    p.add_argument("--copy", action="store_true",
                   help="run every pass in a fresh copy of the working directory (for suites that write into their checkout)")
    p.add_argument("--timeout", type=float, default=600, help="seconds per command (default 600)")
    p.add_argument("--baseline-runs", type=int, default=2, help="baseline repetitions; tests that differ are not judged (default 2)")
    p.add_argument("--resolution", default="1h", help="bisection resolution, e.g. 1h, 10m; 0 to skip bisection (default 1h)")
    p.add_argument("--fake-stat", action="store_true", help="also shift file timestamps (off by default: causes false alarms)")
    p.add_argument("--no-confirm", action="store_true",
                   help="do not re-run red results (baseline, scenario and bisection runs) with the other file-time mode")
    p.add_argument("--libfaketime", help="path to libfaketime (default: search common locations, $CLOCKPROBE_LIBFAKETIME)")
    p.add_argument("--json", metavar="FILE", help="write the report as JSON")
    p.add_argument("--list", action="store_true", help="only print the scenarios")
    p.add_argument("command", nargs=argparse.REMAINDER, help="test command after --")
    return p


def cases_from_args(a) -> List[Case]:
    cases: List[Case] = []
    cmd = list(a.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if cmd:
        text = " ".join(shlex.quote(c) if not ("{junit}" in c) else c for c in cmd)
        cases.append(Case(name=text if not a.junit else "", cmd=text, junit="" if a.junit else None))
    for c in a.cmd or []:
        cases.append(Case(name=c, cmd=c))
    if a.each:
        root = a.cwd or "."
        for f in sorted(glob.glob(os.path.join(root, a.each))):
            if not os.path.isfile(f):   # a matching directory (e.g. __pycache__ for 'tests/*') is not a test
                continue
            rel = os.path.relpath(f, root)
            path = rel if os.path.dirname(rel) else os.path.join(".", rel)   # a bare name would be looked up in PATH
            cases.append(Case(name=rel, cmd=a.runner.replace("{}", shlex.quote(path))))
    for c in cases:
        if c.junit is not None and not c.name:
            c.name = "junit"
    return cases


def main(argv: Optional[List[str]] = None) -> int:
    """Exit code 3 for an unexpected error, so that 1 always means a flip."""
    try:
        return _main(argv)
    except Exception:   # SystemExit (argparse, --help) and KeyboardInterrupt pass through
        traceback.print_exc()
        print("clockprobe: unexpected error (exit code 3).", file=sys.stderr)
        return 3


def _main(argv: Optional[List[str]] = None) -> int:
    a = build_parser().parse_args(_allow_negative_specs(list(sys.argv[1:] if argv is None else argv)))
    if not a.tz:
        a.tz = local_zone_name()
        if not a.tz:
            print("clockprobe: no IANA time zone found ($TZ unset or empty, /etc/localtime not a link to a zoneinfo name with a slash or UTC); using the local UTC offset, "
                  "so there is no DST scenario. Pass --tz to fix this.", file=sys.stderr)
    try:
        if a.tz:
            zone(a.tz)   # check an unknown --tz here, before any run; not every base time or scenario needs the zone
        base = parse_base(a.base, a.tz)
        zones = [] if a.zone == ["none"] else a.zone
        for z in zones or []:
            zone(z)
        scenarios = default_matrix(base, a.tz, a.at, zones)
        resolution = _duration(a.resolution)
    except (ValueError, KeyError, OverflowError) as e:   # OverflowError: out-of-range values such as --resolution inf
        print(f"clockprobe: {e}", file=sys.stderr)
        return 2
    if a.list:
        print(f"base {base.isoformat()} ({a.tz or 'local zone'})")
        for sc in scenarios:
            print(f"  {sc.label:<28} {sc.local().isoformat():<32} {sc.tz or ''}")
        return 0
    cases = cases_from_args(a)
    if not cases:
        print("clockprobe: no tests given (use -- <command>, --cmd or --each)", file=sys.stderr)
        return 2
    if a.cwd and not os.path.isdir(a.cwd):
        print(f"clockprobe: --cwd {a.cwd}: no such directory.", file=sys.stderr)
        return 2
    if a.json and not os.path.isdir(os.path.dirname(os.path.abspath(a.json))):
        print(f"clockprobe: --json {a.json}: directory does not exist.", file=sys.stderr)
        return 2
    if a.json and os.path.isdir(a.json):
        print(f"clockprobe: --json {a.json}: is a directory.", file=sys.stderr)
        return 2
    lib = find_libfaketime(a.libfaketime)
    if not lib and a.libfaketime:
        print(f"clockprobe: --libfaketime {a.libfaketime}: no such file.", file=sys.stderr)
        return 2
    if not lib:
        print("clockprobe: libfaketime not found. Install it (apt install faketime, brew install libfaketime) "
              "or pass --libfaketime PATH.", file=sys.stderr)
        return 2
    base_env = dict(os.environ)

    # The clock environment is built per command (a function, not a dict), so each command starts at the scenario time.
    def runner(instant, tz, only):
        return run_cases(cases, lambda: clock_env(base_env, lib, instant, tz, fake_stat=a.fake_stat), a.cwd, a.timeout, only,
                         copy=a.copy)

    def confirm(instant, tz, only):
        return run_cases(cases, lambda: clock_env(base_env, lib, instant, tz, fake_stat=not a.fake_stat), a.cwd, a.timeout, only,
                         copy=a.copy)

    def plain(only):
        env = dict(base_env)
        if a.tz:
            env["TZ"] = a.tz
        return run_cases(cases, env, a.cwd, a.timeout, only, copy=a.copy)

    rep = probe(runner, base, a.tz, scenarios, baseline_runs=a.baseline_runs, resolution=resolution,
                log=lambda s: print(s, file=sys.stderr, flush=True), confirm=None if a.no_confirm else confirm, plain=plain)
    print(format_report(rep))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(rep.as_dict(), fh, indent=2)
    if rep.errors and not rep.baseline:
        return 2
    if rep.baseline and all(t.endswith(" [no results]") for t in rep.baseline):
        print("clockprobe: the baseline produced no test results (JUnit file missing or unreadable; is {junit} in the command?), "
              "so nothing could be judged.", file=sys.stderr)
        return 2
    return 1 if rep.flips else 0
