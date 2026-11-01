# -*- coding: utf-8 -*-
"""Load versioned navigation graphs shipped with the offline-battle mod."""

import json
import hashlib
import os

from gui.mods.offhangar.paths import mod_dir


FORMAT_NAME = 'offhangar-navgraph'
FORMAT_VERSION = 1
GAME_VERSION = '0.8.2'
MANIFEST_FORMAT = FORMAT_NAME + '-manifest'
STOCK_MAPS = (
	'01_karelia', '02_malinovka', '03_campania', '04_himmelsdorf',
	'05_prohorovka', '06_ensk', '07_lakeville', '08_ruinberg',
	'10_hills', '11_murovanka', '13_erlenberg', '14_siegfried_line',
	'15_komarin', '17_munchen', '18_cliff', '19_monastery',
	'22_slough', '23_westfeld', '28_desert', '29_el_hallouf',
	'31_airfield', '33_fjord', '34_redshire', '35_steppes',
	'36_fishing_bay', '37_caucasus', '38_mannerheim_line',
	'39_crimea', '42_north_america', '44_north_america',
	'45_north_america', '47_canada_a', '51_asia',
)

try:
	_INTEGER_TYPES = (int, long)
except NameError:
	_INTEGER_TYPES = (int,)


def _short_map_name(map_name):
	return str(map_name or '').replace('\\', '/').rstrip('/').split('/')[-1]


def _validate(graph, map_name):
	if not isinstance(graph, dict):
		raise ValueError('navigation graph root is not an object')
	if graph.get('format') != FORMAT_NAME:
		raise ValueError('unsupported navigation graph format')
	if int(graph.get('version', -1)) != FORMAT_VERSION:
		raise ValueError('unsupported navigation graph version')
	if not str(graph.get('game_version', '0.8.2')).startswith('0.8.'):
		raise ValueError('navigation graph belongs to a different client branch')
	if _short_map_name(graph.get('map')) != map_name:
		raise ValueError('navigation graph map does not match the battle')
	width = int(graph.get('width', 0))
	height = int(graph.get('height', 0))
	if width <= 0 or height <= 0:
		raise ValueError('navigation graph dimensions are invalid')
	if len(graph.get('heights_mm') or ()) != width * height:
		raise ValueError('navigation graph height array is incomplete')
	if len(graph.get('links') or ()) != width * height:
		raise ValueError('navigation graph link array is incomplete')
	if len(graph.get('origin') or ()) != 2:
		raise ValueError('navigation graph origin is invalid')
	if float(graph.get('cell_size', 0.0)) <= 0.0:
		raise ValueError('navigation graph cell size is invalid')
	if map_name in STOCK_MAPS:
		formations = graph.get('spawn_formations')
		validation = graph.get('validation') or {}
		bake = graph.get('bake') or {}
		skipped_models = bake.get('spawn_obstacle_skipped_models')
		if (not isinstance(formations, dict) or
			set(formations.keys()) != set(('1', '2')) or
			not bool(validation.get('spawn_compiled_bsp_obb_clearance')) or
			not bool(validation.get('spawn_pairwise_obb_clearance')) or
			not bool(validation.get('spawn_terrain_footprint_clearance')) or
			not bool(validation.get('route_terminal_obb_clearance')) or
			isinstance(skipped_models, bool) or
			not isinstance(skipped_models, _INTEGER_TYPES) or
			skipped_models != 0):
			raise ValueError('stock navigation graph lacks validated spawn poses')
		for team in ('1', '2'):
			poses = formations.get(team)
			if not isinstance(poses, list) or len(poses) != 15:
				raise ValueError('stock navigation graph spawn formation is incomplete')
			for pose in poses:
				try:
					values = tuple(float(value) for value in pose)
				except Exception:
					values = ()
				if (not isinstance(pose, list) or len(values) != 4 or
						not all(value == value and abs(value) != float('inf')
							for value in values)):
					raise ValueError('stock navigation graph spawn pose is invalid')
	return graph


def _sha256(path):
	digest = hashlib.sha256()
	handle = open(path, 'rb')
	try:
		while True:
			block = handle.read(1024 * 1024)
			if not block:
				break
			digest.update(block)
	finally:
		handle.close()
	return digest.hexdigest()


