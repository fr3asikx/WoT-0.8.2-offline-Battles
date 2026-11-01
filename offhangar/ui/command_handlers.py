# -*- coding: utf-8 -*-
from __future__ import absolute_import
import cPickle
import functools
import random
import time
import zlib

import AccountCommands
import BigWorld
import game
import items
from debug_utils import LOG_CURRENT_EXCEPTION
from items import ITEM_TYPE_INDICES, vehicles

from gui.mods.offhangar._constants import REQUEST_CALLBACK_TIME
from gui.mods.offhangar.command_router import RequestResult
from gui.mods.offhangar.data import (
	getOfflineInventory,
	getOfflineStats,
	getOfflineQuestsProgress,
	getOfflineShopItems
)
from gui.mods.offhangar.logging import LOG_DEBUG
from gui.mods.offhangar.offline_battle import (
	schedule_random_battle_flow_after_enqueue,
	start_offline_random_from_hangar,
)
from gui.mods.offhangar.session_guards import normalize_offline_stats
from gui.mods.offhangar.state import save_item_state, save_vehicle_state


def _resolve_cmd(name, fallback):
	return getattr(AccountCommands, name, fallback)


_ACCOUNT_CMD_INDEX = {}

# Core sync/shop/stat commands.
CMD_REQ_SERVER_STATS = _resolve_cmd('CMD_REQ_SERVER_STATS', 100)
CMD_COMPLETE_TUTORIAL = _resolve_cmd('CMD_COMPLETE_TUTORIAL', None)
CMD_SYNC_DATA = _resolve_cmd('CMD_SYNC_DATA', None)
CMD_SYNC_SHOP = _resolve_cmd('CMD_SYNC_SHOP', None)
CMD_SYNC_DOSSIERS = _resolve_cmd('CMD_SYNC_DOSSIERS', None)
CMD_SET_LANGUAGE = _resolve_cmd('CMD_SET_LANGUAGE', 105)

# Battle pipeline (0.8.x may miss some symbolic names, so keep numeric fallback).
CMD_ENQUEUE_RANDOM = _resolve_cmd('CMD_ENQUEUE_RANDOM', 202)
# 0.8.2 has NEITHER name, and the old numeric fallbacks (203/204) landed on real
# command ids - CMD_FREE_XP_CONV=203 and CMD_PREMIUM=204 (AccountCommands.py:40-44).
# Buying premium was therefore answered by handle_prebattle with a bare _success(),
# and the dialog died natively (0xC0000005 @ 0x00C66B43, log 06.10 12:32). None means
# "this client has no such command" and the registration below is then skipped.
CMD_PREBATTLE_ACTION = _resolve_cmd('CMD_PREBATTLE_ACTION', None)
CMD_ARENA_LIST = _resolve_cmd('CMD_ARENA_LIST', None)
CMD_REQ_QUEUE_INFO = _resolve_cmd('CMD_REQ_QUEUE_INFO', 502)

# Hangar fitting/shop commands used by 0.8.2. Symbolic names are preferred when
# present; numeric fallbacks match the values observed in python.log.
# NOTE: Actual values confirmed from python.log:
#   CMD_BUY_ITEM=302, CMD_BUY_AND_EQUIP_ITEM=308, CMD_EQUIP=101,
#   CMD_EQUIP_OPTDEV=102, CMD_EQUIP_SHELLS=103, CMD_EQUIP_EQS=104,
#   CMD_SET_AND_FILL_LAYOUTS=108, CMD_VEH_SETTINGS=107
CMD_BUY_ITEM = _resolve_cmd('CMD_BUY_ITEM', 302)
CMD_BUY_AND_EQUIP_ITEM = _resolve_cmd('CMD_BUY_AND_EQUIP_ITEM', 308)
CMD_EQUIP = _resolve_cmd('CMD_EQUIP', None)
CMD_EQUIP_OPTDEV = _resolve_cmd('CMD_EQUIP_OPTDEV', None)
CMD_EQUIP_SHELLS = _resolve_cmd('CMD_EQUIP_SHELLS', None)
CMD_EQUIP_EQS = _resolve_cmd('CMD_EQUIP_EQS', None)
CMD_SET_AND_FILL_LAYOUTS = _resolve_cmd('CMD_SET_AND_FILL_LAYOUTS', None)
CMD_TMAN_ADD_SKILL = _resolve_cmd('CMD_TMAN_ADD_SKILL', 151)
CMD_TMAN_DROP_SKILLS = _resolve_cmd('CMD_TMAN_DROP_SKILLS', 152)
CMD_VEH_CAMOUFLAGE = _resolve_cmd('CMD_VEH_CAMOUFLAGE', 120)
CMD_VEH_HORN = _resolve_cmd('CMD_VEH_HORN', 121)
CMD_VEH_EMBLEM = _resolve_cmd('CMD_VEH_EMBLEM', 122)
CMD_VEH_INSCRIPTION = _resolve_cmd('CMD_VEH_INSCRIPTION', 123)
CMD_VEH_SETTINGS = _resolve_cmd('CMD_VEH_SETTINGS', 107)


def handle_customization(fake_server, requestID, cmd, args):
	try:
		from gui.mods.offhangar.data import getOfflineInventory
		import time
		import AccountCommands
		import items.vehicles as iv
		from debug_utils import LOG_CURRENT_EXCEPTION
		
		arr = args[0] if (isinstance(args, tuple) and len(args) == 1) else args
		vehInvID = arr[1]

		# Work on the SAME live inventory dict the fitting handlers use.
		# This used to regenerate a fresh copy via getOfflineInventory(): the
		# camo landed only there, so the next equip/module install (which reads
		# the live dict) rebuilt the descriptor WITHOUT the camo and persisted
		# it - 'equip camouflage, then install equipment -> camo resets'.
		inv = _get_inventory_data()
		if inv is None:
			inv = getOfflineInventory().get('inventory', {})
		compDescrs = inv.setdefault(ITEM_TYPE_INDICES['vehicle'], {}).setdefault('compDescr', {})

		if vehInvID not in compDescrs:
			return _success()

		vd = iv.VehicleDescr(compDescrs[vehInvID])
		now = int(time.time())

		if cmd == getattr(AccountCommands, 'CMD_VEH_CAMOUFLAGE', 120):
			vd.setCamouflage(arr[2], arr[3], now, arr[4])
		elif cmd == getattr(AccountCommands, 'CMD_VEH_INSCRIPTION', 123) or len(arr) == 6:
			vd.setPlayerInscription(arr[2], arr[3], now, arr[4], arr[5])
		elif cmd == getattr(AccountCommands, 'CMD_VEH_EMBLEM', 122) or len(arr) == 5:
			vd.setPlayerEmblem(arr[2], arr[3], now, arr[4])

		compDescrs[vehInvID] = vd.makeCompactDescr()
		from gui.mods.offhangar.state import save_vehicle_state
		save_vehicle_state(inv, vehInvID)

		diff = {'inventory': {ITEM_TYPE_INDICES['vehicle']: {'compDescr': {vehInvID: compDescrs[vehInvID]}}}}
		return _success_with_update(diff)
	except Exception:
		from debug_utils import LOG_CURRENT_EXCEPTION
		LOG_CURRENT_EXCEPTION()
		return RequestResult(0, 'Exception', None)


