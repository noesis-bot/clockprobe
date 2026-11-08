"""Tests for clockprobe. Run: python3 -m unittest discover -s tests (from the project root, with src on PYTHONPATH)."""
import datetime as dt
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from clockprobe import cli  # noqa: E402
from clockprobe.core import bisect, probe  # noqa: E402
from clockprobe.runner import FAIL, PASS, Case, RunResult, find_libfaketime, parse_junit, run_cases  # noqa: E402
from clockprobe.scenarios import default_matrix, dst_transitions, parse_at, parse_base, zone  # noqa: E402

BERLIN = "Europe/Berlin"
BASE = parse_base("2026-10-29T12:00", BERLIN)


def fake_runner(rules):
    """rules: test id -> function(local datetime) -> bool (True = passes). Counts calls."""
    calls = []

    def run(instant, tz, only):
        calls.append((instant, tz, only))
        local = instant.astimezone(zone(tz))
        rr = RunResult()
        for t, ok in rules.items():
            if only is None or t in only:
                rr.results[t] = PASS if ok(local) else FAIL
        return rr
    run.calls = calls
    return run


class Scenarios(unittest.TestCase):
    def test_calendar_days_keep_wall_time_across_dst(self):
        base = parse_base("2026-10-22T12:00", BERLIN)
        sc = parse_at("+7d", base, BERLIN)
        self.assertEqual(sc.local().hour, 12)
        self.assertEqual(sc.local().date(), dt.date(2026, 10, 29))

    def test_dst_transition_earlier_in_the_same_utc_hour_is_not_next(self):
        # Lord Howe moves to +11 on 2026-10-04 02:00 local = 2026-10-03 15:30 UTC; a base ten minutes later must not report it.
        tr = dst_transitions(parse_base("2026-10-03T15:40+00:00", "Australia/Lord_Howe"), "Australia/Lord_Howe")
        self.assertGreater(tr[0], parse_base("2026-10-03T15:40+00:00", "Australia/Lord_Howe"))
        self.assertEqual(tr[0].date(), dt.date(2027, 4, 4))

    def test_lowercase_utc_base_zone_skips_utc_scenario(self):
        labels = [sc.label for sc in default_matrix(BASE, "utc", specs=[])]
        self.assertNotIn("base time in UTC", labels)

    def test_utc_alias_base_zone_skips_utc_scenario(self):
        labels = [sc.label for sc in default_matrix(BASE, "Etc/UTC", specs=[])]
        self.assertNotIn("base time in UTC", labels)
        self.assertIn("base time in Pacific/Kiritimati", labels)

    def test_midnight_is_local(self):
        sc = parse_at("midnight", BASE, BERLIN)
        self.assertEqual(sc.local().isoformat(), "2026-10-30T00:30:00+01:00")
        self.assertEqual(sc.instant.astimezone(dt.timezone.utc).date(), dt.date(2026, 10, 29))  # UTC is still the day before

    def test_dst_transitions_berlin(self):
        tr = dst_transitions(parse_base("2026-10-01T12:00", BERLIN), BERLIN)
        self.assertEqual([t.date() for t in tr[:2]], [dt.date(2026, 10, 25), dt.date(2027, 3, 28)])

    def test_no_dst_zone_drops_dst_scenario(self):
        m = default_matrix(parse_base("2026-10-29T12:00", "UTC"), "UTC", zones=[])
        self.assertNotIn("after next DST change", [s.label for s in m])

    def test_zones_are_not_bisected_and_base_zone_skipped(self):
        m = default_matrix(BASE, BERLIN, specs=[], zones=["UTC", BERLIN])
        self.assertEqual([s.tz for s in m], ["UTC"])
        self.assertFalse(m[0].bisectable)

    def test_unknown_spec(self):
        with self.assertRaises(ValueError):
            parse_at("tomorrowish", BASE, BERLIN)


