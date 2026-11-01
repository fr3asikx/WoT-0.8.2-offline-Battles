# -*- coding: utf-8 -*-
"""Additional map tactical definitions for offline/LAN bot AI."""


def _route(route_id, kind, capacity, weight, waypoints):
	return {
		'id': route_id,
		'kind': kind,
		'capacity': int(capacity),
		'weight': float(weight),
		'waypoints': tuple(
			(float(point[0]), float(point[1]), bool(point[2]))
			for point in waypoints),
	}


def _map(name, bounds, base_one, base_two, routes):
	return {
		'name': name,
		'bounds': tuple(float(value) for value in bounds),
		'bases': {
			1: (float(base_one[0]), float(base_one[1])),
			2: (float(base_two[0]), float(base_two[1])),
		},
		'routes': {
			1: routes,
			2: tuple(
				_route(
					route['id'], route['kind'], route['capacity'],
					route['weight'], tuple(reversed(route['waypoints'])))
				for route in routes),
		},
	}


MALINOVKA = _map('02_malinovka', (-500.0, -500.0, 500.0, 500.0),
	(81.04, -396.01), (-339.42, 129.01), (
		_route('west_waterfront', 'line', 4, 0.52,
			((0, 0, 0), (-20, -430, 0), (-120, -430, 0), (-220, -390, 0), (-370, -275, 1), (-405, -185, 0), (0, 0, 0))),
		_route('central_field', 'fire', 4, 0.70,
			((0, 0, 0), (200, -315, 0), (220, -220, 0), (195, -80, 0), (195, 60, 1), (150, 140, 0), (70, 210, 0), (-50, 240, 0), (-230, 190, 0), (0, 0, 0))),
		_route('east_hill_loop', 'flank', 5, 0.78,
			((0, 0, 0), (170, -360, 0), (300, -225, 0), (455, 295, 1), (440, 425, 0), (230, 470, 0), (70, 450, 0), (-150, 295, 0), (-325, 205, 0), (0, 0, 0))),
	))


SIEGFRIED_LINE = _map('14_siegfried_line', (-500.0, -500.0, 500.0, 500.0),
	(255.80, -439.83), (283.85, 434.60), (
		_route('west_field', 'flank', 5, 0.70,
			((0, 0, 0), (102, -346, 0), (62, -324, 0), (-6, -264, 0), (-150, -260, 1), (-290, -85, 1), (-265, 125, 1), (-80, 315, 0), (2, 325, 0), (38, 358, 0), (0, 0, 0))),
		_route('fortification_line', 'fire', 4, 0.66,
			((0, 0, 0), (145, -345, 0), (65, -220, 1), (40, -55, 1), (55, 125, 1), (145, 300, 0), (0, 0, 0))),
		_route('east_city', 'line', 6, 0.64,
			((0, 0, 0), (330, -330, 0), (315, -205, 1), (300, -65, 1), (315, 90, 1), (330, 265, 0), (0, 0, 0))),
	))


AIRFIELD = _map('31_airfield', (-500.0, -500.0, 500.0, 500.0),
	(360.65, -154.44), (-324.05, -176.18), (
		_route('north_runway', 'flank', 5, 0.72,
			((0, 0, 0), (280, 20, 0), (170, 210, 1), (15, 325, 1), (-150, 260, 1), (-270, 25, 0), (0, 0, 0))),
		_route('central_ridges', 'fire', 4, 0.68,
			((0, 0, 0), (245, -95, 0), (135, 15, 1), (15, 70, 1), (-105, 25, 1), (-225, -85, 0), (0, 0, 0))),
		_route('south_towns', 'line', 5, 0.65,
			((0, 0, 0), (250, -235, 0), (145, -285, 1), (15, -305, 1), (-115, -285, 1), (-225, -235, 0), (0, 0, 0))),
	))


TACTICAL_MAPS_EXTRA = {
	'02_malinovka': MALINOVKA,
	'14_siegfried_line': SIEGFRIED_LINE,
	'31_airfield': AIRFIELD,
}
