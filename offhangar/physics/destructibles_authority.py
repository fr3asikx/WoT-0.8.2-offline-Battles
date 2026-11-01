# -*- coding: utf-8 -*-
"""Authoritative offline record of destroyed map objects.

Mirrors what the real BigWorld server kept in the AreaDestructibles
entity's ALL_CLIENTS properties (fallenTrees, fallenColumns,
destroyedFragiles, destroyedModules). Sensors (collision probes, the
proximity registry, later shot impacts) REPORT contacts here; this
module decides, encodes, records and pushes the update to the
per-chunk client entity like a server property push, so the game's
own set_* callbacks drive the visuals.

State lives per space: a new battle (new spaceID) resets everything,
which also makes cross-battle chunk-ID collisions harmless.
"""

import math

import BigWorld
import Math

_state = {
	'spaceID': None,
	'chunks': {},
	'entities': set(),
	'collisionHealth': {},
}

try:
	_INTEGER_TYPES = (int, long)
except NameError:
	_INTEGER_TYPES = (int,)

_PROP_BY_KIND = {
	'tree': 'fallenTrees',
	'column': 'fallenColumns',
	'fragile': 'destroyedFragiles',
	'module': 'destroyedModules',
}

_PREV_BY_PROP = {
	'fallenTrees': '_AreaDestructibles__prevFallenTrees',
	'fallenColumns': '_AreaDestructibles__prevFallenColumns',
	'destroyedFragiles': '_AreaDestructibles__prevDestroyedFragiles',
	'destroyedModules': '_AreaDestructibles__prevDestroyedModules',
}


def _log(*args):
	try:
		from gui.mods.offhangar.logging import LOG_DEBUG
		LOG_DEBUG(*args)
	except Exception:
		pass


def reset(spaceID=None):
	"""Return _state to its initial shape. Callers (the battle sweep)
	MUST use this instead of _state.clear(): clear() removes the
	'spaceID'/'chunks'/'entities' keys, and every later access then
	raises KeyError, killing destructibles for the whole battle."""
	_state['spaceID'] = spaceID
	_state['chunks'] = {}
	_state['entities'] = set()
	_state['collisionHealth'] = {}


def _ensure_shape():
	# Defensive: if anything wiped _state (e.g. a stray clear()), rebuild
	# the required keys so no access below can KeyError.
	if 'spaceID' not in _state:
		_state['spaceID'] = None
	if 'chunks' not in _state:
		_state['chunks'] = {}
	if 'entities' not in _state:
		_state['entities'] = set()
	if 'collisionHealth' not in _state:
		_state['collisionHealth'] = {}


def _reset_if_new_space(spaceID):
	_ensure_shape()
	if _state['spaceID'] != spaceID:
		reset(spaceID)


def _chunk(chunkID):
	_ensure_shape()
	c = _state['chunks'].get(chunkID)
	if c is None:
		c = {'fallenTrees': [], 'fallenColumns': [], 'destroyedFragiles': [], 'destroyedModules': [], 'keys': set(), 'faults': set()}
		_state['chunks'][chunkID] = c
	elif 'faults' not in c:
		c['faults'] = set()
	return c


def is_destroyed(chunkID, itemIndex, matKind=None):
	c = _state.get('chunks', {}).get(chunkID)
	if c is None:
		return False
	return (itemIndex, matKind) in c['keys'] or (itemIndex, None) in c['keys']


_IMPASSABLE_DESTR_KEYWORDS = (
	'content/railway', 'railway/', '/rw0', '/rw1', '/rw2', '/rw3', '/rw4', '/rw5',
	'train', 'wagon', 'vagon', 'carriage', 'cistern', 'locom', 'platform',
	'drezina', 'tram', 'semaphore', 'content/buildingsrare', '/blr0',
	'church', 'cathedral', 'castle', 'bunker', 'pillbox', 'factory', 'hangar',
	'bridge', 'monument',
)

