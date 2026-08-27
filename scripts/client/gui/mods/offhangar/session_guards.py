from GameSessionController import _GameSessionController

from gui.mods.offhangar.logging import LOG_DEBUG
from gui.mods.offhangar.utils import override


FORCE_ALLOW_BATTLE_ENTRY = False


def normalize_offline_stats(stats):
	"""Disable anti-offline restrictions in account stats payload."""
	if not isinstance(stats, dict):
		return
	stats['battlesTillCaptcha'] = 0
	stats['captchaTriesLeft'] = 0
	stats['restrictions'] = {}


def _always_false(baseFunc, baseSelf, *args, **kwargs):
	return False

def _always_zero(baseFunc, baseSelf, *args, **kwargs):
	return 0



def _always_24h(baseFunc, baseSelf, *args, **kwargs):
	return 24 * 3600


def _always_true(baseFunc, baseSelf, *args, **kwargs):
	return True


def install_game_session_guards():
	"""
	Patch GameSessionController restrictions that can block battle entry.
	Only patch existing methods to stay compatible with different 0.8.x builds.
	"""
	patches_false = (
		'needCaptcha',
		'isCaptchaRequired',
		'isParentControlActive',
		'isParentControlEnabled',
		'hasActiveSessionLimit',
		'isSessionStartedThisDay'
	)
	for method_name in patches_false:
		if hasattr(_GameSessionController, method_name):
			override(_GameSessionController, method_name)(_always_false)
			LOG_DEBUG('SessionGuard.disable', method_name)

	
	for method_name in ('getDailyPlayTimeLeft', 'getWeeklyPlayTimeLeft'):
		if hasattr(_GameSessionController, method_name):
			override(_GameSessionController, method_name)(_always_24h)
			LOG_DEBUG('SessionGuard.disable', method_name)

	for method_name in ('_getDailyPlayHours', '_getWeeklyPlayHours'):
		if hasattr(_GameSessionController, method_name):
			override(_GameSessionController, method_name)(_always_zero)
			LOG_DEBUG('SessionGuard.disable', method_name)

	if FORCE_ALLOW_BATTLE_ENTRY:
		for method_name in ('isAccountAllowedToBattle', 'canJoinBattle'):
			if hasattr(_GameSessionController, method_name):
				override(_GameSessionController, method_name)(_always_true)
				LOG_DEBUG('SessionGuard.forceAllowBattle', method_name)




