# -*- coding: utf-8 -*-
import cPickle
import os

from debug_utils import LOG_CURRENT_EXCEPTION
from items import ITEM_TYPE_INDICES

from gui.mods.offhangar.logging import LOG_DEBUG
from gui.mods.offhangar import paths as _paths


# Lives outside res_mods so mod updates never touch the garage save.
STATE_FILE = _paths.USER_STATE_FILE
_ACTIVE_PROFILE_READY = False
_paths.ensure_user_dir()
STATE_VERSION = 2


def activate_profile_state_file(path):
	'''Select the authenticated profile's state before any inventory state is read.'''
	global STATE_FILE, _ACTIVE_PROFILE_READY
	if not path:
		return False
	try:
		_state_path = os.path.normpath(str(path))
		if not os.path.isabs(_state_path):
			_state_path = os.path.normpath(os.path.join(os.getcwd(), _state_path))
		STATE_FILE = _state_path
		_ACTIVE_PROFILE_READY = True
		return True
	except Exception:
		LOG_CURRENT_EXCEPTION()
	return False


def is_profile_state_ready():
	return bool(_ACTIVE_PROFILE_READY)


def _state_log_error(message):
	try:
		import traceback
		from gui.mods.offhangar.logging import LOG_ERROR
		LOG_ERROR('PROFILE_STATE %s\\n%s' % (message, traceback.format_exc()))
	except Exception:
		LOG_CURRENT_EXCEPTION()
	try:
		from gui import SystemMessages
		SystemMessages.pushMessage('Profile data could not be loaded. Please check python.log.'.encode('utf-8'), SystemMessages.SM_TYPE.Error)
	except Exception:
		pass

VEHICLE_FIELDS = (
	'compDescr',
	'eqs',
	'eqsLayout',
	'shells',
	'shellsLayout',
	'settings'
)


def _default_state():
	return {'version': STATE_VERSION, 'vehicles': {}, 'items': {}}


def _fallback_root_state_path():
	return os.path.normpath(os.path.join(os.getcwd(), _paths.USER_STATE_FILE))


def _activate_fallback_root_state():
	'''Use the legacy shared state only before an account profile is active.'''
	global STATE_FILE
	if not _ACTIVE_PROFILE_READY:
		STATE_FILE = _fallback_root_state_path()
	return STATE_FILE


def _migrate_state(state):
	'''Keep compatible garage data when an older profile schema is found.'''
	if not isinstance(state, dict):
		return _default_state(), False
	changed = state.get('version') != STATE_VERSION
	if not isinstance(state.get('vehicles'), dict):
		state['vehicles'] = {}
		changed = True
	if not isinstance(state.get('items'), dict):
		state['items'] = {}
		changed = True
	if state.get('version') != STATE_VERSION:
		state['version'] = STATE_VERSION
		changed = True
	return state, changed


def load_state():
	_activate_fallback_root_state()
	if not os.path.exists(STATE_FILE):
		return _default_state()
	try:
		f = open(STATE_FILE, 'rb')
		try:
			state = cPickle.load(f)
		finally:
			f.close()
		state, changed = _migrate_state(state)
		if changed:
			save_state(state)
		return state
	except Exception:
		_state_log_error('load failed path=%s' % STATE_FILE)
	return _default_state()


def save_state(state):
	try:
		tmpPath = STATE_FILE + '.tmp'
		f = open(tmpPath, 'wb')
		try:
			cPickle.dump(state, f, cPickle.HIGHEST_PROTOCOL)
		finally:
			f.close()
		if os.path.exists(STATE_FILE):
			os.remove(STATE_FILE)
		os.rename(tmpPath, STATE_FILE)
		return True
	except Exception:
		_state_log_error('save failed path=%s' % STATE_FILE)
	return False


