# -*- coding: utf-8 -*-
"""Conservative macro corridors traced from the original 0.8.2 minimaps.

Each route is intentionally only a chain of road, field, or open-valley
anchors.  The local multi-ray driver still owns collision avoidance.
"""


_WEIGHTS = {
	'brawl': {'brawler': 1.00, 'support': 0.60, 'flanker': 0.30,
		'sniper': 0.12, 'scout': 0.18, 'artillery': 0.00},
	'flank': {'brawler': 0.26, 'support': 0.62, 'flanker': 1.00,
		'sniper': 0.40, 'scout': 0.82, 'artillery': 0.00},
	'fire': {'brawler': 0.16, 'support': 0.82, 'flanker': 0.52,
		'sniper': 1.00, 'scout': 0.64, 'artillery': 0.12},
}


def _route(route_id, kind, capacity, risk, waypoints, terminal_hold=False):
	"""Make a route with independent weights for the director's callers."""
	result = {
		'id': route_id,
		'capacity': capacity,
		'risk': risk,
		'hold': True,
		'role_weights': dict(_WEIGHTS[kind]),
		'waypoints': tuple(waypoints),
	}
	if terminal_hold:
		result['terminal_hold'] = True
	return result


def _reverse(route):
	result = dict(route)
	result['role_weights'] = dict(route['role_weights'])
	result['waypoints'] = tuple(reversed(route['waypoints']))
	return result


def _map(name, bounds, base1, base2, routes, routes2=None):
	team1 = tuple(routes)
	team2 = (tuple(routes2) if routes2 is not None else
	         tuple([_reverse(route) for route in team1]))
	return {
		'name': name,
		'bounds': bounds,
		'bases': {1: base1, 2: base2},
		'routes': {1: team1, 2: team2},
	}


KARELIA = _map('01_karelia', (-500.0, -500.0, 500.0, 500.0),
	(397.6, 402.6), (-401.3, -399.9), (
		_route('west_ridge', 'brawl', 5, 0.62, ((397, 402, 0), (260, 470, 0), (80, 430, 0), (-100, 330, 0), (-260, 180, 1), (-330, 30, 0), (-325, -150, 0), (-370, -300, 0), (-401, -399, 0))),
		_route('middle_road', 'fire', 4, 0.56, ((397, 402, 0), (270, 275, 0), (150, 95, 1), (35, -55, 1), (-105, -185, 1), (-250, -285, 0), (-401, -399, 0))),
		_route('east_shelf', 'flank', 4, 0.74, ((397, 402, 0), (455, 260, 0), (465, 80, 0), (410, -80, 1), (300, -240, 0), (155, -355, 0), (0, -425, 0), (-180, -430, 0), (-401, -399, 0))),
	))

_CAMPANIA_TEAM1 = (
	# Province's side hills are fighting positions, not through roads.  A
	# terminal objective lets each team climb its own approach without forcing
	# an impossible U-turn back down the same ramp toward the enemy flag.
	_route('west_hill', 'brawl', 5, 0.60,
		((-0.1, -209.3, 0), (-59, -145, 0), (-114, -178, 0),
		 (-174, -165, 0), (-206, -112, 0), (-212, -66, 1)), True),
	_route('central_village', 'fire', 4, 0.58,
		((0, -205, 0), (15, -125, 0), (20, -45, 1),
		 (15, 40, 1), (10, 120, 1), (0, 195, 0))),
	_route('east_hill', 'flank', 4, 0.76,
		((-0.1, -209.3, 0), (70, -180, 0), (117, -157, 0),
		 (151, -71, 1)), True),
)
_CAMPANIA_TEAM2 = (
	_route('west_hill', 'brawl', 5, 0.60,
		((0.0, 209.4, 0), (-52, 174, 0), (-112, 92, 0),
		 (-150, 70, 1)), True),
	_reverse(_CAMPANIA_TEAM1[1]),
	_route('east_hill', 'flank', 4, 0.76,
		((0.0, 209.4, 0), (80, 215, 0), (162, 215, 0),
		 (198, 196, 0), (224, 126, 1)), True),
)
CAMPANIA = _map('03_campania', (-300.0, -300.0, 300.0, 300.0),
	(-0.1, -209.3), (0.0, 209.4), _CAMPANIA_TEAM1, _CAMPANIA_TEAM2)
del _CAMPANIA_TEAM1
del _CAMPANIA_TEAM2

PROHOROVKA = _map('05_prohorovka', (-500.0, -500.0, 500.0, 500.0),
	(-125.2, 448.5), (51.6, -447.0), (
		_route('west_ridge', 'flank', 4, 0.78, ((-125, 448, 0), (-270, 435, 0), (-350, 280, 0), (-360, 80, 1), (-360, -250, 0), (-320, -360, 0), (-180, -430, 0), (52, -447, 0))),
		_route('central_field', 'fire', 5, 0.66, ((-110, 430, 0), (-80, 305, 0), (-45, 165, 1), (-15, 15, 1), (10, -145, 1), (35, -325, 0))),
		_route('rail_line', 'brawl', 5, 0.70, ((-125, 448, 0), (0, 420, 0), (120, 350, 0), (240, 210, 0), (360, 100, 1), (440, -50, 0), (470, -240, 0), (400, -380, 0), (220, -430, 0), (52, -447, 0))),
	))

ENSK = _map('06_ensk', (-300.0, -300.0, 300.0, 300.0),
	(20.3, 249.7), (19.1, -248.7), (
		_route('west_city', 'brawl', 6, 0.64, ((20, 240, 0), (-75, 205, 0), (-145, 125, 1), (-145, 25, 1), (-125, -85, 1), (-65, -200, 0))),
		_route('rail_yard', 'fire', 4, 0.58, ((15, 240, 0), (75, 185, 0), (125, 95, 1), (135, -10, 1), (115, -110, 1), (65, -205, 0))),
		_route('east_field', 'flank', 4, 0.73, ((25, 240, 0), (155, 210, 0), (225, 115, 1), (230, 10, 1), (210, -100, 1), (130, -205, 0))),
	))

