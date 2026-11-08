# Demo

Two small date bombs. Run from this folder:

```bash
clockprobe --base 2026-10-20T12:30 --tz Europe/Berlin --each 'test_*.py' --runner 'python3 {}'
```

`test_grace_period.py` flips on 2026-10-31 (fixture deadline 2026-10-01 plus 30 days); `test_utc_vs_local.py` flips after local
midnight and in the extreme zones UTC+14 and UTC-11. The base 12:30 Berlin time is 10:30 UTC: 00:30 the next day in Kiritimati and
23:30 the day before in Pago Pago, so both zones disagree with the UTC date. (At 12:00 the Kiritimati scenario would sit exactly on
midnight.)
