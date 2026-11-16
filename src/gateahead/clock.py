"""Indian railway wall time, independent of the server's configured timezone."""

from datetime import timedelta, timezone

# Indian railway schedules use UTC+05:30. A fixed offset needs no host tzdata.
INDIA = timezone(timedelta(hours=5, minutes=30), "Asia/Kolkata")
