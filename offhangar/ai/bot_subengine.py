# -*- coding: utf-8 -*-
"""
WoT Offhangar Native Bot Subengine & Live 360-Degree 3D Tactical Radar (Self-Contained)
Runs directly inside the World of Tanks client process with ZERO external dependencies.
Loads all 33 maps' 3D solid building/rock/fortification OBB geometry from offhangar/maps_obb.json
and 3D terrain elevation + OFFHNAV1 surface relief grids, computing sub-millisecond
16-sector 360-degree hull obstacle/slope/elevation vision (front, flanks, and rear),
continuous uphill/downhill/cross-slope grading, 3D gun-barrel/window line-of-sight rays,
and serving a 60 FPS HTML5 3D tactical radar on http://127.0.0.1:8768/
"""

import io
import json
import math
import os
import sys
import threading
import time

try:
    from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
except ImportError:
    from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    _UNICODE_TYPE = unicode
except NameError:
    _UNICODE_TYPE = str

HTTP_PORT = 8768
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_OFFHANGAR_DIR = os.path.abspath(os.path.join(_CURRENT_DIR, '..'))
_MAPS_OBB_PATH = os.path.join(_OFFHANGAR_DIR, 'maps_obb.json')

# 16-sector 360-degree unit circle offsets (every 22.5 deg = pi/8 rad from hull yaw):
# 0: 0 deg (Front), 1: +22.5 deg (FR inner), 2: +45 deg (FR diag), 3: +67.5 deg (FR wide),
# 4: +90 deg (Right beam), 5: +112.5 deg (RR wide), 6: +135 deg (RR diag), 7: +157.5 deg (RR inner),
# 8: 180 deg (Rear center), 9: -157.5 deg (RL inner), 10: -135 deg (RL diag), 11: -112.5 deg (RL wide),
# 12: -90 deg (Left beam), 13: -67.5 deg (FL wide), 14: -45 deg (FL diag), 15: -22.5 deg (FL inner)
_SECTOR_ANGLES = tuple(i * (math.pi / 8.0) for i in range(16))
_SECTOR_MAX_DIST = (
    13.0, 11.2, 9.8, 8.6,
    8.0,  8.2,  9.2, 9.8,
    10.2, 9.8,  9.2, 8.2,
    8.0,  8.6,  9.8, 11.2,
)


class SpatialOBBGrid(object):
    """High-speed 3D spatial hash grid for microsecond 360-degree ray-OBB and 3D terrain slope/elevation queries."""
    def __init__(self, cell_size=25.0):
        self.cell_size = float(cell_size)
        self.inv_cell = 1.0 / float(cell_size)
        self.solid_grid = {}
        self.whisker_grid = {}
        self.solid_obbs = []  # (cx, cz, hw, hl, cos_yaw, sin_yaw, rad, minY_pad, maxY_pad)
        self.all_obbs = []
        self.bounds = [-500.0, -500.0, 500.0, 500.0]
        self.nav_ox = -500.0
        self.nav_oz = -500.0
        self.nav_cs = 4.0
        self.nav_inv_cs = 0.25
        self.nav_w = 0
        self.nav_h = 0
        self.nav_valid = None
        self.nav_hazards = None
        self.nav_near_haz = None
        self.nav_heights_mm = None
        self.nav_links = None
        self.mesh3d_ox = -600.0
        self.mesh3d_oz = -600.0
        self.mesh3d_step = 0.0
        self.mesh3d_inv_step = 0.0
        self.mesh3d_w = 0
        self.mesh3d_h = 0
        self.mesh3d_flags = None
        self.mesh3d_heights = None

    def set_geometry(self, bounds, obb_list):
        self.bounds = bounds or [-500.0, -500.0, 500.0, 500.0]
        self.solid_obbs = []
        self.solid_grid.clear()
        self.whisker_grid.clear()
        self.all_obbs = obb_list or []
        inv_cs = self.inv_cell
        for item in self.all_obbs:
            if len(item) >= 5:
                is_destr = int(item[7]) if len(item) >= 8 else 0
                if is_destr:
                    continue
                x, z, hw, hl, yaw = float(item[0]), float(item[1]), float(item[2]), float(item[3]), float(item[4])
                minY = float(item[5]) if len(item) >= 6 else -100.0
                maxY = float(item[6]) if len(item) >= 7 else 100.0
                rad = math.hypot(hw, hl)
                # BigWorld yaw convention: forward (+Z) is (sin(yaw), cos(yaw)), right (+X) is (cos(yaw), -sin(yaw)).
                # Local projection: lx = dx * cos(yaw) - dz * sin(yaw), lz = dx * sin(yaw) + dz * cos(yaw).
                c = math.cos(yaw)
                s = math.sin(yaw)
                idx = len(self.solid_obbs)
                # Pad vertical bounds by +-1.35m so low obstacles (dragon's teeth, boulders, bunkers) on sloped ground never slip under/over hull rays
                self.solid_obbs.append((x, z, hw, hl, c, s, rad, minY - 1.35, maxY + 1.35))
                min_cx = int(math.floor((x - rad - 1.0) * inv_cs))
                max_cx = int(math.floor((x + rad + 1.0) * inv_cs))
                min_cz = int(math.floor((z - rad - 1.0) * inv_cs))
                max_cz = int(math.floor((z + rad + 1.0) * inv_cs))
                for cx in range(min_cx, max_cx + 1):
                    for cz in range(min_cz, max_cz + 1):
                        k = (cx, cz)
                        lst = self.solid_grid.get(k)
                        if lst is None:
                            self.solid_grid[k] = [idx]
                        else:
                            lst.append(idx)
                w_pad = rad + 14.5
                w_min_cx = int(math.floor((x - w_pad) * inv_cs))
                w_max_cx = int(math.floor((x + w_pad) * inv_cs))
                w_min_cz = int(math.floor((z - w_pad) * inv_cs))
                w_max_cz = int(math.floor((z + w_pad) * inv_cs))
                for cx in range(w_min_cx, w_max_cx + 1):
                    for cz in range(w_min_cz, w_max_cz + 1):
                        k = (cx, cz)
                        lst = self.whisker_grid.get(k)
                        if lst is None:
                            self.whisker_grid[k] = [idx]
                        else:
                            lst.append(idx)

    def set_navgraph(self, graph):
        if not isinstance(graph, dict):
            self.nav_w = 0
            self.nav_h = 0
            self.nav_valid = None
            self.nav_hazards = None
            self.nav_near_haz = None
            self.nav_heights_mm = None
            self.nav_links = None
            self.mesh3d_w = 0
            self.mesh3d_h = 0
            self.mesh3d_flags = None
            self.mesh3d_heights = None
            return
        try:
            orig = graph.get('origin') or (-500.0, -500.0)
            self.nav_ox = float(orig[0])
            self.nav_oz = float(orig[1])
            self.nav_cs = float(graph.get('cell_size', 4.0) or 4.0)
            self.nav_inv_cs = 1.0 / self.nav_cs
            self.nav_w = int(graph.get('width', 0) or 0)
            self.nav_h = int(graph.get('height', 0) or 0)
            self.nav_hazards = graph.get('hazards')
            heights_mm = graph.get('heights_mm')
            links = graph.get('links')
            self.nav_heights_mm = heights_mm
            self.nav_links = links
            mesh3d = graph.get('mesh3d')
            if isinstance(mesh3d, (tuple, list)) and len(mesh3d) == 7:
                self.mesh3d_ox = float(mesh3d[0])
                self.mesh3d_oz = float(mesh3d[1])
                self.mesh3d_step = max(0.5, float(mesh3d[2]))
                self.mesh3d_inv_step = 1.0 / self.mesh3d_step
                self.mesh3d_w = int(mesh3d[3])
                self.mesh3d_h = int(mesh3d[4])
                self.mesh3d_flags = mesh3d[5]
                self.mesh3d_heights = mesh3d[6]
            else:
                self.mesh3d_w = 0
                self.mesh3d_h = 0
                self.mesh3d_flags = None
                self.mesh3d_heights = None
            if self.nav_w > 0 and self.nav_h > 0 and self.nav_hazards and heights_mm and links:
                w = self.nav_w
                h = self.nav_h
                hz = self.nav_hazards
                val = bytearray(w * h)
                for idx in range(w * h):
                    if heights_mm[idx] is not None and not (hz[idx] & 3) and links[idx] != 0:
                        val[idx] = 1
                self.nav_valid = val
                near = bytearray(w * h)
                for z in range(h):
                    row = z * w
                    for x in range(w):
                        idx = row + x
                        if (hz[idx] & 1):
                            for dz in (-1, 0, 1):
                                nz = z + dz
                                if 0 <= nz < h:
                                    nrow = nz * w
                                    for dx in (-1, 0, 1):
                                        nx = x + dx
                                        if 0 <= nx < w:
                                            near[nrow + nx] = 1
                self.nav_near_haz = near
            else:
                self.nav_near_haz = None
        except Exception:
            self.nav_w = 0
            self.nav_near_haz = None

    def is_near_hazard(self, rx, rz):
        if not self.nav_near_haz or self.nav_w <= 0:
            return False
        cx = int(math.floor((rx - self.nav_ox) * self.nav_inv_cs + 0.5))
        cz = int(math.floor((rz - self.nav_oz) * self.nav_inv_cs + 0.5))
        if 0 <= cx < self.nav_w and 0 <= cz < self.nav_h:
            return bool(self.nav_near_haz[cz * self.nav_w + cx])
        return True

    def sample_elevation_3d(self, wx, wz, fallback_y=0.0):
        """Return (ground_y, surface_y, is_solid_or_fatal) at world (wx, wz) in O(1)."""
        gy = float(fallback_y)
        sy = gy
        fatal = False
        hm = None
        if self.nav_w > 0 and self.nav_heights_mm:
            cx = int(math.floor((wx - self.nav_ox) * self.nav_inv_cs + 0.5))
            cz = int(math.floor((wz - self.nav_oz) * self.nav_inv_cs + 0.5))
            if 0 <= cx < self.nav_w and 0 <= cz < self.nav_h:
                idx = cz * self.nav_w + cx
                hm = self.nav_heights_mm[idx]
                hz = int(self.nav_hazards[idx]) if self.nav_hazards else 0
                if (hz & 3) != 0 or not (self.nav_valid and self.nav_valid[idx]):
                    fatal = True
                if hm is not None:
                    gy = float(hm) * 0.001
                    sy = gy
            else:
                fatal = True
        if self.mesh3d_w > 0 and self.mesh3d_heights:
            mx = int((wx - self.mesh3d_ox) * self.mesh3d_inv_step)
            mz = int((wz - self.mesh3d_oz) * self.mesh3d_inv_step)
            if 0 <= mx < self.mesh3d_w and 0 <= mz < self.mesh3d_h:
                midx = mz * self.mesh3d_w + mx
                mfl = int(self.mesh3d_flags[midx]) if self.mesh3d_flags else 1
                if mfl:
                    mh = float(self.mesh3d_heights[midx])
                    if hm is None:
                        gy = mh
                        sy = mh
                        if (mfl & 2) != 0 and self.nav_w <= 0:
                            fatal = True
                    else:
                        # If cell is a valid drivable road/bridge/underpass in navgraph, ignore overhead bridge/archway roofs (mh - gy > 1.8m)!
                        if abs(mh - gy) <= 1.8:
                            sy = mh
        return gy, sy, fatal

    def cast_hazard_ray(self, rx, rz, rdx, rdz, max_d):
        if not self.nav_hazards or not self.nav_valid or self.nav_w <= 0:
            return max_d, None
        w = self.nav_w
        h = self.nav_h
        ox = self.nav_ox
        oz = self.nav_oz
        inv_cs = self.nav_inv_cs
        hz = self.nav_hazards
        val = self.nav_valid
        d = 2.0
        while d <= max_d:
            px = rx + rdx * d
            pz = rz + rdz * d
            cx = int(math.floor((px - ox) * inv_cs + 0.5))
            cz = int(math.floor((pz - oz) * inv_cs + 0.5))
            if cx < 0 or cx >= w or cz < 0 or cz >= h:
                return d, (round(px, 2), round(pz, 2))
            idx = cz * w + cx
            if (hz[idx] & 3) or not val[idx]:
                return d, (round(px, 2), round(pz, 2))
            d += 2.0
        return max_d, None

    def local_solid_obbs(self, rx, ry, rz):
        """Return solid building/rock/fortification OBBs within 14.5m of (rx, rz) that overlap hull height + slope."""
        indices = self.whisker_grid.get((int(math.floor(rx * self.inv_cell)), int(math.floor(rz * self.inv_cell))))
        if not indices:
            return ()
        obbs = self.solid_obbs
        hy = ry + 0.9
        res = []
        for idx in indices:
            o = obbs[idx]
            # Allow +-3.2m vertical window around hull height so obstacles on steep uphill/downhill slopes are included
            if (hy + 3.2) < o[7] or (hy - 3.2) > o[8]:
                continue
            dx = rx - o[0]
            dz = rz - o[1]
            lim = o[6] + 14.5
            if dx * dx + dz * dz <= lim * lim:
                res.append(o)
        return res

    def _test_obb_list(self, indices, checked, rx, rz, rdx, rdz, best_d, ry, rdy):
        obbs = self.solid_obbs
        hit_pt = None
        for idx in indices:
            if checked is not None:
                if idx in checked:
                    continue
                checked.add(idx)
            cx_o, cz_o, hw, hl, c, s, rad, minY_pad, maxY_pad = obbs[idx]
            dx = rx - cx_o
            dz = rz - cz_o
            proj = -(dx * rdx + dz * rdz)
            if proj < -rad or proj > best_d + rad:
                continue
            perp_sq = (dx * dx + dz * dz) - proj * proj
            if perp_sq > rad * rad:
                continue
            lx = dx * c - dz * s
            lz = dx * s + dz * c
            ldx = rdx * c - rdz * s
            ldz = rdx * s + rdz * c
            if abs(ldx) < 1e-6:
                if abs(lx) > hw:
                    continue
                t1x, t2x = -1e9, 1e9
            else:
                inv_x = 1.0 / ldx
                t1x = (-hw - lx) * inv_x
                t2x = (hw - lx) * inv_x
                if t1x > t2x:
                    t1x, t2x = t2x, t1x
            if abs(ldz) < 1e-6:
                if abs(lz) > hl:
                    continue
                t1z, t2z = -1e9, 1e9
            else:
                inv_z = 1.0 / ldz
                t1z = (-hl - lz) * inv_z
                t2z = (hl - lz) * inv_z
                if t1z > t2z:
                    t1z, t2z = t2z, t1z
            tmin = t1x if t1x > t1z else t1z
            tmax = t2x if t2x < t2z else t2z
            if tmin <= tmax and tmax >= 0.0 and 0.0 <= tmin < best_d:
                if ry is not None:
                    hit_y = ry + rdy * tmin
                    if hit_y < minY_pad or hit_y > maxY_pad:
                        continue
                best_d = tmin
                hit_pt = (round(rx + rdx * tmin, 2), round(rz + rdz * tmin, 2))
        return best_d, hit_pt

    def raycast(self, rx, rz, rdx, rdz, max_d, ry=None, rdy=0.0, solid_only=True):
        best_d = float(max_d)
        hit_pt = None
        inv_cs = self.inv_cell
        grid = self.solid_grid
        if not grid:
            return (max_d, None)

        checked = set()
        steps = int(max_d / 18.0) + 1
        step_len = max_d / float(steps)
        prev_cell = None
        for st in range(steps + 1):
            t_cur = st * step_len
            if t_cur > best_d + 18.0:
                break
            cx = int(math.floor((rx + rdx * t_cur) * inv_cs))
            cz = int(math.floor((rz + rdz * t_cur) * inv_cs))
            cell = (cx, cz)
            if cell == prev_cell:
                continue
            prev_cell = cell
            indices = grid.get(cell)
            if not indices:
                continue
            nd, npt = self._test_obb_list(indices, checked, rx, rz, rdx, rdz, best_d, ry, rdy)
            if npt is not None:
                best_d, hit_pt = nd, npt
        return (best_d, hit_pt) if hit_pt else (max_d, None)