def install_dossier_guard():
    try:
        import dossiers
        from gui.mods.offhangar.logging import LOG_DEBUG
        _orig = dossiers.getAccountDossierDescr
        def _safe(compDescr):
            if compDescr is None:
                compDescr = ''
            return _orig(compDescr)
        dossiers.getAccountDossierDescr = _safe
        
        import Account
        orig_onCmdResponse = Account.Account.onCmdResponse
        def onCmdResponse_hook(self, requestID, resultID, errorStr, *args, **kwargs):
            LOG_DEBUG('Account.onCmdResponse executed!', requestID, resultID, errorStr)
            return orig_onCmdResponse(self, requestID, resultID, errorStr, *args, **kwargs)
        Account.Account.onCmdResponse = onCmdResponse_hook
        PlayerAccountCls = getattr(Account, 'PlayerAccount', None)
        if PlayerAccountCls:
            orig_doCmd = getattr(PlayerAccountCls, '_PlayerAccount__doCmd', None)
            if orig_doCmd:
                def doCmd_hook(self, doCmdMethod, cmd, callback, *args):
                    LOG_DEBUG('PlayerAccount.__doCmd CALLED!', doCmdMethod, cmd, 'callback is None:', callback is None)
                    return orig_doCmd(self, doCmdMethod, cmd, callback, *args)
                setattr(PlayerAccountCls, '_PlayerAccount__doCmd', doCmd_hook)
        
            orig_onCmdResponseExt = getattr(PlayerAccountCls, 'onCmdResponseExt', None)
            if orig_onCmdResponseExt:
                def onCmdResponseExt_hook(self, requestID, resultID, errorStr, ext, *args, **kwargs):
                    cb = self._PlayerAccount__onCmdResponse.get(requestID) if hasattr(self, '_PlayerAccount__onCmdResponse') else None
                    LOG_DEBUG('PlayerAccount.onCmdResponseExt executed!', requestID, resultID, errorStr, len(ext) if ext else 0, 'Callback found:', cb is not None)
                    return orig_onCmdResponseExt(self, requestID, resultID, errorStr, ext, *args, **kwargs)
                setattr(PlayerAccountCls, 'onCmdResponseExt', onCmdResponseExt_hook)
        
        from gui.Scaleform.customization.EmblemInterface import EmblemInterface
        orig_onChangeVehicleEmblem = EmblemInterface._EmblemInterface__onChangeVehicleEmblem
        def onChangeVehicleEmblem_hook(self, resultID, price):
            LOG_DEBUG('EmblemInterface.__onChangeVehicleEmblem CALLED! resultID:', resultID, 'price:', price)
            LOG_DEBUG('Delegates on onCustomizationChangeSuccess:', len(self.onCustomizationChangeSuccess._Event__delegates) if hasattr(self.onCustomizationChangeSuccess, '_Event__delegates') else 'NO DELEGATES ATTRIBUTE', self.onCustomizationChangeSuccess._Event__delegates if hasattr(self.onCustomizationChangeSuccess, '_Event__delegates') else '')
            return orig_onChangeVehicleEmblem(self, resultID, price)
        EmblemInterface._EmblemInterface__onChangeVehicleEmblem = onChangeVehicleEmblem_hook
        
        from gui.Scaleform.customization.InscriptionInterface import InscriptionInterface
        orig_onChangeVehicleInscription = InscriptionInterface._InscriptionInterface__onChangeVehicleInscription
        def onChangeVehicleInscription_hook(self, resultID, price):
            LOG_DEBUG('InscriptionInterface.__onChangeVehicleInscription CALLED! resultID:', resultID, 'price:', price)
            LOG_DEBUG('Delegates on onCustomizationChangeSuccess:', len(self.onCustomizationChangeSuccess._Event__delegates) if hasattr(self.onCustomizationChangeSuccess, '_Event__delegates') else 'NO DELEGATES ATTRIBUTE', self.onCustomizationChangeSuccess._Event__delegates if hasattr(self.onCustomizationChangeSuccess, '_Event__delegates') else '')
            return orig_onChangeVehicleInscription(self, resultID, price)
        InscriptionInterface._InscriptionInterface__onChangeVehicleInscription = onChangeVehicleInscription_hook
        
        from gui.Scaleform.Waiting import Waiting
        orig_waiting_hide = Waiting.hide
        def waiting_hide_hook(message):
            LOG_DEBUG('Waiting.hide called for:', message)
            return orig_waiting_hide(message)
        Waiting.hide = staticmethod(waiting_hide_hook)
        
        orig_waiting_show = Waiting.show
        def waiting_show_hook(message, *args, **kwargs):
            LOG_DEBUG('Waiting.show called for:', message)
            return orig_waiting_show(message, *args, **kwargs)
        Waiting.show = staticmethod(waiting_show_hook)
        
        from gui.Scaleform.VehicleCustomization import VehicleCustomization
        orig_ci_onCustomizationChangeSuccess = VehicleCustomization._VehicleCustomization__ci_onCustomizationChangeSuccess
        def ci_onCustomizationChangeSuccess_hook(self, message, type):
            LOG_DEBUG('__ci_onCustomizationChangeSuccess CALLED! current steps:', self._VehicleCustomization__steps)
            res = orig_ci_onCustomizationChangeSuccess(self, message, type)
            if hasattr(self, '_VehicleCustomization__steps'):
                if self._VehicleCustomization__steps <= 0:
                    LOG_DEBUG('Steps reached 0! Forcing __onServerResponsesReceived just in case')
                    self._VehicleCustomization__onServerResponsesReceived()
            return res
        VehicleCustomization._VehicleCustomization__ci_onCustomizationChangeSuccess = ci_onCustomizationChangeSuccess_hook

        orig_onConfirmApply = VehicleCustomization.onConfirmApply
        def onConfirmApply_hook(self, _):
            import BigWorld
            inv = BigWorld.player().inventory
            
            actual_commands_sent = [0]
            
            orig_changeVehicleEmblem = inv.changeVehicleEmblem
            orig_changeVehicleInscription = inv.changeVehicleInscription
            orig_changeVehicleCamouflage = inv.changeVehicleCamouflage
            orig_changeVehicleHorn = getattr(inv, 'changeVehicleHorn', None)
            
            def hook_emblem(*args, **kwargs):
                actual_commands_sent[0] += 1
                return orig_changeVehicleEmblem(*args, **kwargs)
                
            def hook_inscription(*args, **kwargs):
                actual_commands_sent[0] += 1
                return orig_changeVehicleInscription(*args, **kwargs)
                
            def hook_camouflage(*args, **kwargs):
                actual_commands_sent[0] += 1
                return orig_changeVehicleCamouflage(*args, **kwargs)
                
            def hook_horn(*args, **kwargs):
                actual_commands_sent[0] += 1
                if orig_changeVehicleHorn:
                    return orig_changeVehicleHorn(*args, **kwargs)
                
            inv.changeVehicleEmblem = hook_emblem
            inv.changeVehicleInscription = hook_inscription
            inv.changeVehicleCamouflage = hook_camouflage
            if orig_changeVehicleHorn:
                inv.changeVehicleHorn = hook_horn
            
            try:
                res = orig_onConfirmApply(self, _)
            finally:
                inv.changeVehicleEmblem = orig_changeVehicleEmblem
                inv.changeVehicleInscription = orig_changeVehicleInscription
                inv.changeVehicleCamouflage = orig_changeVehicleCamouflage
                if orig_changeVehicleHorn:
                    inv.changeVehicleHorn = orig_changeVehicleHorn
                
            if hasattr(self, '_VehicleCustomization__steps'):
                wg_steps = self._VehicleCustomization__steps
                self._VehicleCustomization__steps = actual_commands_sent[0]
                LOG_DEBUG('VehicleCustomization.onConfirmApply EXACT COMMANDS SENT:', actual_commands_sent[0], 'WG STEPS WAS:', wg_steps)
                
                if self._VehicleCustomization__steps <= 0:
                    self._VehicleCustomization__onServerResponsesReceived()
            return res
        VehicleCustomization.onConfirmApply = onConfirmApply_hook

        orig_onServerResponsesReceived = VehicleCustomization._VehicleCustomization__onServerResponsesReceived
        def onServerResponsesReceived_hook(self):
            LOG_DEBUG('__onServerResponsesReceived CALLED! lockUpdate:', self._VehicleCustomization__lockUpdate)
            try:
                return orig_onServerResponsesReceived(self)
            except TypeError:
                # Offline the forced second call finds the one-shot completion
                # callback already consumed (None). First call did the work -
                # swallow instead of aborting the customization event chain.
                LOG_DEBUG('__onServerResponsesReceived: duplicate call swallowed')
        VehicleCustomization._VehicleCustomization__onServerResponsesReceived = onServerResponsesReceived_hook
        
        orig_vc_dispossessUI = VehicleCustomization.dispossessUI
        def vc_dispossessUI_hook(self):
            LOG_DEBUG('VehicleCustomization.dispossessUI CALLED! steps:', self._VehicleCustomization__steps)
            return orig_vc_dispossessUI(self)
        VehicleCustomization.dispossessUI = vc_dispossessUI_hook
        
        from gui.Scaleform.customization.BaseTimedCustomizationInterface import BaseTimedCustomizationInterface
        orig_dispossessUI = BaseTimedCustomizationInterface.dispossessUI
        def dispossessUI_hook(self):
            LOG_DEBUG('BaseTimedCustomizationInterface.dispossessUI CALLED for:', self._name)
            return orig_dispossessUI(self)
        BaseTimedCustomizationInterface.dispossessUI = dispossessUI_hook
        
        LOG_DEBUG('SessionGuard.dossier_guard_installed')
    except Exception:
        from debug_utils import LOG_CURRENT_EXCEPTION
        LOG_CURRENT_EXCEPTION()

