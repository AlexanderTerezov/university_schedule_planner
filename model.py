"""Step 2: build the timetable (greedy + local search, see greedy.py) and verify it."""

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

import config
import importer


DAY = 24 * 60
ORDINAL = {0: "1st", 1: "2nd", 2: "3rd"}


def weekday(day_index):
    """Weekday of a date in the scheduling period: Monday=0, Sunday=6."""
    return (config.START_DATE.weekday() + day_index) % 7


@dataclass
class Placement:
    session: object
    scheduled: bool = False
    day: int = 0
    start: int = 0
    room: str | None = None
    room_rank: int = 0
    unmet: list = field(default_factory=list)

    @property
    def end(self):
        return self.start + self.session.length * 60 # pyright: ignore[reportAttributeAccessIssue]

    @property
    def date(self):
        return config.START_DATE + dt.timedelta(days=self.day)


def valid_starts(length_min):
    """Return legal class start times, respecting working hours and lunch."""
    latest = config.DAY_END_MIN - length_min
    lunch_end = config.LUNCH_START_MIN + config.LUNCH_MINUTES
    starts = []

    for start in range(
        config.DAY_START_MIN,
        latest + 1,
        config.SLOT_MINUTES,
    ):
        overlaps_lunch = (
            config.LUNCH_MINUTES > 0
            and start < lunch_end
            and start + length_min > config.LUNCH_START_MIN
        )
        if not overlaps_lunch:
            starts.append(start)

    return starts


def slot_key(session):
    """Parallel groups of one course part must share a time."""
    if session.course.parallel:
        return id(session.course), session.part
    return (session.id,)


def cohort_groups(sessions):
    """Largest group count declared for each student cohort."""
    result = defaultdict(int)

    for session in sessions:
        for cohort in session.course.cohorts:
            result[cohort] = max(
                result[cohort],
                session.course.groups,
            )

    return result


def student_sets(session, groups):
    """Student groups occupied by a session."""
    course = session.course
    result = []

    for cohort in course.cohorts:
        if course.groups == 1:
            numbers = range(1, groups[cohort] + 1)
        else:
            numbers = (session.group,)

        result.extend((cohort, number) for number in numbers)

    return result


def unmet_preferences(placement):
    """Return unmet soft requirements for a scheduled class."""
    if not placement.scheduled:
        return []

    course = placement.session.course
    day = weekday(placement.day)
    unmet = [
        f"requirement '{req.raw}'"
        for req in course.requirements
        if not req.ok(day, placement.start, placement.end)
    ]

    if placement.room_rank > 0:
        rank = ORDINAL.get(
            placement.room_rank,
            f"{placement.room_rank + 1}th",
        )
        unmet.append(
            f"room: got {rank} choice ({placement.room})"
        )

    if not course.rooms:
        unmet.append("no room preference or room assigned")

    session = placement.session
    if session.resized and session.length != course.duration:
        unmet.append(
            f"length: {session.length} h instead of the usual {course.duration} h"
        )

    return unmet


