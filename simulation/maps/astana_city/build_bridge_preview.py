"""Experimental elevation/3D preview; never overwrites the working city network."""
import json
import hashlib
import math
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
import sumolib

BASE = Path(__file__).resolve().parent
OUT = BASE / "generated/bridge_preview"
RAMPS = {"24672587", "165975916", "37883864", "24672593", "24672599", "37883874",
         "37883866", "1381198546", "24672609", "1381198544", "44127578", "44127582",
         "1381198543", "1381198545"}
UPPER = {"37883868", "37883870", "1186145847", "1186145848", "742779723", "166195960"}


def set_height(shape, start, end):
    points = [list(map(float, p.split(",")))[:2] for p in shape.split()]
    distances = [0.0]
    for a, b in zip(points, points[1:]):
        distances.append(distances[-1] + math.dist(a, b))
    total = distances[-1] or 1
    return " ".join(f"{p[0]},{p[1]},{start+(end-start)*d/total:.4f}" for p, d in zip(points, distances))


def distance_to_segment(p, a, b):
    dx, dy = b[0]-a[0], b[1]-a[1]
    t = max(0, min(1, ((p[0]-a[0])*dx+(p[1]-a[1])*dy)/(dx*dx+dy*dy or 1)))
    return math.hypot(p[0]-a[0]-t*dx, p[1]-a[1]-t*dy)


