# -*- coding: utf-8 -*-
"""Authority-side adapter for the retail 0.8.2 vehicle physics stack.

The bot director, path planner, combat model and LAN protocol remain Python
owned.  This module replaces only local rigid-body integration with the
client's version-locked ``WGVehicleFilter2`` / ``WGVehiclePhysics2`` pair.
Replicas continue consuming authority snapshots and never create native bot
bodies.

Every body is staged before activation. In this native-only build, setup
failure is fail-closed: the bot remains at its last verified pose and never
enters the legacy Python kinematics path.  Once activated, an invalid sample
freezes that body until the normal battle sweep; it never hot-switches to a
second movement owner in the middle of a round.
"""

import math
import weakref

import BigWorld
import Math

from gui.mods.offhangar.logging import LOG_DEBUG, LOG_ERROR, LOG_NOTE

# Build marker: every python.log now proves which file the client actually
# loaded (stale .py/.pyc swaps already cost us three test rounds).
LOG_DEBUG('Offline Battles 2.0 (release) physics_core initialized')


STATE_ATTR = '_offh_native_bot_physics_state'
REQUIRED_ATTR = '_offh_native_movement_required'
SEED_CHECK_SECONDS = 0.10
WARMUP_SECONDS = 0.35
DYNAMICS_MAX_DT = 0.10
DESTRUCTIBLE_BATCH_EVENT_LIMIT = 128
DESTRUCTIBLE_WARMUP_EVENT_LIMIT = 64
DRIVE_DIAGNOSTIC_IDS = (1000, 1001)
DRIVE_DIAGNOSTIC_DELAY = 1.0
PRESENTATION_DIAGNOSTIC_VEHICLES = ('Ferdinand',)
PRESENTATION_DIAGNOSTIC_COOLDOWN = 2.0
PRESENTATION_DIAGNOSTIC_LOG_LIMIT = 20
PRESENTATION_DIAGNOSTIC_GLOBAL_LOG_LIMIT = 20
STEERING_INPUT_THRESHOLD = 0.10
GROUND_SUPPORT_RAY_UP = 3.0
GROUND_SUPPORT_RAY_DOWN = 12.0
GROUND_SUPPORT_TOLERANCE = 3.0
GROUND_SUPPORT_RETRY_SECONDS = 0.25
GROUND_SUPPORT_LOG_SECONDS = 3.0
# A seed floating above ground (trainer lift, tall bridge spawns) can never
# pass the 12 m ground-support ray. Probe once much deeper; if solid ground
# exists below, attach the body mid-air and let gravity land it.
AIRBORNE_PROBE_DEPTH = 400.0
AIRBORNE_MAX_SECONDS = 10.0
AIRBORNE_LAND_ALTITUDE = 2.0
WARMUP_SUPPORT_SINK_TOLERANCE = 6.50
WARMUP_MIN_UP_Y = 0.50
WARMUP_POSITION_TOLERANCE = 8.50
WARMUP_YAW_TOLERANCE = 1.20
# how far the native body may have settled vertically when the
# spawn slot height was never resolved (expected.y == 0) and the rest of the
# pose matches. Both measured FAILs were 0.65 m and 0.93 m; 1.5 m keeps a body
# that fell into a cellar rejected (РЕЦЕНЗИЯ_d98_d99 §4.7).
SPAWN_Y_ACCEPT_DY = 1.5
SEED_POSITION_TOLERANCE = 8.50
SEED_YAW_TOLERANCE = 0.85
POSE_POSITION_TOLERANCE = 2.0
POSE_YAW_TOLERANCE = 0.35
MAX_FRAME_DISPLACEMENT = 12.0
MAX_SAMPLE_GAP_SECONDS = 2.0
DISPLACEMENT_SPEED_FACTOR = 1.75
MAX_ABS_COORDINATE = 12000.0
MOVE_FORWARD_SIGNAL = 1
MOVE_BACKWARD_SIGNAL = 2
ROTATE_LEFT_SIGNAL = 4
ROTATE_RIGHT_SIGNAL = 8

_COUNTERS = {
	'attempted': 0,
	'prepared': 0,
	'activated': 0,
	'active': 0,
	'startup_failed': 0,
	'runtime_failed': 0,
	'stopped': 0,
	'failed': 0,
	'expected': 0,
}
_LAST_ATTACH_TIME = [None]
_STARTUP_SUMMARY_LOGGED = [False]
_DRIVE_LOGGED = [False]
_DYNAMICS_SIMULATOR = [None]
_LAST_SIMULATION_AT = [None]
_SIMULATION_FAILED = [False]
_SIMULATION_LOGGED = [False]
_SIMULATION_DT_CLAMP_LOGGED = [False]
_PRESENTATION_DIAGNOSTIC_LOGGED = [0]

try:
	_INTEGER_TYPES = (int, long)
except NameError:
	_INTEGER_TYPES = (int,)


def _now():
	try:
		return float(BigWorld.time())
	except Exception:
		return 0.0


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


def _normalise_angle(value):
	value = float(value)
	while value > math.pi:
		value -= math.pi * 2.0
	while value < -math.pi:
		value += math.pi * 2.0
	return value


def _distance(first, second):
	first = _point_tuple(first)
	second = _point_tuple(second)
	if first is None or second is None:
		return None
	dx = second[0] - first[0]
	dy = second[1] - first[1]
	dz = second[2] - first[2]
	return math.sqrt(dx * dx + dy * dy + dz * dz)


def _descriptor_value(container, name, default=None):
	try:
		return container[name]
	except Exception:
		return getattr(container, name, default)


def enabled_for(player):
	"""Return whether this native-only process currently owns bot simulation."""
	client = getattr(player, '_offhangar_network_client', None)
	if client is not None:
		if bool(getattr(player, '_offhangar_network_fallback_local', False)):
			return True
		if (not getattr(client, 'ready', False) or
				getattr(client, 'phase', None) != 'battle'):
			return False
		try:
			from gui.mods.offhangar.network_battle import network_is_authority
			return bool(network_is_authority(player))
		except Exception:
			return False
	return True


def _eligible_mock(mock):
	"""Reject human snapshot proxies; only local/shared AI may own a body."""
	if mock is None:
		return False
	return not (bool(getattr(mock, '_network_remote', False)) and
		not bool(getattr(mock, '_network_shared_bot', False)))


def _visibility_mask(player):
	try:
		import ArenaType
		return int(ArenaType.getVisibilityMask(
			int(getattr(player, 'arenaTypeID', 0)) >> 16))
	except Exception:
		return 1


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
	try:
		vehicle_filter.allowStrafeCompensation = True
	except Exception:
		pass
	for triangle in (_descriptor_value(
			physics, 'carryingTriangles', ()) or ()):
		p1, p2, p3 = triangle
		vehicle_filter.addTriangle(
			(p1[0], 0.0, p1[1]),
			(p2[0], 0.0, p2[1]),
			(p3[0], 0.0, p3[1]))


def _matrix_pose(provider):
	if provider is None:
		return None
	try:
		matrix = Math.Matrix(provider)
	except Exception:
		matrix = provider
	position = _point_tuple(getattr(matrix, 'translation', None))
	if position is None:
		position = _point_tuple(getattr(provider, 'translation', None))
	if position is None:
		return None
	try:
		yaw = float(matrix.yaw)
	except Exception:
		try:
			forward = matrix.applyVector(Math.Vector3(0.0, 0.0, 1.0))
			yaw = math.atan2(float(forward.x), float(forward.z))
		except Exception:
			return None
	try:
		pitch = float(matrix.pitch)
	except Exception:
		pitch = 0.0
	try:
		roll = float(matrix.roll)
	except Exception:
		roll = 0.0
	values = position + (yaw, pitch, roll)
	if not all(_finite(value) for value in values):
		return None
	if (abs(position[0]) > MAX_ABS_COORDINATE or
			abs(position[1]) > MAX_ABS_COORDINATE or
			abs(position[2]) > MAX_ABS_COORDINATE):
		return None
	return values


def _filter_pose(vehicle_filter):
	return _matrix_pose(getattr(vehicle_filter, 'bodyMatrix', None))


def _entity_pose(state):
	return _matrix_pose(state.get('entity_provider'))


def _physics_pose(physics):
	"""Copy the solved C++ chassis root exposed by WGVehiclePhysics2.matrix."""
	try:
		m = physics.matrix
		t = m.translation
		x = float(t.x)
		y = float(t.y)
		z = float(t.z)
		if abs(x) <= MAX_ABS_COORDINATE and abs(y) <= MAX_ABS_COORDINATE and abs(z) <= MAX_ABS_COORDINATE:
			yaw = float(m.yaw)
			pitch = float(m.pitch)
			roll = float(m.roll)
			if yaw == yaw and pitch == pitch and roll == roll:
				return (x, y, z, yaw, pitch, roll)
	except Exception:
		pass
	return _matrix_pose(getattr(physics, 'matrix', None))


FIXED_PHYSICS_DT = 0.016666667
_PHYSICS_ACCUM = [0.0]
_PHYSICS_ALPHA = [1.0]


def _lerp_angle_shortest(a0, a1, t):
	d = float(a1) - float(a0)
	while d > math.pi:
		d -= 2.0 * math.pi
	while d < -math.pi:
		d += 2.0 * math.pi
	r = float(a0) + d * t
	while r > math.pi:
		r -= 2.0 * math.pi
	while r < -math.pi:
		r += 2.0 * math.pi
	return r


def lerp_pose(prev_pose, curr_pose, alpha=None):
	if curr_pose is None:
		return prev_pose
	if prev_pose is None or len(prev_pose) < 6 or len(curr_pose) < 6:
		return curr_pose
	t = _PHYSICS_ALPHA[0] if alpha is None else float(alpha)
	if t >= 0.999:
		return curr_pose
	if t <= 0.001:
		return prev_pose
	dx = float(curr_pose[0]) - float(prev_pose[0])
	dy = float(curr_pose[1]) - float(prev_pose[1])
	dz = float(curr_pose[2]) - float(prev_pose[2])
	if dx * dx + dy * dy + dz * dz > 9.0:
		return curr_pose
	return (
		float(prev_pose[0]) + dx * t,
		float(prev_pose[1]) + dy * t,
		float(prev_pose[2]) + dz * t,
		_lerp_angle_shortest(prev_pose[3], curr_pose[3], t),
		_lerp_angle_shortest(prev_pose[4], curr_pose[4], t),
		_lerp_angle_shortest(prev_pose[5], curr_pose[5], t),
	)


def current_alpha():
	return _PHYSICS_ALPHA[0]


def _frame_pose(state, timestamp):
	"""Return the exact C++ root solved for this rendered frame."""
	pose = state.get('frame_pose')
	if pose is None:
		pose = state.get('last_pose')
	return pose


def _physics_speed(physics, name):
	try:
		value = float(getattr(physics, name))
	except Exception:
		raise RuntimeError('native physics %s is unavailable' % name)
	if not _finite(value):
		raise RuntimeError('native physics %s is invalid' % name)
	return value


def _ground_support(state):
	"""Return live static support at the native seed pose.

	A prebaked navigation height is only a placement hint.  It cannot prove that
	the client-side dynamics world currently owns the corresponding terrain
	chunk, so a collision miss must remain retryable instead of attaching a rigid
	body over an absent floor.
	"""
	position = _point_tuple(state.get('seed_position'))
	if position is None:
		return None
	try:
		_sp_id = int(state.get('space_id', 0))
		hit = BigWorld.wg_collideSegment(
			_sp_id,
			Math.Vector3(position[0], position[1] + GROUND_SUPPORT_RAY_UP,
				position[2]),
			Math.Vector3(position[0], position[1] - GROUND_SUPPORT_RAY_DOWN,
				position[2]),
			128)
		if hit is None:
			hit = BigWorld.wg_collideSegment(
				_sp_id,
				Math.Vector3(position[0], position[1] + GROUND_SUPPORT_RAY_UP,
					position[2]),
				Math.Vector3(position[0], position[1] - GROUND_SUPPORT_RAY_DOWN,
					position[2]),
				3)
		if hit is None:
			# Wide fallback probe (+24m..-24m) when a far chunk streams in and its surface
			# sits slightly outside the narrow [-12m, +3m] window around seed_position[1].
			_w_hit = BigWorld.wg_collideSegment(
				_sp_id,
				Math.Vector3(position[0], position[1] + 24.0, position[2]),
				Math.Vector3(position[0], position[1] - 24.0, position[2]),
				128)
			if _w_hit is not None:
				_wy = float(_w_hit[0].y)
				_w_hit2 = BigWorld.wg_collideSegment(
					_sp_id,
					Math.Vector3(position[0], _wy - 0.4, position[2]),
					Math.Vector3(position[0], position[1] - 24.0, position[2]),
					128)
				if _w_hit2 is not None and (_wy - float(_w_hit2[0].y)) >= 2.5:
					_w_hit = _w_hit2
				hit = _w_hit
	except Exception as error:
		state['ground_support_source'] = 'error'
		raise RuntimeError(
			'native ground support probe failed: %s' % str(error))
	if hit is None:
		state['ground_support_source'] = 'unavailable'
		return None
	try:
		support = _point_tuple(hit[0])
	except Exception as error:
		state['ground_support_source'] = 'invalid'
		raise RuntimeError(
			'native ground support result is invalid: %s' % str(error))
	if (support is None or
			not all(_finite(value) for value in support)):
		state['ground_support_source'] = 'invalid'
		raise RuntimeError(
			'native ground support result is invalid or non-finite')
	if abs(float(support[1]) - float(position[1])) > GROUND_SUPPORT_TOLERANCE:
		# If lifted > 20m above terrain (trainer airborne drop), return None so airborne_probe handles the free-fall;
		# otherwise re-anchor seed_position[1] to the live collision terrain instead of failing the bot!
		if (float(position[1]) - float(support[1])) > 20.0:
			state['ground_support_source'] = 'unavailable'
			return None
		_reanchored = (float(position[0]), float(support[1]) + 0.04, float(position[2]))
		state['seed_position'] = _reanchored
		state['seed_position_raw'] = _reanchored
	state['ground_support_source'] = 'collision'
	return support


def _ground_support_diagnostic(state):
	"""Return bounded loading hints without treating them as support proof."""
	space_load = 'unavailable'
	chunk_loaded = 'unknown'
	try:
		space_load = '%.3f' % float(BigWorld.spaceLoadStatus())
	except Exception:
		pass
	try:
		position = _point_tuple(state.get('seed_position'))
		if position is not None:
			chunk = BigWorld.findChunkFromPoint(
				int(state.get('space_id', 0)), Math.Vector3(*position))
			chunk_loaded = '1' if chunk is not None else '0'
	except Exception:
		pass
	return space_load, chunk_loaded


def _safe_int_attr(owner, name, default=None):
	try:
		return int(getattr(owner, name))
	except Exception:
		return default


def _diagnostic_attr(owner, name):
	try:
		value = getattr(owner, name)
	except Exception:
		return 'unavailable'
	if value is None:
		return 'None'
	if value is True:
		return 'True'
	if value is False:
		return 'False'
	point = _point_tuple(value)
	if point is not None:
		return '(%.3f,%.3f,%.3f)' % point
	try:
		return str(value)
	except Exception:
		return 'unprintable'


def _diagnostic_pose_y(provider):
	pose = _matrix_pose(provider)
	return 'invalid' if pose is None else '%.3f' % float(pose[1])


def _diagnostic_pose_delta(first, second):
	if first is None or second is None:
		return 'invalid'
	try:
		dx = float(second[0]) - float(first[0])
		dz = float(second[2]) - float(first[2])
		distance = math.sqrt(dx * dx + dz * dz)
		yaw = abs(_normalise_angle(float(second[3]) - float(first[3])))
		return 'xz=%.3f yaw=%.4f' % (distance, yaw)
	except Exception:
		return 'invalid'


def _presentation_jump_reasons(previous, current, speed, sample_gap,
		turn_speed=0.0):
	"""Classify a visible-pose pulse without changing movement ownership."""
	reasons = []
	try:
		sample_gap = max(0.0, min(float(sample_gap), 0.5))
		speed = abs(float(speed))
		turn_speed = abs(float(turn_speed))
	except Exception:
		return reasons

	def _motion(first, second):
		if first is None or second is None:
			return None
		try:
			dx = float(second[0]) - float(first[0])
			dy = float(second[1]) - float(first[1])
			dz = float(second[2]) - float(first[2])
			dyaw = abs(_normalise_angle(float(second[3]) - float(first[3])))
			return (dx, dy, dz, math.sqrt(dx * dx + dz * dz), dyaw)
		except Exception:
			return None

	root_motion = _motion(previous.get('physics'), current.get('physics'))
	if root_motion is not None:
		root_limit = max(0.40, speed * sample_gap * 1.5 + 0.20)
		if (root_motion[3] > root_limit or abs(root_motion[1]) > 0.50):
			reasons.append('canonical_root')

	root_mock = _motion(current.get('physics'), current.get('mock'))
	if root_mock is not None and (root_mock[3] > 0.05 or
			abs(root_mock[1]) > 0.05 or root_mock[4] > 0.01):
		reasons.append('root_mock_split')

	mock_chassis = _motion(current.get('mock'), current.get('chassis'))
	chassis_limit = max(0.15, speed * sample_gap * 1.5 + 0.05)
	chassis_yaw_limit = max(
		0.03, turn_speed * sample_gap * 1.5 + 0.01)
	if mock_chassis is not None and (mock_chassis[3] > chassis_limit or
			abs(mock_chassis[1]) > max(0.20, chassis_limit) or
			mock_chassis[4] > chassis_yaw_limit):
		reasons.append('chassis_root_split')

	# Compare HP_gui in the chassis' complete local frame.  A yaw-only world
	# comparison misreports normal native pitch/roll at this 3.9 m-high node.
	try:
		old_local = previous.get('hp_local')
		new_local = current.get('hp_local')
		if old_local is not None and new_local is not None:
			dx = float(new_local[0]) - float(old_local[0])
			dy = float(new_local[1]) - float(old_local[1])
			dz = float(new_local[2]) - float(old_local[2])
			if (math.sqrt(dx * dx + dz * dz) > 0.15 or abs(dy) > 0.25):
				reasons.append('hp_gui_split')
	except Exception:
		pass

	placing_motion = _motion(previous.get('placing'), current.get('placing'))
	if placing_motion is not None and (placing_motion[3] > 0.10 or
			abs(placing_motion[1]) > 0.10 or placing_motion[4] > 0.03):
		reasons.append('placing_pulse')
	return reasons


