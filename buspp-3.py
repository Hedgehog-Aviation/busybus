import io
import csv
import time
import zipfile
import threading
import requests
from flask import Flask, jsonify, request as flask_request
from google.transit import gtfs_realtime_pb2

# ──────────────────────────────────────────────────────────────────
API_KEY      = "YOU NEED TO PUT YOUR ACTUAL KEY HERE. GET IT FROM THE OPENDATA TRANSPORT FOR NSW WEBSITE."
HEADERS      = {"Authorization": f"apikey {API_KEY}"}
REALTIME_URL = "https://api.transport.nsw.gov.au/v1/gtfs/realtime/buses"
VEHICLE_URL  = "https://api.transport.nsw.gov.au/v1/gtfs/vehiclepos/buses"
TP_URL       = "https://api.transport.nsw.gov.au/v1/tp/departure_mon"
SCHEDULE_URLS = [
    "https://api.transport.nsw.gov.au/v1/gtfs/schedule/buses/SBSC006",
    "https://api.transport.nsw.gov.au/v1/gtfs/schedule/buses/GSBC001",
    "https://api.transport.nsw.gov.au/v1/gtfs/schedule/buses/GSBC002",
    "https://api.transport.nsw.gov.au/v1/gtfs/schedule/buses/GSBC003",
    "https://api.transport.nsw.gov.au/v1/gtfs/schedule/buses/GSBC004",
    "https://api.transport.nsw.gov.au/v1/gtfs/schedule/buses/GSBC007",
    "https://api.transport.nsw.gov.au/v1/gtfs/schedule/buses/GSBC008",
    "https://api.transport.nsw.gov.au/v1/gtfs/schedule/buses/GSBC009",
    "https://api.transport.nsw.gov.au/v1/gtfs/schedule/buses/GSBC010",
    "https://api.transport.nsw.gov.au/v1/gtfs/schedule/buses/GSBC014",
    "https://api.transport.nsw.gov.au/v1/gtfs/schedule/buses/OSMBSC001",
]

# ─────────────────────────────────
_stop = {
    "id":   "200923",
    "name": "Bus Stop",
    "lat":  -33.867417,
    "lon":  151.192611,
}
_stop_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────
_schedule_cache    = {}
_schedule_cache_ts = 0
_schedule_lock     = threading.Lock()

def get_trip_headsigns():
    global _schedule_cache, _schedule_cache_ts
    with _schedule_lock:
        if time.time() - _schedule_cache_ts < 3600 and _schedule_cache:
            return _schedule_cache
        trips = {}
        for url in SCHEDULE_URLS:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=15)
                if resp.status_code == 200:
                    z = zipfile.ZipFile(io.BytesIO(resp.content))
                    with z.open("trips.txt") as f:
                        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
                            short    = row.get("route_id", "").split("_")[-1]
                            headsign = row.get("trip_headsign", "")
                            trips[row["trip_id"]] = (short, headsign)
                    print(f"Loaded {url.split('/')[-1]} ({len(trips)} trips total)")
                else:
                    print(f"Schedule {url.split('/')[-1]}: HTTP {resp.status_code}")
            except Exception as e:
                print(f"Schedule error ({url.split('/')[-1]}): {e}")
        _schedule_cache    = trips
        _schedule_cache_ts = time.time()
        return trips

