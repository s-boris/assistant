#!/usr/local/bin/python3.13 -I
"""Synthetic sample history for ~/opt/unraid-capacity-tests/run.sh (the samples.jsonl format `record` writes).

  gen.py OUT [--days 40] [--rate GB/day] [--cache-rate GB/day] [--drop-day N --drop-gb G] [--age-days A]
             [--docker-rate GB/day | --docker-gb V1,V2,...]

Two 18 TB data disks; at the newest sample disk1 has 2 TB free and disk2 2.5 TB, the media share's floor is
100000000 KiB (102.4 GB), so ~4.295 TB is usable: 100 GB/day -> full in ~43 days. The cache pool (1 TB) ends
with 500 GB free; docker.img (100 GB) is flat at 30 GB used. --drop-day puts a one-off delete of G GB on that
day (index). --docker-rate: docker.img grows steadily, ending at 30 GB; --docker-gb: its used GB on the last
len(list) days (older days repeat the first value).
"""

import argparse
import json
import time

ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("--days", type=int, default=40)
ap.add_argument("--rate", type=float, default=100)
ap.add_argument("--cache-rate", type=float, default=0)
ap.add_argument("--drop-day", type=int, default=-1)
ap.add_argument("--drop-gb", type=float, default=0)
ap.add_argument("--age-days", type=float, default=0)
ap.add_argument("--docker-rate", type=float, default=0)
ap.add_argument("--docker-gb", default="")
a = ap.parse_args()
G, TB = 1e9, 1e12
SIZE, FLOOR = 18 * TB, 100000000 * 1024
# The newest sample sits at the latest 11:00 UTC (12:00/13:00 Zurich) at least 10 min ago: samples are exactly
# 86400 s apart, and the script counts the first sample of each local day, so a time of day within an hour of
# midnight would put two samples on one date (or skip one) across a DST change and the rates would depend on the
# hour the suite runs.
DAY, ANCHOR = 86400, 11 * 3600
end = (int(time.time()) - 600 - ANCHOR) // DAY * DAY + ANCHOR - int(a.age_days * DAY)
end1, end2, cend = 16 * TB, 15.5 * TB, 0.5 * TB
dk = [float(x) for x in a.docker_gb.split(",") if x.strip()]
with open(a.out, "w") as f:
    for i in range(a.days):
        back = a.days - 1 - i  # days before the newest sample
        dropped = a.drop_gb * G if 0 <= a.drop_day and i < a.drop_day else 0
        u1 = end1 - back * a.rate * G / 2 + dropped  # before the drop, usage was higher by the dropped amount
        u2 = end2 - back * a.rate * G / 2
        cu = cend - back * a.cache_rate * G
        du = (dk[max(0, len(dk) - 1 - back)] * G) if dk else 30 * G - back * a.docker_rate * G
        ts = end - back * 86400
        disks = {
            "disk1": {"size": SIZE, "used": int(u1), "free": int(SIZE - u1), "status": "DISK_OK"},
            "disk2": {"size": SIZE, "used": int(u2), "free": int(SIZE - u2), "status": "DISK_OK"},
        }
        s = {
            "v": 1,
            "ts": ts,
            "date": time.strftime("%Y-%m-%d", time.localtime(ts)),
            "host": "Homesrv",
            "disks": disks,
            "pools": {"cache": {"size": int(TB), "used": int(cu), "free": int(TB - cu), "fs": "btrfs"}},
            "docker_img": {"size": int(100 * G), "used": int(du), "free": int(100 * G - du)},
            "alloc": {"share": "media", "allocator": "highwater", "floor": FLOOR, "include": "", "exclude": ""},
        }
        s["array"] = {k: sum(d[k] for d in disks.values()) for k in ("size", "used", "free")}
        s["array"]["n"] = 2
        f.write(json.dumps(s, separators=(",", ":")) + "\n")
