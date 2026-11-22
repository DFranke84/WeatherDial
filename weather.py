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
        alerts.append({"id": a.get("id") or f.get("id"), "event": a["event"], "severity": a.get("severity"),
                       "headline": a.get("headline"), "description": a.get("description"),
                       "instruction": a.get("instruction"), "ends": a.get("ends") or a.get("expires"),
                       "source": "NWS"})

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
    st = get("https://swd.weatherflow.com/swd/rest/stations?token=" + urllib.parse.quote(tok))
    station = next((s for s in st["stations"] if s["station_id"] == cfg.get("tempest_station_id")),
                   st["stations"][0])
    sid = station["station_id"]
    device = next((dv["device_id"] for dv in station.get("devices", []) if dv.get("device_type") == "ST"), None)
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
    # Highest wind and gust today and yesterday, from the station's own history
    # (complete, whether or not WeatherDial was running).
    wind_max, temps = {}, {}
    if device:
        midnight = clock.from_epoch(time.time()).replace(hour=0, minute=0, second=0, microsecond=0)
        start = int(time.time() - (clock.from_epoch(time.time()) - midnight).total_seconds()) - 86400
        h = get(f"https://swd.weatherflow.com/swd/rest/observations/device/{device}"
                f"?time_start={start}&time_end={int(time.time())}&token={urllib.parse.quote(tok)}")
        wind_max = wind_from_obs(h.get("obs") or [], clock)
        temps = temps_from_obs(h.get("obs") or [], clock)
    # station pressure (mb) through the last day, for the barometer's history needle
    obs = (h.get("obs") or []) if device else []
    p_hist = [[ob[0], ob[6]] for ob in obs if len(ob) > 6 and ob[6]]
    # temperature (°F) and wind (avg, gust mph) through the last day, for the trend graphs
    t_hist = [[ob[0], round(ob[7] * 9 / 5 + 32, 1)] for ob in obs if len(ob) > 7 and ob[7] is not None]
    w_hist = [[ob[0], round((ob[2] or 0) * 2.23694, 1), round((ob[3] or 0) * 2.23694, 1)] for ob in obs if len(ob) > 3]
    return {"daily": daily, "hourly": hourly, "now": now,
            "extra": {"station_id": sid, "device_id": device, "wind_max": wind_max, "temps": temps,
                      "p_hist": p_hist, "t_hist": t_hist, "w_hist": w_hist}}


def wind_from_obs(obs, clock):
    """Tempest observations -> {date: {wind, gust, wind_at, gust_at}} in mph."""
    out = {}
    for ob in obs:
        t = clock.from_epoch(ob[0])
        day = out.setdefault(clock.day(t), {"wind": None, "gust": None})
        for k, i in (("wind", 2), ("gust", 3)):
            v = ob[i] if len(ob) > i else None
            if v is not None and (day[k] is None or v * 2.23694 > day[k]):
                day[k], day[k + "_at"] = round(v * 2.23694), t.strftime("%H:%M")
    return out


def temps_from_obs(obs, clock):
    """Tempest observations -> {date: {hi, lo, first, last, n}} in °F (actual daily high/low)."""
    out = {}
    for ob in obs:
        if len(ob) < 8 or ob[7] is None:
            continue
        t = clock.from_epoch(ob[0])
        f = ob[7] * 9 / 5 + 32
        hm = t.strftime("%H:%M")
        d = out.setdefault(clock.day(t), {"hi": f, "lo": f, "hi_at": hm, "lo_at": hm, "first": hm, "n": 0})
        if f > d["hi"]:
            d["hi"], d["hi_at"] = f, hm
        if f < d["lo"]:
            d["lo"], d["lo_at"] = f, hm
        d["last"], d["n"] = hm, d["n"] + 1
    for d in out.values():
        d["hi"], d["lo"] = round(d["hi"], 1), round(d["lo"], 1)
    return out


# ---------------------------------------------------------------- accuracy (forecast vs actual)
# forecasts.json: {date: {source: {"1": {hi, lo}, "2": ...}}}  - the last forecast each source made
#                 1, 2, ... days before that date
# actuals.json:   {date: {hi, lo, how}}                        - what your station measured

VDIR = DATA / "verify"
_saved_text = {}


def save_if_changed(path, obj):
    text = json.dumps(obj, separators=(",", ":"), sort_keys=True)
    if _saved_text.get(path) != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        _saved_text[path] = text


def record_forecasts(fc_daily, today):
    path = VDIR / "forecasts.json"
    arc = load_json(path, {})
    t0 = dt.date.fromisoformat(today)
    for src, days in fc_daily.items():
        for d in days:
            lead = (dt.date.fromisoformat(d["date"]) - t0).days
            if 1 <= lead <= 5 and (d.get("hi") is not None or d.get("lo") is not None):
                arc.setdefault(d["date"], {}).setdefault(src, {})[str(lead)] = {"hi": d.get("hi"), "lo": d.get("lo")}
    cutoff = (t0 - dt.timedelta(days=400)).isoformat()
    save_if_changed(path, {k: v for k, v in arc.items() if k >= cutoff})


RANK = {"Tempest cloud": 3, "WeatherDial log": 1}   # better source of actuals wins


