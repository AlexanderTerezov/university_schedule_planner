"""Constructive timetable plus local search.

Hard rules (a class is never placed against them):
  * a teacher, a student group or a room is in one place at a time; working hours; lunch;
  * parts of a course group in order (equal-length parts); parallel groups at the same time;
  * at most ONE session per day for every course group;
  * every exercise comes after the first lecture of its subject (matched by name + shared cohort), and when a
    lecture and an exercise of the same subject fall on one day, the lecture is first.

build():    classes are placed wave by wave (every course's 1st session, then every 2nd ...), hardest first,
            each at the cheapest legal day/time/room.
improve():  takes classes that are still unplaced, finds the day/time where only 1-3 placed classes are in the
            way, moves those out, puts the unplaced class in and tries to re-place the moved ones elsewhere.
            The change is kept only if no hours are lost (and usually some are gained), otherwise undone.
"""
import random
import time
from collections import defaultdict

import config
import importer
import model


class Unit:
    """One scheduling decision: a session, or the sessions of parallel groups that must run at the same time."""

    def __init__(self, sessions, groups):
        self.sessions = sessions
        self.length = sessions[0].length * 60
        self.hours = sessions[0].length
        self.course = sessions[0].course
        self.teachers = {t for s in sessions for t in s.teachers}
        self.students = {st for s in sessions for st in model.student_sets(s, groups)}
        self.resources = [("t", t) for t in self.teachers] + [("s", st) for st in self.students]
        self.starts = model.valid_starts(self.length)
        self.chain = None
        self.pos = 0
        self.prev = None
        self.next = None
        self.done = False
        self.day = 0
        self.start = 0
        self.rooms = {}                       # session id -> (room, rank)

    @property
    def abs_start(self):
        return self.day * model.DAY + self.start

    @property
    def abs_end(self):
        return self.abs_start + self.length

    def keys(self):
        return self.resources + [("r", room) for room, _ in self.rooms.values()]


