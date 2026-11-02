"""Road-tile merges must preserve topology and restrictions across tile seams."""
import xml.etree.ElementTree as ET

import pytest

from simulation.maps.astana_city.build_city import merge_roads


def test_merge_deduplicates_versions_and_preserves_cross_tile_restriction(tmp_path):
    a = tmp_path / "a.xml"
    b = tmp_path / "b.xml"
    a.write_text('''<osm><node id="1" lat="51" lon="71"/><node id="2" lat="51" lon="72"/>
      <way id="10" version="1"><nd ref="1"/><nd ref="2"/><tag k="highway" v="primary"/></way>
      <relation id="100"><member type="way" ref="10" role="from"/>
      <member type="node" ref="2" role="via"/><member type="way" ref="11" role="to"/>
      <tag k="type" v="restriction"/><tag k="restriction" v="no_left_turn"/></relation></osm>''')
    b.write_text('''<osm><node id="1" lat="51" lon="71"/><node id="2" lat="51" lon="72"/>
      <node id="3" lat="52" lon="72"/>
      <way id="10" version="2"><nd ref="1"/><nd ref="2"/><tag k="highway" v="primary"/>
      <tag k="lanes" v="4"/></way>
      <way id="11"><nd ref="2"/><nd ref="3"/><tag k="highway" v="primary"/></way></osm>''')
    root, report = merge_roads([a, b])
    assert report == {"road_ways": 2, "nodes": 3, "restrictions": 1,
                      "incomplete_restriction_ids": []}
    assert root.find("way[@id='10']").get("version") == "2"
    assert [x.tag for x in root] == ["node", "node", "node", "way", "way", "relation"]
    assert ET.tostring(root) == ET.tostring(merge_roads([b, a])[0])


def test_merge_rejects_missing_geometry_node(tmp_path):
    tile = tmp_path / "bad.xml"
    tile.write_text('''<osm><way id="10"><nd ref="1"/>
      <tag k="highway" v="primary"/></way></osm>''')
    with pytest.raises(ValueError, match="Incomplete road"):
        merge_roads([tile])
