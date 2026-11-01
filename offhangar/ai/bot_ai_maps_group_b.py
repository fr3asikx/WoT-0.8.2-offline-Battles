# -*- coding: utf-8 -*-
"""Sparse tactical corridors traced from the original 0.8.2 minimaps.

The packed ``arena_defs`` files supplied the bounds and CTF base positions.
Waypoints follow visible roads, open banks, and map edges; this is deliberately
macro data, leaving the live multi-ray driver to make local collision choices.
"""


def _weights(brawler, support, flanker, sniper, scout, artillery):
	return {
		'brawler': brawler, 'support': support, 'flanker': flanker,
		'sniper': sniper, 'scout': scout, 'artillery': artillery,
	}


def _route(route_id, capacity, risk, weights, waypoints,
		terminal_hold=False):
	result = {
		'id': route_id,
		'capacity': capacity,
		'risk': risk,
		'role_weights': weights,
		'hold': waypoints[-1],
		'waypoints': tuple(waypoints),
	}
	if terminal_hold:
		result['terminal_hold'] = True
	return result


def _reverse_routes(routes):
	"""Return independent routes for the opposite CTF base (Python 2.6)."""
	result = []
	for route in routes:
		copy = dict(route)
		copy['role_weights'] = dict(route['role_weights'])
		copy['waypoints'] = tuple(reversed(route['waypoints']))
		copy['hold'] = copy['waypoints'][-1]
		result.append(copy)
	return tuple(result)


def _map(name, bounds, base1, base2, routes, dry_only=False, routes2=None):
	team1 = tuple(routes)
	team2 = (tuple(routes2) if routes2 is not None else
	         _reverse_routes(team1))
	return {
		'name': name,
		'bounds': bounds,
		'bases': {1: base1, 2: base2},
		'routes': {1: team1, 2: team2},
		'dry_only': bool(dry_only),
	}


# 15_komarin: north and south are through lanes.  The centre is a contested
# position reached from different bridge approaches, not another perimeter
# route forced all the way to the opposite flag.
_KOMARIN_TEAM1 = (
		_route('east_flank', 5, 0.58, _weights(.90, .75, .70, .35, .55, .00),
			((60, -340, 0), (160, -274, 0), (262, -146, 1), (262, -6, 1), (278, 126, 0), (283, 168, 0))),
		_route('forest_overwatch', 4, 0.52, _weights(.55, .85, .65, .95, .75, .10),
			((60, -340, 0), (66, -118, 1), (178, -98, 1), (234, -2, 1), (274, 42, 0), (283, 168, 0))),
		_route('west_flank', 4, 0.62, _weights(.85, .70, .85, .40, .60, .00),
			((60, -340, 0), (-126, -358, 0), (-266, -330, 0), (-294, -194, 1), (-318, 62, 1), (-186, 238, 0), (110, 286, 0), (283, 168, 0))),
		_route('center_island', 3, 0.72, _weights(.75, .60, .90, .45, .85, .00),
			((60, -340, 0), (-42, -146, 1), (26, -130, 1), (178, -98, 1), (274, 42, 0), (283, 168, 0))),
)
_KOMARIN_TEAM2 = (
		_route('west_flank', 5, 0.58, _weights(.90, .75, .70, .35, .55, .00),
			((-107, 342, 0), (-222, 210, 0), (-294, 86, 1), (-318, 62, 1), (-326, -78, 0), (-281, -192, 0))),
		_route('forest_overwatch', 4, 0.52, _weights(.55, .85, .65, .95, .75, .10),
			((-107, 342, 0), (-82, 238, 1), (-194, 230, 1), (-234, 126, 1), (-314, 38, 0), (-294, -126, 0), (-281, -192, 0))),
		_route('east_flank', 4, 0.62, _weights(.85, .70, .85, .40, .60, .00),
			((-107, 342, 0), (110, 286, 0), (278, 210, 0), (278, 166, 1), (262, -6, 1), (158, -274, 0), (-126, -358, 0), (-281, -192, 0))),
		_route('center_island', 3, 0.72, _weights(.75, .60, .90, .45, .85, .00),
			((-107, 342, 0), (-82, 238, 1), (-18, 182, 1), (18, 118, 1), (-18, 182, 0), (-234, 126, 0), (-318, 62, 0), (-281, -192, 0))),
)
KOMARIN = _map('15_komarin', (-400.0, -400.0, 400.0, 400.0),
	(-280.772, -192.392), (282.752, 167.894), _KOMARIN_TEAM1,
	dry_only=True, routes2=_KOMARIN_TEAM2)
del _KOMARIN_TEAM1
del _KOMARIN_TEAM2