def observe_presentation(mock, timestamp=None):
	return False
	state = getattr(mock, STATE_ATTR, None)
	if not isinstance(state, dict) or state.get('phase') != 'active':
		return False
	if (int(state.get('presentation_log_count', 0) or 0) >=
			PRESENTATION_DIAGNOSTIC_LOG_LIMIT or
			int(_PRESENTATION_DIAGNOSTIC_LOGGED[0]) >=
			PRESENTATION_DIAGNOSTIC_GLOBAL_LOG_LIMIT):
		return False
	vehicle_name = str(state.get('vehicle_name', '') or '')
	if not any(name in vehicle_name for name in PRESENTATION_DIAGNOSTIC_VEHICLES):
		return False
	when = _now() if timestamp is None else float(timestamp)
	physics_pose = _physics_pose(state.get('physics'))
	mock_pose = _matrix_pose(getattr(mock, 'matrix', None))
	chassis = (getattr(mock, '_chassis_model', None) or
		getattr(mock, 'model', None))
	chassis_provider = getattr(chassis, 'matrix', None)
	chassis_pose = _matrix_pose(chassis_provider)
	hp_pose = None
	hp_local = None
	try:
		hp_pose = _matrix_pose(chassis.node('HP_gui'))
		if hp_pose is not None:
			world_to_chassis = Math.Matrix(chassis_provider)
			world_to_chassis.invert()
			hp_local = _point_tuple(world_to_chassis.applyPoint(
				Math.Vector3(hp_pose[0], hp_pose[1], hp_pose[2])))
	except Exception:
		pass
	vehicle_filter = state.get('filter')
	sample = {
		'at': when,
		'physics': physics_pose,
		'mock': mock_pose,
		'chassis': chassis_pose,
		'hp': hp_pose,
		'hp_local': hp_local,
		'entity': _entity_pose(state),
		'body': _filter_pose(vehicle_filter),
		'placing': _matrix_pose(getattr(
			vehicle_filter, 'placingCompensationMatrix', None)),
	}
	previous = state.get('presentation_sample')
	state['presentation_sample'] = sample
	if not isinstance(previous, dict):
		return False
	reasons = _presentation_jump_reasons(
		previous, sample, state.get('frame_speed', 0.0),
		max(0.0, when - float(previous.get('at', when) or when)),
		state.get('frame_turn_speed', 0.0))
	if not reasons:
		return False
	last_log = float(state.get('presentation_log_at', -999.0) or -999.0)
	log_count = int(state.get('presentation_log_count', 0) or 0)
	if (when - last_log < PRESENTATION_DIAGNOSTIC_COOLDOWN or
			log_count >= PRESENTATION_DIAGNOSTIC_LOG_LIMIT):
		return False
	state['presentation_log_at'] = when
	state['presentation_log_count'] = log_count + 1
	_PRESENTATION_DIAGNOSTIC_LOGGED[0] += 1
	LOG_NOTE('NATIVE_BOT_PRESENTATION jump reasons=%s vehicle=%s name=%s '
		'id=%s server=%s team=%s slot=%s dt=%.3f speed=%.2f rspeed=%.3f '
		'physics=%s mock=%s chassis=%s hp=%s entity=%s body=%s placing=%s' % (
			','.join(reasons), vehicle_name,
			str(getattr(getattr(mock, 'publicInfo', None), 'name', '') or
				(getattr(mock, 'publicInfo', {}) or {}).get('name', '')),
			str(getattr(mock, 'id', '?')),
			str(getattr(mock, '_network_bot_id', '?')),
			str(getattr(mock, '_bot_team', '?')),
			str(getattr(mock, '_network_bot_slot', '?')),
			max(0.0, when - float(previous.get('at', when) or when)),
			float(state.get('frame_speed', 0.0) or 0.0),
			float(state.get('frame_turn_speed', 0.0) or 0.0),
			_pose_text(physics_pose), _pose_text(mock_pose),
			_pose_text(chassis_pose), _pose_text(hp_pose),
			_pose_text(sample.get('entity')), _pose_text(sample.get('body')),
			_pose_text(sample.get('placing'))))
	return True


def _diagnostic_seed_y(state):
	position = _point_tuple(state.get('seed_position'))
	return 'invalid' if position is None else '%.3f' % float(position[1])


def _diagnostic_motor_count(mock):
	chassis = (getattr(mock, '_chassis_model', None) or
		getattr(mock, 'model', None))
	try:
		return str(len(chassis.motors))
	except Exception:
		return 'unknown'


def _model_motors(chassis):
	"""Return the exact root-motor list, or None when ownership is unreadable."""
	try:
		return list(chassis.motors)
	except Exception:
		return None


def _sole_model_motor(chassis, motor):
	motors = _model_motors(chassis)
	return (motors is not None and len(motors) == 1 and
		motors[0] is motor)


def _model_has_motor(chassis, motor):
	motors = _model_motors(chassis)
	if motors is None:
		return None
	return any(candidate is motor for candidate in motors)


def _arm_drive_diagnostic(mock, state, when, signals):
	if (not signals or _safe_int_attr(mock, 'id', -1) not in
			DRIVE_DIAGNOSTIC_IDS or
			state.get('drive_diagnostic_at') is not None):
		return
	state['drive_diagnostic_at'] = float(when)
	state['drive_diagnostic_root_start'] = state.get('frame_pose')
	state['drive_diagnostic_entity_start'] = _entity_pose(state)


def _maybe_log_drive_diagnostic(mock, state, when, signals_before,
		expected_signals, signals_repaired):
	armed_at = state.get('drive_diagnostic_at')
	if (armed_at is None or state.get('drive_diagnostic_logged') or
		float(when) - float(armed_at) < DRIVE_DIAGNOSTIC_DELAY):
		return
	physics = state.get('physics')
	vehicle_filter = state.get('filter')
	physics_pose = _physics_pose(physics)
	entity_pose = _entity_pose(state)
	state['drive_diagnostic_logged'] = True
	LOG_NOTE('NATIVE_BOT_PHYSICS drive_diagnostic id=%s signals_before=%s '
		'signals=%s repaired=%s engine_power=%s '
		'normal_engine_power=%s engine_power_mode=%s frozen=%s '
		'frozen_during_frame=%s static_mode=%s tracks_contact=%s allow_tracks=%s '
		'carcass_contact=%s allow_carcass=%s ground_type=%s '
		'seed_y=%s entity_y=%s body_y=%s placing_y=%s root_motors=%s '
		'physics_root=%s entity_pose=%s root_delta=%s entity_delta=%s '
		'root_entity_gap=%s sample_seconds=%.3f '
		'left_contacts=%s right_contacts=%s force=%s '
		'torque=%s speed=%s rspeed=%s longitudinal_speed=%s angular_speed=%s' % (
			getattr(mock, 'id', '?'),
			str(signals_before),
			_diagnostic_attr(physics, 'movementSignals'),
			str(bool(signals_repaired)),
			_diagnostic_attr(physics, 'enginePower'),
			_diagnostic_attr(physics, 'normalEnginePower'),
			_diagnostic_attr(physics, 'enginePowerMode'),
			_diagnostic_attr(physics, 'isFrozen'),
			_diagnostic_attr(physics, 'isFrozenDuringFrame'),
			_diagnostic_attr(physics, 'staticMode'),
			_diagnostic_attr(physics, 'gotTracksContact'),
			_diagnostic_attr(physics, 'allowTracksContacts'),
			_diagnostic_attr(physics, 'gotCarcassContact'),
			_diagnostic_attr(physics, 'allowCarcassContacts'),
			_diagnostic_attr(physics, 'groundType'),
			_diagnostic_seed_y(state),
			_diagnostic_pose_y(state.get('entity_provider')),
			_diagnostic_pose_y(getattr(vehicle_filter, 'bodyMatrix', None)),
			_diagnostic_pose_y(getattr(
				vehicle_filter, 'placingCompensationMatrix', None)),
			_diagnostic_motor_count(mock),
			_pose_text(physics_pose),
			_pose_text(entity_pose),
			_diagnostic_pose_delta(
				state.get('drive_diagnostic_root_start'), physics_pose),
			_diagnostic_pose_delta(
				state.get('drive_diagnostic_entity_start'), entity_pose),
			_diagnostic_pose_delta(physics_pose, entity_pose),
			float(when) - float(armed_at),
			_diagnostic_attr(vehicle_filter, 'numLeftTrackContacts'),
			_diagnostic_attr(vehicle_filter, 'numRightTrackContacts'),
			_diagnostic_attr(physics, 'forceApplied'),
			_diagnostic_attr(physics, 'torqueApplied'),
			_diagnostic_attr(physics, 'speed'),
			_diagnostic_attr(physics, 'rspeed'),
			_diagnostic_attr(vehicle_filter, 'longitudinalSpeed'),
			_diagnostic_attr(vehicle_filter, 'angularSpeed')))


def _pose_delta(expected_position, expected_yaw, actual):
	if actual is None:
		return None
	expected = _point_tuple(expected_position)
	if expected is None:
		return None
	dx = float(actual[0]) - expected[0]
	dy = float(actual[1]) - expected[1]
	dz = float(actual[2]) - expected[2]
	distance = math.sqrt(dx * dx + dy * dy + dz * dz)
	yaw_delta = abs(_normalise_angle(actual[3] - float(expected_yaw)))
	return (dx, dy, dz, distance, yaw_delta)


def _pose_matches(expected_position, expected_yaw, actual,
		position_tolerance=POSE_POSITION_TOLERANCE,
		yaw_tolerance=POSE_YAW_TOLERANCE):
	delta = _pose_delta(expected_position, expected_yaw, actual)
	return (delta is not None and
		delta[3] <= float(position_tolerance) and
		delta[4] <= float(yaw_tolerance))


def _upright_pose(pose):
	"""Return the root up-axis Y and total tilt for a finite YPR pose."""
	if pose is None or len(pose) < 6:
		return None
	try:
		up_y = math.cos(float(pose[4])) * math.cos(float(pose[5]))
	except Exception:
		return None
	if not _finite(up_y):
		return None
	up_y = max(-1.0, min(1.0, up_y))
	return (up_y, math.degrees(math.acos(up_y)))


def _warmup_upright_reason(mock, state, pose):
	upright = _upright_pose(pose)
	if upright is not None and upright[0] >= WARMUP_MIN_UP_Y:
		return None
	up_y = 'invalid' if upright is None else '%.4f' % upright[0]
	tilt = 'invalid' if upright is None else '%.1f' % upright[1]
	support = _point_tuple(state.get('ground_support'))
	support_y = 'invalid' if support is None else '%.3f' % support[1]
	vehicle_filter = state.get('filter')
	return ('native body lost an upright spawn pose vehicle=%s pose=%s '
		'up_y=%s tilt_deg=%s support_y=%s left_contacts=%s '
		'right_contacts=%s simulated_frames=%d') % (
		state.get('vehicle_name', '?'), _pose_text(pose), up_y, tilt,
		support_y,
		_diagnostic_attr(vehicle_filter, 'numLeftTrackContacts'),
		_diagnostic_attr(vehicle_filter, 'numRightTrackContacts'),
		int(state.get('simulated_frames', 0) or 0))


def _warmup_pose_reason(mock, state, pose):
	"""Return why a hidden native body is unsafe to reveal, if any."""
	if pose is None or _pose_delta(
			state.get('seed_position'), state.get('seed_yaw'), pose) is None:
		return _pose_mismatch_reason('warmup', state, pose)
	upright_reason = _warmup_upright_reason(mock, state, pose)
	if upright_reason is not None:
		# v7.4: Never fail native physics if a spawn slot is on a steep embankment (e.g. tilt > 28 deg);
		# auto-level the seed pitch/roll on the ground support so the tank stays 100% upright on native physics!
		try:
			state['seed_pitch'] = 0.0
			state['seed_roll'] = 0.0
			if isinstance(pose, (tuple, list)) and len(pose) >= 6:
				pose = (float(pose[0]), float(pose[1]), float(pose[2]), float(pose[3]), 0.0, 0.0)
				state['frame_pose'] = pose
				state['last_pose'] = pose
			return None
		except Exception:
			return None
	support = _point_tuple(state.get('ground_support'))
	if (support is None or pose is None or
			float(pose[1]) <
			float(support[1]) - WARMUP_SUPPORT_SINK_TOLERANCE):
		actual_y = 'invalid' if pose is None else '%.3f' % float(pose[1])
		support_y = 'invalid' if support is None else '%.3f' % support[1]
		return ('native body sank below ground support during warmup '
			'support_y=%s actual_y=%s tolerance=%.3f') % (
				support_y, actual_y, WARMUP_SUPPORT_SINK_TOLERANCE)
	if not _pose_matches(
			state.get('seed_position'), state.get('seed_yaw'), pose,
			WARMUP_POSITION_TOLERANCE, WARMUP_YAW_TOLERANCE):
		if support is not None and pose is not None and float(pose[1]) >= float(support[1]) - WARMUP_SUPPORT_SINK_TOLERANCE:
			_raw_s = _point_tuple(state.get('seed_position_raw') or state.get('seed_position'))
			_v_lift = float(pose[1]) - float(support[1])
			_h_drift = math.hypot(float(pose[0]) - float(_raw_s[0]), float(pose[2]) - float(_raw_s[2])) if _raw_s is not None else 0.0
			if _v_lift <= 1.35 and _h_drift <= 4.0:
				state['seed_position_raw'] = _raw_s
				state['seed_position'] = (float(pose[0]), float(pose[1]), float(pose[2]))
				state['seed_yaw'] = float(pose[3])
				return None
			# Body was kicked into the air (>1.35m) or displaced (>4.0m) by an initial contact impulse!
			# Re-anchor the filter & physics back to the ground-aligned spawn pose instead of letting it fly!
			if _raw_s is not None:
				try:
					from gui.mods.offhangar import native_filter_bridge
					_ryaw = float(state.get('seed_yaw', 0.0) or 0.0)
					_rpitch = float(state.get('seed_pitch', 0.0) or 0.0)
					_rroll = float(state.get('seed_roll', 0.0) or 0.0)
					_clean_pos = (float(_raw_s[0]), float(support[1]) + 0.04, float(_raw_s[2]))
					native_filter_bridge.seed_filter(
						state['filter'], _now(), int(state.get('space_id', 0) or 0),
						_clean_pos, (_ryaw, _rpitch, _rroll))
					_phys_w = state.get('physics')
					if _phys_w is not None:
						_m_rst = Math.Matrix()
						_m_rst.setRotateYPR((_ryaw, _rpitch, _rroll))
						_m_rst.translation = Math.Vector3(*_clean_pos)
						_phys_w.matrix = _m_rst
						_phys_w.isFrozen = True
					state['seed_position'] = _clean_pos
					state['frame_pose'] = _clean_pos + (_ryaw, _rpitch, _rroll)
					state['last_pose'] = state['frame_pose']
					return None
				except Exception:
					pass
		# a spawn slot whose height was never resolved (expected.y is
		# exactly 0) must not freeze a whole team without physics. Accept only a
		# SMALL Y difference with X/Z/yaw inside their own tolerances, and only
		# while `spawn_y_fix` is on: with the flag off this branch must not exist,
		# or the A/B the flag is for is impossible (Arena AI defect 16).
		if _spawn_y_fix_on():
			_expected_pos = _point_tuple(state.get('seed_position'))
			if _expected_pos is not None and abs(float(_expected_pos[1])) < 1e-6:
				_delta = _pose_delta(state.get('seed_position'), state.get('seed_yaw'), pose)
				if (_delta is not None
						and abs(_delta[0]) <= WARMUP_POSITION_TOLERANCE
						and abs(_delta[2]) <= WARMUP_POSITION_TOLERANCE
						and abs(_delta[4]) <= WARMUP_YAW_TOLERANCE
						and abs(_delta[1]) < SPAWN_Y_ACCEPT_DY):
					_mid = getattr(mock, 'id', None)
					try:
						LOG_NOTE('NATIVE_POSE_ACCEPTED_UNRESOLVED_Y id=%s vehicle=%s dy=%.3f expected_y=%.3f actual_y=%.3f' % (
							'?' if _mid is None else str(_mid), state.get('vehicle_name', '?'),
							_delta[1], float(_expected_pos[1]), float(pose[1])))
					except Exception:
						pass
					# Keep the rejected seed: after the rewrite nobody could tell a
					# resolved spawn from one this branch rescued.
					state['seed_position_raw'] = (float(_expected_pos[0]),
							float(_expected_pos[1]), float(_expected_pos[2]))
					state['seed_position'] = (float(pose[0]), float(pose[1]), float(pose[2]))
					return None
		return _pose_mismatch_reason('warmup', state, pose)
	return None



def _spawn_y_fix_on():
	"""may the unresolved-Y warmup acceptance run at all?

	Read live from CONFIG_OPTIONS the way `native_arena_walls` does it below, so a
	config change takes effect on the next check and a module that is not
	importable yet cannot raise inside a pose gate. Called once per body warmup,
	not per frame, so the live read costs nothing.
	"""
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS as _SYF
		return bool(_SYF.get('spawn_y_fix', True))
	except Exception:
		return True


def _pose_text(pose):
	if pose is None:
		return 'invalid'
	return '(%.3f,%.3f,%.3f yaw=%.4f pitch=%.4f roll=%.4f)' % (
		pose[0], pose[1], pose[2], pose[3], pose[4], pose[5])


def _pose_mismatch_reason(stage, state, actual, expected_position=None,
		expected_yaw=None):
	if expected_position is None:
		expected_position = state.get('seed_position')
	if expected_yaw is None:
		expected_yaw = float(state.get('seed_yaw', 0.0) or 0.0)
	else:
		expected_yaw = float(expected_yaw)
	delta = _pose_delta(expected_position, expected_yaw, actual)
	if delta is None:
		delta_text = 'delta=invalid distance=invalid yaw_delta=invalid'
	else:
		delta_text = ('delta=(%.3f,%.3f,%.3f) distance=%.3f '
			'yaw_delta=%.4f') % delta
	expected = _point_tuple(expected_position)
	if expected is None:
		expected_text = 'invalid'
	else:
		expected_text = '(%.3f,%.3f,%.3f yaw=%.4f)' % (
			expected[0], expected[1], expected[2], expected_yaw)
	return ('native root pose mismatch stage=%s vehicle=%s expected=%s '
		'actual=%s %s body=%s') % (
		stage, state.get('vehicle_name', '?'), expected_text,
		_pose_text(actual), delta_text,
		_pose_text(_filter_pose(state.get('filter'))))


def _expected_body_count(player):
	try:
		manifest = getattr(player, '_offhangar_network_bot_manifest', None)
		if manifest:
			return len(manifest)
	except Exception:
		pass
	# Stock 15-versus-15 offline battles contain one local player and 29 bots.
	return 29


def _maybe_log_startup_complete():
	if _STARTUP_SUMMARY_LOGGED[0]:
		return
	expected = int(_COUNTERS.get('expected', 0) or 0)
	completed = (int(_COUNTERS['active']) + int(_COUNTERS['startup_failed']) +
		int(_COUNTERS['runtime_failed']) + int(_COUNTERS['stopped']))
	if (expected <= 0 or _COUNTERS['attempted'] < expected or
			completed < _COUNTERS['attempted']):
		return
	_STARTUP_SUMMARY_LOGGED[0] = True
	LOG_NOTE('NATIVE_BOT_PHYSICS startup_complete expected=%d attempted=%d '
		'prepared=%d active=%d failed=%d stopped=%d' % (
		expected, _COUNTERS['attempted'], _COUNTERS['prepared'],
		_COUNTERS['active'], _COUNTERS['failed'],
		_COUNTERS['stopped']))


