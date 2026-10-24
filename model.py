"""Step 2: Build, solve, and verify the timetable using OR-Tools CP-SAT."""

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

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


class Slot:
    """One shared or individual scheduling decision."""

    def __init__(self, model, name, length_min):
        self.length_min = length_min
        self.starts = valid_starts(length_min)

        self.present = model.NewBoolVar(f"present_{name}")
        self.on_day = [
            model.NewBoolVar(f"day_{day}_{name}")
            for day in range(config.NUM_DAYS)
        ]
        self.on_time = {
            start: model.NewBoolVar(f"time_{start}_{name}")
            for start in self.starts
        }

        # Scheduled slots have exactly one day and one start time.
        model.Add(sum(self.on_day) == self.present)
        model.Add(sum(self.on_time.values()) == self.present)

        self.start = model.NewIntVar(
            0,
            max(0, (config.NUM_DAYS - 1) * DAY + config.DAY_END_MIN),
            f"start_{name}",
        )
        model.Add(
            self.start
            == sum(
                day * DAY * variable
                for day, variable in enumerate(self.on_day)
            )
            + sum(
                minute * variable
                for minute, variable in self.on_time.items()
            )
        )

        self.interval = model.NewOptionalFixedSizeIntervalVar(
            self.start,
            length_min,
            self.present,
            f"interval_{name}",
        )

    def on_days(self, days):
        return sum(self.on_day[day] for day in days)

    def at_times(self, times):
        return sum(self.on_time[time] for time in times)


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


class Progress(cp_model.CpSolverSolutionCallback):
    """Print occasional objective updates during the search."""

    def __init__(self, log):
        super().__init__()
        self.log = log
        self.last_report = -15

    def on_solution_callback(self):
        elapsed = self.WallTime()
        if elapsed - self.last_report >= 15:
            self.last_report = elapsed
            self.log(
                f"  {elapsed:6.0f}s  "
                f"total penalty {self.ObjectiveValue():,.0f}"
            )


