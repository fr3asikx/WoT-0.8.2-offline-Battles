# -*- coding: utf-8 -*-
"""Engine-free battle statistics used by the legacy results screen."""

import math


def new_stats(now=0.0):
	return {
		'start_time': None,
		'created_time': float(now or 0.0),
		'shots': 0,
		'hits': 0,
		'pierced': 0,
		'he_hits': 0,
		'damage_dealt': 0,
		'damage_assisted': 0,
		'damage_received': 0,
		'shots_received': 0,
		'kills': 0,
		'capture_points': 0,
		'dropped_capture_points': 0,
		'mileage': 0.0,
		'last_position': None,
		'spotted_ids': set(),
		'damaged_ids': set(),
		'details': {},
	}


def mark_started(stats, now):
	if stats is not None and stats.get('start_time') is None:
		stats['start_time'] = float(now or 0.0)


def record_shot(stats):
	if stats is not None:
		stats['shots'] = int(stats.get('shots', 0)) + 1


def _detail(stats, target_id):
	try:
		target_id = int(target_id)
	except Exception:
		return None
	details = stats.setdefault('details', {})
	value = details.get(target_id)
	if value is None:
		value = {
			'spotted': 0, 'killed': 0, 'hits': 0, 'he_hits': 0,
			'pierced': 0, 'damageDealt': 0, 'damageAssisted': 0,
			'crits': 0, 'fire': 0,
		}
		details[target_id] = value
	return value


def record_spotted(stats, target_id):
	if stats is None:
		return False
	try:
		target_id = int(target_id)
	except Exception:
		return False
	spotted = stats.setdefault('spotted_ids', set())
	if target_id in spotted:
		return False
	spotted.add(target_id)
	detail = _detail(stats, target_id)
	if detail is not None:
		detail['spotted'] = 1
	return True


def record_outgoing_hit(stats, target_id, damage, shot_result=2,
		dead=False, count_hit=True, he_hit=False, crits=0, fire=0):
	if stats is None:
		return
	damage = max(0, int(damage or 0))
	detail = _detail(stats, target_id)
	if count_hit:
		stats['hits'] = int(stats.get('hits', 0)) + 1
		if detail is not None:
			detail['hits'] = int(detail.get('hits', 0)) + 1
	if int(shot_result or 0) in (2, 4) and (damage > 0 or int(crits or 0) > 0):
		stats['pierced'] = int(stats.get('pierced', 0)) + 1
		if detail is not None:
			detail['pierced'] = int(detail.get('pierced', 0)) + 1
	if he_hit:
		stats['he_hits'] = int(stats.get('he_hits', 0)) + 1
		if detail is not None:
			detail['he_hits'] = int(detail.get('he_hits', 0)) + 1
	stats['damage_dealt'] = int(stats.get('damage_dealt', 0)) + damage
	if detail is not None:
		detail['damageDealt'] = int(detail.get('damageDealt', 0)) + damage
		if int(crits or 0) > 0:
			# In WoT 0.8.2 (battle_results_shared.py & BattleResultsWindow.swf), `details[vId]['crits']`
			# is a plain integer COUNT of critical hits shown next to "Повреждения:", NOT a 32-bit bitmask!
			_c_in = int(crits)
			if _c_in > 16:
				_c_mask = (_c_in & 0xFFF) | ((_c_in >> 12) & 0xFFF) | ((_c_in >> 24) & 0xFF)
				_c_in = bin(_c_mask).count('1')
			detail['crits'] = min(14, max(int(detail.get('crits', 0) or 0), _c_in))
		if int(fire or 0) > 0:
			detail['fire'] = int(detail.get('fire', 0)) + int(fire)
	if damage > 0 or int(crits or 0) > 0:
		stats.setdefault('damaged_ids', set()).add(int(target_id))
	if dead and detail is not None and not detail.get('killed'):
		detail['killed'] = 1
		stats['kills'] = int(stats.get('kills', 0)) + 1


def record_incoming_hit(stats, damage):
	if stats is None:
		return
	stats['shots_received'] = int(stats.get('shots_received', 0)) + 1
	stats['damage_received'] = (
		int(stats.get('damage_received', 0)) + max(0, int(damage or 0)))


def record_assist(stats, target_id, damage, dead=False):
	if stats is None:
		return
	damage = max(0, int(damage or 0))
	if damage <= 0:
		return
	stats['damage_assisted'] = int(stats.get('damage_assisted', 0)) + damage
	detail = _detail(stats, target_id)
	if detail is not None:
		detail['damageAssisted'] = int(detail.get('damageAssisted', 0)) + damage
		if dead:
			detail['killed'] = max(1, int(detail.get('killed', 0)))


def record_capture(stats, points=1):
	if stats is not None:
		stats['capture_points'] = min(
			100, int(stats.get('capture_points', 0)) + max(0, int(points or 0)))


def record_dropped_capture(stats, points=1):
	if stats is not None:
		stats['dropped_capture_points'] = min(
			100, int(stats.get('dropped_capture_points', 0)) +
			max(0, int(points or 0)))


def record_position(stats, position):
	if stats is None or position is None:
		return
	try:
		current = (float(position[0]), float(position[1]), float(position[2]))
	except Exception:
		return
	previous = stats.get('last_position')
	stats['last_position'] = current
	if previous is None:
		return
	dx = current[0] - previous[0]
	dz = current[2] - previous[2]
	distance = math.sqrt(dx * dx + dz * dz)
	# A network correction or spawn relocation is not driven mileage.
	if distance <= 40.0:
		stats['mileage'] = float(stats.get('mileage', 0.0)) + distance


def result_values(stats, now):
	stats = stats or new_stats(now)
	started = stats.get('start_time')
	if started is None:
		started = stats.get('created_time', float(now or 0.0))
	return {
		'shots': int(stats.get('shots', 0)),
		'hits': int(stats.get('hits', 0)),
		'he_hits': int(stats.get('he_hits', 0)),
		'pierced': int(stats.get('pierced', 0)),
		'damageDealt': int(stats.get('damage_dealt', 0)),
		'damageAssisted': int(stats.get('damage_assisted', 0)),
		'damageReceived': int(stats.get('damage_received', 0)),
		'shotsReceived': int(stats.get('shots_received', 0)),
		'spotted': len(stats.get('spotted_ids', ())),
		'damaged': len(stats.get('damaged_ids', ())),
		'kills': int(stats.get('kills', 0)),
		'capturePoints': int(stats.get('capture_points', 0)),
		'droppedCapturePoints': int(stats.get('dropped_capture_points', 0)),
		'mileage': int(round(float(stats.get('mileage', 0.0)))),
		'lifeTime': max(0, int(float(now or 0.0) - float(started or 0.0))),
		'details': dict(stats.get('details', {})),
	}
