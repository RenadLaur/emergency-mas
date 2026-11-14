"""Reproducible local elevation overlays; no changes to the working city net."""
import hashlib
import heapq
import html
import json
import math
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import sumolib
try:
    from .build_bridge_preview import set_height
except ImportError:
    from build_bridge_preview import set_height

BASE = Path(__file__).resolve().parent
OUT = BASE / 'generated/bridge_preview/catalog'


def original_ids(edge):
    ids = {edge.get('id').split('#')[0].lstrip('-')}
    for p in edge.findall(".//param[@key='origId']"):
        ids.update(s.split('#')[0].lstrip('-') for s in p.get('value').split())
    return ids


def elevate(root, profile):
    edges = [e for e in root.findall('edge') if e.get('function') != 'internal']
    anchors = profile['anchors']
    fixed, graph, length_graph, matched = {}, {}, {}, set()
    for e in edges:
        a, b = e.get('from'), e.get('to')
        edge_length = max(float(e.find('lane').get('length')), 1)
        weight = 1 / edge_length
        graph.setdefault(a, []).append((b, weight))
        graph.setdefault(b, []).append((a, weight))
        length_graph.setdefault(a, []).append((b, edge_length))
        length_graph.setdefault(b, []).append((a, edge_length))
        for key in original_ids(e) & anchors.keys():
            matched.add(key)
            for node in (a, b):
                if node in fixed and fixed[node] != anchors[key]:
                    raise ValueError(f'Conflicting elevation anchors at {node}')
                fixed[node] = anchors[key]
    if matched != anchors.keys():
        raise ValueError(f'Missing anchors: {anchors.keys() - matched}')
    prior_nodes = set()
    for group in profile.get('separate_groups', []):
        nodes = {n for e in edges if original_ids(e) & set(group)
                 for n in (e.get('from'), e.get('to'))}
        if nodes & prior_nodes:
            raise ValueError('Upper and lower roads share a junction')
        prior_nodes.update(nodes)
    transition_nodes = set()
    transition_radius = profile.get('transition_radius_meters')
    if transition_radius:
        distances = {node: 0.0 for node in fixed}
        queue = [(0.0, node) for node in fixed]
        heapq.heapify(queue)
        while queue:
            distance, node = heapq.heappop(queue)
            if distance != distances[node] or distance > transition_radius:
                continue
            transition_nodes.add(node)
            for neighbour, edge_length in length_graph.get(node, []):
                candidate = distance + edge_length
                if candidate <= transition_radius and candidate < distances.get(neighbour, math.inf):
                    distances[neighbour] = candidate
                    heapq.heappush(queue, (candidate, neighbour))
    allowed = set(anchors) | set(profile['transitions'])
    for e in edges:
        if not original_ids(e) & allowed:
            for node in (e.get('from'), e.get('to')):
                if node not in transition_nodes:
                    fixed.setdefault(node, 0.)
    heights = {n: fixed.get(n, 0.) for n in graph}
    free = sorted(heights.keys() - fixed.keys())
    for _ in range(10000):
        delta = 0.
        for n in free:
            z = sum(heights[k]*w for k,w in graph[n])/sum(w for _,w in graph[n])
            delta = max(delta, abs(z-heights[n])); heights[n] = z
        if delta < 1e-9:
            break
    else:
        raise ValueError('Elevation solver did not converge')
    for n in root.findall('junction'):
        z = heights.get(n.get('id'), 0.)
        n.set('z', str(z))
        if n.get('shape'):
            n.set('shape', set_height(n.get('shape'), z, z))
    grades = []
    for e in root.findall('edge'):
        if e.get('function') == 'internal':
            a = b = heights[e.get('id')[1:].rsplit('_', 1)[0]]
        else:
            a, b = heights[e.get('from')], heights[e.get('to')]
        for item in [e, *e.findall('lane')]:
            if item.get('shape'):
                item.set('shape', set_height(item.get('shape'), a, b))
        for lane in e.findall('lane'):
            pts = [tuple(map(float,p.split(','))) for p in lane.get('shape').split()]
            length = sum(math.dist(p[:2],q[:2]) for p,q in zip(pts,pts[1:]))
            if length:
                grades.append((abs(b-a)/length*100, e.get('id')))
    shapes = {l.get('id'): [tuple(map(float,p.split(','))) for p in l.get('shape').split()]
              for l in root.findall('edge/lane')}
    for c in root.findall('connection'):
        src = c.get('from')+'_'+c.get('fromLane')
        dst = c.get('via') or c.get('to')+'_'+c.get('toLane')
        if abs(shapes[src][-1][2]-shapes[dst][0][2]) > .001:
            raise ValueError(f'Vertical discontinuity {src} -> {dst}')
    return {'anchors_matched': sorted(matched), 'vertical_discontinuities': 0,
            'transition_radius_meters': transition_radius,
            'max_grade_percent': max(grades)[0],
            'steep_edges': sorted([g for g in grades if g[0] > 8], reverse=True)}