_PASSABLE_FENCE_KEYWORDS = (
	'content/gatesandfences', 'gatesandfences/', '/gaf', 'fence', 'gate',
	'stonesimplewall', 'stonerichwall', 'stoneoldwall', 'clayfence', 'reedyfence',
	'wattledfence', 'villagefance', 'hedgerow', 'palisade', 'wicker', 'stak',
	'bld002_', 'bld003_', 'bld004_', 'bld6111_', 'bld6112_', 'bld6113_',
	'bld701_', 'bld702_', 'bld703_', 'bld704_', 'bld705_', 'bld706_', 'bld707_', 'bld708_', 'bld709_',
	'bldaf_001', 'bldaf_002', 'bldaf_003', 'bldaf_004', 'bldaf_006',
	'bldam_002', 'bldam_003', 'bldam_006', 'bldam_008', 'bldam_028', 'bldam_038',
	'bldas_002', 'bldas_003', 'bldas_004', 'bldas_005', 'bldas_007', 'bldas_008', 'bldas_010', 'bldas_011',
	'bldnw_005', 'bldnw_006', 'bldsc_002', 'bld_constructor',
	'envaf_012', 'envaf_013', 'envas_003', 'envas_004', 'envas_005', 'envas_006', 'envas_007',
	'blraf_002_granary', 'blraf_003_marquee', 'blr005_cellar', 'mle002_kpp',
	'wooddryer', 'pavilion', 'smallwoodshes', 'vshedsmall', 'smallshed',
	'housesshanty', 'metalshanty', 'haystack', 'telega', 'smallcart', 'ruscart',
	'kiosk', 'scaffold', 'awning', 'woodconstructions', 'marketshelves', 'tradeshelves',
)


def is_impassable_destructible_name(filename):
	fl = str(filename or '').strip().lower().replace('\\', '/')
	if not fl:
		return False
	return any(k in fl for k in _IMPASSABLE_DESTR_KEYWORDS)


def is_passable_fence_name(filename):
	fl = str(filename or '').strip().lower().replace('\\', '/')
	if not fl:
		return False
	return any(k in fl for k in _PASSABLE_FENCE_KEYWORDS)


def resolve_destructible_desc(spaceID, chunkID, itemIndex, itemFilename=''):
	import AreaDestructibles
	desc = None
	if spaceID is not None and chunkID is not None and itemIndex is not None:
		try:
			desc = AreaDestructibles.g_cache.getDestructibleDesc(
				int(spaceID), int(chunkID), int(itemIndex))
		except Exception:
			desc = None
	if desc is None and itemFilename:
		try:
			desc = AreaDestructibles.g_cache.getDescByFilename(itemFilename)
		except Exception:
			desc = None
	return desc


def resolve_destructible_filename(spaceID, chunkID, itemIndex, itemFilename='', desc=None):
	names = []
	if itemFilename:
		names.append(str(itemFilename))
	if isinstance(desc, dict) and desc.get('filename'):
		names.append(str(desc.get('filename')))
	if spaceID is not None and chunkID is not None and itemIndex is not None:
		try:
			fn = BigWorld.wg_getDestructibleFilename(int(spaceID), int(chunkID), int(itemIndex))
			if fn:
				names.append(str(fn))
		except Exception:
			pass
	return ' '.join(names)


