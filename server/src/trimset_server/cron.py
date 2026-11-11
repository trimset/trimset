"""Five-field cron expressions, in UTC, for `refit_schedule` (FR-30): minute, hour, day of
month, month, day of week (0-7, 0 and 7 are Sunday). A field is `*`, a number, a range
`a-b`, any of those with a step `/n`, or a comma list of them. When both day fields are
restricted, a day matches if either does, as in classic cron.
"""

from datetime import datetime, timedelta

FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _field(text: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        span, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if span == "*":
            start, end = low, high
        elif "-" in span:
            a, b = span.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(span)
            if step_text:
                end = high
        if not (low <= start <= end <= high) or step < 1:
            raise ValueError(f"{part!r} is outside {low}-{high}")
        values.update(range(start, end + 1, step))
    return values


class Cron:
    def __init__(self, expression: str) -> None:
        parts = expression.split()
        if len(parts) != 5:
            raise ValueError("a cron expression has five fields: minute hour day month weekday")
        try:
            sets = [_field(p, lo, hi) for p, (lo, hi) in zip(parts, FIELDS, strict=True)]
        except ValueError as e:
            raise ValueError(f"not a cron expression: {e}") from None
        self.minute, self.hour, self.day, self.month, weekday = sets
        self.weekday = {d % 7 for d in weekday}  # 7 is Sunday too
        self.any_day, self.any_weekday = parts[2] == "*", parts[4] == "*"

    def matches(self, t: datetime) -> bool:
        if t.minute not in self.minute or t.hour not in self.hour or t.month not in self.month:
            return False
        day, weekday = t.day in self.day, (t.weekday() + 1) % 7 in self.weekday
        if self.any_day or self.any_weekday:
            return day and weekday
        return day or weekday

    def latest(self, after: datetime, until: datetime) -> datetime | None:
        """The last whole minute in (after, until] that matches, or None."""
        t = until.replace(second=0, microsecond=0)
        while t > after:
            if self.matches(t):
                return t
            t -= timedelta(minutes=1)
        return None