MUNCHEN = _map('17_munchen', (-300.0, -300.0, 300.0, 300.0),
	(-83.7, -201.7), (65.3, 220.5), (
		_route('west_streets', 5, 0.61, _weights(.90, .65, .30, .12, .22, .00),
			((-80, -190, 0), (-155, -165, 0), (-210, -85, 1), (-210, 10, 1), (-165, 105, 1), (-55, 195, 0))),
		_route('east_rail', 4, 0.48, _weights(.25, .72, .65, 1.00, .62, .12),
			((-84, -202, 0), (35, -225, 0), (140, -208, 0), (205, -100, 0), (267, 8, 1), (264, 133, 0), (190, 225, 0), (65, 221, 0))),
		_route('center_blocks', 3, 0.78, _weights(.75, 1.00, .42, .22, .30, .00),
			((-80, -175, 0), (-60, -105, 1), (-35, -40, 1), (-5, 35, 1), (30, 105, 1), (60, 185, 0))),
	))


CLIFF = _map('18_cliff', (-500.0, -500.0, 500.0, 500.0),
	(-287.4, -436.6), (-251.6, 434.6), (
		_route('west_coast', 4, 0.58, _weights(.55, .88, .60, .72, .48, .08),
			((-290, -420, 0), (-400, -330, 0), (-430, -175, 1), (-410, 0, 1), (-365, 185, 1), (-285, 390, 0))),
		_route('central_road', 5, 0.73, _weights(.92, .70, .35, .15, .22, .00),
			((-280, -420, 0), (-245, -285, 0), (-190, -135, 1), (-145, 20, 1), (-175, 170, 1), (-240, 385, 0))),
		_route('east_ridge', 3, 0.76, _weights(.32, .68, 1.00, .66, .90, .00),
			((-287, -437, 0), (-100, -346, 0), (77, -212, 0), (260, -90, 0), (313, 96, 1), (120, 205, 0), (-73, 313, 0), (-252, 435, 0))),
	))


MONASTERY = _map('19_monastery', (-500.0, -500.0, 500.0, 500.0),
	(20.1, -387.9), (-0.4, 397.4), (
		_route('west_field', 4, 0.51, _weights(.48, .78, .64, .86, .54, .10),
			((15, -370, 0), (-155, -310, 0), (-300, -190, 0), (-350, 0, 1), (-295, 185, 1), (-80, 345, 0))),
		_route('monastery_lane', 5, 0.79, _weights(1.00, .72, .28, .10, .15, .00),
			((20, -370, 0), (0, -250, 0), (-20, -115, 1), (-15, 25, 1), (-20, 170, 1), (-5, 350, 0))),
		_route('east_hills', 3, 0.70, _weights(.34, .68, 1.00, .76, .88, .00),
			((35, -370, 0), (185, -295, 0), (315, -145, 1), (300, 30, 1), (205, 185, 1), (55, 350, 0))),
	))


SLOUGH = _map('22_slough', (-500.0, -500.0, 500.0, 500.0),
	(-403.7, -424.1), (383.3, 422.8), (
		_route('west_ridge', 4, 0.57, _weights(.48, .78, .72, .82, .50, .10),
			((-385, -405, 0), (-445, -255, 0), (-420, -55, 1), (-315, 135, 1), (-105, 315, 1), (345, 400, 0))),
		_route('middle_low', 5, 0.72, _weights(.95, .76, .36, .15, .22, .00),
			((-385, -405, 0), (-285, -285, 0), (-150, -135, 1), (0, 10, 1), (145, 155, 1), (350, 400, 0))),
		_route('east_ridge', 3, 0.69, _weights(.35, .65, 1.00, .70, .94, .00),
			((-404, -424, 0), (-342, -416, 0), (-242, -434, 0),
			 (-66, -424, 0), (258, -315, 1), (326, -266, 0),
			 (390, -166, 0), (390, 0, 0), (330, 170, 0),
			 (332, 298, 0), (383, 423, 0))),
	))


_WESTFELD_TEAM1 = (
		_route('north_ridge', 3, 0.67, _weights(.34, .75, 1.00, .82, .82, .00),
			((-300, -340, 0), (-377, -186, 0), (-356, -20, 0),
			 (-281, 135, 0), (-164, 258, 0), (-50, 308, 0),
			 (0, 400, 0), (150, 450, 1), (280, 420, 0),
			 (339, 300, 0))),
		# The village sits on two shelves separated by a steep transition.
		# Each team advances to its own fighting entrance instead of following
		# a base-to-base polyline that doubles back across the cliff.
		_route('central_village', 5, 0.75, _weights(.95, .72, .34, .12, .20, .00),
			((-300, -340, 0), (-220, -280, 0), (-130, -190, 0),
			 (-40, -95, 0), (60, 10, 0), (90, 100, 0),
			 (90, 202, 0), (78, 226, 1)), True),
		_route('east_fields', 4, 0.50, _weights(.45, .88, .58, 1.00, .46, .16),
			((-300, -340, 0), (-110, -415, 0), (90, -449, 0), (291, -417, 0), (419, -288, 0), (446, -87, 1), (424, 114, 0), (339, 300, 0))),
)
_WESTFELD_TEAM2 = (
	_reverse_routes((_WESTFELD_TEAM1[0],))[0],
	_route('central_village', 5, 0.75,
		_weights(.95, .72, .34, .12, .20, .00),
		((339, 300, 0), (262, 234, 0), (230, 146, 0),
		 (174, 130, 1)), True),
	_reverse_routes((_WESTFELD_TEAM1[2],))[0],
)
WESTFELD = _map('23_westfeld', (-500.0, -500.0, 500.0, 500.0),
	(-300.1, -339.6), (339.4, 299.8), _WESTFELD_TEAM1,
	routes2=_WESTFELD_TEAM2)