def _manifest_entry(directory, map_name):
	"""Validate the complete batch marker and return this map's record.

	A missing manifest remains valid for one-file developer builds. Once a batch
	manifest is present, however, a partial copy or a stale/tampered graph must
	not silently drive bots with mixed navigation data.
	"""
	path = os.path.join(directory, 'manifest.json')
	if not os.path.isfile(path):
		return None
	handle = open(path, 'r')
	try:
		manifest = json.load(handle)
	finally:
		handle.close()
	if (not isinstance(manifest, dict) or
			manifest.get('format') != MANIFEST_FORMAT or
			int(manifest.get('version', -1)) != FORMAT_VERSION or
			not str(manifest.get('game_version', '0.8.2')).startswith('0.8.')):
		raise ValueError('navigation manifest is incompatible')
	records = manifest.get('maps') or ()
	if len(records) != len(STOCK_MAPS):
		raise ValueError('navigation manifest is incomplete')
	expected = set(STOCK_MAPS)
	seen = set()
	selected = None
	for record in records:
		if not isinstance(record, dict):
			raise ValueError('navigation manifest record is invalid')
		name = _short_map_name(record.get('map'))
		filename = str(record.get('file') or '')
		if (name not in expected or name in seen or
				filename != name + '.json' or
				len(str(record.get('sha256') or '')) != 64):
			raise ValueError('navigation manifest record is invalid')
		seen.add(name)
		if not os.path.isfile(os.path.join(directory, filename)):
			raise ValueError('navigation graph batch is incomplete')
		if name == map_name:
			selected = record
	if seen != expected:
		raise ValueError('navigation manifest is incomplete')
	return selected


_MESH3D_CACHE = {}
_GRAPH_MEM_CACHE = {}


def load_3d_mesh_grid(map_name):
	"""Load binary 3D surface/roof elevation grid (OFFHNAV1) from navmesh/<map>.grid."""
	short_name = _short_map_name(map_name)
	if not short_name:
		return None
	if short_name in _MESH3D_CACHE:
		return _MESH3D_CACHE[short_name]
	try:
		import struct
		grid_path = os.path.join(mod_dir(), 'navmesh', short_name + '.grid')
		if not os.path.isfile(grid_path):
			_MESH3D_CACHE[short_name] = None
			return None
		handle = open(grid_path, 'rb')
		try:
			raw = handle.read()
		finally:
			handle.close()
		if len(raw) < 20 or raw[:8] != b'OFFHNAV1':
			_MESH3D_CACHE[short_name] = None
			return None
		hdr_len = struct.unpack('<I', raw[8:12])[0]
		hdr_lines = raw[12:12 + hdr_len].decode('ascii', 'ignore').strip().splitlines()
		if len(hdr_lines) < 4:
			_MESH3D_CACHE[short_name] = None
			return None
		b_parts = [float(x) for x in hdr_lines[1].split()]
		min_x, min_z = b_parts[0], b_parts[1]
		step = float(hdr_lines[2].strip())
		wh_parts = [int(x) for x in hdr_lines[3].split()]
		gw, gh = wh_parts[0], wh_parts[1]
		count = gw * gh
		offset = 12 + hdr_len
		if len(raw) < offset + count + count * 4:
			_MESH3D_CACHE[short_name] = None
			return None
		from array import array
		if len(_MESH3D_CACHE) >= 1:
			_MESH3D_CACHE.clear()
		g_flags = bytearray(raw[offset:offset + count])
		g_heights = array('f')
		g_heights.frombytes(raw[offset + count:offset + count * 5]) if hasattr(g_heights, 'frombytes') else g_heights.fromstring(raw[offset + count:offset + count * 5])
		mesh3d = (min_x, min_z, step, gw, gh, g_flags, g_heights)
		_MESH3D_CACHE[short_name] = mesh3d
		return mesh3d
	except Exception:
		_MESH3D_CACHE[short_name] = None
		return None


def load_graph(map_name):
	"""Return a validated graph, or None when this map has not been baked."""
	short_name = _short_map_name(map_name)
	if not short_name:
		return None
	directory = os.path.join(mod_dir(), 'navgraphs')
	entry = _manifest_entry(directory, short_name)
	path = os.path.join(directory, short_name + '.json')
	if not os.path.isfile(path):
		try:
			from gui.mods.offhangar import dynamic_map_baker as _dmb
			cached = _dmb.load_cached_navgraph(short_name)
			if cached is not None:
				return _validate(cached, short_name)
		except Exception:
			pass
		return None
	handle = open(path, 'r')
	try:
		graph = json.load(handle)
	finally:
		handle.close()
	validated = _validate(graph, short_name)
	try:
		mesh3d = load_3d_mesh_grid(short_name)
		if mesh3d is not None:
			validated['mesh3d'] = mesh3d
	except Exception:
		pass
	return validated