def solve(sessions, log=print, fixed=None, seconds=None, hint=None):
    """Schedule sessions. Return (placements, solver statistics).

    fixed: optional {session id: Placement}. Those sessions are pinned to the given day, time and room.
    hint: optional list of Placements used as the solver's starting point (e.g. from greedy.build).
    """
    sessions = list(sessions)

    if not sessions:
        return [], {
            "status": "OPTIMAL",
            "objective": 0,
            "bound": 0,
            "seconds": 0,
        }

    model = cp_model.CpModel()
    slots = {}

    by_teacher = defaultdict(list)
    by_students = defaultdict(list)
    by_room = defaultdict(list)
    cohort_slots = defaultdict(list)
    chains = defaultdict(list)
    room_literals = {}
    penalties = []

    groups = cohort_groups(sessions)
    weekend_days = [
        day for day in range(config.NUM_DAYS)
        if weekday(day) >= 5
    ]

    # Build one time slot per independent scheduling decision.
    for session in sessions:
        course = session.course
        key = slot_key(session)

        if key not in slots:
            slot = slots[key] = Slot(
                model,
                str(session.id),
                session.length * 60,
            )

            chain_key = (
                id(course),
                0 if course.parallel else session.group,
            )
            chains[chain_key].append(slot)

        slot = slots[key]

        # Penalize all teaching hours lost by leaving this slot unscheduled.
        # (This loop already runs once per session, so parallel groups are counted once each.)
        penalties.append(
            config.W_UNSCHEDULED_PER_HOUR * session.length * (1 - slot.present)
        )

        # A slot must satisfy each preference or incur its penalty.
        for requirement in course.requirements:
            if requirement.kind in ("on_days", "not_day"):
                acceptable_days = [
                    day
                    for day in range(config.NUM_DAYS)
                    if requirement.ok(weekday(day), 0, 0)
                ]
                hit = slot.on_days(acceptable_days)
            else:
                acceptable_times = [
                    start
                    for start in slot.starts
                    if requirement.ok(
                        0,
                        start,
                        start + slot.length_min,
                    )
                ]
                hit = slot.at_times(acceptable_times)

            penalties.append(
                config.W_REQUIREMENT * (slot.present - hit)
            )

        penalties.append(
            config.W_WEEKEND * slot.on_days(weekend_days)
        )
        penalties.append(
            config.W_LATE
            * slot.at_times([
                start for start in slot.starts
                if start + slot.length_min > config.LATE_AFTER_MIN
            ])
        )

        for teacher in session.teachers:
            by_teacher[teacher].append(slot.interval)

        for student_set in student_sets(session, groups):
            by_students[student_set].append(slot.interval)
            cohort_slots[student_set].append(
                (slot, session.length)
            )

        # Each session needs its own room assignment.
        if course.rooms:
            literals = []

            for rank, room in enumerate(course.rooms):
                literal = model.NewBoolVar( # pyright: ignore[reportAttributeAccessIssue]
                    f"room_{session.id}_{rank}"
                )
                interval = model.NewOptionalFixedSizeIntervalVar( # pyright: ignore[reportAttributeAccessIssue]
                    slot.start,
                    slot.length_min,
                    literal,
                    f"room_interval_{session.id}_{rank}",
                )

                by_room[room].append(interval)
                penalties.append(
                    config.W_ROOM[
                        min(rank, len(config.W_ROOM) - 1)
                    ] * literal
                )
                literals.append((room, rank, literal))

            model.Add( # pyright: ignore[reportAttributeAccessIssue]
                sum(literal for _, _, literal in literals)
                == slot.present
            )
            room_literals[session.id] = literals

    # No teacher, student group, or room may be double-booked.
    for resource_map in (by_teacher, by_students, by_room):
        for intervals in resource_map.values():
            if len(intervals) > 1:
                model.AddNoOverlap(intervals) # pyright: ignore[reportAttributeAccessIssue]

    # Keep equal-length sessions in chronological order.
    # Penalize scheduling multiple sessions in a chain on the same day.
    course_of = {id(session.course): session.course for session in sessions}
    for chain_key, chain in chains.items():
        # Two sessions of the same lecture on one day with a break in between: strongly discouraged.
        if course_of[chain_key[0]].kind == importer.LECTURE and config.W_SAME_DAY_GAP:
            for i, a in enumerate(chain):
                for b in chain[i + 1:]:
                    same = model.NewBoolVar(f"same_day_{len(penalties)}") # pyright: ignore[reportAttributeAccessIssue]
                    day_a = sum(d * v for d, v in enumerate(a.on_day))
                    day_b = sum(d * v for d, v in enumerate(b.on_day))
                    model.Add(day_a == day_b).OnlyEnforceIf(same) # pyright: ignore[reportAttributeAccessIssue]
                    model.Add(day_a != day_b).OnlyEnforceIf(same.Not()) # pyright: ignore[reportAttributeAccessIssue]
                    touch_ab = model.NewBoolVar(f"touch_ab_{len(penalties)}") # pyright: ignore[reportAttributeAccessIssue]
                    touch_ba = model.NewBoolVar(f"touch_ba_{len(penalties)}") # pyright: ignore[reportAttributeAccessIssue]
                    model.Add(a.start + a.length_min == b.start).OnlyEnforceIf(touch_ab) # pyright: ignore[reportAttributeAccessIssue]
                    model.Add(a.start + a.length_min != b.start).OnlyEnforceIf(touch_ab.Not()) # pyright: ignore[reportAttributeAccessIssue]
                    model.Add(b.start + b.length_min == a.start).OnlyEnforceIf(touch_ba) # pyright: ignore[reportAttributeAccessIssue]
                    model.Add(b.start + b.length_min != a.start).OnlyEnforceIf(touch_ba.Not()) # pyright: ignore[reportAttributeAccessIssue]
                    gapped = model.NewBoolVar(f"gapped_{len(penalties)}") # pyright: ignore[reportAttributeAccessIssue]
                    model.AddBoolOr([a.present.Not(), b.present.Not(), same.Not(), touch_ab, touch_ba, gapped]) # pyright: ignore[reportAttributeAccessIssue]
                    penalties.append(config.W_SAME_DAY_GAP * gapped)

        for first, second in zip(chain, chain[1:]):
            if first.length_min == second.length_min:
                model.AddImplication(second.present, first.present) # pyright: ignore[reportAttributeAccessIssue]
                model.Add( # pyright: ignore[reportAttributeAccessIssue]
                    first.start + first.length_min <= second.start
                ).OnlyEnforceIf([first.present, second.present])

        if len(chain) > 1:
            for day in range(config.NUM_DAYS):
                extra = model.NewIntVar( # pyright: ignore[reportAttributeAccessIssue]
                    0, len(chain), f"same_day_{day}_{len(penalties)}"
                )
                model.Add( # pyright: ignore[reportAttributeAccessIssue]
                    extra >= sum(slot.on_day[day] for slot in chain) - 1
                )
                penalties.append(config.W_SAME_DAY * extra)

    # Soft rule: an exercise should not begin before the first lecture of its subject has finished.
    # Compared on the first session of each exercise group; nothing is charged if either side is unplaced.
    courses = {id(session.course): session.course for session in sessions}
    for lecture, exercise in importer.link_lectures(list(courses.values())):
        lecture_chain = chains.get((id(lecture), 1))
        if not lecture_chain:
            continue
        first_lecture = lecture_chain[0]
        for key, chain in chains.items():
            if key[0] != id(exercise):
                continue
            first_exercise = chain[0]
            too_early = model.NewBoolVar(f"exercise_before_lecture_{len(penalties)}") # pyright: ignore[reportAttributeAccessIssue]
            model.Add( # pyright: ignore[reportAttributeAccessIssue]
                first_exercise.start >= first_lecture.start + first_lecture.length_min
            ).OnlyEnforceIf([first_exercise.present, first_lecture.present, too_early.Not()])
            penalties.append(config.W_LECTURE_FIRST * too_early)

    # Penalize cohort daily loads above the configured comfort limit.
    for student_set, entries in cohort_slots.items():
        for day in range(config.NUM_DAYS):
            daily_hours = sum(
                hours * slot.on_day[day]
                for slot, hours in entries
            )
            excess = model.NewIntVar( # pyright: ignore[reportAttributeAccessIssue]
                0, 24, f"daily_excess_{day}_{len(penalties)}"
            )
            model.Add( # pyright: ignore[reportAttributeAccessIssue]
                excess >= daily_hours - config.MAX_DAILY_HOURS
            )
            penalties.append(config.W_LONG_DAY * excess)

    # Sessions placed in an earlier round stay exactly where they are.
    for session in sessions:
        placed = (fixed or {}).get(session.id)
        if placed is None:
            continue
        slot = slots[slot_key(session)]
        model.Add(slot.present == 1) # pyright: ignore[reportAttributeAccessIssue]
        model.Add(slot.on_day[placed.day] == 1) # pyright: ignore[reportAttributeAccessIssue]
        model.Add(slot.on_time[placed.start] == 1) # pyright: ignore[reportAttributeAccessIssue]
        for room, _, literal in room_literals.get(session.id, ()):
            model.Add(literal == int(room == placed.room)) # pyright: ignore[reportAttributeAccessIssue]

    if hint:
        hinted = {p.session.id: p for p in hint}
        seen = set()
        for session in sessions:
            p = hinted.get(session.id)
            if p is None:
                continue
            key = slot_key(session)
            slot = slots[key]
            if key not in seen:
                seen.add(key)
                model.AddHint(slot.present, int(p.scheduled)) # pyright: ignore[reportAttributeAccessIssue]
                for d, var in enumerate(slot.on_day):
                    model.AddHint(var, int(p.scheduled and d == p.day)) # pyright: ignore[reportAttributeAccessIssue]
                for minute, var in slot.on_time.items():
                    model.AddHint(var, int(p.scheduled and minute == p.start)) # pyright: ignore[reportAttributeAccessIssue]
                model.AddHint(slot.start, p.day * DAY + p.start if p.scheduled else 0) # pyright: ignore[reportAttributeAccessIssue]
            for room, _, literal in room_literals.get(session.id, ()):
                model.AddHint(literal, int(p.scheduled and room == p.room)) # pyright: ignore[reportAttributeAccessIssue]

    model.Minimize(sum(penalties)) # pyright: ignore[reportAttributeAccessIssue]

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds or config.SOLVER_SECONDS
    solver.parameters.num_workers = config.SOLVER_WORKERS
    if hint:
        solver.parameters.repair_hint = True

    log(
        f"Solving {len(sessions)} sessions across "
        f"{config.NUM_DAYS} days..."
    )

    status = solver.Solve(model, Progress(log))

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(
            f"Solver did not find a timetable: "
            f"{solver.StatusName(status)}"
        )

    placements = []

    for session in sessions:
        slot = slots[slot_key(session)]

        if not solver.BooleanValue(slot.present):
            placements.append(Placement(session))
            continue

        day = next(
            d for d, variable in enumerate(slot.on_day)
            if solver.BooleanValue(variable)
        )

        room, rank = None, 0
        for candidate, candidate_rank, literal in room_literals.get(
            session.id, ()
        ):
            if solver.BooleanValue(literal):
                room, rank = candidate, candidate_rank
                break

        placement = Placement(
            session=session,
            scheduled=True,
            day=day,
            start=solver.Value(slot.start) - day * DAY,
            room=room,
            room_rank=rank,
        )
        placement.unmet = unmet_preferences(placement)
        placements.append(placement)

    stats = {
        "status": solver.StatusName(status),
        "objective": solver.ObjectiveValue(),
        "bound": solver.BestObjectiveBound(),
        "seconds": solver.WallTime(),
    }

    return placements, stats


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


