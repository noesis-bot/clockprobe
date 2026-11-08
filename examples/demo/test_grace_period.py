"""A fixture deadline plus a 30-day grace period is compared with today. Green until the grace period ends, then red for good."""
import datetime as dt
import sys

FIXTURE_DEADLINE = dt.date(2026, 10, 1)
admitted = FIXTURE_DEADLINE + dt.timedelta(days=30) <= dt.date.today()
sys.exit(0 if not admitted else 1)                  # the test assumes the fixture is never admitted
