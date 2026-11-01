# -*- coding: utf-8 -*-
''' (bot_ai_v2): what the bot's OWN machine can and cannot do.

Why this module exists
----------------------
The tactical planner in `bot_ai.py` never sees the geometry of the vehicle it
commands: `grep -cE "pitchLimits|turretYawLimits|traverseSpeed|rotationSpeed"
bot_ai.py` = **0** (measured 09.10.2026 on md5 4d33fa530579f7aed39c91ff18a6c6ec).
Its entry point `BattleDirector.order_for(bot_id, position, hull_yaw, health,
max_health, now, reload_time, shoot_timer)` (`bot_ai.py:3440`) receives no gun,
turret or suspension data at all, so a bot whose nose points at the sky on a
slope is told `fire_allowed=True` and shoots over the target - the symptom the
whole aiming thread started from (`_pi/outgoing/ТЕХЗАДАНИЕ_bot_ai_v2.md` §2 п.1).

The client already resolves the real yaw-dependent elevation limits in
`_offh_ai_artillery_pitch_limits(vehicle, target_yaw)`
(`offline_battle.py:9955-9971` on the current tree, md5
ed133703837f83ba32eb43e53328ba96; the number 8589 quoted in the spec and in
PI_STATE §0.101 is stale).  That helper needs a live `vehicle`, so it cannot be
reused by the LAN-server / worker branch.  This module does the same job from ONE
typeDescriptor, caches the result per vehicle name and imports no engine module
at load time - it runs in the client, in the worker and in headless tests.

Descriptor field names are read off the stock parser, not off memory:
  gun['pitchLimits']   {'basic': (min,max) rad, 'absolute': widest union,
                        optional 'front'/'back': (min,max,angle) rad,
                        optional 'transition': rad}
                       dev_ref/stock_082/common/items/vehicles.py:2048-2059,
                       :2232-2261 (_readGunPitchExtraLimits)
  gun['rotationSpeed'] rad/s, elevation speed           vehicles.py:2000
  gun['reloadTime']    seconds                          vehicles.py:2002
  gun['aimingTime']    seconds                          vehicles.py:2003
  gun['shotDispersionAngle'] rad = shotDispersionRadius/100   vehicles.py:2061
  gun['shotDispersionFactors']                          vehicles.py:2062
  gun['shots'][i]      {'shell','defaultPortion','piercingPower','speed',
                        'gravity','maxDistance'}        vehicles.py:2356-2361
  turret['rotationSpeed'] rad/s, traverse speed         vehicles.py:1931
  turret['yawLimits']  (min,max) rad, None when the arc is full
                                                        vehicles.py:1976
  turret['circularVisionRadius'] metres                 vehicles.py:1953-1954
  hull['turretPositions'][0] + chassis['hullPosition'] + turret['gunPosition']
                       = muzzle offset, the same chain the client uses
                       (`offline_battle.py:16009-16010`)
  physics              {'weight','enginePower','speedLimits':(fwd,bwd) m/s,
                        'terrainResistance','rotationSpeedLimit'}
                                                        vehicles.py:848-863, :937-939

Pitch convention is BigWorld's: NEGATIVE pitch is barrel UP (elevation),
POSITIVE is barrel DOWN (depression) - see the `barrel_direction` docstring
(`bot_ai_driver.py:425-431`) and the live defaults
`_gun_min_pitch = -0.35  # ~-20 deg (ELEVATION - UP)` /
`_gun_max_pitch =  0.15  # ~+8.6 deg (DEPRESSION - DOWN)`
(`offline_battle.py:16081-16082`).

Sector blending itself is native: `gun_rotation_shared.calcPitchLimitsFromDesc`
calls `BigWorld.wg_calcGunPitchLimits(turretYaw, basic, front, back, transition)`
(dev_ref/stock_082/common/gun_rotation_shared.py:10-26;
DEFAULT_GUN_PITCH_LIMITS_TRANSITION = 0.4,
dev_ref/stock_082/common/constants.py:37).  Outside the client that call does not
exist, so `gun_pitch_limits()` INTERSECTS the known sectors instead: the result
is tighter than the native blend, never wider, which keeps the fire gate
conservative offline.  (`dev_ref/offline_0922/gun_pitch_limits.py` mirrors the
0.9.x curve format `minPitch`/`maxPitch` and does NOT apply to 0.8.2.)

Nothing here logs, spawns, aims or shoots: the module only answers "can this
machine physically do that, and how long does it take".
'''

import math

