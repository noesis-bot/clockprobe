"""Shows a date in UTC where users expect their local date. Red whenever the UTC date and the local date differ: east of UTC from
local midnight until the UTC date catches up (in Berlin: 00:00-01:00 in winter, 00:00-02:00 in summer), west of UTC from UTC midnight
until local midnight."""
import datetime as dt
import sys

posted = dt.datetime.now(dt.timezone.utc)
shown = posted.date().isoformat()                 # bug: should be posted.astimezone().date()
expected = dt.datetime.now().date().isoformat()   # what the user sees on their calendar
sys.exit(0 if shown == expected else 1)