def can_crush(owner, spaceID, chunkID, itemIndex, matKind, itemFilename,
		vehicleSpeed):
	"""Apply the retail mass/speed/health test to one resolved contact."""
	try:
		import AreaDestructibles
		import DestructiblesCache
		desc = resolve_destructible_desc(spaceID, chunkID, itemIndex, itemFilename)
		full_name = resolve_destructible_filename(spaceID, chunkID, itemIndex, itemFilename, desc)
		if is_impassable_destructible_name(full_name):
			return False
		if is_passable_fence_name(full_name):
			return True
		if int(matKind or 0) > 100:
			# DAMAGED_MATKIND (101..130) is an already-collapsed structure foundation / chassis
			return False
		if is_destroyed(chunkID, itemIndex, matKind):
			return True
		if desc is None:
			return False
		scale = _destructible_scale(spaceID, chunkID, itemIndex)
		if scale is None or scale <= 0.0:
			scale = 1.0
		mass = float(owner.typeDescriptor.physics['weight'])
		speed = max(float(vehicleSpeed), 10.0)
		if mass <= 0.0:
			return False
		instantDamage = 0.5 * mass * speed * speed * 0.00015
		if desc['type'] == AreaDestructibles.DESTR_TYPE_STRUCTURE:
			# In WoT 0.8.2, DESTR_TYPE_STRUCTURE objects (train wagons, rail cars, buildings, sheds)
			# leave an impassable foundation/undercarriage (DAMAGED_MATKIND 101..130) when crushed.
			# Only allow crushing if explicitly matched as a passable gate/fence.
			full_name = resolve_destructible_filename(spaceID, chunkID, itemIndex, itemFilename, desc)
			if is_passable_fence_name(full_name):
				return True
			return False
		else:
			unitMass = float(AreaDestructibles.g_cache.unitVehicleMass)
			if unitMass > 0.0:
				instantDamage *= math.pow(
					mass / unitMass, desc.get('kineticDamageCorrection', 0.0))
			referenceHealth = float(desc.get('health', 50.0))
			scaled_hp = float(DestructiblesCache.scaledDestructibleHealth(scale, referenceHealth))
			return scaled_hp < (instantDamage * 2.0)
	except Exception:
		return False


def _callback_int(value, name):
	if isinstance(value, bool) or not isinstance(value, _INTEGER_TYPES):
		raise ValueError('%s must be an integer' % name)
	return int(value)


def _finite_float(value, name):
	try:
		value = float(value)
	except Exception:
		raise ValueError('%s must be finite' % name)
	if math.isnan(value) or math.isinf(value):
		raise ValueError('%s must be finite' % name)
	return value


def _validate_point(pos):
	try:
		values = (pos.x, pos.y, pos.z)
	except Exception:
		try:
			values = (pos[0], pos[1], pos[2])
		except Exception:
			raise ValueError('hitPoint must contain three finite values')
	for value in values:
		_finite_float(value, 'hitPoint')


def _destructible_scale(spaceID, chunkID, itemIndex):
	matrix_value = BigWorld.wg_getDestructibleMatrix(
		spaceID, chunkID, itemIndex)
	if matrix_value is None:
		return None
	matrix = Math.Matrix(matrix_value)
	axis = matrix.applyVector(Math.Vector3(0.0, 1.0, 0.0))
	scale = _finite_float(axis.length, 'destructible scale')
	if scale <= 0.0:
		raise ValueError('destructible scale must be positive')
	return scale


def _collision_record(spaceID, chunkID, itemIndex):
	key = (chunkID, itemIndex)
	record = _state['collisionHealth'].get(key)
	if record is not None:
		return record

	import AreaDestructibles
	import DestructiblesCache
	desc = AreaDestructibles.g_cache.getDestructibleDesc(
		spaceID, chunkID, itemIndex)
	if desc is None:
		return None
	scale = _destructible_scale(spaceID, chunkID, itemIndex)
	if scale is None:
		return None

	destructible_type = int(desc['type'])
	values = {}
	dependencies = {}
	if destructible_type == AreaDestructibles.DESTR_TYPE_STRUCTURE:
		import constants
		_dm = getattr(constants, 'DESTRUCTIBLE_MATKIND', None)
		normal_min = int(getattr(_dm, 'NORMAL_MIN', getattr(DestructiblesCache, 'DESTRUCTIBLE_MATKIND_NORMAL_MIN', 71)))
		normal_max = int(getattr(_dm, 'NORMAL_MAX', getattr(DestructiblesCache, 'DESTRUCTIBLE_MATKIND_NORMAL_MAX', 101)))
		modules = desc.get('modules')
		if not isinstance(modules, dict) or not modules:
			return None
		for raw_kind, module in modules.items():
			try:
				mat_kind = _callback_int(raw_kind, 'module matKind')
			except Exception:
				continue
			if mat_kind < normal_min or mat_kind > normal_max:
				continue
			values[mat_kind] = int(
				DestructiblesCache.scaledDestructibleHealth(
					scale, int(module.get('health', 100) if isinstance(module, dict) else 100)))
		if not values:
			return None
		for raw_root, raw_depends in desc.get(
				'destroyDepends', {}).items():
			root = _callback_int(raw_root, 'destroyDepends matKind')
			if root not in values:
				continue
			depends = set()
			for raw_kind in raw_depends:
				mat_kind = _callback_int(
					raw_kind, 'destroyDepends dependent matKind')
				if mat_kind in values and mat_kind != root:
					depends.add(mat_kind)
			if depends:
				dependencies[root] = tuple(sorted(depends))
	else:
		allowed_types = (
			AreaDestructibles.DESTR_TYPE_TREE,
			AreaDestructibles.DESTR_TYPE_FALLING_ATOM,
			AreaDestructibles.DESTR_TYPE_FRAGILE,
		)
		if destructible_type not in allowed_types:
			raise ValueError('unsupported destructible type')
		values[None] = int(
			DestructiblesCache.scaledDestructibleHealth(
				scale, int(desc['health'])))

	record = {
		'type': destructible_type,
		'values': values,
		'dependencies': dependencies,
	}
	_state['collisionHealth'][key] = record
	return record


