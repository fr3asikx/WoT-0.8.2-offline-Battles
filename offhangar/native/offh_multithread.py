# -*- coding: utf-8 -*-
"""
offh_multithread.py — PhysicsSpace 60 Hz (2..4 Thread Pool) + 16-Angle Corridor Feelers + 120+ FPS LERP Sub-Engine for WoT 0.8.2

1. Loads the RVA-bound `offh_mt.pyd` (compiled without _ctypes.pyd or python26.dll,
   using `.idata` KERNEL32.dll imports + RVA table matching WorldOfTanks.exe 0.8.2).
2. Runs `PhysicsSpace` on up to 4 parallel Win32 worker threads at a fixed 60 Hz tick
   (dt = 0.016667f), computing 4-corner terrain height/attitude, 60 Hz turret/gun slew,
   32x32 LOS bitmask, and 16-direction terrain/water/cliff corridor feelers in C, followed by
   `resolve_inter_collisions`.
3. Provides O(1) `is_corridor_clear_cached(eid, absolute_yaw)`, `has_los_cached(obs, tgt)`,
   and `has_ram_contact_cached(eid)` with zero per-frame tuple/dict churn.
"""

import array
import imp
import math
import os

import BigWorld
import Math

try:
	from gui.mods.offhangar.logging import LOG_NOTE, LOG_DEBUG, LOG_ERROR
except Exception:
	def LOG_NOTE(*args): pass
	def LOG_DEBUG(*args): pass
	def LOG_ERROR(*args): pass

MAX_VEHICLES = 30
IN_STRIDE = 16
OUT_STRIDE = 8
SUB_IN_STRIDE = 24
SUB_OUT_STRIDE = 16

SPT_MATKIND_LEAVES = 72
DESTRUCTIBLE_MATKIND_MIN = 71
DESTRUCTIBLE_MATKIND_MAX = 100
COLLFLAG_TERRAIN = 8


def _load_offh_mt_pyd():
	here = os.path.dirname(os.path.abspath(__file__))
	parent_dir = os.path.dirname(here)
	candidates = (
		os.path.join(here, 'offh_mt.pyd'),
		os.path.join(parent_dir, 'offh_mt.pyd'),
		'./res_mods/0.8.2/scripts/client/gui/mods/offhangar/offh_mt.pyd',
		'res_mods/0.8.2/scripts/client/gui/mods/offhangar/offh_mt.pyd',
	)
	for path in candidates:
		try:
			if os.path.isfile(path):
				mod = imp.load_dynamic('offh_mt', os.path.abspath(path))
				probe = array.array('f', [12345.0, -6789.0])
				status = array.array('f', [0.0, 0.0, 0.0, 0.0])
				mod.calibrate_array_offset(probe, status)
				try:
					mod._worker_count = int(status[0])
				except Exception:
					pass
				LOG_NOTE(
					'Offline Battles 2.0 (release) native sub-engine loaded: workers=%d exe_guards=%d/6 native_collide=%d' % (
						int(status[0]), int(status[2]), int(status[3])))
				return mod
		except Exception as err:
			LOG_NOTE('OFFH_MT load candidate failed (%s): %s' % (path, str(err)))
	return None


_NATIVE_MT = _load_offh_mt_pyd()


class FastMainThreadLos(object):
	"""Zero-allocation main-thread LOS accelerator for WoT 0.8.2."""

	def __init__(self, space_id_fn, mat_name_fn):
		self._space_id_fn = space_id_fn
		self._mat_name_fn = mat_name_fn
		self._transparent_mat_cache = {SPT_MATKIND_LEAVES: True}
		self.last_hit_flags = 0
		self.last_hit_matkind = 0

	def _filter_cb(self, mat_kind, coll_flags, item_id=-1, chunk_id=-1):
		try:
			mk = int(mat_kind)
		except Exception:
			return True
		if (int(coll_flags) & COLLFLAG_TERRAIN) != 0:
			self.last_hit_flags = int(coll_flags)
			self.last_hit_matkind = mk
			return True
		if mk == SPT_MATKIND_LEAVES:
			return False
		cached = self._transparent_mat_cache.get(mk)
		if cached is True:
			return False
		self.last_hit_flags = int(coll_flags)
		self.last_hit_matkind = mk
		return True

	def is_last_hit_terrain(self):
		return (self.last_hit_flags & COLLFLAG_TERRAIN) != 0


