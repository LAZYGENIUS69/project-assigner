import csv
import datetime as dt
import io
import json
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import cache, db, schedule
from .allocate import (
    BATCH_SIZE, AllocationError, assign_individual, into_batches, parse_csv, summarise,
)

HERE = Path(__file__).resolve().parent
PROJECTS = json.loads((HERE / "projects.json").read_text())

app = FastAPI(title="project-assigner")


@app.get("/api/health")
def health():
    out = {"status": "ok", "postgres": False, "redis": False}
    try:
        db.query("SELECT 1")
        out["postgres"] = True
    except Exception as exc:
        out["pg_error"] = str(exc)
    try:
        cache.client().ping()
        out["redis"] = True
    except Exception as exc:
        out["redis_error"] = str(exc)
    return out if (out["postgres"] and out["redis"]) else JSONResponse(out, status_code=503)


@app.get("/api/projects")
def projects():
    hit = cache.get_json("projects")
    if hit:
        return {"projects": hit, "cached": True}
    cache.set_json("projects", PROJECTS, ttl=86400)
    return {"projects": PROJECTS, "cached": False}


def _pool(name):
    if name in ("A", "B"):
        return [p for p in PROJECTS if p["set"] == name]
    if name == "starter":
        return [p for p in PROJECTS if p.get("starter")]
    return list(PROJECTS)


# ---------------------------- cohorts -----------------------------
@app.get("/api/cohorts")
def list_cohorts():
    return {"cohorts": db.query(
        "SELECT c.id, c.label,"
        " (SELECT count(*) FROM students s WHERE s.cohort_id = c.id) AS students"
        " FROM cohorts c ORDER BY c.id DESC")}


def _store_cohort(label, roster):
    row = db.one("INSERT INTO cohorts (label) VALUES (%s) RETURNING id", (label,))
    cid = row["id"]
    with db.connect() as conn, conn.cursor() as cur:
        for s in roster:
            cur.execute("INSERT INTO students (cohort_id, name, usn) VALUES (%s,%s,%s)",
                        (cid, s["name"], s["usn"] or None))
    return {"id": cid, "label": label, "students": len(roster)}


@app.post("/api/cohorts/upload", status_code=201)
async def upload_cohort(file: UploadFile):
    """Upload the class list as CSV. This is how a cohort gets in."""
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    try:
        roster = parse_csv(raw)
    except AllocationError as exc:
        raise HTTPException(400, str(exc))
    return _store_cohort(file.filename or "Uploaded CSV", roster)


@app.post("/api/cohorts", status_code=201)
def paste_cohort(payload: dict = Body(...)):
    try:
        roster = parse_csv(payload.get("text", ""))
    except AllocationError as exc:
        raise HTTPException(400, str(exc))
    return _store_cohort(str(payload.get("label", "Pasted roster")).strip() or "Pasted roster",
                         roster)


# ------------------------------ runs ------------------------------
@app.post("/api/runs", status_code=201)
def create_run(payload: dict = Body(...)):
    cohort_id = payload.get("cohort_id")
    seed = int(payload.get("seed", 42))
    pool_name = payload.get("pool", "all")
    batch = int(payload.get("batch_size", BATCH_SIZE))

    students = db.query("SELECT id, name, usn FROM students WHERE cohort_id = %s ORDER BY id",
                        (cohort_id,))
    if not students:
        raise HTTPException(404, "no such cohort, or it has no students")

    pool = _pool(pool_name)
    if not pool:
        raise HTTPException(400, f"pool {pool_name!r} is empty")

    try:
        allocation = assign_individual([dict(s) for s in students], pool, seed)
    except AllocationError as exc:
        raise HTTPException(400, str(exc))

    batches = into_batches(allocation, batch)
    run = db.one("INSERT INTO runs (cohort_id, seed, pool, batch_size)"
                 " VALUES (%s,%s,%s,%s) RETURNING id",
                 (cohort_id, seed, pool_name, batch))
    run_id = run["id"]

    with db.connect() as conn, conn.cursor() as cur:
        for bno, group in enumerate(batches, start=1):
            for a in group:
                cur.execute(
                    "INSERT INTO assignments"
                    " (run_id, student_id, position, batch_no, project_id, project_title)"
                    " VALUES (%s,%s,%s,%s,%s,%s)",
                    (run_id, a["student"]["id"], a["position"], bno,
                     a["project"]["id"], a["project"]["title"]))

    cache.reset_reveals(run_id)
    return {"run_id": run_id, "seed": seed, "pool": pool_name, "batch_size": batch,
            **summarise(allocation)}


