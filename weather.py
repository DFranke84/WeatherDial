"""WeatherDial data fetcher.

Pulls every weather source for the location in config.json, turns each into the
same shape, works out a consensus, and writes data/latest.json for the panel.

    python weather.py          fetch (each source only when its cache is stale)
    python weather.py --force  fetch everything now
"""
import datetime as dt
import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CACHE = DATA / "cache"
UA = "WeatherDial/0.1 (personal desktop weather widget)"
UTC = dt.timezone.utc


# ---------------------------------------------------------------- helpers

def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    tmp.replace(path)


def get(url, timeout=25, headers=None):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/geo+json, application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def short_error(e):
    """Error text without the URL (URLs may hold API keys)."""
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (401, 403):
            return f"key refused (HTTP {e.code}): expired trial or wrong key?"
        if e.code == 429:
            return "too many requests today (free limit reached)"
        return f"HTTP {e.code} {e.reason}"
    if isinstance(e, urllib.error.URLError):
        return f"network: {e.reason}"
    return f"{type(e).__name__}: {e}"


def c_to_f(c):
    return None if c is None else c * 9 / 5 + 32


def r1(x):
    return None if x is None else round(x, 1)


def r0(x):
    return None if x is None else round(x)


class Clock:
    """Converts times to local 'YYYY-MM-DDTHH:00' keys using the place's UTC offset."""

    def __init__(self, offset_s):
        self.off = dt.timedelta(seconds=offset_s)

    def from_epoch(self, t):
        return (dt.datetime.fromtimestamp(t, UTC) + self.off).replace(tzinfo=None)

    def from_iso(self, s):
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:          # already local
            return d
        return (d.astimezone(UTC) + self.off).replace(tzinfo=None)

    @staticmethod
    def hour(d):
        return d.strftime("%Y-%m-%dT%H:00")

    @staticmethod
    def day(d):
        return d.strftime("%Y-%m-%d")


# ---------------------------------------------------------------- icons

WMO = {0: "clear", 1: "partly", 2: "partly", 3: "cloudy", 45: "fog", 48: "fog",
       51: "drizzle", 53: "drizzle", 55: "drizzle", 56: "sleet", 57: "sleet",
       61: "rain", 63: "rain", 65: "rain", 66: "sleet", 67: "sleet",
       71: "snow", 73: "snow", 75: "snow", 77: "snow", 80: "rain", 81: "rain", 82: "rain",
       85: "snow", 86: "snow", 95: "storm", 96: "storm", 99: "storm"}
WMO_TEXT = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog",
            48: "Freezing fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
            56: "Freezing drizzle", 57: "Freezing drizzle", 61: "Light rain", 63: "Rain",
            65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain", 71: "Light snow",
            73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Rain showers",
            81: "Rain showers", 82: "Heavy showers", 85: "Snow showers", 86: "Snow showers",
            95: "Thunderstorms", 96: "Thunderstorms, hail", 99: "Thunderstorms, hail"}


def text_icon(text):
    t = (text or "").lower()
    for words, icon in ((("thunder", "t-storm", "tstorm"), "storm"),
                        (("snow", "flurr", "blizzard"), "snow"),
                        (("sleet", "freezing", "ice pellet", "wintry"), "sleet"),
                        (("smoke",), "smoke"),
                        (("rain", "shower"), "rain"),
                        (("drizzle",), "drizzle"),
                        (("fog", "haze", "mist"), "fog"),
                        (("mostly cloudy", "overcast", "cloudy"), None),
                        ):
        if any(w in t for w in words):
            if icon is None:
                return "partly" if "partly" in t else "cloudy"
            return icon
    if any(w in t for w in ("partly", "mostly sunny", "mostly clear", "few clouds")):
        return "partly"
    if any(w in t for w in ("sunny", "clear", "fair")):
        return "clear"
    if any(w in t for w in ("wind", "breez")):
        return "wind"
    return "cloudy" if t else None


def tomorrow_icon(code):
    c = int(code or 0)
    if c in (1000,):
        return "clear"
    if c in (1100, 1101):
        return "partly"
    if c in (1102, 1001):
        return "cloudy"
    if c // 1000 == 2:
        return "fog"
    if c == 4000:
        return "drizzle"
    if c // 1000 == 4:
        return "rain"
    if c // 1000 == 5:
        return "snow"
    if c // 1000 in (6, 7):
        return "sleet"
    if c // 1000 == 8:
        return "storm"
    return None


TOMORROW_TEXT = {1000: "Clear", 1100: "Mostly clear", 1101: "Partly cloudy", 1102: "Mostly cloudy",
                 1001: "Cloudy", 2000: "Fog", 2100: "Light fog", 4000: "Drizzle", 4001: "Rain",
                 4200: "Light rain", 4201: "Heavy rain", 5000: "Snow", 5001: "Flurries",
                 5100: "Light snow", 5101: "Heavy snow", 6000: "Freezing drizzle",
                 6001: "Freezing rain", 6200: "Light freezing rain", 6201: "Heavy freezing rain",
                 7000: "Ice pellets", 7101: "Heavy ice pellets", 7102: "Light ice pellets",
                 8000: "Thunderstorm"}


