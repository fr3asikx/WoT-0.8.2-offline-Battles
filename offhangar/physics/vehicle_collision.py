# -*- coding: utf-8 -*-
"""Small, engine-independent rigid-body helpers for vehicle collisions.

The retail 0.8.2 physics body is sized from the chassis hit tester, not the
hull hit tester.  Keep this module free of BigWorld imports so the geometry and
mass response can be tested with normal Python.
"""

import math


DEFAULT_SHAPE = (1.5, 3.5, -0.8, 2.0)
_SHAPE_CACHE = {}
SPATIAL_CELL_SIZE = 24.0
TERRAIN_PROFILE_MAXIMUM_GRADIENT = 1.28


def drivable_rising_profile(heights, segment_length,
		maximum_gradient=TERRAIN_PROFILE_MAXIMUM_GRADIENT,
		minimum_rise=0.15, allow_flat=False):
	"""Return whether ground samples describe a gradual terrain profile.

	By default a flat profile is deliberately not a hill: generic callers use
	this result to distinguish climbable terrain from a solid obstacle
	intersecting a horizontal hull sweep. A caller that has separately proved the
	whole segment contains terrain and no non-terrain geometry may set
	``allow_flat``. Smooth descents and rounded crests are ground too. Rejecting
	an abrupt step in either direction keeps rocks, walls and wagon decks in the
	solid-collision path.
	"""
	try:
		values = [float(value) for value in heights]
		if len(values) < 2:
			return False
		segment = max(0.001, float(segment_length))
		if (not allow_flat and
				max(values) - min(values) <=
				max(0.0, float(minimum_rise))):
			return False
		maximum_step = segment * max(0.0, float(maximum_gradient))
		for index in range(1, len(values)):
			if abs(values[index] - values[index - 1]) > maximum_step:
				return False
		return True
	except Exception:
		return False


def build_spatial_index(bodies, cell_size=SPATIAL_CELL_SIZE):
	"""Bucket body ids by x/z so callers avoid a full all-pairs scan.

	``bodies`` is the per-frame immutable snapshot used by both local steering
	and collision broad phase. A 24 m cell is wider than any 0.8.2 tank body and
	also matches the driver's prediction radius, so the surrounding nine cells
	are a complete candidate set for both consumers.
	"""
	size = max(1.0, float(cell_size))
	buckets = {}
	for body_id, body in (bodies or {}).items():
		try:
			position = body.get('position') if isinstance(body, dict) else body
			x = _coord(position, 0)
			z = _coord(position, 2)
			key = (int(math.floor(x / size)), int(math.floor(z / size)))
			buckets.setdefault(key, []).append(body_id)
		except Exception:
			continue
	return size, buckets


def nearby_ids(index, x, z):
	"""Return ids in the query cell and its eight neighbours."""
	if not index:
		return ()
	try:
		size, buckets = index
		cell_x = int(math.floor(float(x) / float(size)))
		cell_z = int(math.floor(float(z) / float(size)))
	except Exception:
		return ()
	result = []
	for offset_z in (-1, 0, 1):
		for offset_x in (-1, 0, 1):
			result.extend(buckets.get(
				(cell_x + offset_x, cell_z + offset_z), ()))
	return tuple(result)


def unique_candidate_map(index, bodies, solver_ids):
	"""Assign each nearby unordered body pair to one local solver.

	The collision resolver still performs the exact vertical, radius and OBB
	tests against live poses.  This helper only removes the duplicate broad-phase
	enumeration that otherwise happens when both vehicles visit the same pair in
	their per-frame loops.  A pair with one non-local body is assigned to its local
	solver; local/local pairs are assigned to the lower stable entity id.
	"""
	if not index or not bodies:
		return {}
	local = set(solver_ids or ())
	if not local:
		return {}
	result = dict((body_id, []) for body_id in local if body_id in bodies)
	try:
		unused_size, buckets = index
		bucket_keys = sorted(buckets.keys())
	except Exception:
		return {}

	def _assign(first_id, second_id):
		if (first_id == second_id or first_id not in bodies or
				second_id not in bodies):
			return
		first_local = first_id in local
		second_local = second_id in local
		if first_local and second_local:
			try:
				solver_id = min(first_id, second_id)
			except Exception:
				solver_id = first_id if repr(first_id) < repr(second_id) else second_id
			candidate_id = second_id if solver_id == first_id else first_id
		elif first_local:
			solver_id = first_id
			candidate_id = second_id
		elif second_local:
			solver_id = second_id
			candidate_id = first_id
		else:
			return
		result.setdefault(solver_id, []).append(candidate_id)

	# Same-cell combinations plus four forward neighbouring cells cover the
	# complete 3x3 query square exactly once, without one nearby_ids allocation
	# per vehicle and without a per-frame seen-pair set.
	forward_neighbours = ((0, 1), (1, -1), (1, 0), (1, 1))
	for cell_x, cell_z in bucket_keys:
		current = sorted(buckets.get((cell_x, cell_z), ()))
		for first_index in range(len(current)):
			for second_index in range(first_index + 1, len(current)):
				_assign(current[first_index], current[second_index])
		for offset_x, offset_z in forward_neighbours:
			other = sorted(buckets.get(
				(cell_x + offset_x, cell_z + offset_z), ()))
			for first_id in current:
				for second_id in other:
					_assign(first_id, second_id)
	return dict((body_id, tuple(candidate_ids))
	            for body_id, candidate_ids in result.items())


