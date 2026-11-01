# -*- coding: utf-8 -*-
"""Conservative macro routes traced from the stock 0.8.2 minimaps.

This is deliberately data-only.  Coordinates and bases were read from each
packed ``res/scripts/arena_defs/*.xml`` and checked against the corresponding
``spaces/<map>/mmap.dds`` in the stock package.  A waypoint's third value is
the bot director's existing hold flag.
"""


_WEIGHTS = {
	'flank': {'brawler': 0.42, 'support': 0.64, 'flanker': 1.00,
		'sniper': 0.44, 'scout': 0.78, 'artillery': 0.08},
	'line': {'brawler': 1.00, 'support': 0.62, 'flanker': 0.38,
		'sniper': 0.16, 'scout': 0.28, 'artillery': 0.00},
	'overwatch': {'brawler': 0.22, 'support': 0.92, 'flanker': 0.55,
		'sniper': 1.00, 'scout': 0.52, 'artillery': 0.20},
}


def _route(route_id, kind, capacity, risk, waypoints):
	return {
		'id': route_id,
		'capacity': capacity,
		'risk': risk,
		'role_weights': dict(_WEIGHTS[kind]),
		'waypoints': tuple(waypoints),
	}


def _map(name, bounds, base1, base2, routes):
	"""Make both directions; every source route starts at team 1's base."""
	calibrated_routes = []
	for route in routes:
		calibrated = dict(route)
		calibrated['role_weights'] = dict(route['role_weights'])
		waypoints = list(route['waypoints'])
		waypoints[0] = (base1[0], base1[1], 0)
		waypoints[-1] = (base2[0], base2[1], 0)
		calibrated['waypoints'] = tuple(waypoints)
		calibrated_routes.append(calibrated)
	reversed_routes = []
	for route in calibrated_routes:
		other = dict(route)
		other['role_weights'] = dict(route['role_weights'])
		other['waypoints'] = tuple(reversed(route['waypoints']))
		reversed_routes.append(other)
	return {
		'name': name,
		'bounds': bounds,
		'bases': {1: base1, 2: base2},
		'routes': {1: tuple(calibrated_routes), 2: tuple(reversed_routes)},
	}


# Redshire: the river and the built-up south-east make the bends essential.
REDSHIRE = _map('34_redshire', (-500.0, -500.0, 500.0, 500.0),
	(368.69, -269.52), (-209.86, 368.25), (
	_route('east_ridge', 'overwatch', 4, 0.57, ((370, -270, 0), (355, -145, 0), (292, -58, 1), (205, 35, 1), (90, 146, 1), (-85, 286, 0), (-210, 368, 0))),
	_route('river_town', 'line', 5, 0.72, ((370, -270, 0), (270, -245, 0), (172, -174, 1), (105, -88, 1), (23, 8, 1), (-84, 112, 1), (-165, 250, 0), (-210, 368, 0))),
	_route('west_fields', 'flank', 4, 0.49, ((370, -270, 0), (246, -340, 0), (88, -366, 0), (-92, -300, 1), (-250, -155, 1), (-325, 32, 1), (-280, 218, 0), (-210, 368, 0))),
))

STEPPES = _map('35_steppes', (-500.0, -500.0, 500.0, 500.0),
	(228.22, -341.93), (-88.82, 361.86), (
	_route('east_ridge', 'overwatch', 4, 0.55, ((228, -342, 0), (362, -264, 0), (394, -97, 1), (389, 78, 0), (362, 249, 0), (258, 377, 0), (83, 381, 0), (-89, 362, 0))),
	_route('central_hollow', 'line', 5, 0.74, ((228, -342, 0), (75, -270, 0), (23, -102, 0), (-108, 50, 1), (-130, 174, 0), (-89, 362, 0))),
	_route('west_rocks', 'flank', 4, 0.52, ((228, -342, 0), (45, -377, 0), (-142, -364, 0), (-289, -252, 0), (-357, -86, 1), (-336, 99, 0), (-234, 249, 0), (-89, 362, 0))),
))