def mph_text(s):
    """'5 to 10 mph' -> 10"""
    nums = [int(n) for n in "".join(ch if ch.isdigit() else " " for ch in (s or "")).split()]
    return max(nums) if nums else None


# ---------------------------------------------------------------- sources
# Each returns {"daily": [...], "hourly": [...], optional "now", "alerts", "extra"}.
# daily  item: date, hi, lo, pop, precip, wind, text, icon
# hourly item: t, temp, pop, precip, wind, gust, text, icon

OM_MODELS = {  # Open-Meteo model id -> display name
    "gfs_seamless": "NOAA GFS",
    "ecmwf_ifs025": "ECMWF (Europe)",
    "icon_seamless": "DWD ICON (Germany)",
    "gem_seamless": "GEM (Canada)",
    "ukmo_seamless": "UK Met Office",
}
OM_UNITS = "temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch&timezone=auto"


def src_openmeteo(cfg, keys):
    """Open-Meteo 'best match' blend: current conditions, hourly detail, sun times."""
    q = (f"latitude={cfg['lat']}&longitude={cfg['lon']}&{OM_UNITS}&forecast_days=16"
         "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,"
         "wind_speed_10m,wind_gusts_10m,wind_direction_10m,pressure_msl,cloud_cover,is_day,dew_point_2m"
         "&hourly=temperature_2m,apparent_temperature,precipitation_probability,precipitation,"
         "weather_code,wind_speed_10m,wind_gusts_10m,relative_humidity_2m,uv_index,is_day,snowfall"
         "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
         "precipitation_sum,snowfall_sum,wind_speed_10m_max,wind_gusts_10m_max,sunrise,sunset,"
         "uv_index_max,daylight_duration")
    d = get("https://api.open-meteo.com/v1/forecast?" + q)
    h, dd, c = d["hourly"], d["daily"], d["current"]
    hourly = [{"t": h["time"][i][:13] + ":00", "temp": r1(h["temperature_2m"][i]),
               "feels": r1(h["apparent_temperature"][i]),
               "pop": h["precipitation_probability"][i], "precip": h["precipitation"][i],
               "snow": h["snowfall"][i],
               "wind": r0(h["wind_speed_10m"][i]), "gust": r0(h["wind_gusts_10m"][i]),
               "rh": h["relative_humidity_2m"][i], "uv": h["uv_index"][i],
               "day": h["is_day"][i],
               "text": WMO_TEXT.get(h["weather_code"][i]), "icon": WMO.get(h["weather_code"][i])}
              for i in range(len(h["time"]))]
    daily = [{"date": dd["time"][i], "hi": r1(dd["temperature_2m_max"][i]),
              "lo": r1(dd["temperature_2m_min"][i]), "pop": dd["precipitation_probability_max"][i],
              "precip": dd["precipitation_sum"][i], "snow": dd["snowfall_sum"][i],
              "wind": r0(dd["wind_speed_10m_max"][i]), "gust": r0(dd["wind_gusts_10m_max"][i]),
              "text": WMO_TEXT.get(dd["weather_code"][i]), "icon": WMO.get(dd["weather_code"][i]),
              "sunrise": dd["sunrise"][i], "sunset": dd["sunset"][i], "uv": dd["uv_index_max"][i],
              "daylight": dd["daylight_duration"][i]}
             for i in range(len(dd["time"]))]
    now = {"time": c["time"], "temp": r1(c["temperature_2m"]), "feels": r1(c["apparent_temperature"]),
           "rh": c["relative_humidity_2m"], "dew": r1(c["dew_point_2m"]),
           "wind": r0(c["wind_speed_10m"]), "gust": r0(c["wind_gusts_10m"]),
           "dir": c["wind_direction_10m"], "pressure": r1(c["pressure_msl"] * 0.02953 * 10) / 10 if c["pressure_msl"] else None,
           "cloud": c["cloud_cover"], "day": c["is_day"],
           "text": WMO_TEXT.get(c["weather_code"]), "icon": WMO.get(c["weather_code"])}
    return {"daily": daily, "hourly": hourly, "now": now,
            "extra": {"utc_offset": d["utc_offset_seconds"], "timezone": d["timezone"],
                      "elevation_m": d.get("elevation")}}


