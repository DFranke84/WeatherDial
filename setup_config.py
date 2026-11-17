"""WeatherDial setup: asks a few questions and writes config.json and keys.json.

    python setup_config.py        (Install.bat runs this for you)

Press Enter to keep the value shown in [brackets] or to skip an optional step.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG, KEYS = HERE / "config.json", HERE / "keys.json"
UA = "WeatherDial/0.1 (personal desktop weather widget)"


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def ask(prompt, default=""):
    shown = f" [{default}]" if default not in ("", None) else ""
    try:
        v = input(f"  {prompt}{shown}: ").strip()
    except EOFError:
        v = ""
    return v or (default if default is not None else "")


def yes(prompt, default=False):
    v = ask(prompt + (" (Y/n)" if default else " (y/N)"), "").lower()
    return default if not v else v.startswith("y")


def parse_latlon(text):
    """'39.7392, -104.9903', '39.7392N 104.9903W', a Google Maps link... -> (lat, lon)."""
    t = text.strip()
    m = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", t)            # Google Maps link
    if m:
        return float(m.group(1)), float(m.group(2))
    nums = re.findall(r"(-?\d+(?:\.\d+)?)\s*°?\s*([NSEWnsew])?", t)
    if len(nums) < 2:
        return None
    (a, ha), (b, hb) = nums[0], nums[1]
    lat, lon = float(a), float(b)
    if ha.upper() == "S":
        lat = -abs(lat)
    if hb.upper() == "W":
        lon = -abs(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def section(title):
    print(f"\n ---- {title} " + "-" * max(0, 58 - len(title)))


def main():
    cfg = load(CFG, {}) or {}
    keys = load(KEYS, {}) or {}
    keys.setdefault("_help", "Paste each key between the quotes. Leave blank to skip that source. "
                             "This file stays on this PC only.")
    print("\n ===== WeatherDial setup =====")
    print("  Press Enter to keep the value in [brackets], or to skip an optional step.")

    # -- location
    section("1. Your location")
    print("  In Google Maps, right-click your house and click the numbers at the top of the")
    print("  menu (that copies them), then paste here. Example: 39.7392, -104.9903")
    have = f"{cfg['lat']}, {cfg['lon']}" if cfg.get("lat") else ""
    while True:
        ll = parse_latlon(ask("Latitude, longitude", have))
        if ll:
            break
        print("  That doesn't look like a latitude, longitude. Try again.")
    cfg["lat"], cfg["lon"] = round(ll[0], 4), round(ll[1], 4)
    try:
        p = get(f"https://api.weather.gov/points/{cfg['lat']:.4f},{cfg['lon']:.4f}")["properties"]
        rel = p.get("relativeLocation", {}).get("properties", {})
        print(f"  OK: near {rel.get('city')}, {rel.get('state')} (National Weather Service office {p.get('cwa')}).")
    except Exception:
        print("  Note: the US National Weather Service doesn't cover this spot (outside the US?).")
        print("  The other sources still work; warnings and the forecaster's discussion won't.")
    cfg["name"] = ask("Name to show on the panel", cfg.get("name", "Home"))

    # -- Tempest
    section("2. Tempest weather station (optional)")
    print("  If you have a Tempest, WeatherDial shows its readings live. For the extras")
    print("  (lightning, history, its forecast) it needs your free personal token:")
    print("  tempestwx.com > Settings > Data Authorizations > Create Token.")
    tok = ask("Tempest token (Enter to skip)", keys.get("tempest_token", ""))
    keys["tempest_token"] = tok
    if tok:
        try:
            st = get("https://swd.weatherflow.com/swd/rest/stations?token=" + urllib.parse.quote(tok)).get("stations") or []
            if not st:
                print("  The token works but no station is linked to it.")
            elif len(st) == 1:
                print(f"  OK: found your station \"{st[0].get('name')}\".")
                cfg["tempest_station_id"] = st[0]["station_id"]
            else:
                for i, s in enumerate(st, 1):
                    print(f"   {i}. {s.get('name')}")
                pick = ask("Which station", "1")
                s = st[int(pick) - 1 if pick.isdigit() and 0 < int(pick) <= len(st) else 0]
                cfg["tempest_station_id"] = s["station_id"]
                print(f"  OK: using \"{s.get('name')}\".")
        except Exception as e:
            print(f"  Couldn't check the token ({e}). It's saved anyway; check it later in the Sources tab.")

    # -- how live readings arrive
    wd_guess = next((str(d) for d in (Path(os.environ.get("USERPROFILE", "")) / "DataFiles" / "wdisplay",
                                      Path("C:/wdisplay")) if (d / "sentwurapiddata.txt").exists()), "")
    if cfg.get("weather_display_dir") or wd_guess:
        section("3. Weather Display")
        print("  Weather Display was found on this PC. It already listens to the Tempest hub,")
        print("  so WeatherDial can read the live values from Weather Display's file instead.")
        if yes("Use Weather Display for the live readings?", True):
            cfg["weather_display_dir"] = ask("Weather Display folder", cfg.get("weather_display_dir") or wd_guess)
        else:
            cfg.pop("weather_display_dir", None)
    else:
        cfg.pop("weather_display_dir", None)
        if tok or yes("Do you have a Tempest hub on this home network?", False):
            cfg["tempest_udp"] = True
            print("\n  WeatherDial will listen to the Tempest hub on your home network for live readings.")
            print("  The first time it starts, Windows may ask whether Python may use the network:")
            print("  tick PRIVATE networks and click Allow.")
        else:
            cfg["tempest_udp"] = False   # no hub: don't listen (and no firewall question)

    # -- optional forecast keys
    section("4. Extra forecast sources (optional, all free)")
    print("  Each key adds one more forecast to the average. Enter skips. You can add them later:")
    print("  right-click the panel > Edit API keys...")
    for k, name, where in [("pirateweather", "Pirate Weather", "pirate-weather.apiable.io"),
                           ("weatherapi", "WeatherAPI.com", "weatherapi.com/signup.aspx"),
                           ("visualcrossing", "Visual Crossing", "visualcrossing.com/sign-up"),
                           ("tomorrowio", "Tomorrow.io", "app.tomorrow.io/signup"),
                           ("weathercom", "Weather.com (only if your station uploads to Weather Underground)",
                            "wunderground.com/member/api-keys"),
                           ("accuweather", "AccuWeather (free trial, then paid)", "developer.accuweather.com")]:
        keys[k] = ask(f"{name}  ({where})", keys.get(k, ""))

    sys.path.insert(0, str(HERE))
    import weather   # writes every setting (yours or the default) plus a _help section
    CFG.write_text(weather.config_text(cfg), encoding="utf-8")
    KEYS.write_text(json.dumps(keys, indent=2) + "\n", encoding="utf-8")
    print("\n  Saved config.json and keys.json (they stay on this PC).")
    print("  To change them later: run Install.bat again, or right-click the panel.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Setup cancelled; nothing changed.")
        sys.exit(1)