_VEHICLE_MODULE_TYPES = set(
	ITEM_TYPE_INDICES[name]
	for name in (
		'vehicleChassis',
		'vehicleEngine',
		'vehicleRadio',
		'vehicleTurret',
		'vehicleGun'
	)
	if name in ITEM_TYPE_INDICES
)




def _pack_stream(requestID, data):
	data = zlib.compress(cPickle.dumps(data))
	desc = cPickle.dumps((len(data), zlib.crc32(data)))
	return functools.partial(game.onStreamComplete, requestID, desc, data)


def _offh_deny_purchase_cfg():
	"""True when money commands may be refused instead of answered with a false success.

	Flag `offline_deny_purchase_commands`, shipped default false = today's behaviour.
	"""
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS
		return bool(CONFIG_OPTIONS.get('offline_deny_purchase_commands', False))
	except Exception:
		return False


def _unavailable(errorStr='OFFLINE'):
	return RequestResult(AccountCommands.RES_NOT_AVAILABLE, errorStr, None)


def handle_unavailable(fake_server, requestID, cmd, args):
	"""Refuse a purchase/money command honestly instead of reporting success.

	`handle_prebattle` used to answer these with a bare `_success()`, because the
	numeric fallbacks for two names 0.8.2 does not have (`CMD_PREBATTLE_ACTION`,
	`CMD_ARENA_LIST`) landed on real command ids: `AccountCommands.py:40-44` has
	`CMD_EXCHANGE=202`, `CMD_FREE_XP_CONV=203`, `CMD_PREMIUM=204`, `CMD_BUY_SLOT=205`,
	`CMD_BUY_BERTHS=206`. Measured 06.10 12:32 - clicking 'buy premium' produced
	`doCmdInt3 204 ... 0 360.0 0` -> `Router.dispatch 227 204 handler:
	handle_prebattle` -> `onCmdResponse ... 0` (false success) -> the dialog took its
	success branch with no data behind it and the client died with
	`0xC0000005 @ 0x00C66B43 (Read @ 0x0)`. A negative result makes the Flash dialog
	show an error and stay alive. `handle_unknown` is deliberately left alone: our own
	flows may depend on its permissive answer.
	"""
	LOG_DEBUG('CommandRouter.unavailable', requestID, cmd, args)
	return _unavailable()


def _success(data=None):
	return RequestResult(AccountCommands.RES_SUCCESS, '', data)


def _success_with_update(diff, data=None):
	if diff:
		try:
			import BigWorld
			player = BigWorld.player()
			if player is not None:
				inv = getattr(player, 'inventory', None)
				if inv is not None and hasattr(inv, 'synchronize'):
					try:
						inv.synchronize(False, diff)
					except Exception:
						pass
				try:
					if hasattr(player, '_update'):
						_rev0 = 0
						if hasattr(player, 'syncData') and hasattr(player.syncData, 'revision'):
							_rev0 = int(getattr(player.syncData, 'revision', 0) or 0)
						diff['prevRev'] = _rev0
						diff['rev'] = _rev0 + 1
						player._update(True, diff)
				except Exception:
					pass
				def _deferred_fitting_update(_p=player, _d=diff):
					try:
						_refresh_current_vehicle()
					except Exception:
						LOG_CURRENT_EXCEPTION()
				BigWorld.callback(0.08, _deferred_fitting_update)
		except Exception:
			LOG_CURRENT_EXCEPTION()
	return _success(data=data)


def _stream():
	return RequestResult(AccountCommands.RES_STREAM, '', None)


def _get_inventory_cache():
	player = BigWorld.player()
	inv = getattr(player, 'inventory', None)
	if inv is None:
		return None
	cache = getattr(inv, '_Inventory__cache', None)
	if not isinstance(cache, dict):
		return None
	return cache


def _get_inventory_data():
	"""Return the live inventory data dict (keyed by itemTypeIdx 1..11)."""
	player = BigWorld.player()
	cache = _get_inventory_cache()
	if cache is not None:
		data = cache.get('inventory')
		if isinstance(data, dict):
			return data
		if ITEM_TYPE_INDICES['vehicle'] in cache:
			if player is not None:
				player._offhangar_inventory = cache
			return cache
	if player is not None:
		syncData = getattr(player, 'syncData', None)
		if isinstance(syncData, dict):
			data = syncData.get('inventory')
			if isinstance(data, dict):
				return data
		data = getattr(player, '_offhangar_inventory', None)
		if isinstance(data, dict):
			return data
	return None


def _get_item_type_idx(compactDescr):
	try:
		return items.getTypeOfCompactDescr(compactDescr)
	except Exception:
		LOG_CURRENT_EXCEPTION()
	return None


def _add_inventory_item(compactDescr, count):
	invData = _get_inventory_data()
	itemTypeIdx = _get_item_type_idx(compactDescr)
	if invData is None or itemTypeIdx is None:
		LOG_DEBUG('Fitting.buyItem.noInventory', compactDescr, count)
		return None
	bucket = invData.setdefault(itemTypeIdx, {})
	if not isinstance(bucket, dict):
		return None
	bucket[compactDescr] = bucket.get(compactDescr, 0) + max(1, int(count or 1))
	save_item_state(invData, itemTypeIdx)
	return (itemTypeIdx, bucket[compactDescr])


def _inventory_add_diff(compactDescr, count):
	"""Buy an item AND return the inventory diff to push to the client, so
	the Research/buy panel sees it as owned (without this the module is
	installed on the vehicle but still shows as buyable -> re-buy loop)."""
	res = _add_inventory_item(compactDescr, count)
	if not res:
		return None
	itemTypeIdx, newCount = res
	return {'inventory': {itemTypeIdx: {compactDescr: newCount}}}


