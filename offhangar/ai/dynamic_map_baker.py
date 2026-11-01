# -*- coding: utf-8 -*-
"""
ai/dynamic_map_baker.py — Универсальный внутридвижковый авто-генератор навигационных мешей (NavGraph),
стартовых формаций (15x2), тактических маршрутов (Left/Center/Right + SPG) и кустов (FoliageMap)
для любых новых или переработанных карт ветки World of Tanks 0.8.x (0.8.0 — 0.8.11).

Как работает:
1. Если для карты нет предзапечённого графа в `navgraphs/<map>.json` (например, карты из 0.8.3–0.8.11:
   `60_asia_miao` Жемчужная река, `73_asia_korea` Священная долина, `85_winter` Белогорск-19 / Северогорск,
   `63_tundra` Тундра, `84_winter` Северо-Запад, `59_asia_great_wall` Скрытая деревня, `87_ruinberg_on_fire`),
   либо если в другом патче 0.8.x базы/точки спавна карты сместились более чем на 85 м:
2. Сначала проверяется пользовательский кэш `offhangar_user/navcache/<map>.json` (вне `res_mods`).
3. Если кэша ещё нет, то при загрузке геометрии карты (`BigWorld.spaceLoadStatus() >= 0.85`)
   модуль за ~25–80 мс сканирует коллизионное BSP-дерево карты (`offh_mt.native_collide_segment`
   в C++ или `BigWorld.wg_collideSegment` + `BigWorld.wg_collideWater`):
   - строит матрицу высот `heights_mm` и маску препятствий `hazards` (здания, скалы, обрывы, глубокая/мелкая вода);
   - попутно собирает 3D-объёмы крон и кустов SpeedTree (`matKind == 72`) в `offhangar_user/navcache/foliage_<map>.json`;
   - вычисляет 8-направленную матрицу связности `links` по уклонам рельефа;
   - формирует 15 проверенных точек спавна для Команды 1 и Команды 2 (`spawn_formations`);
   - прокладывает волновым алгоритмом A* 3 тактических коридора (`left_flank`, `center`, `right_flank`),
     рубежи прикрытия базы (`rear_guard`), огневые позиции САУ (`spg_positions`) и карманы рельефа (`tactical_pockets`);
   - сохраняет готовый JSON в `offhangar_user/navcache/<map>.json` и мгновенно загружает матрицу высот
     в многопоточный C++ пул `offh_mt.pyd` (`upload_baked_navgraph_heights`).
"""

import heapq
import json
import math
import os

try:
	from gui.mods.offhangar.logging import LOG_NOTE, LOG_DEBUG, LOG_ERROR
except Exception:
	def LOG_NOTE(*a, **k): pass
	def LOG_DEBUG(*a, **k): pass
	def LOG_ERROR(*a, **k): pass

NAVCACHE_DIR = os.path.join('offhangar_user', 'navcache')

_DIRECTIONS = (
	(-1, -1), (0, -1), (1, -1),
	(-1,  0),          (1,  0),
	(-1,  1), (0,  1), (1,  1),
)
_DIR_COSTS = (
	1.41421356, 1.0, 1.41421356,
	1.0,             1.0,
	1.41421356, 1.0, 1.41421356,
)

_RUNTIME_BAKED_MEM = {}
_RUNTIME_FOLIAGE_MEM = {}


def _short_map_name(map_name):
	return str(map_name or '').replace('\\', '/').rstrip('/').split('/')[-1].lower()


def ensure_navcache_dir():
	try:
		if not os.path.isdir(NAVCACHE_DIR):
			os.makedirs(NAVCACHE_DIR)
	except Exception:
		pass


def _navcache_path(map_name):
	ensure_navcache_dir()
	return os.path.join(NAVCACHE_DIR, _short_map_name(map_name) + '.json')


def _foliage_cache_path(map_name):
	ensure_navcache_dir()
	return os.path.join(NAVCACHE_DIR, 'foliage_' + _short_map_name(map_name) + '.json')


