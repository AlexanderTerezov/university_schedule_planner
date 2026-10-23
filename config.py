"""Everything you may want to change lives here."""
from datetime import date

# --- Input / output -------------------------------------------------------------
INPUT_FILE = "data/fixed_data.xlsx"
LECTURES_SHEET = "Sheet1"
EXERCISES_SHEET = "Sheet2"
OUTPUT_FILE = "timetable.xlsx"

# --- Scheduling period (every date in the range is usable, weekends included) ---
START_DATE = date(2027, 1, 4)    # Monday
END_DATE = date(2027, 1, 24)     # Sunday

# --- Daily hours and lunch (24h "HH:MM"; hours in the sheets are clock hours) -----
DAY_START = "08:00"
DAY_END = "20:00"
LUNCH_START = "12:00"
LUNCH_MINUTES = 60               # nothing may be scheduled in this window; 0 = no lunch break
SLOT_MINUTES = 60                # classes start on multiples of this, counted from DAY_START

# --- Records with missing values -------------------------------------------------
# None   = skip the record and log an ERROR (nothing is invented).
# number = use that value instead and log an INFO.
DEFAULT_GROUPS = 1               # exercises with an empty "Брой групи"
DEFAULT_CONTINUATION = 3         # rows with an empty "Продължение" (3 is the most common value)

# --- What makes a timetable "good" (penalties: the solver minimises the total) -----
# Order of importance: leaving a class out >> breaking a preference from the sheet >> comfort.
W_UNSCHEDULED_PER_HOUR = 1000    # per hour of a class that cannot be placed at all
W_REQUIREMENT = 30               # per class that breaks a rule from "Изисквания"
W_ROOM = (0, 4, 8)               # 1st, 2nd, 3rd room wish (later wishes cost the last value)
W_WEEKEND = 6                    # per class on Saturday / Sunday
W_SAME_DAY = 8                   # per extra class of the same subject+group on one day
W_LONG_DAY = 5                   # per hour above MAX_DAILY_HOURS for a student cohort on one day
MAX_DAILY_HOURS = 6
W_LATE = 2                       # per class that ends after LATE_AFTER
LATE_AFTER = "18:00"

# --- Solver ----------------------------------------------------------------------
SOLVER_SECONDS = 1200
SOLVER_WORKERS = 0               # 0 = use all CPU cores

# --- Known typos in the source file (upper case, Cyrillic) -------------------------
SPECIALTY_ALIASES = {
    "КПКПМ": "КМКПМ",
    "КМПКМ": "КМКПМ",
    "ГГГПТ": "ГГПТТ",
    "ГПМР": "ГПМЕР",
    "АУИТ": "АИУТ",
}
ROOM_ALIASES = {
    "L01D": "101D",
}


# --- Derived values (no need to edit) ----------------------------------------------
def _minutes(hhmm):
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


DAY_START_MIN = _minutes(DAY_START)
DAY_END_MIN = _minutes(DAY_END)
LUNCH_START_MIN = _minutes(LUNCH_START)
LATE_AFTER_MIN = _minutes(LATE_AFTER)
NUM_DAYS = (END_DATE - START_DATE).days + 1
