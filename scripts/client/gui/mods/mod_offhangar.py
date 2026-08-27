# -*- coding: utf-8 -*-
import os
import signal

import Account
import AccountCommands
import Avatar as AvatarModule
import AvatarInputHandler as AvatarInputHandlerModule
import BigWorld
import account_shared

from ConnectionManager import connectionManager
from GameSessionController import _GameSessionController
from account_helpers.Shop import Shop
from debug_utils import LOG_CURRENT_EXCEPTION, LOG_ERROR
from gui.Scaleform.Login import Login
from gui.Scaleform.CommonPage import CommonPage
from gui.Scaleform.gui_items.Vehicle import Vehicle
from helpers.time_utils import _TimeCorrector, _g_instance
from nations import INDICES
from predefined_hosts import g_preDefinedHosts

def _inject_submodule(mod_name, rel_path):
	"""Inject a .py file as a submodule when the normal import fails (missing .pyc)."""
	import sys, os
	full_name = 'gui.mods.offhangar.' + mod_name
	if full_name in sys.modules:
		return sys.modules[full_name]
	import types
	mod = types.ModuleType(full_name)
	mod.__file__ = rel_path
	sys.modules[full_name] = mod
	try:
		execfile(rel_path, mod.__dict__)
	except Exception:
		del sys.modules[full_name]
		raise
	# Bind as an attribute of the parent package. A normal import does this
	# automatically, but execfile injection does not - without it
	# `from gui.mods.offhangar import <name>` (e.g. command_router importing
	# command_handlers) fails with 'cannot import name <name>' even though
	# the module is in sys.modules.
	try:
		parent = sys.modules.get('gui.mods.offhangar')
		if parent is not None:
			setattr(parent, mod_name, mod)
	except Exception:
		pass
	return mod

def _safe_import_offhangar():
	"""Try normal package imports; fall back to execfile injection if .pyc is missing."""
	import sys, os
	candidates = []
	try:
		_this = __file__
		candidates.append(os.path.join(os.path.dirname(_this), 'offhangar'))
		_abs = os.path.abspath(_this)
		candidates.append(os.path.join(os.path.dirname(_abs), 'offhangar'))
	except Exception:
		pass
	candidates.append(r'res_mods\0.8.2\scripts\client\gui\mods\offhangar')
	# Dependency order (deps before dependents): a module that imports another
	# offhangar submodule at import time must come after it. When these load
	# from source (no .pyc) the on-demand import machinery cannot resolve a
	# sibling .py that is not yet in sys.modules, so order + multi-pass retry
	# are both needed. logging/utils/_constants/server are injected HERE too
	# (not just star-imported below) so the whole package works source-only,
	# without any .pyc:
	#   paths <- logging <- _constants ; utils <- logging ; command_handlers
	#   <- command_router <- server ; offline_battle_stack <- offline_battle
	submodules = ['paths', 'physics', 'physics_monitor', 'utils', 'logging', '_constants', 'data', 'state',
	              'command_handlers', 'command_router', 'server',
	              'session_guards', 'destructibles_authority', 'pen_indicator',
	              'battle_ledger', 'battle_medals', 'battle_economy',
	              'bot_routes', 'nav_grid',
	              'offline_battle_stack', 'offline_battle']

	def _try_load(name):
		full = 'gui.mods.offhangar.' + name
		if full in sys.modules:
			return True
		try:
			__import__(full)
			return True
		except ImportError:
			pass
		for _pkg_dir in candidates:
			py_path = os.path.join(_pkg_dir, name + '.py')
			if os.path.exists(py_path):
				try:
					_inject_submodule(name, py_path)
					return True
				except Exception:
					# A dependency may not be loaded yet; a later pass retries.
					if full in sys.modules:
						del sys.modules[full]
		return False

	# Multi-pass: keep going while at least one module newly loads. This makes
	# the loader tolerant of any import order among the source-loaded modules.
	pending = list(submodules)
	for _ in range(len(submodules) + 1):
		if not pending:
			break
		still = []
		progressed = False
		for name in pending:
			if _try_load(name):
				progressed = True
			else:
				still.append(name)
		pending = still
		if not progressed:
			break
	for name in pending:
		import traceback
		try:
			_inject_submodule(name, os.path.join(candidates[0], name + '.py'))
		except Exception:
			try:
				from gui.mods.offhangar.logging import LOG_DEBUG as _LD
				_LD('ImportDBG: giving up on', name, traceback.format_exc())
			except Exception:
				print('[OFFHANGAR] ImportDBG give up', name, traceback.format_exc())


_safe_import_offhangar()