install_dossier_guard()


def install_sync_data_guard():
    """
    Patch AccountSyncData.waitForSync so that in offline mode it immediately
    fires callbacks instead of waiting. This makes Stats.get() return cached
    values right away rather than returning None while waiting for a server.
    """
    try:
        from account_helpers.AccountSyncData import AccountSyncData
        from AccountCommands import RES_CACHE
        from gui.mods.offhangar.logging import LOG_DEBUG
        _orig_waitForSync = AccountSyncData.waitForSync
        def _offline_waitForSync(self, callback):
            if self._AccountSyncData__isSynchronized:
                if callback is not None:
                    callback(RES_CACHE)
                return
            if not self._AccountSyncData__ignore:
                # Force synchronized so future calls fire immediately
                self._AccountSyncData__isSynchronized = True
                if callback is not None:
                    callback(RES_CACHE)
                return
            return _orig_waitForSync(self, callback)
        AccountSyncData.waitForSync = _offline_waitForSync
        LOG_DEBUG('SessionGuard.sync_data_guard_installed')
    except Exception:
        from debug_utils import LOG_CURRENT_EXCEPTION
        LOG_CURRENT_EXCEPTION()

install_sync_data_guard()


def install_stats_defaults_guard():
    """
    Patch Stats.__onGetResponse to inject safe defaults for any stat that is
    still None after the cache lookup. This handles the race between Hangar
    requesting stats and Stats.synchronize() actually running.
    """
    try:
        from account_helpers.Stats import Stats
        from gui.mods.offhangar.logging import LOG_DEBUG
        _DEFAULTS = {
            'eliteVehicles': set(),
            'multipliedXPVehs': set(),
            'unlocks': set(),
            'vehTypeXP': {},
            'vehTypeLocks': {},
            'globalVehicleLocks': {},
            'restrictions': {},
            'slots': 2000,
            'dossier': '',
            'credits': 100000000,
            'gold': 1000000,
            'berths': 40,
            'freeXP': 100000000,
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
            'clanDBID': 0,
            'attrs': 0,
            'premiumExpiryTime': 0,
            'autoBanTime': 0,
            'oldVehInvID': 0,
            'globalRating': 0,
            'fortResource': 0,
            'unitAcceptDeadline': 0,
        }
        _orig = Stats._Stats__onGetResponse
        def _safe_onGetResponse(self, statName, callback, resultID):
            # We must intercept the inner callback instead, because __onGetResponse
            # looks up the value in cache and passes it to the callback!
            def _inner_callback(resID, val, *args, **kwargs):
                if val is None and statName in _DEFAULTS:
                    val = _DEFAULTS[statName]
                if callback:
                    callback(resID, val, *args, **kwargs)
            return _orig(self, statName, _inner_callback if callback else None, resultID)
        Stats._Stats__onGetResponse = _safe_onGetResponse
        LOG_DEBUG('SessionGuard.stats_defaults_guard_installed')
    except Exception:
        from debug_utils import LOG_CURRENT_EXCEPTION
        LOG_CURRENT_EXCEPTION()

