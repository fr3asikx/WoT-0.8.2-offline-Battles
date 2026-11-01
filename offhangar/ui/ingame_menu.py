# -*- coding: utf-8 -*-
"""
WoT Offline Battles 2.0 - In-Game & Hangar Configuration Menu
"""

import BigWorld
import Keys

try:
	import GUI
	from gui import SystemMessages, WindowsManager
	from gui.InputHandler import g_instance as g_inputHandler
except Exception:
	g_inputHandler = None

_MENU_OPEN = False
_CONFIG_CACHE = {}

_KEY_1 = getattr(Keys, 'KEY_1', -1)
_KEY_2 = getattr(Keys, 'KEY_2', -2)
_KEY_3 = getattr(Keys, 'KEY_3', -3)
_KEY_4 = getattr(Keys, 'KEY_4', -4)
_KEY_5 = getattr(Keys, 'KEY_5', -5)
_KEY_NUMPAD1 = getattr(Keys, 'KEY_NUMPAD1', -11)
_KEY_NUMPAD2 = getattr(Keys, 'KEY_NUMPAD2', -12)
_KEY_NUMPAD3 = getattr(Keys, 'KEY_NUMPAD3', -13)
_KEY_NUMPAD4 = getattr(Keys, 'KEY_NUMPAD4', -14)
_KEY_NUMPAD5 = getattr(Keys, 'KEY_NUMPAD5', -15)
_KEY_MINUS = getattr(Keys, 'KEY_MINUS', -21)
_KEY_NUMPADMINUS = getattr(Keys, 'KEY_NUMPADMINUS', getattr(Keys, 'KEY_NUMPADSUB', -22))
_KEY_EQUALS = getattr(Keys, 'KEY_EQUALS', -23)
_KEY_NUMPADPLUS = getattr(Keys, 'KEY_NUMPADPLUS', getattr(Keys, 'KEY_ADD', -24))
_KEY_ESCAPE = getattr(Keys, 'KEY_ESCAPE', -30)
_KEY_RETURN = getattr(Keys, 'KEY_RETURN', -31)
_KEY_NUMPADENTER = getattr(Keys, 'KEY_NUMPADENTER', -32)

STRINGS = {
	'ru': {
		'title': u'[ НАСТРОЙКИ WOT OFFLINE BATTLES 2.0 ]',
		'line1': u'1. Никнейм игрока: {nick}',
		'line2': u'2. Ботов в команде: {bots}',
		'line3': u'3. Подвеска и физика: {susp}',
		'line4': u'4. Язык / Language: {lang}',
		'line5': u'5. Сохранить и выйти (Enter / ESC)',
		'hint': u'Нажмите клавиши 1-4 для изменения.',
		'saved': u'Настройки WoT Offline Battles 2.0 успешно применены!',
		'susp_tight': u'Вкл: Плотная (0.22)',
		'susp_realistic': u'Вкл: Реалистичная (0.35)',
		'susp_standard': u'Вкл: Мягкая (0.50)',
		'susp_off': u'Выкл (0.0)',
		'nick_tip': u'Чтобы сменить ник, введите в чат: /nick <Имя> или выберите из списка.'
	},
	'en': {
		'title': u'[ WOT OFFLINE BATTLES 2.0 SETTINGS ]',
		'line1': u'1. Player Nickname: {nick}',
		'line2': u'2. Bots Per Team: {bots}',
		'line3': u'3. Suspension & Physics: {susp}',
		'line4': u'4. Language / Язык: {lang}',
		'line5': u'5. Apply & Close (Enter / ESC)',
		'hint': u'Press keys 1-4 to toggle settings.',
		'saved': u'WoT Offline Battles 2.0 settings applied!',
		'susp_tight': u'On: Tight (0.22)',
		'susp_realistic': u'On: Realistic (0.35)',
		'susp_standard': u'On: Soft (0.50)',
		'susp_off': u'Off (0.0)',
		'nick_tip': u'To set nickname, type in chat: /nick <Name>'
	}
}

BOT_COUNTS = [15, 12, 10, 7, 5, 3, 1]
SUSPENSION_MODES = [('tight', 0.22), ('realistic', 0.35), ('standard', 0.50), ('off', 0.0)]
PRESET_NICKS = ['Defaultplayer', 'Tankist_2012', 'Commander', 'SteelHunter', 'Veteran', 'IronFist']


