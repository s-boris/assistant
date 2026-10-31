#!/usr/bin/env python3
"""Writes the synthetic Jefit exports in fixtures/ (made-up data, fixed seed; layout of a real export).

jefit-kg.csv        12 weeks of an upper/lower split ending 2026-10-17: warm-up sets, a renamed exercise,
                    a quoted name with a comma, bodyweight and timed sets, a cardio row, an empty log,
                    a log without its session row, a custom exercise, PROFILE body stats, set logs, notes.
jefit-kg-later.csv  the same export two days later: one session got an extra set, two new sessions.
jefit-lb.csv        a short export in pounds / inches (SETTING mass " lb").
jefit-minimal.csv   the bare two-section layout (no SETTING, no ids): needs --unit.
Run: python3 gen_fixtures.py  (rewrites the files; tests compare against the committed ones)
"""

import csv
import datetime as dt
import io
import os
import random

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
UTC = dt.UTC
START = dt.date(2026, 7, 27)  # a Monday
TS = '"2026-10-17 20:00:00"'

# name, belongSys, eid
EX = {
    "bench": ("Barbell Bench Press", 1, 3),
    "ohp": ("Barbell Overhead Press", 1, 19),
    "lat": ("Dumbbell Lateral Raise", 1, 41),
    "push": ("Cable Triceps Pushdown", 1, 88),
    "row": ("Barbell Row", 1, 27),
    "incline": ("Incline Dumbbell Press", 1, 9),
    "pullup": ("Pull Up", 1, 30),
    "dips": ("Dips", 1, 12),
    "cross": ("Cable Crossover, High to Low", 1, 15),
    "curl": ("Dumbbell Curl", 1, 50),
    "squat": ("Barbell Squat", 1, 101),
    "rdl": ("Romanian Deadlift", 1, 110),
    "legext": ("Leg Extension", 1, 120),
    "calf": ("Standing Calf Raise", 1, 130),
    "plank": ("Plank", 1, 61),
    "hip": ("Barbell Hip Thrust", 1, 140),
    "dead": ("Deadlift", 1, 105),
    "press": ("Leg Press", 1, 121),
    "legcurl": ("Lying Leg Curl", 1, 111),
    "hlr": ("Hanging Leg Raise", 1, 62),
    "tread": ("Treadmill", 1, 500),
    "bird": ("Bird Dog", 1, 777),
    "land": ("Landmine 180", 0, 1),
}


def q(v):
    s = str(v)
    return '"' + s.replace('"', '""') + '"' if any(c in s for c in ',"\n') else s


def sets(ws, reps):
    return ",".join(f"{w:g}x{r}" for w, r in zip(ws, reps, strict=False))


def plan_dates(extra=False):
    out = []
    for wk in range(12):
        monday = START + dt.timedelta(weeks=wk)
        days = [(0, "UA"), (1, "LA"), (3, "UB"), (5, "LB")]
        if wk == 2:
            days = [d for d in days if d[1] != "UB"]
        if wk == 6:  # holiday week
            continue
        if wk == 7:
            days = [d for d in days if d[0] >= 3]
        out += [(monday + dt.timedelta(days=o), kind) for o, kind in days]
    if extra:
        out += [(dt.date(2026, 10, 19), "UA"), (dt.date(2026, 10, 20), "LA")]
    return out