# ─────────────────────────────────────────────────────
def lookup_stop(stop_id):
    """Returns {id, name, lat, lon} or raises."""
    now   = time.localtime()
    date  = time.strftime("%Y%m%d", now)
    hhmm  = time.strftime("%H%M", now)
    params = {
        "outputFormat":          "rapidJSON",
        "coordOutputFormat":     "EPSG:4326",
        "mode":                  "direct",
        "type_dm":               "stop",
        "name_dm":               stop_id,
        "itdDate":               date,
        "itdTime":               hhmm,
        "departureMonitorMacro": "true",
        "TfNSWDM":               "true",
        "version":               "10.2.1.42",
    }
    resp = requests.get(TP_URL, headers=HEADERS, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    locs = data.get("locations", [])
    if not locs:
        raise ValueError(f"No stop found for ID {stop_id!r}")
    loc  = locs[0]
    name = loc.get("disassembledName") or loc.get("name") or stop_id
    coord = loc.get("coord", [None, None])
    return {
        "id":   stop_id,
        "name": name,
        "lat":  coord[0],
        "lon":  coord[1],
    }

# ──────────────────────────────────────────────────────────
def fetch_data():
    with _stop_lock:
        stop_id = _stop["id"]

    headsigns = get_trip_headsigns()
    now       = int(time.time())
    occ_map   = {
        0: "Empty", 1: "Many seats", 2: "Few seats",
        3: "Standing only", 4: "Crushed", 5: "Full", 6: "Not accepting"
    }

    # where the bus at
    vehicle_pos = {}
    try:
        vresp = requests.get(VEHICLE_URL, headers=HEADERS, timeout=10)
        vfeed = gtfs_realtime_pb2.FeedMessage()
        vfeed.ParseFromString(vresp.content)
        for entity in vfeed.entity:
            if not entity.HasField("vehicle"):
                continue
            v   = entity.vehicle
            tid = v.trip.trip_id
            if not tid:
                continue
            # what kind of bus
            vmodel = ""
            try:
                raw = v.vehicle.SerializeToString()
                i2  = 0
                while i2 < len(raw):
                    b0 = raw[i2]; i2 += 1
                    field_num = b0 >> 3; wire_type = b0 & 0x7
                    if wire_type == 2:
                        length = 0; shift = 0
                        while True:
                            b1 = raw[i2]; i2 += 1
                            length |= (b1 & 0x7F) << shift; shift += 7
                            if not (b1 & 0x80): break
                        val = raw[i2:i2+length]; i2 += length
                        if field_num == 1527:
                            vmodel = val.decode("utf-8", errors="replace").split("~")[0]
                            break
                    elif wire_type == 0:
                        while raw[i2] & 0x80: i2 += 1
                        i2 += 1
                    else:
                        break
            except Exception:
                pass
            vehicle_pos[tid] = {
                "lat":        v.position.latitude,
                "lon":        v.position.longitude,
                "bearing":    v.position.bearing,
                "speed_kmh":  round(v.position.speed * 3.6) if v.position.speed else 0,
                "label":      v.vehicle.label,
                "plate":      v.vehicle.license_plate,
                "occupancy":  occ_map.get(v.occupancy_status, "Unknown"),
                "occ_status": v.occupancy_status,
                "model":      vmodel,
            }
    except Exception as e:
        print(f"Vehicle fetch error: {e}")

    # when the bus gonna be here
    buses = []
    try:
        rresp = requests.get(REALTIME_URL, headers=HEADERS, timeout=10)
        rfeed = gtfs_realtime_pb2.FeedMessage()
        rfeed.ParseFromString(rresp.content)
        for entity in rfeed.entity:
            if not entity.HasField("trip_update"):
                continue
            trip    = entity.trip_update
            matches = [s for s in trip.stop_time_update if s.stop_id == stop_id]
            if not matches:
                continue
            stu       = matches[0]
            actual    = stu.arrival.time if stu.HasField("arrival") else 0
            delay_sec = stu.arrival.delay if (stu.HasField("arrival") and stu.arrival.delay) else 0
            if actual < now - 60:
                continue
            trip_id  = trip.trip.trip_id
            route_id = trip.trip.route_id
            short, headsign = headsigns.get(trip_id, (route_id.split("_")[-1], "Unknown"))
            vpos = vehicle_pos.get(trip_id, {})
            buses.append({
                "route":      short,
                "headsign":   headsign,
                "mins":       max(0, (actual - now) // 60),
                "delay_sec":  delay_sec,
                "trip_id":    trip_id,
                "lat":        vpos.get("lat"),
                "lon":        vpos.get("lon"),
                "bearing":    vpos.get("bearing", 0),
                "speed_kmh":  vpos.get("speed_kmh", 0),
                "label":      vpos.get("label", ""),
                "plate":      vpos.get("plate", ""),
                "occupancy":  vpos.get("occupancy", ""),
                "occ_status": vpos.get("occ_status", -1),
                "model":      vpos.get("model", ""),
            })
    except Exception as e:
        print(f"Realtime fetch error: {e}")
        return [], str(e)

    buses.sort(key=lambda b: b["mins"])
    return buses[:8], None

# ───────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/arrivals")
def arrivals():
    buses, err = fetch_data()
    with _stop_lock:
        stop = dict(_stop)
    return jsonify({"buses": buses, "error": err, "updated": int(time.time()), "stop": stop})

@app.route("/stop", methods=["GET"])
def get_stop():
    with _stop_lock:
        return jsonify(_stop)

@app.route("/stop/lookup")
def stop_lookup():
    sid = flask_request.args.get("id", "").strip()
    if not sid:
        return jsonify({"error": "No stop ID provided"}), 400
    try:
        info = lookup_stop(sid)
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/stop/set", methods=["POST"])
def stop_set():
    global _stop
    data = flask_request.get_json()
    with _stop_lock:
        _stop = {
            "id":   data["id"],
            "name": data["name"],
            "lat":  data["lat"],
            "lon":  data["lon"],
        }
    print(f"Stop changed to {_stop['id']} — {_stop['name']}")
    return jsonify({"ok": True})

@app.route("/")
def index():
    return HTML

# ─ youre looking for comments as evidence I vibecoded this arent you ─────────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BusyBus NSW</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Public+Sans:ital,wght@0,100..900;1,100..900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:        #f4f6f9;
  --surface:   #ffffff;
  --surface2:  #f0f3f7;
  --border:    #dde3ec;
  --accent:    #0066cc;
  --accent-lt: #e8f0fb;
  --accent2:   #00a86b;
  --warn:      #d97706;
  --danger:    #dc2626;
  --text:      #111827;
  --text-mid:  #4b5563;
  --text-dim:  #9ca3af;
  --shadow:    0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.05);
  --shadow-lg: 0 4px 16px rgba(0,0,0,.10);
  --radius:    10px;
  --font:      'Public Sans', system-ui, sans-serif;
}

