# -*- coding: utf-8 -*-
"""Deterministic strategic AI for offline/LAN bots.

This module is deliberately engine-free so its decisions can be tested on a
modern Python interpreter while the shipped client still imports it on its
Python 2 runtime. It owns stable personalities, vehicle-role scoring, shared
team contact memory, route assignment and target selection. Rendering,
physics, collision and shell resolution remain in ``offline_battle.py``.
"""

import hashlib
import math
import random

from gui.mods.offhangar import bot_ai_maps


def _offh_ai_log_note(text):
	"""Best-effort LOG_NOTE that keeps this module importable on a plain
	Python interpreter with no WoT client engine present (see the module
	docstring). gui.mods.offhangar.logging pulls in engine-only utils, so it
	is imported lazily here and any failure is swallowed - a missing log
	line is never worth breaking a unit test or a bot's decision over."""
	try:
		s = str(text or '')
		if s.startswith(('SQUAD_MIX_CFG ', 'SQUAD_COHESION_CFG ', 'SQUAD_MIX ', 'SQUAD_ASSIGN ')):
			from gui.mods.offhangar.logging import LOG_DEBUG as _log_dbg
			_log_dbg(text)
			return
		from gui.mods.offhangar.logging import LOG_NOTE as _log_note
		_log_note(text)
	except Exception:
		pass


CONTACT_MEMORY_SECONDS = 600.0  # v5.2: Full-battle minimap memory of last known enemy positions
TARGET_HYSTERESIS_BONUS = 55.0
LOCAL_FORCE_RADIUS = 185.0
BATTLE_TIER_RADIUS = 1
MATCH_CLASSES = ('heavyTank', 'mediumTank', 'AT-SPG', 'lightTank', 'SPG')

# --- Artillery self-defense (v4.59) ---------------------------------------
# An SPG normally sits at `artillery_home` and never turns its hull or asks
# for help - realistic while nothing is near it, indefensible once a hunter
# actually reaches it. These thresholds are deliberately generous distances
# (a light tank closing to within 2-3 gun lengths of an SPG is already a
# lost duel in real WoT, so the point is to react well before that, not to
# simulate a fair fight once it's already too close).
ARTY_DANGER_RADIUS = 150.0      # visible enemy within this range = "being hunted"
ARTY_CRITICAL_RADIUS = 60.0     # close enough that standing still is a death sentence
ARTY_HELP_COOLDOWN = 22.0       # do not spam HELPME every tick while still threatened
ARTY_NO_LOS_HELP_SECONDS = 12.0 # how long a known-but-unseen contact must persist
                                 # before asking a squad to clear the sightline for it

# --- Rusher/kamikaze aggression (v4.66 доп.16) ----------------------------
# Light tanks (and fast mediums, by the same speed check) with a high enough
# 'rusher_streak' roll (see make_personality) play a distinctly more
# reckless game: they bias route selection toward the highest-risk lane
# instead of the best-scoring one, push to engagement range earlier, and
# shrug off a losing local force balance that would send a cautious scout
# home. This is deliberately a per-battle roll, not a class-wide switch -
# most lights still play the normal cautious-scout game; a rusher is the
# occasional bot that decided to trade survivability for early pressure and
# spotting-through-force instead of spotting-through-caution.
RUSHER_THRESHOLD = 0.96
RUSHER_MIN_SPEED = 48.0  # km/h - excludes slow "light" chassis, includes fast mediums
RUSHER_WITHDRAW_BALANCE_BONUS = 1.1  # how much worse the balance must be before a rusher still bails

# --- Fire discipline: stop-shoot-move (v4.70) ----------------------------
# Bots stop to aim and fire, then relocate laterally before the next shot.
# This creates a rhythmic peek cycle that matches mid-skill human play:
# stand still -> aim -> fire -> slide sideways 8-15m -> repeat.  Much more
# dangerous than the old "drive and spray" or "camp forever" extremes.
FIRE_DISCIPLINE_RELOCATE_MIN = 8.0      # minimum lateral shift after a shot
FIRE_DISCIPLINE_RELOCATE_MAX = 15.0     # maximum lateral shift
FIRE_DISCIPLINE_PULLBACK = 4.0          # also retreat slightly toward cover
FIRE_DISCIPLINE_COOLDOWN = 1.6          # seconds to hold relocated position
FIRE_DISCIPLINE_MIN_RANGE = 60.0        # too close -> just brawl, don't peek

# --- Route switching (v4.70) ----------------------------------------------
# When a lane is clearly lost (far more enemies than allies near the bot's
# current route), a bot may abandon it for a healthier corridor instead of
# feeding one tank at a time into a lost fight.
ROUTE_SWITCH_BALANCE = -2.0             # local balance must be this bad
ROUTE_SWITCH_COOLDOWN_SECONDS = 35.0    # per-bot switch cooldown
ROUTE_SWITCH_MIN_ENEMY_DIST = 120.0     # must not be actively chased

# --- Tactical convergence / dynamic grouping (v4.70) ----------------------
# The "kuchka" system. Bots without their own contacts that are near an
# active fight (multiple enemies + allies already engaged) converge on its
# centre instead of continuing along an empty route to nowhere. This is
# reactive, per-tick grouping - independent of static squad assignments.
CONVERGENCE_SCAN_RADIUS = 300.0         # how far a bot looks for a fight
CONVERGENCE_MIN_FIGHT_SIZE = 3          # total combatants (allies+enemies)
CONVERGENCE_SPREAD = 30.0              # disperse around the fight centre

# --- Crossfire coordination (v4.70) ---------------------------------------
# When multiple allies aim at the same target, they spread laterally to
# create crossing angles. Even a 25-degree spread makes the target choose
# which shooter to angle against, massively increasing effective DPM.
CROSSFIRE_MIN_ANGLE_RAD = 0.44          # ~25 degrees minimum separation
CROSSFIRE_OFFSET_DISTANCE = 35.0        # how far to shift along the arc

# --- Cyber-bot tactical baiting and tanking (v4.73) ------------------------
# Baiting: fake peeking around corners / tracks poking out to bait enemy shots.
CYBER_BAIT_MIN_RANGE = 12.0          # Minimum range to attempt fake peek (m)
CYBER_BAIT_MAX_RANGE = 280.0         # Maximum range to attempt fake peek (m)
CYBER_BAIT_HARD_TWITCH = 0.45        # Hard bot: fast twitch duration (s)
CYBER_BAIT_MED_TWITCH = 0.85         # Medium bot: slower twitch duration (s)
CYBER_BAIT_RETREAT_TIME = 0.65       # Duration of emergency reverse (s)
CYBER_RELOAD_COVER_THRESHOLD = 1.4   # Min remaining reload time to seek cover (s)



def _is_rusher_profile(profile):
	"""Class/speed gate: only scouty light tanks and fast mediums are eligible."""
	class_tag = profile.get('class_tag')
	if class_tag not in ('lightTank', 'mediumTank'):
		return False
	if class_tag == 'mediumTank' and _number(profile.get('speed', 0.0), 0.0) * 3.6 < RUSHER_MIN_SPEED:
		return False
	return True


def _is_rusher(agent):
	"""True for a bot currently playing the rusher/kamikaze aggression style.

	Combines the class/speed gate with the bot's own per-battle roll, so the
	same light tank archetype plays differently battle to battle instead of
	every light on the team turning into an identical yolo-scout squad.
	"""
	profile = agent.get('profile') or {}
	if not _is_rusher_profile(profile):
		return False
	personality = agent.get('personality') or {}
	return _number(personality.get('rusher_streak', 0.0)) >= RUSHER_THRESHOLD


# --- King of the hill base-holding (v4.66 доп.17) -------------------------
# Not a stock WoT game mode - this is the offhangar project's own optional
# battle style: instead of every squad just following its route toward the
# enemy flag, one squad per team is designated to hold the map's central
# "hill" (the key contested area) as a coordinated group, falling back to it
# whenever the squad as a whole is not actively winning a fight elsewhere.
# Deliberately squad-level, not per-bot: a hill worth holding is worth
# holding together, the same reasoning _assign_squad already applies to a
# rush/fallback call.
KING_OF_HILL_HOLD_RADIUS = 22.0   # how tight a "holding" squad clusters on the point
KING_OF_HILL_SQUAD_INDEX = 0      # which squad per team (by join order) holds the hill


# --- Squads --------------------------------------------------------------
# A squad is a small fireteam within one team, with one member acting as its
# leader. This is deliberately independent of "route": route decides which
# lane a bot heads down, squad decides who it fights alongside once it gets
# there. SPGs never join a squad - they already stand off on their own.
SQUAD_SIZE = 4
SQUAD_MIX_COUNT = 3   # squads per team when bot_squad_mix is on
SQUAD_MIX_CAP = 5     # members per mixed squad before a new one opens
SQUAD_ORDER_HOLD_LEADER_SECONDS = 20.0
SQUAD_RECALL_RADIUS = 130.0
SQUAD_HELP_RADIUS = 220.0
# A player-issued recall (future 'cover me' / 'need help' radio command) must
# outrank the squad leader's own recall of the same bot, but only briefly -
# long enough for the player's own tactical moment, not for the rest of the
# battle. Kept here, not in offline_battle.py, so the whole precedence rule
# lives with the rest of squad logic in one file.
PLAYER_RECALL_PRIORITY_SECONDS = 12.0

# --- Player radio commands ------------------------------------------------
# Names, argsCnt and target kind below are taken directly from the shipped
# 0.8.2 client's CHAT_COMMANDS enum (scripts/common/chat_shared.pyc) and
# Avatar.onChatShortcut / onChatShortcutTarget / onMinimapCellClicked
# (scripts/client/avatar.pyc) - decompiled and read for this project, not
# guessed. A few commands from the enum are deliberately left out here:
# RELOADINGGUN/POSITIVE/NEGATIVE are pure social flavour with no tactical
# payload, so offline_battle.py can just echo them into chat directly without
# involving this module at all.
#
#   command             argsCnt  target
#   HELPME              0        none            (broadcast to team)
#   ATTACK              0        none            (broadcast to team)
#   BACKTOBASE          0        none            (broadcast to team)
#   ATTENTIONTOCELL     1        minimap cell
#   ATTACKENEMY         1        enemy vehicle
#   SUPPORTMEWITHFIRE   1        enemy vehicle
#   FOLLOWME            1        ally vehicle    (private: "you, follow me")
#   TURNBACK            1        ally vehicle    (private: "you, turn back")
#   HELPMEEX            1        ally vehicle    (private: "you specifically, help")
#   STOP                1        ally vehicle    (private: "you, stop")
#
# This module owns the command's TACTICAL meaning; offline_battle.py only
# forwards (command, issuer_id, issuer_team, issuer_position, cell_index,
# target_id) from Avatar's own callbacks into apply_radio_command below. A
# command never overrides a bot's own survival decision - see
# RADIO_PROTECTED_MODES - the same rule the squad leader's own rush/fallback
# call is held to against a lone member's withdraw.
RADIO_COMMAND_ATTACK = 'ATTACK'
RADIO_COMMAND_STOP = 'STOP'
RADIO_COMMAND_TURNBACK = 'TURNBACK'
RADIO_COMMAND_BACKTOBASE = 'BACKTOBASE'
RADIO_COMMAND_ATTENTION_TO_CELL = 'ATTENTIONTOCELL'
RADIO_COMMAND_HELP_ME = 'HELPME'
RADIO_COMMAND_FOLLOW_ME = 'FOLLOWME'
RADIO_COMMAND_HELP_ME_EX = 'HELPMEEX'
RADIO_COMMAND_ATTACK_ENEMY = 'ATTACKENEMY'
RADIO_COMMAND_SUPPORT_FIRE = 'SUPPORTMEWITHFIRE'
RADIO_COMMANDS = frozenset((
	RADIO_COMMAND_ATTACK, RADIO_COMMAND_STOP, RADIO_COMMAND_TURNBACK,
	RADIO_COMMAND_BACKTOBASE, RADIO_COMMAND_ATTENTION_TO_CELL,
	RADIO_COMMAND_HELP_ME, RADIO_COMMAND_FOLLOW_ME, RADIO_COMMAND_HELP_ME_EX,
	RADIO_COMMAND_ATTACK_ENEMY, RADIO_COMMAND_SUPPORT_FIRE,
))
# Targets an ENEMY vehicle (client arg is that vehicle's id).
RADIO_TARGET_COMMANDS = frozenset((
	RADIO_COMMAND_ATTACK_ENEMY, RADIO_COMMAND_SUPPORT_FIRE,
))
# Targets one specific ALLY vehicle (client's "private address" commands -
# Avatar.__onSenderPrivateUserChatCmd). Only that one bot acts; it is not a
# broadcast recruitment call like HELPME.
RADIO_ALLY_TARGET_COMMANDS = frozenset((
	RADIO_COMMAND_FOLLOW_ME, RADIO_COMMAND_TURNBACK,
	RADIO_COMMAND_HELP_ME_EX, RADIO_COMMAND_STOP,
))
# A command holds for this long before it needs to be reissued - long enough
# to actually cross the map on a 'cover me', short enough that a forgotten
# order does not haunt a bot for the rest of the battle.
RADIO_COMMAND_HOLD_SECONDS = 25.0
RADIO_SOFT_HOLD_SECONDS = 12.0   # an accepted soft call lasts this long
# Never let a radio command pull a bot out of its own emergency. Compare
# order_for's own low-HP withdraw gate and the squad fallback/rush split -
# this list exists so all three respect the same one rule instead of each
# reinventing it slightly differently.
RADIO_PROTECTED_MODES = frozenset((
	'withdraw', 'artillery_fire', 'artillery_point_blank',
))
# How many bots one HELPME/FOLLOWME call recruits, nearest first. Matches
# a real squad's size so 'cover me' cannot empty an entire flank onto one
# player and leave the line open.
RADIO_HELP_RECRUITS = SQUAD_SIZE - 1

# --- Bot-to-player advisory (the offhangar project's own idea; not a stock
# WoT feature - see PHYSICS_NOTES.md for the credit line) ------------------
# A bot near the player reads the same local force balance the squad system
# already computes for itself, but centred on the PLAYER instead of on a
# squad. If it looks bad, the nearest bot calls "hold" - not a command that
# blocks the player's input (this module never touches player controls), just
# advice offline_battle.py can surface as a chat line / voice line / HUD
# marker. When the balance later turns favourable, the same or another nearby
# bot calls "advance" as the explicit all-clear the player asked for: a rush
# after "advance" is not a coin flip, it is a rush the bots' own numbers
# already back.
ADVISORY_RADIUS = 140.0
# Hysteresis gap between the "hold" and "advance" thresholds. Without a gap,
# a balance sitting near the boundary would flip the call every tick as tiny
# position/health changes cross back and forth - exactly the mandrazh-style
# flicker this project already fixed once in the native-physics gating, just
# in a tactical-advice form instead of a physics form.
ADVISORY_HOLD_BALANCE = -0.55
ADVISORY_ADVANCE_BALANCE = 0.35
ADVISORY_MIN_HOLD_SECONDS = 3.0
ADVISORY_REPEAT_SECONDS = 55.0
# "Get off my sight!" fires only when an ally is BETWEEN the shooter and an
# already-locked, already-visible target - it is a friendly-fire/blocked-shot
# warning, never a request to move out of the player's general direction.
ADVISORY_BLOCKED_SHOT_HALF_WIDTH = 2.6


def _number(value, default=0.0):
	try:
		result = float(value)
		if math.isinf(result) or math.isnan(result):
			return float(default)
		return result
	except Exception:
		return float(default)


# Authentic 2012 WoT 0.8.2 Dedicated Scout LTs (Battle Tiers up to 7-12):
_WOT_082_DEDICATED_SCOUTS_T4 = (
	'T-50', 'A-20', 'Pz38NA', 'Pz38_NA', 'M5_Stuart', 'VK1602', 'Luchs', 'M5A1_Stuart', 'Ke_Ho'
)
_WOT_082_DEDICATED_SCOUTS_T5 = (
	'T_50_2', 'MT25', 'VK2801', 'M24_Chaffee', 'Chaffee', 'ELC_AMX', '59_16', 'Auf_Panther'
)
_WOT_082_DEDICATED_SCOUTS_HIGH = (
	'AMX_12t', 'AMX_13_75', 'AMX_13_90', 'Type62', 'WZ_131', 'WZ_132', 'T21', 'T71', 'MT25', 'VK2801', 'Auf_Panther'
)
_WOT_082_INFANTRY_BRAWLER_LTS = (
	'AMX_38', 'AMX_40', 'Hotchkiss_H35', 'H39_captured', 'D1',
	'Valentine', 'Valentine_LL', 'PzII_J', 'T_127', 'Matilda'
)


def _is_dedicated_scout_lt(candidate):
	"""True only for authentic 0.8.2 dedicated scout light tanks (never AMX 40 / Valentine / low-tier combat LTs)."""
	try:
		if vehicle_match_class(candidate) != 'lightTank':
			return False
		lvl = int(candidate.get('level', 0) or 0)
		name = str(candidate.get('name', '') or '')
		if any(tok in name for tok in _WOT_082_INFANTRY_BRAWLER_LTS):
			return False
		if lvl == 4:
			return any(tok in name for tok in _WOT_082_DEDICATED_SCOUTS_T4)
		if lvl == 5:
			return any(tok in name for tok in _WOT_082_DEDICATED_SCOUTS_T5)
		if lvl >= 6:
			return True
		return False
	except Exception:
		return False

_CLIENT_MAX_SPG_TIER_CACHE = [0]

def _get_client_max_spg_tier(pool=None):
	if _CLIENT_MAX_SPG_TIER_CACHE[0] > 0:
		return _CLIENT_MAX_SPG_TIER_CACHE[0]
	try:
		if pool:
			spg_lvls = [int(c.get('level', 0) or 0) for c in pool if 'SPG' in (c.get('tags') or ()) and 'AT-SPG' not in (c.get('tags') or ())]
			if spg_lvls:
				_CLIENT_MAX_SPG_TIER_CACHE[0] = max(spg_lvls)
				return _CLIENT_MAX_SPG_TIER_CACHE[0]
		from items import vehicles
		import nations
		max_lvl = 0
		for nid in nations.INDICES.values():
			for v_id, v_data in vehicles.g_list.getList(nid).items():
				tags = v_data.get('tags') or ()
				if 'SPG' in tags and 'AT-SPG' not in tags:
					lvl = int(v_data.get('level', 0) or 0)
					if lvl > max_lvl:
						max_lvl = lvl
		if max_lvl > 0:
			_CLIENT_MAX_SPG_TIER_CACHE[0] = max_lvl
			return max_lvl
	except Exception:
		pass
	return 8

def effective_match_tier(candidate_or_level, pool=None):
	"""Return the authentic 2012 WoT 0.8.2 matchmaking anchor tier for a player/bot vehicle.
	In 0.8.2 (max SPG tier = 8), high-tier SPGs (T6-T8) and dedicated scouts have elevated battle tiers
	so a Tier 8 SPG (Object 261, GW Typ E, T92, BatChat 155) always fights in Tier 8-9-10 battles!"""
	try:
		if isinstance(candidate_or_level, dict):
			lvl = int(candidate_or_level.get('level', 1) or 1)
			tags = candidate_or_level.get('tags') or ()
			cls = vehicle_match_class(candidate_or_level)
		else:
			lvl = int(candidate_or_level or 1)
			tags = ()
			cls = 'mediumTank'
		lvl = max(1, min(10, lvl))
		if (cls == 'SPG' or ('SPG' in tags and 'AT-SPG' not in tags)) and _get_client_max_spg_tier(pool) <= 8:
			if lvl >= 8:
				return 10
			if lvl == 7:
				return 9
			if lvl == 6:
				return 8
			if lvl == 5:
				return 7
			if lvl == 4:
				return 6
			if lvl == 3:
				return 4
		return lvl
	except Exception:
		return 5


def vehicle_in_battle_tier_band(player_tier, candidate_tier, candidate_tags=()):
	"""Dynamic WoT 0.8.x battle tier eligibility:
	- On 0.8.0..0.8.5 clients (max SPG tier = 8): authentic 2012 Tier 2-8 SPG table.
	- On 0.8.6..0.8.11 clients (max SPG tier = 10): stretched Tier 2-10 SPG +/-2 table."""
	try:
		pt = int(player_tier)
		ct = int(candidate_tier)
		tags = candidate_tags or ()
		if 'SPG' in tags and 'AT-SPG' not in tags:
			if _get_client_max_spg_tier() >= 9:
				return abs(ct - pt) <= 2
			if pt >= 9:
				return ct in (6, 7, 8)
			if pt >= 6:
				return max(3, pt - 3) <= ct <= min(8, pt - 1)
			if pt in (4, 5):
				return max(2, pt - 2) <= ct <= min(5, pt)
			return ct == 2
		if 'lightTank' in tags and pt >= 6:
			# Historical 0.8.2 Scout MM: Tier 4-8 scouts see high-tier battles (up to Battle Tier 9-12)
			return max(4, pt - 4) <= ct <= min(8, pt)
		return abs(ct - pt) <= 2
	except Exception:
		return False


def select_bot_lineup(pool, count, spg_limit=1, fallback_candidates=()):
	"""Fill a team while enforcing an exact SPG cap.

	``AT-SPG`` is a tank destroyer in the legacy tags and does not consume the
	artillery slot.  Human artillery is accounted for by the caller through a
	reduced ``spg_limit``.
	"""
	count = max(0, int(count))
	spg_limit = max(0, int(spg_limit))
	pool = list(pool or ())
	if not pool or count <= 0:
		return []
	regular = []
	seen = set()
	for candidate in pool + list(fallback_candidates or ()):
		try:
			tags = candidate['tags']
			name = candidate['name']
		except Exception:
			continue
		if 'SPG' in tags or name in seen:
			continue
		seen.add(name)
		regular.append(candidate)
	result = []
	spg_count = 0
	regular_index = 0
	for index in range(count):
		candidate = pool[index % len(pool)]
		try:
			is_spg = 'SPG' in candidate['tags']
		except Exception:
			is_spg = False
		if is_spg and spg_count >= spg_limit:
			if not regular:
				continue
			candidate = regular[regular_index % len(regular)]
			regular_index += 1
			is_spg = False
		if is_spg:
			spg_count += 1
		result.append(candidate)
	return result


def vehicle_match_class(candidate):
	"""Return the exact legacy matchmaking class for a vehicle record."""
	try:
		tags = candidate['tags']
	except Exception:
		tags = ()
	for class_tag in MATCH_CLASSES:
		if class_tag in tags:
			return class_tag
	return 'mediumTank'


def choose_match_tiers(player_tier, mode_roll, side_roll=0.5,
		available_tiers=()):
	"""v6.9: Choose an authentic 2012 WoT 0.8.2 battle tier spread (usually 3 main tiers,
	e.g. Tiers 8-9-10 when player is Tier 8/9/10, while Scout LTs and SPGs use their 0.8.2
	battle-tier band down to Tier 5-8 just like in the 2013 Ruinberg replay)."""
	try:
		player_tier = max(1, min(10, int(player_tier)))
	except Exception:
		player_tier = 1
	available = set()
	for value in (available_tiers or range(1, 11)):
		try:
			value = int(value)
		except Exception:
			continue
		if 1 <= value <= 10:
			available.add(value)
	available.add(player_tier)
	try:
		mode_roll = float(mode_roll)
		side_roll = float(side_roll)
	except Exception:
		mode_roll = side_roll = 0.5

	# For Tier 6-10, 2012 WoT predominantly forms 3-tier main battle spreads (e.g. 8-9-10, 7-8-9, 6-7-8)
	if player_tier >= 6:
		if player_tier == 10:
			spreads = [(8, 9, 10), (8, 9, 10), (9, 10)]
		elif player_tier == 9:
			spreads = [(8, 9, 10), (7, 8, 9), (8, 9, 10)]
		elif player_tier == 8:
			spreads = [(8, 9, 10), (7, 8, 9), (6, 7, 8)]
		else:
			spreads = [
				(player_tier, player_tier + 1, player_tier + 2),
				(player_tier - 1, player_tier, player_tier + 1),
				(player_tier - 2, player_tier - 1, player_tier),
			]
		idx = int((mode_roll * 0.999) * len(spreads))
		chosen = tuple(t for t in spreads[idx] if t in available)
		if chosen:
			return chosen

	lower = player_tier - 1 if player_tier - 1 in available else None
	upper = player_tier + 1 if player_tier + 1 in available else None
	if mode_roll < 0.18 or (lower is None and upper is None):
		return (player_tier,)
	if mode_roll < 0.48 or lower is None or upper is None:
		if lower is not None and upper is not None:
			other = lower if side_roll < 0.5 else upper
		elif lower is not None:
			other = lower
		else:
			other = upper
		return tuple(sorted((player_tier, other)))
	return (lower, player_tier, upper)


def select_vehicle_variety_pool(candidates, player_tier, max_unique,
		rng=None):
	"""Build a small texture-safe pool while preserving tiers and classes."""
	rng = rng or random
	max_unique = max(0, int(max_unique))
	if max_unique <= 0:
		return list(candidates or ())
	unique = []
	seen = set()
	for candidate in (candidates or ()):
		try:
			name = str(candidate['name'])
		except Exception:
			continue
		if name in seen:
			continue
		seen.add(name)
		unique.append(candidate)
	try:
		rng.shuffle(unique)
	except Exception:
		random.shuffle(unique)
	selected = []
	selected_names = set()

	def _take(options):
		for candidate in options:
			name = str(candidate['name'])
			if name not in selected_names:
				selected.append(candidate)
				selected_names.add(name)
				return True
		return False

	# Prefer one of each role at the selected tier, then fill any missing role
	# from a neighbouring tier. This keeps single-tier matches tactically varied.
	for class_tag in MATCH_CLASSES:
		if len(selected) >= max_unique:
			break
		exact = [candidate for candidate in unique
			if int(candidate.get('level', 0) or 0) == int(player_tier) and
			vehicle_match_class(candidate) == class_tag]
		if not _take(exact):
			_take([candidate for candidate in unique
				if vehicle_match_class(candidate) == class_tag])
	# Make every available tier representable before using the remaining slots.
	levels = sorted(set(int(candidate.get('level', 0) or 0)
		for candidate in unique))
	for level in levels:
		if len(selected) >= max_unique:
			break
		if any(int(candidate.get('level', 0) or 0) == level
				for candidate in selected):
			continue
		_take([candidate for candidate in unique
			if int(candidate.get('level', 0) or 0) == level])
	for candidate in unique:
		if len(selected) >= max_unique:
			break
		_take((candidate,))
	return selected


def build_match_template(pool, team_size, player_candidate, allowed_tiers,
		rng=None, required_profiles=()):
	"""Return one tier/class template that both teams can independently fill."""
	rng = rng or random
	team_size = max(1, int(team_size))
	allowed = tuple(sorted(set(int(value) for value in allowed_tiers)))
	if not allowed:
		allowed = (int(player_candidate.get('level', 1) or 1),)
	raw_player_tier = int(player_candidate.get('level', allowed[0]) or allowed[0])
	player_class = vehicle_match_class(player_candidate)
	player_name = str(player_candidate.get('name', ''))
	player_tier = effective_match_tier(player_candidate, pool)
	# v8.4 Authentic 2012 WoT 0.8.2 Matchmaker (Battle Tiers 1-12 + Scout MM + SPG MM + Class Weights + Flavors):
	# 1. If the player is in a dedicated Tier 4/5 Scout LT (e.g. T-50-2 -> Battle Tiers 7-12,
	#    T-50 / Luchs / Leopard / Pz38nA / M5 Stuart -> Battle Tiers 5-9), or a 0.8.2 SPG (Tier 8 Object 261 -> Tier 10),
	#    shift the main battle tier window upward just like the real 0.8.2 matchmaker!
	is_t5_scout = (player_class == 'lightTank' and raw_player_tier == 5 and any(s in player_name for s in _WOT_082_DEDICATED_SCOUTS_T5))
	is_t4_scout = (player_class == 'lightTank' and raw_player_tier == 4 and any(s in player_name for s in _WOT_082_DEDICATED_SCOUTS_T4))
	_shared_b_seed = getattr(rng, '_offh_shared_battle_seed', None)
	_bracket_rng = random.Random(_shared_b_seed) if _shared_b_seed is not None else rng
	try:
		_scout_roll = _bracket_rng.random()
	except Exception:
		_scout_roll = 0.5
	_pool_tiers = sorted(set(int(c.get('level', 0) or 0) for c in (pool or ()) if int(c.get('level', 0) or 0) > 0))
	_max_pool_tier = max(_pool_tiers) if _pool_tiers else 10
	if player_class == 'SPG' and player_tier > raw_player_tier:
		if player_tier >= 10:
			allowed = [8, 9, 10]
		elif player_tier == 9:
			allowed = [8, 9, 10] if _scout_roll < 0.65 else [7, 8, 9]
		else:
			allowed = [max(3, player_tier - 2), max(3, player_tier - 1), min(_max_pool_tier, player_tier)]
		allowed = sorted(set(lvl for lvl in allowed if 1 <= lvl <= _max_pool_tier)) or [raw_player_tier]
	elif is_t5_scout:
		# T-50-2 / VK2801 / Chaffee / ELC AMX: Battle Tiers 6..9 (40% Tier 5-7, 40% Tier 6-8, 20% Tier 7-9)
		_top_bt = 6 if _scout_roll < 0.40 else (7 if _scout_roll < 0.80 else 8)
		_top_bt = min(_top_bt, max(5, _max_pool_tier - 1))
		allowed = [lvl for lvl in (_top_bt - 1, _top_bt, min(_max_pool_tier, _top_bt + 1)) if 1 <= lvl <= 10]
	elif is_t4_scout:
		# T-50 / A-20 / Pz38nA / Luchs / M5 Stuart: Battle Tiers 5..7 (45% Tier 4-6, 40% Tier 5-7, 15% Tier 6-8)
		_top_bt = 5 if _scout_roll < 0.45 else (6 if _scout_roll < 0.85 else 7)
		_top_bt = min(_top_bt, max(4, _max_pool_tier - 1))
		allowed = [lvl for lvl in (_top_bt - 1, _top_bt, min(_max_pool_tier, _top_bt + 1)) if 1 <= lvl <= 10]
	else:
		# Authentic 2012 WoT 0.8.2 Battle Tier Chart (Tiers 1-3 capped at +1, Tier 4 infantry/heavies preferential, Tiers 5-10 +/-2):
		if player_tier == 1:
			allowed = [1] if _scout_roll < 0.45 else [1, 2]
		elif player_tier == 2:
			allowed = [1, 2] if _scout_roll < 0.45 else [2, 3]
		elif player_tier == 3:
			allowed = [2, 3] if _scout_roll < 0.45 else [2, 3, 4]
		elif player_tier == 4:
			_is_pref_t4 = (player_class == 'heavyTank' or any(tok in player_name for tok in _WOT_082_INFANTRY_BRAWLER_LTS))
			if _is_pref_t4 or _scout_roll < 0.40:
				allowed = [3, 4] if _scout_roll < 0.25 else [3, 4, 5]
			elif _scout_roll < 0.80:
				allowed = [3, 4, 5]
			else:
				allowed = [4, 5, 6]
		elif player_tier >= 5 and len(allowed) >= 2:
			if _scout_roll < 0.38:
				_max_t = player_tier
				allowed = [max(3, _max_t - 2), max(3, _max_t - 1), _max_t]
			elif _scout_roll < 0.75:
				_max_t = min(_max_pool_tier, min(10, player_tier + 1))
				allowed = [max(3, _max_t - 2), max(3, _max_t - 1), _max_t]
			else:
				_max_t = min(_max_pool_tier, min(10, player_tier + 2))
				allowed = [max(3, _max_t - 2), max(3, _max_t - 1), _max_t]
		allowed = sorted(set(lvl for lvl in allowed if 1 <= lvl <= _max_pool_tier)) or [player_tier]

	max_allowed = max(allowed) if allowed else player_tier
	min_allowed = min(allowed) if allowed else player_tier
	usable = []
	for candidate in (pool or ()):
		c_lvl = int(candidate.get('level', 0) or 0)
		c_cls = vehicle_match_class(candidate)
		if c_lvl in allowed:
			# Prevent non-scout low-tier lights from flooding Tier 6-10 combat slots
			if c_cls == 'lightTank' and max_allowed >= 6 and c_lvl < max(4, min_allowed) and not _is_dedicated_scout_lt(candidate):
				continue
			usable.append(candidate)
		elif c_cls == 'SPG' and vehicle_in_battle_tier_band(max_allowed, c_lvl, ('SPG',)):
			usable.append(candidate)
		elif c_cls == 'lightTank' and _is_dedicated_scout_lt(candidate) and max_allowed >= 6 and max(5 if max_allowed >= 9 else 4, max_allowed - 5) <= c_lvl <= min(8, max_allowed):
			usable.append(candidate)
	# Safety guard: if usable lacks core HT/MT/TD combat tanks (e.g. caller passed a narrow pool),
	# include all non-SPG vehicles from pool within [player_tier - 2, max_allowed]
	has_core_combat = any(vehicle_match_class(c) in ('heavyTank', 'mediumTank', 'AT-SPG') for c in usable)
	if not has_core_combat and pool:
		for candidate in pool:
			c_lvl = int(candidate.get('level', 0) or 0)
			if max(1, player_tier - 2) <= c_lvl <= max_allowed and candidate not in usable:
				usable.append(candidate)
		allowed = sorted(set(int(c.get('level', player_tier) or player_tier) for c in usable if vehicle_match_class(c) != 'SPG'))[-3:] or [player_tier]
		max_allowed = max(allowed)
		min_allowed = min(allowed)
	if not usable:
		usable = [player_candidate]
	required = list(required_profiles) if required_profiles is not None else [player_candidate]
	required = required[:team_size]

	# Weighted 2012 tier distribution: ~25% top tier, ~35% mid tier, ~40% bottom tier
	tier_slots = []
	if len(allowed) == 3:
		_t_low, _t_mid, _t_high = allowed[0], allowed[1], allowed[2]
		_t_pattern = (
			_t_high, _t_high, _t_high, _t_high,
			_t_mid, _t_mid, _t_mid, _t_mid, _t_mid,
			_t_low, _t_low, _t_low, _t_low, _t_low, _t_low
		)
	else:
		_t_pattern = tuple(allowed) if allowed else (player_tier,)
	while len(tier_slots) < team_size:
		for level in _t_pattern:
			if len(tier_slots) >= team_size:
				break
			tier_slots.append(level)
	try:
		rng.shuffle(tier_slots)
	except Exception:
		random.shuffle(tier_slots)

	# Roll authentic 2012 WoT 0.8.2 Battle Flavor across Tiers 1..10 (shared across both teams):
	try:
		_flavor_roll = _bracket_rng.random()
	except Exception:
		_flavor_roll = 0.25
	if max_allowed == 1:
		_mm_flavor = 'tier1_sandbox_2012'
		_target_spgs = 0
		_max_lts_for_match = team_size
		regular_classes = (
			'lightTank', 'lightTank', 'lightTank', 'mediumTank',
			'lightTank', 'lightTank', 'mediumTank')
	elif max_allowed <= 3:
		_mm_flavor = 'low_tier_2012'
		_target_spgs = 1 if (_flavor_roll < 0.60) else 0
		_max_lts_for_match = max(6, int(team_size * 0.55))
		regular_classes = (
			'mediumTank', 'mediumTank', 'mediumTank',
			'AT-SPG', 'AT-SPG',
			'lightTank', 'lightTank', 'lightTank', 'lightTank', 'lightTank')
	elif max_allowed == 4:
		# Tier 4 transition: only 1-2 HTs exist at Tier 4 (French B1), while MTs/TDs/LTs form the core!
		_mm_flavor = 'tier4_transition_2012'
		_target_spgs = 1 if (_flavor_roll < 0.70) else 2
		_max_lts_for_match = 4
		regular_classes = (
			'heavyTank', 'heavyTank',
			'mediumTank', 'mediumTank', 'mediumTank', 'mediumTank', 'mediumTank',
			'AT-SPG', 'AT-SPG', 'AT-SPG',
			'lightTank', 'lightTank', 'lightTank')
	elif _flavor_roll < 0.65:
		_mm_flavor = 'standard_2012'
		_target_spgs = 1 if (_flavor_roll < 0.35) else 2
		_max_lts_for_match = 2
		regular_classes = (
			'heavyTank', 'heavyTank', 'heavyTank', 'heavyTank',
			'mediumTank', 'mediumTank', 'mediumTank', 'mediumTank',
			'AT-SPG', 'AT-SPG', 'AT-SPG', 'lightTank', 'lightTank')
	elif _flavor_roll < 0.80:
		_mm_flavor = 'arty_party_2012'
		_target_spgs = 3 if (_flavor_roll < 0.74) else 4
		_max_lts_for_match = 2
		regular_classes = (
			'heavyTank', 'heavyTank', 'heavyTank',
			'mediumTank', 'mediumTank', 'mediumTank',
			'AT-SPG', 'AT-SPG', 'AT-SPG',
			'lightTank', 'lightTank')
	elif _flavor_roll < 0.92:
		_mm_flavor = 'heavy_fist_2012'
		_target_spgs = 0 if (_flavor_roll < 0.85) else 1
		_max_lts_for_match = 1
		regular_classes = (
			'heavyTank', 'heavyTank', 'heavyTank', 'heavyTank', 'heavyTank', 'heavyTank',
			'mediumTank', 'mediumTank', 'mediumTank',
			'AT-SPG', 'AT-SPG', 'AT-SPG', 'AT-SPG', 'lightTank')
	else:
		_mm_flavor = 'wolfpack_scouts_2012'
		_target_spgs = 1 if (_flavor_roll < 0.96) else 2
		_max_lts_for_match = 3
		regular_classes = (
			'heavyTank', 'heavyTank', 'heavyTank',
			'mediumTank', 'mediumTank', 'mediumTank', 'mediumTank', 'mediumTank',
			'AT-SPG', 'AT-SPG', 'lightTank', 'lightTank', 'lightTank')

	class_slots = []
	while len(class_slots) < team_size:
		for class_tag in regular_classes:
			if len(class_slots) >= team_size:
				break
			class_slots.append(class_tag)
	has_spg = any(vehicle_match_class(candidate) == 'SPG' for candidate in (usable + list(pool or ())))
	required_spgs = sum(vehicle_match_class(profile) == 'SPG' for profile in required)
	_max_spgs_for_match = max(required_spgs, _target_spgs) if has_spg else required_spgs
	if team_size < 10:
		_max_spgs_for_match = min(_max_spgs_for_match, 1)
	# Never overwrite lightTank scout slots at the end of class_slots when inserting SPGs!
	_spg_inserted = 0
	for _cs_i in range(len(class_slots)):
		if _spg_inserted >= _max_spgs_for_match:
			break
		if class_slots[_cs_i] != 'lightTank':
			class_slots[_cs_i] = 'SPG'
			_spg_inserted += 1
	try:
		rng.shuffle(class_slots)
	except Exception:
		random.shuffle(class_slots)

	_offh_ai_log_note('MM_2012 flavor=%s player=%s(T%d) battle_tiers=%s spg_cap=%d' % (
		_mm_flavor, player_name, player_tier, str(allowed), _max_spgs_for_match))

	result = []
	usage = {}
	spg_count = 0
	lt_count = 0
	for profile in required:
		desired_tier = int(profile.get('level', player_tier) or player_tier)
		desired_class = vehicle_match_class(profile)
		choices = [candidate for candidate in usable
			if int(candidate.get('level', 0) or 0) == desired_tier and
			vehicle_match_class(candidate) == desired_class]
		candidate = choices[0] if choices else profile
		result.append(candidate)
		name = str(candidate.get('name', ''))
		usage[name] = usage.get(name, 0) + 1
		if desired_class == 'SPG':
			spg_count += 1
		elif desired_class == 'lightTank':
			lt_count += 1
		if desired_tier in tier_slots:
			tier_slots.remove(desired_tier)
		elif tier_slots:
			tier_slots.pop()
		if desired_class in class_slots:
			class_slots.remove(desired_class)
		elif class_slots:
			class_slots.pop()

	for index in range(len(result), team_size):
		desired_tier = tier_slots.pop() if tier_slots else max_allowed
		desired_class = class_slots.pop() if class_slots else player_class
		if desired_class == 'lightTank' and lt_count >= _max_lts_for_match and max_allowed >= 4:
			desired_class = 'mediumTank' if (index % 2 == 0) else 'heavyTank'
		choices = [candidate for candidate in usable
			if int(candidate.get('level', 0) or 0) == desired_tier and
			vehicle_match_class(candidate) == desired_class]
		if not choices and desired_class == 'SPG':
			choices = [candidate for candidate in (pool or ())
				if vehicle_match_class(candidate) == 'SPG' and
				vehicle_in_battle_tier_band(max_allowed, candidate.get('level', 0), candidate.get('tags', ()))]
		elif not choices and desired_class == 'lightTank':
			choices = [candidate for candidate in usable
				if vehicle_match_class(candidate) == 'lightTank' and (_is_dedicated_scout_lt(candidate) or max_allowed <= 4)]
		if not choices:
			choices = [candidate for candidate in usable
				if vehicle_match_class(candidate) == desired_class]
		if not choices:
			choices = [candidate for candidate in usable
				if int(candidate.get('level', 0) or 0) == desired_tier and
				(vehicle_match_class(candidate) != 'lightTank' or lt_count < _max_lts_for_match or max_allowed <= 3)]
		if not choices:
			choices = [candidate for candidate in usable
				if vehicle_match_class(candidate) in ('heavyTank', 'mediumTank', 'AT-SPG')]
		if not choices:
			choices = list(usable)
		if spg_count >= _max_spgs_for_match:
			regular = [candidate for candidate in choices
				if vehicle_match_class(candidate) != 'SPG']
			if regular:
				choices = regular
		if lt_count >= _max_lts_for_match and max_allowed >= 4:
			non_lt = [candidate for candidate in choices
				if vehicle_match_class(candidate) != 'lightTank']
			if non_lt:
				choices = non_lt
		try:
			rng.shuffle(choices)
		except Exception:
			random.shuffle(choices)
		# Limit duplicate identical tanks to at most 2 per team when alternatives exist
		candidate = min(choices, key=lambda value:
			usage.get(str(value.get('name', '')), 0))
		name = str(candidate.get('name', ''))
		usage[name] = usage.get(name, 0) + 1
		if vehicle_match_class(candidate) == 'SPG':
			spg_count += 1
		elif vehicle_match_class(candidate) == 'lightTank':
			lt_count += 1
		result.append(candidate)
	return result


def remaining_match_template(template, human_profiles):
	"""Remove the closest template slot for each human already on a team."""
	remaining = list(template or ())
	for human in (human_profiles or ()):
		if not remaining:
			break
		human_tier = int(human.get('level', 0) or 0)
		human_class = vehicle_match_class(human)
		best_index = 0
		best_score = None
		for index, candidate in enumerate(remaining):
			candidate_tier = int(candidate.get('level', 0) or 0)
			candidate_class = vehicle_match_class(candidate)
			score = abs(candidate_tier - human_tier) * 3
			if candidate_class != human_class:
				score += 8
			if best_score is None or score < best_score:
				best_score = score
				best_index = index
		remaining.pop(best_index)
	return remaining


def bot_initially_visible(bot_team, player_team, spotting_enabled):
	"""Hide an enemy model before its first spotting update can run."""
	try:
		return (not bool(spotting_enabled) or
			int(bot_team) == int(player_team))
	except Exception:
		return not bool(spotting_enabled)


def entity_visible_to_minimap(entity):
	"""Expose a mock entity to the minimap only after its first spot."""
	return bool(getattr(entity, '_spot_visible', True))


def trimmed_sight_segment(observer, target, observer_height=2.5,
		target_height=1.5, start_clearance=4.0, end_clearance=4.0):
	"""Return a world ray that excludes the two vehicle hull volumes.

	The legacy battle creates a ``PyModelObstacle`` for every mock vehicle.  A
	world ray from centre to centre can therefore hit its shooter immediately or
	the intended target at the far end and report a blocked lane.  Static-world
	LOS only needs the part *between* the vehicles.  Very close vehicles have no
	meaningful middle segment and are considered mutually exposed by returning
	``None``.
	"""
	try:
		ox = float(observer[0]); oy = float(observer[1]); oz = float(observer[2])
		tx = float(target[0]); ty = float(target[1]); tz = float(target[2])
		dx = tx - ox
		dz = tz - oz
		distance = math.sqrt(dx * dx + dz * dz)
		start_clearance = min(max(0.0, float(start_clearance)), distance * 0.35)
		end_clearance = min(max(0.0, float(end_clearance)), distance * 0.35)
		if distance <= 0.5:
			return None
		unit_x = dx / distance
		unit_z = dz / distance
		return (
			(ox + unit_x * start_clearance, oy + float(observer_height),
			 oz + unit_z * start_clearance),
			(tx - unit_x * end_clearance, ty + float(target_height),
			 tz - unit_z * end_clearance),
		)
	except Exception:
		return ()


def route_toward_enemy(route, team, bases):
	"""Orient a route from its own flag toward its tactical objective."""
	result = dict(route or {})
	result['role_weights'] = dict(result.get('role_weights', {}) or {})
	points = list(result.get('waypoints', ()) or ())
	if len(points) < 2 or result.get('pre_oriented'):
		result['waypoints'] = tuple(points)
		return result
	try:
		own = bases.get(int(team))
		enemy = bases.get(3 - int(team))
		if own is None or enemy is None or own == enemy:
			result['waypoints'] = tuple(points)
			return result
		def _distance(point, base):
			return ((float(point[0]) - float(base[0])) ** 2 +
				(float(point[1]) - float(base[1])) ** 2)
		forward = _distance(points[0], own) + _distance(points[-1], enemy)
		reverse = _distance(points[-1], own) + _distance(points[0], enemy)
		if reverse < forward:
			points.reverse()
		own_point = (float(own[0]), float(own[1]), 0)
		if _distance(points[0], own) > 5625.0:
			points.insert(0, own_point)
		enemy_point = (float(enemy[0]), float(enemy[1]), 0)
		_rid_l = str(result.get('id', '')).lower()
		if ('cap' in _rid_l or 'base' in _rid_l or _distance(points[-1], enemy) <= 18225.0) and _distance(points[-1], enemy) > 484.0:
			points.append(enemy_point)
	except Exception:
		pass
	result['waypoints'] = tuple(points)
	return result


def _mapping_get(value, key, default=None):
	try:
		if hasattr(value, 'get'):
			return value.get(key, default)
		return value[key]
	except Exception:
		return default


def _attribute_or_key(value, key, default=None):
	try:
		result = getattr(value, key)
		if result is not None:
			return result
	except Exception:
		pass
	return _mapping_get(value, key, default)


def stable_seed(*parts):
	"""Return the same positive integer on Python 2 and Python 3."""
	text_parts = []
	for part in parts:
		try:
			text_parts.append(str(part))
		except Exception:
			text_parts.append('?')
	payload = '|'.join(text_parts).encode('utf-8')
	return int(hashlib.sha1(payload).hexdigest()[:8], 16) & 0x7fffffff


def make_personality(seed, skill_tier=None):
	"""Create coherent per-battle tactical personality traits."""
	rng = random.Random(int(seed))
	traits = {}
	for name in ('aggression', 'caution', 'teamwork', 'patience',
	             'initiative', 'adaptability', 'jiggle'):
		traits[name] = 0.18 + rng.random() * 0.64
	traits['teamwork'] = max(0.65, min(0.98, traits['teamwork'] + 0.35))
	traits['initiative'] = max(0.60, min(0.95, traits['initiative'] + 0.30))
	traits['adaptability'] = max(0.60, min(0.95, traits['adaptability'] + 0.25))
	traits['jiggle'] = max(0.05, min(0.35, traits['jiggle'] * 0.4))
	traits['aggression'] = max(0.05, min(0.95,
		traits['aggression'] + (0.5 - traits['caution']) * 0.22))
	traits['caution'] = max(0.05, min(0.95,
		traits['caution'] + (0.5 - traits['aggression']) * 0.12))
	traits['route_jitter'] = rng.uniform(-1.5, 1.5)
	traits['hold_jitter'] = rng.uniform(-1.5, 1.5)
	traits['rusher_streak'] = rng.random()
	return traits


def _tags_from_descriptor(descriptor):
	type_info = _attribute_or_key(descriptor, 'type', None)
	tags = _attribute_or_key(type_info, 'tags', ()) or ()
	try:
		return tuple(tags)
	except Exception:
		return ()


def _forward_speed(descriptor):
	physics = _attribute_or_key(descriptor, 'physics', {}) or {}
	limits = _mapping_get(physics, 'speedLimits', None)
	try:
		return abs(float(limits[0]))
	except Exception:
		return 0.0


def _primary_armor(component):
	armor = _mapping_get(component or {}, 'primaryArmor', 0.0)
	if isinstance(armor, (tuple, list)):
		return max([_number(item, 0.0) for item in armor] or [0.0])
	return _number(armor, 0.0)


def _component_armor(component):
	"""Extract (front, side, rear) armor values for hull or turret."""
	if not component:
		return (0.0, 0.0, 0.0)
	armor = _mapping_get(component, 'primaryArmor', None)
	if armor is None:
		armor = _mapping_get(component, 'armor', None)
	if isinstance(armor, (tuple, list)) and len(armor) >= 3:
		return (_number(armor[0], 0.0), _number(armor[1], 0.0), _number(armor[2], 0.0))
	elif isinstance(armor, (tuple, list)) and len(armor) > 0:
		val = _number(armor[0], 0.0)
		return (val, val, val)
	val = _number(armor, 0.0)
	return (val, val, val)


def _has_pike_nose(vehicle_name):
	"""Check if tank has a pike nose (V-shaped pointed frontal armor hull).

	Angling the hull on a pike nose flattens the front cheek toward incoming fire,
	effectively reducing nominal angled thickness. Pike nose tanks MUST face directly.
	"""
	if not vehicle_name:
		return False
	name_lower = str(vehicle_name).lower()
	pike_tokens = ('is-3', 'is-7', 'is-8', 'is3', 'is7', 'is8', '110', 'wz-111', 'wz111', 'obj_268', 'object 268')
	for token in pike_tokens:
		if token in name_lower:
			return True
	return False



def _middle_value(value, default=0.0):
	"""Return a representative value for scalar or min/max vehicle data."""
	if isinstance(value, (tuple, list)):
		values = [_number(item, default) for item in value]
		if values:
			return sum(values) / float(len(values))
	return _number(value, default)


def _sequence_first(value, default=0.0):
	"""Return element 0 of a Vector2/tuple/list, or the value as a scalar."""
	if value is None:
		return float(default)
	if isinstance(value, (tuple, list)):
		if not value:
			return float(default)
		return _number(value[0], default)
	try:
		return _number(value[0], default)
	except Exception:
		return _number(value, default)


def _shell_penetration(shot, shell):
	"""Nominal penetration in mm at 100 m for one entry of gun['shots'].

	In 0.8.2 the value lives on the SHOT, not on the shell, and it is a Vector2
	(penetration at 100 m, penetration at maxDistance):
	  dev_ref/stock_082/common/items/vehicles.py:2352-2364 (_readShot) builds
	    {'shell', 'defaultPortion', 'piercingPower', 'speed', 'gravity',
	     'maxDistance'};
	  dev_ref/stock_082/common/items/vehicles.py:2300-2343 (_readShell) builds
	    {'caliber', 'isTracer', 'damage', 'kind', ...} and has NO piercingPower;
	  dev_ref/stock_082/common/items/vehicles.py:2076-2086 (_readGun) stores
	    res['shots'] = tuple(_readShot(...)).
	The mod already relies on this layout in _offh_penetration
	(offline_battle.py:2869, `shot.get('piercingPower', (100.0, 100.0))`) and at
	offline_battle.py:16371 (`shot['piercingPower']`).

	Reading it from `shell` instead missed every time, so each profile carried
	penetration 0.0: select_shell_index then fined every AP round -70 and the
	HE branch always won (log 01.10 23:26: 67/67 BOT_SHOT shell=2), and
	_choose_contact treated every target with armor >= 130 as impenetrable.
	Element [0] is the hangar number and matches the calibration of
	required_penetration in select_shell_index. The shell level and the
	alternative key names stay as fallbacks for descriptors not built by
	_readShot.
	"""
	for source in (shot, shell):
		for key in ('piercingPower', 'penetration', 'piercing'):
			value = _mapping_get(source, key, None)
			if value is not None:
				return max(0.0, _sequence_first(value, 0.0))
	return 0.0


def _shell_damage(shell):
	"""Hit-point damage of the shell.

	shell['damage'] is a pair (damage/armor, damage/devices) in 0.8.2 -
	dev_ref/stock_082/common/items/vehicles.py:2308-2310. Element [0] is the HP
	damage. The old _middle_value path averaged the pair and understated it
	(ST-I 122 mm printed 277.5 instead of 390), which also narrowed the
	"HE finishes the target" window in select_shell_index.
	"""
	return max(0.0, _sequence_first(_mapping_get(shell, 'damage', 0.0), 0.0))


def _shell_profiles(descriptor):
	"""Extract the small shell summary needed by the tactical planner."""
	gun = _attribute_or_key(descriptor, 'gun', {}) or {}
	shots = _mapping_get(gun, 'shots', ()) or ()
	result = []
	try:
		iterator = enumerate(shots)
	except Exception:
		iterator = ()
	for index, shot in iterator:
		shell = _mapping_get(shot, 'shell', {}) or {}
		kind = _mapping_get(shell, 'kind', '') or ''
		cal = _number(_mapping_get(shell, 'caliber', _mapping_get(shot, 'caliber', 0.0)), 0.0)
		result.append({
			'index': int(index),
			'kind': str(kind),
			'caliber': float(cal),
			'penetration': _shell_penetration(shot, shell),
			'damage': _shell_damage(shell),
			'speed': _number(_mapping_get(shot, 'speed', 0.0), 0.0),
			'gravity': abs(_number(_mapping_get(shot, 'gravity', 0.0), 0.0)),
		})
	return tuple(result)


_FIGHT_FIX_STATE = {'on': None, 'why': {}, 'at': 0.0}


def _fight_fix_on():
	""" flag `bot_fight_fix` (default true), read once per session."""
	on = _FIGHT_FIX_STATE['on']
	if on is None:
		try:
			from gui.mods.offhangar._constants import CONFIG_OPTIONS as _FF
			on = bool(_FF.get('bot_fight_fix', True))
		except Exception:
			on = True
		_FIGHT_FIX_STATE['on'] = on
		_offh_ai_log_note('FIGHT_FIX_CFG on=%s' % ('yes' if on else 'no'))
	return on


def _withdraw_why(reason, now):
	w = _FIGHT_FIX_STATE['why']
	w[reason] = w.get(reason, 0) + 1
	if now - _FIGHT_FIX_STATE['at'] >= 20.0:
		_FIGHT_FIX_STATE['at'] = now
		_offh_ai_log_note('WITHDRAW_WHY ' + ' '.join(
			'%s=%d' % (k, w[k]) for k in sorted(w)))
		_FIGHT_FIX_STATE['why'] = {}


try:
	_CLOCK_FN = __import__('time').clock
except AttributeError:
	_CLOCK_FN = __import__('time').time


_REAL_COVER_STATE = {'on': None, 'log': 0, 'found': 0, 'none': 0}


def _real_cover_on():
	""" flag `bot_real_cover` (default true), read once per session."""
	on = _REAL_COVER_STATE['on']
	if on is None:
		try:
			from gui.mods.offhangar._constants import CONFIG_OPTIONS as _RC
			on = bool(_RC.get('bot_real_cover', True))
		except Exception:
			on = True
		_REAL_COVER_STATE['on'] = on
		_offh_ai_log_note('REAL_COVER_CFG on=%s' % ('yes' if on else 'no'))
	return on


_FALLBACK_3D_GRIDS = {}


def _real_cover_grid(map_name=None):
	try:
		import sys as _rc_sys
		mod = _rc_sys.modules.get('gui.mods.offhangar.offline_battle')
		nav = getattr(mod, 'g_offh_terrain_navigator', None) if mod is not None else None
		grid = getattr(nav, 'grid', None)
		if grid is not None and getattr(grid, 'prebaked', False) and hasattr(grid, 'find_cover'):
			return grid
		m_name = map_name
		if not m_name and mod is not None:
			m_name = getattr(mod, 'g_offh_battle_mapname', None) or (
				getattr(getattr(mod, 'g_offh_bot_director', None), 'map_name', None))
		if m_name:
			m_short = str(m_name).replace('\\', '/').rstrip('/').split('/')[-1]
			if m_short in _FALLBACK_3D_GRIDS:
				return _FALLBACK_3D_GRIDS[m_short]
			from gui.mods.offhangar.prebaked_navigation import load_graph as _lg_rc
			from gui.mods.offhangar.bot_ai_navigation import TerrainGrid as _TG_rc
			b_graph = getattr(mod, 'g_offh_baked_navigation_graph', None) if mod is not None else None
			if not isinstance(b_graph, dict) or b_graph.get('map') != m_short:
				b_graph = _lg_rc(m_short)
			if isinstance(b_graph, dict):
				if len(_FALLBACK_3D_GRIDS) >= 1:
					_FALLBACK_3D_GRIDS.clear()
				g_inst = _TG_rc(lambda x, z, y: y, None, b_graph.get('bounds'), b_graph.get('cell_size', 4.0), baked_graph=b_graph)
				_FALLBACK_3D_GRIDS[m_short] = g_inst
				return g_inst
	except Exception:
		pass
	return None


_THREAT_PRIO_STATE = {'on': None}


def _threat_prio_on():
	""" flag `bot_threat_priority` (default true), read once per session."""
	on = _THREAT_PRIO_STATE['on']
	if on is None:
		try:
			from gui.mods.offhangar._constants import CONFIG_OPTIONS as _TP
			on = bool(_TP.get('bot_threat_priority', True))
		except Exception:
			on = True
		_THREAT_PRIO_STATE['on'] = on
		_offh_ai_log_note('THREAT_PRIO_CFG on=%s' % ('yes' if on else 'no'))
	return on


def _class_fire_ranges_cfg():
	"""Flag `bot_class_fire_ranges` (default True in v3.3)."""
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS as _CR
		return bool(_CR.get('bot_class_fire_ranges', True))
	except Exception:
		return True


def build_vehicle_profile(descriptor):
	"""Derive tactical roles from class tags plus available vehicle stats."""
	tags = _tags_from_descriptor(descriptor)
	class_tag = 'mediumTank'
	for candidate in ('heavyTank', 'mediumTank', 'lightTank', 'AT-SPG', 'SPG'):
		if candidate in tags:
			class_tag = candidate
			break

	role_defaults = {
		'heavyTank': {'brawler': 0.92, 'support': 0.55, 'flanker': 0.18,
		              'sniper': 0.20, 'scout': 0.05, 'artillery': 0.00},
		'mediumTank': {'brawler': 0.46, 'support': 0.76, 'flanker': 0.86,
		               'sniper': 0.48, 'scout': 0.42, 'artillery': 0.00},
		'lightTank': {'brawler': 0.10, 'support': 0.45, 'flanker': 0.72,
		              'sniper': 0.28, 'scout': 0.96, 'artillery': 0.00},
		'AT-SPG': {'brawler': 0.32, 'support': 0.78, 'flanker': 0.18,
		           'sniper': 0.92, 'scout': 0.08, 'artillery': 0.00},
		'SPG': {'brawler': 0.00, 'support': 0.10, 'flanker': 0.00,
		        'sniper': 0.16, 'scout': 0.00, 'artillery': 1.00},
	}
	roles = dict(role_defaults[class_tag])
	speed = _forward_speed(descriptor)
	hull = _attribute_or_key(descriptor, 'hull', {}) or {}
	turret = _attribute_or_key(descriptor, 'turret', {}) or {}
	armor = max(_primary_armor(hull), _primary_armor(turret))
	hull_armor = _component_armor(hull)
	turret_armor = _component_armor(turret)
	h_front, h_side, h_rear = hull_armor
	t_front, t_side, t_rear = turret_armor

	gun = _attribute_or_key(descriptor, 'gun', {}) or {}
	reload_time = _number(_mapping_get(gun, 'reloadTime', 4.0), 4.0)

	type_info = _attribute_or_key(descriptor, 'type', None)
	vehicle_name = _attribute_or_key(type_info, 'name', class_tag)
	level = 1
	try:
		level = int(_attribute_or_key(type_info, 'level', _attribute_or_key(descriptor, 'level', 1)) or 1)
	except Exception:
		level = 1
	level = max(1, min(10, level))

	is_infantry_lt = False
	if class_tag == 'lightTank':
		if (any(tok in str(vehicle_name or '') for tok in _WOT_082_INFANTRY_BRAWLER_LTS) or
				(0.0 < speed < 11.8) or (armor >= max(38.0, level * 10.5))):
			is_infantry_lt = True
			roles = {'brawler': 0.80, 'support': 0.68, 'flanker': 0.44,
			         'sniper': 0.32, 'scout': 0.20, 'artillery': 0.00}
		elif level <= 3:
			# At Tiers 1-3, Light Tanks form the primary battle line alongside early Mediums
			roles = {'brawler': 0.56, 'support': 0.68, 'flanker': 0.84,
			         'sniper': 0.40, 'scout': 0.60, 'artillery': 0.00}
	elif class_tag == 'mediumTank':
		if level <= 3 or armor >= max(60.0, level * 12.0):
			roles['brawler'] = max(roles['brawler'], 0.74)

	if speed >= 15.0 and not is_infantry_lt:
		roles['flanker'] = min(1.0, roles['flanker'] + 0.12)
		roles['scout'] = min(1.0, roles['scout'] + 0.08)
	elif speed and speed < 9.0:
		roles['flanker'] = max(0.0, roles['flanker'] - 0.16)
	if armor >= 120.0:
		roles['brawler'] = min(1.0, roles['brawler'] + 0.18)
		roles['sniper'] = max(0.0, roles['sniper'] - 0.08)

	desired_ranges = {
		'heavyTank': (95.0, 480.0),
		'mediumTank': (135.0 if level >= 4 else 110.0, 520.0),
		'lightTank': (105.0 if is_infantry_lt else (115.0 if level <= 3 else 150.0), 480.0),
		'AT-SPG': (320.0, 565.0),
		'SPG': (520.0, 1250.0),
	}
	desired_range, fire_range = desired_ranges[class_tag]
	if armor >= 120.0 and class_tag == 'AT-SPG':
		desired_range = 275.0
		fire_range = 565.0
	if _class_fire_ranges_cfg():
		if class_tag == 'AT-SPG':
			fire_range = max(fire_range, 520.0 if armor >= 120.0 else 565.0)
		elif class_tag == 'heavyTank':
			fire_range = max(fire_range, 480.0)
		elif class_tag in ('mediumTank', 'lightTank'):
			fire_range = max(fire_range, 500.0)

	dominant = 'support'
	dominant_score = -1.0
	for role_name, role_score in roles.items():
		if role_score > dominant_score:
			dominant = role_name
			dominant_score = role_score

	has_pike = _has_pike_nose(vehicle_name)
	has_limited_traverse = (class_tag in ('AT-SPG', 'SPG'))
	try:
		_tyl = _mapping_get(gun, 'turretYawLimits', None)
		if isinstance(_tyl, (tuple, list)) and len(_tyl) >= 2 and _tyl[0] is not None and _tyl[1] is not None:
			has_limited_traverse = (abs(float(_tyl[1]) - float(_tyl[0])) < 5.8)
		elif class_tag == 'AT-SPG':
			_vnl_td = str(vehicle_name or '').lower()
			if any(_tk in _vnl_td for _tk in ('m10_wolverine', 'm36_slagger', 'm18_hellcat', 't49', 'm8a1', 't25_2', 't28_prototype', 't30', 't110e4')):
				has_limited_traverse = False
	except Exception:
		pass
	sidescrape_capable = False
	if not has_limited_traverse and class_tag not in ('AT-SPG', 'SPG'):
		if h_side >= 75.0 or (h_front > 0.0 and (h_side / h_front) >= 0.58):
			sidescrape_capable = True
		vname_lower = str(vehicle_name or '').lower()
		for kw in ('maus', 'e-100', 'e100', 'e-75', 'e75', 'kv-4', 'kv4', 'is-4', 'is4', 'vk4502', 'churchill', 'black_prince', 'st_i', 'kv-3', 'kv-1'):
			if kw in vname_lower:
				sidescrape_capable = True
				break
	else:
		vname_lower = str(vehicle_name or '').lower()

	hull_down_specialist = False
	if t_front >= 160.0 and (h_front <= 0.0 or t_front >= h_front * 1.30):
		hull_down_specialist = True
	for kw in ('t29', 't34', 't30', 't32', 'conqueror', 'caernarvon', 'is-7'):
		if kw in vname_lower:
			hull_down_specialist = True
			break

	is_autoloader = False
	try:
		clip_cfg = _mapping_get(gun, 'clip', None)
		if isinstance(clip_cfg, (tuple, list)) and len(clip_cfg) >= 1 and int(clip_cfg[0] or 1) > 1:
			is_autoloader = True
	except Exception:
		pass
	for kw in ('amx_50', 'amx50', 'bat_chatillon', 'batchat', 'lorraine', 't69', 't54e1', 't57', 'amx_13', 'amx13', 'foch_155'):
		if kw in vname_lower:
			is_autoloader = True
			break

	return {
		'level': level,
		'class_tag': class_tag,
		'vehicle_name': str(vehicle_name or class_tag),
		'roles': roles,
		'dominant_role': dominant,
		'desired_range': desired_range,
		'fire_range': fire_range,
		'speed': speed,
		'armor': armor,
		'hull_armor': hull_armor,
		'turret_armor': turret_armor,
		'has_pike_nose': has_pike,
		'has_limited_traverse': has_limited_traverse,
		'sidescrape_capable': sidescrape_capable,
		'hull_down_specialist': hull_down_specialist,
		'is_autoloader': is_autoloader,
		'is_infantry_lt': is_infantry_lt,
		'reload_time': reload_time,
		'shells': _shell_profiles(descriptor),
	}


def select_shell_index(profile, target, personality, skill_tier='medium'):
	"""v6.8: TTX-based shell selection — only true HE/HESH/Derp guns or SPGs use HE;
	regular tanks strictly choose between AP (shell 0) and Gold APCR/HEAT (shell 1)."""
	shells = profile.get('shells', ()) or ()
	if not shells:
		return 0
	class_tag = str(profile.get('class_tag', 'mediumTank') or 'mediumTank')
	skill = 'hard'
	if skill == 'easy':
		return int(shells[0].get('index', 0))
	target_armor = max(0.0, _number(target.get('armor', 0.0), 0.0))
	target_health = max(0.0, _number(target.get('health', 0.0), 0.0))
	distance = max(0.0, _number(target.get('distance', 0.0), 0.0))
	target_cls = str(target.get('class_tag', '') or '')
	# [FIX v12.0] Account for sloped/angled effective armor (pike nose, sidescrape, heavy turret/hull angling)
	# so bots intelligently load Gold (APCR/HEAT) against angled Heavies/TDs and switch back to standard AP on soft targets!
	angle_factor = 1.18 if target_cls in ('heavyTank', 'AT-SPG') else (1.10 if target_cls == 'mediumTank' else 1.0)
	own_level = int(profile.get('level', 8) or 8)
	tgt_level = int(target.get('level', own_level) or own_level)
	tier_disadv = max(0, tgt_level - own_level)
	required_penetration = target_armor * angle_factor * (1.06 + tier_disadv * 0.09 + min(distance, 500.0) / 1800.0)
	std_pen = max(0.0, _number(shells[0].get('penetration', 0.0), 0.0))
	std_dmg = max(0.0, _number(shells[0].get('damage', 0.0), 0.0))
	max_non_he_pen = std_pen
	for _s in shells:
		_sk = str(_s.get('kind', '')).lower()
		if not ('explosive' in _sk and 'hollow' not in _sk):
			_sp = max(0.0, _number(_s.get('penetration', 0.0), 0.0))
			if _sp > max_non_he_pen:
				max_non_he_pen = _sp

	best_index = int(shells[0].get('index', 0))
	best_score = -1e18
	for s_pos, shell in enumerate(shells):
		kind = str(shell.get('kind', '')).lower()
		penetration = max(0.0, _number(shell.get('penetration', 0.0), 0.0))
		damage = max(0.0, _number(shell.get('damage', 0.0), 0.0))
		caliber = max(0.0, _number(shell.get('caliber', 0.0), 0.0))
		is_explosive = ('explosive' in kind and 'hollow' not in kind)
		margin = penetration - required_penetration
		score = min(margin, 55.0) * 0.32 + damage * 0.045
		if s_pos == 0 and penetration >= required_penetration * (1.04 if skill == 'hard' else 0.94):
			score += 32.0
		if penetration >= required_penetration:
			score += 45.0
		else:
			score -= min(75.0, abs(margin) * 0.60)
		if is_explosive:
			# Determine by TTX if this gun is actually meant to fire HE/HESH:
			# - SPG howitzers
			# - High-pen HESH / derp guns (penetration >= 105mm or >= 65% of standard AP pen, e.g. FV4202/Centurion 7/1 HESH, KV-2, SU-152)
			# - Large-caliber 150mm+ assault guns/TDs (caliber >= 149mm and HE damage >= 700 and HE pen >= 75mm, e.g. ISU-152, Obj 268, E-100 15cm, T110E4/E3)
			is_mid_tier_derp = (
				caliber >= 75.0 and
				0.0 < std_pen <= 70.0 and
				damage >= std_dmg * 1.22
			)
			is_he_gun = (
				class_tag == 'SPG' or
				penetration >= 105.0 or
				(std_pen > 0.0 and penetration >= std_pen * 0.65) or
				(caliber >= 149.0 and damage >= 700.0 and penetration >= 75.0) or
				is_mid_tier_derp
			)
			if not is_he_gun:
				# Regular medium/heavy/light/small-caliber TD guns (T-62A, M48, BatChat, IS-7, Obj 263, etc.)
				# must NEVER randomly load HE to chip for 80-90 HP!
				score = -1e9
			else:
				# True HE/HESH/Derp gun: fire HE when target armor is thin enough to penetrate with HE/HESH,
				# or on mid-tier derp guns whenever AP cannot reliably penetrate the target!
				if target_armor <= penetration * 0.92:
					score += 85.0
				elif is_mid_tier_derp and target_armor > std_pen * 0.88:
					score += 95.0
				elif target_armor > max_non_he_pen * 1.12 and target_health <= damage * 0.32:
					score += 48.0
				else:
					score -= 120.0
		if score > best_score:
			best_score = score
			best_index = int(shell.get('index', best_index))
	return max(0, best_index)


def _distance_2d(first, second):
	dx = _number(first[0]) - _number(second[0])
	z1 = _number(first[2]) if len(first) >= 3 else _number(first[1])
	z2 = _number(second[2]) if len(second) >= 3 else _number(second[1])
	dz = z1 - z2
	return math.sqrt(dx * dx + dz * dz)


def _angle_delta(target, current):
	delta = target - current
	while delta > math.pi:
		delta -= math.pi * 2.0
	while delta < -math.pi:
		delta += math.pi * 2.0
	return delta


def _map_data_with_baked_routes(map_data, baked_routes):
	"""Return tactical metadata with graph-validated locomotion waypoints."""
	if map_data is None or not isinstance(baked_routes, dict):
		return map_data
	result = dict(map_data)
	routes = {}
	for team in (1, 2):
		converted = []
		values = baked_routes.get(str(team), baked_routes.get(team, ())) or ()
		for raw in values:
			if not isinstance(raw, dict):
				continue
			waypoints = []
			for point in raw.get('waypoints', ()) or ():
				try:
					waypoints.append((float(point[0]), float(point[1]),
					                  bool(point[2]) if len(point) > 2 else False))
				except Exception:
					continue
			if not waypoints:
				continue
			route = dict(raw)
			route['role_weights'] = dict(raw.get('role_weights', {}) or {})
			route['waypoints'] = tuple(waypoints)
			converted.append(route)
		routes[team] = tuple(converted)
	if not routes.get(1) or not routes.get(2):
		return map_data
	result['routes'] = routes
	if 'spg_positions' in baked_routes and isinstance(baked_routes.get('spg_positions'), dict):
		result['spg_positions'] = baked_routes['spg_positions']
	if 'tactical_pockets' in baked_routes and isinstance(baked_routes.get('tactical_pockets'), (list, tuple)):
		result['tactical_pockets'] = baked_routes['tactical_pockets']
	return result


class BattleDirector(object):
	"""Shared per-battle planner for both teams."""

	def __init__(self, map_name, battle_seed, bases=None, bounds=None,
	             baked_routes=None):
		self.map_name = bot_ai_maps.normalize_map_name(map_name)
		if battle_seed is None or int(battle_seed or 0) <= 100:
			import time as _t_seed, struct as _st_seed
			try:
				_os_ent = _st_seed.unpack('<I', os.urandom(4))[0]
			except Exception:
				_os_ent = int(_t_seed.time() * 1000000.0)
			battle_seed = (int(_t_seed.time() * 1000.0) ^ _os_ent ^ (int(battle_seed or 0) * 2654435761)) & 0x7FFFFFFF
		self.battle_seed = stable_seed(battle_seed, self.map_name)
		self.team_plans = {}
		self.map_data = _map_data_with_baked_routes(
			bot_ai_maps.get_tactical_map(self.map_name), baked_routes)
		self.bases = {}
		self.bounds = bounds
		self.spg_positions = {1: (), 2: ()}
		self.tactical_pockets = ()
		self._pocket_buckets = {}
		if self.map_data is not None:
			self.bases.update(self.map_data.get('bases', {}))
			self.bounds = self.map_data.get('bounds', self.bounds)
			raw_spg = self.map_data.get('spg_positions') or {}
			if isinstance(raw_spg, dict):
				for tm_s in (1, 2):
					pts_s = raw_spg.get(str(tm_s), raw_spg.get(tm_s, ())) or ()
					clean_s = []
					for p_s in pts_s:
						try:
							clean_s.append((float(p_s[0]), float(p_s[1])))
						except Exception:
							pass
					self.spg_positions[tm_s] = tuple(clean_s)
			raw_pockets = self.map_data.get('tactical_pockets') or ()
			clean_pockets = []
			for pk in raw_pockets:
				try:
					if len(pk) >= 9:
						pt_t = (
							float(pk[0]), float(pk[1]), float(pk[2]),
							float(pk[3]), float(pk[4]), float(pk[5]),
							float(pk[6]), float(pk[7]), int(pk[8]))
						clean_pockets.append(pt_t)
						bx = int(math.floor(pt_t[0] / 32.0))
						bz = int(math.floor(pt_t[2] / 32.0))
						self._pocket_buckets.setdefault((bx, bz), []).append(pt_t)
				except Exception:
					pass
			self.tactical_pockets = tuple(clean_pockets)
		# The live arena definition is authoritative. Static tactical data only
		# supplies a fallback for tests or incomplete legacy DataSections.
		self.bases.update(dict(bases or {}))
		self.agents = {}
		self.contacts = {1: {}, 2: {}}
		self.wrecks = {}
		self.route_usage = {}
		# squads[team] -> ordered list of squad dicts: {'id', 'members' (bot_id
		# list in join order), 'leader_id'}. Join order is what registration
		# order already is (register_profile is called in ascending entity-id
		# order - see offline_battle.py's failover-determinism comment), so a
		# rebuilt director after an authority failover reassigns the exact
		# same squads and the exact same leaders without any extra state.
		self.squads = {1: [], 2: []}
		# bot_id -> {'command', 'issuer_id', 'issuer_team', 'position',
		# 'cell_position', 'target_id', 'issued_at'}. One entry per recruited
		# bot; a fresh call for the same bot replaces its entry outright.
		self.radio_orders = {}
		self.arena_bounds = None
		# v4.62 perf: order_for() is called once per living bot per tick, and
		# _choose_contact/_local_force_balance each used to re-walk the FULL
		# self.agents dict (and re-run _known_contacts' stale-pruning pass)
		# from scratch on every single one of those calls - O(contacts x
		# agents) work repeated per bot instead of once per team per frame.
		# These two caches make that O(1) amortised after the first bot of
		# each team touches them on a given tick: _known_contacts_cache holds
		# the already-pruned contact list per team, and _focus_counts_cache
		# holds a target_id -> count of agents currently aimed at it, built
		# in one pass over self.agents instead of one pass per contact.
		# Keyed by the 'now' value the frame's first caller passed in, so a
		# still-mid-tick multi-bot loop (same 'now' argument) reuses it and a
		# new tick (new 'now') rebuilds it exactly once, lazily.
		self._contacts_cache_key = None
		self._known_contacts_cache = {}
		self._focus_counts_cache = {}
		# {'mode', 'set_at', 'last_said_at'} or None - see player_advisory.
		self.player_advisory_state = None
		# (kind, speaker_id, extra) for offline_battle's bot chatter.
		self.chat_events = []
		# v4.66 доп.17 - king of the hill: off by default (normal CTF/base-
		# capture play is unaffected until offline_battle.py opts a battle
		# into it via set_king_of_hill).
		self.king_of_hill_enabled = False
		# v4.66 доп.18 - Assault-mode coordination: None means "not detected /
		# not an Assault battle", otherwise 1 or 2 names the attacking team
		# (WoT Assault always has exactly one attacker and one defender).
		self.assault_attacker_team = None
		# v4.73 - Vehicle discharge tracking for baiting and punishment
		self.vehicle_shot_history = {}

	def set_arena_bounds(self, bounds):
		"""(min_x, min_z, max_x, max_z), needed only to resolve ATTENTIONTOCELL."""
		try:
			self.arena_bounds = tuple(float(value) for value in bounds)
			if len(self.arena_bounds) != 4:
				self.arena_bounds = None
		except Exception:
			self.arena_bounds = None

	def set_king_of_hill(self, enabled):
		"""v4.66 доп.17 - opt this battle into central-point-holding squads.

		Call-site owned by offline_battle.py; this module only reacts to the
		flag, it never decides on its own which battles get it.
		"""
		self.king_of_hill_enabled = bool(enabled)

	def notify_base_capture(self, base_team, points, invaders_count, base_pos=None, is_domination=False, now=None):
		"""Alert the AI director when a team's base (or neutral Encounter point) is being captured,
		with 8.5s hysteresis so temporary invader drops or partial resets do not abort defense."""
		if not hasattr(self, '_base_capture_threats'):
			self._base_capture_threats = {}
		if not hasattr(self, '_assigned_base_defenders'):
			self._assigned_base_defenders = {1: set(), 2: set()}
		_ref_now = float(getattr(self, '_last_order_now', 0.0) or 0.0)
		now_val = float(now) if now is not None else _ref_now
		if _ref_now > 0.0 and abs(now_val - _ref_now) > 5.0:
			now_val = _ref_now
		b_team = int(base_team)
		if not isinstance(base_pos, (tuple, list)) or len(base_pos) < 2:
			base_pos = self.bases.get(b_team)
		pts = int(points or 0)
		invs = int(invaders_count or 0)
		prev_t = self._base_capture_threats.get(b_team) or {}
		prev_peak_invs = int(prev_t.get('peak_invaders', 0) or 0)
		prev_peak_until = float(prev_t.get('peak_invaders_until', 0.0) or 0.0)
		prev_hold_until = float(prev_t.get('hold_until', 0.0) or 0.0)
		if invs >= prev_peak_invs or now_val > prev_peak_until:
			peak_invs = invs
			peak_until = now_val + 8.5 if invs > 0 else 0.0
		else:
			peak_invs = prev_peak_invs
			peak_until = prev_peak_until
		hold_until = (now_val + 8.5) if (pts > 0 or invs > 0) else prev_hold_until
		self._base_capture_threats[b_team] = {
			'points': pts,
			'invaders_count': invs,
			'peak_invaders': peak_invs,
			'peak_invaders_until': peak_until,
			'hold_until': hold_until,
			'base_pos': (float(base_pos[0]), float(base_pos[1])) if (isinstance(base_pos, (tuple, list)) and len(base_pos) >= 2) else prev_t.get('base_pos'),
			'is_domination': bool(is_domination),
			'updated_at': now_val
		}
		if pts <= 0 and invs <= 0 and (now_val <= 0.0 or now_val > hold_until):
			self._assigned_base_defenders.setdefault(b_team, set()).clear()

	def _update_base_defenders(self, team, now):
		"""Selectively audit and assign capable tanks on `team` to defend their base, scaling
		with invader count (1, 2, 3+) and capture points while preserving hysteresis."""
		threats = getattr(self, '_base_capture_threats', None)
		if not threats:
			return set()
		t_id = int(team or 1)
		now_f = float(now or 0.0)
		assigned_map = self.__dict__.setdefault('_assigned_base_defenders', {1: set(), 2: set()})
		cache_key = (t_id, round(now_f, 1))
		if getattr(self, '_base_def_cache_key', None) == cache_key:
			return assigned_map.get(t_id, set())
		self._base_def_cache_key = cache_key
		threat = threats.get(t_id)
		if threat is None:
			assigned_map[t_id] = set()
			return set()
		pts = int(threat.get('points', 0) or 0)
		invs = int(threat.get('invaders_count', 0) or 0)
		peak_invs = int(threat.get('peak_invaders', invs) or invs)
		peak_until = float(threat.get('peak_invaders_until', 0.0) or 0.0)
		eff_invs = max(invs, peak_invs if now_f <= peak_until else invs)
		hold_until = float(threat.get('hold_until', 0.0) or 0.0)
		upd = float(threat.get('updated_at', 0.0) or 0.0)
		if upd > 0.0 and (now_f - upd) < -5.0:
			upd = now_f
			threat['updated_at'] = now_f
		if (pts <= 0 and eff_invs <= 0 and now_f > hold_until) or (now_f > 0.0 and upd > 0.0 and (now_f - upd) > 14.0):
			assigned_map[t_id] = set()
			return set()
		b_pos = threat.get('base_pos') or self.bases.get(t_id)
		if b_pos is None:
			assigned_map[t_id] = set()
			return set()
		prev_set = set(assigned_map.get(t_id) or ())
		candidates = []
		for ag in self.agents.values():
			if int(ag.get('team', 0) or 0) != t_id:
				continue
			hp_frac = _number(ag.get('health_fraction', 0.0), 0.0)
			if hp_frac <= 0.0 or _number(ag.get('hp_abs', 1.0), 1.0) <= 0.0:
				continue
			pos = ag.get('position')
			if pos is None:
				continue
			prof = ag.get('profile') or {}
			ctag = prof.get('class_tag', 'mediumTank')
			if ctag == 'SPG':
				continue
			dist = math.hypot(float(pos[0]) - float(b_pos[0]), float(pos[2]) - float(b_pos[1]))
			spd = max(6.5, _number(prof.get('max_speed', 12.0), 12.0))
			eta = dist / spd
			if ctag in ('lightTank', 'mediumTank'):
				eta -= 4.0
			# Selective audit: prefer healthy tanks for multi-invader rush, penalize critically low HP (< 22%) if far away
			if hp_frac < 0.22 and dist > 110.0:
				eta += 10.0
			elif hp_frac >= 0.55 and eff_invs >= 2:
				eta -= 2.5
			if int(ag['id']) in prev_set:
				eta -= 18.0
			# Check if locked in point-blank brawl (< 36m) away from base
			if dist > 95.0:
				last_ord = ag.get('last_order') or {}
				tid = last_ord.get('target_id')
				vis_c = (self.contacts.get(t_id) or {}).get(tid) if tid is not None else None
				if vis_c and vis_c.get('visible') and _distance_2d(pos, vis_c['position']) < 36.0:
					eta += 20.0
			candidates.append((eta, int(ag['id']), dist))
		if not candidates:
			assigned_map[t_id] = set()
			return set()
		candidates.sort(key=lambda item: item[0])
		# Scale defender count by effective invaders (1, 2, 3+) and cap points:
		if len(candidates) == 1:
			need_n = 1
		elif (eff_invs >= 3 or pts >= 70) and len(candidates) >= 4:
			need_n = 4
		elif (eff_invs >= 2 or pts >= 45) and len(candidates) >= 3:
			need_n = 3
		else:
			need_n = min(2, len(candidates))
		chosen = set(item[1] for item in candidates[:need_n])
		# Also include any additional ally already within 95m of the threatened base
		for _eta, _aid, _adist in candidates[need_n:]:
			if _adist <= 95.0 and len(chosen) < 5:
				chosen.add(_aid)
		if chosen != prev_set:
			_offh_ai_log_note('BASE_DEFENSE_ASSIGNED team=%d pts=%d inv=%d eff_inv=%d defenders=%s' % (
				t_id, pts, invs, eff_invs, sorted(list(chosen))))
		assigned_map[t_id] = chosen
		return chosen

	def _apply_base_defense_order(self, agent, order, position, contact, now, reload_time=4.0, shoot_timer=999.0):
		"""3D-grid-backed base defense execution with invader-count tactics:
		- 1 invader & pts < 65: careful cover/standoff approach (45m-80m) with aimed reset shots.
		- 2+ invaders or pts >= 65 (with 8.5s hysteresis): coordinated multi-angle rush (18m-38m)."""
		try:
			my_t = int(agent.get('team', 1) or 1)
			profile = agent.get('profile') or {}
			if profile.get('class_tag') == 'SPG':
				return False
			defenders = self._update_base_defenders(my_t, now)
			my_id = int(agent.get('id', 0) or 0)
			if my_id not in defenders:
				return False
			threat = (getattr(self, '_base_capture_threats', None) or {}).get(my_t)
			if not threat:
				return False
			b_pos = threat.get('base_pos') or self.bases.get(my_t)
			if b_pos is None:
				return False
			bx, bz = float(b_pos[0]), float(b_pos[1])
			now_f = float(now or 0.0)
			pts = int(threat.get('points', 0) or 0)
			invs = int(threat.get('invaders_count', 0) or 0)
			peak_invs = int(threat.get('peak_invaders', invs) or invs)
			peak_until = float(threat.get('peak_invaders_until', 0.0) or 0.0)
			eff_invs = max(invs, peak_invs if now_f <= peak_until else invs)
			rush_mode = bool(eff_invs >= 2 or pts >= 65)
			def_list = sorted(list(defenders))
			def_idx = def_list.index(my_id) if my_id in def_list else (my_id % 4)
			dist_to_base = math.hypot(float(position[0]) - bx, float(position[2]) - bz)
			grid = _real_cover_grid(self.map_name)
			base_y = grid.ground_height_3d(bx, bz, float(position[1])) if (grid is not None and hasattr(grid, 'ground_height_3d')) else float(position[1])
			base_3d = (bx, base_y, bz)

			# Find if there is a spotted/known capper inside or right next to the 50m capture circle
			capper_contact = None
			if contact is not None and contact.get('position') is not None and _distance_2d(contact['position'], base_3d) <= 54.0:
				capper_contact = contact
			else:
				for c in (self.contacts.get(my_t) or {}).values():
					if c.get('position') is not None and _distance_2d(c['position'], base_3d) <= 54.0:
						age_c = max(0.0, now_f - float(c.get('last_seen', -999.0) or -999.0))
						if c.get('visible') or age_c <= 22.0:
							capper_contact = c
							break

			# If there's an immediate, lethal visible threat within 85m or we are actively under fire,
			# and base capture is not yet critical (pts < 75), do NOT blindly tunnel-vision across the map!
			# Yield to standard tactical combat so the bot engages in self-defense with cover instead of exposing rear!
			if contact is not None and contact.get('visible') and pts < 75:
				_c_threat_d = _distance_2d(position, contact['position'])
				if _c_threat_d <= 85.0 or (now_f - float(agent.get('incoming_at', -999.0) or -999.0)) <= 4.0:
					return False

			if capper_contact is not None:
				c_pos = capper_contact['position']
				c_dist = _distance_2d(position, c_pos)
				s_ids = capper_contact.get('shootable_by_ids') or ()
				has_lane_cap = bool(capper_contact.get('visible') and (not s_ids or my_id in s_ids))
				rld_rem = max(0.0, _number(reload_time, 4.0) - _number(shoot_timer, 999.0))
				order['target_id'] = capper_contact['id']
				order['aim_position'] = c_pos
				order['face_position'] = self._angled_face_position(agent, position, c_pos, now=now)
				order['fire_allowed'] = has_lane_cap
				order['combat_mode'] = 'squad_defend_base'
				order['defense_base_id'] = my_t
				order['max_speed_mps'] = None
				agent['route_speed_limit_mps'] = None
				if rush_mode:
					# Coordinated multi-angle pincer rush (2+ invaders or urgent >=65 pts)
					if has_lane_cap and c_dist <= 155.0 and rld_rem <= 0.30:
						order['move_position'] = tuple(position)
						order['throttle_override'] = 0.0
						return True
					base_ang = math.atan2(float(position[2]) - float(c_pos[2]), float(position[0]) - float(c_pos[0]))
					pincer_ang = base_ang + ((def_idx % 4) - 1.5) * 0.65
					rush_r = 22.0 + float(def_idx % 3) * 6.0
					rush_pt = (float(c_pos[0]) + math.cos(pincer_ang) * rush_r, float(c_pos[1]), float(c_pos[2]) + math.sin(pincer_ang) * rush_r)
					if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
						rush_pt = grid.snap_to_safe_baked_point(rush_pt, 6) or self._spread_attack_position(agent, position, c_pos)
					order['move_position'] = rush_pt
					order['throttle_override'] = None
					return True
				else:
					# Careful tactical reset against 1 invader (pts < 65): use cover/hull-down standoff (45m-80m)
					if has_lane_cap and c_dist <= float(profile.get('fire_range', 500.0)) * 0.95:
						if c_dist <= 190.0 and rld_rem <= 0.45:
							order['move_position'] = tuple(position)
							order['throttle_override'] = 0.0
							return True
						if rld_rem > 1.2 and c_dist <= 130.0:
							cov_pt = self._reload_cover_position(agent, position, c_pos)
							if cov_pt is not None:
								order['move_position'] = cov_pt
								order['throttle_override'] = None
								return True
					base_ang = math.atan2(float(position[2]) - float(c_pos[2]), float(position[0]) - float(c_pos[0]))
					careful_ang = base_ang + ((def_idx % 3) - 1) * 0.52
					standoff_r = 52.0 + float(def_idx % 2) * 14.0
					careful_pt = (float(c_pos[0]) + math.cos(careful_ang) * standoff_r, float(c_pos[1]), float(c_pos[2]) + math.sin(careful_ang) * standoff_r)
					if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
						careful_pt = grid.snap_to_safe_baked_point(careful_pt, 6) or self._spread_attack_position(agent, position, c_pos)
					order['move_position'] = careful_pt
					order['throttle_override'] = None
					return True

			# Capper is NOT spotted yet, but base siren is active:
			# Rush mode drives deep into the circle (12m-20m); careful mode approaches the perimeter (24m-34m) with gun pre-aimed!
			sector_idx = (def_idx + int(now_f / 4.5)) % 4
			sweep_ang = sector_idx * (0.5 * math.pi) + (def_idx % 3) * 0.42
			if rush_mode:
				sweep_r = 18.0 if dist_to_base <= 36.0 else 24.0
			else:
				sweep_r = 28.0 if dist_to_base <= 48.0 else 38.0
			dest = (
				bx + math.cos(sweep_ang) * sweep_r,
				base_y,
				bz + math.sin(sweep_ang) * sweep_r,
			)
			if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
				dest = grid.snap_to_safe_baked_point(dest, 8) or base_3d
			order['move_position'] = dest
			if contact is not None and contact.get('visible'):
				order['target_id'] = contact['id']
				order['aim_position'] = contact.get('position', base_3d)
				order['face_position'] = self._angled_face_position(agent, position, contact['position'], now=now)
				order['fire_allowed'] = True
				rld_rem = max(0.0, _number(reload_time, 4.0) - _number(shoot_timer, 999.0))
				if not rush_mode and _distance_2d(position, contact['position']) <= 160.0 and rld_rem > 1.2:
					cov_pt = self._reload_cover_position(agent, position, contact['position'])
					if cov_pt is not None:
						order['move_position'] = cov_pt
						order['throttle_override'] = None
						return True
			else:
				order['target_id'] = None
				order['aim_position'] = base_3d
				order['face_position'] = dest
				order['fire_allowed'] = False
			order['throttle_override'] = None
			order['max_speed_mps'] = None
			agent['route_speed_limit_mps'] = None
			order['combat_mode'] = 'squad_defend_base'
			order['defense_base_id'] = my_t
			return True
		except Exception:
			return False

	def set_assault_mode(self, attacker_team):
		"""v4.66 доп.18 - tell this director which team is attacking in an
		Assault-mode battle, or None if this battle is not Assault at all.

		offline_battle.py resolves the actual gameplayName (see its own
		_offh_arena_type_matches / arena.arenaType.gameplayName reads) and the
		attacking team from the arena's own team data; this module never
		inspects engine objects itself, it only consumes the resolved value.
		"""
		try:
			self.assault_attacker_team = (
				int(attacker_team) if attacker_team is not None else None)
		except (TypeError, ValueError):
			self.assault_attacker_team = None

	def _hill_point(self):
		"""Return the map's central 'hill' point (or Encounter controlPoint) for holding."""
		cp = getattr(self, 'control_point', None)
		if cp is not None:
			try:
				return (float(cp[0]), float(cp[1]))
			except Exception:
				pass
		bounds = self.bounds or self.arena_bounds
		if bounds is not None:
			try:
				min_x, min_z, max_x, max_z = bounds
				return ((float(min_x) + float(max_x)) * 0.5,
				        (float(min_z) + float(max_z)) * 0.5)
			except Exception:
				pass
		base1 = self.bases.get(1)
		base2 = self.bases.get(2)
		if base1 is not None and base2 is not None:
			return ((float(base1[0]) + float(base2[0])) * 0.5,
			        (float(base1[1]) + float(base2[1])) * 0.5)
		return None

	def _cell_to_world(self, cell_index, current_y):
		"""10x10 minimap grid -> world point, cell 0 at the map's NW corner.

		Matches the stock client's own minimap-click convention (row-major,
		top row first) so a cell index taken straight from the client's click
		handler lands on the square the player actually clicked.
		"""
		bounds = self.arena_bounds
		try:
			cell_index = int(cell_index)
		except (TypeError, ValueError):
			return None
		if bounds is None or not (0 <= cell_index <= 99):
			return None
		min_x, min_z, max_x, max_z = bounds
		if not (min_x < max_x and min_z < max_z):
			return None
		# minimap.pyc's own _onMapClicked computes cell_index as
		# row * cellCount[1] + column (row-major) - so the inverse is
		# row, column = divmod(cell_index, 10), NOT column, row. Swapping
		# these two silently transposes the grid: a click near the top-right
		# of the minimap would have resolved to a world point near the
		# bottom-left.
		row, column = divmod(cell_index, 10)
		cell_w = (max_x - min_x) / 10.0
		cell_h = (max_z - min_z) / 10.0
		cx = min_x + (column + 0.5) * cell_w
		# Row 0 is the NORTH edge of the minimap (top of the screen), which is
		# the HIGH-z edge in world space.
		cz = max_z - (row + 0.5) * cell_h
		return (cx, float(current_y), cz)

	def apply_radio_command(self, command, issuer_id, issuer_team,
			issuer_position, now, cell_index=None, target_id=None,
			recipient_ids=None):
		"""Admit one player radio command exactly the way the stock client's
		own targeting rules say it should be addressed.

		Three shapes, matching CHAT_COMMANDS' own argsCnt/target semantics
		(see the RADIO_* comment block above - read from the shipped
		chat_shared.pyc, not guessed):
		  - RADIO_ALLY_TARGET_COMMANDS (STOP/TURNBACK/HELPMEEX/FOLLOWME with a
		    clicked ally): ``target_id`` is that ally's vehicle id. ONLY that
		    one bot receives the order - this is Avatar's own "private
		    address", not a recruitment call.
		  - RADIO_TARGET_COMMANDS (ATTACKENEMY/SUPPORTMEWITHFIRE): the
		    issuer's nearest squad is recruited to focus ``target_id`` (an
		    enemy vehicle id).
		  - everything else (ATTACK/BACKTOBASE/HELPME/ATTENTIONTOCELL): no
		    target at all - recruits the issuer's nearest squad the same way
		    a real HELPME call does in stock play.
		"""
		if command not in RADIO_COMMANDS:
			return ()
		# the same order can arrive by two paths (server.py + chat forward).
		_dk = (command, int(issuer_id or 0), target_id, cell_index)
		_dd = getattr(self, '_radio_dedupe', None)
		if _dd is None:
			_dd = self._radio_dedupe = {}
		if _dk in _dd and float(now) - _dd[_dk] < 1.5:
			return ()
		_dd[_dk] = float(now)
		if command in RADIO_ALLY_TARGET_COMMANDS:
			if target_id is None:
				return ()
			agent = self.agents.get(int(target_id))
			if (agent is None or agent.get('team') != issuer_team or
					agent.get('health_fraction', 1.0) <= 0.0):
				return ()
			if self._radio_soft():
				_p = 0.45 + 0.4 * _number(agent['personality'].get('teamwork', 0.5), 0.5)
				if self._radio_busy(agent):
					_p -= 0.25
				if self._radio_roll(agent, now, 1) > _p:
					_offh_ai_log_note('RADIO_SOFT cmd=%s issuer=%s asked=1 took=0' % (command, str(issuer_id)))
					return ()
			self.radio_orders[agent['id']] = {
				'command': command,
				'issuer_id': int(issuer_id),
				'issuer_team': int(issuer_team),
				'position': tuple(issuer_position) if issuer_position else None,
				'cell_position': None,
				'target_id': None,
				'issued_at': float(now),
			}
			return (agent['id'],)
		if command in RADIO_TARGET_COMMANDS and target_id is None:
			return ()
		cell_position = None
		if command == RADIO_COMMAND_ATTENTION_TO_CELL:
			cell_position = self._cell_to_world(
				cell_index, issuer_position[1] if issuer_position else 0.0)
			if cell_position is None:
				return ()
		if recipient_ids is not None:
			candidate_ids = [int(value) for value in recipient_ids]
		else:
			#  (bot_radio_calm): a BOT's own call recruits one bot, not a squad.
			_recruits = RADIO_HELP_RECRUITS
			if self._radio_calm() and int(issuer_id) in self.agents:
				_recruits = 1
			if self._radio_soft():
				# ask a few nearby allies; each decides for himself.
				_recruits = 1 if int(issuer_id) in self.agents else 2
				candidate_ids = self._nearest_squadmates(
					issuer_team, issuer_position, 5)
			else:
				candidate_ids = self._nearest_squadmates(
					issuer_team, issuer_position, _recruits)[:_recruits]
		_soft = self._radio_soft() and recipient_ids is None
		_asked = 0
		accepted = []
		for bot_id in candidate_ids:
			agent = self.agents.get(bot_id)
			if agent is None or agent.get('team') != issuer_team:
				continue
			if agent.get('health_fraction', 1.0) <= 0.0:
				continue
			if _soft:
				if int(bot_id) == int(issuer_id) or len(accepted) >= _recruits or _asked >= 3:
					continue
				if (issuer_position and agent.get('position') is not None and
						_distance_2d(issuer_position, agent['position']) > 250.0):
					continue
				_asked += 1
				_p = 0.08 + 0.42 * _number(agent['personality'].get('teamwork', 0.5), 0.5)
				if self._radio_busy(agent) and command not in RADIO_TARGET_COMMANDS:
					_p -= 0.25
				if self._radio_roll(agent, now, 2) > _p:
					continue
			self.radio_orders[bot_id] = {
				'command': command,
				'issuer_id': int(issuer_id),
				'issuer_team': int(issuer_team),
				'position': tuple(issuer_position) if issuer_position else None,
				'cell_position': cell_position,
				'target_id': target_id,
				'issued_at': float(now),
				'hold': RADIO_SOFT_HOLD_SECONDS if _soft else RADIO_COMMAND_HOLD_SECONDS,
			}
			accepted.append(bot_id)
		if _soft:
			_offh_ai_log_note('RADIO_SOFT cmd=%s issuer=%s asked=%d took=%d' % (
				command, str(issuer_id), _asked, len(accepted)))
		return tuple(accepted)

	def _callouts_on(self):
		""" flag bot_callouts, read once per director."""
		on = getattr(self, '_callouts_cache', None)
		if on is None:
			on = True
			try:
				from gui.mods.offhangar._constants import CONFIG_OPTIONS as _CO_CFG
				on = bool(_CO_CFG.get('bot_callouts', True))
			except Exception:
				pass
			self._callouts_cache = on
			_offh_ai_log_note('CALLOUTS_CFG on=%s' % ('yes' if on else 'no'))
		return on

	def _callout_event(self, kind, speaker, target, why=''):
		self.chat_events.append((kind, speaker, target, str(why or '')))
		del self.chat_events[:-16]
		_offh_ai_log_note('CALLOUT kind=%s speaker=%s target=%s why=%s' % (
			kind, str(speaker), str(target), why))

	def _callout_warnings(self, now):
		"""v5.1: Predictive danger warnings for allies and the player rushing into strong tanks, crossfire, or SPG fire."""
		battle_start = self.__dict__.setdefault('_co_battle_start_at', float(now))
		if float(now) - float(battle_start) < 25.0:
			return
		prev_map = self.__dict__.setdefault('_co_prev', {})
		still = self.__dict__.setdefault('_co_still', {})
		warned = self.__dict__.setdefault('_co_warned', {})
		team_last_warn = self.__dict__.setdefault('_co_team_last_warn', {})
		allies = []
		for ag in self.agents.values():
			if ag.get('health_fraction', 1.0) <= 0.0 or ag.get('position') is None:
				continue
			allies.append((ag['id'], ag['team'], ag['position'],
				_number(ag.get('hp_abs'), 0.0), ag.get('health_fraction', 1.0)))
		pvid = getattr(self, 'player_vid', None)
		if (pvid is not None and getattr(self, 'player_position', None) is not None and
				getattr(self, 'player_team', None) is not None):
			allies.append((int(pvid), int(self.player_team), self.player_position,
				_number(getattr(self, 'player_hp', 0.0), 0.0),
				_number(getattr(self, 'player_hp_frac', 1.0), 1.0)))
		spg_known_active = {}
		spg_targets = {}
		for ag in self.agents.values():
			if ag['profile'].get('class_tag') == 'SPG' and ag.get('health_fraction', 1.0) > 0.0:
				spg_team = int(ag['team'])
				obs_team = 1 if spg_team == 2 else 2
				spg_contact = (self.contacts.get(obs_team) or {}).get(ag['id'])
				spg_hist = self.vehicle_shot_history.get(int(ag['id']))
				spg_seen_or_heard = bool(
					(spg_contact is not None and (spg_contact.get('visible') or (now - float(spg_contact.get('last_seen', -999.0))) <= 30.0)) or
					(spg_hist is not None and (now - float(spg_hist.get('last_shot_time', -999.0))) <= 35.0))
				if spg_seen_or_heard:
					spg_known_active[spg_team] = True
					_lo_spg = ag.get('last_order') or {}
					if _lo_spg.get('target_id') is not None:
						spg_targets.setdefault(spg_team, set()).add(int(_lo_spg.get('target_id')))
		for key, team, pos, hp, hpf in allies:
			prev = prev_map.get(key)
			prev_map[key] = tuple(pos)
			if prev is not None and _distance_2d(prev, pos) < 1.2:
				still[key] = still.get(key, 0) + 1
			else:
				still[key] = 0
			if prev is None or now - warned.get(key, -1e9) < 45.0:
				continue
			if now - team_last_warn.get(team, -1e9) < 38.0:
				continue
			kind = None
			why = ''
			danger_enemy_id = None
			loaded_guns_ahead = 0
			for e in self.contacts.get(team, {}).values():
				if not e.get('visible') or _number(e.get('health'), 0.0) <= 0.0:
					continue
				# Never warn/brake for enemies behind buildings or terrain without a direct firing lane!
				if int(key) not in (e.get('shootable_by_bot_ids') or ()):
					continue
				d = _distance_2d(pos, e['position'])
				if d > 320.0:
					continue
				approach = _distance_2d(prev, e['position']) - d > 2.2
				if not approach:
					continue
				ea = self.agents.get(e['id'])
				hist = self.vehicle_shot_history.get(int(e['id']))
				loaded = hist is None or now >= (hist['last_shot_time'] + hist['reload_time'] - 1.2)
				if loaded and d < 285.0:
					loaded_guns_ahead += 1
				if ea is not None and hp > 0.0:
					shells = ea['profile'].get('shells', ()) or ()
					alpha = _number(shells[0].get('damage', 0.0), 0.0) if shells else 0.0
					e_armor = _number(ea['profile'].get('armor', 0.0), 0.0)
					e_class = ea['profile'].get('class_tag', '')
					if loaded and (alpha >= hp * 0.75 or (alpha >= 440.0 and d < 260.0)):
						kind = 'warn_stop' if d < 220.0 else 'warn_back'
						why = 'enemy:%s' % str(e['id'])
						danger_enemy_id = e['id']
						break
					if loaded and (e_armor >= 175.0 or e_class in ('heavyTank', 'AT-SPG')) and d < 230.0 and hpf < 0.65:
						kind = 'warn_strong'
						why = 'enemy:%s' % str(e['id'])
						danger_enemy_id = e['id']
						break
				ef = _number(e.get('health'), 0.0) / max(_number(e.get('max_health'), 1.0), 1.0)
				if hpf < 0.40 and d < 280.0 and ef > max(0.5, hpf + 0.2):
					kind = 'warn_back'
					why = 'enemy:%s' % str(e['id'])
					danger_enemy_id = e['id']
					break
				if danger_enemy_id is None:
					danger_enemy_id = e['id']
			if kind is None and loaded_guns_ahead >= 2 and danger_enemy_id is not None:
				kind = 'warn_stop'
				why = 'enemy:%s' % str(danger_enemy_id)
			enemy_team = 2 if team == 1 else 1
			if kind is None and spg_known_active.get(enemy_team):
				seen = self.contacts.get(enemy_team, {}).get(key)
				is_spg_target = int(key) in spg_targets.get(enemy_team, set())
				last_arty_warn = self.__dict__.setdefault('_co_team_arty_warn', {}).get(team, -1e9)
				if (seen is not None and seen.get('visible') and is_spg_target and still.get(key, 0) >= 6 and (now - last_arty_warn) >= 65.0):
					self._co_team_arty_warn[team] = now
					kind = 'warn_arty'
					why = 'arty'
			if kind is None:
				continue
			speaker = None
			best_d = 320.0
			for ag in self.agents.values():
				if (ag['id'] == key or ag['team'] != team or ag.get('position') is None or
						ag.get('health_fraction', 1.0) <= 0.0):
					continue
				d = _distance_2d(pos, ag['position'])
				if d < best_d:
					best_d = d
					speaker = ag
			warned[key] = now
			if speaker is None:
				continue
			if self._radio_roll(speaker, now, 7) > 0.55 + 0.35 * _number(
					speaker['personality'].get('teamwork', 0.5), 0.5):
				continue
			team_last_warn[team] = now
			self._callout_event(kind, speaker['id'], key, why)
			if key in self.agents:
				warned_ag = self.agents[key]
				# Warned bot heeds the warning unless it is an ultra-reckless rusher
				if not _is_rusher(warned_ag) or warned_ag.get('health_fraction', 1.0) < 0.55:
					warned_ag['heeded_warning_until'] = now + 5.5
					warned_ag['heeded_warning_kind'] = kind
					warned_ag['heeded_warning_enemy'] = danger_enemy_id
				self.apply_radio_command(
					RADIO_COMMAND_STOP if kind in ('warn_stop', 'warn_strong') else RADIO_COMMAND_TURNBACK,
					speaker['id'], team, speaker['position'], now, target_id=key)

	def _radio_soft(self):
		""" flag bot_radio_soft, read once per director."""
		soft = getattr(self, '_radio_soft_cache', None)
		if soft is None:
			soft = True
			try:
				from gui.mods.offhangar._constants import CONFIG_OPTIONS as _RS_CFG
				soft = bool(_RS_CFG.get('bot_radio_soft', True))
			except Exception:
				pass
			self._radio_soft_cache = soft
			_offh_ai_log_note('RADIO_SOFT_CFG on=%s' % ('yes' if soft else 'no'))
		return soft

	def _radio_roll(self, agent, now, salt):
		"""deterministic 0..1 per bot/moment (same on every authority)."""
		v = (int(agent.get('seed', 0)) * 2654435761 + int(float(now) * 4.0) * 40503 + salt * 977) & 0xffffffff
		v ^= v >> 15
		v = (v * 2246822519) & 0xffffffff
		v ^= v >> 13
		return (v & 0xffff) / 65535.0

	def _radio_busy(self, agent):
		order = agent.get('last_order') or {}
		return bool(agent.get('target_id') is not None and
			order.get('fire_allowed', False)) or order.get('combat_mode') in (
			'engage', 'attack', 'brawl', 'snipe')

	def _radio_calm(self):
		""" flag bot_radio_calm, read once per director."""
		calm = getattr(self, '_radio_calm_cache', None)
		if calm is None:
			calm = False
			try:
				from gui.mods.offhangar._constants import CONFIG_OPTIONS as _RC_CFG
				calm = bool(_RC_CFG.get('bot_radio_calm', False))
			except Exception:
				calm = False
			self._radio_calm_cache = calm
			_offh_ai_log_note('RADIO_CALM_CFG on=%s' % calm)
		return calm

	def _nearest_squadmates(self, team, position, count):
		"""Prefer one whole nearby squad over an arbitrary nearest-N scatter.

		A HELPME that always answers with 'whoever is closest, individually'
		tends to strip one gun each from three different squads instead of
		sending one coherent fireteam - the same dilution problem the squad
		system exists to avoid elsewhere.
		"""
		if position is None:
			return ()
		best_squad = None
		best_distance = None
		for squad in self.squads.get(int(team), ()):
			members = self._squad_members(squad)
			if not members:
				continue
			leader = self.agents.get(squad.get('leader_id')) or members[0]
			leader_position = leader.get('position')
			if leader_position is None:
				continue
			distance = _distance_2d(position, leader_position)
			if best_distance is None or distance < best_distance:
				best_distance = distance
				best_squad = squad
		if best_squad is not None:
			members = self._squad_members(best_squad)
			members.sort(key=lambda member: _distance_2d(
				position, member.get('position') or position))
			return tuple(member['id'] for member in members[:count])
		everyone = [agent for agent in self.agents.values()
			if agent.get('team') == int(team) and
			agent.get('health_fraction', 1.0) > 0.0 and
			agent.get('position') is not None]
		everyone.sort(key=lambda agent: _distance_2d(position, agent['position']))
		return tuple(agent['id'] for agent in everyone[:count])

	def _radio_order_for(self, agent, now):
		"""Return this bot's live radio order, expiring a stale one lazily."""
		order = self.radio_orders.get(agent['id'])
		if order is None:
			return None
		if now - float(order.get('issued_at', 0.0)) > float(order.get('hold', RADIO_COMMAND_HOLD_SECONDS)):
			del self.radio_orders[agent['id']]
			return None
		return order

	def register(self, bot_id, team, descriptor, display_name='Bot', skill_tier='medium', spawn_position=None, preassigned_route_id=None, prebattle_id=0):
		# Descriptor parsing is immutable and materially expensive on Python 2.6.
		# The render callback may see an already registered vehicle many times; avoid
		# rebuilding its tactical profile before register_profile can return it.
		existing = self.agents.get(int(bot_id))
		if existing is not None:
			if skill_tier:
				existing['skill_tier'] = str(skill_tier).lower()
			if spawn_position is not None and existing.get('position') is None:
				existing['position'] = (float(spawn_position[0]), float(spawn_position[1]), float(spawn_position[2]))
			if prebattle_id:
				existing['prebattle_id'] = int(prebattle_id)
			return existing
		return self.register_profile(
			bot_id, team, build_vehicle_profile(descriptor), display_name, skill_tier=skill_tier, spawn_position=spawn_position, preassigned_route_id=preassigned_route_id, prebattle_id=prebattle_id)

	def register_profile(self, bot_id, team, profile, display_name='Bot', skill_tier='medium', spawn_position=None, preassigned_route_id=None, prebattle_id=0):
		"""Register serialized profile data on either client or LAN server."""
		bot_id = int(bot_id)
		agent = self.agents.get(bot_id)
		if agent is not None:
			if skill_tier:
				agent['skill_tier'] = str(skill_tier).lower()
			if spawn_position is not None and agent.get('position') is None:
				agent['position'] = (float(spawn_position[0]), float(spawn_position[1]), float(spawn_position[2]))
			return agent
		profile = dict(profile or {})
		profile['roles'] = dict(profile.get('roles', {}) or {})
		profile['shells'] = tuple(profile.get('shells', ()) or ())
		seed = stable_seed(self.battle_seed, bot_id, display_name,
		                   profile.get('vehicle_name'))
		_init_pos = None
		if spawn_position is not None:
			try:
				_init_pos = (float(spawn_position[0]), float(spawn_position[1]), float(spawn_position[2]))
			except Exception:
				_init_pos = None
		agent = {
			'id': bot_id,
			'team': int(team),
			'profile': profile,
			'personality': make_personality(seed, skill_tier=skill_tier),
			'seed': seed,
			'skill_tier': 'hard',
			'cyber_bait_state': 'idle',
			'cyber_bait_timer': 0.0,
			'cyber_bait_target_id': None,
			'cyber_bait_start_pos': None,
			'cyber_bait_cooldown_until': 0.0,
			'last_shot_at': -999.0,
			'shot_count': 0,
			'route': None,
			'waypoint_index': 0,
			'target_id': None,
			'last_order': None,
			'position': _init_pos,
			'preassigned_route_id': str(preassigned_route_id) if preassigned_route_id else None,
			'prebattle_id': int(prebattle_id or 0),
			'health_fraction': 1.0,
			'squad_id': None,
			'squad_role': None,
		}
		if int(prebattle_id or 0) > 0 and not agent.get('preassigned_route_id'):
			for _pm_ag in self.agents.values():
				if int(_pm_ag.get('team', 0) or 0) == int(team) and int(_pm_ag.get('prebattle_id', 0) or 0) == int(prebattle_id):
					_pm_rid = (_pm_ag.get('route') or {}).get('id')
					if _pm_rid and _pm_rid != 'rear_guard':
						agent['preassigned_route_id'] = _pm_rid
						break
		agent['route'] = self._assign_route(agent)
		self.agents[bot_id] = agent
		self._assign_squad(agent)
		return agent

	def _d82_cfg(self):
		"""(squad_mix, hill_hold_s), read once per director."""
		c = getattr(self, '_d82_cache', None)
		if c is None:
			mix, hold = True, 75.0
			try:
				from gui.mods.offhangar._constants import CONFIG_OPTIONS as _D82
				mix = bool(_D82.get('bot_squad_mix', True))
				hold = max(0.0, float(_D82.get('bot_hill_hold_s', 75.0)))
			except Exception:
				pass
			c = self._d82_cache = (mix, hold)
			_offh_ai_log_note('SQUAD_MIX_CFG mix=%s hill_hold_s=%.0f' % ('yes' if mix else 'no', hold))
		return c

	def _squad_cohesion_on(self):
		""" flag, read once per director."""
		on = getattr(self, '_squad_cohesion_cache', None)
		if on is None:
			try:
				from gui.mods.offhangar._constants import CONFIG_OPTIONS as _SC_CFG
				on = bool(_SC_CFG.get('bot_squad_cohesion', False))
			except Exception:
				on = False
			self._squad_cohesion_cache = on
			_offh_ai_log_note('SQUAD_COHESION_CFG on=%s' % ('yes' if on else 'no'))
		return on

	def _squad_centre(self, squad):
		pts = [m['position'] for m in self._squad_members(squad) if m.get('position')]
		if not pts:
			return None
		return (sum(p[0] for p in pts) / len(pts), 0.0, sum(p[2] for p in pts) / len(pts))

	def _squad_call_help(self, squad, balance, now):
		"""Losing squad -> nearest other squad of the team within SQUAD_HELP_RADIUS."""
		centre = self._squad_centre(squad)
		if centre is None:
			return
		if now < float(squad.get('help_called_until', 0.0)):
			return
		team = None
		for m in self._squad_members(squad):
			team = m.get('team')
			break
		best = None
		best_d = SQUAD_HELP_RADIUS
		for other in self.squads.get(team, ()):
			if other is squad:
				continue
			c = self._squad_centre(other)
			if c is None:
				continue
			d = _distance_2d(c, centre)
			if d <= best_d:
				best, best_d = other, d
		squad['help_called_until'] = now + 25.0
		if best is None:
			return
		best['help_request'] = {'pos': centre, 'until': now + 20.0, 'from': squad['id']}
		_offh_ai_log_note('SQUAD_HELP from=%s to=%s balance=%.2f dist=%.0f' % (
			str(squad['id']), str(best['id']), balance, best_d))
		self.chat_events.append(('help_call', squad.get('leader_id'), None))
		self.chat_events.append(('help_go', best.get('leader_id'), None))
		del self.chat_events[:-8]

	def _assign_squad(self, agent):
		"""Slot a newly registered bot into its team's squads, in join order.

		SPGs sit this out - they already plan alone (artillery_hold in
		order_for). Everyone else fills the most recently opened squad on
		their team until it reaches SQUAD_SIZE, then a new squad opens. This
		mirrors _assign_route's determinism: same registration order (fixed
		by the caller) always produces the same squads and the same first
		member per squad, so an authority failover reconstructs them exactly.
		"""
		if agent['profile'].get('class_tag') == 'SPG':
			return
		team = agent['team']
		squads = self.squads.setdefault(team, [])
		my_route_id = (agent.get('route') or {}).get('id', 'direct')
		squad = None
		for cand in squads:
			if cand.get('route_id') == my_route_id and len(cand['members']) < SQUAD_MIX_CAP:
				squad = cand
				break
		if squad is None:
			squad = {
				'id': '%d-%d' % (team, len(squads) + 1),
				'route_id': my_route_id,
				'members': [],
				'leader_id': None,
				'order': None,
				'order_set_at': 0.0,
			}
			squads.append(squad)
		_offh_ai_log_note('SQUAD_MIX bot=%s cls=%s squad=%s route=%s n=%d' % (
			str(agent['id']), str(agent['profile'].get('class_tag')),
			squad['id'], str(my_route_id), len(squad['members']) + 1))
		if False:
			squad = {
				'id': '%d-%d' % (team, len(squads) + 1),
				'members': [],
				'leader_id': None,
				'order': None,
				'order_set_at': 0.0,
			}
			squads.append(squad)
		squad['members'].append(agent['id'])
		agent['squad_id'] = squad['id']
		if squad['leader_id'] is None:
			squad['leader_id'] = agent['id']
			agent['squad_role'] = 'leader'
		else:
			agent['squad_role'] = 'member'
		# The LAST joiner in a full squad holds the rear instead of piling
		# onto the leader's exact target with everyone else. Without this a
		# squad_rush order (see _apply_squad_order) sends all four guns to
		# the identical point - a "turboslive": the whole squad commits to
		# one spot with nothing watching the flank/rear an enemy could use
		# to slip in behind the rush. Re-evaluated every time the squad
		# actually fills (not just once at creation) so a squad that lost
		# its rear guard and later backfills gets a new one instead of
		# staying one gun short of the role forever.
		if len(squad['members']) >= SQUAD_SIZE:
			rear_id = squad['members'][-1]
			# v8.5: Never assign a lightTank as rear_guard! Prefer AT-SPG or non-leader support tank.
			for _cand_rid in reversed(squad['members']):
				_cand_ag = self.agents.get(_cand_rid)
				if _cand_ag is None or _cand_ag.get('squad_role') == 'leader':
					continue
				_cand_cls = (_cand_ag.get('profile') or {}).get('class_tag', '')
				if _cand_cls != 'lightTank':
					rear_id = _cand_rid
					break
			else:
				rear_id = None
			for member_id in squad['members']:
				member_agent = self.agents.get(member_id)
				if member_agent is None or member_agent.get('squad_role') == 'leader':
					continue
				member_agent['squad_role'] = (
					'rear_guard' if member_id == rear_id else 'member')
			_offh_ai_log_note(
				'SQUAD_ASSIGN squad=%s team=%s size=%d leader=%s rear_guard=%s' % (
					str(squad['id']), str(team), len(squad['members']),
					str(squad['leader_id']), str(rear_id)))

	def _squad_for(self, agent):
		team = agent.get('team')
		squad_id = agent.get('squad_id')
		if squad_id is None:
			return None
		for squad in self.squads.get(team, ()):
			if squad['id'] == squad_id:
				return squad
		return None

	def _squad_members(self, squad, alive_only=True):
		result = []
		for member_id in squad.get('members', ()):
			member = self.agents.get(member_id)
			if member is None:
				continue
			if alive_only and member.get('health_fraction', 1.0) <= 0.0:
				continue
			result.append(member)
		return result

	def _squad_force_balance(self, squad, now):
		"""Local force balance for the squad as a whole, not just one member.

		Reuses _local_force_balance's ally/enemy weighting but centred on the
		squad's own members and their combined known contacts, so a leader's
		rush/fallback call reflects "can my four guns win this", not "can I
		personally win this" - the same distinction a human squad leader in a
		clan battle would actually be making.
		"""
		members = self._squad_members(squad)
		if not members:
			return 0.0
		allies = 0.0
		for member in members:
			allies += max(0.25, _number(member.get('health_fraction', 1.0), 1.0))
		team = members[0]['team']
		enemies = 0.0
		centre_x = sum(member['position'][0] for member in members
			if member.get('position')) / max(1, len(members))
		centre_z = sum(member['position'][2] for member in members
			if member.get('position')) / max(1, len(members))
		for contact in self._known_contacts(team, now):
			if _distance_2d(contact['position'], (centre_x, 0.0, centre_z)) <= LOCAL_FORCE_RADIUS * 1.3:
				enemies += max(0.3, contact['health'] / max(contact['max_health'], 1.0))
		return allies - enemies

	def _squad_order(self, squad, now):
		"""The leader's own call: rush, hold, fall back together, or call for
		help toward whichever ally squad is closest to the trouble.

		This is deliberately separate from any player radio command - it is
		what the bot side does to itself so a mirrored enemy team is not just
		a shooting gallery of independent actors either (the same fairness
		point the squad-leader idea was meant to fix). order_for() blends this
		in as one more pull on a member's own decision, weighted by that
		member's personality, rather than overriding it outright.
		"""
		cached = squad.get('order')
		if cached is not None and now - float(squad.get('order_set_at', 0.0)) < SQUAD_ORDER_HOLD_LEADER_SECONDS:
			return cached
		leader = self.agents.get(squad.get('leader_id'))
		members = self._squad_members(squad)
		if leader is None or not members:
			squad['order'] = None
			return None
		balance = self._squad_force_balance(squad, now)
		leader_personality = leader['personality']
		if balance <= -0.8 and self._squad_cohesion_on():
			self._squad_call_help(squad, balance, now)
		# v12.5: Disable static squad_hold_hill ring parking so squads push their full routes and fight!
		hill_point = None
		_hold = self._d82_cfg()[1]
		if hill_point is not None and _hold > 0.0:
			if now < float(squad.get('hill_cool_until', 0.0)):
				hill_point = None
			else:
				if squad.get('hill_first') is None:
					squad['hill_first'] = now
				lpos = leader.get('position')
				if (squad.get('hill_since') is None and lpos is not None and
						math.hypot(float(lpos[0]) - hill_point[0], float(lpos[2]) - hill_point[1])
						<= KING_OF_HILL_HOLD_RADIUS * 2.0):
					squad['hill_since'] = now
				since = squad.get('hill_since')
				if ((since is not None and now - since > _hold) or
						(since is None and now - squad['hill_first'] > _hold * 2.0)):
					squad['hill_since'] = None
					squad['hill_first'] = None
					squad['hill_cool_until'] = now + _hold * 2.0
					hill_point = None
					_offh_ai_log_note('HILL_RELEASE squad=%s' % str(squad.get('id')))
		# v4.66 доп.18 - Assault-mode coordination thresholds. The attacking
		# team commits to a rush more readily (a breach push needs to start
		# before the force balance is fully lopsided, or the defender's timer
		# advantage wins by default) and backs off a losing fight less
		# eagerly (falling back piecemeal just feeds the defender kills for
		# free without the base ever coming under real pressure). The
		# defending team does the opposite: it holds its assigned lane rather
		# than rushing out of position to chase a favourable balance, since a
		# gap in coverage is what lets an attacking squad breach elsewhere.
		rush_balance = 1.1
		rush_aggression = 0.35
		fallback_balance = -1.4
		is_assault = self.assault_attacker_team is not None
		if is_assault:
			rush_balance = 0.45
			rush_aggression = 0.22
			fallback_balance = -2.2
		if hill_point is not None:
			mode = 'squad_hold_hill'
		elif balance <= fallback_balance and leader_personality['caution'] >= leader_personality['aggression']:
			mode = 'squad_fallback'
		elif balance >= rush_balance and leader_personality['aggression'] > rush_aggression:
			mode = 'squad_rush'
		else:
			mode = None
		order = {'mode': mode, 'balance': balance, 'leader_id': leader['id'],
		         'hill_point': hill_point}
		squad['order'] = order
		squad['order_set_at'] = now
		return order

	def _is_hill_squad(self, squad):
		"""True for the squads designated to hold/dominate the hill or neutral base."""
		team = None
		for member_id in squad.get('members', ()):
			agent = self.agents.get(member_id)
			if agent is not None:
				team = agent.get('team')
				break
		if team is None:
			return False
		team_squads = self.squads.get(team, ())
		if not team_squads:
			return False
		index = min(KING_OF_HILL_SQUAD_INDEX, max(0, len(team_squads) - 1))
		return bool(team_squads) and team_squads[index] is squad

	def _promote_squad_leader(self, squad, now):
		"""Hand leadership to the next living member, in original join order.

		Called lazily from order_for rather than from a death callback: this
		module has no death event, only the health_fraction each agent
		reports every tick, so promotion happens the first time order_for
		notices the old leader is no longer alive (or was never registered -
		defensive, should not happen).
		"""
		leader = self.agents.get(squad.get('leader_id'))
		if leader is not None and leader.get('health_fraction', 1.0) > 0.0:
			return leader
		for member_id in squad.get('members', ()):
			candidate = self.agents.get(member_id)
			if candidate is not None and candidate.get('health_fraction', 1.0) > 0.0:
				squad['leader_id'] = member_id
				candidate['squad_role'] = 'leader'
				squad['order'] = None
				squad['order_set_at'] = now
				return candidate
		squad['leader_id'] = None
		return None

	def _enemy_base_for(self, team, position=None):
		try:
			if getattr(self, 'control_point', None) is not None:
				cp = self.control_point
				y = float(position[1]) if (position is not None and len(position) >= 3) else 0.0
				return (float(cp[0]), y, float(cp[1]))
			enemy_team = 2 if int(team or 1) == 1 else 1
			eb = self.bases.get(enemy_team)
			if eb is not None and len(eb) >= 2:
				y = float(position[1]) if (position is not None and len(position) >= 3) else 0.0
				return (float(eb[0]), y, float(eb[1]))
		except Exception:
			pass
		return (0.0, float(position[1]) if (position is not None and len(position) >= 3) else 0.0, 0.0)

	def _routes_for(self, team):
		if self.map_data is None:
			return ()
		return self.map_data.get('routes', {}).get(int(team), ()) or ()

	def _get_team_tactical_plan(self, team):
		team = int(team)
		plans = self.__dict__.setdefault('team_plans', {})
		if team in plans:
			return plans[team]
		routes = list(self._routes_for(team))
		if not routes:
			plan = {'name': 'balanced_split', 'primary_heavy': None, 'secondary_heavy': None, 'primary_flank': None, 'center_route': None}
			plans[team] = plan
			return plan
		by_brawl = sorted(routes, key=lambda r: _number((r.get('role_weights') or {}).get('brawler', 0.0)), reverse=True)
		by_flank = sorted(routes, key=lambda r: _number((r.get('role_weights') or {}).get('flanker', 0.0)) + 0.35 * _number((r.get('role_weights') or {}).get('scout', 0.0)), reverse=True)
		by_center = sorted(routes, key=lambda r: _number((r.get('role_weights') or {}).get('scout', 0.0)) + _number((r.get('role_weights') or {}).get('sniper', 0.0)) + 0.5 * _number((r.get('role_weights') or {}).get('support', 0.0)), reverse=True)
		p_heavy = by_brawl[0].get('id')
		s_heavy = by_brawl[1].get('id') if len(by_brawl) > 1 else p_heavy
		p_flank = by_flank[0].get('id')
		for cand in by_flank:
			if cand.get('id') not in (p_heavy,):
				p_flank = cand.get('id')
				break
		c_route = by_center[0].get('id')
		for cand in by_center:
			if cand.get('id') not in (p_heavy, p_flank):
				c_route = cand.get('id')
				break
		p_seed = stable_seed(self.battle_seed, 'tactical_plan_v72', team)
		plan_names = (
			'balanced_split',
			'heavy_fist_primary',
			'heavy_fist_secondary',
			'medium_wolfpack_flank',
			'center_crossfire',
		)
		chosen_name = plan_names[int(p_seed % len(plan_names))]
		plan = {
			'name': chosen_name,
			'primary_heavy': p_heavy,
			'secondary_heavy': s_heavy,
			'primary_flank': p_flank,
			'center_route': c_route,
		}
		plans[team] = plan
		_offh_ai_log_note('TACTICAL_PLAN team=%d plan=%s heavy1=%s heavy2=%s flank=%s center=%s' % (
			team, chosen_name, str(p_heavy), str(s_heavy), str(p_flank), str(c_route)))
		return plan

	def _assign_tactical_subrole(self, agent, route):
		profile = agent.get('profile') or {}
		class_tag = str(profile.get('class_tag', 'mediumTank'))
		armor = _number(profile.get('armor', 80.0), 80.0)
		speed = _number(profile.get('speed', 12.0), 12.0)
		bot_id = int(agent.get('id', 0) or 0)
		team = int(agent.get('team', 1) or 1)
		plan = self._get_team_tactical_plan(team)
		plan_name = plan.get('name', 'balanced_split')
		p_heavy = plan.get('primary_heavy')
		s_heavy = plan.get('secondary_heavy')
		rid = (route or {}).get('id', '')
		rw = (route or {}).get('role_weights') or {}
		r_seed = stable_seed(self.battle_seed, 'subrole_v72', bot_id, rid)
		_cls_usage_map = self.__dict__.setdefault('class_route_usage', {})
		_cls_on_route = int(_cls_usage_map.get((team, rid, class_tag), 0))

		# v8.8 Dynamic Team-Composition & Tier-Aware Role Allocation:
		# Ensures that in low-tier battles (Tiers 1-4), wolfpack battles, or full-LT/MT teams,
		# at most 1-2 tanks act as dedicated scouts while the rest dynamically step up as frontline
		# spearheads and second-line flankers so every corridor has a coherent combat formation!
		own_level = int(profile.get('level', 8) or 8)
		team_agents = [ag for ag in self.agents.values() if int(ag.get('team', 0) or 0) == team]
		battle_max_tier = max([own_level] + [int((ag.get('profile') or {}).get('level', own_level) or own_level) for ag in self.agents.values()])
		tier_delta = max(0, battle_max_tier - own_level)
		agent['tier_delta'] = tier_delta
		route_spearheads = sum(
			1 for ag in team_agents
			if (ag.get('route') or {}).get('id') == rid and ag.get('tactical_subrole') == 'spearhead'
		)
		team_scouts = sum(
			1 for ag in team_agents
			if ag.get('tactical_subrole') in ('passive_bush_scout', 'active_ridge_scout')
		)
		team_passive_scouts = sum(
			1 for ag in team_agents
			if ag.get('tactical_subrole') == 'passive_bush_scout'
		)
		is_auto = bool(profile.get('is_autoloader', False))
		if is_auto and class_tag in ('heavyTank', 'mediumTank', 'AT-SPG'):
			agent['meta_role'] = 'autoloader_assassin'
		elif tier_delta >= 2 and class_tag in ('heavyTank', 'mediumTank'):
			agent['meta_role'] = 'bottom_tier_support'
		elif tier_delta <= 1 and (armor >= 160.0 or profile.get('sidescrape_capable') or profile.get('hull_down_specialist')):
			agent['meta_role'] = 'top_meta_anchor'
		else:
			agent['meta_role'] = 'standard'

		if class_tag == 'lightTank':
			max_scouts = 1 if battle_max_tier <= 3 else 2
			if agent.get('is_combat_lt') or profile.get('is_infantry_lt') or team_scouts >= max_scouts:
				agent['is_combat_lt'] = True
				if route_spearheads == 0 or (rid in (p_heavy, s_heavy) and route_spearheads < 2):
					return 'spearhead'
				return 'second_line_support'
			# Dedicated scout (max 1 passive bush scout per team, and only in Tier 4+ battles)
			if battle_max_tier >= 4 and team_passive_scouts == 0 and (_number(rw.get('sniper', 0.0)) >= 0.75 or (r_seed % 3 == 0)):
				return 'passive_bush_scout'
			return 'active_ridge_scout'
		elif class_tag == 'AT-SPG':
			# Heavy armored TDs or any TD routed into a heavy_brawl corridor act as 2nd-line assault_support_td (never camping in banana alley!)
			_ctype_sr = str((route or {}).get('corridor_type', '') or '')
			if _ctype_sr == 'heavy_brawl' or _number(rw.get('brawler', 0.0)) >= 0.80 or (armor >= 145.0 and tier_delta <= 1):
				return 'assault_support_td'
			return 'bush_sniper'
		elif class_tag == 'heavyTank':
			# Autoloaders (AMX 50 100/120/50B, T57 Heavy) or bottom-tier heavies (Tier 8 in Tier 10 battle) play 2nd-line support!
			if (is_auto or tier_delta >= 2 or (speed >= 11.5 and armor < 155.0)) and route_spearheads >= 1:
				return 'second_line_support'
			if _cls_on_route >= 2 and (r_seed % 2 == 1):
				return 'second_line_support'
			return 'spearhead'
		elif class_tag == 'mediumTank':
			# Guarantee every flank has a spearhead even when the team has few or no Heavy Tanks!
			if not is_auto and (route_spearheads == 0 or (rid in (p_heavy, s_heavy) and route_spearheads < 2)):
				return 'spearhead'
			if not is_auto and tier_delta <= 1 and armor >= 95.0 and _cls_on_route == 0 and (r_seed % 2 == 0):
				return 'spearhead'
			return 'second_line_support'
		return 'artillery_support'

	def _assign_route(self, agent):
		routes = self._routes_for(agent['team'])
		if not routes:
			return None
		team = int(agent['team'])
		plan = self._get_team_tactical_plan(team)
		plan_name = plan.get('name', 'balanced_split')
		p_heavy = plan.get('primary_heavy')
		s_heavy = plan.get('secondary_heavy')
		p_flank = plan.get('primary_flank')
		c_route = plan.get('center_route')

		profile = agent['profile']
		personality = agent['personality']
		best = None
		best_score = -1e18
		class_tag = str(profile.get('class_tag', 'mediumTank'))
		armor = _number(profile.get('armor', 80.0), 80.0)
		speed = _number(profile.get('speed', 12.0), 12.0)
		own_level = int(profile.get('level', 5) or 5)
		_cls_usage_map = self.__dict__.setdefault('class_route_usage', {})

		team_agents = [ag for ag in self.agents.values() if int(ag.get('team', 0) or 0) == team]
		team_ht_count = sum(1 for ag in team_agents if (ag.get('profile') or {}).get('class_tag') == 'heavyTank')
		team_scouts = sum(1 for ag in team_agents if ag.get('tactical_subrole') in ('passive_bush_scout', 'active_ridge_scout'))
		battle_max_tier = max([own_level] + [int((ag.get('profile') or {}).get('level', own_level) or own_level) for ag in self.agents.values()])
		max_scouts = 1 if battle_max_tier <= 3 else 2

		# Determine if a lightTank should act as a Combat Tank (brawler/flanker) instead of a pure Scout:
		# - Infantry/brawler LTs (AMX 40, Valentine, H35, AMX 38) ALWAYS act as combat tanks.
		# - Once a team already has `max_scouts` (1 at Tiers 1-3, 2 at Tiers 4-10), or if main brawl lanes
		#   are still empty on an LT-heavy team, excess LTs convert into Combat LTs!
		p_heavy_used = int(self.route_usage.get((team, p_heavy), 0)) if p_heavy else 99
		s_heavy_used = int(self.route_usage.get((team, s_heavy), 0)) if s_heavy else 99
		p_flank_used = int(self.route_usage.get((team, p_flank), 0)) if p_flank else 99
		main_lanes_understaffed = (team_ht_count < 2 and (p_heavy_used < 2 or s_heavy_used < 2))
		is_combat_lt = False
		if class_tag == 'lightTank':
			# Fast light tanks (T-50-2, AMX 13 90/75, VK 2801, Chaffee, etc.) ALWAYS act as dedicated scouts
			# (up to max_scouts per team) and never get turned into frontline heavy spearheads just because they spawned first!
			if (profile.get('is_infantry_lt') or
					team_scouts >= max_scouts or
					(speed < 12.8 and main_lanes_understaffed and armor >= max(30.0, own_level * 8.0))):
				is_combat_lt = True
				agent['is_combat_lt'] = True

		# 1. Pre-filter candidate routes (excluding rear_guard for non-SPGs) and group into 3 Major Directions
		eligible_routes = [r for r in routes if r.get('id') != 'rear_guard' or class_tag == 'SPG'] or list(routes)
		_dir_by_rid = {}
		_dir_used = {0: 0, 1: 0, 2: 0}
		_dir_cls_used = {0: 0, 1: 0, 2: 0}
		_n_el = len(eligible_routes)
		_n_dirs_ba = min(3, max(1, _n_el))
		_b_own_ba = self.bases.get(int(team)) or (0.0, -300.0)
		_b_en_ba = self.bases.get(2 if int(team) == 1 else 1) or (0.0, 300.0)
		_bdx_ba = float(_b_en_ba[0]) - float(_b_own_ba[0])
		_bdz_ba = float(_b_en_ba[1]) - float(_b_own_ba[1])
		if math.hypot(_bdx_ba, _bdz_ba) < 25.0 and eligible_routes:
			_r0_wps = eligible_routes[0].get('waypoints') or ()
			if len(_r0_wps) >= 2:
				_b_own_ba = (float(_r0_wps[0][0]), float(_r0_wps[0][1]))
				_b_en_ba = (float(_r0_wps[-1][0]), float(_r0_wps[-1][1]))
				_bdx_ba = float(_b_en_ba[0]) - float(_b_own_ba[0])
				_bdz_ba = float(_b_en_ba[1]) - float(_b_own_ba[1])
		_blen_ba = max(1.0, math.hypot(_bdx_ba, _bdz_ba))
		_lx_ba, _lz_ba = _bdz_ba / _blen_ba, -_bdx_ba / _blen_ba
		_cx_ba, _cz_ba = 0.5 * (float(_b_own_ba[0]) + float(_b_en_ba[0])), 0.5 * (float(_b_own_ba[1]) + float(_b_en_ba[1]))
		_r_lat_list = []
		for _r_el in eligible_routes:
			_rwps_el = _r_el.get('waypoints') or ()
			if _rwps_el:
				_mid_el = _rwps_el[len(_rwps_el) // 2]
				_lat_el = (float(_mid_el[0]) - _cx_ba) * _lx_ba + (float(_mid_el[1]) - _cz_ba) * _lz_ba
			else:
				_lat_el = 0.0
			_r_lat_list.append((_lat_el, _r_el))
		_r_lat_list.sort(key=lambda _x: _x[0])
		for _idx_el, (_lat_v, _r_el) in enumerate(_r_lat_list):
			_d_idx = min(_n_dirs_ba - 1, int(_idx_el * _n_dirs_ba / float(max(1, _n_el))))
			_rid_el = _r_el.get('id')
			_dir_by_rid[_rid_el] = _d_idx
			_dir_used[_d_idx] = _dir_used.get(_d_idx, 0) + int(self.route_usage.get((team, _rid_el), 0))
			_dir_cls_used[_d_idx] = _dir_cls_used.get(_d_idx, 0) + int(_cls_usage_map.get((team, _rid_el, class_tag), 0))

		# 2. v12.5 Hardcoded Per-Route Quota Enforcement (min_tanks >= 2, max_tanks <= capacity):
		#    - NO route is ever allowed to remain empty (0 tanks) or have 1 lone tank ("одна калека")!
		#    - While ANY route has < min_tanks (2), ONLY routes with < min_tanks are eligible!
		#    - Once all routes have >= min_tanks, strictly enforce max_tanks (3..4) per route!
		def _r_min_q(r_obj):
			if r_obj.get('id') == 'rear_guard':
				return 0
			return max(1, int(r_obj.get('min_tanks', 2) or 2))

		def _r_max_q(r_obj):
			return max(_r_min_q(r_obj), int(r_obj.get('max_tanks', r_obj.get('capacity', 4)) or 4))

		def _r_cls_cap(r_obj, c_tag):
			mpc = r_obj.get('max_per_class')
			if isinstance(mpc, dict) and c_tag in mpc:
				return max(0, int(mpc[c_tag]))
			if c_tag == 'AT-SPG':
				return 2
			if c_tag == 'lightTank':
				return 2
			return max(2, _r_max_q(r_obj))

		def _r_allows_cls(r_obj, c_tag):
			if c_tag == 'SPG':
				return True
			allw = r_obj.get('allowed_classes')
			if isinstance(allw, (list, tuple)) and allw and c_tag not in allw:
				return False
			rid = r_obj.get('id')
			cur_cls_n = int(_cls_usage_map.get((team, rid, c_tag), 0))
			if cur_cls_n >= _r_cls_cap(r_obj, c_tag):
				return False
			# Never allow AT-SPGs to stack onto a route that has 0 frontline tanks (HT/MT/LT) if another route has frontline tanks!
			if c_tag == 'AT-SPG' and cur_cls_n >= 1:
				front_n = (
					int(_cls_usage_map.get((team, rid, 'heavyTank'), 0)) +
					int(_cls_usage_map.get((team, rid, 'mediumTank'), 0)) +
					int(_cls_usage_map.get((team, rid, 'lightTank'), 0))
				)
				if front_n == 0:
					return False
			return True

		def _r_prefers_cls(r_obj, c_tag):
			pref = r_obj.get('preferred_classes')
			if isinstance(pref, (list, tuple)) and pref:
				return c_tag in pref
			return False

		_not_full_routes = [
			r for r in eligible_routes
			if r.get('id') != 'rear_guard' and int(self.route_usage.get((team, r.get('id')), 0)) < _r_max_q(r)
		] or list(eligible_routes)

		# 1. Strictly filter to routes that ALLOW this vehicle class (including max_per_class) and are below max_tanks!
		_cls_allowed_pool = [r for r in _not_full_routes if _r_allows_cls(r, class_tag)]
		if not _cls_allowed_pool:
			# Fallback if all routes hit max_per_class: still respect allowed_classes!
			_cls_allowed_pool = [
				r for r in _not_full_routes
				if not r.get('allowed_classes') or class_tag in r.get('allowed_classes')
			] or list(_not_full_routes)
		# 2. CRITICAL: Fill ALL routes up to min_tanks BEFORE filling any route above min_tanks!
		#    Previously `_cls_pref_not_full` came before `_cls_under_min`, which caused bots on Lakeville
		#    to stack 10 tanks into town routes while leaving `west_valley` and `lake_road` understaffed!
		_cls_under_min = [r for r in _cls_allowed_pool if int(self.route_usage.get((team, r.get('id')), 0)) < _r_min_q(r)]
		_cls_pref_under_min = [r for r in _cls_under_min if _r_prefers_cls(r, class_tag)]
		_cls_pref_not_full = [r for r in _cls_allowed_pool if _r_prefers_cls(r, class_tag)]

		if class_tag == 'heavyTank':
			# v13.1 (Arrofur request: "туда всех тяжей послать, чтобы тяжи были на ожидаемой позиции для них"):
			# Heavy tanks strictly prioritize heavy_brawl / heavy-preferred corridors up to max_tanks BEFORE
			# ever spilling over to under-min medium/scout flanks!
			if _cls_pref_under_min:
				_quota_pool = _cls_pref_under_min
			elif _cls_pref_not_full:
				_quota_pool = _cls_pref_not_full
			elif _cls_under_min:
				_quota_pool = _cls_under_min
			else:
				_quota_pool = _cls_allowed_pool
		elif class_tag != 'SPG':
			if _cls_pref_under_min:
				_quota_pool = _cls_pref_under_min
			elif _cls_under_min:
				_quota_pool = _cls_under_min
			elif _cls_pref_not_full:
				_quota_pool = _cls_pref_not_full
			else:
				_quota_pool = _cls_allowed_pool
		else:
			_quota_pool = _not_full_routes

		open_routes = list(_quota_pool)

		# Honor preassigned_route_id ONLY if no other route is starving below min_tanks AND it allows this vehicle class!
		_pre_rid_early = agent.get('preassigned_route_id')
		if _pre_rid_early and class_tag != 'SPG':
			for _er_p in _quota_pool:
				if _er_p.get('id') == _pre_rid_early and int(self.route_usage.get((team, _er_p.get('id')), 0)) < _r_max_q(_er_p):
					open_routes = [_er_p]
					break

		routes = tuple(open_routes)

		# v8.9 Spawn-Side Alignment: compute distance from the tank's actual spawn slot
		# to each route's entry waypoint so left-flank spawners naturally prefer left/center-left
		# corridors and right-flank spawners prefer right/center-right corridors instead of
		# crossing 90 degrees across the entire 15-tank spawn grid!
		_spawn_pos = agent.get('position')
		_pre_rid = agent.get('preassigned_route_id')
		_route_entry_dists = {}
		_min_entry_dist = 9999.0
		_mean_entry_dist = 0.0
		_own_b_ssa = None if (self.king_of_hill_enabled or self.bases.get(1) == self.bases.get(2)) else self.bases.get(int(team))
		if _spawn_pos is not None:
			for _r_cand in routes:
				_r_oriented = route_toward_enemy(_r_cand, team, self.bases)
				_r_wps = (_r_oriented or {}).get('waypoints') or _r_cand.get('waypoints') or ()
				if _r_wps:
					_w0 = (float(_r_wps[0][0]), float(_r_wps[0][1]))
					_wp_near = _r_wps[min(2, len(_r_wps) - 1)]
					_wp_div = _r_wps[min(8, len(_r_wps) - 1)]
					for _w_cand in _r_wps:
						if math.hypot(float(_w_cand[0]) - _w0[0], float(_w_cand[1]) - _w0[1]) >= 40.0:
							_wp_near = _w_cand
							break
					for _w_cand in _r_wps:
						if math.hypot(float(_w_cand[0]) - _w0[0], float(_w_cand[1]) - _w0[1]) >= 115.0:
							_wp_div = _w_cand
							break
					_d_near = math.hypot(float(_wp_near[0]) - float(_spawn_pos[0]), float(_wp_near[1]) - float(_spawn_pos[2]))
					_d_div = math.hypot(float(_wp_div[0]) - float(_spawn_pos[0]), float(_wp_div[1]) - float(_spawn_pos[2]))
					_ed = 0.60 * _d_near + 0.40 * _d_div
					_route_entry_dists[_r_cand.get('id')] = _ed
			if _route_entry_dists:
				_min_entry_dist = min(_route_entry_dists.values())
				_mean_entry_dist = sum(_route_entry_dists.values()) / float(len(_route_entry_dists))

		_min_route_used = min([int(self.route_usage.get((team, _er.get('id')), 0)) for _er in eligible_routes if _er.get('id') != 'rear_guard'] or [0])

		for route in routes:
			rid = route.get('id')
			if rid == 'lake_road' and class_tag == 'SPG':
				continue
			role_weights = route.get('role_weights', {})
			score = 0.0
			for role_name, vehicle_score in profile['roles'].items():
				score += vehicle_score * _number(role_weights.get(role_name, 0.0)) * 20.0
			brawler_w = _number(role_weights.get('brawler', 0.0))
			flanker_w = _number(role_weights.get('flanker', 0.0))
			scout_w = _number(role_weights.get('scout', 0.0))
			sniper_w = _number(role_weights.get('sniper', 0.0))
			support_w = _number(role_weights.get('support', 0.0))
			_cls_used = int(_cls_usage_map.get((team, rid, class_tag), 0))
			_route_total_used = int(self.route_usage.get((team, rid), 0))

			# Per-battle route alternation jitter (+/- 14 pts) so individual tanks logically vary between viable lanes
			alt_hash = stable_seed(self.battle_seed, 'route_alt_v72', int(agent.get('id', 0) or 0), rid)
			alt_jitter = ((alt_hash % 29) - 14) * 1.05
			score += alt_jitter
			if _pre_rid and rid == _pre_rid and _route_total_used < max(1, int(route.get('capacity', 4))) + 1:
				score += 95.0
			if rid in _route_entry_dists and _mean_entry_dist > 1.0:
				# Mild spawn-wing bias (+/-14 pts max) so bots don't criss-cross needlessly,
				# but tactical role weights (up to 75-100 pts) STRICTLY dictate vehicle placement!
				_dist_adv = _mean_entry_dist - _route_entry_dists[rid]
				score += max(-14.0, min(14.0, _dist_adv * 0.18))

			# v12.6 Explicit Per-Route Class & Min/Max Quota Enforcement:
			_r_min_cur = _r_min_q(route)
			_r_max_cur = _r_max_q(route)
			_r_pref_list = route.get('preferred_classes') or ()
			_r_allw_list = route.get('allowed_classes') or ()
			if class_tag != 'SPG' and rid != 'rear_guard':
				if _r_pref_list and class_tag == _r_pref_list[0]:
					score += 135.0
				elif class_tag in _r_pref_list:
					score += 90.0
				if _r_allw_list:
					if class_tag in _r_allw_list:
						score += 40.0
					else:
						score -= 240.0
				if _route_total_used < _r_min_cur:
					score += (_r_min_cur - _route_total_used) * 52.0
				if _route_total_used >= _r_max_cur:
					score -= (_route_total_used - _r_max_cur + 1) * 220.0

			if class_tag == 'heavyTank':
				score += brawler_w * 78.0 + support_w * 32.0 + flanker_w * 14.0
				if brawler_w < 0.35 and flanker_w < 0.60 and support_w < 0.60:
					score -= 65.0
				score -= _cls_used * 38.0
				if _cls_used >= 3:
					score -= 90.0
				_t_delta = int(agent.get('tier_delta', 0) or 0)
				if _t_delta <= 1:
					if plan_name == 'heavy_fist_primary' and rid == p_heavy and _cls_used < 3:
						score += 28.0
					elif plan_name == 'heavy_fist_secondary' and rid == s_heavy and _cls_used < 3:
						score += 28.0
				else:
					if support_w >= 0.45 or rid in (s_heavy, p_flank):
						score += 24.0
				if speed >= 9.5 and rid == p_flank and _cls_used < 2:
					score += 24.0
				elif plan_name == 'center_crossfire' and rid in (c_route, p_flank) and _cls_used < 2:
					score += 20.0
			elif class_tag == 'lightTank' and not is_combat_lt:
				# Dedicated Scout LT (max 1-2 per team): strictly prioritize high-vision spotting & flanking corridors
				_scout_used = int(_cls_usage_map.get((team, rid, 'scout_lt'), 0))
				score += scout_w * 85.0 + flanker_w * 35.0 + sniper_w * 15.0
				if rid == p_heavy and brawler_w >= 0.85 and scout_w < 0.35:
					score -= 75.0
				if rid in (c_route, p_flank):
					score += 28.0
				score -= _scout_used * 180.0
			elif class_tag in ('mediumTank', 'lightTank'):
				# Medium Tanks & Combat Light Tanks: prioritize flanking/hill/center corridors while keeping all lanes staffed
				if profile.get('is_infantry_lt') or (main_lanes_understaffed and rid in (p_heavy, s_heavy) and _route_total_used < 2):
					score += brawler_w * 45.0 + support_w * 26.0 + flanker_w * 22.0
					if rid == p_heavy and int(self.route_usage.get((team, p_heavy), 0)) < 2:
						score += 34.0
					elif rid == s_heavy and int(self.route_usage.get((team, s_heavy), 0)) < 2:
						score += 30.0
				else:
					score += flanker_w * 68.0 + scout_w * 26.0 + support_w * 24.0 + brawler_w * 20.0
				if rid == p_flank and _route_total_used < 3:
					score += 28.0
				elif rid == c_route and _route_total_used < 2:
					score += 24.0
				score -= _route_total_used * 22.0
			elif class_tag == 'AT-SPG':
				# Tank Destroyers seek sniper and support corridors; assault TDs also favor heavy fist
				score += sniper_w * 75.0 + support_w * 36.0 + brawler_w * 18.0 - scout_w * 15.0
				if armor >= 150.0 and ((plan_name == 'heavy_fist_primary' and rid == p_heavy) or (plan_name == 'heavy_fist_secondary' and rid == s_heavy)):
					score += 32.0
				score -= _cls_used * 65.0
			elif class_tag == 'SPG':
				score += _number(role_weights.get('artillery', 0.0)) * 65.0 + sniper_w * 20.0

			risk = _number(route.get('risk', 0.5), 0.5)
			score += risk * personality['aggression'] * 14.0
			score -= risk * personality['caution'] * 11.0
			score += personality['initiative'] * risk * 5.0
			score += personality['route_jitter']
			if _is_rusher(agent):
				score += risk * 28.0
			key = (team, rid)
			used = int(self.route_usage.get(key, 0))
			capacity = min(4, max(3, int(route.get('capacity', 3))))
			fill_ratio = float(used) / float(capacity)
			if class_tag not in ('lightTank', 'AT-SPG'):
				if (plan_name == 'heavy_fist_primary' and rid == p_heavy) or (
						plan_name == 'heavy_fist_secondary' and rid == s_heavy) or (
						plan_name == 'medium_wolfpack_flank' and rid == p_flank):
					score -= fill_ratio * 18.0
				else:
					score -= fill_ratio * 28.0
			else:
				score -= fill_ratio * 16.0
			# Strict per-direction cap across all 33 maps: never overflow any direction into a herd
			if used >= capacity:
				score -= (used - capacity + 1) * 160.0
			if self.assault_attacker_team is not None:
				score -= fill_ratio * 22.0
			if score > best_score:
				best_score = score
				best = route
		if best is None and eligible_routes:
			# Bulletproof Fail-safe: pick the eligible route with the lowest saturation ratio!
			best = min(eligible_routes, key=lambda r: float(self.route_usage.get((team, r.get('id')), 0)) / max(1.0, float(r.get('capacity', 1))))

		if best is not None:
			subrole = self._assign_tactical_subrole(agent, best)
			agent['tactical_subrole'] = subrole
			key = (team, best.get('id'))
			self.route_usage[key] = int(self.route_usage.get(key, 0)) + 1
			_ckey = (team, best.get('id'), class_tag)
			_cls_usage_map[_ckey] = int(_cls_usage_map.get(_ckey, 0)) + 1
			if class_tag == 'lightTank' and subrole in ('passive_bush_scout', 'active_ridge_scout'):
				_skey = (team, best.get('id'), 'scout_lt')
				_cls_usage_map[_skey] = int(_cls_usage_map.get(_skey, 0)) + 1
			_offh_ai_log_note('BOT_ROLE bot=%s team=%d cls=%s subrole=%s route=%s plan=%s' % (
				str(agent.get('id')), team, class_tag, subrole, str(best.get('id')), plan_name))
			return route_toward_enemy(best, agent['team'], self.bases)
		return None

	def update_wrecks(self, driver_frame):
		"""Update live dead-tank wreck positions/dimensions so bots can use wrecks as hull-down/shield cover in brawls."""
		try:
			wrecks = {}
			if isinstance(driver_frame, dict):
				for wid, wbody in driver_frame.items():
					if isinstance(wbody, dict) and wbody.get('is_wreck', False):
						wpos = wbody.get('position')
						if wpos is not None and len(wpos) >= 3:
							wrecks[int(wid)] = {
								'id': int(wid),
								'position': (float(wpos[0]), float(wpos[1]), float(wpos[2])),
								'yaw': float(wbody.get('yaw', 0.0) or 0.0),
								'half_length': float(wbody.get('half_length', 3.2) or 3.2),
								'half_width': float(wbody.get('half_width', 1.6) or 1.6),
								'mass': float(wbody.get('mass', 25000.0) or 25000.0),
							}
			self.wrecks = wrecks
		except Exception:
			pass

	def update_contact(self, observing_team, target_id, target_team, position,
	                   health, max_health, class_tag, visible, now,
	                   armor=0.0, speed=0.0, shootable_by_ids=None,
	                   hull_yaw=None, gun_yaw=None, gun_pitch=None, reload_left=None):
		observing_team = int(observing_team)
		if _number(health, 0.0) <= 0.0:
			try:
				_dead_ag = self.agents.get(int(target_id))
				if _dead_ag is not None:
					_dead_ag['health_fraction'] = 0.0
					_dead_ag['hp_abs'] = 0.0
					_dead_ag['target_id'] = None
				for _tm_c in (1, 2):
					(self.contacts.get(_tm_c) or {}).pop(target_id, None)
					(self.contacts.get(_tm_c) or {}).pop(int(target_id), None)
					_ro = getattr(self, 'radio_orders', {}).get(_tm_c)
					if isinstance(_ro, dict) and (_ro.get('target_id') == target_id or _ro.get('issuer_id') == target_id):
						self.radio_orders[_tm_c] = None
				self._contacts_cache_key = None
			except Exception:
				pass
			return
		if observing_team == int(target_team):
			return
		team_contacts = self.contacts.setdefault(observing_team, {})
		contact = team_contacts.get(target_id)
		if visible:
			vx, vz = 0.0, 0.0
			if contact is not None and contact.get('position') is not None:
				dt_c = _number(now) - _number(contact.get('last_seen'), _number(now))
				if 0.02 < dt_c < 2.5:
					vx = max(-22.0, min(22.0, (float(position[0]) - float(contact['position'][0])) / dt_c))
					vz = max(-22.0, min(22.0, (float(position[2]) - float(contact['position'][2])) / dt_c))
				else:
					vx, vz = contact.get('velocity', (0.0, 0.0))
			team_contacts[target_id] = {
				'id': target_id,
				'team': int(target_team),
				'position': tuple(position),
				'velocity': (vx, vz),
				'health': max(0.0, _number(health, 1.0)),
				'max_health': max(1.0, _number(max_health, 1.0)),
				'class_tag': str(class_tag or 'mediumTank'),
				'armor': max(0.0, _number(armor, 0.0)),
				'speed': max(0.0, _number(speed, 0.0)),
				'hull_yaw': float(hull_yaw) if hull_yaw is not None else (contact.get('hull_yaw') if contact else None),
				'gun_yaw': float(gun_yaw) if gun_yaw is not None else (contact.get('gun_yaw') if contact else None),
				'gun_pitch': float(gun_pitch) if gun_pitch is not None else (contact.get('gun_pitch') if contact else None),
				'reload_left': float(reload_left) if reload_left is not None else (contact.get('reload_left') if contact else None),
				'visible': True,
				'last_seen': _number(now),
				'shootable_by_ids': (tuple(int(value) for value in shootable_by_ids)
				                     if shootable_by_ids is not None else None),
			}
		elif contact is not None:
			contact['visible'] = False
			contact['shootable_by_ids'] = ()

	def _refresh_contacts_cache(self, now):
		"""Rebuild the per-tick known-contacts/focus-count caches once.

		v4.62 perf: called lazily the first time either cache is asked for
		with a new 'now' value this tick. Prunes stale contacts for BOTH
		teams and counts every agent's current target_id in one single pass
		over self.agents, instead of _choose_contact/_local_force_balance
		each re-walking self.agents once per contact per bot (O(contacts x
		agents), repeated for every living bot every tick).
		"""
		known_by_team = {}
		for team in (1, 2):
			known = []
			stale = []
			for target_id, contact in self.contacts.get(int(team), {}).items():
				age = _number(now) - _number(contact.get('last_seen'))
				if age > CONTACT_MEMORY_SECONDS or contact.get('health', 0.0) <= 0.0:
					stale.append(target_id)
				else:
					known.append(contact)
			for target_id in stale:
				try:
					del self.contacts[int(team)][target_id]
				except Exception:
					pass
			known_by_team[team] = known
		focus_counts = {}
		for agent in self.agents.values():
			target_id = agent.get('target_id')
			if target_id is None or _number(agent.get('hp_abs', 1.0), 1.0) <= 0.0:
				continue
			profile = agent.get('profile', {})
			if profile.get('class_tag') == 'SPG':
				continue
			tm = int(agent.get('team', 1) or 1)
			c_obj = (self.contacts.get(tm) or {}).get(target_id)
			if c_obj is not None:
				s_ids = c_obj.get('shootable_by_ids') or ()
				a_pos = agent.get('position')
				c_pos = c_obj.get('position')
				if int(agent.get('id', -1)) not in s_ids and (a_pos is None or c_pos is None or _distance_2d(a_pos, c_pos) > 155.0):
					continue
			key = (tm, target_id)
			focus_counts[key] = focus_counts.get(key, 0) + 1
		self._known_contacts_cache = known_by_team
		self._focus_counts_cache = focus_counts
		self._contacts_cache_key = now

	def _known_contacts(self, team, now):
		ck = self._contacts_cache_key
		if ck is None or abs(float(now) - float(ck)) > 0.08:
			self._refresh_contacts_cache(now)
		return self._known_contacts_cache.get(int(team), [])

	def _focus_count(self, team, target_id):
		# Relies on the caller having already touched _known_contacts (or
		# _local_force_balance) this tick so the cache is fresh for 'now' -
		# every call site in this module does (order_for always calls
		# _choose_contact, which calls _known_contacts, before this).
		return self._focus_counts_cache.get((team, target_id), 0)

	def _desired_focus(self, contact):
		"""Reserve extra guns for durable threats without dog-piling wrecks."""
		remaining = max(0.0, _number(contact.get('health', 0.0), 0.0))
		count = 1
		if remaining >= 900.0 or contact.get('class_tag') in ('heavyTank', 'AT-SPG'):
			count = 2
		if remaining >= 1800.0:
			count = 3
		return count

	def _local_force_balance(self, agent, position, target_position, now):
		_lfb_c = agent.get('_lfb_cache')
		if _lfb_c is not None and (float(now) - _lfb_c[0]) < 0.45:
			if ((float(position[0]) - _lfb_c[1]) ** 2 + (float(position[2]) - _lfb_c[2]) ** 2 < 64.0 and
					(float(target_position[0]) - _lfb_c[3]) ** 2 + (float(target_position[2]) - _lfb_c[4]) ** 2 < 64.0):
				return _lfb_c[5]
		allies = max(0.35, _number(agent.get('health_fraction', 1.0), 1.0))
		for other in self.agents.values():
			if other.get('id') == agent.get('id') or other.get('team') != agent.get('team'):
				continue
			other_hp = _number(other.get('health_fraction', 1.0), 1.0)
			if other_hp <= 0.0 or _number(other.get('hp_abs', 1.0), 1.0) <= 0.0:
				continue
			other_position = other.get('position')
			if (other_position is not None and
			        _distance_2d(other_position, position) <= LOCAL_FORCE_RADIUS * 1.45):
				allies += max(0.30, other_hp)
		enemies = 0.0
		engage_dist = _distance_2d(position, target_position)
		_raw_inc = agent.get('incoming_from')
		if isinstance(_raw_inc, dict):
			incoming = _raw_inc
		elif _raw_inc is not None:
			incoming = {int(_raw_inc): _number(agent.get('incoming_at'), -999.0)}
		else:
			incoming = {}
		for contact in self._known_contacts(agent['team'], now):
			c_pos = contact.get('position')
			if c_pos is None:
				continue
			d_me = _distance_2d(c_pos, position)
			d_tgt = _distance_2d(c_pos, target_position)
			is_shooting_me = (now - _number(incoming.get(contact.get('id')), -999.0)) <= 8.0
			# Only count enemies that actually threaten our position (close/mid range or actively shooting us),
			# not a distant cluster 400m away that cannot brawl with us!
			if d_me <= 220.0 or is_shooting_me or (engage_dist <= 230.0 and d_tgt <= LOCAL_FORCE_RADIUS):
				enemies += max(0.3, contact['health'] / max(contact['max_health'], 1.0))
			elif d_tgt <= LOCAL_FORCE_RADIUS and contact.get('visible'):
				enemies += 0.35 * max(0.3, contact['health'] / max(contact['max_health'], 1.0))
		_res_lfb = allies - enemies
		agent['_lfb_cache'] = (float(now), float(position[0]), float(position[2]), float(target_position[0]), float(target_position[2]), _res_lfb)
		return _res_lfb

	def _flank_position(self, agent, position, target_position):
		"""v12.2 Latched, navgraph-snapped tangential flank position so LTs/MTs never oscillate 180 deg."""
		now_f = float(self.__dict__.get('_contacts_cache_key', 0.0) or 0.0)
		latched = agent.get('_latched_flank_pos')
		latched_until = float(agent.get('_latched_flank_until', 0.0) or 0.0)
		latched_tgt = agent.get('_latched_flank_tgt')
		if (latched is not None and now_f < latched_until and
				_distance_2d(position, latched) > 7.5 and
				latched_tgt is not None and _distance_2d(latched_tgt, target_position) <= 18.0):
			return latched
		# Only stay on bridge/crossing route when separated by a long deep-water river/lake (> 95m)!
		# When a building, bunker, or hill blocks direct LOS, compute a free 3D flanking arc around it!
		if _distance_2d(position, target_position) > 95.0 and self._is_water_or_cliff_chord(position, target_position):
			if not agent.get('route_completed') and agent.get('route'):
				return agent.get('_last_route_pos') or self._route_position(agent, position, now_f, 0.0)
			grid_fb = _real_cover_grid()
			if grid_fb is not None and hasattr(grid_fb, 'snap_to_safe_baked_point'):
				return grid_fb.snap_to_safe_baked_point(target_position, 14) or tuple(target_position)
			return tuple(target_position)
		dx = position[0] - target_position[0]
		dz = position[2] - target_position[2]
		length = math.sqrt(dx * dx + dz * dz)
		if length < 0.1:
			return tuple(position)
		ux = dx / length
		uz = dz / length
		bot_id = int(agent.get('id', 0) or 0)
		side = float(agent.get('_flank_orbit_side', 0.0) or 0.0)
		if side == 0.0:
			side = -1.0 if ((int(agent.get('seed', 0) or 0) + bot_id) & 1) else 1.0
			agent['_flank_orbit_side'] = side
		tan_x = -uz * side
		tan_z = ux * side
		class_tag = (agent.get('profile') or {}).get('class_tag', 'mediumTank')
		if class_tag == 'lightTank' and not agent.get('is_combat_lt'):
			min_r = 26.0
			rad_in = 18.0 if length > 50.0 else 6.0
			tan_step = 28.0
		elif class_tag in ('mediumTank', 'lightTank'):
			min_r = 28.0
			rad_in = 16.0 if length > 55.0 else 6.0
			tan_step = 26.0
		else:
			min_r = 32.0
			rad_in = 14.0 if length > 60.0 else 4.0
			tan_step = 22.0
		orbit_r = min(55.0, max(min_r, length - rad_in))
		cand = (target_position[0] + ux * orbit_r + tan_x * tan_step,
		        position[1],
		        target_position[2] + uz * orbit_r + tan_z * tan_step)
		grid = _real_cover_grid()
		res_pt = cand
		alt_cand = (target_position[0] + ux * orbit_r - tan_x * tan_step,
		            position[1],
		            target_position[2] + uz * orbit_r - tan_z * tan_step)
		if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
			# Free-roaming 3D Flank & Terrain-Fold Fan: evaluate 6 candidate 3D flanking positions around target
			# (testing both sides and wider 3D bypass arcs around buildings/hills, rewarding 3D sightlines, hull-down folds, and bushes)
			_best_f_pt = None
			_best_f_sc = -1e9
			_best_f_side = side
			_base_y_f = float(position[1])
			for _s_mult, _r_sc, _t_sc in ((side, 1.0, 1.0), (-side, 1.0, 1.0), (side, 0.82, 1.45), (-side, 0.82, 1.45), (side, 1.15, 0.65), (-side, 1.15, 0.65)):
				_tx_f = -uz * _s_mult
				_tz_f = ux * _s_mult
				_raw_f = (target_position[0] + ux * (orbit_r * _r_sc) + _tx_f * (tan_step * _t_sc),
				          _base_y_f,
				          target_position[2] + uz * (orbit_r * _r_sc) + _tz_f * (tan_step * _t_sc))
				_sf_pt = grid.snap_to_safe_baked_point(_raw_f, 10)
				if _sf_pt is None or self._is_water_or_cliff_chord(position, _sf_pt):
					continue
				if hasattr(grid, 'mesh3d_flag') and (grid.mesh3d_flag(_sf_pt[0], _sf_pt[2]) & 2) != 0:
					continue
				_f_sc = 0.0
				if _s_mult == side:
					_f_sc += 6.0
				if self._is_3d_corridor_clear(position, _sf_pt, 1.10):
					_f_sc += 18.0
				if self._has_3d_sightline(_sf_pt, target_position):
					_f_sc += 24.0
				if hasattr(grid, 'is_hulldown_3d') and grid.is_hulldown_3d(_sf_pt[0], _sf_pt[2]):
					_f_sc += 11.0
				if hasattr(grid, 'is_bush_3d') and grid.is_bush_3d(_sf_pt[0], _sf_pt[2]):
					_f_sc += (12.0 if class_tag == 'lightTank' else 6.5)
				_f_sc -= _distance_2d(position, _sf_pt) * 0.12
				if _f_sc > _best_f_sc:
					_best_f_sc = _f_sc
					_best_f_pt = _sf_pt
					_best_f_side = _s_mult
			if _best_f_pt is not None:
				agent['_flank_orbit_side'] = _best_f_side
				res_pt = _best_f_pt
			else:
				res_pt = grid.snap_to_safe_baked_point(target_position, 14) or tuple(target_position)
		agent['_latched_flank_pos'] = res_pt
		agent['_latched_flank_until'] = now_f + 4.8
		agent['_latched_flank_tgt'] = tuple(target_position)
		return res_pt

	def _spread_attack_position(self, agent, position, target_position):
		"""v12.2 Latched, navgraph-snapped attack position near the target (18..34m standoff)
		so advancing TDs/HTs/MTs NEVER get ordered 155m backward into an impassable city block!"""
		now_s = float(self.__dict__.get('_contacts_cache_key', 0.0) or 0.0)
		latched = agent.get('_latched_spread_pos')
		latched_until = float(agent.get('_latched_spread_until', 0.0) or 0.0)
		latched_tgt = agent.get('_latched_spread_tgt')
		if (latched is not None and now_s < latched_until and
				_distance_2d(position, latched) > 7.5 and
				latched_tgt is not None and _distance_2d(latched_tgt, target_position) <= 16.0):
			return latched
		if self._is_water_or_cliff_chord(position, target_position):
			if not agent.get('route_completed') and agent.get('route'):
				return agent.get('_last_route_pos') or self._route_position(agent, position, now_s, 0.0)
			grid_s0 = _real_cover_grid()
			if grid_s0 is not None and hasattr(grid_s0, 'snap_to_safe_baked_point'):
				return grid_s0.snap_to_safe_baked_point(target_position, 14) or tuple(target_position)
			return tuple(target_position)
		dx = position[0] - target_position[0]
		dz = position[2] - target_position[2]
		length = math.sqrt(dx * dx + dz * dz)
		if length < 16.0:
			return tuple(target_position)
		bot_id = int(agent.get('id', 0) or 0)
		base_ang = math.atan2(dx, dz)
		slot_off = ((bot_id % 5) - 2) * 0.28
		ang = base_ang + slot_off
		standoff = 16.0 if _is_rusher(agent) else min(34.0, max(18.0, length * 0.35))
		cand = (target_position[0] + math.sin(ang) * standoff,
		        target_position[1],
		        target_position[2] + math.cos(ang) * standoff)
		grid = _real_cover_grid()
		if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
			safe_pt = grid.snap_to_safe_baked_point(cand, 14)
			if safe_pt is None:
				safe_pt = grid.snap_to_safe_baked_point(target_position, 14)
			if safe_pt is not None:
				cand = safe_pt
		agent['_latched_spread_pos'] = cand
		agent['_latched_spread_until'] = now_s + 5.2
		agent['_latched_spread_tgt'] = tuple(target_position)
		return cand

	def _anti_herd_cfg(self):
		""" config, read once per director (= once per battle)."""
		cfg = getattr(self, '_anti_herd_cache', False)
		if cfg is False:
			cfg = {
				'spare_player': False,
				'hard_n': 2,
				'mult': 0.55,
				'close_m': 60.0,
			}
			try:
				from gui.mods.offhangar._constants import CONFIG_OPTIONS as _AH_CFG
				if _AH_CFG.get('bot_anti_herd', True):
					tun = _AH_CFG.get('bot_anti_herd_tuning') or {}
					cfg = {
						'spare_player': bool(_AH_CFG.get('bot_anti_herd_spare_player', False)),
						'hard_n': int(tun.get('hard_n', 2)),
						'mult': float(tun.get('mult', 0.55)),
						'close_m': float(tun.get('close_m', 60.0)),
					}
				else:
					cfg = None
			except Exception:
				pass
			self._anti_herd_cache = cfg
			self._anti_herd_stats = {'refused': 0, 'kept': 0, 'at': None}
			_offh_ai_log_note('ANTI_HERD_CFG %r' % (cfg,))
		return cfg

	def _is_mountain_blocked(self, start, end):
		try:
			sx, sz = float(start[0]), float(start[2])
			ex, ez = float(end[0]), float(end[2])
			if (ex - sx) ** 2 + (ez - sz) ** 2 < 16.0:
				return False
			ckey = (int(sx) >> 3, int(sz) >> 3, int(ex) >> 3, int(ez) >> 3)
			cache = self.__dict__.get('_mtn_pair_cache')
			if cache is None:
				cache = {}
				self._mtn_pair_cache = cache
			else:
				cval = cache.get(ckey)
				if cval is not None:
					return cval
			grid = _real_cover_grid()
			res = bool(grid.is_mountain_blocked(start, end)) if (grid is not None and hasattr(grid, 'is_mountain_blocked')) else False
			if len(cache) > 16000:
				cache.clear()
			cache[ckey] = res
			return res
		except Exception:
			return False

	def _is_chord_impassable(self, start, end):
		try:
			sx, sz = float(start[0]), float(start[2])
			ex, ez = float(end[0]), float(end[2])
			d2 = (ex - sx) ** 2 + (ez - sz) ** 2
			if d2 < 0.25:
				return False
			if d2 < 64.0:
				grid_s = _real_cover_grid()
				if grid_s is not None and getattr(grid_s, 'prebaked', False):
					if grid_s.segment_has_baked_hazard(start, end, 3) or grid_s._segment_near_baked_hazard(start, end):
						return True
					return not grid_s.segment_clear(start, end)
				return False
			ckey = (int(sx) >> 3, int(sz) >> 3, int(ex) >> 3, int(ez) >> 3)
			cache = self.__dict__.get('_chord_pair_cache')
			if cache is None:
				cache = {}
				self._chord_pair_cache = cache
			else:
				cval = cache.get(ckey)
				if cval is not None:
					return cval
			grid = _real_cover_grid()
			res = bool(grid.is_chord_impassable(start, end)) if (grid is not None and hasattr(grid, 'is_chord_impassable')) else False
			if len(cache) > 16000:
				cache.clear()
			cache[ckey] = res
			return res
		except Exception:
			return False

	def _is_hazard_point(self, position, radius_cells=2):
		try:
			if position is None:
				return False
			grid = getattr(self, 'grid', None) or _real_cover_grid(getattr(self, 'map_name', None))
			if grid is not None and hasattr(grid, 'baked_hazard_near'):
				return bool(grid.baked_hazard_near(position, radius_cells))
		except Exception:
			pass
		return False

	def _is_water_or_cliff_chord(self, start, end):
		try:
			if start is None or end is None:
				return False
			grid = getattr(self, 'grid', None) or _real_cover_grid(getattr(self, 'map_name', None))
			if grid is not None and hasattr(grid, 'segment_has_baked_hazard'):
				return bool(grid.segment_has_baked_hazard(start, end, 3))
		except Exception:
			pass
		return False

	def _has_3d_sightline(self, start, end, barrel_height=1.45):
		"""True if both 3D terrain ridge check and 3D building OBB raycast (barrel +1.45m or cupola +1.85m) are clear."""
		try:
			if start is None or end is None:
				return False
			if self._is_mountain_blocked(start, end):
				return False
			from gui.mods.offhangar.ai import bot_subengine as _bsub_s
			return bool(_bsub_s.check_3d_sightline(start, end, barrel_height))
		except Exception:
			return not self._is_mountain_blocked(start, end)

	def _is_3d_corridor_clear(self, start, end, hull_pad=1.20):
		"""True if a tank can drive from start to end in 3D without hitting water, cliffs, terrain walls, or 3D building OBBs."""
		try:
			if start is None or end is None:
				return False
			if self._is_hazard_point(end, 2) or self._is_water_or_cliff_chord(start, end) or self._is_chord_impassable(start, end):
				return False
			from gui.mods.offhangar.ai import bot_subengine as _bsub_c
			if not _bsub_c.check_3d_drive_corridor(start, end, hull_pad):
				return False
			wrecks = getattr(self, 'wrecks', None)
			if wrecks:
				ex, ez = float(end[0]), float(end[2])
				for _w in wrecks.values():
					_wp = _w.get('position')
					if _wp is not None and ((float(_wp[0]) - ex) ** 2 + (float(_wp[2]) - ez) ** 2) < 25.0:
						return False
			return True
		except Exception:
			return not self._is_chord_impassable(start, end)

	def _choose_contact(self, agent, position, hull_yaw, now):
		contacts = self._known_contacts(agent['team'], now)
		if not contacts:
			agent['target_id'] = None
			return None
		profile = agent['profile']
		personality = agent['personality']
		best = None
		best_score = -1e18
		_ah = self._anti_herd_cfg()
		_herd_best = None
		_herd_best_score = -1e18
		for contact in contacts:
			_herd_refused = False
			distance = _distance_2d(position, contact['position'])
			age = max(0.0, _number(now) - _number(contact.get('last_seen')))
			visible = bool(contact.get('visible'))
			shootable_by_ids = contact.get('shootable_by_ids')
			has_direct_lane = bool(
				visible and shootable_by_ids and int(agent['id']) in shootable_by_ids)
			_is_spg_c = (profile.get('class_tag') == 'SPG')
			_route_done_c = bool(agent.get('route_completed'))
			_is_player_c = (int(contact['id']) not in self.agents)
			_is_true_mtn_c = (not has_direct_lane and not _is_spg_c and
			                  self._is_mountain_blocked(position, contact['position']))
			_mtn_blk_c = (not has_direct_lane and not _is_spg_c and
			              (_is_true_mtn_c or self._is_chord_impassable(position, contact['position'])))
			_eb_c = self.bases.get(2 if agent.get('team') == 1 else 1)
			_near_eb_c = (not _is_spg_c and _eb_c is not None and _distance_2d(position, _eb_c) <= 28.0)
			if _near_eb_c:
				# Capturing enemy base: NEVER abandon capture circle for unspotted ghosts or occluded tanks far away!
				if not has_direct_lane and not (visible and distance <= 95.0 and not _mtn_blk_c):
					continue
			# Base decap check across full 50m capture circle (52.0m tolerance):
			_is_capper_early = False
			_b_th_early = getattr(self, '_base_capture_threats', None)
			if _b_th_early:
				for _th_k, _th_e in _b_th_early.items():
					if (_th_k == agent.get('team') or _th_e.get('is_domination')) and (_th_e.get('points', 0) > 0 or _th_e.get('invaders_count', 0) > 0):
						_bp_e = _th_e.get('base_pos')
						if _bp_e is not None and _distance_2d(contact['position'], _bp_e) <= 52.0:
							_is_capper_early = True
							break
			_is_assigned_def_c = int(agent.get('id', 0) or 0) in (getattr(self, '_assigned_base_defenders', {}).get(int(agent.get('team', 1) or 1)) or ())
			_is_duel_att_c = (
				int(contact['id']) == int(agent.get('hit_reaction_attacker_id') or -999) and
				(now - float(agent.get('last_damaged_at', -999.0) or -999.0)) <= 16.0)
			# v12.8 Zero-Dementia Corner & Duel Lock ("не отводить взгляд с деменцией за углом"):
			# Remember the enemy we were actively aiming/fighting so when they back up behind a corner or wreck,
			# we NEVER drop them from target selection for 18 full seconds!
			_is_sticky_corner_tgt = (
				int(contact['id']) in (int(agent.get('target_id') or -999), int(agent.get('last_engaged_target_id') or -999)) and
				(now - float(agent.get('last_engaged_at', -999.0) or -999.0)) <= 10.0 and
				distance <= 145.0 and age <= 10.0 and (not _mtn_blk_c or distance <= 65.0))
			if has_direct_lane or (_is_duel_att_c and (now - float(agent.get('last_damaged_at', -999.0) or -999.0)) <= 4.0):
				agent['last_engaged_target_id'] = int(contact['id'])
				agent['last_engaged_at'] = float(now)
			_cls_c = profile.get('class_tag', 'mediumTank')
			_max_active_aim_d = 435.0 if _cls_c == 'AT-SPG' else (345.0 if _cls_c == 'heavyTank' else (375.0 if _cls_c == 'mediumTank' else 335.0))
			if not _is_spg_c and not (_is_capper_early and _is_assigned_def_c):
				if distance > _max_active_aim_d and not (_is_capper_early and distance <= 480.0):
					continue
				if not has_direct_lane and distance > 185.0 and not (_is_capper_early and distance <= 320.0 and not _mtn_blk_c):
					continue
				if not _route_done_c:
					if not has_direct_lane and _mtn_blk_c and distance > 95.0 and not _is_sticky_corner_tgt:
						continue
					if not has_direct_lane and distance > 65.0 and self._focus_count(agent['team'], contact['id']) >= 3 and not _is_sticky_corner_tgt:
						continue
					if not (
						(has_direct_lane and distance <= _max_active_aim_d) or
						(_is_sticky_corner_tgt and distance <= 145.0 and (not _is_true_mtn_c or distance <= 65.0)) or
						((visible or age <= 8.0) and distance <= 155.0 and not _mtn_blk_c) or
						(_is_capper_early and distance <= 350.0 and not _is_true_mtn_c) or
						(_is_duel_att_c and distance <= 185.0 and not _is_true_mtn_c)
					):
						continue
				elif not visible and age > 14.0 and not _is_sticky_corner_tgt:
					continue
				elif _is_true_mtn_c and not has_direct_lane and distance > 95.0 and not _is_capper_early:
					continue
			focus = self._focus_count(agent['team'], contact['id'])
			desired_focus = self._desired_focus(contact)
			roles = profile.get('roles', {})
			mobility = max(_number(roles.get('scout')), _number(roles.get('flanker')))
			if _is_spg_c:
				engagement_range = max(1250.0, profile.get('fire_range', 0.0))
			else:
				engagement_range = _max_active_aim_d
			health_fraction = contact['health'] / max(contact['max_health'], 1.0)
			dx = contact['position'][0] - position[0]
			dy = float(contact['position'][1]) - float(position[1])
			dz = contact['position'][2] - position[2]
			bearing = math.atan2(dx, dz)
			turn_cost = abs(_angle_delta(bearing, hull_yaw)) / math.pi
			range_error = abs(distance - profile['desired_range']) / max(profile['desired_range'], 50.0)
			skill_tier = 'hard'
			if visible:
				score = 100.0
			elif age <= 6.5:
				# Anti-dementia: recently unspotted enemy retains high priority so bots hold aim / blind-fire!
				score = 88.0 - age * 4.2
			elif age <= 45.0:
				score = max(28.0, 62.0 - (age - 6.5) * 0.85)
			else:
				# Long-term memory: bots remember where enemies were last seen and go hunt them when nearby fights finish
				score = max(14.0, 28.0 - min(14.0, (age - 45.0) * 0.05))
			# Base decap priority: if this contact is inside a threatened base capture circle, prioritize!
			_is_capper = _is_capper_early
			if _is_capper:
				score += (2200.0 if _is_assigned_def_c else 1250.0)  # Decap priority: reset capture immediately!
			if _is_sticky_corner_tgt:
				# Corner-lock persistence when no direct-lane target overrides it
				score += (145.0 if has_direct_lane else 55.0)
			if _is_player_c and (visible or age <= 14.0) and distance <= 350.0:
				# Player threat priority when shootable or in the same open corridor
				score += (85.0 if has_direct_lane else (45.0 if not _mtn_blk_c else 15.0))
				if distance <= 135.0 and (has_direct_lane or not _mtn_blk_c):
					score += (95.0 if has_direct_lane else 35.0)
			# v5.2 Tracer blind-fire priority: if this bot just spotted a tracer from contact['id'], lock onto it!
			if (agent.get('tracer_target_id') == contact['id'] and
					now < float(agent.get('tracer_blind_until', 0.0) or 0.0)):
				score += 135.0
			# v5.2 "Желанный пирог — каноном арта": Enemy SPG is the #1 sweetest prize!
			if contact.get('class_tag') == 'SPG':
				if visible or age <= 25.0 or contact.get('tracer_spotted_at'):
					if profile.get('class_tag') in ('lightTank', 'mediumTank') or _is_rusher(agent):
						score += 145.0
					else:
						score += 95.0
				else:
					score += 55.0
			if _is_spg_c:
				_arc_c = (agent.get('arty_arc_map') or {}).get(int(contact['id']))
				_arc_clear_c = bool(_arc_c.get('clear', True)) if _arc_c is not None else True
				_fire_rng_c = max(450.0, float(profile.get('fire_range', 1200.0) or 1200.0))
				if distance <= _fire_rng_c * 0.96:
					score += 160.0
					score += (185.0 if _arc_clear_c else -145.0)
				else:
					score -= 260.0
			else:
				if has_direct_lane:
					score += 420.0
				elif _mtn_blk_c:
					score -= (165.0 if distance > 75.0 else 95.0)
			# 50/50 Coin-Flip Target Priority ("приоритет цели по монетке"):
			# Latched per 8-second tactical window so bots dynamically split/prioritize spotted targets
			if visible or age <= 5.0:
				_prio_coin = ((int(agent.get('seed', 0) or 0) + int(agent.get('id', 0) or 0) * 13 + int(contact['id']) * 31 + int(now / 8.0)) & 1)
				if _prio_coin == 1:
					score += 48.0
			score += (1.0 - health_fraction) * 42.0
			score -= range_error * (11.0 - personality['aggression'] * 4.0)
			score -= turn_cost * 18.0
			_overhead_unreachable = False
			# Prevent bridge-deck vs under-bridge vertical false lock (e.g. 45_north_america railway bridge dy=7.6m at dist<28m):
			if profile.get('class_tag') != 'SPG' and abs(dy) > 5.8 and distance < 28.0 and not has_direct_lane:
				continue
			if profile.get('class_tag') != 'SPG' and dy > 12.0:
				_req_elev = math.atan2(dy, max(distance, 6.0))
				if _req_elev > 0.28:
					_overhead_unreachable = True
					score -= min(175.0, (_req_elev - 0.25) * 340.0)
					if focus >= 2:
						score -= 95.0
			# Self-Defense & Active Duel Lock: NEVER turn away from an enemy who shot us or is in direct duel!
			_inc_from = agent.get('incoming_from')
			_last_inc = agent.get('last_incoming_from')
			_inc_at = float(agent.get('incoming_at', -999.0) or -999.0)
			_cid_i = int(contact['id'])
			if isinstance(_inc_from, dict) and _cid_i in _inc_from:
				_c_inc_at = _number(_inc_from.get(_cid_i), -999.0)
			elif _last_inc is not None and _cid_i == int(_last_inc):
				_c_inc_at = _inc_at
			elif isinstance(_inc_from, (int, float)) and _cid_i == int(_inc_from):
				_c_inc_at = _inc_at
			else:
				_c_inc_at = -999.0
			_is_duel_attacker = ((now - _c_inc_at) <= 14.0 and (visible or age <= 8.0))
			if _is_duel_attacker:
				score += 125.0
			if _threat_prio_on() and (visible or age <= 5.5) and not _overhead_unreachable:
				if distance < profile['desired_range']:
					score += range_error * (11.0 - personality['aggression'] * 4.0)
				if distance < 200.0:
					score += (200.0 - distance) / 200.0 * 75.0
				if contact['id'] == agent.get('target_id') and distance < profile['fire_range']:
					# Strong target hysteresis so bots facing 2-3 enemies don't jitter their turret back and forth between targets
					score += 175.0 if has_direct_lane else 120.0
					if distance <= 135.0:
						score += 60.0

			# v4.75 "Минус ствол!" - Kill-shot priority for one-shot targets:
			own_shells = profile.get('shells', ()) or ()
			own_alpha = _number(own_shells[0].get('damage', 150.0), 150.0) if own_shells else 150.0
			target_hp = _number(contact.get('health', 9999.0), 9999.0)
			if target_hp <= own_alpha:
				if skill_tier == 'hard':
					score += 80.0
					if target_hp <= own_alpha * 0.75:
						score += 25.0
				elif skill_tier == 'medium':
					score += 48.0

			# Proximity & Close-Quarters Threat Lock: prioritize close enemies with a clear lane or open corridor!
			if (visible or age <= 4.5) and distance <= 220.0 and not _overhead_unreachable:
				_prox_mult = 1.0 if has_direct_lane else (0.55 if not _mtn_blk_c else 0.18)
				score += max(0.0, (220.0 - distance) * 0.65) * _prox_mult
				if distance <= 115.0 and (has_direct_lane or not _mtn_blk_c):
					score += (115.0 - distance) * 1.85 * _prox_mult
					if distance <= 68.0:
						score += (240.0 + (68.0 - distance) * 2.5) * _prox_mult
				# v13.0 Reload & Gun-Ray Target Scoring ('кто откдшился, кто нет' + 'лучи орудий'):
				if visible and distance <= 165.0 and not _mtn_blk_c:
					_c_rld_left, _, _ = self.estimate_target_reload_left(agent, contact['id'], now)
					_c_gyaw = contact.get('gun_yaw')
					if _c_rld_left >= 2.2 and (has_direct_lane or distance <= 110.0):
						score += (65.0 if has_direct_lane else 25.0)
					if _c_gyaw is not None:
						_b_to_me = math.atan2(float(position[0]) - float(contact['position'][0]), float(position[2]) - float(contact['position'][2]))
						_g_off = abs(_angle_delta(float(_c_gyaw), _b_to_me))
						if _g_off <= 0.35 and _c_rld_left <= 1.5:
							score += (55.0 if has_direct_lane else 20.0)
					if distance <= 38.0:
						score += (320.0 if has_direct_lane else 110.0)
					if contact['id'] == agent.get('target_id') and distance <= 90.0:
						score += (165.0 if has_direct_lane else 45.0)

			# Anti-herd ONLY applies to distant targets (>220m) that we are NOT already dueling or shooting directly!
			_exempt_from_anti_herd = (
				has_direct_lane or _is_duel_attacker or
				distance <= 220.0 or contact['id'] == agent.get('target_id') or
				target_hp <= own_alpha * 1.2)
			if _ah and not _exempt_from_anti_herd and not (_ah['spare_player'] and int(contact['id']) not in self.agents):
				_others = focus - (1 if contact['id'] == agent.get('target_id') else 0)
				if _others < desired_focus:
					score += _others * personality['teamwork'] * 5.0
				else:
					_k = _others - desired_focus + 1
					if _others >= desired_focus + _ah['hard_n'] + 1:
						_herd_refused = True
					score -= min(45.0, _k * 14.0)
			elif focus < desired_focus:
				score += focus * personality['teamwork'] * 6.0
			elif focus > desired_focus:
				score -= (focus - desired_focus) * 18.0
			if contact['id'] == agent.get('target_id'):
				if profile.get('class_tag') == 'SPG':
					_c_is_lt = (contact.get('class_tag') == 'lightTank')
					_arc_cur = (agent.get('arty_arc_map') or {}).get(int(contact['id']))
					_arc_cur_blk = bool(_arc_cur is not None and not _arc_cur.get('clear', True))
					if _arc_cur_blk:
						score += 0.0
					elif _c_is_lt:
						score += 25.0
					elif age <= 6.0:
						score += 95.0
					else:
						score += TARGET_HYSTERESIS_BONUS
				else:
					if visible:
						score += TARGET_HYSTERESIS_BONUS
					elif age <= 2.5:
						score += TARGET_HYSTERESIS_BONUS * 0.35
			if profile.get('class_tag') == 'SPG':
				_herd_refused = False
				_c_cls = contact.get('class_tag', 'mediumTank')
				_c_spd = _number(contact.get('speed', 0.0), 0.0)
				if _c_cls == 'heavyTank':
					score += 75.0
				elif _c_cls == 'AT-SPG':
					score += 65.0
				elif _c_cls == 'mediumTank':
					score += 40.0
				elif _c_cls == 'lightTank':
					# SPG aggros on a distant (>185m) lightTank only ~30% of the time (per 20s window),
					# unless the LT is rushing artillery (<185m) or stationary/slow (<3.5 m/s)!
					_lt_window = int(now / 20.0)
					_lt_hash = ((int(agent.get('seed', 0) or 0) * 31 + int(contact['id']) * 17 + _lt_window * 13) & 0x7fffffff) / float(0x7fffffff)
					if distance <= 185.0:
						score += 55.0
					elif _c_spd <= 3.5:
						score += 35.0
					elif _lt_hash <= 0.30:
						score += 20.0
					else:
						score -= 115.0
						if _c_spd >= 7.0:
							score -= 85.0
				if _c_spd <= 3.0 and _c_cls in ('heavyTank', 'AT-SPG', 'mediumTank'):
					score += 45.0
				_cluster_cnt = 0
				for _other_c in contacts:
					if _other_c['id'] != contact['id'] and _distance_2d(contact['position'], _other_c['position']) <= 15.0:
						_cluster_cnt += 1
						score += 68.0
						if _number(_other_c.get('health', 1000.0), 1000.0) <= 650.0:
							score += 42.0
				_arc_map = agent.get('arty_arc_map') or {}
				_arc_st = _arc_map.get(int(contact['id']))
				if _arc_st is not None and (now - float(_arc_st.get('updated_at', -999.0))) <= 8.0:
					if _arc_st.get('clear', True):
						score += 95.0
					else:
						_blk_dur = now - float(_arc_st.get('blocked_since', now))
						if _blk_dur >= 1.5:
							score -= min(240.0, 120.0 + _blk_dur * 25.0)
			elif skill_tier in ('hard', 'medium') and distance <= 190.0 and visible:
				_est_left, _est_rld, _ = self.estimate_target_reload_left(agent, contact['id'], now)
				if _est_left >= 2.2:
					score += min(95.0, _est_left * 9.5)
					if (agent.get('last_whiff_from') == int(contact['id']) and
							(now - float(agent.get('last_whiff_at', -999.0) or -999.0)) <= 6.5):
						score += 65.0

			# v4.74 Target armor assessment & paper target focus:
			target_armor = max(0.0, _number(contact.get('armor', 0.0), 0.0))
			target_class = contact.get('class_tag', 'mediumTank')
			shells = profile.get('shells', ()) or ()
			own_pen = _number(shells[0].get('penetration', 100.0), 100.0) if shells else 100.0
			for _sh_p in shells:
				_sk_p = str(_sh_p.get('kind', '')).lower()
				if not ('explosive' in _sk_p and 'hollow' not in _sk_p):
					own_pen = max(own_pen, _number(_sh_p.get('penetration', 0.0), 0.0))

			# v12.0 Light Tank Role Priority: Scouts actively hunt, distract, and circle isolated TDs, SPGs, and slow Heavies!
			if profile.get('class_tag') == 'lightTank' and (visible or age <= 6.0):
				if target_class in ('AT-SPG', 'SPG') and distance <= 185.0:
					score += 145.0 + max(0.0, (185.0 - distance) * 0.90)
				elif target_class == 'heavyTank' and distance <= 85.0:
					score += 65.0

			# v12.0 AT-SPG vs Circling/Rushing Light Tank (50/50 Coin Flip):
			# Either furiously rotate hull to shoot the scout (coin == 0), OR ignore the distraction
			# until a threshold moment (< 24m point-blank, took a hit from the scout, or no other frontal threat)!
			if profile.get('class_tag') == 'AT-SPG' and target_class == 'lightTank' and visible and distance <= 82.0:
				_has_front_other = any(
					_oc['id'] != contact['id'] and _oc.get('visible') and _oc.get('class_tag') != 'lightTank' and
					_distance_2d(position, _oc['position']) <= 260.0 and
					abs(_angle_delta(math.atan2(_oc['position'][0] - position[0], _oc['position'][2] - position[2]), hull_yaw)) <= 0.65
					for _oc in contacts
				)
				_td_scout_coin = ((int(agent.get('seed', 0) or 0) + int(contact['id']) + int(now / 14.0)) & 1)
				_threshold_moment = (
					distance <= 24.0 or
					_is_duel_attacker or
					agent.get('health_fraction', 1.0) < 0.48 or
					not _has_front_other
				)
				if _td_scout_coin == 0 or _threshold_moment:
					score += 360.0
					agent['td_track_scout_id'] = contact['id']
				else:
					score -= 220.0

			# Soft/paper target priority (glass cannons, SPGs, light tanks, paper TDs):
			if target_armor <= 45.0 or target_class in ('lightTank', 'SPG'):
				score += 18.0 * max(0.25, 1.25 - health_fraction)
				score += 6.0 * personality.get('aggression', 0.5)

			# Frontal impenetrable target fear / respect (evaluated against best AP/APCR/HEAT shell):
			is_impenetrable = (target_armor >= 135.0 and own_pen < target_armor * (0.58 if _fight_fix_on() else 0.78))
			if is_impenetrable and len(contacts) > 1:
				score -= 24.0 * personality.get('caution', 0.5)

			if contact.get('class_tag') in ('lightTank', 'SPG'):
				score += 4.0 * personality['initiative']
			if _herd_refused:
				if score > _herd_best_score:
					_herd_best_score = score
					_herd_best = contact
				continue
			if score > best_score:
				best_score = score
				best = contact
		if _ah:
			_hs = self._anti_herd_stats
			if _herd_best is not None:
				if best is None:
					best = _herd_best
					_hs['kept'] += 1
				else:
					_hs['refused'] += 1
			if _hs['at'] is None:
				_hs['at'] = now
			elif now - _hs['at'] >= 20.0:
				_offh_ai_log_note('ANTI_HERD refused=%d kept=%d' % (_hs['refused'], _hs['kept']))
				_hs['at'] = now
		if (best is not None and best.get('visible') and
				best.get('id') != agent.get('target_id') and self._callouts_on()):
			if best.get('class_tag') == 'SPG' and now - float(agent.get('co_arty_pie_at', -1e9)) > 25.0:
				agent['co_arty_pie_at'] = now
				self._callout_event('arty_pie_hunt', agent['id'], best.get('id'), 'arty_pie')
			elif now - float(agent.get('co_attack_at', -1e9)) > 30.0:
				agent['co_attack_at'] = now
				if self._radio_roll(agent, now, 5) < 0.12 + 0.25 * _number(
						personality.get('teamwork', 0.5), 0.5):
					self._callout_event('attack_target', agent['id'], best.get('id'), 'new_target')
		agent['target_id'] = best.get('id') if best is not None else None
		if best is not None:
			_b_arm = max(0.0, _number(best.get('armor', 0.0), 0.0))
			_b_shells = profile.get('shells', ()) or ()
			_b_pen = _number(_b_shells[0].get('penetration', 100.0), 100.0) if _b_shells else 100.0
			for _sh_b in _b_shells:
				_sk_b = str(_sh_b.get('kind', '')).lower()
				if not ('explosive' in _sk_b and 'hollow' not in _sk_b):
					_b_pen = max(_b_pen, _number(_sh_b.get('penetration', 0.0), 0.0))
			agent['target_impenetrable'] = bool(_b_arm >= 135.0 and _b_pen < _b_arm * 0.58)
		else:
			agent['target_impenetrable'] = False
		return best

	def _can_use_route_shortcut(self, agent):
		profile = agent.get('profile') or {}
		class_tag = str(profile.get('class_tag', 'mediumTank'))
		if class_tag == 'SPG':
			return False
		skill_tier = 'hard'
		if skill_tier == 'hard':
			return True
		if skill_tier == 'medium' or class_tag in ('mediumTank', 'lightTank'):
			return ((int(agent.get('seed', 0) or 0) + int(agent.get('id', 0) or 0)) & 1) == 0
		return False

	def _try_congestion_reroute(self, agent, position, route, waypoints, index, now, battle_elapsed):
		"""v12.0: Dynamic congestion avoidance across parallel city streets & corridors.
		When >= 2 allied tanks bunch up within 34m ahead on the same street (or an ally stops < 26m ahead),
		switch trailing bots onto an adjacent open street/corridor within 72m or shift their lateral lane."""
		try:
			if battle_elapsed < 8.0 or index >= len(waypoints) - 1:
				return route, waypoints, index
			profile = agent.get('profile') or {}
			ctag = profile.get('class_tag', 'mediumTank')
			if ctag == 'SPG':
				return route, waypoints, index
			if (now - float(agent.get('_last_cong_check_at', -999.0) or -999.0)) < 1.2:
				return route, waypoints, index
			agent['_last_cong_check_at'] = now
			wp = waypoints[index]
			dx_w = float(wp[0]) - float(position[0])
			dz_w = float(wp[1]) - float(position[2])
			w_len = math.hypot(dx_w, dz_w)
			if w_len < 1.0:
				return route, waypoints, index
			ux, uz = dx_w / w_len, dz_w / w_len
			nx, nz = -uz, ux
			bot_id = int(agent.get('id', 0) or 0)
			team = int(agent.get('team', 1) or 1)
			cur_rid = str((route or {}).get('id', ''))
			allies_ahead = 0
			stopped_ahead = 0
			avg_lat_ahead = 0.0
			for oa in self.agents.values():
				if oa['id'] == bot_id or oa.get('team') != team or oa.get('health_fraction', 1.0) <= 0.0:
					continue
				op = oa.get('position')
				if op is None:
					continue
				odx = float(op[0]) - float(position[0])
				odz = float(op[2]) - float(position[2])
				fwd = odx * ux + odz * uz
				lat = odx * nx + odz * nz
				if 2.0 <= fwd <= 34.0 and abs(lat) <= 13.0:
					allies_ahead += 1
					avg_lat_ahead += lat
					ospd = float(oa.get('recent_speed', 5.0) or 5.0)
					if fwd <= 26.0 and ospd < 1.5:
						stopped_ahead += 1
			if allies_ahead < 2 and stopped_ahead < 1:
				return route, waypoints, index
			# Shift lateral lane away from the blocked side of the street without abandoning our assigned corridor
			if allies_ahead > 0 and (now - float(agent.get('_last_cong_switch_at', -999.0) or -999.0)) >= 6.0:
				mean_lat = avg_lat_ahead / float(allies_ahead)
				agent['lane_slot'] = -1.25 if mean_lat >= 0.0 else 1.25
				agent['_last_cong_switch_at'] = now
			return route, waypoints, index
		except Exception:
			pass
		return route, waypoints, index

	def _try_route_shortcut(self, agent, position, waypoints, index, now, battle_elapsed=0.0):
		try:
			if not self._can_use_route_shortcut(agent) or index + 2 >= len(waypoints):
				return index
			route_obj = agent.get('route') or {}
			rid_sc = str(route_obj.get('id', '')).lower()
			# Never cut corners on narrow water, cliff, bridge, valley, or gorge corridors!
			if any(_kw in rid_sc for _kw in ('lake', 'cliff', 'bridge', 'crossing', 'ford', 'river', 'island', 'causeway', 'gorge', 'valley', 'canyon', 'marsh', 'swamp')):
				return index
			last_chk = float(agent.get('_shortcut_chk_at', -999.0) or -999.0)
			if (float(now) - last_chk) < 1.15:
				return index
			agent['_shortcut_chk_at'] = float(now)
			grid = _real_cover_grid()
			if grid is None or not getattr(grid, 'prebaked', False):
				return index
			cur_wp = waypoints[index]
			if len(cur_wp) > 2 and cur_wp[2] and battle_elapsed < 85.0:
				return index
			max_look = min(len(waypoints) - 1, index + 7)
			for look_i in range(max_look, index + 1, -1):
				if battle_elapsed < 85.0 and any((len(waypoints[_k]) > 2 and waypoints[_k][2]) for _k in range(index, look_i)):
					continue
				w_cand = waypoints[look_i]
				cand_pt = (float(w_cand[0]), float(position[1]), float(w_cand[1]))
				d_cand = _distance_2d(position, cand_pt)
				if d_cand < 22.0 or d_cand > 155.0:
					continue
				if hasattr(grid, 'baked_open_corridor') and grid.baked_open_corridor(position, cand_pt, 2):
					if not self._is_mountain_blocked(position, cand_pt) and not self._is_chord_impassable(position, cand_pt):
						agent['waypoint_index'] = look_i
						return look_i
		except Exception:
			pass
		return index

	def _route_position(self, agent, position, now, hull_yaw=None):
		route = agent.get('route')
		if route is None:
			# Bulletproof Fail-safe: Never return straight-line enemy base across unmapped obstacles!
			try:
				agent['route'] = self._assign_route(agent)
				route = agent.get('route')
			except Exception:
				route = None
			if route is None:
				return tuple(position)
		waypoints = route.get('waypoints', ())
		if not waypoints:
			return tuple(position)
		_b_start_rp = self.__dict__.setdefault('_battle_start_at', float(now or 0.0))
		_battle_elapsed = max(0.0, float(now or 0.0) - float(_b_start_rp))
		if not agent.get('route_started', False):
			if len(waypoints) > 1:
				nearest = max(1, min(range(len(waypoints)), key=lambda _wi: _distance_2d(
					position, (float(waypoints[_wi][0]), position[1], float(waypoints[_wi][1])))))
			else:
				nearest = 0
			_raw_own_b = self.bases.get(int(agent.get('team', 1) or 1))
			if (self.king_of_hill_enabled or self.bases.get(1) == self.bases.get(2) or _raw_own_b is None or
					math.hypot(float(waypoints[0][0]) - float(_raw_own_b[0]), float(waypoints[0][1]) - float(_raw_own_b[1])) > 85.0):
				_own_b_rp = (float(waypoints[0][0]), float(waypoints[0][1]))
			else:
				_own_b_rp = _raw_own_b
			_own_d_rp = math.hypot(float(position[0]) - float(_own_b_rp[0]), float(position[2]) - float(_own_b_rp[1])) if _own_b_rp is not None else 0.0
			# Overall forward exit vector of this route over the first 3-4 waypoints
			_ref_idx = min(3, len(waypoints) - 1)
			_rf_dx = float(waypoints[_ref_idx][0]) - float(waypoints[0][0])
			_rf_dz = float(waypoints[_ref_idx][1]) - float(waypoints[0][1])
			_rf_len = math.hypot(_rf_dx, _rf_dz) or 1.0
			_rf_ux, _rf_uz = _rf_dx / _rf_len, _rf_dz / _rf_len
			while nearest + 1 < len(waypoints):
				_wx_c = float(waypoints[nearest][0])
				_wz_c = float(waypoints[nearest][1])
				_wx_n = float(waypoints[nearest + 1][0])
				_wz_n = float(waypoints[nearest + 1][1])
				_d_cur_wp = _distance_2d(position, (_wx_c, position[1], _wz_c))
				_seg_x = _wx_n - _wx_c
				_seg_z = _wz_n - _wz_c
				_rel_x = float(position[0]) - _wx_c
				_rel_z = float(position[2]) - _wz_c
				_to_wx = _wx_c - float(position[0])
				_to_wz = _wz_c - float(position[2])
				_fwd_proj = _to_wx * _rf_ux + _to_wz * _rf_uz
				if _d_cur_wp < 13.5:
					nearest += 1
					continue
				if self._is_water_or_cliff_chord(position, (_wx_n, position[1], _wz_n)):
					break
				if (_d_cur_wp < 26.0 or
						(_d_cur_wp < 68.0 and (_rel_x * _seg_x + _rel_z * _seg_z) >= 0.0 and _fwd_proj < 12.0)):
					nearest += 1
				else:
					break
			agent['waypoint_index'] = nearest
			agent['route_started'] = True
			agent['route_join_anchor'] = tuple(position)
			agent['route_join_index'] = nearest
		index = min(int(agent.get('waypoint_index', 0)), len(waypoints) - 1)
		# v12.0 Dynamic Congestion Avoidance & Parallel Street Switching:
		# If >= 2 allies are bunched up (< 34m ahead) or a stationary ally blocks the lane (< 26m ahead),
		# dynamically reroute onto an adjacent parallel street/corridor or offset to the open side of the street!
		route, waypoints, index = self._try_congestion_reroute(agent, position, route, waypoints, index, now, _battle_elapsed)
		# v12.0 Dynamic Route Cutting & True Map Vision ('срез по маршруту'):
		# Hard bots (100%) and Medium bots / Medium Tanks (50/50 coin flip) scan 2..4 waypoints ahead
		# on the baked navgraph; if a wide obstacle-free corridor exists across a square/side-street,
		# they cut corners directly instead of rigidly following every breadcrumb!
		index = self._try_route_shortcut(agent, position, waypoints, index, now, _battle_elapsed)
		# v7.2 Tactical Sub-Role Waypoint Doctrine (AT-SPG bush_sniper / assault_support_td & LT passive_bush_scout):
		_subrole_w = agent.get('tactical_subrole', '')
		_ctag_w = agent['profile'].get('class_tag', 'mediumTank')
		_td_hold_bush = False
		# Dedicated scouts advance continuously along their spotting corridor until enemy contact is established;
		# never clamp waypoint_index mid-route when contact is None (which caused mid-route stalls and 180-deg turnbacks).
		waypoint = waypoints[index]

		# Direction of current corridor segment
		if index > 0:
			prev_wp = waypoints[index - 1]
			seg_dx = float(waypoint[0]) - float(prev_wp[0])
			seg_dz = float(waypoint[1]) - float(prev_wp[1])
		elif len(waypoints) > 1:
			next_wp = waypoints[1]
			seg_dx = float(next_wp[0]) - float(waypoint[0])
			seg_dz = float(next_wp[1]) - float(waypoint[1])
		else:
			seg_dx, seg_dz = 0.0, 0.0

		seg_len_sq = seg_dx * seg_dx + seg_dz * seg_dz
		if seg_len_sq > 0.01:
			seg_len = math.sqrt(seg_len_sq)
			dir_x, dir_z = seg_dx / seg_len, seg_dz / seg_len
			norm_x, norm_z = -dir_z, dir_x
		else:
			dir_x, dir_z = 0.0, 0.0
			norm_x, norm_z = 0.0, 0.0

		# Role-based lateral corridor dispersion & trailing offsets
		# Prevents bots from moving in a single-file train ("паровозиком").
		profile = agent.get('profile') or {}
		class_tag = profile.get('class_tag', 'mediumTank')
		personality = agent.get('personality') or {}
		bot_id = int(agent.get('id', 0) or 0)

		# Natural parallel corridor lane assignment:
		# Use physical lateral offset relative to corridor segment normal so tanks
		# naturally keep their spawn lateral lanes and never crisscross each other!
		_lane_slot = agent.get('lane_slot')
		if _lane_slot is None:
			if norm_x != 0.0 or norm_z != 0.0:
				dx = float(position[0]) - float(waypoint[0])
				dz = float(position[2]) - float(waypoint[1])
				lat_proj = dx * norm_x + dz * norm_z
				_lane_slot = max(-2.0, min(2.0, lat_proj / 7.5))
			else:
				_lane_slot = float((bot_id % 5) - 2) * 0.5
			agent['lane_slot'] = _lane_slot

		if class_tag == 'lightTank' and not agent.get('is_combat_lt'):
			slot = -1.0 if _lane_slot < 0.0 else 1.0
			lat_dist = slot * 12.0 + _lane_slot * 2.5
			trail_dist = 6.0
		elif class_tag in ('heavyTank', 'brawler'):
			# v13.1 (Arrofur request: "чтобы типо тяжи были как на случайном пути но и со ожидаемой позиции для них"):
			# Each heavy tank weaves along its own deterministic randomized sub-path offset per waypoint
			# while converging onto its expected hull-down / sidescrape heavy position at the clash zone!
			_ht_wp_seed = stable_seed(self.battle_seed, 'ht_subpath', bot_id, index)
			_ht_sub_wave = ((_ht_wp_seed % 19) - 9) * 0.42
			lat_dist = _lane_slot * 11.5 + _ht_sub_wave
			if agent.get('tactical_subrole') == 'second_line_support':
				trail_dist = -18.0 + float((bot_id % 3) - 1) * 5.0
			else:
				trail_dist = 6.0 + float((bot_id % 3) - 1) * 7.5
		elif class_tag == 'AT-SPG':
			slot = -1.0 if _lane_slot < 0.0 else 1.0
			lat_dist = slot * 12.0 + _lane_slot * 3.5
			trail_dist = -22.0 if agent.get('tactical_subrole') == 'assault_support_td' else -32.0
		else:  # mediumTank
			lat_dist = _lane_slot * 9.5
			if agent.get('tactical_subrole') == 'spearhead':
				trail_dist = 5.0 + float((bot_id % 3) - 1) * 4.0
			else:
				trail_dist = -12.0 + float((bot_id % 3) - 1) * 5.0

		jitter = _number(personality.get('route_jitter', 0.0), 0.0) * 1.5
		lat_dist += jitter
		# Dynamic Roadway Boundary Clamp:
		# On narrow cliff/lake/bridge shelves, clamp lateral deviation tightly to stay on the road;
		# on standard corridors, clamp to +-4.2m so tanks never push off road into rocks/mountains!
		_r_id_cur = str((route or {}).get('id', ''))
		_is_narrow_water_route = any(_kw in _r_id_cur for _kw in ('lake', 'cliff', 'bridge', 'crossing', 'ford', 'river', 'island', 'causeway'))
		if _r_id_cur == 'lake_road':
			lat_dist = 0.0
		elif _is_narrow_water_route:
			lat_dist = max(-1.5, min(1.5, lat_dist))
		else:
			lat_dist = max(-4.2, min(4.2, lat_dist))

		# Game conditions & vehicle advantages:
		# Top-tier, well-armoured, healthy tanks spearhead the assault; wounded or
		# lower-tier allies yield the point and take supportive second-line positions.
		tier = int(profile.get('level', 5) or 5)
		health_frac = _number(agent.get('health_fraction', 1.0), 1.0)
		tier_bonus = (tier - 5) * 1.5
		health_offset = (health_frac - 0.7) * 8.0  # +2.4m if 100% HP, -4.8m if 10% HP
		trail_dist += tier_bonus + health_offset
		# Tactical Tier & Subrole Doctrine (Living Player Behavior):
		# Top-tier armored heavy anchors lead the charge and peek first;
		# bottom-tier allies (-2 levels) and autoloaders support from second line!
		armor = _number(profile.get('armor', 80.0), 80.0)
		meta_role = agent.get('meta_role', '')
		tier_delta = int(agent.get('tier_delta', 0) or 0)
		if meta_role == 'top_meta_anchor' or (tier_delta <= 1 and class_tag == 'heavyTank' and armor >= 150.0):
			trail_dist += 8.0
		elif meta_role == 'bottom_tier_support' or tier_delta >= 2:
			trail_dist -= 18.0
		elif meta_role == 'autoloader_assassin':
			trail_dist -= 12.0
		if health_frac < 0.40:
			trail_dist -= 16.0

		# v6.8: Only compress lateral corridor dispersion when far along mountain climb (index >= 3 and time > 15s);
		# at spawn and initial rollout, maintain wide lateral separation so allies don't funnel or clip!
		if index > 2 and _battle_elapsed >= 15.0 and _is_narrow_water_route:
			lat_dist *= 0.65
			trail_dist *= 0.50
		# Cliff/bridge/crossing corridor tight clamp (never widen narrow bridges or lake roads!):
		if (route.get('id') or '') == 'lake_road':
			lat_dist = 0.0
		elif (route.get('id') or '') in ('rail_west', 'station_street'):
			lat_dist = 0.0 if abs(float(waypoint[1])) >= 265.0 else max(-1.0, min(1.0, lat_dist))
		elif _is_narrow_water_route:
			lat_dist = max(-1.5, min(1.5, lat_dist))
		else:
			lat_dist = max(-14.5, min(14.5, lat_dist * 1.45))
		# Only apply longitudinal trail_dist on terminal or hold waypoints!
		# On intermediate transit waypoints, non-zero trail_dist (-32m..+16m) places world behind
		# the previous waypoint (causing 180-deg turnaround carousels) or past corners into walls!
		_is_hold_cur = bool((len(waypoint) > 2 and waypoint[2]) or _td_hold_bush or (index + 1 >= len(waypoints)))
		if not _is_hold_cur:
			trail_dist = 0.0
		else:
			trail_dist = max(-8.0, min(8.0, trail_dist))

		base_world = (float(waypoint[0]), float(position[1]), float(waypoint[1]))
		grid_r = _real_cover_grid()
		if grid_r is not None and getattr(grid_r, 'prebaked', False):
			try:
				_bw_cell = grid_r.cell_for(base_world)
				_bw_idx = grid_r._baked_index(_bw_cell)
				if _bw_idx is None or not grid_r._baked_valid[_bw_idx]:
					_snapped_bw = grid_r.snap_to_safe_baked_point(base_world, 14)
					if _snapped_bw is not None:
						base_world = (float(_snapped_bw[0]), float(position[1]), float(_snapped_bw[2]))
			except Exception:
				pass
		world = (float(base_world[0]) + norm_x * lat_dist + dir_x * trail_dist,
		         float(position[1]),
		         float(base_world[2]) + norm_z * lat_dist + dir_z * trail_dist)
		_wpc = agent.get('_wp_world_cache')
		if _wpc is not None and _wpc[0] == index and abs(_wpc[1] - world[0]) < 0.2 and abs(_wpc[2] - world[2]) < 0.2:
			if not _wpc[3]:
				world = base_world
		elif grid_r is not None and getattr(grid_r, 'prebaked', False):
			try:
				_rcell = grid_r.cell_for(world)
				_ridx = grid_r._baked_index(_rcell)
				_wp_ok = not (_ridx is None or not grid_r._baked_valid[_ridx] or
						not grid_r.is_safe_baked_point(world) or
						(grid_r._baked_hazard_near_two and grid_r._baked_hazard_near_two[_ridx]) or
						grid_r.segment_has_baked_hazard(base_world, world, 3) or
						not grid_r.segment_clear(base_world, world))
				agent['_wp_world_cache'] = (index, world[0], world[2], _wp_ok)
				if not _wp_ok:
					world = base_world
			except Exception:
				world = base_world

		# v7.3 Clash-Zone Hold & Tactical Pocket Anchor (`waypoint[2] == True`):
		# When a tank reaches a mid-map clash waypoint (`hold=True`, e.g. Westfield village D4-E6 or north ridge A2-C4,
		# Himmelsdorf banana, Ruinberg/Ensk streets) during the first 105s of battle, DO NOT blindly race through
		# into the enemy's base climb road! Hold the tactical clash zone for 22s (or while enemies are within 235m)
		# and anchor in a nearby prebaked building corner / ridge hull-down pocket!
		is_hold_wp = False

		# v12.2 Route Speed Control & Corner/Hazard Braking across all 33 maps:
		# Compute upcoming turn angle at `waypoint` and proximity to water/cliff hazards so fast tanks
		# brake smoothly before turns and never cut corners or fly off roads (like Lakeville lake_road)!
		_turn_rad_wp = 0.0
		_near_hazard_wp = bool(_is_narrow_water_route)
		_dist_to_base_wp = _distance_2d(position, base_world)
		if index + 1 < len(waypoints):
			_nx_w = waypoints[index + 1]
			_in_dx = float(waypoint[0]) - float(position[0])
			_in_dz = float(waypoint[1]) - float(position[2])
			if (_in_dx * _in_dx + _in_dz * _in_dz) < 16.0 and index > 0:
				_in_dx = float(waypoint[0]) - float(waypoints[index - 1][0])
				_in_dz = float(waypoint[1]) - float(waypoints[index - 1][1])
			_out_dx = float(_nx_w[0]) - float(waypoint[0])
			_out_dz = float(_nx_w[1]) - float(waypoint[1])
			if (_in_dx * _in_dx + _in_dz * _in_dz) > 1.0 and (_out_dx * _out_dx + _out_dz * _out_dz) > 1.0:
				_turn_rad_wp = abs(_angle_delta(math.atan2(_out_dx, _out_dz), math.atan2(_in_dx, _in_dz)))
		if not _near_hazard_wp and grid_r is not None and getattr(grid_r, 'prebaked', False):
			try:
				_b_cell = grid_r.cell_for(base_world)
				_b_idx = grid_r._baked_index(_b_cell)
				if _b_idx is not None and grid_r._baked_hazard_near_two and grid_r._baked_hazard_near_two[_b_idx]:
					_near_hazard_wp = True
			except Exception:
				pass
		_spd_limit_mps = None
		if _r_id_cur == 'lake_road':
			_spd_limit_mps = 5.0 if (_turn_rad_wp >= 0.30 and _dist_to_base_wp <= 30.0) else 6.8
		elif _near_hazard_wp:
			_spd_limit_mps = 5.8 if (_turn_rad_wp >= 0.38 and _dist_to_base_wp <= 32.0) else 8.2
		elif _dist_to_base_wp <= 36.0:
			if _turn_rad_wp >= 1.00:
				_spd_limit_mps = 6.2
			elif _turn_rad_wp >= 0.55:
				_spd_limit_mps = 8.5
			elif _turn_rad_wp >= 0.32:
				_spd_limit_mps = 11.2
		agent['route_speed_limit_mps'] = _spd_limit_mps

		# Advance waypoint if near the target, near the base waypoint, or already past the waypoint bisector
		_passed_wp = False
		_hold_active = is_hold_wp and _battle_elapsed < 105.0 and now < float(agent.get('wp_hold_until_%d' % index, 0.0))
		_tight_corner_or_hazard = (_r_id_cur == 'lake_road' or _near_hazard_wp or _turn_rad_wp >= 0.48)
		_pass_gate_d = 12.0 if _tight_corner_or_hazard else 32.0
		_reach_world_d = 9.0 if _r_id_cur == 'lake_road' else (11.5 if _tight_corner_or_hazard else 18.0)
		_reach_base_d = 9.5 if _r_id_cur == 'lake_road' else (12.0 if _tight_corner_or_hazard else 22.0)
		if index + 1 < len(waypoints) and not _hold_active and _dist_to_base_wp <= _pass_gate_d:
			_nx_wp0 = waypoints[index + 1]
			_to_nx_x = float(_nx_wp0[0]) - float(waypoint[0])
			_to_nx_z = float(_nx_wp0[1]) - float(waypoint[1])
			_rel_wx = float(position[0]) - float(waypoint[0])
			_rel_wz = float(position[2]) - float(waypoint[1])
			if (_rel_wx * _to_nx_x + _rel_wz * _to_nx_z) >= 0.0:
				_passed_wp = True
		# Wreck-on-Waypoint Bypass & 8.0s Progress Watchdog:
		# If a dead tank wreck sits within 8.5m of the current intermediate waypoint and we are within 22m, advance immediately!
		if index + 1 < len(waypoints) and not _hold_active and _dist_to_base_wp <= 22.0 and getattr(self, 'wrecks', None):
			for _w_obj in self.wrecks.values():
				_w_p = _w_obj.get('position')
				if _w_p is not None and _distance_2d(base_world, _w_p) <= 8.5:
					_passed_wp = True
					break
		if index + 1 < len(waypoints) and not _hold_active and _dist_to_base_wp <= 42.0:
			_wp_tr = agent.get('_wp_prog_tracker')
			if _wp_tr is None or _wp_tr[0] != index:
				agent['_wp_prog_tracker'] = (index, float(now), float(_dist_to_base_wp))
			else:
				_tr_idx, _tr_t0, _tr_best_d = _wp_tr
				if float(_dist_to_base_wp) < float(_tr_best_d) - 3.0:
					agent['_wp_prog_tracker'] = (index, float(now), float(_dist_to_base_wp))
				elif (float(now) - float(_tr_t0)) >= 9.0 and (float(now) - float(agent.get('last_seen_enemy_at', -999.0) or -999.0)) > 4.0:
					_nx_chk = (float(waypoints[index + 1][0]), float(position[1]), float(waypoints[index + 1][1]))
					if _dist_to_base_wp <= 22.0 or not self._is_chord_impassable(position, _nx_chk):
						_passed_wp = True
						agent['_wp_prog_tracker'] = (index + 1, float(now), 9999.0)
					else:
						agent['_wp_prog_tracker'] = (index, float(now), float(_dist_to_base_wp))
			_sw_tr = agent.get('_spin_watch_tracker')
			if _sw_tr is None or _sw_tr[0] != index:
				agent['_spin_watch_tracker'] = (index, float(now), float(position[0]), float(position[2]))
			elif (float(now) - float(_sw_tr[1])) >= 10.5:
				_sw_disp = math.hypot(float(position[0]) - float(_sw_tr[2]), float(position[2]) - float(_sw_tr[3]))
				if _sw_disp < 12.0 and (float(now) - float(agent.get('last_seen_enemy_at', -999.0) or -999.0)) > 3.5:
					_nx_chk2 = (float(waypoints[index + 1][0]), float(position[1]), float(waypoints[index + 1][1]))
					if _dist_to_base_wp <= 22.0 or not self._is_chord_impassable(position, _nx_chk2):
						_passed_wp = True
				agent['_spin_watch_tracker'] = (index + (1 if _passed_wp else 0), float(now), float(position[0]), float(position[2]))
		if _distance_2d(position, world) <= _reach_world_d or _dist_to_base_wp <= _reach_base_d or _passed_wp:
			if index + 1 < len(waypoints):
				agent['waypoint_index'] = index + 1
				next_wp = waypoints[index + 1]
				next_dx = float(next_wp[0]) - float(waypoint[0])
				next_dz = float(next_wp[1]) - float(waypoint[1])
				next_len_sq = next_dx * next_dx + next_dz * next_dz
				if next_len_sq > 0.01:
					next_len = math.sqrt(next_len_sq)
					n_dir_x, n_dir_z = next_dx / next_len, next_dz / next_len
					n_norm_x, n_norm_z = -n_dir_z, n_dir_x
				else:
					n_dir_x, n_dir_z = dir_x, dir_z
					n_norm_x, n_norm_z = norm_x, norm_z
				next_base = (float(next_wp[0]), float(position[1]), float(next_wp[1]))
				world = (float(next_wp[0]) + n_norm_x * lat_dist,
				         float(position[1]),
				         float(next_wp[1]) + n_norm_z * lat_dist)
				if grid_r is not None and getattr(grid_r, 'prebaked', False):
					try:
						_ncell = grid_r.cell_for(world)
						_nidx = grid_r._baked_index(_ncell)
						if (_nidx is None or not grid_r._baked_valid[_nidx] or
								not grid_r.is_safe_baked_point(world) or
								(grid_r._baked_hazard_near_two and grid_r._baked_hazard_near_two[_nidx]) or
								grid_r.segment_has_baked_hazard(next_base, world, 3) or
								not grid_r.segment_clear(next_base, world)):
							world = next_base
					except Exception:
						world = next_base
			else:
				agent['route_completed'] = True
				_now_rp = float(getattr(self, '_last_order_now', 0.0) or 0.0)
				if self._should_bot_capture_base(agent, position, _now_rp):
					_eb = self.bases.get(2 if agent.get('team') == 1 else 1)
					if _eb is not None:
						_aid_c = int(agent.get('id', 0) or 0) + int(agent.get('seed', 0) or 0)
						_ang_c = (_aid_c * 1.947787) % (2.0 * math.pi)
						_rad_c = 5.0 + float(_aid_c % 3) * 2.2
						_eb_w = (float(_eb[0]) + math.cos(_ang_c) * _rad_c, float(position[1]), float(_eb[1]) + math.sin(_ang_c) * _rad_c)
						if grid_r is not None and hasattr(grid_r, 'snap_to_safe_baked_point'):
							_sn_c = grid_r.snap_to_safe_baked_point(_eb_w, 3)
							if _sn_c is not None and math.hypot(float(_sn_c[0]) - float(_eb[0]), float(_sn_c[2]) - float(_eb[1])) <= 12.5:
								_eb_w = _sn_c
						return _eb_w
				_kd_obj = self._kd_hunt_objective(agent, position, _now_rp)
				if _kd_obj is not None:
					return self._convergence_position(agent, position, _kd_obj)
				return base_world
		elif agent.get('route_completed'):
			_now_rp = float(getattr(self, '_last_order_now', 0.0) or 0.0)
			if self._should_bot_capture_base(agent, position, _now_rp):
				_eb = self.bases.get(2 if agent.get('team') == 1 else 1)
				if _eb is not None:
					_aid_c = int(agent.get('id', 0) or 0) + int(agent.get('seed', 0) or 0)
					_ang_c = (_aid_c * 1.947787) % (2.0 * math.pi)
					_rad_c = 5.0 + float(_aid_c % 3) * 2.2
					_eb_w = (float(_eb[0]) + math.cos(_ang_c) * _rad_c, float(position[1]), float(_eb[1]) + math.sin(_ang_c) * _rad_c)
					if grid_r is not None and hasattr(grid_r, 'snap_to_safe_baked_point'):
						_sn_c = grid_r.snap_to_safe_baked_point(_eb_w, 3)
						if _sn_c is not None and math.hypot(float(_sn_c[0]) - float(_eb[0]), float(_sn_c[2]) - float(_eb[1])) <= 12.5:
							_eb_w = _sn_c
					return _eb_w
			_kd_obj = self._kd_hunt_objective(agent, position, _now_rp)
			if _kd_obj is not None:
				return self._convergence_position(agent, position, _kd_obj)
			return base_world
		return world

	def _fallback_position(self, agent, position):
		"""Return a short reverse-retreat point (<= 28m) toward the previous route anchor so tanks back up with frontal armor facing the threat instead of spinning 180 degrees."""
		route = agent.get('route')
		waypoints = route.get('waypoints', ()) if route is not None else ()
		if not waypoints:
			return tuple(position)
		index = max(0, min(int(agent.get('waypoint_index', 0)) - 1,
		                   len(waypoints) - 1))
		waypoint = waypoints[index]
		wx, wz = float(waypoint[0]), float(waypoint[1])
		dx = wx - float(position[0])
		dz = wz - float(position[2])
		dist = math.sqrt(dx * dx + dz * dz)
		if dist > 26.0:
			return (float(position[0]) + (dx / dist) * 26.0, float(position[1]), float(position[2]) + (dz / dist) * 26.0)
		return (wx, float(position[1]), wz)

	def _route_anchor(self, agent, position):
		"""Return the strategic anchor immediately before the current waypoint."""
		route = agent.get('route')
		waypoints = route.get('waypoints', ()) if route is not None else ()
		if not waypoints:
			return tuple(position)
		index = max(0, min(int(agent.get('waypoint_index', 0)),
		                   len(waypoints) - 1))
		if int(agent.get('route_join_index', -1)) == index:
			anchor = agent.get('route_join_anchor')
			if anchor is not None:
				return tuple(anchor)
		index = max(0, min(index - 1,
		                   len(waypoints) - 1))
		waypoint = waypoints[index]
		return (float(waypoint[0]), float(position[1]), float(waypoint[1]))

	def _angled_face_position(self, agent, position, target_position, now=0.0):
		"""Tactical hull posture: reverse diamond, sidescraping, pike-nose and wiggling.

		- Non-turreted TDs (AT-SPG) and SPGs face the target directly.
		- Pike nose tanks (IS-3, IS-7, IS-8, 110): Hard bots face forward (<= 3.5 deg)
		  because angling flattens the front cheek. Medium bots have a 50% chance of
		  misplaying by angling 24 deg.
		- Thick-sided tanks (Maus, E-100, KV-4, IS-4, E-75, etc.): Reverse diamond at
		  18-22 deg. Medium bots over-angle to 35 deg, exposing the drive wheel.
		- Dynamic cyber-wiggling: In close/medium combat (< 160m), Hard bots oscillate
		  hull yaw by +-5.5 deg (6 rad/s) to disrupt enemy aim at command cupolas/hatches.
		"""
		profile = agent.get('profile') or {}
		class_tag = profile.get('class_tag', 'mediumTank')
		if class_tag in ('AT-SPG', 'SPG'):
			return target_position
		if profile.get('dominant_role') not in ('brawler', 'support'):
			return target_position
		dx = target_position[0] - position[0]
		dz = target_position[2] - position[2]
		dist = math.sqrt(dx * dx + dz * dz)
		if dist < 0.1:
			return target_position

		skill_tier = 'hard'
		tank_level = int(profile.get('level', 5) or 5)
		has_pike = bool(profile.get('has_pike_nose', False)) and (tank_level >= 5)
		sidescrape = bool(profile.get('sidescrape_capable', False)) and (tank_level >= 4)
		seed = int(agent.get('seed', 0) or 0)

		# 1. Base posture angle depending on tank geometry and bot skill tier
		if has_pike:
			if skill_tier == 'hard':
				# Киберкотлеты know pike nose must face straight at incoming fire
				base_angle_deg = 3.0
			elif skill_tier == 'medium':
				# 50% human mistake (angling pike nose exposes flat cheek)
				base_angle_deg = 4.0 if ((seed & 2) == 0) else 24.0
			else:
				base_angle_deg = 10.0 + float(seed % 10)
		elif sidescrape:
			if skill_tier == 'hard':
				# Ideal sidescrape / reverse angle (19-21 deg gives > 70 deg ricochet)
				base_angle_deg = 20.0
			elif skill_tier == 'medium':
				# Medium bots attempt sidescraping, but 40% over-angle exposing drive wheel
				base_angle_deg = 21.0 if ((seed % 5) != 0) else 35.0
			else:
				base_angle_deg = 15.0 + float(seed % 12)
		else:
			# Standard hull
			caution = _number(agent.get('personality', {}).get('caution', 0.5), 0.5)
			if skill_tier == 'hard':
				base_angle_deg = 15.0 + caution * 8.0
			elif skill_tier == 'medium':
				base_angle_deg = 14.0 + caution * 16.0
			else:
				base_angle_deg = 12.0 + float(seed % 10)

		side = 1.0 if ((seed & 1) == 0) else -1.0
		angle = math.radians(base_angle_deg * side)

		# 2. Hold a rock-steady diamond angle (ромб) without sine-wave fidgeting so the hull never twitches!

		cosine = math.cos(angle)
		sine = math.sin(angle)
		angled_x = dx * cosine - dz * sine
		angled_z = dx * sine + dz * cosine
		return (position[0] + angled_x, target_position[1],
		        position[2] + angled_z)


	def _apply_artillery_self_defense(self, agent, order, position, contact, now):
		"""v4.59: give a static SPG permission to break its firing pose when it
		is actually in danger, and to call its team for help - instead of
		sitting bolted to `artillery_home` no matter what is closing on it.

		Three cases, in priority order (most urgent overrides an earlier one
		this same tick since they all just overwrite the same order fields):
		1. A visible enemy has closed inside ARTY_CRITICAL_RADIUS - too close
		   to out-DPS with a slow-turning gun. Turn the hull away and start
		   pulling back toward the route anchor (a real retreat destination,
		   not just spinning in place); this is the one case allowed to
		   override `artillery_fire` above, since surviving beats one more
		   shot at this range.
		2. A visible enemy is inside ARTY_DANGER_RADIUS but not yet critical -
		   let the hull track/face the threat (so it can still get a shot off
		   if the threat is also the current fire target) and call HELPME so
		   nearby allies converge on this position, same as a real player's
		   panic-radio call. Rate-limited per bot so it does not spam a call
		   every tick for as long as the threat lingers.
		3. No case 1/2 threat, but the SPG has a KNOWN contact it cannot get
		   line-of-sight on for a long stretch (stuck behind a building/ridge)
		   - ask the team for support fire on that contact's last known
		   position instead of just sitting idle and doing nothing about it.
		"""
		team = agent.get('team')
		near_enemy = None
		near_distance = None
		if contact is not None and contact.get('visible'):
			near_distance = _distance_2d(position, contact['position'])
			near_enemy = contact
		# SPG Self-Preservation & Shotgun Roulette ('в рулетку на удачу'):
		# Detect any visible enemy closing within 165m (with LOS or within 75m) BEFORE they even fire at us!
		for _c in self._known_contacts(team, now):
			if not _c.get('visible') or _number(_c.get('health'), 0.0) <= 0.0:
				continue
			_d = _distance_2d(position, _c['position'])
			if _d > 165.0:
				continue
			_s_ids_a = _c.get('shootable_by_ids')
			_has_los_a = bool((_s_ids_a is not None and int(agent['id']) in _s_ids_a) or (
				not self._is_chord_impassable(position, _c['position']) and not self._is_mountain_blocked(position, _c['position'])))
			if not _has_los_a and _d > 75.0:
				continue
			if near_distance is None or _d < near_distance:
				near_enemy, near_distance = _c, _d
		if near_enemy is not None and near_distance <= 165.0:
			order['target_id'] = near_enemy['id']
			order['aim_position'] = near_enemy['position']
			order['face_position'] = near_enemy['position']
			order['fire_allowed'] = True
			order['combat_mode'] = 'artillery_point_blank'
			# If enemy is within 95m, back up into cover while keeping hull & gun aimed at the attacker to fire TD-style!
			if near_distance <= 95.0:
				_ret_pt = self._reload_cover_position(agent, position, near_enemy['position']) or self._fallback_position(agent, position)
				order['move_position'] = _ret_pt
				order['throttle_override'] = -0.85
			else:
				order['move_position'] = tuple(position)
				order['throttle_override'] = 0.0
			last_log = _number(agent.get('_arty_pb_log'), -1e18)
			if now - last_log > 10.0:
				agent['_arty_pb_log'] = now
				_offh_ai_log_note('ARTY_DEFENSE bot=%s mode=point_blank_roulette dist=%.1f target=%s' % (
					agent['id'], near_distance, near_enemy['id']))
			last_help = _number(agent.get('artillery_help_at'), -1e18)
			if now - last_help > ARTY_HELP_COOLDOWN:
				agent['artillery_help_at'] = now
				self.apply_radio_command(
					RADIO_COMMAND_HELP_ME, agent['id'], team, tuple(position), now)
			agent['artillery_no_los_since'] = None
			return
		if near_enemy is not None and near_distance <= ARTY_DANGER_RADIUS:
			# Not critical yet - stay put and keep shooting if it's already the
			# fire target (order_for's own branch above already aimed/fired at
			# it when visible), but make sure the hull is actually tracking the
			# threat rather than whatever stale face_position route_position
			# left behind, and call for help before it closes further.
			order['face_position'] = near_enemy['position']
			last_help = _number(agent.get('artillery_help_at'), -1e18)
			if now - last_help > ARTY_HELP_COOLDOWN:
				agent['artillery_help_at'] = now
				accepted = self.apply_radio_command(
					RADIO_COMMAND_HELP_ME, agent['id'], team, tuple(position), now)
				_offh_ai_log_note(
					'ARTY_DEFENSE bot=%s mode=alert dist=%.1f help_recruits=%d' % (
						agent['id'], near_distance, len(accepted)))
			agent['artillery_no_los_since'] = None
			return
		# No immediate visible threat. Track how long a KNOWN contact has gone
		# unseen (stuck behind cover) and ask for support fire on it once that
		# drags on, instead of silently doing nothing every tick in between.
		if contact is not None and not contact.get('visible'):
			since = agent.get('artillery_no_los_since')
			if since is None:
				agent['artillery_no_los_since'] = now
			elif now - since > ARTY_NO_LOS_HELP_SECONDS:
				last_help = _number(agent.get('artillery_help_at'), -1e18)
				if now - last_help > ARTY_HELP_COOLDOWN:
					agent['artillery_help_at'] = now
					accepted = self.apply_radio_command(
						RADIO_COMMAND_SUPPORT_FIRE, agent['id'], team,
						tuple(position), now, target_id=contact.get('id'))
					_offh_ai_log_note(
						'ARTY_DEFENSE bot=%s mode=support_fire target=%s '
						'no_los_for=%.1fs recruits=%d' % (
							agent['id'], contact.get('id'), now - since,
							len(accepted)))
				agent['artillery_no_los_since'] = now  # reset: don't re-fire every tick
		else:
			agent['artillery_no_los_since'] = None

	# ------------------------------------------------------------------
	# v4.70 & v4.73 intelligence methods (cyber-bot baiting, cover, tanking)
	# ------------------------------------------------------------------

	def notify_shot(self, bot_id, now):
		"""Called by offline_battle.py each time a bot actually fires."""
		self.notify_vehicle_shot(bot_id, now, reload_time=4.0)

	def _is_same_team_vehicle(self, team, other_id):
		if other_id is None:
			return False
		oid = int(other_id)
		o_ag = self.agents.get(oid)
		if o_ag is not None:
			return int(o_ag.get('team', 0) or 0) == int(team)
		p_vid = getattr(self, 'player_vid', None)
		if p_vid is not None and oid == int(p_vid):
			return int(getattr(self, 'player_team', 1) or 1) == int(team)
		# Default player vehicle ID in offline battle is 1 on team 1 unless overridden
		if oid == 1 and p_vid is None and int(team) == int(getattr(self, 'player_team', 1) or 1):
			return True
		return False

	def note_incoming(self, victim_id, attacker_id, now=0.0):
		"""Record who is shooting at victim_id so victim prioritizes self-defense and never turns away mid-duel."""
		try:
			agent = self.agents.get(int(victim_id))
			if agent is not None and attacker_id is not None:
				att_id = int(attacker_id)
				if self._is_same_team_vehicle(agent.get('team', 1), att_id):
					return
				now_f = float(now or 0.0)
				inc_map = agent.get('incoming_from')
				if not isinstance(inc_map, dict):
					inc_map = {}
				inc_map[att_id] = now_f
				agent['incoming_from'] = inc_map
				agent['last_incoming_from'] = att_id
				agent['incoming_at'] = now_f
				agent['shots_at_me'] = int(agent.get('shots_at_me', 0) or 0) + 1
		except Exception:
			pass

	def notify_vehicle_shot(self, vehicle_id, now, reload_time=4.0):
		"""Track shot timestamp and reload duration for any vehicle (player or bot)."""
		vid = int(vehicle_id)
		now_f = float(now)
		rld_f = max(1.0, float(reload_time or 4.0))
		prev_hist = self.vehicle_shot_history.get(vid) or {}
		self.vehicle_shot_history[vid] = {
			'last_shot_time': now_f,
			'reload_time': rld_f,
			'shots_fired': int(prev_hist.get('shots_fired', 0) or 0) + 1,
		}
		agent = self.agents.get(vid)
		if agent is not None:
			agent['last_shot_at'] = now_f
			agent['shot_count'] = int(agent.get('shot_count', 0) or 0) + 1

	def note_damage_received(self, victim_id, attacker_id, attacker_pos, damage, hull_yaw=0.0, turret_yaw=0.0, now=0.0):
		"""v5.2: React intelligently when penetrated — especially from an unexpected flank or by an unspotted shooter.

		- If a TD ('AT-SPG'), SPG, or sniper/medium takes a penetrating hit from an unspotted enemy
		  or from an unexpected angle (> 38 deg off current gun/hull bearing):
		  * Mobile / cautious / repeatedly hit bots choose `hit_relocate`: immediately leave the compromised
		    bush/spot for a lateral cover position 28-42m away!
		  * Patient / heavily armored bots choose `hit_ambush_watch`: reverse into local cover, turn hull/gun
		    toward the incoming hit vector, and 'watch the map' waiting for the attacker to reveal itself!
		"""
		try:
			now_f = float(now or 0.0)
			agent = self.agents.get(int(victim_id))
			if agent is None or attacker_pos is None or _number(damage, 0.0) < 5.0:
				return
			team = int(agent.get('team', 1) or 1)
			if self._is_same_team_vehicle(team, attacker_id):
				return  # Ignore accidental friendly fire from teammates (including player)!
			att_ag = self.agents.get(int(attacker_id)) if attacker_id is not None else None
			_att_is_spg = bool(att_ag is not None and (att_ag.get('profile') or {}).get('class_tag') == 'SPG')
			if not _att_is_spg and attacker_id is not None:
				_att_c = (self.contacts.get(team) or {}).get(attacker_id)
				if _att_c is not None and _att_c.get('class_tag') == 'SPG':
					_att_is_spg = True
			if _att_is_spg and agent.get('position') is not None:
				self.note_artillery_impact(victim_id, attacker_id, attacker_pos, agent.get('position'), now_f, direct_hit=True)
			if att_ag is not None:
				# Penetrating hit resets consecutive bounce counter on this victim
				_bmap = att_ag.get('bounces_on_target')
				if isinstance(_bmap, dict):
					_bmap.pop(int(victim_id), None)
			self.note_incoming(victim_id, attacker_id, now_f)
			vpos = agent.get('position')
			if vpos is None:
				return
			# Check if attacker is currently visible to victim's team
			att_contact = (self.contacts.get(team) or {}).get(attacker_id)
			is_unspotted = (att_contact is None or not att_contact.get('visible'))
			dx = float(attacker_pos[0]) - float(vpos[0])
			dz = float(attacker_pos[2]) - float(vpos[2])
			dist = math.sqrt(dx * dx + dz * dz)
			if dist < 1.0:
				return
			inc_bearing = math.atan2(dx, dz)
			look_yaw = float(hull_yaw or 0.0) + float(turret_yaw or 0.0)
			flank_angle = abs(_angle_delta(inc_bearing, look_yaw))
			is_unexpected_flank = (flank_angle >= 0.66)  # >= ~38 degrees off current aim
			# Also inject/refresh an approximate contact at attacker_pos if the shooter was unspotted
			# (because in WoT the damage indicator arrow shows the exact direction the shell came from!)
			if is_unspotted and attacker_id is not None:
				seed = int(agent.get('seed', 0) or 0) + int(now_f * 10)
				# Estimate distance along incoming vector (clamp to 160..320m if unknown)
				est_d = min(340.0, max(120.0, dist * (0.85 + 0.30 * ((seed % 11) / 10.0))))
				susp_pos = (
					float(vpos[0]) + (dx / dist) * est_d,
					float(attacker_pos[1]),
					float(vpos[2]) + (dz / dist) * est_d)
				tm_contacts = self.contacts.setdefault(team, {})
				if attacker_id not in tm_contacts:
					tm_contacts[attacker_id] = {
						'id': attacker_id,
						'team': 2 if team == 1 else 1,
						'position': susp_pos,
						'velocity': (0.0, 0.0),
						'health': 800.0,
						'max_health': 1000.0,
						'class_tag': 'AT-SPG',
						'armor': 100.0,
						'speed': 0.0,
						'visible': False,
						'last_seen': now_f,
						'shootable_by_ids': (),
					}
				else:
					tm_contacts[attacker_id]['position'] = susp_pos
					tm_contacts[attacker_id]['last_seen'] = now_f
				self._contacts_cache_key = None

			# Count hits taken near the same position
			last_hit_pos = agent.get('last_hit_own_pos')
			if last_hit_pos is not None and _distance_2d(vpos, last_hit_pos) < 12.0 and (now_f - float(agent.get('last_hit_at', -999.0))) < 25.0:
				hits_here = int(agent.get('hits_at_current_spot', 0) or 0) + 1
			else:
				hits_here = 1
			agent['last_hit_own_pos'] = tuple(vpos)
			agent['last_hit_at'] = now_f
			agent['hits_at_current_spot'] = hits_here
			agent['last_hit_from_pos'] = tuple(attacker_pos)

			profile = agent.get('profile') or {}
			class_tag = profile.get('class_tag', 'mediumTank')
			personality = agent.get('personality') or {}
			skill_tier = 'hard'
			ux, uz = dx / dist, dz / dist
			side = 1.0 if ((int(agent.get('seed', 0)) + hits_here) & 1) else -1.0
			nx, nz = -uz * side, ux * side

			agent['hit_reaction_attacker_id'] = int(attacker_id) if attacker_id is not None else None
			_is_armored_brawler = (class_tag == 'heavyTank' or (class_tag == 'AT-SPG' and _number(profile.get('armor', 80.0), 80.0) >= 140.0))
			_last_hr_at = float(agent.get('_last_hr_triggered_at', -999.0) or -999.0)
			# [FIX v12.0] Eliminate close-range HIT_REACTION twitching ("ёрзали на месте"):
			# Never trigger hit_reaction relocate/watch_map when fighting a spotted enemy in our frontal hemisphere
			# or within 95m! Only react to unspotted sniper fire (>95m) or true rear/blind-flank ambushes (>115 deg) with a 12s cooldown.
			if (not is_unspotted and (dist <= 95.0 or flank_angle < 2.0)) or (now_f - _last_hr_at) < 12.0:
				pass
			elif is_unspotted or flank_angle >= 2.0:
				agent['_last_hr_triggered_at'] = now_f
				prefer_relocate = (not _is_armored_brawler) and (
					class_tag in ('SPG', 'lightTank', 'mediumTank') or
					hits_here >= 2)
				if prefer_relocate:
					# Relocate 24-36m laterally and back away from the incoming shell line!
					reloc_pos = self._reload_cover_position(agent, vpos, attacker_pos)
					if reloc_pos is None or _distance_2d(vpos, reloc_pos) < 14.0:
						reloc_pos = (
							float(vpos[0]) - ux * 18.0 + nx * 26.0,
							float(vpos[1]),
							float(vpos[2]) - uz * 18.0 + nz * 26.0)
						grid = _real_cover_grid()
						if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
							reloc_pos = grid.snap_to_safe_baked_point(reloc_pos, 10) or reloc_pos
					agent['hit_reaction_mode'] = 'relocate'
					agent['hit_reaction_pos'] = reloc_pos
					agent['hit_reaction_face'] = tuple(attacker_pos)
					agent['hit_reaction_until'] = now_f + (5.5 if is_unspotted else 4.0)
					agent['hits_at_current_spot'] = 0
					_tm_hr = self.__dict__.setdefault('_team_hit_react_chat_at', {})
					if (self._callouts_on() and
							now_f - float(agent.get('last_hit_react_chat_at', -999.0)) >= 45.0 and
							now_f - float(_tm_hr.get(team, -999.0)) >= 35.0 and
							(not is_unspotted or dist >= 95.0)):
						agent['last_hit_react_chat_at'] = now_f
						_tm_hr[team] = now_f
						self._callout_event('hit_relocate', agent['id'], attacker_id, 'blind' if is_unspotted else 'flank')
					_offh_ai_log_note('HIT_REACTION bot=%s class=%s mode=relocate unspotted=%s flank_rad=%.2f' % (
						str(victim_id), class_tag, str(is_unspotted), flank_angle))
				else:
					# Back into cover 14m, turn hull/gun toward the unspotted threat, and briefly scan before pushing/relocating!
					watch_pos = self._reload_cover_position(agent, vpos, attacker_pos)
					if watch_pos is None or _distance_2d(vpos, watch_pos) < 8.0:
						watch_pos = (
							float(vpos[0]) - ux * 14.0 + nx * 8.0,
							float(vpos[1]),
							float(vpos[2]) - uz * 14.0 + nz * 8.0)
						grid = _real_cover_grid()
						if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
							watch_pos = grid.snap_to_safe_baked_point(watch_pos, 8) or watch_pos
					agent['hit_reaction_mode'] = 'watch_map'
					agent['hit_reaction_pos'] = watch_pos
					agent['hit_reaction_face'] = tuple(attacker_pos)
					agent['hit_reaction_until'] = now_f + 4.2
					_tm_hr = self.__dict__.setdefault('_team_hit_react_chat_at', {})
					if (self._callouts_on() and
							now_f - float(agent.get('last_hit_react_chat_at', -999.0)) >= 45.0 and
							now_f - float(_tm_hr.get(team, -999.0)) >= 35.0 and
							(not is_unspotted or dist >= 95.0)):
						agent['last_hit_react_chat_at'] = now_f
						_tm_hr[team] = now_f
						self._callout_event('hit_watch_map', agent['id'], attacker_id, 'blind' if is_unspotted else 'flank')
					_offh_ai_log_note('HIT_REACTION bot=%s class=%s mode=watch_map unspotted=%s flank_rad=%.2f' % (
						str(victim_id), class_tag, str(is_unspotted), flank_angle))
		except Exception:
			pass

	def note_unspotted_tracer(self, shooter_id, shooter_team, shooter_pos, target_pos, class_tag='SPG', now=0.0):
		"""v8.4: Make unspotted tracer blind-fire a rare, authentic event (in combat),
		mostly only when an SPG has no active spotted target and catches an enemy SPG/camping TD tracer!"""
		try:
			if shooter_pos is None:
				return
			now_f = float(now or 0.0)
			obs_team = 1 if int(shooter_team or 2) == 2 else 2
			# Team-wide cooldown: at most one tracer blind-fire detection every 35 seconds per team
			_tm_last_tr = self.__dict__.setdefault('_team_tracer_detect_at', {})
			if now_f - float(_tm_last_tr.get(obs_team, -999.0)) < 35.0:
				return
			tm_contacts = self.contacts.setdefault(obs_team, {})
			existing = tm_contacts.get(shooter_id)
			if existing is not None and existing.get('visible'):
				return  # Already spotted normally
			sx, sy, sz = float(shooter_pos[0]), float(shooter_pos[1]), float(shooter_pos[2])
			detectors = []
			for ag in self.agents.values():
				if ag.get('team') != obs_team or ag.get('health_fraction', 1.0) <= 0.0 or ag.get('position') is None:
					continue
				ag_class = ag['profile'].get('class_tag', 'mediumTank')
				ag_skill = 'hard'
				if ag_skill == 'easy':
					continue
				# Per-bot cooldown (55s) and per-battle cap (max 2 blind-fire engagements per bot)
				if now_f - float(ag.get('last_tracer_detect_at', -999.0)) < 55.0:
					continue
				if int(ag.get('tracer_detect_count', 0) or 0) >= 2:
					continue
				d = _distance_2d(ag['position'], (sx, sy, sz))
				seed = int(ag.get('seed', 0) or 0) + int(now_f * 7) + int(shooter_id or 0)
				roll = ((seed * 1103515245 + 12345) & 0x7fffffff) / float(0x7fffffff)
				if ag_class == 'SPG':
					# If SPG already has a live spotted target in sight, it rarely abandons it for a blind tracer
					_has_live_tgt = False
					_cur_tid = ag.get('target_id')
					if _cur_tid is not None and _cur_tid in tm_contacts and tm_contacts[_cur_tid].get('visible'):
						_has_live_tgt = True
					chance = (0.14 if ag_skill == 'hard' else 0.06)
					if str(class_tag) not in ('SPG', 'AT-SPG'):
						chance *= 0.30
					if _has_live_tgt:
						chance *= 0.35
					if roll <= chance:
						detectors.append((ag, True))
				elif 110.0 <= d <= 300.0 and ag_class in ('AT-SPG', 'mediumTank'):
					if self._is_mountain_blocked(ag['position'], (sx, sy, sz)) or self._is_chord_impassable(ag['position'], (sx, sy, sz)):
						continue
					near_impact = (target_pos is not None and _distance_2d(ag['position'], target_pos) <= 22.0)
					if not near_impact:
						continue
					chance = 0.07 if ag_skill == 'hard' else 0.025
					if roll <= chance:
						detectors.append((ag, False))
			if not detectors:
				return
			best_ag, is_spg_det = detectors[0]
			_tm_last_tr[obs_team] = now_f
			best_ag['last_tracer_detect_at'] = now_f
			best_ag['tracer_detect_count'] = int(best_ag.get('tracer_detect_count', 0) or 0) + 1
			best_skill = 'hard'
			scatter = 8.5 if best_skill == 'hard' else (13.0 if best_skill == 'medium' else 18.0)
			ang = float((int(best_ag.get('seed', 0)) + int(now_f * 13)) % 360) * (math.pi / 180.0)
			approx_pos = (sx + math.cos(ang) * scatter, sy, sz + math.sin(ang) * scatter)
			tm_contacts[shooter_id] = {
				'id': shooter_id,
				'team': int(shooter_team or (3 - obs_team)),
				'position': approx_pos,
				'velocity': (0.0, 0.0),
				'health': existing.get('health', 550.0) if existing else 550.0,
				'max_health': existing.get('max_health', 600.0) if existing else 600.0,
				'class_tag': str(class_tag or 'SPG'),
				'armor': existing.get('armor', 50.0) if existing else 50.0,
				'speed': 0.0,
				'visible': False,
				'last_seen': now_f,
				'tracer_spotted_at': now_f,
				'shootable_by_ids': tuple(int(d[0]['id']) for d in detectors if d[1]),
			}
			self._contacts_cache_key = None
			for det_ag, det_is_spg in detectors[:2]:
				if det_is_spg or not self._is_mountain_blocked(det_ag['position'], approx_pos):
					det_ag['tracer_target_id'] = shooter_id
					det_ag['tracer_aim_pos'] = approx_pos
					det_ag['tracer_blind_until'] = now_f + (12.0 if det_is_spg else 4.5)
			_tm_tr = self.__dict__.setdefault('_team_tracer_chat_at', {})
			if (self._callouts_on() and is_spg_det and
					now_f - float(best_ag.get('last_tracer_chat_at', -999.0)) >= 75.0 and
					now_f - float(_tm_tr.get(obs_team, -999.0)) >= 75.0):
				best_ag['last_tracer_chat_at'] = now_f
				_tm_tr[obs_team] = now_f
				self._callout_event('tracer_blind', best_ag['id'], shooter_id, 'spg')
			_offh_ai_log_note('TRACER_DETECTED by=%s(spg=%s) shooter=%s class=%s approx=(%.1f,%.1f) scatter=%.1fm' % (
				str(best_ag['id']), str(is_spg_det), str(shooter_id), str(class_tag), approx_pos[0], approx_pos[2], scatter))
		except Exception:
			pass

	def note_enemy_whiff_or_bounce(self, bot_id, shooter_id, outcome='miss', now=0.0):
		"""v8.8: Record that shooter_id just missed or bounced a shot on/near bot_id, and track consecutive bounces on the shooter."""
		try:
			now_f = float(now or 0.0)
			self.note_incoming(bot_id, shooter_id, now_f)
			agent = self.agents.get(int(bot_id))
			if agent is not None and shooter_id is not None:
				if self._is_same_team_vehicle(agent.get('team', 1), shooter_id):
					return
				agent['last_whiff_from'] = int(shooter_id)
				agent['last_whiff_at'] = now_f
				agent['last_whiff_kind'] = str(outcome or 'miss')
				if str(outcome) == 'bounce':
					shooter_ag = self.agents.get(int(shooter_id))
					if shooter_ag is not None:
						_bmap = shooter_ag.get('bounces_on_target')
						if not isinstance(_bmap, dict):
							_bmap = {}
							shooter_ag['bounces_on_target'] = _bmap
						_bmap[int(bot_id)] = int(_bmap.get(int(bot_id), 0) or 0) + 1
				_offh_ai_log_note('CYBER_WHIFF_DETECTED bot=%s shooter=%s outcome=%s' % (
					str(bot_id), str(shooter_id), str(outcome)))
		except Exception:
			pass

	def estimate_target_reload_left(self, agent, target_id, now):
		"""v13.0: Real-time + shot-history estimation of an enemy vehicle's remaining reload (CD / КД, 'кто откдшился, кто нет')."""
		try:
			live_rld = None
			try:
				team_c = self.contacts.get(int((agent or {}).get('team', 1) or 1)) or {}
				c_obj = team_c.get(int(target_id)) or team_c.get(target_id)
				if isinstance(c_obj, dict) and c_obj.get('reload_left') is not None:
					live_rld = max(0.0, float(c_obj.get('reload_left', 0.0) or 0.0))
			except Exception:
				live_rld = None
			info = self.vehicle_shot_history.get(int(target_id))
			if not info and live_rld is None:
				return (0.0, 4.0, False)
			last_shot = float(info.get('last_shot_time', -999.0)) if info else -999.0
			real_rld = max(1.5, float(info.get('reload_time', 4.0))) if info else max(4.0, float(live_rld or 4.0))
			elapsed = max(0.0, float(now) - last_shot) if info else 999.0
			exact_left = max(0.0, real_rld - elapsed) if info else 0.0
			if live_rld is not None:
				exact_left = max(exact_left, live_rld)
			skill = 'hard'
			seed = int((agent or {}).get('seed', 0) or 0) + int(target_id or 0)
			if skill == 'hard':
				err_mult = 0.98 + 0.04 * ((seed % 11) / 10.0)
			elif skill == 'medium':
				err_mult = 0.90 + 0.20 * ((seed % 13) / 12.0)
			else:
				err_mult = 0.80 + 0.40 * ((seed % 7) / 6.0)
			est_rld = real_rld * err_mult
			est_left = max(0.0, exact_left * err_mult)
			# True reload window: an enemy with a 12s reload is STILL on reload 6s after firing (not just <=3.0s)!
			on_reload = bool(est_left >= 2.0 or elapsed <= 3.0)
			return (est_left, est_rld, on_reload)
		except Exception:
			return (0.0, 4.0, False)

	def _evaluate_enemy_gun_rays_and_reload(self, agent, position, contact, peek_pos=None, now=0.0):
		"""v13.0: Evaluate enemy 3D gun rays ('лучи орудий друг у друга') and live reload states ('кто откдшился, кто нет').
		Returns tactical awareness dict for corner peeking, shot-baiting, and reload punishment."""
		res = {
			'target_reload_left': 0.0,
			'target_gun_ray_off': math.pi,
			'target_aiming_at_us': False,
			'target_looking_away': False,
			'any_loaded_gun_locking': False,
			'locking_enemy_id': None,
			'safe_punish_window': False,
		}
		if contact is None or position is None:
			return res
		try:
			check_pt = peek_pos if peek_pos is not None else position
			tid = contact.get('id')
			rld_left, _rld_tot, _on_rld = self.estimate_target_reload_left(agent, tid, now)
			res['target_reload_left'] = rld_left
			epos = contact.get('position')
			gun_yaw = contact.get('gun_yaw')
			if gun_yaw is None and tid in self.agents:
				ag_e = self.agents.get(tid) or {}
				if ag_e.get('hull_yaw') is not None:
					gun_yaw = float(ag_e.get('hull_yaw', 0.0) or 0.0) + float(ag_e.get('turret_yaw', 0.0) or 0.0)
			if epos is not None and gun_yaw is not None:
				bearing_to_us = math.atan2(float(check_pt[0]) - float(epos[0]), float(check_pt[2]) - float(epos[2]))
				ray_off = abs(_angle_delta(float(gun_yaw), bearing_to_us))
				res['target_gun_ray_off'] = ray_off
				res['target_aiming_at_us'] = bool(ray_off <= 0.35)
				res['target_looking_away'] = bool(ray_off >= 0.48)
			# Scan all visible enemies within 165m to see if ANY loaded enemy has its gun ray locked on our peek point
			team = int(agent.get('team', 1) or 1)
			known = self._known_contacts(team, now)
			for oc in known:
				if not oc.get('visible') or oc.get('position') is None:
					continue
				odist = _distance_2d(check_pt, oc['position'])
				if odist > 165.0:
					continue
				oid = oc.get('id')
				o_rld, _, _ = self.estimate_target_reload_left(agent, oid, now)
				if o_rld > 1.35:
					continue
				o_gyaw = oc.get('gun_yaw')
				if o_gyaw is None and oid in self.agents:
					oag = self.agents.get(oid) or {}
					if oag.get('hull_yaw') is not None:
						o_gyaw = float(oag.get('hull_yaw', 0.0) or 0.0) + float(oag.get('turret_yaw', 0.0) or 0.0)
				if o_gyaw is None:
					continue
				o_bear = math.atan2(float(check_pt[0]) - float(oc['position'][0]), float(check_pt[2]) - float(oc['position'][2]))
				o_off = abs(_angle_delta(float(o_gyaw), o_bear))
				if o_off <= 0.35:
					# Check that no mountain/solid 3D building blocks this enemy from our peek point
					s_ids = oc.get('shootable_by_ids')
					if (s_ids and int(agent['id']) in s_ids) or self._has_3d_sightline(check_pt, oc['position'], 1.45):
						res['any_loaded_gun_locking'] = True
						res['locking_enemy_id'] = oid
						break
			res['safe_punish_window'] = bool(
				(res['target_reload_left'] >= 2.1 or res['target_looking_away']) and
				not res['any_loaded_gun_locking']
			)
		except Exception:
			pass
		return res

	def note_artillery_impact(self, bot_id, spg_id, spg_pos, impact_pos, now=0.0, direct_hit=False):
		"""v12.0 Anti-Arty Smart Relocation:
		Until an SPG fires at/near a bot, the bot holds its aggressive fighting spot normally.
		Once an SPG shell lands within 30m (or hits it directly), the bot marks its current spot
		as compromised by artillery and relocates to a building/ridge arty-safe shadow while continuing to fight!"""
		try:
			now_f = float(now or 0.0)
			agent = self.agents.get(int(bot_id))
			if agent is None or spg_pos is None or impact_pos is None:
				return
			if (agent.get('profile') or {}).get('class_tag') == 'SPG':
				return
			if self._is_same_team_vehicle(agent.get('team', 1), spg_id):
				return
			vpos = agent.get('position')
			if vpos is None:
				return
			d_imp = _distance_2d(vpos, impact_pos)
			if not direct_hit and d_imp > 30.0:
				return
			skill_tier = 'hard'
			if skill_tier == 'easy' and not direct_hit:
				return
			agent['arty_threat_pos'] = (float(spg_pos[0]), float(spg_pos[1]), float(spg_pos[2]))
			agent['arty_compromised_pos'] = tuple(vpos)
			agent['arty_compromised_until'] = now_f + (15.0 if skill_tier == 'hard' else 10.5)
			agent['arty_safe_reloc_pos'] = None
			_offh_ai_log_note('ARTY_EVADE_TRIGGER bot=%s spg=%s d_imp=%.1f direct=%s' % (
				str(bot_id), str(spg_id), d_imp, str(direct_hit)))
		except Exception:
			pass

	def _find_arty_safe_position(self, agent, position, arty_origin, enemy_pos, now):
		"""Find a nearby (18..42m) fighting position shielded from arty_origin by a tall building/cliff
		('арта-сейв') or displaced laterally out of the SPG's pre-aimed splash zone."""
		try:
			cached = agent.get('arty_safe_reloc_pos')
			if cached is not None and _distance_2d(position, cached) > 5.0:
				return cached
			comp_pos = agent.get('arty_compromised_pos') or position
			adx = float(arty_origin[0]) - float(position[0])
			adz = float(arty_origin[2]) - float(position[2])
			adist = max(1.0, math.sqrt(adx * adx + adz * adz))
			aux, auz = adx / adist, adz / adist
			anx, anz = -auz, aux
			grid = _real_cover_grid()
			best_pt = None
			best_sc = -1e18
			side = 1.0 if ((int(agent.get('seed', 0) or 0) + int(now * 0.2)) & 1) else -1.0
			cands = []
			# 1) Check prebaked tactical pockets within 38m
			if enemy_pos is not None:
				pk = self._find_tactical_pocket(agent, position, enemy_pos, max_radius=38.0)
				if pk is not None and _distance_2d(comp_pos, pk['stand']) >= 14.0:
					cands.append(pk['stand'])
			# 2) Sample lateral & diagonal offsets around position
			for lat in (24.0 * side, -24.0 * side, 36.0 * side, -36.0 * side, 16.0 * side, -16.0 * side):
				for fwd in (-14.0, 0.0, 14.0, 24.0):
					cands.append((
						float(position[0]) + anx * lat + aux * fwd,
						float(position[1]),
						float(position[2]) + anz * lat + auz * fwd))
			for rc in cands:
				pt = rc
				if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
					pt = grid.snap_to_safe_baked_point(pt, 6)
					if pt is None:
						continue
				d_from_comp = _distance_2d(comp_pos, pt)
				if d_from_comp < 15.0 or d_from_comp > 52.0:
					continue
				if self._is_chord_impassable(position, pt):
					continue
				sc = d_from_comp * 1.2
				# Check if a tall building/cliff obstacle sits 8..22m from pt toward arty_origin ('арта-сейв'!)
				if grid is not None and getattr(grid, 'prebaked', False):
					for step_m in (8.0, 14.0, 20.0):
						probe_a = (float(pt[0]) + aux * step_m, float(pt[1]), float(pt[2]) + auz * step_m)
						if grid._baked_index(grid.cell_for(probe_a)) is None:
							sc += 95.0
							break
				if enemy_pos is not None and not self._is_chord_impassable(pt, enemy_pos):
					sc += 45.0
				if sc > best_sc:
					best_sc = sc
					best_pt = pt
			agent['arty_safe_reloc_pos'] = best_pt
			return best_pt
		except Exception:
			return None

	def note_artillery_arc_status(self, spg_id, target_id, arc_clear, now=0.0):
		"""v5.1: Record whether SPG `spg_id` currently has a clear ballistic arc to `target_id`."""
		try:
			agent = self.agents.get(int(spg_id))
			if agent is None or target_id is None:
				return
			now_f = float(now or 0.0)
			tid = int(target_id)
			arc_map = agent.setdefault('arty_arc_map', {})
			prev = arc_map.get(tid)
			is_clr = bool(arc_clear)
			if is_clr:
				arc_map[tid] = {'clear': True, 'updated_at': now_f, 'blocked_since': None}
			else:
				blk_since = prev.get('blocked_since') if (prev and not prev.get('clear', True)) else None
				if blk_since is None:
					blk_since = now_f
				arc_map[tid] = {'clear': False, 'updated_at': now_f, 'blocked_since': float(blk_since)}
		except Exception:
			pass

	def _assign_spg_home(self, agent, position):
		"""v7.4: Assign each SPG a camouflage-bush artillery position from `spg_positions` + `foliage`
		so SPGs sit inside a bush with Camouflage Net (`маскировочная сеть`) active and clear forward sightlines!"""
		try:
			agent.setdefault('initial_spawn_pos', (float(position[0]), float(position[1]), float(position[2])))
			team = int(agent.get('team', 1) or 1)
			spg_list = self.spg_positions.get(team) or ()
			used_map = self.__dict__.setdefault('_spg_slot_usage', {})
			slot_idx = int(used_map.get(team, 0))
			used_map[team] = slot_idx + 1
			_own_b_spg = self.bases.get(team) or (float(position[0]), float(position[2]))
			_en_b_spg = self.bases.get(2 if team == 1 else 1) or (0.0, 0.0)
			_claimed_pts = self.__dict__.setdefault('_spg_claimed_pts', {}).setdefault(team, [])
			_valid_spg_list = [
				p for p in spg_list
				if math.hypot(float(p[0]) - float(position[0]), float(p[1]) - float(position[2])) <= 240.0 and
				not self._is_water_or_cliff_chord(position, (float(p[0]), float(position[1]), float(p[1]))) and
				not self._is_hazard_point((float(p[0]), float(position[1]), float(p[1])))
			]
			if not _valid_spg_list:
				# Dynamic mode-aware SPG nests (e.g. Assault / Domination where spawns differ from CTF):
				# Fan SPGs out 55..95m laterally and rearward from their spawn and >= 45m from base without crossing rivers!
				_fdx_s = float(_en_b_spg[0]) - float(position[0])
				_fdz_s = float(_en_b_spg[1]) - float(position[2])
				_fl_s = max(1.0, math.hypot(_fdx_s, _fdz_s))
				_ux_s, _uz_s = _fdx_s / _fl_s, _fdz_s / _fl_s
				_lx_s, _lz_s = -_uz_s, _ux_s
				_dyn_nests = []
				for _lat_s in (-68.0, 68.0, -44.0, 44.0, -92.0, 92.0):
					for _back_s in (-28.0, -12.0, 12.0):
						_cx_s = float(position[0]) + _lx_s * _lat_s + _ux_s * _back_s
						_cz_s = float(position[2]) + _lz_s * _lat_s + _uz_s * _back_s
						_cpt_s = (_cx_s, float(position[1]), _cz_s)
						if not self._is_water_or_cliff_chord(position, _cpt_s) and not self._is_hazard_point(_cpt_s):
							_dyn_nests.append((_cx_s, _cz_s))
				_valid_spg_list = _dyn_nests
			if _valid_spg_list:
				claimed = self.__dict__.setdefault('_spg_claimed_indices', {}).setdefault(team, set())
				cands_idx = [i for i in range(len(_valid_spg_list)) if i not in claimed] or list(range(len(_valid_spg_list)))
				def _spg_slot_score(i):
					sx, sz = float(_valid_spg_list[i][0]), float(_valid_spg_list[i][1])
					d_base = math.hypot(sx - float(_own_b_spg[0]), sz - float(_own_b_spg[1]))
					d_me = math.hypot(sx - float(position[0]), sz - float(position[2]))
					pen = 0.0
					if d_base < 45.0:
						pen += (45.0 - d_base) * 12.0
					if d_me < 34.0:
						pen += (34.0 - d_me) * 8.0
					for ox, oz in _claimed_pts:
						d_o = math.hypot(sx - ox, sz - oz)
						if d_o < 36.0:
							pen += (36.0 - d_o) * 14.0
					_fdx_s = float(_en_b_spg[0]) - float(sx)
					_fdz_s = float(_en_b_spg[1]) - float(sz)
					_fdl_s = max(1.0, math.hypot(_fdx_s, _fdz_s))
					_fwd_probe_s = (sx + (_fdx_s / _fdl_s) * 35.0, float(position[1]), sz + (_fdz_s / _fdl_s) * 35.0)
					if self._is_chord_impassable((sx, float(position[1]), sz), _fwd_probe_s):
						pen += 450.0
					return d_me + pen
				chosen_idx = min(cands_idx, key=_spg_slot_score)
				claimed.add(chosen_idx)
				agent['spg_slot_idx'] = chosen_idx
				bx, bz = float(_valid_spg_list[chosen_idx][0]), float(_valid_spg_list[chosen_idx][1])
				if slot_idx >= len(_valid_spg_list):
					lat = float((slot_idx % 3) - 1) * 32.0
					bx += lat
					bz -= lat * 0.5
				# Check if there is a foliage bush within 28m of (bx, bz) that stays >= 32m from spawn & >= 42m from base!
				try:
					fol_list = getattr(self, 'foliage', None) or (self.map_data.get('foliage') if isinstance(self.map_data, dict) else None) or ()
					best_f = None
					best_fd = 28.0
					for f_it in fol_list:
						fx, fz = float(f_it[0] if isinstance(f_it, (tuple, list)) else getattr(f_it, 'x', 0.0)), float(f_it[2] if isinstance(f_it, (tuple, list)) else getattr(f_it, 'z', 0.0))
						fd = math.sqrt((fx - bx) ** 2 + (fz - bz) ** 2)
						if fd < best_fd:
							if math.hypot(fx - float(position[0]), fz - float(position[2])) < 32.0:
								continue
							if math.hypot(fx - float(_own_b_spg[0]), fz - float(_own_b_spg[1])) < 42.0:
								continue
							if any(math.hypot(fx - ox, fz - oz) < 32.0 for ox, oz in _claimed_pts):
								continue
							if self._is_water_or_cliff_chord(position, (fx, float(position[1]), fz)) or self._is_hazard_point((fx, float(position[1]), fz)):
								continue
							best_fd = fd
							best_f = (fx, fz)
					if best_f is not None:
						bx, bz = best_f[0], best_f[1]
				except Exception:
					pass
				_claimed_pts.append((bx, bz))
				pt = (bx, float(position[1]), bz)
				grid = _real_cover_grid()
				if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
					safe_pt = grid.snap_to_safe_baked_point(pt, 6)
					if safe_pt is not None:
						return safe_pt
				return pt
		except Exception:
			pass
		return tuple(position)

	def _spg_reposition_point(self, agent, position, target_pos=None):
		"""Pro Artillery firing-lane finder: evaluate rear SPG nests and lateral rear avenues
		with full 0xFF wall clearance so SPGs never drive forward into city streets or building walls!"""
		try:
			team = int(agent.get('team', 1) or 1)
			spg_list = self.spg_positions.get(team) or ()
			step_idx = int(agent.get('arty_repos_count', 0) or 0) + 1
			agent['arty_repos_count'] = step_idx
			init_sp = agent.get('initial_spawn_pos') or agent.get('artillery_home') or tuple(position)
			eb_check = self._enemy_base_for(team, position)
			profile = agent.get('profile') or {}
			fire_rng = max(450.0, float(profile.get('fire_range', 1200.0) or 1200.0))
			ref_tgt = target_pos if target_pos is not None else (float(eb_check[0]), float(position[1]), float(eb_check[2]))
			dx = float(ref_tgt[0]) - float(init_sp[0])
			dz = float(ref_tgt[2]) - float(init_sp[2])
			dist = max(1.0, math.sqrt(dx * dx + dz * dz))
			ux, uz = dx / dist, dz / dist
			nx, nz = -uz, ux
			side = 1.0 if ((int(agent.get('seed', 0)) + step_idx) & 1) else -1.0
			grid = _real_cover_grid()
			raw_cands = []
			# 1) Prebaked SPG nests (including wide lateral rail/field avenues)
			for _s_idx, _sp in enumerate(spg_list):
				nx_p, nz_p = float(_sp[0]), float(_sp[1])
				jit = float((int(agent.get('id', 0)) % 3) - 1) * 8.0
				raw_cands.append((nx_p + jit, float(position[1]), nz_p - jit * 0.5))
			# 2) Lateral firing lane offsets anchored strictly around init_sp (NEVER advancing step-by-step into city blocks!)
			_fwd_need = max(0.0, min(65.0, dist - fire_rng * 0.86))
			for _lat in (38.0 * side, -38.0 * side, 68.0 * side, -68.0 * side, 98.0 * side, -98.0 * side, 22.0 * side, -22.0 * side):
				for _fwd in (_fwd_need, max(-18.0, _fwd_need - 14.0)):
					raw_cands.append((
						float(init_sp[0]) + nx * _lat + ux * _fwd,
						float(position[1]),
						float(init_sp[2]) + nz * _lat + uz * _fwd))
			best_pt = None
			best_score = -1e18
			for _rc in raw_cands:
				cand = _rc
				if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
					safe_pt = grid.snap_to_safe_baked_point(cand, 6)
					if safe_pt is None:
						continue
					cand = safe_pt
					# Ensure the candidate cell is safely navigable with open transit links
					try:
						_c_idx = grid._baked_index(grid.cell_for(cand))
						if _c_idx is None or bin(int(grid._baked_links[_c_idx])).count('1') < 2:
							continue
					except Exception:
						pass
				d_move = _distance_2d(position, cand)
				if d_move < 12.0 or d_move > 185.0:
					continue
				if self._is_water_or_cliff_chord(position, cand) or self._is_hazard_point(cand):
					continue
				d_tgt = _distance_2d(cand, ref_tgt)
				d_eb = math.hypot(float(cand[0]) - float(eb_check[0]), float(cand[2]) - float(eb_check[2]))
				if d_tgt < 195.0 or d_eb < 210.0:
					continue
				sc = 0.0
				if d_tgt <= fire_rng * 0.92:
					sc += 220.0
				else:
					sc -= (d_tgt - fire_rng * 0.92) * 1.5
				# Check if initial 55m firing corridor from cand toward ref_tgt is clear of buildings/cliffs
				_cdx = float(ref_tgt[0]) - float(cand[0])
				_cdz = float(ref_tgt[2]) - float(cand[2])
				_cdl = max(1.0, math.sqrt(_cdx * _cdx + _cdz * _cdz))
				_probe_55 = (
					float(cand[0]) + (_cdx / _cdl) * min(55.0, _cdl * 0.45),
					float(cand[1]),
					float(cand[2]) + (_cdz / _cdl) * min(55.0, _cdl * 0.45))
				if not self._is_chord_impassable(cand, _probe_55):
					sc += 180.0
				else:
					sc -= 200.0
				# Also check if target end (last 45m before ref_tgt) is clearer along this angle
				_probe_tgt = (
					float(ref_tgt[0]) - (_cdx / _cdl) * min(45.0, _cdl * 0.35),
					float(ref_tgt[1]),
					float(ref_tgt[2]) - (_cdz / _cdl) * min(45.0, _cdl * 0.35))
				if not self._is_chord_impassable(_probe_tgt, ref_tgt):
					sc += 95.0
				sc -= abs(d_move - 42.0) * 0.85
				if sc > best_score:
					best_score = sc
					best_pt = cand
			if best_pt is not None:
				agent['artillery_home'] = best_pt
				return best_pt
			return tuple(position)
		except Exception:
			return tuple(position)

	def _find_wreck_cover(self, agent, position, enemy_pos, max_radius=28.0, ally_positions=()):
		"""Find a nearby dead tank wreck ('труп танка') to use as a hull-down / lower-plate shield in heavy brawls ('в мясных случаях')."""
		try:
			wrecks = getattr(self, 'wrecks', None)
			if not wrecks or position is None or enemy_pos is None:
				return None
			px, py, pz = float(position[0]), float(position[1]), float(position[2])
			ex, ez = float(enemy_pos[0]), float(enemy_pos[2])
			my_enemy_d = math.hypot(ex - px, ez - pz)
			if my_enemy_d < 10.0 or my_enemy_d > 155.0:
				return None
			profile = agent.get('profile') or {}
			is_casemate = bool(profile.get('has_limited_traverse') or profile.get('class_tag') == 'AT-SPG')
			r_sq = float(max_radius) * float(max_radius)
			best_w = None
			best_sc = -1e9
			grid = _real_cover_grid()
			for wid, w in wrecks.items():
				wpos = w.get('position')
				if wpos is None:
					continue
				wx, wy, wz = float(wpos[0]), float(wpos[1]), float(wpos[2])
				dd_sq = (wx - px) ** 2 + (wz - pz) ** 2
				if dd_sq > r_sq:
					continue
				# Never use a multi-wreck graveyard/pileup (>= 2 wrecks within 11.5m) as hull-down cover!
				_near_w_cnt = 0
				for _wid2, _w2 in wrecks.items():
					if _wid2 != wid:
						_wp2 = _w2.get('position')
						if _wp2 is not None and ((float(_wp2[0]) - wx) ** 2 + (float(_wp2[2]) - wz) ** 2) <= 132.25:
							_near_w_cnt += 1
				if _near_w_cnt >= 3:
					continue
				# Vector from enemy to wreck
				ewx = wx - ex
				ewz = wz - ez
				ew_d = math.hypot(ewx, ewz)
				if ew_d < 7.5 or ew_d > my_enemy_d + 6.0:
					continue
				ux, uz = ewx / ew_d, ewz / ew_d
				# Stand point 4.4m behind the wreck relative to enemy fire (shields lower glacis & hull)
				stand_off = max(3.8, float(w.get('half_width', 1.6)) + 2.6)
				sx = wx + ux * stand_off
				sz = wz + uz * stand_off
				sy = wy
				if any((sx - ax) ** 2 + (sz - az) ** 2 < 42.25 for ax, az in ally_positions):
					continue
				if self._is_chord_impassable(position, (sx, sy, sz)):
					continue
				if grid is not None and getattr(grid, 'prebaked', False):
					s_cell = grid.cell_for((sx, sy, sz))
					s_idx = grid._baked_index(s_cell)
					if s_idx is None or not grid._baked_valid[s_idx] or (grid._baked_hazard_near_one and grid._baked_hazard_near_one[s_idx]):
						continue
					sy = float(grid._baked_heights[s_idx]) * 0.001
				# Peek point: for casemate TDs, peek straight over the wreck deck; for turreted tanks, tight 1.15m offset so lower hull stays shielded behind the wreck!
				lat_x, lat_z = -uz, ux
				side_s = 1.0 if ((px - wx) * lat_x + (pz - wz) * lat_z) >= 0.0 else -1.0
				lat_shift = 0.35 if is_casemate else (1.15 * side_s)
				kx = wx + ux * (stand_off - 0.65) + lat_x * lat_shift
				kz = wz + uz * (stand_off - 0.65) + lat_z * lat_shift
				ky = sy
				if self._is_hazard_point((kx, ky, kz), 2):
					continue
				# Require clear firing corridor from the wreck peek point to the enemy (not blocked by solid buildings, mountains, or 3D OBBs)!
				if self._is_mountain_blocked((kx, ky, kz), enemy_pos) or self._is_chord_impassable((kx, ky, kz), enemy_pos):
					continue
				try:
					from gui.mods.offhangar.ai import bot_subengine as _bsub_wc
					if not _bsub_wc.check_3d_sightline((kx, ky, kz), enemy_pos, 1.45):
						continue
				except Exception:
					pass
				d_to_stand = math.hypot(sx - px, sz - pz)
				sc = 62.0 - d_to_stand * 0.95 + (14.0 if my_enemy_d <= 95.0 else 0.0)
				# Score bonus if wreck dimensions adequately shield own vehicle hull/lower plate:
				w_width = float(w.get('half_width', 1.6)) * 2.0
				own_width = float(profile.get('half_width', 1.7)) * 2.0
				if w_width >= own_width * 0.80:
					sc += 32.0  # Massive bonus for wreck covering lower glacis & hull!
				# Score bonus for using wreck in dangerous situations (low HP, targeted, reloading):
				hp_frac = float(agent.get('health_fraction', 1.0) or 1.0)
				if hp_frac < 0.50:
					sc += 28.0  # High danger bonus: use dead tank as emergency shield
				if bool(agent.get('is_targeted')) or (now - float(agent.get('incoming_at', -999.0) or -999.0)) < 4.0:
					sc += 24.0  # Under direct enemy crosshairs / incoming fire
				if max(0.0, _number(reload_time, 4.0) - _number(shoot_timer, 999.0)) > 1.2:
					sc += 20.0  # Reloading behind protective carcass
				if sc > best_sc:
					best_sc = sc
					best_w = {
						'stand': (sx, sy, sz),
						'peek': (kx, ky, kz),
						'face': (ex, sy, ez),
						'kind': 3,
						'is_wreck_cover': True,
						'wreck_id': int(wid),
						'score': sc,
					}
			return best_w
		except Exception:
			return None

	def _dynamic_obstacle_pocket(self, agent, position, enemy_pos, ally_positions=()):
		"""Real-time 2D building/rock wall corner pocket finder on the baked grid when no prebaked pocket is within 16m."""
		try:
			grid = _real_cover_grid()
			if grid is None or not getattr(grid, 'prebaked', False) or position is None or enemy_pos is None:
				return None
			px, py, pz = float(position[0]), float(position[1]), float(position[2])
			ex, ez = float(enemy_pos[0]), float(enemy_pos[2])
			dx, dz = ex - px, ez - pz
			dist = math.hypot(dx, dz)
			if dist < 14.0 or dist > 165.0:
				return None
			ux, uz = dx / dist, dz / dist
			lx, lz = -uz, ux
			profile = agent.get('profile') or {}
			is_casemate = bool(profile.get('has_limited_traverse') or profile.get('class_tag') == 'AT-SPG')
			if is_casemate:
				return None
			_pk_bl = agent.get('_pocket_blacklist') or {}
			_now_bl = float(getattr(self, '_last_now', 0.0) or 0.0)
			for f_off in (0.0, -5.0, 5.0, -10.0):
				for l_off in (-7.0, 7.0, -12.0, 12.0, 0.0):
					sx = px + ux * f_off + lx * l_off
					sz = pz + uz * f_off + lz * l_off
					if any((sx - ax) ** 2 + (sz - az) ** 2 < 182.25 for ax, az in ally_positions):
						continue
					s_cell = grid.cell_for((sx, py, sz))
					s_idx = grid._baked_index(s_cell)
					if s_idx is None or not grid._baked_valid[s_idx] or (grid._baked_hazard_near_one and grid._baked_hazard_near_one[s_idx]):
						continue
					# Stand point must have an impassable building/rock obstacle between it and the enemy within 18m!
					probe_wall = (sx + ux * min(16.0, dist * 0.5), py, sz + uz * min(16.0, dist * 0.5))
					if not self._is_chord_impassable((sx, py, sz), probe_wall):
						continue
					if self._is_chord_impassable(position, (sx, py, sz)):
						continue
					sy = float(grid._baked_heights[s_idx]) * 0.001
					# Find the peek point 5.5..8.5m to the left or right of stand that clears the obstacle edge!
					for p_side in (1.0, -1.0):
						for p_lat in (5.5, 8.0):
							kx = sx + lx * (p_side * p_lat) + ux * 1.5
							kz = sz + lz * (p_side * p_lat) + uz * 1.5
							_bl_k = (int(round(kx)), int(round(kz)))
							if _bl_k in _pk_bl and (_now_bl <= 0.0 or _now_bl < float(_pk_bl.get(_bl_k, 0.0) or 0.0)):
								continue
							if any((kx - ax) ** 2 + (kz - az) ** 2 < 182.25 for ax, az in ally_positions):
								continue
							k_cell = grid.cell_for((kx, sy, kz))
							k_idx = grid._baked_index(k_cell)
							if k_idx is None or not grid._baked_valid[k_idx] or (grid._baked_hazard_near_one and grid._baked_hazard_near_one[k_idx]):
								continue
							ky = float(grid._baked_heights[k_idx]) * 0.001
							_los_3d_ok = False
							try:
								from gui.mods.offhangar.ai import bot_subengine as _bsub_pk
								_los_3d_ok = bool(_bsub_pk.check_3d_sightline((kx, ky, kz), enemy_pos, 1.45))
							except Exception:
								_los_3d_ok = False
							if not self._is_chord_impassable((sx, sy, sz), (kx, sy, kz)) and (_los_3d_ok or not self._is_chord_impassable((kx, sy, kz), enemy_pos)):
								rad = math.radians(16.0 * (-p_side))
								ca, sa = math.cos(rad), math.sin(rad)
								fx = sx + (dx * ca - dz * sa)
								fz = sz + (dx * sa + dz * ca)
								return {
									'stand': (sx, sy, sz),
									'peek': (kx, ky, kz),
									'face': (fx, sy, fz),
									'kind': 1,
								}
			return None
		except Exception:
			return None

	def _find_tactical_pocket(self, agent, position, enemy_pos, max_radius=36.0):
		"""v12.0: O(1) spatial-hash lookup of prebaked `tactical_pockets`, live dead-tank wreck cover (`kind == 3`),
		and dynamic 2D building/rock corner cover (`_dynamic_obstacle_pocket`).
		Returns `{'stand': (x, y, z), 'peek': (x, y, z), 'face': (x, y, z), 'kind': kind, ...}` or None."""
		try:
			if position is None or enemy_pos is None:
				return None
			now = _CLOCK_FN()
			pk_cache = agent.get('_pk_cache')
			if pk_cache is not None and (now - pk_cache[0]) < 1.8:
				if (abs(pk_cache[1] - position[0]) < 9.0 and abs(pk_cache[2] - position[2]) < 9.0 and
						abs(pk_cache[3] - enemy_pos[0]) < 20.0 and abs(pk_cache[4] - enemy_pos[2]) < 20.0):
					return pk_cache[5]

			px, py, pz = float(position[0]), float(position[1]), float(position[2])
			ex, ez = float(enemy_pos[0]), float(enemy_pos[2])
			profile = agent.get('profile') or {}
			is_casemate = bool(profile.get('has_limited_traverse') or profile.get('class_tag') == 'AT-SPG')
			prefer_corner = bool(profile.get('sidescrape_capable', False))
			prefer_hulldown = bool(profile.get('hull_down_specialist', False)) or is_casemate
			has_pike = bool(profile.get('has_pike_nose', False)) or is_casemate

			bx0 = int(math.floor(px / 32.0))
			bz0 = int(math.floor(pz / 32.0))
			r_Cells = int(math.ceil(max_radius / 32.0))
			r_sq = max_radius * max_radius
			bot_id = int(agent.get('id', 0) or 0)

			# Strict 1-tank-per-position limit (13.5m exclusion radius across stand, peek, and ally positions) + dead wreck exclusion (5.5m)
			ally_positions = []
			for oa in self.agents.values():
				if oa['id'] != bot_id and oa.get('team') == agent.get('team') and oa.get('health_fraction', 1.0) > 0.0:
					op = oa.get('position')
					if op is not None:
						ally_positions.append((float(op[0]), float(op[2])))
					for _pk_k in ('_claimed_pocket_pos', '_claimed_pocket_peek'):
						cpk = oa.get(_pk_k)
						if cpk is not None:
							ally_positions.append((float(cpk[0]), float(cpk[2])))
			wreck_positions = []
			for _w_pk in (getattr(self, 'wrecks', None) or {}).values():
				_wp_pk = _w_pk.get('position')
				if _wp_pk is not None:
					wreck_positions.append((float(_wp_pk[0]), float(_wp_pk[2])))
			_pk_bl = agent.get('_pocket_blacklist') or {}

			best_res = None
			best_score = -1e9
			if self._pocket_buckets:
				for dbx in range(-r_Cells, r_Cells + 1):
					for dbz in range(-r_Cells, r_Cells + 1):
						bucket = self._pocket_buckets.get((bx0 + dbx, bz0 + dbz))
						if not bucket:
							continue
						for pk in bucket:
							sx, sy, sz, kx, ky, kz, nx, nz, kind = pk
							_bl_key = (int(round(kx)), int(round(kz)))
							if now < float(_pk_bl.get(_bl_key, 0.0) or 0.0):
								continue
							dd_sq = (sx - px) ** 2 + (sz - pz) ** 2
							if dd_sq > r_sq:
								continue
							tdx = ex - kx
							tdz = ez - kz
							tdist = math.sqrt(tdx * tdx + tdz * tdz)
							if tdist < 9.5:
								continue
							align = (tdx * nx + tdz * nz) / tdist
							if align < (0.38 if is_casemate else 0.32):
								continue
							if self._is_mountain_blocked((kx, ky, kz), enemy_pos) or self._is_chord_impassable(position, (sx, sy, sz)) or self._is_chord_impassable(position, (kx, ky, kz)):
								continue
							_pk_3d_los = False
							try:
								from gui.mods.offhangar.ai import bot_subengine as _bsub_fpk
								_pk_3d_los = bool(_bsub_fpk.check_3d_sightline((kx, ky, kz), enemy_pos, 1.45))
							except Exception:
								_pk_3d_los = False
							if (kind == 1 or tdist <= 95.0) and not _pk_3d_los and self._is_chord_impassable((kx, ky, kz), enemy_pos):
								continue
							if kind == 1 and not self._is_chord_impassable((sx, sy, sz), (sx + (tdx / tdist) * min(18.0, tdist * 0.7), sy, sz + (tdz / tdist) * min(18.0, tdist * 0.7))):
								continue
							occupied = False
							for ax, az in ally_positions:
								if (sx - ax) ** 2 + (sz - az) ** 2 < 182.25 or (kx - ax) ** 2 + (kz - az) ** 2 < 182.25:
									occupied = True
									break
							if not occupied and wreck_positions:
								for wx_p, wz_p in wreck_positions:
									if (sx - wx_p) ** 2 + (sz - wz_p) ** 2 < 30.25 or (kx - wx_p) ** 2 + (kz - wz_p) ** 2 < 30.25:
										occupied = True
										break
							if occupied:
								continue
							dist_p = math.sqrt(dd_sq)
							score = align * 42.0 - dist_p * 0.90 + 26.0  # Solid object cover bonus (building, rock, wall)
							if kind == 1 and (prefer_corner or is_casemate):
								score += 18.0
							elif kind == 2 and prefer_hulldown:
								score += 18.0
							if score > best_score:
								best_score = score
								if is_casemate:
									fx, fz = ex, ez
								else:
									ang_deg = 6.0 if has_pike else (19.0 if kind == 1 else 14.0)
									pk_dx, pk_dz = kx - sx, kz - sz
									cross = (tdx * pk_dz - tdz * pk_dx)
									sign = -1.0 if cross >= 0.0 else 1.0
									rad = math.radians(ang_deg * sign)
									ca, sa = math.cos(rad), math.sin(rad)
									fx = sx + (tdx * ca - tdz * sa)
									fz = sz + (tdx * sa + tdz * ca)
								best_res = {
									'stand': (sx, sy, sz),
									'peek': (kx, ky, kz),
									'face': (fx, sy, fz),
									'kind': kind,
								}

			# Evaluate nearby dead tank wrecks as tactical lower-plate/hull-down cover in brawls ("хорошее укрытие в мясных случаях")!
			wreck_cov = self._find_wreck_cover(agent, position, enemy_pos, max_radius=min(28.0, float(max_radius)), ally_positions=ally_positions)
			if wreck_cov is not None and float(wreck_cov.get('score', 0.0)) > best_score:
				best_res = wreck_cov
				best_score = float(wreck_cov.get('score', 0.0))

			# Fallback: real-time 2D building/rock corner scan if no pocket or wreck was within 16m
			if best_res is None or _distance_2d(position, best_res['stand']) > 18.0:
				dyn_pk = self._dynamic_obstacle_pocket(agent, position, enemy_pos, ally_positions=ally_positions)
				if dyn_pk is not None and (best_res is None or _distance_2d(position, dyn_pk['stand']) + 3.0 < _distance_2d(position, best_res['stand'])):
					best_res = dyn_pk

			agent['_claimed_pocket_pos'] = best_res['stand'] if best_res is not None else None
			agent['_claimed_pocket_peek'] = best_res['peek'] if best_res is not None else None
			agent['_pk_cache'] = (now, px, pz, ex, ez, best_res)
			return best_res
		except Exception:
			return None

	def _real_cover_point(self, agent, position, enemy_pos, max_radius=None):
		"""cached cover search on the baked navgraph (None = no cover)."""
		grid = _real_cover_grid()
		if grid is None:
			return None
		now = _CLOCK_FN()
		cache = agent.get('_rc_cache')
		if cache is not None and now - cache[0] < 8.0:
			cx, cz = cache[1]
			if (abs(cx - position[0]) < 20.0 and abs(cz - position[2]) < 20.0 and
					abs(cache[2][0] - enemy_pos[0]) < 35.0 and
					abs(cache[2][2] - enemy_pos[2]) < 35.0):
				return cache[3]
		# v6.8: Rate-limit heavy Dijkstra cover search across all bots (at most 1 search per 350ms)
		_last_rc_global = float(_REAL_COVER_STATE.get('last_call_at', -999.0) or -999.0)
		if now - _last_rc_global < 0.35:
			return cache[3] if cache is not None else None
		_REAL_COVER_STATE['last_call_at'] = now
		if max_radius is None:
			profile = agent.get('profile') or {}
			tag = profile.get('class_tag')
			max_radius = 22.0 if tag in ('heavyTank', 'AT-SPG') else 30.0
		threats = [tuple(enemy_pos)]
		extra = []
		try:
			known = getattr(self, '_known_contacts_cache', {}).get(int(agent.get('team') or 0), [])
		except Exception:
			known = []
		for contact in known:
			try:
				if not contact.get('visible') or contact.get('position') is None:
					continue
				cp = tuple(contact['position'])
				if abs(cp[0] - enemy_pos[0]) < 10.0 and abs(cp[2] - enemy_pos[2]) < 10.0:
					continue
				dd = (cp[0] - position[0]) ** 2 + (cp[2] - position[2]) ** 2
				if dd < 250.0 * 250.0:
					extra.append((dd, cp))
			except Exception:
				pass
		extra.sort()
		for dd, cp in extra[:2]:
			threats.append(cp)
		point = None
		try:
			home = self._route_anchor(agent, position)
			point = grid.find_cover(position, threats, max_radius=max_radius, home=home)
			if point is not None:
				_bot_id_rc = int(agent.get('id', 0) or 0)
				_crowded_rc = False
				for _oa_rc in self.agents.values():
					if _oa_rc['id'] != _bot_id_rc and _oa_rc.get('team') == agent.get('team') and _oa_rc.get('health_fraction', 1.0) > 0.0:
						for _chk_p in (_oa_rc.get('position'), _oa_rc.get('_claimed_cover_pos')):
							if _chk_p is not None and (float(point[0]) - float(_chk_p[0])) ** 2 + (float(point[2]) - float(_chk_p[2])) ** 2 < 225.0:
								_crowded_rc = True
								break
						if _crowded_rc:
							break
				if _crowded_rc:
					_tdx_rc = float(enemy_pos[0]) - float(point[0])
					_tdz_rc = float(enemy_pos[2]) - float(point[2])
					_tl_rc = max(1.0, math.sqrt(_tdx_rc * _tdx_rc + _tdz_rc * _tdz_rc))
					_nx_rc, _nz_rc = -_tdz_rc / _tl_rc, _tdx_rc / _tl_rc
					_slot_rc = ((_bot_id_rc % 5) - 2)
					if _slot_rc == 0:
						_slot_rc = 1 if (_bot_id_rc & 1) else -1
					_alt_rc = (float(point[0]) + _nx_rc * (_slot_rc * 15.0), float(point[1]), float(point[2]) + _nz_rc * (_slot_rc * 15.0))
					point = grid.snap_to_safe_baked_point(_alt_rc, 6) if hasattr(grid, 'snap_to_safe_baked_point') else _alt_rc
		except Exception as exc:
			_offh_ai_log_note('REAL_COVER_ERR %s' % (exc,))
			point = None
		agent['_claimed_cover_pos'] = point
		agent['_rc_cache'] = (now, (position[0], position[2]), tuple(enemy_pos), point)
		st = _REAL_COVER_STATE
		if point is not None:
			st['found'] += 1
		else:
			st['none'] += 1
		if now - st['log'] > 20.0:
			st['log'] = now
			fan = 0
			try:
				import sys as _rc_sys2
				fan = int(getattr(_rc_sys2.modules.get('gui.mods.offhangar.offline_battle'), 'g_offh_d93_fan', 0) or 0)
			except Exception:
				pass
			_offh_ai_log_note('REAL_COVER found=%d none=%d fan=%d threats=%d last=%s' % (
				st['found'], st['none'], fan, len(threats),
				'none' if point is None else '%.0f,%.0f' % (point[0], point[2])))
		return point

	def _terrain_hulldown_position(self, agent, position, enemy_pos, route_pos=None):
		"""v6.8: Fast cached local crest / hull-down pocket finder (zero BFS spike, 2.5s per-bot cache)."""
		try:
			profile = agent.get('profile') or {}
			if profile.get('class_tag') == 'SPG':
				return None
			now = _CLOCK_FN()
			hd_cache = agent.get('_hd_cache')
			if hd_cache is not None and (now - hd_cache[0]) < 2.5:
				if (abs(hd_cache[1] - position[0]) < 12.0 and abs(hd_cache[2] - position[2]) < 12.0 and
						abs(hd_cache[3] - enemy_pos[0]) < 24.0 and abs(hd_cache[4] - enemy_pos[2]) < 24.0):
					return hd_cache[5]
			# Sample 9 local heightgrid points for a reverse-slope crest cell (where a cell 6-10m toward enemy is higher)
			grid = _real_cover_grid()
			if grid is not None and getattr(grid, 'prebaked', False):
				dx = float(enemy_pos[0]) - float(position[0])
				dz = float(enemy_pos[2]) - float(position[2])
				dlen = math.sqrt(dx * dx + dz * dz)
				if dlen > 12.0:
					ux, uz = dx / dlen, dz / dlen
					nx, nz = -uz, ux
					_ally_hd_pts = []
					_my_id_hd = int(agent.get('id', 0) or 0)
					for _oa_hd in self.agents.values():
						if _oa_hd['id'] != _my_id_hd and _oa_hd.get('team') == agent.get('team') and _oa_hd.get('health_fraction', 1.0) > 0.0:
							for _hp_c in (_oa_hd.get('position'), _oa_hd.get('_claimed_hd_pos')):
								if _hp_c is not None:
									_ally_hd_pts.append((float(_hp_c[0]), float(_hp_c[2])))
					best_pt = None
					best_sc = -1e9
					seed_s = 1.0 if (_my_id_hd & 1) else -1.0
					for fwd in (-6.0, 0.0, 6.0):
						for lat in (-14.0 * seed_s, -7.0 * seed_s, 0.0, 7.0 * seed_s, 14.0 * seed_s):
							cx = float(position[0]) + ux * fwd + nx * lat
							cz = float(position[2]) + uz * fwd + nz * lat
							if any((cx - _ax) ** 2 + (cz - _az) ** 2 < 196.0 for _ax, _az in _ally_hd_pts):
								continue
							cell = grid.cell_for((cx, position[1], cz))
							idx = grid._baked_index(cell)
							if idx is None or not grid._baked_valid[idx]:
								continue
							if grid._baked_hazard_near_one and grid._baked_hazard_near_one[idx]:
								continue
							h_here = float(grid._baked_heights[idx]) * 0.001
							# Check crest height 6m and 10m toward the enemy
							c_cell1 = grid.cell_for((cx + ux * 6.0, position[1], cz + uz * 6.0))
							c_idx1 = grid._baked_index(c_cell1)
							if c_idx1 is None or grid._baked_heights[c_idx1] is None:
								continue
							h_crest = float(grid._baked_heights[c_idx1]) * 0.001
							c_cell2 = grid.cell_for((cx + ux * 10.0, position[1], cz + uz * 10.0))
							c_idx2 = grid._baked_index(c_cell2)
							if c_idx2 is not None and grid._baked_heights[c_idx2] is not None:
								h_crest = max(h_crest, float(grid._baked_heights[c_idx2]) * 0.001)
							diff_h = h_crest - h_here
							# Ideal hull-down: crest is 0.45m..1.85m higher than our tracks, hiding lower plate!
							if 0.45 <= diff_h <= 1.85:
								sc = 12.0 - abs(diff_h - 1.05) * 6.0 - (abs(fwd) + abs(lat)) * 0.18
								if sc > best_sc:
									best_sc = sc
									best_pt = grid.point_for(cell, h_here)
					agent['_claimed_hd_pos'] = best_pt
					agent['_hd_cache'] = (now, float(position[0]), float(position[2]), float(enemy_pos[0]), float(enemy_pos[2]), best_pt)
					if best_pt is not None:
						return best_pt
		except Exception:
			pass
		return None

	def _reload_cover_position(self, agent, position, enemy_pos):
		"""Calculate a cover/retreat position away from enemy fire while reloading.

		Instead of standing exposed in the open during reload, the bot pulls back
		behind the nearest building corner / LOS shadow (5-12m) or toward its route anchor.
		"""
		profile = agent.get('profile') or {}
		if profile.get('class_tag') == 'SPG':
			return None
		dx = position[0] - enemy_pos[0]
		dz = position[2] - enemy_pos[2]
		dist = math.sqrt(dx * dx + dz * dz)
		if dist < 0.1:
			return None
		dir_x = dx / dist
		dir_z = dz / dist

		_is_casemate = bool(profile.get('has_limited_traverse') or profile.get('class_tag') == 'AT-SPG')
		# 1. O(1) tactical pocket & dead-wreck cover lookup (casemate TDs only accept frontal/hull-down/wreck pockets within +-22 deg of straight reverse)
		pk = self._find_tactical_pocket(agent, position, enemy_pos, max_radius=24.0)
		if pk is not None and _distance_2d(position, pk['stand']) <= 22.0:
			if not _is_casemate:
				return pk['stand']
			_rdx = float(pk['stand'][0]) - float(position[0])
			_rdz = float(pk['stand'][2]) - float(position[2])
			_rl = math.hypot(_rdx, _rdz)
			if _rl <= 2.5 or ((_rdx * dir_x + _rdz * dir_z) / _rl) >= 0.88:
				return pk['stand']

		# 2. Fast 2D/3D Map Building Shadow scan (never steps into water/cliffs; requires solid building/ridge occlusion)
		try:
			from gui.mods.offhangar.ai import bot_subengine as _bsub_rc
		except Exception:
			_bsub_rc = None
		try:
			lat_x, lat_z = -dir_z, dir_x
			_cand_dirs = ((1.0, 0.0), (0.94, 0.34), (0.94, -0.34)) if _is_casemate else ((0.75, 0.65), (0.75, -0.65), (0.35, 0.92), (0.35, -0.92), (1.0, 0.0))
			for step_d in (6.5, 10.5):
				for f_w, l_w in _cand_dirs:
					cx = float(position[0]) + (dir_x * f_w + lat_x * l_w) * step_d
					cz = float(position[2]) + (dir_z * f_w + lat_z * l_w) * step_d
					cand = (cx, float(position[1]), cz)
					if self._is_hazard_point(cand, 2) or self._is_water_or_cliff_chord(position, cand) or self._is_chord_impassable(position, cand):
						continue
					_solid_occ = self._is_mountain_blocked(cand, enemy_pos) or (_bsub_rc is not None and not _bsub_rc.check_3d_sightline(cand, enemy_pos, 1.45))
					if _solid_occ:
						return cand
		except Exception:
			pass

		# 3. Full Dijkstra cover search fallback (only if within 16m, away from water/cliffs, and genuinely occluding LOS)
		if _real_cover_on():
			cover = self._real_cover_point(agent, position, enemy_pos)
			if (cover is not None and _distance_2d(position, cover) <= 16.0 and
					not self._is_hazard_point(cover, 2) and not self._is_water_or_cliff_chord(position, cover) and
					self._is_chord_impassable(cover, enemy_pos)):
				return cover

		# Never fabricate a blind 8.5m pullback in open streets without real LOS-occluding cover!
		return None

	def _evaluate_cyber_bait(self, agent, order, position, contact, now, reload_remaining):
		"""v5.1: Tactical corner-baiting, enemy reload (CD) estimation, and whiff/bounce punishment.

		- Tracks the enemy's estimated remaining reload via `estimate_target_reload_left`.
		- Detects when the enemy misses a shot near the bot or bounces off its armor (`last_whiff_from`).
		- When the enemy wastes a shot and goes on reload:
		  * Long reload (>= 5.5s, or one-shotable enemy): transitions to `punish_push` — drives out
		    around the corner to punish the helpless reloading enemy and calls allies to push!
		  * Medium reload (2.4s..5.5s): transitions to `punish_peek` — steps out to a clear line of
		    sight, fires a shot, and retreats before the enemy finishes reloading!
		"""
		skill_tier = 'hard'
		if skill_tier not in ('hard', 'medium'):
			return None
		tank_level = int(agent.get('profile', {}).get('level', 5) or 5)
		if tank_level < 3:
			return None
		if contact is None or not contact.get('visible'):
			agent['cyber_bait_state'] = 'idle'
			return None

		target_id = contact.get('id')
		if target_id is None:
			return None

		dist = _distance_2d(position, contact['position'])
		if dist < CYBER_BAIT_MIN_RANGE or dist > CYBER_BAIT_MAX_RANGE:
			agent['cyber_bait_state'] = 'idle'
			return None

		est_reload_left, est_total_rld, target_just_fired = self.estimate_target_reload_left(
			agent, target_id, now)
		whiff_from = agent.get('last_whiff_from')
		whiff_at = float(agent.get('last_whiff_at', -999.0) or -999.0)
		recent_whiff_on_me = (
			whiff_from is not None and int(whiff_from) == int(target_id) and
			(now - whiff_at) <= 4.0)

		# Compute corner-peek and retreat-cover positions
		dx = float(contact['position'][0]) - float(position[0])
		dz = float(contact['position'][2]) - float(position[2])
		dlen = max(0.1, math.sqrt(dx * dx + dz * dz))
		ux, uz = dx / dlen, dz / dlen
		seed = int(agent.get('seed', 0) or 0)
		side = 1.0 if (seed & 1) else -1.0
		nx, nz = -uz * side, ux * side
		start_pos = agent.get('cyber_bait_start_pos') or tuple(position)
		bait_out_pos = (
			float(start_pos[0]) + ux * 5.8 + nx * 4.2,
			float(position[1]),
			float(start_pos[2]) + uz * 5.8 + nz * 4.2)
		bait_back_pos = self._reload_cover_position(agent, position, contact['position'])
		if bait_back_pos is None:
			bait_back_pos = (
				float(start_pos[0]) - ux * 8.5 + nx * 2.5,
				float(position[1]),
				float(start_pos[2]) - uz * 8.5 + nz * 2.5)

		_s_ids = contact.get('shootable_by_ids')
		has_direct_lane = bool(contact.get('visible') and (_s_ids is None or int(agent['id']) in _s_ids))
		force_balance = _number(order.get('force_balance', 0.0), 0.0)
		own_shells = (agent.get('profile') or {}).get('shells', ()) or ()
		own_alpha = _number(own_shells[0].get('damage', 200.0), 200.0) if own_shells else 200.0
		target_hp = _number(contact.get('health', 9999.0), 9999.0)

		state = agent.get('cyber_bait_state', 'idle')
		state_timer = float(agent.get('cyber_bait_timer', 0.0) or 0.0)
		cooldown_until = float(agent.get('cyber_bait_cooldown_until', 0.0) or 0.0)

		_s_ids_cb = contact.get('shootable_by_ids')
		_has_lane_cb = bool(contact.get('visible') and (_s_ids_cb is None or int(agent['id']) in _s_ids_cb))
		_cb_tpos = contact['position']
		if (not _has_lane_cb and not self._has_3d_sightline(position, _cb_tpos, 1.45) and self._is_chord_impassable(position, _cb_tpos)):
			agent['cyber_bait_state'] = 'idle'
			return None

		ray_info = self._evaluate_enemy_gun_rays_and_reload(agent, position, contact, peek_pos=position, now=now)
		# Instant Whiff / Bounce / Reload Window / Distracted Turret Trigger ('кто откдшился' + 'лучи орудий'):
		# Trigger punishment whenever the enemy is on reload (est_reload_left >= 2.1s) OR has turned its gun ray away (>= 0.52 rad)
		# while no other loaded enemy gun ray is locking us and our own gun is loaded (reload_remaining <= 0.85s)!
		if state not in ('punish_push', 'punish_peek') and now >= cooldown_until:
			_distracted_window = bool(ray_info.get('target_looking_away') and not ray_info.get('any_loaded_gun_locking') and dist <= 115.0 and force_balance >= -1.1)
			if ((recent_whiff_on_me or (est_reload_left >= 2.1 and (_has_lane_cb or dist <= 115.0)) or _distracted_window) and
					not ray_info.get('any_loaded_gun_locking') and reload_remaining <= 0.85 and force_balance >= -1.1):
				wants_punish = (skill_tier == 'hard') or (skill_tier == 'medium' and ((seed + int(now)) % 3 != 0))
				if wants_punish:
					agent['cyber_bait_distracted'] = bool(_distracted_window and est_reload_left < 2.1)
					if (est_reload_left >= 5.5 or target_hp <= own_alpha * 1.1) and dist <= 135.0 and force_balance >= -0.95:
						punish_window = min(8.0, max(2.0, est_reload_left - 0.8))
						agent['cyber_bait_state'] = 'punish_push'
						agent['cyber_bait_timer'] = now + punish_window
						agent['cyber_bait_cooldown_until'] = now + punish_window + 4.5
						state = 'punish_push'
						state_timer = agent['cyber_bait_timer']
						_team_pun_last = self.__dict__.setdefault('_co_punish_team_last', {})
						if (now - float(agent.get('last_punish_call_at', -999.0)) >= 35.0 and
								now - float(_team_pun_last.get(agent['team'], -999.0)) >= 24.0):
							agent['last_punish_call_at'] = now
							_team_pun_last[agent['team']] = now
							if self._callouts_on():
								self._callout_event(
									'punish_push', agent['id'], target_id,
									'rld:%d' % int(round(est_reload_left)))
							self.apply_radio_command(
								RADIO_COMMAND_ATTACK_ENEMY, agent['id'], agent['team'],
								position, now, target_id=target_id)
						_offh_ai_log_note('CYBER_PUNISH bot=%s target=%s mode=punish_push est_rld=%.1fs dist=%.1fm' % (
							str(agent['id']), str(target_id), est_reload_left, dist))
					else:
						punish_window = min(3.8, max(1.5, est_reload_left - 0.7))
						agent['cyber_bait_state'] = 'punish_peek'
						agent['cyber_bait_timer'] = now + punish_window
						agent['cyber_bait_cooldown_until'] = now + punish_window + 4.0
						state = 'punish_peek'
						state_timer = agent['cyber_bait_timer']
						_offh_ai_log_note('CYBER_PUNISH bot=%s target=%s mode=punish_peek est_rld=%.1fs dist=%.1fm' % (
							str(agent['id']), str(target_id), est_reload_left, dist))

		# Disable artificial forward/backward micro-twitches (twitch_out / twitch_back)
		# so tanks never rock back and forth in the middle of streets! Only execute
		# genuine reload punishment (punish_push / punish_peek) when enemy wasted a shot.
		if state not in ('punish_push', 'punish_peek'):
			agent['cyber_bait_state'] = 'idle'
			return None

		# State 4: PUNISH_PUSH - Enemy wasted a long-reload shot; drive out with lateral slot dispersion (no herding)!
		if state == 'punish_push':
			punish_until = state_timer
			if now >= punish_until or est_reload_left <= 0.85 or (reload_remaining > 1.2 and est_reload_left < reload_remaining + 0.8):
				agent['cyber_bait_state'] = 'idle'
				agent['cyber_bait_cooldown_until'] = now + 3.5
				return None
			_ctag_pp = (agent.get('profile') or {}).get('class_tag', 'mediumTank')
			if _ctag_pp in ('lightTank', 'mediumTank'):
				push_pos = self._flank_position(agent, position, contact['position'])
			else:
				push_pos = self._spread_attack_position(agent, position, contact['position'])
			return {
				'move_position': push_pos,
				'face_position': self._angled_face_position(agent, position, contact['position'], now=now),
				'combat_mode': 'punish_push',
				'fire_allowed': True,
				'throttle_override': None,
			}

		# State 5: PUNISH_PEEK - Step out from cover to a clear firing point, shoot while angled, then duck back!
		if state == 'punish_peek':
			punish_until = state_timer
			if now >= punish_until or (not agent.get('cyber_bait_distracted') and est_reload_left <= 0.75) or reload_remaining > 0.85:
				agent['cyber_bait_state'] = 'idle'
				agent['cyber_bait_cooldown_until'] = now + 4.0
				return None
			_ang_face = self._angled_face_position(agent, position, contact['position'], now=now)
			if not has_direct_lane:
				peek_pos = self._terrain_hulldown_position(agent, position, contact['position']) or self._spread_attack_position(agent, position, contact['position'])
				return {
					'move_position': peek_pos,
					'face_position': _ang_face,
					'combat_mode': 'punish_peek',
					'fire_allowed': True,
					'throttle_override': None,
				}
			return {
				'move_position': tuple(position),
				'face_position': _ang_face,
				'combat_mode': 'punish_peek',
				'fire_allowed': True,
				'throttle_override': 0.0,
			}

		return None

	def _evaluate_high_ground_tactics(self, agent, position, contact, now):
		"""v12.0 Hard-Mode Hill Tricks (50/50 Coin Flip on Slopes & Ridges):
		When a Hard (or 50% Medium) bot is on elevated terrain (1.8m <= delta_y <= 8.5m, dist <= 46m) above an enemy:
		- Coin 0 ('crest_depress_peek'): Edge partly down the slope (8..14m) so hull pitch tilts the gun down
		  enough to overcome gun depression limits and shoot the target below!
		- Coin 1 ('airborne_jump_shot' / 'cliff_dive_ram'): Go full throttle (1.0) over the hill crest,
		  fire mid-air while jumping/descending, and crush/ram the enemy below from above!"""
		if contact is None or not contact.get('visible') or position is None:
			return None
		c_pos = contact.get('position')
		if c_pos is None:
			return None

		delta_y = float(position[1]) - float(c_pos[1])
		if delta_y < 1.8 or delta_y > 8.5:
			return None

		horiz_dist = _distance_2d(position, c_pos)
		if horiz_dist < 3.0 or horiz_dist > 46.0:
			return None

		skill_tier = 'hard'
		if skill_tier == 'easy':
			return None
		profile = agent.get('profile') or {}
		class_tag = profile.get('class_tag', 'mediumTank')
		if class_tag == 'SPG':
			return None
		health_frac = _number(agent.get('health_fraction', 1.0), 1.0)
		seed = int(agent.get('seed', 0) or 0)
		if skill_tier == 'medium' and ((seed + int(agent.get('id', 0) or 0)) & 1) != 0:
			return None

		# 50/50 coin flip per 10s tactical window: Crest-Depression Descent vs Full-Throttle Jump/Dive-Ram!
		coin = ((seed + int(contact.get('id', 0) or 0) + int(now / 10.0)) & 1)
		dx = float(c_pos[0]) - float(position[0])
		dz = float(c_pos[2]) - float(position[2])
		ux, uz = dx / max(horiz_dist, 0.5), dz / max(horiz_dist, 0.5)

		# Coin 1 disabled: prevents bots from diving off cliffs or climbing onto vertical rocks!
		if False and coin == 1 and class_tag != 'lightTank' and horiz_dist <= 28.0 and 1.8 <= delta_y <= 3.0 and health_frac >= 0.55:
			target_hp = _number(contact.get('health', 1000.0), 1000.0)
			own_hp = _number(agent.get('hp_abs', 1000.0), 1000.0)
			speed = _number(profile.get('speed', 12.0), 12.0)
			ram_cd = float(agent.get('_cliff_ram_cd', 0.0) or 0.0)
			ram_started = float(agent.get('_cliff_ram_started', 0.0) or 0.0)
			if ram_started > 0.0 and (now - ram_started) > 3.2:
				agent['_cliff_ram_started'] = 0.0
				agent['_cliff_ram_cd'] = now + 24.0
				ram_cd = now + 24.0
			if now >= ram_cd and not self._is_mountain_blocked(position, c_pos) and not self._is_chord_impassable(position, c_pos):
				# Heavy / armored tank crushes enemy from above ('cliff_dive_ram'); fast LT/MT executes mid-air jump shot ('airborne_jump_shot')!
				is_dive_crush = (class_tag in ('heavyTank', 'AT-SPG') or own_hp >= target_hp * 0.95 or target_hp <= 420.0)
				if is_dive_crush or speed >= 12.0:
					mode_name = 'cliff_dive_ram' if is_dive_crush else 'airborne_jump_shot'
					if ram_started <= 0.0:
						agent['_cliff_ram_started'] = now
						_offh_ai_log_note(
							'HILL_TRICK bot=%s target=%s mode=%s dy=%.1f dist=%.1f HP=%.2f' % (
								str(agent['id']), str(contact['id']), mode_name, delta_y, horiz_dist, health_frac))
					return {
						'move_position': c_pos,
						'aim_position': c_pos,
						'face_position': c_pos,
						'fire_allowed': True,
						'throttle_override': 1.0,
						'combat_mode': mode_name,
					}

		# Coin 0 (or fallback when jump is on cooldown): Controlled Crest Descent to tilt nose down for Gun Depression!
		if (delta_y / max(horiz_dist, 1.0)) >= 0.11 and horiz_dist >= 10.0 and not self._is_chord_impassable(position, c_pos):
			step_down = min(12.0, max(6.5, horiz_dist * 0.32))
			side = 1.0 if (seed & 2) else -1.0
			desc_pt = (
				float(position[0]) + ux * step_down - uz * 3.0 * side,
				float(position[1]),
				float(position[2]) + uz * step_down + ux * 3.0 * side)
			grid = _real_cover_grid()
			if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
				desc_pt = grid.snap_to_safe_baked_point(desc_pt, 5)
				if desc_pt is None or self._is_hazard_point(desc_pt, 2) or self._is_water_or_cliff_chord(position, desc_pt) or self._is_chord_impassable(position, desc_pt):
					return None
			return {
				'move_position': desc_pt,
				'aim_position': c_pos,
				'face_position': self._angled_face_position(agent, position, c_pos, now=now),
				'fire_allowed': True,
				'throttle_override': 0.58,
				'combat_mode': 'crest_depress_peek',
			}

		return None

	def _evaluate_defender_shield(self, agent, position, contact, now):
		"""v12.0 Defender Mechanic ('Защитник' / living armor shield):
		When a nearby allied tank (<= 48m, including the player) is critically wounded (HP < 38%)
		and under fire from a visible enemy, a healthy armored bot (HP >= 62%, HT or armored MT/TD)
		drives onto the line of fire 9.5m in front of the wounded ally and angles its armor as a shield!"""
		try:
			profile = agent.get('profile') or {}
			class_tag = profile.get('class_tag', 'mediumTank')
			armor = _number(profile.get('armor', 80.0), 80.0)
			if class_tag == 'SPG' or class_tag == 'lightTank':
				return None
			if class_tag != 'heavyTank' and armor < 115.0:
				return None
			if _number(agent.get('health_fraction', 1.0), 1.0) < 0.62:
				return None
			skill_tier = 'hard'
			if skill_tier == 'easy':
				return None
			if skill_tier == 'medium' and ((int(agent.get('seed', 0) or 0) + int(agent.get('id', 0) or 0)) % 3 == 0):
				return None
			if contact is None or not contact.get('visible') or contact.get('position') is None:
				return None
			threat_pos = contact['position']
			team = int(agent.get('team', 1) or 1)
			my_id = int(agent.get('id', 0) or 0)
			best_ally_pos = None
			best_ally_id = None
			best_d = 48.0
			for oa in self.agents.values():
				if oa['id'] == my_id or int(oa.get('team', 0) or 0) != team:
					continue
				hpf = _number(oa.get('health_fraction', 1.0), 1.0)
				if hpf <= 0.0 or hpf > 0.38 or oa.get('position') is None:
					continue
				if (now - float(oa.get('incoming_at', -999.0) or -999.0)) > 9.0:
					continue
				shielder = oa.get('_shielded_by_id')
				if shielder is not None and int(shielder) != my_id and (now - float(oa.get('_shielded_at', -999.0) or -999.0)) < 6.0:
					continue
				d_a = _distance_2d(position, oa['position'])
				if d_a < best_d:
					best_d = d_a
					best_ally_pos = oa['position']
					best_ally_id = oa['id']
			# Also check if the human player on our team is low HP nearby!
			pvid = getattr(self, 'player_vid', None)
			if (best_ally_pos is None and pvid is not None and int(getattr(self, 'player_team', -1) or -1) == team and
					getattr(self, 'player_position', None) is not None and
					0.0 < _number(getattr(self, 'player_hp_frac', 1.0), 1.0) <= 0.38):
				d_p = _distance_2d(position, self.player_position)
				if d_p <= 48.0:
					best_ally_pos = self.player_position
					best_ally_id = int(pvid)
			if best_ally_pos is None:
				return None
			tdx = float(threat_pos[0]) - float(best_ally_pos[0])
			tdz = float(threat_pos[2]) - float(best_ally_pos[2])
			tdist = math.sqrt(tdx * tdx + tdz * tdz)
			if tdist < 18.0 or tdist > 240.0:
				return None
			ux, uz = tdx / tdist, tdz / tdist
			shield_pt = (
				float(best_ally_pos[0]) + ux * 9.5,
				float(position[1]),
				float(best_ally_pos[2]) + uz * 9.5)
			grid = _real_cover_grid()
			if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
				shield_pt = grid.snap_to_safe_baked_point(shield_pt, 5) or shield_pt
			if self._is_chord_impassable(position, shield_pt):
				return None
			if best_ally_id in self.agents:
				self.agents[best_ally_id]['_shielded_by_id'] = my_id
				self.agents[best_ally_id]['_shielded_at'] = now
			if now - float(agent.get('_last_shield_log', -999.0) or -999.0) >= 18.0:
				agent['_last_shield_log'] = now
				_offh_ai_log_note('DEFENDER_SHIELD bot=%s protecting_ally=%s threat=%s' % (
					str(my_id), str(best_ally_id), str(contact.get('id'))))
			return {
				'move_position': shield_pt,
				'aim_position': threat_pos,
				'face_position': self._angled_face_position(agent, position, threat_pos, now=now),
				'fire_allowed': True,
				'throttle_override': 0.0 if _distance_2d(position, shield_pt) <= 4.5 else None,
				'combat_mode': 'defender_shield',
			}
		except Exception:
			return None

	def _evaluate_lone_wolf(self, agent, position, contact, route_position, force_balance, now):
		"""v12.0 Lone Wolf Mechanic ('Одинокий волк'):
		When a non-SPG bot finds itself completely alone on a flank (0 living allies within 145m after 35s of battle):
		- If outnumbered / about to be surrounded (enemies_near >= 2 or force_balance <= -0.65):
		  executes a fighting tactical retreat ('lone_wolf_retreat') from cover to cover toward the nearest
		  allied group or home base, keeping frontal armor and gun pointed at the pursuers!
		- If facing 0..1 enemies on Hard/Medium: plays stealthy ambush/flank ('lone_wolf_stalk') using cover pockets!"""
		try:
			profile = agent.get('profile') or {}
			class_tag = profile.get('class_tag', 'mediumTank')
			if class_tag in ('SPG', 'AT-SPG') or profile.get('has_limited_traverse'):
				return None
			skill_tier = 'hard'
			if skill_tier == 'easy':
				return None
			battle_start = float(self.__dict__.get('_battle_start_at', now) or now)
			if (now - battle_start) < 35.0:
				return None
			team = int(agent.get('team', 1) or 1)
			my_id = int(agent.get('id', 0) or 0)
			nearest_ally_pos = None
			nearest_ally_d = 1e18
			for oa in self.agents.values():
				if oa['id'] == my_id or int(oa.get('team', 0) or 0) != team:
					continue
				if _number(oa.get('health_fraction', 1.0), 1.0) <= 0.0 or oa.get('position') is None:
					continue
				d_a = _distance_2d(position, oa['position'])
				if d_a <= 145.0:
					return None  # Not alone!
				if d_a < nearest_ally_d:
					nearest_ally_d = d_a
					nearest_ally_pos = oa['position']
			pvid = getattr(self, 'player_vid', None)
			if (pvid is not None and int(getattr(self, 'player_team', -1) or -1) == team and
					getattr(self, 'player_position', None) is not None and
					_number(getattr(self, 'player_hp_frac', 1.0), 1.0) > 0.0):
				d_p = _distance_2d(position, self.player_position)
				if d_p <= 145.0:
					return None
				if d_p < nearest_ally_d:
					nearest_ally_d = d_p
					nearest_ally_pos = self.player_position

			# Bot is a Lone Wolf on this flank! Count non-SPG enemies within 195m (never retreat from artillery!)
			if contact is not None and str(contact.get('class_tag', '')) == 'SPG':
				return None
			enemies_near = 0
			for c in self._known_contacts(team, now):
				if str(c.get('class_tag', '')) == 'SPG':
					continue
				if c.get('position') is not None and (c.get('visible') or (now - _number(c.get('last_seen'), -999.0)) <= 18.0):
					if _distance_2d(position, c['position']) <= 195.0:
						enemies_near += 1
			t_pos = contact['position'] if (contact is not None and contact.get('position') is not None) else None
			d_enemy = _distance_2d(position, t_pos) if t_pos is not None else 999.0
			# Never trigger lone_wolf_retreat in a 1v1 or < 28m brawl; only when outnumbered (enemies_near >= 2) and losing!
			if d_enemy >= 28.0 and enemies_near >= 2 and (force_balance <= -0.85 or _number(agent.get('health_fraction', 1.0), 1.0) < 0.45):
				ret_pt = agent.get('_lw_retreat_pos')
				ret_until = float(agent.get('_lw_retreat_until', 0.0) or 0.0)
				if ret_pt is None or now >= ret_until or _distance_2d(position, ret_pt) <= 6.5:
					own_b = self.bases.get(team)
					ret_goal = nearest_ally_pos if nearest_ally_pos is not None else (
						(float(own_b[0]), float(position[1]), float(own_b[1])) if own_b is not None else self._fallback_position(agent, position))
					rdx = float(ret_goal[0]) - float(position[0])
					rdz = float(ret_goal[2]) - float(position[2])
					rdl = max(1.0, math.sqrt(rdx * rdx + rdz * rdz))
					step_r = min(28.0, rdl)
					ret_pt = (float(position[0]) + (rdx / rdl) * step_r, float(position[1]), float(position[2]) + (rdz / rdl) * step_r)
					grid = _real_cover_grid()
					if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
						ret_pt = grid.snap_to_safe_baked_point(ret_pt, 10) or ret_pt
					if self._is_chord_impassable(position, ret_pt):
						ret_pt = self._fallback_position(agent, position)
					agent['_lw_retreat_pos'] = ret_pt
					agent['_lw_retreat_until'] = now + 5.0
				if now - float(agent.get('_last_lw_log', -999.0) or -999.0) >= 18.0:
					agent['_last_lw_log'] = now
					_offh_ai_log_note('LONE_WOLF bot=%s mode=retreat enemies_near=%d balance=%.2f' % (
						str(my_id), enemies_near, force_balance))
				_aim_lw = t_pos if t_pos is not None else ret_pt
				_d_ret_lw = _distance_2d(position, ret_pt)
				_rev_lw = bool(contact is not None and contact.get('visible') and d_enemy <= 145.0 and _d_ret_lw <= 34.0)
				return {
					'move_position': ret_pt,
					'aim_position': _aim_lw,
					'face_position': (self._angled_face_position(agent, position, _aim_lw, now=now) if (t_pos is not None and (d_enemy <= 165.0 or _rev_lw)) else ret_pt),
					'fire_allowed': bool(contact is not None and contact.get('visible')),
					'throttle_override': -0.85 if _rev_lw else None,
					'combat_mode': 'lone_wolf_retreat',
				}
			elif contact is not None and contact.get('visible') and enemies_near == 1:
				# 1v1 Lone Wolf duel: use tactical pocket / hull-down ambush
				pk_lw = self._find_tactical_pocket(agent, position, contact['position'], max_radius=34.0)
				if pk_lw is not None:
					return {
						'move_position': pk_lw['peek'],
						'aim_position': contact['position'],
						'face_position': pk_lw['face'],
						'fire_allowed': True,
						'throttle_override': None,
						'combat_mode': 'lone_wolf_stalk',
					}
		except Exception:
			pass
		return None

	def _evaluate_fear_response(self, agent, position, contact, route_position, force_balance, reload_remaining, now):
		"""v14.0 Authentic Tactical Sense of Fear & Self-Preservation across ALL classes (LT, MT, HT, AT-SPG):
		- Any tank under mortal threat of destruction (hp_frac <= 0.34 or hp_abs <= 300), taking hits in the open
		  (inc_age <= 6.5s), or outnumbered/focused by >= 2 enemies immediately seeks hard cover and reverses out
		  of the line of fire instead of sitting stationary or charging into multiple guns!
		- All tanks within 135m reverse straight back into cover keeping their frontal armor aimed at the threat (throttle_override = -0.88).
		- Once in cover and gun is loaded (reload_remaining <= 0.45s and inc_age > 3.8s), returns None so the tank peeks out to shoot instead of freezing!"""
		try:
			if contact is None or contact.get('position') is None:
				return None
			profile = agent.get('profile') or {}
			class_tag = str(profile.get('class_tag', 'mediumTank'))
			target_cls = str(contact.get('class_tag', 'mediumTank'))
			if class_tag == 'SPG' or target_cls == 'SPG':
				return None
			is_casemate = bool(class_tag == 'AT-SPG' or profile.get('has_limited_traverse'))
			c_pos = contact['position']
			dist = _distance_2d(position, c_pos)
			if dist < 14.0 or dist > 235.0:
				return None
			hp_frac = _number(agent.get('health_fraction', 1.0), 1.0)
			hp_abs = _number(agent.get('hp_abs', 1000.0), 1000.0)
			inc_age = float(now) - float(agent.get('incoming_at', -999.0) or -999.0)
			target_hp = _number(contact.get('health', 1000.0), 1000.0)
			team = int(agent.get('team', 1) or 1)
			enemies_135 = 0
			for _oc_f in self._known_contacts(team, now):
				if str(_oc_f.get('class_tag', '')) == 'SPG':
					continue
				if (_oc_f.get('visible') or (now - float(_oc_f.get('last_seen', -999.0) or -999.0)) <= 6.0) and _oc_f.get('position') is not None and _distance_2d(position, _oc_f['position']) <= 135.0:
					enemies_135 += 1
			mortal_danger = (hp_frac <= 0.30 or hp_abs <= 260.0)
			# Never interrupt an active dynamic attack (circle_of_death, wolfpack_encircle, ram_brawl, clinch, punish_push)
			# or a 1v1 LT/MT close-range orbit (< 90m) unless critically low HP and outnumbered!
			_prev_cm_f = str((agent.get('last_order') or {}).get('combat_mode', '') or '')
			if _prev_cm_f in ('circle_of_death', 'wolfpack_encircle', 'ram_brawl', 'clinch', 'punish_push') and not (hp_frac <= 0.22 and enemies_135 >= 2):
				return None
			if class_tag in ('lightTank', 'mediumTank') and dist <= 90.0 and enemies_135 <= 2 and hp_frac >= 0.25:
				return None
			# Once our gun is loaded (<= 0.45s) and we haven't been hit in the last 3.8s, allow peeking/shooting instead of freezing in cover forever!
			if reload_remaining <= 0.45 and inc_age > 3.8:
				return None
			should_fear = False
			reverse_retreat = (dist <= 135.0 or is_casemate)
			if mortal_danger:
				should_fear = True
			elif inc_age <= 6.5 and (hp_frac < 0.72 or enemies_135 >= 2 or reload_remaining > 0.75):
				should_fear = True
			elif is_casemate:
				if (dist <= 95.0 and (reload_remaining > 1.1 or hp_frac < 0.58 or enemies_135 >= 2)) or (enemies_135 >= 2 and force_balance <= -0.55):
					should_fear = True
			elif class_tag == 'lightTank' and not profile.get('is_infantry_lt'):
				if (enemies_135 >= 2 and dist <= 155.0) or (target_cls in ('heavyTank', 'AT-SPG') and dist <= 110.0 and reload_remaining > 0.9) or (hp_frac < 0.65 and inc_age <= 8.0):
					should_fear = True
			else:
				if (enemies_135 >= 2 and force_balance <= -0.55 and (hp_frac < 0.68 or reload_remaining > 1.1 or inc_age <= 6.5)) or (hp_frac < 0.45 and (inc_age <= 8.0 or force_balance < 0.0 or reload_remaining > 1.2)):
					should_fear = True
			if not should_fear:
				return None
			fear_pt = agent.get('_fear_retreat_pos')
			fear_until = float(agent.get('_fear_retreat_until', 0.0) or 0.0)
			# If already at fear_pt and still taking hits (inc_age <= 2.6s), fear_pt is exposed — back up further along route!
			if fear_pt is not None and _distance_2d(position, fear_pt) <= 4.5 and inc_age <= 2.6:
				fear_pt = self._fallback_position(agent, position)
				agent['_fear_retreat_pos'] = fear_pt
				agent['_fear_retreat_until'] = now + 5.0
			if fear_pt is None or now >= fear_until:
				pk_cov = self._find_tactical_pocket(agent, position, c_pos, max_radius=28.0)
				if pk_cov is not None and _distance_2d(position, pk_cov['stand']) >= 2.5:
					fear_pt = pk_cov['stand']
				else:
					fear_pt = self._reload_cover_position(agent, position, c_pos) or self._fallback_position(agent, position)
				if fear_pt is None:
					return None
				grid = _real_cover_grid()
				if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
					fear_pt = grid.snap_to_safe_baked_point(fear_pt, 10) or fear_pt
				agent['_fear_retreat_pos'] = fear_pt
				agent['_fear_retreat_until'] = now + (6.0 if mortal_danger else 4.8)
			d_to_fear = _distance_2d(position, fear_pt)
			_use_rev_fear = bool(reverse_retreat and d_to_fear <= 34.0)
			face_pt = c_pos if (is_casemate and _use_rev_fear) else (self._angled_face_position(agent, position, c_pos, now=now) if (_use_rev_fear or dist <= 165.0) else fear_pt)
			return {
				'move_position': fear_pt if d_to_fear > 3.6 else tuple(position),
				'aim_position': c_pos,
				'face_position': face_pt,
				'fire_allowed': True,
				'throttle_override': (0.0 if d_to_fear <= 3.6 else (-0.88 if _use_rev_fear else None)),
				'combat_mode': 'reload_cover' if _use_rev_fear else 'withdraw',
			}
		except Exception:
			pass
		return None

	def _evaluate_encirclement(self, agent, position, contact, force_balance, now):
		"""v12.0 Coordinated Wolfpack Encirclement ('Окружение' / Pincer Crossfire):
		When bots have clear numerical & HP superiority (force_balance >= 0.85, >= 2 allies vs 1 isolated enemy
		within 120m, and bot HP >= 42% so nobody feeds unnecessarily), assign each attacker an opposite angular
		pincer sector (0 deg anchor, +115 deg left flank, -115 deg right flank, 175 deg rear) around the target!"""
		try:
			if contact is None or not contact.get('visible') or contact.get('position') is None:
				return None
			profile = agent.get('profile') or {}
			class_tag = profile.get('class_tag', 'mediumTank')
			if class_tag in ('SPG', 'AT-SPG'):
				return None
			skill_tier = 'hard'
			if skill_tier == 'easy':
				return None
			if _number(agent.get('health_fraction', 1.0), 1.0) < 0.42 or force_balance < 0.85:
				return None
			c_pos = contact['position']
			dist = _distance_2d(position, c_pos)
			if dist < 14.0 or dist > 120.0:
				return None
			if self._is_chord_impassable(position, c_pos) or self._is_mountain_blocked(position, c_pos):
				return None
			team = int(agent.get('team', 1) or 1)
			# Verify target is isolated enough to encircle safely without walking into crossfire
			enemies_near_tgt = 0
			for oc in self._known_contacts(team, now):
				if oc.get('visible') and oc.get('position') is not None and _distance_2d(c_pos, oc['position']) <= 115.0:
					enemies_near_tgt += 1
			if enemies_near_tgt > 2:
				return None
			pack_cands = []
			for oa in self.agents.values():
				if int(oa.get('team', 0) or 0) != team or _number(oa.get('health_fraction', 1.0), 1.0) <= 0.25 or oa.get('position') is None:
					continue
				if (oa.get('profile') or {}).get('class_tag') == 'SPG':
					continue
				_od = _distance_2d(oa['position'], c_pos)
				if oa.get('target_id') == contact['id'] and _od <= 85.0:
					pack_cands.append((_od, int(oa['id'])))
			if len(pack_cands) < max(2, enemies_near_tgt + 1):
				return None
			pack_cands.sort()
			pack_ids = [aid for _od, aid in pack_cands[:3]]
			my_id = int(agent.get('id', 0) or 0)
			if my_id not in pack_ids:
				return None
			slot = pack_ids.index(my_id)
			if slot == 0:
				return None  # Lead closest tank brawls directly while slots 1..2 pincer flanks
			_s_ids_enc = contact.get('shootable_by_ids')
			_has_lane_enc = bool(contact.get('visible') and (_s_ids_enc is None or my_id in _s_ids_enc))
			_own_b_enc = self.bases.get(team) or (0.0, 0.0)
			base_ang = math.atan2(float(_own_b_enc[0]) - float(c_pos[0]), float(_own_b_enc[1]) - float(c_pos[2]))
			sector_offsets = (0.0, 1.65, -1.65, 2.95)
			goal_ang = base_ang + sector_offsets[slot % len(sector_offsets)]
			standoff_r = 18.0 if class_tag == 'lightTank' else (22.0 if class_tag == 'mediumTank' else 25.0)
			enc_pt = (
				float(c_pos[0]) + math.sin(goal_ang) * standoff_r,
				float(position[1]),
				float(c_pos[2]) + math.cos(goal_ang) * standoff_r)
			grid = _real_cover_grid()
			if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
				safe_enc = grid.snap_to_safe_baked_point(enc_pt, 6)
				if (safe_enc is None or
						not self._is_3d_corridor_clear(position, safe_enc, 1.15) or
						not self._has_3d_sightline(safe_enc, c_pos, 1.45)):
					return None
				enc_pt = safe_enc
			if _distance_2d(position, enc_pt) <= 13.5 and not _has_lane_enc:
				return None
			_mid_enc = ((float(position[0]) + float(enc_pt[0])) * 0.5, float(position[1]), (float(position[2]) + float(enc_pt[2])) * 0.5)
			if now - float(agent.get('_last_enc_log', -999.0) or -999.0) >= 18.0:
				agent['_last_enc_log'] = now
				_offh_ai_log_note('WOLFPACK_ENCIRCLE bot=%s target=%s slot=%d/%d balance=%.2f' % (
					str(my_id), str(contact['id']), slot + 1, len(pack_ids), force_balance))
			return {
				'move_position': enc_pt,
				'aim_position': c_pos,
				'face_position': enc_pt if _distance_2d(position, enc_pt) > 12.0 else self._angled_face_position(agent, position, c_pos, now=now),
				'fire_allowed': True,
				'throttle_override': 1.0 if _distance_2d(position, enc_pt) > 14.0 else None,
				'combat_mode': 'wolfpack_encircle',
				'plan_waypoints': [_mid_enc, enc_pt],
				'tactic_reason': '3D Wolfpack Pincer #%d/%d (bal=+%.1f, 3D LOS clear)' % (slot + 1, len(pack_ids), force_balance),
				'action_desc': 'Охват с фланга -> (%.0f, %.0f), огонь по #%s' % (enc_pt[0], enc_pt[2], str(contact['id'])),
			}
		except Exception:
			return None

	def _evaluate_light_tank_tactics(self, agent, position, contact, now, hull_yaw=0.0, route_position=None):
		"""v15.0 3D Route-Aware Dynamic Orbit & Flanking Planner for Light & Medium Tanks:
		- Evaluates multi-step 3D trajectories (4-step CW vs CCW carousel arcs & route-blended approach spirals)
		  around the target using 3D building OBB raycasts, 3D sightlines, dead wreck avoidance, and route alignment.
		- Scores each 3D arc by clear 3D steps, 3D firing windows, enemy gun/turret rear exposure, route flow, and hull momentum.
		- Latches the chosen dynamic plan (`_lt_mt_orbit_plan`), steps smoothly through waypoints at 100% throttle,
		  and automatically reverses into a pendulum rear-arc if a building/cliff blocks a full 360-degree circle!"""
		profile = agent.get('profile') or {}
		class_tag = profile.get('class_tag')
		speed = _number(profile.get('speed', 12.0), 12.0)
		if class_tag not in ('lightTank', 'mediumTank') or speed < 10.5 or profile.get('is_infantry_lt'):
			return None

		if contact is None or not contact.get('visible') or position is None:
			return None

		c_pos = contact.get('position')
		if c_pos is None:
			return None

		if abs(float(c_pos[1]) - float(position[1])) > 7.5:
			return None

		# Forbid dynamic circling on narrow water bridges or lethal drop cliffs:
		route_id = str((agent.get('route') or {}).get('id', '') or '').lower()
		if any(sub in route_id for sub in ('lake_road', 'bridge', 'cliff_pass', 'gorge', 'choke')):
			return None
		if self._is_hazard_point(position, 2) or self._is_hazard_point(c_pos, 2):
			return None

		dx = float(position[0]) - float(c_pos[0])
		dz = float(position[2]) - float(c_pos[2])
		dist = math.sqrt(dx * dx + dz * dz)
		if dist < 4.0:
			return None

		bot_id = int(agent.get('id', 0) or 0)
		ux = dx / dist
		uz = dz / dist

		# Count visible enemies clustered near this target (within 90m)
		enemies_near_target = 0
		try:
			team_contacts = self.contacts.get(int(agent.get('team', 1) or 1), {})
			for _oc in team_contacts.values():
				if _oc.get('visible') and _oc.get('position') is not None and _number(_oc.get('health', 0.0)) > 0.0:
					_op = _oc['position']
					if (float(_op[0]) - float(c_pos[0])) ** 2 + (float(_op[2]) - float(c_pos[2])) ** 2 <= 90.0 * 90.0:
						enemies_near_target += 1
		except Exception:
			enemies_near_target = 1

		target_cls = str(contact.get('class_tag', ''))
		_is_spg_prey = (target_cls == 'SPG')
		_s_ids = contact.get('shootable_by_ids')
		has_direct_lane = bool((_s_ids is not None and bot_id in _s_ids) or (_s_ids is None and self._has_3d_sightline(position, c_pos, 1.45)))
		can_circle = (
			(_is_spg_prey and dist <= 165.0) or
			((has_direct_lane or dist <= 92.0) and
			 _number(agent.get('health_fraction', 1.0), 1.0) >= 0.20 and
			 enemies_near_target <= 3)
		)
		max_orbit_d = 165.0 if _is_spg_prey else (95.0 if class_tag == 'lightTank' else 84.0)
		if can_circle and 4.5 <= dist <= max_orbit_d and (not self._is_chord_impassable(position, c_pos) or (_is_spg_prey and dist <= 135.0)):
			_t_max_hp = _number(contact.get('max_health', 1200.0), 1200.0)
			_t_arm = _number(contact.get('armor', 80.0), 80.0)
			_bulk_pad = max(0.0, min(6.0, (_t_max_hp - 1100.0) / 280.0 + (_t_arm - 90.0) / 45.0))
			if target_cls in ('AT-SPG', 'SPG'):
				ideal_r = (14.5 if class_tag == 'lightTank' else 18.0) + _bulk_pad
			elif target_cls == 'heavyTank':
				ideal_r = (17.5 if class_tag == 'lightTank' else 21.5) + _bulk_pad
			else:
				_mod_r = 2.8 * math.sin(_number(now, 0.0) * 0.85 + float(bot_id % 11))
				ideal_r = (20.0 if class_tag == 'lightTank' else 24.0) + _bulk_pad + _mod_r

			grid = _real_cover_grid()
			cur_bearing = math.atan2(dx, dz)  # angle from target to our bot
			route_pos_eff = route_position or agent.get('_last_route_pos')
			route_id_str = str((agent.get('route') or {}).get('id', 'direct') or 'direct')

			# Enemy gun/turret direction for rear-turret orbit preference
			enemy_gun_yaw = contact.get('gun_yaw')
			tid = contact.get('id')
			if enemy_gun_yaw is None and tid in self.agents:
				_ag_e = self.agents.get(tid) or {}
				if _ag_e.get('hull_yaw') is not None:
					enemy_gun_yaw = float(_ag_e.get('hull_yaw', 0.0) or 0.0) + float(_ag_e.get('turret_yaw', 0.0) or 0.0)

			# Check existing latched dynamic orbit plan first
			plan = agent.get('_lt_mt_orbit_plan')
			need_replan = True
			if (isinstance(plan, dict) and plan.get('tid') == tid and
					float(now) < float(plan.get('until', 0.0) or 0.0) and
					_distance_2d(c_pos, plan.get('enemy_anchor', c_pos)) <= 14.0):
				wps = list(plan.get('waypoints') or ())
				s_idx = int(plan.get('step_idx', 0) or 0)
				while s_idx + 1 < len(wps) and _distance_2d(position, wps[s_idx]) <= 9.5:
					s_idx += 1
				plan['step_idx'] = s_idx
				if s_idx < len(wps):
					cur_wp = wps[s_idx]
					if _distance_2d(position, cur_wp) <= 7.5 and s_idx == len(wps) - 1:
						# Reached end of current 4-step arc: if full circle wasn't 4/4, flip direction for pendulum rear sweep!
						if int(plan.get('total_valid', 4)) < 3:
							agent['_orbit_plan_dir'] = -float(plan.get('dir', 1.0))
						need_replan = True
					elif self._is_3d_corridor_clear(position, cur_wp, 1.05):
						need_replan = False
					else:
						# Current step became blocked by a tank/wreck/wall -> flip and replan immediately!
						agent['_orbit_plan_dir'] = -float(plan.get('dir', 1.0))
						need_replan = True

			if need_replan:
				hull_fx, hull_fz = math.sin(float(hull_yaw or 0.0)), math.cos(float(hull_yaw or 0.0))
				rt_dx, rt_dz, rt_len = 0.0, 0.0, 0.0
				if route_pos_eff is not None:
					rt_dx = float(route_pos_eff[0]) - float(position[0])
					rt_dz = float(route_pos_eff[2]) - float(position[2])
					rt_len = math.hypot(rt_dx, rt_dz)
					if rt_len > 1.0:
						rt_dx /= rt_len
						rt_dz /= rt_len

				prev_dir = float(agent.get('_orbit_plan_dir', 0.0) or 0.0)
				best_cand = None
				best_score = -1e9

				for cand_dir in (1.0, -1.0):
					# Build 4-step 3D dynamic trajectory (spiral-in approach if dist > 38m, or tight carousel arc if <= 38m)
					cand_wps = []
					los_cnt = 0
					prev_pt = position
					if dist > 42.0:
						ang_steps = (0.52, 1.10, 1.75, 2.40)
						rad_steps = (max(ideal_r, dist * 0.72), max(ideal_r, dist * 0.50), ideal_r, ideal_r)
					else:
						ang_steps = (0.68, 1.38, 2.10, 2.82)
						rad_steps = (max(ideal_r, dist * 0.68) if dist > 28.0 else ideal_r, ideal_r, ideal_r, ideal_r)

					for step_i in range(4):
						ang_i = cur_bearing + cand_dir * ang_steps[step_i]
						r_i = rad_steps[step_i]
						wx = float(c_pos[0]) + math.sin(ang_i) * r_i
						wz = float(c_pos[2]) + math.cos(ang_i) * r_i
						# Blend 25% of route direction into the first approach step when > 45m out so flank respects map corridor!
						if step_i == 0 and dist > 45.0 and rt_len > 8.0:
							wx = wx * 0.78 + (float(position[0]) + rt_dx * 24.0) * 0.22
							wz = wz * 0.78 + (float(position[2]) + rt_dz * 24.0) * 0.22
						raw_pt = (wx, float(position[1]), wz)
						if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
							sn_pt = grid.snap_to_safe_baked_point(raw_pt, 6)
							if sn_pt is None:
								break
							raw_pt = sn_pt
						if not self._is_3d_corridor_clear(prev_pt, raw_pt, 1.10) or self._is_water_or_cliff_chord(prev_pt, raw_pt) or self._is_hazard_point(raw_pt, 2):
							break
						cand_wps.append(raw_pt)
						if self._has_3d_sightline(raw_pt, c_pos, 1.45):
							los_cnt += 1
						prev_pt = raw_pt

					# Circling requires at least 2 open steps (~90-140 deg flanking arc):
					if len(cand_wps) < 2:
						continue

					# Score this 3D dynamic orbit candidate
					wp0 = cand_wps[0]
					s0_dx = float(wp0[0]) - float(position[0])
					s0_dz = float(wp0[2]) - float(position[2])
					s0_l = max(0.5, math.hypot(s0_dx, s0_dz))
					s0_ux, s0_uz = s0_dx / s0_l, s0_dz / s0_l

					sc = len(cand_wps) * 32.0 + los_cnt * 16.0
					# 1) Enemy gun/turret avoidance: prefer orbiting toward the rear of the enemy's gun barrel!
					rear_deg = 0
					if enemy_gun_yaw is not None:
						wp0_bear = math.atan2(float(wp0[0]) - float(c_pos[0]), float(wp0[2]) - float(c_pos[2]))
						gun_off = abs(_angle_delta(wp0_bear, float(enemy_gun_yaw)))
						rear_deg = int(round(math.degrees(gun_off)))
						sc += gun_off * 24.0
					# 2) Route alignment bonus ('с учётом маршрута'):
					rt_align = (s0_ux * rt_dx + s0_uz * rt_dz) if rt_len > 1.0 else 0.0
					sc += rt_align * 24.0
					# 3) Hull momentum bonus (smooth entry without losing speed):
					mom_align = (s0_ux * hull_fx + s0_uz * hull_fz)
					sc += mom_align * 18.0
					# 4) Hysteresis lock on active direction:
					if prev_dir == cand_dir and agent.get('_orbit_plan_tid') == tid:
						sc += 20.0

					if sc > best_score:
						best_score = sc
						dir_label = 'CW' if cand_dir > 0 else 'CCW'
						mode_label = '3D-Spiral->Orbit' if dist > 42.0 else ('3D-Orbit' if len(cand_wps) >= 3 else '3D-Pendulum-Flank')
						best_cand = {
							'tid': tid,
							'dir': cand_dir,
							'waypoints': cand_wps,
							'step_idx': 0,
							'total_valid': len(cand_wps),
							'los_cnt': los_cnt,
							'enemy_anchor': tuple(c_pos),
							'until': float(now) + 2.4,
							'reason': '%s %s (r=%.0fm, %d/4 3D-шагов чисто, %d/4 3D-прострел, уход от ствола %d°, маршрут=%s)' % (
								mode_label, dir_label, ideal_r, len(cand_wps), los_cnt, rear_deg, route_id_str),
							'desc': 'Динамическое кручение %s [шаг 1/%d -> (%.0f, %.0f)], газ 100%%, огонь с ходу по #%s' % (
								dir_label, len(cand_wps), wp0[0], wp0[2], str(tid)),
						}

				if best_cand is None:
					return None
				plan = best_cand
				agent['_lt_mt_orbit_plan'] = plan
				agent['_orbit_plan_dir'] = plan['dir']
				agent['_orbit_plan_tid'] = tid

			wps = plan['waypoints']
			s_idx = min(int(plan.get('step_idx', 0) or 0), len(wps) - 1)
			move_pt = wps[s_idx]
			rem_wps = list(wps[s_idx:])
			dir_label = 'CW' if float(plan.get('dir', 1.0)) > 0 else 'CCW'
			act_desc = 'Динамическое кручение %s [шаг %d/%d -> (%.0f, %.0f)], газ 100%%, огонь с ходу по #%s' % (
				dir_label, s_idx + 1, len(wps), move_pt[0], move_pt[2], str(tid))
			return {
				'move_position': move_pt,
				'aim_position': c_pos,
				'face_position': move_pt,
				'throttle_override': 1.0,
				'fire_allowed': True,
				'combat_mode': 'circle_of_death',
				'plan_waypoints': rem_wps,
				'tactic_reason': plan.get('reason', '3D Dynamic Orbit'),
				'action_desc': act_desc,
			}

		# 2. Active Scouting Sweep ONLY for dedicated `active_ridge_scout` LTs (never combat LTs or MTs)
		if (class_tag == 'lightTank' and
				agent.get('tactical_subrole') == 'active_ridge_scout' and
				not agent.get('is_combat_lt') and
				enemies_near_target >= 2 and
				115.0 < dist <= 245.0):
			sweep_sign = 1.0 if (int(now / 8.5) + bot_id) % 2 == 0 else -1.0
			lat_step = 12.0 * sweep_sign
			rad_push = -8.0 if dist > 165.0 else (8.0 if dist < 130.0 else 0.0)
			scout_x = position[0] - ux * rad_push + (-uz * lat_step)
			scout_z = position[2] - uz * rad_push + (ux * lat_step)
			scout_pt = (scout_x, position[1], scout_z)
			grid = _real_cover_grid()
			if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
				safe_sp = grid.snap_to_safe_baked_point(scout_pt, 5)
				if safe_sp is None:
					return None
				scout_pt = safe_sp
			return {
				'move_position': scout_pt,
				'aim_position': c_pos,
				'face_position': scout_pt,
				'throttle_override': 1.0,
				'fire_allowed': True,
				'combat_mode': 'flank',
			}

		return None

	def _apply_light_tank_weaving(self, agent, position, move_position, now):
		"""Apply subtle evasive weaving ONLY when a fast scout LT is actively engaged or under fire."""
		profile = agent.get('profile') or {}
		class_tag = profile.get('class_tag')
		if class_tag != 'lightTank' or profile.get('is_infantry_lt') or agent.get('is_combat_lt'):
			return move_position
		_r_wv = (agent.get('route') or {}).get('id')
		if _r_wv in ('lake_road', 'bridge', 'east_bridge', 'center_bridge') or self._is_hazard_point(position):
			return move_position
		# Never weave during peaceful route travel: only weave when actively engaged or recently shot at!
		inc_age = float(now) - float(agent.get('incoming_at', -999.0) or -999.0)
		if agent.get('target_id') is None and inc_age > 6.0:
			return move_position

		skill_tier = 'hard'
		if move_position is None or position is None:
			return move_position

		mdx = move_position[0] - position[0]
		mdz = move_position[2] - position[2]
		mlen = math.sqrt(mdx * mdx + mdz * mdz)
		if mlen < 32.0 or self._is_chord_impassable(position, move_position):
			return move_position

		bot_id = int(agent.get('id', 0) or 0)
		norm_x = -mdz / mlen
		norm_z = mdx / mlen

		if skill_tier == 'hard':
			wave = math.sin(float(now) * 2.8 + float(bot_id) * 2.1) * 2.6
			return (move_position[0] + norm_x * wave, position[1], move_position[2] + norm_z * wave)
		elif skill_tier == 'medium':
			cycle = (float(now) + float(bot_id)) % 6.0
			if cycle < 3.0:
				wave = math.sin(float(now) * 2.2 + float(bot_id)) * 2.0
				return (move_position[0] + norm_x * wave, position[1], move_position[2] + norm_z * wave)
		return move_position

	def _fire_discipline_position(self, agent, position, target_position):
		"""After firing:
		- AT-SPG and SPG hold their stationary bush/sniping posture (return None).
		- Heavy tanks & brawlers reverse straight back along the line of fire (sidescrape rock back)
		  so they NEVER turn their hull sideways and expose their side/rear armor!
		- Mobile lights/mediums at medium range angle back into cover.
		"""
		profile = agent.get('profile') or {}
		class_tag = profile.get('class_tag', 'mediumTank')
		if class_tag in ('SPG', 'AT-SPG'):
			return None
		tier_level = int(profile.get('level', 1) or 1)
		if tier_level < 4:
			return None
		dx = target_position[0] - position[0]
		dz = target_position[2] - position[2]
		dist = math.sqrt(dx * dx + dz * dz)
		if dist < FIRE_DISCIPLINE_MIN_RANGE or dist < 0.1:
			return None
		seed = int(agent.get('seed', 0) or 0)
		side = 1.0 if ((seed & 1) == 0) else -1.0
		norm_x = -dz / dist
		norm_z = dx / dist
		if class_tag == 'heavyTank' or profile.get('dominant_role') == 'brawler':
			# Reverse straight back at a ~18 deg reverse-diamond angle so LocalDriver uses tactical_reverse!
			new_x = position[0] - (dx / dist) * 8.5 + norm_x * 2.2 * side
			new_z = position[2] - (dz / dist) * 8.5 + norm_z * 2.2 * side
			return (new_x, position[1], new_z)
		new_x = position[0] - (dx / dist) * 9.0 + norm_x * 3.8 * side
		new_z = position[2] - (dz / dist) * 9.0 + norm_z * 3.8 * side
		return (new_x, position[1], new_z)

	def _consider_route_switch(self, agent, position, now):
		"""v13.0 Dynamic Tactical Regrouping ('Перегруппировка'):
		1) Quota & Dead-Flank Rebalancing: If a bot's current corridor exceeds max_tanks OR has >=3 allies
		   with 0 spotted enemies while another corridor is understaffed (< min_tanks) or under heavy enemy attack,
		   peel off and regroup to the threatened/understaffed flank!
		2) Outnumbered Retreat/Regroup: If a bot's corridor is heavily outnumbered (balance <= -2) and no enemy
		   is within 75m, switch to reinforce the nearest allied flank instead of feeding 1-by-1!"""
		profile = agent.get('profile') or {}
		class_tag = str(profile.get('class_tag', 'mediumTank'))
		if class_tag == 'SPG':
			return False
		if bool(agent.get('route_completed')) or int(agent.get('waypoint_index', 0) or 0) > 2:
			return False
		last_switch = _number(agent.get('route_switch_at'), -1e18)
		if now - last_switch < 35.0 or int(agent.get('route_switches_count', 0) or 0) >= 3:
			return False
		current_route = agent.get('route')
		if current_route is None:
			return False
		team = int(agent['team'])
		current_id = current_route.get('id')
		if not current_id or current_id == 'rear_guard':
			return False
		contacts = self._known_contacts(team, now)
		min_enemy_dist = 999.0
		for contact in contacts:
			if contact.get('visible'):
				d = _distance_2d(position, contact['position'])
				if d < min_enemy_dist:
					min_enemy_dist = d
		if min_enemy_dist < 115.0:
			return False
		route_waypoints = current_route.get('waypoints', ())
		if not route_waypoints:
			return False
		wp_index = min(int(agent.get('waypoint_index', 0)), len(route_waypoints) - 1)
		current_wp = route_waypoints[wp_index]
		route_pos = (float(current_wp[0]), position[1], float(current_wp[1]))
		corridor_radius = 195.0
		nearby_enemies = sum(1 for c in contacts if _distance_2d(c['position'], route_pos) <= corridor_radius)
		living_team_allies = [
			a for a in self.agents.values()
			if int(a.get('team', 0) or 0) == team and
			_number(a.get('health_fraction', 1.0), 1.0) > 0.0 and
			a.get('position') is not None and
			(a.get('profile') or {}).get('class_tag') != 'SPG'
		]
		on_cur_route = [a for a in living_team_allies if (a.get('route') or {}).get('id') == current_id]
		cur_min_q = max(1, int(current_route.get('min_tanks', 2) or 2))
		cur_max_q = max(cur_min_q, int(current_route.get('max_tanks', current_route.get('capacity', 4)) or 4))
		nearby_allies = max(len(on_cur_route) - 1, sum(
			1 for other in living_team_allies
			if other.get('id') != agent['id'] and _distance_2d(other['position'], route_pos) <= corridor_radius))
		balance = nearby_allies - nearby_enemies
		over_quota = (len(on_cur_route) > cur_max_q)
		idle_excess = (bool(agent.get('route_completed')) and nearby_enemies == 0 and len(on_cur_route) > cur_min_q and contacts)
		outnumbered = (nearby_enemies >= nearby_allies + 2 and len(on_cur_route) <= 2)
		if not (over_quota or idle_excess or outnumbered):
			return False
		routes = self._routes_for(team)
		if not routes:
			return False
		_cls_usage_map = self.__dict__.setdefault('class_route_usage', {})
		best_route = None
		best_score = -1e18
		for candidate in routes:
			cand_id = candidate.get('id')
			if not cand_id or cand_id == current_id or cand_id == 'rear_guard':
				continue
			allw = candidate.get('allowed_classes')
			if isinstance(allw, (list, tuple)) and allw and class_tag not in allw:
				continue
			cand_waypoints = candidate.get('waypoints', ())
			if not cand_waypoints:
				continue
			cand_min_q = max(1, int(candidate.get('min_tanks', 2) or 2))
			cand_max_q = max(cand_min_q, int(candidate.get('max_tanks', candidate.get('capacity', 4)) or 4))
			cand_allies = sum(1 for a in living_team_allies if (a.get('route') or {}).get('id') == cand_id)
			if cand_allies >= cand_max_q:
				continue
			mid_wp = cand_waypoints[len(cand_waypoints) // 2]
			mid_pos = (float(mid_wp[0]), position[1], float(mid_wp[1]))
			reachable_wps = [w for w in cand_waypoints if not self._is_mountain_blocked(position, (float(w[0]), position[1], float(w[1]))) and not self._is_chord_impassable(position, (float(w[0]), position[1], float(w[1])))]
			if not reachable_wps and not bool(agent.get('route_completed')):
				continue
			nearest_cand_d = min(_distance_2d(position, (float(w[0]), position[1], float(w[1]))) for w in (reachable_wps or cand_waypoints))
			if nearest_cand_d > 165.0 and not bool(agent.get('route_completed')):
				continue
			cand_enemies = sum(1 for c in contacts if _distance_2d(c['position'], mid_pos) <= corridor_radius)
			score = 0.0
			if cand_allies < cand_min_q:
				score += (cand_min_q - cand_allies) * 55.0
			if cand_enemies > 0 and cand_allies <= cand_enemies + 1:
				score += cand_enemies * 28.0
			pref = candidate.get('preferred_classes') or ()
			if class_tag in pref:
				score += 35.0
			score -= _distance_2d(position, mid_pos) * 0.06
			if score > best_score:
				best_score = score
				best_route = candidate
		if best_route is None or best_score < 12.0:
			return False
		old_usage_key = (team, current_id)
		self.route_usage[old_usage_key] = max(0, int(self.route_usage.get(old_usage_key, 1)) - 1)
		_cls_usage_map[(team, current_id, class_tag)] = max(0, int(_cls_usage_map.get((team, current_id, class_tag), 1)) - 1)
		oriented = route_toward_enemy(best_route, team, self.bases)
		agent['route'] = oriented
		# Pick the nearest forward waypoint on the new route so the tank cuts across cleanly
		new_wps = oriented.get('waypoints') or ()
		best_wp_idx = 0
		if new_wps:
			reachable_nwps = [i for i in range(len(new_wps)) if not self._is_mountain_blocked(position, (float(new_wps[i][0]), float(position[1]), float(new_wps[i][1]))) and not self._is_chord_impassable(position, (float(new_wps[i][0]), float(position[1]), float(new_wps[i][1])))]
			if reachable_nwps:
				best_wp_idx = min(reachable_nwps, key=lambda i: math.hypot(float(new_wps[i][0]) - float(position[0]), float(new_wps[i][1]) - float(position[2])))
				best_wp_idx = min(len(new_wps) - 1, best_wp_idx + 1)
			else:
				best_wp_idx = min(range(len(new_wps)), key=lambda i: math.hypot(float(new_wps[i][0]) - float(position[0]), float(new_wps[i][1]) - float(position[2])))
				best_wp_idx = min(len(new_wps) - 1, best_wp_idx + 1)
		agent['waypoint_index'] = best_wp_idx
		agent['route_completed'] = False
		agent['route_started'] = True
		agent['route_switch_at'] = now
		agent['route_switches_count'] = int(agent.get('route_switches_count', 0) or 0) + 1
		new_id = best_route.get('id')
		new_usage_key = (team, new_id)
		self.route_usage[new_usage_key] = int(self.route_usage.get(new_usage_key, 0)) + 1
		_cls_usage_map[(team, new_id, class_tag)] = int(_cls_usage_map.get((team, new_id, class_tag), 0)) + 1
		_offh_ai_log_note(
			'ROUTE_SWITCH bot=%s team=%s cls=%s from=%s to=%s balance=%.1f score=%.1f' % (
				agent['id'], team, class_tag, current_id, new_id, balance, best_score))
		return True

	def _active_fight_centre(self, team, position, now):
		"""Find the centroid of the nearest active fight (or any remaining known enemy in late game) for convergence."""
		contacts = self._known_contacts(team, now)
		if not contacts:
			return None
		fight_contacts = []
		for contact in contacts:
			if _distance_2d(position, contact['position']) <= CONVERGENCE_SCAN_RADIUS:
				if not self._is_chord_impassable(position, contact['position']):
					fight_contacts.append(contact)
		if fight_contacts:
			centroid_x = sum(c['position'][0] for c in fight_contacts) / len(fight_contacts)
			centroid_z = sum(c['position'][2] for c in fight_contacts) / len(fight_contacts)
			centroid = (centroid_x, position[1], centroid_z)
			engaged_allies = 0
			for other in self.agents.values():
				if (other.get('team') == team and
						_number(other.get('health_fraction', 1.0), 1.0) > 0.0 and
						other.get('target_id') is not None and
						other.get('position') is not None and
						_distance_2d(other['position'], centroid) <= CONVERGENCE_SCAN_RADIUS):
					engaged_allies += 1
			if engaged_allies + len(fight_contacts) >= CONVERGENCE_MIN_FIGHT_SIZE or (float(now) - float(self.__dict__.setdefault('_battle_start_at', float(now)))) > 160.0:
				return centroid
		# Fallback when route is completed: hunt the nearest known enemy anywhere on the map!
		nearest_c = min(contacts, key=lambda c: _distance_2d(position, c['position']))
		return (float(nearest_c['position'][0]), position[1], float(nearest_c['position'][2]))

	def _kd_hunt_objective(self, agent, position, now):
		"""Find the best K/D hunting objective across the map when a bot finishes its route
		or bypasses base capture to hunt surviving enemies."""
		my_t = int(agent.get('team', 1) or 1)
		fc = self._active_fight_centre(my_t, position, now)
		if fc is not None:
			return fc
		# Check any historical/last-seen enemy contact for this team
		all_team_c = [c for c in (self.contacts.get(my_t) or {}).values() if c.get('position') is not None]
		if all_team_c:
			best_c = min(all_team_c, key=lambda c: _distance_2d(position, c['position']))
			return (float(best_c['position'][0]), float(position[1]), float(best_c['position'][2]))
		# Otherwise sweep toward the sector of the nearest surviving enemy vehicle
		living_enemies = [
			ag for ag in self.agents.values()
			if ag.get('team') not in (None, my_t) and
			_number(ag.get('health_fraction', 0.0), 0.0) > 0.0 and
			_number(ag.get('hp_abs', 1.0), 1.0) > 0.0 and
			ag.get('position') is not None
		]
		if living_enemies:
			nearest_e = min(living_enemies, key=lambda ag: _distance_2d(position, ag['position']))
			e_pos = nearest_e['position']
			return (float(e_pos[0]), float(position[1]), float(e_pos[2]))
		return None

	def _should_bot_capture_base(self, agent, position, now):
		"""Prioritize K/D and damage hunting over base capture while enemies are still alive:
		- When >= 4 enemies are alive: at most 1 designated capper may cap; everyone else sweeps for kills.
		- When 2-3 enemies are alive: at most 1-2 cappers may cap; others hunt down survivors.
		- Never allow > 2 tanks to pile into the capture circle simultaneously."""
		profile = agent.get('profile') or {}
		if profile.get('class_tag') == 'SPG':
			return False
		my_t = int(agent.get('team', 1) or 1)
		my_id = int(agent.get('id', 0) or 0)
		# In Assault mode, the defending team has NO enemy base to capture!
		if self.assault_attacker_team in (1, 2) and my_t != int(self.assault_attacker_team):
			return False
		# Assigned base defenders must defend their own base, never cap!
		if my_id in (getattr(self, '_assigned_base_defenders', {}).get(my_t) or ()):
			return False
		seed = int(agent.get('seed', 0) or 0)
		eb = self.bases.get(2 if my_t == 1 else 1)
		if eb is None:
			return False
		if bool(agent.get('_is_capturing_base')) and math.hypot(float(position[0]) - float(eb[0]), float(position[2]) - float(eb[1])) <= 34.0:
			return True
		alive_enemies = 0
		allies_near_cap = []
		for ag in self.agents.values():
			if _number(ag.get('health_fraction', 0.0), 0.0) <= 0.0 or _number(ag.get('hp_abs', 1.0), 1.0) <= 0.0:
				continue
			if ag.get('team') != my_t:
				alive_enemies += 1
			else:
				apos = ag.get('position')
				if apos is not None:
					d_eb = math.hypot(float(apos[0]) - float(eb[0]), float(apos[2]) - float(eb[1]))
					if d_eb <= 65.0:
						allies_near_cap.append((d_eb, int(ag['id'])))
		allies_near_cap.sort()
		capper_ids = [aid for _, aid in allies_near_cap]
		max_cappers = 2 if alive_enemies >= 5 else 3
		if my_id in capper_ids:
			return capper_ids.index(my_id) < max_cappers
		return len(capper_ids) < max_cappers

	def _convergence_position(self, agent, position, fight_centre):
		"""Spread converging bots around the fight at their desired range.

		Instead of all converging bots stacking on the same point, each one
		takes a position on a ring around the fight centre at its class's
		desired engagement range, spaced by an id-derived angle.
		"""
		bot_id = int(agent.get('id', 0) or 0)
		profile = agent.get('profile') or {}
		desired_range = max(80.0, _number(profile.get('desired_range', 180.0), 180.0))
		# Approach direction: from our current position toward the fight
		dx = fight_centre[0] - position[0]
		dz = fight_centre[2] - position[2]
		dist = math.sqrt(dx * dx + dz * dz)
		if dist < 0.1:
			dx, dz, dist = 1.0, 0.0, 1.0
		# Tier scaling: low tiers (I-III) converge directly ("толпа на звуки боя"),
		# while mid/high tiers (IV+) execute tactical ring perimeter dispersion.
		tier_level = int(profile.get('level', 1) or 1)
		if tier_level < 4:
			approach_dist = max(35.0, dist - 30.0)
			return (position[0] + (dx / dist) * approach_dist,
			        position[1],
			        position[2] + (dz / dist) * approach_dist)
		# Target ring at desired_range from fight centre, with angular spread
		approach_angle = math.atan2(dx, dz)
		slot_angle = ((bot_id % 7) - 3) * 0.32  # +/- ~55 degrees spread
		ring_angle = approach_angle + slot_angle
		ring_dist = min(desired_range * 0.85, max(dist - 20.0, 40.0))
		target_x = fight_centre[0] - math.sin(ring_angle) * ring_dist
		target_z = fight_centre[2] - math.cos(ring_angle) * ring_dist
		# Add some personal spread so bots don't stack on the same arc point
		spread_x = math.cos(bot_id * 1.618) * CONVERGENCE_SPREAD * 0.6
		spread_z = math.sin(bot_id * 1.618) * CONVERGENCE_SPREAD * 0.6
		conv_pt = (target_x + spread_x, position[1], target_z + spread_z)
		grid = _real_cover_grid()
		if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
			conv_pt = grid.snap_to_safe_baked_point(conv_pt, 8) or conv_pt
		return conv_pt

	def _crossfire_offset(self, agent, position, target_position):
		"""Offset position to create crossing angles with allies on same target.

		Returns a new move_position that maximises angular separation from
		the nearest ally also aiming at the same target, or None when the
		current position already has enough spread.
		"""
		target_id = agent.get('target_id')
		if target_id is None:
			return None
		# Tier scaling: crossfire coordination only applies to tier IV+ crews.
		# Beginners in sandbox do not coordinate angular crossfire.
		profile = agent.get('profile') or {}
		if profile.get('class_tag') in ('AT-SPG', 'SPG', 'heavyTank'):
			return None
		tier_level = int(profile.get('level', 1) or 1)
		if tier_level < 4:
			return None
		team = agent['team']
		dx = target_position[0] - position[0]
		dz = target_position[2] - position[2]
		dist = math.sqrt(dx * dx + dz * dz)
		if dist < 5.0:
			return None
		our_bearing = math.atan2(dx, dz)
		# Find the closest angular ally aimed at the same target
		min_angle_diff = math.pi
		for other in self.agents.values():
			if (other.get('id') == agent['id'] or
					other.get('team') != team or
					other.get('target_id') != target_id or
					_number(other.get('health_fraction', 1.0), 1.0) <= 0.0 or
					other.get('position') is None):
				continue
			adx = target_position[0] - other['position'][0]
			adz = target_position[2] - other['position'][2]
			ally_bearing = math.atan2(adx, adz)
			diff = abs(_angle_delta(our_bearing, ally_bearing))
			if diff < min_angle_diff:
				min_angle_diff = diff
		if min_angle_diff >= CROSSFIRE_MIN_ANGLE_RAD:
			return None  # Already spread enough
		# Need to offset - slide along an arc around the target
		side = 1.0 if (int(agent.get('id', 0)) & 1) else -1.0
		offset_angle = our_bearing + side * CROSSFIRE_MIN_ANGLE_RAD
		use_range = min(_number(agent['profile'].get('desired_range', 200.0), 200.0), dist)
		return (target_position[0] - math.sin(offset_angle) * use_range,
		        position[1],
		        target_position[2] - math.cos(offset_angle) * use_range)

	def order_for(self, bot_id, position, hull_yaw, health, max_health, now, reload_time=4.0, shoot_timer=999.0):
		self._last_order_now = float(now or 0.0)
		agent = self.agents[int(bot_id)]
		self._update_base_defenders(int(agent.get('team', 1) or 1), self._last_order_now)
		agent['position'] = tuple(position)
		agent['health_fraction'] = (
			_number(health, 1.0) / max(_number(max_health, 1.0), 1.0))
		agent['hp_abs'] = _number(health, 0.0)
		if self._callouts_on() and now - float(getattr(self, '_co_warn_at', -1e9)) >= 1.5:
			self._co_warn_at = now
			try:
				self._callout_warnings(now)
			except Exception as _co_err:
				_offh_ai_log_note('CALLOUT_ERR %r' % (_co_err,))
		contact = self._choose_contact(agent, position, hull_yaw, now)
		route_position = self._route_position(agent, position, now, hull_yaw)
		agent['_last_route_pos'] = route_position
		profile = agent['profile']
		personality = agent['personality']
		order = {
			'target_id': None,
			'aim_position': route_position,
			'face_position': route_position,
			'move_position': route_position,
			'fire_allowed': False,
			'combat_mode': 'route',
			'throttle_override': None,
			'desired_range': profile['desired_range'],
			'fire_range': profile['fire_range'],
			'route_id': agent['route'].get('id') if agent.get('route') else 'direct',
			'route_index': int(agent.get('waypoint_index', 0)),
			'route_anchor': self._route_anchor(agent, position),
			'personality': personality,
			'profile': profile,
			'shell_index': int(agent.get('last_shell_index', 0) or 0),
			'force_balance': 0.0,
			'max_speed_mps': agent.get('route_speed_limit_mps'),
		}
		if profile.get('class_tag') == 'SPG':
			home = agent.get('artillery_home')
			if home is None:
				home = self._assign_spg_home(agent, position)
				agent['artillery_home'] = home
				agent['last_arty_reloc_at'] = now
				_offh_ai_log_note('SPG_DEPLOY bot=%s team=%s spawn=(%.1f,%.1f) bush_home=(%.1f,%.1f)' % (
					str(agent['id']), str(agent.get('team')), float(position[0]), float(position[2]), float(home[0]), float(home[2])))
			# v7.4 Camouflage-Net Bush Hold & Front-Obstacle Awareness:
			# Do NOT relocate every 2 shots! Stay stationary in the bush so Camouflage Net (+25% camo) stays active!
			# Only relocate rarely: if damaged/spotted recently (< 5.0s) and cooldown >= 45s, or if immediate front obstacle blocks firing!
			_spg_rld_rem = max(0.0, _number(reload_time, 18.0) - _number(shoot_timer, 999.0))
			_last_reloc_at = float(agent.get('last_arty_reloc_at', -999.0) or -999.0)
			_recent_dmg = (now - float(agent.get('last_damaged_at', -999.0) or -999.0)) < 5.0
			if _recent_dmg and (now - _last_reloc_at) >= 45.0:
				agent['last_arty_reloc_at'] = now
				_new_home = self._spg_reposition_point(agent, position, contact['position'] if contact else None)
				agent['artillery_home'] = _new_home
				home = _new_home
			elif contact is not None and _distance_2d(position, home) <= 12.0 and (now - _last_reloc_at) >= 18.0:
				# Check if an obstacle/building/steep hill sits right in front of the SPG (within 28m toward target)
				_tdx_s = float(contact['position'][0]) - float(position[0])
				_tdz_s = float(contact['position'][2]) - float(position[2])
				_tdist_s = max(1.0, math.sqrt(_tdx_s * _tdx_s + _tdz_s * _tdz_s))
				_front_probe = (
					float(position[0]) + (_tdx_s / _tdist_s) * min(28.0, _tdist_s * 0.4),
					float(position[1]),
					float(position[2]) + (_tdz_s / _tdist_s) * min(28.0, _tdist_s * 0.4))
				_arc_cur0 = (agent.get('arty_arc_map') or {}).get(int(contact['id']))
				_arc_blk0 = bool(_arc_cur0 is not None and not _arc_cur0.get('clear', True))
				if _arc_blk0 and self._is_chord_impassable(position, _front_probe):
					# Front obstacle right in front of the gun! Do NOT oscillate 14m left and right against the same wall!
					# Reposition to an open firing avenue (railways, open fields, clear corridor) with verified clearance!
					agent['last_arty_reloc_at'] = now
					_new_home = self._spg_reposition_point(agent, position, contact['position'] if contact else None)
					agent['artillery_home'] = _new_home
					home = _new_home
			if _distance_2d(position, home) > 9.5:
				order['move_position'] = home
				order['route_anchor'] = home
				order['face_position'] = home
				order['aim_position'] = home
				order['throttle_override'] = None
				order['combat_mode'] = 'artillery_deploy'
			else:
				order['move_position'] = tuple(position)
				order['route_anchor'] = tuple(home)
				# v7.6: Pre-aim hull & gun toward map center / enemy half while waiting for targets!
				order['face_position'] = (0.0, float(position[1]), 0.0)
				order['aim_position'] = (0.0, float(position[1]), 0.0)
				order['throttle_override'] = 0.0
				order['combat_mode'] = 'artillery_hold'
			# If SPG took damage or an enemy is rushing close:
			_c_dist_spg = _distance_2d(position, contact['position']) if (contact is not None and contact.get('position')) else 999.0
			_enemy_approaching = bool(contact is not None and contact.get('visible') and _c_dist_spg <= 220.0)
			if _enemy_approaching:
				# Stand ground and fight: shotgun defense (прямая наводка / шотган) against incoming enemy!
				# Never turn paper rear and flee without firing when enemy is approaching!
				if _spg_rld_rem <= 3.2 or _c_dist_spg <= 125.0:
					order['move_position'] = tuple(position)
					order['face_position'] = contact['position']
					order['aim_position'] = contact['position']
					order['fire_allowed'] = True
					order['throttle_override'] = 0.0
					order['combat_mode'] = 'artillery_shotgun_defense'
					agent['last_order'] = order
					return order
			_spg_hit_until = float(agent.get('hit_reaction_until', 0.0) or 0.0)
			_spg_hit_pos = agent.get('hit_reaction_pos')
			if now < _spg_hit_until and _spg_hit_pos is not None and _distance_2d(position, _spg_hit_pos) > 5.0:
				order['move_position'] = _spg_hit_pos
				order['throttle_override'] = None
				order['fire_allowed'] = False
				order['combat_mode'] = 'artillery_reposition'
				agent['last_order'] = order
				return order
			_c_age = max(0.0, _number(now) - _number(contact.get('last_seen'), -999.0)) if contact is not None else 999.0
			_is_tracer_shot = bool(
				contact is not None and (
					(agent.get('tracer_target_id') == contact['id'] and now < float(agent.get('tracer_blind_until', 0.0) or 0.0)) or
					(contact.get('tracer_spotted_at') and (now - float(contact.get('tracer_spotted_at', -999.0))) <= 12.0)))
			if contact is not None and (contact.get('visible') or _c_age <= 6.5 or _is_tracer_shot):
				order['target_id'] = contact['id']
				_aim_pos_spg = tuple(contact['position'])
				_chosen_spg_shell = 0
				# v6.8 Smart Artillery:
				# 1) Check if enemy tanks are clustered ("орава", 2+ enemies within 15m):
				#    aim HE (shell 0) right at the cluster centroid to splash/kill 2-3 tanks at once!
				_cluster_pts = [_aim_pos_spg]
				try:
					_known_c = getattr(self, '_known_contacts_cache', {}).get(int(agent.get('team') or 0), [])
					for _oc in _known_c:
						if _oc.get('id') != contact['id'] and _oc.get('position') is not None:
							_oc_age = max(0.0, _number(now) - _number(_oc.get('last_seen'), -999.0))
							if (_oc.get('visible') or _oc_age <= 5.0) and _distance_2d(_aim_pos_spg, _oc['position']) <= 15.0:
								_cluster_pts.append(tuple(_oc['position']))
				except Exception:
					pass
				if len(_cluster_pts) >= 2:
					_cx_s = sum(p[0] for p in _cluster_pts) / float(len(_cluster_pts))
					_cy_s = sum(p[1] for p in _cluster_pts) / float(len(_cluster_pts))
					_cz_s = sum(p[2] for p in _cluster_pts) / float(len(_cluster_pts))
					_aim_pos_spg = (_cx_s, _cy_s, _cz_s)
					_chosen_spg_shell = 0
				else:
					# 2) Single isolated target: if heavily armored (>= 130mm), healthy (>= 550 HP),
					#    slow/stationary (<= 4.5 m/s) at medium/close range (<= 580m), check if SPG has
					#    an AP/HEAT shell that can punch directly through!
					_t_dist_spg = _distance_2d(position, _aim_pos_spg)
					_t_arm_spg = _number(contact.get('armor', 0.0), 0.0)
					_t_hp_spg = _number(contact.get('health', 0.0), 0.0)
					_t_spd_spg = _number(contact.get('speed', 0.0), 0.0)
					if _t_dist_spg <= 580.0 and _t_arm_spg >= 130.0 and _t_hp_spg >= 550.0 and _t_spd_spg <= 4.5:
						for _sh_s in (agent['profile'].get('shells') or ()):
							_sk_s = str(_sh_s.get('kind', '')).lower()
							_sp_s = _number(_sh_s.get('penetration', 0.0), 0.0)
							if ('explosive' not in _sk_s or 'hollow' in _sk_s) and _sp_s >= _t_arm_spg * 1.02:
								# [FIX v12.0] ~16% tactical chance for SPG to load AP/HEAT against slow, heavily armored targets
								if ((int(agent.get('id', 0)) * 31 + int(contact.get('id', 0)) * 17 + int(now * 0.05)) % 100) < 16:
									_chosen_spg_shell = int(_sh_s.get('index', 0))
								break
				order['aim_position'] = _aim_pos_spg
				order['face_position'] = _aim_pos_spg
				_spg_fire_rng = max(450.0, float(profile.get('fire_range', 1200.0) or 1200.0))
				_dist_to_spg_tgt = _distance_2d(position, _aim_pos_spg)
				if _dist_to_spg_tgt > _spg_fire_rng * 0.93:
					# Target is beyond gun range! Advance to a safe firing position within range!
					if _distance_2d(home, _aim_pos_spg) > _spg_fire_rng * 0.88 or (now - float(agent.get('last_arty_reloc_at', -999.0) or -999.0)) >= 8.0:
						agent['last_arty_reloc_at'] = now
						home = self._spg_reposition_point(agent, position, _aim_pos_spg)
						agent['artillery_home'] = home
					order['move_position'] = home
					order['face_position'] = home
					order['throttle_override'] = None
					order['fire_allowed'] = False
					order['combat_mode'] = 'artillery_deploy'
				else:
					# If SPG is still deploying out of a spawn valley/slope (> 18m from home) and has > 2.5s reload left,
					# keep driving to the firing position; once within 18m (or loaded with clear arc), stop and aim!
					_arc_cur = (agent.get('arty_arc_map') or {}).get(int(contact['id']))
					_arc_is_blk = bool(_arc_cur is not None and not _arc_cur.get('clear', True))
					if _distance_2d(position, home) > 9.5 and (_spg_rld_rem > 2.5 or _arc_is_blk):
						order['move_position'] = home
						order['throttle_override'] = None
						order['fire_allowed'] = False
						order['combat_mode'] = 'artillery_deploy'
					else:
						order['move_position'] = tuple(position)
						order['throttle_override'] = 0.0
						order['fire_allowed'] = True
						order['combat_mode'] = 'artillery_fire'
				order['shell_index'] = _chosen_spg_shell
				# v5.1 Smart Artillery: check if target is behind tall cover (arc blocked)
				_arc_info = (agent.get('arty_arc_map') or {}).get(int(contact['id']))
				_arc_blocked = bool(_arc_info is not None and not _arc_info.get('clear', True))
				_blk_dur = (now - float(_arc_info.get('blocked_since', now))) if _arc_blocked else 0.0
				if _arc_blocked and _blk_dur >= 2.5:
					# If a lightTank is behind cover for >= 2.5s, drop target Lock so SPG switches to HT/TD/MT!
					if contact.get('class_tag') == 'lightTank':
						agent['target_id'] = None
					# 1) Ask a nearby allied tank to bait/flush the enemy out of cover (max once per 48s, non-LT targets)!
					if contact.get('class_tag') != 'lightTank' and now - float(agent.get('last_arty_bait_req_at', -999.0)) >= 48.0:
						_best_ally_id = None
						_best_ally_d = 230.0
						for _ag_a in self.agents.values():
							if (_ag_a['id'] == agent['id'] or _ag_a.get('team') != agent.get('team') or
									_ag_a.get('health_fraction', 1.0) <= 0.25 or
									_ag_a['profile'].get('class_tag') == 'SPG' or
									_ag_a.get('position') is None):
								continue
							_da = _distance_2d(_ag_a['position'], contact['position'])
							if _da < _best_ally_d:
								_best_ally_d = _da
								_best_ally_id = _ag_a['id']
						_pvid_a = getattr(self, 'player_vid', None)
						if (_pvid_a is not None and getattr(self, 'player_team', None) == agent.get('team') and
								getattr(self, 'player_position', None) is not None and
								_number(getattr(self, 'player_hp_frac', 0.0), 0.0) > 0.25):
							_dp_a = _distance_2d(self.player_position, contact['position'])
							if _dp_a < _best_ally_d:
								_best_ally_d = _dp_a
								_best_ally_id = int(_pvid_a)
						if _best_ally_id is not None and _best_ally_d <= 85.0:
							agent['last_arty_bait_req_at'] = now
							if self._callouts_on():
								self._callout_event(
									'arty_bait_request', agent['id'], contact['id'],
									'ally:%s' % str(_best_ally_id))
							if _best_ally_id in self.agents:
								_ally_ag = self.agents[_best_ally_id]
								_ally_ag['arty_flush_task'] = {
									'target_id': contact['id'],
									'spg_id': agent['id'],
									'started_at': now,
									'until': now + 3.2,
								}
								if self._callouts_on():
									self._callout_event(
										'arty_bait_ack', _best_ally_id, contact['id'],
										'spg:%s' % str(agent['id']))
					# 2) Pro SPG relocates when arc is blocked >= 12.0s with a 32.0s cooldown so SPGs never spin in circles!
					_is_spotted_spg = bool(agent.get('is_spotted', False))
					if (_blk_dur >= 12.0 or _is_spotted_spg) and now - float(agent.get('last_arty_repos_at', -999.0)) >= 32.0:
						_repos_pt = self._spg_reposition_point(agent, position, contact['position'])
						agent['artillery_home'] = _repos_pt
						agent['arty_repos_target'] = _repos_pt
						agent['arty_repos_until'] = now + 16.0
						agent['last_arty_repos_at'] = now
						if self._callouts_on():
							self._callout_event('arty_reposition', agent['id'], contact['id'], 'threat' if _is_spotted_spg else 'blocked')
			# Execute lateral SPG reposition if active
			_repos_until = float(agent.get('arty_repos_until', 0.0) or 0.0)
			_repos_tgt = agent.get('arty_repos_target')
			if now < _repos_until and _repos_tgt is not None and _distance_2d(position, _repos_tgt) > 5.5:
				order['move_position'] = _repos_tgt
				order['throttle_override'] = None
				order['fire_allowed'] = False
				order['combat_mode'] = 'artillery_reposition'
			self._apply_artillery_self_defense(agent, order, position, contact, now)
			# v12.0 Solo SPG Panic / Hide / Rare Self-Destruct when all non-SPG allies are destroyed:
			if now >= 45.0:
				_my_t_spg = agent.get('team')
				_alive_non_spg_allies = 0
				_alive_enemies_cnt = 0
				for _ag_chk in self.agents.values():
					if _ag_chk.get('health_fraction', 0.0) <= 0.0 or _ag_chk.get('hp_abs', 1.0) <= 0.0:
						continue
					if _ag_chk.get('team') == _my_t_spg:
						if (_ag_chk.get('profile') or {}).get('class_tag') != 'SPG':
							_alive_non_spg_allies += 1
					else:
						_alive_enemies_cnt += 1
				if getattr(self, 'player_team', None) == _my_t_spg and _number(getattr(self, 'player_hp_frac', 0.0), 0.0) > 0.0:
					_alive_non_spg_allies += 1
				if _alive_non_spg_allies == 0 and _alive_enemies_cnt >= 1:
					_spg_fate_roll = (int(agent.get('seed', 0) or 0) + int(agent.get('id', 0) or 0) * 17) % 7
					if _spg_fate_roll == 0:
						# Rare (~14%) refusal to give the enemy the final frag: drown in deep water or HE wall self-destruct!
						_haz_list = getattr(self, 'hazards', None) or ()
						_best_water = None
						_best_w_d = 260.0
						for _hz in _haz_list:
							if isinstance(_hz, dict) and str(_hz.get('kind', 'water')) in ('water', 'deep_water', 'lake', 'river'):
								_hx = _number(_hz.get('x', (_hz.get('position') or (0, 0))[0]), 0.0)
								_hz_z = _number(_hz.get('z', (_hz.get('position') or (0, 0))[-1]), 0.0)
								_wd = math.hypot(position[0] - _hx, position[2] - _hz_z)
								if _wd < _best_w_d:
									_best_w_d = _wd
									_best_water = (_hx, position[1], _hz_z)
						if _best_water is not None:
							order['move_position'] = _best_water
							order['face_position'] = _best_water
							order['throttle_override'] = 1.0
							order['fire_allowed'] = False
							order['combat_mode'] = 'solo_spg_drown'
						else:
							_wall_pt = agent.get('solo_spg_wall_pt')
							if _wall_pt is None:
								for _ang_i in range(12):
									_ang_r = _ang_i * (math.pi / 6.0)
									for _probe_d in (8.0, 14.0, 22.0, 32.0):
										_cand_w = (position[0] + math.sin(_ang_r) * _probe_d, position[1], position[2] + math.cos(_ang_r) * _probe_d)
										if self._is_chord_impassable(position, _cand_w):
											_wall_pt = (position[0] + math.sin(_ang_r) * max(2.5, _probe_d - 3.5), position[1], position[2] + math.cos(_ang_r) * max(2.5, _probe_d - 3.5))
											break
									if _wall_pt is not None:
										break
								if _wall_pt is None:
									_wall_pt = (position[0] + math.sin(hull_yaw) * 3.0, position[1], position[2] + math.cos(hull_yaw) * 3.0)
								agent['solo_spg_wall_pt'] = _wall_pt
							order['move_position'] = _wall_pt
							order['face_position'] = _wall_pt
							order['aim_position'] = _wall_pt
							order['shell_index'] = 0
							order['throttle_override'] = 1.0 if _distance_2d(position, _wall_pt) > 3.5 else 0.0
							order['fire_allowed'] = bool(_distance_2d(position, _wall_pt) <= 4.5)
							order['combat_mode'] = 'solo_spg_wall_he'
					else:
						# Majority (~86%) panic/hide quietly in cover/corner waiting for last ambush shot
						_hide_pt = agent.get('solo_spg_hide_pt')
						if _hide_pt is None:
							_hide_pt = home if home is not None else tuple(position)
							agent['solo_spg_hide_pt'] = _hide_pt
						if _distance_2d(position, _hide_pt) > 7.5:
							order['move_position'] = _hide_pt
							order['throttle_override'] = None
						else:
							order['move_position'] = tuple(position)
							order['throttle_override'] = 0.0
						if contact is not None and contact.get('visible') and _distance_2d(position, contact['position']) <= 260.0:
							order['aim_position'] = tuple(contact['position'])
							order['face_position'] = tuple(contact['position'])
							order['fire_allowed'] = True
						else:
							order['fire_allowed'] = False
						order['combat_mode'] = 'solo_spg_hide'
			agent['last_order'] = order
			return order
		if contact is not None:
			distance = _distance_2d(position, contact['position'])
			contact['distance'] = distance
			force_balance = self._local_force_balance(
				agent, position, contact['position'], now)
			order['force_balance'] = force_balance
			order['target_id'] = contact['id']
			_s_ids = contact.get('shootable_by_ids')
			_mtn_blocked = (profile.get('class_tag') != 'SPG' and
			                self._is_mountain_blocked(position, contact['position']))
			_chord_impassable = (profile.get('class_tag') != 'SPG' and
			                     self._is_chord_impassable(position, contact['position']))
			has_direct_lane = bool(
				contact.get('visible') and
				((_s_ids is not None and int(agent['id']) in _s_ids) or
				 (_s_ids is None and not _mtn_blocked)))
			if has_direct_lane:
				agent['last_direct_lane_at'] = now
				order['aim_position'] = contact['position']
				order['face_position'] = self._angled_face_position(
					agent, position, contact['position'], now=now)
			else:
				# Pre-aim gun/turret at any spotted/recently-seen enemy within tactical range (195m turreted, 230m TD),
				# and NEVER drop aim when an enemy backs behind a corner within 165m or during an active duel!
				_is_td_pa = bool(profile.get('has_limited_traverse') or profile.get('class_tag') == 'AT-SPG')
				_pa_max_d = 230.0 if _is_td_pa else 195.0
				_is_sticky_pa = (int(contact['id']) == int(agent.get('last_engaged_target_id') or -999) and (now - float(agent.get('last_engaged_at', -999.0) or -999.0)) <= 18.0 and distance <= 220.0)
				_seen_recent_pa = bool(contact.get('visible') or (now - float(contact.get('last_seen', -999.0) or -999.0)) <= 14.0 or _is_sticky_pa)
				_can_preaim_occ = bool(_seen_recent_pa and distance <= _pa_max_d and (not _mtn_blocked or distance <= 165.0 or _is_sticky_pa))
				order['aim_position'] = contact['position'] if _can_preaim_occ else route_position
				order['face_position'] = contact['position'] if (_is_td_pa and _seen_recent_pa and distance <= 160.0 and (not _mtn_blocked or distance <= 125.0 or _is_sticky_pa)) else route_position
			order['fire_allowed'] = bool(has_direct_lane)
			order['shell_index'] = select_shell_index(profile, contact, personality, agent.get('skill_tier', 'medium'))
			agent['last_shell_index'] = int(order['shell_index'] or 0)
			if int(agent.get('_last_impen_tid', -1) or -1) != int(contact['id']):
				agent['target_impenetrable'] = False
				agent['_last_impen_tid'] = int(contact['id'])
			_bounces_on_tgt = int((agent.get('bounces_on_target') or {}).get(int(contact['id']), 0) or 0)
			if _bounces_on_tgt >= 2 and 'hard' != 'easy':
				# After 2+ consecutive bounces on this target, load highest-penetration non-HE shell (Gold APCR/HEAT)!
				_best_gold_idx = order['shell_index']
				_best_gold_pen = 0.0
				for _sh_g in (profile.get('shells') or ()):
					_sk_g = str(_sh_g.get('kind', '')).lower()
					if not ('explosive' in _sk_g and 'hollow' not in _sk_g):
						_sp_g = float(_sh_g.get('penetration', 0.0) or 0.0)
						if _sp_g > _best_gold_pen:
							_best_gold_pen = _sp_g
							_best_gold_idx = int(_sh_g.get('index', 0) or 0)
				order['shell_index'] = _best_gold_idx
				agent['target_impenetrable'] = True
			# v12.4 Mental Memory ('мысленная память'):
			# Only defer to mental_memory when the enemy is far away (> 165m), behind a mountain ridge (> 85m), or no longer visible!
			# When a visible enemy is within 165m behind a building corner, NEVER clear target_id—enter the combat block below
			# so the bot pre-aims its turret, uses building firing-lane pockets ('прострелы'), sidescrapes, or flanks!
			# Dynamic GPS Navigator vs Long-Range Mental Memory:
			# Defer to route ONLY when enemy is far away (> 185m), ancient/unseen, or behind an impassable map-dividing mountain ridge (> 85m).
			# When an enemy is spotted within tactical range (<= 185m) behind a city building corner, NEVER drop target_id or force route!
			# Bots dynamically navigate around corners towards the enemy using A* and engage tactical pockets (windows, corners, sidescraping)!
			_is_td_mm = bool(profile.get('has_limited_traverse') or profile.get('class_tag') == 'AT-SPG')
			_mm_max_d = 230.0 if _is_td_mm else 195.0
			_occ_blocked = bool(_mtn_blocked or _chord_impassable)
			_is_sticky_mm = (int(contact['id']) == int(agent.get('last_engaged_target_id') or -999) and (now - float(agent.get('last_engaged_at', -999.0) or -999.0)) <= 12.0 and distance <= 145.0 and not (_mtn_blocked and distance > 75.0))
			_seen_recent_mm = bool(contact.get('visible') or _is_sticky_mm or (distance <= 165.0 and (now - float(contact.get('last_seen', -999.0) or -999.0)) <= 14.0))
			_is_true_mtn_mm = bool(_mtn_blocked)
			_over_focused_occ = (not _is_sticky_mm and distance > 62.0 and self._focus_count(agent['team'], contact['id']) >= 3)
			_is_far_or_ancient = (distance > 185.0 or (not _seen_recent_mm and distance > 120.0) or (_is_true_mtn_mm and distance > 85.0) or (_occ_blocked and not agent.get('route_completed') and (distance > 115.0 or _over_focused_occ)))
			if not has_direct_lane and profile.get('class_tag') != 'SPG' and _is_far_or_ancient:
				_mm = agent.setdefault('mental_memory', {})
				_mm[int(contact['id'])] = {
					'id': int(contact['id']),
					'position': tuple(contact['position']),
					'seen_at': float(now),
					'distance': float(distance),
				}
				if not agent.get('route_completed'):
					order['target_id'] = None
					order['aim_position'] = route_position
					order['face_position'] = route_position
					order['move_position'] = route_position
					order['fire_allowed'] = False
					order['combat_mode'] = 'route'
				else:
					# Route completed ("стяжка"): navigate around obstacle via A* while keeping turret/hull forward along movement!
					_hunt_mtn = self._spread_attack_position(agent, position, contact['position'])
					order['target_id'] = contact['id'] if (_seen_recent_mm and distance <= 145.0 and not _mtn_blocked) else None
					order['aim_position'] = contact['position'] if order['target_id'] is not None else _hunt_mtn
					order['face_position'] = contact['position'] if (_is_td_mm and order['target_id'] is not None) else _hunt_mtn
					order['move_position'] = _hunt_mtn
					order['fire_allowed'] = False
					order['combat_mode'] = 'advance_contact'
			elif _seen_recent_mm:
				order['combat_mode'] = 'engage'
				rusher = _is_rusher(agent)
				close_ratio = 0.52 + personality['aggression'] * 0.12
				far_ratio = 1.02 + personality['caution'] * 0.28
				if rusher:
					# v4.66 доп.16 - close the gap earlier and accept a much
					# tighter brawl range than the cautious-scout default;
					# this is the whole point of scouting-through-force
					# instead of scouting-through-caution.
					far_ratio = 0.78

				# v15.0 Conflict-Free 3D Tactical Arbitrator:
				# For mobile LTs/MTs, 3D Dynamic Orbit / Flanking (lt_tactics) & Wolfpack Encirclement take priority over
				# defensive pullback (fear_tactics) and slope creeping (high_ground_tactics), preventing mid-attack reversals!
				_rld_rem_tac = max(0.0, _number(reload_time, 4.0) - _number(shoot_timer, 999.0))
				lt_tactics = self._evaluate_light_tank_tactics(
					agent, position, contact, now, hull_yaw=hull_yaw, route_position=route_position)
				fear_tactics = self._evaluate_fear_response(
					agent, position, contact, route_position, force_balance, _rld_rem_tac, now) if lt_tactics is None else None
				high_ground_tactics = self._evaluate_high_ground_tactics(
					agent, position, contact, now) if (lt_tactics is None and fear_tactics is None) else None
				shield_tactics = self._evaluate_defender_shield(
					agent, position, contact, now) if (lt_tactics is None and fear_tactics is None and high_ground_tactics is None) else None
				encircle_tactics = self._evaluate_encirclement(
					agent, position, contact, force_balance, now) if (lt_tactics is None and fear_tactics is None and high_ground_tactics is None and shield_tactics is None) else None
				lone_wolf_tactics = self._evaluate_lone_wolf(
					agent, position, contact, route_position, force_balance, now) if (lt_tactics is None and fear_tactics is None and high_ground_tactics is None and shield_tactics is None and encircle_tactics is None) else None

				if lt_tactics is not None:
					for lt_k, lt_v in lt_tactics.items():
						order[lt_k] = lt_v
				elif fear_tactics is not None:
					for fr_k, fr_v in fear_tactics.items():
						order[fr_k] = fr_v
				elif high_ground_tactics is not None:
					for hg_k, hg_v in high_ground_tactics.items():
						order[hg_k] = hg_v
				elif shield_tactics is not None:
					for sh_k, sh_v in shield_tactics.items():
						order[sh_k] = sh_v
				elif lone_wolf_tactics is not None:
					for lw_k, lw_v in lone_wolf_tactics.items():
						order[lw_k] = lw_v
				elif encircle_tactics is not None:
					for ec_k, ec_v in encircle_tactics.items():
						order[ec_k] = ec_v
				elif agent.get('target_impenetrable') and not (rusher and distance < 20.0):
					# Facing impenetrable front: mobile tanks and anyone without a direct lane flank;
					# heavies work weakspots briefly (<= 14s) at close/mid range, then flank or push!
					is_mobile = (profile.get('class_tag') in ('lightTank', 'mediumTank') or
					             profile.get('roles', {}).get('flanker', 0.0) >= 0.5)
					_impen_since = float(agent.get('_impen_hold_since', now) or now)
					if agent.get('_impen_hold_tid') != contact['id']:
						agent['_impen_hold_tid'] = contact['id']
						agent['_impen_hold_since'] = now
						_impen_since = now
					if is_mobile or not has_direct_lane or distance > 165.0 or (now - _impen_since) > 4.5 or contact.get('class_tag') == 'AT-SPG' or distance <= 42.0:
						order['move_position'] = self._flank_position(
							agent, position, contact['position']) if not _chord_impassable else route_position
						order['face_position'] = self._angled_face_position(agent, position, contact['position'], now=now)
						order['combat_mode'] = 'flank'
					elif _threat_prio_on() and force_balance >= -0.8:
						order['move_position'] = tuple(position)
						_withdraw_why('impen_hold', now)
					else:
						order['move_position'] = self._fallback_position(agent, position)
						order['combat_mode'] = 'withdraw'
						_withdraw_why('impen', now)
					# Radio call for ally support fire against the monster
					if now - float(agent.get('last_support_call', 0.0)) > (45.0 if self._radio_calm() else 20.0):
						agent['last_support_call'] = now
						if self._callouts_on():
							self._callout_event('support_fire', agent['id'], contact['id'], 'outgunned')
						self.apply_radio_command(
							RADIO_COMMAND_SUPPORT_FIRE, agent['id'], agent['team'],
							position, now, target_id=contact['id'])
				elif (distance < 165.0 and agent.get('health_fraction', 1.0) < 0.28 and
				        (now - float(agent.get('incoming_at', -999.0) or -999.0)) < 6.0 and
				        force_balance < -2.4 and not rusher):
					# Only retreat into local terrain cover when critically hurt and actively focused!
					_cov_ret = self._terrain_hulldown_position(agent, position, contact['position'], route_position) or self._reload_cover_position(agent, position, contact['position'])
					if _cov_ret is None:
						_cov_ret = route_position
					order['move_position'] = _cov_ret
					order['combat_mode'] = 'reload_cover'
					_withdraw_why('balance', now)
				elif (float(contact['position'][1]) - float(position[1])) > 12.0 and distance < max(120.0, (float(contact['position'][1]) - float(position[1])) * 3.4):
					# Target is high overhead on a bridge/cliff (e.g. Westfield bridge):
					if _distance_2d(position, route_position) > 18.0 and not agent.get('route_completed'):
						order['move_position'] = route_position
						order['combat_mode'] = 'route'
					else:
						order['move_position'] = self._flank_position(agent, position, contact['position'])
						order['combat_mode'] = 'flank'
				elif _chord_impassable:
					agent['last_seen_enemy_at'] = now
					_rld_rem_ow = max(0.0, _number(reload_time, 4.0) - _number(shoot_timer, 999.0))
					_pk_ow = self._find_tactical_pocket(agent, position, contact['position'], max_radius=26.0) if (distance <= 85.0 and not (force_balance >= 0.65 and _rld_rem_ow <= 0.85 and not has_direct_lane)) else None
					if _pk_ow is not None and not has_direct_lane and (_distance_2d(position, _pk_ow['peek']) <= 7.5 or _distance_2d(position, _pk_ow['stand']) <= 6.5):
						_p_key_ow = (int(round(_pk_ow['peek'][0])), int(round(_pk_ow['peek'][2])))
						if agent.get('_peek_no_los_key') != _p_key_ow:
							agent['_peek_no_los_key'] = _p_key_ow
							agent['_peek_no_los_since'] = float(now)
						elif (float(now) - float(agent.get('_peek_no_los_since', now) or now)) >= 1.8:
							agent.setdefault('_pocket_blacklist', {})[_p_key_ow] = float(now) + 18.0
							agent['_pk_cache'] = None
							_pk_ow = None
					if _pk_ow is not None and _distance_2d(position, _pk_ow['peek']) <= 28.0:
						if _rld_rem_ow > 1.25:
							order['move_position'] = tuple(position) if _distance_2d(position, _pk_ow['stand']) <= 3.8 else _pk_ow['stand']
							order['face_position'] = _pk_ow['face']
							order['aim_position'] = contact['position']
							order['combat_mode'] = 'reload_cover'
							order['fire_allowed'] = False
						else:
							order['move_position'] = tuple(position) if (has_direct_lane and _distance_2d(position, _pk_ow['peek']) <= 4.8) else _pk_ow['peek']
							order['face_position'] = contact['position'] if profile.get('class_tag') == 'AT-SPG' else _pk_ow['face']
							order['aim_position'] = contact['position']
							order['combat_mode'] = 'engage' if has_direct_lane else 'punish_peek'
					elif has_direct_lane and _rld_rem_ow <= 0.95 and distance <= profile['fire_range'] * 0.98:
						order['move_position'] = tuple(position)
						order['face_position'] = contact['position']
						order['aim_position'] = contact['position']
						order['throttle_override'] = 0.0
						order['combat_mode'] = 'engage'
					elif profile.get('class_tag') == 'AT-SPG' and not _mtn_blocked and distance <= 55.0 and (now - float(agent.get('last_direct_lane_at', -999.0) or -999.0)) <= 4.0:
						order['move_position'] = tuple(position)
						order['face_position'] = contact['position']
						order['aim_position'] = contact['position']
						order['throttle_override'] = 0.0
						order['combat_mode'] = 'engage'
					else:
						order['move_position'] = route_position if (not agent.get('route_completed') and (distance > 175.0 or (_mtn_blocked and distance > 85.0))) else self._spread_attack_position(agent, position, contact['position'])
						order['face_position'] = self._angled_face_position(agent, position, contact['position'], now=now) if distance <= 165.0 else order['move_position']
						order['aim_position'] = contact['position'] if distance <= 125.0 else order['move_position']
						order['combat_mode'] = 'engage' if has_direct_lane else ('route' if order['move_position'] == route_position else 'advance_contact')
				elif distance > profile['desired_range'] * far_ratio and profile.get('class_tag') != 'AT-SPG':
					agent['last_seen_enemy_at'] = now
					if distance > 265.0 and not agent.get('route_completed'):
						order['move_position'] = route_position
						order['combat_mode'] = 'advance_contact'
					elif profile.get('class_tag') in ('lightTank', 'mediumTank'):
						order['move_position'] = self._flank_position(agent, position, contact['position'])
						order['combat_mode'] = 'flank'
					else:
						order['move_position'] = self._spread_attack_position(
							agent, position, contact['position'])
						order['combat_mode'] = 'advance_contact'
				elif not has_direct_lane:
					agent['last_seen_enemy_at'] = now
					# Enemy is occluded behind a dead tank wreck, building corner, or ridge!
					_rld_rem_nl = max(0.0, _number(reload_time, 4.0) - _number(shoot_timer, 999.0))
					_rcov_until = float(agent.get('_latched_rcov_until', 0.0) or 0.0)
					_rcov_pos = agent.get('_latched_rcov_pos')
					if now < _rcov_until and _rld_rem_nl > 0.85 and _rcov_pos is not None:
						_d_rcov = _distance_2d(position, _rcov_pos)
						if _d_rcov <= 4.0:
							order['move_position'] = tuple(position)
							order['throttle_override'] = 0.0
						else:
							order['move_position'] = _rcov_pos
							order['throttle_override'] = -0.82 if _d_rcov <= 34.0 else None
						order['face_position'] = self._angled_face_position(agent, position, contact['position'], now=now) if (distance <= 165.0 or _d_rcov <= 34.0) else _rcov_pos
						order['aim_position'] = contact['position']
						order['combat_mode'] = 'reload_cover'
						order['fire_allowed'] = False
					else:
						_is_casemate_td_c = bool(profile.get('has_limited_traverse') or profile.get('class_tag') == 'AT-SPG')
						_pk_cov = self._find_tactical_pocket(agent, position, contact['position'], max_radius=28.0) if (distance <= 115.0 and not (force_balance >= 0.65 and _rld_rem_nl <= 0.85 and distance <= 75.0)) else None
						if _pk_cov is not None and _pk_cov.get('wreck_id') is not None:
							order['wreck_cover_id'] = _pk_cov.get('wreck_id')
						_pk_ok = (
							_pk_cov is not None and
							_distance_2d(position, _pk_cov['stand']) <= 26.0)
						if _pk_ok and (_distance_2d(position, _pk_cov['peek']) <= 7.5 or (_rld_rem_nl <= 0.85 and _distance_2d(position, _pk_cov['stand']) <= 6.5)):
							_p_key_nl = (int(round(_pk_cov['peek'][0])), int(round(_pk_cov['peek'][2])))
							if agent.get('_peek_no_los_key') != _p_key_nl:
								agent['_peek_no_los_key'] = _p_key_nl
								agent['_peek_no_los_since'] = float(now)
							elif (float(now) - float(agent.get('_peek_no_los_since', now) or now)) >= 1.8:
								agent.setdefault('_pocket_blacklist', {})[_p_key_nl] = float(now) + 18.0
								agent['_pk_cache'] = None
								_pk_ok = False
						else:
							agent['_peek_no_los_key'] = None
						_h_seed = int(agent.get('seed', 0) or 0) + int(agent.get('id', 0) or 0) * 37
						if _pk_ok:
							_ray_nl = self._evaluate_enemy_gun_rays_and_reload(
								agent, position, contact, peek_pos=_pk_cov['peek'], now=now)
							_enemy_locking_pk = bool(_ray_nl.get('any_loaded_gun_locking'))
							_safe_punish_pk = bool(_ray_nl.get('safe_punish_window'))
							if _rld_rem_nl > 0.65:
								# While reloading behind cover: hold steady inside the cover stand point (no 1.1s forward/reverse seesaw!)
								if _distance_2d(position, _pk_cov['stand']) <= 4.2:
									order['move_position'] = tuple(position)
									order['throttle_override'] = 0.0
									order['combat_mode'] = 'reload_cover'
								else:
									order['move_position'] = _pk_cov['stand']
									order['throttle_override'] = -0.78 if _distance_2d(position, _pk_cov['stand']) <= 14.0 else None
									order['combat_mode'] = 'reload_cover'
								order['face_position'] = contact['position'] if _is_casemate_td_c else _pk_cov['face']
								order['aim_position'] = contact['position']
								order['fire_allowed'] = False
							elif _enemy_locking_pk and not _safe_punish_pk and not _is_casemate_td_c:
								# v13.0 Gun-Ray Corner Discipline & Shot Baiting ('лучи орудий' + 'байтить на выстрел'):
								# A loaded enemy has its gun ray locked right on our corner! Do NOT blindly drive out full-hull!
								# Instead, cycle a 3.4s angled track-bait juke (1.15s poke 38% out at auto-ricochet angle -> 1.35s snap back to stand)
								# until the enemy wastes its shot (reload_left >= 2.1s) or turns its turret away!
								if agent.get('_corner_lock_tid') != contact['id']:
									agent['_corner_lock_tid'] = contact['id']
									agent['_corner_lock_since'] = float(now)
								_lock_dur = max(0.0, float(now) - float(agent.get('_corner_lock_since', now) or now))
								# Slow 5.6s corner-bait cycle (2.2s angled poke -> 3.4s hold) ONLY when distance > 28.0m
								# so close-range tanks (<= 28m) never juke back and forth ("танцуют")!
								_b_phase = (float(now) + float(_h_seed % 19) * 0.37) % 5.6
								_sx, _sy, _sz = _pk_cov['stand']
								_kx, _ky, _kz = _pk_cov['peek']
								if _lock_dur > 7.2 and (profile.get('class_tag') in ('lightTank', 'mediumTank') and _number(profile.get('armor', 80.0), 80.0) < 110.0):
									# Paper LT/MT refuses to trade into a pre-aimed corner after 7.2s: flank from another angle!
									agent['_corner_lock_since'] = float(now)
									order['move_position'] = self._flank_position(agent, position, contact['position'])
									order['face_position'] = order['move_position']
									order['aim_position'] = contact['position']
									order['combat_mode'] = 'flank'
								elif _lock_dur > 7.8 or distance <= 28.0:
									# Heavily armored tank or close-range brawler (<28m) commits to the angled peek without oscillating
									agent['_corner_lock_since'] = float(now)
									order['move_position'] = _pk_cov['peek']
									order['face_position'] = _pk_cov['face']
									order['aim_position'] = contact['position']
									order['combat_mode'] = 'punish_peek'
								elif _b_phase < 2.20:
									order['move_position'] = (_sx + (_kx - _sx) * 0.40, _sy, _sz + (_kz - _sz) * 0.40)
									order['face_position'] = _pk_cov['face']
									order['aim_position'] = contact['position']
									order['throttle_override'] = 0.62
									order['combat_mode'] = 'bait_juke_out'
									order['fire_allowed'] = True
								else:
									if _distance_2d(position, _pk_cov['stand']) <= 3.5:
										order['move_position'] = tuple(position)
										order['throttle_override'] = 0.0
									else:
										order['move_position'] = _pk_cov['stand']
										order['throttle_override'] = -0.78
									order['face_position'] = _pk_cov['face']
									order['aim_position'] = contact['position']
									order['combat_mode'] = 'bait_juke_back'
									order['fire_allowed'] = True
							else:
								agent['_corner_lock_since'] = float(now)
								if _safe_punish_pk and float(_ray_nl.get('target_reload_left', 0.0) or 0.0) >= 5.2 and distance <= 95.0 and force_balance >= -0.5:
									# Enemy has a long reload (>5.2s) left! Push out past the corner to punish!
									order['move_position'] = self._spread_attack_position(agent, position, contact['position'])
									order['face_position'] = self._angled_face_position(agent, position, contact['position'], now=now)
									order['aim_position'] = contact['position']
									order['combat_mode'] = 'punish_push'
								else:
									order['move_position'] = _pk_cov['peek']
									order['face_position'] = contact['position'] if _is_casemate_td_c else _pk_cov['face']
									order['aim_position'] = contact['position']
									order['combat_mode'] = 'punish_peek'
						elif _is_casemate_td_c and not _mtn_blocked and not _chord_impassable and distance <= 75.0 and (now - float(agent.get('last_direct_lane_at', -999.0) or -999.0)) <= 5.5:
							# Casemate TD within 75m in the same open corridor (no solid building block) briefly pre-aims through a wreck (<= 5.5s)
							order['move_position'] = tuple(position)
							order['face_position'] = contact['position']
							order['aim_position'] = contact['position']
							order['throttle_override'] = 0.0
							order['combat_mode'] = 'engage'
							order['fire_allowed'] = False
						else:
							# Close the distance / flank around buildings first while keeping turret pre-aimed within tactical range!
							if not agent.get('route_completed') and (distance > 185.0 or (_mtn_blocked and distance > 85.0)):
								order['move_position'] = route_position
								order['combat_mode'] = 'route'
							elif profile.get('class_tag') in ('lightTank', 'mediumTank') or profile.get('roles', {}).get('flanker', 0.0) >= 0.45:
								order['move_position'] = self._flank_position(agent, position, contact['position'])
								order['combat_mode'] = 'advance_contact'
							else:
								_win_pt = None
								if profile.get('class_tag') == 'AT-SPG' and distance <= 380.0 and not _mtn_blocked:
									_win_pt = self._crossfire_offset(agent, position, contact['position'])
									if _win_pt is not None:
										order['move_position'] = _win_pt
									elif distance >= 135.0:
										order['move_position'] = tuple(position)
										order['throttle_override'] = 0.0
									else:
										order['move_position'] = self._spread_attack_position(agent, position, contact['position'])
									order['combat_mode'] = 'support_snipe'
								else:
									order['move_position'] = route_position if (distance > 250.0 or (_mtn_blocked and distance > 165.0)) and not agent.get('route_completed') else self._spread_attack_position(agent, position, contact['position'])
									order['combat_mode'] = 'advance_contact'
							order['aim_position'] = contact['position'] if (not _mtn_blocked and distance <= (230.0 if _is_casemate_td_c else 185.0)) else (contact['position'] if (not _is_casemate_td_c and distance <= 165.0) else order['move_position'])
							order['face_position'] = contact['position'] if (_is_casemate_td_c and not _mtn_blocked and distance <= 115.0) else order['move_position']
							order['fire_allowed'] = False
				else:
					# Target is visible AND we have a direct firing lane (has_direct_lane == True)!
					agent['last_seen_enemy_at'] = now
					_ctag_m = profile.get('class_tag', 'mediumTank')
					_is_combat_lt_m = bool(agent.get('is_combat_lt') or profile.get('is_infantry_lt'))
					_sk_m = 'hard'
					_meta_m = str(agent.get('meta_role', 'standard'))
					_is_casemate_m = bool(profile.get('has_limited_traverse') or _ctag_m == 'AT-SPG')
					_in_brawl_zone = (distance <= 165.0 or bool(agent.get('route_completed')) or agent.get('health_fraction', 1.0) < 0.55)
					_pk_m = None if not _in_brawl_zone else self._find_tactical_pocket(agent, position, contact['position'], max_radius=34.0)
					if _pk_m is not None and _pk_m.get('wreck_id') is not None:
						order['wreck_cover_id'] = _pk_m.get('wreck_id')
					_hd_pos = None if (_pk_m is not None or not _in_brawl_zone) else self._terrain_hulldown_position(agent, position, contact['position'], route_position)
					if _ctag_m == 'lightTank' and not _is_combat_lt_m:
						# AMX 13 90 / Light Tank cover & wreck discipline: when within 135m and a dead wreck or corner pocket is right nearby (<=22m),
						# use it to shield the hull from dangerous enemies while shooting, otherwise flank!
						if 18.0 <= distance <= 135.0 and _pk_m is not None and _distance_2d(position, _pk_m['peek']) <= 22.0 and (_pk_m.get('is_wreck_cover') or _number(contact.get('armor', 80.0), 80.0) >= 85.0 or force_balance < 0.2):
							order['move_position'] = _pk_m['peek']
							order['face_position'] = _pk_m['face']
							order['combat_mode'] = 'punish_peek'
						else:
							order['move_position'] = self._flank_position(agent, position, contact['position']) if distance <= 285.0 else (route_position if not agent.get('route_completed') else self._flank_position(agent, position, contact['position']))
							order['combat_mode'] = 'flank'
					elif _ctag_m == 'mediumTank' or _is_combat_lt_m:
						_own_hp_m = _number(agent.get('hp_abs', 1000.0), 1000.0)
						_tgt_hp_m = _number(contact.get('health', 1000.0), 1000.0)
						_tgt_arm_m = _number(contact.get('armor', 80.0), 80.0)
						_rld_rem_m = max(0.0, _number(reload_time, 4.0) - _number(shoot_timer, 999.0))
						# Close-quarters finishing ram/brawl (<= 24m) when stronger/healthier:
						if (_sk_m in ('medium', 'hard') and distance <= 24.0 and not _chord_impassable and
								agent.get('health_fraction', 1.0) >= 0.45 and
								(profile.get('armor', 80.0) >= _tgt_arm_m * 0.95 or _own_hp_m >= _tgt_hp_m * 1.35 or _tgt_hp_m <= 220.0)):
							order['move_position'] = contact['position']
							order['face_position'] = contact['position']
							order['throttle_override'] = 1.0
							order['combat_mode'] = 'ram_brawl'
						elif not agent.get('route_completed') and distance > 255.0:
							order['move_position'] = route_position
							order['combat_mode'] = 'advance_contact'
						elif 24.0 <= distance <= 165.0 and _pk_m is not None and _distance_2d(position, _pk_m['peek']) <= 26.0:
							order['move_position'] = _pk_m['peek']
							order['face_position'] = _pk_m['face']
							order['combat_mode'] = 'punish_peek'
						elif 24.0 <= distance <= 165.0 and _hd_pos is not None and _distance_2d(position, _hd_pos) <= 22.0:
							order['move_position'] = _hd_pos
							order['combat_mode'] = 'engage'
						elif not _chord_impassable and distance <= 175.0:
							# Maneuver to crossfire/flank angle between shots!
							_cf_pos = self._crossfire_offset(agent, position, contact['position'])
							_flk_m = _cf_pos if (_cf_pos is not None and _distance_2d(position, _cf_pos) > 14.0) else self._flank_position(agent, position, contact['position'])
							order['move_position'] = _flk_m
							order['face_position'] = self._angled_face_position(agent, position, contact['position'], now=now) if distance >= 85.0 else _flk_m
							order['combat_mode'] = 'flank'
						else:
							order['move_position'] = self._flank_position(agent, position, contact['position'])
							order['combat_mode'] = 'flank'
					elif _ctag_m == 'heavyTank':
						_own_hp_h = _number(agent.get('hp_abs', agent.get('health', 1500.0)), 1500.0)
						_tgt_hp_h = _number(contact.get('health', 1200.0), 1200.0)
						_rld_rem_h = max(0.0, _number(reload_time, 8.0) - _number(shoot_timer, 999.0))
						_max_pk_dist_h = 10.0 if distance <= 28.0 else 28.0
						_has_near_corner_h = (_pk_m is not None and _distance_2d(position, _pk_m['stand']) <= _max_pk_dist_h)
						_clinch_latched = (
							int(agent.get('_latched_clinch_tid', -999) or -999) == int(contact['id']) and
							float(now) < float(agent.get('_latched_clinch_until', 0.0) or 0.0) and
							distance <= 19.5)
						if contact.get('class_tag') == 'AT-SPG' and distance <= 46.0 and not _chord_impassable and agent.get('health_fraction', 1.0) >= 0.30:
							# Never stand stationary in front of a casemate TD's gun at close range: flank past its traverse arc!
							_flk_td_h = self._flank_position(agent, position, contact['position'])
							order['move_position'] = _flk_td_h
							order['face_position'] = _flk_td_h if distance <= 32.0 else contact['position']
							order['combat_mode'] = 'flank'
						elif _clinch_latched or (_sk_m in ('medium', 'hard') and distance <= (15.0 if _has_near_corner_h else 22.0) and not _chord_impassable and
								agent.get('health_fraction', 1.0) >= 0.35 and
								(_tgt_hp_h <= 320.0 or _own_hp_h >= _tgt_hp_h * 1.25 or distance <= 13.5)):
							if not _clinch_latched:
								agent['_latched_clinch_tid'] = int(contact['id'])
								agent['_latched_clinch_until'] = float(now) + 4.8
							order['move_position'] = contact['position']
							order['face_position'] = contact['position']
							order['throttle_override'] = 1.0 if distance > 7.5 else 0.0
							order['combat_mode'] = 'ram_brawl' if distance > 14.5 else 'clinch'
						elif _pk_m is not None and _distance_2d(position, _pk_m['peek']) <= _max_pk_dist_h:
							# Always fight from nearby building corner, rock, terrain crest, or dead tank wreck instead of standing in the open!
							order['move_position'] = _pk_m['peek']
							order['face_position'] = _pk_m['face']
							order['combat_mode'] = 'punish_peek'
						elif _hd_pos is not None and _distance_2d(position, _hd_pos) <= 22.0:
							order['move_position'] = _hd_pos
							order['face_position'] = self._angled_face_position(agent, position, contact['position'], now=now)
							order['combat_mode'] = 'engage'
						else:
							_brawl_d = 125.0 if _meta_m in ('bottom_tier_support', 'autoloader_assassin') else 75.0
							if not agent.get('route_completed') and distance > 245.0:
								order['move_position'] = route_position
								order['combat_mode'] = 'advance_contact'
							elif not _chord_impassable and distance > _brawl_d:
								order['move_position'] = self._spread_attack_position(agent, position, contact['position'])
								order['face_position'] = self._angled_face_position(agent, position, contact['position'], now=now)
								order['combat_mode'] = 'advance_contact'
							else:
								# In the open with no pocket within 28m: angle hull (ромбом) and rock/advance rather than standing like a statue!
								_ang_face = self._angled_face_position(agent, position, contact['position'], now=now)
								order['move_position'] = self._spread_attack_position(agent, position, contact['position']) if _rld_rem_h > 1.2 else tuple(position)
								order['face_position'] = _ang_face
								order['combat_mode'] = 'engage'
					else:
						# [FIX v12.0] True AT-SPG ("ПТ-САУ") support-ray, hull-down/wreck cover & anti-squirm close-combat defense:
						# Never send a turretless TD at <= 145m to _spread_attack_position (which is at desired_range=250m and makes it turn its side/rear!).
						_rld_rem_td = max(0.0, _number(reload_time, 6.0) - _number(shoot_timer, 999.0))
						# Bush / foliage concealment check: if TD is in a bush and distance >= 115m, hold position with camo net!
						_fol_list = getattr(self, 'foliage', None) or ()
						_td_in_bush = any(((_fb[0] - position[0]) ** 2 + (_fb[2] - position[2]) ** 2) <= 36.0 for _fb in _fol_list) if _fol_list else False
						if _td_in_bush and distance >= 115.0 and not _mtn_blocked:
							order['move_position'] = tuple(position)
							order['face_position'] = contact['position']
							order['throttle_override'] = 0.0
							order['combat_mode'] = 'support_snipe' 
						if distance < 22.0:
							# Close-quarters anti-circle defense: if enemy is trying to get around our gun arc, back up straight while keeping front armor on enemy!
							_td_dx = float(contact['position'][0]) - float(position[0])
							_td_dz = float(contact['position'][2]) - float(position[2])
							_td_yw = math.atan2(_td_dx, _td_dz)
							_td_err = abs(_angle_delta(_td_yw, hull_yaw))
							_td_ul = max(1.0, math.hypot(_td_dx, _td_dz))
							_back_pt = (float(position[0]) - (_td_dx / _td_ul) * 9.0, float(position[1]), float(position[2]) - (_td_dz / _td_ul) * 9.0)
							if (_td_err > 0.30 or (distance < 12.0 and _rld_rem_td > 1.0)) and not self._is_chord_impassable(position, _back_pt):
								order['move_position'] = _back_pt
								order['face_position'] = contact['position']
								order['throttle_override'] = -0.58
								order['combat_mode'] = 'reload_cover'
							else:
								order['move_position'] = tuple(position)
								order['face_position'] = contact['position']
								order['throttle_override'] = 0.0
								order['combat_mode'] = 'engage'
						elif distance <= 48.0:
							# At 22..48m, hold steady and pivot hull onto the enemy (or use a wreck/crest right in front of us within 10m)
							if _pk_m is not None and _distance_2d(position, _pk_m['peek']) <= 10.0 and _rld_rem_td > 1.2:
								order['move_position'] = _pk_m['peek']
							else:
								order['move_position'] = tuple(position)
								order['throttle_override'] = 0.0
							order['face_position'] = contact['position']
							order['combat_mode'] = 'engage'
						elif _pk_m is not None and _distance_2d(position, _pk_m['peek']) <= 24.0:
							order['move_position'] = _pk_m['peek']
							order['face_position'] = contact['position']
							order['combat_mode'] = 'engage'
						elif _hd_pos is not None and _distance_2d(position, _hd_pos) <= 20.0:
							order['move_position'] = _hd_pos
							order['face_position'] = contact['position']
							order['combat_mode'] = 'engage'
						else:
							order['move_position'] = tuple(position)
							order['face_position'] = contact['position']
							order['combat_mode'] = 'engage'

				if not has_direct_lane and order.get('combat_mode') not in ('punish_peek', 'reload_cover', 'circle_of_death', 'wolfpack_encircle', 'punish_push', 'ram_brawl', 'clinch', 'flank'):
					_is_td_nl_f = bool(profile.get('has_limited_traverse') or profile.get('class_tag') == 'AT-SPG')
					_pa_lim_f = 230.0 if _is_td_nl_f else 195.0
					order['aim_position'] = contact['position'] if (distance <= _pa_lim_f and (not _mtn_blocked or distance <= 165.0 or _is_sticky_mm)) else order['move_position']
					if (_is_td_nl_f and distance <= 155.0) or order.get('move_position') == tuple(position) or distance <= 165.0:
						order['face_position'] = self._angled_face_position(agent, position, contact['position'], now=now)
					else:
						order['face_position'] = order['move_position']
					order['fire_allowed'] = False
				elif order.get('combat_mode') not in ('cliff_dive_ram', 'airborne_jump_shot', 'crest_depress_peek', 'circle_of_death', 'defender_shield', 'lone_wolf_retreat', 'lone_wolf_stalk', 'wolfpack_encircle', 'hill_descent_flank', 'clinch', 'ram_brawl'):
					reload_remaining = max(0.0, _number(reload_time, 4.0) - _number(shoot_timer, 999.0))
					skill_tier = 'hard'
					_ctag_f = profile.get('class_tag', 'mediumTank')
					_is_casemate_f = bool(profile.get('has_limited_traverse') or _ctag_f == 'AT-SPG')
					_meta_f = str(agent.get('meta_role', 'standard'))
					_is_c_lt_f = bool(agent.get('is_combat_lt') or profile.get('is_infantry_lt'))
					_pk_f = self._find_tactical_pocket(agent, position, contact['position'], max_radius=32.0)
					if _pk_f is not None and _pk_f.get('wreck_id') is not None:
						order['wreck_cover_id'] = _pk_f.get('wreck_id')
					bait_order = None
					if skill_tier in ('hard', 'medium') and (_ctag_f == 'heavyTank' or ((_ctag_f == 'mediumTank' or _is_c_lt_f) and order.get('combat_mode') != 'flank')):
						bait_order = self._evaluate_cyber_bait(
							agent, order, position, contact, now, reload_remaining)
					if bait_order is not None:
						for b_key, b_val in bait_order.items():
							order[b_key] = b_val
					elif _ctag_f in ('heavyTank', 'mediumTank', 'AT-SPG', 'lightTank'):
						# v12.0 Active Cover Peek-a-Boo ('Выкатился — дал шот — откатился за укрытие/труп танка на КД'):
						# Applies across ALL combat modes (engage, punish_peek, advance_contact, flank, support_fire)
						# whenever reloading (reload_remaining > 1.25s) so tanks NEVER stand out in the open like a shooting gallery ("тиром заниматься")!
						_rld_thresh = 1.25 if not _is_casemate_f else 1.85
						_max_rcov_d = 175.0
						# Require distance > 18.5m before initiating reload_cover pullback so close brawlers (<18.5m) never oscillate between clinch and reload_cover!
						_max_stand_d = 10.0 if distance <= 28.0 else (14.0 if _is_casemate_f else 22.0)
						if (reload_remaining > _rld_thresh and distance > (22.0 if _is_casemate_f else 18.5) and distance <= _max_rcov_d and
								order['combat_mode'] in ('engage', 'punish_peek', 'advance_contact', 'flank', 'support_fire')):
							_chosen_rcov = None
							_chosen_rface = contact['position']
							if _pk_f is not None and _distance_2d(position, _pk_f['stand']) <= _max_stand_d:
								# Accept building corner (kind==1), hull-down reverse slope (kind==2), or dead tank wreck shield (kind==3)!
								_rc_ok = True
								if _is_casemate_f:
									_cdx = float( position[0]) - float(contact['position'][0])
									_cdz = float(position[2]) - float(contact['position'][2])
									_cl = max(1.0, math.hypot(_cdx, _cdz))
									_sdx = float(_pk_f['stand'][0]) - float(position[0])
									_sdz = float(_pk_f['stand'][2]) - float(position[2])
									_sl = math.hypot(_sdx, _sdz)
									_rc_ok = (_sl <= 2.5 or ((_sdx * _cdx + _sdz * _cdz) / (_sl * _cl)) >= 0.88)
								if _rc_ok:
									_chosen_rcov = _pk_f['stand']
									_chosen_rface = contact['position'] if _is_casemate_f else _pk_f['face']
							if _chosen_rcov is None:
								cover_pos = self._reload_cover_position(agent, position, contact['position'])
								if cover_pos is not None and 2.5 <= _distance_2d(position, cover_pos) <= (14.0 if _is_casemate_f else 22.0):
									if self._is_chord_impassable(cover_pos, contact['position']) or self._is_mountain_blocked(cover_pos, contact['position']):
										_chosen_rcov = cover_pos
									elif reload_remaining > 6.5 and not _is_casemate_f:
										_chosen_rcov = self._fallback_position(agent, position)
							if _chosen_rcov is not None and _distance_2d(position, _chosen_rcov) <= 4.2 and has_direct_lane:
								# Already at _chosen_rcov and STILL in enemy LOS: it is exposed! Pull back further along route!
								_fb_rc = self._fallback_position(agent, position)
								_chosen_rcov = _fb_rc if _distance_2d(position, _fb_rc) > 4.5 else None
							if _chosen_rcov is not None:
								agent['_latched_rcov_pos'] = _chosen_rcov
								agent['_latched_rcov_until'] = now + min(reload_remaining - 0.35, 4.5)
								if _distance_2d(position, _chosen_rcov) <= 3.5:
									order['move_position'] = tuple(position)
									order['throttle_override'] = 0.0
								else:
									order['move_position'] = _chosen_rcov
									order['throttle_override'] = -0.78 if _distance_2d(position, _chosen_rcov) <= 14.0 else None
								order['face_position'] = _chosen_rface
								order['aim_position'] = contact['position']
								order['combat_mode'] = 'reload_cover'
								order['fire_allowed'] = False
							elif not _is_casemate_f and order['move_position'] == tuple(position):
								# In the open with no cover nearby while reloading: never sit frozen like a shooting gallery!
								if _ctag_f == 'mediumTank' or _is_c_lt_f:
									order['move_position'] = self._flank_position(agent, position, contact['position'])
									order['combat_mode'] = 'flank'
								else:
									order['move_position'] = self._spread_attack_position(agent, position, contact['position'])
									order['face_position'] = self._angled_face_position(agent, position, contact['position'], now=now)
									order['combat_mode'] = 'advance_contact'

					# v12.4 True Stop-to-Aim & Settle Fire Discipline ("НАВОДИТЬСЯ, СВОДИТЬСЯ, А ПОТОМ СТРЕЛЯТЬ"):
					if order.get('combat_mode') not in ('circle_of_death', 'flank', 'ram_brawl', 'clinch', 'reload_cover') and _ctag_f != 'lightTank':
						_has_limited_traverse = bool(profile.get('has_limited_traverse') or _ctag_f in ('AT-SPG', 'SPG'))
						if not has_direct_lane or reload_remaining > 0.45 or int(agent.get('_direct_aim_tid', -1) or -1) != int(contact['id']):
							agent['_direct_aim_tid'] = int(contact['id'])
							agent['_direct_aim_started_at'] = float(now)
						_aim_stall = max(0.0, float(now) - float(agent.get('_direct_aim_started_at', now) or now))
						# Zero minimum stop distance for AT-SPG so casemate TDs never drop out of stationary aiming at close range (<18m)!
						_min_stop_d = 0.0 if _ctag_f == 'AT-SPG' else (28.0 if _ctag_f == 'heavyTank' else 135.0)
						_max_aim_hold = 3.20 if _ctag_f in ('heavyTank', 'AT-SPG') else 1.85
						# If a turreted tank is driving out to a nearby cover peek point (_pk_f['peek'] within 4..16m), let it reach the peek point before halting!
						_approaching_peek = (_pk_f is not None and 3.2 < _distance_2d(position, _pk_f['peek']) <= 18.0)
						if not _approaching_peek and has_direct_lane and distance >= _min_stop_d and reload_remaining <= 0.35 and _aim_stall <= _max_aim_hold and distance <= profile['fire_range'] * 0.98:
							order['move_position'] = tuple(position)
							if _pk_f is not None and _ctag_f != 'AT-SPG':
								order['face_position'] = _pk_f['face']
							elif _ctag_f in ('heavyTank', 'AT-SPG'):
								order['face_position'] = contact['position']
							else:
								order['face_position'] = self._angled_face_position(agent, position, contact['position'], now=now)
							order['combat_mode'] = 'engage'
							order['throttle_override'] = 0.0
							order['fire_allowed'] = True
						elif _aim_stall > _max_aim_hold:
							if _aim_stall > _max_aim_hold + 2.2:
								agent['_direct_aim_started_at'] = float(now)
							if _is_casemate_f and has_direct_lane and distance <= profile['fire_range'] * 0.98:
								order['move_position'] = tuple(position)
								order['face_position'] = contact['position']
								order['combat_mode'] = 'engage'
								order['throttle_override'] = 0.0
								order['fire_allowed'] = True
							else:
								if _pk_f is not None and _distance_2d(position, _pk_f['peek']) > 4.0:
									order['move_position'] = _pk_f['peek']
									order['face_position'] = contact['position']
								else:
									order['move_position'] = self._flank_position(agent, position, contact['position']) if _ctag_f in ('lightTank', 'mediumTank') else self._spread_attack_position(agent, position, contact['position'])
									order['face_position'] = contact['position']
								order['combat_mode'] = 'advance_contact'
								order['throttle_override'] = None

				# v5.1: Allied bot executing Artillery's request to bait/lure enemy out of cover (never interrupts an active attack!)
				_in_active_attack = order.get('combat_mode') in ('circle_of_death', 'wolfpack_encircle', 'punish_push', 'ram_brawl', 'clinch', 'flank')
				reload_remaining = max(0.0, _number(reload_time, 6.0) - _number(shoot_timer, 999.0))
				_flush_task = agent.get('arty_flush_task')
				if not _in_active_attack and isinstance(_flush_task, dict) and now < float(_flush_task.get('until', 0.0)) and (not has_direct_lane or reload_remaining > 1.5):
					if int(contact['id']) == int(_flush_task.get('target_id', -999)) and distance <= 85.0:
						_ft_elapsed = now - float(_flush_task.get('started_at', now))
						if _ft_elapsed < 1.3:
							# Phase 1: aggressive fake-poke toward enemy corner to grab their attention
							order['move_position'] = self._spread_attack_position(agent, position, contact['position'])
							order['throttle_override'] = 0.85
							order['combat_mode'] = 'bait_twitch_out'
						else:
							# Phase 2: tactical reverse lure — pull back 14m so enemy chases us out of cover into SPG's reticle!
							_lure_pos = self._reload_cover_position(agent, position, contact['position']) or self._fallback_position(agent, position)
							order['move_position'] = _lure_pos
							order['throttle_override'] = -0.85
							order['combat_mode'] = 'bait_twitch_back'
							_enemy_ag = self.agents.get(int(contact['id']))
							if _enemy_ag is not None:
								_enemy_ag['provoked_by_id'] = agent['id']
								_enemy_ag['provoked_until'] = now + 4.5
								_enemy_ag['provoked_pos'] = tuple(position)

				# v5.1: Enemy bot reacting to provocation (taking the bait and driving out of cover!)
				_prov_until = float(agent.get('provoked_until', 0.0) or 0.0)
				if now < _prov_until and agent.get('provoked_pos') is not None and distance <= 190.0:
					_sk_prov = 'hard'
					_takes_bait = (_sk_prov == 'easy') or (_sk_prov == 'medium' and (int(agent.get('seed', 0)) % 2 == 0)) or (personality.get('aggression', 0.5) >= 0.62)
					if _takes_bait and agent.get('health_fraction', 1.0) >= 0.35:
						order['move_position'] = self._spread_attack_position(agent, position, agent['provoked_pos'])
						order['throttle_override'] = 0.90
						order['combat_mode'] = 'advance_contact'
						order['fire_allowed'] = True

				# v9.1: If the tank's gun is loaded (or nearly loaded <= 1.8s) and has a direct firing lane on a visible enemy,
				# finish aiming and shoot first before executing any warning/hit-reaction cover pullback!
				_rld_rem_now = max(0.0, _number(reload_time, 6.0) - _number(shoot_timer, 999.0))
				_ready_to_shoot_back = bool(has_direct_lane and _rld_rem_now <= 1.8)
				_is_ht_or_td = profile.get('class_tag') in ('heavyTank', 'AT-SPG')
				# v5.1: Bot heeding an ally's predictive danger warning (never interrupts an active attack!)
				_heed_until = float(agent.get('heeded_warning_until', 0.0) or 0.0)
				if not _in_active_attack and now < _heed_until and has_direct_lane and not _ready_to_shoot_back:
					_cov_w = self._reload_cover_position(agent, position, contact['position']) or self._fallback_position(agent, position)
					order['move_position'] = _cov_w
					order['throttle_override'] = -0.85 if (distance < 220.0 or _is_ht_or_td) else None
					order['combat_mode'] = 'reload_cover'

				# v5.3: Reaction to unexpected flank / blind hit (never interrupts an active attack!)
				_hr_until = float(agent.get('hit_reaction_until', 0.0) or 0.0)
				_hr_pos = agent.get('hit_reaction_pos')
				if not _in_active_attack and now < _hr_until and _hr_pos is not None and not _ready_to_shoot_back:
					_hr_mode = 'watch_map' if _is_ht_or_td else str(agent.get('hit_reaction_mode', 'relocate'))
					_hr_face = contact['position'] if contact.get('visible') else (agent.get('hit_reaction_face') or contact['position'])
					_hr_dist = _distance_2d(position, _hr_pos)
					if _hr_dist <= 4.5:
						# Reached cover point: clear hit reaction early so bot never sits frozen!
						agent['hit_reaction_until'] = min(_hr_until, now + 0.8)
					if _hr_mode == 'relocate' and _hr_dist > 4.5:
						order['move_position'] = _hr_pos
						order['face_position'] = self._angled_face_position(agent, position, _hr_face, now=now)
						order['aim_position'] = _hr_face
						order['throttle_override'] = -0.85 if _is_ht_or_td else None
						order['combat_mode'] = 'reload_cover'
					elif _hr_mode == 'watch_map' and _hr_dist > 4.5:
						order['move_position'] = _hr_pos
						order['face_position'] = self._angled_face_position(agent, position, _hr_face, now=now)
						order['aim_position'] = _hr_face
						order['throttle_override'] = -0.85
						order['combat_mode'] = 'reload_cover'

				# v12.0 Anti-Arty Smart Relocation: if an SPG shell recently landed near/hit this bot,
				# relocate out of the compromised spot into a building/cliff arty-safe shadow while still aiming at enemies!
				_arty_comp_until = float(agent.get('arty_compromised_until', 0.0) or 0.0)
				if not _in_active_attack and now < _arty_comp_until and agent.get('arty_threat_pos') is not None and not _ready_to_shoot_back:
					_arty_safe_p = self._find_arty_safe_position(agent, position, agent['arty_threat_pos'], contact['position'], now)
					if _arty_safe_p is not None and _distance_2d(position, _arty_safe_p) > 5.0:
						order['move_position'] = _arty_safe_p
						order['face_position'] = self._angled_face_position(agent, position, contact['position'], now=now)
						order['throttle_override'] = None
						order['combat_mode'] = 'arty_evade_relocate'
			else:
				# v5.2: Anti-dementia, tracer blind-fire, hit reactions, and active hunting of Last Known Positions!
				_u_age = max(0.0, _number(now) - _number(contact.get('last_seen'), -999.0))
				_u_vel = contact.get('velocity', (0.0, 0.0)) or (0.0, 0.0)
				_ext_t = min(2.0, _u_age)
				_pred_pos = (
					float(contact['position'][0]) + float(_u_vel[0]) * _ext_t,
					float(contact['position'][1]),
					float(contact['position'][2]) + float(_u_vel[1]) * _ext_t)
				_sk_u = 'hard'
				_is_tracer_tgt = bool(
					(agent.get('tracer_target_id') == contact['id'] and now < float(agent.get('tracer_blind_until', 0.0) or 0.0)) or
					(contact.get('tracer_spotted_at') and (now - float(contact.get('tracer_spotted_at', -999.0))) <= 6.0))
				_can_blind = (not _mtn_blocked and not _chord_impassable and
				              (_is_tracer_tgt or (now - float(agent.get('last_direct_lane_at', -999.0) or -999.0)) <= 6.5))
				_blind_win = (6.5 if _is_tracer_tgt else (4.5 if _sk_u == 'hard' else (2.6 if _sk_u == 'medium' else 0.0))) if _can_blind else 0.0
				if _can_blind and _u_age <= _blind_win:
					order['aim_position'] = _pred_pos
					order['face_position'] = self._angled_face_position(agent, position, _pred_pos, now=now)
				else:
					order['aim_position'] = route_position
					order['face_position'] = route_position
				_hr_until = float(agent.get('hit_reaction_until', 0.0) or 0.0)
				_hr_pos = agent.get('hit_reaction_pos')
				if now < _hr_until and _hr_pos is not None:
					# Just took a blind/flank hit while no enemy is visible: relocate or back into cover while aiming at hit vector!
					_hr_mode = str(agent.get('hit_reaction_mode', 'relocate'))
					_hr_face = agent.get('hit_reaction_face') or _pred_pos
					_hr_dist = _distance_2d(position, _hr_pos)
					order['face_position'] = self._angled_face_position(agent, position, _hr_face, now=now)
					order['aim_position'] = _hr_face
					if _hr_dist <= 4.5:
						agent['hit_reaction_until'] = min(_hr_until, now + 1.0)
					if _hr_mode == 'relocate' and _hr_dist > 4.5:
						order['move_position'] = _hr_pos
						order['throttle_override'] = None
						order['combat_mode'] = 'reload_cover'
					elif _hr_dist > 4.5:
						order['move_position'] = _hr_pos
						order['throttle_override'] = -0.85
						order['combat_mode'] = 'reload_cover'
					else:
						order['move_position'] = tuple(position)
						order['throttle_override'] = 0.0
						order['fire_allowed'] = True
						order['combat_mode'] = 'investigate'
				elif _u_age <= _blind_win and distance <= 320.0:
					# Hold aim on the bush/corner (or tracer flash!) and fire a blind-shot!
					order['move_position'] = tuple(position)
					order['throttle_override'] = 0.0
					order['fire_allowed'] = True
					order['combat_mode'] = 'engage'
				else:
					# v6.6: If the bot has not completed its route, return to pushing its corridor
					# instead of parking for 12s or herding all squads toward one remembered dot!
					if not agent.get('route_completed') and (_u_age > 4.0 or distance > 135.0 or _chord_impassable):
						order['move_position'] = route_position
						order['face_position'] = route_position
						order['throttle_override'] = None
						order['combat_mode'] = 'route'
					else:
						order['combat_mode'] = 'investigate'
						if distance < 22.0 and _u_age > 6.0:
							_ebase = self.bases.get(2 if agent.get('team') == 1 else 1)
							if _ebase is not None:
								_bdx = float(_ebase[0]) - float(position[0])
								_bdz = float(_ebase[1]) - float(position[2])
								_bdl = max(1.0, math.sqrt(_bdx * _bdx + _bdz * _bdz))
								_next_hunt = (
									float(position[0]) + (_bdx / _bdl) * min(85.0, _bdl),
									float(position[1]),
									float(position[2]) + (_bdz / _bdl) * min(85.0, _bdl))
								contact['position'] = _next_hunt
								_pred_pos = _next_hunt
						order['move_position'] = self._spread_attack_position(agent, position, _pred_pos)
						order['aim_position'] = order['move_position']
						order['face_position'] = order['move_position']
						order['throttle_override'] = None
		# v13.0 Dynamic Regrouping ('Перегруппировка'): rebalance over-quota/empty lanes or losing flanks
		if (order.get('combat_mode') in ('route', 'investigate', 'advance_contact') and
				(contact is None or (not contact.get('visible') and _distance_2d(position, contact['position']) > 125.0))):
			if self._consider_route_switch(agent, position, now):
				route_position = self._route_position(
					agent, position, now, hull_yaw)
				order['move_position'] = route_position
				order['face_position'] = route_position
				order['route_id'] = (agent['route'].get('id')
					if agent.get('route') else 'direct')
		# v4.70 tactical convergence (grouping / "kuchka"): when a bot has
		# no contacts of its own but a significant fight is nearby, converge
		# on the fight instead of driving an empty route to nowhere.
		if (contact is None and order.get('combat_mode') == 'route' and
				bool(agent.get('route_completed')) and
				profile.get('class_tag') != 'SPG'):
			_allow_cap_cv = self._should_bot_capture_base(agent, position, now)
			_eb_cv = self.bases.get(2 if agent.get('team') == 1 else 1)
			_near_cap_cv = (_allow_cap_cv and _eb_cv is not None and _distance_2d(position, (_eb_cv[0], position[1], _eb_cv[1])) <= 35.0)
			fight_centre = self._kd_hunt_objective(agent, position, now)
			if fight_centre is not None and (not _near_cap_cv or _distance_2d(position, fight_centre) <= 85.0):
				conv_pos = self._convergence_position(
					agent, position, fight_centre)
				order['move_position'] = conv_pos
				order['face_position'] = fight_centre
				order['combat_mode'] = 'converge'
		if contact is None:
			_hr_u0 = float(agent.get('hit_reaction_until', 0.0) or 0.0)
			_hr_p0 = agent.get('hit_reaction_pos')
			if now < _hr_u0 and _hr_p0 is not None:
				_hr_f0 = agent.get('hit_reaction_face') or order['aim_position']
				order['target_id'] = agent.get('hit_reaction_attacker_id')
				order['aim_position'] = _hr_f0
				order['face_position'] = self._angled_face_position(agent, position, _hr_f0, now=now)
				_d_hr0 = _distance_2d(position, _hr_p0)
				if _d_hr0 > 4.5:
					order['move_position'] = _hr_p0
					order['throttle_override'] = -0.85 if (agent.get('hit_reaction_mode') == 'watch_map' and _d_hr0 <= 14.0) else None
					order['combat_mode'] = 'reload_cover'
		current_fraction = _number(health, 1.0) / max(_number(max_health, 1.0), 1.0)
		self._apply_squad_order(agent, order, position, now)
		self._apply_base_defense_order(agent, order, position, contact, now, reload_time, shoot_timer)
		_inc_recent_lh = (now - float(agent.get('incoming_at', -999.0) or -999.0)) < 8.5
		_s_ids_lh = contact.get('shootable_by_ids') if contact is not None else None
		_has_lane_lh = bool(contact is not None and contact.get('visible') and (_s_ids_lh is None or int(agent['id']) in _s_ids_lh))
		_contact_near_lh = bool(contact is not None and contact.get('position') is not None and _distance_2d(position, contact['position']) < 225.0)
		_vis_threat_lh = bool(_has_lane_lh and _contact_near_lh)
		_crit_hp_lh = (current_fraction <= (0.28 + personality['caution'] * 0.10) or _number(health, 1000.0) <= 280.0)
		# v14.0 Persistent Self-Preservation: a critically wounded tank (<= 28..36% HP or <= 280 HP) MUST stay in cover
		# as long as any enemy contact is within 225m, instead of charging back out around the corner 6s after taking a hit!
		low_health_withdraw = bool(_crit_hp_lh and (_vis_threat_lh or _inc_recent_lh or (_contact_near_lh and order.get('combat_mode') in ('route', 'advance_contact', 'punish_push', 'engage', 'flank'))))
		if low_health_withdraw:
			_withdraw_why('lowhp', now)
		if not low_health_withdraw:
			self._apply_radio_order(agent, order, position, now)
		if low_health_withdraw:
			# Latch withdraw cover position for 7.5s and hold defensively inside cover so critically low-HP tanks NEVER feed!
			_lh_cov = agent.get('_lh_withdraw_pos') if now < float(agent.get('_lh_withdraw_until', 0.0) or 0.0) else None
			if _lh_cov is None:
				_pk_lh = self._find_tactical_pocket(agent, position, contact['position'], max_radius=28.0) if (contact is not None and contact.get('position') is not None) else None
				if _pk_lh is not None and _pk_lh.get('stand') is not None:
					_lh_cov = _pk_lh['stand']
				else:
					_lh_cov = (self._reload_cover_position(agent, position, contact['position'])
						if contact is not None and contact.get('position') is not None else None) or self._fallback_position(agent, position)
				agent['_lh_withdraw_pos'] = _lh_cov
				agent['_lh_withdraw_until'] = now + 7.5
			if contact is not None and contact.get('position') is not None:
				order['aim_position'] = contact['position']
				order['face_position'] = contact['position'] if (profile.get('class_tag') == 'AT-SPG' or profile.get('has_limited_traverse')) else self._angled_face_position(agent, position, contact['position'], now=now)
			if _lh_cov is not None and (_distance_2d(position, _lh_cov) <= 4.2 or (not _vis_threat_lh and not _inc_recent_lh and _distance_2d(position, _lh_cov) <= 9.5)):
				order['move_position'] = tuple(position)
				order['throttle_override'] = 0.0
			else:
				order['move_position'] = _lh_cov
				order['throttle_override'] = -0.88 if (_vis_threat_lh or _inc_recent_lh or _distance_2d(position, _lh_cov) <= 18.0) else None
			order['combat_mode'] = 'reload_cover' if order.get('throttle_override') is not None and float(order.get('throttle_override') or 0.0) < 0.0 else 'withdraw'
		# v14.0 Wreck-Barrier Corridor Detour & Route Switch:
		# If a narrow street/corridor (e.g. Himmelsdorf Banana alley) is blocked by dead tank wrecks and this bot makes
		# < 3.8m progress over 6.0s while trying to travel past wrecks ahead (and NOT actively dueling with direct LOS),
		# compute a lateral bypass OR switch to an adjacent open route with a 40s cooldown and blocked-route memory.
		try:
			_wrecks_dict = getattr(self, 'wrecks', None) or {}
			_cm_wd = str(order.get('combat_mode', 'route') or 'route')
			_has_dl_wd = bool(contact is not None and contact.get('visible') and ((contact.get('shootable_by_ids') is not None and int(agent['id']) in contact.get('shootable_by_ids')) or (contact.get('shootable_by_ids') is None and not self._is_mountain_blocked(position, contact['position']))))
			_close_brawl_duel = bool(contact is not None and _has_dl_wd and float(contact.get('distance', 999.0) or 999.0) <= 18.0 and _cm_wd in ('clinch', 'ram_brawl'))
			if _wrecks_dict and profile.get('class_tag') != 'SPG' and not _close_brawl_duel:
				_det_until = float(agent.get('_wreck_detour_until', 0.0) or 0.0)
				_det_wp = agent.get('_wreck_detour_wp')
				if now < _det_until and _det_wp is not None:
					if _distance_2d(position, _det_wp) <= 5.5:
						agent['_wreck_detour_until'] = 0.0
						agent['_wreck_detour_wp'] = None
					else:
						order['move_position'] = _det_wp
						order['face_position'] = _det_wp
						order['throttle_override'] = None
						order['wreck_cover_id'] = None
						if _cm_wd in ('reload_cover', 'punish_peek', 'bait_juke_out', 'bait_juke_back', 'engage'):
							order['combat_mode'] = 'advance_contact'
				else:
					_mp_w = order.get('move_position') or position
					_dir_x = float(_mp_w[0]) - float(position[0])
					_dir_z = float(_mp_w[2]) - float(position[2])
					_dir_l = math.hypot(_dir_x, _dir_z)
					if _dir_l < 1.0 and contact is not None and contact.get('position') is not None:
						_dir_x = float(contact['position'][0]) - float(position[0])
						_dir_z = float(contact['position'][2]) - float(position[2])
						_dir_l = math.hypot(_dir_x, _dir_z) or 1.0
					elif _dir_l < 1.0:
						_dir_l = 1.0
					_near_wrecks = []
					_graveyard_wrecks = 0
					for _w_item in _wrecks_dict.values():
						_wp_i = _w_item.get('position')
						if _wp_i is not None:
							_wd = _distance_2d(position, _wp_i)
							if _wd <= 20.0:
								_graveyard_wrecks += 1
							if _wd <= 15.5:
								_wx = float(_wp_i[0]) - float(position[0])
								_wz = float(_wp_i[2]) - float(position[2])
								if _wd <= 8.5 or ((_wx * _dir_x + _wz * _dir_z) / (_wd * _dir_l)) >= 0.15:
									_near_wrecks.append(_wp_i)
					_trying_to_move = (
						(_mp_w is not None and _distance_2d(position, _mp_w) > 4.5 and order.get('throttle_override') != 0.0) or
						(_graveyard_wrecks >= 2 and not _has_dl_wd)
					)
					if _trying_to_move and _near_wrecks:
						_w_anc = agent.get('_wreck_prog_anchor')
						_w_since = float(agent.get('_wreck_prog_since', now) or now)
						if _w_anc is None or _distance_2d(position, _w_anc) >= 3.8:
							agent['_wreck_prog_anchor'] = tuple(position)
							agent['_wreck_prog_since'] = float(now)
						elif (now - _w_since) >= (3.8 if _graveyard_wrecks >= 2 else 5.2):
							agent['_wreck_prog_anchor'] = tuple(position)
							agent['_wreck_prog_since'] = float(now)
							_stall_cnt = int(agent.get('_wreck_stall_count', 0) or 0) + 1
							agent['_wreck_stall_count'] = _stall_cnt
							# Clear latched cover/flank/spread positions that are trapping the bot in the wreck graveyard!
							agent['_pk_cache'] = None
							agent.pop('_latched_flank_pos', None)
							agent.pop('_latched_spread_pos', None)
							agent.pop('_latched_rcov_pos', None)
							_grid_w = _real_cover_grid()
							if _grid_w is not None and hasattr(_grid_w, 'remember_failed_segment'):
								for _wpt_f in _near_wrecks:
									_grid_w.remember_failed_segment(position, _wpt_f, now, ttl=38.0, penalty=380.0)
							# 1) First try an immediate walkable lateral/diagonal gap around the wreck cluster
							_base_yw = math.atan2(_dir_x, _dir_z)
							for _ang_d in (0.65, -0.65, 1.05, -1.05, 1.45, -1.45, 2.05, -2.05, 2.65, -2.65):
								for _dist_d in (15.0, 23.0):
									_cyw = _base_yw + _ang_d
									_cpt = (float(position[0]) + math.sin(_cyw) * _dist_d, float(position[1]), float(position[2]) + math.cos(_cyw) * _dist_d)
									if _grid_w is not None and hasattr(_grid_w, 'snap_to_safe_baked_point'):
										_cpt_s = _grid_w.snap_to_safe_baked_point(_cpt, 4)
										if _cpt_s is None:
											continue
										_cpt = _cpt_s
									if self._is_hazard_point(_cpt, 2) or self._is_water_or_cliff_chord(position, _cpt) or self._is_chord_impassable(position, _cpt):
										continue
									_clear_of_wrecks = True
									for _wpt in _near_wrecks:
										if _distance_2d(_cpt, _wpt) < 6.2:
											_clear_of_wrecks = False
											break
									if _clear_of_wrecks:
										agent['_wreck_detour_wp'] = _cpt
										agent['_wreck_detour_until'] = float(now) + 6.5
										order['move_position'] = _cpt
										order['face_position'] = _cpt
										order['throttle_override'] = None
										order['wreck_cover_id'] = None
										if _cm_wd in ('reload_cover', 'punish_peek', 'bait_juke_out', 'bait_juke_back', 'engage'):
											order['combat_mode'] = 'advance_contact'
										break
								if agent.get('_wreck_detour_wp') is not None:
									break
							# 2) If >= 2 wrecks form a barrier (or stalled >= 2 times) and 16s cooldown elapsed, switch route!
							_last_w_sw = float(agent.get('_last_wreck_route_switch_at', -999.0) or -999.0)
							if (len(_near_wrecks) >= 2 or _graveyard_wrecks >= 2 or (agent.get('_wreck_detour_wp') is None and _stall_cnt >= 2)) and (now - _last_w_sw) >= 16.0:
									_cur_rid = (agent.get('route') or {}).get('id')
									_blocked_rids = agent.get('_wreck_blocked_routes')
									if not isinstance(_blocked_rids, set):
										_blocked_rids = set()
										agent['_wreck_blocked_routes'] = _blocked_rids
									if _cur_rid:
										_blocked_rids.add(_cur_rid)
									_cand_routes = [r for r in (self._routes_for(agent.get('team', 1)) or ()) if r.get('id') and r.get('id') not in _blocked_rids and r.get('id') != 'rear_guard' and r.get('waypoints')]
									if _cand_routes:
										_best_r = None
										_best_r_d = 1e9
										for _cr in _cand_routes:
											for _wp_c in (_cr.get('waypoints') or ()):
												_wpt_cand = (float(_wp_c[0]), float(position[1]), float(_wp_c[1]))
												if self._is_mountain_blocked(position, _wpt_cand) or self._is_chord_impassable(position, _wpt_cand):
													continue
												_d_c = math.hypot(float(_wp_c[0]) - float(position[0]), float(_wp_c[1]) - float(position[2]))
												if _d_c < _best_r_d:
													_best_r_d = _d_c
													_best_r = _cr
										if _best_r is not None:
											_oriented_r = route_toward_enemy(_best_r, agent.get('team', 1), self.bases)
											agent['route'] = _oriented_r
											agent['_last_wreck_route_switch_at'] = float(now)
											_nwps = _oriented_r.get('waypoints') or ()
											if _nwps:
												_reachable_nwps = [i for i in range(len(_nwps)) if not self._is_mountain_blocked(position, (float(_nwps[i][0]), float(position[1]), float(_nwps[i][1]))) and not self._is_chord_impassable(position, (float(_nwps[i][0]), float(position[1]), float(_nwps[i][1])))]
												if _reachable_nwps:
													_b_idx = min(_reachable_nwps, key=lambda i: math.hypot(float(_nwps[i][0]) - float(position[0]), float(_nwps[i][1]) - float(position[2])))
													agent['waypoint_index'] = min(len(_nwps) - 1, _b_idx + 1)
													_wp_sel = _nwps[agent['waypoint_index']]
													_det_cand = (float(_wp_sel[0]), float(position[1]), float(_wp_sel[1]))
													agent['_wreck_detour_wp'] = _det_cand
													agent['_wreck_detour_until'] = float(now) + 12.0
													order['move_position'] = _det_cand
													order['route_id'] = _best_r.get('id')
													_offh_ai_log_note('WRECK_BARRIER_DETOUR bot=%s switched route %s -> %s wrecks=%d' % (agent.get('id'), _cur_rid, _best_r.get('id'), len(_near_wrecks)))
										else:
											_cur_wps = (agent.get('route') or {}).get('waypoints') or ()
											_cur_idx = int(agent.get('waypoint_index', 0) or 0)
											if _cur_wps and _cur_idx > 0:
												_back_idx = max(0, _cur_idx - 1)
												_wp_back = _cur_wps[_back_idx]
												_det_cand = (float(_wp_back[0]), float(position[1]), float(_wp_back[1]))
												agent['_wreck_detour_wp'] = _det_cand
												agent['_wreck_detour_until'] = float(now) + 9.5
												order['move_position'] = _det_cand
												agent['waypoint_index'] = _back_idx
												_offh_ai_log_note('WRECK_BARRIER_CANYON_RETREAT bot=%s backing out of gorge wp=%d -> wp=%d' % (agent.get('id'), _cur_idx, _back_idx))
						else:
							agent['_wreck_prog_anchor'] = None
							agent['_wreck_stall_count'] = 0
		except Exception:
			pass

		# v8.7 Stateless Combat-Line Echelon Spacing (prevents 6-8 heavies/TDs/mediums from bunching into a tight herd on a ridge):
		# Compares only against actual current positions of living teammates (never mutated last_order), so it never oscillates!
		try:
			_cm_sp = str(order.get('combat_mode', 'route'))
			_rid_sp = (agent.get('route') or {}).get('id')
			if _rid_sp not in ('lake_road', 'bridge', 'east_bridge', 'center_bridge') and not self._is_hazard_point(position) and _cm_sp not in ('route', 'advance', 'artillery_hold', 'artillery_fire', 'cliff_dive_ram', 'airborne_jump_shot', 'crest_depress_peek', 'circle_of_death', 'defender_shield', 'lone_wolf_retreat', 'wolfpack_encircle', 'arty_evade_relocate', 'ram_brawl', 'clinch', 'reload_cover', 'punish_peek', 'punish_push', 'take_cover', 'cover_return', 'withdraw', 'squad_defend_base') and order.get('throttle_override') is None:
				_mp_sp = order.get('move_position') or position
				_my_id_sp = int(agent.get('id', 0) or 0)
				_my_tm_sp = agent.get('team')
				_dist_to_mp = _distance_2d(position, _mp_sp)
				if _dist_to_mp <= 75.0:
					_rep_x, _rep_z = 0.0, 0.0
					_close_allies = 0
					for _oa_sp in self.agents.values():
						if _oa_sp['id'] == _my_id_sp or _oa_sp.get('team') != _my_tm_sp or _oa_sp.get('health_fraction', 1.0) <= 0.0:
							continue
						_op_sp = _oa_sp.get('position')
						if _op_sp is None:
							continue
						_adx = float(_mp_sp[0]) - float(_op_sp[0])
						_adz = float(_mp_sp[2]) - float(_op_sp[2])
						_ad2 = _adx * _adx + _adz * _adz
						if _ad2 < 15.0 * 15.0:
							_close_allies += 1
							if _ad2 > 0.25:
								_adl = math.sqrt(_ad2)
								_w_sp = (15.0 - _adl) / 15.0
								_rep_x += (_adx / _adl) * _w_sp
								_rep_z += (_adz / _adl) * _w_sp
							else:
								_ang_sp = (_my_id_sp * 2.39996) % (2.0 * math.pi)
								_rep_x += math.sin(_ang_sp)
								_rep_z += math.cos(_ang_sp)
					if _close_allies > 0 and (_rep_x * _rep_x + _rep_z * _rep_z) > 1e-4:
						# If stopped in a clump with a lower-ID teammate (< 11.5m), un-park and step into echelon formation!
						if order.get('throttle_override') == 0.0 and _close_allies >= 1:
							for _oa_pk in self.agents.values():
								if _oa_pk['id'] < _my_id_sp and _oa_pk.get('team') == _my_tm_sp and _oa_pk.get('health_fraction', 1.0) > 0.0:
									_op_pk = _oa_pk.get('position')
									if _op_pk is not None and _distance_2d(position, _op_pk) < 11.5:
										order['throttle_override'] = None
										break
						if order.get('throttle_override') != 0.0:
							_rl_sp = math.sqrt(_rep_x * _rep_x + _rep_z * _rep_z)
							_push_d = min(16.0, 9.5 + _close_allies * 3.0)
							_ech_cand = (
								float(_mp_sp[0]) + (_rep_x / _rl_sp) * _push_d,
								float(_mp_sp[1]),
								float(_mp_sp[2]) + (_rep_z / _rl_sp) * _push_d,
							)
							if not self._is_hazard_point(_ech_cand, 2) and not self._is_water_or_cliff_chord(position, _ech_cand) and not self._is_chord_impassable(position, _ech_cand):
								order['move_position'] = _ech_cand
		except Exception:
			pass

		# v12.5 Base Capture Circle Retention ("Точки захвата баз" - 50.0m stock circle):
		# Once a non-SPG bot reaches the enemy base (<= 45m from _eb_cap), keep it inside the capture circle
		# (at an 8..22m capture slot) instead of letting echelon spacing, weaving, or distant contacts pull it out!
		_eb_cap_f = self.bases.get(2 if agent.get('team') == 1 else 1)
		_is_dom_f = (self.bases.get(1) is not None and self.bases.get(1) == self.bases.get(2))
		_r_id_f = str((agent.get('route') or {}).get('id', ''))
		_d_eb_f = _distance_2d(position, _eb_cap_f) if _eb_cap_f is not None else 999.0
		if _d_eb_f > 36.0:
			agent['_is_capturing_base'] = False
		_can_cap_f = (
			profile.get('class_tag') != 'SPG' and
			order.get('combat_mode') != 'squad_defend_base' and
			_eb_cap_f is not None and
			_d_eb_f <= 45.0 and
			self._should_bot_capture_base(agent, position, now) and
			(not _is_dom_f or 'cap' in _r_id_f or 'base' in _r_id_f or _d_eb_f <= 34.0))
		# Redirect excess K/D Hunters BEFORE they step into the 30m capture circle (at 32..65m out)!
		if not _can_cap_f and profile.get('class_tag') != 'SPG' and _eb_cap_f is not None and _d_eb_f <= 65.0:
			agent['route_completed'] = True
			if contact is None:
				_kd_out = self._kd_hunt_objective(agent, position, now)
				if _kd_out is not None:
					order['move_position'] = self._convergence_position(agent, position, _kd_out)
					order['face_position'] = _kd_out
					order['throttle_override'] = None
					order['combat_mode'] = 'converge'
		if _can_cap_f:
			agent['route_completed'] = True
			if _d_eb_f <= 30.0:
				agent['_is_capturing_base'] = True
			_aid_cp = int(agent.get('id', 0) or 0)
			_ang_cp = (_aid_cp * 1.947787) % (2.0 * math.pi)
			_rad_cp = 5.5 + float(_aid_cp % 3) * 2.5
			_cap_slot = (
				float(_eb_cap_f[0]) + math.cos(_ang_cp) * _rad_cp,
				float(position[1]),
				float(_eb_cap_f[1]) + math.sin(_ang_cp) * _rad_cp)
			_grid_cp = _real_cover_grid()
			if _grid_cp is not None and hasattr(_grid_cp, 'snap_to_safe_baked_point'):
				_sn_cp = _grid_cp.snap_to_safe_baked_point(_cap_slot, 3)
				if _sn_cp is not None and 3.5 <= _distance_2d(_sn_cp, _eb_cap_f) <= 15.0:
					_cap_slot = _sn_cp
			if _d_eb_f <= 16.0 and _distance_2d(position, _cap_slot) <= 6.0:
				order['move_position'] = tuple(position)
				order['throttle_override'] = 0.0
			else:
				order['move_position'] = _cap_slot
				order['throttle_override'] = None
			if contact is not None and contact.get('visible'):
				order['aim_position'] = contact['position']
				order['face_position'] = self._angled_face_position(agent, position, contact['position'], now=now)
			else:
				order['target_id'] = None
				order['fire_allowed'] = False
				order['face_position'] = (0.0, float(position[1]), 0.0)
				order['aim_position'] = (0.0, float(position[1]), 0.0)
			order['combat_mode'] = 'capture_base'

		# Clear target_id whenever the bot is merely driving to move_position without aiming at an enemy!
		if not order.get('fire_allowed') and order.get('aim_position') == order.get('move_position') and order.get('combat_mode') in ('route', 'investigate', 'converge', 'advance_contact'):
			order['target_id'] = None

		# v4.75 Active light tank evasive weaving (мансование змейкой)
		if order.get('combat_mode') not in ('artillery_hold', 'artillery_fire', 'cliff_dive_ram', 'circle_of_death', 'capture_base') and order.get('throttle_override') != 0.0 and not _can_cap_f:
			order['move_position'] = self._apply_light_tank_weaving(
				agent, position, order.get('move_position'), now)


		try:
			_mp = order.get('move_position')
			if _mp is not None and _mp != tuple(position) and order.get('throttle_override') != 0.0 and order.get('combat_mode') != 'artillery_hold':
				_grid = _real_cover_grid()
				if _mp != route_position and _grid is not None and hasattr(_grid, 'snap_to_safe_baked_point'):
					_mp_c = _grid.cell_for(_mp)
					_mp_i = _grid._baked_index(_mp_c)
					if _mp_i is None or not _grid._baked_valid[_mp_i] or _grid.baked_hazard_near(_mp, 1):
						_safe_mp = _grid.snap_to_safe_baked_point(_mp, 6)
						if _safe_mp is not None:
							order['move_position'] = _safe_mp
						else:
							order['move_position'] = route_position
				_mp_chk = order.get('move_position')
				if (_mp_chk is not None and _mp_chk != tuple(position) and
						_distance_2d(position, _mp_chk) <= 95.0 and self._is_water_or_cliff_chord(position, _mp_chk)):
					if not agent.get('route_completed') and route_position is not None and not self._is_water_or_cliff_chord(position, route_position):
						order['move_position'] = route_position
					elif _grid is not None and hasattr(_grid, 'clamp_to_safe_corridor'):
						_clamped_w = _grid.clamp_to_safe_corridor(position, _mp_chk)
						if _clamped_w is not None and not self._is_water_or_cliff_chord(position, _clamped_w) and not self._is_hazard_point(_clamped_w, 1):
							order['move_position'] = _clamped_w
						elif _distance_2d(position, _mp_chk) <= 28.0:
							order['move_position'] = tuple(position)
							order['throttle_override'] = 0.0
		except Exception:
			pass
		# v12.0 Combat Order Position Hysteresis: prevent sub-second 1-5m target jitter in the same combat mode!
		try:
			_prev_ord = agent.get('last_order')
			_prev_ord_t = float(agent.get('_last_order_move_at', -999.0) or -999.0)
			if (isinstance(_prev_ord, dict) and contact is not None and
					order.get('combat_mode') == _prev_ord.get('combat_mode') and
					order.get('combat_mode') not in ('route', 'advance', 'circle_of_death', 'ram_brawl', 'clinch') and
					(now - _prev_ord_t) < 1.35):
				_pmp = _prev_ord.get('move_position')
				_nmp = order.get('move_position')
				if _pmp is not None and _nmp is not None and 0.1 < _distance_2d(_pmp, _nmp) <= 6.0:
					order['move_position'] = _pmp
			else:
				agent['_last_order_move_at'] = now
		except Exception:
			pass
		try:
			_mv_final = order.get('move_position')
			if (_mv_final is not None and position is not None and
					order.get('combat_mode') not in ('route', 'advance', 'converge', 'advance_contact', 'squad_defend_base', 'capture_base') and
					_distance_2d(position, _mv_final) > 1.5):
				_grid_clamp = _real_cover_grid()
				if _grid_clamp is not None and hasattr(_grid_clamp, 'clamp_to_safe_corridor'):
					order['move_position'] = _grid_clamp.clamp_to_safe_corridor(position, _mv_final)
			# v12.5 Anti-Stall Mid-Route Waypoint Guard:
			# If a bot on 'route' has move_position within 6.5m of position before completing its route,
			# advance waypoint_index immediately so it can NEVER stall in driver_arrived mid-route!
			if (order.get('combat_mode') == 'route' and not agent.get('route_completed') and
					order.get('move_position') is not None and _distance_2d(position, order['move_position']) <= 6.5):
				_rt_cur = agent.get('route') or {}
				_wps_cur = _rt_cur.get('waypoints') or ()
				_wi_cur = int(agent.get('waypoint_index', 0) or 0)
				if _wi_cur + 1 < len(_wps_cur):
					agent['waypoint_index'] = _wi_cur + 1
					route_position = self._route_position(agent, position, now, hull_yaw)
					order['move_position'] = route_position
					order['route_index'] = int(agent.get('waypoint_index', 0))
					order['route_anchor'] = self._route_anchor(agent, position)
					if order.get('target_id') is None:
						order['face_position'] = route_position
						order['aim_position'] = route_position
		except Exception:
			pass
		# v15.0 Populate 3D Trajectory Plan (plan_waypoints), Tactic Selection Reason, and Action Description for Debug & 3D Radar
		try:
			_cm_fin = str(order.get('combat_mode', 'route') or 'route')
			_rt_obj = agent.get('route') or {}
			_rt_id_fin = str(_rt_obj.get('id', 'direct') or 'direct')
			_rt_wps_fin = _rt_obj.get('waypoints') or ()
			_wp_idx_fin = int(agent.get('waypoint_index', 0) or 0)
			if not order.get('plan_waypoints'):
				_pw_list = []
				_mp_f0 = order.get('move_position')
				_grid_pw = _real_cover_grid()
				if _mp_f0 is not None and _distance_2d(position, _mp_f0) > 2.5:
					if _grid_pw is not None and hasattr(_grid_pw, 'trace_terrain_path') and _distance_2d(position, _mp_f0) > 20.0:
						for _tp_w in (_grid_pw.trace_terrain_path(position, _mp_f0, 6) or ()):
							_pw_list.append((round(float(_tp_w[0]), 1), round(float(_tp_w[1]), 1), round(float(_tp_w[2]), 1)))
					if not _pw_list:
						_pw_list.append((round(float(_mp_f0[0]), 1), round(float(_mp_f0[1]), 1), round(float(_mp_f0[2]), 1)))
				if _cm_fin == 'route' and _rt_wps_fin and not agent.get('route_completed'):
					for _k_w in range(_wp_idx_fin + 1, min(len(_rt_wps_fin), _wp_idx_fin + 6)):
						_w_item_f = _rt_wps_fin[_k_w]
						_wy_f = self._ground_height(float(_w_item_f[0]), float(_w_item_f[1]), float(position[1]))
						_w_pt_f = (round(float(_w_item_f[0]), 1), round(float(_wy_f), 1), round(float(_w_item_f[1]), 1))
						if not _pw_list or _distance_2d(_pw_list[-1], _w_pt_f) > 6.0:
							_pw_list.append(_w_pt_f)
				order['plan_waypoints'] = _pw_list
			if not order.get('tactic_reason'):
				if contact is not None:
					_c_d_fin = _distance_2d(position, contact['position'])
					_c_id_fin = contact.get('id')
					if _cm_fin == 'flank':
						order['tactic_reason'] = '3D Flank (d=%.0fm до #%s, обход с учётом маршрута %s)' % (_c_d_fin, str(_c_id_fin), _rt_id_fin)
					elif _cm_fin == 'punish_peek':
						order['tactic_reason'] = '3D Peek-a-Boo (d=%.0fm до #%s, 3D-прострел из укрытия)' % (_c_d_fin, str(_c_id_fin))
					elif _cm_fin == 'punish_push':
						order['tactic_reason'] = '3D Reload Punish Push (враг #%s на КД, d=%.0fm)' % (str(_c_id_fin), _c_d_fin)
					elif _cm_fin == 'reload_cover':
						order['tactic_reason'] = '3D Cover Pullback (откат в 3D-тень на КД от #%s)' % str(_c_id_fin)
					elif _cm_fin in ('ram_brawl', 'clinch'):
						order['tactic_reason'] = 'Close Brawl / Clinch (d=%.0fm до #%s)' % (_c_d_fin, str(_c_id_fin))
					elif _cm_fin == 'advance_contact':
						order['tactic_reason'] = '3D Advance to Contact (сближение с #%s по коридору %s, d=%.0fm)' % (str(_c_id_fin), _rt_id_fin, _c_d_fin)
					else:
						order['tactic_reason'] = '3D Combat (%s по цели #%s, d=%.0fm)' % (_cm_fin, str(_c_id_fin), _c_d_fin)
				else:
					order['tactic_reason'] = 'Навигация по маршруту %s (точка %d/%d)' % (_rt_id_fin, min(_wp_idx_fin + 1, max(1, len(_rt_wps_fin))), max(1, len(_rt_wps_fin)))
			if not order.get('action_desc'):
				_mp_act = order.get('move_position') or position
				_fire_str = 'огонь разрешён' if order.get('fire_allowed') else 'сближение/манёвр'
				order['action_desc'] = 'Ход -> (%.0f, %.0f) [%s, %s]' % (float(_mp_act[0]), float(_mp_act[2]), _cm_fin, _fire_str)

			# Emit structured [TACTIC_PLAN] debug log on mode/target change or LT/MT dynamic plan update (throttled to 4.0s per bot)
			_prev_log_cm = agent.get('_last_logged_tactic_mode')
			_prev_log_tid = agent.get('_last_logged_tactic_tid')
			_prev_log_at = float(agent.get('_last_logged_tactic_at', -999.0) or -999.0)
			_cur_tid_log = order.get('target_id')
			_is_lt_mt_dyn = (_cm_fin in ('circle_of_death', 'flank', 'wolfpack_encircle') and profile.get('class_tag') in ('lightTank', 'mediumTank'))
			if ((_cm_fin != _prev_log_cm or _cur_tid_log != _prev_log_tid) and (now - _prev_log_at) >= 2.2) or (_is_lt_mt_dyn and (now - _prev_log_at) >= 5.0):
				agent['_last_logged_tactic_mode'] = _cm_fin
				agent['_last_logged_tactic_tid'] = _cur_tid_log
				agent['_last_logged_tactic_at'] = float(now)
				_pw_str = ' -> '.join('(%.0f,%.0f)' % (float(p[0]), float(p[2])) for p in (order.get('plan_waypoints') or ())[:4])
				_offh_ai_log_note(
					'TACTIC_PLAN bot=%s (%s) mode=%s route=%s[%d] tgt=%s | WHY: %s | PLAN: %s | ACT: %s' % (
						str(agent.get('id')), str(profile.get('class_tag', 'Tank')), _cm_fin, _rt_id_fin, _wp_idx_fin,
						str(_cur_tid_log), str(order.get('tactic_reason', '')), _pw_str or 'hold', str(order.get('action_desc', ''))))
		except Exception:
			pass
		agent['last_order'] = order
		return order

	def _apply_squad_order(self, agent, order, position, now):
		"""Blend the squad leader's rush/fallback call into one member's order.

		A squad call is a lean, not a leash: it nudges move_position and can
		soften a solo withdraw into a hold if the squad as a whole is still
		winning, but it never fabricates a target or authorizes a shot the
		member's own _choose_contact did not already find. Weighted by
		teamwork so a lone-wolf personality mostly ignores its leader, the
		same knob the rest of this module already uses for focus-fire
		deference.
		"""
		if (agent.get('profile') or {}).get('class_tag') == 'SPG' or order.get('combat_mode') in ('artillery_hold', 'artillery_fire', 'artillery_deploy', 'artillery_reposition'):
			return
		squad = self._squad_for(agent)
		if squad is None:
			return
		self._promote_squad_leader(squad, now)
		squad_order = self._squad_order(squad, now)
		if (self._squad_cohesion_on() and
				order.get('combat_mode') in (('route',) if _fight_fix_on() else ('route', 'advance_contact'))):
			# only while travelling - a fight is never overridden.
			help_req = squad.get('help_request')
			leader = self.agents.get(squad.get('leader_id'))
			if help_req is not None and now < float(help_req.get('until', 0.0)):
				hp = help_req['pos']
				if not self._is_chord_impassable(position, (hp[0], position[1], hp[2])) and _distance_2d(position, hp) <= 165.0:
					order['move_position'] = (hp[0], position[1], hp[2])
					order['combat_mode'] = 'squad_help'
					return
			if (False and leader is not None and leader['id'] != agent['id'] and
					leader.get('position') is not None and
					leader.get('health_fraction', 1.0) > 0.0 and
					90.0 < _distance_2d(position, leader['position']) <= 170.0 and
					not self._is_chord_impassable(position, leader['position'])):
				members = squad.get('members', ())
				try:
					slot = members.index(agent['id'])
				except ValueError:
					slot = 1
				lp = leader['position']
				side = 1.0 if (slot % 2) else -1.0
				lat = 14.0 * ((slot + 1) // 2) * side
				dx = position[0] - lp[0]
				dz = position[2] - lp[2]
				dl = max(0.1, math.sqrt(dx * dx + dz * dz))
				order['move_position'] = (lp[0] + dx / dl * 20.0 - dz / dl * lat, position[1],
					lp[2] + dz / dl * 20.0 + dx / dl * lat)
				order['combat_mode'] = 'squad_regroup'
				log_at = getattr(self, '_squad_regroup_log_at', 0.0)
				if now - log_at >= 10.0:
					self._squad_regroup_log_at = now
					_offh_ai_log_note('SQUAD_REGROUP bot=%s squad=%s dist=%.0f' % (
						str(agent['id']), str(squad.get('id')), dl))
				return
		# v12.0 Team Alive-Roster Awareness, Base Defense Coin-Flip, and Last-Man-Standing:
		my_t = agent.get('team')
		_alive_lt_mt = 0
		_alive_ht_td = 0
		_alive_non_spg = 0
		_alive_enemies = 0
		for _ag_t in self.agents.values():
			if _ag_t.get('health_fraction', 0.0) <= 0.0 or _ag_t.get('hp_abs', 1.0) <= 0.0:
				continue
			_ct_t = (_ag_t.get('profile') or {}).get('class_tag', 'mediumTank')
			if _ag_t.get('team') == my_t:
				if _ct_t != 'SPG':
					_alive_non_spg += 1
					if _ct_t in ('lightTank', 'mediumTank'):
						_alive_lt_mt += 1
					else:
						_alive_ht_td += 1
			else:
				_alive_enemies += 1
		if getattr(self, 'player_team', None) == my_t and _number(getattr(self, 'player_hp_frac', 0.0), 0.0) > 0.0:
			_alive_non_spg += 1

		# Last Man Standing (1 non-SPG survivor on team against 2+ enemies after 45s):
		# Cannot split itself in two -> deterministic coin-flip between desperate all-in push/decap vs doomed hold to the last shell!
		if now >= 45.0 and _alive_non_spg == 1 and _alive_enemies >= 2:
			_lms_coin = (int(agent.get('seed', 0) or 0) + int(agent.get('id', 0) or 0)) & 1
			if _lms_coin == 1:
				order['throttle_override'] = 1.0
				order['combat_mode'] = 'last_stand_desperate'
			else:
				if order.get('target_id') is not None and order.get('fire_allowed'):
					order['move_position'] = tuple(position)
					order['throttle_override'] = 0.0
				order['combat_mode'] = 'last_stand_doomed_hold'
			return

		# Base defense is handled deterministically by _update_base_defenders & _apply_base_defense_order (1-2 tanks).

		if squad_order is None or squad_order.get('mode') is None:
			return
		personality = agent['personality']
		deference = personality['teamwork']
		if agent['id'] == squad_order.get('leader_id'):
			deference = max(deference, 0.75)
		if deference < 0.30:
			return
		if squad_order['mode'] == 'squad_hold_hill':
			# Only converge on the hill point while travelling on route without an active combat target,
			# and NEVER pull a bot out of the enemy base capture circle!
			_eb_sq = self.bases.get(2 if agent.get('team') == 1 else 1)
			if _eb_sq is not None and _distance_2d(position, _eb_sq) <= 45.0:
				return
			hill_point = squad_order.get('hill_point')
			if hill_point is not None and order.get('target_id') is None and order.get('combat_mode') in ('route', 'converge') and bool(agent.get('route_completed')):
				members = squad.get('members', ())
				try:
					slot = members.index(agent['id'])
				except ValueError:
					slot = 0
				angle = (2.0 * math.pi * slot) / max(1, len(members))
				dist_r = 14.0 + (slot % 3) * 5.0
				hold_x = hill_point[0] + math.cos(angle) * dist_r
				hold_z = hill_point[1] + math.sin(angle) * dist_r
				hold_position = (hold_x, position[1], hold_z)
				order['move_position'] = hold_position
				order['face_position'] = (hold_x + math.cos(angle) * 60.0, position[1], hold_z + math.sin(angle) * 60.0)
				order['combat_mode'] = 'squad_hold_hill'
			return
		if squad_order['mode'] == 'squad_defend_lane':
			return
		if squad_order['mode'] == 'squad_fallback':
			# Never freeze advancing/fighting squad members at tuple(position) ('squad_hold')!
			pass
		elif (squad_order['mode'] == 'squad_rush' and
				order.get('combat_mode') in ('route', 'advance_contact') and
				deference >= 0.45):
			leader = self.agents.get(squad_order.get('leader_id'))
			leader_target = None
			if leader is not None and leader.get('target_id') is not None:
				leader_target = self.contacts.get(agent['team'], {}).get(
					leader['target_id'])
			if (leader_target is not None and leader_target.get('health', 0.0) > 0.0 and
					_distance_2d(position, leader_target['position']) <= 165.0 and
					not self._is_chord_impassable(position, leader_target['position'])):
				if agent.get('squad_role') == 'rear_guard':
					rush_target = leader_target['position']
					dx = rush_target[0] - position[0]
					dz = rush_target[2] - position[2]
					dist = math.sqrt(dx * dx + dz * dz)
					side_sign = 1.0 if (agent['id'] % 2 == 0) else -1.0
					if dist > 35.0:
						guard_position = (
							position[0] + dx * 0.35 - dz * 0.15 * side_sign,
							position[1],
							position[2] + dz * 0.35 + dx * 0.15 * side_sign,
						)
						_g_grid = _real_cover_grid()
						if _g_grid is not None and hasattr(_g_grid, 'snap_to_safe_baked_point'):
							guard_position = _g_grid.snap_to_safe_baked_point(guard_position, 10) or tuple(position)
					else:
						guard_position = tuple(position)
					order['move_position'] = guard_position
					order['face_position'] = rush_target
					order['combat_mode'] = 'squad_rush_guard'
					try:
						_guard_log_at = getattr(
							self, '_offh_squad_guard_log_at', None)
						if _guard_log_at is None:
							_guard_log_at = {}
							self._offh_squad_guard_log_at = _guard_log_at
						_last_guard_log = float(_guard_log_at.get(agent['id'], 0.0))
						if now - _last_guard_log >= 60.0:
							_guard_log_at[agent['id']] = now
							_offh_ai_log_note(
								'SQUAD_RUSH_GUARD bot=%s squad=%s rear-guarding rush, '
								'guard_pos=(%.1f,%.1f) rush_target=(%.1f,%.1f)' % (
									str(agent['id']), str(squad.get('id')),
									guard_position[0], guard_position[2],
									rush_target[0], rush_target[2]))
					except Exception:
						pass
				else:
					members = squad.get('members', ())
					try:
						slot = members.index(agent['id'])
					except ValueError:
						slot = 0
					rush_target = leader_target['position']
					if len(members) > 1 and slot > 0:
						dx = rush_target[0] - position[0]
						dz = rush_target[2] - position[2]
						dist = math.sqrt(dx * dx + dz * dz)
						if dist > 0.1:
							side_sign = 1.0 if (slot % 2 == 1) else -1.0
							slot_spread = 6.0 * ((slot + 1) // 2)
							norm_x = -dz / dist
							norm_z = dx / dist
							order['move_position'] = (
								rush_target[0] + norm_x * slot_spread * side_sign,
								position[1],
								rush_target[2] + norm_z * slot_spread * side_sign)
						else:
							order['move_position'] = rush_target
					else:
						order['move_position'] = rush_target
					order['combat_mode'] = 'squad_rush'
			else:
				squad['order'] = None

	def _apply_radio_order(self, agent, order, position, now):
		"""Overlay one admitted player radio command on top of everything
		order_for and _apply_squad_order already decided.

		Deliberately similar in spirit to a squad call - a command steers
		move/face/aim and can wave off the ordinary route, but RADIO_PROTECTED_
		MODES keeps it from cancelling a mode this module considers a hard
		local decision (see the call site: low-health withdraw is checked
		before this ever runs, so it never even reaches here).
		"""
		radio = self._radio_order_for(agent, now)
		if radio is None or order.get('combat_mode') in RADIO_PROTECTED_MODES:
			return
		command = radio['command']
		if (self._radio_soft() and command not in RADIO_TARGET_COMMANDS and
				order.get('target_id') is not None and order.get('fire_allowed', False)):
			return  # a real player finishes his own fight first
		if command == RADIO_COMMAND_STOP:
			order['move_position'] = tuple(position)
			order['throttle_override'] = 0.0
			order['combat_mode'] = 'radio_stop'
		elif command == RADIO_COMMAND_ATTACK:
			target = radio.get('position')
			if target is not None:
				order['move_position'] = target
				order['face_position'] = target
				order['throttle_override'] = None
				order['combat_mode'] = 'radio_attack'
		elif command == RADIO_COMMAND_TURNBACK:
			anchor = order.get('route_anchor') or self._route_anchor(agent, position)
			order['move_position'] = anchor
			order['face_position'] = anchor
			order['throttle_override'] = None
			order['combat_mode'] = 'radio_turnback'
		elif command == RADIO_COMMAND_BACKTOBASE:
			base = self.bases.get(agent['team'])
			if base is not None:
				home = (base[0], position[1], base[1])
				order['move_position'] = home
				order['face_position'] = home
				order['throttle_override'] = None
				order['combat_mode'] = 'radio_backtobase'
		elif command == RADIO_COMMAND_ATTENTION_TO_CELL:
			cell = radio.get('cell_position')
			if cell is not None:
				order['move_position'] = cell
				order['face_position'] = cell
				order['throttle_override'] = None
				order['combat_mode'] = 'radio_attention_cell'
		elif command in (RADIO_COMMAND_HELP_ME, RADIO_COMMAND_FOLLOW_ME,
				RADIO_COMMAND_HELP_ME_EX):
			issuer_id = radio.get('issuer_id')
			issuer_position = None
			if issuer_id is not None:
				issuer_agent = self.agents.get(issuer_id)
				if issuer_agent is not None:
					if issuer_agent.get('health_fraction', 1.0) <= 0.0:
						return
					if issuer_agent.get('position') is not None:
						issuer_position = issuer_agent['position']
			if issuer_position is None and getattr(self, 'player_position', None) is not None:
				issuer_position = self.player_position
			if issuer_position is None:
				issuer_position = radio.get('position')
			if issuer_position is not None:
				# Fan recruited bots out behind the issuer AND strictly clamp to safe baked navgraph ground
				# so calling 'Follow Me!' on a narrow cliff/lake road NEVER projects the escort target into water!
				dx = position[0] - issuer_position[0]
				dz = position[2] - issuer_position[2]
				length = math.sqrt(dx * dx + dz * dz)
				side = 1.0 if (agent['seed'] & 1) else -1.0
				lateral = 0.0 if self._is_hazard_point(issuer_position) else (6.0 * side)
				back = 14.0
				if length > 0.1:
					dx /= length; dz /= length
				else:
					dx, dz = 0.0, -1.0
				point = (issuer_position[0] + dx * back - dz * lateral,
				         position[1],
				         issuer_position[2] + dz * back + dx * lateral)
				grid = _real_cover_grid()
				if grid is not None and hasattr(grid, 'snap_to_safe_baked_point'):
					safe_iss = grid.snap_to_safe_baked_point(issuer_position, 8) or tuple(issuer_position)
					safe_pt = grid.snap_to_safe_baked_point(point, 8)
					if safe_pt is None or self._is_chord_impassable(safe_iss, safe_pt):
						point = safe_iss
					else:
						point = safe_pt
				else:
					point = self._clamp_point(point)
				order['move_position'] = point
				if order.get('target_id') is None:
					order['face_position'] = point if length > 35.0 else issuer_position
				order['throttle_override'] = None
				order['combat_mode'] = 'radio_follow'
		elif command in RADIO_TARGET_COMMANDS:
			target_id = radio.get('target_id')
			contact = self.contacts.get(agent['team'], {}).get(target_id)
			if contact is not None and contact.get('visible') and _number(contact.get('health', 0.0), 0.0) > 0.0:
				order['target_id'] = contact['id']
				order['aim_position'] = contact['position']
				order['face_position'] = contact['position']
				order['fire_allowed'] = True
				_r_sids = contact.get('shootable_by_ids')
				if _r_sids is not None and int(agent['id']) not in _r_sids:
					order['move_position'] = self._spread_attack_position(agent, position, contact['position'])
					order['throttle_override'] = None
				order['combat_mode'] = ('radio_support_fire'
					if command == RADIO_COMMAND_SUPPORT_FIRE else 'radio_attack_enemy')

	def player_advisory(self, player_team, position, health, max_health, now,
			player_class=None):
		"""'Hold' / 'advance' read-out from the nearest bots, for the PLAYER.

		This never touches player input - offline_battle.py decides how to
		surface the returned dict (chat line, voice line, HUD marker). Call it
		once per tick with the player's own position/health; it returns None
		when no nearby bot has anything worth saying, or a dict:
		    {'mode': 'hold' | 'advance', 'speaker_id': bot_id, 'balance': f}
		'hold' means the nearest bots read the local fight as bad for a rush
		right now; 'advance' is the explicit all-clear once that reading
		flips - the whole point being that an 'advance' the player waits for
		is backed by the bots' own numbers, not a guess.

		player_class: one of MATCH_CLASSES ('heavyTank', 'mediumTank', 'AT-SPG',
		'lightTank', 'SPG'), or None if the caller does not know/care. A heavy
		with hitpoints to spare reads as a bigger, tankier chunk of `allies`
		than its raw health_fraction alone would give it - the same tank at
		half health is a very different proposition for a heavy (still a lot
		of effective HP and armour) than for a light (half of not much). At
		LOW health this inverts: a heavy that is already chewed up is telling
		the bots it just spent its armour advantage and, being slow, cannot
		disengage the way a light could - so a hurt heavy should get pulled
		toward 'hold'/fallback sooner than the plain balance number implies,
		not later. Lights/mediums are left at their plain health_fraction
		weight in both directions - the class effect is specifically about
		"tanky at range" reading as more durable than its HP bar shows, and a
		spent tank giving up that durability.
		"""
		player_team = int(player_team)
		try:
			if position is not None:
				self.player_position = (float(position[0]), float(position[1]), float(position[2]))
			self.player_team = player_team
			self.player_hp = _number(health, 0.0)
			self.player_hp_frac = _number(health, 1.0) / max(_number(max_health, 1.0), 1.0)
		except Exception:
			pass
		health_fraction = _number(health, 1.0) / max(_number(max_health, 1.0), 1.0)
		if health_fraction <= 0.0 or position is None:
			self.player_position = None
			return None
		nearby = [agent for agent in self.agents.values()
			if agent.get('team') == player_team and
			agent.get('health_fraction', 1.0) > 0.0 and
			agent.get('position') is not None and
			_distance_2d(position, agent['position']) <= ADVISORY_RADIUS]
		if not nearby:
			return None
		player_weight = health_fraction
		is_heavy_class = (player_class == 'heavyTank')
		if is_heavy_class:
			if health_fraction >= 0.35:
				# Still armoured, still a real brick to trade with - worth
				# more than a medium/light at the same fraction.
				player_weight = health_fraction * 1.35
			else:
				# Low HP on a heavy is not "a bit hurt", it is "the one thing
				# this class brings (soaking hits) is basically gone, and it
				# cannot run from a bad fight the way a light can" - weighted
				# DOWN below the plain fraction so the balance tips toward
				# 'hold'/fallback earlier than it would for any other class.
				player_weight = health_fraction * 0.6
		allies = player_weight
		for agent in nearby:
			allies += max(0.25, _number(agent.get('health_fraction', 1.0), 1.0))
		battle_start_adv = self.__dict__.setdefault('_adv_battle_start_at', float(now))
		if float(now) - float(battle_start_adv) < 35.0 or player_class == 'SPG':
			return None
		enemies = 0.0
		engaged_enemies_near = 0
		for contact in self._known_contacts(player_team, now):
			if _distance_2d(contact['position'], position) <= ADVISORY_RADIUS and not self._is_chord_impassable(position, contact['position']):
				enemies += max(0.3, contact['health'] / max(contact['max_health'], 1.0))
				if contact.get('visible'):
					engaged_enemies_near += 1
		total_alive_enemies = sum(
			1 for ag in self.agents.values()
			if ag.get('team') != player_team and _number(ag.get('health_fraction', 1.0), 1.0) > 0.0)
		balance = allies - enemies
		hold_threshold = ADVISORY_HOLD_BALANCE
		advance_threshold = ADVISORY_ADVANCE_BALANCE
		if is_heavy_class:
			if health_fraction < 0.35:
				# A beat-up heavy should hear "hold" a bit sooner (less
				# negative balance needed) and "advance" a bit later (needs a
				# clearer green light before it commits its last chunk of HP)
				# than the plain thresholds - it has less room for a bad
				# trade to still work out than the balance number alone says.
				hold_threshold = ADVISORY_HOLD_BALANCE + 0.35
				advance_threshold = ADVISORY_ADVANCE_BALANCE + 0.30
			else:
				# Full-health heavy: the bots are more willing to greenlight
				# a push, because this class can absorb a losing trade and
				# still come out even - "go give them hell" needs less
				# certainty than it would for a squishier class.
				advance_threshold = ADVISORY_ADVANCE_BALANCE - 0.20
		# v4.74 Check for impenetrable heavy target right in front of player
		heavy_threat = False
		for contact in self._known_contacts(player_team, now):
			if _distance_2d(contact['position'], position) <= 100.0:
				c_armor = _number(contact.get('armor', 0.0), 0.0)
				if c_armor >= 135.0 and player_class in ('lightTank', 'mediumTank', 'SPG'):
					heavy_threat = True
					break
				elif c_armor >= 180.0:
					heavy_threat = True
					break

		state = self.player_advisory_state
		if state is None:
			state = {'mode': None, 'set_at': 0.0, 'last_said_at': -1e9}
			self.player_advisory_state = state
		held_long_enough = (now - float(state.get('set_at', 0.0)) >=
			ADVISORY_MIN_HOLD_SECONDS)
		mode = state.get('mode')
		if heavy_threat and mode != 'cant_pen' and held_long_enough:
			mode = 'cant_pen'
			state['mode'] = mode
			state['set_at'] = now
		elif not heavy_threat and mode == 'cant_pen' and held_long_enough:
			mode = None
			state['mode'] = None
			state['set_at'] = now
		elif mode != 'hold' and engaged_enemies_near >= 1 and balance <= hold_threshold and held_long_enough:
			mode = 'hold'
			state['mode'] = mode
			state['set_at'] = now
		elif mode != 'advance_cleanup' and 0 < total_alive_enemies <= 4 and balance >= advance_threshold and held_long_enough:
			mode = 'advance_cleanup'
			state['mode'] = mode
			state['set_at'] = now
		elif mode != 'advance' and engaged_enemies_near >= 1 and enemies >= 0.6 and balance >= advance_threshold and held_long_enough:
			mode = 'advance'
			state['mode'] = mode
			state['set_at'] = now
		else:
			return None
		if mode is None:
			return None
		if now - float(state.get('last_said_at', -1e9)) < max(65.0, float(ADVISORY_REPEAT_SECONDS)):
			return None
		speaker = min(nearby, key=lambda agent: _distance_2d(position, agent['position']))
		state['last_said_at'] = now
		return {'mode': mode, 'speaker_id': speaker['id'], 'balance': round(balance, 2)}

	def blocked_shot_advisory(self, shooter_id, now):
		"""'Get off my sight!' - an ally sits between this bot and its own
		already-visible, already-locked target.

		Deliberately narrow: this never fires just because an ally is
		somewhere in the shooter's general facing, only when it is genuinely
		standing in the exact firing lane to a target the shooter can
		already see and would otherwise shoot. Geometry mirrors
		trimmed_sight_segment - a capsule test along the shooter-to-target
		line, not a cone, so a wide flanking ally never trips it.
		"""
		agent = self.agents.get(int(shooter_id))
		if agent is None:
			return None
		order = agent.get('last_order')
		position = agent.get('position')
		if (order is None or position is None or
				not order.get('fire_allowed') or order.get('target_id') is None):
			return None
		target_id = order['target_id']
		contact = self.contacts.get(agent['team'], {}).get(target_id)
		if contact is None or not contact.get('visible'):
			return None
		target_position = contact['position']
		dx = target_position[0] - position[0]
		dz = target_position[2] - position[2]
		length = math.sqrt(dx * dx + dz * dz)
		if length < 4.0:
			return None
		dx /= length; dz /= length
		for other in self.agents.values():
			if (other['id'] == agent['id'] or other.get('team') != agent['team'] or
					other.get('health_fraction', 1.0) <= 0.0):
				continue
			other_position = other.get('position')
			if other_position is None:
				continue
			ox = other_position[0] - position[0]
			oz = other_position[2] - position[2]
			along = ox * dx + oz * dz
			if along <= 3.0 or along >= length - 3.0:
				continue
			lateral = abs(ox * dz - oz * dx)
			if lateral <= ADVISORY_BLOCKED_SHOT_HALF_WIDTH:
				return {'blocker_id': other['id'], 'shooter_id': agent['id'],
					'target_id': target_id}
		return None


def route_summary(director, bot_id):
	agent = director.agents.get(int(bot_id))
	if agent is None:
		return 'unregistered'
	profile = agent['profile']
	personality = agent['personality']
	route = agent.get('route')
	return ('route=%s role=%s aggression=%.2f caution=%.2f teamwork=%.2f '
	        'patience=%.2f initiative=%.2f jiggle=%.2f' % (
			route.get('id') if route else 'direct', profile['dominant_role'],
			personality['aggression'], personality['caution'],
			personality['teamwork'], personality['patience'],
			personality['initiative'], personality['jiggle']))