class NativeMultithreadCoordinator(object):
	"""Coordinates the Multi-Thread 60 Hz PhysicsSpace, Corridor Feelers & LERP Sub-Engine in offh_mt.pyd."""

	def __init__(self):
		self.native = _NATIVE_MT
		self.in_buf = array.array('f', [0.0] * (32 * IN_STRIDE))
		self.out_buf = array.array('f', [0.0] * (32 * OUT_STRIDE))
		self.sub_in_buf = array.array('f', [0.0] * (32 * SUB_IN_STRIDE))
		self.sub_out_buf = array.array('f', [0.0] * (32 * SUB_OUT_STRIDE))
		self.interp_single_out = array.array('f', [0.0] * 12)
		self.foliage_out = array.array('f', [0.0])
		self.pose_out = array.array('f', [0.0] * 14)
		self.collide_out = array.array('f', [0.0] * 10)
		self.spot_los_out = array.array('f', [0.0, 0.0])
		self.shot_in_buf = array.array('f', [0.0] * 18)
		self.shot_out_buf = array.array('f', [0.0] * 8)
		self.lidar_out = array.array('f', [0.0] * 8)
		self.win_out = array.array('f', [0.0, 0.0])
		self.eid_to_idx = {}
		self.cached_los_mask = {}
		self.cached_corridor = {}
		self.cached_ram_id = {}
		self._visual_entities = {}
		self.last_frame_count = 0
		self.last_harvest_count = 0
		self.last_submit_count = 0
		self._last_step_time = None
		self._uploaded_map_id = None
		self._uploaded_foliage_id = None
		self._foliage_ready = False
		self._has_physics_space = bool(self.native is not None and hasattr(self.native, 'step_physics_space'))
		self._has_fast_pub = bool(self.native is not None and hasattr(self.native, 'fast_publish_root'))
		self._has_fast_seed = bool(self.native is not None and hasattr(self.native, 'fast_seed_and_output'))
		self._has_native_collide = bool(self.native is not None and hasattr(self.native, 'native_collide_segment'))
		self._has_ballistic_impact = bool(self.native is not None and hasattr(self.native, 'native_ballistic_impact'))
		self._has_spot_3d_los = bool(self.native is not None and hasattr(self.native, 'native_spot_3d_los'))
		self._has_solve_shot = bool(self.native is not None and hasattr(self.native, 'native_solve_shot_angles'))
		self.ballistic_out = array.array('f', [0.0] * 10)
		self._has_lidar = bool(self.native is not None and hasattr(self.native, 'lidar_scan'))
		self._has_win_fix = bool(self.native is not None and hasattr(self.native, 'fix_window_mode'))
		self._has_d3d9_low_latency = bool(self.native is not None and hasattr(self.native, 'apply_d3d9_low_latency'))
		self._has_scan_target = bool(self.native is not None and hasattr(self.native, 'native_scan_outline_and_autoaim'))
		self.d3d9_latency_out = array.array('f', [0.0, 0.0])
		self.target_scan_in = array.array('f', [0.0] * 256)
		self.target_scan_out = array.array('f', [0.0] * 8)
		self._has_async_dbuf = bool(
			self.native is not None and
			hasattr(self.native, 'get_last_results_nonblocking') and
			hasattr(self.native, 'submit_step_async'))
		self.async_meta = array.array('f', [0.0, 0.0, 0.0])
		self._idx_to_eid = []
		self._mt_time_accum = 0.0
		self._mt_last_submit_ts = None

	def is_native_active(self):
		return self.native is not None

	def apply_d3d9_low_latency(self, max_latency=1):
		"""Query IDirect3DDevice9Ex and set SetMaximumFrameLatency(1) to eliminate GPU frame backlog."""
		if not self._has_d3d9_low_latency:
			return False
		try:
			lbuf = self.d3d9_latency_out
			lbuf[0] = 0.0
			lbuf[1] = 0.0
			self.native.apply_d3d9_low_latency(lbuf, int(max_latency))
			return bool(lbuf[0] > 0.5)
		except Exception:
			return False

	def native_scan_outline_and_autoaim(self, mode, player_vid, player_team, space_id,
	                                    gx, gy, gz, gdx, gdy, gdz,
	                                    cx, cy, cz, cdx, cdy, cdz,
	                                    vehicles_list):
		"""
		High-performance C vector projection + C++ obstacle raycast for outline & autoaim.
		mode: 0 = outline pick, 1 = autoaim screen cone pick.
		Returns (best_vid, best_dist_or_score, is_ally, hit_ray) or None on fallback.
		"""
		if not self._has_scan_target:
			return None
		try:
			ibuf = self.target_scan_in
			obuf = self.target_scan_out
			ibuf[0] = float(mode)
			ibuf[1] = float(player_vid)
			ibuf[2] = float(player_team)
			ibuf[3] = float(space_id)
			ibuf[4] = float(gx);  ibuf[5] = float(gy);  ibuf[6] = float(gz)
			ibuf[7] = float(gdx); ibuf[8] = float(gdy); ibuf[9] = float(gdz)
			ibuf[10] = float(cx);  ibuf[11] = float(cy);  ibuf[12] = float(cz)
			ibuf[13] = float(cdx); ibuf[14] = float(cdy); ibuf[15] = float(cdz)

			n = min(len(vehicles_list), 32)
			ibuf[16] = float(n)
			for i in xrange(n):
				v = vehicles_list[i]
				base = 17 + i * 7
				ibuf[base + 0] = float(v[0])
				ibuf[base + 1] = float(v[1])
				ibuf[base + 2] = float(v[2])
				ibuf[base + 3] = float(v[3])
				ibuf[base + 4] = float(v[4])
				ibuf[base + 5] = float(v[5])
				ibuf[base + 6] = float(v[6])

			self.native.native_scan_outline_and_autoaim(ibuf, obuf)
			if obuf[7] > 0.5:
				return (int(obuf[0]), float(obuf[1]), int(obuf[2]), bool(obuf[3] > 0.5))
		except Exception:
			pass
		return None

	def fast_publish_root(self, vehicle_filter, vehicle_physics, timestamp, space_id):
		if not self._has_fast_pub:
			return None
		try:
			pbuf = self.pose_out
			self.native.fast_publish_root(
				vehicle_filter, vehicle_physics, float(timestamp), int(space_id), pbuf)
			if 0.5 < pbuf[0] < 1.5:
				return (
					float(pbuf[1]), float(pbuf[2]), float(pbuf[3]),
					float(pbuf[4]), float(pbuf[5]), float(pbuf[6]))
		except Exception:
			pass
		return None

	def fast_publish_root_ex(self, vehicle_filter, vehicle_physics, timestamp, space_id, solver_dt=0.0, prev_pose=None):
		if not self._has_fast_pub:
			return None
		try:
			pbuf = self.pose_out
			pbuf[10] = 0.0
			self.native.fast_publish_root(
				vehicle_filter, vehicle_physics, float(timestamp), int(space_id), pbuf)
			st_code = float(pbuf[0])
			if st_code > 1.5:
				return 'frozen'
			if st_code > 0.5:
				return (
					(float(pbuf[1]), float(pbuf[2]), float(pbuf[3]),
					 float(pbuf[4]), float(pbuf[5]), float(pbuf[6])),
					float(pbuf[7]),
					float(pbuf[8]),
					bool(pbuf[9] > 0.5),
					bool(pbuf[10] > 0.5))
		except Exception:
			pass
		return None

	def native_spot_3d_los(self, space_id, ox, oy, oz, tx, ty, tz, obs_roof, tgt_roof, tgt_hull, t_yaw=0.0, fl=2.5, full_contour=True):
		if not self._has_spot_3d_los:
			return None
		try:
			sbuf = self.spot_los_out
			self.native.native_spot_3d_los(
				int(space_id),
				float(ox), float(oy), float(oz),
				float(tx), float(ty), float(tz),
				float(obs_roof), float(tgt_roof), float(tgt_hull),
				float(t_yaw), float(fl), 1 if full_contour else 0, sbuf)
			code = float(sbuf[0])
			if code > 1.5:
				return None
			return bool(code > 0.5)
		except Exception:
			return None

	def native_solve_shot_angles(self, hx, hy, hz, hyaw, hpitch, hroll, cur_tyaw, tx_off, ty_off, tz_off, gx_off, gy_off, gz_off, sx, sy, sz, proj_speed, proj_gravity):
		if not self._has_solve_shot:
			return None
		try:
			ibuf = self.shot_in_buf
			obuf = self.shot_out_buf
			ibuf[0] = float(hx); ibuf[1] = float(hy); ibuf[2] = float(hz)
			ibuf[3] = float(hyaw); ibuf[4] = float(hpitch); ibuf[5] = float(hroll)
			ibuf[6] = float(cur_tyaw)
			ibuf[7] = float(tx_off); ibuf[8] = float(ty_off); ibuf[9] = float(tz_off)
			ibuf[10] = float(gx_off); ibuf[11] = float(gy_off); ibuf[12] = float(gz_off)
			ibuf[13] = float(sx); ibuf[14] = float(sy); ibuf[15] = float(sz)
			ibuf[16] = float(proj_speed); ibuf[17] = float(proj_gravity)
			self.native.native_solve_shot_angles(ibuf, obuf)
			if float(obuf[0]) > 0.5:
				return (
					(float(obuf[1]), float(obuf[2]), float(obuf[3])),
					float(obuf[4]),
					float(obuf[5]),
					float(obuf[6]))
		except Exception:
			pass
		return None

	def native_ballistic_impact(self, space_id, sx, sy, sz, vx, vy, vz, gy, sub_dt, max_t, max_dist):
		if not self._has_ballistic_impact:
			return None
		try:
			bbuf = self.ballistic_out
			self.native.native_ballistic_impact(
				int(space_id),
				float(sx), float(sy), float(sz),
				float(vx), float(vy), float(vz),
				float(gy), float(sub_dt), float(max_t), float(max_dist),
				bbuf)
			code = float(bbuf[0])
			if code > 1.5:
				return None
			return (
				bool(code > 0.5),
				(float(bbuf[1]), float(bbuf[2]), float(bbuf[3])),
				(float(bbuf[4]), float(bbuf[5]), float(bbuf[6])),
				float(bbuf[7]),
				float(bbuf[8]),
				int(bbuf[9]))
		except Exception:
			return None

	def native_collide_segment(self, space_id, sx, sy, sz, ex, ey, ez, skip_flags=128, max_matkind=0):
		if not self._has_native_collide:
			return -1
		try:
			cbuf = self.collide_out
			self.native.native_collide_segment(
				int(space_id), float(sx), float(sy), float(sz),
				float(ex), float(ey), float(ez),
				int(skip_flags), int(max_matkind), cbuf)
			code = float(cbuf[0])
			if code < 0.5:
				return 0
			if code < 1.5:
				return (
					float(cbuf[1]), float(cbuf[2]), float(cbuf[3]),
					float(cbuf[4]), float(cbuf[5]), float(cbuf[6]),
					int(cbuf[7]), int(cbuf[8]))
		except Exception:
			pass
		return -1

	def fast_seed_and_output(self, vehicle_filter, timestamp, space_id, x, y, z, yaw, pitch, roll):
		if not self._has_fast_seed:
			return False
		try:
			self.native.fast_seed_and_output(
				vehicle_filter, float(timestamp), int(space_id),
				float(x), float(y), float(z),
				float(yaw), float(pitch), float(roll))
			return True
		except Exception:
			return False

	def fix_window_mode(self, windowed=True, width=0, height=0):
		if not self._has_win_fix:
			return (0, 0)
		try:
			wbuf = self.win_out
			wbuf[0] = 0.0
			wbuf[1] = 0.0
			self.native.fix_window_mode(
				1 if windowed else 0, int(width or 0), int(height or 0), wbuf)
			return (int(wbuf[0]), int(wbuf[1]))
		except Exception:
			return (0, 0)

	def ensure_windowed_style(self, width=0, height=0):
		if not self._has_win_fix:
			return (0, 0)
		try:
			wbuf = self.win_out
			wbuf[0] = 0.0
			wbuf[1] = 0.0
			self.native.ensure_windowed_style(int(width or 0), int(height or 0), wbuf)
			return (int(wbuf[0]), int(wbuf[1]))
		except Exception:
			return (0, 0)

	def shutdown_process(self):
		if self.native is not None and hasattr(self.native, 'shutdown_process'):
			try:
				self.native.shutdown_process(self.win_out)
			except Exception:
				pass

	def upload_baked_navgraph_heights(self, baked_graph, map_name=''):
		if self.native is None or not isinstance(baked_graph, dict):
			return False
		nx = int(baked_graph.get('width', baked_graph.get('nx', 0)) or 0)
		nz = int(baked_graph.get('height', baked_graph.get('nz', 0)) or 0)
		sig = (map_name, nx, nz)
		if self._uploaded_map_id == sig:
			return True
		try:
			origin = baked_graph.get('origin') or baked_graph.get('bounds') or (-500.0, -500.0)
			min_x = float(origin[0])
			min_z = float(origin[1])
			cell_sz = float(baked_graph.get('cell_size', 4.0) or 4.0)
			heights_mm = baked_graph.get('heights_mm') or ()
			hazards = baked_graph.get('hazards')
			bake = baked_graph.get('bake') if isinstance(baked_graph.get('bake'), dict) else {}
			dry_only = bool(bake.get('dry_only_routes', True))
			hazard_mask = 15 if dry_only else 7
			if nx < 2 or nz < 2 or len(heights_mm) < nx * nz:
				return False
			has_haz = bool(hazards is not None and len(hazards) >= nx * nz)
			haz_halo = bytearray(nx * nz)
			if has_haz:
				for idx in xrange(nx * nz):
					if (int(hazards[idx]) & hazard_mask) != 0:
						cx = idx % nx
						cz = idx // nx
						for nz_i in xrange(max(0, cz - 1), min(nz, cz + 2)):
							row_b = nz_i * nx
							for nx_i in xrange(max(0, cx - 1), min(nx, cx + 2)):
								haz_halo[row_b + nx_i] = 1
			f_arr = array.array('f', [0.0] * (nx * nz))
			for idx in xrange(nx * nz):
				val = heights_mm[idx]
				if val is None:
					if has_haz and (int(hazards[idx]) & 1) != 0:
						f_arr[idx] = 0.0 + 10000.0
					else:
						# Impassable cell: encode +10000.0 hazard flag so is_cell_obstacle_nolock (>5000) blocks movement,
						# without inventing a +500m heightfield wall on railway tracks/rubble/corner halos that breaks LOS!
						f_arr[idx] = 0.0 + 10000.0
				elif has_haz and (int(hazards[idx]) & hazard_mask) != 0:
					f_arr[idx] = (float(val) * 0.001) + 10000.0
				elif haz_halo[idx]:
					f_arr[idx] = (float(val) * 0.001) + 20000.0
				else:
					f_arr[idx] = float(val) * 0.001
			self.native.upload_heightfield(nx, nz, min_x, min_z, cell_sz, f_arr)
			self._uploaded_map_id = sig
			self._last_step_time = None
			LOG_NOTE('OFFH_MT uploaded %d height+hazard cells (%dx%d, %.1fm) to PhysicsSpace C pool' % (
				nx * nz, nx, nz, cell_sz))
			return True
		except Exception as err:
			LOG_DEBUG('OFFH_MT upload_baked_navgraph_heights err: %s' % str(err))
			return False

	def upload_prebaked_foliage(self, instances, map_name=''):
		if self.native is None or not hasattr(self.native, 'upload_foliage'):
			return False
		count = len(instances) if instances else 0
		sig = (map_name, count)
		if self._uploaded_foliage_id == sig:
			return True
		try:
			if count <= 0:
				empty = array.array('f', [0.0])
				self.native.upload_foliage(0, empty)
				self._uploaded_foliage_id = sig
				self._foliage_ready = False
				return True
			f_arr = array.array('f', [0.0] * (count * 10))
			pos = 0
			for inst in instances:
				for k in xrange(10):
					f_arr[pos + k] = float(inst[k])
				pos += 10
			self.native.upload_foliage(count, f_arr)
			self._uploaded_foliage_id = sig
			self._foliage_ready = True
			LOG_NOTE('OFFH_MT uploaded %d 3D foliage volumes (%s) to C pool' % (
				count, str(map_name)))
			return True
		except Exception as err:
			LOG_DEBUG('OFFH_MT upload_prebaked_foliage err: %s' % str(err))
			self._foliage_ready = False
			return False

	def foliage_bonus_fast(self, observer, target, fired_recently=False):
		if not self._foliage_ready or self.native is None:
			return None
		try:
			self.native.foliage_bonus(
				float(observer[0]), float(observer[1]), float(observer[2]),
				float(target[0]), float(target[1]), float(target[2]),
				1 if fired_recently else 0, self.foliage_out)
			return float(self.foliage_out[0])
		except Exception:
			return None

	def get_interpolated_transform(self, eid, alpha=-1.0):
		if alpha >= 0.0 and self._has_physics_space:
			try:
				ibuf = self.interp_single_out
				self.native.get_interpolated_transform(int(eid), float(alpha), ibuf)
				if ibuf[0] > 0.5:
					return (
						float(ibuf[1]), float(ibuf[2]), float(ibuf[3]),
						float(ibuf[4]), float(ibuf[5]), float(ibuf[6]),
						float(ibuf[7]), float(ibuf[8]), 0.0, 0.0)
			except Exception:
				pass
		i = self.eid_to_idx.get(int(eid))
		if i is None:
			return None
		base = i * SUB_OUT_STRIDE
		sout = self.sub_out_buf
		return (
			float(sout[base + 1]), float(sout[base + 2]), float(sout[base + 3]),
			float(sout[base + 4]), float(sout[base + 5]), float(sout[base + 6]),
			float(sout[base + 7]), float(sout[base + 8]), float(sout[base + 9]), float(sout[base + 10]))

	def get_visual_smoothed_pose(self, eid, target_pos, target_ypr, target_tyaw, target_gpitch, dt, alpha=1.0, is_player=False, prev_pos=None, prev_ypr=None):
		"""120+ FPS visual smoothing and interpolation filter (chassis LERP/SLERP + turret/gun SLERP).
		Decouples visual presentation from physics simulation frequency (30/60 Hz),
	enabling 120/144/240+ FPS butter-smooth rendering without judder, tearing or frame drops.
		"""
		try:
			eid_int = int(eid)
		except Exception:
			return target_pos, target_ypr, target_tyaw, target_gpitch

		tx, ty, tz = float(target_pos[0]), float(target_pos[1]), float(target_pos[2])
		tyaw, tpitch, troll = float(target_ypr[0]), float(target_ypr[1]), float(target_ypr[2])
		tty = float(target_tyaw)
		tgp = float(target_gpitch)

		if getattr(self, '_visual_entities', None) is None:
			self._visual_entities = {}

		entry = self._visual_entities.get(eid_int)
		if entry is None:
			entry = {
				'pos': [tx, ty, tz],
				'ypr': [tyaw, tpitch, troll],
				'prev_pos': [tx, ty, tz],
				'prev_ypr': [tyaw, tpitch, troll],
				'curr_pos': [tx, ty, tz],
				'curr_ypr': [tyaw, tpitch, troll],
				'turret_yaw': tty,
				'prev_ty': tty,
				'gun_pitch': tgp,
				'prev_gp': tgp,
			}
			self._visual_entities[eid_int] = entry
			if prev_pos is not None and alpha < 1.0:
				px, py, pz = float(prev_pos[0]), float(prev_pos[1]), float(prev_pos[2])
				pyaw = float(prev_ypr[0])
				if (abs(px) < 0.01 and abs(pz) < 0.01) and (abs(tx) > 0.1 or abs(tz) > 0.1):
					px, py, pz = tx, ty, tz
					pyaw = tyaw
				a = min(1.0, max(0.0, float(alpha)))
				ix = px + (tx - px) * a
				iy = py + (ty - py) * a
				iz = pz + (tz - pz) * a
				dyaw = (tyaw - pyaw + math.pi) % (2.0 * math.pi) - math.pi
				iyaw = (pyaw + dyaw * a + math.pi) % (2.0 * math.pi) - math.pi
				return (ix, iy, iz), (iyaw, tpitch, troll), tty, tgp
			return (tx, ty, tz), (tyaw, tpitch, troll), tty, tgp

		# Check for zero snap (never allow zeros if target is valid)
		if (abs(entry['pos'][0]) < 0.001 and abs(entry['pos'][2]) < 0.001) and (abs(tx) > 0.1 or abs(tz) > 0.1):
			entry['pos'] = [tx, ty, tz]
			entry['ypr'] = [tyaw, tpitch, troll]
			entry['prev_pos'] = [tx, ty, tz]
			entry['prev_ypr'] = [tyaw, tpitch, troll]
			entry['curr_pos'] = [tx, ty, tz]
			entry['curr_ypr'] = [tyaw, tpitch, troll]
			entry['turret_yaw'] = tty
			entry['prev_ty'] = tty
			entry['gun_pitch'] = tgp
			entry['prev_gp'] = tgp
			return (tx, ty, tz), (tyaw, tpitch, troll), tty, tgp

		# Check for teleport / spawn / map reset (> 5.0m jump)
		dx_jump = tx - entry['curr_pos'][0]
		dy_jump = ty - entry['curr_pos'][1]
		dz_jump = tz - entry['curr_pos'][2]
		if (dx_jump * dx_jump + dy_jump * dy_jump + dz_jump * dz_jump) > 25.0:
			entry['pos'] = [tx, ty, tz]
			entry['ypr'] = [tyaw, tpitch, troll]
			entry['prev_pos'] = [tx, ty, tz]
			entry['prev_ypr'] = [tyaw, tpitch, troll]
			entry['curr_pos'] = [tx, ty, tz]
			entry['curr_ypr'] = [tyaw, tpitch, troll]
			entry['turret_yaw'] = tty
			entry['prev_ty'] = tty
			entry['gun_pitch'] = tgp
			entry['prev_gp'] = tgp
			return (tx, ty, tz), (tyaw, tpitch, troll), tty, tgp

		# Track discrete physics state transitions
		if prev_pos is not None:
			px, py, pz = float(prev_pos[0]), float(prev_pos[1]), float(prev_pos[2])
			pyaw, ppitch, proll = float(prev_ypr[0]), float(prev_ypr[1]), float(prev_ypr[2])
			if (abs(px) < 0.01 and abs(pz) < 0.01) and (abs(tx) > 0.1 or abs(tz) > 0.1):
				px, py, pz = tx, ty, tz
				pyaw, ppitch, proll = tyaw, tpitch, troll
		else:
			dx_step = tx - entry['curr_pos'][0]
			dy_step = ty - entry['curr_pos'][1]
			dz_step = tz - entry['curr_pos'][2]
			if (dx_step * dx_step + dy_step * dy_step + dz_step * dz_step) > 1e-6:
				entry['prev_pos'] = list(entry['curr_pos'])
				entry['curr_pos'] = [tx, ty, tz]
				entry['prev_ypr'] = list(entry['curr_ypr'])
				entry['curr_ypr'] = [tyaw, tpitch, troll]
			px, py, pz = entry['prev_pos']
			pyaw, ppitch, proll = entry['prev_ypr']

		# Track turret / gun target changes
		if abs(tty - entry['turret_yaw']) > 1e-4:
			entry['prev_ty'] = entry['turret_yaw']
			entry['turret_yaw'] = tty
		pty = entry['prev_ty']
		cty = entry['turret_yaw']

		if abs(tgp - entry['gun_pitch']) > 1e-4:
			entry['prev_gp'] = entry['gun_pitch']
			entry['gun_pitch'] = tgp
		pgp = entry['prev_gp']
		cgp = entry['gun_pitch']

		# Exact Hermite / LERP interpolation when physics accumulator alpha is between 0.0 and 1.0
		a = min(1.0, max(0.0, float(alpha)))
		if 0.0 <= a < 1.0 and ((tx - px)**2 + (ty - py)**2 + (tz - pz)**2) < 25.0:
			ix = px + (tx - px) * a
			iy = py + (ty - py) * a
			iz = pz + (tz - pz) * a

			dyaw = (tyaw - pyaw + math.pi) % (2.0 * math.pi) - math.pi
			iyaw = (pyaw + dyaw * a + math.pi) % (2.0 * math.pi) - math.pi
			ipitch = ppitch + (tpitch - ppitch) * a
			iroll = proll + (troll - proll) * a

			if is_player:
				ity = cty
				igp = cgp
			else:
				dty = (cty - pty + math.pi) % (2.0 * math.pi) - math.pi
				ity = (pty + dty * a + math.pi) % (2.0 * math.pi) - math.pi
				igp = pgp + (cgp - pgp) * a
		else:
			ix, iy, iz = tx, ty, tz
			iyaw, ipitch, iroll = tyaw, tpitch, troll
			ity = cty
			igp = cgp

		entry['pos'] = [ix, iy, iz]
		entry['ypr'] = [iyaw, ipitch, iroll]

		# Sanity check against zero
		if (abs(ix) < 0.001 and abs(iz) < 0.001) and (abs(tx) > 0.1 or abs(tz) > 0.1):
			ix, iy, iz = tx, ty, tz
			entry['pos'] = [tx, ty, tz]

		return (ix, iy, iz), (iyaw, ipitch, iroll), ity, igp

	def reset_visual_smoothing(self, eid=None):
		if getattr(self, '_visual_entities', None) is not None:
			if eid is None:
				self._visual_entities.clear()
			else:
				self._visual_entities.pop(int(eid), None)


	def is_corridor_clear_cached(self, eid, absolute_yaw):
		"""Consult the 16-angle open-halo corridor bitmask computed in C worker threads.
		Returns True when both bracketing 16-angle sectors are proven open & dry in C,
		or None when near an obstacle/water/cliff so exact probes & wet_escape run."""
		mask = self.cached_corridor.get(int(eid))
		if not mask:
			return None
		ang = (float(absolute_yaw) + math.pi) % (2.0 * math.pi)
		f_idx = ang * (16.0 / (2.0 * math.pi))
		d0 = int(f_idx) & 15
		d1 = (d0 + 1) & 15
		if (mask & (1 << d0)) and (mask & (1 << d1)):
			return True
		return None

	def lidar_scan_fast(self, x, y, z, yaw, speed=0.0):
		if not self._has_lidar:
			return None
		try:
			lbuf = self.lidar_out
			lbuf[7] = 0.0
			self.native.lidar_scan(float(x), float(y), float(z), float(yaw), float(speed), lbuf)
			if lbuf[7] > 0.5:
				return (
					int(lbuf[0]), float(lbuf[1]), float(lbuf[2]),
					float(lbuf[3]), float(lbuf[4]), int(lbuf[5]), float(lbuf[6]))
		except Exception:
			pass
		return None

	def has_ram_contact_cached(self, eid):
		return self.cached_ram_id.get(int(eid), -1) >= 0

	def eyes_scan_fast(self, space_id, x, y, z, yaw, lookahead, half_width):
		if self.native is None or not hasattr(self.native, 'native_eyes_scan'):
			return None
		try:
			ebuf = getattr(self, '_eyes_out_buf', None)
			if ebuf is None:
				ebuf = array.array('f', [0.0] * 10)
				self._eyes_out_buf = ebuf
			cbuf = getattr(self, '_collide_out_buf', None)
			if cbuf is None:
				cbuf = array.array('f', [0.0] * 10)
				self._collide_out_buf = cbuf
			sid = int(space_id)
			fx = float(x); fy = float(y); fz = float(z)
			fyaw = float(yaw); flook = float(lookahead); fhw = float(half_width)
			self.native.native_eyes_scan(sid, fx, fy, fz, fyaw, flook, fhw, ebuf)
			code = float(ebuf[0])
			if code > 1.5:
				return None
			end_y = float(ebuf[1]); mid_y = float(ebuf[2])
			hit_d = float(ebuf[3]); hit_mat = int(ebuf[4])
			ly = float(ebuf[5]); ry = float(ebuf[6])
			s = math.sin(fyaw); c = math.cos(fyaw)
			lx = c; lz = -s
			# If C++ ground probe reported a steep step (hit_mat < 0), check if it actually hit
			# the roof of a crushable house/shed/fence (matKind 71..100) ahead of the tank!
			if code <= 0.5 and hit_mat < 0 and hasattr(self.native, 'native_collide_segment'):
				for off_r in (0.0, -fhw, fhw):
					ox_r = lx * off_r; oz_r = lz * off_r
					for h_r in (1.15, 0.58):
						self.native.native_collide_segment(
							sid, fx + ox_r, fy + h_r, fz + oz_r,
							fx + ox_r + s * flook, fy + h_r, fz + oz_r + c * flook,
							136, 0, cbuf)
						if float(cbuf[0]) == 1.0:
							mk_roof = int(cbuf[7])
							if 71 <= mk_roof <= 100:
								return (False, fy, fy, float(cbuf[9]), mk_roof, fy, fy)
			# v12.3 Anti-Rock-Trap & Steep Grade/Tilt Checks when C++ sweep reports code > 0.5:
			if code > 0.5:
				mid_d_chk = max(2.0, flook * 0.5)
				# 1) Reject extreme track cross-tilt (one track on a boulder/rock or cliff lip)
				if abs(ly - ry) > max(1.05, fhw * 0.72):
					return (False, end_y, mid_y, mid_d_chk, -1, ly, ry)
				# 2) Reject steep steps / boulder tops that exceeded safe climbing/dropping grade
				if ((mid_y - fy) > mid_d_chk * 0.44 or (mid_y - fy) < -(mid_d_chk * 0.40 + 0.55) or
						(end_y - mid_y) > mid_d_chk * 0.44 or (end_y - mid_y) < -(mid_d_chk * 0.40 + 0.55)):
					return (False, end_y, mid_y, mid_d_chk, -1, ly, ry)
			if code > 0.5 and hasattr(self.native, 'native_collide_segment'):
				# 3) If ground ahead rises (> 0.60m), cast a horizontal nose probe at fy + 0.85m
				#    so boulders whose flat top fooled the vertical ground probe are caught by their steep front face!
				if (mid_y > fy + 0.60 or end_y > fy + 0.85):
					d_nose = min(flook, 5.2)
					nx_e = fx + s * d_nose; nz_e = fz + c * d_nose
					for off in (0.0, -fhw, fhw):
						ox = lx * off; oz = lz * off
						self.native.native_collide_segment(
							sid, fx + ox, fy + 0.85, fz + oz,
							nx_e + ox, fy + 0.85, nz_e + oz,
							128, 70, cbuf)
						if float(cbuf[0]) == 1.0 and float(cbuf[5]) < 0.70:
							return (False, end_y, mid_y, float(cbuf[9]), -1, ly, ry)
				# 4) Two-segment slope-following sweep at lower chassis height (+0.52m) with skip_flags=136 (non-terrain solids ONLY)
				#    across 5 lateral hull lanes (0.0, +-0.5*fhw, +-fhw) so 1.2m concrete dragon's teeth pyramids ("ёжики"),
				#    boulders, rocks, train car undercarriages, and ruined foundations on uphill/downhill slopes NEVER slip under or between rays!
				mid_d_s = flook * 0.5
				mx = fx + s * mid_d_s; mz = fz + c * mid_d_s
				ex = fx + s * flook; ez = fz + c * flook
				y0_low = fy + 0.52
				y_mid_low = mid_y + 0.52
				y1_low = end_y + 0.52
				hhw = fhw * 0.5
				for off in (0.0, -hhw, hhw, -fhw, fhw):
					ox = lx * off; oz = lz * off
					self.native.native_collide_segment(
						sid, fx + ox, y0_low, fz + oz,
						mx + ox, y_mid_low, mz + oz,
						136, 0, cbuf)
					if float(cbuf[0]) == 1.0:
						h_ny = float(cbuf[5])
						h_mk = int(cbuf[7])
						if h_ny < 0.78 or (71 <= h_mk <= 130):
							return (False, end_y, mid_y, float(cbuf[9]), (h_mk if h_mk > 0 else 1), ly, ry)
					if off in (0.0, -fhw, fhw):
						self.native.native_collide_segment(
							sid, mx + ox, y_mid_low, mz + oz,
							ex + ox, y1_low, ez + oz,
							136, 0, cbuf)
						if float(cbuf[0]) == 1.0:
							h_ny = float(cbuf[5])
							h_mk = int(cbuf[7])
							if h_ny < 0.78 or (71 <= h_mk <= 130):
								return (False, end_y, mid_y, mid_d_s + float(cbuf[9]), (h_mk if h_mk > 0 else 1), ly, ry)
			return (code > 0.5, end_y, mid_y, hit_d, hit_mat, ly, ry)
		except Exception:
			return None

	def get_last_results_nonblocking(self, render_dt=0.0166667):
		"""Harvest completed results from the previous C++ worker step at the start of the Python frame.
		Never blocks on WaitForSingleObject or holds the GIL during math!"""
		if self.native is None or not self._has_physics_space:
			return False
		try:
			dt_val = max(0.0005, min(0.10, float(render_dt or 0.0166667)))
			if self._has_async_dbuf:
				self.native.get_last_results_nonblocking(dt_val, self.sub_out_buf, self.async_meta)
				count = int(self.async_meta[0])
			else:
				count = self.last_frame_count
			self.last_harvest_count = count
			if count <= 0:
				return False
			sout = self.sub_out_buf
			c_los = self.cached_los_mask
			c_cor = self.cached_corridor
			c_ram = self.cached_ram_id
			eid_to_idx = self.eid_to_idx
			c_los.clear()
			c_cor.clear()
			c_ram.clear()
			eid_to_idx.clear()
			for i in xrange(count):
				base = i * SUB_OUT_STRIDE
				eid_i = int(sout[base + 0])
				if eid_i <= 0:
					continue
				eid_to_idx[eid_i] = i
				c_los[eid_i] = int(sout[base + 11]) | (int(sout[base + 12]) << 16)
				c_cor[eid_i] = int(sout[base + 14]) & 0xFFFF
				c_ram[eid_i] = int(sout[base + 15])
			self.last_frame_count = count
			return True
		except Exception:
			return False

	def submit_step_async(self, mock_vehicles, player_id, render_dt=0.0166667):
		"""Stage fresh vehicle coordinates, velocities, and inputs into the C++ atomic input buffer
		and trigger the background worker threads without blocking the main Python thread."""
		if self.native is None or not mock_vehicles or not self._has_physics_space:
			return False
		_acc_dt = float(getattr(self, '_async_acc_dt', 0.0) or 0.0) + float(render_dt or 0.0166667)
		if _acc_dt < 0.0135 and int(getattr(self, 'last_submit_count', 0) or 0) > 0:
			self._async_acc_dt = _acc_dt
			return False
		self._async_acc_dt = 0.0
		render_dt = _acc_dt
		sbuf = self.sub_in_buf
		count = 0
		for eid, m_veh in mock_vehicles.iteritems():
			if m_veh is None:
				continue
			vdict = m_veh.__dict__
			if not vdict.get('isAlive', False) or (vdict.get('health', 0) or 0) <= 0:
				continue
			pos = vdict.get('position')
			if pos is None:
				continue
			base = count * SUB_IN_STRIDE
			_st_pack = vdict.get('_offh_mt_static_pack')
			if _st_pack is not None:
				hw, hl, mass, pwr, sfwd, sbwd, has_lim, diff_bits = _st_pack
			else:
				shape = vdict.get('_offh_collision_frame_cache')
				if shape and len(shape) >= 3:
					hw = float(shape[1])
					hl = float(shape[2])
				else:
					hw = 1.65
					hl = 3.10
				bphys = vdict.get('_phys_params')
				if bphys:
					mass = float(bphys.get('mass', 35000.0) or 35000.0)
					pwr = float(bphys.get('enginePower', 500.0) or 500.0)
					sfwd = float(bphys.get('speedFwd', 14.0) or 14.0)
					sbwd = float(bphys.get('speedBwd', 5.0) or 5.0)
				else:
					mass, pwr, sfwd, sbwd = 35000.0, 500.0, 14.0, 5.0
				has_lim = 1 if vdict.get('_offh_has_lim_trav', False) else 0
				diff_bits = 2
				if shape and bphys and '_offh_has_lim_trav' in vdict:
					m_veh._offh_mt_static_pack = (hw, hl, mass, pwr, sfwd, sbwd, has_lim, diff_bits)

			look = vdict.get('_offh_look_at')
			has_look = 1 if (look is not None and len(look) >= 3 and eid != player_id) else 0
			st_nat = vdict.get('_offh_native_bot_physics_state')
			c_kin = 0 if (isinstance(st_nat, dict) and st_nat.get('phase') == 'active') else 1
			has_ct = 1 if vdict.get('_offh_has_combat_target', False) else 0
			is_arty_bit = 1 if vdict.get('_offh_is_arty', False) else 0
			flags = 1 | (c_kin << 1) | (has_look << 2) | (has_lim << 3) | (diff_bits << 4) | (has_ct << 6) | (is_arty_bit << 7)

			sbuf[base + 0] = float(eid)
			sbuf[base + 1] = float(pos[0])
			sbuf[base + 2] = float(pos[1])
			sbuf[base + 3] = float(pos[2])
			sbuf[base + 4] = float(vdict.get('yaw', 0.0) or 0.0)
			sbuf[base + 5] = float(vdict.get('pitch', 0.0) or 0.0)
			sbuf[base + 6] = float(vdict.get('roll', 0.0) or 0.0)
			sbuf[base + 7] = float(vdict.get('_veh_velocity', 0.0) or 0.0)
			sbuf[base + 8] = float(vdict.get('_veh_turn_velocity', 0.0) or 0.0)
			sbuf[base + 9] = float(vdict.get('_turret_yaw', 0.0) or 0.0)
			sbuf[base + 10] = float(vdict.get('_gun_pitch', 0.0) or 0.0)
			sbuf[base + 11] = hw
			sbuf[base + 12] = hl
			sbuf[base + 13] = mass
			sbuf[base + 14] = pwr
			sbuf[base + 15] = sfwd
			sbuf[base + 16] = sbwd
			sbuf[base + 17] = float(vdict.get('_last_throttle', 0.0) or 0.0)
			sbuf[base + 18] = float(vdict.get('_last_steer', 0.0) or 0.0)
			if has_look:
				sbuf[base + 19] = float(look[0])
				sbuf[base + 20] = float(look[1])
				sbuf[base + 21] = float(look[2])
			else:
				sbuf[base + 19] = 0.0
				sbuf[base + 20] = 0.0
				sbuf[base + 21] = 0.0
			_rot_eff = max(0.05, min(99.9, float(vdict.get('_offh_rot_speed_eff', 0.60) or 0.60)))
			_pspd = int(round(max(80.0, min(3000.0, float(vdict.get('_offh_proj_speed', 720.0) or 720.0)))))
			sbuf[base + 22] = float(_pspd * 100.0 + _rot_eff)
			sbuf[base + 23] = float(flags)
			count += 1
			if count >= MAX_VEHICLES:
				break
		if count <= 0:
			self.last_submit_count = 0
			return False
		self.last_submit_count = count
		try:
			dt_val = max(0.0005, min(0.10, float(render_dt or 0.0166667)))
			if self._has_async_dbuf:
				self.native.submit_step_async(count, dt_val, sbuf)
			else:
				self.native.step_physics_space(count, dt_val, sbuf, self.sub_out_buf)
			return True
		except Exception:
			return False

	def step_frame(self, mock_vehicles, player_id, player_pos, player_yaw, render_dt=None):
		if self.native is None or not mock_vehicles or not self._has_physics_space:
			return False

		now = float(BigWorld.time())
		if render_dt is None:
			if self._last_step_time is not None:
				render_dt = max(0.001, min(0.10, now - self._last_step_time))
			else:
				render_dt = 0.0166667
		self._last_step_time = now

		sbuf = self.sub_in_buf
		eid_to_idx = self.eid_to_idx
		eid_to_idx.clear()
		idx_to_eid = []
		count = 0

		for eid, m_veh in mock_vehicles.iteritems():
			if m_veh is None:
				continue
			vdict = m_veh.__dict__
			if not vdict.get('isAlive', False) or (vdict.get('health', 0) or 0) <= 0:
				continue
			pos = vdict.get('position')
			if pos is None:
				continue
			base = count * SUB_IN_STRIDE
			shape = vdict.get('_offh_collision_frame_cache')
			if shape and len(shape) >= 3:
				hw = float(shape[1])
				hl = float(shape[2])
			else:
				hw = 1.65
				hl = 3.10
			bphys = vdict.get('_phys_params') or {}
			mass = float(bphys.get('mass', 35000.0) or 35000.0)
			pwr = float(bphys.get('enginePower', 500.0) or 500.0)
			sfwd = float(bphys.get('speedFwd', 14.0) or 14.0)
			sbwd = float(bphys.get('speedBwd', 5.0) or 5.0)

			look = vdict.get('_offh_look_at')
			has_look = 1 if (look is not None and len(look) >= 3 and eid != player_id) else 0
			has_lim = 1 if vdict.get('_offh_has_lim_trav', False) else 0
			st_nat = vdict.get('_offh_native_bot_physics_state')
			c_kin = 0 if (isinstance(st_nat, dict) and st_nat.get('phase') == 'active') else 1
			diff_bits = 2
			has_ct = 1 if vdict.get('_offh_has_combat_target', False) else 0
			is_arty_bit = 1 if vdict.get('_offh_is_arty', False) else 0
			flags = 1 | (c_kin << 1) | (has_look << 2) | (has_lim << 3) | (diff_bits << 4) | (has_ct << 6) | (is_arty_bit << 7)

			sbuf[base + 0] = float(eid)
			sbuf[base + 1] = float(pos[0])
			sbuf[base + 2] = float(pos[1])
			sbuf[base + 3] = float(pos[2])
			sbuf[base + 4] = float(vdict.get('yaw', 0.0) or 0.0)
			sbuf[base + 5] = float(vdict.get('pitch', 0.0) or 0.0)
			sbuf[base + 6] = float(vdict.get('roll', 0.0) or 0.0)
			sbuf[base + 7] = float(vdict.get('_veh_velocity', 0.0) or 0.0)
			sbuf[base + 8] = float(vdict.get('_veh_turn_velocity', 0.0) or 0.0)
			sbuf[base + 9] = float(vdict.get('_turret_yaw', 0.0) or 0.0)
			sbuf[base + 10] = float(vdict.get('_gun_pitch', 0.0) or 0.0)
			sbuf[base + 11] = hw
			sbuf[base + 12] = hl
			sbuf[base + 13] = mass
			sbuf[base + 14] = pwr
			sbuf[base + 15] = sfwd
			sbuf[base + 16] = sbwd
			sbuf[base + 17] = float(vdict.get('_last_throttle', 0.0) or 0.0)
			sbuf[base + 18] = float(vdict.get('_last_steer', 0.0) or 0.0)
			if has_look:
				sbuf[base + 19] = float(look[0])
				sbuf[base + 20] = float(look[1])
				sbuf[base + 21] = float(look[2])
			else:
				sbuf[base + 19] = 0.0
				sbuf[base + 20] = 0.0
				sbuf[base + 21] = 0.0
			_rot_eff = max(0.05, min(99.9, float(vdict.get('_offh_rot_speed_eff', 0.60) or 0.60)))
			_pspd = int(round(max(80.0, min(3000.0, float(vdict.get('_offh_proj_speed', 720.0) or 720.0)))))
			sbuf[base + 22] = float(_pspd * 100.0 + _rot_eff)
			sbuf[base + 23] = float(flags)
			eid_int = int(eid)
			eid_to_idx[eid_int] = count
			idx_to_eid.append(eid_int)
			count += 1
			if count >= MAX_VEHICLES:
				break

		if count == 0:
			return False

		try:
			self.native.step_physics_space(count, float(render_dt), sbuf, self.sub_out_buf)
		except Exception:
			return False

		sout = self.sub_out_buf
		c_los = self.cached_los_mask
		c_cor = self.cached_corridor
		c_ram = self.cached_ram_id
		c_los.clear()
		c_cor.clear()
		c_ram.clear()
		for i in xrange(count):
			eid_i = idx_to_eid[i]
			base = i * SUB_OUT_STRIDE
			c_los[eid_i] = int(sout[base + 11]) | (int(sout[base + 12]) << 16)
			c_cor[eid_i] = int(sout[base + 14]) & 0xFFFF
			c_ram[eid_i] = int(sout[base + 15])

		self.last_frame_count = count
		return True

	def has_los_cached(self, observer_eid, target_eid):
		mask = self.cached_los_mask.get(int(observer_eid))
		if mask is None:
			return None
		j = self.eid_to_idx.get(int(target_eid))
		if j is None:
			return None
		return bool(mask & (1 << j))

	def get_ground_cached(self, eid):
		i = self.eid_to_idx.get(int(eid))
		if i is None:
			return None
		base = i * SUB_OUT_STRIDE
		sout = self.sub_out_buf
		return (float(sout[base + 13]), float(sout[base + 5]), float(sout[base + 6]), 0.0, 0.0, 0.0)

	def get_turret_gun_cached(self, eid):
		i = self.eid_to_idx.get(int(eid))
		if i is None:
			return None
		base = i * SUB_OUT_STRIDE
		sout = self.sub_out_buf
		corr_raw = int(sout[base + 14])
		aim_status = (corr_raw >> 16) & 3
		return (
			float(sout[base + 7]), float(sout[base + 8]),
			float(sout[base + 9]), float(sout[base + 10]),
			bool(aim_status & 1), bool(aim_status & 2))

	def get_corridor_mask_cached(self, eid):
		return self.cached_corridor.get(int(eid))

	def get_render_pose_cached(self, eid):
		return self.get_interpolated_transform(eid)

	def step_bot_aim_native(self, cur_tyaw, cur_gpitch, sx, sy, sz, hyaw, hpitch, hroll, tx, ty, tz, tvx, tvy, tvz, pspd, grav, is_arty, has_ct, diff_int, rot_spd, g_rspd, g_min, g_max, t_min, t_max, has_lim, dt, phase):
		if self.native is None:
			return None
		try:
			fn = getattr(self.native, 'native_step_bot_aim', None)
			if fn is not None:
				ibuf = getattr(self, '_aim_in_buf', None)
				obuf = getattr(self, '_aim_out_buf', None)
				if ibuf is None or obuf is None:
					ibuf = array.array('f', [0.0] * 20)
					obuf = array.array('f', [0.0] * 8)
					self._aim_in_buf = ibuf
					self._aim_out_buf = obuf
				ibuf[0] = float(sx)
				ibuf[1] = float(sy)
				ibuf[2] = float(sz)
				ibuf[3] = float(hyaw)
				ibuf[4] = float(hpitch)
				ibuf[5] = float(hroll)
				ibuf[6] = float(cur_tyaw)
				ibuf[7] = float(cur_gpitch)
				ibuf[8] = float(tx)
				ibuf[9] = float(ty)
				ibuf[10] = float(tz)
				ibuf[11] = float(rot_spd)
				ibuf[12] = float(g_rspd)
				ibuf[13] = float(t_min)
				ibuf[14] = float(t_max)
				ibuf[15] = float(g_min)
				ibuf[16] = float(g_max)
				ibuf[17] = float(pspd)
				ibuf[18] = float(dt)
				pflags = (int(diff_int) & 3) | (4 if has_lim else 0) | (8 if has_ct else 0) | (16 if is_arty else 0)
				ibuf[19] = float(pflags)
				obuf[7] = 0.0
				fn(ibuf, obuf)
				if obuf[7] > 0.5:
					return (
						float(obuf[0]), float(obuf[1]),
						float(obuf[2]), float(obuf[3]),
						int(obuf[4]), float(obuf[5]),
						float(phase) + float(dt))
		except Exception:
			pass
		return None

	def collide_segment(self, space_id, x0, y0, z0, x1, y1, z1, skip_flags=128):
		if self.native is None:
			return None
		try:
			fn = getattr(self.native, 'native_collide_segment', None)
			if fn is not None:
				return fn(int(space_id), float(x0), float(y0), float(z0), float(x1), float(y1), float(z1), int(skip_flags))
		except Exception:
			pass
		return None


_GLOBAL_COORDINATOR = None


def get_coordinator():
	global _GLOBAL_COORDINATOR
	if _GLOBAL_COORDINATOR is None:
		_GLOBAL_COORDINATOR = NativeMultithreadCoordinator()
	return _GLOBAL_COORDINATOR
