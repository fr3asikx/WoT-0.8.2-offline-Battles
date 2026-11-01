# -*- coding: utf-8 -*-
"""Engine-free base-capture accounting for the legacy battle runtime."""


MAX_CAPTURE_POINTS = 100
MAX_CAPTURE_RATE = 3


def new_state():
	return {
		'points': 0,
		'stopped': False,
		'contributors': {},
		'active_contributors': [],
		'invaders': 0,
		'cursor': 0,
	}


def _contributors(state):
	contributors = state.get('contributors')
	if not isinstance(contributors, dict):
		contributors = {}
		state['contributors'] = contributors
	return contributors


def _ordered_unique(values):
	unique = {}
	for value in values or ():
		if value is not None:
			unique[value] = True
	return sorted(unique.keys(), key=lambda value: str(value))


def _refresh_points(state):
	points = sum(max(0, int(value or 0))
		for value in _contributors(state).values())
	state['points'] = max(0, min(points, MAX_CAPTURE_POINTS))
	return state['points']


def advance(state, invader_ids, defenders_present=False, owner_stops_capturing=False):
	"""Advance one one-second capture tick.

	Capture speed is capped at three points per second, but ownership is rotated
	between all active invaders. That ownership lets one damaged or departing
	vehicle lose only its own accumulated contribution.
	"""
	if state is None:
		state = new_state()
	contributors = _contributors(state)
	active = _ordered_unique(invader_ids)
	state['active_contributors'] = list(active)
	state['invaders'] = len(active)
	active_set = set(active)
	dropped = {}
	for vehicle_id in list(contributors.keys()):
		if vehicle_id not in active_set:
			points = max(0, int(contributors.pop(vehicle_id, 0) or 0))
			if points:
				dropped[vehicle_id] = points

	for vehicle_id in active:
		contributors.setdefault(vehicle_id, 0)

	old_points = _refresh_points(state)
	# Standard 0.8.2 CTF/Assault bases declare ownerStopsCapturing=false, while
	# Encounter (domination) neutral bases lock/freeze capture when both teams
	# occupy the circle simultaneously (owner_stops_capturing=True).
	state['stopped'] = bool(owner_stops_capturing and active and defenders_present)
	gained = {}
	if active and not state['stopped'] and old_points < MAX_CAPTURE_POINTS:
		cursor = int(state.get('cursor', 0) or 0) % len(active)
		budget = min(MAX_CAPTURE_RATE, len(active),
			MAX_CAPTURE_POINTS - old_points)
		for offset in range(budget):
			vehicle_id = active[(cursor + offset) % len(active)]
			contributors[vehicle_id] = int(contributors.get(vehicle_id, 0) or 0) + 1
			gained[vehicle_id] = int(gained.get(vehicle_id, 0) or 0) + 1
		state['cursor'] = (cursor + budget) % len(active)
	elif not active:
		state['cursor'] = 0

	points = _refresh_points(state)
	return {
		'old_points': old_points,
		'points': points,
		'gained': gained,
		'dropped': dropped,
		'stopped': bool(state.get('stopped', False)),
	}


def drop_vehicle(state, vehicle_id):
	"""Remove one vehicle's contribution and return the points it lost."""
	if state is None or vehicle_id is None:
		return 0
	contributors = _contributors(state)
	dropped = max(0, int(contributors.pop(vehicle_id, 0) or 0))
	active = [value for value in state.get('active_contributors') or ()
		if value != vehicle_id]
	state['active_contributors'] = active
	state['invaders'] = len(active)
	_refresh_points(state)
	if not contributors:
		state['stopped'] = False
		state['cursor'] = 0
	return dropped