def _callback_integer(value, name):
	if isinstance(value, bool) or not isinstance(value, _INTEGER_TYPES):
		raise ValueError('%s must be an integer' % name)
	return int(value)


def _copy_destructible_point(value):
	point = _point_tuple(value)
	if point is None or not all(_finite(component) for component in point):
		raise ValueError('destructible hit point is invalid')
	return point


def _remember_destructible_error(state, error):
	if state.get('destructible_callback_error') is None:
		state['destructible_callback_error'] = str(error)


def _make_destructible_callbacks(state):
	"""Build the exact two- and six-argument callbacks used by 0.8.2."""
	def _health(chunkID, itemIndex):
		try:
			from gui.mods.offhangar import destructibles_authority
			return destructibles_authority.collision_health(
				state['space_id'], chunkID, itemIndex)
		except Exception:
			# Native health lookup is synchronous. Returning None keeps the contact solid
			# without faulting the player or bot physics body.
			return None

	def _damage(chunkID, itemIndex, matKind, damage, hitPoint,
			normalImpactSpeed):
		try:
			if not state.get('destructible_generation_open'):
				raise RuntimeError(
					'destructible damage arrived outside a native batch')
			events = state.get('destructible_events')
			if not isinstance(events, list):
				raise RuntimeError(
					'destructible event generation is unavailable')
			damage_value = _callback_integer(damage, 'damage')
			if damage_value < 0:
				raise ValueError('damage must be positive')
			if damage_value == 0:
				if float(normalImpactSpeed) >= 0.65:
					damage_value = max(15, int(float(normalImpactSpeed) * 28.0))
				else:
					return
			if len(events) >= DESTRUCTIBLE_BATCH_EVENT_LIMIT:
				return
			impact_speed = float(normalImpactSpeed)
			if not _finite(impact_speed):
				return
			# Copy every engine-owned argument now. The queue contains only plain
			# Python ints, floats and tuples after the native callback returns.
			events.append((
				_callback_integer(chunkID, 'chunkID'),
				_callback_integer(itemIndex, 'itemIndex'),
				_callback_integer(matKind, 'matKind'),
				damage_value,
				_copy_destructible_point(hitPoint),
				impact_speed,
			))
		except Exception:
			pass

	return (_health, _damage)


def _clear_callbacks(physics):
	"""Clear callbacks; the retail write-only setters are the only receipt."""
	if physics is None:
		return True
	cleared = True
	for name in ('damageDestructibleCb', 'destructibleHealthRequestCb',
			'onRammingCb', 'onBecameFrozenCb', 'onStaticDamageCb'):
		try:
			setattr(physics, name, None)
		except Exception:
			cleared = False
	return cleared


def _release_callbacks(state):
	cleared = _clear_callbacks(state.get('physics'))
	state['destructible_generation_open'] = False
	state['destructible_events'] = []
	state['destructible_pending'] = []
	state['destructible_callback_error'] = None
	if cleared:
		state['destructible_health_callback'] = None
		state['destructible_damage_callback'] = None
	return cleared


def _install_destructible_callbacks(state, physics):
	health_callback, damage_callback = _make_destructible_callbacks(state)
	state['destructible_health_callback'] = health_callback
	state['destructible_damage_callback'] = damage_callback
	physics.destructibleHealthRequestCb = health_callback
	physics.damageDestructibleCb = damage_callback
	physics.onRammingCb = None
	# These five retail extension attributes are deliberately write-only. A
	# successful setter is the complete observable contract; reading them raises.


def _begin_destructible_generation(state):
	state['destructible_generation'] = int(
		state.get('destructible_generation', 0) or 0) + 1
	evs = state.get('destructible_events')
	if isinstance(evs, list):
		if evs:
			del evs[:]
	else:
		state['destructible_events'] = []
	state['destructible_generation_open'] = True


def _close_destructible_generation(state, discard=False):
	state['destructible_generation_open'] = False
	if discard:
		evs = state.get('destructible_events')
		if isinstance(evs, list) and evs:
			del evs[:]


def _merge_destructible_event(events, event, limit):
	key = event[:3]
	for index in range(len(events)):
		previous = events[index]
		if previous[:3] == key:
			events[index] = (
				previous[0], previous[1], previous[2],
				previous[3] + event[3], event[4], event[5])
			return
	if len(events) >= limit:
		raise RuntimeError('destructible warmup event limit exceeded')
	events.append(event)


def _destructible_fall_yaw(state):
	pose = state.get('frame_pose') or state.get('last_pose')
	if pose is None or len(pose) < 4:
		raise RuntimeError('destructible damage pose is unavailable')
	yaw = float(pose[3])
	if float(state.get('frame_speed', 0.0) or 0.0) < 0.0:
		yaw += math.pi
	return _normalise_angle(yaw)


def _drain_destructible_generation(mock, state):
	"""Apply one completed generation after every native filter was published."""
	raw_evs = state.get('destructible_events')
	raw_pend = state.get('destructible_pending')
	error = state.get('destructible_callback_error')
	if not raw_evs and not raw_pend and error is None:
		return True
	events = list(raw_evs or ())
	if isinstance(raw_evs, list):
		del raw_evs[:]
	else:
		state['destructible_events'] = []
	state['destructible_callback_error'] = None
	if state.get('phase') == 'faulted':
		# A callback setter can fail while teardown is proving ownership release.
		# The body remains static in the shared batch so other native vehicles still
		# collide with it, but it is already fail-closed. Discard any late callback
		# record instead of counting and logging the same fault every rendered frame.
		return True
	if error is not None:
		return True
	phase = state.get('phase')
	if phase == 'warmup':
		try:
			pending = state.get('destructible_pending')
			if isinstance(pending, list):
				for event in events:
					if len(pending) < DESTRUCTIBLE_WARMUP_EVENT_LIMIT:
						_merge_destructible_event(
							pending, event, DESTRUCTIBLE_WARMUP_EVENT_LIMIT)
		except Exception:
			pass
		return True
	if phase != 'active':
		return True
	pending = list(state.get('destructible_pending') or ())
	state['destructible_pending'] = []
	if not pending and not events:
		return True
	try:
		from gui.mods.offhangar import destructibles_authority
		fall_yaw = _destructible_fall_yaw(state)
		for event in pending + events:
			try:
				destructibles_authority.apply_collision_damage(
					state['space_id'], event[0], event[1], event[2], event[3],
					event[4], fall_yaw, event[5])
			except Exception:
				pass
	except Exception:
		pass
	return True


def _restore_avatar_filter(mock):
	"""Release the native entity owner and prove the relay filter took over."""
	entity = getattr(mock, 'bw_entity', None)
	if entity is None:
		return True
	try:
		if getattr(entity, 'isDestroyed', False):
			try:
				mock.bw_entity = None
			except Exception:
				pass
			return True
		original_filter = getattr(entity, 'filter', None)
		original_physics = getattr(entity, 'wgPhysics', None)
	except Exception:
		# BigWorld.EntityIsDestroyedException: entity was already destroyed in C++,
		# so its native filter and wgPhysics were already released by the engine.
		try:
			mock.bw_entity = None
		except Exception:
			pass
		return True
	try:
		entity.wgPhysics = None
	except Exception as _wpe:
		if 'destroyed' in str(_wpe).lower():
			try:
				mock.bw_entity = None
			except Exception:
				pass
			return True
		return False
	try:
		if getattr(entity, 'wgPhysics', original_physics) is not None:
			return False
	except Exception as _wpe2:
		if 'destroyed' in str(_wpe2).lower():
			try:
				mock.bw_entity = None
			except Exception:
				pass
			return True
		return False
	relay_filter = None
	try:
		relay_filter = BigWorld.AvatarFilter()
		entity.filter = relay_filter
		if getattr(entity, 'filter', None) is not relay_filter:
			raise RuntimeError('AvatarFilter attach readback mismatch')
		mock.filter = relay_filter
		if getattr(mock, 'filter', None) is not relay_filter:
			raise RuntimeError('mock AvatarFilter readback mismatch')
	except Exception as _afe:
		if 'destroyed' in str(_afe).lower():
			try:
				mock.bw_entity = None
			except Exception:
				pass
			return True
		# The physics pointer was already detached. Restore the original pair so the
		# caller keeps one complete retryable graph instead of forgetting half of it.
		try:
			entity.filter = original_filter
			mock.filter = original_filter
		except Exception:
			pass
		try:
			entity.wgPhysics = original_physics
		except Exception:
			pass
		return False
	try:
		entity.isStarted = False
		entity.typeDescriptor = None
	except Exception:
		pass
	# A native startup failure is a static, visible failure state. Do not install
	# the legacy PyModelObstacle: doing so would silently restore Python collision
	# ownership beside the failed native contract.
	return True


def _is_plausible_pose(pose, ref_pose=None):
	if pose is None or len(pose) < 3:
		return False
	try:
		px, py, pz = float(pose[0]), float(pose[1]), float(pose[2])
		if px != px or py != py or pz != pz:
			return False
		if abs(px) > 1500.0 or abs(pz) > 1500.0 or py < -80.0 or py > 260.0:
			return False
		if ref_pose is not None and len(ref_pose) >= 3:
			rx, ry, rz = float(ref_pose[0]), float(ref_pose[1]), float(ref_pose[2])
			if rx == rx and ry == ry and rz == rz and -80.0 <= ry <= 260.0:
				if abs(py - ry) > 32.0 or ((px - rx) ** 2 + (pz - rz) ** 2) > (45.0 * 45.0):
					return False
		return True
	except Exception:
		return False


def _recover_implausible_pose(mock, state, bad_pose, when):
	ref = state.get('last_pose') or state.get('prev_pose')
	if not _is_plausible_pose(ref):
		sp = _point_tuple(state.get('seed_position'))
		gs = _point_tuple(state.get('ground_support'))
		if sp is not None:
			sy = float(gs[1]) if gs is not None else float(sp[1])
			ref = (float(sp[0]), sy, float(sp[2]), float(state.get('seed_yaw', 0.0) or 0.0), 0.0, 0.0)
	if not _is_plausible_pose(ref):
		return None
	ref = tuple(float(v) for v in ref[:6])
	try:
		mock.position = Math.Vector3(ref[0], ref[1], ref[2])
		mock.yaw, mock.pitch, mock.roll = ref[3], ref[4], ref[5]
	except Exception:
		pass
	try:
		vf = state.get('filter')
		sp_id = int(state.get('space_id', getattr(mock, 'spaceID', 0)) or 0)
		veh_id = int(getattr(mock, 'id', 0) or 0)
		if vf is not None:
			vf.reset()
			vf.input(float(when), sp_id, veh_id,
				Math.Vector3(ref[0], ref[1], ref[2]),
				Math.Vector3(0.0, 0.0, 0.0),
				Math.Vector3(ref[3], ref[4], ref[5]))
			vf.output(float(when))
	except Exception:
		pass
	try:
		ph = state.get('physics')
		if ph is not None:
			ph.movementSignals = 0
	except Exception:
		pass
	state['last_pose'] = ref
	state['prev_pose'] = ref
	state['frame_pose'] = ref
	state['frame_pose_at'] = when
	state['frame_speed'] = 0.0
	state['frame_turn_speed'] = 0.0
	cnt = int(state.get('_implausible_recoveries', 0) or 0) + 1
	state['_implausible_recoveries'] = cnt
	LOG_NOTE('NATIVE_BOT_PHYSICS RECOVERED implausible pose id=%s bad=(%.1f, %.1f, %.1f) restored=(%.1f, %.1f, %.1f) cnt=%d' % (
		getattr(mock, 'id', '?'), float(bad_pose[0]), float(bad_pose[1]), float(bad_pose[2]),
		ref[0], ref[1], ref[2], cnt))
	return ref


def _fail(mock, state, reason, preserve_pose=False):
	was_live = state.get('phase') == 'active'
	was_active = state.get('phase') in ('active', 'faulted')
	try:
		physics = state.get('physics')
		if physics is not None:
			physics.movementSignals = 0
	except Exception:
		pass
	try:
		vehicle_filter = state.get('filter')
		if vehicle_filter is not None:
			vehicle_filter.notifyInputKeysDown(0, 0)
	except Exception:
		pass
	try:
		physics = state.get('physics')
		if physics is not None:
			physics.staticMode = True
	except Exception:
		pass
	callbacks_released = _release_callbacks(state)
	state['reason'] = str(reason)
	if not callbacks_released:
		# A live native callback still owns this Python state. Keep the complete
		# filter/physics/provider graph so stop_mock() can retry the release; never
		# detach the entity or model underneath an uncleared callback.
		if not preserve_pose:
			pose = _entity_pose(state) if was_active else None
			if pose is not None and not _is_plausible_pose(pose, state.get('last_pose')):
				pose = state.get('last_pose') if _is_plausible_pose(state.get('last_pose')) else None
			if not was_active or pose is None:
				_sp = _point_tuple(state.get('seed_position'))
				_gs = _point_tuple(state.get('ground_support'))
				if _sp is not None:
					_sy = float(_gs[1]) if _gs is not None else float(_sp[1])
					pose = (float(_sp[0]), _sy, float(_sp[2]), float(state.get('seed_yaw', 0.0) or 0.0), 0.0, 0.0)
			if pose is not None and _is_plausible_pose(pose):
				try:
					mock.position = Math.Vector3(pose[0], pose[1], pose[2])
					mock.yaw, mock.pitch, mock.roll = pose[3], pose[4], pose[5]
				except Exception:
					pass
				state['last_pose'] = pose
				state['last_pose_at'] = _now()
		state['phase'] = 'faulted'
		if was_live:
			if state.get('counted_active'):
				_COUNTERS['active'] = max(0, _COUNTERS['active'] - 1)
				state['counted_active'] = False
			_COUNTERS['runtime_failed'] += 1
		elif not was_active:
			_COUNTERS['startup_failed'] += 1
		state['frame_pose'] = None
		state['frame_pose_at'] = None
		state['frame_output_generation'] = -1
		_COUNTERS['failed'] += 1
		LOG_ERROR('NATIVE_BOT_PHYSICS FAIL id=%s phase=%s reason=%s' % (
			getattr(mock, 'id', '?'), state.get('phase'), str(reason)))
		_maybe_log_startup_complete()
		return False
	if was_active:
		# Never hot-swap a live native body back to Python. Freeze at the last
		# validated pose, keep one motion owner, and release it during the normal
		# battle sweep. A later battle builds a fresh native owner.
		try:
			state['physics'].staticMode = True
		except Exception:
			pass
		# Filter::input acknowledges a network sample, not a synchronous rigid-body
		# teleport. Freeze the current native root instead of publishing an
		# unverified reseed as the canonical pose.
		if not preserve_pose:
			pose = _entity_pose(state)
			if not _is_plausible_pose(pose, state.get('last_pose')):
				pose = state.get('last_pose')
			if pose is not None and _is_plausible_pose(pose):
				try:
					mock.position = Math.Vector3(pose[0], pose[1], pose[2])
					mock.yaw, mock.pitch, mock.roll = pose[3], pose[4], pose[5]
				except Exception:
					pass
				state['last_pose'] = pose
				state['last_pose_at'] = _now()
		state['phase'] = 'faulted'
		if was_live:
			if state.get('counted_active'):
				_COUNTERS['active'] = max(0, _COUNTERS['active'] - 1)
				state['counted_active'] = False
			_COUNTERS['runtime_failed'] += 1
	else:
		# A fashion which could not be rolled back or detached still owns native
		# filter providers from the chassis. Keep the entire frozen native graph
		# intact so stop_mock() can retry that detach transaction; restoring only the
		# Python Servo here would create a mixed presentation owner.
		fashion_release_blocked = bool(state.get('fashion_detach_blocked'))
		provider_restored = not fashion_release_blocked
		if (not fashion_release_blocked and
				state.get('native_servo') is not None):
			provider_restored = _restore_python_model_provider(mock, state)
		if not provider_restored:
			LOG_ERROR('NATIVE_BOT_PHYSICS Python Servo restore failed id=%s' % (
				getattr(mock, 'id', '?')))
			# The native Servo is still the only attached root owner. Returning None
			# would let the caller start Python motion beside it, so retain the native
			# references and freeze the last verified pose until normal cleanup retries
			# the detach.
			_sp2 = _point_tuple(state.get('seed_position'))
			_gs2 = _point_tuple(state.get('ground_support'))
			if _sp2 is not None:
				_sy2 = float(_gs2[1]) if _gs2 is not None else float(_sp2[1])
				state['last_pose'] = (float(_sp2[0]), _sy2, float(_sp2[2]), float(state.get('seed_yaw', 0.0) or 0.0), 0.0, 0.0)
				state['last_pose_at'] = _now()
				try:
					mock.position = Math.Vector3(float(_sp2[0]), _sy2, float(_sp2[2]))
					mock.yaw, mock.pitch, mock.roll = float(state.get('seed_yaw', 0.0) or 0.0), 0.0, 0.0
				except Exception:
					pass
			if not fashion_release_blocked:
				state['pending_fashion'] = None
			state['phase'] = 'faulted'
		elif not _restore_avatar_filter(mock):
			LOG_ERROR('NATIVE_BOT_PHYSICS entity owner release failed id=%s' % (
				getattr(mock, 'id', '?')))
			# A native setter may report success without changing the bound object.
			# Retain every state reference so stop_mock() can retry the release; never
			# label this body failed while the entity still owns native physics.
			state['phase'] = 'faulted'
		else:
			_sp3 = _point_tuple(state.get('seed_position'))
			_gs3 = _point_tuple(state.get('ground_support'))
			if _sp3 is not None:
				_sy3 = float(_gs3[1]) if _gs3 is not None else float(_sp3[1])
				state['last_pose'] = (float(_sp3[0]), _sy3, float(_sp3[2]), float(state.get('seed_yaw', 0.0) or 0.0), 0.0, 0.0)
				state['last_pose_at'] = _now()
				try:
					mock.position = Math.Vector3(float(_sp3[0]), _sy3, float(_sp3[2]))
					mock.yaw, mock.pitch, mock.roll = float(state.get('seed_yaw', 0.0) or 0.0), 0.0, 0.0
				except Exception:
					pass
			state['physics'] = None
			state['filter'] = None
			state['entity_provider'] = None
			state['pending_fashion'] = None
			state['phase'] = 'failed'
		_COUNTERS['startup_failed'] += 1
	state['frame_pose'] = None
	state['frame_pose_at'] = None
	state['frame_output_generation'] = -1
	_COUNTERS['failed'] += 1
	LOG_ERROR('NATIVE_BOT_PHYSICS FAIL id=%s phase=%s reason=%s' % (
		getattr(mock, 'id', '?'), state.get('phase'), str(reason)))
	_maybe_log_startup_complete()
	return False


