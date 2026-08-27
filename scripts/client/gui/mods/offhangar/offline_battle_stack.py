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


def _resolve_selected_compact_descr(player):
	try:
		from CurrentVehicle import g_currentVehicle
		item = getattr(g_currentVehicle, 'item', None)
		if item is not None:
			return getattr(item, 'typeCompDescr', 0) or 0
	except Exception:
		pass
	# Fallback: resolve from offline inventory compDescr map by current/selected invID.
	try:
		inv = getattr(player, 'inventory', None)
		if inv is not None and hasattr(inv, '_Inventory__cache'):
			cache = getattr(inv, '_Inventory__cache', None) or {}
			vehData = cache.get('inventory', {}).get(1, {})  # ITEM_TYPE_INDICES['vehicle'] == 1 in 0.8.x
			compDescrMap = vehData.get('compDescr', {})
			# invIDs in our offline inventory start from 1 and map to compactDescr ints.
			vid = getattr(player, 'playerVehicleID', 0) or 0
			if vid and vid in compDescrMap:
				return compDescrMap.get(vid, 0) or 0
	except Exception:
		pass
	try:
		td = getattr(player, 'vehicleTypeDescriptor', None)
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
		# Fallback = the value config_defaults.json ships. These only apply when
		# the config cannot be read at all; when they disagreed with the shipped
		# default they quietly formed a second, invisible configuration.
		_map_variety = int(_CFG_MV.get('map_variety', 0))
	except Exception:
		_map_variety = 0
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
	
	try:
		import ArenaType
		if not getattr(ArenaType, 'g_cache', None):
			ArenaType.init()
		
		# Override if user selected a map via Demonstrator UI
		sel_map_id = getattr(player, '_offhangar_selected_mapId', None)
		# config 'force_map' does the same thing for testing, and hooks HERE
		# rather than on the Fight button because the regular-battle path
		# deliberately deletes _offhangar_selected_mapId - so setting that
		# attribute from config would be wiped before it was ever read.
		# Accepts '02_malinovka' or just 'malinovka': the string branch below
		# already matches either way round.
		if sel_map_id is None:
			try:
				from _constants import CONFIG_OPTIONS as _CFG_FM
				_fm = str(_CFG_FM.get('force_map', '') or '').strip()
				if _fm:
					sel_map_id = _fm
					try:
						from gui.mods.offhangar.logging import LOG_DEBUG as _LDFM
						_LDFM('FORCE MAP from config: %s' % _fm)
					except Exception:
						pass
			except Exception:
				pass
		if sel_map_id is not None:
			if isinstance(sel_map_id, str):
				for k, v in ArenaType.g_cache.iteritems():
					gn = getattr(v, 'geometryName', '').lower()
					# Sometimes it's '02_malinovka' and UI passes 'malinovka'
					if sel_map_id.lower() in gn or gn in sel_map_id.lower():
						map_id = k
						map_name = getattr(v, 'geometryName', '')
						break
			elif sel_map_id in ArenaType.g_cache:
				map_id = sel_map_id
				map_name = ArenaType.g_cache[map_id].geometryName
		
		# If we still haven't resolved it, resolve the randomly chosen map_name
		if map_id not in ArenaType.g_cache:
			for k, v in ArenaType.g_cache.iteritems():
				if getattr(v, 'geometryName', '') == map_name:
					map_id = k
					break
	except: pass

	if 'tutorial' in cmdName.lower():
		map_name = '01_karelia' # Fallback to Karelia for Bootcamp since 0.8.2 tutorial map is missing
		map_id = 1
	allies, enemies = [], []

	player_dbid = getattr(player, 'databaseID', 10000001) or 10000001
	from gui.mods.offhangar._constants import CONFIG_OPTIONS
	_cfg_name = CONFIG_OPTIONS.get('nickname', '')
	player_name = str(_cfg_name) if _cfg_name else getattr(player, 'name', 'offline_player') or 'offline_player'
	# Prefer compDescr by selected inv id to avoid circular playerVehicleID fallback.
	selected_compact_descr = 0
	try:
		from CurrentVehicle import g_currentVehicle
		item = getattr(g_currentVehicle, 'item', None)
		if item is not None:
			if hasattr(item, 'descriptor') and hasattr(item.descriptor, 'makeCompactDescr'):
				try: selected_compact_descr = item.descriptor.makeCompactDescr()
				except: pass
			if not selected_compact_descr:
				selected_compact_descr = getattr(item, 'intCD', 0) or getattr(item, 'typeCompDescr', 0) or 0
			if not selected_compact_descr and hasattr(item, 'descriptor'):
				selected_compact_descr = getattr(item.descriptor, 'typeCompDescr', 0) or 0
		from gui.mods.offhangar.logging import LOG_DEBUG
		LOG_DEBUG('OfflineBattleStack.g_currentVehicle:', item, type(selected_compact_descr))
	except Exception as e:
		from gui.mods.offhangar.logging import LOG_DEBUG
		LOG_DEBUG('OfflineBattleStack.g_currentVehicle ERROR:', str(e))
		selected_compact_descr = 0
	if not selected_compact_descr:
		try:
			inv = getattr(player, 'inventory', None)
			cache = getattr(inv, '_Inventory__cache', None) or {}
			vehData = cache.get('inventory', {}).get(1, {})
			compDescrMap = vehData.get('compDescr', {})
			if selected_veh_inv_id and selected_veh_inv_id in compDescrMap:
				selected_compact_descr = compDescrMap.get(selected_veh_inv_id, 0) or 0
			# Last-resort: pick any available vehicle compact descr.
			if not selected_compact_descr and compDescrMap:
				try:
					selected_compact_descr = compDescrMap.values()[0] or 0
				except Exception:
					selected_compact_descr = 0
			from gui.mods.offhangar.logging import LOG_DEBUG
			LOG_DEBUG('OfflineBattleStack.inventory_fallback:', selected_veh_inv_id, selected_compact_descr)
		except Exception as e:
			from gui.mods.offhangar.logging import LOG_DEBUG
			LOG_DEBUG('OfflineBattleStack.inventory_fallback ERROR:', str(e))
			selected_compact_descr = 0
	if not selected_compact_descr or selected_compact_descr == 1:
		selected_compact_descr = 3329  # MS-1 fallback

	# Use "vehicle id" space distinct from hangar invIDs: battle systems treat it as a vehicle entity id.
	player_vehicle_id = 1
	allies = [_make_player_info(player_dbid, 1, player_name, player_vehicle_id, selected_compact_descr)]
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
		'playerVehicleID': player_vehicle_id,
		'selectedVehInvID': selected_veh_inv_id,
		'selectedVehTypeCompDescr': selected_compact_descr,
		'team1': allies,
		'team2': enemies,
		'vehicles': vehicles,
	}
	LOG_DEBUG('OfflineBattleStack.ready', map_name, 'mapID', map_id, 'allies', len(allies), 'enemies', len(enemies), 'vehCD', repr(selected_compact_descr))
	return ctx