FISHING_BAY = _map('36_fishing_bay', (-500.0, -500.0, 500.0, 500.0),
	(-84.83, 397.81), (-17.02, -396.11), (
	_route('west_fields', 'flank', 5, 0.51, ((-85, 398, 0), (-285, 382, 0), (-435, 275, 0), (-421, 77, 0), (-450, -121, 1), (-405, -308, 0), (-217, -373, 0), (-17, -396, 0))),
	_route('central_road', 'line', 5, 0.69, ((-85, 398, 0), (-72, 265, 0), (-62, 120, 1), (-48, -28, 1), (-38, -170, 1), (-17, -396, 0))),
	_route('harbor_edge', 'overwatch', 3, 0.63, ((-85, 398, 0), (72, 341, 0), (216, 254, 0), (251, 101, 0), (295, -60, 1), (263, -220, 0), (137, -327, 0), (-17, -396, 0))),
))

CAUCASUS = _map('37_caucasus', (-500.0, -500.0, 500.0, 500.0),
	(-376.74, 371.36), (345.80, -399.46), (
	_route('west_pass', 'line', 5, 0.71, ((-377, 371, 0), (-300, 183, 0), (-367, -27, 0), (-421, -244, 1), (-254, -362, 0), (-42, -284, 0), (163, -259, 0), (346, -399, 0))),
	_route('central_basin', 'overwatch', 4, 0.61, ((-377, 371, 0), (-304, 247, 0), (-137, 242, 0), (-18, 127, 1), (114, 24, 0), (139, -141, 0), (229, -280, 0), (346, -399, 0))),
	_route('east_road', 'flank', 3, 0.58, ((-377, 371, 0), (-191, 420, 0), (1, 408, 0), (117, 255, 0), (261, 129, 0), (372, -21, 1), (385, -210, 0), (346, -399, 0))),
))

MANNERHEIM = _map('38_mannerheim_line', (-500.0, -500.0, 500.0, 500.0),
	(398.14, 293.87), (-338.18, -306.26), (
	_route('east_ridge', 'overwatch', 4, 0.56, ((398, 294, 0), (388, -70, 0), (357, -146, 0), (286, -224, 1), (190, -266, 0), (-10, -290, 0), (-130, -282, 0), (-338, -306, 0))),
	_route('central_gorge', 'line', 5, 0.76, ((398, 294, 0), (352, 150, 0), (247, 43, 0), (154, -72, 1), (83, -203, 0), (-54, -254, 0), (-205, -241, 0), (-338, -306, 0))),
	_route('west_lakeside', 'flank', 3, 0.64, ((398, 294, 0), (202, 383, 0), (-6, 432, 0), (-196, 347, 1), (-386, 278, 0), (-364, 94, 0), (-298, -108, 0), (-338, -306, 0))),
))

CRIMEA = _map('39_crimea', (-500.0, -500.0, 500.0, 500.0),
	(106.30, -402.54), (114.69, 350.56), (
	_route('west_coast', 'flank', 4, 0.55, ((106, -403, 0), (12, -255, 0), (-95, -119, 0), (-168, 30, 1), (-173, 201, 0), (-177, 371, 0), (-45, 424, 0), (115, 351, 0))),
	_route('central_village', 'line', 5, 0.73, ((106, -403, 0), (85, -242, 0), (17, -70, 0), (2, 82, 1), (41, 178, 0), (111, 266, 0), (115, 351, 0))),
	_route('east_hills', 'overwatch', 4, 0.59, ((106, -403, 0), (248, -344, 0), (336, -194, 0), (410, -35, 1), (450, 130, 0), (405, 298, 0), (281, 399, 0), (115, 351, 0))),
))

PORT = _map('42_north_america', (-400.0, -432.0, 432.0, 400.0),
	(-191.10, -315.20), (318.00, 286.30), (
	_route('west_city', 'line', 5, 0.74, ((-191, -315, 0), (-248, -162, 0), (-213, -15, 1), (-128, 96, 1), (12, 175, 1), (176, 249, 0), (318, 286, 0))),
	_route('rail_yard', 'overwatch', 4, 0.58, ((-191, -315, 0), (-88, -272, 0), (28, -201, 1), (137, -103, 1), (196, 31, 1), (252, 174, 1), (318, 286, 0))),
	_route('east_city', 'flank', 4, 0.67, ((-191, -315, 0), (-65, -358, 0), (92, -328, 0), (198, -236, 1), (270, -99, 1), (310, 72, 1), (318, 286, 0))),
))

