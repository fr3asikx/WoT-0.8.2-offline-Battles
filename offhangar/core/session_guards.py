# -*- coding: utf-8 -*-
from GameSessionController import _GameSessionController

from gui.mods.offhangar.logging import LOG_DEBUG, LOG_NOTE
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
	"""Patch GameSessionController restrictions that can block battle entry."""
	if globals().get('_offh_game_session_guards_installed', False):
		return
	globals()['_offh_game_session_guards_installed'] = True
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
    if globals().get('_offh_dossier_guard_installed', False):
        return
    try:
        import dossiers
        from gui.mods.offhangar.logging import LOG_DEBUG
        _orig = getattr(dossiers, '_offh_orig_getAccountDossierDescr', dossiers.getAccountDossierDescr)
        dossiers._offh_orig_getAccountDossierDescr = _orig
        def _safe(compDescr=''):
            if compDescr is None:
                compDescr = ''
            d = _orig(compDescr)
            if not compDescr:
                try:
                    from gui.mods.offhangar.data import populate_account_dossier_descr
                    populate_account_dossier_descr(d)
                except Exception:
                    pass
            return d
        dossiers.getAccountDossierDescr = _safe
        if hasattr(dossiers, 'getVehicleDossierDescr'):
            _orig_veh = getattr(dossiers, '_offh_orig_getVehicleDossierDescr', dossiers.getVehicleDossierDescr)
            dossiers._offh_orig_getVehicleDossierDescr = _orig_veh
            def _safe_veh(compDescr='', veh_cd=None):
                if compDescr is None:
                    compDescr = ''
                d = _orig_veh(compDescr)
                if not compDescr:
                    try:
                        _vcd = veh_cd if veh_cd is not None else getattr(dossiers, '_offh_last_veh_cd', None)
                        if _vcd is not None:
                            from gui.mods.offhangar.data import populate_vehicle_dossier_descr
                            populate_vehicle_dossier_descr(d, veh_cd=_vcd)
                    except Exception:
                        pass
                return d
            dossiers.getVehicleDossierDescr = _safe_veh
        try:
            from gui.Scaleform.utils.requesters import StatsRequester as _SR_doss
            if hasattr(_SR_doss, 'getVehicleDossier') and not getattr(_SR_doss, '_offh_veh_doss_tracked', False):
                _orig_sr_gvd = _SR_doss.getVehicleDossier
                def _tracked_getVehicleDossier(self, vehTypeCompDescr, *a, **kw):
                    try:
                        dossiers._offh_last_veh_cd = int(vehTypeCompDescr or 0)
                    except Exception:
                        pass
                    return _orig_sr_gvd(self, vehTypeCompDescr, *a, **kw)
                _SR_doss.getVehicleDossier = _tracked_getVehicleDossier
                _SR_doss._offh_veh_doss_tracked = True
        except Exception:
            pass
        
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
            try:
                if str(message) == 'updateCurrentVehicle':
                    import traceback
                    LOG_NOTE('WAITING updateCurrentVehicle HIDE caller=%s' %
                        ' <- '.join(traceback.format_stack()[-6:-1]).replace('\\n', ' | ')[:900])
            except Exception:
                pass
            LOG_DEBUG('Waiting.hide called for:', message)
            result = orig_waiting_hide(message)
            try:
                if str(message) == 'updateCurrentVehicle':
                    _stack = getattr(Waiting, '_Waiting__waitingStack', None) or []
                    if not _stack:
                        import BigWorld
                        BigWorld.Screener.setEnabled(True)
                        LOG_NOTE('WAITING updateCurrentVehicle screener restored')
            except Exception:
                pass
            return result
        Waiting.hide = staticmethod(waiting_hide_hook)

        orig_waiting_show = Waiting.show
        def waiting_show_hook(message, *args, **kwargs):
            return orig_waiting_show(message, *args, **kwargs)
        Waiting.show = staticmethod(waiting_show_hook)
        _orig_waiting_close = getattr(Waiting, 'close', None)
        if not getattr(Waiting, '_offh_fit_wait_traced', False):
            _orig_fit_show = getattr(Waiting, '_offh_orig_fit_show', Waiting.show)
            _orig_fit_hide = getattr(Waiting, '_offh_orig_fit_hide', Waiting.hide)
            _fit_tokens = ('updateFitting', 'updateAmmo', 'updateTankmen', 'updateVehicle', 'updateMyVehicles')
            def _fit_show_trace(message, *args, **kwargs):
                if str(message) in _fit_tokens:
                    try:
                        import BigWorld as _bw_wd
                        def _auto_dismiss(tok=str(message)):
                            try:
                                from gui.Scaleform.Waiting import Waiting as _W_wd
                                st = getattr(_W_wd, '_Waiting__waitingStack', None) or []
                                if tok in st:
                                    _W_wd.hide(tok)
                                    if not getattr(_W_wd, '_Waiting__waitingStack', None):
                                        _W_wd.close()
                            except Exception:
                                pass
                        _bw_wd.callback(1.2, _auto_dismiss)
                    except Exception:
                        pass
                return _orig_fit_show(message, *args, **kwargs)
            def _fit_hide_trace(message, *args, **kwargs):
                return _orig_fit_hide(message, *args, **kwargs)
            Waiting.show = staticmethod(_fit_show_trace)
            Waiting.hide = staticmethod(_fit_hide_trace)
            Waiting._offh_fit_wait_traced = True
        _orig_waiting_close = getattr(Waiting, 'close', None)
        if callable(_orig_waiting_close):
            def _waiting_close_trace(*args, **kwargs):
                try:
                    _stack = getattr(Waiting, '_Waiting__waitingStack', None)
                    LOG_NOTE('WAITING close called stack=%s' % (list(_stack or []),))
                except Exception:
                    pass
                return _orig_waiting_close(*args, **kwargs)
            Waiting.close = staticmethod(_waiting_close_trace)
        Waiting._offh_vehicle_waiting_patched = True
        
        LOG_DEBUG('SessionGuard.dossier_guard_installed')
    except Exception:
        from debug_utils import LOG_CURRENT_EXCEPTION
        from gui.mods.offhangar.logging import LOG_NOTE
        LOG_CURRENT_EXCEPTION()
        try:
            from gui.Scaleform.Waiting import Waiting
            if not getattr(Waiting, '_offh_vehicle_waiting_patched', False):
                _orig_hide = Waiting.hide
                def _waiting_hide_trace_only(message):
                    if str(message) == 'updateCurrentVehicle':
                        import traceback
                        LOG_NOTE('WAITING updateCurrentVehicle HIDE caller=%s' %
                            ' <- '.join(traceback.format_stack()[-6:-1]).replace('\\n', ' | ')[:900])
                    return _orig_hide(message)
                Waiting.hide = staticmethod(_waiting_hide_trace_only)
                Waiting._offh_vehicle_waiting_patched = True
                LOG_NOTE('WAITING trace-only fallback installed')
        except Exception:
            LOG_CURRENT_EXCEPTION()
        return
    globals()['_offh_dossier_guard_installed'] = True