@app.get("/api/runs")
def list_runs():
    return {"runs": db.query(
        "SELECT r.id, c.label, r.seed, r.pool, r.batch_size,"
        " (SELECT count(*) FROM assignments a WHERE a.run_id = r.id) AS students,"
        " (SELECT count(*) FROM assignments a WHERE a.run_id = r.id AND a.revealed) AS revealed"
        " FROM runs r JOIN cohorts c ON c.id = r.cohort_id ORDER BY r.id DESC")}


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    meta = db.one("SELECT id, cohort_id, seed, pool, batch_size FROM runs WHERE id = %s",
                  (run_id,))
    if not meta:
        raise HTTPException(404, "no such run")
    rows = db.query(
        "SELECT a.position, a.batch_no, a.project_id, a.project_title, a.revealed,"
        " s.name, s.usn FROM assignments a JOIN students s ON s.id = a.student_id"
        " WHERE a.run_id = %s ORDER BY a.position", (run_id,))
    done = db.one("SELECT count(*) AS n FROM assignments WHERE run_id = %s AND revealed",
                  (run_id,))
    return {"run": meta, "assignments": rows, "revealed": done["n"], "total": len(rows)}


@app.post("/api/runs/{run_id}/reveal")
def reveal_next_batch(run_id: int):
    """Reveal the next batch - five students per pull of the lever."""
    nxt = db.one("SELECT min(batch_no) AS b FROM assignments"
                 " WHERE run_id = %s AND NOT revealed", (run_id,))
    if not nxt or nxt["b"] is None:
        total = db.one("SELECT count(*) AS n FROM assignments WHERE run_id = %s", (run_id,))
        if not total or total["n"] == 0:
            raise HTTPException(404, "no such run")
        return JSONResponse({"done": True, "message": "every student has been assigned"},
                            status_code=409)

    bno = nxt["b"]
    db.query("UPDATE assignments SET revealed = TRUE, revealed_at = now()"
             " WHERE run_id = %s AND batch_no = %s", (run_id, bno), fetch=False)
    cache.incr_reveal(run_id)

    rows = db.query(
        "SELECT a.position, a.project_id, a.project_title, s.name, s.usn"
        " FROM assignments a JOIN students s ON s.id = a.student_id"
        " WHERE a.run_id = %s AND a.batch_no = %s ORDER BY a.position", (run_id, bno))
    by_id = {p["id"]: p for p in PROJECTS}
    revealed = [{**dict(r), "project": by_id.get(r["project_id"])} for r in rows]
    left = db.one("SELECT count(DISTINCT batch_no) AS n FROM assignments"
                  " WHERE run_id = %s AND NOT revealed", (run_id,))
    return {"done": False, "batch": bno, "students": revealed, "batches_remaining": left["n"]}


@app.post("/api/runs/{run_id}/reset")
def reset_run(run_id: int):
    db.query("UPDATE assignments SET revealed = FALSE, revealed_at = NULL WHERE run_id = %s",
             (run_id,), fetch=False)
    cache.reset_reveals(run_id)
    return {"run_id": run_id, "reset": True}


@app.get("/api/runs/{run_id}/export.csv")
def export_csv(run_id: int):
    """A real CSV download, for sharing with the class afterwards."""
    rows = db.query(
        "SELECT a.position, s.name, s.usn, a.project_id, a.project_title, a.revealed"
        " FROM assignments a JOIN students s ON s.id = a.student_id"
        " WHERE a.run_id = %s ORDER BY a.position", (run_id,))
    if not rows:
        raise HTTPException(404, "no such run")

    by_id = {p["id"]: p for p in PROJECTS}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Position", "Name", "USN", "Project #", "Project", "Set",
                "Language", "Fork this repo", "Revealed"])
    for r in rows:
        p = by_id.get(r["project_id"], {})
        w.writerow([r["position"], r["name"], r["usn"] or "", r["project_id"],
                    r["project_title"], p.get("set", ""), p.get("language", ""),
                    p.get("repo", ""),
                    "yes" if r["revealed"] else "no"])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="assignments-run-{run_id}.csv"'})


# ---------------------------- schedules ---------------------------
# One 10-minute evening slot per student. See app/schedule.py for the rules.
SCHEDULE_INDEX = "schedules:index"


def _schedule_key(schedule_id):
    return f"schedule:{schedule_id}"


def _render_schedule(schedule_id):
    """The whole schedule as plain JSON-safe data, or None if there isn't one."""
    meta = db.one("SELECT id, cohort_id, seed, start_date FROM schedules WHERE id = %s",
                  (schedule_id,))
    if not meta:
        return None
    rows = db.query(
        "SELECT m.slot_at, s.name, s.usn FROM meeting_slots m"
        " JOIN students s ON s.id = m.student_id"
        " WHERE m.schedule_id = %s ORDER BY m.slot_at", (schedule_id,))

    slots = [{"position": i, "name": r["name"], "usn": r["usn"] or "",
              **schedule.slot_fields(r["slot_at"])}
             for i, r in enumerate(rows, start=1)]
    days = sorted({s["date"] for s in slots})
    return {
        "schedule": {**meta, "start_date": meta["start_date"].isoformat()},
        "days": days,
        "per_day": {d: sum(1 for s in slots if s["date"] == d) for d in days},
        "slots": slots,
        "total": len(slots),
    }