install_stats_defaults_guard()


def install_customization_guard():
    try:
        from gui.Scaleform.customization.BaseTimedCustomizationInterface import BaseTimedCustomizationInterface
        from gui.mods.offhangar.logging import LOG_DEBUG
        _orig = BaseTimedCustomizationInterface.getItemCost
        def _safe_getItemCost(self, itemId, priceIndex):
            try:
                return _orig(self, itemId, priceIndex)
            except AttributeError:
                # Occurs if packageCost is None due to offline shop data missing packages
                return {'cost': 0, 'isGold': False}
        BaseTimedCustomizationInterface.getItemCost = _safe_getItemCost
        _origHandle = BaseTimedCustomizationInterface._BaseTimedCustomizationInterface__handleRentalPackageChange
        def _safe_handleRentalPackageChange(self, *args, **kwargs):
            if getattr(self, '_rentalPackageDP', None) is not None:
                item = getattr(self._rentalPackageDP, 'selectedPackage', None)
                if item is None or item.get('cost', -1) <= -1:
                    if hasattr(self, '_itemsDP'):
                        self._itemsDP.setDefaultCost(0, False)
                        self._itemsDP.refresh()
                    return
            return _origHandle(self, *args, **kwargs)
        BaseTimedCustomizationInterface._BaseTimedCustomizationInterface__handleRentalPackageChange = _safe_handleRentalPackageChange
        
        LOG_DEBUG('SessionGuard.customization_guard_installed')
    except Exception:
        pass

install_customization_guard()


def log_credits():
    try:
        from gui.mods.offhangar.logging import LOG_DEBUG
        import BigWorld
        if hasattr(BigWorld.player(), 'stats'):
            def cb(res, val=None):
                LOG_DEBUG('Credits test:', res, val)
            BigWorld.player().stats.get('credits', cb)
    except Exception as e:
        pass

import BigWorld
if hasattr(BigWorld, 'callback'):
    BigWorld.callback(5.0, log_credits)


def install_all_sync_guards():
    try:
        from account_helpers.Shop import Shop
        from account_helpers.Inventory import Inventory
        from account_helpers.DossierCache import DossierCache
        from account_helpers.Stats import Stats
        from account_helpers.AccountSyncData import AccountSyncData
        from account_helpers.AccountSettings import AccountSettings
        from account_helpers.Trader import Trader
        from AccountCommands import RES_CACHE

        classes = [Shop, Inventory, DossierCache, Stats, AccountSyncData, Trader, AccountSettings]
        for cls in classes:
            if not hasattr(cls, 'waitForSync'): continue
            _orig = cls.waitForSync
            def make_wrapper(orig_method, class_name):
                def _offline_waitForSync(self, callback):
                    if hasattr(self, '_' + class_name + '__ignore') and getattr(self, '_' + class_name + '__ignore'):
                        if callback: callback(-1)
                        return
                    if hasattr(self, '_' + class_name + '__isSynchronizing') and getattr(self, '_' + class_name + '__isSynchronizing'):
                        setattr(self, '_' + class_name + '__isSynchronizing', False)
                    # Force call immediately
                    if callback:
                        try:
                            rev = getattr(self, '_' + class_name + '__getCacheRevision')()
                            callback(RES_CACHE, rev)
                        except Exception:
                            try: callback(RES_CACHE)
                            except Exception: pass
                    return
                return _offline_waitForSync
            cls.waitForSync = make_wrapper(_orig, cls.__name__)
    except Exception:
        pass

