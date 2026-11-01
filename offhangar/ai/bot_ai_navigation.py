# -*- coding: utf-8 -*-
"""Terrain-aware hierarchical navigation for offline/LAN bots.

Strategic map annotations decide where a vehicle should fight. This module
connects those sparse anchors with a low-resolution A* path, while the battle
driver remains responsible for short-range steering around moving vehicles.
The implementation is engine-free; the caller supplies terrain and collision
probes so it can be tested outside the legacy client.
"""

import heapq
import math
import time
from array import array


SQRT_TWO = math.sqrt(2.0)
BAKED_FATAL_HAZARDS = 1 | 2
BAKED_SHALLOW_WATER = 4
BAKED_SHALLOW_WATER_PENALTY = 0.65

try:
	_CLOCK = time.clock
except AttributeError:
	_CLOCK = time.perf_counter


def _distance_2d(first, second):
	dx = float(first[0]) - float(second[0])
	dz = float(first[2]) - float(second[2])
	return math.sqrt(dx * dx + dz * dz)


class TerrainGrid(object):
	"""Lazy terrain graph. Cells and edges are probed only when A* needs them."""

	_NEIGHBOURS = (
		(-1, -1, SQRT_TWO), (0, -1, 1.0), (1, -1, SQRT_TWO),
		(-1, 0, 1.0),                         (1, 0, 1.0),
		(-1, 1, SQRT_TWO),  (0, 1, 1.0),  (1, 1, SQRT_TWO),
	)
	_DIRECTION_INDEX = {
		(-1, -1): 0, (0, -1): 1, (1, -1): 2,
		(-1, 0): 3, (1, 0): 4,
		(-1, 1): 5, (0, 1): 6, (1, 1): 7,
	}

	def __init__(self, ground_probe, obstacle_probe=None, bounds=None,
			cell_size=18.0, max_grade_up=0.48, max_grade_down=0.38,
			baked_graph=None):
		self.ground_probe = ground_probe
		self.obstacle_probe = obstacle_probe
		self.bounds = bounds
		self.cell_size = max(1.0, float(cell_size))
		self.prebaked = False
		self._baked_origin = (0.0, 0.0)
		self._baked_width = 0
		self._baked_height = 0
		self._baked_heights = ()
		self._baked_links = ()
		self._baked_hazards = ()
		self._baked_node_count = 0
		self._baked_hazard_near_one = bytearray()
		self._baked_hazard_near_two = bytearray()
		self._baked_valid = bytearray()
		self._baked_neighbour_deltas = ()
		self._baked_edge_costs = array('d')
		self._baked_max_grade = 0.30
		self._mesh3d_origin = (0.0, 0.0)
		self._mesh3d_step = 0.0
		self._mesh3d_width = 0
		self._mesh3d_height = 0
		self._mesh3d_flags = ()
		self._mesh3d_heights = ()
		self._avoid_shallow_water = True
		if baked_graph is not None:
			self._install_baked_graph(baked_graph)
		self.max_grade_up = float(max_grade_up)
		self.max_grade_down = float(max_grade_down)
		# Weighted A* deliberately favours forward progress over a perfectly
		# shortest coarse-grid route. Every returned edge is still terrain-probed;
		# only the amount of side exploration changes.
		self.heuristic_weight = 1.70
		self._ground_cache = {}
		self._edge_cache = {}
		self._segment_cache = {}
		self._failed_edges = {}

	def _install_baked_graph(self, graph):
		if (graph.get('format') != 'offhangar-navgraph' or
				int(graph.get('version', -1)) != 1):
			raise ValueError('unsupported baked navigation graph')
		width = int(graph.get('width', 0))
		height = int(graph.get('height', 0))
		heights = graph.get('heights_mm') or ()
		links = graph.get('links') or ()
		hazards = graph.get('hazards')
		origin = graph.get('origin') or ()
		if (width <= 0 or height <= 0 or len(origin) != 2 or
				len(heights) != width * height or len(links) != width * height or
				(hazards is not None and len(hazards) != width * height)):
			raise ValueError('invalid baked navigation graph')
		self.cell_size = max(1.0, float(graph.get('cell_size', 0.0)))
		self._baked_origin = (float(origin[0]), float(origin[1]))
		self._baked_width = width
		self._baked_height = height
		self._baked_heights = heights
		self._baked_hazards = hazards if hazards is not None else (0,) * (width * height)
		bake = graph.get('bake') if isinstance(graph.get('bake'), dict) else {}
		self._baked_max_grade = max(0.05, float(bake.get('max_grade', 0.30)))
		# Shallow rivers and fords (BAKED_SHALLOW_WATER = 4) are passable with a mild terrain resistance cost;
		# only deep water (1) and steep cliffs (2) are fatal hazards.
		self._avoid_shallow_water = False
		self._baked_bridge_cells = bytearray(width * height)
		try:
			ox_b, oz_b = self._baked_origin
			cs_b = self.cell_size
			for _r_container in [graph] + [mv for mv in (graph.get('mode_data') or {}).values() if isinstance(mv, dict)]:
				_r_dict = _r_container.get('routes') or {}
				if isinstance(_r_dict, dict):
					for _t_list in _r_dict.values():
						for _ritem in (_t_list or ()):
							_wps = _ritem.get('waypoints', ()) if isinstance(_ritem, dict) else _ritem
							for _pt in (_wps or ()):
								if len(_pt) >= 3 and _pt[2]:
									_bcx = int(round((float(_pt[0]) - ox_b) / cs_b))
									_bcz = int(round((float(_pt[1]) - oz_b) / cs_b))
									for _bz in range(max(0, _bcz - 3), min(height, _bcz + 4)):
										_bbase = _bz * width
										for _bx in range(max(0, _bcx - 3), min(width, _bcx + 4)):
											self._baked_bridge_cells[_bbase + _bx] = 1
		except Exception:
			pass
		mesh3d = graph.get('mesh3d')
		if isinstance(mesh3d, (tuple, list)) and len(mesh3d) == 7:
			self._mesh3d_origin = (float(mesh3d[0]), float(mesh3d[1]))
			self._mesh3d_step = max(0.5, float(mesh3d[2]))
			self._mesh3d_width = int(mesh3d[3])
			self._mesh3d_height = int(mesh3d[4])
			self._mesh3d_flags = mesh3d[5]
			self._mesh3d_heights = mesh3d[6]
		self._build_compact_baked_graph(links)
		self.bounds = tuple(graph.get('bounds') or self.bounds or ()) or None
		self.prebaked = True

	def _build_compact_baked_graph(self, source_links):
		"""Precompute scalar edge costs for the render-thread path search."""
		width = self._baked_width
		height = self._baked_height
		count = width * height
		valid = bytearray(count)
		node_count = 0
		for index in range(count):
			if self._baked_heights[index] is None:
				continue
			if int(self._baked_hazards[index]) & BAKED_FATAL_HAZARDS:
				continue
			if int(source_links[index]) == 0:
				continue
			node_count += 1
			valid[index] = 1
		self._baked_node_count = node_count
		self._baked_valid = valid
		# Dilate ONLY deep water (hazard bit 1) onto low-lying shoreline cells, excluding bridge decks
		# and elevated banks (>0.85m above water). Cliff walls (bit 2) already have valid[index]==0,
		# so dilating bit 2 previously poisoned narrow canyons, gorges, and mountain passes!
		bridge_cells = getattr(self, '_baked_bridge_cells', None)
		hazard_near_one = bytearray(count)
		hazard_near_two = bytearray(count)
		for index in range(count):
			if not (int(self._baked_hazards[index]) & 1):
				continue
			water_h = self._baked_heights[index]
			x = index % width
			z = index // width
			for near_z in range(max(0, z - 2), min(height, z + 3)):
				base = near_z * width
				dz_abs = abs(near_z - z)
				for near_x in range(max(0, x - 2), min(width, x + 3)):
					n_idx = base + near_x
					if bridge_cells is not None and bridge_cells[n_idx]:
						continue
					nh = self._baked_heights[n_idx]
					if nh is not None and water_h is not None and (int(nh) - int(water_h)) >= 850:
						continue
					cheb = max(dz_abs, abs(near_x - x))
					hazard_near_two[n_idx] = 1
					if cheb <= 1:
						hazard_near_one[n_idx] = 1
		self._baked_hazard_near_one = hazard_near_one
		self._baked_hazard_near_two = hazard_near_two
		self._baked_neighbour_deltas = (
			-width - 1, -width, -width + 1, -1,
			1, width - 1, width, width + 1)
		compact_links = bytearray(count)
		edge_costs = array('d', [0.0]) * (count * len(self._NEIGHBOURS))
		for index in range(count):
			if not valid[index]:
				continue
			x = index % width
			z = index // width
			current_y = float(self._baked_heights[index]) / 1000.0
			mask = int(source_links[index])
			filtered_mask = 0
			for direction_index, neighbour in enumerate(self._NEIGHBOURS):
				if not (mask & (1 << direction_index)):
					continue
				next_x = x + neighbour[0]
				next_z = z + neighbour[1]
				if (next_x < 0 or next_x >= width or
						next_z < 0 or next_z >= height):
					continue
				next_index = index + self._baked_neighbour_deltas[direction_index]
				if not valid[next_index]:
					continue
				run = self.cell_size * neighbour[2]
				delta_y = (float(self._baked_heights[next_index]) / 1000.0 -
				           current_y)
				slope = abs(delta_y) / max(run, 0.1)
				slope_ratio = slope / max(0.05, self._baked_max_grade)
				slope_cost = run * slope_ratio * slope_ratio * 7.5
				if delta_y < 0.0:
					slope_cost *= (1.45 if hazard_near_one[next_index] else 1.20)
				hazard_cost = 0.0
				if int(self._baked_hazards[next_index]) & BAKED_SHALLOW_WATER:
					hazard_cost += self.cell_size * 0.65
				if hazard_near_one[next_index]:
					hazard_cost += self.cell_size * 3.5
				elif hazard_near_two[next_index]:
					hazard_cost += self.cell_size * 1.0
				if self._mesh3d_flags and self._mesh3d_width > 0 and self._mesh3d_step > 0.0:
					_nwx = self._baked_origin[0] + next_x * self.cell_size
					_nwz = self._baked_origin[1] + next_z * self.cell_size
					_mgx = int((_nwx - self._mesh3d_origin[0]) / self._mesh3d_step)
					_mgz = int((_nwz - self._mesh3d_origin[1]) / self._mesh3d_step)
					if 0 <= _mgx < self._mesh3d_width and 0 <= _mgz < self._mesh3d_height:
						_midx = _mgz * self._mesh3d_width + _mgx
						_mfl = int(self._mesh3d_flags[_midx])
						if (_mfl & 12) != 0:
							hazard_cost = max(-run * 0.14, hazard_cost - self.cell_size * 0.12)
				edge_costs[index * 8 + direction_index] = max(run * 0.82,
					run + slope_cost + hazard_cost)
				filtered_mask |= 1 << direction_index
			compact_links[index] = filtered_mask
			if filtered_mask == 0:
				valid[index] = 0
		# v13.0 Prune isolated cells and tiny disconnected island pockets (< 32 cells) so no bot ever snaps onto a dead-end 1-cell island!
		try:
			visited = bytearray(count)
			for idx0 in range(count):
				if not valid[idx0] or visited[idx0]:
					continue
				comp = [idx0]
				visited[idx0] = 1
				head = 0
				while head < len(comp):
					cur = comp[head]
					head += 1
					cmask = int(compact_links[cur])
					if not cmask:
						continue
					cx = cur % width
					cz = cur // width
					for d_i, nb in enumerate(self._NEIGHBOURS):
						if not (cmask & (1 << d_i)):
							continue
						nx = cx + nb[0]
						nz = cz + nb[1]
						if 0 <= nx < width and 0 <= nz < height:
							n_idx = cur + self._baked_neighbour_deltas[d_i]
							if valid[n_idx] and not visited[n_idx]:
								visited[n_idx] = 1
								comp.append(n_idx)
				if len(comp) < 32:
					for dead_idx in comp:
						valid[dead_idx] = 0
						compact_links[dead_idx] = 0
		except Exception:
			pass
		# v12.0 Street-Center Clearance Cost: penalize wall-border cells (compact_links != 0xFF)
		# so A* paths run down the center of city streets instead of scraping along brick walls!
		for index in range(count):
			if not valid[index]:
				continue
			mask = int(compact_links[index])
			for direction_index in range(len(self._NEIGHBOURS)):
				if not (mask & (1 << direction_index)):
					continue
				next_index = index + self._baked_neighbour_deltas[direction_index]
				n_links = int(compact_links[next_index])
				if n_links != 0xFF:
					wall_pen = self.cell_size * (0.95 if (n_links & 0x5A) != 0x5A else 0.38)
					edge_costs[index * 8 + direction_index] += wall_pen
		self._baked_links = compact_links
		self._baked_edge_costs = edge_costs

	def cell_for(self, point):
		origin_x, origin_z = self._baked_origin if self.prebaked else (0.0, 0.0)
		return (int(math.floor((float(point[0]) - origin_x) /
		                       self.cell_size + 0.5)),
		        int(math.floor((float(point[2]) - origin_z) /
		                       self.cell_size + 0.5)))

	def point_for(self, cell, height):
		origin_x, origin_z = self._baked_origin if self.prebaked else (0.0, 0.0)
		return (origin_x + cell[0] * self.cell_size, float(height),
		        origin_z + cell[1] * self.cell_size)

	def _baked_index(self, cell):
		if not self.prebaked:
			return None
		x, z = cell
		if x < 0 or x >= self._baked_width or z < 0 or z >= self._baked_height:
			return None
		index = z * self._baked_width + x
		if not self._baked_valid[index]:
			return None
		return index

	def _baked_flat_index(self, cell):
		if not self.prebaked:
			return None
		x, z = cell
		if x < 0 or x >= self._baked_width or z < 0 or z >= self._baked_height:
			return None
		return z * self._baked_width + x

	def _baked_cell_height(self, cell):
		index = self._baked_index(cell)
		if index is None:
			return None
		return float(self._baked_heights[index]) / 1000.0

	def is_2d_corridor_allowed(self, start, end, half_width=1.65):
		"""v12.4 Smart 2D Boundary & Hazard Guard:
		- Strict long-range (up to 15m + shoulders) prohibition against Hard No-Go / Cliff / Water boundaries ((hazards & 3) != 0).
		- Short-range (<= 4.8m centerline only) check against un-collidable 2D solid cells (hm=None), so 8m..14m city streets
		  and corners are never choked while 3D wg_collideSegment handles exact building wall geometry."""
		if not self.prebaked or start is None or end is None:
			return True
		s_cell = self.cell_for(start)
		s_idx = self._baked_index(s_cell)
		if s_idx is None:
			e_cell = self.cell_for(end)
			if self._baked_index(e_cell) is not None:
				return True
			near = self._nearest_baked_cell(s_cell, 8)
			if near is None:
				return True
			near_pt = self.point_for(near, float(start[1]))
			d0 = (float(start[0]) - float(near_pt[0])) ** 2 + (float(start[2]) - float(near_pt[2])) ** 2
			d1 = (float(end[0]) - float(near_pt[0])) ** 2 + (float(end[2]) - float(near_pt[2])) ** 2
			return d1 < d0
		ox, oz = self._baked_origin
		size = self.cell_size
		w = self._baked_width
		h = self._baked_height
		valid = self._baked_valid
		hazards = self._baked_hazards
		sx, sz = float(start[0]), float(start[2])
		ex, ez = float(end[0]), float(end[2])
		dx = ex - sx
		dz = ez - sz
		dist = math.sqrt(dx * dx + dz * dz)
		if dist <= 0.25:
			return True
		ux = dx / dist
		uz = dz / dist
		nx = -uz
		nz = ux
		check_d = min(dist, 15.0)
		sh = max(0.8, min(1.25, float(half_width) * 0.50))
		steps = max(2, int(math.ceil(check_d / (size * 0.45))))
		for i in range(1, steps + 1):
			d_cur = check_d * (float(i) / float(steps))
			px = sx + ux * d_cur
			pz = sz + uz * d_cur
			cx = int(math.floor((px - ox) / size + 0.5))
			cz = int(math.floor((pz - oz) / size + 0.5))
			if cx < 1 or cx >= w - 1 or cz < 1 or cz >= h - 1:
				return False
			idx = cz * w + cx
			# 1) Never allow steering into Hard No-Go / Cliff / Water boundaries at any distance along the ray
			if (int(hazards[idx]) & BAKED_FATAL_HAZARDS) != 0:
				return False
			# 2) For solid building/obstacle cells (hm=None, haz=0), only block immediate <= 4.8m centerline
			if d_cur <= 4.8 and not valid[idx]:
				return False
			# 3) Check lateral shoulders strictly against Hard No-Go / Cliff / Water boundaries (never against building walls!)
			if 2.5 <= d_cur <= 10.5:
				for side in (-sh, sh):
					lx = px + nx * side
					lz = pz + nz * side
					lcx = int(math.floor((lx - ox) / size + 0.5))
					lcz = int(math.floor((lz - oz) / size + 0.5))
					if lcx < 1 or lcx >= w - 1 or lcz < 1 or lcz >= h - 1:
						return False
					lidx = lcz * w + lcx
					if (int(hazards[lidx]) & BAKED_FATAL_HAZARDS) != 0:
						return False
		return True

	def clamp_to_safe_corridor(self, start, end):
		"""v12.5 Combat Boundary Guard:
		- For strategic/A*-navigated moves (> 16m), validate the destination endpoint `end`
		  (snapping it out of any hazard/wall cell) without truncating long chords onto `start`.
		- For short local skirmish maneuvers (<= 16m), clamp before any fatal hazard ((hazards & 3) != 0)
		  while never returning a point within 4.0m of `start`."""
		if not self.prebaked or start is None or end is None:
			return end
		try:
			sx, sy, sz = float(start[0]), float(start[1]), float(start[2])
			ex, ey, ez = float(end[0]), float(end[1]), float(end[2])
			dx = ex - sx
			dz = ez - sz
			dist = math.sqrt(dx * dx + dz * dz)
			if dist < 1.0:
				return end
			ox, oz = self._baked_origin
			size = self.cell_size
			w = self._baked_width
			h = self._baked_height
			valid = self._baked_valid
			hazards = self._baked_hazards
			e_cell = self.cell_for(end)
			e_idx = self._baked_index(e_cell)
			if dist > 16.0:
				if e_idx is None or not valid[e_idx] or (int(hazards[e_idx]) & BAKED_FATAL_HAZARDS) != 0:
					return self.snap_to_safe_baked_point(end, 8) or end
				return end
			last_safe = None
			steps = max(2, int(math.ceil(dist / (size * 0.5))))
			for i in range(1, steps + 1):
				t = float(i) / float(steps)
				px = sx + dx * t
				pz = sz + dz * t
				cx = int(math.floor((px - ox) / size + 0.5))
				cz = int(math.floor((pz - oz) / size + 0.5))
				if cx < 2 or cx >= w - 2 or cz < 2 or cz >= h - 2:
					break
				idx = cz * w + cx
				if (int(hazards[idx]) & BAKED_FATAL_HAZARDS) != 0:
					break
				if valid[idx]:
					last_safe = (px, ey, pz)
			if last_safe is not None and math.hypot(last_safe[0] - sx, last_safe[2] - sz) >= 4.0:
				if e_idx is None or not valid[e_idx] or (int(hazards[e_idx]) & BAKED_FATAL_HAZARDS) != 0:
					return self.snap_to_safe_baked_point(last_safe, 6) or last_safe
				return last_safe
			if e_idx is None or not valid[e_idx] or (int(hazards[e_idx]) & BAKED_FATAL_HAZARDS) != 0:
				return self.snap_to_safe_baked_point(end, 8) or end
			return end
		except Exception:
			return end

	# line-of-sight and cover search on the shipped navgraph.
	# Cells without terrain height that are not water are buildings, rocks or
	# other solid models; the bake removed them because a hull cannot drive there,
	# which also means a shell cannot pass through them at hull height.
	def mesh3d_height(self, wx, wz):
		"""Return exact 3D top surface/roof height from navmesh/<map>.grid (OFFHNAV1) at world (wx, wz)."""
		if not self._mesh3d_heights or self._mesh3d_width <= 0 or self._mesh3d_step <= 0.0:
			return None
		gx = int((float(wx) - self._mesh3d_origin[0]) / self._mesh3d_step)
		gz = int((float(wz) - self._mesh3d_origin[1]) / self._mesh3d_step)
		if gx < 0 or gx >= self._mesh3d_width or gz < 0 or gz >= self._mesh3d_height:
			return None
		idx = gz * self._mesh3d_width + gx
		if self._mesh3d_flags and not self._mesh3d_flags[idx]:
			return None
		return float(self._mesh3d_heights[idx])

	def mesh3d_flag(self, wx, wz):
		"""Return 3D cell flag bitmask from navmesh/<map>.grid (OFFHNAV1):
		bit 0 (1): valid, bit 1 (2): SOLID_3D, bit 2 (4): HULL_DOWN_3D, bit 3 (8): BUSH_3D, bit 4 (16): TREE_3D."""
		if not self._mesh3d_flags or self._mesh3d_width <= 0 or self._mesh3d_step <= 0.0:
			return 0
		gx = int((float(wx) - self._mesh3d_origin[0]) / self._mesh3d_step)
		gz = int((float(wz) - self._mesh3d_origin[1]) / self._mesh3d_step)
		if gx < 0 or gx >= self._mesh3d_width or gz < 0 or gz >= self._mesh3d_height:
			return 0
		return int(self._mesh3d_flags[gz * self._mesh3d_width + gx])

	def is_bush_3d(self, wx, wz):
		return (self.mesh3d_flag(wx, wz) & 8) != 0

	def is_hulldown_3d(self, wx, wz):
		return (self.mesh3d_flag(wx, wz) & 4) != 0

	def ground_height_3d(self, wx, wz, fallback_y=0.0):
		"""Return exact 3D navigable ground height at world (wx, wz) from the 3D grid."""
		if self.prebaked:
			cell = self.cell_for((wx, fallback_y, wz))
			idx = self._baked_index(cell)
			if idx is not None and self._baked_heights[idx] is not None:
				return float(self._baked_heights[idx]) * 0.001
			near = self._nearest_baked_cell(cell, 4)
			if near is not None:
				n_idx = self._baked_index(near)
				if n_idx is not None and self._baked_heights[n_idx] is not None:
					return float(self._baked_heights[n_idx]) * 0.001
		mh = self.mesh3d_height(wx, wz)
		if mh is not None:
			return mh
		return float(fallback_y)

	def obstacle_relief_3d(self, wx, wz, ref_ground_y=None):
		"""Return 3D obstacle height above ground at (wx, wz) using navmesh/<map>.grid + heights_mm."""
		mh = self.mesh3d_height(wx, wz)
		if mh is None:
			return 0.0
		gy = float(ref_ground_y) if ref_ground_y is not None else self.ground_height_3d(wx, wz, mh)
		return max(0.0, mh - gy)

	def score_heading_3d(self, position, candidate_yaw, lookahead=10.5, half_width=1.65):
		"""Evaluate a 3D hull corridor & continuous elevation/slope profile along `candidate_yaw`
		(heights_mm, links, hazards, mesh3d relief, and 3D solid OBBs).
		Returns a scalar terrain/slope cost (lower is better), or None when the 3D hull corridor
		hits a fatal cliff/water hazard, solid 3D obstacle, or impassable slope/cross-tilt."""
		if not self.prebaked or position is None:
			return 0.0
		ox, oz = self._baked_origin
		size = self.cell_size
		w = self._baked_width
		h = self._baked_height
		valid = self._baked_valid
		links = self._baked_links
		hazards = self._baked_hazards
		haz_one = self._baked_hazard_near_one
		haz_two = self._baked_hazard_near_two
		heights = self._baked_heights
		sx, sy, sz = float(position[0]), float(position[1]), float(position[2])
		scx = int(math.floor((sx - ox) / size + 0.5))
		scz = int(math.floor((sz - oz) / size + 0.5))
		s_idx = (scz * w + scx) if (0 <= scx < w and 0 <= scz < h) else -1
		start_y = (float(heights[s_idx]) * 0.001) if (s_idx >= 0 and heights[s_idx] is not None) else sy
		sine = math.sin(float(candidate_yaw))
		cosine = math.cos(float(candidate_yaw))
		lat_x = cosine
		lat_z = -sine
		sh = max(0.85, min(1.30, float(half_width) * 0.65))
		max_d = max(5.5, min(14.0, float(lookahead)))
		# Fast O(1) check against cached local 3D solid OBBs (zero set allocations, zero grid marching)
		try:
			from gui.mods.offhangar.ai import bot_subengine as _bsub_nav
			if _bsub_nav.fast_heading_obb_blocked(sx, start_y, sz, sine, cosine, min(max_d, 8.0), sh * 0.80):
				return None
		except Exception:
			pass
		cost = 0.0
		prev_y = start_y
		prev_d = 0.0
		for d_step in (3.0, 6.0, 9.0, max_d):
			if d_step > max_d + 0.1 or d_step <= prev_d + 0.3:
				continue
			px = sx + sine * d_step
			pz = sz + cosine * d_step
			cx = int(math.floor((px - ox) / size + 0.5))
			cz = int(math.floor((pz - oz) / size + 0.5))
			if cx < 1 or cx >= w - 1 or cz < 1 or cz >= h - 1:
				return None
			idx = cz * w + cx
			hz = int(hazards[idx])
			if (hz & BAKED_FATAL_HAZARDS) != 0:
				return None
			if not valid[idx]:
				if d_step <= 5.8:
					return None
				cost += 2.6
				continue
			cell_y = float(heights[idx]) * 0.001 if heights[idx] is not None else prev_y
			run = max(1.0, d_step - prev_d)
			signed_slope = (cell_y - prev_y) / run
			slope_abs = abs(signed_slope)
			if signed_slope > 0.43 or signed_slope < -0.38:
				return None
			# Non-linear continuous elevation/steepness cost: bots understand gentle vs moderate vs steep slopes
			if signed_slope > 0.04:
				cost += signed_slope * 1.45 + (max(0.0, signed_slope - 0.14) ** 2) * 16.0
			elif signed_slope < -0.06:
				cost += slope_abs * 1.20 + (max(0.0, slope_abs - 0.18) ** 2) * 14.0
			else:
				cost += slope_abs * 0.85
			# Check lateral track shoulders (+-sh) for fatal hazards and lateral cross-slope tilt
			if d_step <= 7.5:
				lx_p = px + lat_x * sh; lz_p = pz + lat_z * sh
				rx_p = px - lat_x * sh; rz_p = pz - lat_z * sh
				lcx = int(math.floor((lx_p - ox) / size + 0.5))
				lcz = int(math.floor((lz_p - oz) / size + 0.5))
				rcx = int(math.floor((rx_p - ox) / size + 0.5))
				rcz = int(math.floor((rz_p - oz) / size + 0.5))
				if 0 <= lcx < w and 0 <= lcz < h and 0 <= rcx < w and 0 <= rcz < h:
					l_idx = lcz * w + lcx
					r_idx = rcz * w + rcx
					if (int(hazards[l_idx]) & BAKED_FATAL_HAZARDS) != 0 or (int(hazards[r_idx]) & BAKED_FATAL_HAZARDS) != 0:
						return None
					l_hm = heights[l_idx]; r_hm = heights[r_idx]
					if l_hm is not None and r_hm is not None:
						cross_tilt = abs(float(l_hm) - float(r_hm)) * 0.001 / (2.0 * sh)
						if cross_tilt > 0.46:
							return None
						if cross_tilt > 0.14:
							cost += (cross_tilt - 0.14) * 2.2
			l_mask = int(links[idx]) if links else 0xFF
			if l_mask == 0xFF:
				cost -= 0.12
			elif (l_mask & 0x5A) != 0x5A:
				cost += 0.38
			if haz_one and haz_one[idx]:
				cost += 1.25
			elif haz_two and haz_two[idx]:
				cost += 0.40
			prev_y = cell_y
			prev_d = d_step
		return cost

	def best_escape_yaw_3d(self, position, current_yaw, desired_yaw):
		"""Scan 24 directions (every 15 deg across 360 deg) on the 3D hull & slope grid for the best open escape heading."""
		if not self.prebaked or position is None:
			return None
		best_yaw = None
		best_score = 1e18
		for step_i in range(24):
			cand_yaw = float(desired_yaw) + (step_i * (2.0 * math.pi / 24.0))
			while cand_yaw > math.pi:
				cand_yaw -= 2.0 * math.pi
			while cand_yaw < -math.pi:
				cand_yaw += 2.0 * math.pi
			c_cost = self.score_heading_3d(position, cand_yaw, 9.5)
			if c_cost is None:
				continue
			d_des = abs(math.atan2(math.sin(cand_yaw - desired_yaw), math.cos(cand_yaw - desired_yaw)))
			d_cur = abs(math.atan2(math.sin(cand_yaw - current_yaw), math.cos(cand_yaw - current_yaw)))
			total = c_cost + d_des * 0.85 + d_cur * 0.35
			if total < best_score:
				best_score = total
				best_yaw = cand_yaw
		return best_yaw

	def best_reverse_yaw_3d(self, position, current_yaw, desired_reverse_yaw=None):
		"""Evaluate a 9-ray rear 3D fan behind `current_yaw` to find the cleanest, flattest reverse heading."""
		if not self.prebaked or position is None:
			return None
		base_rev = float(current_yaw) + math.pi
		while base_rev > math.pi:
			base_rev -= 2.0 * math.pi
		while base_rev < -math.pi:
			base_rev += 2.0 * math.pi
		ref_rev = float(desired_reverse_yaw) if desired_reverse_yaw is not None else base_rev
		best_rev = None
		best_score = 1e18
		for r_off in (0.0, 0.24, -0.24, 0.48, -0.48, 0.74, -0.74, 1.02, -1.02):
			cand_rev = base_rev + r_off
			while cand_rev > math.pi:
				cand_rev -= 2.0 * math.pi
			while cand_rev < -math.pi:
				cand_rev += 2.0 * math.pi
			c_cost = self.score_heading_3d(position, cand_rev, 7.8)
			if c_cost is None:
				continue
			d_ref = abs(math.atan2(math.sin(cand_rev - ref_rev), math.cos(cand_rev - ref_rev)))
			total = c_cost + abs(r_off) * 0.45 + d_ref * 0.35
			if total < best_score:
				best_score = total
				best_rev = cand_rev
		return best_rev

	def _baked_raw_height(self, x, z):
		if x < 0 or x >= self._baked_width or z < 0 or z >= self._baked_height:
			return 'out'
		index = z * self._baked_width + x
		value = self._baked_heights[index]
		if value is None:
			if int(self._baked_hazards[index]) & 1:
				return 'water'
			return 'solid'
		return float(value) / 1000.0

	def baked_los_blocked(self, eye, target, eye_height=2.2, target_height=1.7,
			skip_end=1.5):
		"""True when 3D terrain, a solid 3D building/wagon/rock, or a low hull-down obstacle blocks eye -> target."""
		if not self.prebaked:
			return False
		ox, oz = self._baked_origin
		size = self.cell_size
		ex = float(eye[0]); ez = float(eye[2])
		tx = float(target[0]); tz = float(target[2])
		ey = float(eye[1]) + eye_height
		ty = float(target[1]) + target_height
		dx = tx - ex; dz = tz - ez
		length = math.sqrt(dx * dx + dz * dz)
		if length < size * 1.5:
			return False
		step = min(size, self._mesh3d_step if self._mesh3d_step > 0.5 else size)
		steps = max(2, int(length / step))
		skip = max(3.5, skip_end * step)
		for i in range(1, steps):
			t = float(i) / steps
			d = t * length
			if d < skip or length - d < skip:
				continue
			x = ex + dx * t; z = ez + dz * t
			ray_y = ey + (ty - ey) * t
			mflag = self.mesh3d_flag(x, z)
			mh = self.mesh3d_height(x, z)
			if (mflag & 2) and (mh is None or mh > ray_y - 0.15):
				return True
			if (mflag & 4) and mh is not None and mh > ray_y - 0.08:
				return True
			cx = int(math.floor((x - ox) / size + 0.5))
			cz = int(math.floor((z - oz) / size + 0.5))
			h = self._baked_raw_height(cx, cz)
			if h == 'solid':
				# Solid building/rock cells ALWAYS block direct fire unless roof is strictly below ray_y
				if mh is None or (mflag & 2) or mh > ray_y - 0.15:
					return True
				return True
			if h == 'out' or h == 'water':
				continue
			if h > ray_y + 0.20:
				return True
			if mh is not None and mh > h + 1.85 and mh > ray_y + 0.15:
				return True
		return False

	def is_mountain_blocked(self, eye, target):
		"""True when a solid building, rock outcrop, mountain ridge, or cliff blocks eye->target."""
		if not self.prebaked or eye is None or target is None:
			return False
		ox, oz = self._baked_origin
		size = self.cell_size
		w = self._baked_width; h = self._baked_height
		heights = self._baked_heights; hazards = self._baked_hazards
		ex = float(eye[0]); ez = float(eye[2])
		tx = float(target[0]); tz = float(target[2])
		dx = tx - ex; dz = tz - ez
		length = math.sqrt(dx * dx + dz * dz)
		if length < 3.0:
			return False
		scx = int(math.floor((ex - ox) / size + 0.5))
		scz = int(math.floor((ez - oz) / size + 0.5))
		s_hm = heights[scz * w + scx] if (0 <= scx < w and 0 <= scz < h) else None
		ey = (float(s_hm) * 0.001 if s_hm is not None else float(eye[1])) + 2.2
		tcx = int(math.floor((tx - ox) / size + 0.5))
		tcz = int(math.floor((tz - oz) / size + 0.5))
		t_hm = heights[tcz * w + tcx] if (0 <= tcx < w and 0 <= tcz < h) else None
		ty = (float(t_hm) * 0.001 if t_hm is not None else float(target[1])) + 1.8
		steps = max(3, int(math.ceil(length / (size * 0.85))))
		skip = min(3.0, length * 0.12)
		mtn_hits = 0
		req_hits = 2 if length <= 75.0 else 3
		for i in range(1, steps):
			t = float(i) / steps
			d = t * length
			if d < skip or (length - d) < skip:
				continue
			x = ex + dx * t; z = ez + dz * t
			cx = int(math.floor((x - ox) / size + 0.5))
			cz = int(math.floor((z - oz) / size + 0.5))
			if cx < 0 or cx >= w or cz < 0 or cz >= h:
				continue
			idx = cz * w + cx
			hm = heights[idx]
			hz = int(hazards[idx])
			ray_y = ey + (ty - ey) * t
			if hm is None:
				# Non-water cells with hm is None are solid buildings, rocks, or cliffs
				if not (hz & 1):
					mtn_hits += 1
					if mtn_hits >= 1:
						return True
			else:
				if float(hm) * 0.001 > ray_y + 0.45:
					mtn_hits += 1
					if mtn_hits >= req_hits:
						return True
		return False

	def is_chord_impassable(self, start, target):
		"""True when a solid building, mountain ridge, cliff slope, OR deep water body separates start and target."""
		if not self.prebaked or start is None or target is None:
			return False
		ox, oz = self._baked_origin
		size = self.cell_size
		w = self._baked_width; h = self._baked_height
		heights = self._baked_heights; hazards = self._baked_hazards
		valid = self._baked_valid
		ex = float(start[0]); ez = float(start[2])
		tx = float(target[0]); tz = float(target[2])
		dx = tx - ex; dz = tz - ez
		length = math.sqrt(dx * dx + dz * dz)
		if length < 3.0:
			return False
		# Check 3D slope grade (tank climbing limit ~ 21 deg / 0.38)
		dy = abs(float(target[1]) - float(start[1]))
		if dy / max(0.5, length) > 0.44:
			return True
		if length < 10.0:
			if self.segment_has_baked_hazard(start, target, BAKED_FATAL_HAZARDS) or self._segment_near_baked_hazard(start, target):
				return True
			return not self.segment_clear(start, target)
		scx = int(math.floor((ex - ox) / size + 0.5))
		scz = int(math.floor((ez - oz) / size + 0.5))
		s_hm = heights[scz * w + scx] if (0 <= scx < w and 0 <= scz < h) else None
		ey = (float(s_hm) * 0.001 if s_hm is not None else float(start[1])) + 2.2
		tcx = int(math.floor((tx - ox) / size + 0.5))
		tcz = int(math.floor((tz - oz) / size + 0.5))
		t_hm = heights[tcz * w + tcx] if (0 <= tcx < w and 0 <= tcz < h) else None
		ty = (float(t_hm) * 0.001 if t_hm is not None else float(target[1])) + 1.8
		steps = max(3, int(math.ceil(length / (size * 0.85))))
		skip = min(3.0, length * 0.12)
		mtn_hits = 0
		water_hits = 0
		req_hits = 2 if length <= 75.0 else 3
		for i in range(1, steps):
			t = float(i) / steps
			d = t * length
			if d < skip or (length - d) < skip:
				continue
			x = ex + dx * t; z = ez + dz * t
			cx = int(math.floor((x - ox) / size + 0.5))
			cz = int(math.floor((z - oz) / size + 0.5))
			if cx < 0 or cx >= w or cz < 0 or cz >= h:
				continue
			idx = cz * w + cx
			hm = heights[idx]
			hz = int(hazards[idx])
			ray_y = ey + (ty - ey) * t
			if hm is None:
				if hz & 1:
					water_hits += 1
					if water_hits >= 1:
						return True
				else:
					mtn_hits += 1
					if mtn_hits >= 1:
						return True
			else:
				if hz & 1:
					water_hits += 1
					if water_hits >= 1:
						return True
				if (hz & 2) or (valid and not valid[idx]) or (float(hm) * 0.001 > ray_y + 1.8):
					mtn_hits += 1
					if mtn_hits >= req_hits:
						return True
		return False

	def find_cover(self, position, threats, max_radius=30.0, min_radius=6.0,
			home=None):
		"""Nearest reachable baked cell hidden from every threat position.

		A bounded Dijkstra over the baked links gives the real driving cost
		(around corners, not through houses). Reachable cells are ranked by
		that cost, with extra cost for moving toward the threat and a small
		bonus toward `home`; LOS is tested lazily (max 80 rays).
		Returns (x, y, z) or None.
		"""
		if not self.prebaked or not threats:
			return None
		start_cell = self._nearest_baked_cell(self.cell_for(position), 4)
		start = self._baked_index(start_cell) if start_cell is not None else None
		if start is None:
			return None
		width = self._baked_width
		size = self.cell_size
		px = float(position[0]); pz = float(position[2])
		tx = 0.0; tz = 0.0
		for threat in threats:
			tx += float(threat[0]); tz += float(threat[2])
		tx /= len(threats); tz /= len(threats)
		away_x = px - tx; away_z = pz - tz
		away_len = math.sqrt(away_x * away_x + away_z * away_z) or 1.0
		away_x /= away_len; away_z /= away_len
		home_x = home_z = 0.0
		if home is not None:
			hx = float(home[0]) - px; hz = float(home[2]) - pz
			hl = math.sqrt(hx * hx + hz * hz)
			if hl > 1.0:
				home_x = hx / hl; home_z = hz / hl
		max_radius = min(float(max_radius), 32.0)
		max_cost = max_radius * 1.55
		max_r2 = max_radius * max_radius
		min_r2 = float(min_radius) * float(min_radius)
		links = self._baked_links
		deltas = self._baked_neighbour_deltas
		near_hazard = self._baked_hazard_near_one
		heights = self._baked_heights
		start_h = float(heights[start]) * 0.001 if heights[start] is not None else float(position[1])
		start_x = start % width; start_z = start // width
		best = {start: 0.0}
		heap = [(0.0, start)]
		candidates = []
		while heap:
			cost, index = heapq.heappop(heap)
			if cost > best.get(index, 1e9):
				continue
			cx = (index % width - start_x) * size
			cz = (index // width - start_z) * size
			d2 = cx * cx + cz * cz
			if d2 >= min_r2 and not (near_hazard and near_hazard[index]):
				dist = math.sqrt(d2)
				ux = cx / dist; uz = cz / dist
				toward = -(ux * away_x + uz * away_z)
				cell_h = float(heights[index]) * 0.001 if heights[index] is not None else start_h
				# Prefer reverse-slope / defilade cells, 3D bushes (FLAG_BUSH_3D=8), and 3D hull-down rubble (FLAG_HULL_DOWN=4)
				defilade_bonus = max(-3.0, min(6.0, (start_h - cell_h) * 2.2))
				wx_c = self._baked_origin[0] + (index % width) * size
				wz_c = self._baked_origin[1] + (index // width) * size
				mf_c = self.mesh3d_flag(wx_c, wz_c)
				if mf_c & 8:
					defilade_bonus += 4.5
				if self.mesh3d_flag(wx_c - away_x * 3.0, wz_c - away_z * 3.0) & 4:
					defilade_bonus += 5.5
				score = cost * (1.0 + 0.75 * max(0.0, toward)) - defilade_bonus
				score -= dist * 0.25 * (ux * home_x + uz * home_z)
				candidates.append((score, index))
			mask = links[index]
			if not mask:
				continue
			for direction in range(8):
				if not (mask & (1 << direction)):
					continue
				next_index = index + deltas[direction]
				next_cost = cost + self._baked_edge_costs[index * 8 + direction]
				if next_cost > max_cost or next_cost >= best.get(next_index, 1e9):
					continue
				nx = (next_index % width - start_x) * size
				nz = (next_index // width - start_z) * size
				if nx * nx + nz * nz > max_r2:
					continue
				best[next_index] = next_cost
				heapq.heappush(heap, (next_cost, next_index))
		candidates.sort()
		tested = 0
		hull_down_fallback = None
		for score, index in candidates:
			if tested >= 24:
				break
			tested += 1
			cell = (index % width, index // width)
			pt_y = float(heights[index]) / 1000.0 if heights[index] is not None else float(position[1])
			point = self.point_for(cell, pt_y)
			hidden = True
			hull_hidden = True
			for threat in threats:
				if not self.baked_los_blocked(threat, point):
					hidden = False
					# Check if lower hull (pt_y - 0.85m) is hidden behind a terrain crest (hull-down!)
					if not self.baked_los_blocked(threat, (point[0], point[1] - 0.85, point[2])):
						hull_hidden = False
						break
			if hidden:
				return point
			if hull_hidden and hull_down_fallback is None:
				hull_down_fallback = point
		return hull_down_fallback

	def snap_to_safe_baked_point(self, point, max_radius=8):
		"""Ensure a synthetic tactical waypoint lies on an interior navigable baked cell without yanking narrow corridor points across the map."""
		if not self.prebaked or point is None or len(point) < 3:
			return point
		cell = self.cell_for(point)
		idx = self._baked_index(cell)
		near_haz_one = self._baked_hazard_near_one
		valid = self._baked_valid
		links = self._baked_links
		cx, cz = cell
		w = self._baked_width
		h = self._baked_height
		heights = self._baked_heights
		# If point is ALREADY on a valid hazard-free baked cell, keep it (or step at most 1 cell away if an immediate 0xFF street center is adjacent).
		# Never search r=2..14 cells away for 0xFF when already on a valid narrow road (e.g. Lakeville lake_road or archway)!
		if idx is not None and valid and valid[idx] and links and int(links[idx]) != 0 and not (near_haz_one and near_haz_one[idx]):
			cur_l = int(links[idx])
			if cur_l == 0xFF or (cur_l & 0x5A) == 0x5A:
				return point
			best_adj = None
			best_adj_d2 = 1e18
			for z in range(max(0, cz - 1), min(h, cz + 2)):
				base = z * w
				for x in range(max(0, cx - 1), min(w, cx + 2)):
					if x == cx and z == cz:
						continue
					i = base + x
					if valid[i] and int(links[i]) == 0xFF and not (near_haz_one and near_haz_one[i]):
						d2 = (x - cx) * (x - cx) + (z - cz) * (z - cz)
						if d2 < best_adj_d2:
							best_adj_d2 = d2
							best_adj = (x, z, i)
			if best_adj is not None:
				return self.point_for((best_adj[0], best_adj[1]), float(heights[best_adj[2]]) * 0.001)
			return point
		# Point is on an invalid or hazard-adjacent cell: search outward ring by ring (r = 1 .. max_radius),
		# preferring 0xFF -> 0x5A -> any valid linked cell at the nearest ring r!
		for haz_buf in (near_haz_one, None):
			for r in range(1, max(1, int(max_radius)) + 1):
				for req_mask in (0xFF, 0x5A, 0x00):
					best_cell = None
					best_d2 = 1e18
					for z in range(max(0, cz - r), min(h, cz + r + 1)):
						base = z * w
						for x in range(max(0, cx - r), min(w, cx + r + 1)):
							if max(abs(x - cx), abs(z - cz)) != r:
								continue
							i = base + x
							if not valid[i] or (links and not links[i]) or (haz_buf and haz_buf[i]):
								continue
							if req_mask and links and (int(links[i]) & req_mask) != req_mask:
								continue
							d2 = (x - cx) * (x - cx) + (z - cz) * (z - cz)
							if d2 < best_d2:
								best_d2 = d2
								best_cell = (x, z, i)
					if best_cell is not None:
						return self.point_for((best_cell[0], best_cell[1]), float(heights[best_cell[2]]) * 0.001)
		return None

	def is_safe_baked_point(self, point):
		"""Return True iff point lies on a valid linked cell not directly bordering deep water."""
		if not self.prebaked or point is None:
			return True
		cell = self.cell_for(point)
		idx = self._baked_index(cell)
		if idx is None:
			return False
		if self._baked_hazard_near_one and self._baked_hazard_near_one[idx]:
			return False
		if self._baked_links:
			l_m = int(self._baked_links[idx])
			if l_m == 0 or (l_m & (l_m - 1)) == 0:
				return False
		return True

	def near_baked_navigation(self, point, max_radius=1):
		"""Return whether a realised pose remains in the baked safe corridor."""
		if not self.prebaked:
			return True
		return self._nearest_baked_cell(
			self.cell_for(point), max(0, int(max_radius))) is not None

	def baked_hazard_near(self, point, max_radius=0):
		'''Return whether a pose is in shipped water/cliff risk, not an obstacle.'''
		if not self.prebaked or not self._baked_hazards:
			return False
		cell = self.cell_for(point)
		radius = max(0, int(max_radius))
		flat_index = self._baked_flat_index(cell)
		if flat_index is None:
			return False
		if point is not None and len(point) >= 3 and self._baked_heights:
			_hm = self._baked_heights[flat_index]
			if _hm is not None and (float(_hm) * 0.001 - float(point[1])) > 5.0:
				return False
		if int(self._baked_hazards[flat_index]) & BAKED_FATAL_HAZARDS:
			return True
		if radius == 1 and self._baked_hazard_near_one:
			return bool(self._baked_hazard_near_one[flat_index])
		if radius == 2 and self._baked_hazard_near_two:
			return bool(self._baked_hazard_near_two[flat_index])
		for z in range(cell[1] - radius, cell[1] + radius + 1):
			for x in range(cell[0] - radius, cell[0] + radius + 1):
				index = self._baked_flat_index((x, z))
				mask = BAKED_FATAL_HAZARDS
				if self._avoid_shallow_water:
					mask |= BAKED_SHALLOW_WATER
				if (index is not None and
						int(self._baked_hazards[index]) & mask):
					return True
		return False

	def _nearest_baked_cell(self, cell, max_radius):
		idx0 = self._baked_index(cell)
		if idx0 is not None and int(self._baked_links[idx0]) != 0:
			return cell
		best = None
		best_distance = None
		for radius in range(1, max(0, int(max_radius)) + 1):
			for z in range(cell[1] - radius, cell[1] + radius + 1):
				for x in range(cell[0] - radius, cell[0] + radius + 1):
					if max(abs(x - cell[0]), abs(z - cell[1])) != radius:
						continue
					candidate = (x, z)
					c_idx = self._baked_index(candidate)
					if c_idx is None:
						continue
					links = int(self._baked_links[c_idx])
					if links == 0:
						continue
					distance = ((x - cell[0]) ** 2 + (z - cell[1]) ** 2)
					if (links & 0x5A) == 0:
						distance += 4.5
					if best_distance is None or distance < best_distance:
						best = candidate
						best_distance = distance
			if best is not None:
				return best
		return None

	def _inside(self, x, z):
		if self.bounds is None:
			return True
		try:
			return (float(self.bounds[0]) <= x <= float(self.bounds[2]) and
			        float(self.bounds[1]) <= z <= float(self.bounds[3]))
		except Exception:
			# Invalid bounds must not silently disable the map-edge guard.
			return False

	def clear_negative_cache(self):
		"""Retry cells that may have missed while distant chunks streamed in."""
		for cache in (self._ground_cache, self._edge_cache):
			for key, value in list(cache.items()):
				if value is None:
					cache.pop(key, None)
		for key, value in list(self._segment_cache.items()):
			if not value:
				self._segment_cache.pop(key, None)

	def _layer(self, hint_y):
		return int(math.floor(float(hint_y) / 8.0 + 0.5))

	def _point_key(self, point):
		return (int(math.floor(float(point[0]) * 0.5 + 0.5)),
		        int(math.floor(float(point[2]) * 0.5 + 0.5)),
		        self._layer(point[1]))

	def _edge_cells_for_segment(self, start, end):
		edges = self._edge_keys_for_segment(start, end)
		return edges[0] if edges else None

	def _edge_keys_for_segment(self, start, end):
		start_cell = self.cell_for(start)
		end_cell = self.cell_for(end)
		if start_cell == end_cell:
			return ()
		x, z = start_cell
		target_x, target_z = end_cell
		dx = abs(target_x - x)
		dz = abs(target_z - z)
		step_x = 1 if x < target_x else -1
		step_z = 1 if z < target_z else -1
		error = dx - dz
		cells = [start_cell]
		while x != target_x or z != target_z:
			double_error = error * 2
			if double_error > -dz:
				error -= dz
				x += step_x
			if double_error < dx:
				error += dx
				z += step_z
			cells.append((x, z))
		return tuple(tuple(sorted((cells[index], cells[index + 1])))
		             for index in range(len(cells) - 1))

	def prune_failed_edges(self, now):
		for key, value in list(self._failed_edges.items()):
			if float(now) >= value[0]:
				self._failed_edges.pop(key, None)
		if len(self._failed_edges) > 128:
			ordered = sorted(self._failed_edges.items(), key=lambda item: item[1][0])
			for key, unused in ordered[:len(self._failed_edges) - 128]:
				self._failed_edges.pop(key, None)

	def trim_caches(self):
		for cache, limit in ((self._ground_cache, 4096),
		                     (self._edge_cache, 4096),
		                     (self._segment_cache, 4096)):
			while len(cache) > limit:
				try:
					cache.popitem()
				except Exception:
					break

	def remember_failed_segment(self, start, end, now, ttl=18.0, penalty=240.0):
		"""Penalize the first coarse edge of a route that made no progress."""
		key = self._edge_cells_for_segment(start, end)
		if key is None:
			return
		self._failed_edges[key] = (float(now) + max(1.0, float(ttl)),
		                           max(0.0, float(penalty)))
		if len(self._failed_edges) > 512:
			alive = [(edge, value) for edge, value in self._failed_edges.items()
			         if value[0] > float(now)]
			alive.sort(key=lambda item: item[1][0], reverse=True)
			self._failed_edges = dict(alive[:384])

	def _failed_edge_penalty(self, first_cell, second_cell, now):
		key = tuple(sorted((first_cell, second_cell)))
		value = self._failed_edges.get(key)
		if value is None:
			return 0.0
		if float(now) >= value[0]:
			self._failed_edges.pop(key, None)
			return 0.0
		return value[1]

	def segment_penalty(self, start, end, now):
		if not self._failed_edges:
			return 0.0
		penalty = 0.0
		for key in self._edge_keys_for_segment(start, end):
			penalty = max(penalty,
			              self._failed_edge_penalty(key[0], key[1], now))
		return penalty

	def _baked_segment_cells(self, start, end):
		"""Return graph cells crossed by a straight segment, start included."""
		if not self.prebaked:
			return ()
		start_cell = self._nearest_baked_cell(self.cell_for(start), 2)
		end_cell = self.cell_for(end)
		if start_cell is None or self._baked_index(end_cell) is None:
			return ()
		x, z = start_cell
		target_x, target_z = end_cell
		cells = [(x, z)]
		dx = abs(target_x - x)
		dz = abs(target_z - z)
		step_x = 1 if x < target_x else -1
		step_z = 1 if z < target_z else -1
		error = dx - dz
		while x != target_x or z != target_z:
			double_error = error * 2
			if double_error > -dz:
				error -= dz
				x += step_x
			if double_error < dx:
				error += dx
				z += step_z
			cells.append((x, z))
		return tuple(cells)

	def segment_has_baked_hazard(self, start, end, hazard_mask):
		"""Check cells entered by a shortcut without trapping a tank at its start."""
		if not self.prebaked:
			return False
		cells = self._baked_segment_cells(start, end)
		if not cells:
			return True
		# Exclude the start cell so a tank already on a shallow ford may leave it.
		for cell in cells[1:]:
			index = self._baked_index(cell)
			if (index is None or
					int(self._baked_hazards[index]) & int(hazard_mask)):
				return True
		return False

	def _segment_near_baked_hazard(self, start, end):
		if not self.prebaked or not self._baked_hazard_near_one:
			return False
		cells = self._baked_segment_cells(start, end)
		if not cells:
			return True
		for cell in cells[1:]:
			idx = self._baked_index(cell)
			if idx is None or self._baked_hazard_near_one[idx]:
				return True
		return False

	def baked_open_corridor(self, start, end, clearance_cells=1):
		"""Prove that a short drive lies inside a wide, static baked corridor.

		This is deliberately stricter than :meth:`segment_clear`.  It is used only
		as a fast affirmative answer for the runtime steering feeler: every crossed
		edge must be linked and every neighbouring cell within ``clearance_cells``
		must also be navigable and free of shipped water/cliff hazards.  A narrow
		road, bridge, obstacle edge, invalid start, or missing graph returns False so
		the caller can fall back to exact BigWorld collision rays.
		"""
		if not self.prebaked:
			return False
		start_cell = self.cell_for(start)
		if self._baked_index(start_cell) is None:
			return False
		cells = self._baked_segment_cells(start, end)
		if not cells:
			return False
		for old_cell, cell in zip(cells, cells[1:]):
			if self._baked_edge_height(old_cell, cell) is None:
				return False
		radius = max(0, int(clearance_cells))
		hazard_mask = BAKED_FATAL_HAZARDS
		if self._avoid_shallow_water:
			hazard_mask |= BAKED_SHALLOW_WATER
		checked = set()
		for cell in cells:
			for z in range(cell[1] - radius, cell[1] + radius + 1):
				for x in range(cell[0] - radius, cell[0] + radius + 1):
					neighbour = (x, z)
					if neighbour in checked:
						continue
					checked.add(neighbour)
					index = self._baked_index(neighbour)
					if index is None:
						return False
					if int(self._baked_hazards[index]) & hazard_mask:
						return False
		return True

	def path_has_penalty(self, path, now):
		if not self._failed_edges:
			return False
		for index in range(len(path) - 1):
			if self.segment_penalty(path[index], path[index + 1], now) > 0.0:
				return True
		return False

	def _ground(self, x, z, hint_y):
		if not self._inside(x, z):
			return None
		if self.prebaked:
			return self._baked_cell_height(self.cell_for((x, hint_y, z)))
		key = (int(math.floor(x * 10.0 + 0.5)),
		       int(math.floor(z * 10.0 + 0.5)), self._layer(hint_y))
		if key in self._ground_cache:
			return self._ground_cache[key]
		try:
			height = self.ground_probe(float(x), float(z), float(hint_y))
			if height is not None:
				height = float(height)
		except Exception:
			height = None
		self._ground_cache[key] = height
		return height

	def segment_clear(self, start, end):
		"""Check continuous support and drivable grade, not just both endpoints."""
		distance = _distance_2d(start, end)
		if distance < 0.25:
			return True
		if self.prebaked:
			dy = abs(float(end[1]) - float(start[1]))
			if dy / max(0.5, distance) > 0.44:
				return False
			cells = self._baked_segment_cells(start, end)
			if not cells:
				return False
			if len(cells) == 1:
				return True
			max_y = max(float(start[1]), float(end[1]))
			for old_cell, cell in zip(cells, cells[1:]):
				h_edge = self._baked_edge_height(old_cell, cell)
				if h_edge is None:
					return False
				if h_edge > max_y + 2.4:
					return False
			return True
		start_key = self._point_key(start)
		end_key = self._point_key(end)
		key = (start_key, end_key)
		cached = self._segment_cache.get(key)
		if cached is not None:
			return bool(cached)
		steps = max(1, int(math.ceil(distance / (self.cell_size * 0.42))))
		start_y = self._ground(float(start[0]), float(start[2]), float(start[1]))
		if start_y is None:
			start_y = float(start[1])
		previous = (float(start[0]), start_y, float(start[2]))
		grounded_start = previous
		clear = True
		for index in range(1, steps + 1):
			fraction = float(index) / float(steps)
			x = float(start[0]) + (float(end[0]) - float(start[0])) * fraction
			z = float(start[2]) + (float(end[2]) - float(start[2])) * fraction
			horizontal = math.sqrt((x - previous[0]) ** 2 + (z - previous[2]) ** 2)
			y = self._ground(x, z, previous[1])
			if y is None:
				clear = False
				break
			delta = y - previous[1]
			# Ordinary route segments must be controllable in reverse too. A
			# physically possible slide is not a valid tank-navigation shortcut.
			reversible_grade = min(self.max_grade_up, self.max_grade_down)
			if abs(delta) > horizontal * reversible_grade:
				clear = False
				break
			current = (x, y, z)
			previous = current
		if clear and self.obstacle_probe is not None:
			try:
				if self.obstacle_probe(grounded_start, previous, 2.15):
					clear = False
			except Exception:
				# Collision-query failure is unknown terrain, not proof that a
				# several-ton vehicle has a clear corridor.
				clear = False
		self._segment_cache[key] = bool(clear)
		self._segment_cache[(end_key, start_key)] = bool(clear)
		return clear

	@staticmethod
	def shortcut_preserves_climb_approach(path, start_index, end_index,
			minimum_grade=0.10, minimum_turn=0.30):
		"""Keep the setup point before a steep path changes direction.

		A collision-free chord is not always a controllable tank manoeuvre.  If a
		climb begins immediately after a bend, skipping the bend point makes the
		hull meet the slope diagonally and waste several recovery cycles.  Ordinary
		flat corners and straight climbs remain eligible for smoothing.
		"""
		if end_index - start_index < 2:
			return True
		chord_run = _distance_2d(path[start_index], path[end_index])
		if chord_run > 0.5 and abs(float(path[end_index][1]) - float(path[start_index][1])) / chord_run > 0.44:
			return False
		for index in range(start_index + 1, end_index):
			before = path[index - 1]
			pivot = path[index]
			after = path[index + 1]
			out_dx = float(after[0]) - float(pivot[0])
			out_dz = float(after[2]) - float(pivot[2])
			out_run = math.sqrt(out_dx * out_dx + out_dz * out_dz)
			if out_run <= 0.1:
				continue
			grade = abs(float(after[1]) - float(pivot[1])) / out_run
			if grade <= float(minimum_grade):
				continue
			in_dx = float(pivot[0]) - float(before[0])
			in_dz = float(pivot[2]) - float(before[2])
			if abs(in_dx) + abs(in_dz) <= 0.1:
				continue
			incoming = math.atan2(in_dx, in_dz)
			outgoing = math.atan2(out_dx, out_dz)
			turn = outgoing - incoming
			while turn > math.pi:
				turn -= math.pi * 2.0
			while turn < -math.pi:
				turn += math.pi * 2.0
			if abs(turn) > float(minimum_turn):
				return False
		return True

	def _edge(self, cell, height, next_cell):
		if self.prebaked:
			return self._baked_edge_height(cell, next_cell)
		key = (cell, next_cell, self._layer(height))
		if key in self._edge_cache:
			return self._edge_cache[key]
		start = self.point_for(cell, height)
		point = self.point_for(next_cell, height)
		x = point[0]
		z = point[2]
		end_y = self._ground(x, z, height)
		if end_y is None:
			result = None
		else:
			end = (x, end_y, z)
			result = end_y if self.segment_clear(start, end) else None
		self._edge_cache[key] = result
		return result

	def _baked_edge_height(self, cell, next_cell):
		index = self._baked_index(cell)
		next_index = self._baked_index(next_cell)
		if index is None or next_index is None:
			return None
		dx = next_cell[0] - cell[0]
		dz = next_cell[1] - cell[1]
		direction_index = self._DIRECTION_INDEX.get((dx, dz))
		if (direction_index is None or
				not (int(self._baked_links[index]) & (1 << direction_index))):
			return None
		return float(self._baked_heights[next_index]) / 1000.0

	def _penalty(self, cell, avoid_points):
		penalty = 0.0
		if self.prebaked:
			index = self._baked_index(cell)
			if (index is not None and
					int(self._baked_hazards[index]) & BAKED_SHALLOW_WATER):
				penalty += self.cell_size * BAKED_SHALLOW_WATER_PENALTY
		return penalty + self._avoid_penalty(cell, avoid_points)

	def _avoid_penalty(self, cell, avoid_points):
		if not avoid_points:
			return 0.0
		penalty = 0.0
		point = self.point_for(cell, 0.0)
		x = point[0]
		z = point[2]
		for point in avoid_points:
			dx = x - float(point[0])
			dz = z - float(point[2])
			distance = math.sqrt(dx * dx + dz * dz)
			if distance < self.cell_size * 1.5:
				penalty += (self.cell_size * 1.5 - distance) * 3.0
		return penalty

	def safe_local_target(self, current, goal, now, avoid_points=None,
			side_preference=1.0):
		"""Choose one short, fully probed detour when the global search fails.

		This is deliberately not a direct-to-goal fallback. Every candidate must
		have supported ground, a safe grade, no deep water, no static collision and
		no remembered failed edge. Returning ``None`` means the only safe action is
		to stop and retry the global planner later.
		"""
		dx = float(goal[0]) - float(current[0])
		dz = float(goal[2]) - float(current[2])
		if abs(dx) + abs(dz) < 0.1:
			return None
		desired_yaw = math.atan2(dx, dz)
		side = 1.0 if float(side_preference) >= 0.0 else -1.0
		offsets = (0.0, side * 0.45, -side * 0.45,
		           side * 0.85, -side * 0.85,
		           side * 1.30, -side * 1.30,
		           side * 1.75, -side * 1.75)
		if self.prebaked:
			# v8.8: Step at least 1.6..2.5 cells (6.4..10.0m on 4m grid) so _baked_segment_cells
			# always crosses into the destination cell and checks its links/hazards + 1-cell halo!
			distances = (max(10.0, self.cell_size * 2.5), max(6.4, self.cell_size * 1.6))
		else:
			distances = (self.cell_size * 0.78, self.cell_size * 0.52)
		best = None
		for distance in distances:
			for offset in offsets:
				yaw = desired_yaw + offset
				x = float(current[0]) + math.sin(yaw) * distance
				z = float(current[2]) + math.cos(yaw) * distance
				y = self._ground(x, z, float(current[1]))
				if y is None:
					continue
				candidate = (x, y, z)
				if (self.segment_penalty(current, candidate, now) > 0.0 or
						self.segment_has_baked_hazard(
							current, candidate, BAKED_FATAL_HAZARDS) or
						(self.prebaked and (
							self.baked_hazard_near(candidate, 1) or
							self._segment_near_baked_hazard(current, candidate))) or
						not self.segment_clear(current, candidate)):
					continue
				cell = self.cell_for(candidate)
				score = (_distance_2d(candidate, goal) + abs(offset) * 3.5 +
				         self._penalty(cell, avoid_points) * 2.0)
				value = (score, abs(offset), candidate)
				if best is None or value[:2] < best[:2]:
					best = value
		return best[2] if best is not None else None

	def plan(self, start, goal, avoid_points=None, max_expansions=1600, now=0.0):
		"""Return a supported path synchronously (mainly for tests/tools)."""
		search = self.begin_plan(start, goal, avoid_points, max_expansions, now)
		while not search.done:
			search.step(256)
		return search.result

	def trace_terrain_path(self, start, goal, max_points=8):
		"""Fast terrain-following 3D waypoint trace for radar & tactical plan preview."""
		if not self.prebaked or start is None or goal is None:
			return (tuple(goal),) if goal is not None else ()
		try:
			d2d = _distance_2d(start, goal)
			if d2d <= 22.0 and not self.segment_has_baked_hazard(start, goal, BAKED_FATAL_HAZARDS) and not self.is_chord_impassable(start, goal):
				return (tuple(goal),)
			t_cache = getattr(self, '_trace_cache', None)
			if t_cache is None:
				t_cache = {}
				self._trace_cache = t_cache
			s_c = self.cell_for(start)
			g_c = self.cell_for(goal)
			ck = (s_c[0] // 2, s_c[1] // 2, g_c[0] // 2, g_c[1] // 2)
			if ck in t_cache:
				return t_cache[ck]
			if len(t_cache) > 256:
				t_cache.clear()
			res = self.plan(start, goal, None, 2400, 0.0)
			if res and len(res) >= 2:
				pts = tuple(res[1:1 + int(max_points)])
			else:
				pts = (tuple(goal),)
			t_cache[ck] = pts
			return pts
		except Exception:
			return (tuple(goal),) if goal is not None else ()

	def begin_plan(self, start, goal, avoid_points=None, max_expansions=1600,
			now=0.0):
		if self.prebaked:
			generator = self._plan_steps_baked(
				start, goal, avoid_points, max_expansions, now)
		else:
			generator = self._plan_steps(
				start, goal, avoid_points, max_expansions, now)
		return _TerrainSearch(generator)

	def _plan_steps_baked(self, start, goal, avoid_points, max_expansions, now):
		"""Run the shipped graph with flat integer node identifiers."""
		start_cell = self._nearest_baked_cell(self.cell_for(start), 12)
		raw_goal_cell = self.cell_for(goal)
		goal_cell = self._nearest_baked_cell(raw_goal_cell, 24)
		if start_cell is not None and goal_cell == start_cell and _distance_2d(start, goal) > self.cell_size * 1.5:
			# Never snap an unbaked goal backward onto start_cell (which produces a 1-node path and freezes the bot)!
			# Find the closest valid baked cell in the forward half-plane toward goal.
			gx_d = float(raw_goal_cell[0] - start_cell[0])
			gz_d = float(raw_goal_cell[1] - start_cell[1])
			best_fwd = None
			best_fwd_d2 = 1e18
			for _r_f in range(1, 32):
				for _dz_f in range(-_r_f, _r_f + 1):
					for _dx_f in range(-_r_f, _r_f + 1):
						if max(abs(_dx_f), abs(_dz_f)) != _r_f:
							continue
						_cand_f = (raw_goal_cell[0] + _dx_f, raw_goal_cell[1] + _dz_f)
						_idx_f = self._baked_index(_cand_f)
						if _idx_f is None or not self._baked_valid[_idx_f] or _cand_f == start_cell:
							continue
						if ((_cand_f[0] - start_cell[0]) * gx_d + (_cand_f[1] - start_cell[1]) * gz_d) <= 0.0:
							continue
						_d2_f = float(_dx_f * _dx_f + _dz_f * _dz_f)
						if _d2_f < best_fwd_d2:
							best_fwd_d2 = _d2_f
							best_fwd = _cand_f
				if best_fwd is not None:
					break
			if best_fwd is not None:
				goal_cell = best_fwd
		if start_cell is None or goal_cell is None:
			yield ()
			return
		width = self._baked_width
		start_index = start_cell[1] * width + start_cell[0]
		goal_index = goal_cell[1] * width + goal_cell[0]
		frontier = []
		sequence = 0
		heapq.heappush(frontier, (0.0, sequence, start_index, 0.0))
		came_from = {}
		cost_so_far = {start_index: 0.0}
		reached = None
		closest = start_index
		closest_distance = math.sqrt(
			(start_cell[0] - goal_cell[0]) ** 2 +
			(start_cell[1] - goal_cell[1]) ** 2)
		expansions = 0
		while frontier and expansions < int(max_expansions):
			_unused_priority, _unused_sequence, current, queued_cost = heapq.heappop(frontier)
			if queued_cost != cost_so_far.get(current):
				continue
			expansions += 1
			current_x = current % width
			current_z = current // width
			goal_distance = math.sqrt(
				(current_x - goal_cell[0]) ** 2 +
				(current_z - goal_cell[1]) ** 2)
			if goal_distance < closest_distance and (not self._baked_hazard_near_one[current] or self._baked_hazard_near_one[start_index]):
				closest = current
				closest_distance = goal_distance
			if current == goal_index:
				reached = current
				break
			mask = int(self._baked_links[current])
			base_cost = cost_so_far[current]
			for direction_index, neighbour in enumerate(self._NEIGHBOURS):
				if not (mask & (1 << direction_index)):
					continue
				next_index = current + self._baked_neighbour_deltas[direction_index]
				next_x = current_x + neighbour[0]
				next_z = current_z + neighbour[1]
				new_cost = (base_cost +
				            self._baked_edge_costs[current * 8 + direction_index])
				if avoid_points:
					new_cost += self._avoid_penalty((next_x, next_z), avoid_points)
				if self._failed_edges:
					new_cost += self._failed_edge_penalty(
						(current_x, current_z), (next_x, next_z), now)
				if (next_index not in cost_so_far or
						new_cost < cost_so_far[next_index]):
					cost_so_far[next_index] = new_cost
					came_from[next_index] = current
					dx = next_x - goal_cell[0]
					dz = next_z - goal_cell[1]
					heuristic = (math.sqrt(dx * dx + dz * dz) * self.cell_size *
					             self.heuristic_weight)
					sequence += 1
					heapq.heappush(frontier, (
						new_cost + heuristic, sequence, next_index, new_cost))
			if (expansions & 31) == 0:
				yield None
		if reached is None:
			init_dist = math.sqrt((start_cell[0] - goal_cell[0]) ** 2 + (start_cell[1] - goal_cell[1]) ** 2)
			if closest_distance <= 4.0 or (closest != start_index and closest_distance < init_dist - 1.0):
				reached = closest
			elif frontier and closest != start_index:
				reached = closest
			else:
				yield ()
				return
		indices = [reached]
		while indices[-1] != start_index:
			indices.append(came_from[indices[-1]])
		indices.reverse()
		path = []
		for index in indices:
			cell = (index % width, index // width)
			path.append(self.point_for(
				cell, float(self._baked_heights[index]) / 1000.0))
		goal_y = self._ground(float(goal[0]), float(goal[2]), path[-1][1])
		goal_point = (float(goal[0]),
		              goal_y if goal_y is not None else path[-1][1],
		              float(goal[2]))
		if (not self.segment_has_baked_hazard(path[-1], goal_point, BAKED_FATAL_HAZARDS) and
				not self._segment_near_baked_hazard(path[-1], goal_point) and
				self.segment_clear(path[-1], goal_point)):
			path.append(goal_point)
		yield self._smooth(tuple(path), now)

	def _plan_steps(self, start, goal, avoid_points, max_expansions, now):
		start_cell = self.cell_for(start)
		goal_cell = self.cell_for(goal)
		if self.prebaked:
			start_cell = self._nearest_baked_cell(start_cell, 8)
			goal_cell = self._nearest_baked_cell(goal_cell, 16)
			if start_cell is None or goal_cell is None:
				yield ()
				return
			start_y = self._baked_cell_height(start_cell)
		else:
			start_y = self._ground(float(start[0]), float(start[2]), float(start[1]))
			if start_y is None:
				start_y = float(start[1])
		frontier = []
		sequence = 0
		heapq.heappush(frontier, (0.0, sequence, start_cell, 0.0))
		came_from = {}
		cost_so_far = {start_cell: 0.0}
		heights = {start_cell: start_y}
		reached = None
		closest = start_cell
		closest_distance = math.sqrt(
			(start_cell[0] - goal_cell[0]) ** 2 +
			(start_cell[1] - goal_cell[1]) ** 2)
		expansions = 0
		while frontier and expansions < int(max_expansions):
			_unused_priority, _unused_sequence, current, queued_cost = heapq.heappop(frontier)
			# A better route may have reached this cell after an older heap entry
			# was queued. Expanding stale entries repeatedly exhausted the bounded
			# search on risk-weighted maps even though every destination was linked.
			if queued_cost != cost_so_far.get(current):
				continue
			expansions += 1
			goal_distance = math.sqrt(
				(current[0] - goal_cell[0]) ** 2 +
				(current[1] - goal_cell[1]) ** 2)
			if goal_distance < closest_distance:
				closest = current
				closest_distance = goal_distance
			if current == goal_cell:
				reached = current
				break
			current_y = heights[current]
			for offset_x, offset_z, length_scale in self._NEIGHBOURS:
				next_cell = (current[0] + offset_x, current[1] + offset_z)
				next_y = self._edge(current, current_y, next_cell)
				if next_y is None:
					continue
				if offset_x and offset_z and not self.prebaked:
					# Do not squeeze diagonally across a blocked corner.
					if (self._edge(current, current_y,
					               (current[0] + offset_x, current[1])) is None or
					        self._edge(current, current_y,
					               (current[0], current[1] + offset_z)) is None):
						continue
				run = self.cell_size * length_scale
				delta_y = next_y - current_y
				slope = abs(delta_y) / max(run, 0.1)
				grade_limit = (self._baked_max_grade if self.prebaked else
				               min(self.max_grade_up, self.max_grade_down))
				slope_ratio = slope / max(0.05, grade_limit)
				# Risk rises non-linearly near the controllable-grade limit.  A
				# downhill edge costs a little more because braking and lateral
				# slide leave less recovery room than climbing the same surface.
				slope_cost = run * slope_ratio * slope_ratio * 6.0
				if delta_y < 0.0:
					slope_cost *= 1.25
				new_cost = (cost_so_far[current] + run + slope_cost +
				            self._penalty(next_cell, avoid_points) +
				            self._failed_edge_penalty(current, next_cell, now))
				if next_cell not in cost_so_far or new_cost < cost_so_far[next_cell]:
					cost_so_far[next_cell] = new_cost
					came_from[next_cell] = current
					heights[next_cell] = next_y
					dx = next_cell[0] - goal_cell[0]
					dz = next_cell[1] - goal_cell[1]
					# A modest weighted heuristic keeps the old 32-bit client from
					# exploring a broad irrelevant front around long ridges.
					heuristic = (math.sqrt(dx * dx + dz * dz) * self.cell_size *
					             self.heuristic_weight)
					sequence += 1
					heapq.heappush(frontier,
					               (new_cost + heuristic, sequence, next_cell, new_cost))
			if (expansions & 7) == 0:
				yield None
		if reached is None:
			# Sparse strategic anchors are hand placed on a minimap. A point a few
			# metres inside a building footprint, cliff lip or water edge must not
			# invalidate an otherwise complete route. Use the nearest cell A* could
			# actually reach, but only within three coarse cells: a grossly wrong
			# anchor still fails instead of silently changing battle lanes.
			if closest_distance <= 3.0:
				reached = closest
			elif frontier and closest != start_cell:
				# The bounded search still has work, so return the safest progress it
				# has already proved instead of reporting a false hard failure. The next
				# request continues from that supported partial path.
				reached = closest
			else:
				yield ()
				return
		cells = [reached]
		while cells[-1] != start_cell:
			cells.append(came_from[cells[-1]])
		cells.reverse()
		if self.prebaked:
			path = [self.point_for(start_cell, start_y)]
		else:
			path = [(float(start[0]), start_y, float(start[2]))]
		for cell in cells[1:]:
			path.append(self.point_for(cell, heights[cell]))
		goal_y = self._ground(float(goal[0]), float(goal[2]), path[-1][1])
		goal_point = (float(goal[0]), goal_y if goal_y is not None else path[-1][1],
		              float(goal[2]))
		if self.segment_clear(path[-1], goal_point):
			path.append(goal_point)
		yield self._smooth(tuple(path), now)

	def _smooth(self, path, now=0.0):
		if len(path) < 3:
			return path
		result = [path[0]]
		index = 0
		while index < len(path) - 1:
			furthest = min(len(path) - 1, index + 6)
			while furthest > index + 1:
				if (self.shortcut_preserves_climb_approach(
						path, index, furthest) and
						self.segment_penalty(path[index], path[furthest], now) <= 0.0 and
						not self.segment_has_baked_hazard(
							path[index], path[furthest], BAKED_FATAL_HAZARDS) and
						not self._segment_near_baked_hazard(path[index], path[furthest]) and
						not self.is_mountain_blocked(path[index], path[furthest]) and
						not self.is_chord_impassable(path[index], path[furthest]) and
						self.segment_clear(path[index], path[furthest])):
					break
				furthest -= 1
			result.append(path[furthest])
			index = furthest
		return tuple(result)


class _TerrainSearch(object):
	"""Small resumable A* task so collision probes are spread across frames."""

	def __init__(self, generator):
		self.generator = generator
		self.done = False
		self.result = None
		self.last_frame = None

	def step(self, budget):
		if self.done:
			return True
		for _unused in range(max(1, int(budget))):
			try:
				value = next(self.generator)
			except StopIteration:
				self.done = True
				self.result = ()
				break
			if value is not None:
				self.done = True
				self.result = value
				break
		return self.done


class TerrainNavigator(object):
	"""Shared strategic path cache plus per-bot path following and recovery."""

	def __init__(self, ground_probe, obstacle_probe=None, bounds=None,
			cell_size=18.0, baked_graph=None):
		self.grid = TerrainGrid(ground_probe, obstacle_probe, bounds, cell_size,
		                        baked_graph=baked_graph)
		self.paths = {}
		self.path_times = {}
		self.searches = {}
		self.search_times = {}
		self.bot_states = {}
		self.search_frame_time = None
		self.housekeeping_time = None
		self.search_next_key = None
		self.search_budget_per_frame = 128
		self.search_budget_per_path = 4
		self.search_time_budget = 0.0025
		# A bounded search returns its best fully-probed partial path. This keeps a
		# 29-bot room from waiting tens of seconds for 1600 expansions per job; the
		# continuation search starts after the bot reaches that safe endpoint.
		self.search_max_expansions = 128
		if self.grid.prebaked:
			self.search_budget_per_frame = 8000
			self.search_budget_per_path = 8000
			self.search_time_budget = 0.0100
			self.search_max_expansions = 8000
		self.search_completed = 0
		self.search_failed = 0
		self.search_now = 0.0
		self.fallback_totals = {
			'safe_direct': 0, 'safe_local': 0, 'reactive': 0}
		self.fallback_recovered = 0
		self.fallback_modes = {}

	def _set_fallback_mode(self, bot_id, mode):
		old_mode = self.fallback_modes.get(int(bot_id))
		if old_mode == mode:
			return
		if old_mode is not None and mode is None:
			self.fallback_recovered += 1
		if mode is None:
			self.fallback_modes.pop(int(bot_id), None)
		else:
			self.fallback_modes[int(bot_id)] = mode
			self.fallback_totals[mode] = self.fallback_totals.get(mode, 0) + 1

	def fallback_diagnostics(self, active_bot_ids=None, now=None):
		if active_bot_ids is not None:
			active_ids = set(int(value) for value in active_bot_ids)
			for bot_id in list(self.fallback_modes):
				if bot_id not in active_ids:
					self.fallback_modes.pop(bot_id, None)
		active = {'safe_direct': 0, 'safe_local': 0, 'reactive': 0}
		for mode in self.fallback_modes.values():
			active[mode] = active.get(mode, 0) + 1
		return {
			'graph': {
				'source': 'baked' if self.grid.prebaked else 'runtime',
				'cell_mm': int(round(self.grid.cell_size * 1000.0)),
				'nodes': (int(self.grid._baked_node_count)
				          if self.grid.prebaked else 0),
			},
			'total': dict(self.fallback_totals),
			'active': active,
			'recovered': int(self.fallback_recovered),
			'search': {
				'pending': len(self.searches),
				'completed': int(self.search_completed),
				'failed': int(self.search_failed),
				'oldest_ms': int(max(0.0, self.search_now -
					min(self.search_times.values())) * 1000.0)
					if self.search_times else 0,
				'tick_age_ms': int(max(0.0, float(now) - self.search_now) * 1000.0)
					if now is not None else 0,
			},
		}

	def _fallback_target(self, bot_id, current, goal, now, avoid_points, state,
			allow_safe_local=True):
		"""v8.8: Never return raw distant `tuple(goal)` across water, cliffs, or impassable terrain
		while A* is pending or failed! When A* is still in flight (allow_safe_local=False), hold
		last_target or current instead of blindly stepping toward an unrouted distant goal."""
		last_t = state.get('last_target')
		if last_t is not None and _distance_2d(current, last_t) > 2.5:
			if (not self.grid.segment_has_baked_hazard(
					current, last_t, BAKED_FATAL_HAZARDS) and
					self.grid.segment_clear(current, last_t)):
				return tuple(last_t)
		if not allow_safe_local and _distance_2d(current, goal) > 20.0:
			return tuple(current)
		fallback = self.grid.safe_local_target(
			current, goal, now, avoid_points,
			1.0 if (int(bot_id) % 2) else -1.0)
		if fallback is not None:
			state['last_target'] = tuple(fallback)
			self._set_fallback_mode(bot_id, 'safe_local')
			return tuple(fallback)
		if (not self.grid.segment_has_baked_hazard(
				current, goal, BAKED_FATAL_HAZARDS) and
				self.grid.segment_clear(current, goal)):
			state['last_target'] = tuple(goal)
			self._set_fallback_mode(bot_id, 'safe_direct')
			return tuple(goal)
		if allow_safe_local and _distance_2d(current, goal) > 2.5:
			try:
				snap_pt = self.grid.snap_to_safe_baked_point(current, 8)
				if snap_pt is not None and _distance_2d(current, snap_pt) > 2.2:
					state['last_target'] = tuple(snap_pt)
					self._set_fallback_mode(bot_id, 'safe_snap')
					return tuple(snap_pt)
				goal_yaw = math.atan2(float(goal[0]) - float(current[0]), float(goal[2]) - float(current[2]))
				esc_yaw = self.grid.best_escape_yaw_3d(current, goal_yaw, goal_yaw)
				if esc_yaw is not None:
					esc_pt = (
						float(current[0]) + math.sin(float(esc_yaw)) * 8.0,
						float(current[1]),
						float(current[2]) + math.cos(float(esc_yaw)) * 8.0)
					state['last_target'] = tuple(esc_pt)
					self._set_fallback_mode(bot_id, 'safe_escape')
					return tuple(esc_pt)
			except Exception:
				pass
		return tuple(current)

	def _cache_key(self, path_key, goal, start=None):
		if start is not None:
			return (tuple(path_key), self.grid.cell_for(start), self.grid.cell_for(goal))
		return (tuple(path_key), self.grid.cell_for(goal))

	def _trim_cache(self, now):
		if len(self.paths) <= 96:
			return
		ordered = sorted(self.path_times.items(), key=lambda item: item[1])
		for key, _timestamp in ordered[:len(ordered) - 80]:
			self.paths.pop(key, None)
			self.path_times.pop(key, None)

	def _finish_search(self, key, search, now):
		path = search.result or ()
		self.searches.pop(key, None)
		self.search_times.pop(key, None)
		self.paths[key] = path
		self.path_times[key] = float(now)
		if path:
			self.search_completed += 1
		else:
			self.search_failed += 1

	def _cancel_bot_searches(self, bot_id):
		"""Discard superseded private jobs without touching shared route plans."""
		bot_id = int(bot_id)
		for key in list(self.searches):
			try:
				path_key = key[0]
				owned = (isinstance(path_key, tuple) and len(path_key) > 1 and
				         path_key[0] in ('local', 'join', 'recovery', 'continue') and
				         int(path_key[1]) == bot_id)
			except Exception:
				owned = False
			if owned:
				self.searches.pop(key, None)
				self.search_times.pop(key, None)

	def _advance_searches(self, now):
		"""Give every pending A* task a fair share of the current frame.

		The old on-demand scheduler handed the whole frame budget to whichever bots
		were updated first. With a 29-bot room, later join searches could therefore
		remain pending forever. This rotating queue gives every task one expansion
		before any task receives a second, and remembers the next task across frames.
		"""
		self.search_now = float(now)
		if (self.search_frame_time is not None and
				abs(float(now) - self.search_frame_time) < 0.000001):
			return
		self.search_frame_time = float(now)
		keys = sorted(self.searches, key=lambda value: repr(value))
		if not keys:
			self.search_next_key = None
			return
		if self.search_next_key in keys:
			start = keys.index(self.search_next_key)
			queue = keys[start:] + keys[:start]
		else:
			queue = keys
		steps = {}
		budget = max(0, int(self.search_budget_per_frame))
		per_path = max(1, int(self.search_budget_per_path))
		# Preserve the old 32-expansion floor so one expensive probe cannot reduce
		# a lone search to one node per frame; the clock budget limits only the
		# additional burst up to the new hard cap.
		minimum_round = min(budget, 4)
		processed = 0
		started = _CLOCK()
		while budget > 0 and queue:
			key = queue.pop(0)
			search = self.searches.get(key)
			if search is None:
				continue
			search.step(1)
			search.last_frame = float(now)
			budget -= 1
			processed += 1
			steps[key] = steps.get(key, 0) + 1
			if search.done:
				self._finish_search(key, search, now)
			elif steps[key] < per_path:
				queue.append(key)
			if (processed >= minimum_round and self.search_time_budget > 0.0 and
					_CLOCK() - started >= self.search_time_budget):
				break
		self.search_next_key = queue[0] if queue else None

	def tick(self, now):
		"""Advance shared path jobs once per rendered frame, even when bots hold."""
		if (self.search_frame_time is not None and
				abs(float(now) - self.search_frame_time) < 0.000001):
			return
		self._advance_searches(now)
		# Expiry and size housekeeping does not need render-frame cadence.  The
		# former full dict walks became visible once route/contact searches filled
		# their caches, even on frames where A* itself had no useful work.
		if (self.housekeeping_time is None or
				float(now) - float(self.housekeeping_time) >= 1.0):
			self.housekeeping_time = float(now)
			self._trim_cache(now)
			self.grid.prune_failed_edges(now)
			self.grid.trim_caches()

	def _path(self, path_key, start, goal, now, avoid_points):
		key = self._cache_key(path_key, goal, start)
		if key in self.paths:
			path = self.paths[key]
			# A probe can fail while distant chunks are still streaming. Successful
			# paths are permanent for the battle; failed ones get another chance.
			if path and not self.grid.path_has_penalty(path, now):
				self.path_times[key] = float(now)
				return key, path
			if path:
				del self.paths[key]
				self.path_times.pop(key, None)
			else:
				if float(now) - self.path_times.get(key, 0.0) < 8.0:
					return key, path
				del self.paths[key]
				self.path_times.pop(key, None)
				self.grid.clear_negative_cache()
		search = self.searches.get(key)
		if search is None:
			# Most annotated segments are already open roads. Avoid invoking A*
			# when one continuous support/collision check proves the direct link.
			if (self.grid.segment_penalty(start, goal, now) <= 0.0 and
					not self.grid.segment_has_baked_hazard(
						start, goal, BAKED_FATAL_HAZARDS) and
					not self.grid._segment_near_baked_hazard(start, goal) and
					not self.grid.is_mountain_blocked(start, goal) and
					not self.grid.is_chord_impassable(start, goal) and
					self.grid.segment_clear(start, goal)):
				path = (tuple(start), tuple(goal))
				self.paths[key] = path
				self.path_times[key] = float(now)
				return key, path
			# Moving tanks do not belong in a cached static terrain path. Including all
			# 28 peers made every expansion scan transient positions, permanently baked
			# traffic into shared paths, and multiplied probe cost. LocalDriver handles
			# moving OBBs every frame; A* only owns static terrain and remembered edges.
			search = self.grid.begin_plan(
				start, goal, avoid_points=None,
				max_expansions=self.search_max_expansions, now=now)
			self.searches[key] = search
			self.search_times[key] = float(now)
			if self.grid.prebaked:
				search.step(max(8000, int(self.search_max_expansions)))
				if search.done:
					self._finish_search(key, search, now)
					return key, self.paths[key]
		self._advance_searches(now)
		if key in self.paths:
			return key, self.paths[key]
		if not search.done:
			return key, None
		# _advance_searches normally caches completed jobs. This branch only covers
		# a test double or an externally completed task.
		self._finish_search(key, search, now)
		return key, self.paths[key]

	def next_target(self, bot_id, current, goal, path_key, now,
			anchor=None, avoid_points=None):
		"""Return a terrain-safe local target, holding if no safe path is ready."""
		bot_id = int(bot_id)
		# Search progress is a navigator-wide frame task, not a cache-miss side
		# effect. Once every active bot had a cached/partial path, _path() returned
		# before advancing unrelated join and continuation jobs, leaving the whole
		# room parked with an ever-growing pending queue.
		self.tick(now)
		state = self.bot_states.get(bot_id)
		if state is None:
			state = {'last_position': tuple(current), 'progress_time': float(now),
			         'path_key': None, 'index': 0, 'recovery': 0,
			         'recovery_until': 0.0, 'recovery_key': None,
			         'recovery_start': None, 'request_key': None,
			         'request_path_key': None, 'planned_goal': None,
			         'planned_at': 0.0}
			self.bot_states[bot_id] = state
		path_identity = tuple(path_key)
		planned_goal = state.get('planned_goal')
		if (state.get('request_path_key') == path_identity and
				planned_goal is not None and
				_distance_2d(planned_goal, goal) < self.grid.cell_size * 3.5 and
				float(now) - float(state.get('planned_at', 0.0)) < 4.5):
			# A moving contact may cross a coarse cell every observation. Keep the
			# current terrain plan briefly instead of cancelling it before A* can
			# finish; aiming still uses the target's current live pose.
			goal = tuple(planned_goal)
		else:
			state['request_path_key'] = path_identity
			state['planned_goal'] = tuple(goal)
			state['planned_at'] = float(now)
		request_key = self._cache_key(path_key, goal)
		if state.get('request_key') != request_key:
			# A new route segment or combat target is not evidence that the previous
			# request stalled. Reset recovery before evaluating progress.
			self._cancel_bot_searches(bot_id)
			state['request_key'] = request_key
			state['planned_start'] = tuple(current)
			state['path_key'] = None
			state['index'] = 0
			state['last_position'] = tuple(current)
			state['progress_time'] = float(now)
			state['recovery'] = 0
			state['recovery_until'] = 0.0
			state['recovery_key'] = None
			state['recovery_start'] = None
		if _distance_2d(current, state['last_position']) >= 2.0:
			state['last_position'] = tuple(current)
			state['progress_time'] = float(now)
			state['recovery'] = 0
			state['recovery_until'] = 0.0
		# Fast lane-preserving direct pass: when current -> goal is already a clear
		# local corridor (< 48m) with no hazards, drive straight to this bot's own
		# lane-offset goal instead of routing backward through shared anchor path[0]!
		if (_distance_2d(current, goal) <= 48.0 and
				self.grid.segment_penalty(current, goal, now) <= 0.0 and
				not self.grid.segment_has_baked_hazard(
					current, goal, BAKED_FATAL_HAZARDS) and
				not self.grid._segment_near_baked_hazard(current, goal) and
				not self.grid.is_mountain_blocked(current, goal) and
				not self.grid.is_chord_impassable(current, goal) and
				self.grid.segment_clear(current, goal)):
			state['last_target'] = tuple(goal)
			state['active_path'] = (tuple(goal),)
			self._set_fallback_mode(bot_id, None)
			return tuple(goal)
		# v12.2 Fix for combat-mode 20m circle bug:
		# Check if we ALREADY have an active cached path for this request_key BEFORE calling self._path
		# with a moving `current` start cell! Otherwise every 4m of movement changes `cell_for(current)`,
		# starts a new A* search from scratch, and drops into safe_local_target wall-circling!
		active_key = state.get('path_key')
		active_path = self.paths.get(active_key) if active_key is not None else None
		if active_path and not self.grid.path_has_penalty(active_path, now):
			key = active_key
			path = active_path
			self.path_times[key] = float(now)
		else:
			if anchor is not None and _distance_2d(current, anchor) <= 18.0:
				plan_start = (float(anchor[0]), float(current[1]), float(anchor[2]))
			else:
				_p_start = state.get('planned_start')
				if _p_start is not None and _distance_2d(current, _p_start) <= 24.0:
					plan_start = (float(_p_start[0]), float(current[1]), float(_p_start[2]))
				else:
					plan_start = tuple(current)
					state['planned_start'] = plan_start
			effective_key = tuple(path_key)
			key, path = self._path(effective_key, plan_start, goal, now, None)
			if path is None:
				if (self.grid.segment_penalty(current, goal, now) <= 0.0 and
						not self.grid.segment_has_baked_hazard(
							current, goal, BAKED_FATAL_HAZARDS) and
						not self.grid.is_mountain_blocked(current, goal) and
						not self.grid.is_chord_impassable(current, goal) and
						self.grid.segment_clear(current, goal)):
					state['last_target'] = tuple(goal)
					self._set_fallback_mode(bot_id, 'safe_direct')
					return tuple(goal)
				return self._fallback_target(
					bot_id, current, goal, now, avoid_points, state, False)
			if not path:
				if (self.grid.segment_penalty(current, goal, now) <= 0.0 and
						not self.grid.segment_has_baked_hazard(
							current, goal, BAKED_FATAL_HAZARDS) and
						not self.grid.is_mountain_blocked(current, goal) and
						not self.grid.is_chord_impassable(current, goal) and
						self.grid.segment_clear(current, goal)):
					state['last_target'] = tuple(goal)
					self._set_fallback_mode(bot_id, 'safe_direct')
					return tuple(goal)
				state['path_key'] = key
				return self._fallback_target(
					bot_id, current, goal, now, avoid_points, state, True)
		if state.get('path_key') != key:
			state['path_key'] = key
			state['index'] = 0
			best_index = 0
			best_distance = 1e18
			for index, point in enumerate(path):
				distance = _distance_2d(current, point)
				if distance < best_distance:
					best_distance = distance
					best_index = index
			state['index'] = best_index
		index = min(int(state.get('index', 0)), len(path) - 1)
		if (self.grid.segment_penalty(current, path[index], now) > 0.0 or
				not self.grid.segment_clear(current, path[index])):
			join_key = ('join', bot_id, self.grid.cell_for(current)) + tuple(path_key)
			key, joined_path = self._path(join_key, current, goal, now, avoid_points)
			if joined_path is None:
				return self._fallback_target(
					bot_id, current, goal, now, avoid_points, state, False)
			if not joined_path:
				# The cached strategic path is unusable from this hull's actual
				# position and the join search has conclusively failed. Reuse the
				# same fully probed short fallback as a failed global search; if no
				# safe candidate exists, remain stopped and retry.
				state['path_key'] = key
				return self._fallback_target(
					bot_id, current, goal, now, avoid_points, state, True)
			path = joined_path
			state['path_key'] = key
			state['index'] = 0
			index = 0
		reach_radius = min(10.0, max(6.5, self.grid.cell_size * 1.65))
		_max_skip = max(3, int(round(24.0 / max(1.0, float(self.grid.cell_size)))))
		while index + 1 < len(path):
			_d_pi = _distance_2d(current, path[index])
			# v12.5 Anti-Deadlock: Once within reach_radius of path[index], ALWAYS advance to path[index + 1]
			# (path[index] -> path[index + 1] is already a validated A* edge; blocking index advance here
			# returned path[0] at dist=2.3..2.5m and locked LocalDriver in 'arrived' forever!).
			if _d_pi < reach_radius:
				index += 1
				continue
			_passed_pi = False
			if _d_pi < 28.0:
				_sx = float(path[index + 1][0]) - float(path[index][0])
				_sz = float(path[index + 1][2]) - float(path[index][2])
				_rx = float(current[0]) - float(path[index][0])
				_rz = float(current[2]) - float(path[index][2])
				if (_rx * _sx + _rz * _sz) >= 0.0:
					_passed_pi = True
			if (_passed_pi and
					self.grid.segment_penalty(current, path[index + 1], now) <= 0.0 and
					not self.grid.segment_has_baked_hazard(
						current, path[index + 1], BAKED_FATAL_HAZARDS) and
					not self.grid._segment_near_baked_hazard(current, path[index + 1]) and
					not self.grid.is_mountain_blocked(current, path[index + 1]) and
					not self.grid.is_chord_impassable(current, path[index + 1]) and
					self.grid.segment_clear(current, path[index + 1])):
				index += 1
			else:
				break
		# Look ahead only while every skipped piece is continuously supported and away from water/cliffs.
		lookahead = index
		for candidate in range(index + 1, min(len(path), index + 1 + _max_skip)):
			if (self.grid.shortcut_preserves_climb_approach(
					path, index, candidate) and
					self.grid.segment_penalty(current, path[candidate], now) <= 0.0 and
					not self.grid.segment_has_baked_hazard(
						current, path[candidate], BAKED_FATAL_HAZARDS) and
					not self.grid._segment_near_baked_hazard(current, path[candidate]) and
					not self.grid.is_mountain_blocked(current, path[candidate]) and
					not self.grid.is_chord_impassable(current, path[candidate]) and
					self.grid.segment_clear(current, path[candidate])):
				lookahead = candidate
			else:
				break
		while lookahead + 1 < len(path) and _distance_2d(current, path[lookahead]) <= 5.2:
			lookahead += 1
		if (lookahead == len(path) - 1 and
				_distance_2d(current, path[lookahead]) < reach_radius and
				_distance_2d(path[lookahead], goal) > reach_radius):
			# A bounded A* may return a safe partial path. Reaching that endpoint
			# means "continue planning from here", not "the strategic goal is
			# complete" and not "wait four seconds until stall recovery".
			continue_key = (('continue', bot_id, self.grid.cell_for(current)) +
			                tuple(path_key))
			next_key, continued = self._path(
				continue_key, current, goal, now, avoid_points)
			if continued:
				path = continued
				state['path_key'] = next_key
				next_index = 0
				for candidate in range(1, min(len(path), 1 + _max_skip)):
					if (self.grid.segment_penalty(current, path[candidate], now) <= 0.0 and
							not self.grid.segment_has_baked_hazard(
								current, path[candidate], BAKED_FATAL_HAZARDS) and
							not self.grid._segment_near_baked_hazard(current, path[candidate]) and
							not self.grid.is_mountain_blocked(current, path[candidate]) and
							not self.grid.is_chord_impassable(current, path[candidate]) and
							self.grid.segment_clear(current, path[candidate])):
						next_index = candidate
					else:
						break
				while next_index + 1 < len(path) and _distance_2d(current, path[next_index]) <= 5.2:
					next_index += 1
				_cont_sel = tuple(path[next_index])
				if _distance_2d(current, _cont_sel) <= 4.5 and _distance_2d(current, goal) > 7.5:
					return self._fallback_target(
						bot_id, current, goal, now, avoid_points, state, True)
				state['index'] = next_index
				state['last_target'] = _cont_sel
				return _cont_sel
			if continued is None:
				return self._fallback_target(
					bot_id, current, goal, now, avoid_points, state, False)
			return self._fallback_target(
				bot_id, current, goal, now, avoid_points, state, True)
		selected = tuple(path[lookahead])
		if (lookahead == len(path) - 1 and
				_distance_2d(selected, goal) <= 12.0 and
				not self.grid.is_mountain_blocked(current, goal) and
				not self.grid.is_chord_impassable(current, goal) and
				self.grid.segment_clear(current, goal)):
			selected = tuple(goal)
		if (_distance_2d(current, selected) <= 4.5 and
				_distance_2d(current, goal) > 7.5):
			# A cached path whose first usable edge has become blocked must not park
			# the hull on its own position until the four-second stall timer fires.
			return self._fallback_target(
				bot_id, current, goal, now, avoid_points, state, True)
		state['index'] = lookahead
		state['last_target'] = selected
		_ap = [selected]
		for _pi in range(lookahead + 1, min(len(path), lookahead + 16)):
			_ap.append(tuple(path[_pi]))
		if _distance_2d(_ap[-1], goal) > 8.0 and len(_ap) < 16 and not self.grid.is_chord_impassable(_ap[-1], goal):
			_ap.append(tuple(goal))
		state['active_path'] = tuple(_ap)
		self._set_fallback_mode(bot_id, None)
		return selected

	def active_path_for(self, bot_id):
		try:
			state = self.bot_states.get(int(bot_id))
			if state is None:
				return ()
			return state.get('active_path') or ()
		except Exception:
			return ()

	@staticmethod
	def navigation_paused(current, requested_goal, selected_target,
			minimum_request_distance=15.0, hold_radius=0.5):
		"""True when pathfinding intentionally returned the current position."""
		return (_distance_2d(current, requested_goal) > float(minimum_request_distance) and
		        _distance_2d(current, selected_target) <= float(hold_radius))