class Probe(unittest.TestCase):
    def test_only_green_to_red_is_reported_with_flip_point(self):
        cutoff = dt.datetime(2026, 10, 30, 0, 0, tzinfo=zone(BERLIN))
        run = fake_runner({
            "date_bomb": lambda t: t < cutoff,
            "always_red": lambda t: False,
            "fine": lambda t: True,
        })
        rep = probe(run, BASE, BERLIN, default_matrix(BASE, BERLIN, specs=["+1d"], zones=[]),
                    resolution=dt.timedelta(minutes=10))
        self.assertEqual([f.test for f in rep.flips], ["date_bomb"])
        self.assertEqual(rep.baseline_red, ["always_red"])
        f = rep.flips[0]
        first_red = dt.datetime.fromisoformat(f.first_red)
        self.assertTrue(cutoff <= first_red <= cutoff + dt.timedelta(minutes=10), f.first_red)
        self.assertLess(dt.datetime.fromisoformat(f.last_green), cutoff)

    def test_green_test_without_result_is_named_not_flipped(self):
        def run(instant, tz, only):
            rr = RunResult()
            rr.results["stays"] = PASS
            if instant.astimezone(zone(BERLIN)) < dt.datetime(2026, 10, 30, tzinfo=zone(BERLIN)):
                rr.results["vanishes"] = PASS   # e.g. a collection error under the shifted clock
            return rr
        rep = probe(run, BASE, BERLIN, default_matrix(BASE, BERLIN, specs=["+1d"], zones=[]), resolution=None)
        self.assertEqual(rep.flips, [])
        self.assertEqual(rep.scenarios[0]["no_result"], ["vanishes"])
        from clockprobe.core import format_report
        self.assertIn("without a result under a shifted clock", format_report(rep))

    def test_unstable_baseline_is_not_judged(self):
        state = {"n": 0}

        def flaky(t):
            state["n"] += 1
            return state["n"] % 2 == 1
        rep = probe(fake_runner({"flaky": flaky}), BASE, BERLIN, default_matrix(BASE, BERLIN, specs=["+1d"], zones=[]))
        self.assertEqual(rep.unstable, ["flaky"])
        self.assertEqual(rep.flips, [])

    def test_utc_vs_local_date_found_by_midnight(self):
        # The test compares the UTC date with the local date: red between local midnight and 01:00 in Berlin (winter).
        def same_date(t):
            return t.astimezone(dt.timezone.utc).date() == t.astimezone(zone(BERLIN)).date()
        rep = probe(fake_runner({"status": same_date}), BASE, BERLIN, default_matrix(BASE, BERLIN, specs=["midnight"], zones=[]),
                    resolution=dt.timedelta(minutes=5))
        self.assertEqual(len(rep.flips), 1)
        self.assertTrue(rep.flips[0].first_red.startswith("2026-10-30T00:0"), rep.flips[0].first_red)

    def test_harness_breakage_is_named(self):
        run = fake_runner({"sleeps": lambda t: False, "real_red": lambda t: False})
        plain = lambda only: RunResult(results={"sleeps": PASS, "real_red": FAIL})  # noqa: E731
        rep = probe(run, BASE, BERLIN, [], plain=plain)
        self.assertEqual(rep.baseline_red, ["real_red", "sleeps"])
        self.assertEqual(rep.harness_red, ["sleeps"])

    def test_file_time_artefact_counts_as_pass(self):
        run = fake_runner({"mtime": lambda t: t < BASE + dt.timedelta(hours=1)})
        confirm = lambda instant, tz, only: RunResult(results={"mtime": PASS})  # noqa: E731
        rep = probe(run, BASE, BERLIN, default_matrix(BASE, BERLIN, specs=["+1d"], zones=[]), confirm=confirm)
        self.assertEqual(rep.flips, [])
        self.assertEqual([f.test for f in rep.artefacts], ["mtime"])

    def test_bisect_across_spring_forward_uses_real_time(self):
        # Berlin skips 02:00-03:00 on 2027-03-28. The test turns red at 01:30 UTC (03:30 CEST); the flip point must be a real local time.
        cut = dt.datetime(2027, 3, 28, 1, 30, tzinfo=dt.timezone.utc)
        base = parse_base("2027-03-27T20:00", BERLIN)
        run = fake_runner({"x": lambda t: t < cut})
        rep = probe(run, base, BERLIN, default_matrix(base, BERLIN, specs=["dst"], zones=[]), resolution=dt.timedelta(minutes=5))
        f = rep.flips[0]
        fr = dt.datetime.fromisoformat(f.first_red)
        self.assertTrue(cut <= fr <= cut + dt.timedelta(minutes=5), f.first_red)
        self.assertEqual(fr.utcoffset(), dt.timedelta(hours=2), f.first_red)

    def test_hours_are_elapsed_time_across_dst(self):
        base = parse_base("2027-03-28T01:30", BERLIN)
        self.assertEqual(parse_at("+1h", base, BERLIN).local().isoformat(), "2027-03-28T03:30:00+02:00")

    def test_bisect_step_count(self):
        lo = BASE
        hi = BASE + dt.timedelta(days=32)
        run = fake_runner({"x": lambda t: t < BASE + dt.timedelta(days=3)})
        _, _, steps = bisect(run, "x", lo, hi, BERLIN, dt.timedelta(hours=1))
        self.assertLessEqual(steps, 10)

    def test_bisect_with_sub_second_resolution_terminates(self):
        # Called directly (not through the CLI) with a resolution below one second: it is raised to one second.
        lo = BASE.replace(microsecond=0)
        hi = lo + dt.timedelta(seconds=10)
        calls = {"n": 0}

        def red_late(t):
            calls["n"] += 1
            if calls["n"] > 50:
                raise AssertionError("bisection does not terminate")
            return t < lo + dt.timedelta(seconds=7)
        green, red, _ = bisect(fake_runner({"x": red_late}), "x", lo, hi, BERLIN, dt.timedelta(milliseconds=100))
        self.assertLessEqual(red - green, dt.timedelta(seconds=1))
        self.assertTrue(green < lo + dt.timedelta(seconds=7) <= red)

    def test_bisect_with_sub_second_bound_terminates(self):
        # lo on a whole second, hi half a second later than lo + 1 s: the truncated midpoint fell back onto lo and never moved.
        lo = BASE.replace(microsecond=0)
        hi = lo + dt.timedelta(seconds=1, microseconds=500000)
        calls = {"n": 0}

        def red_from_hi(t):
            calls["n"] += 1
            if calls["n"] > 50:
                raise AssertionError("bisection does not terminate")
            return t < hi
        green, red, steps = bisect(fake_runner({"x": red_from_hi}), "x", lo, hi, BERLIN, dt.timedelta(seconds=1))
        self.assertLessEqual(red - green, dt.timedelta(seconds=1))
        self.assertLessEqual(steps, 2)