install_all_sync_guards()

install_game_session_guards()

def patch_inventory_setAndFillLayouts():
    try:
        import AccountCommands
        from account_helpers.Inventory import Inventory
        from gui.mods.offhangar.logging import LOG_DEBUG
        _orig = Inventory._Inventory__setAndFillLayouts_onShopSynced
        def _patched(self, vehInvID, shellsLayout, eqsLayout, callback, resultID, shopRev):
            LOG_DEBUG('Inventory.__setAndFillLayouts_onShopSynced called', vehInvID, shellsLayout, eqsLayout, resultID, shopRev)
            return _orig(self, vehInvID, shellsLayout, eqsLayout, callback, resultID, shopRev)
        Inventory._Inventory__setAndFillLayouts_onShopSynced = _patched
    except Exception:
        pass
patch_inventory_setAndFillLayouts()


def patch_check_credits_requester():
    try:
        from gui.mods.offhangar.logging import LOG_DEBUG
        from gui.Scaleform.utils.requesters import StatsRequester
        def cb(credits):
            LOG_DEBUG('Requester Credits from patch:', credits)
        StatsRequester().getCredits(cb)
    except Exception as e:
        from debug_utils import LOG_CURRENT_EXCEPTION
        LOG_CURRENT_EXCEPTION()

def log_requester_on_login():
    import BigWorld
    import Account
    _orig = Account.PlayerAccount.onBecomePlayer
    def _patched(self, *args, **kwargs):
        _orig(self, *args, **kwargs)
        BigWorld.callback(2.0, patch_check_credits_requester)
    Account.PlayerAccount.onBecomePlayer = _patched
log_requester_on_login()

def patch_technical_maintenance():
    from gui.Scaleform.TechnicalMaintenance import TechnicalMaintenance
    _orig_populateUI = TechnicalMaintenance.populateUI
    def _patched_populateUI(self, *args, **kwargs):
        _orig_populateUI(self, *args, **kwargs)
        try:
            if hasattr(self, 'flashObject') and self.flashObject is not None:
                if hasattr(self.flashObject, 'as_setCredits'):
                    self.flashObject.as_setCredits(100000000)
                if hasattr(self.flashObject, 'as_setGold'):
                    self.flashObject.as_setGold(1000000)
        except Exception as e:
            pass
    TechnicalMaintenance.populateUI = _patched_populateUI

patch_technical_maintenance()

def patch_technical_maintenance_credits():
    from gui.Scaleform.TechnicalMaintenance import TechnicalMaintenance
    _orig_populateUI = TechnicalMaintenance.populateUI
    def _patched_populateUI(self, *args, **kwargs):
        _orig_populateUI(self, *args, **kwargs)
        try:
            if hasattr(self, 'uiHolder') and hasattr(self.uiHolder, 'call'):
                self.uiHolder.call('techMaintenance.setCredits', [100000000])
                self.uiHolder.call('techMaintenance.setGold', [1000000])
        except Exception as e:
            from debug_utils import LOG_CURRENT_EXCEPTION
            LOG_CURRENT_EXCEPTION()
    TechnicalMaintenance.populateUI = _patched_populateUI

patch_technical_maintenance_credits()