# Internal state singletons
_MAPS_DATABASE = None
_SPATIAL_GRID = SpatialOBBGrid(25.0)
_CURRENT_MAP_NAME = ''
_HTTP_SERVER_INSTANCE = None
_RADAR_MAP_JSON = '{"map":"Standby","bounds":[-500,-500,500,500],"buildings":[]}'
_RADAR_STATE_JSON = '{"map":"Standby","vehicles":[],"timestamp":0}'
_LAST_TICK_TIME = 0.0
_LAST_RADAR_POLL_TIME = 0.0
_LOS_CACHE = {}       # (vid, tid) -> (rx, rz, tx, tz, now, los_clear, los_hit_pt, los_window)
_BYPASS_MEMORY = {}   # vid -> (side_sign, until_t)


def _load_maps_database():
    global _MAPS_DATABASE
    if _MAPS_DATABASE is not None:
        return _MAPS_DATABASE
    if os.path.isfile(_MAPS_OBB_PATH):
        try:
            with open(_MAPS_OBB_PATH, 'r') as f:
                _MAPS_DATABASE = json.load(f)
        except Exception:
            _MAPS_DATABASE = {}
    else:
        _MAPS_DATABASE = {}
    return _MAPS_DATABASE


def set_active_map(raw_map_name):
    global _CURRENT_MAP_NAME, _RADAR_MAP_JSON
    if not raw_map_name:
        return
    short_name = str(raw_map_name).replace('\\', '/').rstrip('/').split('/')[-1].lower()
    if short_name == _CURRENT_MAP_NAME and len(_SPATIAL_GRID.all_obbs) > 0:
        return
    db = _load_maps_database()
    entry = db.get(short_name)
    if entry is None:
        for k in db:
            if k.endswith(short_name) or short_name.endswith(k):
                entry = db[k]
                short_name = k
                break
    bounds = entry.get('bounds', [-500.0, -500.0, 500.0, 500.0]) if entry else [-500.0, -500.0, 500.0, 500.0]
    obbs = entry.get('obbs', []) if entry else []
    _SPATIAL_GRID.set_geometry(bounds, obbs)
    try:
        from gui.mods.offhangar.ai import prebaked_navigation as _pbn
        _SPATIAL_GRID.set_navgraph(_pbn.load_graph(short_name))
    except Exception:
        _SPATIAL_GRID.set_navgraph(None)
    _CURRENT_MAP_NAME = short_name
    _LOS_CACHE.clear()
    _BYPASS_MEMORY.clear()
    _WHISKER_STATIC_CACHE.clear()
    try:
        raw_map_json = json.dumps({
            'map': short_name,
            'bounds': bounds,
            'buildings': obbs,
        })
        if isinstance(raw_map_json, _UNICODE_TYPE):
            raw_map_json = raw_map_json.encode('utf-8')
        _RADAR_MAP_JSON = raw_map_json
    except Exception:
        pass


def check_3d_sightline(start_pos, end_pos, barrel_height=1.45):
    """
    Fast 3D OBB sightline query for bot_ai.py (pockets, windows, arches, firing lanes).
    Returns True if a 3D ray at barrel_height (1.45m) or cupola height (1.85m) clears all solid 3D building OBBs.
    """
    try:
        if start_pos is None or end_pos is None or not _SPATIAL_GRID.solid_obbs:
            return True
        sx, sy, sz = float(start_pos[0]), float(start_pos[1]), float(start_pos[2])
        ex, ey, ez = float(end_pos[0]), float(end_pos[1]), float(end_pos[2])
        dx = ex - sx
        dz = ez - sz
        dist_2d = math.hypot(dx, dz)
        if dist_2d <= 2.0:
            return True
        nx = dx / dist_2d
        nz = dz / dist_2d
        slope_y = (ey - sy) / dist_2d
        _, hit_b = _SPATIAL_GRID.raycast(sx, sz, nx, nz, dist_2d - 1.2, ry=sy + float(barrel_height), rdy=slope_y, solid_only=True)
        if hit_b is None:
            return True
        _, hit_hi = _SPATIAL_GRID.raycast(sx, sz, nx, nz, dist_2d - 1.2, ry=sy + 1.85, rdy=slope_y, solid_only=True)
        return hit_hi is None
    except Exception:
        return True


def check_3d_sightline_detail(start_pos, end_pos, barrel_height=1.45):
    """
    Returns (los_clear, is_window_or_hulldown) in 3D:
    - los_clear: True if gun barrel (+1.45m) or cupola (+1.85m) ray clears all solid 3D OBBs.
    - is_window_or_hulldown: True if upper ray clears while lower hull ray (+0.68m) is shielded by a low wall/window sill!
    """
    try:
        if start_pos is None or end_pos is None or not _SPATIAL_GRID.solid_obbs:
            return (True, False)
        sx, sy, sz = float(start_pos[0]), float(start_pos[1]), float(start_pos[2])
        ex, ey, ez = float(end_pos[0]), float(end_pos[1]), float(end_pos[2])
        dx = ex - sx
        dz = ez - sz
        dist_2d = math.hypot(dx, dz)
        if dist_2d <= 2.0:
            return (True, False)
        nx = dx / dist_2d
        nz = dz / dist_2d
        slope_y = (ey - sy) / dist_2d
        _, hit_b = _SPATIAL_GRID.raycast(sx, sz, nx, nz, dist_2d - 1.2, ry=sy + float(barrel_height), rdy=slope_y, solid_only=True)
        if hit_b is None:
            _, hit_low = _SPATIAL_GRID.raycast(sx, sz, nx, nz, min(28.0, dist_2d - 1.2), ry=sy + 0.68, rdy=slope_y, solid_only=True)
            return (True, hit_low is not None)
        _, hit_hi = _SPATIAL_GRID.raycast(sx, sz, nx, nz, dist_2d - 1.2, ry=sy + 1.85, rdy=slope_y, solid_only=True)
        if hit_hi is None:
            return (True, True)
        return (False, False)
    except Exception:
        return (True, False)