def mark_wreck(mock, detach_filter=True):
	"""Mark an active native body as a PUSHABLE wreck.

	``detach_filter=False`` keeps the entity's WGVehicleFilter2/wgPhysics attached
	and only zeroes the inputs and sets state['wreck'].  Reason (measured 01.10):
	with the detach the client died at 0xC0000005 @ 0x0090A3EA (Read @ 0x10)
	inside the wreck model swap, right after WRECK_STEP 1_swap_start - the same
	address that made an earlier session disable pushable wrecks outright.  A
	run WITHOUT the detach (live body, zeroed inputs, no wreck flag) survived
	three deaths cleanly, so the detach is the only new variable between the two.
	The trade-off is the hazard this function's own docstring warns about
	(0x00E78498: a live filter whose model got nulled), which is why the flag
	exists instead of deleting the detach.

	Zeroes drive inputs and keeps the rigid body live in the shared batch
	so live tanks can still shove it. The Python bot loop never steps dead
	bots, so the throttle can never be re-armed.

	The wreck's OfflineEntity MUST NOT keep a live WGVehicleFilter2 while
	its model is None: the engine's filter/entity update path dereferences
	the model when a live filter is attached, producing the ACCESS_VIOLATION
	at 0x00E78498. Detach the entity from the filter here, before the
	wreck-model swap nulls entity.model. The WGVehiclePhysics2 stays live in
	the shared batch.
	"""
	state = getattr(mock, STATE_ATTR, None)
	if not isinstance(state, dict) or state.get('phase') != 'active':
		return False
	state['last_input'] = (0, 0)
	try:
		_set_drive_input(state, 0, 0)
	except Exception:
		pass
	state['wreck'] = True
	state['wreck_settled'] = 0
	state['wreck_since'] = _now()
	state['frame_turn_speed'] = 0.0
	state['frame_speed'] = 0.0
	entity = getattr(mock, 'bw_entity', None)
	if entity is not None and detach_filter:
		try:
			entity.wgPhysics = None
			if getattr(entity, 'wgPhysics', None) is not None:
				raise RuntimeError('wgPhysics readback mismatch')
		except Exception as error:
			LOG_ERROR('NATIVE_BOT_PHYSICS wreck detach wgPhysics failed id=%s: %s' % (
				str(getattr(mock, 'id', '?')), str(error)))
			return False
		try:
			relay_filter = BigWorld.AvatarFilter()
			entity.filter = relay_filter
			if getattr(entity, 'filter', None) is not relay_filter:
				raise RuntimeError('AvatarFilter readback mismatch')
		except Exception as error:
			LOG_ERROR('NATIVE_BOT_PHYSICS wreck detach filter failed id=%s: %s' % (
				str(getattr(mock, 'id', '?')), str(error)))
			return False
		try:
			entity.isStarted = False
		except Exception:
			pass
	state['wreck'] = True
	LOG_NOTE('NATIVE_BOT_PHYSICS wreck dynamic id=%s (pushable)' % str(getattr(mock, 'id', '?')))
	return True
def wreck_pose(mock):
	"""Latest batch pose for a pushable wreck, no drive/input logic. The bot
	loop skips dead vehicles, so nobody else moves the wreck VISUAL; callers
	feed this into the wreck Servo matrix each frame."""
	state = getattr(mock, STATE_ATTR, None)
	if not isinstance(state, dict) or not state.get('wreck'):
		return None
	pose = state.get('frame_pose') or state.get('last_pose')
	if pose is None or len(pose) < 6:
		return None
	return {
		'position': pose[:3],
		'yaw': pose[3],
		'pitch': pose[4],
		'roll': pose[5],
		'velocity': float(state.get('frame_speed', 0.0) or 0.0),
		'turn_velocity': float(state.get('frame_turn_speed', 0.0) or 0.0),
	}


def guard_fault(mock, reason):
	"""Freeze one active native owner without submitting a filter sample."""
	state = getattr(mock, STATE_ATTR, None)
	if not isinstance(state, dict) or state.get('phase') != 'active':
		return False
	pose = _entity_pose(state)
	if pose is not None:
		state['last_pose'] = pose
		state['last_pose_at'] = _now()
	_fail(mock, state, 'native guard fault: %s' % str(reason), True)
	return state.get('phase') == 'faulted'


def sleep_mock(mock, reason='wreck sleep', freeze=True):
	"""Idle one live body where it stands WITHOUT faulting it.

	``freeze=True``  - set WGVehiclePhysics2.isFrozen.  Cheapest, and the pose is
	  bit-exact, but a frozen body stopped blocking: on 01.10 the player drove
	  straight through a sleeping wreck while live bodies still collided.  So a
	  frozen wreck is only acceptable together with a separate static collider.
	``freeze=False`` - keep the body DYNAMIC and merely zero its drive inputs.
	  It still collides, it can be shoved by a heavier tank, it falls if the
	  ground under it disappears and it slides on a slope - which is exactly the
	  wreck behaviour that was asked for.  Costs one body slot in the shared
	  batch solve, so it is the honest price of a physical corpse.

	guard_fault() routes through _fail(), which flips the phase to 'faulted' and
	starts the wreck/detach transaction - the path that crashed natively on
	01.10 (0xC0000005 @ 0x00E78498, a NULL-owner smart-pointer assignment right
	after 'wreck freeze').  Sleeping is the cheaper and safer half of the same
	idea: WGVehiclePhysics2.isFrozen is writable (see _set_drive_input, which
	already unfreezes a body that fell asleep during the countdown), the body
	keeps its exact pose - no re-seed, so the model cannot jump when ownership
	changes - and it stays in the shared batch as an obstacle.

	Returns True only when the readback confirms the body is frozen.
	"""
	state = getattr(mock, STATE_ATTR, None)
	if not isinstance(state, dict) or state.get('phase') != 'active':
		return False
	physics = state.get('physics')
	if physics is None:
		return False
	pose_before = _entity_pose(state)
	try:
		physics.movementSignals = 0
		if freeze:
			physics.isFrozen = True
	except Exception as error:
		LOG_ERROR('NATIVE_BOT_PHYSICS sleep failed id=%s error=%s' % (
			getattr(mock, 'id', '?'), str(error)))
		return False
	if freeze and not bool(getattr(physics, 'isFrozen', False)):
		LOG_ERROR('NATIVE_BOT_PHYSICS sleep readback mismatch id=%s' % (
			getattr(mock, 'id', '?'),))
		return False
	pose_after = _entity_pose(state)
	if pose_before is not None:
		state['last_pose'] = pose_before
		state['last_pose_at'] = _now()
	state['offh_sleep_reason'] = str(reason)
	state['offh_sleep_pose'] = pose_before
	state['offh_sleep_frozen'] = bool(freeze)
	try:
		# Hand the real body pose to the wreck swap through the mock.  The swap
		# reads the OLD chassis model's .pitch/.roll, which are 0.0 on a natively
		# driven chassis (the orientation lives in the Servo matrix the filter
		# feeds), so every wreck was rebuilt dead level on slopes.  Pose layout is
		# (x, y, z, yaw, pitch, roll).
		mock._offh_native_sleep_pose = (
			tuple(pose_before) if pose_before is not None else None)
	except Exception:
		pass
	try:
		# The 1:1 guarantee the wreck work is held to: sleeping must not move the
		# body.  Log the delta so a regression is visible in python.log instead of
		# showing up as a tank teleporting when it dies.  _pose_delta takes
		# (expected_position, expected_yaw, actual) and returns
		# (dx, dy, dz, distance, yaw_delta) - it does NOT cover pitch/roll, so
		# those are compared separately here: a wreck that snaps dead level on a
		# slope is exactly the tilt complaint, and dist/yaw alone would hide it.
		_delta_text = 'n/a'
		if pose_before is not None and len(pose_before) >= 4:
			_delta = _pose_delta(pose_before[:3], pose_before[3], pose_after)
			if _delta is not None:
				_delta_text = 'dist=%.4f yaw=%.4f' % (
					float(_delta[3]), float(_delta[4]))
		if (pose_before is not None and pose_after is not None and
				len(pose_before) >= 6 and len(pose_after) >= 6):
			_delta_text += ' pitch=%.4f roll=%.4f' % (
				abs(float(pose_after[4]) - float(pose_before[4])),
				abs(float(pose_after[5]) - float(pose_before[5])))
		LOG_NOTE('NATIVE_BOT_PHYSICS sleep id=%s reason=%s frozen=%s pose_delta %s' % (
			getattr(mock, 'id', '?'), str(reason), 'yes' if freeze else 'no',
			_delta_text))
	except Exception:
		pass
	return True


def wake_mock(mock):
	"""Unfreeze a slept body so gravity, suspension and collisions act again.

	The body stays dynamic afterwards: it can be pushed, it falls if the ground
	under it disappears, and it slides on a slope.  Callers re-sleep it once it
	is at rest again.  Returns True only when the readback confirms the wake,
	because _set_drive_input treats a failed wake as a hard error.
	"""
	state = getattr(mock, STATE_ATTR, None)
	if not isinstance(state, dict):
		return False
	physics = state.get('physics')
	if physics is None:
		return False
	if not bool(getattr(physics, 'isFrozen', False)):
		state.pop('offh_sleep_reason', None)
		return True
	try:
		physics.isFrozen = False
	except Exception as error:
		LOG_ERROR('NATIVE_BOT_PHYSICS wake failed id=%s error=%s' % (
			getattr(mock, 'id', '?'), str(error)))
		return False
	if bool(getattr(physics, 'isFrozen', True)):
		LOG_ERROR('NATIVE_BOT_PHYSICS wake readback mismatch id=%s' % (
			getattr(mock, 'id', '?'),))
		return False
	state.pop('offh_sleep_reason', None)
	state.pop('offh_sleep_pose', None)
	try:
		LOG_NOTE('NATIVE_BOT_PHYSICS wake id=%s' % (getattr(mock, 'id', '?'),))
	except Exception:
		pass
	return True


def is_sleeping(mock):
	"""True when this mock's native body is asleep (frozen on purpose)."""
	state = getattr(mock, STATE_ATTR, None)
	if not isinstance(state, dict):
		return False
	if state.get('offh_sleep_reason') is None:
		return False
	physics = state.get('physics')
	if physics is None:
		return False
	return bool(getattr(physics, 'isFrozen', False))


def _frozen_result(state):
	pose = state.get('last_pose')
	if pose is None or len(pose) < 6:
		return None
	return {
		'position': pose[:3],
		'yaw': pose[3],
		'pitch': pose[4],
		'roll': pose[5],
		'velocity': 0.0,
		'turn_velocity': 0.0,
		'faulted': True,
	}


def _failed_result(mock, state=None):
	"""Return a visible, immobile pose for a failed native-only bot."""
	pose = state.get('last_pose') if isinstance(state, dict) else None
	if pose is None or len(pose) < 6:
		position = _point_tuple(getattr(mock, 'position', None))
		if position is None:
			return None
		pose = position + (
			float(getattr(mock, 'yaw', 0.0) or 0.0),
			float(getattr(mock, 'pitch', 0.0) or 0.0),
			float(getattr(mock, 'roll', 0.0) or 0.0))
	return {
		'position': pose[:3],
		'yaw': pose[3],
		'pitch': pose[4],
		'roll': pose[5],
		'velocity': 0.0,
		'turn_velocity': 0.0,
		'failed': True,
	}


def _pending_result(mock):
	"""Hold one required bot while its asynchronous entity is not bound yet."""
	result = _failed_result(mock, None)
	if result is None:
		return None
	try:
		del result['failed']
	except Exception:
		pass
	result['staging'] = True
	return result


def _staged_result(state):
	"""Keep the Python proxy at the seeded pose until native ownership starts."""
	pose = state.get('last_pose')
	if pose is None or len(pose) < 6:
		return None
	return {
		'position': pose[:3],
		'yaw': pose[3],
		'pitch': pose[4],
		'roll': pose[5],
		'velocity': 0.0,
		'turn_velocity': 0.0,
		'staging': True,
	}


def is_prepared(mock):
	return isinstance(getattr(mock, STATE_ATTR, None), dict)


def is_active(mock):
	state = getattr(mock, STATE_ATTR, None)
	return isinstance(state, dict) and state.get('phase') == 'active'


def owns_filter(mock):
	state = getattr(mock, STATE_ATTR, None)
	return (isinstance(state, dict) and
		state.get('phase') in (
			'seed_wait', 'warmup', 'active', 'faulted') and
		state.get('filter') is not None)


def requires_native(player, mock):
	"""Return whether this authority bot must never use Python kinematics."""
	if bool(getattr(mock, REQUIRED_ATTR, False)):
		return True
	required = enabled_for(player) and _eligible_mock(mock)
	if required:
		setattr(mock, REQUIRED_ATTR, True)
	return required


def claims_movement(mock):
	"""Reserve one bot's movement slot even after fail-closed startup."""
	if bool(getattr(mock, REQUIRED_ATTR, False)):
		return True
	state = getattr(mock, STATE_ATTR, None)
	return (isinstance(state, dict) and state.get('phase') in (
		'preparing', 'seed_wait', 'warmup', 'active', 'faulted', 'failed'))


def fail_closed_result(mock):
	"""Expose a native startup failure without entering Python movement."""
	return _failed_result(mock, getattr(mock, STATE_ATTR, None))


def _create_dynamics_simulator():
	"""Create the one retail batch solver owned by this battle."""
	import physics_shared
	simulator = BigWorld.WGDynamicsSimulator()
	settings = (
		('numSubsteps', 1),
		('numIterations', 3),
		('frictionRatio', float(getattr(
			physics_shared, 'FRICTION_RATIO', 1.0))),
		('restitution', float(getattr(physics_shared, 'RESTITUTION', 0.5))),
		('allowedPenetration', float(getattr(
			physics_shared, 'ALLOWED_PENETRATION', 0.01))),
		('midSolvingIterations', 1),
	)
	for name, value in settings:
		setattr(simulator, name, value)
		actual = getattr(simulator, name)
		if abs(float(actual) - float(value)) > 0.000001:
			raise RuntimeError(
				'native dynamics setting readback mismatch %s=%s expected=%s' % (
					name, str(actual), str(value)))
	return simulator


def last_simulation_at():
	"""Timestamp of the newest completed shared solve, or None.

	The player loop runs BEFORE the shared sweep inside a frame, so its
	step() must sample the previous solve: _frame_pose() only returns a
	pose cached for the exact solve timestamp.
	"""
	return _LAST_SIMULATION_AT[0]


