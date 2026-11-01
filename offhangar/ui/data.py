# -*- coding: utf-8 -*-
from __future__ import absolute_import
import functools
import time
from itertools import cycle

import items
import nations
from AccountCommands import VEHICLE_SETTINGS_FLAG
try:
	from constants import ACCOUNT_ATTR
except (ImportError, AttributeError):
	import sys
	_c_mod = sys.modules.get('constants')
	if _c_mod is not None and hasattr(_c_mod, 'ACCOUNT_ATTR'):
		ACCOUNT_ATTR = _c_mod.ACCOUNT_ATTR
	else:
		import imp
		try:
			_cm = imp.load_source('wot_common_constants', r'D:\Games\World_of_Tanks\res\scripts\common\constants.pyc')
			ACCOUNT_ATTR = _cm.ACCOUNT_ATTR
		except Exception:
			class ACCOUNT_ATTR(object):
				RANDOM_BATTLES = 1
				SANDBOX = 2
from items import vehicles, ITEM_TYPE_INDICES, _xml
from nations import AVAILABLE_NAMES, INDICES

from gui.mods.offhangar.logging import *
from gui.mods.offhangar.state import apply_state_to_inventory
from gui.mods.offhangar.utils import *
from gui.mods.offhangar._constants import *

items.init(True)
vehicles.init(True)
from items.vehicles import g_list, g_cache # noqa: E402

doLog = functools.partial(doLog, 'OFFHANGAR')
LOG_NOTE = functools.partial(doLog, '[NOTE]')
LOG_DEBUG = functools.partial(doLog, '[DEBUG]')

itemTypeNameMap = {
	'guns':    ('vehicleGun', g_cache.guns),
	'turrets': ('vehicleTurret', g_cache.turrets),
	'engines': ('vehicleEngine', g_cache.engines),
	'chassis': ('vehicleChassis', g_cache.chassis),
	'radios':  ('vehicleRadio', g_cache.radios),
	'shells':  ('shell', g_cache.shells)
}

commonItemTypeNameMap = {'optional_devices': ('optionalDevice', g_cache.optionalDevices), 'equipments': ('equipment', g_cache.equipments)}

_OFFLINE_SHOP_ITEMS_CACHE = [None]

def _clone_shop_items(src, for_sync=False):
	# Stock Shop.__onSyncComplete expects 4-tuples (credits, gold, camoFactor, hornFactor)
	# and mutates vehPrices[inNationIdx] in-place into 2-tuples (credits, gold).
	# Direct assignments to Shop._Shop__cache (via getOfflineShopCacheData) bypass
	# __onSyncComplete and therefore MUST receive 2-tuples (credits, gold) so
	# ShopRequester / Vehicle price unpacking never raises ValueError!
	out = {}
	veh_type_idx = ITEM_TYPE_INDICES['vehicle']
	for n_idx, n_map in src.iteritems():
		out[n_idx] = {}
		for t_idx, pair in n_map.iteritems():
			d_copy = dict(pair[0])
			if not for_sync and t_idx == veh_type_idx:
				for k_id, v_tup in list(d_copy.items()):
					if isinstance(v_tup, tuple) and len(v_tup) > 2:
						d_copy[k_id] = (v_tup[0], v_tup[1])
			out[n_idx][t_idx] = (d_copy, set(pair[1]))
	return out

