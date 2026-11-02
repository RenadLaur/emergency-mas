"""Check explicit bridge lane corrections against the compiled city network."""
import json
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = Path(__file__).resolve().parent


def check(root, groups):
    expected = {way: (int(g['tags'].get('lanes:forward', g['tags']['lanes'])), g['status'])
                for g in groups for way in g['ways']}
    matches = {way: [] for way in expected}
    for edge in root.findall('edge'):
        if edge.get('function') == 'internal': continue
        ids = {edge.get('id').split('#')[0].lstrip('-')}
        for p in edge.findall(".//param[@key='origId']"):
            ids.update(i.split('#')[0].lstrip('-') for i in p.get('value').split())
        for way in ids & expected.keys():
            count = len(edge.findall('lane'))
            if count != expected[way][0]:
                raise ValueError(f'{way}: {edge.get("id")} has {count}, expected {expected[way][0]}')
            matches[way].append(edge.get('id'))
    if any(not edges for edges in matches.values()):
        raise ValueError(f'Missing corrected ways: {[w for w,e in matches.items() if not e]}')
    return matches


if __name__ == '__main__':
    groups = json.loads((BASE/'bridge_lane_overrides.json').read_text())['groups']
    matches = check(ET.parse(BASE/'generated/astana_city.net.xml').getroot(), groups)
    (BASE/'generated/bridge_lane_validation.json').write_text(json.dumps(matches, indent=2))
    print(f'Passed: {len(matches)} ways, {sum(map(len,matches.values()))} directed segments')