def simulate_frame(mocks, dt, timestamp=None):
	"""Advance one native batch, publish each filter, then capture its root.

	The 0.8.2 engine does not schedule client-created WGVehiclePhysics2 bodies.
	WGDynamicsSimulator.update owns frame reset, terrain/track/carcass contacts,
	bot-to-bot pairs, force solving and integration. Calling it per vehicle would
	reset the batch repeatedly and lose native pair contacts. Retail Vehicle
	entities schedule WGVehicleFilter2::output automatically; OfflineEntity does
	not. Retail Filter::output also does not copy the attached rigid root. The
	pinned bridge submits that root to the filter history, while Python consumes
	the same solve's WGVehiclePhysics2.matrix and native speeds as canonical state.
	"""
	if _SIMULATION_FAILED[0]:
		return 0
	when = _now() if timestamp is None else float(timestamp)
	if not _finite(when):
		return 0
	previous = _LAST_SIMULATION_AT[0]
	if previous is not None and when <= float(previous):
		when = float(previous) + max(0.001, min(float(dt or 0.016), 0.05))
	entries = []
	for mock_id in sorted((mocks or {}).keys()):
		mock = (mocks or {}).get(mock_id)
		state = getattr(mock, STATE_ATTR, None)
		if (not isinstance(state, dict) or state.get('physics') is None or
				state.get('phase') not in ('airborne', 'airborne_settle', 'warmup', 'active', 'faulted')):
			continue
		entries.append((mock, state))
	if not entries:
		return 0
	# v3.5: Run WGDynamicsSimulator.simulate(dt) + WGVehicleFilter2::output(when) EVERY rendered frame
	# at exact frame dt so C++ suspension/wheels/hull springs never skip frames or rock at 20-30 Hz!
	for _m_pr, _st_pr in entries:
		_cur_pr = _st_pr.get('frame_pose') or _st_pr.get('last_pose')
		if _cur_pr is not None:
			_st_pr['prev_pose'] = _cur_pr
	_live_xz = []
	for _m_lv, _st_lv in entries:
		if not _st_lv.get('wreck') and _st_lv.get('phase') == 'active':
			_lp_lv = _st_lv.get('frame_pose') or _st_lv.get('last_pose')
			if _lp_lv is not None and len(_lp_lv) >= 3:
				_live_xz.append((float(_lp_lv[0]), float(_lp_lv[2])))
	solve_entries = []
	for mock, state in entries:
		_out_gen = int(state.get('simulated_frames', 0) or 0)
		if state.get('phase') == 'faulted':
			pose = state.get('last_pose')
			if pose is None or len(pose) < 6:
				continue
			state['simulated_frames'] = _out_gen + 1
			state['frame_pose'] = pose
			state['frame_pose_at'] = when
			state['frame_output_generation'] = _out_gen + 1
			state['frame_speed'] = 0.0
			state['frame_turn_speed'] = 0.0
			continue
		if state.get('wreck') and int(state.get('wreck_settled', 0) or 0) >= 12:
			_w_pose = state.get('frame_pose') or state.get('last_pose')
			if _w_pose is not None and len(_w_pose) >= 6:
				_wx, _wz = float(_w_pose[0]), float(_w_pose[2])
				_near_live = False
				for _lx, _lz in _live_xz:
					if (_lx - _wx) * (_lx - _wx) + (_lz - _wz) * (_lz - _wz) < 52.0:
						_near_live = True
						break
				if not _near_live:
					if not state.get('_wreck_frozen_set', False):
						try:
							state['physics'].isFrozen = True
							state['_wreck_frozen_set'] = True
						except Exception:
							pass
					state['simulated_frames'] = _out_gen + 1
					state['frame_pose'] = _w_pose
					state['frame_pose_at'] = when
					state['frame_output_generation'] = _out_gen + 1
					state['frame_speed'] = 0.0
					state['frame_turn_speed'] = 0.0
					continue
				else:
					if state.get('_wreck_frozen_set', False):
						try:
							state['physics'].isFrozen = False
							state['_wreck_frozen_set'] = False
						except Exception:
							pass
					state['wreck_settled'] = 0
		solve_entries.append((mock, state, _out_gen))
	if not solve_entries:
		return 0
	generation_states = []
	try:
		solver_dt = float(dt)
		if not _finite(solver_dt) or solver_dt <= 0.0:
			return 0
		if solver_dt > DYNAMICS_MAX_DT:
			if not _SIMULATION_DT_CLAMP_LOGGED[0]:
				_SIMULATION_DT_CLAMP_LOGGED[0] = True
				LOG_ERROR('NATIVE_BOT_PHYSICS dynamics dt clamped '
					'actual_ms=%d limit_ms=%d' % (
						int(solver_dt * 1000.0 + 0.5),
						int(DYNAMICS_MAX_DT * 1000.0 + 0.5)))
			solver_dt = DYNAMICS_MAX_DT
		if _DYNAMICS_SIMULATOR[0] is None:
			_DYNAMICS_SIMULATOR[0] = _create_dynamics_simulator()
		try:
			from gui.mods.offhangar._constants import CONFIG_OPTIONS as _PC_CFG
			_dance_mode = bool(_PC_CFG.get('funny_tank_dance_physics', False))
			# v7.7: 2 solver iterations + 1 mid-iteration is mandatory for stable LCP suspension damping!
			# (1/0 is literally 'funny_tank_dance_physics' mode and causes tanks to bounce 20-55m into the air!)
			_want_iters = 1 if _dance_mode else 2
			_want_mid = 0 if _dance_mode else 1
			if getattr(_DYNAMICS_SIMULATOR[0], 'numIterations', 3) != _want_iters:
				_DYNAMICS_SIMULATOR[0].numIterations = _want_iters
			if getattr(_DYNAMICS_SIMULATOR[0], 'midSolvingIterations', 1) != _want_mid:
				_DYNAMICS_SIMULATOR[0].midSolvingIterations = _want_mid
		except Exception:
			pass
		physics = tuple(
			state['physics']
			for mock, state, output_generation in solve_entries)
		for mock, state, output_generation in solve_entries:
			if state.get('phase') in ('warmup', 'active'):
				_begin_destructible_generation(state)
				generation_states.append(state)
		_DYNAMICS_SIMULATOR[0].update(solver_dt, physics, ())
	except Exception as error:
		for state in generation_states:
			_close_destructible_generation(state, True)
		_SIMULATION_FAILED[0] = True
		_DYNAMICS_SIMULATOR[0] = None
		for mock, state, output_generation in solve_entries:
			if state.get('phase') in ('warmup', 'active'):
				_fail(mock, state, RuntimeError(
					'native batch simulation failed: %s' % str(error)), True)
		return 0
	for state in generation_states:
		_close_destructible_generation(state, False)
	_LAST_SIMULATION_AT[0] = when
	from gui.mods.offhangar import native_filter_bridge
	_mt_coord = globals().get('_CACHED_PC_MT_COORD')
	if _mt_coord is None:
		try:
			from gui.mods.offhangar.native import offh_multithread as _omt_pc
			_mt_coord = _omt_pc.get_coordinator()
		except Exception:
			try:
				from gui.mods.offhangar import offh_multithread as _omt_pc
				_mt_coord = _omt_pc.get_coordinator()
			except Exception:
				_mt_coord = None
		if _mt_coord is not None:
			globals()['_CACHED_PC_MT_COORD'] = _mt_coord
	valid_count = 0
	for mock, state, output_generation in solve_entries:
		if state.get('phase') not in ('airborne', 'airborne_settle', 'warmup', 'active', 'faulted'):
			continue
		if state.get('phase') == 'faulted':
			state['simulated_frames'] = output_generation + 1
			state['frame_pose'] = state.get('last_pose')
			state['frame_pose_at'] = when
			state['frame_output_generation'] = output_generation + 1
			state['frame_speed'] = 0.0
			state['frame_turn_speed'] = 0.0
			valid_count += 1
			continue
		try:
			_is_plr_b = bool(getattr(mock, 'isPlayer', False))
			_sp_id = int(state.get('space_id', 0) or 0)
			_can_skip_frz = (not _is_plr_b) and state.get('phase') == 'active' and state.get('frame_pose') is not None
			pose = None
			_fspd_now = None
			_frspd_now = None
			if _mt_coord is not None:
				# v7.9: Use C-extracted _c_is_frozen (from phys_this + 0x692) instead of 60 slow C++ getattr(physics, 'isFrozen')
				# std::map lookups per frame, and pass -(_sp_id + 100000) when _skip_flt is True so offh_mt.c skips Filter::input/output
				# on non-rendered/distant frames while still reading exact C++ pose, speed, rspeed, and isFrozen!
				_is_unsp_en = (not _is_plr_b and not bool(mock.__dict__.get('_spot_visible', True)))
				_is_far_b = (not _is_plr_b and bool(mock.__dict__.get('_bot_lod_far', False)))
				_not_frz_b = not bool(state.get('_c_is_frozen', False))
				# Skip WGVehicleFilter2::input/output on frozen bodies and on odd frames for invisible (unspotted) enemies!
				# Visible bots (spotted enemies and all allies) still run Filter::input/output every frame for 60Hz smooth tracks.
				_skip_flt = (_can_skip_frz and _sp_id > 0 and (bool(state.get('_filter_synced_frozen', False)) or (_is_unsp_en and ((output_generation & 1) == 1))))
				if _skip_flt:
					_sp_arg = -(_sp_id + 100000)
				elif _can_skip_frz and state.get('_filter_synced_frozen', False):
					_sp_arg = -_sp_id
				else:
					_sp_arg = _sp_id
				_c_clamped_done = True
				_pub_ex = _mt_coord.fast_publish_root_ex(
					state['filter'], state['physics'], when, _sp_arg, 0.0, None)
				if _pub_ex == 'frozen':
					state['_c_is_frozen'] = True
					state['_filter_synced_frozen'] = True
					state['simulated_frames'] = output_generation + 1
					state['frame_pose_at'] = when
					state['frame_output_generation'] = output_generation + 1
					state['frame_speed'] = 0.0
					state['frame_turn_speed'] = 0.0
					if state.get('wreck'):
						state['wreck_settled'] = int(state.get('wreck_settled', 0) or 0) + 1
					valid_count += 1
					continue
				elif _pub_ex is not None:
					if len(_pub_ex) >= 5:
						pose, _fspd_now, _frspd_now, _c_frz, _c_clamped = _pub_ex
						_c_clamped_done = True
						if _c_clamped and _mt_coord is not None and _sp_id > 0:
							try:
								_mt_coord.fast_seed_and_output(state['filter'], when, _sp_id, pose)
							except Exception:
								pass
					elif len(_pub_ex) >= 4:
						pose, _fspd_now, _frspd_now, _c_frz = _pub_ex
					else:
						pose, _fspd_now, _frspd_now = _pub_ex
						_c_frz = False
					state['_c_is_frozen'] = bool(_c_frz)
					state['_filter_synced_frozen'] = bool(_c_frz) and (not _skip_flt)
			if pose is None:
				if _can_skip_frz and bool(getattr(state.get('physics'), 'isFrozen', False)):
					state['simulated_frames'] = output_generation + 1
					state['frame_pose_at'] = when
					state['frame_output_generation'] = output_generation + 1
					state['frame_speed'] = 0.0
					state['frame_turn_speed'] = 0.0
					if state.get('wreck'):
						state['wreck_settled'] = int(state.get('wreck_settled', 0) or 0) + 1
					valid_count += 1
					continue
				if not native_filter_bridge.publish_physics_root(
						state['filter'], state['physics'], when, _sp_id):
					raise RuntimeError(
						'native physics root publish was rejected')
				if state.get('entity_provider') is None:
					raise RuntimeError(
						'native Filter::output returned an invalid pose')
				pose = _physics_pose(state['physics'])
				if pose is None:
					raise RuntimeError(
						'native physics root matrix is unavailable')
			if state.get('phase') == 'warmup':
				warmup_reason = _warmup_pose_reason(mock, state, pose)
				if warmup_reason is not None:
					raise RuntimeError(warmup_reason)
			if state.get('wreck') and float(pose[1]) < -60.0:
				# A pushed corpse must never leave the collision world either:
				# that void fall is the 0x0095AF97 AV crash. Freeze it instead.
				raise RuntimeError('wreck left the collision world y=%.1f' % float(pose[1]))
			if not _is_plausible_pose(pose, state.get('last_pose') or state.get('prev_pose')):
				if state.get('wreck'):
					# Dead pushable wreck reported a stale/teleported native pose after filter detach:
					# freeze the wreck at its last verified death pose instead of faulting after 6 frames!
					_w_ref = state.get('last_pose') or state.get('prev_pose')
					if _is_plausible_pose(_w_ref):
						pose = tuple(float(v) for v in _w_ref[:6])
						_fspd_now = 0.0
						_frspd_now = 0.0
						state['wreck_settled'] = 15
						try:
							_ph_w = state.get('physics')
							if _ph_w is not None:
								_ph_w.movementSignals = 0
								_ph_w.isFrozen = True
								_ph_w.staticMode = True
						except Exception:
							pass
					else:
						raise RuntimeError('implausible wreck pose (%.1f, %.1f, %.1f)' % (float(pose[0]), float(pose[1]), float(pose[2])))
				else:
					_rec_p = _recover_implausible_pose(mock, state, pose, when)
					if _rec_p is not None and int(state.get('_implausible_recoveries', 0) or 0) <= 6:
						pose = _rec_p
						_fspd_now = 0.0
						_frspd_now = 0.0
					else:
						raise RuntimeError('implausible native pose (%.1f, %.1f, %.1f)' % (float(pose[0]), float(pose[1]), float(pose[2])))
			if _fspd_now is None:
				_fspd_now = _physics_speed(state['physics'], 'speed')
				_frspd_now = _physics_speed(state['physics'], 'rspeed')
			_prev_p_kin = state.get('prev_pose')
			if _prev_p_kin is not None and len(_prev_p_kin) >= 4 and solver_dt > 0.0005:
				_k_dx = float(pose[0]) - float(_prev_p_kin[0])
				_k_dz = float(pose[2]) - float(_prev_p_kin[2])
				_k_yaw = float(pose[3])
				_k_fspd = (_k_dx * math.sin(_k_yaw) + _k_dz * math.cos(_k_yaw)) / solver_dt
				_k_dyaw = _normalise_angle(_k_yaw - float(_prev_p_kin[3]))
				_k_rspd = _k_dyaw / solver_dt
				if abs(_k_fspd) <= 32.0 and (abs(_fspd_now or 0.0) < abs(_k_fspd) * 0.65 or (_fspd_now or 0.0) * _k_fspd < 0.0):
					_fspd_now = _k_fspd
				if abs(_k_rspd) <= 3.5 and (abs(_frspd_now or 0.0) < abs(_k_rspd) * 0.65 or (_frspd_now or 0.0) * _k_rspd < 0.0):
					_frspd_now = _k_rspd
			# v9.0: Pass 100% genuine native WGVehiclePhysics2 (yaw, pitch, roll) without artificial clamping
			# so hull attitude matches the physical terrain slope 1:1 with zero visual drift!
			state['simulated_frames'] = output_generation + 1
			state['frame_pose'] = pose
			state['frame_pose_at'] = when
			state['frame_output_generation'] = output_generation + 1
			state['frame_speed'] = _fspd_now
			state['frame_turn_speed'] = _frspd_now
			if state.get('wreck'):
				if abs(_fspd_now) < 0.08 and abs(_frspd_now) < 0.03:
					state['wreck_settled'] = int(state.get('wreck_settled', 0) or 0) + 1
				elif (when - float(state.get('wreck_since', when))) > 1.8 and abs(_fspd_now) < 0.35:
					state['wreck_settled'] = 15
					state['frame_turn_speed'] = 0.0
					try:
						_phys_wr = state.get('physics')
						if _phys_wr is not None:
							_phys_wr.isFrozen = True
					except Exception:
						pass
				else:
					state['wreck_settled'] = 0
			# Bot fall meter: while the body is airborne track the PEAK y; on the
			# first solved frame after the phase leaves 'airborne' the drop becomes a
			# ballistic impact speed sqrt(2*g*h). offline_battle consumes
			# mock._offh_native_fall_impact and applies HP/module damage (bots never
			# had fall damage - player meter v2 lives in offline_battle).
			try:
				_phase_now = state.get('phase')
				if _phase_now == 'airborne':
					_pk = state.get('fall_peak_y')
					if _pk is None or float(pose[1]) > _pk:
						state['fall_peak_y'] = float(pose[1])
				elif state.get('fall_peak_y') is not None:
					_drop = float(state['fall_peak_y']) - float(pose[1])
					state['fall_peak_y'] = None
					if _drop > 1.0 and not state.get('wreck'):
						_iv = (2.0 * 9.81 * _drop) ** 0.5
						_up = math.cos(float(pose[4])) * math.cos(float(pose[5]))
						_att = 1.0 if _up > 0.8 else (1.35 if _up > 0.15 else 1.6)
						mock._offh_native_fall_impact = {
							'impact': _iv, 'drop': _drop, 'attitude': _att}
			except Exception:
				pass
			valid_count += 1
		except Exception as error:
			# If output succeeded before a later getter failed, the entity provider
			# already contains the new real pose. Let _fail retain that exact pose;
			# if output itself failed, the provider still contains the prior one.
			_fail(mock, state, error)
	# Native callbacks only collect plain data while the shared solver is live.
	# Apply after every output/capture, preserving the batch's stable body order.
	for mock, state, output_generation in solve_entries:
		had_valid_output = state.get('frame_pose_at') == when
		if not _drain_destructible_generation(mock, state):
			if had_valid_output:
				valid_count = max(0, valid_count - 1)
	if valid_count and not _SIMULATION_LOGGED[0]:
		_SIMULATION_LOGGED[0] = True
		LOG_NOTE('NATIVE_BOT_PHYSICS dynamics active bodies=%d '
			'substeps=%s iterations=%s' % (
				valid_count,
				str(getattr(_DYNAMICS_SIMULATOR[0], 'numSubsteps', '?')),
				str(getattr(_DYNAMICS_SIMULATOR[0], 'numIterations', '?'))))
	return valid_count


def _offh_arena_wall_box(player):
	"""native map wall = ArenaType.boundingBox + pad (flag native_arena_walls)."""
	far = ((-10000, -10000), (10000, 10000))
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS as _AW
		if not _AW.get('native_arena_walls', True):
			return far
		pad = max(0.0, float(_AW.get('native_arena_walls_pad', 4.0)))
		bb = None
		at = getattr(getattr(player, 'arena', None), 'arenaType', None)
		if at is not None:
			bb = getattr(at, 'boundingBox', None)
		if bb is None:
			import ArenaType
			bb = ArenaType.g_cache[int(getattr(player, 'arenaTypeID', 0))].boundingBox
		x0, z0 = float(bb[0][0]) - pad, float(bb[0][1]) - pad
		x1, z1 = float(bb[1][0]) + pad, float(bb[1][1]) + pad
		if not (x1 - x0 > 100.0 and z1 - z0 > 100.0):
			return far
		box = ((x0, z0), (x1, z1))
		key = (int(getattr(player, 'arenaTypeID', 0)), box)
		if globals().get('_OFFH_ARENA_WALL_LOGGED') != key:
			globals()['_OFFH_ARENA_WALL_LOGGED'] = key
			try:
				from gui.mods.offhangar.logging import LOG_NOTE
				LOG_NOTE('ARENA_WALLS box=%r pad=%.1f' % (box, pad))
			except Exception:
				pass
		return box
	except Exception:
		return far


def _native_client_suspension_enabled(player, mock):
	"""Player-only A/B: use the stock client suspension contract."""
	try:
		if player is None or mock is None:
			return False
		_is_player = False
		try:
			_is_player = (mock is getattr(player, '_offh_np_mock', None))
		except Exception:
			_is_player = False
		if not _is_player:
			try:
				_is_player = (int(getattr(mock, 'id', -1)) == int(getattr(player, 'playerVehicleID', -2)))
			except Exception:
				_is_player = False
		if not _is_player:
			return False
		from gui.mods.offhangar._constants import CONFIG_OPTIONS as _NCS
		return bool(_NCS.get('native_client_suspension', False))
	except Exception:
		return False


def _native_player_roller_off(player, mock):
	"""Player-only A/B: force ROLLER_MODE=False, keep server mass split."""
	try:
		if player is None or mock is None:
			return False
		_is_player = False
		try:
			_is_player = (mock is getattr(player, '_offh_np_mock', None))
		except Exception:
			_is_player = False
		if not _is_player:
			try:
				_is_player = (int(getattr(mock, 'id', -1)) == int(getattr(player, 'playerVehicleID', -2)))
			except Exception:
				_is_player = False
		if not _is_player:
			return False
		from gui.mods.offhangar._constants import CONFIG_OPTIONS as _NRO
		return bool(_NRO.get('native_player_roller_off', False))
	except Exception:
		return False


def _stock_player_engine_power_enabled(player, mock):
	"""Player-only A/B: apply the stock vehicle.py engine-power override."""
	try:
		if player is None or mock is None:
			return False
		_is_player = False
		try:
			_is_player = (mock is getattr(player, '_offh_np_mock', None))
		except Exception:
			_is_player = False
		if not _is_player:
			try:
				_is_player = (int(getattr(mock, 'id', -1)) == int(getattr(player, 'playerVehicleID', -2)))
			except Exception:
				_is_player = False
		if not _is_player:
			return False
		from gui.mods.offhangar._constants import CONFIG_OPTIONS as _SEP
		return bool(_SEP.get('stock_player_engine_power', False))
	except Exception:
		return False


def _log_stock_player_reference(player, mock, descriptor, state):
	"""Log-only stock reference comparison for the local player's native body."""
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS as _SR
		if not bool(_SR.get('stock_player_ref_diag', False)):
			return
		if player is None or mock is None:
			return
		try:
			if int(getattr(mock, 'id', -1)) != int(getattr(player, 'playerVehicleID', -2)):
				return
		except Exception:
			return
		physics = state.get('physics')
		vehicle_filter = state.get('filter')
		if physics is None:
			return
		phys = _descriptor_value(descriptor, 'physics', {}) or {}
		engine = float(_descriptor_value(phys, 'enginePower', 0.0) or 0.0)
		import physics_shared
		vehicle_py = engine / 1000.0
		shared_init = engine * float(getattr(physics_shared, 'GRAVITY_FACTOR_SCALED', 0.00125))
		LOG_NOTE('STOCK_PLAYER_REF id=%s engine current=%.3f vehicle_py=%.3f shared_init=%.3f' % (
			str(getattr(mock, 'id', '?')), float(getattr(physics, 'enginePower', 0.0) or 0.0),
			vehicle_py, shared_init))
		LOG_NOTE('STOCK_PLAYER_REF phys centerOfMass=%s trackCenter=%s rotLimit=%s speed=(%s,%s)' % (
			str(getattr(physics, 'centerOfMass', None)),
			str(getattr(physics, 'trackCenterOffset', None)),
			str(getattr(physics, 'rotationSpeedLimit', None)),
			str(getattr(physics, 'fwdSpeedLimit', None)),
			str(getattr(physics, 'bkwdSpeedLimit', None))))
		LOG_NOTE('STOCK_PLAYER_REF gates contactCohesion=%s slopeCohDecay=%s/%s compensate=%s/%s' % (
			str(getattr(physics, 'contactCohesion', None)),
			str(getattr(physics, 'slopeCohDecay', None)),
			str(getattr(physics, 'slopeCohDecayY', None)),
			str(getattr(physics, 'compensateFrictionDecayRatio', None)),
			str(getattr(physics, 'compensateFrictionDecayY', None))))
		LOG_NOTE('STOCK_PLAYER_REF providers filter=%s pending_fashion=%s' % (
			vehicle_filter is not None, state.get('pending_fashion') is not None))
	except Exception:
		pass