def collision_health(spaceID, chunkID, itemIndex):
	"""Return the exact WGVehiclePhysics2 health callback payload.

	Non-structure objects use one scalar health value. Structures use all of
	their normal material kinds; omitted dictionary entries are interpreted as
	zero by the retail executable. ``None`` means the streamed descriptor or
	matrix is not available yet and preserves solid collision in native code.
	"""
	spaceID = _callback_int(spaceID, 'spaceID')
	chunkID = _callback_int(chunkID, 'chunkID')
	itemIndex = _callback_int(itemIndex, 'itemIndex')
	_reset_if_new_space(spaceID)
	record = _collision_record(spaceID, chunkID, itemIndex)
	if record is None:
		return None
	values = record['values']
	desc = resolve_destructible_desc(spaceID, chunkID, itemIndex, '')
	full_name = resolve_destructible_filename(spaceID, chunkID, itemIndex, '', desc)
	is_impassable_rail = is_impassable_destructible_name(full_name)
	is_fence = is_passable_fence_name(full_name)
	if None in values:
		if is_destroyed(chunkID, itemIndex, None):
			return 0
		raw_hp = int(values[None])
		if is_impassable_rail:
			return max(raw_hp, 25000)
		if is_fence or raw_hp <= 650:
			return max(1, min(raw_hp, 12))
		return raw_hp
	result = {}
	for mat_kind, health in values.items():
		if is_destroyed(chunkID, itemIndex, mat_kind):
			result[int(mat_kind)] = 0
		else:
			raw_hp = int(health)
			if is_impassable_rail:
				result[int(mat_kind)] = max(raw_hp, 25000)
			elif is_fence or raw_hp <= 180:
				result[int(mat_kind)] = max(1, min(raw_hp, 12))
			elif raw_hp <= 480:
				result[int(mat_kind)] = max(1, min(raw_hp, 32))
			else:
				result[int(mat_kind)] = raw_hp
	return result


def _mark_collision_destroyed(chunkID, itemIndex, matKinds):
	c = _chunk(chunkID)
	for mat_kind in matKinds:
		c['keys'].add((itemIndex, mat_kind))


