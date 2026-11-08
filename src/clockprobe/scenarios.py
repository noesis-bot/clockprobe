"""Clock scenarios: which moments to run the suite at.

A scenario is a wall-clock instant plus the time zone the test process sees (TZ). Instants are timezone-aware datetimes.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore

UTC = dt.timezone.utc

# Zones at the extremes of the UTC offset range: whatever date the base zone has, one of these has a different one.
EXTREME_ZONES = ["UTC", "Pacific/Kiritimati", "Pacific/Pago_Pago"]
# Zone names with a constant offset of 0 (UTC itself and the tz database links to Etc/UTC or Etc/GMT); such a base zone already is UTC.
UTC_ALIASES = {"UTC", "Etc/UTC", "Etc/UCT", "UCT", "Etc/Universal", "Universal", "Etc/Zulu", "Zulu", "Etc/GMT", "GMT", "Etc/GMT+0",
               "Etc/GMT-0", "Etc/GMT0", "GMT+0", "GMT-0", "GMT0", "Etc/Greenwich", "Greenwich"}


def same_zone(a: Optional[str], b: Optional[str]) -> bool:
    """True if both names denote the same zone: equal, or both UTC (a listed alias, or any spelling of "UTC" as zone() accepts)."""
    def is_utc(n: Optional[str]) -> bool:
        return n is not None and (n in UTC_ALIASES or n.upper() == "UTC")
    return a == b or (is_utc(a) and is_utc(b))


@dataclass(frozen=True)
class Scenario:
    label: str
    instant: dt.datetime          # aware, kept in UTC (see __post_init__)
    tz: Optional[str]             # value for TZ in the child; None = leave TZ alone
    bisectable: bool = True       # base zone and later than the base time, so the flip point can be searched between base and scenario

    def __post_init__(self):
        # Python compares and subtracts aware datetimes that share a tzinfo object by wall time, ignoring DST. Keep every instant in
        # UTC so that ordering and bisection midpoints are real elapsed time.
        object.__setattr__(self, "instant", self.instant.astimezone(UTC))

    def local(self) -> dt.datetime:
        return self.instant.astimezone(zone(self.tz)) if self.tz else self.instant.astimezone()


def zone(name: Optional[str]) -> dt.tzinfo:
    if not name:
        return dt.datetime.now().astimezone().tzinfo  # type: ignore[return-value]
    if name.upper() == "UTC":
        return UTC
    if ZoneInfo is None:
        raise ValueError("time zones need Python 3.9+ (zoneinfo)")
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):   # ZoneInfoNotFoundError is a KeyError
        raise ValueError(f"unknown time zone: {name!r}") from None


_REL = re.compile(r"^([+-]\d+(?:\.\d+)?)([smhdw])$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def fromiso(text: str) -> dt.datetime:
    """datetime.fromisoformat that also takes a trailing Z (Python before 3.11 does not)."""
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return dt.datetime.fromisoformat(text)


def parse_base(text: Optional[str], tz: Optional[str]) -> dt.datetime:
    """Baseline instant: now, or an ISO date/datetime (a trailing Z means UTC; naive values are read in the base zone)."""
    if not text or text == "now":
        return dt.datetime.now(UTC).replace(microsecond=0).astimezone(zone(tz))
    t = fromiso(text)
    if t.tzinfo is None:
        t = t.replace(tzinfo=zone(tz))
    return t


def next_midnight(base: dt.datetime, tz: Optional[str]) -> dt.datetime:
    """First local midnight after base (in the zone tz), as an aware datetime."""
    z = zone(tz)
    local = base.astimezone(z)
    day = local.date() + dt.timedelta(days=1)
    return dt.datetime(day.year, day.month, day.day, tzinfo=z)


def dst_transitions(base: dt.datetime, tz: Optional[str], days: int = 400) -> List[dt.datetime]:
    """UTC offset changes of zone tz after base, searched for `days` days from the start of base's UTC hour (minute resolution), as
    aware datetimes at the change."""
    z = zone(tz)
    out = []
    t = base.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    prev = t.astimezone(z).utcoffset()
    for _ in range(days * 24):
        t += dt.timedelta(hours=1)
        off = t.astimezone(z).utcoffset()
        if off != prev:
            # the change lies in (t - 1h, t]; many zones change on a full UTC hour, some (e.g. Newfoundland, Lord Howe) do not, so the change is searched to the minute
            lo, hi = t - dt.timedelta(hours=1), t
            while hi - lo > dt.timedelta(minutes=1):
                mid = lo + (hi - lo) / 2
                if mid.astimezone(z).utcoffset() == prev:
                    lo = mid
                else:
                    hi = mid
            if hi > base:   # a change off the full hour can lie in base's own UTC hour but before base
                out.append(hi.replace(second=0, microsecond=0).astimezone(z))
            prev = off
    return out


def parse_at(spec: str, base: dt.datetime, tz: Optional[str]) -> Scenario:
    """One scenario from a spec: +1d, -3h, +2w, midnight, before-midnight, dst, new-year, month-end or an ISO datetime."""
    s = spec.strip()
    m = _REL.match(s)
    if m:
        delta = dt.timedelta(seconds=float(m.group(1)) * _UNIT[m.group(2)])
        if m.group(2) in "dw":   # calendar days: same local wall time across a DST change when tz is an IANA zone (without one: 24-hour steps at the current UTC offset), unless it does not exist on the target day
            z = zone(tz)
            local = base.astimezone(z).replace(tzinfo=None) + delta
            inst = local.replace(tzinfo=z)
        else:
            inst = base.astimezone(UTC) + delta   # exact elapsed time, also across a DST change
        return Scenario(s, inst, tz)
    if s == "midnight":
        return Scenario("just after midnight", next_midnight(base, tz).astimezone(UTC) + dt.timedelta(minutes=30), tz)
    if s == "before-midnight":
        return Scenario("just before midnight", next_midnight(base, tz).astimezone(UTC) - dt.timedelta(minutes=5), tz)
    if s == "month-end":
        z = zone(tz)
        local = base.astimezone(z)
        first = (local.replace(day=28) + dt.timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return Scenario("first of next month 00:30", first.astimezone(UTC) + dt.timedelta(minutes=30), tz)
    if s == "new-year":
        z = zone(tz)
        y = base.astimezone(z).year + 1
        return Scenario(f"new year {y} 00:30", dt.datetime(y, 1, 1, 0, 30, tzinfo=z), tz)
    if s == "dst":
        tr = dst_transitions(base, tz)
        if not tr:
            raise ValueError(f"zone {tz or 'local'} has no offset change in the next year")
        return Scenario("after next DST change", tr[0].astimezone(UTC) + dt.timedelta(minutes=30), tz)  # in UTC: wall-time
        # addition would reset fold and put a fall-back scenario 30 minutes before the change
    try:
        t = fromiso(s)
    except ValueError:
        raise ValueError(f"unknown clock spec: {spec!r}") from None
    if t.tzinfo is None:
        t = t.replace(tzinfo=zone(tz))
    return Scenario(s, t, tz)


DEFAULT_SPECS = ["+1d", "+7d", "+31d", "midnight", "before-midnight", "month-end", "new-year", "dst"]


def default_matrix(base: dt.datetime, tz: Optional[str], specs: Optional[List[str]] = None,
                   zones: Optional[List[str]] = None) -> List[Scenario]:
    """The scenarios to run: time specs in the base zone, then the base instant seen from other zones."""
    out: List[Scenario] = []
    for spec in (specs if specs is not None else DEFAULT_SPECS):
        try:
            sc = parse_at(spec, base, tz)
        except ValueError:
            if spec == "dst":     # zones without DST simply have no DST scenario
                continue
            raise
        if sc.instant > base.astimezone(UTC):
            out.append(sc)
        else:
            out.append(Scenario(sc.label, sc.instant, sc.tz, bisectable=False))
    for z in (zones if zones is not None else EXTREME_ZONES):
        if same_zone(z, tz):
            continue
        out.append(Scenario(f"base time in {z}", base, z, bisectable=False))
    return out