def build(extra=False):
    rnd = random.Random(7)
    sessions, logs, setlogs = [], [], []
    sid, lid = 1000, 5000
    n = {"UA": 0, "LA": 0, "UB": 0, "LB": 0}
    for day, kind in plan_dates(extra):
        i = n[kind]
        n[kind] += 1
        start = int(dt.datetime(day.year, day.month, day.day, 18, 0, tzinfo=UTC).timestamp())
        sid += 1
        L = []
        if kind == "UA":
            # bench: progress to week 6, then stuck (plateau)
            top = min(80 + 2.5 * i, 87.5) if i < 6 else [85, 87.5, 85, 87.5, 85, 87.5, 85][min(i - 6, 6)]
            r = [6, 6, 5] if i < 6 else [5, 5, 4]
            bname = "Bench Press" if i < 3 else EX["bench"][0]
            L.append(("bench", bname, f"40x10,60x5,{sets([top] * 3, r)},"))
            o = 45 + 1.25 * i
            L.append(("ohp", None, sets([o] * 3, [6, 6, 6])))
            L.append(("lat", None, sets([10, 10, 10], [12, 12, 11])))
            L.append(("push", None, sets([30 + (i // 3) * 2.5] * 3, [12, 12, 10])))
            L.append(("row", None, sets([60 + 1.25 * i] * 3, [8, 8, 8])))
        elif kind == "LA":
            s = 100 + 2.5 * i
            L.append(("squat", None, f"60x5,80x3,{sets([s] * 3, [5, 5, 5])}"))
            L.append(("rdl", None, sets([80 + 1.25 * i] * 4, [8, 8, 8, 8])))
            L.append(("hip", None, sets([90 + 2.5 * i] * 3, [10, 10, 10])))
            L.append(("legext", None, sets([50 + (i // 4) * 5] * 3, [12, 12, 12])))
            L.append(("calf", None, sets([60] * 4, [12, 12, 12, 12])))
            L.append(("plank", None, "0x60,0x60,0x45"))
            L.append(("bird", None, "0x10,0x10"))
        elif kind == "UB":
            L.append(("incline", None, sets([28 + (i // 3) * 2] * 3, [10, 10, 9])))
            L.append(("pullup", None, sets([0, 0, 0], [7 + i // 4, 6 + i // 4, 6])))
            L.append(("dips", None, sets([0, 0, 0], [12, 10, 10])))
            L.append(("lat", None, sets([10, 10, 10], [12, 12, 12])))
            L.append(("cross", None, sets([15, 15, 15], [12, 12, 12])))
            L.append(("curl", None, sets([12, 12, 12], [10, 10, 8])))
        else:
            L.append(("dead", None, f"100x3,{sets([140 + 2.5 * i] * 2, [5, 5])}"))
            L.append(("press", None, sets([180 + 5 * i] * 3, [10, 10, 10])))
            L.append(("legcurl", None, sets([40 + (i // 3) * 2.5] * 3, [12, 12, 12])))
            L.append(("legext", None, sets([50 + (i // 4) * 5] * 3, [12, 12, 12])))
            L.append(("calf", None, sets([60] * 4, [12, 12, 12, 12])))
            L.append(("hlr", None, "0x12,0x10,0x10"))
            if i % 3 == 0:
                L.append(("tread", None, "0x0.0,0x20"))
            L.append(("land", None, "20x10,20x10"))
        if extra and day == dt.date(2026, 10, 17):
            L = [(k, nm, lg + (",0x8" if k == "hlr" else "")) for k, nm, lg in L]
        total = 0
        for k, nm, lg in L:
            name, bsys, eid = EX[k]
            lid += 1
            for tok in lg.strip(",").split(","):
                w, r = tok.split("x")
                if "." not in r:
                    total += float(w) * int(r)
            rec = 0
            logs.append([42, TS, bsys, lg, lid, rec, day.isoformat(), eid, nm or name, 0, sid, start, "", "No"])
            if k == "plank" and day >= dt.date(2026, 10, 1):
                for j, d in enumerate([60, 60, 45]):
                    setlogs.append([len(setlogs) + 1, 42, lid, j, 0, "", "", "", "", "", d, "default", start, TS])
        dur = 3600 + rnd.randrange(-600, 900, 60)
        sessions.append(
            [len(sessions) + 1, sid, 42, 0, 70, dur, 0, 0, 0, len(L), round(total), 0, start, start + dur, 0, TS, 0, 0]
        )
    # a log whose session row is missing, and an empty log
    lid += 1
    logs.append([42, TS, 1, sets([12, 12], [10, 10]), lid, 0, "2026-08-08", 50, EX["curl"][0], 0, 999, 0, "", "No"])
    lid += 1
    logs.append([42, TS, 1, "", lid, 0, "2026-08-10", 3, "", 0, 1002, 0, "", "No"])
    return sessions, logs, setlogs


def write_kg(path, extra=False):
    sessions, logs, setlogs = build(extra)
    b = io.StringIO()
    w = csv.writer(b, lineterminator="\n")
    b.write("### SETTING " + "#" * 40 + "\n\n")
    b.write("row_id,USERID,TIMESTAMP,gender,currentRoutine,name,length,mass,timer\n")
    b.write(f'1,42,{TS},M,7,," cm"," kg",90\n\n' + "#" * 52 + "\n\n")
    b.write("### PROFILE " + "#" * 40 + "\n\n")
    b.write(
        "row_id,USERID,TIMESTAMP,_id,mydate,weight,fatpercent,chest,arms,waist,calves,height,hips,thighs,"
        "shoulders,neck,forearms,logTime\n"
    )
    for i in range(6):
        d = START + dt.timedelta(weeks=2 * i)
        b.write(
            f"{i + 1},42,{TS},{i + 1},{d.isoformat()},{82.0 - 0.3 * i:.1f},{17 - 0.2 * i:.1f},100,38,"
            f"{84 - 0.4 * i:.1f},0,180,0,0,0,0,0,0\n"
        )
    b.write("\n" + "#" * 52 + "\n\n")
    b.write("### ROUTINES " + "#" * 39 + "\n\n")
    b.write(
        "row_id,USERID,TIMESTAMP,_id,name,difficulty,focus,dayaweek,description,daytype,tags,rdb_id,bannerCode,"
        "progression_flag\n"
    )
    b.write(f'1,42,{TS},7,"Upper, Lower",1,1,4,"Four days, made up",0," ",0,,0\n\n')
    hdr = (
        "row_id,USERID,TIMESTAMP,belongSys,superset,_id,exercise_id,belongplan,exercisename,setcount,timer,logs,"
        "bodypart,mysort,targetrep,setdone,setdonetime,interval_time,interval_unit,rest_time_enabled,"
        "interval_time_enabled,is_tier_primary\n"
    )
    b.write(hdr + f'2,42,{TS},1,0,1,777,71,"Bird Dog",2,60,"",0,0,"10,10",0,0,0,0,1,0,0\n\n')
    b.write(hdr + f'3,42,{TS},1,0,2,101,71,"Barbell Squat",3,180,"",8,1,"5,5,5",0,0,0,0,1,0,0\n')
    b.write("\n" + "#" * 52 + "\n\n")
    b.write("### WORKOUT SESSIONS " + "#" * 31 + "\n\n")
    b.write(
        "rowid,_id,USERID,edit_time,day_id,total_time,workout_time,rest_time,wasted_time,total_exercise,"
        "total_weight,recordbreak,starttime,endtime,workout_mode,TIMESTAMP,calories,avg_heart_rate\n"
    )
    for r in sessions:
        w.writerow(r)
    b.write("\n" + "#" * 52 + "\n\n")
    b.write("### EXERCISE LOGS " + "#" * 34 + "\n\n")
    b.write(
        "USERID,TIMESTAMP,belongSys,logs,_id,record,mydate,eid,ename,day_item_id,belongsession,logTime,"
        "interval_logs,auto_generated\n"
    )
    for r in logs:
        r = list(r)
        r[1] = "2026-10-17 20:00:00"
        w.writerow(r)
    b.write("\n" + "#" * 52 + "\n\n")
    b.write("### EXERCISE SET LOGS " + "#" * 30 + "\n\n")
    b.write(
        "_id,userid,exercise_log_id,set_index,weight_lbs,reps,calories,distance_mi,speed_mph,laps,duration,"
        "set_type,log_time,TIMESTAMP\n"
    )
    for r in setlogs:
        r = list(r)
        r[-1] = "2026-10-17 20:00:00"
        w.writerow(r)
    b.write("\n" + "#" * 52 + "\n\n")
    b.write("### CUSTOM EXERCISES " + "#" * 31 + "\n\n")
    b.write(
        "row_id,USERID,TIMESTAMP,rating,name,description,image2,image1,bodypart,bodypart3,bodypart2,_id,equip1,"
        "equip2,recordtype,link,hashcode,unilateral,sys_exercise_map_id\n"
    )
    b.write(f'1,42,{TS},0,"Landmine 180",,0,0,0,11,11,1,0,0,0,,,0,\n')
    b.write("\n" + "#" * 52 + "\n\n")
    b.write("### NOTES " + "#" * 42 + "\n\n")
    b.write("row_id,TIMESTAMP,USERID,_id,eid,belongSys,mynote,title,mydate,logTime,label,bodypart,is_recovered,type\n")
    b.write(
        f'1,{TS},42,1,101,1,"Felt heavy, slept badly\n### not a section, just text",'
        f'"Barbell Squat",2026-09-22,0,0,-1,0,0\n'
    )
    b.write("\n" + "#" * 52 + "\n")
    with open(path, "w", newline="") as fh:
        fh.write(b.getvalue())


def write_lb(path):
    txt = [
        "### SETTING ###",
        "row_id,USERID,TIMESTAMP,gender,currentRoutine,name,length,mass,timer",
        f'1,77,{TS},F,0,," inch"," lb",60',
        "#" * 20,
        "",
        "### PROFILE ###",
        "row_id,USERID,TIMESTAMP,_id,mydate,weight,fatpercent,chest,arms,waist,logTime",
        f"1,77,{TS},1,2025-10-06,176,0,0,0,32,0",
        "#" * 20,
        "",
        "### WORKOUT SESSIONS ###",
        "_id,starttime,endtime,total_time,total_weight",
    ]
    days = [dt.date(2025, 10, 6), dt.date(2025, 10, 8), dt.date(2025, 10, 10)]
    for i, d in enumerate(days):
        st = int(dt.datetime(d.year, d.month, d.day, 7, 30, tzinfo=UTC).timestamp())
        txt.append(f"{i + 1},{st},{st + 2700},2700,0")
    txt += ["#" * 20, "", "### EXERCISE LOGS ###", "belongsession,eid,belongSys,ename,mydate,logs,record"]
    for i, d in enumerate(days):
        txt.append(f'{i + 1},3,1,"Barbell Bench Press",{d.isoformat()},"135x8,135x8,{135 + 5 * i}x6",0')
        txt.append(f'{i + 1},30,1,"Chin Up",{d.isoformat()},"0x8,0x6,25x5",0')
        txt.append(f'{i + 1},61,1,"Plank",{d.isoformat()},"0x45,0x40",0')
    txt.append("#" * 20)
    with open(path, "w", newline="") as fh:
        fh.write("\n".join(txt) + "\n")


def write_minimal(path):
    txt = [
        "### WORKOUT SESSIONS ###",
        "_id,mydate,starttime,starttimie,total_time",
        "1,2025-11-03,,1762156800,3600",
        "2,2025-11-05,1762329600,,3000",
        "###########################",
        "### EXERCISE LOGS ###",
        "belongsession,ename,logs",
        '1,"Barbell Squat","135x5,185x5,185x5"',
        '2,"Barbell Squat","135x5,190x5,190x5"',
        '2,"Push Up","0x20,0x15"',
        "###########################",
    ]
    with open(path, "w", newline="") as fh:
        fh.write("\n".join(txt) + "\n")


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    write_kg(os.path.join(OUT, "jefit-kg.csv"))
    write_kg(os.path.join(OUT, "jefit-kg-later.csv"), extra=True)
    write_lb(os.path.join(OUT, "jefit-lb.csv"))
    write_minimal(os.path.join(OUT, "jefit-minimal.csv"))
    print("fixtures written to", OUT)
