# -*- coding: utf-8 -*-
'''User-data locations and config loading for the offhangar mod.

User-owned progression files (offhangar_state.dat, profiles, replays) live in
<game folder>/offhangar_user/, OUTSIDE res_mods, so mod updates never touch
player progress. Engine/AI configuration is loaded strictly from shipped
defaults inside res_mods so stale external user configs can never override
physics or AI behavior.
'''

import json
import os
import shutil
import sys

USER_DIR = 'offhangar_user'
USER_CONFIG_FILE = os.path.join(USER_DIR, 'config.json')
USER_STATE_FILE = os.path.join(USER_DIR, 'offhangar_state.dat')
STATE_FILE = USER_STATE_FILE
REPLAYS_DIR = os.path.join(USER_DIR, 'replays')
USER_MANAGER_FILE = os.path.join(USER_DIR, 'manager.json')

DEFAULTS_NAME = 'config_defaults.json'

_OLD_MOD_DIR = os.path.join('res_mods', '0.8.2', 'scripts', 'client', 'gui', 'mods', 'offhangar')
_DETECTED_VER_CACHE = [None]


def detect_wot_client_version():
	'''Dynamically detect the active 0.8.x client version (0.8.0 .. 0.8.11).'''
	if _DETECTED_VER_CACHE[0] is not None:
		return _DETECTED_VER_CACHE[0]
	import re
	for xml_candidate in ('paths.xml', 'version.xml'):
		if os.path.isfile(xml_candidate):
			try:
				fh = open(xml_candidate, 'r')
				try:
					txt = fh.read(4096)
				finally:
					fh.close()
				m = re.search(r'res_mods[/\\](0\.8\.[0-9]+)', txt)
				if not m:
					m = re.search(r'(0\.8\.[0-9]+)', txt)
				if m:
					_DETECTED_VER_CACHE[0] = m.group(1)
					return _DETECTED_VER_CACHE[0]
			except Exception:
				pass
	if os.path.isdir('res_mods'):
		try:
			for entry in sorted(os.listdir('res_mods')):
				if entry.startswith('0.8.'):
					cand = os.path.join('res_mods', entry, 'scripts', 'client', 'gui', 'mods', 'offhangar')
					if os.path.isdir(cand):
						_DETECTED_VER_CACHE[0] = entry
						return entry
		except Exception:
			pass
	_DETECTED_VER_CACHE[0] = '0.8.2'
	return '0.8.2'


def mod_dir():
	ver = detect_wot_client_version()
	dyn_dir = os.path.join('res_mods', ver, 'scripts', 'client', 'gui', 'mods', 'offhangar')
	if os.path.isdir(dyn_dir):
		return dyn_dir
	if os.path.isdir(_OLD_MOD_DIR):
		return _OLD_MOD_DIR
	try:
		d = os.path.dirname(os.path.abspath(sys._getframe().f_code.co_filename))
		if os.path.basename(d) in ('core', 'ai', 'combat', 'physics', 'native', 'network', 'ui'):
			d = os.path.dirname(d)
		if os.path.isdir(d):
			return d
	except Exception:
		pass
	return _OLD_MOD_DIR


def defaults_file():
	cand = os.path.normpath(os.path.join(mod_dir(), DEFAULTS_NAME))
	if os.path.exists(cand):
		return cand
	alt = os.path.normpath(os.path.join(mod_dir(), 'config.json'))
	if os.path.exists(alt):
		return alt
	return cand


def _migrate_file(oldPath, newPath):
	if os.path.exists(newPath) or not os.path.exists(oldPath):
		return False
	try:
		parent = os.path.dirname(newPath)
		if parent and not os.path.exists(parent):
			os.makedirs(parent)
		shutil.copy2(oldPath, newPath)
		return True
	except (OSError, IOError):
		return False


def _migrate_replays(old_dir, new_dir):
	if not os.path.isdir(old_dir):
		return 0
	moved = 0
	try:
		if not os.path.exists(new_dir):
			os.makedirs(new_dir)
		for name in os.listdir(old_dir):
			if not name.lower().endswith('.wotreplay'):
				continue
			src = os.path.join(old_dir, name)
			dst = os.path.join(new_dir, name)
			if os.path.exists(dst) or not os.path.isfile(src):
				continue
			try:
				shutil.copy2(src, dst)
				moved += 1
			except Exception:
				pass
	except Exception:
		pass
	return moved


