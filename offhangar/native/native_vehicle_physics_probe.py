# -*- coding: utf-8 -*-
"""Fail-closed probe for the retail 0.8.2 vehicle physics stack.

Press F6 during a live offline/LAN battle.  One temporary ``OfflineEntity``
is used at a time while the probe tries four pose-seeding paths:

* retail ``WGVehicleFilter2`` assignment order;
* ``AvatarFilter`` attachment followed by the documented copy constructor;
* legacy ``WGVehicleFilter.setPosition(x, z)`` followed by that copy path.
* a version-locked native bridge to ``Filter::input``.

Every stage records the native ``bodyMatrix`` position and yaw.  Native drive
is enabled only when the complete pose remains correct after physics is
attached.  A failed candidate is destroyed before the next one is created, so
an origin filter can never become a moving ghost in the battle.
"""

import math
import time
import weakref

import BigWorld
import Math

from gui.mods.offhangar.logging import LOG_ERROR, LOG_NOTE


INITIAL_DELAY = 0.40
ENTITY_WAIT_SECONDS = 2.0
STAGE_WAIT_SECONDS = 0.08
PHYSICS_SETTLE_SECONDS = 0.35
DRIVE_SECONDS = 2.0
CLEANUP_DELAY = 0.20
MIN_PASS_DISTANCE = 0.50
POSE_POSITION_TOLERANCE = 2.0
POSE_YAW_TOLERANCE = 0.35

_CANDIDATES = (
	'retail_order',
	'avatar_copy',
	'legacy_set_position',
	'native_bridge',
)
_STATE_ATTR = '_offh_native_vehicle_physics_probe_state'
_REQUEST_SERIAL = [0]
_ACTIVE_SERIAL = [0]


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


def _now():
	try:
		return float(BigWorld.time())
	except Exception:
		return time.time()


def _finite(value):
	try:
		value = float(value)
		return not math.isnan(value) and not math.isinf(value)
	except Exception:
		return False


def _point_tuple(value):
	try:
		return (float(value.x), float(value.y), float(value.z))
	except Exception:
		try:
			return (float(value[0]), float(value[1]), float(value[2]))
		except Exception:
			return None


def _point_text(value):
	point = _point_tuple(value)
	if point is None:
		return '<unavailable>'
	return '(%.2f, %.2f, %.2f)' % point


def _type_name(value):
	if value is None:
		return '<none>'
	try:
		return str(type(value).__name__)
	except Exception:
		try:
			return str(value.__class__.__name__)
		except Exception:
			return '<unknown>'


def _normalise_angle(value):
	value = float(value)
	while value > math.pi:
		value -= math.pi * 2.0
	while value < -math.pi:
		value += math.pi * 2.0
	return value


def _distance_3d(first, second):
	first = _point_tuple(first)
	second = _point_tuple(second)
	if first is None or second is None:
		return None
	dx = second[0] - first[0]
	dy = second[1] - first[1]
	dz = second[2] - first[2]
	return math.sqrt(dx * dx + dy * dy + dz * dz)


def request():
	"""Arm one probe run. A second F6 press after completion runs it again."""
	_REQUEST_SERIAL[0] += 1
	_ACTIVE_SERIAL[0] = _REQUEST_SERIAL[0]
	LOG_NOTE('NATIVE_PHYSICS_PROBE requested token=%d' % _REQUEST_SERIAL[0])
	_system_message(
		'Native vehicle physics probe armed (F6). See python.log.',
		'information')
	return _REQUEST_SERIAL[0]


def is_requested():
	return _ACTIVE_SERIAL[0] > 0


def _ground_y(space_id, x, z, hint_y):
	start = Math.Vector3(float(x), float(hint_y) + 35.0, float(z))
	end = Math.Vector3(float(x), float(hint_y) - 120.0, float(z))
	hit = BigWorld.wg_collideSegment(int(space_id), start, end, 128)
	if hit is None:
		return None
	try:
		return float(hit[0].y)
	except Exception:
		return None


def _corridor_clear(space_id, start, end, start_y, end_y):
	"""Require a 3 m-wide, 18 m-long obstacle-free native corridor."""
	dx = float(end[0]) - float(start[0])
	dz = float(end[1]) - float(start[1])
	length = math.sqrt(dx * dx + dz * dz)
	if length <= 0.001:
		return False
	right_x = dz / length
	right_z = -dx / length
	for lateral in (-1.5, 0.0, 1.5):
		first = Math.Vector3(
			float(start[0]) + right_x * lateral, float(start_y) + 1.25,
			float(start[1]) + right_z * lateral)
		second = Math.Vector3(
			float(end[0]) + right_x * lateral, float(end_y) + 1.25,
			float(end[1]) + right_z * lateral)
		if BigWorld.wg_collideSegment(
				int(space_id), first, second, 128) is not None:
			return False
	return True