# Imported by mod_offhangar._safe_import_offhangar before the client bootstrap
# continues. The module-level call is the single owner of guard installation.
install_dossier_guard()


def install_sync_data_guard():
    """Patch AccountSyncData.waitForSync for the offline cache."""
    if globals().get('_offh_sync_data_guard_installed', False):
        return
    globals()['_offh_sync_data_guard_installed'] = True
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
    """Inject safe defaults for stats still missing after cache lookup."""
    if globals().get('_offh_stats_defaults_guard_installed', False):
        return
    globals()['_offh_stats_defaults_guard_installed'] = True
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
            'attrs': 65534,
            'premiumExpiryTime': 2000000000,
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
    if globals().get('_offh_customization_guard_installed', False):
        return
    globals()['_offh_customization_guard_installed'] = True
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
    """Install offline cache responders once, after the canonical package imports."""
    if globals().get('_offh_sync_guards_installed', False):
        return
    globals()['_offh_sync_guards_installed'] = True
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
                    try:
                        if class_name == 'Shop' and not getattr(self, '_Shop__cache', None):
                            from gui.mods.offhangar.data import getOfflineShopCacheData
                            self._Shop__cache = getOfflineShopCacheData()
                        elif class_name == 'Inventory' and not getattr(self, '_Inventory__cache', None):
                            from gui.mods.offhangar.data import getOfflineInventory
                            self.synchronize(True, getOfflineInventory())
                        elif class_name == 'Stats' and not getattr(self, '_Stats__cache', None):
                            from gui.mods.offhangar.data import getOfflineStats
                            _sts = getOfflineStats()
                            normalize_offline_stats(_sts.get('stats', {}))
                            self.synchronize(True, _sts)
                    except Exception:
                        pass
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
        try:
            _orig_getValue = getattr(Shop, '_Shop__getValue', None)
            if callable(_orig_getValue) and not getattr(Shop, '_offh_getvalue_patched', False):
                def _patched_getValue(self, key, callback):
                    if getattr(self, '_Shop__isSynchronizing', False):
                        setattr(self, '_Shop__isSynchronizing', False)
                    return _orig_getValue(self, key, callback)
                Shop._Shop__getValue = _patched_getValue
                Shop._offh_getvalue_patched = True
        except Exception:
            pass
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
    ShopRequester.isEnabledBuyingGoldShellsForCredits = property(lambda self: True, lambda self, val: None)
    ShopRequester.isEnabledBuyingGoldEqsForCredits = property(lambda self: True, lambda self, val: None)

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
        from gui.Scaleform.utils.requesters import StatsRequester as StatsRequesterr
        StatsRequesterr.credits = property(lambda self: 100000000, lambda self, val: None)
        StatsRequesterr.gold = property(lambda self: 1000000, lambda self, val: None)
    except: pass

patch_stats_requesterr_hardcode()

def install_purchase_trace():
    """Pinpoint the purchase-crash window (0xC66B43 right after the requester
    callbacks, before any Server.doCmd line - 07.10 and 13.10): log entry into
    Shop.buy* and into PlayerAccount._doCmdInt4/_doCmdIntArr, so the repro log
    shows exactly which call never returns. Log-only, never raises."""
    try:
        from gui.mods.offhangar.logging import LOG_NOTE
        import Account
        from account_helpers import Shop as _shop_mod
        for _mname in ('buy', 'buyAndEquipItem', 'buyVehicle', 'buyTankman'):
            _orig = getattr(_shop_mod.Shop, _mname, None)
            if _orig is None:
                continue
            def _make(_orig, _mname):
                def _hook(self, *args, **kwargs):
                    LOG_NOTE('SHOP.%s enter args=%s' % (_mname, str(args)[:120]))
                    return _orig(self, *args, **kwargs)
                return _hook
            setattr(_shop_mod.Shop, _mname, _make(_orig, _mname))
        _pa = getattr(Account, 'PlayerAccount', None)
        if _pa is not None:
            for _dname in ('_doCmdInt4', '_doCmdIntArr', '_doCmdInt3', '_doCmdInt2Str', '_doCmdIntArrStrArr', '_doCmdStr'):
                _dorig = getattr(_pa, _dname, None)
                if _dorig is None:
                    continue
                def _dmake(_dorig, _dname):
                    def _dhook(self, cmd, *args, **kwargs):
                        return _dorig(self, cmd, *args, **kwargs)
                    return _dhook
                setattr(_pa, _dname, _dmake(_dorig, _dname))
        LOG_NOTE('PURCHASE_TRACE installed')
    except Exception:
        pass