class Copy(unittest.TestCase):
    def test_copy_keeps_checkout_clean(self):
        d = Path(tempfile.mkdtemp(prefix="clockprobe-copy-test-"))
        (d / "keep.txt").write_text("x")
        case = Case(name="writer", cmd="test -f keep.txt && echo y > written.txt")
        r = run_cases([case], dict(os.environ), str(d), 30, copy=True)
        self.assertEqual(r.results, {"writer": PASS})
        self.assertFalse((d / "written.txt").exists())
        r = run_cases([case], dict(os.environ), str(d), 30)
        self.assertTrue((d / "written.txt").exists())


class Timeout(unittest.TestCase):
    def test_output_printed_before_a_timeout_is_kept(self):
        # TimeoutExpired.stdout is bytes on POSIX even with text=True; the report should still show what the test printed.
        case = Case(name="slow", cmd="echo 'FAIL marker-before-timeout'; exec sleep 5")
        r = run_cases([case], dict(os.environ), None, 0.5)
        self.assertEqual(r.results, {"slow": FAIL})
        self.assertIn("timeout after", r.output["slow"])
        self.assertIn("marker-before-timeout", r.output["slow"])


class FreshClockPerCommand(unittest.TestCase):
    def test_env_function_is_called_once_per_command_in_order(self):
        # With one environment for all commands, later commands would see the scenario time plus the run time of the earlier ones;
        # a bisection step starts only one command. Building the environment per command keeps both measurements the same.
        calls = []

        def env():
            calls.append(len(calls))
            e = dict(os.environ)
            e["CP_N"] = str(len(calls))
            return e
        cases = [Case(name="a", cmd='test "$CP_N" = 1'), Case(name="b", cmd='test "$CP_N" = 2')]
        r = run_cases(cases, env, None, 30)
        self.assertEqual(r.results, {"a": PASS, "b": PASS})
        self.assertEqual(len(calls), 2)