def _probe_yaw(player_yaw):
	# A deliberately non-zero target catches legacy x/z-only seeding.  Avoid a
	# rare player heading that would cancel the first offset back to zero.
	value = _normalise_angle(float(player_yaw) + 0.67)
	if abs(value) < 0.40:
		value = _normalise_angle(value + 0.91)
	return value


def _select_probe_pose(space_id, origin, yaw):
	"""Find a short clear patch away from the player without map assumptions."""
	origin = _point_tuple(origin)
	if origin is None:
		raise RuntimeError('player position is unavailable')
	forward_x = math.sin(float(yaw))
	forward_z = math.cos(float(yaw))
	right_x = math.cos(float(yaw))
	right_z = -math.sin(float(yaw))
	offsets = (
		(-38.0, -20.0), (38.0, -20.0),
		(-48.0, 0.0), (48.0, 0.0),
		(-32.0, 28.0), (32.0, 28.0),
		(0.0, -52.0), (0.0, 52.0),
	)
	for lateral, forward in offsets:
		x = origin[0] + right_x * lateral + forward_x * forward
		z = origin[2] + right_z * lateral + forward_z * forward
		end_x = x + forward_x * 18.0
		end_z = z + forward_z * 18.0
		start_y = _ground_y(space_id, x, z, origin[1])
		middle_y = _ground_y(
			space_id, (x + end_x) * 0.5, (z + end_z) * 0.5,
			origin[1])
		end_y = _ground_y(space_id, end_x, end_z, origin[1])
		if start_y is None or middle_y is None or end_y is None:
			continue
		if (abs(middle_y - start_y) > 2.8 or
				abs(end_y - middle_y) > 2.8):
			continue
		if not _corridor_clear(
				space_id, (x, z), (end_x, end_z), start_y, end_y):
			continue
		return Math.Vector3(x, start_y + 0.10, z), float(yaw)
	raise RuntimeError('no clear probe corridor near the player spawn')


def _descriptor_value(container, name, default=None):
	try:
		return container[name]
	except Exception:
		return getattr(container, name, default)


def _configure_filter(vehicle_filter, descriptor):
	chassis = _descriptor_value(descriptor, 'chassis', {})
	physics = _descriptor_value(descriptor, 'physics', {})
	try:
		vehicle_filter.vehicleWidth = float(
			_descriptor_value(chassis, 'topRightCarryingPoint')[0]) * 2.0
	except Exception:
		vehicle_filter.vehicleWidth = 3.0
	try:
		vehicle_filter.vehicleMaxMove = float(
			_descriptor_value(physics, 'speedLimits')[0]) * 2.0
	except Exception:
		vehicle_filter.vehicleMaxMove = 100.0
	try:
		vehicle_filter.vehicleMinNormalY = float(
			_descriptor_value(physics, 'minPlaneNormalY'))
	except Exception:
		vehicle_filter.vehicleMinNormalY = 0.5
	try:
		vehicle_filter.vehicleCollisionCallback = None
	except Exception:
		pass
	try:
		vehicle_filter.isLaggingStateChangedCallback = None
	except Exception:
		pass
	for triangle in (_descriptor_value(
			physics, 'carryingTriangles', ()) or ()):
		p1, p2, p3 = triangle
		vehicle_filter.addTriangle(
			(p1[0], 0.0, p1[1]),
			(p2[0], 0.0, p2[1]),
			(p3[0], 0.0, p3[1]))


def _visibility_mask(player):
	try:
		import ArenaType
		return int(ArenaType.getVisibilityMask(
			int(getattr(player, 'arenaTypeID', 0)) >> 16))
	except Exception:
		return 1


def _filter_pose(vehicle_filter):
	provider = getattr(vehicle_filter, 'bodyMatrix', None)
	if provider is None:
		return None, None
	try:
		matrix = Math.Matrix(provider)
	except Exception:
		matrix = provider
	position = _point_tuple(getattr(matrix, 'translation', None))
	if position is None:
		position = _point_tuple(getattr(provider, 'translation', None))
	yaw = None
	try:
		yaw = float(matrix.yaw)
	except Exception:
		try:
			forward = matrix.applyVector(Math.Vector3(0.0, 0.0, 1.0))
			yaw = math.atan2(float(forward.x), float(forward.z))
		except Exception:
			pass
	return position, yaw