def getOfflineShopItems(for_sync=False):
	if _OFFLINE_SHOP_ITEMS_CACHE[0] is not None:
		return _clone_shop_items(_OFFLINE_SHOP_ITEMS_CACHE[0], for_sync=for_sync)
	shopItems = {nations.NONE_INDEX: {ITEM_TYPE_INDICES['optionalDevice']: ({}, set()), ITEM_TYPE_INDICES['equipment']: ({}, set())}}
	
	for nationIdx in INDICES.values():
		shopItems[nationIdx] = dict((itemType, ({}, set())) for itemType in items.SIMPLE_ITEM_TYPE_INDICES)
		shopItems[nationIdx][ITEM_TYPE_INDICES['vehicle']] = ({}, set())
		
		# Vehicles
		listXmlPath = vehicles._VEHICLE_TYPE_XML_PATH + AVAILABLE_NAMES[nationIdx] + '/list.xml'

		listSection = ResMgr.openSection(listXmlPath)
		turretsIDs_section = ResMgr.openSection(vehicles._VEHICLE_TYPE_XML_PATH + AVAILABLE_NAMES[nationIdx] + '/components/turrets.xml')['ids']
		chassisIDs_section = ResMgr.openSection(vehicles._VEHICLE_TYPE_XML_PATH + AVAILABLE_NAMES[nationIdx] + '/components/chassis.xml')['ids']

		for vname, vsection in listSection.items():
			ctx = (None, listXmlPath + '/' + vname)
			price = _xml.readPrice(ctx, vsection, 'price')

			# Read additional price data
			xmlVehPath = vehicles._VEHICLE_TYPE_XML_PATH + AVAILABLE_NAMES[nationIdx] + '/' + vname + '.xml'
			vehSec = ResMgr.openSection(xmlVehPath)

			# Read turrets and chassis
			for turretName, turretSection in vehSec['turrets0'].items():
				itemData = itemTypeNameMap['turrets']

				turretID = _xml.readInt(ctx, turretsIDs_section, turretName)
				turretPrice = _xml.readPrice(ctx, turretSection, 'price')
				turret = itemData[1](nationIdx)[turretID]

				shopItems[nationIdx][ITEM_TYPE_INDICES[itemData[0]]][0][turret['compactDescr']] = turretPrice

			for chassisName, chassisSection in vehSec['chassis'].items():
				itemData = itemTypeNameMap['chassis']

				chassisID = _xml.readInt(ctx, chassisIDs_section, chassisName)
				chassisPrice = _xml.readPrice(ctx, chassisSection, 'price')
				chassis = itemData[1](nationIdx)[chassisID]

				shopItems[nationIdx][ITEM_TYPE_INDICES[itemData[0]]][0][chassis['compactDescr']] = chassisPrice
			
			priceFactorCamo = vehSec.readFloat('camouflage/priceFactor')
			hornPriceFactor = vehSec.readFloat('horns/priceFactor')
			ResMgr.purge(xmlVehPath, True)

			id = _xml.readInt(ctx, vsection, 'id', 0, 255)

			shopItems[nationIdx][ITEM_TYPE_INDICES['vehicle']][0][id] = (price[0], price[1], priceFactorCamo, hornPriceFactor)
			

		# Modules
		for itemTypeName, itemData in itemTypeNameMap.items():

			xmlPath = vehicles._VEHICLE_TYPE_XML_PATH + AVAILABLE_NAMES[nationIdx] + '/components/' + itemTypeName + '.xml'
			section = ResMgr.openSection(xmlPath)
			moduleIDs_section = section['ids']
			modules_section = section['shared']

			if itemTypeName == 'shells':
				for moduleName, moduleSection in section.items():
					if moduleName != 'icons':
						try:
							ctx = (None, xmlPath + '/' + moduleName)
							id = _xml.readInt(ctx, moduleSection, 'id', 0, 255)
							price = _xml.readPrice(ctx, moduleSection, 'price')
							module = g_cache.shells(nationIdx)[id]

							shopItems[nationIdx][ITEM_TYPE_INDICES['shell']][0][module['compactDescr']] = (price[0], price[1], 0, 0)
							
						except Exception:
							pass
			else:
				for moduleName, moduleSection in modules_section.items():
					ctx = (None, xmlPath + '/' + moduleName)
					id = _xml.readInt(ctx, moduleIDs_section, moduleName)
					price = _xml.readPrice(ctx, moduleSection, 'price')
					module = itemData[1](nationIdx)[id]

					shopItems[nationIdx][ITEM_TYPE_INDICES[itemData[0]]][0][module['compactDescr']] = price
					

		# Common (OptDevices, Equipment)
		for commonItemTypeName, commonItemData in commonItemTypeNameMap.items():

			xmlPath = vehicles._VEHICLE_TYPE_XML_PATH + 'common/' + commonItemTypeName + '.xml'
			section = ResMgr.openSection(xmlPath)

			for oDname, oDsection in section.items():
				ctx = (None, xmlPath + '/' + oDname)
				id = _xml.readInt(ctx, oDsection, 'id')
				price = _xml.readPrice(ctx, oDsection, 'price')
				device = commonItemData[1]()[id]
				
				shopItems[nations.NONE_INDEX][ITEM_TYPE_INDICES[commonItemData[0]]][0][device.compactDescr] = price
				
			
		ResMgr.purge(xmlPath, True)

	_OFFLINE_SHOP_ITEMS_CACHE[0] = shopItems
	return _clone_shop_items(shopItems, for_sync=for_sync)