# Fallbacks are the ones the client already uses, so a descriptor that cannot be
# parsed degrades to today's behaviour instead of to zero.
PITCH_FALLBACK = (-0.35, 0.15)      # offline_battle.py:16081-16082
YAW_FALLBACK = (-math.pi, math.pi)  # bot_ai_driver.gun_yaw_limits (:373-393)
TURRET_TRAVERSE_FALLBACK = 1.5      # rad/s, offline_battle.py:16076
GUN_TRAVERSE_FALLBACK = 0.75        # rad/s, offline_battle.py:16079
RELOAD_FALLBACK = 4.0               # s, bot_ai.build_vehicle_profile (:1013)
AIMING_FALLBACK = 2.5               # s, typical 0.8.2 gun
DISPERSION_FALLBACK = 0.4           # rad/100 m (shotDispersionRadius 40 cm)
VISION_FALLBACK = 350.0             # m

# Fire-gate margins, radians.  With a known hull pose the required pitch is exact
# to within the frame math, so a small margin only absorbs solver tolerance.
# Without a pose the gate has to assume a level hull, and the log shows slopes far
# steeper than that (`1003 pitch=0.309827 roll=-0.101331` = 18 deg / -6 deg,
# PI_STATE.md:3986), so the margin must cover the unknown tilt.
MARGIN_POSE_KNOWN = 0.065
MARGIN_LEVEL_HULL = 0.12

# Cache guard: one long session must not grow this without bound.
CACHE_LIMIT = 64

_CACHE = {}


# ---------------------------------------------------------------------------
# Descriptor access.  Deliberately local copies of the tiny readers in
# bot_ai.py (:264/:684/:693) and bot_ai_driver.py (:359): importing those
# modules would drag bot_ai_maps/bot_ai_navigation into the worker, and the
# point of this file is to stay process-independent (spec §3.6).
# ---------------------------------------------------------------------------
def _get(component, name, default=None):
	'''Read `name` from a dict-like OR attribute-like descriptor component.'''
	if component is None:
		return default
	try:
		if hasattr(component, 'get'):
			return component.get(name, default)
	except Exception:
		pass
	try:
		return getattr(component, name, default)
	except Exception:
		return default


def _num(value, default=0.0):
	'''float() that never raises and never returns NaN/inf.

	`default=None` is allowed and comes back as None, so callers can tell "not a
	number" from "zero" - the `or 0.0` trap that ate a legitimate zero in d97
	(РЕЦЕНЗИЯ_d97.md, defect 3) does not apply here.'''
	try:
		result = float(value)
	except (TypeError, ValueError):
		return None if default is None else float(default)
	if result != result or result in (float('inf'), float('-inf')):
		return None if default is None else float(default)
	return result


def _pair(value, default=None):
	'''(float, float) out of a Vector2/tuple/list, else `default`.'''
	if value is None:
		return default
	try:
		first = _num(value[0], None)
		second = _num(value[1], None)
	except (TypeError, IndexError, KeyError):
		return default
	if first is None or second is None:
		return default
	return (first, second)


def _component_y(value, default=0.0):
	'''Y of a Math.Vector3 (client) or of a plain sequence (headless).'''
	if value is None:
		return float(default)
	coord = _get(value, 'y', None)
	if coord is None:
		try:
			coord = value[1]
		except (TypeError, IndexError, KeyError):
			coord = None
	return _num(coord, default)


def _ordered(pair, default):
	'''(min, max) even if a descriptor stored them the other way round.'''
	if pair is None:
		return (default[0], default[1])
	first, second = pair[0], pair[1]
	if first > second:
		first, second = second, first
	return (first, second)


_DRIVER = [None, False]


def _driver_module():
	'''bot_ai_driver, imported once.  It is engine-free (only `math`), which is
	why its hull-frame math is reused here instead of rewritten.'''
	if _DRIVER[1]:
		return _DRIVER[0]
	_DRIVER[1] = True
	try:
		from gui.mods.offhangar import bot_ai_driver as driver
		_DRIVER[0] = driver
	except ImportError:
		try:
			import bot_ai_driver as driver
			_DRIVER[0] = driver
		except ImportError:
			_DRIVER[0] = None
	except Exception:
		_DRIVER[0] = None
	return _DRIVER[0]


def angle_delta(target, current):
	'''Signed shortest turn from `current` to `target`, radians in [-pi, pi].'''
	try:
		driver = _driver_module()
		if driver is not None:
			return driver._angle_delta(target, current)
	except Exception:
		pass
	delta = (_num(target, 0.0) - _num(current, 0.0)) % (2.0 * math.pi)
	if delta > math.pi:
		delta -= 2.0 * math.pi
	return delta


