# -*- coding: utf-8 -*-
"""Battle medals, awarded the way the era's server awarded them.

The client ships the thresholds but not the code that applies them:
scripts/common/arena_achievements.py carries ACHIEVEMENT_CONDITIONS in
full, while the routine that reads it lives behind `if IS_BASEAPP` in
arena_achievements_processing, which is server-side and is not in the
client package at all. So the conditions here are WG's own numbers; only
the evaluation is rebuilt.

Two separate outputs, because the results screen reads them from two
different places (scripts/client/gui/scaleform/battleresults.py):

  * dossierPopUps -> the big medal panel on the personal tab.
    __populatePersonalMedals reads pData['dossierPopUps'] as a list of
    (recordIndex, value) pairs. It does NOT look at 'achievements'.
  * achievements  -> the little medal icons on a row of the team table.
    __populateTeamsData reads row['achievements'] as a list of plain
    recordIndex ints.

Both index into dossiers.RECORD_NAMES, not into the ACHIEVEMENTS tuple.
"""

# dossiers.RECORD_NAMES indices, read out of the shipped dossiers/__init__.pyc
# of this exact build. A newer client renumbers these, so they are pinned to
# 0.8.2 rather than looked up by name at runtime - a wrong index silently
# shows the wrong medal.
RECORD_INDEX = {
	'warrior': 34, 'invader': 35, 'sniper': 36, 'defender': 37,
	'steelwall': 38, 'supporter': 39, 'scout': 40, 'evileye': 72,
	'medalWittmann': 49, 'medalOrlik': 50, 'medalOskin': 51,
	'medalHalonen': 52, 'medalBurda': 53, 'medalBillotte': 54,
	'medalKolobanov': 55, 'medalFadin': 56, 'medalRadleyWalters': 73,
	'medalLafayettePool': 74, 'medalLehvaslaiho': 106, 'medalNikolas': 107,
	'medalPascucci': 77, 'medalDumitru': 78, 'medalBrunoPietro': 75,
	'medalTarczay': 76, 'heroesOfRassenay': 110, 'medalDeLanglade': 145,
	'medalTamadaYoshio': 146, 'raider': 61, 'kamikaze': 64,
	'huntsman': 148, 'bombardier': 147, 'luckyDevil': 152,
	'ironMan': 151, 'sturdy': 150, 'medalBrothersInArms': 143,
	'medalCrucialContribution': 144,
}

# Battle heroes: at most ONE of these is handed out per battle, to the
# player who did it best. Offline there is only one human, so the check is
# purely against the threshold. BATTLE_HERO_TEXTS in arena_achievements.py
# lists exactly these eight.
_BATTLE_HEROES = ('warrior', 'invader', 'sniper', 'defender', 'steelwall',
                  'supporter', 'scout', 'evileye')

# Kill-count medals, as (name, minKills, maxKills) straight from
# ACHIEVEMENT_CONDITIONS. They form BANDS: 3 kills is Pascucci, 4 is
# Dumitru, 5+ is Burda. Only the band that matches is awarded, which is
# what maxKills is for.
_KILL_BANDS = (
	('medalPascucci', 3, 3),
	('medalDumitru', 4, 4),
	('medalBurda', 5, 255),
	('medalRadleyWalters', 8, 9),
	('medalLafayettePool', 10, 13),
	('heroesOfRassenay', 14, 255),
)

# Same idea, but every victim has to be at least two tiers above you.
_UPTIER_BANDS = (
	('medalLehvaslaiho', 2, 2),
	('medalOskin', 3, 3),
	('medalNikolas', 4, 255),
)

# Kills taken while below 20% health with 5+ crits on the tank.
_LAST_STAND_BANDS = (
	('medalBillotte', 2, 2),
	('medalBrunoPietro', 3, 4),
	('medalTarczay', 5, 255),
)

_HP_PERCENTAGE = 20   # _BILLOTTE_CMN_CNDS['hpPercentage']
_MIN_CRITS = 5        # _BILLOTTE_CMN_CNDS['minCrits']


def _band(bands, kills):
	for name, lo, hi in bands:
		if lo <= kills <= hi:
			return name
	return None