def _compact_descr_key(value):
	'''Normalize Python 2 byte/unicode compact descriptors without lossy decoding.'''
	if value is None:
		return None
	try:
		if isinstance(value, unicode):
			return value.encode('latin-1')
	except NameError:
		pass
	try:
		return str(value)
	except Exception:
		return None


def apply_state_to_inventory(inventory):
	state = load_state()
	vehData = inventory.get(ITEM_TYPE_INDICES['vehicle'], {})
	_stale_ids = []
	_current_descrs = vehData.get('compDescr', {}) or {}
	for vehInvID, saved in state.get('vehicles', {}).iteritems():
		try:
			vehInvID = int(vehInvID)
		except (ValueError, TypeError):
			pass
		if not isinstance(saved, dict):
			_stale_ids.append(vehInvID)
			continue
		_saved_cd = saved.get('compDescr', saved.get(b'compDescr'))
		_current_cd = _current_descrs.get(vehInvID)
		if not _saved_cd or _current_cd is None:
			LOG_DEBUG('State.skipMalformedVehicle', vehInvID)
			_stale_ids.append(vehInvID)
			continue
		is_compat = False
		if _compact_descr_key(_saved_cd) == _compact_descr_key(_current_cd):
			is_compat = True
		else:
			try:
				from items import vehicles
				saved_vd = vehicles.VehicleDescr(compactDescr=_saved_cd)
				current_vd = vehicles.VehicleDescr(compactDescr=_current_cd)
				if getattr(saved_vd.type, 'id', None) == getattr(current_vd.type, 'id', None):
					is_compat = True
			except Exception:
				is_compat = False
		if not is_compat:
			LOG_DEBUG('State.skipIncompatibleVehicle', vehInvID)
			_stale_ids.append(vehInvID)
			continue
		for fieldName in VEHICLE_FIELDS:
			if fieldName == 'compDescr':
				vehData.setdefault('compDescr', {})[vehInvID] = _saved_cd
				continue
			_saved_value = saved.get(fieldName, saved.get(fieldName.encode('ascii')))
			if _saved_value is not None and fieldName in vehData:
				vehData[fieldName][vehInvID] = _saved_value
	if _stale_ids:
		LOG_DEBUG('State.skippedIncompatibleVehicles', len(_stale_ids))
	for _eq_field in ('eqs', 'eqsLayout'):
		_eq_map = vehData.get(_eq_field)
		if isinstance(_eq_map, dict):
			for _vid, _elist in list(_eq_map.items()):
				if isinstance(_elist, (list, tuple)):
					_norm_l = [abs(int(x or 0)) for x in _elist[:3]]
					while len(_norm_l) < 3:
						_norm_l.append(0)
					_eq_map[_vid] = _norm_l
	_shells_map = vehData.get('shells')
	if isinstance(_shells_map, dict):
		for _vid, _slist in list(_shells_map.items()):
			if isinstance(_slist, (list, tuple)):
				if len(_slist) % 2 != 0 or (len(_slist) > 6 and _slist[0] == 6):
					_raw = list(_slist)
					if _raw and _raw[0] == 6 and len(_raw) >= 7:
						_clean_pairs = _raw[1:7]
					else:
						_clean_pairs = _raw[:len(_raw) - (len(_raw) % 2)]
					_shells_map[_vid] = _clean_pairs
	_shells_layout_map = vehData.get('shellsLayout')
	if isinstance(_shells_layout_map, dict):
		for _vid, _tg_dict in list(_shells_layout_map.items()):
			if isinstance(_tg_dict, dict):
				for _tg_key, _slist in list(_tg_dict.items()):
					if isinstance(_slist, (list, tuple)):
						if len(_slist) % 2 != 0 or (len(_slist) > 6 and _slist[0] == 6):
							_raw = list(_slist)
							if _raw and _raw[0] == 6 and len(_raw) >= 7:
								_clean_pairs = _raw[1:7]
							else:
								_clean_pairs = _raw[:len(_raw) - (len(_raw) % 2)]
							_tg_dict[_tg_key] = _clean_pairs
	for itemTypeIdx, savedItems in state.get('items', {}).iteritems():
		try:
			_idx_num = int(itemTypeIdx)
		except (ValueError, TypeError):
			continue
		bucket = inventory.setdefault(_idx_num, {})
		if isinstance(bucket, dict) and isinstance(savedItems, dict):
			if _idx_num == ITEM_TYPE_INDICES.get('tankman', 8):
				try:
					import items.tankmen
					valid_tmen = {}
					saved_tmen = savedItems.get('compDescr', savedItems.get(b'compDescr', {}))
					if isinstance(saved_tmen, dict):
						for tid, tcd in saved_tmen.items():
							try:
								items.tankmen.TankmanDescr(tcd)
								valid_tmen[int(tid)] = tcd
							except Exception:
								pass
					bucket.setdefault('compDescr', {}).update(valid_tmen)
					saved_veh = savedItems.get('vehicle', savedItems.get(b'vehicle', {}))
					if isinstance(saved_veh, dict):
						for tk, tv in saved_veh.items():
							try:
								bucket.setdefault('vehicle', {})[int(tk)] = int(tv)
							except Exception:
								pass
				except Exception:
					pass
			else:
				bucket.update(savedItems)
	LOG_DEBUG('State.apply', len(state.get('vehicles', {})), 'vehicles')
	return inventory