del _WESTFELD_TEAM1
del _WESTFELD_TEAM2


DESERT = _map('28_desert', (-500.0, -500.0, 500.0, 500.0),
	(373.4855, -178.9612), (-405.0387, 137.5266), (
		_route('north_dunes', 3, 0.69, _weights(.30, .70, 1.00, .76, .92, .00),
			((373, -179, 0), (260, 50, 0), (150, 180, 1), (0, 280, 1), (-160, 270, 1), (-300, 210, 0), (-405, 138, 0))),
		_route('village_road', 5, 0.78, _weights(.92, .78, .36, .14, .24, .00),
			((373, -179, 0), (280, -160, 0), (180, -80, 0), (70, 0, 1), (-60, 70, 1), (-200, 120, 1), (-320, 140, 0), (-405, 138, 0))),
		_route('south_rocks', 4, 0.56, _weights(.50, .86, .60, 1.00, .46, .12),
			((373, -179, 0), (330, -220, 0), (270, -300, 0), (230, -340, 0), (80, -390, 1), (-60, -390, 1), (-190, -310, 1), (-270, -190, 0), (-330, -40, 0), (-405, 138, 0))),
	))


_EL_HALLOUF_TEAM1 = (
		# The eastern road is a hill fight.  It is not a second copy of the
		# central bowl route, so each team approaches a separate side of the
		# ridge and holds there for contact.
		_route('south_valley', 5, 0.70, _weights(.94, .78, .38, .16, .20, .00),
			((299, 319, 0), (350, 230, 0), (370, 102, 1)), True),
		_route('central_bowl', 4, 0.77, _weights(.76, .92, .48, .36, .42, .00),
			((299, 319, 0), (170, 180, 0), (75, 35, 0), (-25, -90, 1), (-155, -225, 0), (-339, -319, 0))),
		_route('north_ridge', 3, 0.65, _weights(.30, .70, 1.00, .92, .88, .00),
			((299, 319, 0), (100, 370, 0), (-150, 350, 0), (-380, 300, 0), (-450, 100, 1), (-420, -150, 0), (-339, -319, 0))),
)
_EL_HALLOUF_TEAM2 = (
	_route('south_valley', 5, 0.70,
		_weights(.94, .78, .38, .16, .20, .00),
		((-339, -319, 0), (-220, -300, 0), (-80, -250, 0),
		 (50, -150, 0), (100, 0, 0), (200, 120, 0),
		 (266, 190, 1)), True),
	_reverse_routes((_EL_HALLOUF_TEAM1[1],))[0],
	_reverse_routes((_EL_HALLOUF_TEAM1[2],))[0],
)
EL_HALLOUF = _map('29_el_hallouf', (-500.0, -500.0, 500.0, 500.0),
	(299.256, 319.406), (-338.5832, -319.3074), _EL_HALLOUF_TEAM1,
	routes2=_EL_HALLOUF_TEAM2)
del _EL_HALLOUF_TEAM1
del _EL_HALLOUF_TEAM2


FJORD = _map('33_fjord', (-500.0, -500.0, 500.0, 500.0),
	(399.1, -42.1), (-381.3, 111.4), (
		_route('north_ridge', 3, 0.66, _weights(.32, .70, 1.00, .88, .86, .00),
			((399, -42, 0), (400, 145, 0), (418, 343, 0), (250, 405, 0), (70, 389, 1), (-115, 404, 0), (-287, 287, 0), (-381, 111, 0))),
		_route('middle_village', 5, 0.80, _weights(.96, .78, .32, .14, .18, .00),
			((399, -42, 0), (186, -39, 0), (22, 95, 0), (-120, 170, 1), (-226, 231, 0), (-326, 138, 0), (-381, 111, 0))),
		_route('south_coast', 4, 0.55, _weights(.46, .88, .62, 1.00, .42, .14),
			((399, -42, 0), (260, -130, 0), (80, -130, 0), (0, -230, 0), (-80, -350, 0), (-150, -350, 1), (-190, -230, 0), (-240, -80, 0), (-381, 111, 0))),
	))


TACTICAL_MAPS_GROUP_B = {
	'15_komarin': KOMARIN,
	'17_munchen': MUNCHEN,
	'18_cliff': CLIFF,
	'19_monastery': MONASTERY,
	'22_slough': SLOUGH,
	'23_westfeld': WESTFELD,
	'28_desert': DESERT,
	'29_el_hallouf': EL_HALLOUF,
	'33_fjord': FJORD,
}
