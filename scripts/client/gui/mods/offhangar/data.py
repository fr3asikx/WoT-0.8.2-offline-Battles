import functools
import time
from itertools import cycle

import items
import nations
from AccountCommands import VEHICLE_SETTINGS_FLAG
from constants import ACCOUNT_ATTR
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

def getOfflineShopItems():
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

	return shopItems

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
		data[ITEM_TYPE_INDICES['vehicle']]['eqs'].update({i: []})
		data[ITEM_TYPE_INDICES['vehicle']]['eqsLayout'].update({i: []})
		data[ITEM_TYPE_INDICES['vehicle']]['shells'].update({i: vehicles.getDefaultAmmoForGun(vehicle.turrets[0][0]['guns'][0])})
		data[ITEM_TYPE_INDICES['vehicle']]['shellsLayout'].update({i: {turretGun: vehicles.getDefaultAmmoForGun(vehicle.turrets[0][0]['guns'][0])}})

		for tmanID in xrange(i_crew, len(tmanList) + i_crew):
			tDescr = items.tankmen.TankmanDescr(compactDescr=next(tmanListCycle))
			tDescr.addXP(10000000)
			data[ITEM_TYPE_INDICES['tankman']]['vehicle'][tmanID] = i
			data[ITEM_TYPE_INDICES['tankman']]['compDescr'][tmanID] = tDescr.makeCompactDescr()
			i_crew += 1

		i += 1

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