def record_actuals(new):
    """new: {date: {hi, lo, how}} for complete past days."""
    if not new:
        return
    path = VDIR / "actuals.json"
    act = load_json(path, {})
    for date, a in new.items():
        old = act.get(date)
        if not old or RANK.get(a["how"], 0) >= RANK.get(old.get("how"), 0):
            act[date] = a
    save_if_changed(path, act)


# ---- hourly forecast vs actual (the dotted line on the 24 h temperature graph)
HOURLY_FC = VDIR / "hourly_forecast.json"   # {"YYYY-MM-DDTHH:00": temp the panel forecast ~1 day ahead}


def record_hourly_forecast(hourly, now_local):
    """Remember, for each coming hour, the panel's forecast as it stood about a day ahead
    (the last value seen while that hour was 20 to 28 hours away)."""
    arc = load_json(HOURLY_FC, {})
    for row in hourly:
        lead = (dt.datetime.fromisoformat(row["t"]) - now_local).total_seconds() / 3600
        if 20 <= lead <= 28 and row.get("temp") is not None:
            arc[row["t"]] = row["temp"]
    cutoff = (now_local - dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:00")
    save_if_changed(HOURLY_FC, {k: v for k, v in arc.items() if k >= cutoff})


def src_past_forecast(cfg, keys, clock):
    """Open-Meteo's archive of past runs: what was forecast a day earlier for each recent hour.
    Fills the dotted line until WeatherDial has saved a day of its own forecasts."""
    d = get(f"https://previous-runs-api.open-meteo.com/v1/forecast?latitude={cfg['lat']}&longitude={cfg['lon']}"
            "&hourly=temperature_2m_previous_day1&past_days=2&forecast_days=1"
            "&temperature_unit=fahrenheit&timezone=auto")["hourly"]
    return {"daily": [], "hourly": [],
            "past_fc": [[t[:13] + ":00", v] for t, v in zip(d["time"], d["temperature_2m_previous_day1"]) if v is not None]}


def past_hourly_forecast(past_src, clock):
    """[[epoch, temp, made_by]] for the last 26 hours: the panel's own day-ahead forecast where
    saved, else Open-Meteo's."""
    own = load_json(HOURLY_FC, {})
    om = dict(past_src.get("past_fc") or [])
    now = time.time()
    out = []
    for key in sorted(set(own) | set(om)):
        e = (dt.datetime.fromisoformat(key).replace(tzinfo=UTC) - clock.off).timestamp()
        if 0 <= now - e <= 26 * 3600:
            out.append([int(e), own[key], "panel"] if key in own else [int(e), om[key], "Open-Meteo"])
    return out


def src_backfill(cfg, keys, clock):
    """Once in a while: fill the accuracy history for the past `accuracy_backfill_days`
    (default 30): the global models' next-day forecasts from Open-Meteo's archive of past
    runs, and your Tempest's actual daily highs/lows (one call per missing day, max 31 a run)."""
    days = int(cfg.get("accuracy_backfill_days", 30))
    today = clock.from_epoch(time.time()).date()
    dates = [(today - dt.timedelta(days=i)).isoformat() for i in range(1, days + 1)]
    done = {"models": 0, "actuals": 0}

    path = VDIR / "forecasts.json"
    arc = load_json(path, {})
    q = (f"latitude={cfg['lat']}&longitude={cfg['lon']}&hourly=temperature_2m_previous_day1"
         f"&models={','.join(OM_MODELS)}&past_days={min(days, 92)}&forecast_days=1"
         "&temperature_unit=fahrenheit&timezone=auto")
    h = get("https://previous-runs-api.open-meteo.com/v1/forecast?" + q)["hourly"]
    for m, name in OM_MODELS.items():
        vals = h.get(f"temperature_2m_previous_day1_{m}") or []
        per = {}
        for t, v in zip(h["time"], vals):
            if v is not None:
                per.setdefault(t[:10], []).append(v)
        for date, vs in per.items():
            if date in dates and len(vs) >= 20 and "1" not in arc.get(date, {}).get(name, {}):
                arc.setdefault(date, {}).setdefault(name, {})["1"] = {"hi": round(max(vs), 1), "lo": round(min(vs), 1)}
                done["models"] += 1
    save_if_changed(path, arc)

    tok = keys.get("tempest_token")
    if tok:
        have = load_json(VDIR / "actuals.json", {})
        missing = [d for d in dates if (have.get(d) or {}).get("how") != "Tempest cloud"][:31]
        if missing:
            st = get("https://swd.weatherflow.com/swd/rest/stations?token=" + urllib.parse.quote(tok))
            station = next((s for s in st["stations"] if s["station_id"] == cfg.get("tempest_station_id")),
                           st["stations"][0])
            device = next((dv["device_id"] for dv in station.get("devices", []) if dv.get("device_type") == "ST"), None)
            new = {}
            for date in missing if device else []:
                start = int((dt.datetime.fromisoformat(date).replace(tzinfo=UTC) - clock.off).timestamp())
                o = get(f"https://swd.weatherflow.com/swd/rest/observations/device/{device}"
                        f"?time_start={start}&time_end={start + 86399}&token={urllib.parse.quote(tok)}")
                t = temps_from_obs(o.get("obs") or [], clock).get(date)
                if t and t["n"] >= 1000:   # most of the day's minutes present
                    new[date] = {"hi": t["hi"], "lo": t["lo"], "how": "Tempest cloud"}
            record_actuals(new)
            done["actuals"] = len(new)
    return {"daily": [], "hourly": [], "backfill": done}


def verify_table(today, keep=365):
    """Day-by-day list for the panel: actual high/low + each source's next-day forecast."""
    arc = load_json(VDIR / "forecasts.json", {})
    act = load_json(VDIR / "actuals.json", {})
    rows = []
    for date in sorted(act, reverse=True)[:keep]:
        if date >= today:
            continue
        fc = {src: v["1"] for src, v in (arc.get(date) or {}).items() if "1" in v}
        rows.append({"date": date, "actual": act[date], "fc": fc})
    return rows


WD_WEATHER = {"RA": "Rain", "-RA": "Light rain", "+RA": "Heavy rain", "SN": "Snow", "-SN": "Light snow",
              "+SN": "Heavy snow", "DZ": "Drizzle", "FG": "Fog", "TS": "Thunderstorm", "SH": "Showers"}


UDP_FILE = DATA / "tempest_udp.json"     # written by app.py's listener for the Tempest hub


def read_weather_display(cfg):
    """Weather Display's Weather Underground upload file (rewritten every few seconds)."""
    path = Path(cfg["weather_display_dir"]) / "sentwurapiddata.txt"
    try:
        line = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except OSError:
        return None, f"can't read {path}"
    q = {k: v[0] for k, v in urllib.parse.parse_qs(line.split("?", 1)[1]).items()}
    when = dt.datetime.strptime(q["dateutc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp()
    f = lambda k: float(q[k]) if q.get(k) not in (None, "") else None
    wx = WD_WEATHER.get(q.get("weather") or "")
    return {"when": when, "temp": f("tempf"), "rh": f("humidity"), "dew": f("dewptf"),
            "wind": f("windspeedmph"), "gust": f("windgustmph"), "dir": f("winddir"),
            "pressure": f("baromin"), "uv": f("UV"), "solar": f("solarradiation"),
            "rain_rate": f("rainin"), "rain_today": f("dailyrainin"), "text": wx}, None


def read_tempest_udp(cfg):
    """The Tempest hub's own local broadcast, as saved by app.py's listener: an observation
    every minute, wind every 3 seconds, and lightning strikes."""
    u = load_json(UDP_FILE)
    if not u:
        return None, "no broadcast heard from a Tempest hub yet"
    if u.get("error"):
        return None, u["error"]
    ob = u.get("obs")
    if not ob:
        return None, "hub heard, waiting for the first full reading (up to 1 minute)"
    c = lambda i: ob[i] if len(ob) > i else None
    t_c, rh, p_mb = c(7), c(8), c(6)
    temp = t_c * 9 / 5 + 32 if t_c is not None else None
    dew = None
    if t_c is not None and rh:                       # Magnus formula
        g = math.log(rh / 100) + 17.62 * t_c / (243.12 + t_c)
        dew = (243.12 * g / (17.62 - g)) * 9 / 5 + 32
    pressure = None
    if p_mb and t_c is not None:                     # station pressure -> sea level
        elev = (load_json(CACHE / "src_openmeteo.json") or {}).get("data", {}).get("extra", {}).get("elevation_m") or 0
        slp = p_mb * (1 - 0.0065 * elev / (t_c + 0.0065 * elev + 273.15)) ** -5.257
        pressure = round(slp * 0.0295300, 3)
    mps = 2.23694
    wind, when = (c(2) or 0) * mps, c(0)
    rw = u.get("wind")                               # rapid wind: fresher direction/speed
    direction = rw[2] if rw and time.time() - rw[0] < 60 else c(4)
    rain_mm = c(12) or 0
    ptype = c(13) or 0
    text = ("Hail" if ptype == 2 else "Rain") if rain_mm > 0 else None
    out = {"when": max(when or 0, rw[0] if rw else 0), "temp": temp, "rh": rh, "dew": dew, "wind": wind,
           "gust": (c(3) or 0) * mps, "dir": direction, "pressure": pressure, "uv": c(10), "solar": c(11),
           "rain_rate": rain_mm / 25.4 * 60, "rain_today": (u.get("rain_day") or {}).get("mm", 0) / 25.4,
           "text": text, "battery": c(16)}
    strikes = [s for s in u.get("strikes") or [] if time.time() - s[0] < 3 * 3600]
    if u.get("strikes"):
        last = u["strikes"][-1]
        out.update(strikes_3h=len(strikes), strike_dist=round(last[1] * 0.621), strike_time=last[0])
    return out, None


def feels_like(temp, rh, wind):
    """Wind chill when cold and windy, heat index when hot, else the temperature."""
    if temp is None:
        return None
    if temp <= 50 and (wind or 0) > 3:
        return 35.74 + .6215 * temp - 35.75 * wind ** .16 + .4275 * temp * wind ** .16
    if temp >= 80 and rh:
        return (-42.379 + 2.04901523 * temp + 10.14333127 * rh - .22475541 * temp * rh - .00683783 * temp * temp
                - .05481717 * rh * rh + .00122874 * temp * temp * rh + .00085282 * temp * rh * rh
                - .00000199 * temp * temp * rh * rh)
    return temp


def src_local_station(cfg, keys, clock):
    """Your station, live and local: Weather Display's file if weather_display_dir is set,
    otherwise the Tempest hub's broadcast on your home network."""
    if cfg.get("weather_display_dir"):
        r, why = read_weather_display(cfg)
        via = "Weather Display"
    elif cfg.get("tempest_udp", True):
        r, why = read_tempest_udp(cfg)
        via = "Tempest hub"
    else:
        return {"daily": [], "hourly": [], "now": None, "note": None}
    if r:
        age = time.time() - r["when"]
        if age > 600:
            r, why = None, f"{via}: last reading {round(age / 60)} min ago"
    if not r:
        return {"daily": [], "hourly": [], "now": None, "note": why}
    # Pressure log (last 26 hours, a point every 5 minutes) for the trend and the gauges.
    plog = load_json(DATA / "pressure_log.json", [])
    now_t = time.time()
    if r["pressure"] is not None:
        plog = [p for p in plog if now_t - p[0] < 26 * 3600]
        if len(plog) >= 2 and now_t - plog[-2][0] < 300:
            plog[-1] = [now_t, r["pressure"]]          # keep the newest value, a point per 5 min
        else:
            plog.append([now_t, r["pressure"]])
        save_json(DATA / "pressure_log.json", plog)
    trend = None
    old = [p for p in plog if now_t - p[0] >= 2.5 * 3600]
    if old:
        diff = plog[-1][1] - old[-1][1]               # change over about the last 3 hours
        trend = "rising" if diff > .03 else "falling" if diff < -.03 else "steady"
    now = {"station": "Your Tempest (live)", "via": via, "time": clock.from_epoch(r["when"]).isoformat(),
           "temp": r1(r["temp"]), "feels": r1(feels_like(r["temp"], r["rh"], r["wind"])), "rh": r0(r["rh"]),
           "dew": r1(r["dew"]), "wind": r0(r["wind"]), "gust": r0(r["gust"]), "dir": r["dir"],
           "pressure": r["pressure"], "trend": trend, "uv": r["uv"], "solar": r["solar"],
           "rain_rate": r["rain_rate"], "rain_today": r["rain_today"],
           "text": r["text"], "icon": text_icon(r["text"]) if r["text"] else None, "age_s": round(age)}
    for k in ("strikes_3h", "strike_dist", "strike_time", "battery"):
        if r.get(k) is not None:
            now[k] = r[k]
    return {"now": now, "daily": [], "hourly": []}


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


ACCU_MEMO = CACHE / "accuweather_setup.json"


def accu_session(cfg, keys):
    """Shared AccuWeather setup: how the key is sent and the location key, found once and
    remembered in data/cache. Returns (call, memo)."""
    key = keys["accuweather"]
    base = "https://dataservice.accuweather.com"
    memo = load_json(ACCU_MEMO, {})
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
            raise urllib.error.HTTPError("", 401, "key refused", None, None)
        save_json(ACCU_MEMO, memo)
    return call, memo


def src_accuweather(cfg, keys, clock):
    """AccuWeather daily + hourly, using the longest forecasts the plan allows (checked once
    a day, so an upgrade or downgrade is picked up by itself)."""
    call, memo = accu_session(cfg, keys)
    memo_file = ACCU_MEMO

    def first_allowed(kind, options):
        # Re-check now and then (once a day) in case the plan changed, e.g. after subscribing.
        if memo.get(kind) in options and time.time() - memo.get(kind + "_checked", 0) < 86400:
            return memo[kind], call(memo[kind])
        memo[kind + "_checked"] = time.time()
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
        _, hh = first_allowed("hourly", [f"/forecasts/v1/hourly/{n}hour/{lk}{q}" for n in (240, 120, 72, 12)])
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


def src_minutecast(cfg, keys, clock):
    """AccuWeather MinuteCast: rain/snow minute by minute for the next 2 hours.
    Included in the AccuWeather trial; a paid add-on afterwards."""
    call, _ = accu_session(cfg, keys)
    d = call(f"/forecasts/v1/minute?q={cfg['lat']},{cfg['lon']}")
    segs = [{"start": s["StartMinute"], "end": s["EndMinute"], "type": (s.get("Type") or "").lower() or None}
            for s in d.get("Summaries") or []]
    return {"daily": [], "hourly": [],
            "minutecast": {"phrase": (d.get("Summary") or {}).get("Phrase"),
                           "type": ((d.get("Summary") or {}).get("Type") or "").lower() or None,
                           "segments": segs, "at": clock.from_epoch(time.time()).isoformat(timespec="minutes")}}


# AccuWeather health & lifestyle indices shown on the Health tab, by ID and group. Change with
# "index_groups": {"Group name": [ids...]} in config.json. Full list: python weather.py --indices
INDEX_GROUPS = {
    "Health": [21, 30, 27, 23, 44, 25, 26, 48, -14, -11, -13, -12, 18],
    "Outdoors & home": [3, 20, 13, 15, 19, 40, 28, 38, 32, 34, 17, 46, 50, 12, 36, 43],
}


# ---------------------------------------------------------------- settings (config.json)
# Every setting, its default and what it does. config.example.json is written from this, and
# any setting missing from your config.json is added with its default when WeatherDial starts
# (your own values are never changed).
CONFIG_SETTINGS = [
    ("name", "Home", "The name shown at the top left of the panel."),
    ("lat", 0.0, "Your latitude in decimal degrees (north is positive)."),
    ("lon", 0.0, "Your longitude in decimal degrees (west is negative, e.g. -105.0)."),
    ("refresh_minutes", 10, "How often the no-key sources (NWS, Open-Meteo) refresh, in minutes (at least 2)."),
    ("tempest_udp", True, "true = listen to a Tempest hub on the home network for live readings; false = don't. "
                          "Restart WeatherDial after changing."),
    ("tempest_udp_port", 50222, "The network port the Tempest hub broadcasts on (normally 50222)."),
    ("tempest_station_id", None, "Which Tempest station to use (setup fills this in from your token). "
                                 "null = the first station on the token."),
    ("weather_display_dir", "", "Only if Weather Display runs your station: its folder, e.g. "
                                "\"C:\\wdisplay\" (write each \\ twice in this file). Then live readings come "
                                "from its file instead of the hub. Leave empty otherwise."),
    ("source_minutes", {}, "Refresh a particular source more or less often, in minutes, e.g. "
                           "{\"AccuWeather\": 10}. Use the names from the Sources tab; at least 2. "
                           "Check that source's daily limit first."),
    ("accuracy_backfill_days", 30, "How many past days to fill in for the accuracy scores (global models' "
                                   "past forecasts and your Tempest's actual highs and lows)."),
    ("alert_notify", "moderate", "Which National Weather Service alerts pop up a Windows notification: "
                                 "\"severe\" (only Severe and Extreme, e.g. most warnings), \"moderate\" (also most "
                                 "watches and advisories), \"all\" (also minor statements), or \"off\". "
                                 "Every alert still shows on the panel and the tray icon either way."),
    ("index_groups", INDEX_GROUPS, "Which AccuWeather health & lifestyle indices the Health tab shows, by "
                                   "number, in groups. List all numbers: python weather.py --indices"),
]


def config_text(cfg):
    """config.json as text: every setting (yours or the default), then a _help section."""
    out = {k: cfg.get(k, default) for k, default, _ in CONFIG_SETTINGS}
    out.update({k: v for k, v in cfg.items() if k not in out and not k.startswith("_")})   # keep extras
    out["_help"] = {k: text for k, _, text in CONFIG_SETTINGS}
    text = json.dumps(out, indent=2, ensure_ascii=False)
    # keep lists of numbers on one line ([21, 30, 27, ...]) so the file stays readable
    import re
    return re.sub(r"\[\s*([-\d,\s]+?)\s*\]", lambda m: "[" + ", ".join(m.group(1).split()).replace(",,", ",") + "]",
                  text) + "\n"


KEY_NAMES = ["tempest_token", "pirateweather", "weatherapi", "visualcrossing", "tomorrowio",
             "weathercom", "accuweather"]


def complete_keys(path):
    """Add any missing key names (empty) to keys.json, keeping the keys already there."""
    keys = load_json(path)
    if not isinstance(keys, dict):
        return []
    added = [k for k in KEY_NAMES if k not in keys]
    if added:
        keys.setdefault("_help", "Paste each key between the quotes. Leave blank to skip that source. "
                                 "This file stays on this PC only. Where to get each key: README, Sources.")
        keys.update({k: "" for k in added})
        Path(path).write_text(json.dumps(keys, indent=2) + "\n", encoding="utf-8")
    return added


def complete_config(path):
    """Add any missing settings (with defaults) and the _help text to an existing config.json,
    keeping the user's values. Returns the names that were added."""
    cfg = load_json(path)
    if not isinstance(cfg, dict):
        return []
    added = [k for k, _, _ in CONFIG_SETTINGS if k not in cfg]
    new = config_text(cfg)
    try:
        if Path(path).read_text(encoding="utf-8") != new:
            Path(path).write_text(new, encoding="utf-8")
    except OSError:
        return []
    return added


def src_indices(cfg, keys, clock):
    """AccuWeather health & lifestyle indices, 5 days, one call."""
    call, memo = accu_session(cfg, keys)
    data = call(f"/indices/v1/daily/5day/{memo['location']}")
    by_id = {}
    for x in data:
        by_id.setdefault(x["ID"], {"id": x["ID"],
                                   "name": x["Name"].replace(" Forecast", "").replace(", brought to you by Country Time", ""),
                                   "ascending": x.get("Ascending"), "days": []})
        by_id[x["ID"]]["days"].append({"date": x["LocalDateTime"][:10], "category": x.get("Category"),
                                       "level": x.get("CategoryValue"), "value": x.get("Value")})
    return {"daily": [], "hourly": [], "indices": by_id}

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
    ("Accuracy history", src_backfill, None, 360, None),
    ("Past forecasts", src_past_forecast, None, 60, None),
    ("Tempest (live, local)", src_local_station, None, 0, None),
    ("Your Tempest", src_tempest, "tempest_token", 5, "https://tempestwx.com/settings/tokens"),
    ("Weather.com", src_weathercom, "weathercom", 30, "https://www.wunderground.com/member/api-keys"),
    ("AccuWeather", src_accuweather, "accuweather", 60, "https://developer.accuweather.com/"),
    ("AccuWeather MinuteCast", src_minutecast, "accuweather", 30, "https://developer.accuweather.com/"),
    ("AccuWeather indices", src_indices, "accuweather", 360, "https://developer.accuweather.com/"),
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


def gauge_data(now, tem_extra, today):
    """Data for the Gauges tab: sea-level pressure over the last day (inHg), the pressure
    3 and 24 hours ago, and today's low/high so far."""
    t = time.time()
    hist = []
    p_now = now.get("pressure")
    tp = tem_extra.get("p_hist") or []
    if tp and p_now:
        # Tempest history is station pressure; shift it to sea level using the live reading.
        offset = float(p_now) - tp[-1][1] * 0.0295300
        hist = [[e, round(mb * 0.0295300 + offset, 3)] for e, mb in tp if t - e <= 26 * 3600]
    own = [p for p in load_json(DATA / "pressure_log.json", []) if t - p[0] <= 26 * 3600]
    if hist:
        hist += [p for p in own if p[0] > hist[-1][0]]
    else:
        hist = own

    def at(ago):
        older = [p for p in hist if t - p[0] >= ago]
        return older[-1][1] if older and t - older[-1][0] <= ago + 1800 else None

    def sample(rows):                 # a point every 15 minutes is plenty for a graph
        out, last = [], 0
        for r in rows:
            if r[0] - last >= 900:
                out.append([int(r[0])] + list(r[1:])); last = r[0]
        return out
    sampled = sample(hist)

    # Temperature and wind for the trend graphs: Tempest history, else our own 5-minute log
    # of whatever station supplies "now" (kept 26 hours).
    hpath = DATA / "hist_log.json"
    hl = [r for r in load_json(hpath, []) if t - r[0] <= 26 * 3600]
    if now.get("temp") is not None and (not hl or t - hl[-1][0] >= 300):
        hl.append([t, now.get("temp"), now.get("wind"), now.get("gust")])
        save_json(hpath, hl)
    th = [r for r in tem_extra.get("t_hist") or [] if t - r[0] <= 26 * 3600]
    wh = [r for r in tem_extra.get("w_hist") or [] if t - r[0] <= 26 * 3600]
    if th:
        th += [[r[0], r[1]] for r in hl if r[0] > th[-1][0]]
    else:
        th = [[r[0], r[1]] for r in hl]
    if wh:
        wh += [[r[0], r[2], r[3]] for r in hl if r[0] > wh[-1][0] and r[2] is not None]
    else:
        wh = [[r[0], r[2], r[3]] for r in hl if r[2] is not None]
    # wind: keep the strongest gust inside each 15-minute step, not just the first reading
    w_s, bucket = [], None
    for e, avg, gust in wh:
        if bucket and e - bucket[0] < 900:
            bucket[1] = max(bucket[1], avg or 0); bucket[2] = max(bucket[2], gust or 0)
        else:
            if bucket: w_s.append(bucket)
            bucket = [int(e), avg or 0, gust or 0]
    if bucket: w_s.append(bucket)

    # Today's actual high and low so far (with times): the station's history since midnight,
    # else WeatherDial's own readings since it started; the live reading can beat either.
    day = None
    hist_day = (tem_extra.get("temps") or {}).get(today)
    if hist_day:
        day = {"hi": hist_day["hi"], "lo": hist_day["lo"], "hi_at": hist_day.get("hi_at"),
               "lo_at": hist_day.get("lo_at"), "since": "00:00"}
    else:
        own_day = load_json(DATA / "wind_max.json", {}).get("today") or {}
        if own_day.get("date") == today and own_day.get("t_hi") is not None:
            day = {"hi": own_day["t_hi"], "lo": own_day["t_lo"], "hi_at": own_day.get("t_hi_at"),
                   "lo_at": own_day.get("t_lo_at"), "since": own_day.get("since")}
    temp_now = now.get("temp")
    if day and temp_now is not None:
        hm = (now.get("time") or "")[11:16] or None
        if temp_now > day["hi"]:
            day["hi"], day["hi_at"] = temp_now, hm
        if temp_now < day["lo"]:
            day["lo"], day["lo_at"] = temp_now, hm

    # Change over the last hour: temperature (°F/hr) and pressure (in/hr).
    def t_at(ago):
        older = [r for r in th if t - r[0] >= ago]
        return older[-1][1] if older and t - older[-1][0] <= ago + 1200 else None
    t1, p1 = t_at(3600), at(3600)
    t_rate = round(temp_now - t1, 1) if temp_now is not None and t1 is not None else None
    p_rate = round(float(p_now) - p1, 3) if p_now and p1 is not None else None

    p3 = at(3 * 3600)
    if p_now and p3 is not None:   # trend from the full history (also right after a restart)
        diff = float(p_now) - p3
        now["trend"] = "rising" if diff > .03 else "falling" if diff < -.03 else "steady"
    # temperature every 15 minutes, but always keep the true highest and lowest readings
    t_s = sample(th)
    last24 = [r for r in th if t - r[0] <= 24 * 3600]
    if last24:
        for r in (max(last24, key=lambda r: r[1]), min(last24, key=lambda r: r[1])):
            if [int(r[0]), r[1]] not in t_s:
                t_s.append([int(r[0]), r[1]])
        t_s.sort()
    return {"p_hist": sampled, "p_3h": p3, "p_24h": at(24 * 3600),
            "t_hist": t_s, "w_hist": w_s, "t_rate_1h": t_rate, "p_rate_1h": p_rate,
            "t_today": day}


def track_wind(now, today, now_local, history=None):
    """Keep today's (and yesterday's) highest wind and gust seen in the 'now' readings,
    and add them to `now` as wind_max (for the Wind tooltip). history = full-day maxima
    from the Tempest cloud, when available: used in preference (it covers the whole day)."""
    path = DATA / "wind_max.json"
    log = load_json(path, {})
    day = log.get("today") or {}
    if day.get("date") != today:
        if day.get("date"):
            log["yesterday"] = day
            # A whole day watched with no gap over an hour also counts as an "actual"
            # for the accuracy scores (used when there's no Tempest cloud history).
            if (day.get("t_hi") is not None and day.get("since", "99") <= "00:30"
                    and day.get("last", "") >= "23:30" and day.get("gap", 999) <= 60):
                record_actuals({day["date"]: {"hi": day["t_hi"], "lo": day["t_lo"], "how": "WeatherDial log"}})
        day = {"date": today, "since": now_local.strftime("%H:%M"), "wind": None, "gust": None}
    hhmm = now_local.strftime("%H:%M")
    for k in ("wind", "gust"):
        v = now.get(k)
        if v is not None and (day.get(k) is None or v > day[k]):
            day[k], day[k + "_at"] = v, hhmm
    temp = now.get("temp")
    if temp is not None:
        mins = now_local.hour * 60 + now_local.minute
        if day.get("last"):
            h, m = map(int, day["last"].split(":"))
            day["gap"] = max(day.get("gap", 0), mins - (h * 60 + m))
        day["last"] = hhmm
        if day.get("t_hi") is None or temp > day["t_hi"]:
            day["t_hi"], day["t_hi_at"] = temp, hhmm
        if day.get("t_lo") is None or temp < day["t_lo"]:
            day["t_lo"], day["t_lo_at"] = temp, hhmm
    log["today"] = day
    save_json(path, log)
    y = log.get("yesterday")
    y = y if y and y.get("date") < today else None
    history = history or {}

    def best(own, date):
        h = history.get(date)
        if not h or h.get("gust") is None:
            return own
        out = {"date": date, "since": "00:00", **h}
        for k in ("wind", "gust"):   # a live 3-second reading can beat the 1-minute history
            if own and own.get(k) is not None and own[k] > (out.get(k) or 0):
                out[k], out[k + "_at"] = own[k], own.get(k + "_at")
        return out
    ydate = (now_local - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    now["wind_max"] = {"today": best(day, today), "yesterday": best(y, ydate)}


def fetch(force=False):
    cfg = load_json(HERE / "config.json")
    keys = load_json(HERE / "keys.json", {})
    if not cfg:
        sys.exit("config.json is missing")
    status = {}
    results = {}

    def run(entry, clock):
        name, fn, keyname, every, signup = entry
        if fn in (src_nws, src_openmeteo):   # no-key sources follow refresh_minutes (min 2)
            every = max(2, cfg.get("refresh_minutes", every))
        override = (cfg.get("source_minutes") or {}).get(name)
        if override:                         # per-source setting, e.g. {"AccuWeather": 10}
            every = max(2, override)
        cfile = CACHE / (fn.__name__ + ".json")
        old = load_json(cfile)
        if keyname and not keys.get(keyname):
            return name, None, {"state": "no key", "signup": signup, "keyed": True}
        # "Refresh now" still waits 2 minutes between downloads, to protect free-key limits
        wait = min(every * 60, 120) if force else every * 60
        if old and time.time() - old["fetched"] < wait:
            return name, old["data"], {"state": "ok", "fetched": old["fetched"], "keyed": bool(keyname)}
        # After a failure, wait before trying again so a refused key or a dead service
        # isn't called every 30 seconds: 6 hours if the key was refused, else the normal pace.
        efile = CACHE / (fn.__name__ + ".error.json")
        err = load_json(efile)
        if err and err.get("key") == (keys.get(keyname) or "")[-6:]:
            pause = 6 * 3600 if err.get("refused") else max(every * 60, 300)
            if time.time() - err["time"] < pause:
                st = {"state": "stale" if old else "error", "error": err["error"], "keyed": bool(keyname),
                      "retry": err["time"] + pause}
                if old:
                    st["fetched"] = old["fetched"]
                return name, old["data"] if old else None, st
        args = (cfg, keys) if fn in (src_openmeteo, src_openmeteo_models, src_air) else (cfg, keys, clock)
        try:
            data = fn(*args)
            save_json(cfile, {"fetched": time.time(), "data": data})
            efile.unlink(missing_ok=True)
            return name, data, {"state": "ok", "fetched": time.time(), "keyed": bool(keyname)}
        except Exception as e:
            refused = isinstance(e, urllib.error.HTTPError) and e.code in (401, 403)
            msg = short_error(e)
            if refused and fn in (src_minutecast, src_indices):
                msg = f"not in your AccuWeather plan, or key refused (HTTP {e.code})"
            save_json(efile, {"time": time.time(), "error": msg, "refused": refused,
                              "key": (keys.get(keyname) or "")[-6:]})
            st = {"state": "error", "error": msg, "keyed": bool(keyname)}
            if old and not refused:   # keep showing the last good data
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
        if name == "Open-Meteo" or not data.get("daily"):   # blend / live-only / extras: not a forecast
            continue
        fc_daily[name] = data["daily"]
        if data["hourly"]:
            fc_hourly[name] = data["hourly"]
    if "models" in status and results.get("models"):
        status.pop("models")
    local = results.get("Tempest (live, local)") or {}
    if local.get("now"):
        status["Tempest (live, local)"]["via"] = local["now"]["via"]
    elif local.get("note"):   # set up but nothing heard: say why in the Sources tab
        status["Tempest (live, local)"] = {"state": "error", "keyed": False, "error": local["note"] + (
            ". If Windows asked about Python and the firewall, allow it on private networks; the PC must be on "
            "the same home network as the Tempest hub." if "heard" in local["note"] else "")}
    else:
        status.pop("Tempest (live, local)", None)

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
            if tem["now"].get(k) is not None:
                wd["now"][k] = tem["now"][k] if k == "rain_yesterday" else wd["now"].get(k, tem["now"][k])
        # The hub's own rain total only counts while WeatherDial runs; the cloud's covers the whole day.
        if wd["now"].get("via") == "Tempest hub" and tem["now"].get("rain_today") is not None:
            wd["now"]["rain_today"] = max(wd["now"]["rain_today"] or 0, tem["now"]["rain_today"])
    readings = [r for r in (wd.get("now"), tem.get("now") if not wd.get("now") else None, nws.get("now"), dict(om.get("now") or {}, station="Open-Meteo model"))
                if r and r.get("temp") is not None]
    if readings and not readings[0].get("text"):   # a station only says "rain"; borrow the sky from others
        other = next((r for r in readings[1:] if r.get("text")), None)
        if other:
            readings[0]["text"], readings[0]["icon"] = other["text"], other.get("icon")
    sun = {d["date"]: d for d in om.get("daily", [])}
    if readings:
        track_wind(readings[0], today, now_local, tem.get("extra", {}).get("wind_max"))

    record_hourly_forecast(hourly, now_local)
    gauges = gauge_data(readings[0] if readings else {}, tem.get("extra", {}), today)
    gauges["f_hist"] = past_hourly_forecast(results.get("Past forecasts") or {}, clock)

    # Accuracy: remember every source's forecasts; record finished days' actual high/low.
    record_forecasts(fc_daily, today)
    record_actuals({d: {"hi": t["hi"], "lo": t["lo"], "how": "Tempest cloud"}
                    for d, t in (tem.get("extra", {}).get("temps") or {}).items()
                    if d < today and t["first"] <= "00:30" and t["last"] >= "23:00"})

    # Optional AccuWeather extras: shown only when available (and MinuteCast only while fresh).
    mc = (results.get("AccuWeather MinuteCast") or {}).get("minutecast")
    mc_age = time.time() - (status.get("AccuWeather MinuteCast") or {}).get("fetched", 0)
    minutecast = mc if mc and mc_age < 45 * 60 else None
    idx = (results.get("AccuWeather indices") or {}).get("indices") or {}
    groups = cfg.get("index_groups") or INDEX_GROUPS
    indices = None
    if idx:
        indices = [{"group": g, "items": [idx[str(i)] if str(i) in idx else idx[i]
                                           for i in ids if str(i) in idx or i in idx]}
                   for g, ids in groups.items()]
        indices = [g for g in indices if g["items"]] or None

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
        "minutecast": minutecast,
        "indices": indices,
        "verify": verify_table(today),
        "gauges": gauges,
        "sources": status,
    }
    save_json(DATA / "latest.json", out)
    return out


if __name__ == "__main__":
    if "--indices" in sys.argv:   # list every AccuWeather index ID (from the last download)
        idx = (load_json(CACHE / "src_indices.json") or {}).get("data", {}).get("indices", {})
        for i, x in sorted(idx.items(), key=lambda kv: int(kv[0])):
            print(f"{i:>4}  {x['name']}  (today: {x['days'][0]['category']})")
        sys.exit()
    o = fetch(force="--force" in sys.argv)
    for name, st in o["sources"].items():
        print(f"{name:22} {st['state']:8} {st.get('error', '')}")
    n = o["now"] or {}
    print(f"\nNow: {n.get('temp')}F {n.get('text')} from {n.get('station')}")
    print(f"{len(o['hourly'])} hours, {len(o['days'])} days, {len(o['alerts'])} alerts")
    for d in o["days"][:7]:
        print(d["date"], d.get("hi"), d.get("lo"), f"({d.get('hi_min')}-{d.get('hi_max')}, n={d.get('hi_n')})", d.get("agree"))