def _merge_diff(a, b):
	if not a:
		return b
	if not b:
		return a
	from gui.mods.offhangar.utils import deepUpdate
	return deepUpdate(a, b)


def _get_vehicle_data():
	invData = _get_inventory_data()
	if invData is None:
		return None
	return invData.get(ITEM_TYPE_INDICES['vehicle'], {})


def _persist_vehicle(vehInvID):
	invData = _get_inventory_data()
	if invData is not None:
		save_vehicle_state(invData, vehInvID)


def _set_vehicle_ammo_for_current_gun(vehData, vehInvID, vehDescr):
	try:
		turretGun = (
			vehicles.makeIntCompactDescrByID('vehicleTurret', *vehDescr.turret['id']),
			vehicles.makeIntCompactDescrByID('vehicleGun', *vehDescr.gun['id'])
		)
		ammo = vehicles.getDefaultAmmoForGun(vehDescr.gun)
		vehData.setdefault('shells', {})[vehInvID] = ammo
		vehData.setdefault('shellsLayout', {})[vehInvID] = {turretGun: ammo}
	except Exception:
		LOG_CURRENT_EXCEPTION()


def _refresh_current_vehicle():
	try:
		from CurrentVehicle import g_currentVehicle
		# onChanged() triggers synchronous Hangar.__onCurrentVehicleChanged.
		# Do NOT call update() here: update() runs an asynchronous 4-yield requester
		# that puts up Waiting.show('updateCurrentVehicle') and freezes the UI on module fitting.
		for methodName in ('onChanged', 'refreshModel', 'refresh'):
			method = getattr(g_currentVehicle, methodName, None)
			if callable(method):
				try:
					method()
					return
				except TypeError:
					continue
	except Exception:
		LOG_CURRENT_EXCEPTION()


def _legacy_handle_equip_module_unused(fake_server, requestID, cmd, args):
	if len(args) >= 2:
		vehInvID = args[0]
		moduleCD = args[1]
		if moduleCD:
			diff = _install_vehicle_module(vehInvID, moduleCD)
			if not diff:
				return _unavailable('MODULE_EQUIP_FAILED')
			return _success_with_update(diff)
	return _unavailable('INVALID_MODULE_EQUIP')


def handle_equip_opt_device(fake_server, requestID, cmd, args):
	if len(args) >= 3:
		vehInvID = args[0]
		deviceCD = args[1]
		slotIdx = args[2]
		diff = _equip_optional_device(vehInvID, deviceCD, slotIdx)
		return _success_with_update(diff)
	return _success()


# Which VehicleDescr attribute holds the fitted component of each module type.
# Needed to hand the REMOVED module back to the depot on a swap.
_MODULE_ATTR_BY_TYPE = {
	'vehicleChassis': 'chassis',
	'vehicleEngine': 'engine',
	'vehicleFuelTank': 'fuelTank',
	'vehicleRadio': 'radio',
	'vehicleTurret': 'turret',
	'vehicleGun': 'gun',
}


def _install_vehicle_module(vehInvID, moduleCompactDescr):
	invData = _get_inventory_data()
	if invData is None:
		return None
	vehData = invData.get(ITEM_TYPE_INDICES['vehicle'], {})
	compDescrs = vehData.get('compDescr', {})
	oldCompDescr = compDescrs.get(vehInvID)
	if not oldCompDescr:
		return None
	itemTypeIdx = _get_item_type_idx(moduleCompactDescr)
	if itemTypeIdx not in _VEHICLE_MODULE_TYPES:
		return None
	try:
		vehDescr = vehicles.VehicleDescr(compactDescr=oldCompDescr)
		# The module coming OFF has to go back to the depot. Without this it was
		# simply dropped - it read as sold, and only came back after switching views
		# because the tree rebuilds its list from the vehicle, not the inventory.
		# A turret swap can force the gun off too; that one returns as well, but
		# only when it actually changed.
		_old_cd = None
		_old_gun_cd = None
		try:
			for _tn, _ta in _MODULE_ATTR_BY_TYPE.items():
				if ITEM_TYPE_INDICES.get(_tn) == itemTypeIdx:
					_old_cd = (getattr(vehDescr, _ta, None) or {}).get('compactDescr')
					break
			if itemTypeIdx == ITEM_TYPE_INDICES.get('vehicleTurret'):
				_old_gun_cd = vehDescr.gun['compactDescr']
		except Exception:
			pass
		# Auto-upgrade suspension if needed and auto-mount compatible turret when installing a gun
		try:
			if vehDescr.type.chassis:
				_top_chs_cd = vehDescr.type.chassis[-1]['compactDescr']
				if itemTypeIdx != ITEM_TYPE_INDICES.get('vehicleChassis') and vehDescr.chassis['compactDescr'] != _top_chs_cd:
					try:
						vehDescr.installComponent(_top_chs_cd)
					except Exception:
						pass
		except Exception:
			pass
		if itemTypeIdx == ITEM_TYPE_INDICES.get('vehicleTurret'):
			gunCompDescr = vehDescr.gun['compactDescr']
			turretDescr = vehicles.getDictDescr(moduleCompactDescr)
			isCompatible = False
			for gun in turretDescr['guns']:
				if gun['compactDescr'] == gunCompDescr:
					isCompatible = True
					break
			if not isCompatible:
				gunCompDescr = turretDescr['guns'][0]['compactDescr']
			vehDescr.installTurret(moduleCompactDescr, gunCompDescr)
		elif itemTypeIdx == ITEM_TYPE_INDICES.get('vehicleGun'):
			_gun_fits_cur = any(g['compactDescr'] == moduleCompactDescr for g in vehDescr.turret.get('guns', ()))
			if _gun_fits_cur:
				vehDescr.installComponent(moduleCompactDescr)
			else:
				_mounted_with_turret = False
				for _t_list in getattr(vehDescr.type, 'turrets', ()):
					for _t_d in _t_list:
						if any(g['compactDescr'] == moduleCompactDescr for g in _t_d.get('guns', ())):
							vehDescr.installTurret(_t_d['compactDescr'], moduleCompactDescr)
							_mounted_with_turret = True
							break
					if _mounted_with_turret:
						break
				if not _mounted_with_turret:
					vehDescr.installComponent(moduleCompactDescr)
		else:
			vehDescr.installComponent(moduleCompactDescr)
		compDescrs[vehInvID] = vehDescr.makeCompactDescr()
		if itemTypeIdx in (
			ITEM_TYPE_INDICES.get('vehicleTurret'),
			ITEM_TYPE_INDICES.get('vehicleGun')
		):
			_set_vehicle_ammo_for_current_gun(vehData, vehInvID, vehDescr)
		_persist_vehicle(vehInvID)
		LOG_DEBUG('Fitting.install.ok', vehInvID, moduleCompactDescr, 'typeIdx', itemTypeIdx)
		_back = None
		try:
			if _old_cd and _old_cd != moduleCompactDescr:
				_back = _inventory_add_diff(_old_cd, 1)
			if _old_gun_cd and vehDescr.gun['compactDescr'] != _old_gun_cd:
				_back = _merge_diff(_back, _inventory_add_diff(_old_gun_cd, 1))
		except Exception:
			LOG_CURRENT_EXCEPTION()
		
		diff = {
			'inventory': {
				ITEM_TYPE_INDICES['vehicle']: {
					'compDescr': {vehInvID: compDescrs[vehInvID]},
					'shellsLayout': {vehInvID: vehData.get('shellsLayout', {}).get(vehInvID)},
					'shells': {vehInvID: vehData.get('shells', {}).get(vehInvID)}
				}
			}
		}
		return _merge_diff(_back, diff)
	except Exception:
		LOG_CURRENT_EXCEPTION()
	return None