def patch_tech_maintenance_update():
    from gui.Scaleform.TechnicalMaintenance import TechnicalMaintenance
    from PlayerEvents import g_playerEvents
    _orig_populateUI = TechnicalMaintenance.populateUI
    def _patched_populateUI(self, *args, **kwargs):
        _orig_populateUI(self, *args, **kwargs)
        try:
            import BigWorld
            if hasattr(BigWorld.player(), 'stats'):
                BigWorld.player().stats.get('credits', lambda resID, val: g_playerEvents.onClientUpdated({'stats': {'credits': val}}))
                BigWorld.player().stats.get('gold', lambda resID, val: g_playerEvents.onClientUpdated({'stats': {'gold': val}}))
            else:
                g_playerEvents.onClientUpdated({'stats': {'credits': 100000000, 'gold': 1000000}})
        except Exception as e:
            from debug_utils import LOG_CURRENT_EXCEPTION
            LOG_CURRENT_EXCEPTION()
    TechnicalMaintenance.populateUI = _patched_populateUI

    def _loop():
        try:
            g_playerEvents.onClientUpdated({'stats': {'credits': 100000000, 'gold': 1000000}})
        except:
            pass
        import BigWorld
        BigWorld.callback(1.0, _loop)
    import BigWorld
    BigWorld.callback(5.0, _loop)

def patch_shop_requester():
    from gui.Scaleform.utils.requesters import ShopRequester
    ShopRequester.isEnabledBuyingGoldShellsForCredits = property(lambda self: True)
    ShopRequester.isEnabledBuyingGoldEqsForCredits = property(lambda self: True)

patch_shop_requester()

def print_stats_cache():
    import BigWorld
    def _cb(*args):
        from debug_utils import LOG_DEBUG
        LOG_DEBUG('---- CACHE KEYS ----', BigWorld.player().stats._Stats__cache.keys())
        LOG_DEBUG('---- CACHE CREDITS ----', BigWorld.player().stats._Stats__cache.get('credits'))
    _pl = BigWorld.player()
    if _pl is not None and getattr(_pl, 'stats', None) is not None:
        _pl.stats.getCache(_cb)
import BigWorld
BigWorld.callback(3.0, print_stats_cache)

def patch_stats_requesterr_hardcode():
    try:
        from gui.Scaleform.utils.requesters import StatsRequesterr
        StatsRequesterr.credits = property(lambda self: 100000000)
        StatsRequesterr.gold = property(lambda self: 1000000)
    except: pass

patch_stats_requesterr_hardcode()

def patch_resolve_selected_compact_descr():
    try:
        from gui.mods.offhangar import offline_battle_stack
        _orig = offline_battle_stack._resolve_selected_compact_descr
        def _patched(player):
            res = _orig(player)
            if not res:
                from items import vehicles
                nationID = 0
                vehID = vehicles.g_list.getList(nationID).keys()[0]
                res = vehicles.makeIntCompactDescrByID('vehicle', nationID, vehID)
            return res
        offline_battle_stack._resolve_selected_compact_descr = _patched
    except: pass
patch_resolve_selected_compact_descr()

def fix_offline_battle_crashes():
    try:
        from gui.Scaleform.BattleDispatcherInterface import BattleDispatcherInterface
        _orig_onFightButtonClick = BattleDispatcherInterface.onFightButtonClick
        def _patched_onFightButtonClick(self, actionName, prebattleID='', *args, **kwargs):
            from gui.mods.offhangar.logging import LOG_DEBUG
            try:
                import BigWorld
                from gui.mods.mod_offhangar import start_offline_random_from_hangar
                from CurrentVehicle import g_currentVehicle
                invID = g_currentVehicle.vehicle.inventoryId if g_currentVehicle.isPresent() else 1
                LOG_DEBUG('OfflineBattle.bypass enqueue random', actionName, prebattleID, args, kwargs)
                
                # Flash sends (callbackId, mapId) which maps to (actionName, prebattleID) in this signature.
                p = BigWorld.player()
                m_id = None
                
                if isinstance(prebattleID, (int, float)):
                    m_id = int(prebattleID)
                elif isinstance(prebattleID, str) and prebattleID.isdigit():
                    m_id = int(prebattleID)
                
                if m_id is not None:
                    import ArenaType
                    if not getattr(ArenaType, 'g_cache', None):
                        ArenaType.init()
                    if m_id in ArenaType.g_cache:
                        setattr(p, '_offhangar_selected_mapId', m_id)
                        LOG_DEBUG("Set map by ID:", m_id)
                    else:
                        if hasattr(p, '_offhangar_selected_mapId'):
                            delattr(p, '_offhangar_selected_mapId')
                elif isinstance(prebattleID, str) and prebattleID:
                    setattr(p, '_offhangar_selected_mapId', prebattleID)
                    LOG_DEBUG("Set map by name:", prebattleID)
                else:
                    # If this is a regular Battle button click, clear any saved map!
                    if hasattr(p, '_offhangar_selected_mapId'):
                        delattr(p, '_offhangar_selected_mapId')
                
                start_offline_random_from_hangar(p, invID)
                return None
            except Exception as e:
                LOG_DEBUG('OfflineBattle.bypass exception', e)
            return _orig_onFightButtonClick(self, actionName, prebattleID, *args, **kwargs)
        BattleDispatcherInterface.onFightButtonClick = _patched_onFightButtonClick
    except Exception as e:
        pass

    try:
        from gui import BattleContext
        _orig_addVehicleData = BattleContext._BattleContext__addVehicleData
        def _patched_addVehicleData(self, vID, vData):
            vDesc = vData.get('vehicleType')
            if isinstance(vDesc, (int, long)):
                class FakeType:
                    tags = frozenset()
                class FakeDesc:
                    type = FakeType()
                vData['vehicleType'] = FakeDesc()
            return _orig_addVehicleData(self, vID, vData)
        BattleContext._BattleContext__addVehicleData = _patched_addVehicleData
    except Exception as e:
        pass