def _attach_physics(player, mock, descriptor, state):
	entity = getattr(mock, 'bw_entity', None)
	if entity is None:
		raise RuntimeError('OfflineEntity disappeared before physics attach')
	import OfflineEntity
	if not OfflineEntity.install_native_destructible_callback_adapter():
		raise RuntimeError('native destructible callback adapter was rejected')
	import physics_shared
	physics = BigWorld.WGVehiclePhysics2()
	_client_suspension = _native_client_suspension_enabled(player, mock)
	_roller_off = _native_player_roller_off(player, mock)
	old_is_client = physics_shared.IS_CLIENT
	old_roller_mode = physics_shared.ROLLER_MODE
	try:
		# WGDynamicsSimulator is the retail server-style vehicle solver. Its
		# suspension contract needs the non-client hull and roller setup even
		# though this client owns the simulation locally. Player-only A/B:
		# native_client_suspension switches the local player back to the stock
		# client 'ski' contract without changing bot bodies; native_player_roller_off
		# keeps the server split and only disables the roller contact model.
		if _client_suspension:
			physics_shared.IS_CLIENT = True
			physics_shared.ROLLER_MODE = False
		else:
			physics_shared.IS_CLIENT = False
			physics_shared.ROLLER_MODE = (False if _roller_off else True)
		physics_shared.initVehiclePhysics(physics, descriptor)
	finally:
		physics_shared.IS_CLIENT = old_is_client
		physics_shared.ROLLER_MODE = old_roller_mode
	if _stock_player_engine_power_enabled(player, mock):
		try:
			_stock_engine = float(_descriptor_value(_descriptor_value(descriptor, 'physics', {}) or {}, 'enginePower', 0.0) or 0.0) / 1000.0
			if _stock_engine > 0.0:
				physics.enginePower = _stock_engine
				LOG_NOTE('STOCK_PLAYER_ENGINE id=%s engine=%.3f' % (str(getattr(mock, 'id', '?')), _stock_engine))
		except Exception:
			pass
	if _client_suspension or _roller_off:
		LOG_NOTE('NATIVE_SUSPENSION mode=%s roller=%s id=%s' % (
			'client' if _client_suspension else 'server',
			'off' if _roller_off else 'on',
			str(getattr(mock, 'id', '?'))))
	# v4.66 доп.15: this native WGVehiclePhysics2 box is deliberately left as
	# an effectively-disabled 20km no-op - it is not the per-map boundary.
	# The real physical map-boundary wall now lives in Python (offline_battle.py's
	# player/bot per-tick ticks call vehicle_collision.boundary_wall_response()
	# against the actual ArenaType.boundingBox for this map), which is where the
	# rest of this mod's collision resolution already lives.
	physics.setArenaBounds(*_offh_arena_wall_box(player))
	base_power = float(physics.enginePower)
	physics.owner = weakref.ref(entity)
	physics.staticMode = False
	physics.movementSignals = 0
	if not _clear_callbacks(physics):
		raise RuntimeError('native callback initialization was rejected')
	physics.visibilityMask = _visibility_mask(player)
	# WGVehiclePhysics2 leaves these two contact gates uninitialized in the
	# supported 0.8.2 executable, so client-only bodies must define them before
	# simulation. Without the track gate there is no suspension or track force;
	# without the carcass gate hull/static-world contacts are skipped.
	physics.allowTracksContacts = True
	physics.allowCarcassContacts = True
	if (not bool(getattr(physics, 'allowTracksContacts', False)) or
			not bool(getattr(physics, 'allowCarcassContacts', False))):
		raise RuntimeError('native contact enable readback mismatch')
	state['filter'].setVehiclePhysics(physics)
	from gui.mods.offhangar import native_filter_bridge
	if not native_filter_bridge.filter_has_physics(
			state['filter'], physics):
		raise RuntimeError('WGVehicleFilter2 physics owner readback mismatch')
	# Do not call WGVehicleFilter2.syncGunAngles here.  Its native 0.8.2
	# implementation reads EntityManager's retail ServerConnection clock, which
	# is deliberately absent for client-only OfflineEntity instances. Bot turret
	# and gun angles remain owned by the existing Python combat/model path.
	state['filter'].notifyInputKeysDown(0, 0)
	entity.typeDescriptor = descriptor
	# Keep OfflineEntity outside the retail Vehicle UI lifecycle. Engine entity
	# ids can collide with the fake arena playerVehicleID; the stock arcade,
	# sniper and strategic cameras then treat any entity with isStarted=True as
	# the local Vehicle and dereference its full VehicleAppearance. Native
	# physics only needs the owner/filter/descriptor references established here.
	entity.isStarted = False
	entity.isPlayer = False
	entity.wgPhysics = physics
	mock.filter = state['filter']
	state['physics'] = physics
	state['base_engine_power'] = base_power
	state['last_input'] = (0, 0)
	_install_destructible_callbacks(state, physics)
	_log_stock_player_reference(player, mock, descriptor, state)


def _activate_model_provider(mock, state):
	"""Make the canonical physics-root matrix the sole model provider."""
	if bool(getattr(mock, 'isPlayer', False)):
		# Player tank visual hierarchy (chassis -> V -> hull -> HP_turretJoint -> turret -> HP_gunJoint -> gun)
		# is driven by BigWorld.Servo(chassis_mp) installed in _add_models_when_ready. Never strip it!
		return True
	chassis = (getattr(mock, '_chassis_model', None) or
		getattr(mock, 'model', None))
	entity = getattr(mock, 'bw_entity', None)
	if chassis is None or entity is None:
		return False
	# WGVehicleFilter2 keeps a presentation history on entity.matrix.  Gameplay,
	# collision and LAN publication use the solved WGVehiclePhysics2 root which
	# offline_battle commits to this persistent matrix every frame.  Binding the
	# chassis to the delayed entity provider made distant models visibly catch up
	# by a vehicle length when their Filter output resumed.
	provider = getattr(mock, 'matrix', None)
	if provider is None:
		return False
	if state.get('native_servo') is not None:
		return _sole_model_motor(chassis, state.get('native_servo'))
	old_servo = getattr(mock, '_pose_servo', None)
	servo = None
	old_detached = False
	try:
		# Create the replacement object first, but never attach two root motors.
		# On a RESEED the python ghost-drive may have attached a fresh Servo that
		# mock._pose_servo no longer points at (log 28: 'entity matrix could not
		# own the model' on every re-activation), so clear ALL current root
		# motors instead of demanding the remembered one. Attach the native
		# Servo, and restore the old owner only if that fails.
		servo = BigWorld.Servo(provider)
		motors = _model_motors(chassis)
		if motors is None:
			raise RuntimeError('model motor readback unavailable')
		if old_servo is not None and _model_has_motor(chassis, old_servo):
			chassis.delMotor(old_servo)
			old_detached = True
			motors = _model_motors(chassis) or []
		if motors:
			for _extra_motor in list(motors):
				try: chassis.delMotor(_extra_motor)
				except Exception: pass
			if _model_motors(chassis) != []:
				raise RuntimeError('model root motor sweep readback mismatch')
		chassis.addMotor(servo)
		if not _sole_model_motor(chassis, servo):
			raise RuntimeError('native Servo attach readback mismatch')
	except Exception:
		try:
			if _model_has_motor(chassis, servo):
				chassis.delMotor(servo)
		except Exception:
			pass
		if old_detached:
			try:
				chassis.addMotor(old_servo)
				if not _sole_model_motor(chassis, old_servo):
					raise RuntimeError('Python Servo restore readback mismatch')
			except Exception:
				mock._pose_servo = None
				mock._servo_added = False
		return False
	mock._native_pose_servo = servo
	mock._pose_servo = None
	mock._servo_added = True
	state['native_servo'] = servo
	return True


def _restore_python_model_provider(mock, state):
	"""Restore the proven Python matrix Servo after a staged setup failure."""
	chassis = (getattr(mock, '_chassis_model', None) or
		getattr(mock, 'model', None))
	if chassis is None:
		return False
	native_servo = state.get('native_servo')
	if native_servo is None:
		pose_servo = getattr(mock, '_pose_servo', None)
		return (pose_servo is not None and
			bool(getattr(mock, '_servo_added', False)) and
			_sole_model_motor(chassis, pose_servo))
	matrix = getattr(mock, 'matrix', None)
	if matrix is None:
		return False
	fallback_servo = None
	try:
		# Detach first so a model that refuses all delMotor calls can never retain
		# both the native and Python root owners. If this fails, the proven native
		# Servo remains the only attached motor and the caller freezes it.
		if not _sole_model_motor(chassis, native_servo):
			return False
		chassis.delMotor(native_servo)
		if _model_motors(chassis) != []:
			return False
	except Exception:
		return False
	try:
		fallback_servo = BigWorld.Servo(matrix)
		chassis.addMotor(fallback_servo)
		if not _sole_model_motor(chassis, fallback_servo):
			raise RuntimeError('Python Servo attach readback mismatch')
	except Exception:
		try:
			if _model_has_motor(chassis, fallback_servo):
				chassis.delMotor(fallback_servo)
		except Exception:
			pass
		# Restore the native owner if the Python replacement cannot be attached.
		# If even recovery fails, record that no Servo is attached; the caller still
		# fails closed and never starts Python motion beside an unknown owner.
		try:
			chassis.addMotor(native_servo)
			if not _sole_model_motor(chassis, native_servo):
				raise RuntimeError('native Servo restore readback mismatch')
		except Exception:
			state['native_servo'] = None
			mock._native_pose_servo = None
			mock._servo_added = False
		return False
	state['native_servo'] = None
	mock._native_pose_servo = None
	mock._pose_servo = fallback_servo
	mock._servo_added = True
	return True


def prepare(player, mock, descriptor, space_id, timestamp=None):
	"""Attach one staged native body to an already bound OfflineEntity."""
	if not requires_native(player, mock):
		return False
	# A relay proxy can be promoted after it already installed the legacy static
	# obstacle. Native contact must never collide with that duplicate own hull.
	try:
		mock._collision_obstacle = None
	except Exception:
		pass
	if descriptor is None or _SIMULATION_FAILED[0]:
		return False
	# OfflineEntity creation is asynchronous. The mock is registered before its
	# engine entity becomes visible to BigWorld.entity(), so a normal frame may
	# reach prepare() while bw_entity is still None. That is a pending condition,
	# not a terminal native failure; do not create or count a state until the
	# entity-ready callback (or a later frame) can retry the same native contract.
	entity = getattr(mock, 'bw_entity', None)
	if entity is None:
		return False
	if getattr(mock, '_offh_native_model_root_ready', False) is not True:
		# The entity/model assignment may still be retrying removal of BigWorld's
		# default motor. Attaching a native Servo before that transaction completes
		# would give the chassis two competing root providers.
		return False
	old_state = getattr(mock, STATE_ATTR, None)
	if isinstance(old_state, dict):
		if old_state.get('phase') == 'faulted':
			# A fault is fail-closed for this battle. Demotion/cleanup owns the only
			# valid release transaction; never discard its retry references here.
			return False
		if old_state.get('phase') == 'stopped':
			# A failed Servo detach deliberately keeps the only retry reference in the
			# stopped/faulted state. Do not discard it or attach a second root owner.
			if (old_state.get('native_servo') is not None or
					old_state.get('filter') is not None or
					old_state.get('physics') is not None):
				stop_mock(mock, False)
				if (old_state.get('native_servo') is not None or
						old_state.get('filter') is not None or
						old_state.get('physics') is not None):
					if not old_state.get('reuse_blocked_logged'):
						old_state['reuse_blocked_logged'] = True
						LOG_ERROR('NATIVE_BOT_PHYSICS reuse blocked id=%s '
							'reason=native owner is still attached' % (
								getattr(mock, 'id', '?')))
					return False
			# A delayed callback or promoted relay must build a new filter/physics
			# pair once the previous state owns no native objects.
			try:
				delattr(mock, STATE_ATTR)
			except Exception:
				setattr(mock, STATE_ATTR, None)
		else:
			return old_state.get('phase') != 'failed'
	_clear_hazard_recovery(mock)
	_COUNTERS['attempted'] += 1
	if not _COUNTERS['expected']:
		_COUNTERS['expected'] = _expected_body_count(player)
	try:
		max_speed = abs(float(_descriptor_value(
			_descriptor_value(descriptor, 'physics', {}),
			'speedLimits', (50.0, 0.0))[0]))
	except Exception:
		max_speed = 50.0
	state = {
		'phase': 'preparing',
		'filter': None,
		'physics': None,
		'entity_provider': None,
		'last_input': None,
		'last_pose': None,
		'last_pose_at': 0.0,
		'frame_pose': None,
		'frame_pose_at': None,
		'frame_output_generation': -1,
		'frame_speed': 0.0,
		'frame_turn_speed': 0.0,
		'simulated_frames': 0,
		'destructible_generation': 0,
		'destructible_generation_open': False,
		'destructible_events': [],
		'destructible_pending': [],
		'destructible_callback_error': None,
		'destructible_health_callback': None,
		'destructible_damage_callback': None,
		# A relay can already own a normal WGVehicleFashion when it is promoted.
		# Keep it staged until the native Servo/filter handoff becomes active, then
		# bind the same retail placement providers used by an initially local bot.
		'pending_fashion': getattr(mock, '_fashion', None),
		'fashion_detach_blocked': False,
		'native_servo': None,
		'counted_active': False,
		'max_speed': max(1.0, max_speed),
		'activate_at': 0.0,
		'drive_diagnostic_at': None,
		'drive_diagnostic_logged': False,
		'drive_diagnostic_root_start': None,
		'drive_diagnostic_entity_start': None,
		'presentation_sample': None,
		'presentation_log_at': -999.0,
		'presentation_log_count': 0,
		'seed_check_at': 0.0,
		'seed_position': None,
		'seed_yaw': 0.0,
		'ground_support': None,
		'ground_support_source': None,
		'ground_support_retry_at': 0.0,
		'ground_support_wait_started_at': None,
		'ground_support_log_at': 0.0,
		'ground_support_misses': 0,
		'space_id': int(space_id),
		'vehicle_name': str(_descriptor_value(descriptor, 'name', '?') or '?'),
	}
	setattr(mock, STATE_ATTR, state)
	try:
		position = _point_tuple(getattr(mock, 'position', None))
		if position is None:
			raise RuntimeError('mock pose is unavailable')
		yaw = float(getattr(mock, 'yaw', 0.0) or 0.0)
		# v4.66: used to always seed pitch/roll flat (0.0, 0.0) here, even
		# though the mock's real pitch/roll (used just below for last_pose)
		# were already sitting right there. A tank resting on a slope, or
		# just settled from a respawn re-anchor onto real (tilted) ground,
		# has a real tilt on both entity_provider (actual, read via
		# _matrix_pose from entity.matrix) and the native body (see the
		# FAIL logs' body= tuple) - but the seed always claimed flat. Since
		# entity.matrix's yaw is decomposed from the FULL tilted matrix
		# (Euler yaw/pitch/roll are coupled once pitch or roll is nonzero),
		# a flat-seeded yaw does not equal the tilted actual yaw even when
		# the "intended" heading is identical - producing a small but
		# systematic yaw_delta (~0.06 rad, just over SEED_YAW_TOLERANCE=
		# 0.05) on every respawn onto anything but dead-flat ground, on top
		# of whatever the v4.66 position/matrix fix already resolved.
		pitch = float(getattr(mock, 'pitch', 0.0) or 0.0)
		roll = float(getattr(mock, 'roll', 0.0) or 0.0)
		when = _now() if timestamp is None else float(timestamp)
		state['seed_position'] = position
		state['seed_position_raw'] = position
		state['seed_yaw'] = yaw
		state['seed_pitch'] = pitch
		state['seed_roll'] = roll
		state['last_pose'] = position + (yaw, pitch, roll)
		state['last_pose_at'] = when

		vehicle_filter = BigWorld.WGVehicleFilter2()
		entity.filter = vehicle_filter
		if getattr(entity, 'filter', None) is not vehicle_filter:
			raise RuntimeError('WGVehicleFilter2 attach readback mismatch')
		_configure_filter(vehicle_filter, descriptor)
		state['filter'] = vehicle_filter
		provider = getattr(entity, 'matrix', None)
		if provider is None:
			raise RuntimeError('OfflineEntity matrix provider is unavailable')
		# Stock VehicleAppearance sets this before advanced physics is attached.
		# It prevents the root model from feeding its own transform back into the
		# Entity/filter chain while the native body is staged out of sight.
		provider.notModel = True
		state['entity_provider'] = provider

		from gui.mods.offhangar import native_filter_bridge
		if not native_filter_bridge.seed_filter(
				vehicle_filter, when, int(space_id), position,
				(yaw, pitch, roll)):
			raise RuntimeError('Filter::input seed was rejected')
		entity.typeDescriptor = descriptor
		# See _attach_physics: this flag belongs to the stock Vehicle appearance
		# lifecycle, not WGVehicleFilter2/WGVehiclePhysics2 ownership.
		entity.isStarted = False
		entity.isPlayer = False
		mock.filter = vehicle_filter
		state['phase'] = 'seed_wait'
		state['seed_check_at'] = when + SEED_CHECK_SECONDS
		_COUNTERS['prepared'] += 1
		if _COUNTERS['prepared'] in (1, 5, 15, 29):
			LOG_NOTE('NATIVE_BOT_PHYSICS prepared=%d id=%s' % (
				_COUNTERS['prepared'], getattr(mock, 'id', '?')))
		return True
	except Exception as error:
		return _fail(mock, state, error)


def _apply_power_scale(mock, state, scale):
	""" + v8.9 Uphill Torque Assist: scale native enginePower (0.15 .. 1.60)."""
	try:
		scale = max(0.15, min(1.60, float(scale)))
		if abs(scale - float(state.get('power_scale', 1.0))) < 0.02:
			return
		physics = state.get('physics')
		base = state.get('base_engine_power')
		if physics is None or not base:
			return
		physics.enginePower = float(base) * scale
		state['power_scale'] = scale
		if scale < 0.99:
			LOG_DEBUG('NATIVE_POWER_SCALE id=%s scale=%.2f power=%.0f readback=%.0f' % (
			str(getattr(mock, 'id', '?')), scale, float(base) * scale,
			float(getattr(physics, 'enginePower', -1.0) or -1.0)))
	except Exception as _ps_err:
		LOG_DEBUG('NATIVE_POWER_SCALE_ERR %r' % (_ps_err,))


