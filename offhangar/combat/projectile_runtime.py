# -*- coding: utf-8 -*-
"""Pure helpers for the client-authoritative in-flight projectile runtime.

Keep this module independent of BigWorld so its swept-motion maths can be
tested under the repository's normal Python 3 test runner while the game still
loads the same source under Python 2.6.
"""

PROJECTILE_CALLBACK_SECONDS = 0.01
PROJECTILE_MAX_SUBSTEP_SECONDS = 0.025
PROJECTILE_BROADPHASE_RADIUS = 15.0


def trajectory_position(start, velocity, gravity, elapsed):
	"""Return ``r0 + v0*t + 1/2*g*t^2`` as a plain three-tuple."""
	t = max(0.0, float(elapsed or 0.0))
	half_t_sq = 0.5 * t * t
	return (
		float(start[0]) + float(velocity[0]) * t + float(gravity[0]) * half_t_sq,
		float(start[1]) + float(velocity[1]) * t + float(gravity[1]) * half_t_sq,
		float(start[2]) + float(velocity[2]) * t + float(gravity[2]) * half_t_sq,
	)


def lerp3(first, second, fraction):
	f = max(0.0, min(1.0, float(fraction or 0.0)))
	return (
		float(first[0]) + (float(second[0]) - float(first[0])) * f,
		float(first[1]) + (float(second[1]) - float(first[1])) * f,
		float(first[2]) + (float(second[2]) - float(first[2])) * f,
	)


def compensate_segment_for_moving_target(projectile_start, projectile_end,
		target_previous, target_current, interval_start=0.0, interval_end=1.0):
	"""Express a projectile chord in the target's current collision frame.

	``collideSegment`` only sees the vehicle's current matrix.  Moving both chord
	endpoints by the target displacement that remains after each endpoint turns
	the query into a relative-motion sweep.  A fast tank can therefore move out
	of an unaimed shell, or into a correctly led one, between rendered frames.
	"""
	previous_at_start = lerp3(target_previous, target_current, interval_start)
	previous_at_end = lerp3(target_previous, target_current, interval_end)
	return (
		(
			float(projectile_start[0]) + float(target_current[0]) - previous_at_start[0],
			float(projectile_start[1]) + float(target_current[1]) - previous_at_start[1],
			float(projectile_start[2]) + float(target_current[2]) - previous_at_start[2],
		),
		(
			float(projectile_end[0]) + float(target_current[0]) - previous_at_end[0],
			float(projectile_end[1]) + float(target_current[1]) - previous_at_end[1],
			float(projectile_end[2]) + float(target_current[2]) - previous_at_end[2],
		),
	)


def point_segment_distance_sq(point, start, end):
	"""Squared 3-D distance used before invoking an expensive hit tester."""
	sx = float(end[0]) - float(start[0])
	sy = float(end[1]) - float(start[1])
	sz = float(end[2]) - float(start[2])
	px = float(point[0]) - float(start[0])
	py = float(point[1]) - float(start[1])
	pz = float(point[2]) - float(start[2])
	denom = sx * sx + sy * sy + sz * sz
	if denom <= 1e-12:
		return px * px + py * py + pz * pz
	fraction = (px * sx + py * sy + pz * sz) / denom
	fraction = max(0.0, min(1.0, fraction))
	dx = px - sx * fraction
	dy = py - sy * fraction
	dz = pz - sz * fraction
	return dx * dx + dy * dy + dz * dz


def substep_boundaries(start_time, end_time, maximum_step=PROJECTILE_MAX_SUBSTEP_SECONDS):
	"""Yield bounded time chords without dropping time after a slow frame."""
	start_time = max(0.0, float(start_time or 0.0))
	end_time = max(start_time, float(end_time or 0.0))
	maximum_step = max(0.001, float(maximum_step or PROJECTILE_MAX_SUBSTEP_SECONDS))
	cursor = start_time
	while cursor + 1e-9 < end_time:
		next_time = min(end_time, cursor + maximum_step)
		yield cursor, next_time
		cursor = next_time