def _equip_optional_device(vehInvID, deviceCompactDescr, slotIdx):
	vehData = _get_vehicle_data()
	if vehData is None:
		return None
	compDescrs = vehData.get('compDescr', {})
	oldCompDescr = compDescrs.get(vehInvID)
	if not oldCompDescr:
		return None
	try:
		vehDescr = vehicles.VehicleDescr(compactDescr=oldCompDescr)
		if deviceCompactDescr:
			vehDescr.installOptionalDevice(deviceCompactDescr, slotIdx)
		else:
			vehDescr.removeOptionalDevice(slotIdx)
		compDescrs[vehInvID] = vehDescr.makeCompactDescr()
		_persist_vehicle(vehInvID)
		LOG_DEBUG('Fitting.optDevice.ok', vehInvID, deviceCompactDescr, slotIdx)

		diff = {
			'inventory': {
				ITEM_TYPE_INDICES['vehicle']: {
					'compDescr': {vehInvID: compDescrs[vehInvID]},
					'eqs': {vehInvID: vehData.get('eqs', {}).get(vehInvID)}
				}
			}
		}
		return diff
	except Exception:
		LOG_CURRENT_EXCEPTION()
	return None


def _pair_layout(values):
	result = []
	idx = 0
	while idx + 1 < len(values):
		result.append((values[idx], values[idx + 1]))
		idx += 2
	return result


def _current_turret_gun_key(vehInvID):
	vehData = _get_vehicle_data()
	if vehData is None:
		return None
	compDescr = vehData.get('compDescr', {}).get(vehInvID)
	if not compDescr:
		return None
	try:
		vehDescr = vehicles.VehicleDescr(compactDescr=compDescr)
		return (
			vehicles.makeIntCompactDescrByID('vehicleTurret', *vehDescr.turret['id']),
			vehicles.makeIntCompactDescrByID('vehicleGun', *vehDescr.gun['id'])
		)
	except Exception:
		LOG_CURRENT_EXCEPTION()
	return None


def _set_shells(vehInvID, values):
	vehData = _get_vehicle_data()
	if vehData is None:
		return None
	if len(values) % 2:
		LOG_DEBUG('Fitting.shells.invalid_pair_count', vehInvID, len(values))
		return None
	if values:
		shellsLayout = list(values)
		shells = []
		for i in xrange(0, len(values), 2):
			shells.append(abs(values[i]))
			shells.append(values[i + 1])
		turretGun = _current_turret_gun_key(vehInvID)
		if turretGun is not None:
			vehData.setdefault('shellsLayout', {})[vehInvID] = {turretGun: shellsLayout}
		vehData.setdefault('shells', {})[vehInvID] = shells
	_persist_vehicle(vehInvID)
	LOG_DEBUG('Fitting.shells.ok', vehInvID, values)
	diff = {
		'inventory': {
			ITEM_TYPE_INDICES['vehicle']: {
				'shellsLayout': {vehInvID: vehData.get('shellsLayout', {}).get(vehInvID)},
				'shells': {vehInvID: vehData.get('shells', {}).get(vehInvID)}
			}
		}
	}
	return diff


def _set_equipments(vehInvID, values):
	vehData = _get_vehicle_data()
	if vehData is None:
		return False
	_norm_eqs = [abs(int(x or 0)) for x in (values or [])[:3]]
	while len(_norm_eqs) < 3:
		_norm_eqs.append(0)
	vehData.setdefault('eqs', {})[vehInvID] = list(_norm_eqs)
	vehData.setdefault('eqsLayout', {})[vehInvID] = list(_norm_eqs)
	_persist_vehicle(vehInvID)
	LOG_DEBUG('Fitting.eqs.ok', vehInvID, _norm_eqs)
	diff = {
		'inventory': {
			ITEM_TYPE_INDICES['vehicle']: {
				'eqs': {vehInvID: vehData.get('eqs', {}).get(vehInvID)},
				'eqsLayout': {vehInvID: vehData.get('eqsLayout', {}).get(vehInvID)}
			}
		}
	}
	return diff


