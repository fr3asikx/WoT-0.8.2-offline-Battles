# -*- coding: utf-8 -*-

MAP_GAMEPLAY_TYPES = {
	'01_karelia': ('ctf', 'assault'),
	'02_malinovka': ('ctf', 'assault', 'domination'),
	'03_campania': ('ctf',),
	'04_himmelsdorf': ('ctf', 'domination'),
	'05_prohorovka': ('ctf',),
	'06_ensk': ('ctf', 'domination'),
	'07_lakeville': ('ctf',),
	'08_ruinberg': ('ctf', 'domination'),
	'10_hills': ('ctf',),
	'11_murovanka': ('ctf', 'domination'),
	'13_erlenberg': ('ctf', 'assault'),
	'14_siegfried_line': ('ctf', 'domination', 'assault'),
	'15_komarin': ('ctf',),
	'17_munchen': ('ctf',),
	'18_cliff': ('ctf',),
	'19_monastery': ('ctf',),
	'22_slough': ('ctf',),
	'23_westfeld': ('ctf', 'assault'),
	'28_desert': ('ctf', 'assault', 'domination'),
	'29_el_hallouf': ('ctf', 'domination'),
	'31_airfield': ('ctf',),
	'33_fjord': ('ctf',),
	'34_redshire': ('ctf',),
	'35_steppes': ('ctf', 'domination'),
	'36_fishing_bay': ('ctf',),
	'37_caucasus': ('ctf',),
	'38_mannerheim_line': ('ctf',),
	'39_crimea': ('ctf',),
	'42_north_america': ('ctf',),
	'44_north_america': ('ctf',),
	'45_north_america': ('ctf',),
	'47_canada_a': ('ctf',),
	'51_asia': ('ctf',),
}

import os
import random

from gui.mods.offhangar.logging import LOG_DEBUG


def _veh_type_descriptor_from_compact_descr(compact_descr):
	"""
	BattleContext expects `vehicleType` to be a VehicleDescr object (has `.type`),
	not an int compact descr.
	"""
	try:
		from items import vehicles
		if type(compact_descr) is int:
			nationID = (compact_descr >> 4) & 15
			vehicleID = compact_descr >> 8
			vd = vehicles.VehicleDescr(typeID=(nationID, vehicleID))
		else:
			vd = vehicles.VehicleDescr(compactDescr=compact_descr)
		return vd
	except Exception:
		return None


def _discover_map_pool():
	"""
	Build map pool from the game's arena_defs naming convention.
	We cannot rely on plain-text XML here (0.8.2 ships DataSection binaries),
	so we keep a resilient, filename-based fallback list.
	"""
	# Primary: hardcoded list derived from WoT 0.8.2 `res/scripts/arena_defs/*.xml`.
	names = (
		'01_karelia',
		'02_malinovka',
		'03_campania',
		'04_himmelsdorf',
		'05_prohorovka',
		'06_ensk',
		'07_lakeville',
		'08_ruinberg',
		'10_hills',
		'11_murovanka',
		'13_erlenberg',
		'14_siegfried_line',
		'15_komarin',
		'17_munchen',
		'18_cliff',
		'19_monastery',
		'22_slough',
		'23_westfeld',
		'28_desert',
		'29_el_hallouf',
		'31_airfield',
		'33_fjord',
		'34_redshire',
		'35_steppes',
		'36_fishing_bay',
		'37_caucasus',
		'38_mannerheim_line',
		'39_crimea',
		'42_north_america',
		'44_north_america',
		'45_north_america',
		'47_canada_a',
		'51_asia',
	)

	pool = []
	seen_geom = set()
	# Dynamically inspect ArenaType.g_cache when inside any 0.8.x client (0.8.0..0.8.11)
	# so new maps (60_asia_miao, 73_asia_korea, 85_winter, 63_tundra, 84_winter, 59_asia_great_wall, 87_ruinberg_on_fire, etc.)
	# and their supported gameplay modes (ctf, domination, assault) are registered automatically!
	try:
		import ArenaType
		if not getattr(ArenaType, 'g_cache', None):
			ArenaType.init()
		cache = getattr(ArenaType, 'g_cache', None) or {}
		gp_by_map = {}
		for k, v in cache.items():
			geom = str(getattr(v, 'geometryName', '') or '').replace('\\', '/').split('/')[-1].lower()
			gp = str(getattr(v, 'gameplayName', 'ctf') or 'ctf').lower()
			if not geom or 'tutorial' in geom or 'training' in geom:
				continue
			if gp in ('ctf', 'domination', 'assault'):
				gp_by_map.setdefault(geom, set()).add(gp)
			if gp == 'ctf' and geom not in seen_geom:
				seen_geom.add(geom)
				pool.append((int(k), geom))
		for geom, gps in gp_by_map.items():
			MAP_GAMEPLAY_TYPES[geom] = tuple(sorted(gps))
	except Exception:
		pass
	if not pool:
		for nm in names:
			try:
				map_id = int(nm.split('_', 1)[0])
			except Exception:
				map_id = 0
			pool.append((map_id, nm))
	return tuple(pool)