def getOfflineShopCacheData(revision=2, for_sync=False):
	raw_items = getOfflineShopItems(for_sync=True)
	camo_factors = {}
	horn_factors = dict((n_id, {}) for n_id in INDICES.values())
	veh_idx = ITEM_TYPE_INDICES['vehicle']
	for n_id, n_data in raw_items.iteritems():
		if veh_idx in n_data:
			cf_map = {}
			hf_map = horn_factors.setdefault(n_id, {})
			for v_id, p_tup in n_data[veh_idx][0].iteritems():
				if isinstance(p_tup, tuple) and len(p_tup) >= 4:
					cf_map[v_id] = p_tup[2]
					hf_map[v_id] = p_tup[3]
			camo_factors[n_id] = cf_map
	return {
		'rev': revision,
		'prevRev': 0,
		'berthsPrices': (16, 16, [300]),
		'freeXPConversion': (25, 1),
		'dropSkillsCost': {
			0: {'xpReuseFraction': 0.5, 'gold': 0, 'credits': 0},
			1: {'xpReuseFraction': 0.75, 'gold': 0, 'credits': 20000},
			2: {'xpReuseFraction': 1.0, 'gold': 200, 'credits': 0}
		},
		'refSystem': {
			'maxNumberOfReferrals': 50,
			'posByXPinTeam': 10,
			'maxReferralXPPool': 350000,
			'periods': [(24, 3.0), (168, 2.0), (876000, 1.5)]
		},
		'playerEmblemCost': {
			0: (15, True),
			30: (6000, False),
			7: (1500, False)
		},
		'premiumCost': {
			1: 250,
			3: 650,
			7: 1250,
			30: 2500,
			180: 13500,
			360: 24000
		},
		'winXPFactorMode': 0,
		'sellPriceModif': 0.5,
		'passportChangeCost': 50,
		'exchangeRateForShellsAndEqs': 400,
		'exchangeRate': 400,
		'tankmanCost': ({
			'isPremium': False,
			'baseRoleLoss': 0.20000000298023224,
			'gold': 0,
			'credits': 0,
			'classChangeRoleLoss': 0.20000000298023224,
			'roleLevel': 50
		},
		{
			'isPremium': False,
			'baseRoleLoss': 0.10000000149011612,
			'gold': 0,
			'credits': 20000,
			'classChangeRoleLoss': 0.10000000149011612,
			'roleLevel': 75
		},
		{
			'isPremium': True,
			'baseRoleLoss': 0.0,
			'gold': 200,
			'credits': 0,
			'classChangeRoleLoss': 0.0,
			'roleLevel': 100
		}),
		'paidRemovalCost': 10,
		'dailyXPFactor': 2,
		'changeRoleCost': 500,
		'items': getOfflineShopItems(for_sync=for_sync),
		'camoPriceFactors': camo_factors,
		'hornPriceFactors': horn_factors,
		'hornCost': (1, 1),
		'camouflageCost': {},
		'customization': dict((nation, {'camouflages': {}}) for nation in INDICES.values()),
		'isEnabledBuyingGoldShellsForCredits': True,
		'slotsPrices': (9, [300]),
		'freeXPToTManXPRate': 10,
		'sellPriceFactor': 0.5,
		'isEnabledBuyingGoldEqsForCredits': True,
		'playerInscriptionCost': {
			0: (15, True),
			7: (1500, False),
			30: (6000, False),
			'nations': {}
		}
	}

