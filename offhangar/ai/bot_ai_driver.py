# -*- coding: utf-8 -*-
"""Engine-free short-range driver for offline battle bots.

The strategic director supplies a waypoint.  Callers supply ``direction_clear``
for the current collision/terrain query; this module chooses only throttle and
steering, so it is safe to exercise outside the BigWorld client.
"""

import math


STEERING_SCALE_RADIANS = 0.72


def _angle_delta(target, current):
	value = float(target) - float(current)
	while value > math.pi:
		value -= math.pi * 2.0
	while value < -math.pi:
		value += math.pi * 2.0
	return value


def steering_turn(target_yaw, current_yaw, turn_velocity=0.0):
	"""Return the zero-overshoot steering request for WGVehiclePhysics2.
	In WGVehiclePhysics2, any abs(turn) > 0.10 presses the digital A/D key at 100% track torque.
	Therefore, angular velocity damping must ONLY release the steering key early (return 0.0)
	and NEVER cross zero to apply opposite-direction counter-torque (which causes 5Hz left-right twitching)!"""
	raw_delta = _angle_delta(target_yaw, current_yaw)
	if abs(raw_delta) < 0.085:
		return 0.0
	damped = raw_delta - float(turn_velocity or 0.0) * 0.24
	if damped * raw_delta <= 0.0 or abs(damped) < 0.085:
		return 0.0
	return max(-1.0, min(1.0, damped / STEERING_SCALE_RADIANS))


def _yaw_to(first, second):
	return math.atan2(float(second[0]) - float(first[0]),
	                  float(second[2]) - float(first[2]))


def _distance(first, second):
	dx = float(first[0]) - float(second[0])
	dz = float(first[2]) - float(second[2])
	return math.sqrt(dx * dx + dz * dz)


def _traffic_number(value, default=0.0):
	try:
		value = float(value)
	except (TypeError, ValueError):
		return float(default)
	if value != value or value == float('inf') or value == float('-inf'):
		return float(default)
	return value


def _traffic_position(value):
	if not isinstance(value, dict):
		return (0.0, 0.0, 0.0)
	position = value.get('position') or value.get('pos')
	try:
		return (float(position[0]), float(position[1]), float(position[2]))
	except (TypeError, ValueError, IndexError):
		return (0.0, 0.0, 0.0)


def traffic_identity(entity_id, network_bot_id=None):
	"""Use the server bot id for LAN-stable traffic right of way."""
	if network_bot_id is not None:
		try:
			return int(network_bot_id)
		except (TypeError, ValueError):
			pass
	try:
		return int(entity_id)
	except (TypeError, ValueError):
		return 0


def friendly_traffic_throttle(source, command, neighbours):
	"""Return a deterministic throttle cap for nearby friendly traffic.

	A follower always respects a vehicle ahead in the same lane. At a crossing
	or merge the lower bot id has right of way, while every bot yields to a human
	vehicle. This breaks symmetric stop/turn/reverse loops without changing the
	chosen route or bypassing physical tank collision.
	"""
	throttle = max(-1.0, min(1.0,
		_traffic_number((command or {}).get('throttle'))))
	if throttle <= 0.01 or not isinstance(source, dict):
		return throttle, False
	position = _traffic_position(source)
	source_team = int(_traffic_number(source.get('team')))
	if source_team not in (1, 2):
		return throttle, False
	yaw = _traffic_number(source.get('yaw'))
	own_speed = abs(_traffic_number(source.get('speed')))
	own_length = max(0.5, _traffic_number(source.get('half_length'), 3.5))
	own_width = max(0.3, _traffic_number(source.get('half_width'), 1.7))
	sine, cosine = math.sin(yaw), math.cos(yaw)
	target_yaw = _traffic_number((command or {}).get('target_yaw'), yaw)
	target_sine, target_cosine = math.sin(target_yaw), math.cos(target_yaw)
	nearest = None
	for raw in neighbours or ():
		if not isinstance(raw, dict):
			continue
		if int(_traffic_number(raw.get('team'))) != source_team:
			continue
		other = raw.get('position') or raw.get('pos')
		if other is None:
			continue
		try:
			dx = float(other[0]) - position[0]
			dz = float(other[2]) - position[2]
			if abs(float(other[1]) - position[1]) > 5.0:
				continue
		except (TypeError, ValueError, IndexError):
			continue
		forward = dx * sine + dz * cosine
		lateral = abs(dx * cosine - dz * sine)
		if abs(_angle_delta(target_yaw, yaw)) > 0.20:
			target_forward = dx * target_sine + dz * target_cosine
			target_lateral = abs(dx * target_cosine - dz * target_sine)
			if target_forward > 0.0 and target_lateral < lateral:
				forward = target_forward
				lateral = target_lateral
		other_length = max(0.5,
			_traffic_number(raw.get('half_length'), 3.5))
		other_width = max(0.3,
			_traffic_number(raw.get('half_width'), 1.7))
		corridor_yaw = target_yaw if abs(
			_angle_delta(target_yaw, yaw)) > 0.20 else yaw
		other_yaw = _traffic_number(raw.get('yaw'), corridor_yaw)
		same_direction = abs(
			_angle_delta(other_yaw, corridor_yaw)) < 0.65
		if (not same_direction and not bool(raw.get('is_human')) and
				raw.get('id') is not None and source.get('id') is not None):
			try:
				if int(raw.get('id')) > int(source.get('id')):
					continue
			except (TypeError, ValueError):
				pass
		clearance = forward - own_length - other_length
		max_scan_clearance = 24.0 if same_direction else 5.5
		lat_limit = (own_width + other_width) * (0.95 if same_direction else 0.80)
		if (forward <= 0.0 or clearance > max_scan_clearance or
				lateral > lat_limit):
			continue
		other_velocity = raw.get('velocity') or raw.get('vel')
		try:
			other_vx = float(other_velocity[0])
			other_vz = float(other_velocity[2])
		except (TypeError, ValueError, IndexError):
			other_vx = 0.0
			other_vz = 0.0
		corridor_sine = math.sin(corridor_yaw)
		corridor_cosine = math.cos(corridor_yaw)
		other_forward = max(0.0,
			other_vx * corridor_sine + other_vz * corridor_cosine)
		candidate = (clearance, other_forward, same_direction)
		if nearest is None or candidate[0] < nearest[0]:
			nearest = candidate
	if nearest is None:
		return throttle, False
	clearance, leader_speed, is_same_lane = nearest
	safe_clearance = min(3.2, max(1.1, own_speed * 0.42))
	if clearance <= safe_clearance:
		if clearance > 0.45 or leader_speed >= 1.0:
			# Match moving leader speed smoothly without rear-ending
			paced = min(throttle, max(0.42, min(0.84, (leader_speed + 1.2) / 9.5)))
			return paced, False
		return 0.0, True
	# v12.0 Adaptive Cruise Control (20-28m follow distance in columns):
	if is_same_lane and clearance <= 22.0:
		if leader_speed >= 1.0 and own_speed > leader_speed + 0.5:
			cruise_frac = max(0.0, min(1.0, (clearance - safe_clearance) / 18.8))
			pace_cap = max(0.48, min(1.0, (leader_speed + 1.0) / 10.0 + cruise_frac * 0.45))
			return min(throttle, pace_cap), False
		elif leader_speed < 1.0 and clearance <= 14.0:
			# Leader ahead is stopped/aiming: slow slightly while preparing lateral overtake
			return min(throttle, max(0.55, min(0.85, clearance / 14.0))), False
	if own_speed > leader_speed + 2.0 and clearance <= 8.0:
		limited = min(throttle, max(0.65, min(
			1.0, (clearance - safe_clearance) / 4.5)))
		return limited, False
	return throttle, False