# Every submodule is now injected into sys.modules, so these star-imports
# resolve. They MUST come after the loader: this package cannot be imported
# normally in the embedded client ('Parent module gui.mods not found'), and
# server imports command_router - which the loader injects above.
from gui.mods.offhangar.logging import *
from gui.mods.offhangar.utils import *
from gui.mods.offhangar._constants import *
from gui.mods.offhangar.server import *

try:
	from gui.mods.offhangar.data import getOfflineShopItems
except ImportError:
	import gui.mods.offhangar.data as _data_mod
	getOfflineShopItems = getattr(_data_mod, 'getOfflineShopItems', None)

try:
	from gui.mods.offhangar.session_guards import install_game_session_guards
except ImportError:
	import gui.mods.offhangar.session_guards as _sg_mod
	install_game_session_guards = getattr(_sg_mod, 'install_game_session_guards', lambda: None)

try:
	from gui.mods.offhangar.offline_battle import start_offline_random_from_hangar
except ImportError:
	import gui.mods.offhangar.offline_battle as _ob_mod
	start_offline_random_from_hangar = getattr(_ob_mod, 'start_offline_random_from_hangar', lambda *a, **k: None)


Account.LOG_DEBUG = LOG_DEBUG
Account.LOG_NOTE = LOG_NOTE
Account.LOG_ERROR = LOG_ERROR

g_preDefinedHosts._hosts.append(g_preDefinedHosts._makeHostItem(OFFLINE_SERVER_ADDRESS, OFFLINE_SERVER_ADDRESS, OFFLINE_SERVER_ADDRESS))


class _OfflineArenaStub(object):
	class _VehicleTypeStub(object):
		def __init__(self):
			self.type = self
			self.tags = set()
			self.turretRotatorSpeed = 0.0
			self.circularVisionRadius = 0

		def __getattr__(self, name):
			return 0

	class _ArenaTypeStub(object):
		def __init__(self):
			self.weatherPresets = []
			self.geometryName = ''
			self.gameplayName = ''
			self.teamBasePositions = {1: {1: (0,0)}, 2: {1: (0,0)}}
			self.umbraEnabled = 0
			self.boundingBox = ( (0,0), (1000, 1000) )
			self.defaultReverbPreset = ''
			self.waterTexScale = 0.5
			self.waterFreqX = 1.0
			self.waterFreqZ = 1.0
			self.minimap = None

	class _EventStub(object):
		# (delegate name, exception type, message) already reported. Class level, so
		# one stub's noise is not repeated by the next.
		_reported = set()

		def __init__(self):
			self.delegates = []

		def __iadd__(self, other):
			if other not in self.delegates:
				self.delegates.append(other)
			return self

		def __isub__(self, other):
			if other in self.delegates:
				self.delegates.remove(other)
			return self

		def __call__(self, *args, **kwargs):
			from gui.mods.offhangar.logging import LOG_DEBUG
			for delegate in self.delegates:
				try: delegate(*args, **kwargs)
				except Exception as e:
					# Report each distinct (delegate, error) ONCE, and name it.
					# Battle-UI handlers whose Scaleform movie is not built offline
					# raise on every single event fire, so this logged hundreds of
					# anonymous "'NoneType' object has no attribute 'call'" lines per
					# session - noise that buries the errors worth reading. The stub
					# is nested in a class body, so the set has to be reached through
					# self.__class__: the bare name is in no runtime scope here.
					try:
						_seen = self.__class__._reported
						_key = (getattr(delegate, '__name__', repr(delegate)), type(e).__name__, str(e))
						if _key not in _seen:
							_seen.add(_key)
							LOG_DEBUG('EventStub delegate error (first time): %s -> %s: %s' % _key)
					except Exception:
						LOG_DEBUG('EventStub Delegate Error', e)

	def __init__(self):
		self.vehicles = {}
		self.statistics = {}
		self.arenaType = self._ArenaTypeStub()
		# constants.ARENA_GUI_TYPE.UNKNOWN. This used to be assigned RANDOM (1) and
		# then immediately overwritten with 0 two lines later, so the first
		# assignment never had any effect - 0 is and always was the live value.
		self.guiType = 0
		self.gameMode = 0 # constants.ARENA_GAME_MODE.CTF
		self.bonusType = 0
		self.extraData = {}
		self._event_stubs = {}
		self.period = 1
		self.periodLength = 600
		# periodEndTime is NOT set here - __getattr__ returns serverTime()+600 lazily

	def __getattr__(self, name):
		if name == 'periodEndTime':
			try:
				return BigWorld.serverTime() + 600
			except Exception:
				return 0
		if name.startswith('on'):
			if name not in self._event_stubs:
				self._event_stubs[name] = self._EventStub()
			return self._event_stubs[name]
		return 0


class _OfflineVehicleStub(object):
	class _TypeDescriptorStub(object):
		def __init__(self):
			self.type = _OfflineArenaStub._VehicleTypeStub()

		def __getattr__(self, name):
			return 0

	def __init__(self):
		self.typeDescriptor = self._TypeDescriptorStub()
		self.id = 0