install_purchase_trace()

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
                from gui.mods.offhangar import cbt_profiles_and_locale as _cbt_f
                _cbt_f.close_cbt_welcome_modal()
            except Exception:
                pass
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
                elif isinstance(prebattleID, str) and (prebattleID.isdigit() or prebattleID.strip() in ('-999', '-998')):
                    m_id = int(prebattleID.strip())
                if m_id == -999 or str(actionName) == '-999':
                    try:
                        from gui.mods.offhangar import offline_battle as _ob_mp
                        _ob_mp._offh_toggle_scaleform_map_picker()
                    except Exception:
                        pass
                    return None
                if m_id == -998 or str(actionName) == '-998':
                    try:
                        from gui.mods.offhangar import offline_battle as _ob_mp
                        _ob_mp._offh_cycle_bot_difficulty_from_hangar()
                    except Exception:
                        pass
                    return None
                
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
                    # Preserve map selected via the Native GUI Map Picker Window if active
                    _picker_mid_sg = None
                    try:
                        from gui.mods.offhangar import offline_battle as _ob_sg
                        _picker_mid_sg = (_ob_sg._OFFH_MAP_PICKER_STATE or {}).get('selected_map_id')
                    except Exception:
                        _picker_mid_sg = None
                    if _picker_mid_sg is not None:
                        setattr(p, '_offhangar_selected_mapId', int(_picker_mid_sg))
                    elif hasattr(p, '_offhangar_selected_mapId'):
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
        from gui.Scaleform.utils.requesters import StatsRequester as StatsRequesterr
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
        with open("./dump_nickname.log", "w") as fout:
            fout.write("NICKNAME: " + str(_constants.OFFLINE_NICKNAME) + "\n")
            fout.write("CONFIG_OPTIONS: " + str(_constants.CONFIG_OPTIONS) + "\n")
    except Exception as e:
        pass

# patch_techtree_get_file() disabled for CBT release


def _offh_cleanup_stale_pyc():
    try:
        import os as _os_pyc
        _here = _os_pyc.path.dirname(_os_pyc.path.abspath(__file__))
        _root = _os_pyc.path.dirname(_here) if _os_pyc.path.basename(_here) in ('core', 'ai', 'combat', 'physics', 'native', 'network', 'ui') else _here
        _scan_dirs = [_root]
        for _sub in ('core', 'ai', 'combat', 'physics', 'native', 'network', 'ui'):
            _sd = _os_pyc.path.join(_root, _sub)
            if _os_pyc.path.isdir(_sd):
                _scan_dirs.append(_sd)
        for _d in _scan_dirs:
            for _fn in _os_pyc.listdir(_d):
                if _fn.endswith('.pyc') and _fn not in ('visual_suspension.pyc', 'offh_native_math.pyc'):
                    _py = _os_pyc.path.join(_d, _fn[:-1])
                    if _os_pyc.path.exists(_py):
                        try:
                            _os_pyc.remove(_os_pyc.path.join(_d, _fn))
                        except Exception:
                            pass
    except Exception:
        pass

_offh_cleanup_stale_pyc()



_OFFH_DEFAULT_CMD_KEYS = (
    ('CMD_MOVE_FORWARD', 'KEY_W', ()),
    ('CMD_MOVE_FORWARD_SPEC', 'KEY_W', ('KEY_LALT',)),
    ('CMD_MOVE_BACKWARD', 'KEY_S', ()),
    ('CMD_ROTATE_LEFT', 'KEY_A', ()),
    ('CMD_ROTATE_RIGHT', 'KEY_D', ()),
    ('CMD_INCREMENT_CRUISE_MODE', 'KEY_R', ()),
    ('CMD_DECREMENT_CRUISE_MODE', 'KEY_F', ()),
    ('CMD_STOP_UNTIL_FIRE', 'KEY_SPACE', ()),
    ('CMD_SWITCH_SERVER_MARKER', 'KEY_0', ('KEY_LALT',)),
    ('CMD_AMMO_CHOICE_1', 'KEY_1', ()),
    ('CMD_AMMO_CHOICE_2', 'KEY_2', ()),
    ('CMD_AMMO_CHOICE_3', 'KEY_3', ()),
    ('CMD_AMMO_CHOICE_4', 'KEY_4', ()),
    ('CMD_AMMO_CHOICE_5', 'KEY_5', ()),
    ('CMD_AMMO_CHOICE_6', 'KEY_6', ()),
    ('CMD_AMMO_CHOICE_7', 'KEY_7', ()),
    ('CMD_AMMO_CHOICE_8', 'KEY_8', ()),
    ('CMD_AMMO_CHOICE_9', 'KEY_9', ()),
    ('CMD_AMMO_CHOICE_0', 'KEY_0', ()),
    ('CMD_CHAT_SHORTCUT_ATTACK', 'KEY_T', ()),
    ('CMD_CHAT_SHORTCUT_BACKTOBASE', 'KEY_F2', ()),
    ('CMD_CHAT_SHORTCUT_FOLLOWME', 'KEY_F3', ()),
    ('CMD_CHAT_SHORTCUT_POSITIVE', 'KEY_F5', ()),
    ('CMD_CHAT_SHORTCUT_NEGATIVE', 'KEY_F6', ()),
    ('CMD_CHAT_SHORTCUT_HELPME', 'KEY_F7', ()),
    ('CMD_CHAT_SHORTCUT_RELOAD', 'KEY_F8', ()),
    ('CMD_RADIAL_MENU_SHOW', 'KEY_Z', ()),
    ('CMD_CM_SHOOT', 'KEY_LEFTMOUSE', ()),
    ('CMD_CM_SWITCH_TRAJECTORY', 'KEY_G', ()),
    ('CMD_CM_FREE_CAMERA', 'KEY_RIGHTMOUSE', ()),
    ('CMD_CM_LOCK_TARGET', 'KEY_RIGHTMOUSE', ()),
    ('CMD_CM_LOCK_TARGET_OFF', 'KEY_E', ()),
    ('CMD_CM_CAMERA_ROTATE_LEFT', 'KEY_LEFTARROW', ()),
    ('CMD_CM_CAMERA_ROTATE_RIGHT', 'KEY_RIGHTARROW', ()),
    ('CMD_CM_CAMERA_ROTATE_UP', 'KEY_UPARROW', ()),
    ('CMD_CM_CAMERA_ROTATE_DOWN', 'KEY_DOWNARROW', ()),
    ('CMD_CM_VEHICLE_SWITCH_AUTOROTATION', 'KEY_X', ()),
    ('CMD_CM_INCREASE_ZOOM', 'KEY_PGUP', ()),
    ('CMD_CM_DECREASE_ZOOM', 'KEY_PGDN', ()),
    ('CMD_CM_ALTERNATE_MODE', 'KEY_LSHIFT', ()),
    ('CMD_CM_CAMERA_RESTORE_DEFAULT', 'KEY_NUMPAD5', ()),
    ('CMD_CM_POSTMORTEM_NEXT_VEHICLE', 'KEY_LEFTMOUSE', ()),
    ('CMD_CM_POSTMORTEM_SELF_VEHICLE', 'KEY_RIGHTMOUSE', ()),
    ('CMD_VEHICLE_MARKERS_SHOW_INFO', 'KEY_LALT', ()),
    ('CMD_AMMO_PANEL_SELECT', 'KEY_TAB', ()),
    ('CMD_MINIMAP_HIGHLIGHT_CELL', 'KEY_LEFTMOUSE', ('KEY_LCONTROL',)),
    ('CMD_MINIMAP_VEHICLE_SPECIFIC', 'KEY_RIGHTMOUSE', ('KEY_LCONTROL',)),
    ('CMD_MINIMAP_SIZE_UP', 'KEY_EQUALS', ()),
    ('CMD_MINIMAP_SIZE_DOWN', 'KEY_MINUS', ()),
    ('CMD_MINIMAP_VISIBLE', 'KEY_M', ()),
    ('CMD_VOICECHAT_MUTE', 'KEY_Q', ()),
    ('CMD_USE_HORN', 'KEY_H', ()),
    ('CMD_LOGITECH_SWITCH_VIEW', 'KEY_N', ()),
    ('CMD_TOGGLE_GUI', 'KEY_V', ()),
    ('CMD_RELOAD_PARTIAL_CLIP', 'KEY_C', ()),
)