def _pose_record(state, label, vehicle_filter):
	position, yaw = _filter_pose(vehicle_filter)
	position_delta = _distance_3d(state.get('position'), position)
	yaw_delta = None
	if yaw is not None:
		yaw_delta = abs(_normalise_angle(yaw - float(state.get('yaw', 0.0))))
	valid = (position_delta is not None and yaw_delta is not None and
			_finite(position_delta) and _finite(yaw_delta) and
			position_delta <= POSE_POSITION_TOLERANCE and
			yaw_delta <= POSE_YAW_TOLERANCE)
	LOG_NOTE(
		'NATIVE_PHYSICS_PROBE POSE candidate=%s stage=%s expected=%s/%.3f '
		'actual=%s/%s delta=%s/%s valid=%s' % (
			state.get('candidate'), label, _point_text(state.get('position')),
			float(state.get('yaw', 0.0)), _point_text(position),
			'<unavailable>' if yaw is None else '%.3f' % yaw,
			'<unavailable>' if position_delta is None else
				'%.2fm' % position_delta,
			'<unavailable>' if yaw_delta is None else '%.3frad' % yaw_delta,
			valid))
	return valid


def _speed_snapshot(vehicle_filter):
	values = []
	for name in ('longitudinalSpeed', 'strafeSpeed', 'angularSpeed'):
		try:
			values.append(float(getattr(vehicle_filter, name)))
		except Exception:
			values.append(None)
	try:
		speed_info = getattr(vehicle_filter, 'speedInfo')
		values.append(float(speed_info.value[0]))
	except Exception:
		values.append(None)
	return tuple(values)


def _contact_snapshot(vehicle_filter):
	values = []
	for name in ('numLeftTrackContacts', 'numRightTrackContacts'):
		try:
			values.append(int(getattr(vehicle_filter, name)))
		except Exception:
			values.append(None)
	return tuple(values)


def _destroy_entity(state):
	entity_id = state.get('entity_id')
	state['entity'] = None
	state['source_filter'] = None
	state['filter'] = None
	state['physics'] = None
	if entity_id is not None:
		try:
			BigWorld.destroyEntity(int(entity_id))
		except Exception:
			pass
	state['entity_id'] = None


def _stop_native_input(state):
	vehicle_filter = state.get('filter')
	if vehicle_filter is not None:
		try:
			vehicle_filter.notifyInputKeysDown(0, 0)
		except Exception:
			pass
	physics = state.get('physics')
	if physics is not None:
		for name in ('damageDestructibleCb',
				'destructibleHealthRequestCb', 'onRammingCb',
				'onBecameFrozenCb', 'onStaticDamageCb'):
			try:
				setattr(physics, name, None)
			except Exception:
				pass


def _finish(state, passed, detail):
	_stop_native_input(state)
	state['passed'] = bool(passed)
	state['detail'] = str(detail)
	state['phase'] = 'cleanup'
	state['next_at'] = _now() + CLEANUP_DELAY
	if passed:
		LOG_NOTE('NATIVE_PHYSICS_PROBE PASS %s' % detail)
		_system_message('Native vehicle physics probe passed.', 'information')
	else:
		LOG_ERROR('NATIVE_PHYSICS_PROBE FAIL %s' % detail)
		_system_message(
			'Native vehicle physics probe failed. See python.log.', 'error')


def _fail(state, stage, error):
	_finish(state, False, 'stage=%s error=%s' % (stage, str(error)))


def _next_candidate(state, reason):
	name = state.get('candidate', '<none>')
	state.setdefault('candidate_failures', []).append(
		'%s:%s' % (name, str(reason)))
	LOG_NOTE(
		'NATIVE_PHYSICS_PROBE CANDIDATE FAIL candidate=%s reason=%s' % (
			name, str(reason)))
	_stop_native_input(state)
	_destroy_entity(state)
	state['candidate_index'] = int(state.get('candidate_index', 0)) + 1
	if state['candidate_index'] >= len(_CANDIDATES):
		_finish(
			state, False,
			'stage=pose_seed candidates=%s' %
			';'.join(state.get('candidate_failures', [])))
		return
	state['candidate'] = _CANDIDATES[state['candidate_index']]
	state['phase'] = 'create_candidate'
	state['next_at'] = _now() + STAGE_WAIT_SECONDS