LAKEVILLE = _map('07_lakeville', (-400.0, -400.0, 400.0, 400.0),
	(-169.5, 319.4), (-169.5, -319.0), (
		# Lakeville's world axes line up with the minimap. Lake road capacity is strictly 1 (scouts only)
		# to prevent bottlenecks and cliff diving, while main brawlers take west_valley and east_town:
		_route('west_valley', 'brawl', 7, 0.63, ((-169, 319, 0), (-314, 298, 0), (-330, 189, 1), (-331, 40, 1), (-315, -101, 1), (-278, -211, 0), (-225, -273, 0))),
		_route('lake_road', 'fire', 1, 0.62, ((-170, 298, 0), (-126, 268, 0), (-98, 228, 0), (-101, 176, 1), (-103, 112, 1), (-91, 80, 1), (-117, 48, 1), (-95, 16, 1), (-87, -16, 1), (-81, -52, 1), (-87, -80, 1), (-104, -128, 1), (-104, -188, 1), (-114, -240, 0), (-146, -276, 0))),
		_route('east_town', 'flank', 7, 0.74, ((-169, 319, 0), (-9, 325, 0), (164, 306, 0), (289, 267, 0), (322, 173, 1), (314, 40, 1), (284, -93, 1), (218, -187, 0), (70, -265, 0), (-79, -297, 0))),
	))

RUINBERG = _map('08_ruinberg', (-400.0, -400.0, 400.0, 400.0),
	(-66.4, 306.1), (-82.9, -290.9), (
		_route('west_city', 'brawl', 6, 0.66, ((-66, 306, 0), (-180, 230, 0), (-250, 130, 0), (-325, 0, 1), (-300, -70, 0), (-150, -180, 0), (-83, -291, 0))),
		_route('central_streets', 'fire', 4, 0.67, ((-66, 306, 0), (-10, 250, 0), (50, 130, 0), (70, 10, 1), (60, -100, 0), (10, -230, 0), (-83, -291, 0))),
		_route('east_fields', 'flank', 5, 0.72, ((-66, 306, 0), (20, 300, 0), (200, 240, 0), (330, 150, 0), (360, 20, 1), (345, -150, 0), (250, -240, 0), (100, -260, 0), (-83, -291, 0))),
	))

HILLS = _map('10_hills', (-400.0, -400.0, 400.0, 400.0),
	(175.8, -305.8), (-236.7, 329.7), (
		_route('southwest_road', 'brawl', 5, 0.66, ((176, -306, 0), (0, -290, 0), (-100, -180, 0), (-270, -150, 0), (-340, -60, 1), (-340, 100, 0), (-280, 180, 0), (-240, 280, 0), (-237, 330, 0))),
		_route('central_hills', 'fire', 4, 0.72, ((176, -306, 0), (135, -200, 0), (75, -125, 0), (-30, -95, 0), (-75, 0, 1), (-75, 130, 0), (-120, 215, 0), (-237, 330, 0))),
		_route('east_coast', 'flank', 4, 0.77, ((176, -306, 0), (165, -215, 0), (135, -120, 0), (250, 50, 1), (255, 100, 0), (125, 170, 0), (20, 255, 0), (-100, 280, 0), (-237, 330, 0))),
	))

MUROVANKA = _map('11_murovanka', (-400.0, -400.0, 400.0, 400.0),
	(202.8, 296.1), (-205.0, -292.8), (
		_route('west_woods', 'brawl', 5, 0.66, ((203, 296, 0), (70, 330, 0), (-90, 330, 0), (-235, 260, 0), (-345, 140, 0), (-370, -20, 1), (-350, -170, 0), (-290, -260, 0), (-205, -293, 0))),
		_route('central_field', 'fire', 4, 0.65, ((200, 285, 0), (120, 195, 0), (45, 105, 1), (-20, 5, 1), (-90, -105, 1), (-165, -225, 0))),
		_route('east_village', 'flank', 4, 0.74, ((203, 296, 0), (260, 165, 0), (295, 50, 0), (285, -90, 1), (240, -120, 0), (120, -135, 0), (60, -240, 0), (-110, -270, 0), (-205, -293, 0))),
	))

ERLENBERG = _map('13_erlenberg', (-500.0, -500.0, 500.0, 500.0),
	(-146.2, -0.1), (146.4, 0.1), (
		_route('north_bridge', 'brawl', 5, 0.72, ((-140, 0, 0), (-135, 110, 0), (-105, 225, 1), (-20, 300, 1), (85, 230, 1), (135, 105, 0))),
		_route('middle_crossing', 'fire', 4, 0.76, ((-140, 0, 0), (-75, 20, 0), (-20, 15, 1), (35, 10, 1), (90, 15, 1), (135, 0, 0))),
		_route('south_bridge', 'flank', 4, 0.74, ((-140, 0, 0), (-130, -105, 0), (-90, -220, 1), (5, -295, 1), (100, -220, 1), (135, -105, 0))),
	))


TACTICAL_MAPS_GROUP_A = {
	'01_karelia': KARELIA,
	'03_campania': CAMPANIA,
	'05_prohorovka': PROHOROVKA,
	'06_ensk': ENSK,
	'07_lakeville': LAKEVILLE,
	'08_ruinberg': RUINBERG,
	'10_hills': HILLS,
	'11_murovanka': MUROVANKA,
	'13_erlenberg': ERLENBERG,
}
