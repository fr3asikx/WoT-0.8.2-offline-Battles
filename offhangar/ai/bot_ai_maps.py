# -*- coding: utf-8 -*-
"""Tactical route data for the offline bot director.

Coordinates in this file belong to the pinned 0.8.2 client, not a modern WoT
map. Himmelsdorf was traced against the original ``04_himmelsdorf.pkg``
minimap and its packed arena definition:

* bounds: (-300, -300) -> (400, 400)
* team 1 ctf base: south, approximately (-48, -303)
* team 2 ctf base: north, approximately (17, 300)

The routes are intentionally sparse macro waypoints. The existing multi-ray
driver remains responsible for local obstacle avoidance between them.
"""


HIMMELSDORF = {'name': '04_himmelsdorf', 'bounds': (-300.0, -300.0, 400.0, 400.0), 'bases': {1: (2.5, -252.6), 2: (67.1, 350.0)}, 'routes': {1: ({'capacity': 4, 'id': 'rail_west', 'max_tanks': 4, 'min_tanks': 2, 'pre_oriented': True, 'risk': 0.55, 'role_weights': {'brawler': 0.35, 'flanker': 0.88, 'scout': 0.92, 'sniper': 0.65, 'support': 0.62}, 'waypoints': ((-62.0, -286.0, 0), (-114.0, -286.0, 0), (-186.0, -278.0, 0), (-202.0, -242.0, 0), (-206.0, -178.0, 0), (-206.0, -126.0, 0), (-206.0, -74.0, 0), (-206.0, -26.0, 0), (-206.0, 30.0, 0), (-194.0, 86.0, 0), (-206.0, 142.0, 0), (-202.0, 198.0, 0), (-166.0, 254.0, 0), (-118.0, 286.0, 0), (-38.0, 314.0, 0), (38.0, 336.0, 0), (62.0, 346.0, 0))}, {'capacity': 4, 'id': 'station_street', 'max_tanks': 4, 'min_tanks': 2, 'pre_oriented': True, 'risk': 0.55, 'role_weights': {'brawler': 0.68, 'flanker': 0.82, 'scout': 0.52, 'sniper': 0.55, 'support': 0.78}, 'waypoints': ((-30.0, -282.0, 0), (-86.0, -286.0, 0), (-114.0, -282.0, 0), (-122.0, -170.0, 0), (-122.0, -126.0, 0), (-122.0, -62.0, 0), (-142.0, -6.0, 0), (-138.0, 46.0, 0), (-138.0, 102.0, 0), (-126.0, 154.0, 0), (-122.0, 194.0, 0), (-122.0, 254.0, 0), (-38.0, 314.0, 0), (38.0, 336.0, 0), (62.0, 346.0, 0))}, {'capacity': 4, 'id': 'center_square', 'max_tanks': 4, 'min_tanks': 2, 'pre_oriented': True, 'risk': 0.55, 'role_weights': {'brawler': 0.65, 'flanker': 0.78, 'scout': 0.58, 'sniper': 0.72, 'support': 0.85}, 'waypoints': ((-26.0, -266.0, 0), (-38.0, -210.0, 0), (-42.0, -146.0, 0), (-34.0, -62.0, 0), (-18.0, 16.0, 0), (-26.0, 96.0, 0), (-90.0, 100.0, 0), (-130.0, 154.0, 0), (-122.0, 246.0, 0), (-74.0, 322.0, 0), (-34.0, 322.0, 0), (38.0, 336.0, 0), (62.0, 346.0, 0))}, {'capacity': 4, 'id': 'banana', 'max_tanks': 4, 'min_tanks': 2, 'pre_oriented': True, 'risk': 0.55, 'role_weights': {'brawler': 0.96, 'flanker': 0.32, 'scout': 0.18, 'sniper': 0.45, 'support': 0.78}, 'waypoints': ((46.0, -266.0, 0), (46.0, -194.0, 0), (46.0, -122.0, 0), (46.0, -46.0, 0), (50.0, 26.0, 0), (54.0, 94.0, 0), (42.0, 166.0, 0), (46.0, 234.0, 0), (50.0, 328.0, 0), (62.0, 346.0, 0))}, {'capacity': 4, 'id': 'hill', 'max_tanks': 4, 'min_tanks': 2, 'pre_oriented': True, 'risk': 0.55, 'role_weights': {'brawler': 0.86, 'flanker': 0.94, 'scout': 0.58, 'sniper': 0.42, 'support': 0.76}, 'waypoints': ((66.0, -282.0, 0), (158.0, -286.0, 0), (266.0, -274.0, 0), (382.0, -262.0, 0), (386.0, -142.0, 0), (350.0, -38.0, 0), (326.0, 6.0, 0), (374.0, 126.0, 0), (386.0, 234.0, 0), (354.0, 302.0, 0), (246.0, 298.0, 0), (142.0, 298.0, 0), (62.0, 346.0, 0))}), 2: ({'capacity': 4, 'id': 'rail_west', 'max_tanks': 4, 'min_tanks': 2, 'pre_oriented': True, 'risk': 0.55, 'role_weights': {'brawler': 0.35, 'flanker': 0.88, 'scout': 0.92, 'sniper': 0.65, 'support': 0.62}, 'waypoints': ((14.0, 322.0, 0), (-38.0, 314.0, 0), (-118.0, 286.0, 0), (-166.0, 254.0, 0), (-202.0, 198.0, 0), (-206.0, 142.0, 0), (-194.0, 86.0, 0), (-206.0, 30.0, 0), (-206.0, -26.0, 0), (-206.0, -74.0, 0), (-206.0, -126.0, 0), (-206.0, -178.0, 0), (-202.0, -242.0, 0), (-186.0, -278.0, 0), (-114.0, -282.0, 0), (2.5, -252.6, 0))}, {'capacity': 4, 'id': 'station_street', 'max_tanks': 4, 'min_tanks': 2, 'pre_oriented': True, 'risk': 0.55, 'role_weights': {'brawler': 0.68, 'flanker': 0.82, 'scout': 0.52, 'sniper': 0.55, 'support': 0.78}, 'waypoints': ((14.0, 322.0, 0), (-38.0, 314.0, 0), (-122.0, 254.0, 0), (-122.0, 194.0, 0), (-126.0, 154.0, 0), (-138.0, 102.0, 0), (-138.0, 46.0, 0), (-142.0, -6.0, 0), (-122.0, -62.0, 0), (-122.0, -126.0, 0), (-114.0, -170.0, 0), (2.5, -252.6, 0))}, {'capacity': 4, 'id': 'center_square', 'max_tanks': 4, 'min_tanks': 2, 'pre_oriented': True, 'risk': 0.55, 'role_weights': {'brawler': 0.65, 'flanker': 0.78, 'scout': 0.58, 'sniper': 0.72, 'support': 0.85}, 'waypoints': ((62.0, 346.0, 0), (38.0, 336.0, 0), (-34.0, 322.0, 0), (-74.0, 322.0, 0), (-122.0, 246.0, 0), (-130.0, 154.0, 0), (-90.0, 100.0, 0), (-26.0, 96.0, 0), (-18.0, 16.0, 0), (-34.0, -62.0, 0), (-42.0, -146.0, 0), (-38.0, -210.0, 0), (-6.0, -252.0, 0))}, {'capacity': 4, 'id': 'banana', 'max_tanks': 4, 'min_tanks': 2, 'pre_oriented': True, 'risk': 0.55, 'role_weights': {'brawler': 0.96, 'flanker': 0.32, 'scout': 0.18, 'sniper': 0.45, 'support': 0.78}, 'waypoints': ((62.0, 346.0, 0), (50.0, 328.0, 0), (46.0, 234.0, 0), (42.0, 166.0, 0), (54.0, 94.0, 0), (50.0, 26.0, 0), (46.0, -46.0, 0), (46.0, -122.0, 0), (46.0, -194.0, 0), (14.0, -250.0, 0))}, {'capacity': 4, 'id': 'hill', 'max_tanks': 4, 'min_tanks': 2, 'pre_oriented': True, 'risk': 0.55, 'role_weights': {'brawler': 0.86, 'flanker': 0.94, 'scout': 0.58, 'sniper': 0.42, 'support': 0.76}, 'waypoints': ((50.0, 322.0, 0), (142.0, 298.0, 0), (246.0, 298.0, 0), (354.0, 302.0, 0), (386.0, 234.0, 0), (374.0, 126.0, 0), (326.0, 6.0, 0), (350.0, -38.0, 0), (386.0, -142.0, 0), (382.0, -262.0, 0), (266.0, -274.0, 0), (158.0, -286.0, 0), (66.0, -282.0, 0), (-34.0, -222.0, 0), (2.5, -252.6, 0))})}}