def _create_candidate(state, space_id):
	LOG_NOTE(
		'NATIVE_PHYSICS_PROBE CANDIDATE START candidate=%s pos=%s yaw=%.3f' % (
			state['candidate'], _point_text(state['position']), state['yaw']))
	entity_id = BigWorld.createEntity(
		'OfflineEntity', int(space_id), 0, state['position'],
		(0.0, 0.0, state['yaw']), dict())
	state['entity_id'] = int(entity_id)
	state['phase'] = 'wait_entity'
	state['deadline'] = _now() + ENTITY_WAIT_SECONDS
	state['next_at'] = _now()


def _attach_physics(state, descriptor, player):
	state['phase'] = 'physics_attach'
	LOG_NOTE(
		'NATIVE_PHYSICS_PROBE STAGE physics candidate=%s' %
		state.get('candidate'))
	import physics_shared
	physics = BigWorld.WGVehiclePhysics2()
	physics_shared.initVehiclePhysics(physics, descriptor)
	physics.setArenaBounds((-10000, -10000), (10000, 10000))
	physics.enginePower = float(
		_descriptor_value(
			_descriptor_value(descriptor, 'physics', {}),
			'enginePower', 0.0)) / 1000.0
	physics.owner = weakref.ref(state['entity'])
	physics.staticMode = False
	physics.movementSignals = 0
	for name in ('damageDestructibleCb',
			'destructibleHealthRequestCb', 'onRammingCb',
			'onBecameFrozenCb', 'onStaticDamageCb'):
		try:
			setattr(physics, name, None)
		except Exception:
			pass
	physics.visibilityMask = _visibility_mask(player)
	state['filter'].setVehiclePhysics(physics)
	# syncGunAngles uses the retail ServerConnection clock in 0.8.2. The probe's
	# client-only OfflineEntity has no connection, and its gun angles are unused.
	state['entity'].wgPhysics = physics
	state['physics'] = physics
	state['phase'] = 'physics_wait'
	state['next_at'] = _now() + PHYSICS_SETTLE_SECONDS


def cancel(player):
	"""Stop and destroy a live probe before the battle resource sweep."""
	state = getattr(player, _STATE_ATTR, None) if player is not None else None
	if isinstance(state, dict):
		_stop_native_input(state)
		_destroy_entity(state)
		state['phase'] = 'done'
	_ACTIVE_SERIAL[0] = 0
	return True