def getOfflineInventory():

	import items
	import items.tankmen
	data = dict((idx, {}) for idx in ITEM_TYPE_INDICES.values())
	i = 1
	i_crew = 1
	compDescr = {}
	data[ITEM_TYPE_INDICES['vehicle']] = {
		'repair': {},
		'lastCrew': {},
		'crew': {},
		'settings': {},
		'compDescr': {},
		'eqs': {},
		'eqsLayout': {},
		'shells': {},
		'customizationExpiryTime': {},
		'lock': {},
		'shellsLayout': {}
	}

	data[ITEM_TYPE_INDICES['tankman']] = {
		'vehicle': {},
		'compDescr': {}
	}

	for key, value in g_list._VehicleList__ids.items():
		if 'Type59_Gold' in key:
			continue
		vehicle = vehicles.VehicleDescr(typeID=value)
		compDescr[i] = vehicle.makeCompactDescr()
		turretGun = (vehicles.makeIntCompactDescrByID('vehicleTurret', *vehicle.turrets[0][0]['id']), vehicles.makeIntCompactDescrByID('vehicleGun', *vehicle.turrets[0][0]['guns'][0]['id']))

		tmanList = items.tankmen.generateTankmen(value[0], value[1], vehicle.type.crewRoles, False, items.tankmen.MAX_SKILL_LEVEL, [])
		tmanListCycle = cycle(tmanList)

		data[ITEM_TYPE_INDICES['vehicle']]['crew'].update({i: [tmanID for tmanID in xrange(i_crew, len(tmanList) + i_crew)]})
		data[ITEM_TYPE_INDICES['vehicle']]['settings'].update({i: VEHICLE_SETTINGS_FLAG.AUTO_REPAIR | VEHICLE_SETTINGS_FLAG.AUTO_LOAD})
		data[ITEM_TYPE_INDICES['vehicle']]['compDescr'].update(compDescr)
		data[ITEM_TYPE_INDICES['vehicle']]['eqs'].update({i: [0, 0, 0]})
		data[ITEM_TYPE_INDICES['vehicle']]['eqsLayout'].update({i: [0, 0, 0]})
		data[ITEM_TYPE_INDICES['vehicle']]['shells'].update({i: vehicles.getDefaultAmmoForGun(vehicle.turrets[0][0]['guns'][0])})
		data[ITEM_TYPE_INDICES['vehicle']]['shellsLayout'].update({i: {turretGun: vehicles.getDefaultAmmoForGun(vehicle.turrets[0][0]['guns'][0])}})

		for tmanID in xrange(i_crew, len(tmanList) + i_crew):
			tDescr = items.tankmen.TankmanDescr(compactDescr=next(tmanListCycle))
			tDescr.addXP(10000000)
			data[ITEM_TYPE_INDICES['tankman']]['vehicle'][tmanID] = i
			data[ITEM_TYPE_INDICES['tankman']]['compDescr'][tmanID] = tDescr.makeCompactDescr()
			i_crew += 1

		i += 1

	try:
		for _nid in nations.INDICES.values():
			for _sid in g_cache.shells(_nid).keys():
				_scd = vehicles.makeIntCompactDescrByID('shell', _nid, _sid)
				data[ITEM_TYPE_INDICES['shell']][_scd] = max(int(data[ITEM_TYPE_INDICES['shell']].get(_scd, 0) or 0), 1000)
		for _eid in g_cache.equipments().keys():
			_ecd = vehicles.makeIntCompactDescrByID('equipment', nations.NONE_INDEX, _eid)
			data[ITEM_TYPE_INDICES['equipment']][_ecd] = max(int(data[ITEM_TYPE_INDICES['equipment']].get(_ecd, 0) or 0), 100)
		for _oid in g_cache.optionalDevices().keys():
			_ocd = vehicles.makeIntCompactDescrByID('optionalDevice', nations.NONE_INDEX, _oid)
			data[ITEM_TYPE_INDICES['optionalDevice']][_ocd] = max(int(data[ITEM_TYPE_INDICES['optionalDevice']].get(_ocd, 0) or 0), 100)
	except Exception:
		pass
	apply_state_to_inventory(data)
	return {'inventory': data}