def apply_collision_damage(spaceID, chunkID, itemIndex, matKind, damage,
		pos, fallYaw, impactSpeed):
	"""Apply one six-argument native damage event to authoritative health.

	Returns False for partial damage and True for an already or newly destroyed
	object/module. Invalid native data and terminal delivery failures raise so
	the owning body can fail closed after the shared physics batch completes.
	"""
	spaceID = _callback_int(spaceID, 'spaceID')
	chunkID = _callback_int(chunkID, 'chunkID')
	itemIndex = _callback_int(itemIndex, 'itemIndex')
	matKind = _callback_int(matKind, 'matKind')
	damage = _callback_int(damage, 'damage')
	if damage <= 0:
		raise ValueError('damage must be positive')
	_validate_point(pos)
	fallYaw = _finite_float(fallYaw, 'fallYaw')
	impactSpeed = _finite_float(impactSpeed, 'impactSpeed')

	_reset_if_new_space(spaceID)
	record = _collision_record(spaceID, chunkID, itemIndex)
	if record is None:
		raise RuntimeError('destructible descriptor is unavailable')

	import AreaDestructibles
	destructible_type = record['type']
	value_key = None
	if destructible_type == AreaDestructibles.DESTR_TYPE_STRUCTURE:
		value_key = matKind
		if value_key not in record['values']:
			raise ValueError('structure matKind is unavailable')
	if is_destroyed(chunkID, itemIndex, value_key):
		record['values'][value_key] = 0
		return True

	current = int(record['values'][value_key])
	if current <= 0:
		raise RuntimeError('damage reported for non-positive health')
	remaining = max(0, current - damage)
	if remaining > 0:
		record['values'][value_key] = remaining
		return False

	if destructible_type == AreaDestructibles.DESTR_TYPE_TREE:
		applied = destroy_tree(
			spaceID, chunkID, itemIndex, fallYaw, impactSpeed, pos)
	elif destructible_type == AreaDestructibles.DESTR_TYPE_FALLING_ATOM:
		applied = destroy_column(
			spaceID, chunkID, itemIndex, fallYaw, impactSpeed, pos)
	elif destructible_type == AreaDestructibles.DESTR_TYPE_FRAGILE:
		applied = destroy_fragile(spaceID, chunkID, itemIndex, pos)
	elif destructible_type == AreaDestructibles.DESTR_TYPE_STRUCTURE:
		applied = destroy_module(
			spaceID, chunkID, itemIndex, matKind, pos, False)
	else:
		raise RuntimeError('unsupported destructible type')
	if not applied:
		raise RuntimeError('terminal destructible update was not delivered')

	record['values'][value_key] = 0
	destroyed_kinds = [value_key]
	if value_key is not None:
		for dependent in record['dependencies'].get(value_key, ()):
			record['values'][dependent] = 0
			destroyed_kinds.append(dependent)
	_mark_collision_destroyed(chunkID, itemIndex, destroyed_kinds)
	return True


def _ensure_chunk(spaceID, chunkID, pos):
	"""Start the manager space, spawn the chunk controller entity once
	(the real server did this) and register the chunk. Returns the
	controller, or None while the entity is still spawning."""
	import AreaDestructibles
	mgr = AreaDestructibles.g_destructiblesManager
	if mgr is None:
		return None
	# Keep the manager's space in sync with the CURRENT battle. Starting only
	# when None left a STALE spaceID after battle 1: mgr kept the previous,
	# now-RELEASED space, so the engine's __launchFallEffect later called
	# getDestructibleDesc(self.__spaceID, ...) -> wg_getDestructibleFilename
	# with a dead/None spaceID = "argument 1 must be set to an int", aborting
	# the tree/column fall (seen on a later-battle Fjords). startSpace() does
	# clear() first, so re-starting on a space change is safe and fires once.
	_cur_sid = int(spaceID) if spaceID is not None else None
	try:
		_mgr_sid = mgr.getSpaceID()
	except Exception:
		_mgr_sid = None
	if _cur_sid is not None and _mgr_sid != _cur_sid:
		mgr.startSpace(_cur_sid)
	if chunkID not in _state['entities']:
		c = _chunk(chunkID)
		try:
			entityID = BigWorld.createEntity('AreaDestructibles', spaceID, 0, Math.Vector3(pos[0], pos[1], pos[2]), (0.0, 0.0, 0.0), {
				'fallenTrees': list(c['fallenTrees']),
				'fallenColumns': list(c['fallenColumns']),
				'destroyedFragiles': list(c['destroyedFragiles']),
				'destroyedModules': list(c['destroyedModules']),
			})
			if entityID is None or int(entityID) <= 0:
				raise RuntimeError('AreaDestructibles entity was not created')
			_state['entities'].add(chunkID)
		except Exception:
			_log('DestrAuth: createEntity failed for chunk', chunkID)
	try:
		if not mgr.isChunkLoaded(chunkID):
			fnames = BigWorld.wg_getChunkDestrFilenames(spaceID, chunkID)
			if fnames is not None:
				mgr.onChunkLoad(chunkID, len(fnames))
	except Exception:
		_log('DestrAuth: onChunkLoad failed for chunk', chunkID)
	return mgr.getController(chunkID)