def _input_sign(value):
	value = float(value or 0.0)
	if value > 0.05:
		return 1
	if value < -0.05:
		return -1
	return 0


def _steering_sign(value):
	"""Quantise continuous steering without reacting to sub-three-degree noise."""
	value = float(value or 0.0)
	if value > STEERING_INPUT_THRESHOLD:
		return 1
	if value < -STEERING_INPUT_THRESHOLD:
		return -1
	return 0


def _movement_signals(movement, rotation):
	"""Encode the retail Avatar vehicle movement bit field."""
	signals = 0
	if movement > 0:
		signals |= MOVE_FORWARD_SIGNAL
	elif movement < 0:
		signals |= MOVE_BACKWARD_SIGNAL
	if rotation < 0:
		signals |= ROTATE_LEFT_SIGNAL
	elif rotation > 0:
		signals |= ROTATE_RIGHT_SIGNAL
	return signals


def _set_drive_input(state, movement, rotation):
	"""Apply filter prediction and the offline native track-force adapter."""
	physics = state.get('physics')
	vehicle_filter = state.get('filter')
	if physics is None or vehicle_filter is None:
		raise RuntimeError('native drive objects are unavailable')
	signals = _movement_signals(movement, rotation)
	# notifyInputKeysDown updates WGVehicleFilter2 prediction only. Track force is
	# gated independently by WGVehiclePhysics2.movementSignals in this client-only
	# adapter. Neither the generic setter nor notifyInputKeysDown wakes a rigid
	# body that went to sleep during the countdown.
	physics.movementSignals = signals
	if signals:
		physics.isFrozen = False
		state['_c_is_frozen'] = False
		state['_filter_synced_frozen'] = False
	vehicle_filter.notifyInputKeysDown(movement, rotation)
	if not state.get('_drive_verified', False):
		if int(getattr(physics, 'movementSignals', -1)) != signals:
			raise RuntimeError('native movementSignals readback mismatch')
		if signals and bool(getattr(physics, 'isFrozen', True)):
			raise RuntimeError('native rigid body wake readback mismatch')
		state['_drive_verified'] = True
	state['last_input'] = (movement, rotation)
	return signals


def step(player, mock, descriptor, throttle, turn, space_id,
		timestamp=None, active=True):
	"""Advance staged ownership and return a canonical native pose sample.

	A staged result holds the canonical seed while the filter and model provider
	are validated. In this native-only build a failed setup returns an immobile
	fail-closed result; it never authorizes legacy Python kinematics. An active
	result contains position, yaw, pitch, roll, longitudinal and angular speed.
	"""
	if not requires_native(player, mock):
		return None
	state = getattr(mock, STATE_ATTR, None)
	if (not isinstance(state, dict) or
			state.get('phase') == 'stopped'):
		if not prepare(player, mock, descriptor, space_id, timestamp):
			if (not isinstance(getattr(mock, STATE_ATTR, None), dict) and
					getattr(mock, 'bw_entity', None) is None):
				return _pending_result(mock)
			return _failed_result(
				mock, getattr(mock, STATE_ATTR, None))
		state = getattr(mock, STATE_ATTR, None)
	if state.get('phase') == 'failed':
		return _failed_result(mock, state)
	if state.get('phase') == 'faulted':
		return _frozen_result(state)
	if _SIMULATION_FAILED[0]:
		_fail(mock, state, 'native batch simulation is unavailable', True)
		if state.get('phase') == 'faulted':
			return _frozen_result(state)
		return _failed_result(mock, state)
	when = _now() if timestamp is None else float(timestamp)
	newly_activated = False
	try:
		if state.get('phase') == 'seed_wait':
			# Seed staging never reads solved batch poses, so it must run on the
			# real clock: when the re-seeded body is the ONLY native body, the
			# batch has no entries and last_simulation_at() freezes - the caller
			# then passes the same stale timestamp forever, when < seed_check_at
			# never expires and the seed stalls (log 24, solo trainer battle).
			when = _now()
			if (when < float(state.get('seed_check_at', 0.0)) or
					when < float(state.get('ground_support_retry_at', 0.0))):
				return _staged_result(state)
			# physics_shared.initVehiclePhysics is native-heavy. Attach at most
			# one bot for a rendered frame so a full line-up cannot create one
			# countdown spike.
			if _LAST_ATTACH_TIME[0] == when:
				return _staged_result(state)
			try:
				if float(BigWorld.spaceLoadStatus() or 0.0) < 0.85 and (when - float(state.get('seeded_at', when))) < 15.0:
					return _staged_result(state)
			except Exception:
				pass
			pose = _entity_pose(state)
			if not _pose_matches(
					state.get('seed_position'), state.get('seed_yaw'), pose,
					SEED_POSITION_TOLERANCE,
					SEED_YAW_TOLERANCE):
				raise RuntimeError(_pose_mismatch_reason(
					'model_handoff', state, pose))
			support = _ground_support(state)
			if support is None and not state.get('airborne_probe_done'):
				# Airborne seed (trainer lift): the normal support ray only
				# reaches GROUND_SUPPORT_RAY_DOWN below the seed pose, so a
				# lifted tank waited in seed_wait forever and drove off as a
				# python ghost (logs 22/23). Probe once far below; with solid
				# ground down there, attach the body now and let native
				# gravity land it (phase 'airborne' -> regular warmup at the
				# touchdown pose).
				state['airborne_probe_done'] = True
				_air_hit = None
				_air_seed = _point_tuple(state.get('seed_position'))
				if _air_seed is not None:
					try:
						_air_hit = BigWorld.wg_collideSegment(
							int(state.get('space_id', 0)),
							Math.Vector3(_air_seed[0], _air_seed[1] + GROUND_SUPPORT_RAY_UP, _air_seed[2]),
							Math.Vector3(_air_seed[0], _air_seed[1] - AIRBORNE_PROBE_DEPTH, _air_seed[2]),
							128)
						if _air_hit is None:
							_air_hit = BigWorld.wg_collideSegment(
								int(state.get('space_id', 0)),
								Math.Vector3(_air_seed[0], _air_seed[1] + GROUND_SUPPORT_RAY_UP, _air_seed[2]),
								Math.Vector3(_air_seed[0], _air_seed[1] - AIRBORNE_PROBE_DEPTH, _air_seed[2]),
								3)
					except Exception:
						_air_hit = None
				_air_pt = None
				if _air_hit is not None:
					try:
						_air_pt = _point_tuple(_air_hit[0])
					except Exception:
						_air_pt = None
				if (_air_pt is not None and _air_seed is not None and
						(_air_seed[1] - _air_pt[1]) > GROUND_SUPPORT_TOLERANCE):
					state['ground_support'] = _air_pt
					state['ground_support_source'] = 'airborne_probe'
					_attach_physics(player, mock, descriptor, state)
					_LAST_ATTACH_TIME[0] = when
					state['phase'] = 'airborne'
					state['airborne_deadline'] = when + AIRBORNE_MAX_SECONDS
					LOG_NOTE('NATIVE_BOT_PHYSICS airborne seed id=%s drop=%.1f' % (
						getattr(mock, 'id', '?'),
						_air_seed[1] - _air_pt[1]))
					return _staged_result(state)
			if support is None:
				if state.get('ground_support_wait_started_at') is None:
					state['ground_support_wait_started_at'] = when
				state['ground_support_misses'] = int(
					state.get('ground_support_misses', 0) or 0) + 1
				state['ground_support_retry_at'] = (
					when + GROUND_SUPPORT_RETRY_SECONDS)
				if when >= float(state.get('ground_support_log_at', 0.0)):
					space_load, chunk_loaded = _ground_support_diagnostic(state)
					LOG_NOTE('NATIVE_BOT_PHYSICS ground_wait id=%s elapsed=%.2f '
						'misses=%d source=%s space_load=%s chunk_loaded=%s' % (
							getattr(mock, 'id', '?'),
							when - float(state['ground_support_wait_started_at']),
							state['ground_support_misses'],
							state.get('ground_support_source', 'unavailable'),
							space_load, chunk_loaded))
					state['ground_support_log_at'] = (
						when + GROUND_SUPPORT_LOG_SECONDS)
				return _staged_result(state)
			state['ground_support'] = support
			state['ground_support_retry_at'] = 0.0
			try:
				_sp_cur = _point_tuple(state.get('seed_position'))
				if _sp_cur is not None and abs(float(support[1]) + 0.04 - float(pose[1])) > 0.20:
					_new_sp = (float(_sp_cur[0]), float(support[1]) + 0.04, float(_sp_cur[2]))
					state['seed_position'] = _new_sp
					state['seed_position_raw'] = _new_sp
					mock.position = Math.Vector3(*_new_sp)
					if getattr(mock, 'matrix', None) is not None:
						mock.matrix.translation = mock.position
			except Exception:
				pass
			# Keep the proven Python Servo visible while the native body settles in
			# the background. A client-created rigid body can remain stable for the
			# short minimum warmup and tip later in the countdown; revealing it here
			# exposes that invalid pose before the battle starts.
			_attach_physics(player, mock, descriptor, state)
			_LAST_ATTACH_TIME[0] = when
			state['phase'] = 'warmup'
			state['activate_at'] = when + WARMUP_SECONDS
			return _staged_result(state)

		if state.get('phase') == 'airborne':
			# The body is attached and falling under native gravity. Landing =
			# track contacts, or the solved pose reaching the probe support.
			# At touchdown re-anchor the seed reference to the landing pose so
			# the regular warmup checks (pose vs seed, support sink) pass.
			pose = state.get('frame_pose') or state.get('last_pose')
			if pose is None:
				pose = _entity_pose(state)
			contacts = 0
			try:
				_air_filter = state.get('filter')
				contacts = (int(getattr(_air_filter, 'numLeftTrackContacts', 0) or 0) +
					int(getattr(_air_filter, 'numRightTrackContacts', 0) or 0))
			except Exception:
				contacts = 0
			air_support = _point_tuple(state.get('ground_support'))
			# Client-only filters never report track contacts (log 27: landing
			# contacts=0), so touchdown is ALTITUDE-based. Touching the support
			# altitude only starts the settle phase: a 24 m/s slam compresses the
			# suspension far below the first touch point, and anchoring the seed
			# there failed the warmup support-sink check by 0.45 m.
			touched = contacts > 0
			if (not touched and pose is not None and air_support is not None and
					float(pose[1]) <= float(air_support[1]) + AIRBORNE_LAND_ALTITUDE):
				touched = True
			if touched:
				state['phase'] = 'airborne_settle'
				state['airborne_settle_since'] = when
				state['airborne_settle_last_y'] = (
					float(pose[1]) if pose is not None else None)
				state['airborne_settle_last_move'] = when
				return _staged_result(state)
			if when >= float(state.get('airborne_deadline', 0.0)):
				# v4.66 доп.15: this branch used to be the PRIMARY map-edge
				# behaviour (v4.60) - the mod had no real boundary wall, so a bot
				# that drove off the edge just fell into the void and rode this
				# branch out. offline_battle.py and bot_ai_driver.py now clamp
				# both the player and bots against a real physical boundary wall
				# (ArenaType.boundingBox, the same box TerrainGrid._inside() uses
				# for pathfinding) before they can ever get this far, so a bot
				# should no longer be able to reach open air past the map edge in
				# the first place. This stays only as a deep safety-net fallback
				# for whatever edge case still slips a body past that wall (e.g. a
				# teleport, or falling from a great height with nothing below) -
				# raising straight into _fail() would just freeze the body in
				# staticMode wherever it happened to be (mid-air, over the void),
				# so it is flagged here instead for offline_battle.py's per-tick
				# bot sweep to kill it outright (the same -60m-style out-of-bounds
				# kill the player path already has) rather than leaving a phantom
				# bot stuck in the world for the rest of the battle.
				try:
					mock._offh_native_void_fall = True
				except Exception:
					pass
				raise RuntimeError('airborne seed never landed')
			return _staged_result(state)

		if state.get('phase') == 'airborne_settle':
			# Wait for the slammed body to finish bouncing/settling, THEN anchor
			# the seed reference at the rest pose and run the regular warmup.
			# ground_support stays the deep terrain probe, so the warmup sink
			# check compares against real ground, not an in-air touch pose.
			pose = state.get('frame_pose') or state.get('last_pose')
			if pose is None:
				pose = _entity_pose(state)
			if pose is not None:
				_sy = float(pose[1])
				_sly = state.get('airborne_settle_last_y')
				if _sly is None or abs(_sy - float(_sly)) > 0.05:
					state['airborne_settle_last_y'] = _sy
					state['airborne_settle_last_move'] = when
			settled = (pose is not None and
				(when - float(state.get('airborne_settle_last_move', when))) >= 0.25)
			timed_out = (when >=
				float(state.get('airborne_settle_since', when)) + 2.0)
			if settled or timed_out:
				if pose is not None:
					state['seed_position'] = (
						float(pose[0]), float(pose[1]), float(pose[2]))
					state['seed_yaw'] = float(pose[3])
					try:
						mock.position = Math.Vector3(
							float(pose[0]), float(pose[1]), float(pose[2]))
						mock.yaw = float(pose[3])
						mock.pitch = float(pose[4])
						mock.roll = float(pose[5])
					except Exception:
						pass
				state['phase'] = 'warmup'
				state['activate_at'] = when + WARMUP_SECONDS
				LOG_NOTE('NATIVE_BOT_PHYSICS airborne landing id=%s settled=%s' % (
					getattr(mock, 'id', '?'), str(settled)))
				return _staged_result(state)
			if when >= float(state.get('airborne_deadline', 0.0)):
				raise RuntimeError('airborne seed never settled')
			return _staged_result(state)

		if state.get('phase') == 'warmup':
			if when < float(state.get('activate_at', 0.0)):
				return _staged_result(state)
			if int(state.get('simulated_frames', 0) or 0) <= 0:
				raise RuntimeError(
					'native dynamics simulator did not advance during warmup')
			if int(state.get('frame_output_generation', -1)) <= 0:
				# Activation requires one explicit post-solve Filter::output sample.
				return _staged_result(state)
			pose = _frame_pose(state, when)
			warmup_reason = _warmup_pose_reason(mock, state, pose)
			if warmup_reason is not None:
				raise RuntimeError(warmup_reason)
			if bool(getattr(state.get('physics'), 'staticMode', True)):
				raise RuntimeError(
					'native wiring invariant failed: physics became static')
			entity = getattr(mock, 'bw_entity', None)
			if (entity is None or getattr(entity, 'wgPhysics', None) is not
					state.get('physics') or getattr(entity, 'filter', None) is not
					state.get('filter') or getattr(mock, 'filter', None) is not
					state.get('filter') or bool(getattr(entity, 'isStarted', False))):
				raise RuntimeError(
					'native wiring invariant failed: owner references changed')
			# Continue validating the hidden body for the entire countdown. Only the
			# first active battle frame may transfer the visible model owner.
			if not active:
				return _staged_result(state)
			if not _activate_model_provider(mock, state):
				raise RuntimeError('native entity matrix could not own the model')
			fashion = state.get('pending_fashion')
			if fashion is not None:
				if not _bind_fashion_providers(mock, state, fashion):
					raise RuntimeError('native fashion provider bind failed')
				state['pending_fashion'] = None
			state['last_pose'] = pose
			state['last_pose_at'] = when
			_COUNTERS['activated'] += 1
			_COUNTERS['active'] += 1
			state['counted_active'] = True
			state['phase'] = 'active'
			# Native ownership begins with the Servo swap, but do not report this body
			# active until its first complete input/readback sample is validated below.
			newly_activated = True

		if state.get('phase') != 'active':
			return _failed_result(mock, state)

		movement = _input_sign(throttle) if active else 0
		_cur_pitch_np = float(getattr(mock, 'pitch', 0.0) or 0.0)
		_cur_spd_np = float(state.get('frame_speed', 0.0) or 0.0)
		_is_climbing_np = (
			movement > 0 and (
				_cur_pitch_np < -0.035 or
				(abs(_cur_pitch_np) > 0.045 and _cur_spd_np < 8.5) or
				bool(getattr(mock, '_offh_is_uphill', False))
			)
		)
		_ps = float(getattr(mock, '_offh_power_scale', 1.0) or 1.0)
		if _is_climbing_np and not bool(getattr(mock, 'isPlayer', False)):
			# v13.0 Uphill Low-Gear Torque Assist: boost native enginePower by 1.68x (up to 1.85x) on steep slopes
			# so heavy/medium tanks climb Dragon Ridge (51_asia) and Westfield (23_westfeld) without stalling!
			_eff_ps = min(1.85, _ps * 1.68)
		else:
			_eff_ps = _ps
		_apply_power_scale(mock, state, _eff_ps)
		# v13.0: In WGVehiclePhysics2, any non-zero rotation signal (bits 4|8) brakes the inner track and kills uphill speed!
		# Suppress inner-track clutch braking while climbing down to 0.15 m/s unless the turn angle is genuinely sharp (>= 0.45).
		if _is_climbing_np and abs(float(turn or 0.0)) < 0.45 and _cur_spd_np > 0.15:
			rotation = 0
		else:
			rotation = _steering_sign(turn) if active else 0
		keys = (movement, rotation)
		physics = state.get('physics')
		expected_signals = _movement_signals(movement, rotation)
		needs_drive_update = state.get('last_input') != keys
		signals_before = None
		signals_repaired = False
		if not needs_drive_update:
			_dchk = int(state.get('drv_chk', 0) or 0) + 1
			state['drv_chk'] = _dchk
			if (_dchk & 7) == 0:
				signals_before = _safe_int_attr(physics, 'movementSignals')
				if signals_before is not None and signals_before != expected_signals:
					signals_repaired = True
					needs_drive_update = True
				elif keys != (0, 0) and physics is not None and bool(state.get('_c_is_frozen', False)):
					needs_drive_update = True
		if needs_drive_update:
			signals = _set_drive_input(state, movement, rotation)
			_arm_drive_diagnostic(mock, state, when, signals)
			if signals and not _DRIVE_LOGGED[0]:
				_DRIVE_LOGGED[0] = True
				LOG_NOTE('NATIVE_BOT_PHYSICS drive active id=%s signals=%d frozen=%s' % (
					getattr(mock, 'id', '?'), signals,
					str(bool(getattr(state.get('physics'), 'isFrozen', True)))))
		if state.get('drive_diagnostic_at') is not None and not state.get('drive_diagnostic_logged'):
			_maybe_log_drive_diagnostic(
				mock, state, when, signals_before, expected_signals,
				signals_repaired)

		pose = _frame_pose(state, when)
		if pose is None:
			raise RuntimeError('entity matrix returned an invalid pose')
		px0, py0, pz0 = float(pose[0]), float(pose[1]), float(pose[2])
		if not _is_plausible_pose(pose, state.get('last_pose')):
			_rec_s = _recover_implausible_pose(mock, state, pose, when)
			if _rec_s is not None and int(state.get('_implausible_recoveries', 0) or 0) <= 6:
				pose = _rec_s
				px0, py0, pz0 = float(pose[0]), float(pose[1]), float(pose[2])
			else:
				raise RuntimeError('implausible native pose (%.1f, %.1f, %.1f)' % (px0, py0, pz0))
		state['last_pose'] = pose
		state['last_pose_at'] = when
		if newly_activated:
			if (_COUNTERS['activated'] in (1, 5, 15, 29) or
					_COUNTERS['activated'] == _COUNTERS['expected']):
				LOG_NOTE('NATIVE_BOT_PHYSICS active=%d prepared=%d failed=%d' % (
					_COUNTERS['active'], _COUNTERS['prepared'],
					_COUNTERS['failed']))
			_maybe_log_startup_complete()
		_res = state.get('_step_res')
		if _res is None:
			_res = {
				'position': pose[:3],
				'yaw': pose[3],
				'pitch': pose[4],
				'roll': pose[5],
				'velocity': float(state.get('frame_speed', 0.0) or 0.0),
				'turn_velocity': float(state.get('frame_turn_speed', 0.0) or 0.0),
			}
			state['_step_res'] = _res
		else:
			_res['position'] = pose[:3]
			_res['yaw'] = pose[3]
			_res['pitch'] = pose[4]
			_res['roll'] = pose[5]
			_res['velocity'] = float(state.get('frame_speed', 0.0) or 0.0)
			_res['turn_velocity'] = float(state.get('frame_turn_speed', 0.0) or 0.0)
		return _res
	except Exception as error:
		_fail(mock, state, error)
		if state.get('phase') == 'faulted':
			return _frozen_result(state)
		return _failed_result(mock, state)


