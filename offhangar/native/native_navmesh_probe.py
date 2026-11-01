# -*- coding: utf-8 -*-
"""One-shot runtime check for the Lakeville native-navmesh experiment."""

import BigWorld
import Math
import math
import struct
import time

from gui.mods.offhangar.logging import LOG_ERROR, LOG_NOTE


PROBE_MAP = '07_lakeville'
PROBE_GIRTH = 0.5
PROBE_DISTANCE = 8.0
INITIAL_DELAY = 1.0
RETRY_INTERVAL = 0.5
RETRY_WINDOW = 8.0
MAX_ATTEMPTS = 18


def _system_message(message, level='information'):
	try:
		from gui.SystemMessages import SM_TYPE, pushMessage
		if level == 'error':
			message_type = SM_TYPE.Error
		elif level == 'warning':
			message_type = SM_TYPE.Warning
		else:
			message_type = SM_TYPE.Information
		text = str(message)
		try:
			text = text.encode('utf-8')
		except Exception:
			pass
		pushMessage(text, message_type)
		return True
	except Exception:
		return False


def _short_map_name(map_name):
	try:
		return str(map_name or '').replace('\\', '/').rstrip('/').split('/')[-1]
	except Exception:
		return ''


def _point_text(point):
	try:
		return '(%.1f, %.1f, %.1f)' % (
			float(point.x), float(point.y), float(point.z))
	except Exception:
		return str(point)


def _now():
	try:
		return float(BigWorld.time())
	except Exception:
		return time.time()


def _chunk_id(position):
	grid_x = int(math.floor(float(position.x) / 100.0))
	grid_z = int(math.floor(float(position.z) / 100.0))
	return '%04x%04xo' % (grid_x & 65535, grid_z & 65535)


def _chunk_centre(position):
	grid_x = int(math.floor(float(position.x) / 100.0))
	grid_z = int(math.floor(float(position.z) / 100.0))
	return Math.Vector3(
		grid_x * 100.0 + 50.0, float(position.y),
		grid_z * 100.0 + 50.0)


def _resource_snapshot(position):
	"""Describe exactly what the live ResMgr can see from the overlay."""
	chunk_id = _chunk_id(position)
	try:
		import ResMgr
		settings = ResMgr.openSection('spaces/%s/space.settings' % PROBE_MAP)
		enabled = False
		if settings is not None:
			try:
				enabled = bool(settings['clientNavigation/enable'].asBool)
			except Exception:
				pass
		chunk = ResMgr.openSection(
			'spaces/%s/%s.chunk' % (PROBE_MAP, chunk_id))
		resource = ''
		if chunk is not None:
			try:
				resource = str(chunk['worldNavmesh/resource'].asString)
			except Exception:
				pass
		binary_length = -1
		header = None
		if resource:
			section = ResMgr.openSection('spaces/%s/%s' % (PROBE_MAP, resource))
			if section is not None:
				raw = str(section.asBinary)
				binary_length = len(raw)
				if binary_length >= 16:
					header = struct.unpack('<ifii', raw[:16])
		return 'enable=%s chunk=%s ref=%s bytes=%s header=%s' % (
			enabled, chunk_id, resource or '<missing>', binary_length, header)
	except Exception as error:
		return 'chunk=%s audit_error=%s' % (chunk_id, str(error))


def _candidate_pairs(position):
	source = Math.Vector3(float(position.x), float(position.y), float(position.z))
	delta_x = PROBE_DISTANCE if float(source.x) < -150.0 else -PROBE_DISTANCE
	destination = Math.Vector3(
		float(source.x) + delta_x, float(source.y), float(source.z))
	centre = _chunk_centre(source)
	centre_delta = PROBE_DISTANCE if float(centre.x) < -150.0 else -PROBE_DISTANCE
	centre_destination = Math.Vector3(
		float(centre.x) + centre_delta, float(centre.y), float(centre.z))
	return (
		('player', source, destination),
		('chunk_center', centre, centre_destination),
	)


def maybe_run(player, map_name, position, battle_generation):
	"""Retry briefly after terrain placement while chunk items finish binding."""
	if player is None or _short_map_name(map_name) != PROBE_MAP:
		return False
	try:
		if getattr(getattr(player, 'arena', None), 'period', 0) != 3:
			return False
		if not getattr(player, '_offh_spawn_fixed', False):
			return False
		marker = int(battle_generation or 0)
		now = _now()
		state = getattr(player, '_offh_native_navmesh_probe_state', None)
		if not isinstance(state, dict) or state.get('generation') != marker:
			state = {
				'generation': marker,
				'started': now,
				'next_attempt': now + INITIAL_DELAY,
				'attempts': 0,
				'done': False,
				'last_error': '',
				'resource': '',
			}
			player._offh_native_navmesh_probe_state = state
			return False
		if state.get('done') or now < float(state.get('next_attempt', 0.0)):
			return False
	except Exception:
		return False

	state['attempts'] += 1
	attempt = state['attempts']
	if attempt == 1:
		state['resource'] = _resource_snapshot(position)
		LOG_NOTE('NAVMESH_PROBE RESOURCE %s' % state['resource'])

	try:
		navigate = getattr(BigWorld, 'navigatePathPoints', None)
		if not callable(navigate):
			raise RuntimeError('BigWorld.navigatePathPoints is unavailable')
		candidate_errors = []
		for mode, source, destination in _candidate_pairs(position):
			try:
				path = navigate(source, destination, 100.0, PROBE_GIRTH)
				if path is None or len(path) == 0:
					raise RuntimeError('native navigator returned an empty path')
				state['done'] = True
				LOG_NOTE(
					'NAVMESH_PROBE PASS map=%s attempt=%d mode=%s points=%d '
					'src=%s dst=%s first=%s last=%s' % (
						PROBE_MAP, attempt, mode, len(path), _point_text(source),
						_point_text(destination), _point_text(path[0]),
						_point_text(path[-1])))
				_system_message(
					'Native navmesh loaded: %d path point(s).' % len(path),
					'information')
				return True
			except Exception as error:
				candidate_errors.append('%s=%s' % (mode, str(error)))
		raise RuntimeError('; '.join(candidate_errors))
	except Exception as error:
		state['last_error'] = str(error)
		elapsed = now - float(state.get('started', now))
		if elapsed >= RETRY_WINDOW or attempt >= MAX_ATTEMPTS:
			state['done'] = True
			LOG_ERROR(
				'NAVMESH_PROBE FAIL map=%s attempts=%d elapsed=%.1f pos=%s '
				'resource={%s} error=%s' % (
					PROBE_MAP, attempt, elapsed, _point_text(position),
					state.get('resource', ''), str(error)))
			_system_message(
				'Native navmesh probe failed: %s' % str(error), 'error')
		else:
			state['next_attempt'] = now + RETRY_INTERVAL
			if attempt in (1, 4, 8, 12, 16):
				LOG_NOTE(
					'NAVMESH_PROBE WAIT attempt=%d elapsed=%.1f error=%s' % (
						attempt, elapsed, str(error)))
		return False
