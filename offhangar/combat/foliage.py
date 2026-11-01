# -*- coding: utf-8 -*-
"""Pair-specific concealment from prebaked World of Tanks vegetation.

The module is engine-free and Python 2.6 compatible.  Map resources are baked
into oriented foliage volumes; runtime spotting only visits spatial cells
crossed by the observer-to-target segment.
"""

import math


FOLIAGE_CAMOUFLAGE_PER_VOLUME = 0.15
FOLIAGE_CAMOUFLAGE_LIMIT = 0.48
FIRE_TRANSPARENCY_DISTANCE = 15.0
OBSERVER_EYE_HEIGHT = 2.0
TARGET_CHECK_HEIGHT = 1.5


def _segment_cells(start, end, cell_size):
	"""Return a cheap super-sampled grid traversal for one sight segment."""
	cell_size = max(1.0, float(cell_size))
	dx = float(end[0]) - float(start[0])
	dz = float(end[2]) - float(start[2])
	steps = max(1, int(math.ceil(max(abs(dx), abs(dz)) / cell_size * 2.0)))
	result = []
	seen = set()
	for index in range(steps + 1):
		fraction = float(index) / float(steps)
		cell = (int(math.floor((float(start[0]) + dx * fraction) / cell_size)),
		        int(math.floor((float(start[2]) + dz * fraction) / cell_size)))
		if cell not in seen:
			seen.add(cell)
			result.append(cell)
	return result


def _slab_interval(origin, delta, minimum, maximum, low, high):
	if abs(delta) <= 1e-9:
		if origin < minimum or origin > maximum:
			return None
		return low, high
	first = (minimum - origin) / delta
	second = (maximum - origin) / delta
	if first > second:
		first, second = second, first
	low = max(low, first)
	high = min(high, second)
	if low > high:
		return None
	return low, high


def _intersects(instance, start, end):
	"""Test one 3-D sight segment against an oriented foliage box.

	Rows store the inverse horizontal transform, avoiding a matrix inversion in
	the 2 Hz spotting loop:
	[cx, min_y, cz, max_y, i00, i01, i10, i11, strength, radius].
	"""
	dx0 = start[0] - instance[0]
	dz0 = start[2] - instance[2]
	dx1 = end[0] - instance[0]
	dz1 = end[2] - instance[2]
	u0 = instance[4] * dx0 + instance[5] * dz0
	v0 = instance[6] * dx0 + instance[7] * dz0
	u1 = instance[4] * dx1 + instance[5] * dz1
	v1 = instance[6] * dx1 + instance[7] * dz1
	interval = _slab_interval(u0, u1 - u0, -1.15, 1.15, 0.0, 1.0)
	if interval is None:
		return False
	interval = _slab_interval(v0, v1 - v0, -1.15, 1.15,
	                         interval[0], interval[1])
	if interval is None:
		return False
	middle = (interval[0] + interval[1]) * 0.5
	y = start[1] + (end[1] - start[1]) * middle
	return (instance[1] - 0.6) <= y <= (instance[3] + 0.6)


class FoliageMap(object):
	"""Validated spatial foliage index for one arena."""

	def __init__(self, data):
		self.map_name = str(data.get('map') or '')
		self.cell_size = max(1.0, float(data.get('cell_size', 32.0)))
		self.instances = tuple(
			tuple(float(value) for value in instance)
			for instance in (data.get('instances') or ()))
		self.cells = {}
		for key, values in (data.get('cells') or {}).items():
			parts = str(key).split(',', 1)
			if len(parts) == 2:
				self.cells[(int(parts[0]), int(parts[1]))] = tuple(
					int(value) for value in values)
		self._pair_cache = {}
		try:
			from gui.mods.offhangar.offh_multithread import get_coordinator
			get_coordinator().upload_prebaked_foliage(self.instances, self.map_name)
		except Exception:
			pass

	def camouflage_bonus(self, observer, target, fired_recently=False):
		"""Return additive camouflage for this observer-target pair."""
		try:
			ox, oy, oz = float(observer[0]), float(observer[1]), float(observer[2])
			tx, ty, tz = float(target[0]), float(target[1]), float(target[2])
			ckey = (int(ox * 0.5), int(oz * 0.5), int(tx * 0.5), int(tz * 0.5), bool(fired_recently))
			cached = self._pair_cache.get(ckey)
			if cached is not None:
				return cached
			if len(self._pair_cache) > 512:
				self._pair_cache.clear()
			from gui.mods.offhangar.offh_multithread import get_coordinator
			c_res = get_coordinator().foliage_bonus_fast(observer, target, fired_recently)
			if c_res is not None:
				self._pair_cache[ckey] = c_res
				return c_res
		except Exception:
			ckey = None
		start = (float(observer[0]), float(observer[1]) + OBSERVER_EYE_HEIGHT,
		         float(observer[2]))
		end = (float(target[0]), float(target[1]) + TARGET_CHECK_HEIGHT,
		       float(target[2]))
		seen = set()
		bonus = 0.0
		for cell_x, cell_z in _segment_cells(start, end, self.cell_size):
			for instance_id in self.cells.get((cell_x, cell_z), ()):
				if instance_id in seen:
					continue
				seen.add(instance_id)
				if instance_id < 0 or instance_id >= len(self.instances):
					continue
				instance = self.instances[instance_id]
				# v6.8: In WoT 0.8.2, only bushes/trees within ~25m of the target (or 12m of the observer)
				# provide concealment for that pair; distant mid-map trees do not stack camo!
				tdx = float(target[0]) - instance[0]
				tdz = float(target[2]) - instance[2]
				tdist_sq = tdx * tdx + tdz * tdz
				odx = float(observer[0]) - instance[0]
				odz = float(observer[2]) - instance[2]
				odist_sq = odx * odx + odz * odz
				max_t_r = 25.0 + float(instance[9])
				max_o_r = 12.0 + float(instance[9])
				if tdist_sq > max_t_r * max_t_r and odist_sq > max_o_r * max_o_r:
					continue
				if fired_recently:
					if tdist_sq <= (FIRE_TRANSPARENCY_DISTANCE + instance[9]) ** 2:
						continue
				if _intersects(instance, start, end):
					bonus += instance[8]
					if bonus >= FOLIAGE_CAMOUFLAGE_LIMIT:
						return FOLIAGE_CAMOUFLAGE_LIMIT
		return min(FOLIAGE_CAMOUFLAGE_LIMIT, max(0.0, bonus))
