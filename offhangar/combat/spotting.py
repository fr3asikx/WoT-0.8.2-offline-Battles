# -*- coding: utf-8 -*-
"""Deterministic World of Tanks 0.8.2 spotting calculations (3D Spherical TTX Edition).

This module deliberately has no BigWorld imports.  The battle adapter supplies
vehicle/resource data while the formula remains testable on modern Python and
loadable by the embedded Python 2.6 runtime.
"""


# Canonical World of Tanks 50 metre 3D proximity spotting ("рентген", unconditional 360-degree spherical detection)
PROXIMITY_SPOT_DISTANCE = 50.0
# Canonical World of Tanks 445 metre maximum spotting sphere radius (excess view range pierces camo inside 445m)
MAX_SPOT_DISTANCE = 445.0
SPOT_MEMORY_SECONDS = 10.0
SIXTH_SENSE_DELAY_SECONDS = 3.0
STILL_DEVICE_DELAY_SECONDS = 3.0
MOVING_SPEED_EPSILON = 0.5
SHOT_CAMOUFLAGE_SECONDS = 4.5


def clamp(value, minimum, maximum):
	return max(float(minimum), min(float(maximum), float(value)))


def qualification_factor(role_level):
	"""Normalize the old 0.5 + 0.00375 * level curve to 100% crew."""
	return (0.5 + 0.00375 * clamp(role_level, 0.0, 150.0)) / 0.875


def effective_view_range(base_range, commander_level=100.0,
		vision_factor=1.0, recon_level=0.0, situational_level=0.0,
		still_device_factor=1.0, still_device_active=False):
	"""Return uncapped TTX view range; excess range over 445m counters target camouflage."""
	result = max(PROXIMITY_SPOT_DISTANCE, float(base_range or 0.0))
	result *= qualification_factor(commander_level)
	result *= max(0.0, float(vision_factor or 0.0))
	result *= 1.0 + 0.0002 * clamp(recon_level, 0.0, 100.0)
	result *= 1.0 + 0.0003 * clamp(situational_level, 0.0, 100.0)
	if still_device_active:
		result *= max(1.0, float(still_device_factor or 1.0))
	return max(PROXIMITY_SPOT_DISTANCE, result)


def crew_camouflage_factor(skill_level):
	"""Scale a raw invisibility coefficient by the common Camouflage skill.

	The stored coefficient represents the fully trained value.  A crew without
	the skill receives 4/7 of it; training supplies the remaining 3/7 linearly.
	This is also the convention used by the imported 0.9.17.1 data snapshot.
	"""
	return 4.0 / 7.0 + 3.0 / 7.0 * clamp(skill_level, 0.0, 100.0) / 100.0


def effective_camouflage(moving_base, still_base, moving=False,
		crew_skill_level=0.0, turret_factor=1.0, paint_factor=1.0,
		camouflage_net_factor=1.0, camouflage_net_active=False,
		shot_factor=1.0, fired_recently=False, foliage_bonus=0.0):
	"""Return the target's concealment coefficient in the 0..1 range.

	0.8.2 camouflage paint and the camouflage net were multiplicative.  Foliage
	is additive and capped separately so stacked bushes cannot make a vehicle
	undetectable outside the unconditional 50 metre proximity sphere.
	"""
	base = float(moving_base if moving else still_base)
	result = max(0.0, base)
	result *= crew_camouflage_factor(crew_skill_level)
	result *= max(0.0, float(turret_factor or 0.0))
	result *= max(0.0, float(paint_factor or 0.0))
	if camouflage_net_active and not moving:
		result *= max(1.0, float(camouflage_net_factor or 1.0))
	if fired_recently:
		result *= clamp(shot_factor, 0.0, 1.0)
	result += clamp(foliage_bonus, 0.0, 0.48)
	return clamp(result, 0.0, 0.78)


def detection_distance(view_range, camouflage):
	"""Apply the historical 50 m floor and 445 m spotting sphere ceiling."""
	view_range = max(PROXIMITY_SPOT_DISTANCE, float(view_range or 0.0))
	camouflage = clamp(camouflage, 0.0, 0.78)
	distance = view_range - (view_range - PROXIMITY_SPOT_DISTANCE) * camouflage
	return clamp(distance, PROXIMITY_SPOT_DISTANCE, MAX_SPOT_DISTANCE)


def foliage_visibility_bound(distance_sq, view_range, camouflage,
		max_foliage_bonus=0.48):
	"""Classify a pair only when every possible foliage result agrees.

	``True`` means the target remains inside spotting range even at the maximum
	foliage bonus, ``False`` means it is outside range with no foliage, and
	``None`` preserves the exact pair-specific foliage query for the interval in
	between.  This is a mathematical short-circuit, not a visibility cache.
	"""
	distance_sq = max(0.0, float(distance_sq or 0.0))
	clear_range = detection_distance(view_range, camouflage)
	if distance_sq > clear_range * clear_range:
		return False
	worst_camouflage = clamp(
		float(camouflage or 0.0) + max(0.0, float(max_foliage_bonus or 0.0)),
		0.0, 0.95)
	worst_range = detection_distance(view_range, worst_camouflage)
	if distance_sq <= worst_range * worst_range:
		return True
	return None


def is_detected(distance, view_range, camouflage, has_line_of_sight=True):
	distance = max(0.0, float(distance or 0.0))
	if distance <= PROXIMITY_SPOT_DISTANCE:
		return True
	return bool(has_line_of_sight and
	            distance <= detection_distance(view_range, camouflage))