TACTICAL_MAPS = {
	'04_himmelsdorf': HIMMELSDORF,
}

# The stock 0.8.2 map set is split into data-only modules so this registry
# stays reviewable. In the legacy arena data, "ctf" names the normal
# base-capture battle type; assault and encounter variants are not annotated.
from gui.mods.offhangar import bot_ai_maps_group_a
from gui.mods.offhangar import bot_ai_maps_group_b
from gui.mods.offhangar import bot_ai_maps_group_c
from gui.mods.offhangar import bot_ai_maps_extra

TACTICAL_MAPS.update(bot_ai_maps_group_a.TACTICAL_MAPS_GROUP_A)
TACTICAL_MAPS.update(bot_ai_maps_group_b.TACTICAL_MAPS_GROUP_B)
TACTICAL_MAPS.update(bot_ai_maps_group_c.TACTICAL_MAPS_GROUP_C)
TACTICAL_MAPS.update(bot_ai_maps_extra.TACTICAL_MAPS_EXTRA)


def normalize_map_name(map_name):
	name = str(map_name or '').replace('\\', '/').split('/')[-1]
	if name.endswith('.xml'):
		name = name[:-4]
	return name.lower()


def get_tactical_map(map_name):
	return TACTICAL_MAPS.get(normalize_map_name(map_name))