def _offh_repair_command_mapping():
    """Repair any KEY_NONE (0) or missing bindings in CommandMapping.g_instance
    caused by opening/applying Settings -> Controls in offline mode."""
    try:
        import CommandMapping
        import Keys
        import Settings
        from gui.mods.offhangar.logging import LOG_NOTE
        cm = getattr(CommandMapping, 'g_instance', None)
        if cm is None:
            return 0
        repaired = []
        mapping = getattr(cm, '_CommandMapping__mapping', None)
        if isinstance(mapping, dict):
            # Remove any bucket keyed by 0 (KEY_NONE) or None
            for bad_k in (0, getattr(Keys, 'KEY_NONE', 0), None):
                if bad_k in mapping:
                    mapping.pop(bad_k, None)
        for cmd_name, def_fire, def_sats in _OFFH_DEFAULT_CMD_KEYS:
            if not hasattr(CommandMapping, cmd_name):
                continue
            cur_key = None
            try:
                cur_key = cm.get(cmd_name)
            except Exception:
                cur_key = None
            # For commands with satellite keys (where cm.get returns None by design), check mapping directly
            if cur_key is None and len(def_sats) > 0 and isinstance(mapping, dict):
                cmd_id = cm.getCommand(cmd_name)
                found_sat = False
                for fk, lki in mapping.iteritems():
                    if fk and fk != getattr(Keys, 'KEY_NONE', 0):
                        for ki in lki:
                            if ki[0] == cmd_id:
                                found_sat = True
                                break
                    if found_sat:
                        break
                if found_sat:
                    continue
            if cur_key is None or int(cur_key) <= 0:
                try:
                    cm.remove(cmd_name)
                except Exception:
                    pass
                try:
                    cm.add(cmd_name, def_fire, list(def_sats), True)
                    repaired.append('%s->%s' % (cmd_name, def_fire))
                except Exception:
                    pass
        if repaired:
            try:
                up = getattr(getattr(Settings, 'g_instance', None), 'userPrefs', None)
                if up is not None and up.has_key('commandMapping'):
                    sec = up['commandMapping']
                    for sub_name in list(sec.keys()):
                        sub = sec[sub_name]
                        fk_s = sub.readString('fireKey', '')
                        if not fk_s or fk_s in ('KEY_NONE', 'NONE', 'KEY_0', '0'):
                            sec.deleteSection(sub_name)
                cm.save()
            except Exception:
                pass
            LOG_NOTE('CMD_MAPPING_REPAIR restored %d unbound/KEY_NONE commands: %s' % (
                len(repaired), ', '.join(repaired[:10])))
        return len(repaired)
    except Exception:
        return 0


def _offh_sync_window_frame(force_windowed=None):
    """Restore WS_OVERLAPPEDWINDOW (title bar + minimize/maximize/close buttons) and work-area
    bounds when leaving Fullscreen mode under DXVK/D3D9, and release any cursor clip."""
    try:
        import os as _os_wf
        if _os_wf.environ.get('OFFHANGAR_WORKER_MODE') == 'authority_worker':
            return
        import BigWorld
        from gui.mods.offhangar import offh_multithread as _omt_wf
        coord = _omt_wf.get_coordinator()
        is_win = force_windowed if force_windowed is not None else bool(BigWorld.isVideoWindowed())
        req_w, req_h = 0, 0
        try:
            res = BigWorld.wg_getCurrentResolution(True)
            if res and len(res) >= 2:
                req_w, req_h = int(res[0]), int(res[1])
        except Exception:
            pass
        if is_win:
            cw, ch = coord.fix_window_mode(True, req_w, req_h)
            if cw >= 640 and ch >= 480 and (cw != req_w or ch != req_h):
                try:
                    _orig_rw = getattr(BigWorld, '_offh_orig_resizeWindow', BigWorld.resizeWindow)
                    _orig_rw(cw, ch)
                except Exception:
                    pass
        else:
            coord.fix_window_mode(False, req_w, req_h)
        try:
            import GUI
            mc = GUI.mcursor() if hasattr(GUI, 'mcursor') else None
            if mc is not None:
                mc.visible = False
                if hasattr(mc, 'clipped'):
                    mc.clipped = False
        except Exception:
            pass
    except Exception:
        pass


def _offh_window_watchdog():
    try:
        import os as _os_ww
        if _os_ww.environ.get('OFFHANGAR_WORKER_MODE') == 'authority_worker':
            return
        import BigWorld
        if bool(BigWorld.isVideoWindowed()):
            from gui.mods.offhangar import offh_multithread as _omt_ww
            res = (0, 0)
            try:
                r = BigWorld.wg_getCurrentResolution(True)
                if r and len(r) >= 2:
                    res = (int(r[0]), int(r[1]))
            except Exception:
                pass
            _omt_ww.get_coordinator().ensure_windowed_style(res[0], res[1])
    except Exception:
        pass
    try:
        import BigWorld
        BigWorld.callback(1.0, _offh_window_watchdog)
    except Exception:
        pass