_MAP_POOL = _discover_map_pool()


def _make_player_info(acc_id, team, nick, veh_id, veh_type_compact_descr):
	return {
		'accountDBID': acc_id,
		'name': nick,
		'team': team,
		'vehicleInvID': veh_id,
		'vehTypeCompDescr': veh_type_compact_descr,
		# Battle GUI may use this key; keep compact descr here (icons/etc).
		'vehicleType': veh_type_compact_descr,
		'isAlive': True,
	}


def _extract_cv_compact_descr_and_inv_id():
	"""Extract (compact_descr, inv_id) from WoT 0.8.2 CurrentVehicle.g_currentVehicle."""
	try:
		from CurrentVehicle import g_currentVehicle
		if g_currentVehicle is None:
			return 0, 0
		# In WoT 0.8.2, CurrentVehicle exposes the selected Vehicle via .vehicle (or _CurrentVehicle__vehicle),
		# NOT .item (which was introduced in 0.8.5+). Check both for compatibility!
		for attr in ('vehicle', '_CurrentVehicle__vehicle', 'item'):
			vobj = getattr(g_currentVehicle, attr, None)
			if vobj is None:
				continue
			inv_id = int(getattr(vobj, 'inventoryId', 0) or getattr(vobj, 'invID', 0) or getattr(vobj, 'id', 0) or 0)
			descr = getattr(vobj, 'descriptor', None)
			cd = 0
			if descr is not None:
				if hasattr(descr, 'makeCompactDescr'):
					try:
						raw_cd = descr.makeCompactDescr()
						if isinstance(raw_cd, str) and len(raw_cd) >= 10:
							cd = raw_cd
						elif raw_cd:
							cd = int(raw_cd)
					except Exception:
						cd = 0
				if not cd:
					cd = int(getattr(getattr(descr, 'type', None), 'compactDescr', 0) or getattr(descr, 'typeCompDescr', 0) or 0)
			if not cd:
				raw_vcd = getattr(vobj, 'strCompactDescr', None) or getattr(vobj, 'compactDescr', 0) or getattr(vobj, 'intCD', 0) or getattr(vobj, 'typeCompDescr', 0)
				if isinstance(raw_vcd, str) and len(raw_vcd) >= 10:
					cd = raw_vcd
				else:
					try:
						cd = int(raw_vcd or 0)
					except Exception:
						cd = 0
			if cd or inv_id:
				return cd, inv_id
	except Exception:
		pass
	return 0, 0


def _lookup_inv_comp_descr_map(player):
	"""Return the {vehInvID: compactDescr} dict from WoT 0.8.2 Inventory cache or player._offhangar_inventory."""
	try:
		inv = getattr(player, 'inventory', None)
		cache = getattr(inv, '_Inventory__cache', None) if inv is not None else None
		if isinstance(cache, dict):
			# In WoT 0.8.2 Inventory.__cache, top-level key 1 (ITEM_TYPE_INDICES['vehicle']) holds {'compDescr': {...}}
			vd = cache.get(1) or (cache.get('inventory') or {}).get(1)
			if isinstance(vd, dict) and isinstance(vd.get('compDescr'), dict) and vd['compDescr']:
				return vd['compDescr']
	except Exception:
		pass
	try:
		p_inv = getattr(player, '_offhangar_inventory', None)
		if isinstance(p_inv, dict):
			vd = p_inv.get(1) or (p_inv.get('inventory') or {}).get(1)
			if isinstance(vd, dict) and isinstance(vd.get('compDescr'), dict) and vd['compDescr']:
				return vd['compDescr']
	except Exception:
		pass
	return {}