def maybe_run(player, descriptor, position, yaw, space_id,
		battle_generation):
	"""Advance one staged probe step; called from the normal battle tick."""
	if player is None or descriptor is None or not is_requested():
		return False
	try:
		if getattr(getattr(player, 'arena', None), 'period', 0) != 3:
			return False
	except Exception:
		return False

	token = int(_ACTIVE_SERIAL[0])
	state = getattr(player, _STATE_ATTR, None)
	if (not isinstance(state, dict) or state.get('token') != token or
			state.get('generation') != int(battle_generation or 0)):
		if isinstance(state, dict):
			_stop_native_input(state)
			_destroy_entity(state)
		state = {
			'token': token,
			'generation': int(battle_generation or 0),
			'phase': 'prepare',
			'next_at': _now() + INITIAL_DELAY,
			'entity_id': None,
			'entity': None,
			'source_filter': None,
			'filter': None,
			'physics': None,
			'candidate_index': 0,
			'candidate': _CANDIDATES[0],
			'candidate_failures': [],
			'space_id': int(space_id),
		}
		setattr(player, _STATE_ATTR, state)
		LOG_NOTE(
			'NATIVE_PHYSICS_PROBE ARM token=%d generation=%d candidates=%s' % (
				token, state['generation'], ','.join(_CANDIDATES)))
		return False

	if state.get('phase') == 'done' or _now() < float(
			state.get('next_at', 0.0)):
		return False

	phase = state.get('phase')
	try:
		if phase == 'prepare':
			probe_yaw = _probe_yaw(yaw)
			probe_position, probe_yaw = _select_probe_pose(
				space_id, position, probe_yaw)
			state['position'] = probe_position
			state['yaw'] = probe_yaw
			state['phase'] = 'create_candidate'
			state['next_at'] = _now()
			return False

		if phase == 'create_candidate':
			_create_candidate(state, space_id)
			return False

		if phase == 'wait_entity':
			entity = BigWorld.entity(state['entity_id'])
			if entity is None:
				if _now() >= float(state.get('deadline', 0.0)):
					raise RuntimeError('temporary OfflineEntity did not bind')
				state['next_at'] = _now() + 0.05
				return False
			original_filter = getattr(entity, 'filter', None)
			if original_filter is None:
				raise RuntimeError(
					'temporary OfflineEntity has no engine-created filter')
			entity.typeDescriptor = descriptor
			# Client-only OfflineEntity never enters the retail Vehicle UI lifecycle.
			# A fake playerVehicleID may collide with this engine entity ID, and stock
			# cameras then dereference appearance fields that the probe does not own.
			entity.isStarted = False
			entity.isPlayer = False
			state['entity'] = entity
			LOG_NOTE(
				'NATIVE_PHYSICS_PROBE STAGE bind candidate=%s entity=%d '
				'source=%s entity_pos=%s' % (
					state['candidate'], entity.id, _type_name(original_filter),
					_point_text(getattr(entity, 'position', None))))

			if state['candidate'] == 'retail_order':
				vehicle_filter = BigWorld.WGVehicleFilter2()
				# Retail assigns first, then configures VehicleAppearance fields.
				entity.filter = vehicle_filter
				_configure_filter(vehicle_filter, descriptor)
				state['filter'] = vehicle_filter
				_pose_record(state, 'T0_assign', vehicle_filter)
				state['phase'] = 'retail_wait_1'
				state['next_at'] = _now() + STAGE_WAIT_SECONDS
				return False

			if state['candidate'] == 'avatar_copy':
				try:
					BigWorld.AvatarFilter(original_filter)
					LOG_NOTE(
						'NATIVE_PHYSICS_PROBE ABI unexpected: '
						'AvatarFilter accepted %s' % _type_name(original_filter))
				except Exception as error:
					LOG_NOTE(
						'NATIVE_PHYSICS_PROBE ABI expected rejection: '
						'AvatarFilter(%s) error=%s' % (
							_type_name(original_filter), str(error)))
				seed = BigWorld.AvatarFilter()
				entity.filter = seed
				state['source_filter'] = seed
				_pose_record(state, 'T0_avatar_assign', seed)
				state['phase'] = 'avatar_seed_wait_1'
				state['next_at'] = _now() + STAGE_WAIT_SECONDS
				return False

			if state['candidate'] == 'legacy_set_position':
				seed = BigWorld.WGVehicleFilter()
				entity.filter = seed
				state['source_filter'] = seed
				seed.setPosition(
					float(state['position'].x), float(state['position'].z))
				_pose_record(state, 'T0_legacy_setPosition', seed)
				state['phase'] = 'legacy_seed_wait_1'
				state['next_at'] = _now() + STAGE_WAIT_SECONDS
				return False

			if state['candidate'] == 'native_bridge':
				vehicle_filter = BigWorld.WGVehicleFilter2()
				entity.filter = vehicle_filter
				_configure_filter(vehicle_filter, descriptor)
				state['filter'] = vehicle_filter
				_pose_record(state, 'T0_native_before_seed', vehicle_filter)
				from gui.mods.offhangar import native_filter_bridge
				if not native_filter_bridge.seed_filter(
						vehicle_filter, _now(), state['space_id'],
						state['position'], (state['yaw'], 0.0, 0.0)):
					_next_candidate(state, 'native Filter::input bridge rejected seed')
					return False
				_pose_record(state, 'T1_native_seed_return', vehicle_filter)
				state['phase'] = 'native_seed_wait_1'
				state['next_at'] = _now() + STAGE_WAIT_SECONDS
				return False

			raise RuntimeError('unknown candidate %r' % state['candidate'])

		if phase == 'retail_wait_1':
			_pose_record(state, 'T1_callback', state['filter'])
			state['phase'] = 'retail_wait_2'
			state['next_at'] = _now() + STAGE_WAIT_SECONDS
			return False

		if phase == 'retail_wait_2':
			_pose_record(state, 'T2_callback', state['filter'])
			_attach_physics(state, descriptor, player)
			return False

		if phase == 'avatar_seed_wait_1':
			_pose_record(state, 'T1_avatar_callback', state['source_filter'])
			state['phase'] = 'avatar_seed_wait_2'
			state['next_at'] = _now() + STAGE_WAIT_SECONDS
			return False

		if phase == 'avatar_seed_wait_2':
			_pose_record(state, 'T2_avatar_callback', state['source_filter'])
			vehicle_filter = BigWorld.WGVehicleFilter2(state['source_filter'])
			state['entity'].filter = vehicle_filter
			_configure_filter(vehicle_filter, descriptor)
			state['filter'] = vehicle_filter
			_pose_record(state, 'T3_copy_assign', vehicle_filter)
			state['phase'] = 'copy_wait_1'
			state['next_at'] = _now() + STAGE_WAIT_SECONDS
			return False

		if phase == 'legacy_seed_wait_1':
			_pose_record(state, 'T1_legacy_callback', state['source_filter'])
			state['phase'] = 'legacy_seed_wait_2'
			state['next_at'] = _now() + STAGE_WAIT_SECONDS
			return False

		if phase == 'legacy_seed_wait_2':
			_pose_record(state, 'T2_legacy_callback', state['source_filter'])
			vehicle_filter = BigWorld.WGVehicleFilter2(state['source_filter'])
			state['entity'].filter = vehicle_filter
			_configure_filter(vehicle_filter, descriptor)
			state['filter'] = vehicle_filter
			_pose_record(state, 'T3_copy_assign', vehicle_filter)
			state['phase'] = 'copy_wait_1'
			state['next_at'] = _now() + STAGE_WAIT_SECONDS
			return False

		if phase == 'copy_wait_1':
			_pose_record(state, 'T4_copy_callback', state['filter'])
			state['phase'] = 'copy_wait_2'
			state['next_at'] = _now() + STAGE_WAIT_SECONDS
			return False

		if phase == 'copy_wait_2':
			if not _pose_record(state, 'T5_copy_callback', state['filter']):
				_next_candidate(state, 'complete pose unavailable after copy')
				return False
			_attach_physics(state, descriptor, player)
			return False

		if phase == 'native_seed_wait_1':
			_pose_record(state, 'T2_native_callback', state['filter'])
			state['phase'] = 'native_seed_wait_2'
			state['next_at'] = _now() + STAGE_WAIT_SECONDS
			return False

		if phase == 'native_seed_wait_2':
			if not _pose_record(
					state, 'T3_native_callback', state['filter']):
				_next_candidate(
					state, 'complete pose unavailable after native seed')
				return False
			_attach_physics(state, descriptor, player)
			return False

		if phase == 'physics_wait':
			if not _pose_record(state, 'T6_physics_callback', state['filter']):
				_next_candidate(state, 'complete pose unavailable after physics')
				return False
			initial, unused_yaw = _filter_pose(state['filter'])
			if initial is None:
				raise RuntimeError('native body matrix has no position')
			state['initial'] = initial
			state['initial_speed'] = _speed_snapshot(state['filter'])
			state['initial_contacts'] = _contact_snapshot(state['filter'])
			LOG_NOTE(
				'NATIVE_PHYSICS_PROBE STAGE drive candidate=%s initial=%s '
				'speed=%s contacts=%s' % (
					state['candidate'], _point_text(initial),
					state['initial_speed'], state['initial_contacts']))
			state['filter'].notifyInputKeysDown(1, 0)
			state['phase'] = 'drive'
			state['next_at'] = _now() + DRIVE_SECONDS
			return False

		if phase == 'drive':
			state['filter'].notifyInputKeysDown(0, 0)
			final, unused_yaw = _filter_pose(state['filter'])
			if final is None:
				raise RuntimeError('native body matrix lost its position')
			initial = state['initial']
			distance = _distance_3d(initial, final)
			speeds = _speed_snapshot(state['filter'])
			contacts = _contact_snapshot(state['filter'])
			detail = (
				'candidate=%s distance=%.2fm initial=%s final=%s '
				'speed0=%s speed1=%s contacts0=%s contacts1=%s' % (
					state['candidate'], distance, _point_text(initial),
					_point_text(final), state.get('initial_speed'), speeds,
					state.get('initial_contacts'), contacts))
			passed = (_finite(distance) and distance >= MIN_PASS_DISTANCE and
					all(_finite(value) for value in final))
			_finish(state, passed, detail)
			return passed

		if phase == 'cleanup':
			_destroy_entity(state)
			state['phase'] = 'done'
			if _ACTIVE_SERIAL[0] == state['token']:
				_ACTIVE_SERIAL[0] = 0
			LOG_NOTE(
				'NATIVE_PHYSICS_PROBE CLEANUP token=%d passed=%s' % (
					state['token'], state.get('passed')))
			return bool(state.get('passed'))

		raise RuntimeError('unknown probe phase %r' % phase)
	except Exception as error:
		_fail(state, state.get('phase', phase), error)
		return False