def check_3d_drive_corridor(start_pos, end_pos, hull_pad=1.25):
    """
    Fast 3D hull driveability check for LocalDriver, dynamic LT/MT orbit arcs, and flanking plans:
    Verifies that centerline and lateral track shoulders (+-hull_pad) from start_pos to end_pos
    do NOT collide with solid 3D building/fortification/rock OBBs at hull height (+0.9m) and do NOT enter water or cliffs.
    """
    try:
        if start_pos is None or end_pos is None:
            return False
        sx, sy, sz = float(start_pos[0]), float(start_pos[1]), float(start_pos[2])
        ex, ey, ez = float(end_pos[0]), float(end_pos[1]), float(end_pos[2])
        dx = ex - sx
        dz = ez - sz
        dist_2d = math.hypot(dx, dz)
        if dist_2d <= 0.5:
            return not _SPATIAL_GRID.is_near_hazard(ex, ez)
        nx = dx / dist_2d
        nz = dz / dist_2d
        # Estimate end elevation from 3D terrain grid when end_pos[1] == start_pos[1]
        if abs(ey - sy) < 0.05 and _SPATIAL_GRID.nav_w > 0:
            ey_est, _, _ = _SPATIAL_GRID.sample_elevation_3d(ex, ez, ey)
            slope_y = (ey_est - sy) / dist_2d
        else:
            slope_y = (ey - sy) / dist_2d
        # 1. Check 3D water/cliff hazard ray along centerline
        _, haz_hit = _SPATIAL_GRID.cast_hazard_ray(sx, sz, nx, nz, dist_2d)
        if haz_hit is not None:
            return False
        # 2. Check 3D solid building/fortification OBBs along centerline and track shoulders (+-hull_pad)
        if _SPATIAL_GRID.solid_obbs:
            _, b_hit = _SPATIAL_GRID.raycast(sx, sz, nx, nz, dist_2d, ry=sy + 0.90, rdy=slope_y, solid_only=True)
            if b_hit is not None:
                return False
            if hull_pad > 0.2 and dist_2d <= 48.0:
                px, pz = -nz * float(hull_pad), nx * float(hull_pad)
                _, l_hit = _SPATIAL_GRID.raycast(sx + px, sz + pz, nx, nz, dist_2d, ry=sy + 0.90, rdy=slope_y, solid_only=True)
                if l_hit is not None:
                    return False
                _, r_hit = _SPATIAL_GRID.raycast(sx - px, sz - pz, nx, nz, dist_2d, ry=sy + 0.90, rdy=slope_y, solid_only=True)
                if r_hit is not None:
                    return False
        return True
    except Exception:
        return True


_WHISKER_STATIC_CACHE = {}  # vid -> (rx, rz, yaw, sec_dists, sec_hits, sec_hazs, sec_grades, sec_max_grades, sec_dy)
_FAST_OBB_CACHE = [None, None, ()]  # [ix, iz, obb_list]
_LAST_RADAR_JSON_TIME = 0.0
_SUB_TICK_COUNTER = 0


def fast_heading_obb_blocked(sx, sy, sz, rdx, rdz, max_d=8.0, hull_pad=0.9):
    """
    Ultra-fast O(1) local OBB heading check for LocalDriver._choose_yaw (called 15x per bot):
    Reuses the cached local OBB list for the bot's cell and tests (rdx, rdz) with zero allocations.
    """
    if not _SPATIAL_GRID.solid_obbs:
        return False
    ix = int(sx * 0.5)
    iz = int(sz * 0.5)
    if _FAST_OBB_CACHE[0] == ix and _FAST_OBB_CACHE[1] == iz:
        local_blds = _FAST_OBB_CACHE[2]
    else:
        local_blds = _SPATIAL_GRID.local_solid_obbs(sx, sy, sz)
        _FAST_OBB_CACHE[0] = ix
        _FAST_OBB_CACHE[1] = iz
        _FAST_OBB_CACHE[2] = local_blds
    if not local_blds:
        return False
    hy = sy + 0.90
    pad = float(hull_pad)
    for cx_o, cz_o, hw, hl, c, s, rad, minY_pad, maxY_pad in local_blds:
        if hy < minY_pad or hy > maxY_pad:
            continue
        dx = sx - cx_o
        dz = sz - cz_o
        proj = -(dx * rdx + dz * rdz)
        if proj < -rad or proj > max_d + rad:
            continue
        if (dx * dx + dz * dz) - proj * proj > (rad + pad) * (rad + pad):
            continue
        lx = dx * c - dz * s
        lz = dz * c + dx * s
        ldx = rdx * c - rdz * s
        ldz = rdz * c + rdx * s
        hw_p = hw + pad
        hl_p = hl + pad
        if abs(ldx) < 1e-6:
            if abs(lx) > hw_p:
                continue
            t1x, t2x = -1e9, 1e9
        else:
            inv_x = 1.0 / ldx
            t1x = (-hw_p - lx) * inv_x
            t2x = (hw_p - lx) * inv_x
            if t1x > t2x:
                t1x, t2x = t2x, t1x
        if abs(ldz) < 1e-6:
            if abs(lz) > hl_p:
                continue
            t1z, t2z = -1e9, 1e9
        else:
            inv_z = 1.0 / ldz
            t1z = (-hl_p - lz) * inv_z
            t2z = (hl_p - lz) * inv_z
            if t1z > t2z:
                t1z, t2z = t2z, t1z
        tmin = t1x if t1x > t1z else t1z
        tmax = t2x if t2x < t2z else t2z
        if tmin <= tmax and tmax >= 0.0 and 0.0 <= tmin < max_d:
            return True
    return False


def _cast_static_whisker(rx, rz, rdx, rdz, max_d, local_blds, check_haz=False, y0=0.0, fast_ring=False):
    """
    Static 3D world ray (terrain elevation/slope + hazards + solid 3D OBBs):
    Cached per bot until the hull moves >= 0.55m or rotates >= 0.045 rad.
    """
    best_d = float(max_d)
    hit_pt = None
    hit_haz = False

    prev_y = y0
    prev_d = 0.0
    max_local_grade = 0.0
    end_y = y0
    ring_steps = (4.2, float(max_d)) if (fast_ring or max_d < 9.0) else (3.5, 7.0, min(11.0, float(max_d)))
    for d_ring in ring_steps:
        if d_ring > best_d or d_ring <= prev_d + 0.2:
            continue
        px = rx + rdx * d_ring
        pz = rz + rdz * d_ring
        gy, sy, is_fatal = _SPATIAL_GRID.sample_elevation_3d(px, pz, prev_y)
        run = d_ring - prev_d
        loc_grade = (gy - prev_y) / run
        if abs(loc_grade) > abs(max_local_grade):
            max_local_grade = loc_grade
        step_relief = sy - gy
        if is_fatal or step_relief >= 1.25 or loc_grade > 0.43 or loc_grade < -0.38:
            if d_ring < best_d:
                best_d = d_ring
                hit_pt = (round(px, 2), round(pz, 2))
                hit_haz = True
            end_y = gy
            break
        prev_y = gy
        prev_d = d_ring
        end_y = gy

    slope_grade = (end_y - y0) / max(2.5, prev_d if prev_d > 0.5 else float(max_d))
    delta_y = end_y - y0

    if check_haz:
        hd, hpt = _SPATIAL_GRID.cast_hazard_ray(rx, rz, rdx, rdz, best_d)
        if hpt is not None and hd < best_d:
            best_d = hd
            hit_pt = hpt
            hit_haz = True

    if local_blds:
        for cx_o, cz_o, hw, hl, c, s, rad, minY_pad, maxY_pad in local_blds:
            dx = rx - cx_o
            dz = rz - cz_o
            proj = -(dx * rdx + dz * rdz)
            if proj < -rad or proj > best_d + rad:
                continue
            if (dx * dx + dz * dz) - proj * proj > rad * rad:
                continue
            lx = dx * c - dz * s
            lz = dx * s + dz * c
            ldx = rdx * c - rdz * s
            ldz = rdx * s + rdz * c
            if abs(ldx) < 1e-6:
                if abs(lx) > hw:
                    continue
                t1x, t2x = -1e9, 1e9
            else:
                inv_x = 1.0 / ldx
                t1x = (-hw - lx) * inv_x
                t2x = (hw - lx) * inv_x
                if t1x > t2x:
                    t1x, t2x = t2x, t1x
            if abs(ldz) < 1e-6:
                if abs(lz) > hl:
                    continue
                t1z, t2z = -1e9, 1e9
            else:
                inv_z = 1.0 / ldz
                t1z = (-hl - lz) * inv_z
                t2z = (hl - lz) * inv_z
                if t1z > t2z:
                    t1z, t2z = t2z, t1z
            tmin = t1x if t1x > t1z else t1z
            tmax = t2x if t2x < t2z else t2z
            if tmin <= tmax and tmax >= 0.0 and 0.0 <= tmin < best_d:
                ray_hy = y0 + 0.90 + slope_grade * tmin
                if ray_hy < minY_pad or ray_hy > maxY_pad:
                    continue
                best_d = tmin
                hit_pt = (round(rx + rdx * tmin, 2), round(rz + rdz * tmin, 2))
                hit_haz = False

    return best_d, hit_pt, hit_haz, slope_grade, max_local_grade, delta_y


def _test_dynamic_vehs_whisker(rx, rz, rdx, rdz, best_d, hit_pt, hit_haz, local_vehs, ignore_vid):
    hit_veh = False
    for v_id, vx, vy, vz, hw, hl, c, s, rad in local_vehs:
        if v_id == ignore_vid:
            continue
        dx = rx - vx
        dz = rz - vz
        proj = -(dx * rdx + dz * rdz)
        if proj < 0.0 or proj > best_d + rad:
            continue
        if (dx * dx + dz * dz) - proj * proj > rad * rad:
            continue
        lx = dx * c - dz * s
        lz = dx * s + dz * c
        ldx = rdx * c - rdz * s
        ldz = rdx * s + rdz * c
        if abs(ldx) < 1e-6:
            if abs(lx) > hw:
                continue
            t1x, t2x = -1e9, 1e9
        else:
            inv_x = 1.0 / ldx
            t1x = (-hw - lx) * inv_x
            t2x = (hw - lx) * inv_x
            if t1x > t2x:
                t1x, t2x = t2x, t1x
        if abs(ldz) < 1e-6:
            if abs(lz) > hl:
                continue
            t1z, t2z = -1e9, 1e9
        else:
            inv_z = 1.0 / ldz
            t1z = (-hl - lz) * inv_z
            t2z = (hl - lz) * inv_z
            if t1z > t2z:
                t1z, t2z = t2z, t1z
        tmin = t1x if t1x > t1z else t1z
        tmax = t2x if t2x < t2z else t2z
        if tmin <= tmax and tmax >= 0.0 and 0.0 <= tmin < best_d:
            best_d = tmin
            hit_pt = (round(rx + rdx * tmin, 2), round(rz + rdz * tmin, 2))
            hit_veh = True
            hit_haz = False
    return best_d, hit_pt, hit_veh, hit_haz


