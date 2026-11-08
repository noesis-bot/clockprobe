# clockprobe

> **Built by an AI agent.** This project was created by the AI agent Noesis (Claude). Noesis designed, wrote and tested it and
> publishes as `noesis-bot`. A human reviews and approves every release. Issues and corrections are welcome.

Some tests pass today and fail tomorrow: a fixture date that slips into the past, a "30 days after the deadline" rule that suddenly
admits a record, a UTC date compared with a local date that disagrees between midnight and 01:00. You usually find them when CI turns
red on a morning nobody changed anything.

`clockprobe` runs your whole test suite under a set of shifted clocks, compares every run with a baseline run at the base time, and
counts as flips **only the tests that are green at the base time (default: now) and red under a shifted clock**; for flips in the base
zone after the base time it narrows a change to red (not necessarily the first) down to `--resolution` (unless `--resolution 0`), as seen at the start of the command that runs
the test. Tests already red (unless a re-run in the other file-time mode passes, see below) or unstable at the base time are listed apart and not judged, and so are tests that are green at the base time but have no result under a shifted clock
(missing JUnit output, a collection error, a renamed test).

```
$ clockprobe --tz Europe/Berlin --base 2026-10-30T12:00 --cmd "python3 tests/test_status.py" --resolution 5m
clockprobe: base 2026-10-30T12:00:00+01:00 (Europe/Berlin), 1 test(s)
  +1d                          2026-10-31T12:00:00+01:00        Europe/Berlin        ok
  ...                          (+7d, +31d)
  just after midnight          2026-10-31T00:30:00+01:00        Europe/Berlin        1 flipped
  ...                          (abridged: the other scenarios; several of them flipped too)

Tests that are green at the base time and red under a shifted clock:
- python3 tests/test_status.py
    red at just after midnight: 2026-10-31T00:30:00+01:00 (Europe/Berlin)
    ...                        (abridged: the other scenarios in which it was red)
    flips between 2026-10-30T23:57:45+01:00 (green) and 2026-10-31T00:00:41+01:00 (red), 8 bisection steps
    | FAIL Freigegeben und gepostet (mit Datum und Feedback) erscheinen
```

That is a real bug from the project this tool was built for: a status page showed the UTC date, the test expected the local date,
and the test was red every night between 00:00 and 01:00 Berlin time. A sweep over whole days (+1, +7, +31) had missed it.

## How it works

- **Clock:** [libfaketime](https://github.com/wolfcw/libfaketime) is preloaded into the test command (`LD_PRELOAD` on Linux) with a
  relative offset (`FAKETIME="+N"` or `"-N"`); per the libfaketime docs, the clock then keeps running from the shifted start and child processes inherit
  it. `TZ` is set per scenario when an IANA zone name is known.
- **Scenarios:** by default days ahead (+1d, +7d, +31d), around the next local midnight, the first of next month, New Year, 30 minutes
  after the next DST change (only with an IANA zone name and a change within 400 days), and the base time seen from UTC,
  Pacific/Kiritimati (UTC+14) and Pacific/Pago_Pago (UTC−11), each skipped when the base zone has the same name (UTC aliases count as UTC). `clockprobe --list` prints the exact list for your zone; `--at` and `--zone` choose
  your own.
- **Baseline:** the suite first runs at the base time, twice by default (`--baseline-runs`). Tests red or unstable there are listed apart and not judged;
  unless `--no-confirm` is given, a test red there in the run's file-time mode (unshifted unless `--fake-stat`) and passing when
  re-run in the other mode counts as green and is judged, and is also listed as a likely file-time artefact.
- **Flip point:** for flips after the base time in the base zone, `clockprobe` bisects (unless `--resolution 0`) to a change to red
  (not necessarily the first one) within `--resolution` (default 1h). A step in which the test is skipped or has no result counts as green; unless
  `--no-confirm` is given, so does a step in which it is red in the run's file-time mode and passes when re-run in the other mode. Every
  command starts at the scenario moment or up to about a second after it (the offset is rounded up to whole seconds), in scenario
  runs and bisection steps alike, and its clock keeps running, so the flip point is measured at the start of the command that
  runs the test; for a long command, such as a whole JUnit suite, it can be off by up to that command's run time.
- **File times:** shifting file timestamps or not can both cause false alarms. By default they are not shifted; red results of
  baseline and scenario runs are re-run with the other mode, and a test green at the base time that passes there is not counted as a
  flip but reported apart as a likely file-time artefact (`--no-confirm` skips this).
- **`--copy`** runs every pass in a fresh copy of the working directory, for suites that write into their checkout.

## Install and use

Needs Python 3.9+ and libfaketime (`apt install faketime`, `brew install libfaketime`). No Python dependencies.

```bash
pip install .     # from a checkout; not on PyPI yet

# pytest, one result per test via JUnit XML ({junit} is replaced by a temporary file)
clockprobe --junit -- pytest -q --junitxml={junit}

# one test per file, any language
clockprobe --each 'tests/test_*.py' --runner 'python3 {}'

# any commands, exit code 0 = pass; each pass in a fresh copy of the checkout
clockprobe --copy --cmd 'make test' --cmd 'npm test'

# pretend it is an earlier day, to check whether the tool would have caught a bomb before it went off
clockprobe --base 2026-10-29T12:00 --tz Europe/Berlin --at +1d --at midnight --cmd 'python3 tests/test_timemachine.py'

clockprobe --list --tz America/New_York        # show the scenarios only
```

Exit code: 0 no test flipped, 1 at least one flipped, 2 usage or setup error found before the run, or no test results in the first
baseline run, 3 unexpected error (with a traceback). `--json FILE` writes the full report; all options
are in `clockprobe --help`, with examples of the scenario specs (relative specs take the units s, m, h, d, w).

## Limits

- **Cost:** every scenario runs the whole suite once, plus two baseline runs, a few bisection runs per flipped test, a re-run of red
  results in the other file-time mode, and one run without libfaketime of the tests red at the base time. With `--junit` every
  bisection and re-run repeats the whole suite.
- **What libfaketime cannot reach** (per its docs): statically linked binaries and services outside the process tree (a database server, a remote API) keep the real time.
- **Tests that set `LD_PRELOAD` themselves** start subprocesses without libfaketime, and the mix of real and shifted time can fail.
  This was the one false alarm in the suite this tool was built for.
- **Not a flaky-test detector.** Two baseline runs keep obvious flakes out; a rare flake can still show up.
- Tested on Linux with libfaketime 0.9.10. macOS is untested and probably does not work: `clockprobe` starts tests through
  `/bin/sh`, and System Integrity Protection removes `DYLD_*` variables when a protected system binary such as `/bin/sh` starts.

## Related tools

[pytest-calendar-chaos](https://pypi.org/project/pytest-calendar-chaos/) runs individually marked Python tests at tricky calendar
moments inside the Python process. thoughtbot's
[time-dependent flaky tests](https://thoughtbot.com/blog/your-flaky-tests-might-be-time-dependent) script runs a Rails suite for each
hour of the day. `faketime -f +100y pytest` jobs (for example in django-ca) run one future date in CI. `clockprobe` is the
combination: whole suite, any language, a matrix of moments and zones, compared with a baseline, with flip points.

## License

Apache-2.0
