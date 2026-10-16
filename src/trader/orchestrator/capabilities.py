"""What this engine does that an older recording may predate (DECISIONS D-055).

``boot.json`` lists the capabilities its day ran with, and a replay grants only those: a recorded
day replays without calls it never made, so it still matches."""

FEES = "fees_v1"  # books the fees the broker charged, at boot and at EOD
SKEW_HOURLY = "skew_hourly"  # reads the clock's skew every hour, not only at boot
SHADOW_TAPE = "shadow_tape"  # reads ambiguous trigger minutes from the SIP tape (D-057)

CURRENT: frozenset[str] = frozenset({FEES, SKEW_HOURLY, SHADOW_TAPE})