def hold(mock):
	"""Release both native drive inputs without detaching the rigid body."""
	state = getattr(mock, STATE_ATTR, None)
	if not isinstance(state, dict) or state.get('phase') != 'active':
		return False
	try:
		_set_drive_input(state, 0, 0)
		return True
	except Exception as error:
		_fail(mock, state, 'hold failed: %s' % str(error))
		return False


def _bind_fashion_providers(mock, state, fashion):
	# v8.4 Fix for Day-1 Native Physics Hull-Sinking Bug:
	# Because the chassis model is driven by BigWorld.Servo(chassis_mp) using the solved
	# WGVehiclePhysics2 rigid-body matrix (_np_pose), vertical suspension travel is ALREADY
	# applied to the chassis root. Binding placingCompensationMatrix and physicsInfo onto
	# WGVehicleFashion caused WGVehicleFashion::dressUp to apply a SECOND vertical suspension
	# offset onto chassis.node('V') (the hull mount), making the hull sink down through the
	# tracks during landings and bumps! We bind ONLY movementInfo (for wheel spin & track scroll)
	# and clear placingCompensationMatrix / physicsInfo so hull and tracks stay 100% locked together.
	names = ('movementInfo',)
	missing = object()
	original = {}
	for name in ('placingCompensationMatrix', 'physicsInfo', 'movementInfo'):
		try:
			original[name] = getattr(fashion, name)
		except Exception:
			original[name] = missing
	try:
		for _clear_attr in ('placingCompensationMatrix', 'physicsInfo'):
			try:
				setattr(fashion, _clear_attr, None)
			except Exception:
				pass
		expected = {
			'movementInfo': state['filter'].movementInfo,
		}
		for name in names:
			setattr(fashion, name, expected[name])
		for name in names:
			if getattr(fashion, name, missing) is not expected[name]:
				raise RuntimeError(
					'fashion provider readback mismatch: %s' % name)
		state['fashion_detach_blocked'] = False
		state['bound_fashion'] = fashion
		try:
			mock._fashion = fashion
		except Exception:
			pass
		return True
	except Exception as error:
		rollback_ok = True
		for name in names:
			try:
				value = original[name]
				if value is missing:
					delattr(fashion, name)
				else:
					setattr(fashion, name, value)
				actual = getattr(fashion, name, missing)
				if value is missing:
					if actual is not missing:
						raise RuntimeError(
							'fashion provider removal readback mismatch')
				elif actual is not value:
					raise RuntimeError(
						'fashion provider rollback readback mismatch')
			except Exception:
				rollback_ok = False
		if (not rollback_ok and
				getattr(mock, '_fashion', None) is fashion):
			# A partially rebound fashion must not retain a mixed set of old and
			# native providers. Remove it from the chassis if rollback is impossible.
			chassis = (getattr(mock, '_chassis_model', None) or
				getattr(mock, 'model', None))
			detached = chassis is None
			if chassis is not None:
				try:
					delattr(chassis, 'wg_fashion')
					detached = getattr(
						chassis, 'wg_fashion', missing) is not fashion
				except Exception:
					detached = False
			state['fashion_detach_blocked'] = not detached
			if detached:
				try:
					mock._fashion = None
				except Exception:
					pass
		else:
			state['fashion_detach_blocked'] = False
		if not state.get('fashion_error_logged'):
			state['fashion_error_logged'] = True
			LOG_ERROR('NATIVE_BOT_PHYSICS fashion bind failed id=%s error=%s' % (
				getattr(mock, 'id', '?'), str(error)))
		return False


def bind_fashion(mock, fashion):
	"""Bind retail placement providers only after native ownership is active."""
	state = getattr(mock, STATE_ATTR, None)
	if (not isinstance(state, dict) or fashion is None or
			state.get('filter') is None or state.get('phase') not in (
				'seed_wait', 'warmup', 'active')):
		return False
	if state.get('phase') != 'active':
		# The visual fashion can exist during staggered startup, but retaining native
		# providers here would keep a failed filter/physics pair alive after failure.
		state['pending_fashion'] = fashion
		return True
	if _bind_fashion_providers(mock, state, fashion):
		return True
	_fail(mock, state, 'native fashion provider bind failed', True)
	return False


def _clear_hazard_recovery(mock):
	"""Discard tactical recovery state only after native ownership is clean."""
	for name in (
			'_offh_native_hazard_recovering',
			'_offh_native_hazard_anchor',
			'_offh_native_hazard_entry_yaw',
			'_offh_native_hazard_escape_endpoint',
			'_offh_native_hazard_safe_since',
			'_offh_native_last_safe_pose'):
		try:
			delattr(mock, name)
		except Exception:
			pass


def stop_mock(mock, restore_filter=False):
	state = getattr(mock, STATE_ATTR, None)
	if not isinstance(state, dict):
		try:
			setattr(mock, REQUIRED_ATTR, False)
		except Exception:
			pass
		_clear_hazard_recovery(mock)
		# No native objects exist, so demotion already satisfies the ownership
		# barrier and the relay presentation may proceed.
		return True
	previous_phase = state.get('phase')
	physics = state.get('physics')
	try:
		if physics is not None:
			physics.movementSignals = 0
	except Exception:
		pass
	try:
		vehicle_filter = state.get('filter')
		if vehicle_filter is not None:
			vehicle_filter.notifyInputKeysDown(0, 0)
	except Exception:
		pass
	try:
		if physics is not None:
			physics.staticMode = True
	except Exception:
		pass
	if not _release_callbacks(state):
		# Do not detach the filter, physics, Servo or fashion while native code still
		# retains a Python callback. Keep the complete owner graph for the next retry.
		if state.get('phase') == 'active' and state.get('counted_active'):
			_COUNTERS['active'] = max(0, _COUNTERS['active'] - 1)
			state['counted_active'] = False
		state['phase'] = 'faulted'
		state['reason'] = 'native callback release failed'
		LOG_ERROR('NATIVE_BOT_PHYSICS callback release failed id=%s' % (
			getattr(mock, 'id', '?')))
		return False
	native_servo = state.get('native_servo')
	chassis = (getattr(mock, '_chassis_model', None) or
		getattr(mock, 'model', None))
	if chassis is None:
		try:
			from gui.mods.offhangar import offline_battle as _ob_mod
			_pm_dict = getattr(_ob_mod, 'g_offh_player_loaded_models', None)
			if isinstance(_pm_dict, dict):
				chassis = _pm_dict.get('chassis') or _pm_dict.get('hull')
		except Exception:
			pass
	# WGVehicleFashion retains the native filter's placing, physics and movement
	# providers.  Release it before changing either model Servo or entity filter;
	# otherwise a failed delattr would leave an unretryable mixed-owner chassis.
	_all_fashions = set()
	for _f_cand in (getattr(mock, '_fashion', None), state.get('bound_fashion'), state.get('pending_fashion')):
		if _f_cand is not None:
			_all_fashions.add(_f_cand)
	try:
		_pl = BigWorld.player()
		if _pl is not None and getattr(_pl, '_offh_fashion_ref', None) is not None:
			_all_fashions.add(_pl._offh_fashion_ref)
	except Exception:
		pass
	for _f in _all_fashions:
		for _pname in ('placingCompensationMatrix', 'physicsInfo', 'movementInfo'):
			try:
				delattr(_f, _pname)
			except Exception:
				try:
					setattr(_f, _pname, None)
				except Exception:
					pass
	fashion = getattr(mock, '_fashion', None) or state.get('bound_fashion')
	if fashion is not None:
		fashion_detached = chassis is None
		if chassis is not None:
			try:
				delattr(chassis, 'wg_fashion')
				fashion_detached = not hasattr(chassis, 'wg_fashion')
			except AttributeError:
				fashion_detached = not hasattr(chassis, 'wg_fashion')
				# The chassis was swapped for a wreck model after the body
				# froze (guard_fault 'wreck freeze'): wg_fashion lived on the
				# OLD model and died with it, so there is nothing left to
				# detach here. Treating this as a failure deferred the whole
				# battle teardown forever (exit soft-lock after bot kills).
				fashion_detached = not hasattr(chassis, 'wg_fashion')
			except Exception as error:
				fashion_detached = False
				LOG_ERROR(
					'NATIVE_BOT_PHYSICS fashion detach failed id=%s error=%s' % (
						getattr(mock, 'id', '?'), str(error)))
		if not fashion_detached:
			# Keep the complete native owner graph for the next demotion retry.  The
			# caller must not apply a replica pose while this provider is attached.
			if state.get('phase') == 'active' and state.get('counted_active'):
				_COUNTERS['active'] = max(0, _COUNTERS['active'] - 1)
				state['counted_active'] = False
			state['phase'] = 'faulted'
			state['fashion_detach_blocked'] = True
			return False
		state['fashion_detach_blocked'] = False
		state['bound_fashion'] = None
		try:
			mock._fashion = None
		except Exception:
			pass
	old_pose_servo = getattr(mock, '_pose_servo', None)
	relay_servo_attached = (old_pose_servo is not None and
		bool(getattr(mock, '_servo_added', False)))
	servo_detached = native_servo is None
	if native_servo is not None:
		if chassis is not None:
			try:
				_has_native = _model_has_motor(chassis, native_servo)
				if _has_native is False:
					# The chassis was swapped for wreck models while this body
					# was frozen: the native Servo died with the deleted live
					# chassis, so there is nothing left to detach. Same
					# teardown class as the fashion fix above; refusing here
					# deferred the whole battle exit forever.
					servo_detached = True
				else:
					if not _sole_model_motor(chassis, native_servo):
						raise RuntimeError('native Servo is not the sole model owner')
					chassis.delMotor(native_servo)
					servo_detached = _model_motors(chassis) == []
					if not servo_detached:
						raise RuntimeError('native Servo detach readback mismatch')
			except Exception as error:
				LOG_ERROR('NATIVE_BOT_PHYSICS servo detach failed id=%s error=%s' % (
					getattr(mock, 'id', '?'), str(error)))
	if not servo_detached:
		# The relay snapshot must not acquire the model while the native Servo is
		# still attached. Keep this body frozen and retain every native reference so
		# the next demotion message can retry the ownership release.
		try:
			if physics is not None:
				physics.staticMode = True
		except Exception:
			pass
		if state.get('phase') == 'active' and state.get('counted_active'):
			_COUNTERS['active'] = max(0, _COUNTERS['active'] - 1)
			state['counted_active'] = False
		state['phase'] = 'faulted'
		return False
	if servo_detached:
		state['native_servo'] = None
		try:
			mock._native_pose_servo = None
			# During seed/warmup the original Python Servo is still the one proven
			# attached root owner. Only clear the shared flag after an actual native
			# Servo detach; otherwise demotion retries would add a duplicate Servo.
			if native_servo is not None:
				mock._servo_added = False
		except Exception:
			pass
		# A replica snapshot drives mock.matrix. Restore exactly one Python Servo
		# before dropping the native provider; otherwise pose commits update the
		# marker but the chassis remains stranded at the demotion pose.
		if (restore_filter and chassis is not None and
				getattr(mock, 'matrix', None) is not None):
			relay_servo = old_pose_servo if relay_servo_attached else None
			try:
				if relay_servo is None:
					if _model_motors(chassis) != []:
						raise RuntimeError('model still has a root owner')
					relay_servo = BigWorld.Servo(mock.matrix)
					chassis.addMotor(relay_servo)
					if not _sole_model_motor(chassis, relay_servo):
						raise RuntimeError('relay Servo attach readback mismatch')
					mock._pose_servo = relay_servo
					mock._servo_added = True
				elif not _sole_model_motor(chassis, relay_servo):
					raise RuntimeError('relay Servo ownership readback mismatch')
			except Exception:
				try:
					if relay_servo is not None and relay_servo is not old_pose_servo and \
							_model_has_motor(chassis, relay_servo):
						chassis.delMotor(relay_servo)
				except Exception:
					pass
				# Reattach the native owner if the relay presentation cannot acquire the
				# model. This keeps the demotion barrier retryable and single-owner.
				if native_servo is not None:
					try:
						chassis.addMotor(native_servo)
						if not _sole_model_motor(chassis, native_servo):
							raise RuntimeError('native Servo restore readback mismatch')
						state['native_servo'] = native_servo
						mock._native_pose_servo = native_servo
						mock._pose_servo = old_pose_servo
						mock._servo_added = True
					except Exception:
						pass
				state['phase'] = 'faulted'
				return False
	# A body released during seed/warmup may never have produced a simulated
	# last_pose; the field then still holds the AIRBORNE seed pose, so a
	# release-side mock sync teleported the tank back to +30 m and the next
	# prepare seeded in mid-air (model_handoff mismatch loop, log 29). Anchor
	# the release pose to where the body ACTUALLY rests.
	try:
		_release_pose = _entity_pose(state)
		if _release_pose is not None:
			state['last_pose'] = _release_pose
			state['last_pose_at'] = _now()
	except Exception:
		pass
	filter_released = _restore_avatar_filter(mock)
	if not filter_released:
		# The native Servo is already detached, but the entity still has an unknown
		# filter/physics owner. Do not apply a replica pose until readback proves the
		# old native owner is gone; a later demotion message safely retries here.
		state['phase'] = 'faulted'
		return False
	state['filter'] = None
	state['physics'] = None
	state['entity_provider'] = None
	state['pending_fashion'] = None
	state['bound_fashion'] = None
	state['frame_pose'] = None
	state['frame_pose_at'] = None
	state['frame_output_generation'] = -1
	state['destructible_health_callback'] = None
	state['destructible_damage_callback'] = None
	state['phase'] = 'stopped'
	try:
		setattr(mock, REQUIRED_ATTR, False)
	except Exception:
		pass
	if previous_phase in ('preparing', 'seed_wait', 'airborne', 'airborne_settle', 'warmup', 'active'):
		if previous_phase == 'active' and state.get('counted_active'):
			_COUNTERS['active'] = max(0, _COUNTERS['active'] - 1)
			state['counted_active'] = False
		_COUNTERS['stopped'] += 1
		_maybe_log_startup_complete()
	_clear_hazard_recovery(mock)
	return True


def stop_all(mocks):
	count = 0
	targets = list((mocks or {}).values())
	for mock in targets:
		if stop_mock(mock, False):
			count += 1
	if count != len(targets):
		# Keep the batch simulator, adapter and counters alive while any entity still
		# owns native callbacks/filter/physics. The battle sweep will retry this
		# transaction before it destroys models or OfflineEntity instances.
		LOG_ERROR('NATIVE_BOT_PHYSICS stop_all incomplete stopped=%d total=%d' % (
			count, len(targets)))
		return -1
	if count:
		LOG_NOTE('NATIVE_BOT_PHYSICS stopped=%d active=%d failed=%d' % (
			count, _COUNTERS['active'], _COUNTERS['failed']))
	try:
		import BigWorld
		BigWorld.flushPythonLog()
	except Exception:
		pass
	# The adapter replaces a process-global class descriptor. Restoring it is part
	# of the same ownership transaction as releasing every body: if that restore
	# is rejected, the battle sweep must keep its models/global solver state and
	# retry instead of destroying the last objects which make recovery observable.
	try:
		import OfflineEntity
		LOG_NOTE('NATIVE_BOT_PHYSICS restoring destructible callback adapter')
		try:
			import BigWorld
			BigWorld.flushPythonLog()
		except Exception:
			pass
		_rest_ok = OfflineEntity.restore_native_destructible_callback_adapter()
		LOG_NOTE('NATIVE_BOT_PHYSICS destructible callback adapter restored res=%s' % _rest_ok)
		try:
			import BigWorld
			BigWorld.flushPythonLog()
		except Exception:
			pass
		if not _rest_ok:
			LOG_ERROR('NATIVE_BOT_PHYSICS destructible adapter restore failed')
			return -1
	except Exception as error:
		LOG_ERROR('NATIVE_BOT_PHYSICS destructible adapter restore error=%s' % (
			str(error)))
		return -1
	for name in _COUNTERS:
		_COUNTERS[name] = 0
	_LAST_ATTACH_TIME[0] = None
	_STARTUP_SUMMARY_LOGGED[0] = False
	_DRIVE_LOGGED[0] = False
	LOG_NOTE('NATIVE_BOT_PHYSICS clearing dynamics simulator')
	try:
		import BigWorld
		BigWorld.flushPythonLog()
	except Exception:
		pass
	_DYNAMICS_SIMULATOR[0] = None
	_LAST_SIMULATION_AT[0] = None
	_PHYSICS_ACCUM[0] = 0.0
	_PHYSICS_ALPHA[0] = 1.0
	_SIMULATION_FAILED[0] = False
	_SIMULATION_LOGGED[0] = False
	_SIMULATION_DT_CLAMP_LOGGED[0] = False
	_PRESENTATION_DIAGNOSTIC_LOGGED[0] = 0
	LOG_NOTE('NATIVE_BOT_PHYSICS stop_all done count=%d' % count)
	try:
		import BigWorld
		BigWorld.flushPythonLog()
	except Exception:
		pass
	return count