def _set_layouts(arr):
	LOG_DEBUG('Fitting.set_layouts.arr', arr)
	# The array from doCmdIntArr for CMD_SET_AND_FILL_LAYOUTS (108) in stock 0.8.2 is:
	# [shopRev, vehInvID, shellsLen, shell0_cd, shell0_cnt, ..., eqsLen, eq0_cd, ...]
	if len(arr) < 2:
		return None
	vehData = _get_vehicle_data()
	if vehData is None:
		return None
	compDescrs = vehData.get('compDescr', {})
	if len(arr) >= 2 and arr[1] in compDescrs:
		vehInvID = arr[1]
		pos = 2
	elif arr[0] in compDescrs:
		vehInvID = arr[0]
		pos = 1
	elif arr[0] == 0 and len(arr) >= 2:
		vehInvID = arr[1]
		pos = 2
	elif len(arr) >= 2:
		vehInvID = arr[1]
		pos = 2
	else:
		vehInvID = arr[0]
		pos = 1

	shellsLen = arr[pos] if pos < len(arr) else 0
	pos += 1
	shellValues = list(arr[pos:pos + shellsLen])
	pos += shellsLen
	eqsLen = arr[pos] if pos < len(arr) else 0
	pos += 1
	eqsValues = list(arr[pos:pos + eqsLen])
	if len(shellValues) % 2:
		LOG_DEBUG('Fitting.layouts.invalid_shell_pair_count', vehInvID, len(shellValues))
		return None
	if shellValues:
		shellsLayout = list(shellValues)
		shells = []
		for i in xrange(0, len(shellValues), 2):
			shells.append(abs(shellValues[i]))
			shells.append(shellValues[i + 1])
		turretGun = _current_turret_gun_key(vehInvID)
		if turretGun is not None:
			vehData.setdefault('shellsLayout', {})[vehInvID] = {turretGun: shellsLayout}
		vehData.setdefault('shells', {})[vehInvID] = shells
	if eqsValues is not None:
		if len(eqsValues) == 6 or (len(eqsValues) % 2 == 0 and len(eqsValues) > 3):
			eqsList = [abs(int(eqsValues[i] or 0)) for i in xrange(0, min(len(eqsValues), 6), 2)]
		else:
			eqsList = [abs(int(x or 0)) for x in eqsValues[:3]]
		while len(eqsList) < 3:
			eqsList.append(0)
		vehData.setdefault('eqsLayout', {})[vehInvID] = list(eqsList)
		vehData.setdefault('eqs', {})[vehInvID] = list(eqsList)
	_persist_vehicle(vehInvID)
	LOG_DEBUG('Fitting.layouts.ok', vehInvID, shellValues, eqsValues)
	diff = {
		'inventory': {
			ITEM_TYPE_INDICES['vehicle']: {
				'shellsLayout': {vehInvID: vehData.get('shellsLayout', {}).get(vehInvID)},
				'eqsLayout': {vehInvID: vehData.get('eqsLayout', {}).get(vehInvID)},
				'eqs': {vehInvID: vehData.get('eqs', {}).get(vehInvID)},
				'shells': {vehInvID: vehData.get('shells', {}).get(vehInvID)}
			}
		}
	}
	return diff


def _change_vehicle_setting(vehInvID, setting, isOn):
	vehData = _get_vehicle_data()
	if vehData is None:
		return False
	settings = vehData.setdefault('settings', {})
	value = settings.get(vehInvID, 0)
	if isOn:
		value |= setting
	else:
		value &= ~setting
	settings[vehInvID] = value
	_persist_vehicle(vehInvID)
	LOG_DEBUG('Fitting.settings.ok', vehInvID, setting, isOn)
	return True


def handle_server_stats(fake_server, requestID, cmd, args):
	player = BigWorld.player()
	tnow = time.time()
	key = (requestID, cmd)
	lastKey = getattr(player, '_offhangar_stats_last_key', None)
	lastT = getattr(player, '_offhangar_stats_last_time', 0.0)
	dedupe = (
		getattr(player, 'isOffline', False)
		and key == lastKey
		and (tnow - lastT) < 0.3
	)
	if dedupe:
		LOG_DEBUG('Offline.stats dedupe skip receiveServerStats', requestID, cmd)
	else:
		try:
			BigWorld.player().receiveServerStats({
				'clusterCCU': 155000 * (1 - random.uniform(0.0, 0.15)),
				'regionCCU': 815000 * (1 - random.uniform(0.0, 0.15))
			})
		except Exception:
			LOG_CURRENT_EXCEPTION()
	player._offhangar_stats_last_key = key
	player._offhangar_stats_last_time = tnow

	# Removed heuristic501Burst to prevent auto-start
	return _success()


def handle_complete_tutorial(fake_server, requestID, cmd, args):
	if len(args) >= 2 and _install_vehicle_module(args[0], args[1]):
		return _success()
	return _success({})


def _purchase_item_label(compactDescr):
	try:
		itemTypeIdx = _get_item_type_idx(compactDescr)
		for name in ('shell', 'equipment', 'optionalDevice', 'vehicleChassis',
			'vehicleEngine', 'vehicleFuelTank', 'vehicleRadio',
			'vehicleTurret', 'vehicleGun'):
			if name in ITEM_TYPE_INDICES and itemTypeIdx == ITEM_TYPE_INDICES[name]:
				return name
		return 'typeIdx=%s' % itemTypeIdx
	except Exception:
		return 'unknown'


def handle_buy_item(fake_server, requestID, cmd, args):
	compactDescr = args[1] if len(args) > 1 else 0
	count = args[2] if len(args) > 2 else 1
	label = _purchase_item_label(compactDescr) if compactDescr else 'missing'
	LOG_DEBUG('PURCHASE.item.begin req=%s cmd=%s kind=%s count=%s' % (
		requestID, cmd, label, count))
	diff = None
	if compactDescr:
		diff = _inventory_add_diff(compactDescr, count)
		if diff:
			LOG_DEBUG('Shop.buyItem.ok', compactDescr, count)
		else:
			LOG_DEBUG('Shop.buyItem.skip', compactDescr, count)
	LOG_DEBUG('PURCHASE.item.dispatch_done req=%s kind=%s updated=%s' % (
		requestID, label, bool(diff)))
	if diff:
		return _success_with_update(diff)
	return _success()


def handle_buy_and_equip_item(fake_server, requestID, cmd, args):
	arr = args[0] if len(args) == 1 and isinstance(args[0], (list, tuple)) else args
	vehData = _get_vehicle_data()
	compDescrs = (vehData.get('compDescr', {}) if vehData else {})
	if len(arr) >= 3 and arr[2] in compDescrs:
		# Stock 0.8.2 format: [cacheRev, compDescr, vehInvID, slotIdx, isPaidRemoval, gunCompDescr]
		compactDescr = arr[1]
		vehInvID = arr[2]
		slotIdx = arr[3] if len(arr) > 3 else 0
	elif len(arr) >= 2 and arr[0] in compDescrs:
		vehInvID = arr[0]
		compactDescr = arr[1]
		slotIdx = arr[2] if len(arr) > 2 else 0
	elif len(arr) >= 3:
		compactDescr = arr[1]
		vehInvID = arr[2]
		slotIdx = arr[3] if len(arr) > 3 else 0
	elif len(arr) >= 2:
		vehInvID = arr[0]
		compactDescr = arr[1]
		slotIdx = 0
	else:
		LOG_DEBUG('PURCHASE.buy_equip.invalid req=%s cmd=%s argc=%s' % (
			requestID, cmd, len(arr)))
		return _success()

	label = _purchase_item_label(compactDescr) if compactDescr else 'missing'
	LOG_DEBUG('PURCHASE.buy_equip.begin req=%s cmd=%s kind=%s vehicle=%s' % (
		requestID, cmd, label, vehInvID))
	diff = None
	if compactDescr:
		# Buy (adds to inventory + its diff) AND equip, then merge both diffs
		# so the client learns the item is BOTH owned and installed.
		inv_diff = _inventory_add_diff(compactDescr, 1)
		itemTypeIdx = _get_item_type_idx(compactDescr)
		if itemTypeIdx == ITEM_TYPE_INDICES.get('optionalDevice'):
			equip_diff = _equip_optional_device(vehInvID, compactDescr, slotIdx)
		elif itemTypeIdx == ITEM_TYPE_INDICES.get('equipment'):
			eqs = list(vehData.get('eqs', {}).get(vehInvID, [0, 0, 0])) if vehData else [0, 0, 0]
			while len(eqs) < 3:
				eqs.append(0)
			if 0 <= slotIdx < 3:
				eqs[slotIdx] = compactDescr
			equip_diff = _set_equipments(vehInvID, eqs)
		else:
			equip_diff = _install_vehicle_module(vehInvID, compactDescr)
		diff = _merge_diff(inv_diff, equip_diff)
	LOG_DEBUG('PURCHASE.buy_equip.dispatch_done req=%s kind=%s updated=%s' % (
		requestID, label, bool(diff)))

	if diff:
		return _success_with_update(diff)
	return _success()


