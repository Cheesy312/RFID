from flask import Flask, request, render_template, Response
import sqlite3, csv, io
from datetime import datetime, timedelta

app = Flask(__name__)
DB_PATH = "/home/pi/projects/engine_registry.db"

STATIONS = ["Station1", "Station2", "Station3"]
GANTT_WINDOW_MIN = 480  # 8 hours window


# ---------------- DB ----------------
def db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


# Return scans for engine
def scans_for_engine(cur, engine_id):
    cur.execute("""
        SELECT station, timestamp FROM Step2Registry
        WHERE engine_id=?
        ORDER BY timestamp ASC
    """, (engine_id,))
    return cur.fetchall()


# Build Gantt segments
def build_segments(scans):
    if not scans:
        return []
    seg = []
    st, start = scans[0]
    for s, ts in scans[1:]:
        if s != st:
            seg.append((st, start, ts))
            st, start = s, ts
    seg.append((st, start, None))
    return seg


# Check if engine finished all stations
def engine_is_complete(scans):
    if not scans:
        return False
    seen = {s for s, _ in scans}
    return all(st in seen for st in STATIONS) and scans[-1][0] == STATIONS[-1]


# Format time display for table
def format_spent(active_time):
    secs = int(active_time) if active_time else 0
    return f"{secs//60:02}:{secs%60:02}"


# ---------------- STEP1 REGISTER ENGINE ----------------
@app.route("/post", methods=["POST"])
def post_step1():
    data = request.get_json(force=True)
    epc, eng = data.get("epc"), data.get("eng")
    if not epc or not eng:
        return "missing", 400
    conn = db(); cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO EngineRegistry(epc,engine_name) VALUES(?,?)",
        (epc, eng)
    )
    conn.commit()
    return "OK"


# ---------------- STEP2 SCANS (ACTIVE TIME) ----------------
@app.route("/post_step2", methods=["POST"])
def post_step2():
    try:
        data = request.get_json(force=True)
    except:
        return "INVALID_JSON", 400

    epc = data.get("epc")
    station = data.get("station")
    if not epc or not station:
        return "MISSING_FIELDS", 400

    now = datetime.utcnow()

    conn = db(); cur = conn.cursor()
    cur.execute("SELECT id, last_seen, active_time_seconds FROM EngineRegistry WHERE epc=?", (epc,))
    row = cur.fetchone()

    # Drop unknown tags silently
    if not row:
        conn.close()
        return "IGNORED_UNKNOWN_EPC", 200

    engine_id, last_seen, active_time = row

    # time accumulation logic
    if last_seen:
        try:
            last_dt = datetime.fromisoformat(last_seen)
        except:
            last_dt = datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S")
        delta = (now - last_dt).total_seconds()
        if delta < 10:
            active_time += delta
        else:
            active_time = 0
    else:
        active_time = 0

    # Update engine state
    cur.execute(
        "UPDATE EngineRegistry SET last_seen=?, active_time_seconds=? WHERE id=?",
        (now.isoformat(), active_time, engine_id)
    )

    # Store scan
    cur.execute(
        "INSERT INTO Step2Registry (engine_id, timestamp, station) VALUES (?,?,?)",
        (engine_id, now.isoformat(), station)
    )

    conn.commit()
    conn.close()
    return "OK", 200


# ---------------- DASHBOARD ROUTES ----------------
@app.route("/")
@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route("/fragment_table")
def fragment_table():
    q = (request.args.get("q") or "").lower()
    sort = request.args.get("sort","last")
    direction = request.args.get("dir","desc")
    filter_mode = request.args.get("filter","active")

    conn = db(); cur = conn.cursor()
    cur.execute("SELECT id, epc, engine_name, last_seen, active_time_seconds FROM EngineRegistry")
    engines = cur.fetchall()

    rows=[]
    for eid, epc, eng, last_seen, active_time in engines:
        scans = scans_for_engine(cur, eid)
        complete = engine_is_complete(scans)

        # filter logic
        if filter_mode=="active" and complete: continue
        if filter_mode=="completed" and not complete: continue
        if filter_mode in STATIONS and (not scans or scans[-1][0]!=filter_mode): continue
        if q and q not in eng.lower() and q not in epc.lower(): continue

        st = scans[-1][0] if scans else ""
        last_ts = last_seen or ""
        spent = format_spent(active_time)

        rows.append([eng, epc, st, last_ts, spent, eid])

    key = {"engine":0,"epc":1,"station":2,"last":3}.get(sort,3)
    rows.sort(key=lambda r:r[key], reverse=(direction=="desc"))

    html=["<table class='w-full text-sm bg-slate-800 rounded overflow-hidden'>"]
    html.append("<thead class='bg-slate-700 text-gray-200'><tr>")
    for label,field in [("Engine","engine"),("EPC","epc"),("Station","station"),("Last Scan","last")]:
        html.append(f"<th class='p-2 cursor-pointer' onclick=\"setSort('{field}')\">{label}</th>")
    html.append("<th class='p-2'>Time</th><th class='p-2'>Actions</th></tr></thead><tbody>")

    for eng,epc,st,last_ts,spent,eid in rows:
        html.append(
            f"<tr class='border-b border-slate-700'>"
            f"<td class='p-2 font-bold text-blue-300'>{eng}</td>"
            f"<td class='p-2'>{epc}</td>"
            f"<td class='p-2'>{st}</td>"
            f"<td class='p-2'>{last_ts}</td>"
            f"<td class='p-2'>{spent}</td>"
            f"<td class='p-2 flex gap-2'>"
            f"<button class='bg-blue-500 px-2 py-1 rounded' onclick=\"openRename('{eid}','{eng}')\">Rename</button>"
            f"<button class='bg-purple-500 px-2 py-1 rounded' onclick=\"openTimeline('{eid}','{eng}')\">Timeline</button>"
            f"<button class='bg-red-600 px-2 py-1 rounded' onclick=\"openDelete('{eid}','{eng}')\">Delete</button>"
            "</td></tr>"
        )

    html.append("</tbody></table>")
    return "".join(html)