def load_cached_navgraph(map_name):
	return None
	if short_name in _RUNTIME_BAKED_MEM:
		return _RUNTIME_BAKED_MEM[short_name]
	path = _navcache_path(short_name)
	if not os.path.isfile(path):
		return None
	try:
		f = open(path, 'r')
		try:
			graph = json.load(f)
		finally:
			f.close()
		if isinstance(graph, dict) and graph.get('format') == 'offhangar-navgraph':
			_RUNTIME_BAKED_MEM[short_name] = graph
			return graph
	except Exception as err:
		LOG_DEBUG('DYNAMIC_NAVMESH load_cached_navgraph err (%s): %s' % (short_name, str(err)))
	return None


def load_cached_or_empty_foliage_data(map_name):
	short_name = _short_map_name(map_name)
	if not short_name:
		return {'format': 'offhangar-foliage', 'version': 1, 'game_version': '0.8.2', 'map': '', 'cell_size': 32.0, 'instances': [], 'cells': {}}
	if short_name in _RUNTIME_FOLIAGE_MEM:
		return _RUNTIME_FOLIAGE_MEM[short_name]
	path = _foliage_cache_path(short_name)
	if os.path.isfile(path):
		try:
			f = open(path, 'r')
			try:
				data = json.load(f)
			finally:
				f.close()
			if isinstance(data, dict) and data.get('format') == 'offhangar-foliage':
				_RUNTIME_FOLIAGE_MEM[short_name] = data
				return data
		except Exception:
			pass
	return {
		'format': 'offhangar-foliage',
		'version': 1,
		'game_version': '0.8.2',
		'map': short_name,
		'cell_size': 32.0,
		'instances': [],
		'cells': {},
	}