def src_openmeteo_models(cfg, keys):
    """Five independent global models through Open-Meteo, one result per model."""
    q = (f"latitude={cfg['lat']}&longitude={cfg['lon']}&{OM_UNITS}&forecast_days=16"
         f"&models={','.join(OM_MODELS)}"
         "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
         "precipitation_sum,wind_speed_10m_max"
         "&hourly=temperature_2m,precipitation_probability,precipitation,wind_speed_10m,weather_code")
    d = get("https://api.open-meteo.com/v1/forecast?" + q)
    out = {}
    for m, name in OM_MODELS.items():
        dd, h = d["daily"], d["hourly"]
        k = lambda base: f"{base}_{m}"
        daily = []
        for i, date in enumerate(dd["time"]):
            hi, lo = dd.get(k("temperature_2m_max"), [None] * 99)[i], dd.get(k("temperature_2m_min"), [None] * 99)[i]
            if hi is None and lo is None:
                continue
            code = dd.get(k("weather_code"), [None] * 99)[i]
            daily.append({"date": date, "hi": r1(hi), "lo": r1(lo),
                          "pop": dd.get(k("precipitation_probability_max"), [None] * 99)[i],
                          "precip": dd.get(k("precipitation_sum"), [None] * 99)[i],
                          "wind": r0(dd.get(k("wind_speed_10m_max"), [None] * 99)[i]),
                          "text": WMO_TEXT.get(code), "icon": WMO.get(code)})
        hourly = []
        for i, t in enumerate(h["time"]):
            temp = h.get(k("temperature_2m"), [None] * 999)[i]
            if temp is None:
                continue
            code = h.get(k("weather_code"), [None] * 999)[i]
            hourly.append({"t": t[:13] + ":00", "temp": r1(temp),
                           "pop": h.get(k("precipitation_probability"), [None] * 999)[i],
                           "precip": h.get(k("precipitation"), [None] * 999)[i],
                           "wind": r0(h.get(k("wind_speed_10m"), [None] * 999)[i]),
                           "text": WMO_TEXT.get(code), "icon": WMO.get(code)})
        out[name] = {"daily": daily, "hourly": hourly}
    return out


def src_air(cfg, keys):
    q = (f"latitude={cfg['lat']}&longitude={cfg['lon']}&timezone=auto&forecast_days=4"
         "&current=us_aqi,pm2_5,pm10,ozone,uv_index&hourly=us_aqi,pm2_5")
    d = get("https://air-quality-api.open-meteo.com/v1/air-quality?" + q)
    return {"now": d["current"],
            "hourly": [{"t": t[:13] + ":00", "aqi": a, "pm25": p}
                       for t, a, p in zip(d["hourly"]["time"], d["hourly"]["us_aqi"], d["hourly"]["pm2_5"])]}


def src_nws(cfg, keys, clock):
    base = "https://api.weather.gov"
    pt = get(f"{base}/points/{cfg['lat']:.4f},{cfg['lon']:.4f}")["properties"]
    with ThreadPoolExecutor(4) as ex:
        f_fc = ex.submit(get, pt["forecast"])
        f_hr = ex.submit(get, pt["forecastHourly"])
        f_al = ex.submit(get, f"{base}/alerts/active?point={cfg['lat']:.4f},{cfg['lon']:.4f}")
        f_st = ex.submit(get, pt["observationStations"])
        f_afd = ex.submit(get, f"{base}/products/types/AFD/locations/{pt['cwa']}")
        fc, hr, al, st, afd = (f.result() for f in (f_fc, f_hr, f_al, f_st, f_afd))

    # Daily: day period -> high for that date; night period -> low for the NEXT morning.
    days = {}
    periods = []
    for p in fc["properties"]["periods"]:
        start = clock.from_iso(p["startTime"])
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
        periods.append({"name": p["name"], "day": p["isDaytime"], "temp": p["temperature"],
                        "pop": pop, "wind": p["windSpeed"] + " " + p["windDirection"],
                        "text": p["shortForecast"], "detail": p["detailedForecast"],
                        "icon": text_icon(p["shortForecast"])})
        if p["isDaytime"]:
            d = days.setdefault(clock.day(start), {"date": clock.day(start)})
            d.update(hi=p["temperature"], text=p["shortForecast"], icon=text_icon(p["shortForecast"]),
                     wind=mph_text(p["windSpeed"]))
            d["pop"] = max(pop or 0, d.get("pop") or 0)
        else:
            nxt = clock.day(start + dt.timedelta(days=1))
            d = days.setdefault(nxt, {"date": nxt})
            d["lo"] = p["temperature"]
            today = days.setdefault(clock.day(start), {"date": clock.day(start)})
            today["pop"] = max(pop or 0, today.get("pop") or 0)
    daily = [days[k] for k in sorted(days)]

    hourly = []
    for p in hr["properties"]["periods"]:
        t = clock.from_iso(p["startTime"])
        hourly.append({"t": clock.hour(t), "temp": p["temperature"],
                       "pop": (p.get("probabilityOfPrecipitation") or {}).get("value"),
                       "wind": mph_text(p["windSpeed"]), "text": p["shortForecast"],
                       "icon": text_icon(p["shortForecast"]),
                       "rh": (p.get("relativeHumidity") or {}).get("value")})

    alerts = []
    for f in al.get("features", []):
        a = f["properties"]
        alerts.append({"event": a["event"], "severity": a.get("severity"), "headline": a.get("headline"),
                       "description": a.get("description"), "instruction": a.get("instruction"),
                       "ends": a.get("ends") or a.get("expires"), "source": "NWS"})

    # Latest observation from the nearest station that has a recent temperature.
    now = None
    for s in st.get("features", [])[:3]:
        sid = s["properties"]["stationIdentifier"]
        try:
            o = get(f"{base}/stations/{sid}/observations/latest")["properties"]
        except Exception:
            continue
        if (o.get("temperature") or {}).get("value") is None:
            continue
        v = lambda k: (o.get(k) or {}).get("value")
        kmh = lambda x: None if x is None else x / 1.609
        now = {"station": f"{s['properties']['name']} ({sid})", "time": o["timestamp"],
               "temp": r1(c_to_f(v("temperature"))), "dew": r1(c_to_f(v("dewpoint"))),
               "rh": r0(v("relativeHumidity")), "wind": r0(kmh(v("windSpeed"))),
               "gust": r0(kmh(v("windGust"))), "dir": v("windDirection"),
               "pressure": None if v("barometricPressure") is None else round(v("barometricPressure") / 3386.39, 2),
               "visibility": None if v("visibility") is None else round(v("visibility") / 1609, 1),
               "text": o.get("textDescription"), "icon": text_icon(o.get("textDescription"))}
        break

    discussion = None
    graph = afd.get("@graph") or []
    if graph:
        prod = get(f"{base}/products/{graph[0]['id']}")
        discussion = {"office": pt["cwa"], "issued": prod.get("issuanceTime"),
                      "text": prod.get("productText", "")}

    rel = pt.get("relativeLocation", {}).get("properties", {})
    return {"daily": daily, "hourly": hourly, "now": now, "alerts": alerts,
            "extra": {"periods": periods, "discussion": discussion, "office": pt["cwa"],
                      "near": f"{rel.get('city', '')}, {rel.get('state', '')}".strip(", ")}}