class _DummyFlashDialogMember(object):
    """Safe stand-in when SettingsInterface looks up popup23/popup3 after the user clicked OK."""
    def __getattr__(self, name):
        return lambda *a, **kw: None
    def invoke(self, *a, **kw):
        return None
    def setMovieVariable(self, *a, **kw):
        return None


def install_video_mode_and_settings_guard():
    """Hook BigWorld.changeVideoMode, BigWorld.resizeWindow, and SettingsInterface so exiting
    fullscreen mode always restores window borders/buttons and never locks UI on missing popup."""
    try:
        import os as _os_vg
        if _os_vg.environ.get('OFFHANGAR_WORKER_MODE') == 'authority_worker':
            return
        import BigWorld
        from gui.mods.offhangar.logging import LOG_NOTE

        if not getattr(BigWorld, '_offh_videomode_hooked', False):
            _orig_cvm = BigWorld.changeVideoMode
            _orig_rw = BigWorld.resizeWindow
            BigWorld._offh_orig_changeVideoMode = _orig_cvm
            BigWorld._offh_orig_resizeWindow = _orig_rw

            def _wrapped_changeVideoMode(modeIndex, windowed):
                is_win = bool(windowed)
                try:
                    if is_win:
                        from gui.mods.offhangar import offh_multithread as _omt_pre
                        _omt_pre.get_coordinator().fix_window_mode(True, 0, 0)
                except Exception:
                    pass
                try:
                    _orig_cvm(modeIndex, windowed)
                except Exception as _cvm_e:
                    LOG_NOTE('VIDEO_MODE changeVideoMode err: %s' % str(_cvm_e))
                _offh_sync_window_frame(is_win)
                try:
                    BigWorld.callback(0.05, lambda: _offh_sync_window_frame(is_win))
                    BigWorld.callback(0.20, lambda: _offh_sync_window_frame(is_win))
                    BigWorld.callback(0.50, lambda: _offh_sync_window_frame(is_win))
                except Exception:
                    pass
                try:
                    import Settings
                    if getattr(Settings, 'g_instance', None) is not None:
                        Settings.g_instance.save()
                    BigWorld.savePreferences()
                except Exception:
                    pass

            def _wrapped_resizeWindow(width, height):
                try:
                    _orig_rw(width, height)
                except Exception:
                    pass
                try:
                    if bool(BigWorld.isVideoWindowed()):
                        from gui.mods.offhangar import offh_multithread as _omt_rw
                        _omt_rw.get_coordinator().ensure_windowed_style(int(width), int(height))
                        BigWorld.callback(0.05, lambda: _offh_sync_window_frame(True))
                except Exception:
                    pass

            BigWorld.changeVideoMode = _wrapped_changeVideoMode
            BigWorld.resizeWindow = _wrapped_resizeWindow
            BigWorld._offh_videomode_hooked = True
            BigWorld.callback(1.0, _offh_window_watchdog)
            LOG_DEBUG('VIDEO_MODE_GUARD installed')
    except Exception:
        pass

    try:
        try:
            from account_helpers.AccountSettings import AccountSettings as _AS_boot
            _ARC_KEYS = ('centralTag', 'net', 'reloader', 'condition', 'cassette', 'gunTag', 'mixing')
            _SNP_KEYS = ('snpCentralTag', 'snpNet', 'snpReloader', 'snpCondition', 'snpCassette', 'snpGunTag', 'snpMixing')
            _ARC_TO_SNP = dict(zip(_ARC_KEYS, _SNP_KEYS))
            _SNP_TO_ARC = dict(zip(_SNP_KEYS, _ARC_KEYS))

            class _OffhAimSettingsDict(dict):
                """Dict wrapper that exposes the exact vanilla keys on iteration (for SettingsInterface)
                while resolving both snp* and unprefixed keys on lookup (for SniperAim / ArcadeAim)."""
                def __getitem__(self, key):
                    if dict.__contains__(self, key):
                        return dict.__getitem__(self, key)
                    alt = _SNP_TO_ARC.get(key) or _ARC_TO_SNP.get(key)
                    if alt and dict.__contains__(self, alt):
                        return dict.__getitem__(self, alt)
                    return {'alpha': 100, 'type': 0}

                def get(self, key, default=None):
                    if dict.__contains__(self, key):
                        return dict.__getitem__(self, key)
                    alt = _SNP_TO_ARC.get(key) or _ARC_TO_SNP.get(key)
                    if alt and dict.__contains__(self, alt):
                        return dict.__getitem__(self, alt)
                    return default if default is not None else {'alpha': 100, 'type': 0}

            def _offh_sanitize_aim_section(name, raw_dict):
                if not isinstance(raw_dict, dict):
                    raw_dict = {}
                want_keys = _SNP_KEYS if name == 'sniper' else _ARC_KEYS
                other_to_want = _ARC_TO_SNP if name == 'sniper' else _SNP_TO_ARC
                dirty = False
                for k in list(raw_dict.keys()):
                    if k in other_to_want:
                        wk = other_to_want[k]
                        if wk not in raw_dict and isinstance(raw_dict.get(k), dict):
                            raw_dict[wk] = dict(raw_dict[k])
                        raw_dict.pop(k, None)
                        dirty = True
                    elif k not in want_keys:
                        raw_dict.pop(k, None)
                        dirty = True
                for wk in want_keys:
                    if wk not in raw_dict or not isinstance(raw_dict.get(wk), dict):
                        raw_dict[wk] = {'alpha': 100, 'type': 0}
                        dirty = True
                return _OffhAimSettingsDict(raw_dict), dirty

            _orig_as_get = getattr(_AS_boot, 'getSettings', None)
            if getattr(_orig_as_get, '_offh_wrapped', False):
                _orig_as_get = _orig_as_get._offh_orig
            if callable(_orig_as_get):
                def _safe_as_getSettings(name, *a, **kw):
                    res = _orig_as_get(name, *a, **kw)
                    if name in ('arcade', 'sniper'):
                        clean_res, dirty = _offh_sanitize_aim_section(name, res)
                        if isinstance(res, dict):
                            res.clear()
                            res.update(clean_res)
                        if dirty:
                            try:
                                _AS_boot.setSettings(name, dict(clean_res))
                            except Exception:
                                pass
                        return clean_res
                    return res
                _safe_as_getSettings._offh_wrapped = True
                _safe_as_getSettings._offh_orig = _orig_as_get
                _AS_boot.getSettings = staticmethod(_safe_as_getSettings)
                # Clean both sections and repair CommandMapping immediately at boot
                _AS_boot.getSettings('arcade')
                _AS_boot.getSettings('sniper')
                _offh_repair_command_mapping()
        except Exception:
            pass

        from gui.Scaleform.SettingsInterface import SettingsInterface as _SI
        if not getattr(_SI, '_offh_si_patched', False):
            _orig_pop = getattr(_SI, 'populateUI', None)
            if _orig_pop is not None:
                def _wrapped_si_populateUI(self, proxy):
                    try:
                        from account_helpers.AccountSettings import AccountSettings as _AS_clean
                        _AS_clean.getSettings('arcade')
                        _AS_clean.getSettings('sniper')
                    except Exception:
                        pass
                    _orig_pop(self, proxy)
                    try:
                        uh = getattr(self, 'uiHolder', None)
                        if uh is not None and not getattr(uh, '_offh_gm_wrapped', False):
                            _orig_gm = uh.getMember
                            def _safe_getMember(name, _ogm=_orig_gm):
                                m = _ogm(name)
                                if m is None and str(name).startswith('popup'):
                                    return _DummyFlashDialogMember()
                                return m
                            uh.getMember = _safe_getMember
                            uh._offh_gm_wrapped = True
                    except Exception:
                        pass
                _SI.populateUI = _wrapped_si_populateUI

            for _mname in ('onSettingsChange', '_SettingsInterface__onSettingsChange', 'onRecreateDevice', '_SettingsInterface__onRecreateDevice', 'altEnter'):
                _orig_m = getattr(_SI, _mname, None)
                if callable(_orig_m):
                    def _make_si_wrap(_fn):
                        def _si_wrap(self, *a, **kw):
                            try:
                                uh = getattr(self, 'uiHolder', None)
                                if uh is not None and not getattr(uh, '_offh_gm_wrapped', False):
                                    _orig_gm = uh.getMember
                                    def _safe_getMember(name, _ogm=_orig_gm):
                                        m = _ogm(name)
                                        if m is None and str(name).startswith('popup'):
                                            return _DummyFlashDialogMember()
                                        return m
                                    uh.getMember = _safe_getMember
                                    uh._offh_gm_wrapped = True
                            except Exception:
                                pass
                            res = _fn(self, *a, **kw)
                            try:
                                _offh_repair_command_mapping()
                            except Exception:
                                pass
                            try:
                                import BigWorld
                                if _mname in ('onRecreateDevice', '_SettingsInterface__onRecreateDevice', 'altEnter'):
                                    _offh_sync_window_frame()
                                    BigWorld.callback(0.1, _offh_sync_window_frame)
                                    BigWorld.callback(0.3, _offh_sync_window_frame)
                            except Exception:
                                pass
                            return res
                        return _si_wrap
                    setattr(_SI, _mname, _make_si_wrap(_orig_m))
            _SI._offh_si_patched = True
    except Exception:
        pass