def _resolve_selected_compact_descr(player):
	cd, cv_inv_id = _extract_cv_compact_descr_and_inv_id()
	if cd:
		return cd
	try:
		compDescrMap = _lookup_inv_comp_descr_map(player)
		if compDescrMap:
			from gui.mods.offhangar.state import load_selected_vehicle
			for cand_id in (cv_inv_id, load_selected_vehicle(), getattr(player, 'playerVehicleID', 0)):
				if cand_id and int(cand_id) in compDescrMap:
					return compDescrMap[int(cand_id)]
	except Exception:
		pass
	try:
		td = getattr(player, 'vehicleTypeDescriptor', None)
		if td is not None and hasattr(td, 'makeCompactDescr'):
			return td.makeCompactDescr()
		return getattr(td, 'typeCompDescr', 0) or 0
	except Exception:
		return 0


def build_offline_battle_context(player, selected_veh_inv_id, cmdName=''):
	"""
	Build minimal pseudo-battle stack for the client:
	- map: random from 0.8.2 arena_defs pool
	- team1: player + 14 bots
	- team2: 15 bots

	Important: keys/fields here are consumed by the arena/avatar stubs injected by the mod.
	"""
	# EXPERIMENTAL feature (config 'map_variety'; may be unstable / change):
	# Limit map VARIETY: each distinct map caches ~150-300 MB of terrain/object
	# textures the 32-bit process never frees, so unlimited random maps climb
	# the hangar baseline until a map load OOM-crashes it. Pick a small STABLE
	# subset ONCE and reuse it so those textures load only once
	# (config 'map_variety'; 0 = unlimited/old behaviour). Smaller = safer.
	_pool = _MAP_POOL
	try:
		from _constants import CONFIG_OPTIONS as _CFG_MV
		_map_variety = int(_CFG_MV.get('map_variety', 3))
	except Exception:
		_map_variety = 3
	if _map_variety > 0 and len(_MAP_POOL) > _map_variety:
		_sub = globals().get('_g_offh_map_subset')
		if not _sub:
			try:
				_sub = random.sample(_MAP_POOL, _map_variety)
			except Exception:
				_sub = _MAP_POOL[:_map_variety]
			globals()['_g_offh_map_subset'] = _sub
		_pool = _sub
	# Avoid the SAME map twice in a row when the pool has room (nobody wants
	# back-to-back identical maps). Retries a few times against the last pick.
	_last = globals().get('_g_offh_last_map')
	_pick = random.choice(_pool)
	if len(_pool) > 1:
		_mtries = 0
		while _pick[1] == _last and _mtries < 8:
			_pick = random.choice(_pool)
			_mtries += 1
	globals()['_g_offh_last_map'] = _pick[1]
	map_id, map_name = _pick

	# Roll gameplay type (ctf, domination, assault) based on map capabilities & config
	try:
		from _constants import CONFIG_OPTIONS as _CFG_GP
		_en_assault = bool(_CFG_GP.get('enable_assault', True))
		_en_domination = bool(_CFG_GP.get('enable_domination', True))
		_forced_gp = str(_CFG_GP.get('force_gameplay', '') or '').strip().lower()
	except Exception:
		_en_assault, _en_domination, _forced_gp = True, True, ''

	_bare_map = map_name.split('/')[-1].lower()
	_supported_gps = list(MAP_GAMEPLAY_TYPES.get(_bare_map, ('ctf',)))
	if _forced_gp and _forced_gp in _supported_gps:
		chosen_gameplay = _forced_gp
	else:
		_allowed_gps = ['ctf']
		if _en_assault and 'assault' in _supported_gps:
			_allowed_gps.append('assault')
		if _en_domination and 'domination' in _supported_gps:
			_allowed_gps.append('domination')
		if len(_allowed_gps) == 1:
			chosen_gameplay = 'ctf'
		elif 'assault' in _allowed_gps and 'domination' in _allowed_gps:
			_r_val = random.random()
			chosen_gameplay = 'ctf' if _r_val < 0.60 else ('domination' if _r_val < 0.80 else 'assault')
		elif 'domination' in _allowed_gps:
			chosen_gameplay = 'domination' if random.random() < 0.30 else 'ctf'
		elif 'assault' in _allowed_gps:
			chosen_gameplay = 'assault' if random.random() < 0.30 else 'ctf'
		else:
			chosen_gameplay = 'ctf'
	# Network clients must load the same map before they can exchange canonical
	# coordinates. The authority later supplies the shared map-frame basis used
	# by server-side static navigation; BigWorld terrain queries remain local.
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS as _NET_CFG
		import os as _os_net
		_is_net = bool(_NET_CFG.get('network_mode', False)) or (_os_net.environ.get('OFFHANGAR_NETWORK_MODE') == '1') or (_os_net.environ.get('OFFHANGAR_WORKER_MODE') == 'authority_worker')
		if _is_net:
			_net_name = getattr(player, '_offhangar_network_map_name', None)
			if not _net_name:
				_net_name = _NET_CFG.get('network_map_name', map_name)
			_net_name = str(_net_name or map_name)
			if _net_name not in ('random', 'server_random'):
				map_name = _net_name
	except Exception:
		pass

	try:
		import ArenaType
		if not getattr(ArenaType, 'g_cache', None):
			ArenaType.init()
		
		# Override if user selected a map via Demonstrator UI or Map Picker Window
		sel_map_id = getattr(player, '_offhangar_selected_mapId', None)
		if sel_map_id is not None and not getattr(player, '_offhangar_network_map_name', None):
			if isinstance(sel_map_id, (int, long, float)) and int(sel_map_id) in ArenaType.g_cache:
				map_id = int(sel_map_id)
				map_name = ArenaType.g_cache[map_id].geometryName
				chosen_gameplay = str(getattr(ArenaType.g_cache[map_id], 'gameplayName', chosen_gameplay) or chosen_gameplay)
			elif isinstance(sel_map_id, str):
				if sel_map_id.isdigit() and int(sel_map_id) in ArenaType.g_cache:
					map_id = int(sel_map_id)
					map_name = ArenaType.g_cache[map_id].geometryName
					chosen_gameplay = str(getattr(ArenaType.g_cache[map_id], 'gameplayName', chosen_gameplay) or chosen_gameplay)
				else:
					for k, v in ArenaType.g_cache.iteritems():
						gn = getattr(v, 'geometryName', '').lower()
						if sel_map_id.lower() in gn or gn in sel_map_id.lower():
							map_id = k
							map_name = getattr(v, 'geometryName', '')
							break
		else:
			target_bare = map_name.split('/')[-1].lower()
			for _want_gp in (chosen_gameplay, 'ctf', None):
				_matched_gp = False
				for k, v in ArenaType.g_cache.iteritems():
					gn_bare = getattr(v, 'geometryName', '').split('/')[-1].lower()
					if gn_bare == target_bare and (_want_gp is None or str(getattr(v, 'gameplayName', 'ctf') or 'ctf') == _want_gp):
						map_id = k
						map_name = getattr(v, 'geometryName', '')
						_matched_gp = True
						break
				if _matched_gp:
					break
	except: pass

	if 'tutorial' in cmdName.lower():
		map_name = '01_karelia' # Fallback to Karelia for Bootcamp since 0.8.2 tutorial map is missing
		map_id = 1
	allies, enemies = [], []

	player_dbid = getattr(player, 'databaseID', 10000001) or 10000001
	from gui.mods.offhangar._constants import CONFIG_OPTIONS
	try:
		from gui.mods.offhangar.core.cbt_profiles_and_locale import get_active_nickname
		_act_nick = get_active_nickname()
	except Exception:
		_act_nick = None
	_cfg_name = CONFIG_OPTIONS.get('nickname', '')
	player_name = str(_act_nick) if _act_nick else (str(_cfg_name) if _cfg_name else getattr(player, 'name', 'offline_player') or 'offline_player')
	if _is_net:
		player_name = str(getattr(player, '_offhangar_network_name', None) or player_name)
	# Resolve selected vehicle compact descriptor from WoT 0.8.2 CurrentVehicle.vehicle or Inventory cache[1]
	compDescrMap = _lookup_inv_comp_descr_map(player) or {}
	if not selected_veh_inv_id:
		try:
			from gui.mods.offhangar.state import load_selected_vehicle
			selected_veh_inv_id = int(load_selected_vehicle() or 0)
		except Exception:
			pass
	if not selected_veh_inv_id:
		try:
			from gui.mods.offhangar import state as _st_obs
			selected_veh_inv_id = int(getattr(_st_obs, '_LAST_SELECTED_VEH_INV_ID', 0) or 0)
		except Exception:
			pass
	if not selected_veh_inv_id:
		try:
			from account_helpers.AccountSettings import AccountSettings
			selected_veh_inv_id = int(AccountSettings.getFavorites('current') or 0)
		except Exception:
			pass

	selected_compact_descr = 0
	if selected_veh_inv_id and int(selected_veh_inv_id) in compDescrMap:
		selected_compact_descr = compDescrMap[int(selected_veh_inv_id)]

	if not selected_compact_descr or (isinstance(selected_compact_descr, str) and len(selected_compact_descr) < 10):
		cv_cd, cv_inv_id = _extract_cv_compact_descr_and_inv_id()
		if cv_cd and (not selected_compact_descr or (isinstance(cv_cd, str) and len(cv_cd) >= 10)):
			selected_compact_descr = cv_cd
			if not selected_veh_inv_id and cv_inv_id:
				selected_veh_inv_id = cv_inv_id

	if not selected_compact_descr or (isinstance(selected_compact_descr, str) and len(selected_compact_descr) < 10):
		if compDescrMap:
			if selected_veh_inv_id and int(selected_veh_inv_id) in compDescrMap:
				selected_compact_descr = compDescrMap[int(selected_veh_inv_id)]
			if not selected_compact_descr:
				selected_compact_descr = list(compDescrMap.values())[0] or 0
	if not selected_compact_descr or selected_compact_descr == 1:
		selected_compact_descr = 3329  # MS-1 fallback

	# Use "vehicle id" space distinct from hangar invIDs: battle systems treat it as a vehicle entity id.
	player_vehicle_id = 1
	player_team = 1
	try:
		if _is_net:
			player_team = int(getattr(player, '_offhangar_network_team', 1) or 1)
		else:
			# Offline: honour the per-battle coin flip set by _step_on_enqueued
			# (random_team config). Defaults to 1 if it was never rolled.
			player_team = int(getattr(player, '_offhangar_team', 1) or 1)
	except Exception:
		player_team = 1
	if player_team not in (1, 2):
		player_team = 1
	allies = [_make_player_info(player_dbid, player_team, player_name, player_vehicle_id, selected_compact_descr)]
	enemies = []
	acc_id = 1

	from _constants import CONFIG_OPTIONS
	prefix_allies = str(CONFIG_OPTIONS.get('bot_name_prefix_allies', 'Bot_'))
	prefix_enemies = str(CONFIG_OPTIONS.get('bot_name_prefix_enemies', 'Bot_'))

	pass

	vehicles = {}
	for p in (allies + enemies):
		v_id = p['vehicleInvID']
		td = _veh_type_descriptor_from_compact_descr(p['vehTypeCompDescr'])
		vehicles[v_id] = {
			'accountDBID': p['accountDBID'],
			'team': p['team'],
			'name': p['name'],
			'vehTypeCompDescr': p['vehTypeCompDescr'],
			'vehicleType': td if td is not None else type('FakeDesc', (object,), {'type': type('FakeType', (object,), {'tags': frozenset(), 'userString': 'Bot', 'shortUserString': 'Bot', 'name': 'ussr-T-34', 'icon': '', 'iconPath': ''})(), 'name': 'bot', 'icon': '', 'iconPath': '', 'shortUserString': 'Bot'})(),
			'isAlive': True,
			'clanAbbrev': '',
			'clanDBID': 0,
			'prebattleID': 0,
			'isTeamKiller': False,
			'playerGroup': 0,
			'isAvatarReady': True,
			'events': {},
			'frags': 0,
		}

	ctx = {
		'mapID': map_id,
		'mapName': map_name,
		'gameplayName': chosen_gameplay,
		'playerVehicleID': player_vehicle_id,
		'selectedVehInvID': selected_veh_inv_id,
		'selectedVehTypeCompDescr': selected_compact_descr,
		'team1': allies,
		'team2': enemies,
		'vehicles': vehicles,
	}
	LOG_DEBUG('OfflineBattleStack.ready', map_name, 'mapID', map_id, 'allies', len(allies), 'enemies', len(enemies), 'vehCD', repr(selected_compact_descr))
	return ctx