class Junit(unittest.TestCase):
    def test_parse(self):
        xml = textwrap.dedent("""\
            <testsuites><testsuite>
              <testcase classname="t.A" name="ok"/>
              <testcase classname="t.A" name="bad"><failure message="x"/></testcase>
              <testcase classname="t.A" name="err"><error message="x"/></testcase>
              <testcase name="skipped"><skipped/></testcase>
            </testsuite></testsuites>""")
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as fh:
            fh.write(xml)
        try:
            self.assertEqual(parse_junit(fh.name), {"t.A::ok": PASS, "t.A::bad": FAIL, "t.A::err": FAIL, "skipped": "skip"})
        finally:
            os.unlink(fh.name)


@unittest.skipUnless(find_libfaketime(), "libfaketime not installed")
class Cli(unittest.TestCase):
    def test_negative_spec_without_equals(self):
        a = cli.build_parser().parse_args(cli._allow_negative_specs(["--at", "-3h", "--at", "+1d", "--", "x", "--at", "-1h"]))
        self.assertEqual(a.at, ["-3h", "+1d"])
        self.assertEqual(a.command, ["--", "x", "--at", "-1h"])

    def test_list_accepts_negative_spec(self):
        self.assertEqual(cli.main(["--list", "--base", "2026-10-29T12:00", "--tz", BERLIN, "--at", "-3h", "--zone", "none"]), 0)

    def test_local_zone_name_from_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "usr/share/zoneinfo/Europe/Berlin"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"")
            link = Path(tmp) / "localtime"
            link.symlink_to(target)
            self.assertEqual(cli.local_zone_name(str(link)), "Europe/Berlin")
            plain = Path(tmp) / "plain"
            plain.write_bytes(b"")
            self.assertIsNone(cli.local_zone_name(str(plain)))


class Offset(unittest.TestCase):
    def test_child_never_before_scenario_moment(self):
        from clockprobe.runner import clock_env
        for frac in (0.1, 0.4, 0.6, 0.9):
            instant = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1, seconds=frac)
            env = clock_env({}, "/lib/x.so", instant, "UTC")
            now = dt.datetime.now(dt.timezone.utc)
            self.assertGreaterEqual(now + dt.timedelta(seconds=int(env["FAKETIME"])), instant)