def _apply(spaceID, chunkID, pos, kind, destrData, dedupKey):
	import AreaDestructibles
	_reset_if_new_space(spaceID)
	c = _chunk(chunkID)
	if dedupKey in c['keys']:
		return False
	if dedupKey in c['faults']:
		return False
	ctrl = _ensure_chunk(spaceID, chunkID, pos)
	prop = _PROP_BY_KIND[kind]
	# Use exactly one irreversible native delivery boundary. Calling a controller
	# setter here would call this manager again internally, and an exception after
	# that nested call cannot be rolled back safely.
	dmgTypes = {
		'tree': AreaDestructibles._DAMAGE_TYPE_TREE,
		'column': AreaDestructibles._DAMAGE_TYPE_COLUMN,
		'fragile': AreaDestructibles._DAMAGE_TYPE_FRAGILE,
		'module': AreaDestructibles._DAMAGE_TYPE_MODULE,
	}
	try:
		try:
			import helpers as _da_hlp
			_true_fn = lambda: True
			try:
				if hasattr(_da_hlp.isPlayerAvatar, 'func_code'):
					_da_hlp.isPlayerAvatar.func_code = _true_fn.func_code
			except Exception:
				pass
			_da_hlp.isPlayerAvatar = _true_fn
			AreaDestructibles.isPlayerAvatar = _true_fn
		except Exception:
			pass
		AreaDestructibles.g_destructiblesManager.orderDestructibleDestroy(
			chunkID, dmgTypes[kind], destrData, True)
		try:
			if pos is not None and BigWorld.camera() is not None:
				_cpos = BigWorld.camera().position
				_ddx = float(pos[0]) - float(_cpos.x)
				_ddz = float(pos[2]) - float(_cpos.z)
				_dist_sq = _ddx * _ddx + _ddz * _ddz
				if _dist_sq <= 22500.0:
					_pl_da = BigWorld.player()
					_fm_da = getattr(_pl_da, '_offh_destr_snd_carrier', None) if _pl_da is not None else None
					_fm_mat = getattr(_pl_da, '_offh_destr_snd_mat', None) if _pl_da is not None else None
					if _fm_da is None or not getattr(_fm_da, 'inWorld', False) or getattr(_pl_da, '_offh_destr_snd_sid', None) != spaceID:
						_fm_da = BigWorld.Model('objects/fake_model.model')
						BigWorld.addModel(_fm_da)
						try:
							BigWorld.addAlwaysUpdateModel(_fm_da)
						except Exception:
							pass
						_fm_mat = Math.Matrix()
						_fm_mat.setIdentity()
						try:
							_fm_da.addMotor(BigWorld.Servo(_fm_mat))
						except Exception:
							pass
						if _pl_da is not None:
							_pl_da._offh_destr_snd_carrier = _fm_da
							_pl_da._offh_destr_snd_mat = _fm_mat
							_pl_da._offh_destr_snd_sid = spaceID
					_wpos = Math.Vector3(float(pos[0]), float(pos[1]), float(pos[2]))
					if _fm_mat is not None:
						_fm_mat.setTranslate(_wpos)
					try:
						_fm_da.position = _wpos
					except Exception:
						pass
					# Extract authentic FMOD event names from the destructible descriptor's EffectsLists
					_snd_names = []
					try:
						_item_idx = dedupKey[0] if isinstance(dedupKey, tuple) else int(destrData)
						_mat_k = dedupKey[1] if (isinstance(dedupKey, tuple) and len(dedupKey) > 1) else None
						_ddesc = AreaDestructibles.g_cache.getDestructibleDesc(spaceID, chunkID, _item_idx)
						if isinstance(_ddesc, dict):
							_cand_effs = []
							for _ek in ('effectDesc', 'fractureEffect', 'touchdownEffect', 'fallEffect', 'decayEffect'):
								if _ek in _ddesc and _ddesc[_ek] is not None:
									_cand_effs.append(_ddesc[_ek])
							if _mat_k is not None and isinstance(_ddesc.get('modules'), dict):
								_m_dict = _ddesc['modules'].get(_mat_k)
								if isinstance(_m_dict, dict):
									for _mk in ('ramEffect', 'hitEffect', 'decayEffect', 'effectDesc'):
										if _mk in _m_dict and _m_dict[_mk] is not None:
											_cand_effs.append(_m_dict[_mk])
							for _ce in _cand_effs:
								_el = _ce[1] if (isinstance(_ce, (tuple, list)) and len(_ce) >= 2) else getattr(_ce, 'effectsList', _ce)
								for _ed in (getattr(_el, '_EffectsList__effectDescList', None) or ()):
									_sn = getattr(_ed, '_soundName', None) or getattr(_ed, 'soundName', None)
									if _sn and _sn not in _snd_names:
										_snd_names.append(_sn)
									_imps = getattr(_ed, '_impactNames', None)
									if isinstance(_imps, (tuple, list)):
										for _im in _imps:
											if _im and _im not in _snd_names:
												_snd_names.append(_im)
					except Exception:
						pass
					if _dist_sq <= 625.0 or not _snd_names:
						_snd_names.append('/hits_n_impacts/collision_with_an_obstacle')
					if _snd_names:
						try:
							from gui.mods.offhangar import offline_battle as _ob_da
							_ob_da._offh_play_persistent_sound(_snd_names[0], model=_fm_da)
						except Exception:
							pass
		except Exception:
			pass
	except Exception:
		# The native method is irreversible and offers no receipt on exception.
		# Latch this contact as a terminal fault instead of risking a duplicate
		# delivery on the next physics frame.
		c['faults'].add(dedupKey)
		_log('DestrAuth: terminal direct order failure', chunkID, kind)
		return False
	# Commit replay and dedup state only after native delivery accepted it.
	c[prop].append(destrData)
	c['keys'].add(dedupKey)
	if ctrl is not None:
		# Mirror the accepted authoritative state without firing set_* and therefore
		# without issuing a second native order.
		try:
			values = getattr(ctrl, prop)
			if destrData not in values:
				values.append(destrData)
			setattr(ctrl, _PREV_BY_PROP[prop], frozenset(values))
		except Exception:
			_log('DestrAuth: controller ledger sync failed', chunkID, kind)
	return True