# ---------------- GANTT SVG ----------------
def station_color(name):
    return {"Station1":"#60a5fa","Station2":"#34d399","Station3":"#fbbf24"}.get(name,"#a78bfa")

@app.route("/gantt_svg")
def gantt_svg():
    filter_mode = request.args.get("filter","active")
    conn=db();cur=conn.cursor()
    cur.execute("SELECT id, engine_name FROM EngineRegistry ORDER BY engine_name ASC")
    engines=cur.fetchall()

    now = datetime.now()
    start = now - timedelta(minutes=GANTT_WINDOW_MIN)
    total = (now-start).total_seconds()

    LEFT=120; TOP=40; H=22; GAP=8; WIDTH=1100; usable=WIDTH-LEFT-20

    rows=[]
    for eid, eng in engines:
        scans = scans_for_engine(cur, eid)
        if not scans: continue
        if filter_mode=="active" and engine_is_complete(scans): continue
        if filter_mode=="completed" and not engine_is_complete(scans): continue
        if filter_mode in STATIONS and scans[-1][0]!=filter_mode: continue

        segs = build_segments(scans)
        boxes=[]

        for st,en,lv in segs:
            try:
                t0 = datetime.fromisoformat(en)
            except:
                t0 = datetime.strptime(en,"%Y-%m-%d %H:%M:%S")

            t1 = datetime.fromisoformat(lv) if lv else now

            t0=max(t0,start); t1=min(t1,now)
            if t1<=t0: continue

            x=LEFT+int((t0-start).total_seconds()/total*usable)
            w=max(1,int((t1-t0).total_seconds()/total*usable))
            boxes.append((st,x,w))

        if boxes: rows.append((eng,boxes))

    height = TOP + len(rows)*(H+GAP) + 30
    svg=[f"<svg width='{WIDTH}' height='{height}' xmlns='http://www.w3.org/2000/svg'>"]
    svg.append("<rect width='100%' height='100%' fill='#0f172a'/>")
    svg.append(f"<text x='20' y='24' fill='#e2e8f0' font-size='16'>Gantt — last {GANTT_WINDOW_MIN} min</text>")

    steps=max(4,GANTT_WINDOW_MIN//60)
    for i in range(steps+1):
        frac=i/steps
        x=LEFT+int(frac*usable)
        t=start+timedelta(minutes=i*(GANTT_WINDOW_MIN/steps))
        svg.append(f"<line x1='{x}' y1='{TOP-5}' x2='{x}' y2='{height-20}' stroke='#334155'/>")
        svg.append(f"<text x='{x-18}' y='{TOP-10}' fill='#94a3b8' font-size='11'>{t.strftime('%H:%M')}</text>")

    y=TOP
    for eng,boxes in rows:
        svg.append(f"<text x='10' y='{y+H-6}' fill='#93c5fd' font-size='12'>{eng}</text>")
        svg.append(f"<rect x='{LEFT}' y='{y}' width='{usable}' height='{H}' fill='#0b1220' stroke='#1f2937'/>")
        for st,x,w in boxes:
            svg.append(f"<rect x='{x}' y='{y+2}' width='{w}' height='{H-4}' rx='3' ry='3' fill='{station_color(st)}'/>")
        y+=H+GAP

    svg.append("</svg>")
    return "".join(svg)


# ---------------- RUN ----------------
if __name__ == "__main__":
    print("✅ Flask running at http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000)