def getOfflineStats():
	unlocksSet = set()
	vehiclesSet = set()

	for nationID in nations.INDICES.values():
		unlocksSet.update([vehicles.makeIntCompactDescrByID('optionalDevice', nationID, i) for i in g_cache.optionalDevices().keys()])
		unlocksSet.update([vehicles.makeIntCompactDescrByID('equipment', nationID, i) for i in g_cache.equipments().keys()])
		unlocksSet.update([vehicles.makeIntCompactDescrByID('vehicleChassis', nationID, i) for i in g_cache.chassis(nationID).keys()])
		unlocksSet.update([vehicles.makeIntCompactDescrByID('vehicleEngine', nationID, i) for i in g_cache.engines(nationID).keys()])
		unlocksSet.update([vehicles.makeIntCompactDescrByID('vehicleFuelTank', nationID, i) for i in g_cache.fuelTanks(nationID).keys()])
		unlocksSet.update([vehicles.makeIntCompactDescrByID('vehicleRadio', nationID, i) for i in g_cache.radios(nationID).keys()])
		unlocksSet.update([vehicles.makeIntCompactDescrByID('vehicleTurret', nationID, i) for i in g_cache.turrets(nationID).keys()])
		unlocksSet.update([vehicles.makeIntCompactDescrByID('vehicleGun', nationID, i) for i in g_cache.guns(nationID).keys()])
		unlocksSet.update([vehicles.makeIntCompactDescrByID('shell', nationID, i) for i in g_cache.shells(nationID).keys()])

		vData = [vehicles.makeIntCompactDescrByID('vehicle', nationID, i) for i in g_list.getList(nationID).keys()]
		unlocksSet.update(vData)
		vehiclesSet.update(vData)

	attrs = 0
	for field in dir(ACCOUNT_ATTR):
		value = getattr(ACCOUNT_ATTR, field, None)
		if isinstance(value, (int, long)):
			attrs |= value

	vehTypeXP = dict([(i, 0) for i in vehiclesSet])

	return {
		'account': {
			'autoBanTime': 0,
			'attrs': attrs,
			'clanDBID': 0,
			'premiumExpiryTime': time.time() + 86400,
			'credits': 100000000,
			'gold': 1000000,
			'slots': 2000,
			'berths': 40,
			'freeXP': 100000000,
			'dossier': '',
			'clanInfo': ('', '', 0, 0, 0),
			'accOnline': 0,
			'accOffline': 0,
			'freeTMenLeft': 0,
			'freeVehiclesLeft': 0,
			'vehicleSellsLeft': 0,
			'captchaTriesLeft': 0,
			'hasFinPassword': True,
			'finPswdAttemptsLeft': 0,
			'tkillIsSuspected': False,
			'denunciationsLeft': 0,
			'tutorialsCompleted': 33553532,
			'battlesTillCaptcha': 99,
			'dailyPlayHours': [0],
			'playLimits': ((0, ''), (0, '')),
		},
		'stats': {
			'credits': 100000000,
			'gold': 1000000,
			'vehTypeXP': vehTypeXP,
			'vehTypeLocks': {},
			'restrictions': {},
			'globalVehicleLocks': {},
			'unlocks': unlocksSet,
			'eliteVehicles': vehiclesSet,
			'multipliedXPVehs': unlocksSet,
			'oldVehInvID': 0,
			'SPA': {'/common/goldfish_bonus_applied/': u'1'},
			'globalRating': 0,
			'fortResource': 0,
			'unitAcceptDeadline': 0,
		},
		'cache': {
			'isFinPswdVerified': True,
		},
		'economics': {
		}
	}

def getOfflineQuestsProgress():
	return {'quests': {}}


import json as _offh_stats_json

_PERSIST_STATS_CACHE = None

def _get_persist_stats_path():
    try:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        user_dir = os.path.join(base_dir, 'offhangar_user')
        if not os.path.isdir(user_dir):
            os.makedirs(user_dir)
        return os.path.join(user_dir, 'account_stats.json')
    except Exception:
        return 'offhangar_account_stats.json'