def install_hangar_system_messages_guard():
    """Prevent Scaleform NULL-deref crash 0xC0000005 @ 0x00C66B43 when stock
    Shop / Research / Fitting / TechnicalMaintenance / Screenshot calls
    SystemMessages.pushMessage or SystemMessages.pushI18nMessage in offline Hangar."""
    if globals().get('_offh_hangar_sysmsg_guard_installed', False):
        return
    globals()['_offh_hangar_sysmsg_guard_installed'] = True
    install_video_mode_and_settings_guard()
    try:
        import sys as _sys_sm
        from gui import SystemMessages as _SM
        from gui.mods.offhangar.logging import LOG_NOTE

        def _safe_pushMessage(text, type=None, priority=None, *args, **kwargs):
            try:
                _txt_s = str(text or '')
                if ('install_error' in _txt_s) or ('Не удалось установить' in _txt_s):
                    return None
                _retry = int(kwargs.pop('_offh_retry', 0)) if isinstance(kwargs, dict) else 0
            except Exception:
                _txt_s = ''
                _retry = 0
            try:
                from gui.WindowsManager import g_windowsManager
                _win = getattr(g_windowsManager, 'window', None)
                _iface = getattr(_win, 'currentInterface', None) if _win is not None else None
                if _iface != 'hangar':
                    if _retry < 10:
                        import BigWorld
                        _kw = dict(kwargs) if isinstance(kwargs, dict) else {}
                        _kw['_offh_retry'] = _retry + 1
                        BigWorld.callback(0.8, lambda: _safe_pushMessage(text, type, priority, *args, **_kw))
                    return None
                _push_fn = getattr(_SM, '_offh_push_native', None) or getattr(_SM, '_offh_orig_pushMessage', None)
                if _push_fn is not None:
                    try:
                        return _push_fn(text, type)
                    except TypeError:
                        return _push_fn(text, type, priority, *args)
            except Exception as _e:
                try:
                    log_note('SYSMSG push fallback: %s' % (_e,))
                except Exception:
                    pass
            return None

        def _safe_pushI18nMessage(key, *args, **kwargs):
            try:
                _k_str = str(key or '')
                if 'charge/inventory_error' in _k_str:
                    return None
                LOG_NOTE('SYSMSG_I18N intercepted: %s' % _k_str[:120])
                from helpers import i18n
                _sm_type = kwargs.pop('type', None)
                _prio = kwargs.pop('priority', None)
                _txt = i18n.makeString(key, *args, **kwargs)
                if _txt and not str(_txt).startswith('#system_messages:charge/inventory_error'):
                    return _safe_pushMessage(_txt, _sm_type, _prio)
            except Exception:
                pass
            return None

        def _safe_smi_pushMessage(self, text, type=None, priority=None, *args, **kwargs):
            return _safe_pushMessage(text, type, priority, *args, **kwargs)

        def _safe_smi_pushI18nMessage(self, key, *args, **kwargs):
            return _safe_pushI18nMessage(key, *args, **kwargs)

        _g_inst = getattr(_SM, 'g_instance', None)
        if not hasattr(_SM, '_offh_orig_pushMessage'):
            _orig_pm = getattr(_g_inst, 'pushMessage', None) if _g_inst is not None else getattr(_SM, 'pushMessage', None)
            if callable(_orig_pm) and getattr(_orig_pm, '__name__', '') != '_safe_pushMessage':
                _SM._offh_orig_pushMessage = _orig_pm
        _SM.pushMessage = _safe_pushMessage
        _SM.pushI18nMessage = _safe_pushI18nMessage
        if _g_inst is not None:
            try:
                if not hasattr(_SM, '_offh_orig_pushMessage') and callable(getattr(_g_inst, 'pushMessage', None)):
                    if getattr(_g_inst.pushMessage, '__name__', '') != '_safe_pushMessage':
                        _SM._offh_orig_pushMessage = _g_inst.pushMessage
                _g_inst.pushMessage = _safe_pushMessage
                _g_inst.pushI18nMessage = _safe_pushI18nMessage
            except Exception:
                pass
        try:
            from gui.Scaleform.SystemMessagesInterface import SystemMessagesInterface as _SMI
            if not hasattr(_SMI, '_offh_orig_smi_pushMessage'):
                _orig_smi_pm = getattr(_SMI, 'pushMessage', None)
                if callable(_orig_smi_pm) and getattr(_orig_smi_pm, '__name__', '') != '_safe_smi_pushMessage':
                    _SMI._offh_orig_smi_pushMessage = _orig_smi_pm
            _SMI.pushMessage = _safe_smi_pushMessage
            _SMI.pushI18nMessage = _safe_smi_pushI18nMessage
            _SMI._offh_sysmsg_guarded = True
            def _offh_push_native(text, sm_type=None):
                try:
                    _inst = getattr(_SM, 'g_instance', None)
                    _fn = getattr(_SMI, '_offh_orig_smi_pushMessage', None)
                    if _inst is not None and callable(_fn):
                        if sm_type is None:
                            sm_type = getattr(_SM.SM_TYPE, 'GameGreeting', getattr(_SM.SM_TYPE, 'Information', getattr(_SM.SM_TYPE, 'Info', None)))
                        if sm_type is not None:
                            _fn(_inst, text, sm_type)
                        else:
                            _fn(_inst, text)
                        return True
                except Exception:
                    pass
                return False
            _SM._offh_push_native = _offh_push_native
        except Exception:
            pass
        for _mname, _mobj in list(_sys_sm.modules.items()):
            if _mobj is None or not str(_mname).startswith('gui.'):
                continue
            try:
                if hasattr(_mobj, 'pushMessage'):
                    setattr(_mobj, 'pushMessage', _safe_pushMessage)
                if hasattr(_mobj, 'pushI18nMessage'):
                    setattr(_mobj, 'pushI18nMessage', _safe_pushI18nMessage)
            except Exception:
                pass
        if not getattr(_SM, '_offh_sysmsg_guarded', False):
            _SM._offh_sysmsg_guarded = True
            LOG_DEBUG('SYSMSG_GUARD installed')
    except Exception:
        pass

    try:
        import Event as _EvMod
        from messenger.wrappers import ChannelWrapper as _CW
        if not hasattr(_CW, 'waitingDestroyedEvent'):
            _CW.waitingDestroyedEvent = _EvMod.Event()
        if not hasattr(_CW, '__getattr__'):
            def _cw_safe_getattr(self, name):
                if name.endswith('Event') or name.startswith('on'):
                    ev = _EvMod.Event()
                    try:
                        setattr(self, name, ev)
                    except Exception:
                        pass
                    return ev
                return lambda *a, **kw: None
            _CW.__getattr__ = _cw_safe_getattr
    except Exception:
        pass

    try:
        from gui.Scaleform.CommonPage import CommonPage as _CP
        if not getattr(_CP, '_offh_unsub_guarded', False):
            _orig_unsub = _CP._unsubscribe
            def _safe_unsub(self, *a, **kw):
                try:
                    return _orig_unsub(self, *a, **kw)
                except Exception:
                    try:
                        self._CommonPage__isSubscribe = False
                    except Exception:
                        pass
                    return None
            _CP._unsubscribe = _safe_unsub
            _CP._offh_unsub_guarded = True
    except Exception:
        pass

    try:
        from messenger.gui.Scalefrom.MessengerLobbyInterface import MessengerLobbyInterface as _MLI
        if not getattr(_MLI, '_offh_pref_guarded', False):
            _orig_pref = getattr(_MLI, '_MessengerLobbyInterface__onApplyUserPreferences', None)
            if _orig_pref is not None:
                def _safe_pref(self, *a, **kw):
                    try:
                        return _orig_pref(self, *a, **kw)
                    except ReferenceError:
                        return None
                    except Exception:
                        return None
                _MLI._MessengerLobbyInterface__onApplyUserPreferences = _safe_pref
                _MLI._offh_pref_guarded = True
    except Exception:
        pass


