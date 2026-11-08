"""Run test cases under a shifted clock (libfaketime) and collect per-test results."""
from __future__ import annotations

import datetime as dt
import glob
import math
import os
import platform
import shlex
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Union

from .scenarios import UTC

LIB_CANDIDATES = [
    "/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1",
    "/usr/lib/aarch64-linux-gnu/faketime/libfaketime.so.1",
    "/usr/lib/faketime/libfaketime.so.1",
    "/usr/local/lib/faketime/libfaketime.so.1",
    "/usr/lib64/faketime/libfaketime.so.1",
    "/opt/homebrew/lib/faketime/libfaketime.1.dylib",
    "/usr/local/lib/faketime/libfaketime.1.dylib",
]

PASS, FAIL, SKIP = "pass", "fail", "skip"


def find_libfaketime(explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        return explicit if os.path.exists(explicit) else None
    env = os.environ.get("CLOCKPROBE_LIBFAKETIME")
    if env and os.path.exists(env):
        return env
    for c in LIB_CANDIDATES:
        if os.path.exists(c):
            return c
    for pat in ("/usr/lib/*/faketime/libfaketime.so.1", "/nix/store/*/lib/libfaketime.so.1"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return None


def clock_env(base_env: Dict[str, str], lib: str, instant: dt.datetime, tz: Optional[str],
              fake_stat: bool = False) -> Dict[str, str]:
    """Environment whose processes see `instant` (offset rounded up to the next whole second) plus the real time elapsed since the
    environment was built (per the libfaketime docs, a relative FAKETIME offset keeps the clock running).

    The offset is relative to the real clock at the moment the environment is built, so it is unambiguous across DST changes.
    File timestamps are NOT shifted by default (NO_FAKE_STAT=1). Both modes cause false alarms: unshifted, a freshly written file
    looks as old as the clock offset; shifted, an mtime set from the clock and read back carries the offset twice. Red results are
    re-run in the other mode unless --no-confirm is given.
    """
    env = dict(base_env)
    offset = (instant.astimezone(UTC) - dt.datetime.now(UTC)).total_seconds()
    # Round up: the child starts a few milliseconds later and must never see a moment before `instant` (rounding to the nearest
    # second let a child see 23:59:59 at a scenario placed exactly on midnight).
    env["FAKETIME"] = f"{math.ceil(offset):+d}"
    # Do not set FAKETIME_DONT_FAKE_MONOTONIC: observed with libfaketime 0.9.10 and Python 3.11+, time.sleep() then failed with
    # EINVAL, which turned tests that sleep red.
    env.pop("FAKETIME_DONT_FAKE_MONOTONIC", None)
    env["FAKETIME_DONT_RESET"] = "1"            # per the libfaketime docs: child processes keep the start time instead of resetting it
    if fake_stat:
        env.pop("NO_FAKE_STAT", None)   # removed rather than set to 0: per the libfaketime docs its presence disables stat faking
    else:
        env["NO_FAKE_STAT"] = "1"
    if platform.system() == "Darwin":
        env["DYLD_INSERT_LIBRARIES"] = lib + (":" + env["DYLD_INSERT_LIBRARIES"] if env.get("DYLD_INSERT_LIBRARIES") else "")
        env["DYLD_FORCE_FLAT_NAMESPACE"] = "1"
    else:
        env["LD_PRELOAD"] = lib + (":" + env["LD_PRELOAD"] if env.get("LD_PRELOAD") else "")
    if tz:
        env["TZ"] = tz
    env["CLOCKPROBE_NOW"] = instant.isoformat()
    return env


@dataclass
class Case:
    """A command. With junit set, the command writes a JUnit XML file and each distinct classname::name of a <testcase> is one test (duplicates are merged, red wins); otherwise the command is one
    test, passed when it exits 0."""
    name: str
    cmd: str
    junit: Optional[str] = None


@dataclass
class RunResult:
    results: Dict[str, str] = field(default_factory=dict)   # test id -> pass/fail/skip
    errors: List[str] = field(default_factory=list)
    output: Dict[str, str] = field(default_factory=dict)    # test id -> the output lines that most likely say what failed (the tail if none stands out); with JUnit, taken from the output of the whole command


_RANK = {SKIP: 0, PASS: 1, FAIL: 2}


def parse_junit(path: str) -> Dict[str, str]:
    root = ET.parse(path).getroot()
    out: Dict[str, str] = {}
    for tc in root.iter("testcase"):
        tid = (tc.get("classname") + "::" if tc.get("classname") else "") + (tc.get("name") or "?")
        kinds = {child.tag for child in tc}
        if "failure" in kinds or "error" in kinds:
            st = FAIL
        elif "skipped" in kinds:
            st = SKIP
        else:
            st = PASS
        # Several <testcase> elements with the same id are one test: red wins over green, green over skipped.
        if tid not in out or _RANK[st] > _RANK[out[tid]]:
            out[tid] = st
    return out


_FAIL_WORDS = ("FAIL", "Error", "error:", "assert", "Traceback", "expected")


def failure_lines(out: str, n: int = 8) -> str:
    """The lines of a test's output that most likely say what failed; the tail if none stands out."""
    lines = out.splitlines()
    hits = [s for s in lines if any(w in s for w in _FAIL_WORDS) and not s.lstrip().startswith("PASS")]
    return "\n".join((hits or lines)[-n:])


def run_case(case: Case, env: Dict[str, str], cwd: Optional[str], timeout: float) -> RunResult:
    rr = RunResult()
    junit_path = None
    cmd = case.cmd
    if case.junit is not None:
        fd, junit_path = tempfile.mkstemp(prefix="clockprobe-", suffix=".xml")
        os.close(fd)
        os.unlink(junit_path)
        cmd = cmd.replace("{junit}", shlex.quote(junit_path))
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, env=env, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        code, out = p.returncode, p.stdout
    except subprocess.TimeoutExpired as e:
        # On POSIX, TimeoutExpired.stdout holds bytes (or None) even with text=True; keep what the test printed.
        raw = e.stdout or ""
        code, out = None, raw.decode(errors="replace") if isinstance(raw, bytes) else raw
    tail = failure_lines(out)
    if case.junit is None:
        rr.results[case.name] = PASS if code == 0 else FAIL
        if code != 0:
            rr.output[case.name] = tail if code is not None else f"timeout after {timeout:.0f}s\n{tail}"
        return rr
    try:
        res = parse_junit(junit_path) if junit_path and os.path.exists(junit_path) else {}
    except ET.ParseError as e:
        res = {}
        rr.errors.append(f"{case.name}: unreadable JUnit XML ({e})")
    finally:
        if junit_path and os.path.exists(junit_path):
            os.unlink(junit_path)
    if not res:
        rr.errors.append(f"{case.name}: no JUnit results (exit {code})")
        rr.results[case.name + " [no results]"] = FAIL
        rr.output[case.name + " [no results]"] = tail
    for tid, st in res.items():
        rr.results[f"{case.name}::{tid}" if case.name else tid] = st
        if st == FAIL:
            rr.output[f"{case.name}::{tid}" if case.name else tid] = tail
    return rr


def run_cases(cases: List[Case], env: Union[Dict[str, str], Callable[[], Dict[str, str]]], cwd: Optional[str], timeout: float,
              only: Optional[List[str]] = None, copy: bool = False) -> RunResult:
    """Run the cases (all, or those containing a test in `only`). With copy, the working directory is copied to a fresh temporary
    directory first, so files a test suite writes into its checkout do not leak from one clock into the next.

    env may be a function; it is then called once per case, right before the case starts. The shifted clock is built that way, so every
    command starts at the scenario time, as it does when it runs alone in a bisection step (with one environment for all commands, later
    commands saw the scenario time plus the run time of the earlier ones)."""
    total = RunResult()
    selected = [c for c in cases if only is None or any(t == c.name or t.startswith(c.name + "::") for t in only)]
    if not selected:
        return total
    tmp = None
    run_dir = cwd
    if copy:
        tmp = tempfile.mkdtemp(prefix="clockprobe-copy-")
        run_dir = os.path.join(tmp, "work")
        shutil.copytree(cwd or ".", run_dir, symlinks=True)
    try:
        for c in selected:
            r = run_case(c, env() if callable(env) else env, run_dir, timeout)
            total.results.update(r.results)
            total.errors.extend(r.errors)
            total.output.update(r.output)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return total