def _load_persistent_account_stats():
    global _PERSIST_STATS_CACHE
    if _PERSIST_STATS_CACHE is not None:
        return _PERSIST_STATS_CACHE
    path = _get_persist_stats_path()
    try:
        if os.path.isfile(path):
            with open(path, 'r') as f:
                data = _offh_stats_json.load(f)
                if isinstance(data, dict):
                    _PERSIST_STATS_CACHE = data
                    return _PERSIST_STATS_CACHE
    except Exception:
        pass
    _PERSIST_STATS_CACHE = {
        'account': {
            'battlesCount': 0, 'wins': 0, 'losses': 0, 'survivedBattles': 0,
            'xp': 0, 'maxXP': 0, 'frags': 0, 'maxFrags': 0,
            'shots': 0, 'hits': 0, 'damageDealt': 0, 'damageReceived': 0,
            'spotted': 0, 'capturePoints': 0, 'droppedCapturePoints': 0,
            'treesCut': 0, 'achievements': {},
        },
        'vehicles': {}
    }
    return _PERSIST_STATS_CACHE

def record_persistent_battle_outcome(veh_cd, is_win, survived, xp, frags=0, shots=0, hits=0, dmg_dealt=0, dmg_recv=0, spotted=0, cap=0, decap=0, medals_list=None, mark_of_mastery=0, is_draw=False, **kwargs):
    global _PERSIST_STATS_CACHE
    if 'damage_dealt' in kwargs and not dmg_dealt:
        dmg_dealt = kwargs['damage_dealt']
    if 'damage_received' in kwargs and not dmg_recv:
        dmg_recv = kwargs['damage_received']
    if 'capture_points' in kwargs and not cap:
        cap = kwargs['capture_points']
    if 'dropped_capture_points' in kwargs and not decap:
        decap = kwargs['dropped_capture_points']
    if 'achievements' in kwargs and medals_list is None:
        medals_list = kwargs['achievements']
    st = _load_persistent_account_stats()
    _now_ts = int(time.time())
    acc = st.setdefault('account', {})
    acc['battlesCount'] = int(acc.get('battlesCount', 0)) + 1
    acc['lastBattleTime'] = _now_ts
    acc['mileage'] = int(acc.get('mileage', 0)) + int(kwargs.get('mileage', 0) or 0)
    acc['battleLifeTime'] = int(acc.get('battleLifeTime', 0)) + int(kwargs.get('life_time', 0) or 0)
    if is_win:
        acc['wins'] = int(acc.get('wins', 0)) + 1
    elif not is_draw:
        acc['losses'] = int(acc.get('losses', 0)) + 1
    if survived:
        acc['survivedBattles'] = int(acc.get('survivedBattles', 0)) + 1
    acc['xp'] = int(acc.get('xp', 0)) + int(xp)
    if int(xp) > int(acc.get('maxXP', 0)):
        acc['maxXP'] = int(xp)
        acc['maxXPVehicle'] = int(veh_cd or 0)
    acc['frags'] = int(acc.get('frags', 0)) + int(frags)
    if int(frags) > int(acc.get('maxFrags', 0)):
        acc['maxFrags'] = int(frags)
        acc['maxFragsVehicle'] = int(veh_cd or 0)
    acc['shots'] = int(acc.get('shots', 0)) + int(shots)
    acc['hits'] = int(acc.get('hits', 0)) + int(hits)
    acc['damageDealt'] = int(acc.get('damageDealt', 0)) + int(dmg_dealt)
    acc['damageReceived'] = int(acc.get('damageReceived', 0)) + int(dmg_recv)
    acc['spotted'] = int(acc.get('spotted', 0)) + int(spotted)
    acc['capturePoints'] = int(acc.get('capturePoints', 0)) + int(cap)
    acc['droppedCapturePoints'] = int(acc.get('droppedCapturePoints', 0)) + int(decap)
    ach = acc.setdefault('achievements', {})
    for m_id in (medals_list or []):
        k = str(int(m_id))
        ach[k] = int(ach.get(k, 0)) + 1

    vkey = str(int(veh_cd or 0))
    vehs = st.setdefault('vehicles', {})
    v = vehs.setdefault(vkey, {
        'battlesCount': 0, 'wins': 0, 'losses': 0, 'survivedBattles': 0,
        'xp': 0, 'maxXP': 0, 'frags': 0, 'maxFrags': 0,
        'shots': 0, 'hits': 0, 'damageDealt': 0, 'damageReceived': 0,
        'spotted': 0, 'capturePoints': 0, 'droppedCapturePoints': 0,
        'markOfMastery': 0, 'achievements': {}
    })
    v['battlesCount'] = int(v.get('battlesCount', 0)) + 1
    v['lastBattleTime'] = _now_ts
    v['mileage'] = int(v.get('mileage', 0)) + int(kwargs.get('mileage', 0) or 0)
    v['battleLifeTime'] = int(v.get('battleLifeTime', 0)) + int(kwargs.get('life_time', 0) or 0)
    if is_win:
        v['wins'] = int(v.get('wins', 0)) + 1
    elif not is_draw:
        v['losses'] = int(v.get('losses', 0)) + 1
    if survived:
        v['survivedBattles'] = int(v.get('survivedBattles', 0)) + 1
    v['xp'] = int(v.get('xp', 0)) + int(xp)
    if int(xp) > int(v.get('maxXP', 0)):
        v['maxXP'] = int(xp)
    v['frags'] = int(v.get('frags', 0)) + int(frags)
    if int(frags) > int(v.get('maxFrags', 0)):
        v['maxFrags'] = int(frags)
    v['shots'] = int(v.get('shots', 0)) + int(shots)
    v['hits'] = int(v.get('hits', 0)) + int(hits)
    v['damageDealt'] = int(v.get('damageDealt', 0)) + int(dmg_dealt)
    v['damageReceived'] = int(v.get('damageReceived', 0)) + int(dmg_recv)
    v['spotted'] = int(v.get('spotted', 0)) + int(spotted)
    v['capturePoints'] = int(v.get('capturePoints', 0)) + int(cap)
    v['droppedCapturePoints'] = int(v.get('droppedCapturePoints', 0)) + int(decap)
    if int(mark_of_mastery or 0) > int(v.get('markOfMastery', 0)):
        v['markOfMastery'] = int(mark_of_mastery)
    vach = v.setdefault('achievements', {})
    for m_id in (medals_list or []):
        k = str(int(m_id))
        vach[k] = int(vach.get(k, 0)) + 1

    try:
        with open(_get_persist_stats_path(), 'w') as f:
            _offh_stats_json.dump(st, f, indent=2)
    except Exception:
        pass
    return st