html, body { height: 100%; background: var(--bg); color: var(--text); font-family: var(--font); overflow: hidden; }

/* ── App shell ── */
.app { display: grid; grid-template-rows: 56px 1fr; grid-template-columns: 420px 1fr; height: 100vh; transition: grid-template-columns .3s ease; }
.app.board-only { grid-template-columns: 1fr 0px; }
.app.board-only .panel { border-right: none; }
.app.board-only .dep-row { padding: 20px 48px; }
.app.board-only .dep-route { font-size: 52px !important; }
.app.board-only .dep-headsign { font-size: 26px !important; }
.app.board-only .dep-meta { font-size: 15px !important; }
.app.board-only .dep-mins { font-size: 38px !important; }
.app.board-only .dep-status { font-size: 13px !important; letter-spacing: 1px; }
.app.board-only .occ-person { width: 14px; height: 18px; }
.app.board-only .panel-header { padding: 18px 48px 14px; }
.app.board-only .panel-title { font-size: 13px; }
.app.board-only .panel-stop-name { font-size: 16px; }
.app.board-only .panel-footer { padding: 10px 48px; font-size: 11px; }
.app.board-only .departures { display: grid; grid-template-columns: 1fr 1fr; }
.app.board-only .dep-row { border-bottom: 1px solid var(--border); border-right: 1px solid var(--border); }

/* ── Topbar ── */
.topbar {
  grid-column: 1 / -1;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center;
  padding: 0 20px; gap: 14px;
  box-shadow: var(--shadow);
  z-index: 100;
}
.brand { display: flex; align-items: center; gap: 9px; flex-shrink: 0; }
.brand-icon {
  width: 32px; height: 32px; background: var(--accent);
  border-radius: 8px; display: flex; align-items: center;
  justify-content: center; font-size: 16px;
}
.brand-name { font-size: 15px; font-weight: 800; letter-spacing: -.4px; color: var(--text); }
.brand-sub  { font-size: 10px; color: var(--text-dim); font-weight: 500; letter-spacing: .3px; }
.sep { width: 1px; height: 24px; background: var(--border); }