def install_current_vehicle_persistence_hook():
    """Persist and restore the last selected tank in the Hangar carousel across restarts."""
    try:
        from CurrentVehicle import _CurrentVehicle, g_currentVehicle
        if getattr(_CurrentVehicle, '_offh_persist_patched', False):
            return
        import gui.mods.offhangar.state as _st
        from gui.mods.offhangar.state import save_selected_vehicle, load_selected_vehicle
        from gui.mods.offhangar.logging import LOG_NOTE

        def _cv_get_item(self):
            return self.__dict__.get('item') or getattr(self, 'vehicle', None) or getattr(self, '_CurrentVehicle__vehicle', None)
        def _cv_set_item(self, val):
            self.__dict__['item'] = val
        _CurrentVehicle.item = property(_cv_get_item, _cv_set_item)

        _orig_cv_prop = getattr(_CurrentVehicle, 'vehicle', None)
        _orig_cv_get = _orig_cv_prop.fget if isinstance(_orig_cv_prop, property) else None
        _orig_cv_set = _orig_cv_prop.fset if isinstance(_orig_cv_prop, property) else None
        if callable(_orig_cv_set):
            def _patched_cv_set(self, newVehicle):
                if newVehicle is not None:
                    try:
                        vid = getattr(newVehicle, 'inventoryId', None) or getattr(newVehicle, 'invID', None) or getattr(newVehicle, 'id', None)
                        if vid:
                            _st._LAST_SELECTED_VEH_INV_ID = int(vid)
                            save_selected_vehicle(int(vid))
                    except Exception:
                        pass
                return _orig_cv_set(self, newVehicle)
            _CurrentVehicle.vehicle = property(_orig_cv_get, _patched_cv_set)

        _orig_set_veh = getattr(_CurrentVehicle, 'setVehicleById', None)
        if callable(_orig_set_veh):
            def _patched_setVehicleById(self, vehInvID=0):
                vid = 0
                req_id = vehInvID
                try:
                    vid = int(vehInvID or 0)
                    req_id = vid
                except Exception:
                    try:
                        vid = int(getattr(vehInvID, 'inventoryId', None) or getattr(vehInvID, 'invID', 0) or 0)
                        req_id = vid
                    except Exception:
                        vid = 0
                result = _orig_set_veh(self, req_id)
                if vid > 0:
                    try:
                        _st._LAST_SELECTED_VEH_INV_ID = vid
                        save_selected_vehicle(vid)
                    except Exception:
                        pass
                return result
            _CurrentVehicle.setVehicleById = _patched_setVehicleById
            if not hasattr(_CurrentVehicle, 'selectVehicle'):
                _CurrentVehicle.selectVehicle = _patched_setVehicleById

        # Supply saved vehicle ID to stock CurrentVehicle.getFromServer without breaking adisp
        try:
            from account_helpers.AccountSettings import AccountSettings
            _orig_getFav = getattr(AccountSettings, 'getFavorites', None)
            if callable(_orig_getFav) and not getattr(AccountSettings, '_offh_fav_patched', False):
                def _patched_getFavorites(key):
                    if key == 'current':
                        try:
                            saved_id = load_selected_vehicle()
                            if saved_id and int(saved_id) > 0:
                                return int(saved_id)
                        except Exception:
                            pass
                    return _orig_getFav(key)
                AccountSettings.getFavorites = staticmethod(_patched_getFavorites)
                AccountSettings._offh_fav_patched = True

            _orig_setFav = getattr(AccountSettings, 'setFavorites', None)
            if callable(_orig_setFav) and not getattr(AccountSettings, '_offh_set_fav_patched', False):
                def _patched_setFavorites(key, value):
                    if key == 'current':
                        try:
                            vid = int(value or 0)
                            if vid > 0:
                                _st._LAST_SELECTED_VEH_INV_ID = vid
                                save_selected_vehicle(vid)
                        except Exception:
                            pass
                    return _orig_setFav(key, value)
                AccountSettings.setFavorites = staticmethod(_patched_setFavorites)
                AccountSettings._offh_set_fav_patched = True
        except Exception:
            pass

        try:
            from gui.Scaleform.Hangar import Hangar
            _orig_onVehSel = getattr(Hangar, 'onVehicleSelectChange', None)
            if callable(_orig_onVehSel) and not getattr(Hangar, '_offh_sel_veh_patched', False):
                def _patched_onVehSel(self, callbackId, vehicleId):
                    try:
                        from gui.Scaleform.utils.gui_items import getItemByCompact
                        _v_sel = getItemByCompact(vehicleId)
                        if _v_sel is not None:
                            _v_id = getattr(_v_sel, 'inventoryId', None) or getattr(_v_sel, 'id', None)
                            if _v_id:
                                _st._LAST_SELECTED_VEH_INV_ID = int(_v_id)
                                save_selected_vehicle(int(_v_id))
                    except Exception:
                        pass
                    return _orig_onVehSel(self, callbackId, vehicleId)
                Hangar.onVehicleSelectChange = _patched_onVehSel
                Hangar._offh_sel_veh_patched = True
        except Exception:
            pass

        _CurrentVehicle._offh_persist_patched = True
        LOG_NOTE('VEHICLE_PERSISTENCE installed cleanly without wrapping adisp coroutines')

        saved_id = load_selected_vehicle()
        if saved_id and int(saved_id) > 0:
            try:
                import Settings
                if getattr(Settings, 'g_instance', None) is not None:
                    Settings.g_instance.userPrefs[Settings.KEY_CURRENT_VEHICLE].writeInt(int(saved_id))
            except Exception:
                pass
    except Exception:
        pass