def populate_account_dossier_descr(d):
    """Populate a WoT 0.8.2 AccountDossierDescr in-place from persistent offline stats."""
    try:
        st = _load_persistent_account_stats()
        acc = st.get('account', {})
        vehs = st.get('vehicles', {})
        b_cnt = int(acc.get('battlesCount', 0))
        if b_cnt <= 0:
            return d
        try:
            tot = d['total']
            tot['battlesCount'] = b_cnt
            tot['lastBattleTime'] = int(acc.get('lastBattleTime', int(time.time())))
            tot['treesCut'] = int(acc.get('treesCut', 0))
            tot['mileage'] = int(acc.get('mileage', 0))
            tot['battleLifeTime'] = int(acc.get('battleLifeTime', 0))
        except Exception:
            pass
        try:
            a15 = d['a15x15']
            a15['battlesCount'] = b_cnt
            a15['wins'] = int(acc.get('wins', 0))
            a15['losses'] = int(acc.get('losses', 0))
            a15['survivedBattles'] = int(acc.get('survivedBattles', 0))
            a15['xp'] = int(acc.get('xp', 0))
            a15['frags'] = int(acc.get('frags', 0))
            a15['shots'] = int(acc.get('shots', 0))
            a15['hits'] = int(acc.get('hits', 0))
            a15['damageDealt'] = int(acc.get('damageDealt', 0))
            a15['damageReceived'] = int(acc.get('damageReceived', 0))
            a15['spotted'] = int(acc.get('spotted', 0))
            a15['capturePoints'] = int(acc.get('capturePoints', 0))
            a15['droppedCapturePoints'] = int(acc.get('droppedCapturePoints', 0))
        except Exception:
            pass
        try:
            m15 = d['max15x15']
            m15['maxXP'] = int(acc.get('maxXP', 0))
            if acc.get('maxXPVehicle'):
                m15['maxXPVehicle'] = int(acc.get('maxXPVehicle', 0))
            m15['maxFrags'] = int(acc.get('maxFrags', 0))
            if acc.get('maxFragsVehicle'):
                m15['maxFragsVehicle'] = int(acc.get('maxFragsVehicle', 0))
        except Exception:
            pass
        try:
            cut = d['a15x15Cut']
            for vk_str, vinfo in vehs.items():
                try:
                    vcd = int(vk_str)
                    vb = int(vinfo.get('battlesCount', 0))
                    if vcd <= 0 or vb <= 0:
                        continue
                    vw = int(vinfo.get('wins', 0))
                    vmom = int(vinfo.get('markOfMastery', 0))
                    vxp = int(vinfo.get('xp', 0))
                    # Test 4-tuple (battles, wins, markOfMastery, xp) vs 3-tuple (battles, wins, markOfMastery)
                    for cand_tup in ((vb, vw, vmom, vxp), (vb, vw, vmom)):
                        try:
                            cut[vcd] = cand_tup
                            d.makeCompDescr()
                            break
                        except Exception:
                            try:
                                del cut[vcd]
                            except Exception:
                                pass
                except Exception:
                    pass
        except Exception:
            pass
    except Exception:
        pass
    return d