def ensure_user_dir():
	'''Creates the user-data dir, migrates player progression state/replays once,
	and purges any stale external config/navcache overrides from offhangar_user/.'''
	for d in (USER_DIR, REPLAYS_DIR):
		try:
			if not os.path.isdir(d):
				os.makedirs(d)
		except (OSError, IOError):
			pass
	_migrate_file('offhangar_state.dat', USER_STATE_FILE)
	_migrate_file(os.path.join(_OLD_MOD_DIR, 'offhangar_state.dat'), USER_STATE_FILE)
	_migrate_replays('replays_offline', REPLAYS_DIR)
	_migrate_replays(os.path.join(_OLD_MOD_DIR, 'replays'), REPLAYS_DIR)

	for stale_file in ('config.json', 'config.json.bak', 'config.json.tmp', 'manager.json'):
		sp = os.path.join(USER_DIR, stale_file)
		if os.path.exists(sp):
			try:
				os.remove(sp)
			except Exception:
				pass
	for stale_dir in ('navcache', 'nav_dump'):
		sdp = os.path.join(USER_DIR, stale_dir)
		if os.path.isdir(sdp):
			try:
				shutil.rmtree(sdp)
			except Exception:
				pass


def _read_json(path):
	f = open(path, 'r')
	try:
		data = json.load(f)
	finally:
		f.close()
	if not isinstance(data, dict):
		return {}
	return dict((k, v) for k, v in data.items() if not str(k).startswith('_'))


_cache = [None]


def _migrate_user_config(defaults, user_options, errors):
	return []


def migrate_user_config(defaults=None):
	return {}, []


def load_config():
	'''Returns (options, newKeys, errors).

	Loads strictly from shipped defaults inside res_mods so stale external
	offhangar_user/config.json files can never override or break physics, AI,
	or rendering settings.
	'''
	if _cache[0] is not None:
		return _cache[0]
	ensure_user_dir()
	errors = []
	defaults = {}
	defaultsPath = defaults_file()
	try:
		defaults = _read_json(defaultsPath)
	except (OSError, IOError, ValueError) as e:
		errors.append('cannot read %s: %s' % (defaultsPath, e))
	options = dict(defaults)
	newKeys = []

	options['wreck_detach_filter'] = False
	options['wreck_detach_after_swap'] = False
	options['wreck_entity_model'] = True
	options['reuse_map_space'] = True
	options['full_space_release'] = False
	options['wreck_marker_rebind'] = 'off'
	options['wreck_freeze_mode'] = 'idle'
	options['los_ray_budget'] = 'light'
	options['los_probe_budget'] = 'light'
	options['los_sight_trim'] = True
	options['physics_hz'] = float(options.get('physics_hz', 0.0) or 0.0)
	options['lod_level'] = int(options.get('lod_level', 1) or 1)
	options['hazard_timeout_seconds'] = 4.0
	options['hazard_retrigger_cooldown_seconds'] = 8.0
	options['diag_probe'] = False
	options['gui_step_trace'] = False
	options['wreck_step_trace'] = False
	options['shot_trace_diag'] = False
	options['bot_mode_probe'] = False
	options['results_system_message'] = False
	options['hangar_push_guard'] = True
	options['hangar_push_once'] = bool(options.get('hangar_push_once', True))
	options['shop_cb_defer'] = False
	options['hangar_refs_showlobby'] = bool(options.get('hangar_refs_showlobby', True))
	options['hangar_movie_alpha'] = False
	options['ram_mass_from_body'] = True
	options['ram_direction_fix'] = True
	options['bot_ai_v2'] = True
	options['bot_aim_hull_frame'] = True
	options['bot_class_fire_ranges'] = True
	options['gun_pitch_limits_yaw_aware'] = True
	options['hold_fire_when_gun_limited'] = True
	options['bot_hold_fire_when_pitch_limited'] = True
	options['gun_muzzle_from_descriptor'] = True
	options['gun_muzzle_node_sanity'] = True
	options['player_bot_style_suspension'] = False
	options['player_swing_scale'] = 0.22
	options['bot_swing_scale'] = 1.0
	options['bot_lod_scale'] = 1.0
	options['battle_keep_gui_sounds'] = True
	options['timer_sound_no_double'] = False
	physics_tuning = options.get('physics_tuning')
	if not isinstance(physics_tuning, dict):
		physics_tuning = {}
	physics_tuning['turn_authority_mult'] = 1.0
	physics_tuning['forward_accel_mult'] = 1.0
	physics_tuning['reverse_accel_mult'] = 1.0
	physics_tuning['All_brake_mult'] = 1.0
	options['physics_tuning'] = physics_tuning

	_cache[0] = (options, newKeys, errors)
	return _cache[0]