def intercept_point(shooter_position, target_position, target_velocity,
		projectile_speed, max_lead_time=1.5):
	"""Return the constant-velocity projectile intercept point.

	The caller still applies gun traverse, dispersion and line-of-fire checks.  A
	non-positive shell speed or an impossible intercept falls back to the current
	target pose, bounded by ``max_lead_time`` so stale network velocity cannot make
	a bot aim implausibly far ahead.
	"""
	try:
		shooter = tuple(float(value) for value in shooter_position[:3])
		target = tuple(float(value) for value in target_position[:3])
		velocity = tuple(float(value) for value in target_velocity[:3])
		speed = float(projectile_speed)
		maximum = max(0.0, float(max_lead_time))
	except Exception:
		return target_position
	if speed <= 1.0 or maximum <= 0.0:
		return target
	rx = target[0] - shooter[0]
	ry = target[1] - shooter[1]
	rz = target[2] - shooter[2]
	vx, vy, vz = velocity
	a = vx * vx + vy * vy + vz * vz - speed * speed
	b = 2.0 * (rx * vx + ry * vy + rz * vz)
	c = rx * rx + ry * ry + rz * rz
	lead_time = None
	if c <= 1e-08:
		lead_time = 0.0
	elif abs(a) <= 1e-08:
		if abs(b) > 1e-08:
			candidate = -c / b
			if candidate > 0.0:
				lead_time = candidate
	else:
		discriminant = b * b - 4.0 * a * c
		if discriminant >= 0.0:
			root = math.sqrt(discriminant)
			first = (-b - root) / (2.0 * a)
			second = (-b + root) / (2.0 * a)
			positive = [value for value in (first, second) if value > 0.0]
			if positive:
				lead_time = min(positive)
	if lead_time is None:
		# No exact solution (for example a target outrunning a very slow shell).
		# The distance/speed estimate is still a useful bounded visual lead.
		lead_time = math.sqrt(c) / speed
	lead_time = max(0.0, min(float(lead_time), maximum))
	return (target[0] + vx * lead_time,
	        target[1] + vy * lead_time,
	        target[2] + vz * lead_time)


def ballistic_solutions(shooter_position, target_position, projectile_speed,
		gravity, minimum_pitch=-math.pi * 0.5, maximum_pitch=math.pi * 0.5, allow_high_arc=False):
	"""Return physically valid ``(pitch, flight_time)`` solutions.

	BigWorld uses negative gun pitch for elevation.  Results are ordered by
	flight time, which makes the ordinary low arc the default while retaining a
	second high arc when the installed gun can actually elevate far enough.
	"""
	try:
		shooter = tuple(float(value) for value in shooter_position[:3])
		target = tuple(float(value) for value in target_position[:3])
		speed = float(projectile_speed)
		acceleration = abs(float(gravity))
		minimum = float(minimum_pitch)
		maximum = float(maximum_pitch)
	except Exception:
		return ()
	if speed <= 1.0 or acceleration <= 0.01:
		return ()
	dx = target[0] - shooter[0]
	dz = target[2] - shooter[2]
	distance = math.sqrt(dx * dx + dz * dz)
	if distance <= 0.1:
		return ()
	dy = target[1] - shooter[1]
	speed_sq = speed * speed
	discriminant = (speed_sq * speed_sq - acceleration *
		(acceleration * distance * distance + 2.0 * dy * speed_sq))
	if discriminant < 0.0:
		return ()
	root = math.sqrt(max(0.0, discriminant))
	result = []
	_numerators = (speed_sq - root, speed_sq + root) if allow_high_arc else (speed_sq - root,)
	for numerator in _numerators:
		elevation = math.atan(numerator / (acceleration * distance))
		pitch = -elevation
		if pitch < minimum - 0.0001 or pitch > maximum + 0.0001:
			continue
		horizontal_speed = speed * math.cos(elevation)
		if horizontal_speed <= 0.01:
			continue
		flight_time = distance / horizontal_speed
		if flight_time <= 0.0 or flight_time > 30.0:
			continue
		value = (pitch, flight_time)
		if not result or abs(value[0] - result[-1][0]) > 0.00001:
			result.append(value)
	result.sort(key=lambda value: value[1])
	return tuple(result)


def ballistic_intercept(shooter_position, target_position, target_velocity,
		projectile_speed, gravity, minimum_pitch=-math.pi * 0.5,
		maximum_pitch=math.pi * 0.5, prefer_high=False,
		max_lead_time=12.0):
	"""Iteratively lead a moving target using the shell's real parabola.

	Returns ``(aim_point, pitch, flight_time)`` or ``None``.  Four fixed-point
	iterations are enough for tank velocities while remaining deterministic and
	cheap enough for the legacy client.
	"""
	try:
		target = tuple(float(value) for value in target_position[:3])
		velocity = tuple(float(value) for value in target_velocity[:3])
		maximum_time = max(0.0, float(max_lead_time))
	except Exception:
		return None
	aim = target
	solution = None
	for unused in range(4):
		solutions = ballistic_solutions(
			shooter_position, aim, projectile_speed, gravity,
			minimum_pitch, maximum_pitch, bool(prefer_high))
		if not solutions:
			return None
		solution = solutions[-1] if prefer_high else solutions[0]
		flight_time = min(solution[1], maximum_time) if maximum_time else 0.0
		aim = (target[0] + velocity[0] * flight_time,
		       target[1] + velocity[1] * flight_time,
		       target[2] + velocity[2] * flight_time)
	if solution is None:
		return None
	# Re-solve once for the final lead point so pitch and flight time describe the
	# exact coordinate returned to the turret controller.
	solutions = ballistic_solutions(
		shooter_position, aim, projectile_speed, gravity,
		minimum_pitch, maximum_pitch, bool(prefer_high))
	if not solutions:
		return None
	solution = solutions[-1] if prefer_high else solutions[0]
	return (aim, solution[0], solution[1])


def ballistic_position(start_position, yaw, pitch, projectile_speed, gravity,
		flight_time):
	"""Return one point on the rendered shell parabola."""
	start = tuple(float(value) for value in start_position[:3])
	time = max(0.0, float(flight_time))
	horizontal = math.cos(float(pitch)) * float(projectile_speed)
	velocity_x = math.sin(float(yaw)) * horizontal
	velocity_y = -math.sin(float(pitch)) * float(projectile_speed)
	velocity_z = math.cos(float(yaw)) * horizontal
	return (start[0] + velocity_x * time,
	        start[1] + velocity_y * time - 0.5 * abs(float(gravity)) * time * time,
	        start[2] + velocity_z * time)


def ballistic_path(start_position, yaw, pitch, projectile_speed, gravity,
		flight_time, maximum_step=0.12):
	"""Sample the exact parabola densely enough for collision chords."""
	duration = max(0.0, float(flight_time))
	if duration <= 0.0:
		return (tuple(start_position[:3]),)
	step = max(0.02, min(0.25, float(maximum_step)))
	count = max(1, int(math.ceil(duration / step)))
	return tuple(ballistic_position(
		start_position, yaw, pitch, projectile_speed, gravity,
		duration * float(index) / float(count))
		for index in range(count + 1))


def _identity_phase(bot_id):
	"""Stable 0..1 value without Python's randomized string hash."""
	text = str(bot_id)
	value = 0
	for char in text:
		value = (value * 33 + ord(char)) & 0x7fffffff
	return float(value % 997) / 997.0


def _component_value(component, name, default=None):
	if component is None:
		return default
	try:
		if hasattr(component, 'get'):
			return component.get(name, default)
	except Exception:
		pass
	try:
		return getattr(component, name)
	except Exception:
		return default


def gun_yaw_limits(descriptor):
	"""Return installed gun yaw limits in radians plus a limited-arc flag."""
	gun = _component_value(descriptor, 'gun')
	turret = _component_value(descriptor, 'turret')
	limits = _component_value(gun, 'turretYawLimits')
	if limits is None:
		limits = _component_value(turret, 'yawLimits')
	minimum = -math.pi
	maximum = math.pi
	try:
		minimum = float(limits[0])
		maximum = float(limits[1])
		if abs(minimum) > math.pi + 0.1 or abs(maximum) > math.pi + 0.1:
			minimum = math.radians(minimum)
			maximum = math.radians(maximum)
	except Exception:
		minimum = -math.pi
		maximum = math.pi
	limited = not (minimum <= -math.pi + 0.1 and maximum >= math.pi - 0.1)
	return minimum, maximum, limited


def _rear_laying_deferred(hull_yaw, target_yaw, limit_min, limit_max, allow_rear_start=False):
	"""Prevent a moving or advancing TD from spinning 180 degrees to face rear targets."""
	relative = _angle_delta(float(target_yaw), float(hull_yaw))
	margin = min(0.04, max(0.0, limit_max - limit_min) * 0.25)
	return (not allow_rear_start and
	        abs(relative) > (math.pi * 0.5) and
	        not (limit_min + margin <= relative <= limit_max - margin))


def limited_traverse_target(relative, minimum, maximum, rear_turn=0.0):
	"""Clamp gun traverse travel without reversing an ongoing rear hull-laying side."""
	if rear_turn and abs(relative) > math.pi * 0.5 and not (minimum <= relative <= maximum):
		return maximum if rear_turn > 0.0 else minimum
	return max(minimum, min(maximum, relative))