class CliErrors(unittest.TestCase):
    """Command-line handling; most tests check that usage and setup errors end with exit code 2 and a message naming the problem."""

    def _main(self, argv):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, err.getvalue()

    def test_empty_resolution(self):
        code, err = self._main(["--base", "2026-10-29T12:00", "--tz", BERLIN, "--resolution", "", "--list"])
        self.assertEqual(code, 2)
        self.assertIn("--resolution", err)

    def test_missing_libfaketime_names_path(self):
        missing = "/nonexistent/clockprobe-test/libfaketime.so.1"
        code, err = self._main(["--base", "2026-10-29T12:00", "--tz", BERLIN, "--zone", "none", "--libfaketime", missing,
                                "--", sys.executable, "-c", "pass"])
        self.assertEqual(code, 2)
        self.assertIn(missing, err)

    def test_resolution_below_one_second(self):
        for bad in ("0.5s", "-1h"):
            code, err = self._main(["--base", "2026-10-29T12:00", "--tz", BERLIN, f"--resolution={bad}", "--list"])
            self.assertEqual(code, 2, bad)
            self.assertIn("--resolution", err)

    def test_out_of_range_values_are_usage_errors(self):
        for argv in (["--resolution", "inf"], ["--at", "+99999999d"]):
            code, err = self._main(["--base", "2026-10-29T12:00", "--tz", BERLIN] + argv + ["--list"])
            self.assertEqual(code, 2, argv)

    def test_unknown_tz_with_aware_base_is_exit_2(self):
        # An aware --base and a relative --at never need the zone; it must still be checked before any run.
        code, err = self._main(["--tz", "Foo/Bar", "--base", "2026-10-29T12:00+01:00", "--at", "+3h", "--list"])
        self.assertEqual(code, 2)
        self.assertIn("Foo/Bar", err)

    def test_unknown_zone(self):
        code, err = self._main(["--base", "2026-10-29T12:00", "--tz", BERLIN, "--zone", "Foo/Bar", "--list"])
        self.assertEqual(code, 2)
        self.assertIn("Foo/Bar", err)

    def test_missing_cwd(self):
        code, err = self._main(["--base", "2026-10-29T12:00", "--tz", BERLIN, "--zone", "none", "--cwd", "/nonexistent/clockprobe-cwd",
                                "--cmd", "true"])
        self.assertEqual(code, 2)
        self.assertIn("/nonexistent/clockprobe-cwd", err)

    def test_json_in_missing_directory(self):
        code, err = self._main(["--base", "2026-10-29T12:00", "--tz", BERLIN, "--zone", "none", "--json", "/nonexistent/dir/r.json",
                                "--cmd", "true"])
        self.assertEqual(code, 2)
        self.assertIn("--json", err)

    def test_json_is_a_directory(self):
        with tempfile.TemporaryDirectory() as d:
            code, err = self._main(["--base", "2026-10-29T12:00", "--tz", BERLIN, "--zone", "none", "--json", d, "--cmd", "true"])
        self.assertEqual(code, 2)
        self.assertIn("--json", err)

    def test_unexpected_error_is_exit_3_not_1(self):
        from unittest import mock
        with mock.patch.object(cli, "cases_from_args", side_effect=RuntimeError("boom")):
            code, err = self._main(["--base", "2026-10-29T12:00", "--tz", BERLIN, "--zone", "none", "--cmd", "true"])
        self.assertEqual(code, 3)
        self.assertIn("boom", err)

    def test_each_runs_bare_file_names(self):
        with tempfile.TemporaryDirectory() as d:
            a = cli.build_parser().parse_args(["--each", "t_*.sh", "--cwd", d])
            Path(d, "t_a.sh").write_text("exit 0\n")
            Path(d, "sub").mkdir()
            cases = cli.cases_from_args(a)
            self.assertEqual([(c.name, c.cmd) for c in cases], [("t_a.sh", "./t_a.sh")])

    def test_junit_duplicate_ids_red_wins(self):
        with tempfile.TemporaryDirectory() as d:
            x = Path(d, "r.xml")
            x.write_text('<testsuite><testcase classname="c" name="t"><failure/></testcase>'
                         '<testcase classname="c" name="t"/></testsuite>')
            self.assertEqual(parse_junit(str(x)), {"c::t": FAIL})

    def test_base_with_trailing_z(self):
        self.assertEqual(parse_base("2026-10-29T12:00Z", BERLIN), dt.datetime(2026, 10, 29, 12, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(parse_at("2026-10-29T13:00Z", BASE, BERLIN).instant, dt.datetime(2026, 10, 29, 13, 0, tzinfo=dt.timezone.utc))

    def test_each_skips_matching_directories(self):
        with tempfile.TemporaryDirectory() as d:
            a = cli.build_parser().parse_args(["--each", "*", "--cwd", d])
            Path(d, "t_a.sh").write_text("exit 0\n")
            Path(d, "__pycache__").mkdir()
            cases = cli.cases_from_args(a)
            self.assertEqual([c.name for c in cases], ["t_a.sh"])

    @unittest.skipUnless(find_libfaketime(), "libfaketime not installed")
    def test_junit_without_results_is_a_setup_error(self):
        code, err = self._main(["--base", "2026-10-29T12:00", "--tz", BERLIN, "--zone", "none", "--at", "+1d", "--junit",
                                "--baseline-runs", "1", "--", sys.executable, "-c", "pass"])
        self.assertEqual(code, 2)
        self.assertIn("no test results", err)


class Report(unittest.TestCase):
    def test_flip_point_shown_without_bisection_runs(self):
        from clockprobe.core import Flip, Report as Rep, format_report
        rep = Rep(base="2026-10-29T23:30:00+01:00", tz=BERLIN, baseline={"t": PASS})
        rep.flips.append(Flip(test="t", scenario="just before midnight", at="2026-10-29T23:55:00+01:00", tz=BERLIN,
                              last_green="2026-10-29T23:30:00+01:00", first_red="2026-10-29T23:55:00+01:00", steps=0))
        self.assertIn("flips between 2026-10-29T23:30:00+01:00 (green) and 2026-10-29T23:55:00+01:00", format_report(rep))


class DstScenario(unittest.TestCase):
    def test_fall_back_scenario_lies_after_the_change(self):
        base = parse_base("2026-10-20T12:00", BERLIN)
        sc = parse_at("dst", base, BERLIN)
        # Berlin falls back on 2026-10-25 at 01:00 UTC; the scenario must be 30 minutes after that, not 30 minutes before
        self.assertEqual(sc.instant, dt.datetime(2026, 10, 25, 1, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(sc.local().utcoffset(), dt.timedelta(hours=1))

    def test_spring_forward_scenario(self):
        base = parse_base("2027-03-20T12:00", BERLIN)
        sc = parse_at("dst", base, BERLIN)
        self.assertEqual(sc.instant, dt.datetime(2027, 3, 28, 1, 30, tzinfo=dt.timezone.utc))

    def test_change_off_the_full_hour_is_found_to_the_minute(self):
        # Lord Howe Island changes by 30 minutes at 02:00 local standard time (UTC+10:30), i.e. at hh:30 UTC
        tr = dst_transitions(parse_base("2026-09-01T12:00", "Australia/Lord_Howe"), "Australia/Lord_Howe", days=60)
        self.assertTrue(tr)
        self.assertEqual(tr[0].astimezone(dt.timezone.utc).minute, 30)


@unittest.skipUnless(find_libfaketime(), "libfaketime not installed")
class EndToEnd(unittest.TestCase):
    def test_real_run_finds_date_bomb_and_ignores_file_times(self):
        d = Path(tempfile.mkdtemp(prefix="clockprobe-e2e-"))
        # A test that breaks from 2026-10-30 local time; one that compares a fresh file's mtime with now (red if file times are NOT
        # shifted); one that sets an mtime from the clock and reads it back (red if they ARE shifted: double offset); one always fine.
        (d / "test_bomb.py").write_text("import datetime,sys\nsys.exit(0 if datetime.date.today() < datetime.date(2026,10,30) else 1)\n")
        (d / "test_mtime.py").write_text(textwrap.dedent("""\
            import os, sys, tempfile, time
            fd, p = tempfile.mkstemp(); os.close(fd)
            age = abs(time.time() - os.stat(p).st_mtime)
            os.unlink(p)
            sys.exit(0 if age < 3600 else 1)
            """))
        (d / "test_utime.py").write_text(textwrap.dedent("""\
            import os, sys, tempfile, time
            fd, p = tempfile.mkstemp(); os.close(fd)
            t = time.time() - 60; os.utime(p, (t, t))
            age = time.time() - os.stat(p).st_mtime
            os.unlink(p)
            sys.exit(0 if 0 < age < 3600 else 1)
            """))
        (d / "test_fine.py").write_text("print('ok')\n")
        # time.sleep must keep working under the harness (FAKETIME_DONT_FAKE_MONOTONIC broke it with Python 3.11+)
        (d / "test_sleep.py").write_text("import time, subprocess\ntime.sleep(0.1)\nsubprocess.run(['true'], timeout=5)\n")
        out = d / "report.json"
        code = cli.main(["--base", "2026-10-29T12:00", "--tz", BERLIN, "--at", "+1d", "--zone", "none", "--each", "test_*.py",
                         "--runner", f"{sys.executable} {{}}", "--cwd", str(d), "--resolution", "1h", "--json", str(out)])
        import json
        rep = json.loads(out.read_text())
        self.assertEqual(code, 1)
        self.assertEqual([f["test"] for f in rep["flips"]], ["test_bomb.py"])
        self.assertTrue(rep["flips"][0]["first_red"].startswith("2026-10-30T00:"), rep["flips"][0]["first_red"])
        self.assertEqual(sorted({f["test"] for f in rep["file_time_artefacts"]}), ["test_mtime.py"])
        self.assertNotIn("test_mtime.py", rep["baseline_red"])
        self.assertEqual(rep["baseline_red"], [])


if __name__ == "__main__":
    unittest.main()