def handle_equip_module(fake_server, requestID, cmd, args):
	"""CMD_EQUIP (101): install a vehicle module already owned in inventory.
	Args: (vehInvID, moduleCompactDescr, 0)
	"""
	if len(args) >= 2:
		vehInvID = args[0]
		moduleCD = args[1]
		if moduleCD:
			diff = _install_vehicle_module(vehInvID, moduleCD)
			if not diff:
				return _unavailable('MODULE_EQUIP_FAILED')
			return _success_with_update(diff)
	return _unavailable('INVALID_MODULE_EQUIP')


def handle_equip_optional_device(fake_server, requestID, cmd, args):
	if len(args) >= 3:
		diff = _equip_optional_device(args[0], args[1], args[2])
		return _success_with_update(diff)
	return _success()


def handle_equip_shells(fake_server, requestID, cmd, args):
	arr = args[0] if len(args) == 1 and isinstance(args[0], (list, tuple)) else args
	LOG_DEBUG('PURCHASE.shells_equip.begin req=%s cmd=%s argc=%s' % (
		requestID, cmd, len(arr)))
	if len(arr) >= 2:
		diff = _set_shells(arr[0], list(arr[1:]))
		LOG_DEBUG('PURCHASE.shells_equip.dispatch_done req=%s updated=%s' % (
			requestID, bool(diff)))
		if diff is None:
			return _unavailable('INVALID_SHELL_LAYOUT')
		return _success_with_update(diff)
	LOG_DEBUG('PURCHASE.shells_equip.invalid req=%s argc=%s' % (requestID, len(arr)))
	return _success()


def handle_equip_equipments(fake_server, requestID, cmd, args):
	arr = args[0] if len(args) == 1 and isinstance(args[0], (list, tuple)) else args
	LOG_DEBUG('PURCHASE.consumables_equip.begin req=%s cmd=%s argc=%s' % (
		requestID, cmd, len(arr)))
	if len(arr) >= 2:
		diff = _set_equipments(arr[0], list(arr[1:]))
		LOG_DEBUG('PURCHASE.consumables_equip.dispatch_done req=%s updated=%s' % (
			requestID, bool(diff)))
		return _success_with_update(diff)
	LOG_DEBUG('PURCHASE.consumables_equip.invalid req=%s argc=%s' % (requestID, len(arr)))
	return _success()


def handle_set_and_fill_layouts(fake_server, requestID, cmd, args):
	arr = args[0] if len(args) == 1 and isinstance(args[0], (list, tuple)) else args
	diff = _set_layouts(list(arr))
	if diff is None or diff is False:
		return _unavailable('INVALID_FITTING_LAYOUT')
	return _success_with_update(diff, data={'shells': [], 'eqs': []})


def handle_vehicle_settings(fake_server, requestID, cmd, args):
	if len(args) >= 3:
		_change_vehicle_setting(args[0], args[1], args[2])
	return _success()


def handle_sync_data(fake_server, requestID, cmd, args):
	revision = args[0] if args else 0
	data = {'rev': revision + 2, 'prevRev': revision}
	data.update(getOfflineInventory())
	data.update(getOfflineStats())
	data.update(getOfflineQuestsProgress())
	normalize_offline_stats(data.get('stats', {}))
	# Cache the inventory so _get_inventory_data() can find it before the
	# Inventory object's internal cache is populated (0.8.2 offline path).
	try:
		player = BigWorld.player()
		if getattr(player, 'isOffline', False):
			player._offhangar_inventory = data.get('inventory')
			LOG_DEBUG('Fitting.inventoryCached', len(player._offhangar_inventory) if player._offhangar_inventory else 0)
	except Exception:
		pass
	# Force AccountSyncData to report as synchronized so Stats.get doesn't block.
	# This ensures eliteVehicles, slots etc. are available immediately in Hangar.
	try:
		player = BigWorld.player()
		if player is not None and hasattr(player, 'syncData'):
			syncData = player.syncData
			if not syncData._AccountSyncData__isSynchronized:
				syncData._AccountSyncData__isSynchronized = True
				# Fire all waiting subscribers with RES_CACHE result
				from AccountCommands import RES_CACHE
				subscribers = syncData._AccountSyncData__subscribers
				syncData._AccountSyncData__subscribers = []
				for cb in subscribers:
					try:
						cb(RES_CACHE)
					except Exception:
						pass
				LOG_DEBUG('Fitting.syncDataForced')
	except Exception:
		pass
	return _success(data)


from items import vehicles
import nations
nations_count = len(nations.NAMES)

def _get_customization_data():
	res = []
	try:
		for nationID in range(nations_count):
			cust = vehicles.g_cache.customization(nationID)
			res.append({
				'camouflages': dict((k, v.get('priceFactor', 1.0)) for k, v in cust.get('camouflages', {}).iteritems()),
				'inscriptions': {},
				'emblems': {},
				'horns': {}
			})
	except Exception:
		res = [{'camouflages': {}, 'inscriptions': {}, 'emblems': {}, 'horns': {}} for _ in range(nations_count)]
	return res

_customization_data = _get_customization_data()