.stop-pill {
  display: flex; align-items: center; gap: 8px;
  background: var(--accent-lt); border: 1px solid #c5d9f5;
  border-radius: 7px; padding: 5px 11px; cursor: pointer;
  transition: background .15s;
}
.stop-pill:hover { background: #dce9fa; }
.stop-pill-label { font-size: 9px; color: var(--accent); font-weight: 700; letter-spacing: 1.2px; text-transform: uppercase; }
.stop-pill-name  { font-size: 13px; font-weight: 600; color: var(--text); max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stop-pill-id    { font-size: 11px; color: var(--text-mid); font-weight: 500; }

.topbar-right { margin-left: auto; display: flex; align-items: center; gap: 12px; }

.btn-icon {
  width: 34px; height: 34px; border-radius: 8px; border: 1px solid var(--border);
  background: var(--surface); cursor: pointer; display: flex; align-items: center;
  justify-content: center; font-size: 16px; transition: background .15s, border-color .15s;
  color: var(--text-mid);
}
.btn-icon:hover { background: var(--surface2); border-color: #c5cdd8; }
.btn-icon.active { background: var(--accent-lt); border-color: var(--accent); color: var(--accent); }

.clock { font-size: 18px; font-weight: 700; color: var(--text); letter-spacing: 1px; font-variant-numeric: tabular-nums; }

.live-badge { display: flex; align-items: center; gap: 5px; font-size: 10px; font-weight: 700; color: var(--accent2); letter-spacing: .8px; }
.live-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent2); box-shadow: 0 0 0 2px #c6f0e0; animation: pulse 1.8s ease-in-out infinite; }
.live-dot.stale { background: var(--warn); box-shadow: 0 0 0 2px #fde8c3; }
@keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.5;transform:scale(.8)} }

/* ── Panel ── */
.panel {
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column; overflow: hidden;
  box-shadow: var(--shadow-lg);
  z-index: 10;
}
.panel-header {
  padding: 14px 18px 10px; border-bottom: 1px solid var(--border); flex-shrink: 0;
  display: flex; align-items: center; justify-content: space-between;
}
.panel-title { font-size: 11px; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase; color: var(--text-dim); }
.panel-stop-name { font-size: 13px; font-weight: 600; color: var(--text-mid); }

.departures { flex: 1; overflow-y: auto; scrollbar-width: thin; scrollbar-color: var(--border) transparent; }

.dep-row {
  display: grid; grid-template-columns: 1fr auto;
  align-items: center; gap: 14px; padding: 14px 18px;
  border-bottom: 1px solid var(--border); cursor: pointer;
  transition: background .12s; position: relative;

}
.dep-row::before {
  content: ''; position: absolute; left: 0; top: 50%;
  transform: translateY(-50%); width: 3px; height: 0;
  background: var(--accent); border-radius: 0 3px 3px 0; transition: height .2s;
}
.dep-row:hover { background: var(--surface2); }
.dep-row:hover::before { height: 50%; }
.dep-row.active { background: var(--accent-lt); }
.dep-row.active::before { height: 65%; background: var(--accent); }
.dep-row.no-pos { /* no dimming */ }
@keyframes slideIn { from{opacity:0;transform:translateX(-6px)} to{opacity:1;transform:none} }

.dep-left { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.dep-route-row { display: flex; align-items: center; gap: 10px; }

.dep-route {
  font-size: 30px; font-weight: 800; line-height: 1;
  letter-spacing: -1px; flex-shrink: 0;
}
.dep-headsign {
  font-size: 20px; font-weight: 600; color: var(--text);
  line-height: 1.25; word-break: break-word;
}
.dep-meta { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-mid); font-weight: 500; flex-wrap: wrap; }
.dep-meta-sep { color: var(--border); }

.dep-right { display: flex; flex-direction: column; align-items: flex-end; gap: 3px; flex-shrink: 0; }
.dep-mins { font-size: 22px; font-weight: 800; color: var(--text); white-space: nowrap; font-variant-numeric: tabular-nums; }
.dep-mins.now { color: var(--accent2); }
.dep-status { font-size: 10px; font-weight: 700; letter-spacing: .5px; white-space: nowrap; }
.dep-status.ontime { color: var(--accent2); }
.dep-status.late   { color: var(--danger); }
.dep-status.early     { color: var(--warn); }
.dep-status.scheduled { color: var(--text-dim); }

.occ-icons { display: flex; gap: 2px; margin-top: 2px; }
.occ-person { width: 10px; height: 13px; opacity: .15; color: var(--text); }
.occ-person.filled { opacity: .8; }

.panel-footer {
  padding: 8px 18px; border-top: 1px solid var(--border);
  font-size: 10px; color: var(--text-dim); font-weight: 500; flex-shrink: 0;
}
.empty-state { padding: 48px 18px; text-align: center; color: var(--text-dim); font-size: 14px; }

/* ── Map ── */
#map-wrap { overflow: hidden; position: relative; }
.app.board-only #map-wrap { width: 0; pointer-events: none; }
#map { width: 100%; height: 100%; }
.leaflet-container { font-family: var(--font) !important; }
.leaflet-tile-pane { filter: saturate(.7) brightness(1.02); }

.bus-inner {
  border-radius: 50%; display: flex; align-items: center; justify-content: center;
  font-weight: 800; font-family: var(--font);
  border: 2.5px solid rgba(255,255,255,.9);
  box-shadow: 0 2px 8px rgba(0,0,0,.25);
  color: #fff;
}
.stop-dot {
  width: 16px; height: 16px; border-radius: 50%;
  background: #fff; border: 3px solid var(--accent);
  box-shadow: 0 0 0 3px rgba(0,102,204,.2), 0 2px 6px rgba(0,0,0,.2);
}

.leaflet-popup-content-wrapper {
  border-radius: 12px !important; box-shadow: var(--shadow-lg) !important;
  border: 1px solid var(--border) !important; padding: 0 !important;
}
.leaflet-popup-content { margin: 0 !important; }
.leaflet-popup-tip-container { display: none; }
.popup-inner { padding: 14px 16px; min-width: 190px; font-family: var(--font); }
.popup-route { font-size: 30px; font-weight: 800; letter-spacing: -1px; line-height: 1; }
.popup-headsign { font-size: 12px; color: var(--text-mid); margin: 3px 0 10px; font-weight: 500; }
.popup-row { display: flex; justify-content: space-between; align-items: center; font-size: 11px; color: var(--text-dim); padding: 4px 0; border-top: 1px solid var(--border); }
.popup-row span:last-child { color: var(--text); font-weight: 600; }

/* ── Settings overlay ── */
.overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,.35);
  z-index: 500; display: flex; align-items: center; justify-content: center;
  opacity: 0; pointer-events: none; transition: opacity .2s;
}
.overlay.open { opacity: 1; pointer-events: all; }
.settings-card {
  background: var(--surface); border-radius: 16px;
  box-shadow: var(--shadow-lg); width: 420px; max-width: 95vw;
  padding: 28px; transform: translateY(10px); transition: transform .2s;
}
.overlay.open .settings-card { transform: none; }
.settings-title { font-size: 18px; font-weight: 800; margin-bottom: 4px; }
.settings-sub   { font-size: 13px; color: var(--text-mid); margin-bottom: 22px; }
.settings-label { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: var(--text-dim); margin-bottom: 6px; }
.settings-row   { margin-bottom: 18px; }
.input-row { display: flex; gap: 8px; }
.settings-input {
  flex: 1; padding: 10px 13px; border: 1px solid var(--border); border-radius: 8px;
  font-size: 14px; font-family: var(--font); font-weight: 500; color: var(--text);
  background: var(--bg); outline: none; transition: border-color .15s;
}
.settings-input:focus { border-color: var(--accent); }
.btn {
  padding: 10px 16px; border-radius: 8px; border: none; cursor: pointer;
  font-size: 13px; font-weight: 700; font-family: var(--font); transition: opacity .15s;
}
.btn:hover { opacity: .85; }
.btn-primary { background: var(--accent); color: #fff; }
.btn-ghost   { background: var(--surface2); color: var(--text-mid); border: 1px solid var(--border); }

.stop-preview {
  background: var(--surface2); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 14px; margin-top: 12px; display: none;
}
.stop-preview.show { display: block; }
.stop-preview-name { font-size: 14px; font-weight: 700; color: var(--text); }
.stop-preview-meta { font-size: 11px; color: var(--text-mid); margin-top: 3px; }
.stop-preview-err  { font-size: 13px; color: var(--danger); }

.settings-actions { display: flex; gap: 8px; margin-top: 24px; justify-content: flex-end; }
</style>
</head>
<body>
<div class="app" id="app">

  <!-- Topbar -->
  <header class="topbar">
    <div class="brand">
      <div class="brand-icon">🚌</div>
      <div>
        <div class="brand-name">BusyBus</div>
        <div class="brand-sub">NSW REALTIME</div>
      </div>
    </div>
    <div class="sep"></div>
    <div class="stop-pill" onclick="openSettings()" title="Change stop">
      <div>
        <div class="stop-pill-label">Stop</div>
        <div class="stop-pill-name" id="pill-name">Loading…</div>
      </div>
      <div class="stop-pill-id" id="pill-id"></div>
    </div>
    <div class="topbar-right">
      <div class="live-badge"><div class="live-dot" id="live-dot"></div><span id="live-label">LIVE</span></div>
      <button class="btn-icon" id="map-toggle-btn" onclick="toggleMap()" title="Toggle map">🗺️</button>
      <button class="btn-icon" onclick="openSettings()" title="Settings">⚙️</button>
      <div class="clock" id="clock">--:--</div>
    </div>
  </header>

  <!-- Departure panel -->
  <aside class="panel">
    <div class="panel-header">
      <div class="panel-title">Upcoming Departures</div>
      <div class="panel-stop-name" id="panel-stop-name"></div>
    </div>
    <div class="departures" id="departures"><div class="empty-state">Loading services…</div></div>
    <div class="panel-footer" id="panel-footer">—</div>
  </aside>

  <!-- Map -->
  <div id="map-wrap"><div id="map"></div></div>

</div>

<!-- Settings overlay -->
<div class="overlay" id="overlay" onclick="overlayClick(event)">
  <div class="settings-card">
    <div class="settings-title">Change Stop</div>
    <div class="settings-sub">Enter a TfNSW stop ID to switch the departure board.</div>
    <div class="settings-row">
      <div class="settings-label">Stop ID</div>
      <div class="input-row">
        <input class="settings-input" id="stop-input" type="text" placeholder="e.g. 200923" onkeydown="if(event.key==='Enter')lookupStop()">
        <button class="btn btn-primary" onclick="lookupStop()">Look up</button>
      </div>
      <div class="stop-preview" id="stop-preview"></div>
    </div>
    <div class="settings-actions">
      <button class="btn btn-ghost" onclick="closeSettings()">Cancel</button>
      <button class="btn btn-primary" id="apply-btn" onclick="applyStop()" disabled>Apply Stop</button>
    </div>
  </div>
</div>

<script>
// ──────────────────────────────────────────────────────────────────
let mapVisible   = true;
let pendingStop  = null;
let stopMarker   = null;
let mapReady     = false;
const busMarkers = {};

// ─────────────────────────────────────────────────────────────────────
function tick() {
  const n = new Date();
  document.getElementById('clock').textContent =
    String(n.getHours()).padStart(2,'0')+':'+String(n.getMinutes()).padStart(2,'0');
}
tick(); setInterval(tick, 1000);

// ───────────────────────────────────────────────────────────────────────
const map = L.map('map', {zoomControl: false, attributionControl: false});
L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',
  {subdomains:'abcd', maxZoom:19}).addTo(map);