fix_offline_battle_crashes()

def patch_captcha_view():
    try:
        from gui.Scaleform.CaptchaView import CaptchaView
        
        def _mock_showCaptcha(*args, **kwargs):
            for a in args:
                if callable(a): a()
            for v in kwargs.values():
                if callable(v): v()
        CaptchaView.showCaptcha = _mock_showCaptcha
    except Exception:
        pass
patch_captcha_view()

def patch_stats_requesterr_captcha():
    try:
        from gui.Scaleform.utils.requesters import StatsRequesterr
        def _mock_btc(*args, **kwargs):
            for a in args:
                if callable(a): a(99)
            for k, v in kwargs.items():
                if callable(v): v(99)
            return 99
        StatsRequesterr.battlesTillCaptcha = _mock_btc
    except: pass
patch_stats_requesterr_captcha()

def patch_data_providers_defcost():
    try:
        from gui.scaleform.customization.data_providers import CamouflagesDataProvider, EmblemsDataProvider
        
        _orig_Camo_init = CamouflagesDataProvider.__init__
        def _safe_Camo_init(self, nationID):
            _orig_Camo_init(self, nationID)
            self._CamouflagesDataProvider__defCost = 0.0
        CamouflagesDataProvider.__init__ = _safe_Camo_init
        
        _orig_Emblem_init = EmblemsDataProvider.__init__
        def _safe_Emblem_init(self, *args, **kwargs):
            _orig_Emblem_init(self, *args, **kwargs)
            self._defCost = 0.0
        EmblemsDataProvider.__init__ = _safe_Emblem_init
    except Exception:
        pass

patch_data_providers_defcost()

def debug_construct_object():
    try:
        from gui.scaleform.customization.data_providers import CamouflagesDataProvider
        from debug_utils import LOG_DEBUG
        _orig_construct = CamouflagesDataProvider._constructObject
        
        def _patched_construct(self, cID, groups, camouflages, armorColor, lifeCycle=None, isCurrent=False, withoutCheck=True, currentCompactDescriptor=None):
            camouflageInfo = _orig_construct(self, cID, groups, camouflages, armorColor, lifeCycle, isCurrent, withoutCheck, currentCompactDescriptor)
            
            camouflage = camouflages.get(cID, None)
            if camouflage is None:
                LOG_DEBUG('CAMO DEBUG: cID %s is None in camouflages dict' % cID)
            else:
                showInShop = camouflage.get('showInShop', False)
                denyCompactDescriptor = camouflage.get('deny', [])
                LOG_DEBUG('CAMO DEBUG: cID %s: showInShop=%s, withoutCheck=%s, currentCD=%s, deny=%s' % (
                    cID, showInShop, withoutCheck, currentCompactDescriptor, denyCompactDescriptor
                ))
            
            LOG_DEBUG('CAMO DEBUG: cID %s RESULT: %s' % (cID, camouflageInfo is not None))
            return camouflageInfo
            
        CamouflagesDataProvider._constructObject = _patched_construct
    except Exception as e:
        pass

debug_construct_object()

def debug_onrequestlist():
    try:
        from gui.scaleform.customization.data_providers import CamouflagesDataProvider
        from debug_utils import LOG_DEBUG
        _orig_onrequest = CamouflagesDataProvider.onRequestList
        
        def _patched_onrequest(self, groupName):
            LOG_DEBUG('CAMO DEBUG: onRequestList called with groupName=%s' % groupName)
            result = _orig_onrequest(self, groupName)
            LOG_DEBUG('CAMO DEBUG: onRequestList result len=%s' % len(result))
            return result
            
        CamouflagesDataProvider.onRequestList = _patched_onrequest
    except Exception as e:
        pass

