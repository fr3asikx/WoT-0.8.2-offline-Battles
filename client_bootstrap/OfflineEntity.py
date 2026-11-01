# -*- coding: utf-8 -*-
'''res_mods override of the stock OfflineEntity (res/scripts/client).

Engine-assigned entity ids can collide with vehicle/inventory ids, so native
code that probes BigWorld.entity(playerVehicleID) - e.g. ArcadeControlMode.
__activateAlternateMode on Shift, the arcade<->sniper scroll switch - may get
one of these stubs back and then reads .isStarted / .appearance.isUnderwater().
The stock class had neither attribute: the AttributeError killed the key event
and the strategic (SPG) camera could not be entered for the rest of the battle.
Looking like a not-yet-started Vehicle (isStarted False) short-circuits those
checks safely.
'''

import BigWorld


_native_destructible_original = [None]
_native_destructible_installed = [False]
_native_destructible_error_logged = [False]


def _offh_destructible_may_be_broken(owner, chunkID, itemIndex, matKind,
        itemFilename, itemScale, vehicleSpeed):
    """Retail Vehicle crushability test without a Vehicle type assertion."""
    try:
        import AreaDestructibles
        import DestructiblesCache
        import math
        desc = AreaDestructibles.g_cache.getDescByFilename(itemFilename)
        if desc is None:
            return False
        controller = (
            AreaDestructibles.g_destructiblesManager.getController(chunkID)
        )
        if controller is None:
            return False
        if controller.isDestructibleBroken(
                itemIndex, matKind, desc['type']):
            return True
        mass = owner.typeDescriptor.physics['weight']
        instant_damage = (
            0.5 * mass * vehicleSpeed * vehicleSpeed * 0.00015
        )
        if desc['type'] == DestructiblesCache.DESTR_TYPE_STRUCTURE:
            module_desc = desc['modules'].get(matKind)
            if module_desc is None:
                return False
            reference_health = module_desc['health']
        else:
            unit_mass = AreaDestructibles.g_cache.unitVehicleMass
            instant_damage *= math.pow(
                mass / unit_mass, desc['kineticDamageCorrection']
            )
            reference_health = desc['health']
        return (DestructiblesCache.scaledDestructibleHealth(
            itemScale, reference_health) < instant_damage)
    except Exception as error:
        if not _native_destructible_error_logged[0]:
            _native_destructible_error_logged[0] = True
            try:
                from gui.mods.offhangar.logging import LOG_ERROR
                LOG_ERROR(
                    'NATIVE_BOT_PHYSICS destructible callback failed: %s' %
                    str(error))
            except Exception:
                pass
        return False


def install_native_destructible_callback_adapter():
    """Install the callback shape hard-coded by WGVehiclePhysics2.

    The 0.8.2 native binary imports ``Vehicle.Vehicle`` and retrieves this
    method from the class before calling it with the owner as argument zero.
    A regular Python-2 unbound method rejects our deliberately small
    OfflineEntity owner.  A staticmethod keeps the retail implementation and
    removes only that unrelated class-membership assertion.
    """
    try:
        import Vehicle
        callback = getattr(
            Vehicle.Vehicle, '_isDestructibleMayBeBroken', None)
        if getattr(callback, '_offh_offline_entity_adapter', False):
            return True
        try:
            original = Vehicle.Vehicle.__dict__.get(
                '_isDestructibleMayBeBroken')
        except Exception:
            original = callback
        _offh_destructible_may_be_broken._offh_offline_entity_adapter = True
        Vehicle.Vehicle._isDestructibleMayBeBroken = staticmethod(
            _offh_destructible_may_be_broken)
        _native_destructible_original[0] = original
        _native_destructible_installed[0] = True
        _native_destructible_error_logged[0] = False
        return True
    except Exception:
        return False


def restore_native_destructible_callback_adapter():
    """Restore the exact class descriptor replaced for native bot bodies."""
    if not _native_destructible_installed[0]:
        return True
    try:
        import Vehicle
        callback = getattr(
            Vehicle.Vehicle, '_isDestructibleMayBeBroken', None)
        if not getattr(callback, '_offh_offline_entity_adapter', False):
            return False
        original = _native_destructible_original[0]
        if original is None:
            delattr(Vehicle.Vehicle, '_isDestructibleMayBeBroken')
        else:
            Vehicle.Vehicle._isDestructibleMayBeBroken = original
        _native_destructible_original[0] = None
        _native_destructible_installed[0] = False
        _native_destructible_error_logged[0] = False
        return True
    except Exception:
        return False


class OfflineEntity(BigWorld.Entity):
    isStarted = False
    isPlayer = False
    appearance = None
    typeDescriptor = None
    wgPhysics = None

    def __init__(self):
        pass

    def prerequisites(self):
        return []

    def onEnterWorld(self, prereqs):
        pass

    def onLeaveWorld(self):
        pass

    # WGVehiclePhysics2 normally owns a retail Vehicle and may dispatch these
    # callbacks while resolving static geometry. OfflineEntity is also used by
    # the opt-in native-physics capability probe; no-op handlers keep that probe
    # isolated from gameplay damage and destructible state.
    def onStaticCollision(self, energy, point, normal, miscFlags):
        pass

    def _isDestructibleMayBeBroken(self, chunkID, itemIndex, matKind,
            itemFilename, itemScale, vehicleSpeed):
        return _offh_destructible_may_be_broken(
            self, chunkID, itemIndex, matKind, itemFilename, itemScale,
            vehicleSpeed)

    def showVehicleCollisionEffect(self, position):
        return