# --- wire reporting -----------------------------------------------------------
# Every destruction in EVERY process funnels through destroy_tree / destroy_column
# / destroy_fragile / destroy_module below (their only callers are
# apply_collision_damage here, the shell-impact path in offline_battle.py and one
# more path in the same file), so this is the single place to announce it.
# The LAN layer installs a callback once per battle; in single-process mode it
# stays None and nothing changes. `_SUPPRESS` is set while a REMOTE event is being
# applied locally, otherwise every replica would re-report what it just received
# and the event would loop forever.
_REPORT = [None]
_SUPPRESS = [False]
_TX = [0]


def set_report_callback(fn):
	"""Install (or clear, with None) the wire reporter for destroyed objects."""
	_REPORT[0] = fn if callable(fn) else None


def suppress_reporting(flag):
	"""Temporarily stop reporting - used while applying a remote event."""
	_SUPPRESS[0] = bool(flag)


def _report_destroyed(kind, chunkID, itemIndex, matKind, fallYaw, speed, pos):
	fn = _REPORT[0]
	if fn is None or _SUPPRESS[0]:
		return
	try:
		_x = 0.0
		_y = 0.0
		_z = 0.0
		if pos is not None:
			_x = float(pos[0])
			_y = float(pos[1])
			_z = float(pos[2])
		_rv = fn(kind, int(chunkID), int(itemIndex),
			(-1 if matKind is None else int(matKind)),
			float(fallYaw or 0.0), float(speed or 0.0), (_x, _y, _z))
		# Trace (first 40 per process): a destruction that reaches the reporter but
		# not the other side must be attributable to a hop, not guessed. `_rv` is
		# what LANClient.send_destructible returned - False means it never left this
		# process (no connection, or rejected by the size guard).
		_TX[0] += 1
		if _TX[0] <= 40:
			# LOG_NOTE, not _log(): _log() goes to LOG_DEBUG, which logging.py gates
			# behind _DBG[0] (logging.py:33-35).  With debug off the marker never
			# reached python.log, so DESTR_TX=0 looked like "nothing was destroyed"
			# while the server was accepting 40 reports.  Capped at 40 per process.
			try:
				from gui.mods.offhangar.logging import LOG_NOTE as _TXLOG
				_TXLOG('DESTR_TX kind=%s chunk=%s item=%s mat=%s sent=%r total=%d' % (
					kind, chunkID, itemIndex, matKind, _rv, _TX[0]))
			except Exception:
				_log('DESTR_TX kind=%s chunk=%s sent=%r' % (kind, chunkID, _rv))
	except Exception as _tx_err:
		_TX[0] += 1
		if _TX[0] <= 40:
			try:
				from gui.mods.offhangar.logging import LOG_NOTE as _TXLOG2
				_TXLOG2('DESTR_TX FAILED %r' % (_tx_err,))
			except Exception:
				_log('DESTR_TX FAILED %r' % (_tx_err,))