def populate_vehicle_dossier_descr(d, veh_cd=None):
    """Populate a WoT 0.8.2 VehicleDossierDescr in-place from persistent offline stats."""
    try:
        st = _load_persistent_account_stats()
        vehs = st.get('vehicles', {})
        vinfo = vehs.get(str(int(veh_cd or 0))) if veh_cd else None
        if not vinfo:
            return d
        b_cnt = int(vinfo.get('battlesCount', 0))
        if b_cnt <= 0:
            return d
        try:
            tot = d['total']
            tot['battlesCount'] = b_cnt
            tot['lastBattleTime'] = int(vinfo.get('lastBattleTime', int(time.time())))
            tot['treesCut'] = int(vinfo.get('treesCut', 0))
            tot['mileage'] = int(vinfo.get('mileage', 0))
            tot['battleLifeTime'] = int(vinfo.get('battleLifeTime', 0))
        except Exception:
            pass
        try:
            a15 = d['a15x15']
            a15['battlesCount'] = b_cnt
            a15['wins'] = int(vinfo.get('wins', 0))
            a15['losses'] = int(vinfo.get('losses', 0))
            a15['survivedBattles'] = int(vinfo.get('survivedBattles', 0))
            a15['xp'] = int(vinfo.get('xp', 0))
            a15['frags'] = int(vinfo.get('frags', 0))
            a15['shots'] = int(vinfo.get('shots', 0))
            a15['hits'] = int(vinfo.get('hits', 0))
            a15['damageDealt'] = int(vinfo.get('damageDealt', 0))
            a15['damageReceived'] = int(vinfo.get('damageReceived', 0))
            a15['spotted'] = int(vinfo.get('spotted', 0))
            a15['capturePoints'] = int(vinfo.get('capturePoints', 0))
            a15['droppedCapturePoints'] = int(vinfo.get('droppedCapturePoints', 0))
        except Exception:
            pass
        try:
            m15 = d['max15x15']
            m15['maxXP'] = int(vinfo.get('maxXP', 0))
            m15['maxFrags'] = int(vinfo.get('maxFrags', 0))
        except Exception:
            pass
        try:
            ach = d['achievements']
            if int(vinfo.get('markOfMastery', 0)) > 0:
                ach['markOfMastery'] = int(vinfo.get('markOfMastery', 0))
        except Exception:
            pass
    except Exception:
        pass
    return d
