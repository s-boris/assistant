#!/usr/bin/env -S python3 -I
"""Regenerate the fixture responses for a fictional trip ("testisles", three made-up islands, UTC+4, 2030).
Shapes follow the real APIs (Open-Meteo forecast/marine/archive multi-location lists, NOAA CRW station text,
GOV.UK content JSON). Run: python3 -I make_fixtures.py  (writes ./fixtures/)"""

import datetime as dt
import json
import math
import os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
D0 = dt.date(2030, 3, 8)  # NOW is 2030-03-10: past_days=2 + forecast_days=16 -> 08.03..25.03
os.makedirs(OUT, exist_ok=True)


def dump(name, obj):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def days(start, n):
    return [(start + dt.timedelta(days=i)).isoformat() for i in range(n)]


# per-island daily series (index 0 = 08.03); North: calm 10.03, rough 11.03, marginal 12.03, go 13.03; NW wind 12.-13.03
north = {
    "rain": [1, 1, 30, 5, 0.5] + [0] * 13,
    "wind": [8, 8, 8, 26, 15, 10] + [9] * 12,
    "dir": [120, 120, 120, 200, 300, 310] + [100] * 12,
    "code": [2, 2, 95] + [2] * 15,
}
south = {"rain": [0.2] * 18, "wind": [9] * 18, "dir": [90] * 18, "code": [1] * 18}
east = {"rain": [0] * 18, "wind": [7] * 18, "dir": [80] * 18, "code": [0] * 18}
fc = []
for lat, lon, s in ((10.0, 20.0, north), (9.0, 21.0, south), (8.0, 22.0, east)):
    t = days(D0, 18)
    daily = {
        "time": t,
        "weather_code": s["code"],
        "temperature_2m_max": [30.0] * 18,
        "temperature_2m_min": [25.0] * 18,
        "precipitation_sum": s["rain"],
        "precipitation_probability_max": [80] * 18,
        "wind_speed_10m_max": s["wind"],
        "wind_gusts_10m_max": [w + 8 for w in s["wind"]],
        "wind_direction_10m_dominant": s["dir"],
        "uv_index_max": [9.2] * 18,
        "sunrise": [d + "T06:10" for d in t],
        "sunset": [d + "T18:20" for d in t],
    }
    for k in daily:  # the 16th forecast day comes back empty, as the live API often does
        if k != "time":
            daily[k][-1] = None
    fc.append({"latitude": lat, "longitude": lon, "timezone": "Etc/GMT-4", "daily": daily})
dump("forecast.json", fc)

M0 = dt.date(2030, 3, 10)
waves = {"north": [0.8, 2.4, 1.5, 1.0, 0.9, 0.9, 0.9, 0.9], "south": [0.7, 2.2] + [0.7] * 6, "east": [0.6] * 8}
mar = []
for lat, lon, key in ((10.1, 20.1, "north"), (9.1, 21.1, "south"), (8.1, 22.1, "east")):
    t = days(M0, 8)
    w = waves[key]
    hours = [(dt.datetime(2030, 3, 10) + dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(192)]
    mar.append(
        {
            "latitude": lat,
            "longitude": lon,
            "timezone": "Etc/GMT-4",
            "daily": {
                "time": t,
                "wave_height_max": w,
                "wave_direction_dominant": [135] * 8,
                "wave_period_max": [7.5] * 8,
                "swell_wave_height_max": [round(x * 0.8, 2) for x in w],
                "swell_wave_direction_dominant": [150] * 8,
                "swell_wave_period_max": [8.0] * 8,
                "wind_wave_height_max": [0.4] * 8,
            },
            "hourly": {
                "time": hours,
                "sea_surface_temperature": [28.0 if h % 2 else 29.0 for h in range(192)],
                "sea_level_height_msl": [
                    round(0.5 + 0.4 * math.cos(2 * math.pi * (h - 3) / 12), 3) for h in range(192)
                ],
            },
        }
    )
dump("marine.json", mar)

arc = []
for lat, lon in ((10.0, 20.0), (9.0, 21.0), (8.0, 22.0)):
    t = days(dt.date(2029, 3, 20), 12)
    arc.append(
        {
            "latitude": lat,
            "longitude": lon,
            "daily": {
                "time": t,
                "temperature_2m_max": [30.0] * 12,
                "temperature_2m_min": [25.0] * 12,
                "precipitation_sum": [4.0 if int(d[8:]) % 2 == 0 else 0.0 for d in t],
                "wind_speed_10m_max": [10.0] * 12,
            },
        }
    )
dump("archive.json", arc)

dump(
    "govuk.json",
    {
        "title": "Testland travel advice",
        "public_updated_at": "2030-01-15T10:00:00+00:00",
        "details": {
            "change_description": "Fictional update to the safety page.",
            "alert_status": [],
            "reviewed_at": "2030-01-15T10:00:00Z",
            "parts": [{"slug": "safety-and-security"}],
        },
    },
)

with open(os.path.join(OUT, "crw.txt"), "w", encoding="utf-8") as f:
    f.write(
        "Name:\nTestland\n \nPolygon Middle Longitude:\n20.0000\n \n"
        "YYYY MM DD SST_MIN SST_MAX SST@90th_HS SSTA@90th_HS 90th_HS>0 DHW_from_90th_HS>1 BAA_7day_max\n"
        "2030 03 04 26.0000 29.3000 29.0000      1.2000       0.0000    0.0000            0\n"
        "2030 03 05 26.1000 29.4000 29.1000      1.3000       0.0000    0.0000            0\n"
    )

dump(
    "trips.json",
    {
        "trips": {
            "testisles": {
                "name": "Test Isles",
                "emoji": "🏝",
                "tz": "Etc/GMT-4",
                "from": "2030-03-11",
                "to": "2030-03-30",
                "legs": [
                    {
                        "name": "North Isle",
                        "spot": "Harbour Beach",
                        "from": "2030-03-12",
                        "to": "2030-03-18",
                        "lat": 10.0,
                        "lon": 20.0,
                        "sea_lat": 10.1,
                        "sea_lon": 20.1,
                        "coast_faces_deg": 315,
                        "dive_label": "Outer reef",
                    },
                    {
                        "name": "South Isle",
                        "spot": "Palm Cove",
                        "from": "2030-03-18",
                        "to": "2030-03-22",
                        "lat": 9.0,
                        "lon": 21.0,
                        "sea_lat": 9.1,
                        "sea_lon": 21.1,
                        "coast_faces_deg": 90,
                        "crossing": "Fast ferry",
                    },
                    {
                        "name": "East Isle",
                        "from": "2030-03-24",
                        "to": "2030-03-29",
                        "lat": 8.0,
                        "lon": 22.0,
                        "sea_lat": 8.1,
                        "sea_lon": 22.1,
                    },
                ],
                "reef": {"crw_station": "testland"},
                "advisory": {
                    "govuk": "testland",
                    "links": [{"label": "Other advisory", "url": "https://example.org/advice"}],
                },
                "season": {
                    "climate_years": 5,
                    "months": {"3": "fictional dry season, light winds"},
                    "wind_regimes": [
                        {
                            "name": "NW regime",
                            "from_deg": 270,
                            "to_deg": 360,
                            "min_days": 2,
                            "note": "west coasts get the swell",
                        }
                    ],
                },
                "notes": ["Fictional note line"],
            }
        }
    },
)
print("fixtures written to", OUT)