def src_tempest(cfg, keys, clock):
    tok = keys.get("tempest_token")
    sid = cfg.get("tempest_station_id")
    if not sid:
        st = get("https://swd.weatherflow.com/swd/rest/stations?token=" + urllib.parse.quote(tok))
        sid = st["stations"][0]["station_id"]
    units = "&units_temp=f&units_wind=mph&units_pressure=inhg&units_precip=in&units_distance=mi"
    d = get(f"https://swd.weatherflow.com/swd/rest/better_forecast?station_id={sid}{units}"
            f"&token={urllib.parse.quote(tok)}")
    c = d.get("current_conditions", {})
    now = {"station": "Your Tempest", "time": c.get("time") and clock.from_epoch(c["time"]).isoformat(),
           "temp": r1(c.get("air_temperature")), "feels": r1(c.get("feels_like")),
           "rh": c.get("relative_humidity"), "dew": r1(c.get("dew_point")),
           "wind": r0(c.get("wind_avg")), "gust": r0(c.get("wind_gust")), "dir": c.get("wind_direction"),
           "pressure": c.get("sea_level_pressure"), "trend": c.get("pressure_trend"),
           "uv": c.get("uv"), "solar": c.get("solar_radiation"),
           "rain_today": c.get("precip_accum_local_day"),
           "rain_yesterday": c.get("precip_accum_local_yesterday"),
           "strikes_3h": c.get("lightning_strike_count_last_3hr"),
           "strike_dist": c.get("lightning_strike_last_distance"),
           "strike_time": c.get("lightning_strike_last_epoch"),
           "text": c.get("conditions"), "icon": text_icon(c.get("conditions"))}
    f = d.get("forecast", {})
    daily = []
    for x in f.get("daily", []):
        daily.append({"date": clock.day(clock.from_epoch(x["day_start_local"])),
                      "hi": x.get("air_temp_high"), "lo": x.get("air_temp_low"),
                      "pop": x.get("precip_probability"), "text": x.get("conditions"),
                      "icon": text_icon(x.get("conditions"))})
    hourly = [{"t": clock.hour(clock.from_epoch(x["time"])), "temp": x.get("air_temperature"),
               "pop": x.get("precip_probability"), "wind": r0(x.get("wind_avg")),
               "gust": r0(x.get("wind_gust")), "text": x.get("conditions"),
               "icon": text_icon(x.get("conditions"))} for x in f.get("hourly", [])]
    return {"daily": daily, "hourly": hourly, "now": now, "extra": {"station_id": sid}}


WD_WEATHER = {"RA": "Rain", "-RA": "Light rain", "+RA": "Heavy rain", "SN": "Snow", "-SN": "Light snow",
              "+SN": "Heavy snow", "DZ": "Drizzle", "FG": "Fog", "TS": "Thunderstorm", "SH": "Showers"}