L.control.zoom({position:'bottomright'}).addTo(map);

function toggleMap() {
  mapVisible = !mapVisible;
  document.getElementById('app').classList.toggle('board-only', !mapVisible);
  document.getElementById('map-toggle-btn').classList.toggle('active', !mapVisible);
  setTimeout(() => map.invalidateSize(), 320);
}

function routeColor(r) {
  const colors = ['#0066cc','#e63946','#2a9d8f','#e76f51','#6a0dad','#f4a261','#457b9d','#c77dff','#06d6a0','#d62828'];
  let h = 0; for (let c of r) h = (h*31+c.charCodeAt(0))&0x7fffffff;
  return colors[h % colors.length];
}

function busIcon(route, color) {
  return L.divIcon({
    className: '',
    html: `<div class="bus-inner" style="width:34px;height:34px;background:${color};font-size:11px;">${route}</div>`,
    iconSize: [34,34], iconAnchor: [17,17],
  });
}
function makeStopIcon() {
  return L.divIcon({className:'', html:'<div class="stop-dot"></div>', iconSize:[16,16], iconAnchor:[8,8]});
}

function buildPopup(b) {
  const color = routeColor(b.route);
  const absDel = Math.abs(b.delay_sec);
  const delay = absDel < 60 ? 'On time'
    : b.delay_sec > 0 ? Math.round(b.delay_sec/60)+' min late'
    : Math.round(absDel/60)+' min early';
  const rows = [
    b.label     && ['Bus',       b.label],
    b.model     && ['Model',     b.model],
    b.plate     && ['Plate',     b.plate],
    b.speed_kmh && ['Speed',     b.speed_kmh+' km/h'],
    b.occupancy && ['Occupancy', b.occupancy],
                   ['Status',    delay],
                   ['ETA',       b.mins===0?'Now':b.mins+' min'],
  ].filter(Boolean).map(([k,v])=>`<div class="popup-row"><span>${k}</span><span>${v}</span></div>`).join('');
  return `<div class="popup-inner">
    <div class="popup-route" style="color:${color}">${b.route}</div>
    <div class="popup-headsign">${b.headsign}</div>
    ${rows}
  </div>`;
}