def nearest_ground_point(graph, x, z, max_radius=3):
	"""Return the nearest baked-safe cell centre and its ground height."""
	if not isinstance(graph, dict):
		return None
	try:
		width = int(graph.get('width', 0))
		height = int(graph.get('height', 0))
		cell_size = float(graph.get('cell_size', 0.0))
		origin = graph.get('origin') or ()
		heights = graph.get('heights_mm') or ()
		links = graph.get('links') or ()
		hazards = graph.get('hazards') or (0,) * (width * height)
		if (width <= 0 or height <= 0 or cell_size <= 0.0 or
				len(origin) != 2 or len(heights) != width * height):
			return None
		cx = int(round((float(x) - float(origin[0])) / cell_size))
		cz = int(round((float(z) - float(origin[1])) / cell_size))
	except Exception:
		return None
	best = None
	best_distance = None
	radius_limit = max(0, int(max_radius))
	for radius in range(radius_limit + 1):
		for req_mask in (255, 90, 0):
			for row in range(cz - radius, cz + radius + 1):
				for column in range(cx - radius, cx + radius + 1):
					if radius and max(abs(column - cx), abs(row - cz)) != radius:
						continue
					if column < 0 or column >= width or row < 0 or row >= height:
						continue
					index = row * width + column
					value = heights[index]
					if value is None or (int(hazards[index]) & 3) != 0:
						continue
					if links:
						l_val = int(links[index])
						if l_val == 0 or (l_val & (l_val - 1)) == 0:
							continue
						if req_mask and (l_val & req_mask) != req_mask:
							continue
					distance = (column - cx) ** 2 + (row - cz) ** 2
					if best_distance is None or distance < best_distance:
						best_distance = distance
						best = (
							float(origin[0]) + column * cell_size,
							float(value) / 1000.0,
							float(origin[1]) + row * cell_size,
						)
			if best is not None:
				return best
	return None