# ---------------------------------------------------------------------------
# Gun elevation limits
# ---------------------------------------------------------------------------
def pitch_limit_sectors(pitch_desc):
	'''(basic, absolute, front, back, transition, yaw_dependent) out of a
	`gun['pitchLimits']` mapping.  In 0.8.2 every angle is ALREADY in radians
	(vehicles.py:2048-2059, :2232-2261).  Missing entries come back None.'''
	if pitch_desc is None or not hasattr(pitch_desc, 'get'):
		flat = _pair(pitch_desc, None)
		if flat is None:
			return (None, None, None, None, None, False)
		return (flat, flat, None, None, None, False)
	basic = _pair(_get(pitch_desc, 'basic', None), None)
	absolute = _pair(_get(pitch_desc, 'absolute', None), None)
	if basic is None:
		basic = absolute
	if absolute is None:
		absolute = basic
	front_pair = _pair(_get(pitch_desc, 'front', None), None)
	back_pair = _pair(_get(pitch_desc, 'back', None), None)
	transition = _get(pitch_desc, 'transition', None)
	transition = _num(transition, 0.4) if transition is not None else None
	yaw_dependent = front_pair is not None or back_pair is not None
	return (basic, absolute, front_pair, back_pair, transition, yaw_dependent)


def gun_pitch_limits(pitch_desc, turret_yaw=0.0):
	'''(minimum, maximum, source): elevation window in radians at one turret yaw.

	`minimum` is how high the barrel can go (negative = up), `maximum` how low
	(positive = down) - BigWorld's convention and the same pair
	`_offh_ai_artillery_pitch_limits` returns (`offline_battle.py:9955-9971`).

	`source` is one of:
	  'stock'     - resolved by gun_rotation_shared / native sector blending, the
	                authoritative answer inside the client;
	  'intersect' - offline: intersection of basic/front/back, tighter than the
	                native blend, never wider;
	  'basic'     - the descriptor has no yaw-dependent sectors;
	  'fallback'  - descriptor unreadable, PITCH_FALLBACK used.
	'''
	basic, absolute, front, back, transition, yaw_dependent = (
		pitch_limit_sectors(pitch_desc))
	if basic is None:
		return (PITCH_FALLBACK[0], PITCH_FALLBACK[1], 'fallback')
	if not yaw_dependent:
		return (basic[0], basic[1], 'basic')
	# Inside the client: ask the engine, exactly like the live code does.
	try:
		from gun_rotation_shared import calcPitchLimitsFromDesc
		try:
			resolved = _pair(calcPitchLimitsFromDesc(_num(turret_yaw, 0.0),
			                                         pitch_desc), None)
			if resolved is not None:
				window = _ordered(resolved, basic)
				return (window[0], window[1], 'stock')
		except Exception:
			pass
	except ImportError:
		pass
	# Offline: intersect the sectors that exist.  A shot the native resolver
	# would forbid stays forbidden; one it would allow may be refused here.
	lowest = basic[0]
	highest = basic[1]
	for sector in (front, back):
		if sector is None:
			continue
		lowest = max(lowest, sector[0])
		highest = min(highest, sector[1])
	if lowest > highest:
		# The sectors do not overlap at this yaw: take the narrowest one rather
		# than invent a window, so the gate refuses.
		narrow = front if front is not None else back
		lowest, highest = narrow[0], narrow[1]
		if lowest > highest:
			lowest, highest = _ordered((lowest, highest), basic)
	return (lowest, highest, 'intersect')


def gun_yaw_limits(descriptor):
	'''(minimum, maximum, limited): turret traverse in radians, hull-relative.

	Delegates to the shipped driver so both directors read it the same way
	(`bot_ai_driver.gun_yaw_limits` :373-393 - gun['turretYawLimits'], else
	turret['yawLimits'], degrees converted when |value| > pi + 0.1).'''
	try:
		driver = _driver_module()
		if driver is not None:
			return driver.gun_yaw_limits(descriptor)
	except Exception:
		pass
	limits = _get(_get(descriptor, 'gun', None), 'turretYawLimits', None)
	if limits is None:
		limits = _get(_get(descriptor, 'turret', None), 'yawLimits', None)
	window = _pair(limits, None)
	if window is None:
		return (YAW_FALLBACK[0], YAW_FALLBACK[1], False)
	minimum, maximum = window
	if abs(minimum) > math.pi + 0.1 or abs(maximum) > math.pi + 0.1:
		minimum = math.radians(minimum)
		maximum = math.radians(maximum)
	limited = not (minimum <= -math.pi + 0.1 and maximum >= math.pi - 0.1)
	return (minimum, maximum, limited)


