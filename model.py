
"""Step 2: Build, solve, and verify the timetable using OR-Tools CP-SAT."""

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

import config


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


def solve(sessions, log=print):
    """Schedule sessions. Return (placements, solver statistics)."""
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

	
    slot_sessions = defaultdict(list)
    for session in sessions:
        slot_sessions[slot_key(session)].append(session)

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
        parallel_count = len(slot_sessions[key])
        penalties.append(
            config.W_UNSCHEDULED_PER_HOUR
            * session.length
            * parallel_count
            * (1 - slot.present)
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
    for chain in chains.values():
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

    model.Minimize(sum(penalties)) # pyright: ignore[reportAttributeAccessIssue]

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = config.SOLVER_SECONDS
    solver.parameters.num_workers = config.SOLVER_WORKERS

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