def handle_sync_shop(fake_server, requestID, cmd, args):
	revision = args[0] if args else 0
	# Build minimal shop data the client needs.
	# slotsPrices: list of (goldCost, creditsCost) per slot purchase.
	# The Hangar reads price[1][0] where price = slotsPrices result, so it must be subscriptable.
	import nations
	nations_count = max(nations.INDICES.values()) + 2
	shop_data = {
		'rev': revision + 2,
		'prevRev': revision,
		'slotsPrices': [(3, 0), (3, 0), (3, 0), (3, 0), (3, 0)],
		'berthsPrices': [(3, 0), (3, 0)],
		'dailyXPFactor': 2,
		'exchangeRate': 400,
		'freeXPConversion': (25, 1),
		'freeXPToTManXPRate': 25,
		'goldPackets': [],
		'passportChangeCost': (0, 750000),
		'paidRemovalCost': (0, 10),
		'sellPriceModif': 0.5,
		'dropSkillsCost': [(0, 0), (0, 10000), (0, 20000), (1, 0)],
		'tankmanCost': [(0, 0), (0, 200000), (0, 400000), (50, 0)],
		'premiumCost': {},
		'isEnabledBuyingGoldShellsForCredits': True,
		'isEnabledBuyingGoldEqsForCredits': True,
		'tradeFees': {'selling': 0.5},
		'playerEmblemCost': {7: (50000, False), 30: (100000, False), 0: (100, True)},
		'playerInscriptionCost': {7: (50000, False), 30: (100000, False), 0: (100, True)},
		'hornCost': (50000, 100),
		'camouflageCost': {7: (50000, False), 30: (100000, False), 0: (100, True)},
		'customization': _customization_data,
		'playerEmblems': {'groups': {}},
		'items': getOfflineShopItems(),
		'ebank/vcoinExchangeRate': 0,
		'ebank/vcoinMaxTransactionValue': 0,
		'ebank/vcoinMinTransactionValue': 0,
	}
	BigWorld.callback(REQUEST_CALLBACK_TIME, _pack_stream(requestID, shop_data))
	return _stream()


def handle_sync_dossiers(fake_server, requestID, cmd, args):
	revision = args[0] if args else 0
	BigWorld.callback(REQUEST_CALLBACK_TIME, _pack_stream(requestID, (revision + 2, [])))
	return _stream()


def handle_set_language(fake_server, requestID, cmd, args):
	language = args[0] if args else 'ru'
	BigWorld.callback(REQUEST_CALLBACK_TIME, _pack_stream(requestID, language))
	return _stream()


def handle_enqueue_random(fake_server, requestID, cmd, args):
	cmdName = _ACCOUNT_CMD_INDEX.get(cmd, 'UNKNOWN_ENQUEUE')
	LOG_DEBUG('BattleStub.enqueue IGNORED', requestID, cmdName, cmd, args)
	schedule_random_battle_flow_after_enqueue(cmd, cmdName, args)
	return _success()


def handle_queue_info(fake_server, requestID, cmd, args):
	LOG_DEBUG('BattleStub.queueInfo', requestID, cmd, args)
	try:
		import BigWorld
		p = BigWorld.player()
		qinfo = None
		try:
			from gui.mods.offhangar.network_battle import queue_info_for_player
			qinfo = queue_info_for_player(p)
		except Exception:
			pass
		if qinfo is None:
			# Local mode retains the original synthetic queue population.
			qinfo = {
				'classes': [15, 42, 120, 5, 30],
				'levels': [0, 10, 20, 30, 40, 50, 60, 70, 150, 80, 100]
			}
		if p is not None and hasattr(p, 'receiveQueueInfo'):
			p.receiveQueueInfo(qinfo, {})
	except Exception:
		LOG_CURRENT_EXCEPTION()
	return _success()


def handle_stats_or_enqueue_collision(fake_server, requestID, cmd, args):
	"""
	AccountCommands index assigns 223/700 (ENQUEUE_RANDOM) but also 223 for SERVER_STATS
	in some builds. We distinguish by checking if args are mostly zeroes.
	"""
	int1 = args[0] if args else 0
	int2 = args[1] if len(args) > 1 else 0
	int3 = args[2] if len(args) > 2 else 0
	if int1 == 0 and int2 == 0 and int3 == 0:
		return handle_server_stats(fake_server, requestID, cmd, args)
	cmdName = _ACCOUNT_CMD_INDEX.get(cmd, 'UNKNOWN_ENQUEUE')
	LOG_DEBUG('BattleStub.enqueueSameIdAsStats IGNORED', requestID, cmdName, cmd, args)
	schedule_random_battle_flow_after_enqueue(cmd, cmdName, args)
	return _success()


def handle_prebattle(fake_server, requestID, cmd, args):
	LOG_DEBUG('BattleStub.prebattleOrQueue', requestID, cmd, args)
	return _success()


def handle_unknown(fake_server, requestID, cmd, args):
	# Fallback strategy for offline mode: do not break flow on unknown commands.
	LOG_DEBUG('CommandRouter.unknown', requestID, cmd, args)
	return _success()


def handle_tman_add_skill(fake_server, requestID, cmd, args):
	if len(args) >= 2:
		tmanInvID = args[0]
		skillName = args[1]
		diff = _tman_add_skill(tmanInvID, skillName)
		return _success_with_update(diff)
	return _success()

def _tman_add_skill(tmanInvID, skillName):
	invData = _get_inventory_data()
	if invData is None:
		return None
	tmanData = invData.get(ITEM_TYPE_INDICES['tankman'], {})
	compDescrs = tmanData.get('compDescr', {})
	try:
		tmanInvID = int(tmanInvID)
	except (ValueError, TypeError):
		pass
	if tmanInvID not in compDescrs:
		return None
	try:
		import items.tankmen
		if isinstance(skillName, (int, long)):
			skillName = items.tankmen.SKILL_NAMES[skillName]
		tDescr = items.tankmen.TankmanDescr(compactDescr=compDescrs[tmanInvID])
		tDescr.addXP(10000000)
		tDescr.addSkill(skillName)
		compDescrs[tmanInvID] = tDescr.makeCompactDescr()
		save_item_state(invData, ITEM_TYPE_INDICES['tankman'])
		LOG_DEBUG('Fitting.tmanAddSkill.ok', tmanInvID, skillName)
		diff = {'inventory': {ITEM_TYPE_INDICES['tankman']: {'compDescr': {tmanInvID: compDescrs[tmanInvID]}}}}
		return diff
	except Exception:
		LOG_CURRENT_EXCEPTION()
	return None

def handle_tman_drop_skills(fake_server, requestID, cmd, args):
	if len(args) >= 1:
		tmanInvID = args[0]
		diff = _tman_drop_skills(tmanInvID)
		return _success_with_update(diff)
	return _success()