def _coord(value, index, default=0.0):
	try:
		return float(value[index])
	except Exception:
		try:
			return float((value.x, value.y, value.z)[index])
		except Exception:
			return float(default)


def _bbox(container):
	try:
		return container['hitTester'].bbox
	except Exception:
		return None


def chassis_shape(type_descriptor):
	"""Return (half width, half length, lower y, upper y) for a tank body."""
	if type_descriptor is None:
		return DEFAULT_SHAPE
	cache_key = id(type_descriptor)
	cached = _SHAPE_CACHE.get(cache_key)
	if cached is not None and cached[0] is type_descriptor:
		return cached[1]
	try:
		chassis_box = _bbox(type_descriptor.chassis)
		if chassis_box is None:
			return DEFAULT_SHAPE
		minimum = chassis_box[0]
		maximum = chassis_box[1]
		half_width = max(abs(_coord(minimum, 0)), abs(_coord(maximum, 0)), 0.8)
		half_length = max(abs(_coord(minimum, 2)), abs(_coord(maximum, 2)), 1.0)
		lower_y = _coord(minimum, 1, DEFAULT_SHAPE[2])
		upper_y = _coord(maximum, 1, DEFAULT_SHAPE[3])

		# Retail physics_shared extends the chassis body vertically to contain
		# the mounted hull.  This matters for stacked/falling vehicles, but x/z
		# remain the chassis dimensions.
		try:
			hull_box = _bbox(type_descriptor.hull)
			hull_position = type_descriptor.chassis.get('hullPosition')
			if hull_box is not None and hull_position is not None:
				upper_y = max(upper_y,
				              _coord(hull_position, 1) + _coord(hull_box[1], 1))
		except Exception:
			pass
		shape = (half_width, half_length, lower_y, upper_y)
		# Hold the descriptor with the cached value so CPython cannot reuse its
		# id for a different vehicle descriptor later in a long-running client.
		_SHAPE_CACHE[cache_key] = (type_descriptor, shape)
		return shape
	except Exception:
		return DEFAULT_SHAPE


def vertical_overlap(y_a, shape_a, y_b, shape_b, slop=0.02):
	if y_a is None or y_b is None:
		return True
	a_low = y_a + shape_a[2]
	a_high = y_a + shape_a[3]
	b_low = y_b + shape_b[2]
	b_high = y_b + shape_b[3]
	return min(a_high, b_high) - max(a_low, b_low) > slop


def support_rise_is_obstacle(body_y, support_y, maximum_climb, slop=0.02,
		maximum_step=0.85):
	"""Whether a newly sampled support is a step, not drivable ground.

	A vertical support ray can hit the deck of a rail wagon, a low roof or the
	top of a large prop after the hull has moved partly inside it.  Snapping the
	chassis origin to that height effectively teleports the tank onto the object.
	Only rises that fit the distance this physics tick can actually climb may be
	used as ground support; larger rises must be handled as horizontal obstacles.
	The correction distance grows with frame time, but a low frame rate must not
	turn a vertical wagon side into a drivable step, so it also has a hard cap.
	"""
	if body_y is None or support_y is None:
		return False
	try:
		rise = float(support_y) - float(body_y)
		limit = min(max(0.0, float(maximum_climb)),
			max(0.0, float(maximum_step))) + max(0.0, float(slop))
		return rise > limit
	except Exception:
		return False


def _axes(yaw):
	# Local chassis x (right) and z (forward) in world x/z coordinates.
	s = math.sin(yaw)
	c = math.cos(yaw)
	return ((c, -s), (s, c))