def _classify_slope(grade, max_loc_grade, c_haz):
    if c_haz or max_loc_grade > 0.42 or max_loc_grade < -0.38:
        return 'impassable_cliff'
    if grade >= 0.25 or max_loc_grade >= 0.28:
        return 'steep_climb'
    if grade >= 0.13:
        return 'moderate_climb'
    if grade >= 0.055:
        return 'gentle_up'
    if grade <= -0.20 or max_loc_grade <= -0.24:
        return 'steep_descent'
    if grade <= -0.06:
        return 'gentle_down'
    return 'flat'


def tick_battle_subengine(mock_vehicles, player_vehicle_id, map_name):
    """
    Main 3D subengine tick: computes 16-sector 360-degree hull vision (front, sides, rear),
    continuous 3D terrain elevation/slope profiling, forward & reverse steering/throttle guidance,
    and 3D barrel/window line-of-sight rays with per-bot static ray caching and zero FPS spikes.
    """
    global _LAST_TICK_TIME, _RADAR_STATE_JSON, _LAST_RADAR_JSON_TIME, _SUB_TICK_COUNTER
    now = time.time()
    _SUB_TICK_COUNTER += 1
    if map_name:
        set_active_map(map_name)

    items = list(mock_vehicles.iteritems() if hasattr(mock_vehicles, 'iteritems') else mock_vehicles.items())
    poses = {}
    veh_obbs = []
    for vid, v in items:
        pos = getattr(v, 'position', None)
        if pos is None:
            continue
        px, py, pz = float(pos.x), float(pos.y), float(pos.z)
        is_alive = bool(getattr(v, 'isAlive', True) and (getattr(v, 'health', 0) or 0) > 0)
        if is_alive:
            poses[vid] = (px, py, pz)
        if is_alive or getattr(v, '_wreck_done', False):
            vyaw = float(getattr(v, 'yaw', 0.0) or 0.0)
            veh_obbs.append((vid, px, py, pz, 1.75, 3.45, math.cos(vyaw), math.sin(vyaw), 3.9))

    radar_active = ((now - _LAST_RADAR_POLL_TIME) <= 3.0) and ((now - _LAST_RADAR_JSON_TIME) >= 0.16)
    radar_vehicles = [] if radar_active else None
    p_pose = poses.get(player_vehicle_id)
    tick_parity = _SUB_TICK_COUNTER & 1

    for vid, v in items:
        pos = getattr(v, 'position', None)
        if pos is None:
            continue
        rx = float(pos.x)
        ry = float(getattr(pos, 'y', 0.0) or 0.0)
        rz = float(pos.z)
        yaw = float(getattr(v, 'yaw', 0.0) or 0.0)
        speed = float(getattr(v, '_veh_velocity', 0.0) or 0.0)
        is_alive = bool(getattr(v, 'isAlive', True) and (getattr(v, 'health', 0) or 0) > 0)
        tid = getattr(v, '_ai_target_id', None)
        ai_ord = getattr(v, '_offh_last_order_dict', None)
        if not isinstance(ai_ord, dict):
            oc = getattr(v, '_offh_ai_order_cache', None)
            if isinstance(oc, tuple) and len(oc) >= 3 and isinstance(oc[2], dict):
                ai_ord = oc[2]
        c_mode = str(ai_ord.get('combat_mode', 'route')) if isinstance(ai_ord, dict) else 'route'
        ignore_vid = ai_ord.get('wreck_cover_id') if isinstance(ai_ord, dict) else None
        if c_mode in ('ram_brawl', 'clinch', 'cliff_dive_ram') and tid is not None:
            ignore_vid = tid

        # If not building radar snapshot, stagger distant (>165m from player) non-engaged bots across 2 slices
        prev_guid = getattr(v, '_subengine_guidance', None)
        if not radar_active and is_alive and prev_guid is not None and vid != player_vehicle_id and tid is None:
            if p_pose is not None and ((rx - p_pose[0]) ** 2 + (rz - p_pose[2]) ** 2) > 27225.0:
                if (int(vid) & 1) != tick_parity and not prev_guid.get('wall_warning'):
                    continue

        steer_bias = 0.0
        throttle_cap = 1.0
        rear_steer_bias = 0.0
        rear_throttle_cap = 1.0
        rear_clear_dist = 10.2
        wall_warning = False
        c_dist, c_hit, c_veh = 13.0, None, False
        l_dist, l_hit = 11.2, None
        r_dist, r_hit = 11.2, None
        fl_dist, fl_hit = 9.8, None
        fr_dist, fr_hit = 9.8, None
        b_dist, b_hit = 10.2, None
        rl_dist, rl_hit = 9.2, None
        rr_dist, rr_hit = 9.2, None
        slope_ahead = 0.0
        cross_slope = 0.0
        elev_ahead_m = 0.0
        slope_status = 'flat'
        best_360_yaw = yaw
        rays_16 = None
        los_clear = True
        los_hit_pt = None
        los_window = False

        if is_alive:
            local_vehs = [
                vo for vo in veh_obbs
                if vo[0] != vid and abs(vo[2] - ry) <= 3.2 and (vo[1] - rx) ** 2 + (vo[3] - rz) ** 2 <= 289.0
            ]
            # Check static 360-degree whisker cache (reused when hull moved < 0.55m and rotated < 0.045 rad)
            w_cached = _WHISKER_STATIC_CACHE.get(vid)
            if (w_cached is not None and
                    (rx - w_cached[0]) ** 2 + (rz - w_cached[1]) ** 2 < 0.3025 and
                    abs(math.atan2(math.sin(yaw - w_cached[2]), math.cos(yaw - w_cached[2]))) < 0.045):
                st_dists, st_hits, st_hazs, sec_grades, sec_max_grades, sec_dy = w_cached[3], w_cached[4], w_cached[5], w_cached[6], w_cached[7], w_cached[8]
            else:
                near_haz = _SPATIAL_GRID.is_near_hazard(rx, rz)
                local_blds = _SPATIAL_GRID.local_solid_obbs(rx, ry, rz)
                st_dists = [0.0] * 16
                st_hits = [None] * 16
                st_hazs = [False] * 16
                sec_grades = [0.0] * 16
                sec_max_grades = [0.0] * 16
                sec_dy = [0.0] * 16
                y0_veh, _, _ = _SPATIAL_GRID.sample_elevation_3d(rx, rz, ry)
                for s_idx in range(16):
                    s_yaw = yaw + _SECTOR_ANGLES[s_idx]
                    s_max_d = _SECTOR_MAX_DIST[s_idx]
                    sd, spt, shaz, sgr, smgr, sdy = _cast_static_whisker(
                        rx, rz, math.sin(s_yaw), math.cos(s_yaw), s_max_d,
                        local_blds, near_haz, y0=y0_veh, fast_ring=(s_idx & 1) != 0)
                    st_dists[s_idx] = sd
                    st_hits[s_idx] = spt
                    st_hazs[s_idx] = shaz
                    sec_grades[s_idx] = sgr
                    sec_max_grades[s_idx] = smgr
                    sec_dy[s_idx] = sdy
                _WHISKER_STATIC_CACHE[vid] = (rx, rz, yaw, st_dists, st_hits, st_hazs, sec_grades, sec_max_grades, sec_dy)

            if local_vehs:
                sec_dists = [0.0] * 16
                sec_hits = [None] * 16
                sec_vehs = [False] * 16
                sec_hazs = [False] * 16
                for s_idx in range(16):
                    s_yaw = yaw + _SECTOR_ANGLES[s_idx]
                    sd, spt, sveh, shaz = _test_dynamic_vehs_whisker(
                        rx, rz, math.sin(s_yaw), math.cos(s_yaw),
                        st_dists[s_idx], st_hits[s_idx], st_hazs[s_idx],
                        local_vehs, ignore_vid)
                    sec_dists[s_idx] = sd
                    sec_hits[s_idx] = spt
                    sec_vehs[s_idx] = sveh
                    sec_hazs[s_idx] = shaz
            else:
                sec_dists = st_dists
                sec_hits = st_hits
                sec_vehs = [False] * 16
                sec_hazs = st_hazs

            if radar_active:
                rays_16 = []
                for s_idx in range(16):
                    shaz = sec_hazs[s_idx]
                    spt = sec_hits[s_idx]
                    sgr = sec_grades[s_idx]
                    r_flag = 2 if shaz else (1 if spt is not None else (3 if sgr >= 0.16 else 0))
                    rays_16.append([round(sec_dists[s_idx], 1), round(sgr, 2), r_flag])

            c_dist, c_hit, c_veh, c_haz = sec_dists[0], sec_hits[0], sec_vehs[0], sec_hazs[0]
            r_dist, r_hit, _, r_haz = sec_dists[1], sec_hits[1], sec_vehs[1], sec_hazs[1]
            fr_dist, fr_hit, _, fr_haz = sec_dists[2], sec_hits[2], sec_vehs[2], sec_hazs[2]
            l_dist, l_hit, _, l_haz = sec_dists[15], sec_hits[15], sec_vehs[15], sec_hazs[15]
            fl_dist, fl_hit, _, fl_haz = sec_dists[14], sec_hits[14], sec_vehs[14], sec_hazs[14]

            b_dist, b_hit, _, b_haz = sec_dists[8], sec_hits[8], sec_vehs[8], sec_hazs[8]
            rr_dist, rr_hit, _, rr_haz = min(sec_dists[7], sec_dists[6]), (sec_hits[7] or sec_hits[6]), False, (sec_hazs[7] or sec_hazs[6])
            rl_dist, rl_hit, _, rl_haz = min(sec_dists[9], sec_dists[10]), (sec_hits[9] or sec_hits[10]), False, (sec_hazs[9] or sec_hazs[10])
            rear_clear_dist = min(b_dist, sec_dists[7], sec_dists[9])

            slope_ahead = sec_grades[0]
            elev_ahead_m = sec_dy[0]
            cross_slope = (sec_dy[4] - sec_dy[12]) / 14.0
            slope_status = _classify_slope(slope_ahead, sec_max_grades[0], c_haz)

            def _eff_space(idx_s, w_s):
                d_s = sec_dists[idx_s]
                hz_s = sec_hazs[idx_s]
                gr_s = max(0.0, sec_grades[idx_s])
                slope_factor = max(0.35, 1.0 - max(0.0, gr_s - 0.12) * 2.2)
                return d_s * w_s * (0.25 if hz_s else slope_factor)

            left_space = _eff_space(15, 1.0) + _eff_space(14, 0.75) + _eff_space(13, 0.45) + _eff_space(12, 0.25)
            right_space = _eff_space(1, 1.0) + _eff_space(2, 0.75) + _eff_space(3, 0.45) + _eff_space(4, 0.25)

            if l_hit is not None and l_dist < 8.0 and left_space < right_space and not (r_haz and r_dist < 8.5):
                steer_bias += (1.0 - (l_dist / 8.0)) * (0.95 if l_haz else 0.80)
                wall_warning = True
            elif r_hit is not None and r_dist < 8.0 and right_space < left_space and not (l_haz and l_dist < 8.5):
                steer_bias -= (1.0 - (r_dist / 8.0)) * (0.95 if r_haz else 0.80)
                wall_warning = True
            elif fl_hit is not None and fl_dist < 6.2 and left_space < right_space:
                steer_bias += (1.0 - (fl_dist / 6.2)) * 0.55
                wall_warning = True
            elif fr_hit is not None and fr_dist < 6.2 and right_space < left_space:
                steer_bias -= (1.0 - (fr_dist / 6.2)) * 0.55
                wall_warning = True

            if not wall_warning and slope_ahead >= 0.16 and abs(steer_bias) < 0.15:
                l_diag_grade = min(sec_grades[15], sec_grades[14])
                r_diag_grade = min(sec_grades[1], sec_grades[2])
                if l_diag_grade < slope_ahead - 0.04 and l_dist > 8.5 and not l_haz and l_diag_grade <= r_diag_grade:
                    steer_bias = -0.36
                elif r_diag_grade < slope_ahead - 0.04 and r_dist > 8.5 and not r_haz:
                    steer_bias = 0.36

            if c_hit is not None and c_dist < (11.0 if c_haz else 9.0):
                wall_warning = True
                mem = _BYPASS_MEMORY.get(vid)
                if mem is not None and now < mem[1] and not c_haz:
                    side_sign = mem[0]
                else:
                    side_sign = -1.0 if left_space >= right_space else 1.0
                    _BYPASS_MEMORY[vid] = (side_sign, now + (1.4 if c_haz else 2.2))
                if c_haz:
                    steer_bias = side_sign * max(0.78, min(1.0, (11.0 - c_dist) / 6.5))
                    throttle_cap = 0.55 if c_dist > 5.0 else 0.25
                elif c_veh:
                    steer_bias = side_sign * max(0.65, min(1.0, (9.5 - c_dist) / 6.0))
                    throttle_cap = 0.85 if c_dist > 3.2 else 0.50
                elif c_dist < 6.2:
                    throttle_cap = max(0.32, c_dist / 6.2)
                    if abs(steer_bias) < 0.40:
                        steer_bias = side_sign * 0.90

            steer_bias = max(-1.0, min(1.0, steer_bias))

            rear_left_space = _eff_space(9, 1.0) + _eff_space(10, 0.80) + _eff_space(11, 0.45)
            rear_right_space = _eff_space(7, 1.0) + _eff_space(6, 0.80) + _eff_space(5, 0.45)
            if rear_clear_dist < 6.5:
                rear_throttle_cap = 0.0 if rear_clear_dist < 2.2 else max(0.35, min(1.0, rear_clear_dist / 6.5))
                rear_steer_bias = -0.85 if rear_left_space >= rear_right_space else 0.85
            elif abs(rear_left_space - rear_right_space) > 2.5:
                rear_steer_bias = -0.35 if rear_left_space > rear_right_space else 0.35

            best_360_score = -1e9
            for s_idx in range(16):
                s_ang = _SECTOR_ANGLES[s_idx]
                if s_ang > math.pi:
                    s_ang -= 2.0 * math.pi
                d_norm = sec_dists[s_idx] / _SECTOR_MAX_DIST[s_idx]
                pen_haz = -2.5 if sec_hazs[s_idx] else (-0.8 if sec_hits[s_idx] is not None else 0.0)
                pen_slope = -max(0.0, abs(sec_grades[s_idx]) - 0.12) * 2.5
                align_bonus = -abs(s_ang) * 0.32
                sc_360 = d_norm * 1.5 + pen_haz + pen_slope + align_bonus
                if sc_360 > best_360_score:
                    best_360_score = sc_360
                    best_360_yaw = yaw + s_ang

            if tid is not None and tid in poses:
                tx, ty, tz = poses[tid]
                l_key = (vid, tid)
                l_cached = _LOS_CACHE.get(l_key)
                if (l_cached is not None and (now - l_cached[4]) < 0.38 and
                        (rx - l_cached[0]) ** 2 + (rz - l_cached[1]) ** 2 < 1.96 and
                        (tx - l_cached[2]) ** 2 + (tz - l_cached[3]) ** 2 < 1.96):
                    los_clear, los_hit_pt, los_window = l_cached[5], l_cached[6], l_cached[7]
                else:
                    tdx = tx - rx
                    tdy = ty - ry
                    tdz = tz - rz
                    dist_tgt = math.hypot(tdx, tdz)
                    if dist_tgt > 1.5:
                        nx = tdx / dist_tgt
                        nz = tdz / dist_tgt
                        slope_y = tdy / dist_tgt
                        _, l_pt_b = _SPATIAL_GRID.raycast(rx, rz, nx, nz, dist_tgt - 1.5, ry=ry + 1.45, rdy=slope_y, solid_only=True)
                        if l_pt_b is None:
                            los_clear = True
                            los_hit_pt = None
                            if dist_tgt <= 165.0:
                                _, l_pt_low = _SPATIAL_GRID.raycast(rx, rz, nx, nz, min(28.0, dist_tgt - 1.5), ry=ry + 0.70, rdy=slope_y, solid_only=True)
                                los_window = (l_pt_low is not None)
                        elif dist_tgt <= 165.0:
                            _, l_pt_hi = _SPATIAL_GRID.raycast(rx, rz, nx, nz, dist_tgt - 1.5, ry=ry + 1.85, rdy=slope_y, solid_only=True)
                            if l_pt_hi is None:
                                los_clear = True
                                los_hit_pt = None
                                los_window = True
                            else:
                                los_clear = False
                                los_hit_pt = l_pt_b
                        else:
                            los_clear = False
                            los_hit_pt = l_pt_b
                    _LOS_CACHE[l_key] = (rx, rz, tx, tz, now, los_clear, los_hit_pt, los_window)

            v._subengine_guidance = {
                'steer_bias': round(steer_bias, 2),
                'throttle_cap': round(throttle_cap, 2),
                'rear_steer_bias': round(rear_steer_bias, 2),
                'rear_throttle_cap': round(rear_throttle_cap, 2),
                'rear_clear_dist': round(rear_clear_dist, 1),
                'wall_warning': wall_warning,
                'los_clear': los_clear,
                'los_window': los_window,
                'wall_dist': round(c_dist, 1),
                'slope_ahead': round(slope_ahead, 3),
                'cross_slope': round(cross_slope, 3),
                'elev_ahead_m': round(elev_ahead_m, 2),
                'slope_status': slope_status,
                'best_360_yaw': round(best_360_yaw, 3),
            }

        if not radar_active:
            continue
        _td = getattr(v, 'typeDescriptor', None)
        _tags = getattr(getattr(_td, 'type', None), 'tags', ()) or ()
        _cls = 'Tank'
        for _ct in ('heavyTank', 'mediumTank', 'AT-SPG', 'lightTank', 'SPG'):
            if _ct in _tags:
                _cls = _ct
                break

        _plan_pts = []
        _route_id = ''
        _tac_reason = ''
        _act_desc = ''
        _live_3d = getattr(v, '_offh_live_3d_path', None)
        if isinstance(ai_ord, dict):
            _route_id = str(ai_ord.get('route_id', '') or '')
            _tac_reason = str(ai_ord.get('tactic_reason', '') or '')
            _act_desc = str(ai_ord.get('action_desc', '') or '')
            _raw_pw = ai_ord.get('plan_waypoints')
            # For dynamic orbit / wolfpack encirclement, show the multi-step 3D orbit arc;
            # otherwise prefer the bot's actual live 3D A* trajectory (_offh_live_3d_path) around terrain/obstacles!
            if c_mode in ('circle_of_death', 'wolfpack_encircle') and isinstance(_raw_pw, (list, tuple)) and _raw_pw:
                for _wp_p in _raw_pw[:6]:
                    if isinstance(_wp_p, (list, tuple)) and len(_wp_p) >= 3:
                        _plan_pts.append([round(float(_wp_p[0]), 1), round(float(_wp_p[2]), 1)])
                    elif isinstance(_wp_p, (list, tuple)) and len(_wp_p) == 2:
                        _plan_pts.append([round(float(_wp_p[0]), 1), round(float(_wp_p[1]), 1)])
            elif isinstance(_live_3d, (list, tuple)) and _live_3d:
                for _wp_p in _live_3d[:7]:
                    if isinstance(_wp_p, (list, tuple)) and len(_wp_p) >= 3:
                        _plan_pts.append([round(float(_wp_p[0]), 1), round(float(_wp_p[2]), 1)])
                    elif isinstance(_wp_p, (list, tuple)) and len(_wp_p) == 2:
                        _plan_pts.append([round(float(_wp_p[0]), 1), round(float(_wp_p[1]), 1)])
            elif isinstance(_raw_pw, (list, tuple)) and _raw_pw:
                for _wp_p in _raw_pw[:6]:
                    if isinstance(_wp_p, (list, tuple)) and len(_wp_p) >= 3:
                        _plan_pts.append([round(float(_wp_p[0]), 1), round(float(_wp_p[2]), 1)])
                    elif isinstance(_wp_p, (list, tuple)) and len(_wp_p) == 2:
                        _plan_pts.append([round(float(_wp_p[0]), 1), round(float(_wp_p[1]), 1)])
            elif ai_ord.get('move_position') is not None:
                _mp_r = ai_ord.get('move_position')
                if isinstance(_mp_r, (list, tuple)) and len(_mp_r) >= 3:
                    _plan_pts.append([round(float(_mp_r[0]), 1), round(float(_mp_r[2]), 1)])

        radar_vehicles.append({
            'id': vid,
            'team': int(getattr(v, '_bot_team', getattr(v, 'team', 0))),
            'is_player': (vid == player_vehicle_id),
            'name': getattr(v, 'name', '') or getattr(getattr(_td, 'type', None), 'shortUserString', 'Bot'),
            'class': _cls,
            'mode': c_mode,
            'route_id': _route_id,
            'tactic_reason': _tac_reason,
            'action_desc': _act_desc,
            'plan_pts': _plan_pts,
            'x': round(rx, 2),
            'y': round(ry, 2),
            'z': round(rz, 2),
            'yaw': round(yaw, 3),
            'turret_yaw': round(float(getattr(v, '_turret_yaw', getattr(v, 'turret_yaw', 0.0)) or 0.0), 3),
            'gun_pitch': round(float(getattr(v, '_gun_pitch', getattr(v, 'gun_pitch', 0.0)) or 0.0), 3),
            'speed': round(speed, 1),
            'target_id': tid,
            'hp': int(getattr(v, 'health', 0) or 0),
            'max_hp': int(getattr(v, 'maxHealth', 100) or 100),
            'is_alive': is_alive,
            'c_dist': round(c_dist, 1), 'c_hit': c_hit is not None,
            'l_dist': round(l_dist, 1), 'l_hit': l_hit is not None,
            'r_dist': round(r_dist, 1), 'r_hit': r_hit is not None,
            'fl_dist': round(fl_dist, 1), 'fl_hit': fl_hit is not None,
            'fr_dist': round(fr_dist, 1), 'fr_hit': fr_hit is not None,
            'b_dist': round(b_dist, 1), 'b_hit': b_hit is not None,
            'rl_dist': round(rl_dist, 1), 'rl_hit': rl_hit is not None,
            'rr_dist': round(rr_dist, 1), 'rr_hit': rr_hit is not None,
            'rays_16': rays_16,
            'slope_ahead': round(slope_ahead, 3),
            'cross_slope': round(cross_slope, 3),
            'elev_ahead_m': round(elev_ahead_m, 2),
            'slope_status': slope_status,
            'steer_bias': round(steer_bias, 2),
            'throttle_cap': round(throttle_cap, 2),
            'rear_clear_dist': round(rear_clear_dist, 1),
            'wall_warning': wall_warning,
            'los_clear': los_clear,
            'los_window': los_window,
            'los_hit_pt': los_hit_pt,
        })

    if radar_active:
        try:
            s_json = json.dumps({
                'map': _CURRENT_MAP_NAME or 'Standby',
                'vehicles': radar_vehicles,
                'timestamp': round(now, 3),
            })
            if isinstance(s_json, _UNICODE_TYPE):
                s_json = s_json.encode('utf-8')
            _RADAR_STATE_JSON = s_json
            _LAST_RADAR_JSON_TIME = now
        except Exception:
            pass
    _LAST_TICK_TIME = now