def solve_flexible(sessions, log=print):
    """Greedy start (with length repair), local search, then CP-SAT improvement from that start."""
    import greedy

    sessions = list(sessions)
    placements = greedy.build(sessions)
    greedy_log = []
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
        greedy_log.append(f"greedy round {number} ({offset:+d} h): {before} -> {lost_hours(placements)} h unplaced")
        log("  " + greedy_log[-1])

    # --- local search with re-cutting, until it stalls, the time is up, or everything is placed
    import time
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
        interrupted = True
        log("\nInterrupted - keeping the timetable found so far")
    greedy_log.append(f"local search: {best} h unplaced")
    if interrupted:
        stats = {"status": "LOCAL SEARCH", "objective": 0, "bound": 0, "seconds": 0, "repair": greedy_log}
        renumber_parts(placements)
        return placements, stats

    start = placements
    stats = {"status": "LOCAL SEARCH", "objective": 0, "bound": 0, "seconds": 0}
    if config.USE_CPSAT:
        try:
            candidate, cp_stats = solve(sessions, log, hint=start)
        except RuntimeError as error:      # solver found nothing in time
            log(f"  CP-SAT gave no result ({error}); keeping the local-search timetable")
        else:
            mine = sum(p.session.length for p in start if not p.scheduled)
            theirs = sum(p.session.length for p in candidate if not p.scheduled)
            if theirs <= mine:
                placements, stats = candidate, cp_stats
            else:                          # never trade placed classes for the solver's soft-preference polish
                log(f"  CP-SAT left {theirs} h unplaced vs {mine} h from the local search; keeping the local search")
    repair = list(greedy_log)
    seconds = stats["seconds"]

    # summary of the greedy / local-search steps
    stats["seconds"] = seconds
    stats["repair"] = repair
    renumber_parts(placements)
    return placements, stats


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

    return sorted(problems)