def _tman_drop_skills(tmanInvID):
	invData = _get_inventory_data()
	if invData is None:
		return None
	tmanData = invData.get(ITEM_TYPE_INDICES['tankman'], {})
	compDescrs = tmanData.get('compDescr', {})
	try:
		tmanInvID = int(tmanInvID)
	except (ValueError, TypeError):
		pass
	if tmanInvID not in compDescrs:
		return None
	try:
		import items.tankmen
		tDescr = items.tankmen.TankmanDescr(compactDescr=compDescrs[tmanInvID])
		try:
			tDescr.dropSkills(0.0)
		except TypeError:
			tDescr.dropSkills()
		# Add back enough XP to let them re-select skills immediately
		tDescr.addXP(10000000)
		compDescrs[tmanInvID] = tDescr.makeCompactDescr()
		save_item_state(invData, ITEM_TYPE_INDICES['tankman'])
		LOG_DEBUG('Fitting.tmanDropSkills.ok', tmanInvID)
		diff = {'inventory': {ITEM_TYPE_INDICES['tankman']: {'compDescr': {tmanInvID: compDescrs[tmanInvID]}}}}
		return diff
	except Exception:
		LOG_CURRENT_EXCEPTION()
	return None

def configure_router(router):
	global _ACCOUNT_CMD_INDEX
	_ACCOUNT_CMD_INDEX = {}
	for name in dir(AccountCommands):
		if not name.startswith('CMD_'):
			continue
		value = getattr(AccountCommands, name, None)
		if isinstance(value, int):
			_ACCOUNT_CMD_INDEX[value] = name

	enqueueDebug = []
	for name in dir(AccountCommands):
		if not name.startswith('CMD_'):
			continue
		if 'ENQUEUE' not in name:
			continue
		val = getattr(AccountCommands, name, None)
		if isinstance(val, int):
			enqueueDebug.append((name, val))
	LOG_DEBUG('AccountCommands.CMD_ENQUEUE_*', enqueueDebug)

	cmd501Names = sorted(
		n for n in dir(AccountCommands)
		if n.startswith('CMD_') and getattr(AccountCommands, n, None) == 501
	)
	LOG_DEBUG('AccountCommands.cmdId_501', cmd501Names)
	fittingDebug = []
	for name in ('CMD_BUY_ITEM', 'CMD_BUY_AND_EQUIP_ITEM', 'CMD_EQUIP', 'CMD_EQUIP_OPTDEV', 'CMD_EQUIP_SHELLS', 'CMD_EQUIP_EQS', 'CMD_SET_AND_FILL_LAYOUTS', 'CMD_VEH_SETTINGS'):
		if hasattr(AccountCommands, name):
			fittingDebug.append((name, getattr(AccountCommands, name)))
	LOG_DEBUG('AccountCommands.fitting', fittingDebug)


	if CMD_ENQUEUE_RANDOM == CMD_REQ_SERVER_STATS:
		router.register(CMD_REQ_SERVER_STATS, handle_stats_or_enqueue_collision)
	else:
		if CMD_REQ_SERVER_STATS is not None:
			router.register(CMD_REQ_SERVER_STATS, handle_server_stats)
		if CMD_ENQUEUE_RANDOM is not None:
			router.register(CMD_ENQUEUE_RANDOM, handle_enqueue_random)

	CMD_ENQUEUE_TUTORIAL = getattr(AccountCommands, 'CMD_ENQUEUE_TUTORIAL', None)
	if CMD_ENQUEUE_TUTORIAL is not None:
		router.register(CMD_ENQUEUE_TUTORIAL, handle_enqueue_random)

	for _cmd, _handler in (
		(CMD_COMPLETE_TUTORIAL, handle_complete_tutorial),
		(CMD_SYNC_DATA, handle_sync_data),
		(CMD_SYNC_SHOP, handle_sync_shop),
		(CMD_SYNC_DOSSIERS, handle_sync_dossiers),
		(CMD_SET_LANGUAGE, handle_set_language),
		(CMD_BUY_ITEM, handle_buy_item),
		(CMD_BUY_AND_EQUIP_ITEM, handle_buy_and_equip_item),
		(CMD_EQUIP, handle_equip_module),
		(CMD_EQUIP_OPTDEV, handle_equip_optional_device),
		(CMD_EQUIP_SHELLS, handle_equip_shells),
		(CMD_EQUIP_EQS, handle_equip_equipments),
		(CMD_SET_AND_FILL_LAYOUTS, handle_set_and_fill_layouts),
		(CMD_VEH_SETTINGS, handle_vehicle_settings)
	):
		if _cmd is not None:
			router.register(_cmd, _handler)
	# Battle flow + possible follow-up commands.
	# Real money commands of THIS client, resolved by name so a future client cannot
	# shift them. Registered BEFORE the prebattle ones: the router keeps the first
	# registration for an id (command_router.py:24-27).
	if _offh_deny_purchase_cfg():
		for _pname in ('CMD_EXCHANGE', 'CMD_FREE_XP_CONV', 'CMD_PREMIUM', 'CMD_BUY_SLOT',
				'CMD_BUY_BERTHS'):
			_pid = getattr(AccountCommands, _pname, None)
			if _pid is None:
				continue
			router.register(_pid, handle_unavailable)
			LOG_DEBUG('CommandRouter.deny', _pname, _pid)
	if CMD_PREBATTLE_ACTION is not None:
		router.register(CMD_PREBATTLE_ACTION, handle_prebattle)
	if CMD_ARENA_LIST is not None:
		router.register(CMD_ARENA_LIST, handle_prebattle)
	for _cmd, _handler in (
		(CMD_REQ_QUEUE_INFO, handle_queue_info),
		(CMD_TMAN_ADD_SKILL, handle_tman_add_skill),
		(CMD_TMAN_DROP_SKILLS, handle_tman_drop_skills),
		(CMD_VEH_CAMOUFLAGE, handle_customization),
		(CMD_VEH_HORN, handle_customization),
		(CMD_VEH_EMBLEM, handle_customization),
		(CMD_VEH_INSCRIPTION, handle_customization)
	):
		if _cmd is not None:
			router.register(_cmd, _handler)

	router.set_fallback(handle_unknown)


def _debug_player_methods():
    import BigWorld
    p = BigWorld.player()
    from gui.mods.offhangar.logging import LOG_DEBUG
    LOG_DEBUG('Player methods:', dir(p))

from gui.Scaleform.TechnicalMaintenance import TechnicalMaintenance
import adisp