class SubengineHTTPHandler(BaseHTTPRequestHandler):
    """Zero-lock-contention internal HTTP server serving the Live 3D Tactical Radar."""
    def do_GET(self):
        global _LAST_RADAR_POLL_TIME
        _LAST_RADAR_POLL_TIME = time.time()
        try:
            if self.path == '/' or self.path.startswith('/view'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                cur_m = _CURRENT_MAP_NAME or 'Standby'
                html = VIEWER_HTML.replace('__MAP_NAME__', cur_m)
                if isinstance(html, _UNICODE_TYPE):
                    html = html.encode('utf-8')
                self.wfile.write(html)
            elif self.path.startswith('/map_geom'):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(_RADAR_MAP_JSON)
            elif self.path.startswith('/state'):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(_RADAR_STATE_JSON)
            else:
                self.send_response(404)
                self.end_headers()
        except Exception:
            pass

    def log_message(self, format, *args):
        pass


def _http_server_thread():
    global _HTTP_SERVER_INSTANCE
    try:
        server = HTTPServer(('127.0.0.1', HTTP_PORT), SubengineHTTPHandler)
        _HTTP_SERVER_INSTANCE = server
        server.serve_forever()
    except Exception:
        pass


def ensure_http_radar_running():
    global _HTTP_SERVER_INSTANCE
    if _HTTP_SERVER_INSTANCE is None:
        t = threading.Thread(target=_http_server_thread, name="SubengineRadarHTTP")
        if hasattr(t, 'daemon'):
            t.daemon = True
        elif hasattr(t, 'setDaemon'):
            t.setDaemon(True)
        t.start()


def start_battle_subengine(map_name):
    """Called automatically when battle begins."""
    set_active_map(map_name)
    ensure_http_radar_running()


def stop_battle_subengine():
    """Called when battle ends."""
    global _RADAR_STATE_JSON
    _RADAR_STATE_JSON = '{"map":"Standby","vehicles":[],"timestamp":0}'


VIEWER_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Offhangar Subengine // 360-Deg 3D Live Tactical Radar</title>
    <style>
        * { box-sizing: border-box; }
        body { margin: 0; padding: 0; background: #0c0f14; color: #eee; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; overflow: hidden; user-select: none; }
        #canvas { display: block; width: 100vw; height: 100vh; cursor: crosshair; }
        #ui { position: absolute; top: 16px; left: 16px; background: rgba(16, 21, 29, 0.92); border: 1px solid #273344; border-radius: 8px; padding: 14px 18px; pointer-events: auto; box-shadow: 0 8px 32px rgba(0,0,0,0.6); }
        h1 { margin: 0 0 6px 0; font-size: 15px; font-weight: 700; color: #00e1d9; letter-spacing: 0.8px; display: flex; align-items: center; gap: 8px; }
        .live-dot { width: 8px; height: 8px; background: #00ff88; border-radius: 50%; box-shadow: 0 0 8px #00ff88; animation: pulse 1.8s infinite; }
        @keyframes pulse { 0% { opacity: 0.4; } 50% { opacity: 1; } 100% { opacity: 0.4; } }
        .stat-row { font-size: 12px; color: #94a3b8; margin: 4px 0; display: flex; gap: 12px; }
        .stat-val { color: #f8fafc; font-weight: 600; }
        .legend { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 10px; font-size: 11px; padding-top: 8px; border-top: 1px solid #273344; max-width: 460px; }
        .tag { display: inline-flex; align-items: center; gap: 5px; color: #cbd5e1; }
        .dot { width: 9px; height: 9px; border-radius: 2px; }
        #toolbar { position: absolute; top: 16px; right: 16px; background: rgba(16, 21, 29, 0.92); border: 1px solid #273344; border-radius: 8px; padding: 8px 12px; display: flex; gap: 8px; }
        button { background: #1e293b; color: #e2e8f0; border: 1px solid #334155; border-radius: 6px; padding: 6px 12px; font-size: 12px; cursor: pointer; transition: all 0.15s ease; }
        button:hover { background: #334155; color: #38bdf8; }
        button.active { background: #0284c7; border-color: #38bdf8; color: #fff; }
        #bot-inspector { position: absolute; bottom: 16px; left: 16px; background: rgba(16, 21, 29, 0.94); border: 1px solid #273344; border-radius: 8px; padding: 12px 16px; font-size: 12px; min-width: 265px; display: none; }
        #help { position: absolute; bottom: 16px; right: 16px; background: rgba(16, 21, 29, 0.78); border: 1px solid #273344; border-radius: 6px; padding: 6px 10px; font-size: 11px; color: #64748b; }
    </style>
</head>
<body>
    <div id="ui">
        <h1><span class="live-dot"></span> OFFHANGAR SUBENGINE // 360-DEG 3D RADAR</h1>
        <div class="stat-row">
            <span>Map: <span id="map-name" class="stat-val">__MAP_NAME__</span></span>
            <span>Active Bots: <span id="bot-count" class="stat-val" style="color:#00ff88">0</span></span>
            <span>Radar FPS: <span id="fps-val" class="stat-val">60</span></span>
        </div>
        <div class="stat-row">
            <span>3D Solid OBBs: <span id="bld-count" class="stat-val">0</span></span>
            <span>Destructibles: <span id="destr-count" class="stat-val">0</span></span>
            <span>Engine: <span class="stat-val" style="color:#38bdf8">Free 3D Nav + 16-Ray 360</span></span>
        </div>
        <div class="legend">
            <span class="tag"><span class="dot" style="background:#22c55e"></span> Allies</span>
            <span class="tag"><span class="dot" style="background:#ef4444"></span> Enemies</span>
            <span class="tag"><span class="dot" style="background:#eab308"></span> Player</span>
            <span class="tag"><span class="dot" style="background:#00e5ff"></span> 3D Sight (Clear)</span>
            <span class="tag"><span class="dot" style="background:#a855f7"></span> 3D Window/Hull-Down</span>
            <span class="tag"><span class="dot" style="background:#f97316"></span> Steep Slope Ray</span>
            <span class="tag"><span class="dot" style="background:#38bdf8"></span> 3D Solid Obstacles</span>
            <span class="tag"><span class="dot" style="background:#f59e0b"></span> Destructible Props</span>
        </div>
    </div>

    <div id="toolbar">
        <button id="btn-plans" class="active" onclick="toggleOption('plans')">3D Live Paths & Plans</button>
        <button id="btn-whiskers" class="active" onclick="toggleOption('whiskers')">16-Ray 360 Hull Vision</button>
        <button id="btn-aim" class="active" onclick="toggleOption('aim')">3D Sight Rays</button>
        <button id="btn-buildings" class="active" onclick="toggleOption('buildings')">3D Solid Obstacles</button>
        <button id="btn-destr" onclick="toggleOption('destr')">Destructibles</button>
        <button id="btn-reset" onclick="resetView()">Reset View</button>
    </div>

    <div id="bot-inspector">
        <div style="font-weight:700;color:#38bdf8;margin-bottom:6px" id="insp-name">Bot Inspector</div>
        <div id="insp-details" style="color:#cbd5e1;line-height:1.55"></div>
    </div>

    <div id="help">Pan: Drag LMB | Zoom: Wheel | Follow Bot: Click Tank | Reset: Double Click</div>
    <canvas id="canvas"></canvas>

    <script>
        const canvas = document.getElementById('canvas');
        const ctx = canvas.getContext('2d', { alpha: false });
        // Pre-rendered 2048x2048 world-space canvas so panning/zooming/following a bot is a single GPU drawImage blit!
        const WORLD_TEX_SIZE = 2048;
        const worldCanvas = document.createElement('canvas');
        worldCanvas.width = WORLD_TEX_SIZE;
        worldCanvas.height = WORLD_TEX_SIZE;
        const worldCtx = worldCanvas.getContext('2d', { alpha: false });
        let worldDirty = true;

        let width = 1280, height = 720;
        let scale = 1.0;
        let panX = 640, panY = 360;
        let isDragging = false;
        let startX = 0, startY = 0;

        let loadedMapName = '';
        let bounds = [-500, -500, 500, 500];
        let solidBuildings = [];
        let destrProps = [];
        let vehicles = [];
        let vehMap = new Map();
        let interpPose = new Map();
        let selectedVehicleId = null;

        let opts = { plans: true, whiskers: true, aim: true, buildings: true, destr: false };

        function toggleOption(key) {
            opts[key] = !opts[key];
            const btn = document.getElementById('btn-' + key);
            if (btn) btn.className = opts[key] ? 'active' : '';
            if (key === 'buildings' || key === 'destr') worldDirty = true;
        }

        function resize() {
            width = canvas.width = window.innerWidth;
            height = canvas.height = window.innerHeight;
            resetView();
        }

        function resetView() {
            const worldW = Math.max(100, bounds[2] - bounds[0]);
            const worldH = Math.max(100, bounds[3] - bounds[1]);
            const cx = (bounds[0] + bounds[2]) * 0.5;
            const cz = (bounds[1] + bounds[3]) * 0.5;
            scale = Math.min(width / worldW, height / worldH) * 0.90;
            panX = width / 2 - cx * scale;
            panY = height / 2 + cz * scale;
        }

        window.addEventListener('resize', resize);
        window.addEventListener('load', resize);

        canvas.addEventListener('mousedown', e => {
            if (e.button === 0) {
                isDragging = true;
                startX = e.clientX - panX;
                startY = e.clientY - panY;
                checkVehicleClick(e.clientX, e.clientY);
            }
        });

        canvas.addEventListener('mousemove', e => {
            if (isDragging) {
                panX = e.clientX - startX;
                panY = e.clientY - startY;
            }
        });

        window.addEventListener('mouseup', () => isDragging = false);
        canvas.addEventListener('dblclick', () => { selectedVehicleId = null; resetView(); });

        canvas.addEventListener('wheel', e => {
            e.preventDefault();
            const mouseX = e.clientX;
            const mouseY = e.clientY;
            const zoom = e.deltaY < 0 ? 1.15 : 0.87;
            panX = mouseX - (mouseX - panX) * zoom;
            panY = mouseY - (mouseY - panY) * zoom;
            scale *= zoom;
        });

        function w2s(wx, wz) { return [panX + wx * scale, panY - wz * scale]; }
        function s2w(sx, sy) { return [(sx - panX) / scale, -(sy - panY) / scale]; }

        function checkVehicleClick(sx, sy) {
            const [wx, wz] = s2w(sx, sy);
            let clicked = null;
            let minDist = 14.0 / scale;
            for (let i = 0; i < vehicles.length; i++) {
                const v = vehicles[i];
                const d = Math.hypot(v.x - wx, v.z - wz);
                if (d < minDist) { minDist = d; clicked = v; }
            }
            selectedVehicleId = clicked ? clicked.id : null;
            updateInspector(clicked);
        }

        function updateInspector(v) {
            const panel = document.getElementById('bot-inspector');
            if (!v) { panel.style.display = 'none'; return; }
            panel.style.display = 'block';
            document.getElementById('insp-name').innerText = (v.name || 'Bot') + ' [ID: ' + v.id + ']';
            const pitchDeg = ((v.gun_pitch || 0) * 180 / Math.PI).toFixed(1);
            const slopeDeg = (Math.atan(v.slope_ahead || 0) * 180 / Math.PI).toFixed(1);
            const sightStatus = !v.target_id ? 'Scanning' : (v.los_clear ? (v.los_window ? '<span style="color:#a855f7">3D Window / Hull-Down</span>' : '<span style="color:#00e5ff">3D Direct LOS</span>') : '<span style="color:#f97316">3D Wall Occluded</span>');
            document.getElementById('insp-details').innerHTML =
                'Team: ' + (v.team === 1 ? '<span style="color:#22c55e">Team 1</span>' : '<span style="color:#ef4444">Team 2</span>') + ' | Class: <b>' + (v.class || 'Tank') + '</b><br>' +
                'Mode: <span style="color:#38bdf8;font-weight:700">' + (v.mode || 'route') + '</span>' + (v.route_id ? ' | Sector: <span style="color:#fbbf24">' + v.route_id + '</span>' : '') + '<br>' +
                (v.tactic_reason ? 'Why: <span style="color:#e879f9">' + v.tactic_reason + '</span><br>' : '') +
                (v.action_desc ? 'Plan: <span style="color:#4ade80">' + v.action_desc + '</span><br>' : '') +
                'HP: ' + (v.hp || 0) + ' / ' + (v.max_hp || 0) + ' (' + Math.round((v.hp / (v.max_hp || 1)) * 100) + '%)<br>' +
                '3D Pos: (' + v.x.toFixed(1) + ', <b>' + (v.y || 0).toFixed(1) + 'm</b>, ' + v.z.toFixed(1) + ')<br>' +
                '3D Terrain Ahead: <b>' + (v.slope_status || 'flat') + '</b> (' + slopeDeg + ' deg, dY=' + (v.elev_ahead_m || 0).toFixed(1) + 'm)<br>' +
                'Speed: ' + (v.speed || 0).toFixed(1) + ' m/s | Gun Pitch: ' + pitchDeg + ' deg<br>' +
                '3D Sight: ' + sightStatus + '<br>' +
                'Front/Rear Clear: ' + (v.c_dist || 0).toFixed(1) + 'm / ' + (v.rear_clear_dist || 0).toFixed(1) + 'm | Bias: <span style="color:#38bdf8">' + (v.steer_bias || 0) + '</span>';
        }

        function bakeWorldCanvas() {
            worldCtx.fillStyle = '#0c0f14';
            worldCtx.fillRect(0, 0, WORLD_TEX_SIZE, WORLD_TEX_SIZE);

            const minX = bounds[0], minZ = bounds[1], maxX = bounds[2], maxZ = bounds[3];
            const worldW = Math.max(100, maxX - minX);
            const worldH = Math.max(100, maxZ - minZ);
            const tScaleX = WORLD_TEX_SIZE / worldW;
            const tScaleZ = WORLD_TEX_SIZE / worldH;
            const w2t = (wx, wz) => [(wx - minX) * tScaleX, (maxZ - wz) * tScaleZ];

            const stepX = worldW / 10.0;
            const stepZ = worldH / 10.0;
            const letters = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'J', 'K'];

            worldCtx.strokeStyle = 'rgba(255, 255, 255, 0.07)';
            worldCtx.lineWidth = 1.5;
            worldCtx.font = '22px monospace';
            worldCtx.fillStyle = 'rgba(255, 255, 255, 0.26)';

            for (let i = 0; i <= 10; i++) {
                const [x1, y1] = w2t(minX + i * stepX, minZ);
                const [x2, y2] = w2t(minX + i * stepX, maxZ);
                worldCtx.beginPath(); worldCtx.moveTo(x1, y1); worldCtx.lineTo(x2, y2); worldCtx.stroke();
                if (i < 10) worldCtx.fillText((i + 1).toString(), (x1 + x2) / 2 - 8, WORLD_TEX_SIZE - 12);

                const [gx1, gy1] = w2t(minX, minZ + i * stepZ);
                const [gx2, gy2] = w2t(maxX, minZ + i * stepZ);
                worldCtx.beginPath(); worldCtx.moveTo(gx1, gy1); worldCtx.lineTo(gx2, gy2); worldCtx.stroke();
                if (i < 10) worldCtx.fillText(letters[9 - i], 12, (gy1 + gy2) / 2 + 8);
            }

            if (opts.destr) {
                worldCtx.fillStyle = 'rgba(217, 119, 6, 0.22)';
                worldCtx.strokeStyle = 'rgba(245, 158, 11, 0.58)';
                worldCtx.lineWidth = 1.2;
                for (let i = 0; i < destrProps.length; i++) {
                    const obb = destrProps[i];
                    const [tx, ty] = w2t(obb[0], obb[1]);
                    const w = obb[2] * 2 * tScaleX;
                    const l = obb[3] * 2 * tScaleZ;
                    worldCtx.save();
                    worldCtx.translate(tx, ty);
                    worldCtx.rotate(obb[4]);
                    worldCtx.fillRect(-w / 2, -l / 2, w, l);
                    worldCtx.strokeRect(-w / 2, -l / 2, w, l);
                    worldCtx.restore();
                }
            }

            if (opts.buildings) {
                for (let i = 0; i < solidBuildings.length; i++) {
                    const obb = solidBuildings[i];
                    const [tx, ty] = w2t(obb[0], obb[1]);
                    const w = obb[2] * 2 * tScaleX;
                    const l = obb[3] * 2 * tScaleZ;
                    const hMeters = (obb.length >= 7) ? Math.max(2.0, Math.min(30.0, obb[6] - obb[5])) : 8.0;
                    const roofOff = Math.min(14.0, hMeters * 0.22 * tScaleX);

                    worldCtx.save();
                    worldCtx.translate(tx, ty);
                    worldCtx.rotate(obb[4]);
                    worldCtx.fillStyle = 'rgba(15, 23, 42, 0.85)';
                    worldCtx.fillRect(-w / 2 + roofOff * 0.5, -l / 2 + roofOff * 0.5, w, l);
                    worldCtx.fillStyle = 'rgba(38, 56, 84, 0.92)';
                    worldCtx.strokeStyle = '#38bdf8';
                    worldCtx.lineWidth = 1.6;
                    worldCtx.fillRect(-w / 2 - roofOff * 0.3, -l / 2 - roofOff * 0.3, w, l);
                    worldCtx.strokeRect(-w / 2 - roofOff * 0.3, -l / 2 - roofOff * 0.3, w, l);
                    worldCtx.restore();
                }
            }
            worldDirty = false;
        }

        function lerpAngle(a, b, t) {
            let d = (b - a) % (Math.PI * 2);
            if (d > Math.PI) d -= Math.PI * 2;
            if (d < -Math.PI) d += Math.PI * 2;
            return a + d * t;
        }

        let lastFrameTime = performance.now();
        let frameCount = 0;
        let fpsTimer = 0;

        function render() {
            const now = performance.now();
            const dt = Math.min(0.1, (now - lastFrameTime) / 1000);
            lastFrameTime = now;
            frameCount++;
            fpsTimer += dt;
            if (fpsTimer >= 0.5) {
                document.getElementById('fps-val').innerText = Math.round(frameCount / fpsTimer);
                frameCount = 0;
                fpsTimer = 0;
            }

            if (worldDirty) bakeWorldCanvas();

            // Smooth 60 FPS pose interpolation
            const blend = Math.min(1.0, dt * 11.0);
            for (let i = 0; i < vehicles.length; i++) {
                const v = vehicles[i];
                let p = interpPose.get(v.id);
                if (!p || Math.hypot(v.x - p.x, v.z - p.z) > 35.0) {
                    p = { x: v.x, z: v.z, yaw: v.yaw || 0, tyaw: v.turret_yaw || 0 };
                    interpPose.set(v.id, p);
                } else {
                    p.x += (v.x - p.x) * blend;
                    p.z += (v.z - p.z) * blend;
                    p.yaw = lerpAngle(p.yaw, v.yaw || 0, blend);
                    p.tyaw = lerpAngle(p.tyaw, v.turret_yaw || 0, blend);
                }
            }

            if (selectedVehicleId !== null) {
                const sel = vehMap.get(selectedVehicleId);
                const sp = interpPose.get(selectedVehicleId);
                if (sel && sp) {
                    const targetPanX = width / 2 - sp.x * scale;
                    const targetPanY = height / 2 + sp.z * scale;
                    panX += (targetPanX - panX) * 0.16;
                    panY += (targetPanY - panY) * 0.16;
                }
            }

            // Clear & blit pre-rendered world texture in a single GPU drawImage operation!
            ctx.fillStyle = '#0c0f14';
            ctx.fillRect(0, 0, width, height);
            const [tlX, tlY] = w2s(bounds[0], bounds[3]);
            const [brX, brY] = w2s(bounds[2], bounds[1]);
            ctx.drawImage(worldCanvas, tlX, tlY, brX - tlX, brY - tlY);

            // Batch-render 16-ray 360 whiskers across all alive vehicles in 3 single path strokes!
            if (opts.whiskers) {
                ctx.lineWidth = 0.95;
                ctx.strokeStyle = 'rgba(56, 189, 248, 0.26)';
                ctx.beginPath();
                for (let i = 0; i < vehicles.length; i++) {
                    const v = vehicles[i];
                    if (v.is_alive === false || !v.rays_16) continue;
                    const p = interpPose.get(v.id) || v;
                    const [sx, sy] = w2s(p.x, p.z);
                    if (sx < -80 || sx > width + 80 || sy < -80 || sy > height + 80) continue;
                    const yw = p.yaw || 0;
                    for (let ri = 0; ri < 16; ri++) {
                        const rItem = v.rays_16[ri];
                        if (rItem[2] !== 0) continue;
                        const rayYaw = yw + ri * (Math.PI / 8.0);
                        const rLen = rItem[0] * scale;
                        ctx.moveTo(sx, sy);
                        ctx.lineTo(sx + Math.sin(rayYaw) * rLen, sy - Math.cos(rayYaw) * rLen);
                    }
                }
                ctx.stroke();

                ctx.lineWidth = 1.3;
                ctx.strokeStyle = 'rgba(249, 115, 22, 0.72)';
                ctx.beginPath();
                for (let i = 0; i < vehicles.length; i++) {
                    const v = vehicles[i];
                    if (v.is_alive === false || !v.rays_16) continue;
                    const p = interpPose.get(v.id) || v;
                    const [sx, sy] = w2s(p.x, p.z);
                    if (sx < -80 || sx > width + 80 || sy < -80 || sy > height + 80) continue;
                    const yw = p.yaw || 0;
                    for (let ri = 0; ri < 16; ri++) {
                        const rItem = v.rays_16[ri];
                        if (rItem[2] !== 3) continue;
                        const rayYaw = yw + ri * (Math.PI / 8.0);
                        const rLen = rItem[0] * scale;
                        ctx.moveTo(sx, sy);
                        ctx.lineTo(sx + Math.sin(rayYaw) * rLen, sy - Math.cos(rayYaw) * rLen);
                    }
                }
                ctx.stroke();

                ctx.lineWidth = 1.55;
                ctx.strokeStyle = 'rgba(239, 68, 68, 0.88)';
                ctx.beginPath();
                for (let i = 0; i < vehicles.length; i++) {
                    const v = vehicles[i];
                    if (v.is_alive === false || !v.rays_16) continue;
                    const p = interpPose.get(v.id) || v;
                    const [sx, sy] = w2s(p.x, p.z);
                    if (sx < -80 || sx > width + 80 || sy < -80 || sy > height + 80) continue;
                    const yw = p.yaw || 0;
                    for (let ri = 0; ri < 16; ri++) {
                        const rItem = v.rays_16[ri];
                        if (rItem[2] !== 1 && rItem[2] !== 2) continue;
                        const rayYaw = yw + ri * (Math.PI / 8.0);
                        const rLen = rItem[0] * scale;
                        ctx.moveTo(sx, sy);
                        ctx.lineTo(sx + Math.sin(rayYaw) * rLen, sy - Math.cos(rayYaw) * rLen);
                    }
                }
                ctx.stroke();
            }

            // Draw tanks, live 3D paths, and sight rays
            for (let i = 0; i < vehicles.length; i++) {
                const v = vehicles[i];
                if (v.is_alive !== false) drawTankBox(v);
            }
            requestAnimationFrame(render);
        }

        function drawTankBox(v) {
            const p = interpPose.get(v.id) || v;
            const [sx, sy] = w2s(p.x, p.z);
            if (sx < -120 || sx > width + 120 || sy < -120 || sy > height + 120) return;
            const yaw = p.yaw || 0;
            const aimYaw = yaw + (p.tyaw || 0);
            const isPlayer = v.is_player;
            const isAlly = v.team === 1;
            const color = isPlayer ? '#eab308' : (isAlly ? '#22c55e' : '#ef4444');
            const isSelected = (v.id == selectedVehicleId);

            const hullL = 6.4 * scale;
            const hullW = 3.2 * scale;

            if (opts.plans && v.plan_pts && v.plan_pts.length > 0) {
                ctx.save();
                const isOrbit = (v.mode === 'circle_of_death' || v.mode === 'flank' || v.mode === 'wolfpack_encircle' || v.mode === 'punish_push');
                ctx.strokeStyle = isOrbit ? (isAlly ? '#f43f5e' : '#fb923c') : (isAlly ? 'rgba(16, 185, 129, 0.82)' : 'rgba(248, 113, 113, 0.82)');
                ctx.lineWidth = isOrbit ? 2.2 : 1.6;
                if (!isOrbit) ctx.setLineDash([5, 4]);
                ctx.beginPath();
                ctx.moveTo(sx, sy);
                for (let pi = 0; pi < v.plan_pts.length; pi++) {
                    const [px, py] = w2s(v.plan_pts[pi][0], v.plan_pts[pi][1]);
                    ctx.lineTo(px, py);
                }
                ctx.stroke();
                ctx.setLineDash([]);
                for (let pi = 0; pi < v.plan_pts.length; pi++) {
                    const [px, py] = w2s(v.plan_pts[pi][0], v.plan_pts[pi][1]);
                    ctx.fillStyle = isOrbit ? '#f43f5e' : '#38bdf8';
                    ctx.beginPath();
                    ctx.arc(px, py, isOrbit ? 3.2 : 2.3, 0, Math.PI * 2);
                    ctx.fill();
                }
                ctx.restore();
            }

            if (opts.aim && v.target_id) {
                const tgt = vehMap.get(v.target_id);
                if (tgt && tgt.is_alive !== false) {
                    const tp = interpPose.get(tgt.id) || tgt;
                    const [tx, ty] = w2s(tp.x, tp.z);
                    const losClear = v.los_clear !== false;
                    ctx.save();
                    if (v.los_hit_pt && !losClear) {
                        const [hx, hy] = w2s(v.los_hit_pt[0], v.los_hit_pt[1]);
                        ctx.lineWidth = 1.8;
                        ctx.strokeStyle = '#00e5ff';
                        ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(hx, hy); ctx.stroke();

                        ctx.lineWidth = 1.2;
                        ctx.strokeStyle = '#f97316';
                        ctx.setLineDash([4, 4]);
                        ctx.beginPath(); ctx.moveTo(hx, hy); ctx.lineTo(tx, ty); ctx.stroke();

                        ctx.strokeStyle = '#ef4444';
                        ctx.lineWidth = 2.0;
                        ctx.setLineDash([]);
                        ctx.beginPath();
                        ctx.moveTo(hx - 4, hy - 4); ctx.lineTo(hx + 4, hy + 4);
                        ctx.moveTo(hx + 4, hy - 4); ctx.lineTo(hx - 4, hy + 4);
                        ctx.stroke();
                    } else {
                        ctx.lineWidth = 1.8;
                        ctx.strokeStyle = v.los_window ? '#a855f7' : '#00e5ff';
                        ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(tx, ty); ctx.stroke();
                    }
                    ctx.restore();
                }
            }

            ctx.save();
            ctx.translate(sx, sy);
            ctx.rotate(yaw);
            if (isSelected) {
                ctx.strokeStyle = '#38bdf8';
                ctx.lineWidth = 2.5;
                ctx.strokeRect(-hullW / 2 - 4, -hullL / 2 - 4, hullW + 8, hullL + 8);
            }
            ctx.fillStyle = isPlayer ? '#78350f' : (isAlly ? '#064e3b' : '#7f1d1d');
            ctx.strokeStyle = color;
            ctx.lineWidth = 2.0;
            ctx.fillRect(-hullW / 2, -hullL / 2, hullW, hullL);
            ctx.strokeRect(-hullW / 2, -hullL / 2, hullW, hullL);
            ctx.restore();

            ctx.save();
            const gx = sx + Math.sin(aimYaw) * (hullL * 0.95);
            const gy = sy - Math.cos(aimYaw) * (hullL * 0.95);
            ctx.strokeStyle = '#e2e8f0';
            ctx.lineWidth = 2.6;
            ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(gx, gy); ctx.stroke();
            ctx.fillStyle = color;
            ctx.beginPath(); ctx.arc(sx, sy, hullW * 0.36, 0, Math.PI * 2); ctx.fill();
            ctx.restore();

            if (scale > 0.35) {
                const hpPct = Math.max(0, Math.min(1, (v.hp || 100) / (v.max_hp || 100)));
                ctx.fillStyle = 'rgba(15, 23, 42, 0.85)';
                ctx.fillRect(sx - 18, sy - hullL / 2 - 12, 36, 4);
                ctx.fillStyle = color;
                ctx.fillRect(sx - 18, sy - hullL / 2 - 12, 36 * hpPct, 4);

                ctx.font = '10px -apple-system, sans-serif';
                ctx.fillStyle = '#f1f5f9';
                ctx.textAlign = 'center';
                ctx.fillText((v.name || 'Bot') + ' [' + (v.mode || 'route') + ']', sx, sy - hullL / 2 - 15);
            }
        }

        async function loadMapGeom(mapName) {
            try {
                const res = await fetch('/map_geom');
                const data = await res.json();
                loadedMapName = data.map || mapName;
                bounds = data.bounds || [-500, -500, 500, 500];
                const allObbs = data.buildings || [];
                solidBuildings = allObbs.filter(o => o.length < 8 || o[7] === 0);
                destrProps = allObbs.filter(o => o.length >= 8 && o[7] === 1);
                document.getElementById('bld-count').innerText = solidBuildings.length;
                document.getElementById('destr-count').innerText = destrProps.length;
                worldDirty = true;
                resetView();
            } catch (e) {}
        }

        async function fetchState() {
            try {
                const res = await fetch('/state');
                const data = await res.json();
                const mName = data.map || 'Standby';
                document.getElementById('map-name').innerText = mName;
                if (mName !== 'Standby' && mName !== loadedMapName) {
                    await loadMapGeom(mName);
                }
                vehicles = data.vehicles || [];
                vehMap.clear();
                for (let i = 0; i < vehicles.length; i++) {
                    vehMap.set(vehicles[i].id, vehicles[i]);
                }
                document.getElementById('bot-count').innerText = vehicles.length;
                if (selectedVehicleId !== null) {
                    updateInspector(vehMap.get(selectedVehicleId));
                }
            } catch (e) {}
            setTimeout(fetchState, 180);
        }

        fetchState();
        requestAnimationFrame(render);
    </script>
</body>
</html>
"""
