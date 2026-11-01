# project guide

this is a quick map of the mod. the code is split by job so it is easier to find the bit that needs work.

- `core/` — config, saved state, session setup and logging.
- `native/` — the optional native physics and math helpers.
- `ai/` — bot decisions, navigation and driving.
- `combat/` — guns, armour, damage, spotting and loadouts.
- `physics/` — vehicle movement, collisions and map objects.
- `ui/` — battle HUD, markers, minimap and hangar support.
- `network/` — local battle traffic and replay support.

`offline_battle.py` is the main battle controller. the smaller modules handle the parts that can be tested on their own.

## useful places to start

- hangar and saved settings: `core/paths.py`, `core/state.py`
- battle setup and cleanup: `offline_battle.py`
- bot behaviour: `ai/`
- native worker bridge: `native/`
- post-battle results: the results section in `offline_battle.py`