# ---------------------------------------------------------------------------
# Ammunition
# ---------------------------------------------------------------------------
def shell_capabilities(descriptor):
	'''One entry per gun['shots'] index, keeping BOTH penetration values the
	descriptor carries: `piercingPower` is a Vector2 (pen at 100 m, pen at
	`maxDistance`) - vehicles.py:2356-2361.  The v1 profile keeps only element
	[0] (`bot_ai._shell_penetration` :823-853), which is enough to rank ammo but
	cannot answer "do I still pen at 400 m".'''
	gun = _get(descriptor, 'gun', None)
	shots = _get(gun, 'shots', ()) or ()
	result = []
	try:
		iterator = list(enumerate(shots))
	except Exception:
		iterator = []
	for index, shot in iterator:
		shell = _get(shot, 'shell', {}) or {}
		piercing = _pair(_get(shot, 'piercingPower', None), None)
		if piercing is None:
			piercing = _pair(_get(shell, 'piercingPower', None), None)
		near = piercing[0] if piercing is not None else 0.0
		far = piercing[1] if piercing is not None else near
		result.append({
			'index': int(index),
			'kind': str(_get(shell, 'kind', '') or ''),
			'pen_100': max(0.0, near),
			'pen_far': max(0.0, far),
			'pen_far_distance': max(0.0, _num(_get(shot, 'maxDistance', 0.0), 0.0)),
			'damage': max(0.0, _num(_get(shell, 'damage', 0.0), 0.0)),
			'speed': max(0.0, _num(_get(shot, 'speed', 0.0), 0.0)),
			'gravity': abs(_num(_get(shot, 'gravity', 0.0), 0.0)),
			'caliber': max(0.0, _num(_get(shell, 'caliber', 0.0), 0.0)),
		})
	return tuple(result)


def pen_at(shell, distance):
	'''Nominal penetration at `distance` m: linear between the 100 m value and
	the value at `maxDistance`, which is exactly the pair the descriptor stores.
	The end values are held outside that span.  This only ranks ammunition - the
	client resolves the real hit, the same disclaimer `select_shell_index`
	carries (`bot_ai.py:1100-1103`).'''
	if not shell:
		return 0.0
	near = max(0.0, _num(shell.get('pen_100'), 0.0))
	far = max(0.0, _num(shell.get('pen_far'), near))
	span = _num(shell.get('pen_far_distance'), 0.0)
	distance = max(0.0, _num(distance, 0.0))
	if distance <= 100.0:
		return near
	if span <= 100.0 or distance >= span:
		return far
	fraction = (distance - 100.0) / (span - 100.0)
	return near + (far - near) * fraction


def best_penetration_at(caps, distance):
	'''Highest nominal penetration over every shell at `distance` metres.'''
	best = 0.0
	for shell in (getattr(caps, 'shells', ()) or ()):
		value = pen_at(shell, distance)
		if value > best:
			best = value
	return best