def combat_hull_aim(hull_yaw, target_yaw, gun_min_yaw, gun_max_yaw=None,
		turn_dir=0.0, throttle=0.0, recovery_mode='drive',
		has_target=True, was_aiming=False, turn_velocity=0.0,
		allow_rear_start=False):
	"""Turn a fixed-gun or limited-traverse hull smoothly toward target_yaw with PD damping."""
	if isinstance(gun_min_yaw, (tuple, list)):
		if len(gun_min_yaw) != 2:
			return float(turn_dir), float(throttle), False
		limit_min = float(gun_min_yaw[0])
		limit_max = float(gun_min_yaw[1])
	else:
		if not has_target or target_yaw is None or gun_min_yaw is None or gun_max_yaw is None:
			return float(turn_dir), float(throttle), False
		if recovery_mode in ('reverse_turn', 'escape', 'unstuck'):
			return float(turn_dir), float(throttle), False
		limit_min = float(gun_min_yaw)
		limit_max = float(gun_max_yaw)
	if limit_min > limit_max:
		limit_min, limit_max = limit_max, limit_min
	if (limit_max - limit_min) >= 6.0:
		return float(turn_dir), float(throttle), False
	if _rear_laying_deferred(hull_yaw, target_yaw, limit_min, limit_max, allow_rear_start=allow_rear_start):
		# Target is behind limited-traverse hull: DO NOT brake or turn 180 degrees! Keep moving along route!
		return float(turn_dir), float(throttle), False
	hull_delta = _angle_delta(float(target_yaw), float(hull_yaw))
	if was_aiming:
		keep_min = min(-0.09, limit_min * 0.82)
		keep_max = max(0.09, limit_max * 0.82)
	else:
		keep_min = min(-0.07, limit_min * 0.65)
		keep_max = max(0.07, limit_max * 0.65)
	tv = float(turn_velocity or 0.0)
	if keep_min <= hull_delta <= keep_max:
		# Target is within gun traverse arc: CUT all track power immediately so hull stops cleanly!
		# Never return counter-steering 'brake' torque (in WoT physics, returning a negative turn value
		# presses the opposite steering key at 100% track torque, causing endless left-right oscillation)!
		return 0.0, 0.0, True
	center_yaw = 0.5 * (limit_min + limit_max)
	aim_error = _angle_delta(float(target_yaw), float(hull_yaw) + center_yaw)
	if abs(aim_error) < 0.045:
		return 0.0, 0.0, True
	raw_turn = (aim_error / 0.65) - (tv * 0.35)
	if raw_turn * aim_error <= 0.0 or abs(aim_error) < 0.085:
		return 0.0, 0.0, True
	if abs(aim_error) > 0.22 and abs(tv) < 0.10:
		if aim_error > 0.0 and raw_turn < 0.20:
			raw_turn = 0.20
		elif aim_error < 0.0 and raw_turn > -0.20:
			raw_turn = -0.20
	aim_turn = max(-1.0, min(1.0, raw_turn))
	if abs(aim_turn) < 0.11:
		aim_turn = 0.0
	return aim_turn, 0.0, True

def gun_aligned(target_yaw, hull_yaw, turret_yaw, desired_pitch, gun_pitch,
		yaw_tolerance=0.22, pitch_tolerance=0.35):
	"""Require both rendered traverse and elevation to settle before firing."""
	abs_yaw = float(hull_yaw) + float(turret_yaw)
	yaw_error = abs(_angle_delta(target_yaw, abs_yaw))
	pitch_error = abs(float(desired_pitch) - float(gun_pitch))
	return (yaw_error <= float(yaw_tolerance) and
	        pitch_error <= float(pitch_tolerance))


def barrel_direction(yaw, pitch):
	"""Unit direction for BigWorld's negative-pitch-is-up convention."""
	horizontal = math.cos(float(pitch))
	return (math.sin(float(yaw)) * horizontal,
	        -math.sin(float(pitch)),
	        math.cos(float(yaw)) * horizontal)


def _hull_axes_analytic(yaw, pitch, roll):
	"""Rows of BigWorld ``Matrix.setRotateYPR((yaw, pitch, roll))``.

	Row-vector convention (v' = v * M): row 0 = hull right, row 1 = hull up,
	row 2 = hull forward, all in world space.  Row 2 is exactly
	``barrel_direction(yaw, pitch)``, i.e. negative pitch is nose-up.
	Used only when the engine ``Math`` module is unavailable (offline tests).
	"""
	sy, cy = math.sin(yaw), math.cos(yaw)
	sp, cp = math.sin(pitch), math.cos(pitch)
	sr, cr = math.sin(roll), math.cos(roll)
	right = (cy * cr + sp * sy * sr, cp * sr, -sy * cr + sp * cy * sr)
	up = (-cy * sr + sp * sy * cr, cp * cr, sy * sr + sp * cy * cr)
	forward = (cp * sy, -sp, cp * cy)
	return right, up, forward


def hull_axes(yaw, pitch, roll):
	"""World-space (right, up, forward) unit axes of a hull pose (zero C++ heap allocations)."""
	return _hull_axes_analytic(float(yaw), float(pitch), float(roll))


def world_to_hull_aim(world_yaw, world_pitch, hull_yaw, hull_pitch, hull_roll):
	"""Turn a WORLD aim (yaw, pitch) into (turret_yaw, gun_pitch) for the model."""
	hp = float(hull_pitch)
	hr = float(hull_roll)
	if abs(hp) < 0.002 and abs(hr) < 0.002:
		return _angle_delta(world_yaw, hull_yaw), float(world_pitch) - hp
	d = barrel_direction(world_yaw, world_pitch)
	right, up, forward = _hull_axes_analytic(float(hull_yaw), hp, hr)
	lx = d[0] * right[0] + d[1] * right[1] + d[2] * right[2]
	ly = d[0] * up[0] + d[1] * up[1] + d[2] * up[2]
	lz = d[0] * forward[0] + d[1] * forward[1] + d[2] * forward[2]
	turret_yaw = math.atan2(lx, lz)
	gun_pitch = -math.atan2(ly, math.sqrt(lx * lx + lz * lz))
	return turret_yaw, gun_pitch


def hull_barrel_direction(hull_yaw, hull_pitch, hull_roll, turret_yaw, gun_pitch):
	"""World unit direction of the RENDERED barrel.

	Same chain the model uses: hull matrix * turret yaw (HP_turretJoint) * gun
	pitch (HP_gunJoint).  ``barrel_direction(hull_yaw + turret_yaw, gun_pitch)``
	is only equal to this on level ground.
	"""
	local = barrel_direction(turret_yaw, gun_pitch)
	right, up, forward = hull_axes(hull_yaw, hull_pitch, hull_roll)
	return (local[0] * right[0] + local[1] * up[0] + local[2] * forward[0],
	        local[0] * right[1] + local[1] * up[1] + local[2] * forward[1],
	        local[0] * right[2] + local[1] * up[2] + local[2] * forward[2])