def check_expected_lanes(root, profile):
    """Verify explicitly documented lane counts without changing the network."""
    expected = profile.get('expected_lanes', {})
    if not expected:
        return {}
    matched = {way: [] for way in expected}
    for edge in root.findall('edge'):
        if edge.get('function') == 'internal':
            continue
        for way in original_ids(edge) & expected.keys():
            count = len(edge.findall('lane'))
            if count != expected[way]:
                raise ValueError(f'{profile["id"]}: {way}/{edge.get("id")} has {count} lanes, expected {expected[way]}')
            matched[way].append(edge.get('id'))
    missing = [way for way, edges in matched.items() if not edges]
    if missing:
        raise ValueError(f'{profile["id"]}: expected lane anchors missing from cutout: {missing}')
    return matched


def build(profile, source):
    out = OUT / profile['id']; out.mkdir(parents=True, exist_ok=True)
    flat = out / 'flat.net.xml'
    with (out / 'build.log').open('w') as log:
        subprocess.run([sumolib.checkBinary('netconvert'), '-s', str(source),
                        '--keep-edges.in-geo-boundary', ','.join(map(str, profile['bounds'])),
                        '-o', str(flat)], stdout=log, stderr=subprocess.STDOUT, check=True)
    tree = ET.parse(flat); root = tree.getroot()
    # Only geometry is edited: prove all lane attributes and connections survive.
    before = [(e.tag, dict(e.attrib)) for e in root.iter() if e.tag in ('lane','connection')]
    lane_checks = check_expected_lanes(root, profile)
    checks = elevate(root, profile)
    after = [(e.tag, dict(e.attrib)) for e in root.iter() if e.tag in ('lane','connection')]
    for records in (before, after):
        for tag, attrs in records:
            if tag == 'lane': attrs.pop('shape', None)
    if before != after: raise ValueError('Lane attributes or movements changed')
    target = out / 'network.net.xml'; tree.write(target, encoding='utf-8', xml_declaration=True)
    net = sumolib.net.readNet(str(target)); routes = ET.Element('routes')
    # One low-density traversal per directed anchor edge, with legal neighbours.
    for e in net.getEdges():
        xml_edge = root.find(f"edge[@id='{e.getID()}']")
        if not original_ids(xml_edge) & profile['anchors'].keys() or not e.allows('passenger'): continue
        incoming = [n for n,cs in e.getIncoming().items() if n.allows('passenger') and any(c.getFromLane().allows('passenger') and c.getToLane().allows('passenger') for c in cs)]
        outgoing = [n for n,cs in e.getOutgoing().items() if n.allows('passenger') and any(c.getFromLane().allows('passenger') and c.getToLane().allows('passenger') for c in cs)]
        path = ([incoming[0].getID()] if incoming else []) + [e.getID()] + ([outgoing[0].getID()] if outgoing else [])
        i = len(routes); v = ET.SubElement(routes, 'vehicle', id=f'probe{i}', depart=str(i*20))
        ET.SubElement(v, 'route', edges=' '.join(path))
    if not len(routes): raise ValueError('No route probes')
    ET.ElementTree(routes).write(out/'check.rou.xml')
    with (out/'simulation.log').open('w') as log:
        subprocess.run([sumolib.checkBinary('sumo'), '-n', str(target), '-r', str(out/'check.rou.xml'), '--end', '1800', '--seed','42','--time-to-teleport','-1','--no-step-log','true','--tripinfo-output',str(out/'trips.xml')], stdout=log, stderr=subprocess.STDOUT, check=True)
    trips = ET.parse(out/'trips.xml').getroot().findall('tripinfo')
    if {t.get('id') for t in trips} != {v.get('id') for v in routes}: raise ValueError('Incomplete probes')
    checks.update(completed_probes=len(trips), lane_attributes_and_connections_unchanged=True,
                  expected_lane_matches=lane_checks)
    (out/'validation.json').write_text(json.dumps(checks, indent=2))
    center = net.convertLonLat2XY(*profile['center']); lanes = []
    for e in root.findall('edge'):
        for l in e.findall('lane'):
            pts = [list(map(float,p.split(','))) for p in l.get('shape').split()]
            lanes.append(dict(id=l.get('id'), edge=e.get('id'), width=float(l.get('width','3.2')), internal=e.get('function')=='internal', points=[[p[0]-center[0],p[1]-center[1],p[2]] for p in pts]))
    zs = [p[2] for l in lanes for p in l['points']]
    junctions = []
    for node in root.findall('junction'):
        if node.get('type') == 'internal' or not node.get('shape'):
            continue
        pts = [list(map(float,p.split(','))) for p in node.get('shape').split()]
        if len(pts) >= 3:
            junctions.append([[p[0]-center[0], p[1]-center[1], p[2]] for p in pts])
    data = dict(lanes=lanes, junctions=junctions, supports=[], z_range=[min(zs), max(zs)])
    template = (BASE/'bridge_preview.html').read_text().replace('Мангилик Ел — Сарайшык',html.escape(profile['title']))
    template = template.replace('Высота моста 6,5 м — допущение.', f'Отметки уровней {min(zs):g}…{max(zs):g} м — допущение; не измеренные высоты.')
    template = template.replace('Полотно, ограждения и опоры схематичные, не по инженерным чертежам.', 'Полотно и ограждения схематичные. Опоры и конструкции тоннелей не моделируются.')
    template = template.replace('<header>', '<header><a href="../index.html">← Все мосты</a>')
    (out/'index.html').write_text(template.replace('/*NETWORK_DATA*/null', json.dumps(data)))
    config = ET.Element('configuration'); ET.SubElement(ET.SubElement(config,'input'),'net-file',value='network.net.xml')
    ET.ElementTree(config).write(out/'view.sumocfg')
    (out/'profile.json').write_text(json.dumps(profile, ensure_ascii=False, indent=2))
    return dict(id=profile['id'], title=profile['title'], **checks)


def main():
    source = BASE/'generated/astana_city.net.xml'
    profiles = json.loads((BASE/'bridge_profiles.json').read_text())['profiles']
    results = []
    for p in profiles:
        result = build(p, source); results.append(result); print(json.dumps(result, ensure_ascii=False), flush=True)
    (OUT/'manifest.json').write_text(json.dumps(dict(source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), results=results), ensure_ascii=False, indent=2))
    links = ''.join(f'<li><a href="{r["id"]}/index.html">{html.escape(r["title"])}</a> — {r["completed_probes"]} проездов; максимальный уклон {r["max_grade_percent"]:.1f}%</li>' for r in results)
    (OUT/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Мосты Астаны</title><style>body{font:18px system-ui;max-width:900px;margin:40px auto}li{margin:20px}</style><h1>Мосты и нижние проезды Астаны</h1><p>Проверочные участки SUMO. Высоты условные; полосность сохранена из исходной сети и требует натурной сверки. Рабочая карта города не заменена. Уклоны выше 8% требуют доработки.</p><ul>'+links+'</ul><a href="../index.html">Предыдущий участок Мангилик Ел — Сарайшык</a>')


if __name__ == '__main__': main()