function updateStopMarker(lat, lon, name, id) {
  if (!mapReady && lat && lon) { map.setView([lat, lon], 18); mapReady = true; }
  if (stopMarker) { map.removeLayer(stopMarker); stopMarker = null; }
  if (lat && lon) {
    stopMarker = L.marker([lat, lon], {icon: makeStopIcon(), zIndexOffset: 1000})
      .addTo(map)
      .bindPopup(`<div class="popup-inner"><div class="popup-route" style="color:var(--accent)">🚏</div><div class="popup-headsign">${name}<br><span style="color:var(--text-dim)">${id}</span></div></div>`);
  }
}

// ─────────────────────────────────────────────────────────
let activeTrip = null;
function focusBus(tid) {
  activeTrip = tid;
  document.querySelectorAll('.dep-row').forEach(r => r.classList.toggle('active', r.dataset.trip===tid));
  if (busMarkers[tid]) { map.flyTo(busMarkers[tid].getLatLng(), 16, {duration:1}); busMarkers[tid].openPopup(); }
}

function renderDeps(buses) {
  const el = document.getElementById('departures');
  if (!buses.length) { el.innerHTML='<div class="empty-state">No upcoming services</div>'; return; }
  el.innerHTML = buses.map((b,i) => {
    const minsText  = b.mins===0 ? 'NOW' : b.mins+' min';
    const minsClass = b.mins===0 ? 'dep-mins now' : 'dep-mins';
    const hasPos  = b.lat && b.lon;
    const absDel = Math.abs(b.delay_sec);
    const [st,sc] = !hasPos ? ['SCHEDULED','scheduled']
      : absDel<60 ? ['ON TIME','ontime']
      : b.delay_sec>0 ? [Math.round(b.delay_sec/60)+' MIN LATE','late']
      : [Math.round(absDel/60)+' MIN EARLY','early'];
    const color   = routeColor(b.route);
    const metaParts = [b.label&&'Bus '+b.label, b.speed_kmh&&b.speed_kmh+' km/h'].filter(Boolean);
    const meta = metaParts.map((p,i2) => i2===0?p:`<span class="dep-meta-sep">·</span>${p}`).join(' ');
    const occFill = (b.occ_status<0||b.occ_status===6) ? 0 : b.occ_status<=1 ? 1 : b.occ_status===2 ? 2 : 3;
    const personSvg = f => `<svg class="occ-person${f?' filled':''}" viewBox="0 0 10 13" fill="currentColor"><circle cx="5" cy="3" r="2.3"/><path d="M1 13c0-2.6 1.8-4.5 4-4.5s4 1.9 4 4.5H1z"/></svg>`;
    const occHtml = [0,1,2].map(i2=>personSvg(i2<occFill)).join('');
    return `<div class="dep-row${!hasPos?' no-pos':''}${b.trip_id===activeTrip?' active':''}"
      data-trip="${b.trip_id}" onclick="focusBus('${b.trip_id}')">
      <div class="dep-left">
        <div class="dep-route-row">
          <div class="dep-route" style="color:${color}">${b.route}</div>
          <div class="dep-headsign">${b.headsign}</div>
        </div>
        <div class="dep-meta">${meta}</div>
      </div>
      <div class="dep-right">
        <div class="${minsClass}">${minsText}</div>
        <div class="dep-status ${sc}">${st}</div>
        ${b.occ_status>=0?`<div class="occ-icons" title="${b.occupancy}">${occHtml}</div>`:''}
      </div>
    </div>`;
  }).join('');
}