# ---------------------------------------------------------------------------
# The capabilities object
# ---------------------------------------------------------------------------
class VehicleCapabilities(object):
	'''Everything the planner may know about ONE machine.

	Plain attributes plus `__slots__` (py2.6 has no dataclasses): a field outside
	the schema cannot be read at all, so the `_MockVeh.__getattr__ -> None` trap
	(HANDOFF_antigravity.md §0 п.6) cannot silently invent a default here, and
	`or 0.0` is not needed to stay safe.'''

	__slots__ = (
		'key', 'vehicle_name', 'level', 'class_tag',
		'pitch_limits', 'pitch_basic', 'pitch_absolute', 'pitch_yaw_dependent',
		'pitch_source',
		'yaw_min', 'yaw_max', 'yaw_limited',
		'turret_traverse', 'gun_traverse',
		'reload_time', 'aiming_time', 'dispersion', 'dispersion_factors',
		'shells', 'shell_count',
		'armor_max', 'hull_armor', 'turret_armor', 'has_pike_nose',
		'forward_speed', 'backward_speed', 'power_per_ton', 'hull_traverse',
		'vision_range', 'camo_factor',
		'gun_height', 'weight', 'engine_power',
		'profile', 'complete',
	)

	def __init__(self):
		self.key = ''
		self.vehicle_name = ''
		self.level = 1
		self.class_tag = 'mediumTank'
		self.pitch_limits = None
		self.pitch_basic = PITCH_FALLBACK
		self.pitch_absolute = PITCH_FALLBACK
		self.pitch_yaw_dependent = False
		self.pitch_source = 'fallback'
		self.yaw_min = YAW_FALLBACK[0]
		self.yaw_max = YAW_FALLBACK[1]
		self.yaw_limited = False
		self.turret_traverse = TURRET_TRAVERSE_FALLBACK
		self.gun_traverse = GUN_TRAVERSE_FALLBACK
		self.reload_time = RELOAD_FALLBACK
		self.aiming_time = AIMING_FALLBACK
		self.dispersion = DISPERSION_FALLBACK
		self.dispersion_factors = None
		self.shells = ()
		self.shell_count = 0
		self.armor_max = 0.0
		self.hull_armor = (0.0, 0.0, 0.0)
		self.turret_armor = (0.0, 0.0, 0.0)
		self.has_pike_nose = False
		self.forward_speed = 0.0
		self.backward_speed = 0.0
		self.power_per_ton = 0.0
		self.hull_traverse = 0.0
		self.vision_range = VISION_FALLBACK
		# 0.8.2 keeps camouflage in the customization cache, not in the
		# typeDescriptor (vehicles.py:126-149), so this stays None unless a
		# caller supplies it.  An honest None beats an invented factor.
		self.camo_factor = None
		self.gun_height = 0.0
		self.weight = 0.0
		self.engine_power = 0.0
		self.profile = None
		self.complete = False

	def pitch_window(self, turret_yaw=0.0):
		'''(minimum, maximum, source) for one turret yaw - see gun_pitch_limits.'''
		return gun_pitch_limits(self.pitch_limits, turret_yaw)

	def traverse_seconds(self, turret_yaw_from, turret_yaw_to,
			gun_pitch_from=None, gun_pitch_to=None):
		'''Seconds to slew turret AND gun - the slower of the two.

		The planner needs this to know whether "turn and shoot" still fits inside
		the current window; the driver keeps owning the actual slewing.'''
		yaw_speed = max(0.01, _num(self.turret_traverse, TURRET_TRAVERSE_FALLBACK))
		yaw_cost = abs(angle_delta(turret_yaw_to, turret_yaw_from)) / yaw_speed
		pitch_cost = 0.0
		if gun_pitch_from is not None and gun_pitch_to is not None:
			pitch_speed = max(0.01, _num(self.gun_traverse, GUN_TRAVERSE_FALLBACK))
			pitch_cost = abs(_num(gun_pitch_to, 0.0) -
			                 _num(gun_pitch_from, 0.0)) / pitch_speed
		return max(yaw_cost, pitch_cost)

	def penetration_at(self, distance, shell_index=None):
		'''Nominal penetration at `distance` m, one shell or the best of all.'''
		if shell_index is None:
			return best_penetration_at(self, distance)
		for shell in (self.shells or ()):
			if int(shell.get('index', -1)) == int(shell_index):
				return pen_at(shell, distance)
		return 0.0

	def summary(self):
		'''One loggable line.  Values only, no formula: the instruments that
		check the fire gate (GUNPROBE, FBSOL) must not be fed by this module
		(rule "the probe never recomputes what the code computes").'''
		lowest, highest, source = self.pitch_window(0.0)
		return ('CAPS key=%s name=%s lvl=%d cls=%s pitch=%.3f/%.3f src=%s%s '
		        'yaw=%.2f/%.2f lim=%s trv=%.2f/%.2f rel=%.2f aim=%.2f '
		        'disp=%.3f shells=%d armor=%.0f vis=%.0f gunh=%.2f pwt=%.1f '
		        'complete=%s') % (
			self.key or '?', self.vehicle_name or '?', int(self.level),
			self.class_tag, lowest, highest, source,
			' yawdep' if self.pitch_yaw_dependent else '',
			self.yaw_min, self.yaw_max, 'yes' if self.yaw_limited else 'no',
			self.turret_traverse, self.gun_traverse, self.reload_time,
			self.aiming_time, self.dispersion, int(self.shell_count),
			self.armor_max, self.vision_range, self.gun_height,
			self.power_per_ton, 'yes' if self.complete else 'no')


class FireGate(object):
	'''The answer to "may this machine shoot at that point right now".

	`why` is a stable token so a log marker can be counted without parsing
	prose: ok | elevation | depression | traverse | traverse_time | unknown.'''

	__slots__ = ('ok', 'why', 'distance', 'gun_position', 'target_position',
	             'world_yaw', 'world_pitch', 'turret_yaw', 'gun_pitch',
	             'pitch_min', 'pitch_max', 'pitch_source', 'pitch_margin',
	             'yaw_min', 'yaw_max', 'yaw_limited', 'traverse_s',
	             'pose_known', 'pen_best')

	def __init__(self):
		self.ok = False
		self.why = 'unknown'
		self.distance = 0.0
		self.gun_position = None
		self.target_position = None
		self.world_yaw = 0.0
		self.world_pitch = 0.0
		self.turret_yaw = 0.0
		self.gun_pitch = 0.0
		self.pitch_min = PITCH_FALLBACK[0]
		self.pitch_max = PITCH_FALLBACK[1]
		self.pitch_source = 'fallback'
		self.pitch_margin = MARGIN_LEVEL_HULL
		self.yaw_min = YAW_FALLBACK[0]
		self.yaw_max = YAW_FALLBACK[1]
		self.yaw_limited = False
		self.traverse_s = 0.0
		self.pose_known = False
		self.pen_best = 0.0

	def summary(self):
		return ('V2_FIRE_GATE ok=%s why=%s dist=%.1f pitch=%.4f lim=%.3f/%.3f '
		        'src=%s m=%.3f tyaw=%.3f yawlim=%.2f/%.2f trv=%.2f pose=%s') % (
			'yes' if self.ok else 'no', self.why, self.distance, self.gun_pitch,
			self.pitch_min, self.pitch_max, self.pitch_source, self.pitch_margin,
			self.turret_yaw, self.yaw_min, self.yaw_max, self.traverse_s,
			'known' if self.pose_known else 'level')


