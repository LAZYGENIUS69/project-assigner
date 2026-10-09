"""Pure meeting-slot logic.

One 10-minute slot per student, evenings only, no two students in the
same slot. No database, no HTTP, no printing - everything here takes plain
data and returns plain data, which makes it the file to unit test.

The rules it encodes:
  * slots run 18:00 to 20:00, ten minutes each => 12 slots a day
  * the last slot starts 19:50 and ENDS at 20:00; there is no 20:00 slot
  * teaching days are Monday, Tuesday, Wednesday, Thursday and Saturday -
    FRIDAY IS A HOLIDAY and Sunday is never used
  * 5 days x 12 slots = 60 slots, which is exactly one cohort

Guarantees worth pinning down in tests:
  1. teaching_days never returns a Friday or a Sunday, whatever day you
     start on - including starting ON a Friday, a Saturday or a Sunday
  2. a 2-hour window divides into exactly 12 ten-minute slots, the first
     at 18:00 and the last at 19:50
  3. every student gets exactly one slot and no slot is used twice
  4. more students than slots raises ScheduleError naming the shortfall
  5. the same seed always produces the same schedule

`start` is INCLUSIVE: if the start date is itself a teaching day it is the
first day returned. Starting on a Friday gives Saturday first, starting on
a Sunday gives Monday first.
"""
import datetime as dt
from zoneinfo import ZoneInfo
import random

FRIDAY = 4
SUNDAY = 6
HOLIDAYS = frozenset({FRIDAY})

START_HOUR = 18
END_HOUR = 20
SLOT_MINUTES = 10
DAYS = 5


class ScheduleError(ValueError):
    pass


def teaching_days(start, count=DAYS, holidays=HOLIDAYS):
    """The next `count` teaching days on or after `start`.

    Fridays (weekday 4, the holiday) and Sundays (weekday 6) are skipped.
    Sunday is never a teaching day regardless of `holidays`; `holidays` is
    for the ones that are a policy choice, which here is Friday.
    """
    if count < 1:
        raise ScheduleError("a schedule needs at least one day")
    skip = set(holidays) | {SUNDAY}
    if len(skip) >= 7:
        raise ScheduleError("every weekday is a holiday - no days left to schedule")

    day = start.date() if isinstance(start, dt.datetime) else start
    out = []
    while len(out) < count:
        if day.weekday() not in skip:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


#: The timezone the workshop actually happens in.
#
# This matters more than it looks. The database column is TIMESTAMPTZ, so a
# NAIVE 18:00 is read as 18:00 UTC - which is 23:30 in Bangalore. Students
# would be told "6pm" and their calendar would say half past eleven at night.
# Attaching the zone here, in the pure layer, keeps every caller correct.
LOCAL_TZ = ZoneInfo("Asia/Kolkata")


def day_slots(day, start_hour=START_HOUR, end_hour=END_HOUR, minutes=SLOT_MINUTES,
              tz=LOCAL_TZ):
    """Every slot START time on one day.

    A slot is only generated if it fits whole inside the window, so a
    18:00-20:00 window at ten minutes gives 18:00 ... 19:50 and never a
    20:00 slot that would run past the end.
    """
    if minutes < 1:
        raise ScheduleError("a slot must be at least one minute long")
    if end_hour <= start_hour:
        raise ScheduleError(f"the window {start_hour}:00-{end_hour}:00 is empty")

    day = day.date() if isinstance(day, dt.datetime) else day
    first = dt.datetime(day.year, day.month, day.day, start_hour, 0, tzinfo=tz)
    last_end = dt.datetime(day.year, day.month, day.day, tzinfo=tz) \
        + dt.timedelta(hours=end_hour)
    step = dt.timedelta(minutes=minutes)

    out = []
    at = first
    while at + step <= last_end:
        out.append(at)
        at += step
    return out


def build_slots(days, start_hour=START_HOUR, end_hour=END_HOUR, minutes=SLOT_MINUTES,
                tz=LOCAL_TZ):
    """Every slot across every day, in chronological order."""
    out = []
    for day in days:
        out.extend(day_slots(day, start_hour, end_hour, minutes))
    return out


def capacity(days, start_hour=START_HOUR, end_hour=END_HOUR, minutes=SLOT_MINUTES):
    """How many students this many days can hold."""
    return len(build_slots(days, start_hour, end_hour, minutes))


def assign_slots(students, slots, seed=None):
    """Give every student exactly one slot.

    The roster is shuffled with the seed and then laid into the slots in
    time order, so the schedule is reproducible and defensible: same seed,
    same slot for the same student. Slots themselves are NOT shuffled -
    they are chronological, so leftover slots are the late ones at the end
    rather than holes in the middle.
    """
    if not students:
        return []
    if len(students) > len(slots):
        raise ScheduleError(
            f"{len(students)} students but only {len(slots)} slots - add another day")

    rng = random.Random(seed)
    roster = list(students)
    rng.shuffle(roster)

    return [{"position": i + 1, "student": s, "slot": slots[i]}
            for i, s in enumerate(roster)]


def slot_end(at, minutes=SLOT_MINUTES):
    return at + dt.timedelta(minutes=minutes)


def as_local(at, tz=LOCAL_TZ):
    """Render a slot in the workshop's own timezone.

    Postgres hands timestamps back in the connection's timezone, which in a
    container is UTC. Formatting that directly prints 12:30 for a 18:00 IST
    slot. Every display path must convert first - that is what this is for.

    A naive datetime is assumed to already be local and is left alone.
    """
    if at.tzinfo is None:
        return at
    return at.astimezone(tz)


def format_slot(at, minutes=SLOT_MINUTES):
    """Human readable, for a CSV cell: 'Mon 12 Oct 2026 18:00-18:10'."""
    at = as_local(at)
    return f"{at:%a %d %b %Y %H:%M}-{slot_end(at, minutes):%H:%M}"


def slot_fields(at, minutes=SLOT_MINUTES):
    """The CSV's three separate columns for one slot, in local time."""
    at = as_local(at)
    return {
        "day": f"{at:%A}",
        "date": at.strftime("%Y-%m-%d"),
        "time": f"{at:%H:%M}-{slot_end(at, minutes):%H:%M}",
    }