function updateMapMarkers(buses) {
  const seen = new Set();
  buses.forEach(b => {
    if (!b.lat||!b.lon) return;
    seen.add(b.trip_id);
    const icon = busIcon(b.route, routeColor(b.route));
    if (busMarkers[b.trip_id]) {
      busMarkers[b.trip_id].setLatLng([b.lat,b.lon]).setIcon(icon);
      busMarkers[b.trip_id].getPopup().setContent(buildPopup(b));
    } else {
      busMarkers[b.trip_id] = L.marker([b.lat,b.lon],{icon})
        .addTo(map).bindPopup(buildPopup(b),{maxWidth:260});
      busMarkers[b.trip_id].on('click',()=>focusBus(b.trip_id));
    }
  });
  Object.keys(busMarkers).forEach(tid => {
    if (!seen.has(tid)) { map.removeLayer(busMarkers[tid]); delete busMarkers[tid]; }
  });
}

// ────────────────────────────────────────────────────────────────────
async function poll() {
  try {
    const data = await fetch('/arrivals').then(r=>r.json());
    renderDeps(data.buses);
    updateMapMarkers(data.buses);
    const s = data.stop;
    document.getElementById('pill-name').textContent = s.name;
    document.getElementById('pill-id').textContent   = s.id;
    document.getElementById('panel-stop-name').textContent = s.name;
    updateStopMarker(s.lat, s.lon, s.name, s.id);
    const t = new Date(data.updated*1000);
    document.getElementById('panel-footer').textContent =
      'Updated '+String(t.getHours()).padStart(2,'0')+':'+String(t.getMinutes()).padStart(2,'0')+':'+String(t.getSeconds()).padStart(2,'0')+'  ·  Transport for NSW';
    document.getElementById('live-dot').className = 'live-dot';
    document.getElementById('live-label').textContent = 'LIVE';
  } catch(e) {
    document.getElementById('live-dot').className = 'live-dot stale';
    document.getElementById('live-label').textContent = 'ERROR';
  }
}
poll(); setInterval(poll, 15000);