@app.post("/api/schedules", status_code=201)
def create_schedule(payload: dict = Body(...)):
    """Book the whole cohort: 5 evenings x 12 slots, Friday skipped."""
    cohort_id = payload.get("cohort_id")
    seed = int(payload.get("seed", 42))
    raw_start = str(payload.get("start_date", "")).strip()
    try:
        start = dt.date.fromisoformat(raw_start) if raw_start else dt.date.today()
    except ValueError:
        raise HTTPException(400, f"start_date {raw_start!r} is not a date like 2026-10-12")

    students = db.query("SELECT id, name, usn FROM students WHERE cohort_id = %s ORDER BY id",
                        (cohort_id,))
    if not students:
        raise HTTPException(404, "no such cohort, or it has no students")

    try:
        days = schedule.teaching_days(start)
        slots = schedule.build_slots(days)
        booked = schedule.assign_slots([dict(s) for s in students], slots, seed)
    except schedule.ScheduleError as exc:
        raise HTTPException(400, str(exc))

    # One transaction: a failed slot insert must not leave an empty schedule behind.
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO schedules (cohort_id, seed, start_date)"
                    " VALUES (%s,%s,%s) RETURNING id", (cohort_id, seed, start))
        schedule_id = cur.fetchone()["id"]
        for b in booked:
            cur.execute("INSERT INTO meeting_slots (schedule_id, student_id, slot_at)"
                        " VALUES (%s,%s,%s)", (schedule_id, b["student"]["id"], b["slot"]))

    cache.drop(SCHEDULE_INDEX)
    cache.drop(_schedule_key(schedule_id))
    return {"schedule_id": schedule_id, "cohort_id": cohort_id, "seed": seed,
            "start_date": start.isoformat(), "days": [d.isoformat() for d in days],
            "capacity": len(slots), "booked": len(booked),
            "spare_slots": len(slots) - len(booked)}


@app.get("/api/schedules")
def list_schedules():
    hit = cache.get_json(SCHEDULE_INDEX)
    if hit:
        return {"schedules": hit, "cached": True}
    rows = db.query(
        "SELECT sc.id, sc.cohort_id, c.label, sc.seed, sc.start_date, sc.created_at,"
        " (SELECT count(*) FROM meeting_slots m WHERE m.schedule_id = sc.id) AS slots"
        " FROM schedules sc JOIN cohorts c ON c.id = sc.cohort_id ORDER BY sc.id DESC")
    out = [{**dict(r), "start_date": r["start_date"].isoformat(),
            "created_at": r["created_at"].isoformat()} for r in rows]
    cache.set_json(SCHEDULE_INDEX, out, ttl=3600)
    return {"schedules": out, "cached": False}


@app.get("/api/schedules/{schedule_id}")
def get_schedule(schedule_id: int):
    """Every student with their day and time."""
    hit = cache.get_json(_schedule_key(schedule_id))
    if hit:
        return {**hit, "cached": True}
    out = _render_schedule(schedule_id)
    if not out:
        raise HTTPException(404, "no such schedule")
    cache.set_json(_schedule_key(schedule_id), out, ttl=3600)
    return {**out, "cached": False}


@app.get("/api/schedules/{schedule_id}/export.csv")
def export_schedule_csv(schedule_id: int):
    """A real CSV download, for pinning to the notice board."""
    meta = db.one("SELECT id, cohort_id FROM schedules WHERE id = %s", (schedule_id,))
    if not meta:
        raise HTTPException(404, "no such schedule")

    # Project columns come from the latest run for this cohort, if there is one.
    rows = db.query(
        "SELECT s.name, s.usn, m.slot_at, a.project_id, a.project_title"
        " FROM meeting_slots m"
        " JOIN students s ON s.id = m.student_id"
        " LEFT JOIN assignments a ON a.student_id = m.student_id AND a.run_id ="
        "   (SELECT max(r.id) FROM runs r WHERE r.cohort_id = %s)"
        " WHERE m.schedule_id = %s ORDER BY m.slot_at", (meta["cohort_id"], schedule_id))

    by_id = {p["id"]: p for p in PROJECTS}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Name", "USN", "Day", "Date", "Time", "Project", "Fork this repo"])
    for r in rows:
        f = schedule.slot_fields(r["slot_at"])
        p = by_id.get(r["project_id"], {})
        w.writerow([r["name"], r["usn"] or "", f["day"], f["date"], f["time"],
                    r["project_title"] or "", p.get("repo", "")])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="schedule-{schedule_id}.csv"'})


STATIC = HERE / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC / "index.html"))