def load_menu_config():
	global _CONFIG_CACHE
	if not _CONFIG_CACHE:
		_CONFIG_CACHE = {
			'nickname': 'Defaultplayer',
			'bots_per_team': 15,
			'random_team': True,
			'player_swing_scale': 0.22,
			'language': 'ru'
		}
		try:
			from gui.mods.offhangar._constants import CONFIG_OPTIONS as _CONST_CFG0
			_CONFIG_CACHE['nickname'] = str(_CONST_CFG0.get('nickname', 'Defaultplayer'))
			_CONFIG_CACHE['bots_per_team'] = int(_CONST_CFG0.get('bots_per_team', 15))
			_CONFIG_CACHE['player_swing_scale'] = float(_CONST_CFG0.get('player_swing_scale', 0.22))
		except Exception:
			pass
	return _CONFIG_CACHE


def save_menu_config(cfg=None, silent=False):
	global _CONFIG_CACHE
	if isinstance(cfg, dict):
		_CONFIG_CACHE.update(cfg)
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS as _CONST_CFG_S
		if 'bots_per_team' in _CONFIG_CACHE:
			_CONST_CFG_S['bots_per_team'] = int(_CONFIG_CACHE['bots_per_team'])
		if 'player_swing_scale' in _CONFIG_CACHE:
			_CONST_CFG_S['player_swing_scale'] = float(_CONFIG_CACHE['player_swing_scale'])
		if 'nickname' in _CONFIG_CACHE:
			_CONST_CFG_S['nickname'] = str(_CONFIG_CACHE['nickname'])
	except Exception:
		pass
	if not silent:
		_show_feedback(get_localized_str('saved'), is_system=True)


def get_lang():
	try:
		from gui.mods.offhangar import cbt_profiles_and_locale as _cbt_l
		return _cbt_l.detect_client_language()
	except Exception:
		return _CONFIG_CACHE.get('language', 'ru')


def get_localized_str(key, **kwargs):
	lang = get_lang()
	table = STRINGS.get(lang, STRINGS['ru'])
	val = table.get(key, key)
	if kwargs:
		try:
			val = val.format(**kwargs)
		except Exception:
			pass
	return val


def _show_feedback(msg, is_system=False):
	try:
		BigWorld.playSound('/gui/buttons/default')
	except Exception:
		pass

	shown_in_battle = False
	try:
		from gui import WindowsManager
		bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
		if bw is not None:
			shown_in_battle = True
			p = BigWorld.player()
			if p is not None and hasattr(p, 'broadcast'):
				for line in msg.split('\n'):
					line_s = line.strip()
					if line_s:
						try:
							p.broadcast(0, line_s)
						except Exception:
							pass
	except Exception:
		pass

	if not shown_in_battle:
		try:
			from gui import SystemMessages
			_orig_pm = getattr(SystemMessages, '_offh_orig_pushMessage', None)
			if callable(_orig_pm):
				_orig_pm(msg.encode('utf-8') if isinstance(msg, unicode) else str(msg))
			elif SystemMessages.g_instance is not None:
				SystemMessages.pushMessage(msg)
		except Exception:
			pass


def render_menu():
	cfg = load_menu_config()
	lang = get_lang()

	scale = float(cfg.get('player_swing_scale', 0.22))
	if scale <= 0.05:
		susp_str = get_localized_str('susp_off')
	elif scale <= 0.25:
		susp_str = get_localized_str('susp_tight')
	elif scale <= 0.40:
		susp_str = get_localized_str('susp_realistic')
	else:
		susp_str = get_localized_str('susp_standard')

	lines = [
		get_localized_str('title'),
		get_localized_str('line1', nick=cfg.get('nickname', 'Defaultplayer')),
		get_localized_str('line2', bots=cfg.get('bots_per_team', 15)),
		get_localized_str('line3', susp=susp_str),
		get_localized_str('line4', lang='RU' if lang == 'ru' else 'EN'),
		get_localized_str('line5'),
		get_localized_str('hint')
	]
	full_msg = u'\n'.join(lines)
	_show_feedback(full_msg)


def toggle_menu():
	global _MENU_OPEN
	_MENU_OPEN = not _MENU_OPEN
	if _MENU_OPEN:
		load_menu_config()
		render_menu()
	else:
		save_menu_config()


def cycle_bot_count(delta=1):
	cur = int(_CONFIG_CACHE.get('bots_per_team', 15))
	if cur in BOT_COUNTS:
		idx = BOT_COUNTS.index(cur)
		nxt = BOT_COUNTS[(idx + delta) % len(BOT_COUNTS)]
	else:
		nxt = 15
	_CONFIG_CACHE['bots_per_team'] = nxt
	save_menu_config(silent=True)
	_show_feedback(get_localized_str('line2', bots=nxt))


