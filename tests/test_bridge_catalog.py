import xml.etree.ElementTree as ET
import pytest
from simulation.maps.astana_city.build_bridge_catalog import elevate, original_ids


def network():
    return ET.fromstring('''<net>
    <junction id="a"/><junction id="b"/><junction id="c"/>
    <edge id="-10" from="a" to="b"><lane id="-10_0" length="100" shape="0,0 100,0"/></edge>
    <edge id="20" from="b" to="c"><lane id="20_0" length="100" shape="100,0 200,0"/></edge>
    <connection from="-10" to="20" fromLane="0" toLane="0"/>
    </net>''')


@pytest.mark.parametrize('z', [-6.5, 6.5])
def test_signed_elevation_continuity(z):
    root = network()
    report = elevate(root, dict(anchors={'10':z}, transitions=[]))
    assert report['vertical_discontinuities'] == 0
    assert report['max_grade_percent'] == 6.5
    assert float(root.find("junction[@id='a']").get('z')) == z


def test_missing_anchor_rejected():
    with pytest.raises(ValueError, match='Missing anchors'):
        elevate(network(), dict(anchors={'999':6.5}, transitions=[]))


def test_conflicting_levels_rejected():
    with pytest.raises(ValueError, match='Conflicting'):
        elevate(network(), dict(anchors={'10':6.5,'20':-6.5}, transitions=[]))


def test_orig_ids_normalized():
    e = ET.fromstring('<edge id="-10#1"><lane><param key="origId" value="-20 30#2"/></lane></edge>')
    assert original_ids(e) == {'10','20','30'}