def muzzle_height(caps):
	'''Metres from the vehicle origin to the muzzle, for callers that only know
	the hull position (`order_for` gets `position`, not the gun node).'''
	return _num(getattr(caps, 'gun_height', 0.0), 0.0)


def required_aim(caps, gun_position, target_position, hull_yaw,
		hull_pitch=None, hull_roll=None):
	'''(world_yaw, world_pitch, turret_yaw, gun_pitch, pose_known).

	`world_pitch` follows BigWorld (negative = up).  When the hull pose is known
	the world aim is converted with the shipped, battle-proven frame math
	(`bot_ai_driver.world_to_hull_aim` :472-493, verified to 0.000002 deg by
	`test_hull_frame_aim`); without a pose the level-hull identity is used and
	`pose_known` comes back False, which makes `fire_gate` widen its margin.'''
	gun = gun_position or (0.0, 0.0, 0.0)
	target = target_position or (0.0, 0.0, 0.0)
	delta_x = _num(target[0], 0.0) - _num(gun[0], 0.0)
	delta_z = _num(target[2], 0.0) - _num(gun[2], 0.0)
	delta_y = _num(target[1], 0.0) - _num(gun[1], 0.0)
	horizontal = math.sqrt(delta_x * delta_x + delta_z * delta_z)
	world_yaw = math.atan2(delta_x, delta_z)
	world_pitch = -math.atan2(delta_y, max(horizontal, 1e-6))
	pose_known = hull_pitch is not None and hull_roll is not None
	if pose_known:
		try:
			driver = _driver_module()
			if driver is not None:
				turret_yaw, gun_pitch = driver.world_to_hull_aim(
					world_yaw, world_pitch, _num(hull_yaw, 0.0),
					_num(hull_pitch, 0.0), _num(hull_roll, 0.0))
				return (world_yaw, world_pitch, turret_yaw, gun_pitch, True)
		except Exception:
			pass
		pose_known = False
	turret_yaw = angle_delta(world_yaw, _num(hull_yaw, 0.0))
	return (world_yaw, world_pitch, turret_yaw, world_pitch, pose_known)


def fire_gate(caps, gun_position, target_position, hull_yaw,
		hull_pitch=None, hull_roll=None, turret_yaw=0.0, gun_pitch=0.0,
		margin=None, traverse_budget=None, aim_offset_y=0.0):
	'''THE ADMISSIBILITY RULE (spec §3.1, step 3 of the owner's task).

	`fire_allowed` may only be True when the gun can PHYSICALLY reach the aim
	point: the required elevation lies inside the yaw-dependent pitch window and
	the required traverse lies inside the turret arc.  Everything else must
	become "relocate" or "stay silent" in the tactics layer.

	Optional arguments:
	  hull_pitch/hull_roll - the real suspension pose.  None = assume level hull
	                and widen the margin to MARGIN_LEVEL_HULL;
	  turret_yaw/gun_pitch - where the gun is NOW, only used to price the slew;
	  margin       - override the pitch margin, radians;
	  traverse_budget - seconds; a slew longer than this refuses the shot even
	                when the geometry fits (a bot that needs 6 s to turn should
	                reposition, not stare);
	  aim_offset_y - metres added to the target Y.  The v1 artillery path aims
	                1.0 m above the contact (`offline_battle.py:10228-10230`).

	Returns a FireGate.  Never raises: an unreadable input yields
	`ok=False, why='unknown'`, which is the safe answer for a fire gate.'''
	gate = FireGate()
	try:
		if caps is None or gun_position is None or target_position is None:
			gate.why = 'unknown'
			return gate
		offset_y = _num(aim_offset_y, 0.0)
		aim_point = (_num(target_position[0], 0.0),
		             _num(target_position[1], 0.0) + offset_y,
		             _num(target_position[2], 0.0))
		gate.gun_position = tuple([_num(gun_position[0], 0.0),
		                           _num(gun_position[1], 0.0),
		                           _num(gun_position[2], 0.0)])
		gate.target_position = aim_point
		delta_x = aim_point[0] - gate.gun_position[0]
		delta_z = aim_point[2] - gate.gun_position[2]
		delta_y = aim_point[1] - gate.gun_position[1]
		gate.distance = math.sqrt(delta_x * delta_x + delta_y * delta_y +
		                          delta_z * delta_z)
		gate.pen_best = best_penetration_at(caps, gate.distance)
		(world_yaw, world_pitch, turret_wanted, pitch_wanted,
		 pose_known) = required_aim(caps, gate.gun_position, aim_point,
		                            hull_yaw, hull_pitch, hull_roll)
		gate.world_yaw = world_yaw
		gate.world_pitch = world_pitch
		gate.turret_yaw = turret_wanted
		gate.gun_pitch = pitch_wanted
		gate.pose_known = pose_known
		if pitch_wanted != pitch_wanted or turret_wanted != turret_wanted:
			gate.why = 'unknown'
			return gate
		gate.pitch_margin = (_num(margin, MARGIN_POSE_KNOWN)
		                     if margin is not None else
		                     (MARGIN_POSE_KNOWN if pose_known
		                      else MARGIN_LEVEL_HULL))
		lowest, highest, source = gun_pitch_limits(
			getattr(caps, 'pitch_limits', None), turret_wanted)
		gate.pitch_min = lowest
		gate.pitch_max = highest
		gate.pitch_source = source
		gate.yaw_min = _num(getattr(caps, 'yaw_min', YAW_FALLBACK[0]),
		                    YAW_FALLBACK[0])
		gate.yaw_max = _num(getattr(caps, 'yaw_max', YAW_FALLBACK[1]),
		                    YAW_FALLBACK[1])
		gate.yaw_limited = bool(getattr(caps, 'yaw_limited', False))
		gate.traverse_s = caps.traverse_seconds(
			_num(turret_yaw, 0.0), turret_wanted,
			_num(gun_pitch, 0.0), pitch_wanted)
		margin = gate.pitch_margin
		horizontal = max(0.5, math.sqrt(delta_x * delta_x + delta_z * delta_z))
		if pitch_wanted < lowest - margin:
			if (lowest - pitch_wanted) * horizontal > 1.05 and horizontal > 25.0:
				gate.ok = False
				gate.why = 'elevation'      # target above the gun: barrel cannot rise
				return gate
		if pitch_wanted > highest + margin:
			if (pitch_wanted - highest) * horizontal > 1.05 and horizontal > 25.0:
				gate.ok = False
				gate.why = 'depression'     # target below: not enough depression
				return gate
		if gate.yaw_limited:
			yaw_margin = min(margin, 0.05)
			if not (gate.yaw_min - yaw_margin <= turret_wanted <=
			        gate.yaw_max + yaw_margin):
				gate.ok = False
				gate.why = 'traverse'
				return gate
		if traverse_budget is not None:
			if gate.traverse_s > max(0.0, _num(traverse_budget, 0.0)):
				gate.ok = False
				gate.why = 'traverse_time'
				return gate
		gate.ok = True
		gate.why = 'ok'
		return gate
	except Exception:
		gate.ok = False
		gate.why = 'unknown'
		return gate