def cycle_suspension():
	cur_scale = float(_CONFIG_CACHE.get('player_swing_scale', 0.22))
	if cur_scale <= 0.05:
		nxt_mode, nxt_scale = ('tight', 0.22)
	elif cur_scale <= 0.25:
		nxt_mode, nxt_scale = ('realistic', 0.35)
	elif cur_scale <= 0.40:
		nxt_mode, nxt_scale = ('standard', 0.50)
	else:
		nxt_mode, nxt_scale = ('off', 0.0)
	_CONFIG_CACHE['player_swing_scale'] = nxt_scale
	try:
		import BigWorld
		p = BigWorld.player()
		if p is not None:
			fash = getattr(p, '_fashion', None)
			if fash is not None:
				if nxt_scale <= 0.001:
					try: fash.disableAllSwinging()
					except Exception: pass
				else:
					td = getattr(p, 'typeDescriptor', None)
					if td is not None:
						f_sw = td.hull.get('swinging', {})
						if f_sw:
							pp = tuple((pt * m * nxt_scale) if i < 2 else (pt * m * 1.35) for i, (pt, m) in enumerate(zip(f_sw.get('pitchParams', ()), (0.9, 1.88, 0.3, 4.0, 1.0, 1.0))))
							rp = tuple((r * m * nxt_scale) if i < 2 else (r * m * 1.35) for i, (r, m) in enumerate(zip(f_sw.get('rollParams', ()), (0.8, 1.76, 0.3, 4.0, 1.0, 1.0))))
							fash.setPitchSwinging('V', *pp)
							fash.setRollSwinging('V', *rp)
	except Exception:
		pass
	save_menu_config(silent=True)
	_show_feedback(get_localized_str('line3', susp=get_localized_str('susp_' + nxt_mode)))


def cycle_language():
	cur = get_lang()
	nxt = 'en' if cur == 'ru' else 'ru'
	_CONFIG_CACHE['language'] = nxt
	_CONFIG_CACHE['language_override'] = nxt
	try:
		from gui.mods.offhangar import cbt_profiles_and_locale as _cbt_l
		_cbt_l.set_client_language_override(nxt)
	except Exception:
		pass
	render_menu()


def cycle_nickname():
	cur = _CONFIG_CACHE.get('nickname', 'Defaultplayer')
	idx = PRESET_NICKS.index(cur) if cur in PRESET_NICKS else 0
	nxt = PRESET_NICKS[(idx + 1) % len(PRESET_NICKS)]
	_CONFIG_CACHE['nickname'] = nxt
	save_menu_config(silent=True)
	_show_feedback(get_localized_str('line1', nick=nxt) + u'\n' + get_localized_str('nick_tip'))


def handle_key_event(event):
	return False


def handle_chat_command(text):
	if not text or not text.startswith('/'):
		return False
	parts = text.strip().split()
	cmd = parts[0].lower()
	args = parts[1:]

	cfg = load_menu_config()
	if cmd in ('/help', '/menu', '/offhangar'):
		render_menu()
		return True
	elif cmd == '/nick' and args:
		new_nick = args[0][:20]
		cfg['nickname'] = new_nick
		save_menu_config(silent=True)
		_show_feedback(get_localized_str('line1', nick=new_nick))
		return True
	elif cmd == '/bots' and args:
		try:
			val = max(1, min(15, int(args[0])))
			cfg['bots_per_team'] = val
			save_menu_config(silent=True)
			_show_feedback(get_localized_str('line2', bots=val))
			return True
		except ValueError:
			pass
	elif cmd == '/lang' and args:
		val = args[0].lower()
		if val in ('ru', 'en'):
			cfg['language'] = val
			save_menu_config(silent=True)
			render_menu()
			return True
	elif cmd in ('/susp', '/suspension') and args:
		val = args[0].lower()
		scale_map = {'tight': 0.22, 'realistic': 0.35, 'standard': 0.50}
		if val in scale_map:
			cfg['player_swing_scale'] = scale_map[val]
			save_menu_config(silent=True)
			_show_feedback(get_localized_str('line3', susp=get_localized_str('susp_' + val)))
			return True
	elif cmd in ('/start', '/battle', '/go'):
		try:
			p = BigWorld.player()
			if p is not None:
				from gui.mods.offhangar.network_battle import request_battle_start
				if request_battle_start(p):
					_show_feedback(u'Запуск сетевого боя...' if cfg.get('language') == 'ru' else 'Starting network battle...')
					return True
				from gui.mods.offhangar.offline_battle import start_offline_random_from_hangar
				start_offline_random_from_hangar(p, 0)
				_show_feedback(u'Запуск боя...' if cfg.get('language') == 'ru' else 'Starting battle...')
				return True
		except Exception:
			pass

	return False


def init():
	load_menu_config()


init()