def save_vehicle_state(inventory, vehInvID):
	try:
		vehInvID = int(vehInvID)
	except (ValueError, TypeError):
		pass
	state = load_state()
	vehData = inventory.get(ITEM_TYPE_INDICES['vehicle'], {})
	saved = {}
	for fieldName in VEHICLE_FIELDS:
		field = vehData.get(fieldName, {})
		if isinstance(field, dict) and vehInvID in field:
			saved[fieldName] = field[vehInvID]
	state.setdefault('vehicles', {})[vehInvID] = saved
	ok = save_state(state)
	if ok:
		LOG_DEBUG('State.saveVehicle', vehInvID)
	return ok


def save_item_state(inventory, itemTypeIdx):
	try:
		itemTypeIdx = int(itemTypeIdx)
	except (ValueError, TypeError):
		pass
	state = load_state()
	bucket = inventory.get(itemTypeIdx, {})
	if isinstance(bucket, dict):
		state.setdefault('items', {})[itemTypeIdx] = bucket.copy()
		ok = save_state(state)
		if ok:
			LOG_DEBUG('State.saveItems', itemTypeIdx, len(bucket))
		return ok
	return False


def save_selected_vehicle(vehInvID):
	try:
		vid = int(vehInvID or 0)
		if vid <= 0:
			return False
		state = load_state()
		if state.get('selected_veh_inv_id') == vid:
			return True
		state['selected_veh_inv_id'] = vid
		ok = save_state(state)
		try:
			import BigWorld, Settings
			if getattr(Settings, 'g_instance', None) is not None:
				Settings.g_instance.userPrefs[Settings.KEY_CURRENT_VEHICLE].writeInt(vid)
				Settings.g_instance.save()
			BigWorld.savePreferences()
		except Exception:
			pass
		return ok
	except Exception:
		LOG_CURRENT_EXCEPTION()
	return False


def load_selected_vehicle():
	try:
		state = load_state()
		vid = int(state.get('selected_veh_inv_id', 0) or 0)
		if vid > 0:
			try:
				import BigWorld
				player = BigWorld.player()
				inventory = getattr(player, 'inventory', None) if player is not None else None
				cache = getattr(inventory, '_Inventory__cache', None) or {}
				vehicle_data = cache.get(ITEM_TYPE_INDICES['vehicle'], {})
				if vid in vehicle_data.get('compDescr', {}):
					return vid
			except Exception:
				return vid
			vid = 0
		return None
	except Exception:
		pass
	return None