# ---------------------------------------------------------------------------
# Building and caching
# ---------------------------------------------------------------------------
def _bot_ai_module():
	'''bot_ai, imported lazily and optionally: v2 reuses its armor/class readers
	instead of duplicating them, but must still load without them (worker).'''
	try:
		from gui.mods.offhangar import bot_ai
		return bot_ai
	except ImportError:
		try:
			import bot_ai
			return bot_ai
		except ImportError:
			return None
	except Exception:
		return None


def build_capabilities(descriptor, profile=None, key=''):
	'''One VehicleCapabilities for one typeDescriptor.

	`profile` is the v1 tactical profile (`bot_ai.build_vehicle_profile`,
	:980-1096) when the caller already has it - spec §1 says keep that layer, so
	class/armor/range fields are taken from it verbatim rather than recomputed.
	Without a profile the same numbers are derived through bot_ai's own readers
	when that module can be imported, and `complete` stays False otherwise.'''
	caps = VehicleCapabilities()
	caps.key = str(key or '')
	caps.profile = profile if isinstance(profile, dict) else None
	gun = _get(descriptor, 'gun', None)
	turret = _get(descriptor, 'turret', None)
	hull = _get(descriptor, 'hull', None)
	chassis = _get(descriptor, 'chassis', None)
	physics = _get(descriptor, 'physics', None)
	type_info = _get(descriptor, 'type', None)

	# --- identity ---------------------------------------------------------
	name = _get(type_info, 'name', None)
	if name is None and isinstance(profile, dict):
		name = profile.get('vehicle_name')
	caps.vehicle_name = str(name or caps.key or '')
	if not caps.key:
		caps.key = caps.vehicle_name
	level = _get(type_info, 'level', None)
	if level is None and isinstance(profile, dict):
		level = profile.get('level')
	try:
		caps.level = max(1, min(10, int(_num(level, 1.0))))
	except Exception:
		caps.level = 1
	if isinstance(profile, dict) and profile.get('class_tag'):
		caps.class_tag = str(profile.get('class_tag'))
	else:
		module = _bot_ai_module()
		if module is not None:
			try:
				fallback = module.build_vehicle_profile(descriptor)
				caps.class_tag = str(fallback.get('class_tag', caps.class_tag))
			except Exception:
				pass

	# --- gun geometry -----------------------------------------------------
	caps.pitch_limits = _get(gun, 'pitchLimits', None)
	(basic, absolute, front, back, transition,
	 yaw_dependent) = pitch_limit_sectors(caps.pitch_limits)
	if basic is not None:
		caps.pitch_basic = basic
		caps.pitch_absolute = absolute if absolute is not None else basic
		caps.pitch_yaw_dependent = yaw_dependent
		caps.pitch_source = 'descriptor'
	caps.yaw_min, caps.yaw_max, caps.yaw_limited = gun_yaw_limits(descriptor)
	caps.turret_traverse = max(0.01, _num(_get(turret, 'rotationSpeed', None),
	                                      TURRET_TRAVERSE_FALLBACK))
	caps.gun_traverse = max(0.01, _num(_get(gun, 'rotationSpeed', None),
	                                   GUN_TRAVERSE_FALLBACK))
	caps.reload_time = max(0.1, _num(_get(gun, 'reloadTime', None),
	                                 RELOAD_FALLBACK))
	caps.aiming_time = max(0.1, _num(_get(gun, 'aimingTime', None),
	                                 AIMING_FALLBACK))
	caps.dispersion = max(0.0, _num(_get(gun, 'shotDispersionAngle', None),
	                                DISPERSION_FALLBACK))
	factors = _get(gun, 'shotDispersionFactors', None)
	if factors is not None:
		caps.dispersion_factors = factors
	caps.shells = shell_capabilities(descriptor)
	caps.shell_count = len(caps.shells)
	caps.vision_range = max(0.0, _num(_get(turret, 'circularVisionRadius', None),
	                                  VISION_FALLBACK))

	# --- muzzle height: hull turretPositions + chassis hullPosition + gun ----
	# the same chain the client uses (offline_battle.py:16009-16010)
	height = 0.0
	try:
		positions = _get(hull, 'turretPositions', None)
		if positions:
			height += _component_y(positions[0], 0.0)
		height += _component_y(_get(chassis, 'hullPosition', None), 0.0)
		height += _component_y(_get(turret, 'gunPosition', None), 0.0)
	except Exception:
		height = 0.0
	caps.gun_height = height

	# --- armor / mobility: prefer the v1 profile, else bot_ai's readers -----
	if isinstance(profile, dict):
		caps.armor_max = max(0.0, _num(profile.get('armor'), 0.0))
		hull_armor = profile.get('hull_armor')
		turret_armor = profile.get('turret_armor')
		if hull_armor is not None:
			caps.hull_armor = tuple([_num(v, 0.0) for v in hull_armor])
		if turret_armor is not None:
			caps.turret_armor = tuple([_num(v, 0.0) for v in turret_armor])
		caps.has_pike_nose = bool(profile.get('has_pike_nose'))
		caps.forward_speed = max(0.0, _num(profile.get('speed'), 0.0))
		caps.complete = True
	else:
		module = _bot_ai_module()
		if module is not None:
			try:
				hull_armor = module._component_armor(hull)
				turret_armor = module._component_armor(turret)
				caps.hull_armor = hull_armor
				caps.turret_armor = turret_armor
				caps.armor_max = max(module._primary_armor(hull),
				                     module._primary_armor(turret))
				caps.has_pike_nose = bool(
					module._has_pike_nose(caps.vehicle_name))
				caps.forward_speed = max(0.0, module._forward_speed(descriptor))
				caps.complete = True
			except Exception:
				caps.complete = False
	limits = _pair(_get(physics, 'speedLimits', None), None)
	if limits is not None:
		if caps.forward_speed <= 0.0:
			caps.forward_speed = max(0.0, limits[0])
		caps.backward_speed = max(0.0, limits[1])
	caps.weight = max(0.0, _num(_get(physics, 'weight', None), 0.0))
	caps.engine_power = max(0.0, _num(_get(physics, 'enginePower', None), 0.0))
	if caps.weight > 0.0:
		# physics['weight'] is the sum of component weights in KG and
		# physics['enginePower'] is engine['power'] in HP
		# (vehicles.py:805-849), so specific power in hp/t needs the /1000.
		caps.power_per_ton = caps.engine_power / (caps.weight / 1000.0)
	caps.hull_traverse = max(0.0, _num(_get(chassis, 'rotationSpeed', None),
	                                   0.0))
	return caps


def capabilities_for(key, descriptor=None, profile=None):
	'''Cached `build_capabilities`.  Keyed by a CALLER-SUPPLIED string (vehicle
	name or typeDescriptor compact key) - never by `id(descriptor)`, because an
	id can be reused after the object is collected and would then serve one
	tank's limits to another.'''
	cache_key = str(key or '')
	cached = _CACHE.get(cache_key)
	if cached is not None:
		return cached
	if descriptor is None:
		empty = VehicleCapabilities()
		empty.key = cache_key
		return empty
	caps = build_capabilities(descriptor, profile, cache_key)
	if len(_CACHE) >= CACHE_LIMIT:
		_CACHE.clear()
	_CACHE[cache_key] = caps
	return caps


def reset_cache():
	'''Drop the cache (new battle generation, or a headless test).'''
	try:
		_CACHE.clear()
	except Exception:
		pass


def cache_size():
	return len(_CACHE)