class Scheduler:
    def __init__(self, sessions, placed=None):
        self.sessions = list(sessions)
        placed = placed or {}
        groups = model.cohort_groups(self.sessions)
        self.base, self.step, self.ndays = config.DAY_START_MIN, config.SLOT_MINUTES, config.NUM_DAYS
        self.weekend = {d for d in range(self.ndays) if model.weekday(d) >= 5}

        self.units, self.chains = {}, defaultdict(list)
        members = defaultdict(list)
        for s in self.sessions:
            members[model.slot_key(s)].append(s)
        for s in self.sessions:
            key = model.slot_key(s)
            if key not in self.units:
                u = self.units[key] = Unit(members[key], groups)
                u.chain = (id(s.course), 0 if s.course.parallel else s.group)
                chain = self.chains[u.chain]
                u.pos = len(chain)
                if chain:
                    u.prev = chain[-1]
                    chain[-1].next = u
                chain.append(u)

        self.masks = {}
        self.busy = defaultdict(int)          # (resource, day) -> bit mask of taken slots
        self.occ = defaultdict(list)          # (resource, day) -> units
        self.cohort_hours = defaultdict(int)
        self.chain_day = defaultdict(list)

        courses = {id(s.course): s.course for s in self.sessions}
        self.course_units = defaultdict(list)
        for u in self.units.values():
            self.course_units[id(u.course)].append(u)
        self.lectures_of = defaultdict(list)      # exercise course id -> ids of its lecture courses
        self.exercises_of = defaultdict(list)     # lecture course id -> ids of its exercise courses
        for lecture, exercise in importer.link_lectures(list(courses.values())):
            self.lectures_of[id(exercise)].append(id(lecture))
            self.exercises_of[id(lecture)].append(id(exercise))

        self.teacher_load = defaultdict(int)
        for s in self.sessions:
            for t in s.teachers:
                self.teacher_load[t] += s.length

        for u in self.units.values():
            if all(s.id in placed and placed[s.id].scheduled for s in u.sessions):
                p0 = placed[u.sessions[0].id]
                self.apply(u, p0.day, p0.start,
                           {s.id: (placed[s.id].room, placed[s.id].room_rank) for s in u.sessions if placed[s.id].room})

    # ------------------------------------------------------------------ state
    def mask(self, length, start):
        k = (length, start)
        if k not in self.masks:
            self.masks[k] = ((1 << (length // self.step)) - 1) << ((start - self.base) // self.step)
        return self.masks[k]

    def apply(self, u, day, start, rooms):
        u.done, u.day, u.start, u.rooms = True, day, start, rooms
        m = self.mask(u.length, start)
        for key in u.keys():
            self.busy[(key, day)] |= m
            self.occ[(key, day)].append(u)
        for st in u.students:
            self.cohort_hours[(st, day)] += u.hours
        self.chain_day[(u.chain, day)].append(u)

    def remove(self, u):
        m = self.mask(u.length, u.start)
        for key in u.keys():
            self.busy[(key, u.day)] &= ~m
            self.occ[(key, u.day)].remove(u)
        for st in u.students:
            self.cohort_hours[(st, u.day)] -= u.hours
        self.chain_day[(u.chain, u.day)].remove(u)
        u.done = False
        u.rooms = {}

    # ------------------------------------------------------------------ scoring
    def cost_of(self, u, day, start, rooms):
        abs_start = day * model.DAY + start
        wd = model.weekday(day)
        cost = 0.001 * abs_start
        end = start + u.length
        for s in u.sessions:
            for r in s.course.requirements:
                if not r.ok(wd, start, end):
                    cost += config.W_REQUIREMENT
        if day in self.weekend:
            cost += config.W_WEEKEND
        if end > config.LATE_AFTER_MIN:
            cost += config.W_LATE
        w_room = config.W_ROOM
        for _, (_, rank) in rooms.items():
            cost += w_room[min(rank, len(w_room) - 1)]
        for st in u.students:
            have = self.cohort_hours.get((st, day), 0)
            cost += config.W_LONG_DAY * (max(0, have + u.hours - config.MAX_DAILY_HOURS)
                                         - max(0, have - config.MAX_DAILY_HOURS))
        return cost

    def min_abs(self, u):
        """Earliest start allowed by the chain order, or None if the previous equal-length part is unplaced."""
        if u.prev is not None and u.prev.length == u.length:
            return u.prev.abs_end if u.prev.done else None
        return 0

    # ------------------------------------------------------------------ lecture / exercise rules
    def first_end(self, course_id):
        """End (absolute minutes) of the earliest placed session of a course, or None."""
        ends = [x.abs_end for x in self.course_units.get(course_id, ()) if x.done]
        return min(ends) if ends else None

    def rule_info(self, u):
        """What the placed lectures / exercises demand of u. None = u cannot be placed now (lecture missing)."""
        info = {"lo": 0, "day_end": {}, "min_y": None, "others_first": None, "day_start": {}}
        cid = id(u.course)
        if u.course.kind == importer.EXERCISE:
            for lc in self.lectures_of.get(cid, ()):
                fe = self.first_end(lc)
                if fe is None:
                    return None                    # the lecture is not placed yet
                info["lo"] = max(info["lo"], fe)
                for x in self.course_units.get(lc, ()):
                    if x.done:
                        info["day_end"][x.day] = max(info["day_end"].get(x.day, 0), x.abs_end)
        else:
            ys = [y for ec in self.exercises_of.get(cid, ()) for y in self.course_units.get(ec, ()) if y.done]
            if ys:
                info["min_y"] = min(y.abs_start for y in ys)
                for y in ys:
                    info["day_start"][y.day] = min(info["day_start"].get(y.day, 10 ** 9), y.abs_start)
                info["others_first"] = self.first_end(cid)
        return info

    def legal_time(self, u, info, day, start):
        """Rules that depend on other placed classes, for u at day/start."""
        abs_start = day * model.DAY + start
        if abs_start < info["lo"]:
            return False
        if abs_start < info["day_end"].get(day, 0):
            return False                           # a lecture of the same subject is later that day
        if info["min_y"] is not None:
            abs_end = abs_start + u.length
            if abs_end > info["day_start"].get(day, 10 ** 9):
                return False                       # an exercise of the same subject is earlier that day
            others = info["others_first"]
            if abs_end > info["min_y"] and (others is None or others > info["min_y"]):
                return False                       # this lecture would come after an already placed exercise
        return True

    def rules_ok(self, v):
        """Check the rules for a placed unit against the current state."""
        if len(self.chain_day.get((v.chain, v.day), ())) > 1:
            return False
        cid = id(v.course)
        if v.course.kind == importer.EXERCISE:
            for lc in self.lectures_of.get(cid, ()):
                fe = self.first_end(lc)
                if fe is None or v.abs_start < fe:
                    return False
                for x in self.course_units.get(lc, ()):
                    if x.done and x.day == v.day and x.abs_end > v.abs_start:
                        return False
        else:
            fe = self.first_end(cid)
            for ec in self.exercises_of.get(cid, ()):
                for y in self.course_units.get(ec, ()):
                    if y.done and (y.abs_start < fe or (y.day == v.day and v.abs_end > y.abs_start)):
                        return False
        return True

    def rules_hold(self, changed):
        """After a tentative change: do the rules still hold for everything it could affect?"""
        courses = set()
        for u in changed:
            cid = id(u.course)
            courses.add(cid)
            courses.update(self.lectures_of.get(cid, ()))
            courses.update(self.exercises_of.get(cid, ()))
        for cid in courses:
            for v in self.course_units.get(cid, ()):
                if v.done and not self.rules_ok(v):
                    return False
        return True

    def free_rooms(self, u, day, start):
        """Best free room per session at this time, or None if some session has no free room."""
        m = self.mask(u.length, start)
        rooms, taken = {}, set()
        for s in u.sessions:
            if not s.course.rooms:
                continue
            for rank, room in enumerate(s.course.rooms):
                if room not in taken and not self.busy.get((("r", room), day), 0) & m:
                    rooms[s.id] = (room, rank)
                    taken.add(room)
                    break
            else:
                return None
        return rooms

    # ------------------------------------------------------------------ plain placement
    def place(self, u):
        lo = self.min_abs(u)
        info = self.rule_info(u)
        if lo is None or info is None:
            return False
        best = None
        for day in range(self.ndays):
            if self.chain_day.get((u.chain, day)):
                continue                           # one session per day for a course group
            union = 0
            for res in u.resources:
                union |= self.busy.get((res, day), 0)
            for start in u.starts:
                if day * model.DAY + start < lo or union & self.mask(u.length, start):
                    continue
                if not self.legal_time(u, info, day, start):
                    continue
                rooms = self.free_rooms(u, day, start)
                if rooms is None:
                    continue
                c = self.cost_of(u, day, start, rooms)
                if best is None or c < best[0]:
                    best = (c, day, start, rooms)
        if best is None:
            return False
        self.apply(u, best[1], best[2], best[3])
        return True

    def run_greedy(self):
        def difficulty(u):
            nrooms = min((len(s.course.rooms) for s in u.sessions if s.course.rooms), default=99)
            load = max((self.teacher_load[t] for t in u.teachers), default=0)
            return (u.pos, u.course.kind != importer.LECTURE, nrooms, -load, -u.hours)
        for u in sorted((u for u in self.units.values() if not u.done), key=difficulty):
            self.place(u)

    # ------------------------------------------------------------------ local search
    @staticmethod
    def cascade(u):
        """u and the equal-length successors that must be unplaced with it."""
        out = [u]
        x = u
        while x.next is not None and x.next.length == x.length and x.next.done:
            out.append(x.next)
            x = x.next
        return out

    def blockers_at(self, u, day, start, limit_hours):
        """(units to move out, rooms) for putting u at day/start, or None if too many are in the way."""
        end = start + u.length
        m = self.mask(u.length, start)
        blockers = set()

        def overlapping(key):
            return [o for o in self.occ.get((key, day), ()) if o.start < end and start < o.start + o.length]

        for res in u.resources:
            if self.busy.get((res, day), 0) & m:
                blockers.update(overlapping(res))
        taken = set()
        for s in u.sessions:
            if not s.course.rooms:
                continue
            options = []
            for rank, room in enumerate(s.course.rooms):
                if room in taken:
                    continue
                inway = overlapping(("r", room)) if self.busy.get((("r", room), day), 0) & m else []
                options.append((len(inway), rank, room, inway))
            if not options:
                return None
            _, _, room, inway = min(options, key=lambda t: t[:3])
            taken.add(room)
            blockers.update(inway)
        full = set()
        for b in blockers:
            if b is u.prev or b is u:
                return None
            full.update(self.cascade(b))
        if sum(b.hours for b in full) > limit_hours:
            return None
        return full

    def try_insert(self, u, rng, max_blocked):
        lo = self.min_abs(u)
        info = self.rule_info(u)
        if lo is None or info is None:
            return None
        best = []
        for day in range(self.ndays):
            if self.chain_day.get((u.chain, day)):
                continue
            for start in u.starts:
                if day * model.DAY + start < lo or not self.legal_time(u, info, day, start):
                    continue
                full = self.blockers_at(u, day, start, max_blocked)
                if full is None:
                    continue
                hours = sum(b.hours for b in full)
                best.append((hours, rng.random(), day, start, full))
        if not best:
            return None
        best.sort(key=lambda t: t[:2])
        return best[0]

    def attempt(self, u, rng, max_blocked, plateau):
        """Move blockers out for u. Returns the net hours gained (>= 0) or None when undone."""
        choice = self.try_insert(u, rng, max_blocked)
        if choice is None:
            return None
        _, _, day, start, full = choice
        moved = sorted(full, key=lambda b: b.pos)
        saved = [(b, b.day, b.start, dict(b.rooms)) for b in moved]
        for b in moved:
            self.remove(b)
        rooms = self.free_rooms(u, day, start)
        if rooms is None:
            for b, d, s_, r in saved:
                self.apply(b, d, s_, r)
            return None
        self.apply(u, day, start, rooms)
        lost = 0
        for b in moved:
            if not self.place(b):
                lost += b.hours
        gain = u.hours - lost
        if (gain > 0 or (gain == 0 and plateau)) and self.rules_hold([u] + moved):
            return gain
        for b in moved:
            if b.done:
                self.remove(b)
        self.remove(u)
        for b, d, s_, r in saved:
            self.apply(b, d, s_, r)
        return None

    def unplaced(self):
        return [u for u in self.units.values() if not u.done]

    def improve(self, seconds, patience, log):
        """Local search for up to `seconds`, or until `patience` seconds pass without gaining hours."""
        rng = random.Random(1)
        started = last_gain = last_log = time.time()
        iterations = gained = 0
        while time.time() - started < seconds and time.time() - last_gain < patience:
            todo = [u for u in self.unplaced() if self.min_abs(u) is not None and self.rule_info(u) is not None]
            if not todo:
                break
            u = rng.choice(todo)
            result = self.attempt(u, rng, max_blocked=rng.choice((3, 6, 9)), plateau=rng.random() < 0.3)
            iterations += 1
            if result:
                gained += result
                last_gain = time.time()
            if time.time() - last_log > 60:
                last_log = time.time()
                left = sum(x.hours for x in self.unplaced())
                log(f"  local search {time.time() - started:6.0f}s: {left} h unplaced "
                    f"(+{gained} h gained, {iterations} tries)")
        return gained

    # ------------------------------------------------------------------ output
    def placements(self):
        result = []
        for s in self.sessions:
            u = self.units[model.slot_key(s)]
            if u.done:
                room, rank = u.rooms.get(s.id, (None, 0))
                p = model.Placement(s, True, u.day, u.start, room, rank)
                p.unmet = model.unmet_preferences(p)
            else:
                p = model.Placement(s)
            result.append(p)
        return result


def build(sessions, placed=None, log=print):
    """Place every session that is not in `placed` ({session id: Placement}). Returns a Placement per session."""
    sched = Scheduler(sessions, placed)
    sched.run_greedy()
    return sched.placements()


def improve(sessions, placements, seconds, patience, log=print):
    """Local search starting from `placements`. Returns (placements, hours gained)."""
    sched = Scheduler(sessions, placed={p.session.id: p for p in placements if p.scheduled})
    gained = sched.improve(seconds, patience, log)
    return sched.placements(), gained