def _build_wide_4row_formations(graph, poses, team, gameplay=None):
	import math
	"""Transform a tight 15-slot spawn formation into a wide 4-row formation
	(18m lateral spacing, 15m row spacing, staggered rows) with multi-directional
	flank yaw orientation, while validating every slot on passable non-hazard ground."""
	try:
		if not isinstance(poses, (list, tuple)) or len(poses) < 15:
			return poses
		# Compute anchor (ax, az) and forward direction from baked poses
		valid = []
		for p in poses[:15]:
			if isinstance(p, (list, tuple)) and len(p) >= 4:
				valid.append((float(p[0]), float(p[1]), float(p[2]), float(p[3])))
		if len(valid) < 15:
			return poses
		ax = sum(p[0] for p in valid) / float(len(valid))
		az = sum(p[2] for p in valid) / float(len(valid))
		sin_sum = sum(math.sin(p[3]) for p in valid)
		cos_sum = sum(math.cos(p[3]) for p in valid)
		base_yaw = math.atan2(sin_sum, cos_sum) if (abs(sin_sum) > 1e-4 or abs(cos_sum) > 1e-4) else valid[0][3]
		fwd_x = math.sin(base_yaw)
		fwd_z = math.cos(base_yaw)
		right_x = math.cos(base_yaw)
		right_z = -math.sin(base_yaw)
		# Extract map tactical route headings for multi-directional spawn orientation
		route_yaws = []
		try:
			routes_all = graph.get('routes') or {}
			t_routes = routes_all.get(str(team)) or routes_all.get(int(team)) or {}
			if isinstance(t_routes, dict):
				for rname, rpts in t_routes.items():
					if isinstance(rpts, (list, tuple)) and len(rpts) >= 2:
						rp = rpts[min(2, len(rpts) - 1)]
						dx = float(rp[0]) - ax
						dz = float(rp[1]) - az
						if dx * dx + dz * dz > 100.0:
							ryaw = math.atan2(dx, dz)
							# Lateral projection to sort left-to-right
							lat = dx * right_x + dz * right_z
							route_yaws.append((lat, ryaw))
			route_yaws.sort(key=lambda item: item[0])
		except Exception:
			route_yaws = []
		# 4 rows: Row 0 (front, 4 tanks), Row 1 (4 tanks), Row 2 (4 tanks), Row 3 (rear, 3 tanks)
		# Lateral spacing = 18.0m, Longitudinal row spacing = 15.0m, staggered by 4.5m on odd rows
		row_defs = (
			(18.0, (0.0, -1.0, 1.0, -2.0)),     # Row 0 (slots 0..3): front spearhead
			(3.0,  (-0.5, 0.5, -1.5, 1.5)),     # Row 1 (slots 4..7): second line
			(-12.0, (0.0, -1.0, 1.0, 2.0)),     # Row 2 (slots 8..11): third line
			(-27.0, (-1.0, 0.0, 1.0)),          # Row 3 (slots 12..14): rear / SPG line
		)
		out_poses = []
		placed_xz = []
		slot_idx = 0
		for r_idx, (fwd_off, col_mults) in enumerate(row_defs):
			for c_mult in col_mults:
				if slot_idx >= 15:
					break
				orig_p = valid[slot_idx]
				lat_off = float(c_mult) * 18.0
				wx = ax + fwd_x * float(fwd_off) + right_x * lat_off
				wz = az + fwd_z * float(fwd_off) + right_z * lat_off
				# Multi-directional yaw: fan outward by column and blend with map flank route heading
				fan_yaw = base_yaw + float(c_mult) * 0.22
				if route_yaws:
					if c_mult < -0.6 and len(route_yaws) >= 1:
						r_target = route_yaws[0][1]
					elif c_mult > 0.6 and len(route_yaws) >= 1:
						r_target = route_yaws[-1][1]
					else:
						r_target = route_yaws[len(route_yaws) // 2][1]
					# Blend 45% toward the map's tactical route direction, clamped within +-42 deg of base_yaw
					diff_r = math.atan2(math.sin(r_target - base_yaw), math.cos(r_target - base_yaw))
					diff_r = max(-0.72, min(0.72, diff_r * 0.55 + float(c_mult) * 0.14))
					fan_yaw = math.atan2(math.sin(base_yaw + diff_r), math.cos(base_yaw + diff_r))
				# Validate candidate on navgraph (must be passable, non-hazard, reasonable height delta, >=13m from other slots)
				chosen = None
				for rad in (0.0, 4.0, 8.0, 12.0):
					angles = (0.0,) if rad == 0.0 else (0.0, 1.57, 3.14, 4.71, 0.78, 2.35, 3.92, 5.49)
					for ang in angles:
						cx = wx + math.cos(ang) * rad
						cz = wz + math.sin(ang) * rad
						gp_pt = nearest_ground_point(graph, cx, cz, max_radius=2)
						if gp_pt is None:
							continue
						gx, gy, gz = float(gp_pt[0]), float(gp_pt[1]), float(gp_pt[2])
						# Avoid steep cliffs relative to original slot height
						if abs(gy - orig_p[1]) > 8.5:
							continue
						if any((gx - px) ** 2 + (gz - pz) ** 2 < 13.5 ** 2 for px, pz in placed_xz):
							continue
						chosen = (gx, gy, gz, fan_yaw)
						break
					if chosen is not None:
						break
				if chosen is None:
					for cand_p in ([orig_p] + list(valid)):
						if all((cand_p[0] - px) ** 2 + (cand_p[2] - pz) ** 2 >= 11.0 ** 2 for px, pz in placed_xz):
							chosen = (cand_p[0], cand_p[1], cand_p[2], fan_yaw)
							break
				if chosen is None:
					for rad2 in (8.0, 12.0, 16.0, 20.0, 24.0, 28.0):
						for ai2 in range(12):
							ang2 = 6.2831853 * ai2 / 12.0
							gp2 = nearest_ground_point(graph, orig_p[0] + math.cos(ang2) * rad2, orig_p[2] + math.sin(ang2) * rad2, max_radius=2)
							if gp2 is not None and abs(float(gp2[1]) - orig_p[1]) <= 8.5:
								if all((float(gp2[0]) - px) ** 2 + (float(gp2[2]) - pz) ** 2 >= 11.0 ** 2 for px, pz in placed_xz):
									chosen = (float(gp2[0]), float(gp2[1]), float(gp2[2]), fan_yaw)
									break
						if chosen is not None:
							break
				if chosen is None:
					chosen = (orig_p[0], orig_p[1], orig_p[2], fan_yaw)
				placed_xz.append((chosen[0], chosen[2]))
				out_poses.append(chosen)
				slot_idx += 1
		return out_poses
	except Exception:
		return poses


def spawn_pose(graph, team, slot, gameplay=None):
	"""Return one offline-validated wide 4-row multi-directional spawn pose."""
	try:
		team = int(team)
		slot = int(slot)
		if not isinstance(graph, dict):
			return None
		gp = str(gameplay or '').strip().lower()
		formations = None
		if gp and gp != 'ctf':
			mode_entry = (graph.get('mode_data') or {}).get(gp)
			if isinstance(mode_entry, dict):
				formations = mode_entry.get('spawn_formations')
		if not isinstance(formations, dict):
			formations = graph.get('spawn_formations')
		poses = formations.get(str(team)) if isinstance(formations, dict) else None
		if not poses or team not in (1, 2) or slot < 0 or slot >= 15:
			return None
		pose = poses[slot]
		if len(pose) != 4:
			return None
		values = tuple(float(value) for value in pose)
		for value in values:
			if value != value or abs(value) == float('inf'):
				return None
		return values
	except Exception:
		return None


def nearest_passable_world(graph, x, z, max_radius=8):
	"""Return (gx, gy, gz) of nearest passable baked cell or None."""
	return nearest_ground_point(graph, x, z, max_radius=max_radius)


def _is_waypoint_already_passable(graph, x, z):
	try:
		width = int(graph.get('width', 0))
		height = int(graph.get('height', 0))
		cell_size = float(graph.get('cell_size', 0.0))
		origin = graph.get('origin') or ()
		heights = graph.get('heights_mm') or ()
		links = graph.get('links') or ()
		hazards = graph.get('hazards') or ()
		cx = int(round((float(x) - float(origin[0])) / cell_size))
		cz = int(round((float(z) - float(origin[1])) / cell_size))
		if 0 <= cx < width and 0 <= cz < height:
			idx = cz * width + cx
			if heights[idx] is not None and (int(hazards[idx]) & 3) == 0 and int(links[idx]) > 0:
				return True
	except Exception:
		pass
	return False


def _validate_and_snap_routes(graph, routes_dict):
	if not routes_dict or not isinstance(routes_dict, dict):
		return routes_dict
	cleaned = {}
	for tkey, team_routes in routes_dict.items():
		if isinstance(team_routes, dict):
			t_clean = {}
			for rname, pts in team_routes.items():
				if not isinstance(pts, (list, tuple)):
					t_clean[rname] = pts
					continue
				valid_pts = []
				for pt in pts:
					try:
						px, pz = float(pt[0]), float(pt[1])
						snapped = (px, 0.0, pz) if _is_waypoint_already_passable(graph, px, pz) else nearest_ground_point(graph, px, pz, max_radius=8)
						if snapped is not None:
							if len(pt) > 2:
								valid_pts.append((float(snapped[0]), float(snapped[2]), pt[2]))
							else:
								valid_pts.append((float(snapped[0]), float(snapped[2])))
						else:
							valid_pts.append((px, pz))
					except Exception:
						pass
				t_clean[rname] = tuple(valid_pts) if len(valid_pts) >= 2 else pts
			cleaned[tkey] = t_clean
		elif isinstance(team_routes, (list, tuple)):
			t_clean = []
			for r_entry in team_routes:
				if not isinstance(r_entry, dict) or 'waypoints' not in r_entry:
					t_clean.append(r_entry)
					continue
				pts = r_entry.get('waypoints', ()) or ()
				valid_pts = []
				for pt in pts:
					try:
						px, pz = float(pt[0]), float(pt[1])
						snapped = (px, 0.0, pz) if _is_waypoint_already_passable(graph, px, pz) else nearest_ground_point(graph, px, pz, max_radius=8)
						if snapped is not None:
							if len(pt) > 2:
								valid_pts.append([float(snapped[0]), float(snapped[2]), pt[2]])
							else:
								valid_pts.append([float(snapped[0]), float(snapped[2])])
						else:
							valid_pts.append(list(pt))
					except Exception:
						valid_pts.append(list(pt))
				r_copy = dict(r_entry)
				r_copy['waypoints'] = valid_pts if len(valid_pts) >= 2 else pts
				t_clean.append(r_copy)
			cleaned[tkey] = t_clean
		else:
			cleaned[tkey] = team_routes
	return cleaned if cleaned else routes_dict


def mode_routes(graph, gameplay=None):
	"""Return mode-appropriate routes, spg_positions, and tactical_pockets from graph."""
	if not isinstance(graph, dict):
		return None
	gp = str(gameplay or '').strip().lower()
	routes = None
	spg_pos = None
	if gp and gp != 'ctf':
		mode_entry = (graph.get('mode_data') or {}).get(gp)
		if isinstance(mode_entry, dict):
			routes = mode_entry.get('routes')
			spg_pos = mode_entry.get('spg_positions')
	if not isinstance(routes, dict):
		routes = graph.get('routes')
	if not isinstance(spg_pos, dict):
		spg_pos = graph.get('spg_positions')
	if not isinstance(routes, dict):
		return None
	res = _validate_and_snap_routes(graph, dict(routes))
	if isinstance(spg_pos, dict):
		res['spg_positions'] = spg_pos
	if isinstance(graph.get('tactical_pockets'), (list, tuple)):
		res['tactical_pockets'] = graph.get('tactical_pockets')
	return res