LIVE_OAKS = _map('44_north_america', (-500.0, -500.0, 500.0, 500.0),
	(-356.99, -329.81), (300.19, 363.93), (
	_route('west_town', 'line', 5, 0.73, ((-357, -330, 0), (-386, -115, 0), (-386, 103, 0), (-401, 320, 1), (-289, 444, 0), (-71, 435, 0), (146, 445, 0), (300, 364, 0))),
	_route('east_valley', 'flank', 4, 0.65, ((-357, -330, 0), (-179, -423, 0), (28, -395, 0), (215, -298, 0), (383, -170, 1), (428, 30, 0), (423, 241, 0), (300, 364, 0))),
	_route('lake_north_edge', 'overwatch', 3, 0.52, ((-357, -330, 0), (-340, -234, 0), (-270, 14, 0), (-138, 262, 1), (-78, 315, 0), (-30, 332, 0), (300, 364, 0))),
))

HIGHWAY = _map('45_north_america', (-500.0, -500.0, 500.0, 500.0),
	(197.41, 356.58), (-343.15, -327.37), (
	_route('north_road', 'overwatch', 4, 0.55, ((197, 357, 0), (34, 376, 0), (-103, 290, 0), (-259, 260, 0), (-366, 162, 1), (-378, -1, 0), (-368, -165, 0), (-343, -327, 0))),
	_route('river_crossing', 'line', 5, 0.76, ((197, 357, 0), (128, 210, 0), (-24, 46, 1), (-146, -122, 0), (-343, -327, 0))),
	_route('south_town', 'flank', 4, 0.63, ((197, 357, 0), (394, 249, 0), (418, 15, 0), (438, -218, 1), (340, -387, 0), (115, -326, 0), (-117, -358, 0), (-343, -327, 0))),
))

CANADA_A = _map('47_canada_a', (-500.0, -500.0, 500.0, 500.0),
	(-126.89, -305.91), (213.12, 328.11), (
	_route('west_hills', 'flank', 4, 0.61, ((-127, -306, 0), (-276, -196, 0), (-334, -17, 0), (-361, 170, 1), (-251, 318, 0), (-122, 449, 0), (58, 437, 0), (213, 328, 0))),
	_route('central_road', 'line', 5, 0.70, ((-127, -306, 0), (-197, -214, 0), (-206, -90, 0), (-95, 202, 1), (-50, 255, 0), (38, 310, 0), (142, 334, 0), (213, 328, 0))),
	_route('east_shore', 'overwatch', 3, 0.54, ((-127, -306, 0), (-43, -229, 0), (-63, -95, 0), (-53, 36, 0), (69, 81, 1), (198, 112, 0), (278, 213, 0), (213, 328, 0))),
))

ASIA = _map('51_asia', (-500.0, -500.0, 500.0, 500.0),
	(115.59, -387.34), (69.85, 348.81), (
	_route('west_terraces', 'flank', 4, 0.60, ((116, -387, 0), (-28, -355, 0), (-160, -270, 1), (-246, -124, 1), (-207, 42, 1), (-93, 196, 0), (70, 349, 0))),
	_route('central_village', 'line', 5, 0.75, ((116, -387, 0), (106, -247, 0), (93, -108, 1), (83, 31, 1), (78, 183, 1), (70, 349, 0))),
	_route('east_ridge', 'overwatch', 4, 0.58, ((116, -387, 0), (248, -318, 0), (315, -176, 1), (295, -27, 1), (230, 115, 1), (153, 246, 0), (70, 349, 0))),
))


TACTICAL_MAPS_GROUP_C = {
	'34_redshire': REDSHIRE,
	'35_steppes': STEPPES,
	'36_fishing_bay': FISHING_BAY,
	'37_caucasus': CAUCASUS,
	'38_mannerheim_line': MANNERHEIM,
	'39_crimea': CRIMEA,
	'42_north_america': PORT,
	'44_north_america': LIVE_OAKS,
	'45_north_america': HIGHWAY,
	'47_canada_a': CANADA_A,
	'51_asia': ASIA,
}