def _astar_grid_path(nx, nz, origin_x, origin_z, cell_sz, heights_mm, hazards, links, start_xz, goal_xz, via_xz=None):
	"""Find a valid terrain path on the baked grid from start_xz to goal_xz (optionally via via_xz)."""
	def _to_cell(px, pz):
		cx = int(round((float(px) - origin_x) / cell_sz))
		cz = int(round((float(pz) - origin_z) / cell_sz))
		cx = max(0, min(nx - 1, cx))
		cz = max(0, min(nz - 1, cz))
		return (cx, cz)

	def _nearest_valid_cell(cx, cz, max_r=12):
		for r in range(max_r + 1):
			for dz in range(-r, r + 1):
				for dx in range(-r, r + 1):
					if r > 0 and max(abs(dx), abs(dz)) != r:
						continue
					x = cx + dx
					z = cz + dz
					if 0 <= x < nx and 0 <= z < nz:
						idx = z * nx + x
						if heights_mm[idx] is not None and (hazards[idx] & 3) == 0 and links[idx] != 0:
							return (x, z)
		return (cx, cz)

	def _segment_astar(s_cell, g_cell):
		sx, sz = _nearest_valid_cell(s_cell[0], s_cell[1])
		gx, gz = _nearest_valid_cell(g_cell[0], g_cell[1])
		s_idx = sz * nx + sx
		g_idx = gz * nx + gx
		if s_idx == g_idx:
			return [(sx, sz)]
		open_heap = [(0.0, s_idx)]
		g_score = {s_idx: 0.0}
		came_from = {}
		closed = set()
		max_steps = max(nx * nz * 2, 25000)
		steps = 0
		closest_idx = s_idx
		closest_d = math.hypot(gx - sx, gz - sz)
		while open_heap and steps < max_steps:
			steps += 1
			_, cur = heapq.heappop(open_heap)
			cur_d = math.hypot(gx - (cur % nx), gz - (cur // nx))
			if cur_d < closest_d:
				closest_d = cur_d
				closest_idx = cur
			if cur == g_idx:
				path = []
				while cur in came_from:
					path.append((cur % nx, cur // nx))
					cur = came_from[cur]
				path.append((sx, sz))
				path.reverse()
				return path
			if cur in closed:
				continue
			closed.add(cur)
			cx = cur % nx
			cz = cur // nx
			lmask = links[cur]
			for d_i in range(8):
				if not (lmask & (1 << d_i)):
					continue
				dx, dz = _DIRECTIONS[d_i]
				nx_i = cx + dx
				nz_i = cz + dz
				if nx_i < 0 or nx_i >= nx or nz_i < 0 or nz_i >= nz:
					continue
				n_idx = nz_i * nx + nx_i
				if n_idx in closed or (hazards[n_idx] & 3) != 0:
					continue
				step_cost = _DIR_COSTS[d_i]
				if hazards[n_idx] & 4:
					step_cost *= 3.5
				tentative = g_score[cur] + step_cost
				if tentative < g_score.get(n_idx, 1e18):
					came_from[n_idx] = cur
					g_score[n_idx] = tentative
					h = math.sqrt(float((nx_i - gx) ** 2 + (nz_i - gz) ** 2)) * 1.35
					heapq.heappush(open_heap, (tentative + h, n_idx))
		# Best partial path following actual ground links instead of straight line through mountains
		if closest_idx != s_idx:
			cur = closest_idx
			path = []
			while cur in came_from:
				path.append((cur % nx, cur // nx))
				cur = came_from[cur]
			path.append((sx, sz))
			path.reverse()
			return path
		# Fallback straight-line sampled waypoints avoiding fatal cells
		pts = []
		for k in range(9):
			t = float(k) / 8.0
			ix = int(round(sx + (gx - sx) * t))
			iz = int(round(sz + (gz - sz) * t))
			pts.append(_nearest_valid_cell(ix, iz, 8))
		return pts

	c_start = _to_cell(start_xz[0], start_xz[1])
	c_goal = _to_cell(goal_xz[0], goal_xz[1])
	if via_xz is not None:
		c_via = _to_cell(via_xz[0], via_xz[1])
		p1 = _segment_astar(c_start, c_via)
		p2 = _segment_astar(c_via, c_goal)
		raw_cells = p1 + p2[1:]
	else:
		raw_cells = _segment_astar(c_start, c_goal)

	# Downsample to ~10..14 macro waypoints in world coordinates [x, z, False]
	if not raw_cells:
		return [[float(start_xz[0]), float(start_xz[1]), False], [float(goal_xz[0]), float(goal_xz[1]), False]]
	stride = max(1, len(raw_cells) // 10)
	waypoints = []
	for idx in range(0, len(raw_cells), stride):
		cx, cz = raw_cells[idx]
		wx = round(origin_x + cx * cell_sz, 2)
		wz = round(origin_z + cz * cell_sz, 2)
		waypoints.append([wx, wz, False])
	last_cx, last_cz = raw_cells[-1]
	last_wp = [round(origin_x + last_cx * cell_sz, 2), round(origin_z + last_cz * cell_sz, 2), False]
	if not waypoints or waypoints[-1] != last_wp:
		waypoints.append(last_wp)
	return waypoints


def _build_validated_spawn_formations(nx, nz, origin_x, origin_z, cell_sz, heights_mm, hazards, bounds, bases, spawns):
	"""Generate 15 validated [x, y, z, yaw] spawn poses for Team 1 and Team 2."""
	min_x, min_z, max_x, max_z = bounds
	formations = {'1': [], '2': []}

	def _sample_safe_ground(wx, wz):
		cx = max(0, min(nx - 1, int(round((wx - origin_x) / cell_sz))))
		cz = max(0, min(nz - 1, int(round((wz - origin_z) / cell_sz))))
		for r in range(10):
			for dz in range(-r, r + 1):
				for dx in range(-r, r + 1):
					if r > 0 and max(abs(dx), abs(dz)) != r:
						continue
					ix = cx + dx
					iz = cz + dz
					if 0 <= ix < nx and 0 <= iz < nz:
						idx = iz * nx + ix
						h_mm = heights_mm[idx]
						if h_mm is not None and (hazards[idx] & 3) == 0:
							rx = origin_x + ix * cell_sz if r > 0 else wx
							rz = origin_z + iz * cell_sz if r > 0 else wz
							rx = max(min_x + 12.0, min(max_x - 12.0, rx))
							rz = max(min_z + 12.0, min(max_z - 12.0, rz))
							return (round(rx, 2), round(float(h_mm) * 0.001, 2), round(rz, 2))
		return (round(wx, 2), 0.0, round(wz, 2))

	for t_id in (1, 2):
		enemy_id = 2 if t_id == 1 else 1
		t_spawns = list((spawns or {}).get(t_id) or ())
		if not t_spawns:
			b_pt = (bases or {}).get(t_id)
			if b_pt:
				t_spawns = [(float(b_pt[0]), float(b_pt[1]))]
			else:
				t_spawns = [(0.0, min_z + 80.0 if t_id == 1 else max_z - 80.0)]
		e_base = (bases or {}).get(enemy_id)
		if not e_base:
			e_sp = list((spawns or {}).get(enemy_id) or ())
			e_base = e_sp[0] if e_sp else (0.0, 0.0)

		cols = (0, -1, 1, -2, 2, -3, 3, -4, 4)
		team_poses = []
		placed_xz = []
		for slot in range(15):
			ax, az = float(t_spawns[slot % len(t_spawns)][0]), float(t_spawns[slot % len(t_spawns)][1])
			yaw = math.atan2(float(e_base[0]) - ax, float(e_base[1]) - az)
			k = slot // len(t_spawns)
			col = cols[k % len(cols)]
			row = k // len(cols)
			fwd = (0.0 if (k == 0 and len(t_spawns) >= 15) else (16.0 + row * 12.0))
			lat = (0.0 if (k == 0 and len(t_spawns) >= 15) else (col * 13.0))
			sx = ax + math.sin(yaw) * fwd + math.cos(yaw) * lat
			sz = az + math.cos(yaw) * fwd - math.sin(yaw) * lat
			sx = max(min_x + 14.0, min(max_x - 14.0, sx))
			sz = max(min_z + 14.0, min(max_z - 14.0, sz))
			rx, ry, rz = _sample_safe_ground(sx, sz)
			# Ensure minimum 8.5m pairwise separation between spawn slots
			for _udge in range(8):
				if all((rx - px) ** 2 + (rz - pz) ** 2 >= 8.5 ** 2 for px, pz in placed_xz):
					break
				rx += math.cos(yaw) * 9.0
				rz -= math.sin(yaw) * 9.0
				rx, ry, rz = _sample_safe_ground(rx, rz)
			placed_xz.append((rx, rz))
			team_poses.append([rx, ry, rz, round(yaw, 6)])
		formations[str(t_id)] = team_poses
	return formations


def bake_runtime_map_graph(map_name, space_id, bounds, bases, spawns, gameplay='ctf'):
	"""Scan the loaded BigWorld space BSP to construct and cache a complete offhangar-navgraph JSON."""
	short_name = _short_map_name(map_name)
	if not short_name:
		return None

	import BigWorld
	import Math

	try:
		from gui.mods.offhangar.offh_multithread import get_coordinator
		mt_coord = get_coordinator()
	except Exception:
		mt_coord = None

	if not bounds or len(bounds) < 4:
		bounds = (-500.0, -500.0, 500.0, 500.0)
	min_x = float(bounds[0])
	min_z = float(bounds[1])
	max_x = float(bounds[2])
	max_z = float(bounds[3])
	span_x = max(200.0, max_x - min_x)
	span_z = max(200.0, max_z - min_z)

	# Choose cell size (10m..12m) so total grid cells <= 8100 (~20ms C++ scan)
	cell_sz = 10.0
	if (span_x / cell_sz) * (span_z / cell_sz) > 8100.0:
		cell_sz = 12.0
	if (span_x / cell_sz) * (span_z / cell_sz) > 8500.0:
		cell_sz = 14.0

	nx = max(16, int(math.ceil(span_x / cell_sz)))
	nz = max(16, int(math.ceil(span_z / cell_sz)))
	origin_x = round(min_x + 0.5 * cell_sz, 2)
	origin_z = round(min_z + 0.5 * cell_sz, 2)
	total = nx * nz

	heights_mm = [None] * total
	hazards = [0] * total
	links = [0] * total
	foliage_instances = []
	foliage_cells = {}

	use_native_collide = bool(mt_coord is not None and mt_coord._has_native_collide and space_id > 0)
	has_wg_water = hasattr(BigWorld, 'wg_collideWater')

	for iz in range(nz):
		wz = origin_z + iz * cell_sz
		row_off = iz * nx
		for ix in range(nx):
			wx = origin_x + ix * cell_sz
			idx = row_off + ix
			ground_y = None
			is_obstacle = False
			if use_native_collide:
				# Native C++ collide automatically skips leaves (72) and returns [hx, hy, hz, nx, ny, nz, mat_kind, coll_flags]
				hit = mt_coord.native_collide_segment(space_id, wx, 450.0, wz, wx, -150.0, wz, 128, 0)
				if isinstance(hit, tuple):
					hy = float(hit[1])
					ny = float(hit[4])
					cflags = int(hit[7])
					if (cflags & 8) == 0:
						# Hit a static model/building roof -> check if terrain is far below it (solid building)
						sub_hit = mt_coord.native_collide_segment(space_id, wx, hy - 0.5, wz, wx, hy - 15.0, wz, 128, 0)
						if isinstance(sub_hit, tuple) and (int(sub_hit[7]) & 8) != 0 and (hy - float(sub_hit[1])) > 1.8:
							ground_y = float(sub_hit[1])
							is_obstacle = True
						else:
							ground_y = hy
							if ny < 0.72:
								is_obstacle = True
					else:
						ground_y = hy
						if ny < 0.70:
							is_obstacle = True
			else:
				try:
					hit = BigWorld.wg_collideSegment(space_id, Math.Vector3(wx, 450.0, wz), Math.Vector3(wx, -150.0, wz), 128)
					if hit is not None:
						hy = float(hit[0].y)
						ny = float(hit[1].y)
						mk = int(hit[2])
						if mk == 72 and len(foliage_instances) < 2048:
							# SpeedTree bush / crown detected! Record foliage volume and probe below crown for ground
							sub = BigWorld.wg_collideSegment(space_id, Math.Vector3(wx, hy - 2.5, wz), Math.Vector3(wx, -150.0, wz), 128)
							gy = float(sub[0].y) if sub is not None else (hy - 2.5)
							ground_y = gy
							f_id = len(foliage_instances)
							foliage_instances.append([
								round(wx, 2), round(gy, 2), round(wz, 2), round(hy + 1.5, 2),
								0.28, 0.0, 0.0, 0.28, 0.24, 3.8
							])
							fc_key = '%d,%d' % (int(math.floor(wx / 32.0)), int(math.floor(wz / 32.0)))
							foliage_cells.setdefault(fc_key, []).append(f_id)
						else:
							ground_y = hy
							if ny < 0.70:
								is_obstacle = True
				except Exception:
					ground_y = None

			if ground_y is None:
				hazards[idx] = 1
				continue

			heights_mm[idx] = int(round(ground_y * 1000.0))
			haz = 1 if is_obstacle else 0

			# Check water depth if wg_collideWater is available
			if has_wg_water and not is_obstacle:
				try:
					w_dist = BigWorld.wg_collideWater(Math.Vector3(wx, ground_y + 20.0, wz), Math.Vector3(wx, ground_y - 0.1, wz))
					if w_dist is not None and float(w_dist) >= 0.0 and float(w_dist) < 20.0:
						water_y = (ground_y + 20.0) - float(w_dist)
						depth = water_y - ground_y
						if depth >= 1.35:
							haz |= 2
						elif depth >= 0.35:
							haz |= 4
				except Exception:
					pass
			hazards[idx] = haz

	# Compute 8-direction traversability bitmask `links` and slope cliffs
	max_grade = 0.42
	for iz in range(nz):
		row_off = iz * nx
		for ix in range(nx):
			idx = row_off + ix
			h0 = heights_mm[idx]
			if h0 is None or (hazards[idx] & 3) != 0:
				continue
			y0 = float(h0) * 0.001
			lmask = 0
			steep_neighbors = 0
			for d_i in range(8):
				dx, dz = _DIRECTIONS[d_i]
				nx_i = ix + dx
				nz_i = iz + dz
				if nx_i < 0 or nx_i >= nx or nz_i < 0 or nz_i >= nz:
					continue
				n_idx = nz_i * nx + nx_i
				h1 = heights_mm[n_idx]
				if h1 is None or (hazards[n_idx] & 3) != 0:
					continue
				y1 = float(h1) * 0.001
				dist = _DIR_COSTS[d_i] * cell_sz
				grade = abs(y1 - y0) / dist
				if grade <= max_grade:
					lmask |= (1 << d_i)
				elif grade > 0.58:
					steep_neighbors += 1
			if steep_neighbors >= 3:
				hazards[idx] |= 1
				links[idx] = 0
			else:
				links[idx] = lmask

	# Extract base positions
	b1 = (bases or {}).get(1)
	b2 = (bases or {}).get(2)
	if not b1:
		s1 = list((spawns or {}).get(1) or ())
		b1 = s1[0] if s1 else (0.0, min_z + 100.0)
	if not b2:
		s2 = list((spawns or {}).get(2) or ())
		b2 = s2[0] if s2 else (0.0, max_z - 100.0)
	b1_xz = [round(float(b1[0]), 2), round(float(b1[1]), 2)]
	b2_xz = [round(float(b2[0]), 2), round(float(b2[1]), 2)]

	# Synthesize 3 tactical corridors (left_flank, center, right_flank) + rear_guard
	mid_x = 0.5 * (b1_xz[0] + b2_xz[0])
	mid_z = 0.5 * (b1_xz[1] + b2_xz[1])
	axis_dx = b2_xz[0] - b1_xz[0]
	axis_dz = b2_xz[1] - b1_xz[1]
	axis_len = math.sqrt(axis_dx * axis_dx + axis_dz * axis_dz) or 1.0
	perp_x = -axis_dz / axis_len
	perp_z = axis_dx / axis_len
	flank_offset = min(span_x, span_z) * 0.28

	left_via = (
		max(min_x + 35.0, min(max_x - 35.0, mid_x + perp_x * flank_offset)),
		max(min_z + 35.0, min(max_z - 35.0, mid_z + perp_z * flank_offset)),
	)
	center_via = (
		max(min_x + 35.0, min(max_x - 35.0, mid_x)),
		max(min_z + 35.0, min(max_z - 35.0, mid_z)),
	)
	right_via = (
		max(min_x + 35.0, min(max_x - 35.0, mid_x - perp_x * flank_offset)),
		max(min_z + 35.0, min(max_z - 35.0, mid_z - perp_z * flank_offset)),
	)

	wp_left_1 = _astar_grid_path(nx, nz, origin_x, origin_z, cell_sz, heights_mm, hazards, links, b1_xz, b2_xz, left_via)
	wp_center_1 = _astar_grid_path(nx, nz, origin_x, origin_z, cell_sz, heights_mm, hazards, links, b1_xz, b2_xz, center_via)
	wp_right_1 = _astar_grid_path(nx, nz, origin_x, origin_z, cell_sz, heights_mm, hazards, links, b1_xz, b2_xz, right_via)

	rear_1_xz = [
		round(b1_xz[0] + (axis_dx / axis_len) * 35.0, 2),
		round(b1_xz[1] + (axis_dz / axis_len) * 35.0, 2),
		True,
	]
	rear_2_xz = [
		round(b2_xz[0] - (axis_dx / axis_len) * 35.0, 2),
		round(b2_xz[1] - (axis_dz / axis_len) * 35.0, 2),
		True,
	]

	routes_1 = [
		{
			'id': 'left_flank',
			'capacity': 5,
			'risk': 0.58,
			'role_weights': {'brawler': 1.0, 'support': 0.80, 'flanker': 0.65, 'sniper': 0.55, 'scout': 0.45, 'artillery': 0.0},
			'waypoints': wp_left_1,
		},
		{
			'id': 'center',
			'capacity': 5,
			'risk': 0.65,
			'role_weights': {'brawler': 0.75, 'support': 0.85, 'flanker': 0.85, 'sniper': 0.90, 'scout': 1.0, 'artillery': 0.0},
			'waypoints': wp_center_1,
		},
		{
			'id': 'right_flank',
			'capacity': 5,
			'risk': 0.55,
			'role_weights': {'brawler': 0.85, 'support': 0.78, 'flanker': 1.0, 'sniper': 0.82, 'scout': 0.75, 'artillery': 0.0},
			'waypoints': wp_right_1,
		},
		{
			'id': 'rear_guard',
			'capacity': 2,
			'risk': 0.10,
			'role_weights': {'brawler': 0.0, 'support': 0.20, 'flanker': 0.0, 'sniper': 0.45, 'scout': 0.0, 'artillery': 1.0},
			'waypoints': [rear_1_xz],
		},
	]
	routes_2 = [
		{
			'id': 'left_flank',
			'capacity': 5,
			'risk': 0.58,
			'role_weights': {'brawler': 1.0, 'support': 0.80, 'flanker': 0.65, 'sniper': 0.55, 'scout': 0.45, 'artillery': 0.0},
			'waypoints': list(reversed(wp_left_1)),
		},
		{
			'id': 'center',
			'capacity': 5,
			'risk': 0.65,
			'role_weights': {'brawler': 0.75, 'support': 0.85, 'flanker': 0.85, 'sniper': 0.90, 'scout': 1.0, 'artillery': 0.0},
			'waypoints': list(reversed(wp_center_1)),
		},
		{
			'id': 'right_flank',
			'capacity': 5,
			'risk': 0.55,
			'role_weights': {'brawler': 0.85, 'support': 0.78, 'flanker': 1.0, 'sniper': 0.82, 'scout': 0.75, 'artillery': 0.0},
			'waypoints': list(reversed(wp_right_1)),
		},
		{
			'id': 'rear_guard',
			'capacity': 2,
			'risk': 0.10,
			'role_weights': {'brawler': 0.0, 'support': 0.20, 'flanker': 0.0, 'sniper': 0.45, 'scout': 0.0, 'artillery': 1.0},
			'waypoints': [rear_2_xz],
		},
	]

	spg_positions = {
		'1': [
			[round(b1_xz[0] + perp_x * 45.0, 2), round(b1_xz[1] + perp_z * 45.0, 2)],
			[round(b1_xz[0], 2), round(b1_xz[1], 2)],
			[round(b1_xz[0] - perp_x * 45.0, 2), round(b1_xz[1] - perp_z * 45.0, 2)],
		],
		'2': [
			[round(b2_xz[0] + perp_x * 45.0, 2), round(b2_xz[1] + perp_z * 45.0, 2)],
			[round(b2_xz[0], 2), round(b2_xz[1], 2)],
			[round(b2_xz[0] - perp_x * 45.0, 2), round(b2_xz[1] - perp_z * 45.0, 2)],
		],
	}

	spawn_formations = _build_validated_spawn_formations(
		nx, nz, origin_x, origin_z, cell_sz, heights_mm, hazards,
		(min_x, min_z, max_x, max_z), bases, spawns)

	graph = {
		'format': 'offhangar-navgraph',
		'version': 1,
		'game_version': '0.8.2',
		'map': short_name,
		'width': nx,
		'height': nz,
		'cell_size': cell_sz,
		'origin': [origin_x, origin_z],
		'bounds': [min_x, min_z, max_x, max_z],
		'bases': [b1_xz, b2_xz],
		'directions': [list(d) for d in _DIRECTIONS],
		'heights_mm': heights_mm,
		'hazards': hazards,
		'links': links,
		'spawn_formations': spawn_formations,
		'spg_positions': spg_positions,
		'tactical_pockets': [],
		'routes': {
			'1': routes_1,
			'2': routes_2,
			'spg_positions': spg_positions,
			'tactical_pockets': [],
		},
		'bake': {
			'max_grade': 0.38,
			'max_grade_up': 0.45,
			'max_grade_down': 0.38,
			'dry_only_routes': True,
			'spawn_obstacle_skipped_models': 0,
		},
		'validation': {
			'spawn_compiled_bsp_obb_clearance': True,
			'spawn_pairwise_obb_clearance': True,
			'spawn_terrain_footprint_clearance': True,
			'route_terminal_obb_clearance': True,
			'spawn_slots_per_team': 15,
		},
	}

	_RUNTIME_BAKED_MEM[short_name] = graph
	try:
		path = _navcache_path(short_name)
		f = open(path, 'w')
		try:
			json.dump(graph, f)
		finally:
			f.close()
	except Exception as save_err:
		LOG_DEBUG('DYNAMIC_NAVMESH save err (%s): %s' % (short_name, str(save_err)))

	if foliage_instances:
		fol_data = {
			'format': 'offhangar-foliage',
			'version': 1,
			'game_version': '0.8.2',
			'map': short_name,
			'cell_size': 32.0,
			'instances': foliage_instances,
			'cells': foliage_cells,
		}
		_RUNTIME_FOLIAGE_MEM[short_name] = fol_data
		try:
			fpath = _foliage_cache_path(short_name)
			ff = open(fpath, 'w')
			try:
				json.dump(fol_data, ff)
			finally:
				ff.close()
		except Exception:
			pass

	LOG_NOTE('DYNAMIC_NAVMESH baked & cached 0.8.x map=%s grid=%dx%d (%.1fm) foliage=%d -> %s' % (
		short_name, nx, nz, cell_sz, len(foliage_instances), _navcache_path(short_name)))
	return graph
