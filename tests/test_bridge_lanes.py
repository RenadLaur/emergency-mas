import xml.etree.ElementTree as ET
import pytest
from simulation.maps.astana_city.check_bridge_lanes import check


def test_bidirectional_total_not_applied_per_direction():
    groups = [dict(ways=['10'], tags={'lanes':'6','lanes:forward':'3','lanes:backward':'3'},status='source_supported')]
    root = ET.fromstring('<net><edge id="10"><lane/><lane/><lane/></edge><edge id="-10"><lane/><lane/><lane/></edge></net>')
    assert len(check(root, groups)['10']) == 2
    root.find('edge').remove(root.find('edge/lane'))
    with pytest.raises(ValueError, match='expected 3'): check(root, groups)


def test_absent_way_is_not_silent_success():
    with pytest.raises(ValueError, match='Missing'):
        check(ET.fromstring('<net/>'), [dict(ways=['10'],tags={'lanes':'3'},status='inferred_continuity')])