class _OfflineEvent(object):
	def __iadd__(self, other):
		return self

	def __isub__(self, other):
		return self

	def __call__(self, *args, **kwargs):
		return


def _ensure_postmortem_event(obj):
	if obj is None:
		return
	try:
		cur = getattr(obj, 'onPostmortemVehicleChanged', None)
		if cur is None or callable(cur):
			obj.onPostmortemVehicleChanged = _OfflineEvent()
	except Exception:
		LOG_CURRENT_EXCEPTION()


def fini():
	os.kill(os.getpid(), signal.SIGTERM)

@override(Shop, '__onSyncComplete')
def Shop__onSyncComplete(baseFunc, baseSelf, syncID, data):
	data = {
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
		'items': getOfflineShopItems(),
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

	baseFunc(baseSelf, syncID, data)

@override(_TimeCorrector, 'serverRegionalTime')
def TimeCorrector_serverRegionalTime(baseFunc, baseSelf):
	regionalSecondsOffset = 0
	try:
		serverRegionalSettings = OFFLINE_SERVER_SETTINGS['regional_settings']
		regionalSecondsOffset = serverRegionalSettings['starting_time_of_a_new_day']
	except Exception:
		LOG_CURRENT_EXCEPTION()
	return _g_instance.serverUTCTime + regionalSecondsOffset

@override(_GameSessionController, 'isSessionStartedThisDay')
def GameSessionController_isSessionStartedThisDay(baseFunc, baseSelf):
	serverRegionalSettings = OFFLINE_SERVER_SETTINGS['regional_settings']
	return int(_g_instance.serverRegionalTime) / 86400 == int(baseSelf._GameSessionController__sessionStartedAt + serverRegionalSettings['starting_time_of_a_new_day']) / 86400

@override(_GameSessionController, '_getWeeklyPlayHours')
def GameSessionController_getWeeklyPlayHours(baseFunc, baseSelf):
	serverRegionalSettings = OFFLINE_SERVER_SETTINGS['regional_settings']
	weekDaysCount = account_shared.currentWeekPlayDaysCount(_g_instance.serverUTCTime, serverRegionalSettings['starting_time_of_a_new_day'], serverRegionalSettings['starting_day_of_a_new_weak'])
	return baseSelf._getDailyPlayHours() + sum(baseSelf._GameSessionController__stats.dailyPlayHours[1:weekDaysCount])

@override(Vehicle, 'canSell')
def Vehicle_canSell(baseFunc, baseSelf):
	return BigWorld.player().isOffline or baseFunc(baseSelf)

@override(Login, 'populateUI')
def Login_populateUI(baseFunc, baseSelf, proxy):
	baseFunc(baseSelf, proxy)
	connectionManager.connect(OFFLINE_SERVER_ADDRESS, OFFLINE_LOGIN, OFFLINE_PWD, False, False, False)

@override(CommonPage, 'setClanInfo')
def CommonPage_setClanInfo(baseFunc, baseSelf, clanInfo):
	# Header name: Account.name is a server-owned property (not writable offline),
	# so feed the flash call directly with the nickname from config.json.
	try:
		from gui.mods.offhangar._constants import OFFLINE_NICKNAME as _nick
	except Exception:
		_nick = 'Player'
	_nm = str(_nick)
	if clanInfo is not None and len(clanInfo) > 1:
		_nm = '%s [%s]' % (_nm, clanInfo[1])
	baseSelf.call('common.nameResponse', [_nm, False, clanInfo is not None])

@override(Account.PlayerAccount, 'onBecomePlayer')
def Account_onBecomePlayer_nickname(baseFunc, baseSelf):
	baseFunc(baseSelf)
	# Hangar header shows the raw login value otherwise: force the config nickname.
	try:
		from gui.mods.offhangar._constants import OFFLINE_NICKNAME as _OFFN
		baseSelf.name = _OFFN
	except Exception:
		pass

@override(Account.PlayerAccount, '__init__')
def Account_init(baseFunc, baseSelf):
	baseSelf.isOffline = not baseSelf.name
	# Display name from config.json (AFTER the isOffline check above - offline
	# detection relies on the name being empty at connect time, so the connect
	# call must stay untouched).
	try:
		from gui.mods.offhangar._constants import OFFLINE_NICKNAME as _OFFN
		baseSelf.name = _OFFN
	except Exception:
		pass
	if baseSelf.isOffline:
		baseSelf.fakeServer = FakeServer()
		baseSelf.name = OFFLINE_NICKNAME
		baseSelf.serverSettings = OFFLINE_SERVER_SETTINGS
		baseSelf._offhangar_arena = _OfflineArenaStub()
		baseSelf._offhangar_vehicle_stub = _OfflineVehicleStub()
		baseSelf._offhangar_allow_world_clear = False
		baseSelf._offline_allow_become_non_player = False
		baseSelf._offhangar_stats501_streak = 0

	baseFunc(baseSelf)

	if baseSelf.isOffline:
		BigWorld.player(baseSelf)

# PERF: this runs on EVERY attribute access of the offline player and was the
# hottest Python frame in the battle tick (675735 calls per 300 ticks in
# perf_profile_run2_before.txt, 0.777 s self time). Two things made it that
# expensive, both fixed here:
#   1. The 14 sequential 'name == X' compares ran for every access, and each one
#      re-read baseSelf.isOffline - which re-entered THIS function and walked the
#      whole chain again. That self-recursion is the profile's 675735/287377.
#      Now a single frozenset lookup rejects the ~99% of names we do not serve,
#      and isOffline is read through the ORIGINAL getattribute, so it cannot
#      re-enter.
#   2. utils.override wraps the handler in 'lambda *args, **kwargs', so every
#      access also allocated a throwaway tuple + dict and burned an extra Python
#      frame. This one binds to the class directly instead.
# Keep this set in sync with the branches below - a name that is missing here
# never reaches its branch.
_OFFLINE_ATTRS = frozenset((
	'vehicle', 'team', 'inputHandler', 'arenaTypeID', 'arenaUniqueID',
	'setForcedGuiControlMode', 'onMinimapCellClicked', 'playerVehicleID',
	'vehicleTypeDescriptor', 'onGunShotChanged', 'addModel', 'delModel',
	'selectPlayer', 'arena', 'cell', 'base', 'server',
))

# Re-import must not stack a second patch on top of the first (that would recurse
# until the client dies), so unwrap back to the untouched slot wrapper.
_ACC_ORIG_GETATTRIBUTE = Account.PlayerAccount.__getattribute__
if getattr(_ACC_ORIG_GETATTRIBUTE, '_offh_patched', False):
	_ACC_ORIG_GETATTRIBUTE = _ACC_ORIG_GETATTRIBUTE._offh_orig

# baseFunc is a default arg on purpose: that makes it a local load instead of a
# global one, which matters at this call frequency.
def Account_getattribute(baseSelf, name, baseFunc=_ACC_ORIG_GETATTRIBUTE):
	if name not in _OFFLINE_ATTRS:
		return baseFunc(baseSelf, name)
	if not baseFunc(baseSelf, 'isOffline'):
		return baseFunc(baseSelf, name)
	if name == 'vehicle':
		mock = getattr(baseSelf, '_offhangar_mock_veh', None)
		if mock is not None:
			return mock
		return baseFunc(baseSelf, name)
	if name == 'team':
		return getattr(baseSelf, '_offhangar_team', 1)
	if name == 'inputHandler':
		orig_ih = baseFunc(baseSelf, name)
		if orig_ih and not hasattr(orig_ih, 'onCameraChanged'):
			import Event
			orig_ih.onCameraChanged = Event.Event()
			orig_ih.onPostmortemVehicleChanged = Event.Event()
		return orig_ih
	if name in ('arenaTypeID', 'arenaUniqueID'):
		try:
			return baseSelf.arena.arenaType.id
		except:
			return 1
	if name == 'setForcedGuiControlMode':
		return lambda *args, **kwargs: None
	if name == 'onMinimapCellClicked':
		# Minimap._onMapClicked calls this on the Avatar; offline the account
		# stands in for it and crashed the flash callback with AttributeError
		# on every minimap click (python.log traceback).
		return lambda *args, **kwargs: None
	if name == 'playerVehicleID':
		ctx = getattr(baseSelf, '_offhangar_battle_ctx', None) or {}
		return ctx.get('playerVehicleID', 0)
	if name == 'vehicleTypeDescriptor':
		# In battle, return the REAL battle-tank descriptor (set by offline_battle
		# at spawn via player._offhangar_td). Without this the offline account
		# always handed back a fresh tier-1 MS-1 here, so the crosshair
		# penetration marker, tracer shell speed/gravity and maxHealth all used
		# MS-1 data, not the tank being driven. Also stops allocating a new
		# descriptor on every access.
		_offh_td = getattr(baseSelf, '_offhangar_td', None)
		if _offh_td is not None:
			return _offh_td
		try:
			from items import vehicles
			return vehicles.VehicleDescr(typeName='ussr:MS-1')
		except Exception:
			pass
		vehStub = getattr(baseSelf, '_offhangar_vehicle_stub', None)
		if vehStub is None:
			vehStub = _OfflineVehicleStub()
			baseSelf._offhangar_vehicle_stub = vehStub
		td = getattr(vehStub, 'typeDescriptor', None)
		if td is None:
			vehStub.typeDescriptor = _OfflineVehicleStub._TypeDescriptorStub()
			td = vehStub.typeDescriptor
		return td
	if name == 'onGunShotChanged':
		import Event
		if not hasattr(baseSelf, '_offhangar_onGunShotChanged'):
			baseSelf._offhangar_onGunShotChanged = Event.Event()
		return baseSelf._offhangar_onGunShotChanged
	if name == 'playerVehicleID':
		if getattr(baseSelf, '_offhangar_player_vehicle_id', 0):
			return baseSelf._offhangar_player_vehicle_id
		try:
			from CurrentVehicle import g_currentVehicle
			item = getattr(g_currentVehicle, 'item', None)
			if item is not None:
				return getattr(item, 'invID', 0)
		except Exception:
			LOG_CURRENT_EXCEPTION()
		return 0
	if name in ('addModel', 'delModel'):
		# The offline player is the ACCOUNT entity, so Entity.addModel parents the
		# model to the account's own transform/chunk instead of the battle world. The
		# only offline caller is ProjectileMover (shell tracers) - they were built,
		# moved and lit correctly yet never drawn. Hand back the global model API,
		# what every offline effect that DOES show uses.
		# This must be served here: entity method slots are read-only, so assigning
		# player.addModel raises instead of overriding.
		try:
			import sys as _sysm
			_ob = _sysm.modules.get('gui.mods.offhangar.offline_battle')
			if _ob is not None:
				return _ob._offh_player_add_model if name == 'addModel' else _ob._offh_player_del_model
		except Exception:
			pass
	if name == 'selectPlayer':
		# Clicking a name in the players panel. Scaleform Battle.selectPlayer calls
		# player.selectPlayer(vehID) and the offline player is a PlayerAccount, which
		# has no such method - ten AttributeError tracebacks per battle. Retail maps it
		# to picking that vehicle as the post-mortem spectator target (Avatar.selectPlayer:
		# only a LIVING ally, and only while the GUI owns the controls), so do the same.
		def _offh_select_player(vehID, _p=baseSelf):
			try:
				import sys as _syss
				_ob2 = _syss.modules.get('gui.mods.offhangar.offline_battle')
				if not getattr(_p, '_offh_spectating', False):
					return
				_vid = int(vehID)
				_mv = (_ob2.G_MOCK_VEHICLES if _ob2 is not None else {}) or {}
				_t = _mv.get(_vid)
				if _t is None or (getattr(_t, 'health', 0) or 0) <= 0:
					return
				# The spectator tick rebuilds its target list every frame; steer it by id.
				_p._offh_spec_want = _vid
			except Exception:
				pass
		return _offh_select_player
	if name == 'arena':
		return getattr(baseSelf, '_offhangar_arena', None)
	if name in ('cell', 'base', 'server'):
		name = 'fakeServer'

	return baseFunc(baseSelf, name)

Account_getattribute._offh_patched = True
Account_getattribute._offh_orig = _ACC_ORIG_GETATTRIBUTE
Account.PlayerAccount.__getattribute__ = Account_getattribute

def _offh_patch_techtree():
	"""Research screen: invalidateInstalled does getInvItem(nodeCD).pack() with
	no None check; the offline inventory has no per-module entries for every
	mounted module, so opening the tech tree spammed AttributeError
	('NoneType' object has no attribute 'pack', python.log) on each inventory
	update. Swallow just that case - the node list simply reports no change."""
	try:
		import sys
		if 'gui.Scaleform.techtree.data' not in sys.modules:
			import gui.Scaleform.techtree.data
		_ttd = sys.modules['gui.Scaleform.techtree.data']
		cls = getattr(_ttd, 'ResearchItemsData', None)
		if cls is None or getattr(cls, '_offh_inv_patched', False):
			return
		_orig = cls.invalidateInstalled
		def _safe_invalidateInstalled(self, *a, **kw):
			try:
				return _orig(self, *a, **kw)
			except AttributeError:
				return []
		cls.invalidateInstalled = _safe_invalidateInstalled
		cls._offh_inv_patched = True
	except Exception:
		pass

@override(Account.PlayerAccount, 'onBecomePlayer')
def Account_onBecomePlayer(baseFunc, baseSelf):
	import time
	baseSelf._offline_boot_time = time.time()
	_offh_patch_techtree()
	if not hasattr(baseSelf, 'newFakeModel'):
		def newFakeModel():
			import BigWorld
			# Effects attach nodes to this model; a blank Model('') makes
			# Model.node() raise ('not supported for blank models').
			try:
				return BigWorld.Model('objects/fake_model.model')
			except Exception:
				return BigWorld.Model('')
		baseSelf.newFakeModel = newFakeModel
	baseFunc(baseSelf)
	_ensure_postmortem_event(getattr(baseSelf, 'inputHandler', None))
	if baseSelf.isOffline:
		baseSelf.showGUI(OFFLINE_GUI_CTX)

@override(Account.PlayerAccount, 'handleKeyEvent')
def Account_handleKeyEvent(baseFunc, baseSelf, event):
	import Keys
	if event.isKeyDown() and event.key == Keys.KEY_F12:
		LOG_DEBUG('Offline.F12 pressed -> forcing battle start')
		try:
			from gui.mods.offhangar.offline_battle import start_offline_random_from_hangar
			start_offline_random_from_hangar(baseSelf, 0)
		except Exception:
			LOG_CURRENT_EXCEPTION()
		return True
	return baseFunc(baseSelf, event)

@override(Account.PlayerAccount, 'onBecomeNonPlayer')
def Account_onBecomeNonPlayer(baseFunc, baseSelf):
	import traceback
	LOG_DEBUG('Account.onBecomeNonPlayer() called! Traceback:')
	for line in traceback.format_stack():
		LOG_DEBUG(line.strip())
	if baseSelf.isOffline and not getattr(baseSelf, '_offline_allow_become_non_player', False):
		LOG_DEBUG('OfflineStub.skip onBecomeNonPlayer')
		return
	baseFunc(baseSelf)

@override(BigWorld, 'clearEntitiesAndSpaces')
def BigWorld_clearEntitiesAndSpaces(baseFunc, *args):
	player = BigWorld.player()
	if getattr(player, 'isOffline', False) and not getattr(player, '_offhangar_allow_world_clear', False):
		return
	baseFunc(*args)

@override(BigWorld, 'connect')
def BigWorld_connect(baseFunc, server, loginParams, progressFn):
	if server == OFFLINE_SERVER_ADDRESS:
		LOG_DEBUG('BigWorld.connect')
		progressFn(1, "LOGGED_ON", {})
		BigWorld.createEntity('Account', BigWorld.createSpace(), 0, (0, 0, 0), (0, 0, 0), {})
	else:
		baseFunc(server, loginParams, progressFn)


import game as _game_module
_orig_game_fini = _game_module.fini
def _offline_game_fini():
	player = BigWorld.player()
	if getattr(player, 'isOffline', False) and not getattr(player, '_offline_allow_become_non_player', False):
		LOG_DEBUG('OfflineBattle.blocked game.fini() during battle')
		return
	_orig_game_fini()
_game_module.fini = _offline_game_fini


def _offline_enqueue_random_cmd_id():
	return getattr(AccountCommands, 'CMD_ENQUEUE_RANDOM', 700)

def _offline_enqueue_tutorial_cmd_id():
	return getattr(AccountCommands, 'CMD_ENQUEUE_TUTORIAL', 737)


def _install_offline_account__do_cmd_hook():
	if '_PlayerAccount__doCmd' not in dir(Account.PlayerAccount):
		LOG_DEBUG('Offline.__doCmd missing on PlayerAccount')
		return
	try:
		@override(Account.PlayerAccount, '__doCmd')
		def PlayerAccount___doCmd(baseFunc, baseSelf, doCmdMethod, cmd, callback, *args):
			if not getattr(baseSelf, 'isOffline', False):
				return baseFunc(baseSelf, doCmdMethod, cmd, callback, *args)
			if doCmdMethod != 'doCmdInt3' or (cmd != _offline_enqueue_random_cmd_id() and cmd != _offline_enqueue_tutorial_cmd_id()):
				return baseFunc(baseSelf, doCmdMethod, cmd, callback, *args)
			getRid = getattr(baseSelf, '_PlayerAccount__getRequestID', None)
			if not callable(getRid):
				LOG_DEBUG('Offline.__doCmd ENQUEUE_RANDOM skip no __getRequestID')
				return baseFunc(baseSelf, doCmdMethod, cmd, callback, *args)
			rid = getRid()
			if rid is None:
				return baseFunc(baseSelf, doCmdMethod, cmd, callback, *args)
			respMap = getattr(baseSelf, '_PlayerAccount__onCmdResponse', None)
			if callback is not None and respMap is not None:
				respMap[rid] = callback
			vehInvID = args[0] if args else 0

			def _ack_and_boot():
				try:
					import traceback
					LOG_DEBUG('Offline.__doCmd ENQUEUE_RANDOM caller traceback:')
					for line in traceback.format_stack():
						LOG_DEBUG(line.strip())
					baseSelf.onCmdResponse(rid, AccountCommands.RES_SUCCESS, '')
				except Exception:
					LOG_CURRENT_EXCEPTION()
				LOG_DEBUG('Offline.__doCmd ENQUEUE_RANDOM IGNORED')

			LOG_DEBUG('Offline.__doCmd ENQUEUE_RANDOM', rid, vehInvID)
			BigWorld.callback(0.0, _ack_and_boot)
			return rid
	except Exception:
		LOG_CURRENT_EXCEPTION()


def _install_offline_enqueue_public_hooks():
	if hasattr(Account.PlayerAccount, 'enqueueRandom'):
		@override(Account.PlayerAccount, 'enqueueRandom')
		def PlayerAccount_enqueueRandom(baseFunc, baseSelf, *args, **kwargs):
			if getattr(baseSelf, 'isOffline', False):
				LOG_DEBUG('Offline.enqueueRandom IGNORED')
				return
			return baseFunc(baseSelf, *args, **kwargs)
	else:
		LOG_DEBUG('Offline.enqueueRandom missing')

	# Cancel button in the "searching for battle" panel. Retail dequeueRandom sends
	# CMD_DEQUEUE_RANDOM to the BASE entity, which does not exist offline, so the
	# click did nothing at all and the battle still started when the fake queue
	# timer ran out. Flag the cancel and fire onDequeuedRandom ourselves: that is
	# what clears isInRandomQueue and returns the hangar UI to its normal state.
	if hasattr(Account.PlayerAccount, 'dequeueRandom'):
		@override(Account.PlayerAccount, 'dequeueRandom')
		def PlayerAccount_dequeueRandom(baseFunc, baseSelf, *args, **kwargs):
			if not getattr(baseSelf, 'isOffline', False):
				return baseFunc(baseSelf, *args, **kwargs)
			baseSelf._offhangar_queue_cancelled = True
			LOG_DEBUG('Offline.dequeueRandom -> queue cancelled')
			try:
				baseSelf.onDequeuedRandom()
			except Exception:
				LOG_CURRENT_EXCEPTION()
			# The Prebattle page does NOT close itself on Exit - it only sends the
			# dequeue and then waits for the SERVER to release the vehicle lock:
			#     g_playerEvents.onVehicleLockChanged -> __lockChange -> showHangar()
			# Offline nothing ever unlocks it, so the search screen just stayed up and
			# the click looked dead. Fire the same event the server would.
			try:
				from PlayerEvents import g_playerEvents as _pev
				from AccountCommands import LOCK_REASON as _LR
				from CurrentVehicle import g_currentVehicle as _cv
				_inv = _cv.vehicle.inventoryId
				LOG_DEBUG('Offline.dequeueRandom -> unlock vehicle', _inv)
				_pev.onVehicleLockChanged(_inv, _LR.NONE)
			except Exception:
				LOG_CURRENT_EXCEPTION()
			return
	else:
		LOG_DEBUG('Offline.dequeueRandom missing')

	if hasattr(Account.PlayerAccount, 'enqueueTutorial'):
		@override(Account.PlayerAccount, 'enqueueTutorial')
		def PlayerAccount_enqueueTutorial(baseFunc, baseSelf, *args, **kwargs):
			if getattr(baseSelf, 'isOffline', False):
				LOG_DEBUG('Offline.enqueueTutorial IGNORED')
				return
			return baseFunc(baseSelf, *args, **kwargs)

	candidates = []
	for name in dir(Account.PlayerAccount):
		if not callable(getattr(Account.PlayerAccount, name)):
			continue
		low = name.lower()
		if 'tutorial' in low or 'bootcamp' in low or 'sandbox' in low:
			continue
		if 'enqueue' in low and 'random' in low and name != 'enqueueRandom' and not name.startswith('on'):
			candidates.append(name)
	if candidates:
		LOG_DEBUG('Offline.enqueueExtraCandidates', candidates)
	for methodName in candidates:
		try:
			def _bind(nm):
				@override(Account.PlayerAccount, nm)
				def _enqueueAlt(baseFunc, baseSelf, *args, **kwargs):
					if getattr(baseSelf, 'isOffline', False):
						LOG_DEBUG('Offline.intercepted IGNORED', nm, args)
						return
					return baseFunc(baseSelf, *args, **kwargs)
			_bind(methodName)
		except Exception:
			LOG_CURRENT_EXCEPTION()


def _install_offline_battle_transport_hooks():
	_install_offline_account__do_cmd_hook()
	_install_offline_enqueue_public_hooks()


def _install_offline_avatar_guards():
	def _ensure_offline_avatar_state(baseSelf):
		class DummyMProv(object):
			target = None
		defaults = {
			'_PlayerAvatar__stepsTillInit': 1,
			'_PlayerAvatar__isSpaceInitialized': False,
			'_PlayerAvatar__setOwnVehicleMatrixTimerID': 0,
			'_PlayerAvatar__isForcedGuiControlMode': False,
			'_PlayerAvatar__ownVehicleMProv': DummyMProv(),
			'_PlayerAvatar__shotWaitingTimerID': 0,
			'_PlayerAvatar__fireNonFatalDamageTriggerID': 0,
			'playerVehicleID': 0,
		}
		for key, value in defaults.iteritems():
			if not hasattr(baseSelf, key):
				setattr(baseSelf, key, value)
		if not hasattr(baseSelf, 'arena'):
			baseSelf.arena = _OfflineArenaStub()
		if not hasattr(baseSelf, '_offhangar_vehicle_stub'):
			baseSelf._offhangar_vehicle_stub = _OfflineVehicleStub()
		vehStub = baseSelf._offhangar_vehicle_stub
		for attrName in ('_Avatar__vehicleAttached', '_PlayerAvatar__vehicleAttached', '_Avatar__vehicle'):
			if not hasattr(baseSelf, attrName) or getattr(baseSelf, attrName) is None:
				try:
					setattr(baseSelf, attrName, vehStub)
				except TypeError:
					pass
				except Exception:
					LOG_CURRENT_EXCEPTION()

	seen = set()
	for className in ('Avatar', 'PlayerAvatar'):
		avatarCls = getattr(AvatarModule, className, None)
		if avatarCls is None or not hasattr(avatarCls, 'onEnterWorld'):
			continue
		if id(avatarCls) in seen:
			continue
		seen.add(id(avatarCls))

		@override(avatarCls, 'onEnterWorld')
		def _avatar_onEnterWorld(baseFunc, baseSelf, _className=className, *args, **kwargs):
			_ensure_offline_avatar_state(baseSelf)
			try:
				if args:
					return baseFunc(baseSelf, *args, **kwargs)
				return baseFunc(baseSelf, [])
			except KeyError as ex:
				if 'fake_model.model' in str(ex):
					LOG_DEBUG('OfflineAvatar.ignore missing model', _className, ex)
					return
				raise

		if hasattr(avatarCls, 'onLeaveWorld'):
			@override(avatarCls, 'onLeaveWorld')
			def _avatar_onLeaveWorld(baseFunc, baseSelf, _className=className, *args, **kwargs):
				_ensure_offline_avatar_state(baseSelf)
				try:
					return baseFunc(baseSelf, *args, **kwargs)
				except AttributeError as ex:
					msg = str(ex)
					if 'playerVehicleID' in msg or '_PlayerAvatar__stepsTillInit' in msg or '_PlayerAvatar__setOwnVehicleMatrixTimerID' in msg:
						LOG_DEBUG('OfflineAvatar.ignore leave attr', _className, ex)
						return
					raise
				except ValueError as ex:
					msg = str(ex)
					if 'py_cancelCallback' in msg:
						LOG_DEBUG('OfflineAvatar.ignore leave callback', _className, ex)
						return
					raise

		if hasattr(avatarCls, 'getVehicleAttached'):
			@override(avatarCls, 'getVehicleAttached')
			def _avatar_getVehicleAttached(baseFunc, baseSelf, *args, **kwargs):
				try:
					veh = baseFunc(baseSelf, *args, **kwargs)
					if veh is not None:
						return veh
				except Exception:
					LOG_CURRENT_EXCEPTION()
				_ensure_offline_avatar_state(baseSelf)
				return getattr(baseSelf, '_offhangar_vehicle_stub')


def _install_offline_input_guards():
	accIhCls = getattr(Account, 'AccountInputHandler', None)
	if accIhCls is not None and hasattr(accIhCls, '__init__'):
		@override(accIhCls, '__init__')
		def _accIh_init(baseFunc, baseSelf, *args, **kwargs):
			baseFunc(baseSelf, *args, **kwargs)
			_ensure_postmortem_event(baseSelf)
		LOG_DEBUG('OfflineInput.patched AccountInputHandler.__init__')

	aihCls = getattr(AvatarInputHandlerModule, 'AvatarInputHandler', None)
	LOG_DEBUG('OfflineInput._install guards: aihCls:', aihCls)
	if aihCls is not None:
		LOG_DEBUG('OfflineInput._install guards: hasattr start:', hasattr(aihCls, 'start'))
	if aihCls is None or not hasattr(aihCls, 'start'):
		LOG_DEBUG('OfflineInput._install guards: EARLY RETURN!')
		return

	if hasattr(aihCls, '__init__'):
		@override(aihCls, '__init__')
		def _aih_init(baseFunc, baseSelf, *args, **kwargs):
			baseFunc(baseSelf, *args, **kwargs)
			_ensure_postmortem_event(baseSelf)
		LOG_DEBUG('OfflineInput.patched AvatarInputHandler.__init__')


install_game_session_guards()
_install_offline_battle_transport_hooks()
_install_offline_avatar_guards()
_install_offline_input_guards()
