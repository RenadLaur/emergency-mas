from simulation.maps.astana_city.build_bridge_preview import set_height, distance_to_segment


def test_height_profile_uses_distance_not_vertex_index():
    points = [list(map(float,p.split(','))) for p in set_height('0,0 1,0 10,0',0,5).split()]
    assert [p[2] for p in points] == [0,.5,5]


def test_height_preserves_flat_deck_and_replaces_old_z():
    assert set_height('0,0,99 10,0,-20',6.5,6.5) == '0.0,0.0,6.5000 10.0,0.0,6.5000'


def test_support_clearance_distance_clamps_to_segment():
    assert distance_to_segment([5,3],[0,0],[10,0]) == 3
    assert distance_to_segment([12,0],[0,0],[10,0]) == 2