def elevation_network(binary):
    source = BASE / "generated/interchange/network.net.xml"
    prefix = OUT / "plain"
    subprocess.run([binary, "--sumo-net-file", str(source), "--plain-output-prefix", str(prefix),
                    "--output-file", str(OUT / "flat.net.xml")], check=True, capture_output=True)
    nodes = ET.parse(str(prefix) + ".nod.xml")
    edges = ET.parse(str(prefix) + ".edg.xml")
    xy = {n.get("id"): f"{n.get('x')},{n.get('y')}" for n in nodes.getroot().findall("node")}
    fixed = {}; graph = {}; edge_orig = {}
    for e in edges.getroot().findall("edge"):
        ids = {e.get("id").split("#")[0]}
        for p in e.findall(".//param[@key='origId']"):
            ids.update(p.get("value").split())
        edge_orig[e.get("id")] = ids
        a, b = e.get("from"), e.get("to")
        if not e.get("shape"): e.set("shape", f"{xy[a]} {xy[b]}")
        pts = [tuple(map(float, p.split(",")[:2])) for p in e.get("shape").split()]
        length = sum(math.dist(p,q) for p,q in zip(pts,pts[1:])) or 1
        graph.setdefault(a, []).append((b, 1/length)); graph.setdefault(b, []).append((a, 1/length))
        if ids & UPPER:
            fixed[a] = fixed[b] = 6.5
    for e in edges.getroot().findall("edge"):
        if not edge_orig[e.get("id")] & (UPPER | RAMPS):
            for key in (e.get("from"), e.get("to")):
                fixed.setdefault(key, 0.0)
    heights = {n.get("id"): fixed.get(n.get("id"), 0.0) for n in nodes.getroot().findall("node")}
    for _ in range(2000):
        change = 0
        for key in heights.keys() - fixed.keys():
            adjacent = graph.get(key, [])
            if not adjacent: continue
            z = sum(heights[k]*w for k,w in adjacent)/sum(w for k,w in adjacent)
            change = max(change, abs(z-heights[key])); heights[key] = z
        if change < 1e-8: break
    for n in nodes.getroot().findall("node"):
        n.set("z", str(heights[n.get("id")]))
        if n.get("shape"): n.set("shape", set_height(n.get("shape"), heights[n.get("id")], heights[n.get("id")]))
    for e in edges.getroot().findall("edge"):
        z1, z2 = heights[e.get("from")], heights[e.get("to")]
        e.set("shape", set_height(e.get("shape"), z1, z2))
        for lane in e.findall("lane"):
            if lane.get("shape"): lane.set("shape", set_height(lane.get("shape"), z1, z2))
    nodes.write(OUT / "height.nod.xml", encoding="utf-8", xml_declaration=True)
    edges.write(OUT / "height.edg.xml", encoding="utf-8", xml_declaration=True)
    command = [binary, "--node-files", str(OUT / "height.nod.xml"),
               "--edge-files", str(OUT / "height.edg.xml"),
               "--connection-files", str(prefix)+".con.xml", "--tllogic-files", str(prefix)+".tll.xml",
               "--output-file", str(OUT / "network.net.xml"),
               "--junctions.corner-detail", "10", "--junctions.internal-link-detail", "10",
               "--output.original-names", "--output.street-names"]
    with (OUT / "build.log").open("w") as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    return command


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((BASE / "generated/manifest.json").read_text())
    command = elevation_network(manifest["command"][0])
    root = ET.parse(OUT / "network.net.xml").getroot()
    # netconvert trims lane approaches when constructing junction polygons.
    # Reparameterize elevation on the actual trimmed lanes to avoid steep
    # artificial jumps caused by retaining untrimmed intermediate Z samples.
    node_z = {n.get("id"): float(n.get("z", "0")) for n in root.findall("junction")}
    for edge in root.findall("edge"):
        if edge.get("function") == "internal":
            key = edge.get("id")[1:].rsplit("_", 1)[0]
            a = b = node_z.get(key, 0)
        else:
            a, b = node_z[edge.get("from")], node_z[edge.get("to")]
        for lane in edge.findall("lane"):
            lane.set("shape", set_height(lane.get("shape"), a, b))
    ET.ElementTree(root).write(OUT / "network.net.xml", encoding="utf-8", xml_declaration=True)
    from corridors import lane_signature, movement, verify_subset
    from review_interchange import check_lanes
    flat = ET.parse(BASE / "generated/interchange/network.net.xml").getroot()
    source_lanes = {e.get("id"): lane_signature(e) for e in flat.findall("edge") if e.get("function") != "internal"}
    source_moves = {movement(c) for c in flat.findall("connection") if not c.get("from").startswith(":")}
    checks = verify_subset(root, source_lanes, source_moves, set(source_lanes))
    check_lanes(root)
    lane_shapes = {l.get("id"): [tuple(map(float,p.split(","))) for p in l.get("shape").split()]
                   for l in root.findall("edge/lane")}
    for c in root.findall("connection"):
        a = lane_shapes[c.get("from")+"_"+c.get("fromLane")][-1]
        target = c.get("via") or c.get("to")+"_"+c.get("toLane")
        b = lane_shapes[target][0]
        if abs(a[2]-b[2]) > .01: raise ValueError(f"Vertical discontinuity: {c.attrib}")
    grades = []
    for e in root.findall("edge"):
        key = e.get("id").split("#")[0]
        for l in e.findall("lane"):
            points = lane_shapes[l.get("id")]
            if key in {"165816979", "735041697", "735041698", "688297715"}:
                if any(abs(p[2])>.01 for p in points): raise ValueError("Lower main road lifted")
            if key in UPPER and any(abs(p[2]-6.5)>.01 for p in points): raise ValueError("Bridge deck not raised")
            if key in RAMPS:
                grades.extend(abs(a[2]-b[2])/math.dist(a[:2],b[:2])*100
                              for a,b in zip(points,points[1:]) if math.dist(a[:2],b[:2])>.1)
    if max(grades)>8: raise ValueError(f"Ramp exceeds assumed 8% grade: {max(grades)}")
    with (OUT / "simulation.log").open("w") as log:
        subprocess.run([sumolib.checkBinary("sumo"), "-n", str(OUT / "network.net.xml"),
                        "-r", str(BASE / "generated/interchange/check.rou.xml"),
                        "--end", "1800", "--seed", "42", "--time-to-teleport", "-1",
                        "--no-step-log", "true", "--tripinfo-output", str(OUT / "trips.xml")],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    trips = ET.parse(OUT / "trips.xml").getroot().findall("tripinfo")
    expected = {v.get("id") for v in ET.parse(BASE / "generated/interchange/check.rou.xml").getroot().findall("vehicle")}
    if {t.get("id") for t in trips} != expected: raise ValueError("Incomplete bridge route probes")
    checks.update(completed=len(trips), max_ramp_grade_percent=max(grades), vertical_discontinuities=0)
    (OUT / "validation.json").write_text(json.dumps(checks, indent=2))
    lanes = []
    for edge in root.findall("edge"):
        for lane in edge.findall("lane"):
            points = [list(map(float, p.split(","))) for p in lane.get("shape").split()]
            points = [p if len(p) == 3 else p + [0] for p in points]
            lanes.append({"id": lane.get("id"), "edge": edge.get("id"),
                          "width": float(lane.get("width", "3.2")), "points": points,
                          "internal": edge.get("function") == "internal"})
    coords = [p for lane in lanes for p in lane["points"]]
    net = sumolib.net.readNet(str(OUT / "network.net.xml"))
    center = net.convertLonLat2XY(71.4401, 51.1324)
    for lane in lanes:
        lane["points"] = [[round(p[0]-center[0], 2), round(p[1]-center[1], 2), p[2]]
                          for p in lane["points"]]
    # Schematic supports only in spaces clear of lower road lane envelopes.
    lower = [(a,b,lane["width"]) for lane in lanes for a,b in zip(lane["points"],lane["points"][1:])
             if max(a[2],b[2]) < 4.5]
    supports = []
    for lane in lanes:
        if lane["edge"].split("#")[0] not in UPPER or not lane["id"].endswith("_0"): continue
        for a,b in zip(lane["points"],lane["points"][1:]):
            length = math.dist(a[:2],b[:2])
            for i in range(max(1,int(length/40))):
                t = (i+.5)/max(1,int(length/40)); p = [a[k]+t*(b[k]-a[k]) for k in range(3)]
                if p[2] < 5 or any(math.dist(p[:2],q[:2])<35 for q in supports): continue
                if any(distance_to_segment(p,c,d)<width/2+4 for c,d,width in lower): continue
                supports.append(p)
    data = {"lanes": lanes, "supports": supports, "assumed_layer_height_m": 6.5,
            "z_range": [min(p[2] for p in coords), max(p[2] for p in coords)]}
    if data["z_range"][1] <= 0:
        raise ValueError("Elevation import produced a flat network")
    template = (BASE / "bridge_preview.html").read_text()
    (OUT / "index.html").write_text(template.replace("/*NETWORK_DATA*/null", json.dumps(data)))
    (OUT / "manifest.json").write_text(json.dumps({"command": command,
        "assumptions": "Upper deck 6.5m, main road 0m, interpolated ramps; not surveyed heights; schematic structures",
        "source_sha256": hashlib.sha256((BASE / "generated/interchange/network.net.xml").read_bytes()).hexdigest(),
        "z_range": data["z_range"]}, indent=2))
    config = ET.Element("configuration")
    ET.SubElement(ET.SubElement(config, "input"), "net-file", value="network.net.xml")
    ET.SubElement(ET.SubElement(config, "time"), "end", value="1")
    ET.ElementTree(config).write(OUT / "view.sumocfg", encoding="utf-8", xml_declaration=True)
    print(json.dumps({"lanes": len(lanes), "z_range": data["z_range"], "preview": str(OUT / "index.html")}))


if __name__ == "__main__":
    main()
