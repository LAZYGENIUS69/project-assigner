# Project Assigner

Reads a student list from Excel, randomly puts students into groups, gives
each group a capstone project, and writes the result back out as Excel.

## Install and run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m assigner.cli "~/Downloads/ISE Student List.xlsx" --seed 42 -o out/assignments.xlsx
```

## Options

| Flag | Does |
|---|---|
| `-n, --group-size` | students per group (default 3) |
| `-s, --seed` | **use one.** Same seed = same allocation, so you can reproduce or defend it |
| `--sheet` | pick a sheet by name (default: the first) |
| `--cohort` | label for the output, e.g. `"3rd Year ISE"`. The sheet in the supplied file is labelled `2nd Year` but the cohort is third year, so set this. |
| `--set A` / `--set B` | limit to one half of the catalogue |
| `--starters-only` | only the projects that ship with a ready-made starter repo |
| `--no-repeats` | fail rather than give one project to two groups |
| `--dry-run` | print the allocation, write nothing |
| `--projects` | point at a different catalogue `.json` |

## Output

Three sheets:

- **Assignments** — one row per student: group, name, USN, project, language
- **Groups** — one row per group: members, project, whether a starter exists,
  and the project's *hard part*
- **Summary** — the seed, group size, source sheet and the counts, so the run
  is auditable afterwards

## Reading messy spreadsheets

College lists rarely have headers on row 1. The one this was built against
has a title, a department name, a blank row, and the real headers on **row 7**.

So the reader *searches* the first 30 rows for a row containing both a name
column and an id column, accepting `USN`, `USR`, `Roll No`, `Reg No` and so
on. It fails with a clear message rather than silently importing nothing.

It also refuses duplicate IDs, and skips rows with no name.

## Verified against the real list

```
source       : ISE Student List.xlsx, sheet "2nd Year" (mislabelled - cohort is 3rd year)
students     : 60      (no blanks, no duplicate USNs)
group size 3 : 20 groups, 20 distinct projects, 0 repeats
group size 4 : 15 groups, 15 distinct projects, 0 repeats
group size 2 : 30 groups - refuses with --no-repeats, since there are only 25 projects
--starters-only : 4 projects spread evenly, 5 groups each
```

## Where the logic lives

`assigner/allocate.py` is **pure** — no Excel, no file system, no printing.
That is the file worth unit testing, and the cases that matter are:

- an exact division (60 ÷ 3) versus an awkward one (61 ÷ 3)
- **nobody is ever left in a group alone** — leftovers join existing groups
  rather than forming a group of one
- the same seed always produces the same allocation
- more groups than projects wraps round evenly instead of running out

## Meeting slots (the viva schedule)

Each student gets **one 10-minute slot**, evenings only.

| Rule | Value |
|---|---|
| Window | 18:00 to 20:00 — 12 slots a day |
| First / last slot | starts 18:00 / starts 19:50 and **ends** 20:00. There is no 20:00 slot |
| Days | the next Mon, Tue, Wed, Thu and **Sat** |
| Friday | **holiday — always skipped.** Sunday is never used |
| Capacity | 5 days x 12 = 60 slots, exactly one cohort, zero spare |
| Order | shuffled by `seed`, so the same seed always gives the same schedule |

More students than slots is refused with the shortfall named
(`61 students but only 60 slots - add another day`). Fewer students just
leaves the late slots empty. The start date is *inclusive*: starting on a
Saturday puts the first viva that Saturday, starting on a Friday or a
Sunday moves it to the next teaching day.

### Endpoints

| Method | Path | Does |
|---|---|---|
| `POST` | `/api/schedules` | `{"cohort_id":2,"seed":42,"start_date":"2026-10-12"}` — books the whole cohort |
| `GET` | `/api/schedules` | list them |
| `GET` | `/api/schedules/{id}` | every student with their day and time |
| `GET` | `/api/schedules/{id}/export.csv` | CSV download: Name, USN, Day, Date, Time, Project, Fork this repo |

The CSV joins each student to their project from the **latest run** for that
cohort; the Project and repo columns are blank if the cohort has never been
run. Rendered schedules are cached in Redis and the cache is dropped when a
new schedule is created.

The database enforces the two rules that matter: `UNIQUE(schedule_id,
student_id)` so nobody is booked twice, and `UNIQUE(schedule_id, slot_at)`
so two students are never given the same slot.

### Where the logic lives

`app/schedule.py` is **pure** — no database, no HTTP. That is the file worth
unit testing, and the cases that matter are listed in its docstring: starting
on a Friday, a Saturday or a Sunday; the window dividing into exactly 12
slots with the last ending at 20:00; more students than slots; and the same
seed reproducing the same schedule.