install_current_vehicle_persistence_hook()
install_video_mode_and_settings_guard()


def _schedule_hangar_sysmsg_guard():
    try:
        import Account
        _orig_obp = Account.PlayerAccount.onBecomePlayer
        def _patched_obp(self, *args, **kwargs):
            install_hangar_system_messages_guard()
            install_current_vehicle_persistence_hook()
            res = _orig_obp(self, *args, **kwargs)
            install_hangar_system_messages_guard()
            return res
        Account.PlayerAccount.onBecomePlayer = _patched_obp
    except Exception:
        pass

_schedule_hangar_sysmsg_guard()


def _sanitize_scaleform_battle_results(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ('arenaUniqueID', 'arenaGuiType', 'arenaTypeID', 'GuiType') and isinstance(v, (int, long)):
                out[k] = v
            elif k in ('playerName', 'clanAbbrev', 'vehicleName', 'typeCompDescrStr', 'arenaStr', 'winText', 'finishReasonStr', 'mapName', 'userName', 'killerName') and not isinstance(v, (str, unicode)):
                out[k] = str(v if v is not None else '')
            else:
                out[k] = _sanitize_scaleform_battle_results(v)
        return out
    elif isinstance(obj, list):
        return [_sanitize_scaleform_battle_results(x) for x in obj]
    elif isinstance(obj, tuple):
        return tuple(_sanitize_scaleform_battle_results(x) for x in obj)
    return obj