def src_weather_display(cfg, keys, clock):
    """Your Tempest, read live from Weather Display's Weather Underground upload file
    (written every few seconds, no network needed)."""
    path = Path(cfg["weather_display_dir"]) / "sentwurapiddata.txt"
    line = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    q = {k: v[0] for k, v in urllib.parse.parse_qs(line.split("?", 1)[1]).items()}
    when = dt.datetime.strptime(q["dateutc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    age = (dt.datetime.now(UTC) - when).total_seconds()
    if age > 600:
        raise RuntimeError(f"Weather Display data is {round(age / 60)} min old")
    f = lambda k: float(q[k]) if q.get(k) not in (None, "") else None
    temp, rh, wind = f("tempf"), f("humidity"), f("windspeedmph")
    feels = temp
    if temp is not None and wind is not None and temp <= 50 and wind > 3:
        feels = 35.74 + .6215 * temp - 35.75 * wind ** .16 + .4275 * temp * wind ** .16
    # Pressure trend from our own log of the last 3 hours.
    plog = load_json(DATA / "pressure_log.json", [])
    now_t = time.time()
    if f("baromin") is not None:
        plog = [p for p in plog if now_t - p[0] < 3 * 3600 + 600] + [[now_t, f("baromin")]]
        save_json(DATA / "pressure_log.json", plog)
    trend = None
    old = [p for p in plog if now_t - p[0] >= 2.5 * 3600]
    if old:
        diff = plog[-1][1] - old[0][1]
        trend = "rising" if diff > .03 else "falling" if diff < -.03 else "steady"
    wx = WD_WEATHER.get(q.get("weather") or "")
    return {"now": {"station": "Your Tempest (via Weather Display)", "time": clock.from_epoch(when.timestamp()).isoformat(),
                    "temp": r1(temp), "feels": r1(feels), "rh": r0(rh), "dew": r1(f("dewptf")),
                    "wind": r0(wind), "gust": r0(f("windgustmph")), "dir": f("winddir"),
                    "pressure": f("baromin"), "trend": trend, "uv": f("UV"), "solar": f("solarradiation"),
                    "rain_rate": f("rainin"), "rain_today": f("dailyrainin"),
                    "text": wx, "icon": text_icon(wx) if wx else None, "age_s": round(age)},
            "daily": [], "hourly": []}


def src_weathercom(cfg, keys, clock):
    """weather.com (The Weather Company) 5-day forecast. The free key for Weather Underground
    station owners only covers the daily 5-day forecast (no hourly, no 7/10-day)."""
    d = get(f"https://api.weather.com/v3/wx/forecast/daily/5day?geocode={cfg['lat']},{cfg['lon']}"
            f"&format=json&units=e&language=en-US&apiKey={urllib.parse.quote(keys['weathercom'])}")
    dp = (d.get("daypart") or [{}])[0]
    part = lambda k, j: (dp.get(k) or [None] * 99)[j] if j < len(dp.get(k) or []) else None
    daily = []
    for i, when in enumerate(d["validTimeLocal"]):
        day_txt, night_txt = part("wxPhraseLong", 2 * i), part("wxPhraseLong", 2 * i + 1)
        pops = [p for p in (part("precipChance", 2 * i), part("precipChance", 2 * i + 1)) if p is not None]
        text = day_txt or night_txt
        daily.append({"date": when[:10],
                      "hi": d["calendarDayTemperatureMax"][i], "lo": d["calendarDayTemperatureMin"][i],
                      "pop": max(pops) if pops else None, "precip": d["qpf"][i],
                      "snow": d["qpfSnow"][i], "text": text, "icon": text_icon(text),
                      "narrative": d["narrative"][i]})
    return {"daily": daily, "hourly": []}


def src_accuweather(cfg, keys, clock):
    """AccuWeather daily + hourly. Finds (once) the location key, how the key must be sent
    and which forecast lengths the plan allows, and remembers that in data/cache."""
    key = keys["accuweather"]
    base = "https://dataservice.accuweather.com"
    memo_file = CACHE / "accuweather_setup.json"
    memo = load_json(memo_file, {})
    if memo.get("key_id") != key[-6:] or memo.get("where") != [cfg["lat"], cfg["lon"]]:
        memo = {"key_id": key[-6:], "where": [cfg["lat"], cfg["lon"]]}

    def call(path, auth=None):
        auth = auth or memo.get("auth", "bearer")
        if auth == "bearer":
            return get(f"{base}{path}", headers={"Authorization": f"Bearer {key}"})
        sep = "&" if "?" in path else "?"
        return get(f"{base}{path}{sep}apikey={urllib.parse.quote(key)}")

    if "location" not in memo:
        q = f"/locations/v1/cities/geoposition/search?q={cfg['lat']},{cfg['lon']}"
        for auth in ("bearer", "query"):
            try:
                loc = call(q, auth)
                memo.update(auth=auth, location=loc["Key"], place=loc.get("LocalizedName"))
                break
            except urllib.error.HTTPError as e:
                if e.code not in (401, 403):
                    raise
        else:
            raise RuntimeError("AccuWeather refused the key (expired trial or wrong key?)")

    def first_allowed(kind, options):
        if memo.get(kind):
            return memo[kind], call(memo[kind])
        last = None
        for path in options:
            try:
                data = call(path)
                memo[kind] = path
                return path, data
            except urllib.error.HTTPError as e:
                last = e
                if e.code not in (401, 403, 404):
                    raise
        raise last

    lk = memo["location"]
    q = "?details=true&metric=false"
    _, dd = first_allowed("daily", [f"/forecasts/v1/daily/{n}day/{lk}{q}" for n in (15, 10, 5)])
    try:
        _, hh = first_allowed("hourly", [f"/forecasts/v1/hourly/{n}hour/{lk}{q}" for n in (72, 12)])
    except urllib.error.HTTPError:
        hh = []
    save_json(memo_file, memo)

    # AccuWeather's "low" for a day is the following night's low; move it to the next
    # calendar day so it matches the other sources (each day's low = that morning's low).
    days = dd["DailyForecasts"]
    daily = []
    for i, x in enumerate(days):
        dy, nt = x.get("Day", {}), x.get("Night", {})
        pops = [p for p in (dy.get("PrecipitationProbability"), nt.get("PrecipitationProbability")) if p is not None]
        text = dy.get("IconPhrase") or nt.get("IconPhrase")
        daily.append({"date": x["Date"][:10], "hi": x["Temperature"]["Maximum"]["Value"],
                      "lo": days[i - 1]["Temperature"]["Minimum"]["Value"] if i else None,
                      "pop": max(pops) if pops else None,
                      "precip": round(((dy.get("TotalLiquid") or {}).get("Value") or 0) +
                                      ((nt.get("TotalLiquid") or {}).get("Value") or 0), 2),
                      "snow": round(((dy.get("Snow") or {}).get("Value") or 0) +
                                    ((nt.get("Snow") or {}).get("Value") or 0), 1),
                      "text": text, "icon": text_icon(text), "narrative": dy.get("LongPhrase")})
    hourly = [{"t": clock.hour(clock.from_iso(x["DateTime"])), "temp": x["Temperature"]["Value"],
               "pop": x.get("PrecipitationProbability"),
               "wind": r0(((x.get("Wind") or {}).get("Speed") or {}).get("Value")),
               "gust": r0(((x.get("WindGust") or {}).get("Speed") or {}).get("Value")),
               "text": x.get("IconPhrase"), "icon": text_icon(x.get("IconPhrase"))} for x in hh]
    return {"daily": daily, "hourly": hourly,
            "extra": {"daily_plan": memo.get("daily"), "hourly_plan": memo.get("hourly")}}


def src_pirate(cfg, keys, clock):
    d = get(f"https://api.pirateweather.net/forecast/{urllib.parse.quote(keys['pirateweather'])}/"
            f"{cfg['lat']},{cfg['lon']}?units=us&extend=hourly")
    pi = lambda s: (s or "").replace("-day", "").replace("-night", "")
    icon = lambda s: {"clear": "clear", "partly-cloudy": "partly", "cloudy": "cloudy", "rain": "rain",
                      "snow": "snow", "sleet": "sleet", "wind": "wind", "fog": "fog"}.get(pi(s))
    daily = [{"date": clock.day(clock.from_epoch(x["time"])), "hi": r1(x.get("temperatureMax")),
              "lo": r1(x.get("temperatureMin")), "pop": r0((x.get("precipProbability") or 0) * 100),
              "precip": x.get("precipAccumulation"), "wind": r0(x.get("windSpeed")),
              "text": x.get("summary"), "icon": icon(x.get("icon"))} for x in d["daily"]["data"]]
    hourly = [{"t": clock.hour(clock.from_epoch(x["time"])), "temp": r1(x.get("temperature")),
               "pop": r0((x.get("precipProbability") or 0) * 100), "wind": r0(x.get("windSpeed")),
               "gust": r0(x.get("windGust")), "text": x.get("summary"), "icon": icon(x.get("icon"))}
              for x in d["hourly"]["data"]]
    return {"daily": daily, "hourly": hourly}


def src_visualcrossing(cfg, keys, clock):
    d = get("https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/"
            f"{cfg['lat']},{cfg['lon']}?unitGroup=us&include=days,hours&contentType=json"
            f"&key={urllib.parse.quote(keys['visualcrossing'])}")
    daily, hourly = [], []
    for x in d["days"]:
        daily.append({"date": x["datetime"], "hi": r1(x.get("tempmax")), "lo": r1(x.get("tempmin")),
                      "pop": r0(x.get("precipprob")), "precip": x.get("precip"),
                      "wind": r0(x.get("windspeed")), "text": x.get("conditions"),
                      "icon": text_icon(x.get("conditions"))})
        for hx in x.get("hours", []):
            hourly.append({"t": clock.hour(clock.from_epoch(hx["datetimeEpoch"])), "temp": r1(hx.get("temp")),
                           "pop": r0(hx.get("precipprob")), "wind": r0(hx.get("windspeed")),
                           "gust": r0(hx.get("windgust")), "text": hx.get("conditions"),
                           "icon": text_icon(hx.get("conditions"))})
    return {"daily": daily, "hourly": hourly}


def src_weatherapi(cfg, keys, clock):
    d = get(f"https://api.weatherapi.com/v1/forecast.json?key={urllib.parse.quote(keys['weatherapi'])}"
            f"&q={cfg['lat']},{cfg['lon']}&days=14&alerts=no&aqi=no")
    daily, hourly = [], []
    for x in d["forecast"]["forecastday"]:
        dy = x["day"]
        daily.append({"date": x["date"], "hi": r1(dy.get("maxtemp_f")), "lo": r1(dy.get("mintemp_f")),
                      "pop": max(dy.get("daily_chance_of_rain") or 0, dy.get("daily_chance_of_snow") or 0),
                      "precip": dy.get("totalprecip_in"), "wind": r0(dy.get("maxwind_mph")),
                      "text": dy["condition"]["text"], "icon": text_icon(dy["condition"]["text"])})
        for hx in x["hour"]:
            hourly.append({"t": clock.hour(clock.from_epoch(hx["time_epoch"])), "temp": r1(hx.get("temp_f")),
                           "pop": max(hx.get("chance_of_rain") or 0, hx.get("chance_of_snow") or 0),
                           "wind": r0(hx.get("wind_mph")), "gust": r0(hx.get("gust_mph")),
                           "text": hx["condition"]["text"], "icon": text_icon(hx["condition"]["text"])})
    return {"daily": daily, "hourly": hourly}


def src_tomorrow(cfg, keys, clock):
    d = get(f"https://api.tomorrow.io/v4/weather/forecast?location={cfg['lat']},{cfg['lon']}"
            f"&units=imperial&apikey={urllib.parse.quote(keys['tomorrowio'])}")
    tl = d["timelines"]
    daily = []
    for x in tl.get("daily", []):
        v = x["values"]
        code = v.get("weatherCodeMax")
        daily.append({"date": clock.day(clock.from_iso(x["time"])), "hi": r1(v.get("temperatureMax")),
                      "lo": r1(v.get("temperatureMin")), "pop": r0(v.get("precipitationProbabilityMax")),
                      "wind": r0(v.get("windSpeedMax")), "text": TOMORROW_TEXT.get(code),
                      "icon": tomorrow_icon(code)})
    hourly = []
    for x in tl.get("hourly", []):
        v = x["values"]
        code = v.get("weatherCode")
        hourly.append({"t": clock.hour(clock.from_iso(x["time"])), "temp": r1(v.get("temperature")),
                       "pop": r0(v.get("precipitationProbability")), "wind": r0(v.get("windSpeed")),
                       "gust": r0(v.get("windGust")), "text": TOMORROW_TEXT.get(code),
                       "icon": tomorrow_icon(code)})
    return {"daily": daily, "hourly": hourly}


# name, function, key needed, minutes between refreshes, sign-up page
SOURCES = [
    ("NWS (official)", src_nws, None, 10, None),
    ("Open-Meteo", src_openmeteo, None, 15, None),
    ("models", src_openmeteo_models, None, 60, None),
    ("Air quality", src_air, None, 30, None),
    ("Tempest (live, local)", src_weather_display, None, 0, None),
    ("Your Tempest", src_tempest, "tempest_token", 5, "https://tempestwx.com/settings/tokens"),
    ("Weather.com", src_weathercom, "weathercom", 30, "https://www.wunderground.com/member/api-keys"),
    ("AccuWeather", src_accuweather, "accuweather", 60, "https://developer.accuweather.com/"),
    ("Pirate Weather", src_pirate, "pirateweather", 30, "https://pirate-weather.apiable.io/"),
    ("Visual Crossing", src_visualcrossing, "visualcrossing", 60, "https://www.visualcrossing.com/sign-up"),
    ("WeatherAPI", src_weatherapi, "weatherapi", 30, "https://www.weatherapi.com/signup.aspx"),
    ("Tomorrow.io", src_tomorrow, "tomorrowio", 30, "https://app.tomorrow.io/signup"),
]


# ---------------------------------------------------------------- combine

def moon(day):
    """Moon phase 0..1 (0 new, .5 full) and its name."""
    known_new = dt.datetime(2000, 1, 6, 18, 14, tzinfo=UTC)
    age = ((day - known_new).total_seconds() / 86400) % 29.530588
    p = age / 29.530588
    names = ["New moon", "Waxing crescent", "First quarter", "Waxing gibbous",
             "Full moon", "Waning gibbous", "Last quarter", "Waning crescent"]
    return {"phase": round(p, 3), "name": names[int((p * 8) + .5) % 8],
            "illum": round((1 - math.cos(2 * math.pi * p)) / 2 * 100)}


def consensus(forecasts, field_days):
    """forecasts: {source: [daily items]} -> list of day summaries with spread."""
    out = []
    for date in field_days:
        row = {"date": date, "by": {}}
        for name, days in forecasts.items():
            for d in days:
                if d["date"] == date:
                    row["by"][name] = {k: d.get(k) for k in ("hi", "lo", "pop", "precip", "text", "icon")}
        for k in ("hi", "lo", "pop"):
            vals = [v[k] for v in row["by"].values() if v.get(k) is not None]
            if vals:
                row[k] = round(statistics.mean(vals))
                row[k + "_min"], row[k + "_max"] = round(min(vals)), round(max(vals))
                row[k + "_n"] = len(vals)
        if "hi" in row:
            spread = max(row["hi_max"] - row["hi_min"], row.get("lo_max", 0) - row.get("lo_min", 0))
            row["agree"] = "good" if spread <= 5 else "fair" if spread <= 10 else "low"
        icons = [v["icon"] for v in row["by"].values() if v.get("icon")]
        row["icon"] = max(set(icons), key=icons.count) if icons else None
        out.append(row)
    return out


def fetch(force=False):
    cfg = load_json(HERE / "config.json")
    keys = load_json(HERE / "keys.json", {})
    if not cfg:
        sys.exit("config.json is missing")
    status = {}
    results = {}

    def run(entry, clock):
        name, fn, keyname, every, signup = entry
        cfile = CACHE / (fn.__name__ + ".json")
        old = load_json(cfile)
        if keyname and not keys.get(keyname):
            return name, None, {"state": "no key", "signup": signup, "keyed": True}
        # "Refresh now" still waits 2 minutes between downloads, to protect free-key limits
        wait = min(every * 60, 120) if force else every * 60
        if old and time.time() - old["fetched"] < wait:
            return name, old["data"], {"state": "ok", "fetched": old["fetched"], "keyed": bool(keyname)}
        args = (cfg, keys) if fn in (src_openmeteo, src_openmeteo_models, src_air) else (cfg, keys, clock)
        try:
            data = fn(*args)
            save_json(cfile, {"fetched": time.time(), "data": data})
            return name, data, {"state": "ok", "fetched": time.time(), "keyed": bool(keyname)}
        except Exception as e:
            st = {"state": "error", "error": short_error(e), "keyed": bool(keyname)}
            if old:   # keep showing the last good data
                st.update(state="stale", fetched=old["fetched"])
                return name, old["data"], st
            return name, None, st

    # Open-Meteo first: it gives the place's UTC offset the others need.
    name, om, st = run(SOURCES[1], None)
    results[name], status[name] = om, st
    clock = Clock((om or {}).get("extra", {}).get("utc_offset", -7 * 3600))
    with ThreadPoolExecutor(8) as ex:
        for name, data, st in ex.map(lambda e: run(e, clock), [s for i, s in enumerate(SOURCES) if i != 1]):
            results[name], status[name] = data, st

    # Flatten: every forecast source by display name (models become 5 sources).
    fc_daily, fc_hourly = {}, {}
    for name, data in results.items():
        if not data or name == "Air quality":
            continue
        if name == "models":
            for mname, md in data.items():
                fc_daily[mname], fc_hourly[mname] = md["daily"], md["hourly"]
                status[mname] = {**status["models"], "via": "Open-Meteo"}
            continue
        if name == "Open-Meteo" or not data["daily"]:   # blend / live-only: not a separate forecast
            continue
        fc_daily[name] = data["daily"]
        if data["hourly"]:
            fc_hourly[name] = data["hourly"]
    if "models" in status and results.get("models"):
        status.pop("models")
    if "Tempest (live, local)" in status and not cfg.get("weather_display_dir"):
        status.pop("Tempest (live, local)")

    now_local = clock.from_epoch(time.time())
    today = clock.day(now_local)
    days = sorted({d["date"] for ds in fc_daily.values() for d in ds if d["date"] >= today})[:16]
    hour0 = clock.hour(now_local)

    om = results.get("Open-Meteo") or {}
    nws = results.get("NWS (official)") or {}
    tem = results.get("Your Tempest") or {}
    detail_h = [h for h in om.get("hourly", []) if h["t"] >= hour0][:168]
    hours = [h["t"] for h in detail_h]
    by_hour = {name: {h["t"]: h for h in hs} for name, hs in fc_hourly.items()}
    hourly = []
    for h in detail_h:
        row = dict(h)
        row["by"] = {name: m[h["t"]]["temp"] for name, m in by_hour.items() if h["t"] in m}
        nh = by_hour.get("NWS (official)", {}).get(h["t"])
        if nh:     # official forecast leads where it exists
            row.update(temp=nh["temp"], pop=nh["pop"] if nh["pop"] is not None else row["pop"],
                       text=nh["text"], icon=nh["icon"] or row["icon"])
        hourly.append(row)
    air = results.get("Air quality") or {}
    aqi_h = {a["t"]: a for a in air.get("hourly", [])}
    for row in hourly:
        if row["t"] in aqi_h:
            row["aqi"] = aqi_h[row["t"]]["aqi"]

    # "Now": your station first, then the nearest official station, then the model.
    wd = results.get("Tempest (live, local)") or {}
    if wd.get("now") and tem.get("now"):    # live local values + cloud-only extras (lightning)
        for k in ("strikes_3h", "strike_dist", "strike_time", "rain_yesterday"):
            wd["now"].setdefault(k, tem["now"].get(k))
    readings = [r for r in (wd.get("now"), tem.get("now") if not wd.get("now") else None, nws.get("now"), dict(om.get("now") or {}, station="Open-Meteo model"))
                if r and r.get("temp") is not None]
    sun = {d["date"]: d for d in om.get("daily", [])}
    out = {
        "updated": now_local.isoformat(timespec="minutes"),
        "location": {"name": cfg.get("name", "Home"), "near": nws.get("extra", {}).get("near"),
                     "lat": cfg["lat"], "lon": cfg["lon"],
                     "elevation_ft": r0((om.get("extra", {}).get("elevation_m") or 0) * 3.281)},
        "now": readings[0] if readings else None,
        "readings": readings,
        "alerts": nws.get("alerts", []),
        "hourly": hourly,
        "days": consensus(fc_daily, days),
        "detail_days": {d["date"]: d for d in om.get("daily", [])},
        "periods": nws.get("extra", {}).get("periods", []),
        "discussion": nws.get("extra", {}).get("discussion"),
        "air": air.get("now"),
        "sky": {"today": sun.get(today), "moon": moon(dt.datetime.now(UTC))},
        "sources": status,
    }
    save_json(DATA / "latest.json", out)
    return out


if __name__ == "__main__":
    o = fetch(force="--force" in sys.argv)
    for name, st in o["sources"].items():
        print(f"{name:22} {st['state']:8} {st.get('error', '')}")
    n = o["now"] or {}
    print(f"\nNow: {n.get('temp')}F {n.get('text')} from {n.get('station')}")
    print(f"{len(o['hourly'])} hours, {len(o['days'])} days, {len(o['alerts'])} alerts")
    for d in o["days"][:7]:
        print(d["date"], d.get("hi"), d.get("lo"), f"({d.get('hi_min')}-{d.get('hi_max')}, n={d.get('hi_n')})", d.get("agree"))