def destroy_tree(spaceID, chunkID, itemIndex, fallYaw, speed, pos):
	import AreaDestructibles
	# Native getDestructibleDesc (called by the game's __launchFallEffect)
	# demands a plain int destrID; a float/long index reaching it raised
	# 'argument 1 must be set to an int'. Coerce here.
	chunkID = int(chunkID); itemIndex = int(itemIndex)
	pitch = math.pi / 2.0
	try:
		pc = BigWorld.wg_getDestructibleFallPitchConstr(spaceID, chunkID, itemIndex, fallYaw)
		if pc is not None and pc[0] is not None:
			pitch = pc[0]
	except Exception:
		pass
	speed = max(1, min(3, int(abs(speed))))
	data = AreaDestructibles.encodeFallenTree(itemIndex, fallYaw, pitch, speed)
	_ok = _apply(spaceID, chunkID, pos, 'tree', data, (itemIndex, None))
	if _ok:
		_report_destroyed('tree', chunkID, itemIndex, None, fallYaw, speed, pos)
	return _ok


def destroy_column(spaceID, chunkID, itemIndex, fallYaw, speed, pos):
	import AreaDestructibles
	chunkID = int(chunkID); itemIndex = int(itemIndex)
	speed = max(1, min(3, int(abs(speed))))
	data = AreaDestructibles.encodeFallenColumn(itemIndex, fallYaw, speed)
	_ok = _apply(spaceID, chunkID, pos, 'column', data, (itemIndex, None))
	if _ok:
		_report_destroyed('column', chunkID, itemIndex, None, fallYaw, speed, pos)
	return _ok


def destroy_fragile(spaceID, chunkID, itemIndex, pos):
	# Fragiles take the RAW item index: __destroyFragile does no decode.
	chunkID = int(chunkID); itemIndex = int(itemIndex)
	_ok = _apply(spaceID, chunkID, pos, 'fragile', itemIndex, (itemIndex, None))
	if _ok:
		_report_destroyed('fragile', chunkID, itemIndex, None, 0.0, 0.0, pos)
	return _ok


def destroy_module(spaceID, chunkID, itemIndex, matKind, pos, isShotDamage=False):
	import AreaDestructibles
	chunkID = int(chunkID); itemIndex = int(itemIndex)
	if matKind is not None:
		matKind = int(matKind)
	data = AreaDestructibles.encodeDestructibleModule(itemIndex, matKind, isShotDamage)
	_ok = _apply(spaceID, chunkID, pos, 'module', data, (itemIndex, matKind))
	if _ok:
		_report_destroyed('module', chunkID, itemIndex, matKind, 0.0, 0.0, pos)
	return _ok