def split_hours(hours, target):
    """Cut `hours` into classes of `target` hours; the last one takes what is left.

    A leftover shorter than MIN_LENGTH is topped up from the classes before it, or merged into the last one.
    """
    low = min(config.MIN_LENGTH, hours)
    count = -(-hours // target)
    parts = [target] * (count - 1) + [hours - target * (count - 1)]
    if parts[-1] < low and count > 1:
        need = low - parts[-1]
        for i in range(count - 2, -1, -1):
            give = min(need, parts[i] - low)
            parts[i] -= give
            parts[-1] += give
            need -= give
            if not need:
                break
        if need:
            last = parts.pop()
            parts[-1] += last
    return parts


def resplit(placements, offset):
    """New sessions for the hours that are still unplaced, cut at (usual length + offset) hours."""
    next_id = max(p.session.id for p in placements) + 1
    next_part = defaultdict(int)
    lost = defaultdict(list)
    for p in placements:
        s = p.session
        next_part[id(s.course)] = max(next_part[id(s.course)], s.part + 1)
        if not p.scheduled:
            lost[(id(s.course), s.group)].append(s)

    new = []
    for (course_id, group), old in lost.items():
        course = old[0].course
        target = min(max(course.duration + offset, config.MIN_LENGTH), config.MAX_LENGTH)
        for i, length in enumerate(split_hours(sum(s.length for s in old), target)):
            new.append(importer.Session(next_id, course, group, next_part[course_id] + i,
                                        length, old[0].teachers, resized=True))
            next_id += 1
    return new


def renumber_parts(placements):
    """Part 1, 2, 3... of every course group in time order (repairs leave gaps in the numbering)."""
    chains = defaultdict(list)
    for p in placements:
        chains[(id(p.session.course), p.session.group)].append(p)
    for chain in chains.values():
        chain.sort(key=lambda p: (not p.scheduled, p.day, p.start, p.session.part))
        for i, p in enumerate(chain):
            p.session.part = i


def schedule(sessions, log=print):
    """Greedy start with length re-cuts, then local search. Returns (placements, stats)."""
    import time

    import greedy

    sessions = list(sessions)
    placements = greedy.build(sessions)
    steps = []
    total = sum(s.length for s in sessions)

    def lost_hours(plist):
        return sum(p.session.length for p in plist if not p.scheduled)

    log(f"Greedy start: {total - lost_hours(placements)}/{total} h placed in the usual lengths")
    for number, offset in enumerate(config.REPAIR_LENGTH_OFFSETS, start=1):
        if not lost_hours(placements):
            break
        new = resplit(placements, offset)
        kept = [p for p in placements if p.scheduled]
        sessions = [p.session for p in kept] + new
        before = lost_hours(placements)
        placements = greedy.build(sessions, placed={p.session.id: p for p in kept})
        steps.append(f"greedy round {number} ({offset:+d} h): {before} -> {lost_hours(placements)} h unplaced")
        log("  " + steps[-1])

    # --- local search with re-cutting, until it stalls, the time is up, or everything is placed
    interrupted = False
    offset = 0
    phase_start = last_gain = time.time()
    best = lost_hours(placements)
    log(f"Local search from {best} h unplaced (Ctrl+C stops it and keeps the best timetable)")
    try:
        step = 0
        while best and time.time() - phase_start < config.LOCAL_SEARCH_SECONDS:
            remaining = config.LOCAL_SEARCH_SECONDS - (time.time() - phase_start)
            placements, _ = greedy.improve(sessions, placements, remaining, config.LOCAL_SEARCH_WINDOW, log)
            if config.REPAIR_LENGTH_OFFSETS:
                offset = config.REPAIR_LENGTH_OFFSETS[step % len(config.REPAIR_LENGTH_OFFSETS)]
                step += 1
                kept = [p for p in placements if p.scheduled]
                sessions = [p.session for p in kept] + resplit(placements, offset)
                placements = greedy.build(sessions, placed={p.session.id: p for p in kept})
            now = lost_hours(placements)
            if now < best:
                best, last_gain = now, time.time()
                log(f"  {best} h unplaced after re-cut {offset:+d} h" if config.REPAIR_LENGTH_OFFSETS else f"  {best} h unplaced")
            if time.time() - last_gain > config.LOCAL_SEARCH_STALL_SECONDS:
                log("  no gain for a long time - local search stops")
                break
    except KeyboardInterrupt:
        log("\nInterrupted - keeping the timetable found so far")
    best = lost_hours(placements)
    steps.append(f"local search: {best} h unplaced")
    renumber_parts(placements)
    return placements, {"steps": steps, "unplaced_hours": best, "seconds": round(time.time() - phase_start, 1)}


def verify(placements):
    """Independently check hard constraints in the resulting timetable."""
    problems = set()
    lunch_start = config.LUNCH_START_MIN
    lunch_end = lunch_start + config.LUNCH_MINUTES

    sessions = [
        placement.session
        for placement in placements
        if placement.session is not None
    ]
    groups = cohort_groups(sessions)

    resources = {
        "teacher": defaultdict(list),
        "students": defaultdict(list),
        "room": defaultdict(list),
    }

    for placement in placements:
        if not placement.scheduled:
            continue

        session = placement.session
        subject = session.course.subject[:40]

        if not (
            config.DAY_START_MIN <= placement.start
            and placement.end <= config.DAY_END_MIN
        ):
            problems.add(f"{subject}: outside the working day")

        if (
            config.LUNCH_MINUTES > 0
            and placement.start < lunch_end
            and placement.end > lunch_start
        ):
            problems.add(f"{subject}: overlaps lunch")

        if placement.start % config.SLOT_MINUTES != (
            config.DAY_START_MIN % config.SLOT_MINUTES
        ):
            problems.add(f"{subject}: invalid start-time alignment")

        for teacher in session.teachers:
            resources["teacher"][teacher].append(placement)

        for student_set in student_sets(session, groups):
            resources["students"][student_set].append(placement)

        if placement.room:
            resources["room"][placement.room].append(placement)

    for kind, resource_map in resources.items():
        for resource, items in resource_map.items():
            items.sort(key=lambda p: (p.day, p.start, p.end))

            # Compare every overlapping interval, including nested intervals.
            for index, first in enumerate(items):
                for second in items[index + 1:]:
                    if second.day != first.day:
                        if second.day > first.day:
                            break
                        continue

                    if second.start >= first.end:
                        break

                    problems.add(
                        f"{kind} {resource}: "
                        f"'{first.session.course.subject[:30]}' and "
                        f"'{second.session.course.subject[:30]}' "
                        f"overlap on {first.date}"
                    )

    # --- one session per day for every course group
    per_day = defaultdict(list)
    for placement in placements:
        if placement.scheduled:
            s = placement.session
            per_day[(id(s.course), 0 if s.course.parallel else s.group, placement.day)].append(placement)
    for items in per_day.values():
        slots = {slot_key(p.session) for p in items}   # parallel groups share one slot
        if len(slots) > 1:
            first = items[0]
            problems.add(f"{first.session.course.subject[:40]}: {len(slots)} sessions of the same group on {first.date}")

    # --- lecture before exercise (same subject, shared cohort)
    courses = {id(s.course): s.course for s in sessions}
    by_course = defaultdict(list)
    for placement in placements:
        if placement.scheduled:
            by_course[id(placement.session.course)].append(placement)
    for lecture, exercise in importer.link_lectures(list(courses.values())):
        lectures = by_course.get(id(lecture), [])
        for placement in by_course.get(id(exercise), []):
            abs_start = placement.day * DAY + placement.start
            if not lectures or min(x.day * DAY + x.end for x in lectures) > abs_start:
                problems.add(f"{exercise.subject[:40]}: exercise on {placement.date} is not after the first lecture")
            for x in lectures:
                if x.day == placement.day and x.end > placement.start:
                    problems.add(f"{exercise.subject[:40]}: lecture is not before the exercise on {placement.date}")

    return sorted(problems)