class LocalDriver(object):
	"""Stateful local steering, keyed only by bot id.

	``direction_clear(absolute_yaw)`` must return whether a short vehicle-length
	segment in that direction is drivable.  It may raise; a failed probe is
	treated as blocked.
	"""
	_CANDIDATE_OFFSETS = (
		0.0, 0.20, -0.20, 0.42, -0.42, 0.66, -0.66, 0.92, -0.92, 1.20, -1.20, 1.50, -1.50, 1.82, -1.82)
	steering_turn = staticmethod(steering_turn)
	gun_yaw_limits = staticmethod(gun_yaw_limits)
	combat_hull_aim = staticmethod(combat_hull_aim)
	gun_aligned = staticmethod(gun_aligned)
	barrel_direction = staticmethod(barrel_direction)
	hull_axes = staticmethod(hull_axes)
	world_to_hull_aim = staticmethod(world_to_hull_aim)
	hull_barrel_direction = staticmethod(hull_barrel_direction)
	intercept_point = staticmethod(intercept_point)
	ballistic_solutions = staticmethod(ballistic_solutions)
	ballistic_intercept = staticmethod(ballistic_intercept)
	ballistic_position = staticmethod(ballistic_position)
	ballistic_path = staticmethod(ballistic_path)
	traffic_identity = staticmethod(traffic_identity)
	friendly_traffic_throttle = staticmethod(friendly_traffic_throttle)

	def __init__(self, stuck_seconds=1.8, recovery_seconds=0.85,
			separation_radius=12.0, failure_ttl=2.0,
			traffic_wait_seconds=1.25):
		self.stuck_seconds = max(0.4, float(stuck_seconds))
		self.recovery_seconds = max(0.25, float(recovery_seconds))
		self.separation_radius = max(2.0, float(separation_radius))
		self.failure_ttl = max(0.25, float(failure_ttl))
		self.traffic_wait_seconds = max(0.4, float(traffic_wait_seconds))
		self.states = {}
		self.grid = None
		# Standalone callers are active by default. The battle loop explicitly
		# supplies the countdown phase before asking this driver for an order.
		self._battle_active = True

	def _get_3d_grid(self):
		if self.grid is not None and getattr(self.grid, 'prebaked', False):
			return self.grid
		try:
			import sys as _drv_sys
			mod = _drv_sys.modules.get('gui.mods.offhangar.offline_battle')
			nav = getattr(mod, 'g_offh_terrain_navigator', None) if mod is not None else None
			grid = getattr(nav, 'grid', None)
			if grid is not None and getattr(grid, 'prebaked', False):
				self.grid = grid
				return grid
		except Exception:
			pass
		return None

	@staticmethod
	def resolve_order_positions(position, aim_position, move_position, face_position):
		"""Resolve optional tactical targets without mistaking travel for a hold."""
		stop_without_route = aim_position is None and move_position is None
		if stop_without_route:
			aim_position = position
		elif aim_position is None:
			# Server route orders intentionally omit an aim target until an enemy is
			# spotted.  Use the route target for facing, but do not apply idle braking.
			aim_position = move_position
		if move_position is None:
			move_position = aim_position
		if face_position is None:
			face_position = move_position
		return aim_position, move_position, face_position, stop_without_route

	def forget(self, bot_id):
		self.states.pop(bot_id, None)

	def wait_for_traffic(self, bot_id, dt=0.0, hard_stop=True):
		"""Keep a short right-of-way wait out of stuck recovery.

		A hard stop is bounded so a stationary player or route crossing cannot hold
		the same bot forever. Once the wait expires, arm the existing staggered
		reverse/pivot recovery; the waiting bot still never drives through the
		vehicle that owns right of way.
		"""
		state = self.states.get(bot_id)
		if state is None:
			return False
		if not hard_stop:
			state['traffic_wait_time'] = 0.0
		else:
			step = min(0.35, max(0.0, float(dt)))
			state['traffic_wait_time'] += step
			limit = self.traffic_wait_seconds + state['phase'] * 0.35
			if state.get('clock', 0.0) > 12.0 and state['traffic_wait_time'] >= limit:
				threshold = self.stuck_seconds + state['phase'] * 0.42
				state['stuck_time'] = max(state['stuck_time'], threshold)
				return True
		state['stuck_time'] = 0.0
		state['recovery_time'] = 0.0
		return True

	def clear_traffic_wait(self, bot_id):
		"""Forget an earlier queue once this bot is no longer yielding."""
		state = self.states.get(bot_id)
		if state is None:
			return False
		state['traffic_wait_time'] = 0.0
		return True

	def set_battle_active(self, active):
		"""Reset stale steering history when the battle becomes active."""
		active = bool(active)
		if active and not self._battle_active:
			self.states.clear()
		self._battle_active = active

	def _state(self, bot_id, position):
		state = self.states.get(bot_id)
		if state is None:
			phase = _identity_phase(bot_id)
			state = {
				'last_position': (float(position[0]), float(position[2])),
				'progress_anchor': (float(position[0]), float(position[2])),
				'progress_anchor_clock': 0.0,
				'stuck_time': 0.0,
				'recovery_time': 0.0,
				'recovery_count': 0,
				'steering_yaw': None,
				'steering_age': 999.0,
				'plan_age': 999.0,
				'phase': phase,
				'clock': 0.0,
				'failed_yaws': {},
				'escape_side': 0.0,
				'escape_side_until': 0.0,
				'last_desired_yaw': None,
				'traffic_wait_time': 0.0,
			}
			self.states[bot_id] = state
		return state

	def _yaw_key(self, yaw):
		return int(math.floor(float(yaw) * 4.0 + 0.5))

	def remember_failure(self, bot_id, yaw, ttl=None):
		"""Temporarily penalize a direction after a caller-observed bad path.

		Use this when a terrain probe was clear but later movement establishes that
		the direction is a ditch, steep lip, or another unusable local route.
		"""
		state = self.states.get(bot_id)
		if state is None:
			return
		if ttl is None:
			ttl = self.failure_ttl
		ttl = max(0.1, float(ttl))
		state['failed_yaws'][self._yaw_key(yaw)] = state['clock'] + ttl
		desired = state.get('last_desired_yaw')
		offset = _angle_delta(yaw, desired) if desired is not None else 0.0
		if abs(offset) >= 0.10:
			side = 1.0 if offset > 0.0 else -1.0
		else:
			# Adjacent ids choose opposite initial sides, then retain that side while
			# widening the escape angle. This prevents left/right wall grinding and
			# also helps two touching hulls stop mirroring each other.
			side = 1.0 if (int(state['phase'] * 997.0 + 0.5) & 1) else -1.0
		state['escape_side'] = side
		state['escape_side_until'] = state['clock'] + min(2.0, max(0.8, ttl))
		state['steering_yaw'] = None
		state['plan_age'] = 999.0

	def _failure_penalty(self, state, yaw):
		key = self._yaw_key(yaw)
		expires = state['failed_yaws'].get(key)
		if expires is None:
			return 0.0
		if expires <= state['clock']:
			state['failed_yaws'].pop(key, None)
			return 0.0
		return 3.0 + (expires - state['clock']) / self.failure_ttl

	def _prune_failures(self, state):
		failed = state['failed_yaws']
		for key, expires in list(failed.items()):
			if expires <= state['clock']:
				failed.pop(key, None)
		if len(failed) > 32:
			ordered = sorted(failed.items(), key=lambda item: item[1])
			for key, unused in ordered[:len(failed) - 32]:
				failed.pop(key, None)

	def _neighbour_position(self, neighbour):
		if isinstance(neighbour, dict):
			return neighbour.get('position') or neighbour.get('pos')
		return neighbour

	def _separation_yaw(self, position, desired_yaw, neighbours,
			half_length, half_width):
		"""Return an escape heading only for hulls that already overlap.

		Moving OBB prediction handles impending collisions. Treating every tank
		inside a broad radius as an emergency made harmless side-by-side traffic
		continually override the route and visibly reconsider its steering.
		"""
		push_x = 0.0
		push_z = 0.0
		for neighbour in neighbours or ():
			other = self._neighbour_position(neighbour)
			if other is None:
				continue
			try:
				if _distance(position, other) > 20.0:
					continue
			except Exception:
				continue
			try:
				if abs(float(other[1]) - float(position[1])) > 5.0:
					continue
			except Exception:
				pass
			try:
				dx = float(position[0]) - float(other[0])
				dz = float(position[2]) - float(other[2])
				dist = math.sqrt(dx * dx + dz * dz)
			except Exception:
				continue
			if dist < 0.05 or dist >= self.separation_radius:
				continue
			other_yaw = 0.0
			other_length = half_length
			other_width = half_width
			if isinstance(neighbour, dict):
				other_yaw = float(neighbour.get('yaw', 0.0) or 0.0)
				other_length = float(neighbour.get('half_length', half_length) or half_length)
				other_width = float(neighbour.get('half_width', half_width) or half_width)
			if dist > 8.6 or not self._obb_overlap(
					position, desired_yaw, half_length + 0.75, half_width + 0.75,
					other, other_yaw, other_length + 0.75, other_width + 0.75):
				continue
			weight = max(0.15, (self.separation_radius - dist) / self.separation_radius)
			push_x += dx / dist * weight
			push_z += dz / dist * weight
		if abs(push_x) + abs(push_z) < 0.001:
			return None
		return math.atan2(push_x, push_z)

	def _clear(self, direction_clear, yaw):
		try:
			return bool(direction_clear(yaw))
		except Exception:
			return False

	def _velocity(self, value):
		if value is None:
			return (0.0, 0.0)
		try:
			return (float(value[0]), float(value[2]))
		except Exception:
			try:
				return (float(value[0]), float(value[1]))
			except Exception:
				return (0.0, 0.0)

	def _obb_overlap(self, first, first_yaw, first_length, first_width,
				 second, second_yaw, second_length, second_width):
		"""2D rectangle SAT, using yaw convention atan2(x, z)."""
		axes = ((math.sin(first_yaw), math.cos(first_yaw)),
		        (math.cos(first_yaw), -math.sin(first_yaw)),
		        (math.sin(second_yaw), math.cos(second_yaw)),
		        (math.cos(second_yaw), -math.sin(second_yaw)))
		forward_a = (math.sin(first_yaw), math.cos(first_yaw))
		side_a = (math.cos(first_yaw), -math.sin(first_yaw))
		forward_b = (math.sin(second_yaw), math.cos(second_yaw))
		side_b = (math.cos(second_yaw), -math.sin(second_yaw))
		dx = float(second[0]) - float(first[0])
		dz = float(second[2]) - float(first[2])
		for axis in axes:
			distance = abs(dx * axis[0] + dz * axis[1])
			radius_a = (abs(forward_a[0] * axis[0] + forward_a[1] * axis[1]) * first_length +
			            abs(side_a[0] * axis[0] + side_a[1] * axis[1]) * first_width)
			radius_b = (abs(forward_b[0] * axis[0] + forward_b[1] * axis[1]) * second_length +
			            abs(side_b[0] * axis[0] + side_b[1] * axis[1]) * second_width)
			if distance > radius_a + radius_b:
				return False
		return True

	def _prediction_clear(self, position, candidate_yaw, speed, velocity,
				neighbours, half_length, half_width, rollout_Window=False):
		"""Reject a locally clear ray if its next 0.9s overlaps another OBB (or static wreck ahead)."""
		own_speed = max(0.0, abs(float(speed)))
		_skip_live_moving = own_speed < (2.4 if rollout_Window else 1.55)
		desired_vx = math.sin(candidate_yaw) * own_speed
		desired_vz = math.cos(candidate_yaw) * own_speed
		actual_vx, actual_vz = self._velocity(velocity)
		if abs(actual_vx) + abs(actual_vz) > 0.05:
			own_vx = actual_vx * 0.45 + desired_vx * 0.55
			own_vz = actual_vz * 0.45 + desired_vz * 0.55
		else:
			own_vx = desired_vx
			own_vz = desired_vz
		_cand_ux = math.sin(candidate_yaw)
		_cand_uz = math.cos(candidate_yaw)
		for neighbour in neighbours or ():
			other = self._neighbour_position(neighbour)
			if other is None:
				continue
			try:
				if abs(float(other[1]) - float(position[1])) > 5.0:
					continue
			except Exception:
				pass
			_is_wreck_nb = isinstance(neighbour, dict) and bool(neighbour.get('is_wreck', False))
			if _skip_live_moving and not _is_wreck_nb:
				continue
			other_yaw = 0.0
			other_velocity = None
			other_length = half_length
			other_width = half_width
			if isinstance(neighbour, dict):
				other_yaw = float(neighbour.get('yaw', 0.0) or 0.0)
				other_velocity = neighbour.get('velocity') or neighbour.get('vel')
				other_length = float(neighbour.get('half_length', half_length) or half_length)
				other_width = float(neighbour.get('half_width', half_width) or half_width)
			if _is_wreck_nb:
				# Static dead tank wreck:
				# When multiple wrecks are nearby (wreck piles/chokepoint casualties), relax margins and probe
				# short/medium distances (2.2m, 4.2m) with 0.72 scale so the tank can steer through gaps
				# without all 9 candidate offsets being killed and triggering spastic recovery thrashing!
				_wreck_cluster = sum(1 for _nb_c in neighbours if isinstance(_nb_c, dict) and _nb_c.get('is_wreck', False)) >= 2
				_w_dists = (2.2, 4.2) if _wreck_cluster else (2.4, 4.8)
				_w_scale = 0.72 if _wreck_cluster else 0.84
				for _wd in _w_dists:
					_w_own = (float(position[0]) + _cand_ux * _wd, 0.0, float(position[2]) + _cand_uz * _wd)
					if self._obb_overlap(_w_own, candidate_yaw, half_length * _w_scale, half_width * _w_scale,
							other, other_yaw, other_length * _w_scale, other_width * _w_scale):
						return False
				continue
			other_vx, other_vz = self._velocity(other_velocity)
			if self._obb_overlap(position, candidate_yaw, half_length, half_width,
					other, other_yaw, other_length, other_width):
				continue
			_horizons = (0.30, 0.58) if rollout_Window else (0.32, 0.68, 1.00)
			_scale_obb = 0.84 if rollout_Window else 0.94
			for horizon in _horizons:
				own = (float(position[0]) + own_vx * horizon, 0.0,
				       float(position[2]) + own_vz * horizon)
				predicted = (float(other[0]) + other_vx * horizon, 0.0,
				             float(other[2]) + other_vz * horizon)
				if self._obb_overlap(own, candidate_yaw, half_length * _scale_obb, half_width * _scale_obb,
						predicted, other_yaw, other_length * _scale_obb, other_width * _scale_obb):
					return False
		return True

	def _choose_yaw(self, state, desired_yaw, position, speed, velocity,
				neighbours, direction_clear, half_length, half_width, climb_contour=False):
		separation = self._separation_yaw(
			position, desired_yaw, neighbours, half_length, half_width)
		candidates = []
		# v13.0 Serpentine Diagonal Slope Tacking ("под углом постараться"):
		# Alternate tack side every 6.5s so tanks zig-zag up steep ridges instead of drifting off one side.
		contour_side = 1.0 if (int((float(state.get('clock', 0.0) or 0.0) + float(state.get('phase', 0.5) or 0.5) * 10.0) / 6.5) % 2 == 0) else -1.0
		prev_steer = state.get('steering_yaw')
		prev_off = _angle_delta(prev_steer, desired_yaw) if prev_steer is not None else 0.0
		grid_3d = self._get_3d_grid()
		lookahead_3d = max(7.5, min(13.0, float(half_length) + 3.0 + abs(float(speed)) * 0.75))
		h0_3d = None
		if grid_3d is not None and hasattr(grid_3d, 'ground_height_3d'):
			try:
				h0_3d = grid_3d.ground_height_3d(float(position[0]), float(position[2]), float(position[1]) if len(position) >= 3 else None)
				if climb_contour and h0_3d is not None:
					_hl_d = grid_3d.ground_height_3d(float(position[0]) + math.sin(desired_yaw - 0.45) * 8.0, float(position[2]) + math.cos(desired_yaw - 0.45) * 8.0, h0_3d)
					_hr_d = grid_3d.ground_height_3d(float(position[0]) + math.sin(desired_yaw + 0.45) * 8.0, float(position[2]) + math.cos(desired_yaw + 0.45) * 8.0, h0_3d)
					if _hl_d is not None and _hr_d is not None and abs(float(_hl_d) - float(_hr_d)) > 0.30:
						contour_side = -1.0 if float(_hl_d) < float(_hr_d) else 1.0
			except Exception:
				h0_3d = None
		for offset in self._CANDIDATE_OFFSETS:
			candidate = desired_yaw + offset
			grid_cost = 0.0
			if grid_3d is not None and hasattr(grid_3d, 'score_heading_3d'):
				try:
					g_sc = grid_3d.score_heading_3d(position, candidate, lookahead_3d, half_width)
				except TypeError:
					g_sc = grid_3d.score_heading_3d(position, candidate, lookahead_3d)
				if g_sc is None:
					# 3D grid proves fatal cliff/water or solid obstacle within 5m: penalize heavily
					grid_cost = 50000.0
				else:
					grid_cost = float(g_sc) * 0.45
			score = abs(offset) + self._failure_penalty(state, candidate) + grid_cost
			# Never subtract from score so a diagonal offset (+-0.22 rad) beats straight ahead (0.0 rad)!
			# Only penalize flipping to the opposite side when avoiding an obstacle.
			if abs(prev_off) > 0.14 and abs(offset) > 0.14 and (offset * prev_off < 0.0):
				score += 0.42
			if climb_contour:
				# When climbing a steep slope at low speed, traverse diagonally (14..42 deg) along the gentlest 3D gradient!
				if 0.22 <= abs(offset) <= 0.75:
					score -= (0.62 if (offset * contour_side > 0.0) else 0.42)
				elif abs(offset) < 0.15:
					score += 0.55
				if h0_3d is not None:
					try:
						h_cand = grid_3d.ground_height_3d(
							float(position[0]) + math.sin(candidate) * 8.0,
							float(position[2]) + math.cos(candidate) * 8.0,
							h0_3d)
						if h_cand is not None:
							grad_c = (float(h_cand) - float(h0_3d)) / 8.0
							if grad_c > 0.22:
								score += (grad_c - 0.22) * 3.5
					except Exception:
						pass
			if (state.get('escape_side_until', 0.0) > state['clock'] and
					float(offset) * float(state.get('escape_side', 0.0)) < -0.01):
				# Continue around the selected side of a broad obstacle before trying
				# the opposite side. The bias is finite, so an exhausted side still
				# falls back to the other candidate fan.
				score += 1.25
			if separation is not None:
				# When bodies overlap, separation outranks route alignment; otherwise
				# two tanks can choose the same narrow opening forever.
				score = score * 0.30 + abs(_angle_delta(candidate, separation))
			candidates.append((score, candidate))
		candidates.sort(key=lambda item: item[0])
		# Probe in score order and return the first fully viable direction. Most
		# frames need one terrain ray set instead of probing all seven candidates.
		_is_rollout = float(state.get('clock', 0.0) or 0.0) < 10.0
		_first_terrain_clear = None
		for cand_score, candidate in candidates:
			if cand_score >= 40000.0:
				# Fatal cliff/water/solid impassable hazard heading: NEVER DRIVE HERE!
				continue
			if self._clear(direction_clear, candidate):
				if _first_terrain_clear is None:
					_first_terrain_clear = candidate
				if self._prediction_clear(position, candidate, speed, velocity,
						neighbours, half_length, half_width, rollout_Window=_is_rollout):
					return candidate
		# [FIX v12.0] Eliminate close-range twitching ("ёрзали на месте") without ramming point-blank tanks:
		# If terrain (self._clear) is open and only medium-range OBB prediction (> 7.5m) blocked all candidates,
		# steer along _first_terrain_clear instead of triggering reverse_turn. If another tank is < 7.5m dead ahead,
		# return None so the bot properly yields/backs off instead of ramming allies!
		if _first_terrain_clear is not None and neighbours:
			_close_blk = False
			_fx = math.sin(float(_first_terrain_clear))
			_fz = math.cos(float(_first_terrain_clear))
			for _nb in neighbours:
				_np = _nb.get('position') if isinstance(_nb, dict) else getattr(_nb, 'position', None)
				if _np is not None:
					_ndx = float(_np[0]) - float(position[0])
					_ndz = float(_np[2]) - float(position[2])
					_fwd_p = _ndx * _fx + _ndz * _fz
					_lat_p = abs(_ndx * _fz - _ndz * _fx)
					_is_w = isinstance(_nb, dict) and bool(_nb.get('is_wreck', False))
					# Static corpses are stationary; only consider them blocking if point-blank (<3.2m fwd, <2.2m lat)
					# so the bot smoothly steers past corpses instead of dropping into reverse_turn!
					_fwd_lim = 3.2 if _is_w else 6.4
					_lat_lim = 2.2 if _is_w else 3.3
					if 0.0 < _fwd_p < _fwd_lim and _lat_p < _lat_lim:
						_close_blk = True
						break
			if not _close_blk:
				return _first_terrain_clear
		return None

	def drive(self, bot_id, position, yaw, speed, dt, target, neighbours,
			direction_clear, velocity=None, half_length=3.5, half_width=1.7,
			movement_intent=True, allow_tactical_reverse=False):
		"""Return ``throttle``, ``turn``, ``target_yaw`` and ``recovery_mode``.

		All timing uses supplied seconds.  ``dt`` is clamped so a paused client
		cannot immediately declare every bot stuck when it resumes.
		"""
		state = self._state(bot_id, position)
		step = min(0.35, max(0.0, float(dt)))
		state['clock'] += step
		self._prune_failures(state)
		state['steering_age'] += step
		state['plan_age'] += step
		movement_intent = bool(movement_intent) and self._battle_active
		desired_yaw = _yaw_to(position, target)
		state['last_desired_yaw'] = desired_yaw
		target_distance = _distance(position, target)
		if not movement_intent:
			# Cover/engagement orders intentionally stop within a tolerance. Do not
			# reinterpret that commanded hold as a stuck tank 1.8 seconds later.
			state['stuck_time'] = 0.0
			state['recovery_time'] = 0.0
			state['steering_yaw'] = None
			return {
				'throttle': 0.0,
				'turn': 0.0,
				'target_yaw': float(yaw),
				'recovery_mode': 'arrived',
			}
		displacement = _distance((position[0], 0.0, position[2]),
		                         (state['last_position'][0], 0.0,
		                          state['last_position'][1]))
		state['last_position'] = (float(position[0]), float(position[2]))
		_arr_radius = 2.2 if state.get('was_arrived', False) else 1.4
		if target_distance <= _arr_radius:
			# Reaching a waypoint is a stop with hysteresis (2.6m enter, 4.5m exit) so tank inertia
			# never overshoots the stop circle and triggers a forward/reverse seesaw ('качели')!
			state['was_arrived'] = True
			state['stuck_time'] = 0.0
			state['recovery_time'] = 0.0
			state['steering_yaw'] = None
			return {
				'throttle': 0.0,
				'turn': 0.0,
				'target_yaw': float(yaw),
				'recovery_mode': 'arrived',
			}
		state['was_arrived'] = False

		_prev_yaw_s = state.get('last_yaw')
		_yaw_delta_s = abs(_angle_delta(float(yaw), float(_prev_yaw_s))) if _prev_yaw_s is not None else 0.0
		state['last_yaw'] = float(yaw)
		_was_pivoting = bool(state.get('pivoting', False))
		state['pivoting'] = False
		# v12.3 Net-Progress Anchor + Anti-Rock-Trap ("камни-ловушки") Stuck Detection:
		# When a tank rides onto a boulder/rock, suspension bounce can make instantaneous `speed` jiggle
		# at 0.3..1.0 m/s while the hull stays trapped in the same 1.1m circle! Track net 2D progress:
		# v13.0 True 3D Terrain Slope Awareness & Anti-Rollback Uphill Climbing:
		# Measure actual 3D terrain elevation ahead along both desired_yaw and hull yaw so uphill climbing
		# is detected even when target[1] == position[1], and NEVER trigger downhill reverse_turn or ban
		# desired_yaw when a tank is grinding slowly uphill!
		_terrain_grade_ahead = 0.0
		_grid_up = self._get_3d_grid()
		if _grid_up is not None and hasattr(_grid_up, 'ground_height_3d'):
			try:
				_h_here = _grid_up.ground_height_3d(float(position[0]), float(position[2]), float(position[1]) if len(position) >= 3 else None)
				if _h_here is not None:
					_h_des8 = _grid_up.ground_height_3d(
						float(position[0]) + math.sin(desired_yaw) * 8.0,
						float(position[2]) + math.cos(desired_yaw) * 8.0,
						_h_here)
					_h_yw6 = _grid_up.ground_height_3d(
						float(position[0]) + math.sin(float(yaw)) * 6.0,
						float(position[2]) + math.cos(float(yaw)) * 6.0,
						_h_here)
					_g1 = ((float(_h_des8) - float(_h_here)) / 8.0) if _h_des8 is not None else 0.0
					_g2 = ((float(_h_yw6) - float(_h_here)) / 6.0) if _h_yw6 is not None else 0.0
					_terrain_grade_ahead = max(_g1, _g2)
			except Exception:
				_terrain_grade_ahead = 0.0
		_tgt_grade = ((float(target[1]) - float(position[1])) / max(1.0, target_distance)) if (len(target) >= 3 and len(position) >= 3) else 0.0
		_climb_grade_est = max(_tgt_grade, _terrain_grade_ahead)
		_is_uphill = bool(_climb_grade_est >= 0.065 or (len(target) >= 3 and len(position) >= 3 and (target[1] - position[1]) > 0.40))
		state['is_uphill'] = _is_uphill
		_p_anc = state.get('progress_anchor') or (float(position[0]), float(position[2]))
		_p_anc_clk = float(state.get('progress_anchor_clock', 0.0) or 0.0)
		_net_prog = _distance((position[0], 0.0, position[2]), (_p_anc[0], 0.0, _p_anc[1]))
		_prog_req = 0.40 if _is_uphill else 1.15
		_stall_lim = 4.20 if _is_uphill else 1.15
		if _net_prog >= _prog_req or state.get('clock', 0.0) <= 1.0:
			state['progress_anchor'] = (float(position[0]), float(position[2]))
			state['progress_anchor_clock'] = float(state.get('clock', 0.0) or 0.0)
			_net_stall_age = 0.0
		else:
			_net_stall_age = max(0.0, float(state.get('clock', 0.0) or 0.0) - _p_anc_clk)
		_min_disp = (0.06 if _is_uphill else 0.45) * step
		_uphill_moving = bool(_is_uphill and (float(speed) >= 0.20 or displacement >= 0.06 * step) and self._clear(direction_clear, float(yaw)))
		_is_wedged = (not _uphill_moving) and ((displacement < (0.12 * step) and abs(float(speed)) < 0.18) or (_net_stall_age >= _stall_lim))
		if (state.get('clock', 0.0) > 1.2 and _distance(position, target) > 2.6 and
				not _was_pivoting and not _uphill_moving and
				(_net_stall_age >= _stall_lim or (displacement < _min_disp and abs(float(speed)) < (0.16 if _is_uphill else 0.25) and (_yaw_delta_s < 0.035 or _is_wedged)))):
			state['stuck_time'] += step * (0.65 if _is_uphill else (1.45 if _net_stall_age >= _stall_lim else 1.0))
		else:
			state['stuck_time'] = max(0.0, state['stuck_time'] - step * 1.5)

		threshold = (self.stuck_seconds + 1.45 if _is_uphill else self.stuck_seconds) + state['phase'] * 0.42
		_rec_cd = float(state.get('recovery_cooldown_until', 0.0) or 0.0)
		if state['recovery_time'] > 0.0:
			state['recovery_time'] = max(0.0, state['recovery_time'] - step)
			if state['recovery_time'] == 0.0:
				state['recovery_count'] += 1
				state['stuck_time'] = 0.0
				state['progress_anchor'] = (float(position[0]), float(position[2]))
				state['progress_anchor_clock'] = float(state.get('clock', 0.0) or 0.0)
				state['recovery_cooldown_until'] = state['clock'] + 2.4
		else:
			if state['stuck_time'] >= threshold and state['clock'] >= _rec_cd:
				if _is_uphill:
					# On an uphill climb, NEVER ban desired_yaw and NEVER reverse 2.1s down the mountain!
					# Only ban the exact stalled hull yaw for 3.0s so _choose_yaw picks a diagonal tacking ray!
					state['failed_yaws'][self._yaw_key(float(yaw))] = state['clock'] + 3.0
					state['steering_yaw'] = None
					state['plan_age'] = 999.0
					state['stuck_time'] = 0.0
					state['progress_anchor'] = (float(position[0]), float(position[2]))
					state['progress_anchor_clock'] = float(state.get('clock', 0.0) or 0.0)
					state['recovery_cooldown_until'] = state['clock'] + 2.5
					# Only enter reverse recovery if the forward/diagonal rays are physically blocked by a wall/rock
					if not (self._clear(direction_clear, desired_yaw + 0.44) or self._clear(direction_clear, desired_yaw - 0.44)):
						state['recovery_time'] = 0.85
				else:
					_ban_ttl = max(8.0, float(self.failure_ttl) * 2.0)
					for _b_yw in (state.get('last_clear_yaw'), desired_yaw, float(yaw), float(yaw) - 0.28, float(yaw) + 0.28):
						if _b_yw is not None:
							state['failed_yaws'][self._yaw_key(float(_b_yw))] = state['clock'] + _ban_ttl
					state['steering_yaw'] = None
					state['plan_age'] = 999.0
					state['recovery_time'] = max(2.1, self.recovery_seconds + 0.85 + state['phase'] * 0.35)

		if state['recovery_time'] > 0.0:
			# Query 3D grid AND wreck OBB clearance for the widest open escape heading!
			_grid_esc = self._get_3d_grid()
			_esc_yaw = _grid_esc.best_escape_yaw_3d(position, yaw, desired_yaw) if (_grid_esc is not None and hasattr(_grid_esc, 'best_escape_yaw_3d')) else None
			if _esc_yaw is not None and not self._prediction_clear(position, _esc_yaw, max(3.0, abs(float(speed))), velocity, neighbours, max(0.5, float(half_length)), max(0.3, float(half_width))):
				_esc_yaw = None
			if _esc_yaw is None:
				for _e_off in (0.55, -0.55, 0.90, -0.90, 1.25, -1.25, 1.60, -1.60):
					_c_ey = desired_yaw + _e_off
					if self._clear(direction_clear, _c_ey) and self._prediction_clear(position, _c_ey, 3.5, velocity, neighbours, max(0.5, float(half_length)), max(0.3, float(half_width))):
						_esc_yaw = _c_ey
						break
			_des_err = _angle_delta(_esc_yaw if _esc_yaw is not None else desired_yaw, yaw)
			if abs(_des_err) > 0.12:
				direction = 1.0 if _des_err > 0.0 else -1.0
			else:
				direction = 1.0 if ((state['recovery_count'] + int(state['phase'] * 10)) % 2) else -1.0
			recovery_yaw = _esc_yaw if _esc_yaw is not None else (float(yaw) + direction * 0.95)
			# 7-ray 360-degree Rear 3D Fan: evaluate straight-back and angled rear corridors for obstacles, slopes, and neighbours
			_base_rear = float(yaw) + math.pi
			_best_rev_off = None
			_best_rev_sc = 1e18
			_pref_rev_off = -0.45 * direction
			for _r_off in (0.0, 0.32, -0.32, 0.64, -0.64, 0.92, -0.92):
				_cand_rev = _base_rear + _r_off
				_rux = math.sin(_cand_rev)
				_ruz = math.cos(_cand_rev)
				_r_nb_hit = False
				for _rnb in neighbours or ():
					_rnp = self._neighbour_position(_rnb)
					if _rnp is not None:
						_rdx = float(_rnp[0]) - float(position[0])
						_rdz = float(_rnp[2]) - float(position[2])
						_rfwd = _rdx * _rux + _rdz * _ruz
						_rlat = abs(_rdx * _ruz - _rdz * _rux)
						if 0.0 < _rfwd < 6.2 and _rlat < 3.1:
							_r_nb_hit = True
							break
				if _r_nb_hit or not self._clear(direction_clear, _cand_rev):
					continue
				_r_g_cost = 0.0
				if _grid_esc is not None and hasattr(_grid_esc, 'score_heading_3d'):
					try:
						_rg = _grid_esc.score_heading_3d(position, _cand_rev, 7.5, half_width)
					except TypeError:
						_rg = _grid_esc.score_heading_3d(position, _cand_rev, 7.5)
					if _rg is None:
						continue
					_r_g_cost = float(_rg) * 0.55
				_r_sc = _r_g_cost + abs(_r_off - _pref_rev_off) * 0.45 + abs(_r_off) * 0.25
				if _r_sc < _best_rev_sc:
					_best_rev_sc = _r_sc
					_best_rev_off = _r_off
			if _best_rev_off is None:
				# Rear is blocked by a cliff, lake, or wall: NEVER reverse off the cliff! Pivot in place toward open ground!
				return {
					'throttle': 0.35 if self._clear(direction_clear, float(yaw)) else 0.0,
					'turn': direction,
					'target_yaw': recovery_yaw,
					'recovery_mode': 'pivot',
				}
			if abs(_best_rev_off) > 0.20:
				recovery_turn = -1.0 if _best_rev_off > 0.0 else 1.0
			else:
				recovery_turn = -direction
			return {
				'throttle': -0.72 if abs(_best_rev_off) < 0.50 else -0.65,
				'turn': recovery_turn,
				'target_yaw': recovery_yaw,
				'recovery_mode': 'reverse_turn',
			}

		own_half_length = max(0.5, float(half_length))
		own_half_width = max(0.3, float(half_width))
		# Tactical reverse into cover: ONLY when explicitly retreating/pulling into cover
		# while front armour is facing the enemy; cover 1.4m..36.0m with hysteresis (0.60..0.85 rad)
		# so tanks never enter the 1.4m..4.0m forward-steering vs reverse-throttle oscillation zone!
		_was_tac_rev = (state.get('last_mode') == 'tactical_reverse')
		# Only reverse into cover when the cover point is genuinely BEHIND the hull (>= 125..138 deg from nose)
		# and within 16m so tanks NEVER spin their rear ('жопой') toward a forward/side point or reverse 50m!
		_tac_rev_ang = 2.05 if _was_tac_rev else 2.25
		if (allow_tactical_reverse and 1.4 < target_distance <= 36.0 and
				abs(_angle_delta(desired_yaw, yaw)) >= _tac_rev_ang):
			rev_dir = desired_yaw
			if not (self._clear(direction_clear, rev_dir) and
					self._prediction_clear(position, rev_dir, speed, velocity,
						neighbours, own_half_length, own_half_width)):
				# Try straight-back or slight angled reverse before ever turning our back
				rev_dir = None
				for _r_off in (0.0, 0.35, -0.35, 0.65, -0.65, 0.95, -0.95):
					_cand_r = _angle_delta(float(yaw) + math.pi + _r_off, 0.0)
					if abs(_angle_delta(_cand_r, desired_yaw)) <= 1.45 and self._clear(direction_clear, _cand_r):
						rev_dir = _cand_r
						break
			if rev_dir is not None:
				front_yaw = _angle_delta(rev_dir + math.pi, 0.0)
				rev_turn = -steering_turn(front_yaw, yaw)
				return {
					'throttle': -0.85 if target_distance > 5.5 else -0.52,
					'turn': rev_turn,
					'target_yaw': front_yaw,
					'recovery_mode': 'tactical_reverse',
				}
		chosen_yaw = None
		old_yaw = state.get('steering_yaw')
		# Keep a recently selected steering direction for 750 ms (or 1.4s while pivoting) so the tank
		# holds a steady avoidance/turn line instead of reconsidering left/right rays multiple times per second.
		_hold_plan_limit = 0.85 if _was_pivoting else 0.55
		_desired_is_clear = (
			self._failure_penalty(state, desired_yaw) <= 0.0 and
			self._clear(direction_clear, desired_yaw) and
			self._prediction_clear(position, desired_yaw, speed, velocity,
				neighbours, own_half_length, own_half_width))
		if (old_yaw is not None and state['plan_age'] < _hold_plan_limit and
				(abs(_angle_delta(desired_yaw, old_yaw)) <= 0.06 or not _desired_is_clear) and
				abs(_angle_delta(desired_yaw, old_yaw)) < 1.25 and
				self._failure_penalty(state, old_yaw) <= 0.0 and
				self._clear(direction_clear, old_yaw) and
				self._prediction_clear(position, old_yaw, speed, velocity,
					neighbours, own_half_length, own_half_width)):
			chosen_yaw = old_yaw
		_climb_contour = bool(_climb_grade_est >= 0.105 and abs(float(speed)) < 2.65 and state.get('clock', 0.0) > 1.5)
		if chosen_yaw is None:
			chosen_yaw = self._choose_yaw(
				state, desired_yaw, position, speed, velocity, neighbours,
				direction_clear, own_half_length, own_half_width, climb_contour=_climb_contour)
			state['plan_age'] = 0.0
		if chosen_yaw is None:
			# No forward/diagonal ray is usable: consult 3D grid for exact open escape corridor!
			if state['clock'] >= _rec_cd:
				state['stuck_time'] = max(state['stuck_time'], threshold)
				state['recovery_time'] = max(state.get('recovery_time', 0.0), self.recovery_seconds + state['phase'] * 0.28)
			_grid_esc2 = self._get_3d_grid()
			_esc_yaw2 = _grid_esc2.best_escape_yaw_3d(position, yaw, desired_yaw) if (_grid_esc2 is not None and hasattr(_grid_esc2, 'best_escape_yaw_3d')) else None
			_ref_yaw2 = _esc_yaw2 if _esc_yaw2 is not None else desired_yaw
			direction = 1.0 if ((state['recovery_count'] + int(state['phase'] * 10)) % 2) else -1.0
			if abs(_angle_delta(_ref_yaw2, yaw)) > 0.12:
				direction = 1.0 if _angle_delta(_ref_yaw2, yaw) > 0.0 else -1.0
			recovery_yaw = _esc_yaw2 if _esc_yaw2 is not None else (float(yaw) + direction * 0.85)
			_rear_ok = (
				self._clear(direction_clear, float(yaw) + math.pi) or
				self._clear(direction_clear, float(yaw) + math.pi - 0.78) or
				self._clear(direction_clear, float(yaw) + math.pi + 0.78)
			)
			if not _rear_ok:
				return {
					'throttle': 0.0,
					'turn': direction,
					'target_yaw': recovery_yaw,
					'recovery_mode': 'pivot',
				}
			return {
				'throttle': -0.72,
				'turn': -direction,
				'target_yaw': recovery_yaw,
				'recovery_mode': 'reverse_turn',
			}
		state['last_clear_yaw'] = chosen_yaw

		# Retain a selected side for a short time.
		old_yaw = state['steering_yaw']
		if old_yaw is None or abs(_angle_delta(chosen_yaw, old_yaw)) > 0.09:
			state['steering_yaw'] = chosen_yaw
			state['steering_age'] = 0.0

		delta = _angle_delta(chosen_yaw, yaw)
		turn = steering_turn(chosen_yaw, yaw)
		# Pivot in place ONLY when the corridor ahead of our current nose is blocked (or turn > 95 deg at near-zero speed),
		# and strictly bound pivot duration to <= 1.25s on BOTH branches so a tank NEVER spins in place for 10+ seconds!
		_piv_age = float(state.get('pivot_time', 0.0) or 0.0) if _was_pivoting else 0.0
		_can_enter_piv = (not _was_pivoting) and abs(float(speed)) < 2.2 and float(state.get('clock', 0.0) or 0.0) >= float(state.get('pivot_cooldown_until', 0.0) or 0.0)
		_piv_enter = 1.65
		_piv_exit = 0.68
		if ((_can_enter_piv and abs(delta) > _piv_enter) or (_was_pivoting and abs(delta) > _piv_exit)) and _piv_age < 1.25:
			_piv_stalled = (_was_pivoting and step > 0.001 and _yaw_delta_s < (0.06 * step))
			if _piv_stalled:
				state['stuck_time'] += step * 1.35
			if state['stuck_time'] >= threshold and state['clock'] >= _rec_cd:
				state['pivoting'] = False
				state['pivot_time'] = 0.0
				state['pivot_cooldown_until'] = state['clock'] + 3.5
				state['recovery_time'] = max(state.get('recovery_time', 0.0), self.recovery_seconds * 0.85)
				state['failed_yaws'][self._yaw_key(chosen_yaw)] = state['clock'] + 4.5
				state['steering_yaw'] = None
				return {
					'throttle': -0.72,
					'turn': -turn if abs(turn) > 0.1 else (-1.0 if delta > 0.0 else 1.0),
					'target_yaw': chosen_yaw,
					'recovery_mode': 'reverse_turn',
				}
			state['pivoting'] = True
			state['pivot_time'] = _piv_age + step
			return {
				'throttle': 0.0,
				'turn': turn,
				'target_yaw': chosen_yaw,
				'recovery_mode': 'pivot',
			}
		if _was_pivoting and _piv_age >= 1.25:
			state['pivot_cooldown_until'] = state['clock'] + 4.0
		state['pivot_time'] = 0.0
		avoiding = abs(_angle_delta(chosen_yaw, desired_yaw)) > 0.08
		throttle = 1.0 if target_distance >= 7.0 else max(0.38, min(1.0, target_distance / 7.0))
		if abs(delta) > 1.25:
			throttle = min(throttle, 0.25 if abs(float(speed)) < 2.0 else 0.45)
		elif abs(delta) > 1.05 and abs(float(speed)) > 4.5:
			throttle = min(throttle, 0.35)
		# When steering around a close dead tank wreck (<5.5m), creep at 0.48 throttle so the hull doesn't bump and bounce!
		if neighbours:
			for _nb_th in neighbours:
				if isinstance(_nb_th, dict) and _nb_th.get('is_wreck', False):
					_np_th = _nb_th.get('position')
					if _np_th is not None:
						if ((float(_np_th[0]) - float(position[0])) ** 2 + (float(_np_th[2]) - float(position[2])) ** 2) <= 30.25:
							throttle = min(throttle, 0.48)
							break
		# Narrow ridge / water edge speed governor: when position or next 6m is near fatal drop/water,
		# throttle down so high-speed scouts never carry fatal momentum off cliffs!
		grid_3d = self._get_3d_grid()
		if grid_3d is not None and hasattr(grid_3d, 'baked_hazard_near'):
			if grid_3d.baked_hazard_near(position, 1):
				throttle = min(throttle, 0.22 if abs(float(speed)) > 2.5 else 0.35)
				if abs(float(speed)) > 3.0:
					throttle = -0.25
			elif grid_3d.baked_hazard_near(position, 2) and abs(float(speed)) > 4.5:
				throttle = min(throttle, 0.38)
		return {
			'throttle': throttle,
			'turn': turn,
			'target_yaw': chosen_yaw,
			'recovery_mode': 'avoid' if avoiding else 'drive',
			'is_uphill': bool(_is_uphill),
		}