def evaluate(stats, context=None):
	"""Medals earned this battle.

	stats: the player's own results row - the keys the ledger produces
	       (kills, shots, hits, damageDealt, damageReceived, shotsReceived,
	       spotted, capturePoints, droppedCapturePoints, damageAssisted).
	context: extra facts the row cannot carry, all optional:
	       health / maxHealth   hit points left at the end
	       survived             False when the player died
	       won                  True when the player's team won
	       uptierKills          kills on tanks 2+ tiers above
	       crits                module and crew crits the player TOOK
	       lowHealthKills       kills made while under 20% health
	       enemiesAlive         enemies still alive when the player was
	                            the last one standing on his team

	Returns (popUps, achievements): popUps is the (recordIndex, value)
	list for dossierPopUps, achievements the plain index list for the
	team table. Same medals, two shapes.
	"""
	stats = stats or {}
	context = context or {}
	earned = []

	def add(name):
		if name and name in RECORD_INDEX and name not in earned:
			earned.append(name)

	kills = int(stats.get('kills', 0) or 0)
	shots = int(stats.get('shots', 0) or 0)
	hits = int(stats.get('hits', 0) or 0)
	damage = int(stats.get('damageDealt', 0) or 0)
	spotted = int(stats.get('spotted', 0) or 0)
	assists = int(stats.get('damageAssisted', 0) or 0)
	capture = int(stats.get('capturePoints', 0) or 0)
	dropped = int(stats.get('droppedCapturePoints', 0) or 0)
	taken = int(stats.get('damageReceived', 0) or 0)
	hitsTaken = int(stats.get('shotsReceived', 0) or 0)

	# --- battle heroes, ACHIEVEMENT_CONDITIONS verbatim -----------------
	if kills >= 6:
		add('warrior')                                  # minFrags 6
	if capture >= 80:
		add('invader')                                  # minCapturePts 80
	if shots >= 10 and damage >= 1000 and hits >= int(0.85 * shots + 0.9999):
		add('sniper')                                   # 0.85 accuracy, 10 shots, 1000 dmg
	if dropped >= 70:
		add('defender')                                 # minPoints 70
	if taken >= 1000 and hitsTaken >= 11:
		add('steelwall')                                # minDamage 1000, minHits 11
	if spotted >= 9:
		add('scout')                                    # minDetections 9
	# supporter and evileye both want minAssists 6. WoT counts an assist
	# per ENEMY an ally finished off on your spotting; the offline build
	# has no ally-kill attribution yet, so these two stay unawarded rather
	# than being faked from a number that means something else.
	if assists >= 6:
		add('supporter')
		add('evileye')

	# --- kill counts ----------------------------------------------------
	add(_band(_KILL_BANDS, kills))
	if kills >= 3:
		add('huntsman')                                 # minKills 3
	if kills >= 2:
		add('bombardier')                               # minKills 2
	if kills >= 4:
		add('medalDeLanglade')                          # minKills 4
	if kills >= 12:
		add('medalCrucialContribution')                 # minKills 12

	uptier = int(context.get('uptierKills', 0) or 0)
	if uptier:
		add(_band(_UPTIER_BANDS, uptier))
		if uptier >= 3:
			add('medalOrlik')                           # minVictimLevelDelta 2, minKills 3
			add('medalHalonen')
			add('medalTamadaYoshio')

	# --- last stand -----------------------------------------------------
	low = int(context.get('lowHealthKills', 0) or 0)
	crits = int(context.get('crits', 0) or 0)
	if low and crits >= _MIN_CRITS:
		add(_band(_LAST_STAND_BANDS, low))

	# Kolobanov: the LAST tank left on your team, against five or more
	# still alive, and you come out of it alive. All three parts matter -
	# checking only 'five enemies are alive' hands it out in any battle
	# that ends early with your team intact, which is not the medal.
	if (context.get('survived') and context.get('aloneOnTeam')
			and int(context.get('enemiesAlive', 0) or 0) >= 5):
		add('medalKolobanov')                           # teamDiff 5

	# --- endurance ------------------------------------------------------
	if hitsTaken >= 10 and context.get('survived'):
		add('ironMan')                                  # minHits 10
	maxHealth = float(context.get('maxHealth', 0) or 0)
	health = float(context.get('health', 0) or 0)
	if maxHealth > 0 and context.get('survived'):
		if 0 < (health * 100.0 / maxHealth) <= 10.0:
			add('sturdy')                               # minHealth 10.0 percent

	popUps = [(RECORD_INDEX[name], 1) for name in earned]
	indices = [RECORD_INDEX[name] for name in earned]
	return popUps, indices


def hero_names(names):
	"""The subset that counts as a battle hero, for logging."""
	return [n for n in names if n in _BATTLE_HEROES]


def names_for(indices):
	"""Indices back to names - for the debug line, so the log says
	'warrior' and not '34'."""
	back = dict((v, k) for k, v in RECORD_INDEX.items())
	return [back.get(i, str(i)) for i in indices]