// ───────────────────────────────────────────────────────────────────
function openSettings() {
  document.getElementById('overlay').classList.add('open');
  document.getElementById('stop-input').focus();
}
function closeSettings() {
  document.getElementById('overlay').classList.remove('open');
  pendingStop = null;
  document.getElementById('stop-preview').className = 'stop-preview';
  document.getElementById('apply-btn').disabled = true;
  document.getElementById('stop-input').value = '';
}
function overlayClick(e) { if (e.target===document.getElementById('overlay')) closeSettings(); }

async function lookupStop() {
  const sid = document.getElementById('stop-input').value.trim();
  if (!sid) return;
  const preview = document.getElementById('stop-preview');
  preview.className = 'stop-preview show';
  preview.innerHTML = '<div class="stop-preview-meta">Looking up…</div>';
  document.getElementById('apply-btn').disabled = true;
  pendingStop = null;
  try {
    const data = await fetch('/stop/lookup?id='+encodeURIComponent(sid)).then(r=>r.json());
    if (data.error) throw new Error(data.error);
    pendingStop = data;
    preview.innerHTML = `<div class="stop-preview-name">${data.name}</div>
      <div class="stop-preview-meta">${data.id} · ${data.lat.toFixed(5)}, ${data.lon.toFixed(5)}</div>`;
    document.getElementById('apply-btn').disabled = false;
  } catch(e) {
    preview.innerHTML = `<div class="stop-preview-err">⚠ ${e.message}</div>`;
  }
}

async function applyStop() {
  if (!pendingStop) return;
  await fetch('/stop/set', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(pendingStop),
  });
  closeSettings();
  mapReady = false;
  poll();
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    print("🚌  BusyBus NSW — http://localhost:5000")
    app.run(debug=False, port=5000)