debug_onrequestlist()

def debug_groups_buildlist():
    try:
        from gui.scaleform.customization.data_providers import CamouflageGroupsDataProvider
        from debug_utils import LOG_DEBUG
        _orig_build = CamouflageGroupsDataProvider.buildList
        
        def _patched_build(self):
            import items.vehicles as vehicles
            customization = vehicles.g_cache.customization(self._nationID)
            if customization is None:
                LOG_DEBUG('CAMO DEBUG: customization is None for nationID %s' % self._nationID)
            else:
                groups = customization.get('camouflageGroups', {})
                LOG_DEBUG('CAMO DEBUG: groups count=%s' % len(groups))
                for name, info in groups.iteritems():
                    LOG_DEBUG('CAMO DEBUG: group %s has %s ids' % (name, len(info.get('ids', []))))
            
            return _orig_build(self)
            
        CamouflageGroupsDataProvider.buildList = _patched_build
    except Exception as e:
        pass

debug_groups_buildlist()

def debug_ongetpackagescost():
    try:
        from gui.scaleform.customization.data_providers import RentalPackageDataProviderBase
        from debug_utils import LOG_DEBUG
        _orig_onGetPackagesCost = RentalPackageDataProviderBase._onGetPackagesCost
        
        def _patched_onGetPackagesCost(self, resultID, costs, rev, refresh):
            LOG_DEBUG('CAMO DEBUG: _onGetPackagesCost called! resultID=%s, costs=%s' % (resultID, costs))
            return _orig_onGetPackagesCost(self, resultID, costs, rev, refresh)
            
        RentalPackageDataProviderBase._onGetPackagesCost = _patched_onGetPackagesCost
    except Exception as e:
        pass

debug_ongetpackagescost()

def log_res_cache():
    import BigWorld
    def _do_log():
        try:
            from account_helpers import AccountCommands
            from debug_utils import LOG_DEBUG
            LOG_DEBUG('CAMO DEBUG: RES_CACHE = %s' % getattr(AccountCommands, 'RES_CACHE', 'MISSING'))
            LOG_DEBUG('CAMO DEBUG: RES_SUCCESS = %s' % getattr(AccountCommands, 'RES_SUCCESS', 'MISSING'))
        except Exception as e:
            pass
    BigWorld.callback(1.0, _do_log)

log_res_cache()

def force_shop_costs():
    try:
        from account_helpers.Shop import Shop
        _orig_getCamouflageCost = Shop.getCamouflageCost
        _orig_getPlayerEmblemCost = Shop.getPlayerEmblemCost
        _orig_getPlayerInscriptionCost = Shop.getPlayerInscriptionCost
        
        def _getCamouflageCost(self, callback):
            costs = {7: (50000, False), 30: (100000, False), 0: (100, True)}
            if callback: callback(0, costs, 0)
            
        def _getPlayerEmblemCost(self, callback):
            costs = {7: (50000, False), 30: (100000, False), 0: (100, True)}
            if callback: callback(0, costs, 0)
            
        def _getPlayerInscriptionCost(self, callback):
            costs = {7: (50000, False), 30: (100000, False), 0: (100, True)}
            if callback: callback(0, costs, 0)
            
        Shop.getCamouflageCost = _getCamouflageCost
        Shop.getPlayerEmblemCost = _getPlayerEmblemCost
        Shop.getPlayerInscriptionCost = _getPlayerInscriptionCost
    except Exception as e:
        pass

force_shop_costs()










def patch_techtree():
    try:
        from gui.Scaleform.techtree.data import NationTreeData
        orig_dump = NationTreeData.dump
        def dump_hook(self):
            d = orig_dump(self)
            if self.__class__.__name__ == 'NationTreeData':
                if d.get("scrollIndex", -1) == -1 and len(d.get("nodes", [])) > 0:
                    d["scrollIndex"] = 0
            return d
        NationTreeData.dump = dump_hook
    except Exception as e:
        from debug_utils import LOG_ERROR
        LOG_ERROR("patch_techtree error", e)

patch_techtree()






























































def patch_techtree_get_file():
    try:
        from gui.mods.offhangar import _constants
        with open("C:/Games/World_of_Tanks_0.08.02.00.00_EU_0543_SD/dump_nickname.log", "w") as fout:
            fout.write("NICKNAME: " + str(_constants.OFFLINE_NICKNAME) + "\n")
            fout.write("CONFIG_OPTIONS: " + str(_constants.CONFIG_OPTIONS) + "\n")
    except Exception as e:
        pass

patch_techtree_get_file()
