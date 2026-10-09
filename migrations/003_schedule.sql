-- One viva/demo schedule for a cohort: every student gets one 10-minute
-- evening slot. The seed and the start date are stored so the schedule can
-- be reproduced later and defended if a student says they were singled out.
CREATE TABLE IF NOT EXISTS schedules (
    id         SERIAL PRIMARY KEY,
    cohort_id  INT  NOT NULL REFERENCES cohorts(id) ON DELETE CASCADE,
    seed       INT  NOT NULL,
    start_date DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ONE ROW PER STUDENT. Both UNIQUE constraints carry weight:
--   (schedule_id, student_id) - nobody is booked into two slots
--   (schedule_id, slot_at)    - two students are never in the same slot
CREATE TABLE IF NOT EXISTS meeting_slots (
    id          SERIAL PRIMARY KEY,
    schedule_id INT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    student_id  INT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    slot_at     TIMESTAMPTZ NOT NULL,
    UNIQUE (schedule_id, student_id),
    UNIQUE (schedule_id, slot_at)
);

CREATE INDEX IF NOT EXISTS schedules_cohort ON schedules (cohort_id);
CREATE INDEX IF NOT EXISTS meeting_slots_order ON meeting_slots (schedule_id, slot_at);