def obb_contact(x_a, z_a, yaw_a, shape_a,
	            x_b, z_b, yaw_b, shape_b):
	"""Return (normal x, normal z, penetration), normal pointing B -> A."""
	a_axes = _axes(yaw_a)
	b_axes = _axes(yaw_b)
	dx = x_a - x_b
	dz = z_a - z_b
	best_overlap = None
	best_x = 0.0
	best_z = 0.0

	for axis in (a_axes[0], a_axes[1], b_axes[0], b_axes[1]):
		ax = axis[0]
		az = axis[1]
		ra = (shape_a[0] * abs(ax * a_axes[0][0] + az * a_axes[0][1]) +
		      shape_a[1] * abs(ax * a_axes[1][0] + az * a_axes[1][1]))
		rb = (shape_b[0] * abs(ax * b_axes[0][0] + az * b_axes[0][1]) +
		      shape_b[1] * abs(ax * b_axes[1][0] + az * b_axes[1][1]))
		distance = abs(dx * ax + dz * az)
		overlap = ra + rb - distance
		if overlap <= 0.0:
			return None
		if best_overlap is None or overlap < best_overlap:
			if dx * ax + dz * az < 0.0:
				ax = -ax
				az = -az
			best_overlap = overlap
			best_x = ax
			best_z = az

	return (best_x, best_z, best_overlap)


def pair_response(contact, inv_mass_a, inv_mass_b,
	              velocity_a, velocity_b, slop=0.01, percent=0.95):
	"""Return corrections and velocity deltas for both bodies.

	The impulse is perfectly inelastic along the contact normal (e=0), matching
	tank hulls pushing rather than bouncing.  Tangential velocity is untouched.
	"""
	zero = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
	if contact is None:
		return zero
	inverse_sum = inv_mass_a + inv_mass_b
	if inverse_sum <= 0.0:
		return zero
	nx, nz, penetration = contact
	correction = max(penetration - slop, 0.0) * percent / inverse_sum
	acx = nx * correction * inv_mass_a
	acz = nz * correction * inv_mass_a
	bcx = -nx * correction * inv_mass_b
	bcz = -nz * correction * inv_mass_b

	relative_normal = ((velocity_a[0] - velocity_b[0]) * nx +
	                   (velocity_a[1] - velocity_b[1]) * nz)
	advx = 0.0
	advz = 0.0
	bdvx = 0.0
	bdvz = 0.0
	if relative_normal < 0.0:
		impulse = -relative_normal / inverse_sum
		advx = nx * impulse * inv_mass_a
		advz = nz * impulse * inv_mass_a
		bdvx = -nx * impulse * inv_mass_b
		bdvz = -nz * impulse * inv_mass_b
	return (acx, acz, advx, advz, bcx, bcz, bdvx, bdvz)


def boundary_wall_response(x, z, vx, vz, bounds, margin=2.0, restitution=0.0):
	"""v4.66 доп.15 -- physical map-boundary wall (replaces void-fall).

	Treat the map edge as four static, infinite-height axis-aligned walls
	sitting `margin` metres inside `bounds` = (min_x, min_z, max_x, max_z)
	(the exact ArenaType.boundingBox rectangle already used elsewhere for
	the AI's out-of-bounds checks and, until now, only for a teleport-style
	position clamp). Unlike a pure position clamp, this also kills (or with
	`restitution` > 0, mildly reflects) the outward velocity component, so a
	tank actually FEELS like it hit something solid instead of silently
	being teleported back onto the line while still trying to drive through
	it (which reads as the hull sliding/skating along the edge).

	Returns (new_x, new_z, new_vx, new_vz). Safe to call every physics tick
	unconditionally: it is a no-op while comfortably inside the rectangle.
	"""
	if bounds is None:
		return x, z, vx, vz
	try:
		bx0 = float(bounds[0]) + margin
		bz0 = float(bounds[1]) + margin
		bx1 = float(bounds[2]) - margin
		bz1 = float(bounds[3]) - margin
	except Exception:
		return x, z, vx, vz
	nx = x
	nz = z
	nvx = vx
	nvz = vz
	if x < bx0:
		nx = bx0
		if nvx < 0.0:
			nvx = -nvx * restitution
	elif x > bx1:
		nx = bx1
		if nvx > 0.0:
			nvx = -nvx * restitution
	if z < bz0:
		nz = bz0
		if nvz < 0.0:
			nvz = -nvz * restitution
	elif z > bz1:
		nz = bz1
		if nvz > 0.0:
			nvz = -nvz * restitution
	return nx, nz, nvx, nvz
