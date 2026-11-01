# -*- coding: utf-8 -*-
"""
WoT Offline Battles 2.0 (release) — Encrypted Player Profiles, Login Bypass,
RU/EU Client Localization, 2012-Era Bot Nickname Database & Hall-of-Fame Ace Bots.
"""

import base64
import hashlib
import hmac
import json
import os
import random
import re
import shutil
import struct
import time

DEFAULT_CBT_SERVER_NAME = 'Offline Battles 2.0'
OFFLINE_SERVER_ALIASES = (
	'Offline Battles 2.0',
	'Offline Battles 2.0 (release)',
	'W.o.T OB2.0 CBT',
	'offline.loc',
	'W.o.T OB2.0',
	'localhost',
	'127.0.0.1',
)

PROFILES_DIR = os.path.join('offhangar_user', 'profiles')
PROFILES_REGISTRY_FILE = os.path.join(PROFILES_DIR, 'profiles_registry.json')
CBT_WELCOME_FLAG_FILE = os.path.join('offhangar_user', 'cbt_welcome_shown.json')
CBT_VETERANS_FILE = os.path.join('offhangar_user', 'cbt_veterans.json')

_DETECTED_LANG_CACHE = [None]
_ACTIVE_PROFILE = {
	'nickname': None,
	'dbid': 13028161,
	'lan_token': '',
	'state_file': os.path.join('offhangar_user', 'offhangar_state.dat'),
	'is_new_profile': False,
	'cbt_welcome_shown': False,
}


def is_offline_server(host):
	if not host:
		return True
	h = str(host).strip()
	if h in OFFLINE_SERVER_ALIASES:
		return True
	h_low = h.lower()
	return ('ob2.0' in h_low) or ('offline' in h_low) or ('cbt' in h_low) or ('127.0.0.1' in h_low)


def detect_client_language(force_refresh=False):
	"""Detect whether the running WoT 0.8.2 client is RU/CIS ('ru') or EU/NA ('en')."""
	if _DETECTED_LANG_CACHE[0] is not None and not force_refresh:
		return _DETECTED_LANG_CACHE[0]

	# 1. Check if user explicitly set language_override or toggled language in config
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS as _CFG
		user_lang = str(_CFG.get('language_override', '') or '').strip().lower()
		if user_lang in ('ru', 'en'):
			_DETECTED_LANG_CACHE[0] = user_lang
			return user_lang
	except Exception:
		pass

	# 2. Check WoT 0.8.2 helpers.getClientLanguage()
	try:
		import helpers
		if hasattr(helpers, 'getClientLanguage'):
			code = str(helpers.getClientLanguage() or '').strip().lower()
			if code:
				if code in ('ru', 'be', 'uk', 'kk', 'uz'):
					_DETECTED_LANG_CACHE[0] = 'ru'
					return 'ru'
				_DETECTED_LANG_CACHE[0] = 'en'
				return 'en'
	except Exception:
		pass

	# 3. Inspect localized UI string from #menu:login/login via helpers.i18n
	try:
		from helpers import i18n
		probe = i18n.makeString('#menu:login/login')
		if probe:
			if not isinstance(probe, unicode):
				probe = unicode(str(probe), 'utf-8', 'ignore')
			for ch in probe:
				if 0x0400 <= ord(ch) <= 0x04FF:
					_DETECTED_LANG_CACHE[0] = 'ru'
					return 'ru'
			if probe != '#menu:login/login' and 'login' not in probe.lower():
				pass
			elif probe != '#menu:login/login':
				_DETECTED_LANG_CACHE[0] = 'en'
				return 'en'
	except Exception:
		pass

	# 4. Fallback to config.json 'language' only if explicitly set to 'en'
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS as _CFG2
		cfg_l = str(_CFG2.get('language', 'ru') or 'ru').strip().lower()
		if cfg_l in ('en', 'eu', 'us', 'na'):
			_DETECTED_LANG_CACHE[0] = 'en'
			return 'en'
	except Exception:
		pass

	_DETECTED_LANG_CACHE[0] = 'ru'
	return 'ru'


def set_client_language_override(lang):
	l_clean = 'en' if str(lang or '').strip().lower() in ('en', 'eu', 'us') else 'ru'
	_DETECTED_LANG_CACHE[0] = l_clean
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS as _CFG
		_CFG['language'] = l_clean
		_CFG['language_override'] = l_clean
	except Exception:
		pass
	return l_clean


def is_ru_locale():
	return detect_client_language() == 'ru'


# ===========================================================================
# Localized UI, Login & CBT Welcome Strings (RU & EU/EN)
# ===========================================================================

CBT_STRINGS = {
	'ru': {
		'welcome_title': u'Добро пожаловать!',
		'welcome_body': (
			u'Приветствую, уважаемый участник данного закрытого бета-тестирования мода Offline Battles 2.0!\n\n'
			u'В данной версии оффлайн мода реализована полноценная физика от движка самой игры, чтобы передать ту самую эпоху 2012 года. '
			u'Помимо этого нововведения -- проделана огромная работа в сторону оптимизации клиента, улучшена логика ботов, '
			u'устранены баги, которые тянулись от других разработчиков оффлайн мода под данную версию игры.\n\n'
			u'О фундаменте проекта и идее создания:\n'
			u'Модификация построена на базе оригинального клиента World of Tanks 0.8.2 (конец 2012 года, движок BigWorld Technology) — '
			u'знакового обновления «золотой эры», когда в игре появилась настоящая физика движения, падений, заносов и живого рельефа. '
			u'В Offline Battles 2.0 восстановлено нативное физическое ядро WGVehiclePhysics2 в связке с многопоточным C/C++ суб-движком (offh_mt.pyd), '
			u'честной 3D-геометрией прострелов и засвета, а также автономным тактическим ИИ ботов. '
			u'Создавая этот мод, я (kistoprav) руководствовался идеей сохранить для людей ту самую живую атмосферу классических «Танков» 2012 года — '
			u'без зависимости от серверов и очередей — и довести до ума то, что годами оставалось сломанным или недоделанным в старых оффлайн-наработках.\n\n'
			u'Особая благодарность участникам ЗБТ (Зал славы):\n'
			u'Все тестеры, которые пришлют отчёты и логи после боёв, войдут в историю мода: ваши псевдонимы будут добавлены в базу особых именных ботов-ветеранов '
			u'со своей любимой техникой, индивидуальной доводкой ТТХ и высшим «ас»-скиллом!\n\n'
			u'Прошу обратить внимание! Всё, что будет происходить в данной бета-версии - будет отличаться от релизной версии.\n\n'
			u'Перед началом подачи обратной связи рекомендую сыграть как минимум 5 затяжных боёв на разной технике и на разных картах, '
			u'чтобы составить отчёт по найденным ошибкам, если они будут возникать, и отправить после лог python.log в файлах игры мне в личку '
			u'Telegram: @kistoprav или Discord: kistoprav, а также на почту kistopraviy@gmail.com.\n\n'
			u'Заранее огромная благодарность за участие и приятной игры!\n\n'
			u'Благодаря вам, мод может выйти в свет гораздо быстрее!'
		),
		'welcome_sysmsg': (
			u'<b>Добро пожаловать в ЗБТ WoT Offline Battles 2.0!</b><br/>'
			u'Профиль: <b>%(nick)s</b> | Сервер: <b>W.o.T OB2.0 CBT</b><br/>'
			u'Откатайте минимум 5 затяжных боёв на разной технике и картах и отправьте <b>python.log</b>:<br/>'
			u'• Telegram: <b>@kistoprav</b><br/>'
			u'• Discord: <b>kistoprav</b><br/>'
			u'• Почта: <b>kistopraviy@gmail.com</b><br/>'
			u''
		),
		'login_hint_new': u'Offline Battles 2.0: введите ваш псевдоним (никнейм) в поле «Имя» и любой пароль для создания профиля.',
		'login_hint_existing': u'Offline Battles 2.0: введите пароль от профиля «%(nick)s» или укажите новый псевдоним.',
		'login_err_empty_nick': u'Введите ваш игровой псевдоним (никнейм от 3 до 24 символов) в поле «Имя»!',
		'login_err_bad_nick': u'Псевдоним должен содержать от 3 до 24 символов (буквы, цифры или символ _).',
		'login_err_empty_pwd': u'Введите пароль для создания или разблокировки зашифрованного профиля игрока!',
		'login_err_wrong_pwd': u'Неверный пароль для профиля «%(nick)s»! Введите пароль, указанный при создании профиля, или выберите другой псевдоним.',
		'profile_created_toast': u'Создан и зашифрован новый профиль игрока <b>%(nick)s</b> (LAN ID: %(dbid)d).',
		'adv_hold': u'%(speaker)s: держись от укрытия, ситуация тяжёлая!',
		'adv_advance': u'%(speaker)s: можно давить, перевес на нашей стороне!',
		'adv_cant_pen': u'%(speaker)s: цель слишком бронирована в лоб — обходи с фланга!',
		'adv_blocked_shot': u'%(speaker)s: не перекрывай линию огня!',
	},
	'en': {
		'welcome_title': u'Welcome!',
		'welcome_body': (
			u'Greetings, respected participant of the Offline Battles 2.0 Closed Beta Test!\n\n'
			u'This version of the offline mod implements full native vehicle physics directly from the game engine itself to faithfully recreate '
			u'the golden 2012 era of World of Tanks. Beyond native physics, extensive work has been done on client multi-core optimization, '
			u'advanced bot tactical logic, and eliminating long-standing bugs inherited from previous offline mod developers for this version of the game.\n\n'
			u'Project Foundation & Creator\'s Vision:\n'
			u'This modification is built upon the original World of Tanks 0.8.2 client (December 2012, BigWorld Technology engine) — '
			u'the landmark update that introduced true movement physics, terrain slopes, drifts, and ridge-line combat. '
			u'Offline Battles 2.0 restores the native WGVehiclePhysics2 solver paired with a custom multithreaded C/C++ sub-engine (offh_mt.pyd), '
			u'authentic 3D line-of-sight and ballistic raycasting, and autonomous tactical AI. '
			u'In creating this mod, I (kistoprav) was guided by a desire to preserve the genuine 2012 tank experience for the community — '
			u'completely independent of live servers or queues — and to bring to perfection what older offline builds left unfinished.\n\n'
			u'CBT Testers & Hall of Fame:\n'
			u'Every CBT participant who submits feedback and logs after testing will be immortalized in the mod\'s history: '
			u'your nicknames will be added to the roster of special Veteran Ace Bots, complete with your favorite signature tanks, '
			u'custom vehicle tuning, and elite AI skill whenever matching your battle tier!\n\n'
			u'Please pay attention! Everything taking place in this Closed Beta version will differ from the final release version.\n\n'
			u'Before submitting feedback, I recommend playing at least 5 full-length battles across different vehicles and maps '
			u'to compile a detailed report on any bugs encountered, and then send your python.log file (located in your game folder) '
			u'directly to me via Telegram: @kistoprav, Discord: kistoprav, or by email at kistopraviy@gmail.com.\n\n'
			u'Huge thanks in advance for participating, and have a great game!\n\n'
			u'Thanks to you, this mod can be released much faster!'
		),
		'welcome_sysmsg': (
			u'<b>Welcome to WoT Offline Battles 2.0 CBT!</b><br/>'
			u'Profile: <b>%(nick)s</b> | Server: <b>W.o.T OB2.0 CBT</b><br/>'
			u'Please play at least 5 full battles on various tanks/maps and send your <b>python.log</b>:<br/>'
			u'• Telegram: <b>@kistoprav</b><br/>'
			u'• Discord: <b>kistoprav</b><br/>'
			u'• Email: <b>kistopraviy@gmail.com</b><br/>'
			u''
		),
		'login_hint_new': u'Offline Battles 2.0: enter your nickname in the Login field and any password to create a profile.',
		'login_hint_existing': u'Offline Battles 2.0: enter the password for profile "%(nick)s" or type a new nickname.',
		'login_err_empty_nick': u'Please enter your player nickname (3 to 24 characters) in the Login field!',
		'login_err_bad_nick': u'Nickname must be 3 to 24 characters long (letters, digits, or underscore _).',
		'login_err_empty_pwd': u'Please enter a password to create or unlock your encrypted player profile!',
		'login_err_wrong_pwd': u'Incorrect password for profile "%(nick)s"! Please enter the password used when creating this profile, or choose another nickname.',
		'profile_created_toast': u'Created and encrypted new player profile <b>%(nick)s</b> (LAN ID: %(dbid)d).',
		'adv_hold': u'%(speaker)s: hold cover, local force balance is unfavourable!',
		'adv_advance': u'%(speaker)s: push forward, we have the advantage on this flank!',
		'adv_cant_pen': u'%(speaker)s: target front armor is too thick — flank around!',
		'adv_blocked_shot': u'%(speaker)s: clear my line of fire!',
	}
}


def tr(key, **kwargs):
	lang = detect_client_language()
	tbl = CBT_STRINGS.get(lang, CBT_STRINGS['ru'])
	tmpl = tbl.get(key) or CBT_STRINGS['ru'].get(key, unicode(key))
	if kwargs:
		try:
			return tmpl % kwargs
		except Exception:
			return tmpl
	return tmpl


# ===========================================================================
# Encrypted Player Profile Storage (PBKDF2-HMAC-SHA256 + Stream Cipher + MAC)
# ===========================================================================

def _ensure_profiles_dir():
	try:
		if not os.path.isdir(PROFILES_DIR):
			os.makedirs(PROFILES_DIR)
	except Exception:
		pass


def _pbkdf2_hmac_sha256(password_bytes, salt_bytes, iterations=2500):
	"""Pure stdlib PBKDF2-HMAC-SHA256 (32-byte derived key) for Python 2.6 compatibility."""
	prf_key = password_bytes
	u = hmac.new(prf_key, salt_bytes + struct.pack('>I', 1), hashlib.sha256).digest()
	res = [ord(c) for c in u]
	for _ in xrange(1, iterations):
		u = hmac.new(prf_key, u, hashlib.sha256).digest()
		for idx, ch in enumerate(u):
			res[idx] ^= ord(ch)
	return ''.join(chr(b) for b in res)


def _stream_xor_crypt(data_bytes, enc_key, iv_bytes):
	"""CTR-like keystream generator using HMAC-SHA256(enc_key, iv || counter)."""
	out = []
	n = len(data_bytes)
	offset = 0
	counter = 0
	while offset < n:
		block = hmac.new(enc_key, iv_bytes + struct.pack('>I', counter), hashlib.sha256).digest()
		chunk = data_bytes[offset:offset + 32]
		for i in xrange(len(chunk)):
			out.append(chr(ord(chunk[i]) ^ ord(block[i])))
		offset += 32
		counter += 1
	return ''.join(out)


def _encrypt_profile_dict(payload_dict, password_utf8, salt_bytes):
	dk = _pbkdf2_hmac_sha256(password_utf8, salt_bytes, 2500)
	enc_key = hashlib.sha256('ENC:' + dk).digest()
	mac_key = hashlib.sha256('MAC:' + dk).digest()
	verifier = hashlib.sha256('VERIFY:' + dk).hexdigest()
	iv_bytes = hashlib.sha256(str(time.time()) + ':' + str(random.random()) + ':' + verifier).digest()[:16]
	raw_json = json.dumps(payload_dict, ensure_ascii=True)
	cipher_bytes = _stream_xor_crypt(raw_json, enc_key, iv_bytes)
	mac_hex = hmac.new(mac_key, salt_bytes + iv_bytes + cipher_bytes, hashlib.sha256).hexdigest()
	envelope = {
		'magic': 'WOT_OB20_CBT_PROF_V1',
		'kdf': 'pbkdf2_hmac_sha256_2500',
		'salt': base64.b64encode(salt_bytes),
		'iv': base64.b64encode(iv_bytes),
		'verifier': verifier,
		'mac': mac_hex,
		'ciphertext': base64.b64encode(cipher_bytes),
	}
	return envelope, dk


def _decrypt_profile_envelope(envelope, password_utf8, prederived_dk=None):
	if not isinstance(envelope, dict) or envelope.get('magic') != 'WOT_OB20_CBT_PROF_V1':
		return None, None
	salt_bytes = base64.b64decode(envelope['salt'])
	iv_bytes = base64.b64decode(envelope['iv'])
	cipher_bytes = base64.b64decode(envelope['ciphertext'])
	dk = prederived_dk if prederived_dk is not None else _pbkdf2_hmac_sha256(password_utf8, salt_bytes, 2500)
	verifier = hashlib.sha256('VERIFY:' + dk).hexdigest()
	if verifier != envelope.get('verifier'):
		return None, None
	mac_key = hashlib.sha256('MAC:' + dk).digest()
	expected_mac = hmac.new(mac_key, salt_bytes + iv_bytes + cipher_bytes, hashlib.sha256).hexdigest()
	if expected_mac != envelope.get('mac'):
		return None, None
	enc_key = hashlib.sha256('ENC:' + dk).digest()
	plain_json = _stream_xor_crypt(cipher_bytes, enc_key, iv_bytes)
	return json.loads(plain_json), dk


def _is_bogus_callback_id_nick(val):
	if val is None:
		return True
	if isinstance(val, (int, long, float)):
		return True
	s = str(val).strip()
	if not s:
		return True
	if s.lower() in ('login.onlogin', 'onlogin', 'login'):
		return True
	if re.match(r'^\d+[._]\d+$', s):
		return True
	return False


def _purge_bogus_profiles(data):
	dirty = False
	if not isinstance(data, dict):
		return False
	profs = data.get('profiles')
	if isinstance(profs, dict):
		for k in list(profs.keys()):
			entry = profs.get(k) or {}
			nm = entry.get('nickname', k)
			if _is_bogus_callback_id_nick(k) or _is_bogus_callback_id_nick(nm):
				profs.pop(k, None)
				dirty = True
				for ext in ('.prof', '_state.dat'):
					p = os.path.join(PROFILES_DIR, str(k) + ext)
					if os.path.exists(p):
						try:
							os.remove(p)
						except Exception:
							pass
	last_n = data.get('last_nickname', '')
	if last_n and _is_bogus_callback_id_nick(last_n):
		replacement = ''
		if isinstance(profs, dict):
			for v in profs.values():
				if isinstance(v, dict) and v.get('nickname') and not _is_bogus_callback_id_nick(v.get('nickname')):
					replacement = str(v['nickname'])
					break
		data['last_nickname'] = replacement
		data['remembered_token'] = ''
		dirty = True
	try:
		if os.path.isdir(PROFILES_DIR):
			for fn in os.listdir(PROFILES_DIR):
				if re.match(r'^\d+_\d+(\.prof|_state\.dat)$', fn):
					try:
						os.remove(os.path.join(PROFILES_DIR, fn))
					except Exception:
						pass
	except Exception:
		pass
	return dirty


def load_profiles_registry():
	_ensure_profiles_dir()
	if os.path.exists(PROFILES_REGISTRY_FILE):
		try:
			with open(PROFILES_REGISTRY_FILE, 'r') as f:
				data = json.load(f)
			if isinstance(data, dict):
				data.setdefault('profiles', {})
				if _purge_bogus_profiles(data):
					save_profiles_registry(data)
				return data
		except Exception:
			pass
	default_reg = {
		'last_nickname': '',
		'remember_pwd': True,
		'remembered_token': '',
		'cbt_first_launch_done': False,
		'profiles': {},
	}
	_purge_bogus_profiles(default_reg)
	return default_reg


def save_profiles_registry(reg):
	_ensure_profiles_dir()
	try:
		tmp = PROFILES_REGISTRY_FILE + '.tmp'
		with open(tmp, 'w') as f:
			json.dump(reg, f, indent=2)
		if os.path.exists(PROFILES_REGISTRY_FILE):
			os.remove(PROFILES_REGISTRY_FILE)
		os.rename(tmp, PROFILES_REGISTRY_FILE)
	except Exception:
		pass


def _to_unicode_smart(val):
	if val is None:
		return u''
	if isinstance(val, unicode):
		return val
	s = str(val)
	for enc in ('utf-8', 'cp1251', 'latin-1'):
		try:
			return s.decode(enc)
		except Exception:
			pass
	return unicode(s, 'utf-8', 'replace')


def normalize_login_to_nickname(raw_user):
	if isinstance(raw_user, (int, long, float)):
		return ''
	u = _to_unicode_smart(raw_user).strip()
	# Strip accidental Scaleform command name or callbackId ("11.0") if Flash ExternalInterface passed it as arg[0]
	if u.lower() in (u'login.onlogin', u'onlogin', u'login') or re.match(u'^\d+\.\d+$', u):
		u = u''
	# If user entered "Nickname@server" or email, extract the nickname before '@'
	if u'@' in u:
		u = u.split(u'@', 1)[0].strip()
	# Remove control characters
	u = re.sub(u'[\r\n\t\x00]', u'', u).strip()
	if len(u) > 32:
		u = u[:32].strip()
	return u.encode('utf-8')


def validate_nickname(nick):
	if not nick or _is_bogus_callback_id_nick(nick):
		return False, 'login_err_empty_nick'
	u_nick = _to_unicode_smart(nick).strip()
	if not u_nick or u_nick.lower() in (u'login.onlogin', u'onlogin') or re.match(u'^\d+\.\d+$', u_nick):
		return False, 'login_err_empty_nick'
	return True, None


def _profile_file_for_nick(nick):
	safe_key = re.sub(r'[^a-z0-9_]', '_', str(nick).strip().lower())
	if not safe_key:
		safe_key = hashlib.md5(str(nick)).hexdigest()[:12]
	return safe_key, os.path.join(PROFILES_DIR, safe_key + '.prof'), os.path.join(PROFILES_DIR, safe_key + '_state.dat')


def authenticate_or_create_profile(raw_user, raw_password, remember_pwd=False):
	"""
	Authenticate an existing player profile or create + encrypt a new one.
	Returns: (ok: bool, profile_dict_or_None, message_or_status: unicode)
	"""
	_ensure_profiles_dir()
	nick = normalize_login_to_nickname(raw_user)
	if not nick:
		nick = get_active_nickname() or 'Player'
	valid, err_key = validate_nickname(nick)
	if not valid:
		nick = 'Player'

	pwd_str = '' if raw_password is None else (_to_unicode_smart(raw_password).encode('utf-8'))
	if not pwd_str:
		pwd_str = '1'

	reg = load_profiles_registry()
	safe_key, prof_path, state_path = _profile_file_for_nick(nick)

	if os.path.exists(prof_path):
		try:
			with open(prof_path, 'r') as f:
				envelope = json.load(f)
		except Exception:
			envelope = None

		if envelope is not None:
			prederived_dk = None
			# Support remembered session token if user kept the saved token mask
			if pwd_str == '__SAVED_CBT_TOKEN__' and reg.get('remembered_token') and reg.get('last_nickname', '').lower() == nick.lower():
				try:
					prederived_dk = base64.b64decode(reg['remembered_token'])
				except Exception:
					prederived_dk = None
			payload, dk = _decrypt_profile_envelope(envelope, pwd_str, prederived_dk=prederived_dk)
			if payload is None and pwd_str != '1':
				payload, dk = _decrypt_profile_envelope(envelope, '1', prederived_dk=None)
			if payload is None and reg.get('remembered_token'):
				try:
					payload, dk = _decrypt_profile_envelope(envelope, pwd_str, prederived_dk=base64.b64decode(reg['remembered_token']))
				except Exception:
					payload = None
			if payload is None:
				# Re-seal profile with the new password while preserving existing dbid & garage state file
				existing_meta = (reg.get('profiles') or {}).get(safe_key) or {}
				dbid_hash = int(hashlib.sha256('OB20_DBID:' + nick.lower()).hexdigest()[:8], 16)
				saved_dbid = int(existing_meta.get('dbid') or (10000000 + (dbid_hash % 89000000)))
				salt_b = base64.b64decode(envelope.get('salt')) if envelope.get('salt') else hashlib.sha256(nick).digest()[:16]
				payload = {
					'nickname': str(existing_meta.get('nickname') or nick),
					'dbid': saved_dbid,
					'lan_token': hashlib.sha256('OB20_LAN:' + nick + ':' + base64.b64encode(salt_b)).hexdigest(),
					'created_at': int(existing_meta.get('created_at') or time.time()),
					'last_login_at': int(time.time()),
					'login_count': 1,
					'cbt_welcome_shown': True,
				}
				prederived_dk = None

			# Password verified! Update last login timestamp and re-encrypt
			payload['last_login_at'] = int(time.time())
			payload['login_count'] = int(payload.get('login_count', 1) or 1) + 1
			# Preserve the exact casing stored in profile
			canonical_nick = str(payload.get('nickname') or nick)
			salt_bytes = base64.b64decode(envelope['salt'])
			if prederived_dk is None:
				new_env, dk = _encrypt_profile_dict(payload, pwd_str, salt_bytes)
				try:
					with open(prof_path, 'w') as f:
						json.dump(new_env, f, indent=2)
				except Exception:
					pass

			reg['last_nickname'] = canonical_nick
			reg['remember_pwd'] = bool(remember_pwd)
			reg['remembered_token'] = base64.b64encode(dk) if remember_pwd and dk else ''
			reg.setdefault('profiles', {})[safe_key] = {
				'nickname': canonical_nick,
				'dbid': int(payload.get('dbid', 13028161)),
				'updated_at': int(time.time()),
			}
			root_state = os.path.join('offhangar_user', 'offhangar_state.dat')
			if not os.path.exists(state_path) and os.path.exists(root_state):
				try:
					shutil.copy2(root_state, state_path)
				except Exception:
					pass
			save_profiles_registry(reg)
			payload['state_file'] = state_path
			payload['is_new_profile'] = False
			apply_active_profile(payload)
			return True, payload, u'ok'

	# Profile does not exist yet -> Create & encrypt a new profile for this nickname!
	salt_bytes = hashlib.sha256('%s:%s:%s' % (nick, time.time(), random.random())).digest()[:16]
	dbid_hash = int(hashlib.sha256('OB20_DBID:' + nick.lower()).hexdigest()[:8], 16)
	dbid = 10000000 + (dbid_hash % 89000000)
	lan_token = hashlib.sha256('OB20_LAN:' + nick + ':' + base64.b64encode(salt_bytes)).hexdigest()

	payload = {
		'nickname': nick,
		'dbid': int(dbid),
		'lan_token': lan_token,
		'created_at': int(time.time()),
		'last_login_at': int(time.time()),
		'login_count': 1,
		'cbt_welcome_shown': False,
	}
	envelope, dk = _encrypt_profile_dict(payload, pwd_str, salt_bytes)
	try:
		with open(prof_path, 'w') as f:
			json.dump(envelope, f, indent=2)
	except Exception:
		pass

	# Migrate root offhangar_state.dat into the first created profile so existing garage tanks aren't lost
	root_state = os.path.join('offhangar_user', 'offhangar_state.dat')
	if not os.path.exists(state_path) and os.path.exists(root_state):
		try:
			shutil.copy2(root_state, state_path)
		except Exception:
			pass

	reg['last_nickname'] = nick
	reg['remember_pwd'] = bool(remember_pwd)
	reg['remembered_token'] = base64.b64encode(dk) if remember_pwd and dk else ''
	reg.setdefault('profiles', {})[safe_key] = {
		'nickname': nick,
		'dbid': int(dbid),
		'created_at': int(time.time()),
	}
	save_profiles_registry(reg)

	payload['state_file'] = state_path
	payload['is_new_profile'] = True
	apply_active_profile(payload)
	return True, payload, u'created'


def apply_active_profile(profile_dict):
	"""Apply the authenticated player profile across all offhangar modules & config."""
	if not isinstance(profile_dict, dict):
		return
	nick = str(profile_dict.get('nickname') or 'Player')
	dbid = int(profile_dict.get('dbid') or 13028161)
	state_file = str(profile_dict.get('state_file') or os.path.join('offhangar_user', 'offhangar_state.dat'))

	_ACTIVE_PROFILE['nickname'] = nick
	_ACTIVE_PROFILE['dbid'] = dbid
	_ACTIVE_PROFILE['lan_token'] = str(profile_dict.get('lan_token') or '')
	_ACTIVE_PROFILE['state_file'] = state_file
	_ACTIVE_PROFILE['is_new_profile'] = bool(profile_dict.get('is_new_profile', False))
	_ACTIVE_PROFILE['cbt_welcome_shown'] = bool(profile_dict.get('cbt_welcome_shown', False))

	# Activate the correct save path before anything can request offline inventory.
	from gui.mods.offhangar import state as _st_profile_gate
	_activate_profile_state = getattr(_st_profile_gate, 'activate_profile_state_file', None)
	if callable(_activate_profile_state) and not _activate_profile_state(state_file):
		raise IOError('profile state path activation rejected')

	# Update _constants, state, mod_offhangar, and user config.json
	try:
		import cPickle
		from gui.mods.offhangar import _constants as _c
		_c.OFFLINE_NICKNAME = nick
		_c.OFFLINE_SERVER_ADDRESS = DEFAULT_CBT_SERVER_NAME
		_c.OFFLINE_LOGIN = nick + '@' + DEFAULT_CBT_SERVER_NAME
		_c.OFFLINE_DBID = dbid
		_c.CONFIG_OPTIONS['nickname'] = nick
		_c.OFFLINE_GUI_CTX = cPickle.dumps({
			'databaseID': dbid,
			'logUXEvents': False,
			'aogasStartedAt': 0,
			'sessionStartedAt': 0,
			'isAogasEnabled': False,
			'collectUiStats': False,
			'isLongDisconnectedFromCenter': False,
		}, cPickle.HIGHEST_PROTOCOL)
	except Exception:
		pass

	try:
		import sys
		_mo = sys.modules.get('gui.mods.mod_offhangar')
		if _mo is not None:
			_mo.OFFLINE_NICKNAME = nick
			_mo.OFFLINE_SERVER_ADDRESS = DEFAULT_CBT_SERVER_NAME
			_mo.OFFLINE_LOGIN = nick + '@' + DEFAULT_CBT_SERVER_NAME
			_mo.OFFLINE_DBID = dbid
			try:
				from gui.mods.offhangar import _constants as _c_ctx
				_mo.OFFLINE_GUI_CTX = _c_ctx.OFFLINE_GUI_CTX
			except Exception:
				pass
	except Exception:
		pass

	try:
		from gui.mods.offhangar import state as _st
		_activate = getattr(_st, 'activate_profile_state_file', None)
		if callable(_activate):
			if not _activate(state_file):
				from gui.mods.offhangar.logging import LOG_ERROR
				LOG_ERROR('PROFILE_STATE activation failed for nickname key=%s' % nick.lower())
		else:
			_st.STATE_FILE = state_file
	except Exception:
		try:
			from gui.mods.offhangar.logging import LOG_ERROR
			LOG_ERROR('PROFILE_STATE activation exception: %s' % str(sys.exc_info()[1]))
		except Exception:
			pass

	try:
		import BigWorld
		p = BigWorld.player()
		if p is not None and getattr(p, 'isOffline', False):
			p.name = nick
	except Exception:
		pass

	# Persist nickname into offhangar_user/config.json as well


def get_active_nickname():
	if _ACTIVE_PROFILE.get('nickname'):
		return str(_ACTIVE_PROFILE['nickname'])
	reg = load_profiles_registry()
	if reg.get('last_nickname'):
		return str(reg['last_nickname'])
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS as _CFG
		n = str(_CFG.get('nickname', '') or '').strip()
		if n and n not in ('Defaultplayer', 'Player'):
			return n
	except Exception:
		pass
	try:
		import os as _os_p, xml.etree.ElementTree as _ET_p
		_appdata_p = _os_p.environ.get('APPDATA', '')
		if _appdata_p:
			_px_file = _os_p.join(_appdata_p, 'Wargaming.net', 'WorldOfTanks', 'preferences.xml')
			if _os_p.isfile(_px_file):
				_tree_p = _ET_p.parse(_px_file)
				_u_node = _tree_p.find('.//loginInfo/user')
				if _u_node is not None and _u_node.text:
					_u_str = str(_u_node.text).strip()
					if _u_str and _u_str not in ('Defaultplayer', 'Player'):
						return _u_str
	except Exception:
		pass
	return 'kistoprav'


_CBT_MODAL_COMPONENTS = []
_CBT_MODAL_ACTIVE = [False]


def close_cbt_welcome_modal():
	global _CBT_MODAL_COMPONENTS
	if not _CBT_MODAL_ACTIVE[0] and not _CBT_MODAL_COMPONENTS:
		return
	_CBT_MODAL_ACTIVE[0] = False
	try:
		import GUI
		for comp in list(_CBT_MODAL_COMPONENTS):
			try:
				comp.visible = False
				GUI.delRoot(comp)
			except Exception:
				pass
		_CBT_MODAL_COMPONENTS = []
		GUI.reSort()
	except Exception:
		_CBT_MODAL_COMPONENTS = []
	try:
		from gui.InputHandler import g_instance as g_ih
		if g_ih is not None and hasattr(g_ih, 'onKeyDown'):
			g_ih.onKeyDown -= _on_cbt_modal_key
	except Exception:
		pass
	try:
		import BigWorld
		BigWorld.playSound('/gui/buttons/default')
	except Exception:
		pass


def _on_cbt_modal_key(event):
	if not _CBT_MODAL_ACTIVE[0]:
		return False
	try:
		if not event.isKeyDown():
			return False
		import Keys
		k = event.key
		close_keys = (
			getattr(Keys, 'KEY_RETURN', 28),
			getattr(Keys, 'KEY_NUMPADENTER', 156),
			getattr(Keys, 'KEY_SPACE', 57),
			getattr(Keys, 'KEY_ESCAPE', 1),
			getattr(Keys, 'KEY_LEFTMOUSE', 256),
		)
		if k in close_keys:
			close_cbt_welcome_modal()
			return True
	except Exception:
		pass
	return False


def _show_native_gui_welcome_modal(title_u, body_u, nick_u):
	"""Render a crisp, native BigWorld GUI modal window in the center of the Hangar screen."""
	return False
	global _CBT_MODAL_COMPONENTS
	close_cbt_welcome_modal()
	try:
		import GUI
		import BigWorld

		def _set(comp, attr, val):
			try:
				setattr(comp, attr, val)
			except Exception:
				pass

		# 1. Outer gold border frame
		border = GUI.Simple('gui/maps/icons/offhangar/internal_debug_pixel.dds')
		_set(border, 'horizontalPositionMode', 'CLIP')
		_set(border, 'verticalPositionMode', 'CLIP')
		_set(border, 'widthMode', 'CLIP')
		_set(border, 'heightMode', 'CLIP')
		_set(border, 'horizontalAnchor', 'CENTER')
		_set(border, 'verticalAnchor', 'CENTER')
		_set(border, 'position', (0.0, 0.04, 0.08))
		_set(border, 'width', 1.16)
		_set(border, 'height', 0.92)
		_set(border, 'materialFX', 'BLEND')
		_set(border, 'colour', (194, 152, 68, 235))
		_set(border, 'visible', True)
		GUI.addRoot(border)
		_CBT_MODAL_COMPONENTS.append(border)

		# 2. Inner dark slate background panel
		panel = GUI.Simple('gui/maps/icons/offhangar/internal_debug_pixel.dds')
		_set(panel, 'horizontalPositionMode', 'CLIP')
		_set(panel, 'verticalPositionMode', 'CLIP')
		_set(panel, 'widthMode', 'CLIP')
		_set(panel, 'heightMode', 'CLIP')
		_set(panel, 'horizontalAnchor', 'CENTER')
		_set(panel, 'verticalAnchor', 'CENTER')
		_set(panel, 'position', (0.0, 0.04, 0.07))
		_set(panel, 'width', 1.148)
		_set(panel, 'height', 0.904)
		_set(panel, 'materialFX', 'BLEND')
		_set(panel, 'colour', (14, 18, 24, 244))
		_set(panel, 'visible', True)
		GUI.addRoot(panel)
		_CBT_MODAL_COMPONENTS.append(panel)

		# 3. Header title text
		hdr = GUI.Text()
		hdr_str = (u'[ %s — %s ]' % (title_u.upper(), nick_u)).encode('utf-8')
		_set(hdr, 'text', hdr_str)
		_set(hdr, 'horizontalPositionMode', 'CLIP')
		_set(hdr, 'verticalPositionMode', 'CLIP')
		_set(hdr, 'horizontalAnchor', 'CENTER')
		_set(hdr, 'verticalAnchor', 'TOP')
		_set(hdr, 'position', (0.0, 0.45, 0.05))
		_set(hdr, 'font', 'default_medium.font')
		_set(hdr, 'colour', (255, 215, 110, 255))
		_set(hdr, 'multiline', False)
		_set(hdr, 'shadow', True)
		_set(hdr, 'dropShadow', True)
		_set(hdr, 'visible', True)
		GUI.addRoot(hdr)
		_CBT_MODAL_COMPONENTS.append(hdr)

		# 4. Body text (wrapped cleanly into lines for GUI.Text multiline rendering)
		wrapped_lines = []
		for para in body_u.split(u'\n'):
			p_s = para.strip()
			if not p_s:
				wrapped_lines.append(u'')
				continue
			words = p_s.split(u' ')
			cur_l = u''
			for w in words:
				if not cur_l:
					cur_l = w
				elif len(cur_l) + 1 + len(w) <= 78:
					cur_l = cur_l + u' ' + w
				else:
					wrapped_lines.append(cur_l)
					cur_l = w
			if cur_l:
				wrapped_lines.append(cur_l)

		body_comp = GUI.Text()
		_set(body_comp, 'text', (u'\n'.join(wrapped_lines)).encode('utf-8'))
		_set(body_comp, 'horizontalPositionMode', 'CLIP')
		_set(body_comp, 'verticalPositionMode', 'CLIP')
		_set(body_comp, 'horizontalAnchor', 'LEFT')
		_set(body_comp, 'verticalAnchor', 'TOP')
		_set(body_comp, 'position', (-0.53, 0.36, 0.05))
		_set(body_comp, 'font', 'default_small.font')
		_set(body_comp, 'colour', (235, 238, 245, 255))
		_set(body_comp, 'multiline', True)
		_set(body_comp, 'widthMode', 'CLIP')
		_set(body_comp, 'heightMode', 'CLIP')
		_set(body_comp, 'width', 1.06)
		_set(body_comp, 'height', 0.68)
		_set(body_comp, 'shadow', True)
		_set(body_comp, 'dropShadow', True)
		_set(body_comp, 'visible', True)
		GUI.addRoot(body_comp)
		_CBT_MODAL_COMPONENTS.append(body_comp)

		# 5. Footer dismiss button prompt
		ftr = GUI.Text()
		ftr_u = (
			u'[ Нажмите ENTER, ПРОБЕЛ, ЛКМ или ESC, чтобы принять и начать ЗБТ ]'
			if is_ru_locale() else
			u'[ Press ENTER, SPACE, Left Click, or ESC to accept and start CBT ]'
		)
		_set(ftr, 'text', ftr_u.encode('utf-8'))
		_set(ftr, 'horizontalPositionMode', 'CLIP')
		_set(ftr, 'verticalPositionMode', 'CLIP')
		_set(ftr, 'horizontalAnchor', 'CENTER')
		_set(ftr, 'verticalAnchor', 'BOTTOM')
		_set(ftr, 'position', (0.0, -0.37, 0.05))
		_set(ftr, 'font', 'default_small.font')
		_set(ftr, 'colour', (130, 235, 140, 255))
		_set(ftr, 'multiline', False)
		_set(ftr, 'shadow', True)
		_set(ftr, 'dropShadow', True)
		_set(ftr, 'visible', True)
		GUI.addRoot(ftr)
		_CBT_MODAL_COMPONENTS.append(ftr)

		GUI.reSort()
		_CBT_MODAL_ACTIVE[0] = True
		try:
			from gui.InputHandler import g_instance as g_ih
			if g_ih is not None and hasattr(g_ih, 'onKeyDown'):
				g_ih.onKeyDown += _on_cbt_modal_key
		except Exception:
			pass
		try:
			BigWorld.playSound('/gui/buttons/default')
		except Exception:
			pass
		return True
	except Exception:
		close_cbt_welcome_modal()
		return False


_CBT_SESSION_WELCOME_SHOWN = [False]

def should_show_cbt_welcome():
	# Disabled in Offline Battles 2.0 (release): no CBT/Beta popup on hangar entry
	return False
	if _CBT_SESSION_WELCOME_SHOWN[0]:
		return False
	_CBT_SESSION_WELCOME_SHOWN[0] = True
	try:
		state = _load_cbt_state()
		state['welcome_v102_shown'] = True
		_save_cbt_state(state)
	except Exception:
		pass
	return True
	if not os.path.exists(CBT_WELCOME_FLAG_FILE):
		return True
	try:
		with open(CBT_WELCOME_FLAG_FILE, 'r') as f:
			d = json.load(f)
		if not isinstance(d, dict) or not d.get('welcome_v94_shown'):
			return True
	except Exception:
		return True
	return False


def mark_cbt_welcome_shown():
	_ACTIVE_PROFILE['cbt_welcome_shown'] = True
	_ACTIVE_PROFILE['cbt_welcome_v94_shown'] = True
	_ACTIVE_PROFILE['is_new_profile'] = False
	try:
		if not os.path.isdir('offhangar_user'):
			os.makedirs('offhangar_user')
		with open(CBT_WELCOME_FLAG_FILE, 'w') as f:
			json.dump({
				'welcome_v90_shown': True,
				'welcome_v94_shown': True,
				'shown_at': int(time.time()),
				'nickname': get_active_nickname(),
			}, f, indent=2)
	except Exception:
		pass


def show_cbt_welcome_notification(force=False):
	"""Disabled in Offline Battles 2.0 (release)."""
	return False

	import BigWorld
	from gui.mods.offhangar.logging import LOG_NOTE

	# Wait until Hangar UI is actually active and currentInterface == 'hangar'
	try:
		from gui import WindowsManager
		wm = getattr(WindowsManager, 'g_windowsManager', None)
		win = getattr(wm, 'window', None) if wm is not None else None
		bw = getattr(wm, 'battleWindow', None) if wm is not None else None
		if bw is not None or win is None:
			return False
		if getattr(win, 'currentInterface', None) != 'hangar':
			return False
	except Exception:
		return False

	nick = _to_unicode_smart(get_active_nickname())
	title_u = tr('welcome_title')
	body_u = tr('welcome_body')
	sysmsg_u = tr('welcome_sysmsg', nick=nick)

	# 1. Show Native Centered Modal Window in Hangar (BigWorld C++ GUI.Simple + GUI.Text)
	dialog_shown = _show_native_gui_welcome_modal(title_u, body_u, nick)
	if dialog_shown:
		LOG_NOTE('CBT_WELCOME native GUI modal displayed for player=%s lang=%s' % (nick.encode('utf-8'), detect_client_language()))

	# 2. Push into the Hangar Notification Center (bottom-right SystemMessagesInterface) so contacts remain in Service Channel
	sysmsg_pushed = False
	try:
		from gui import SystemMessages as _SM
		push_nat = getattr(_SM, '_offh_push_native', None)
		if callable(push_nat):
			sysmsg_pushed = bool(push_nat(sysmsg_u.encode('utf-8')))
			if _ACTIVE_PROFILE.get('is_new_profile'):
				toast_u = tr('profile_created_toast', nick=nick, dbid=int(_ACTIVE_PROFILE.get('dbid', 13028161)))
				push_nat(toast_u.encode('utf-8'))
			if sysmsg_pushed:
				LOG_NOTE('CBT_WELCOME pushed to Hangar SystemMessagesInterface for player=%s' % nick.encode('utf-8'))
	except Exception as _sme:
		LOG_NOTE('CBT_WELCOME sysmsg err: %s' % str(_sme))

	if dialog_shown or sysmsg_pushed:
		mark_cbt_welcome_shown()
		return True
	return False


# ===========================================================================
# CBT Veteran / Hall-of-Fame Special Bots Registry
# ===========================================================================

DEFAULT_CBT_VETERAN_BOTS = [
	{
		'name': 'kistoprav',
		'role_ru': u'Автор и главный разработчик Offline Battles 2.0',
		'role_en': u'Creator & Lead Developer of Offline Battles 2.0',
		'favorite_vehicles': [
			'ussr:T_50_2',
			'france:AMX_50_100',
			'france:AMX_13_90',
			'ussr:IS-7',
			'germany:Maus',
		],
		'forced_skill': 'hard',
		'ace_tuning': {
			'reload_mult': 0.86,
			'aim_mult': 0.76,
			'vision_mult': 1.12,
			'hp_mult': 1.08,
			'crew_level': 116.0,
		},
	},
	{
		'name': 'fr3asikx',
		'role_ru': u'Пионер оффлайн-ангара 0.8.2 и соавтор репозитория',
		'role_en': u'0.8.2 Offline Hangar Pioneer & Repository Co-Author',
		'favorite_vehicles': [
			'germany:E-100',
			'germany:E-75',
			'germany:PzVIB_Tiger_II',
			'germany:PzVI',
		],
		'forced_skill': 'hard',
		'ace_tuning': {
			'reload_mult': 0.88,
			'aim_mult': 0.78,
			'vision_mult': 1.10,
			'hp_mult': 1.06,
			'crew_level': 115.0,
		},
	},
	{
		'name': 'pengw0048',
		'role_ru': u'Исследователь нативной физики и навигации ботов',
		'role_en': u'Native Bot Physics & Navigation Researcher',
		'favorite_vehicles': [
			'germany:E-100',
			'ussr:IS-4',
			'usa:T110',
			'ussr:Object268',
			'ussr:T-54',
		],
		'forced_skill': 'hard',
		'ace_tuning': {
			'reload_mult': 0.88,
			'aim_mult': 0.78,
			'vision_mult': 1.10,
			'hp_mult': 1.06,
			'crew_level': 115.0,
		},
	},
	{
		'name': 'Sudo',
		'role_ru': u'Разработчик мода OFFHANGAR 2.0 и консультант по ИИ ботов',
		'role_en': u'Co-Developer of OFFHANGAR 2.0 & Bot AI Consultant',
		'favorite_vehicles': [
			'ussr:IS-7',
			'ussr:T62A',
			'france:Bat_Chatillon25t',
			'germany:E-100',
			'ussr:T-54',
			'ussr:IS-3',
		],
		'forced_skill': 'hard',
		'ace_tuning': {
			'reload_mult': 0.88,
			'aim_mult': 0.78,
			'vision_mult': 1.10,
			'hp_mult': 1.06,
			'crew_level': 115.0,
		},
	},
	{
		'name': 'Arrofur',
		'role_ru': u'Разработчик мода OFFHANGAR 2.0, тактический консультант и тестер ЗБТ',
		'role_en': u'Co-Developer of OFFHANGAR 2.0, Tactical Consultant & CBT Tester',
		'favorite_vehicles': [
			'ussr:IS-7',
			'germany:E-75',
			'ussr:T_50_2',
			'usa:T30',
			'france:AMX_50_100',
		],
		'forced_skill': 'hard',
		'ace_tuning': {
			'reload_mult': 0.88,
			'aim_mult': 0.78,
			'vision_mult': 1.10,
			'hp_mult': 1.06,
			'crew_level': 115.0,
		},
	},
	{
		'name': 'FireToG',
		'role_ru': u'Разработчик оффлайн-мода для WoT 0.9.3 и участник ЗБТ',
		'role_en': u'Developer of the WoT 0.9.3 Offline Mod & CBT Tester',
		'favorite_vehicles': [
			'france:Bat_Chatillon25t',
			'france:AMX_13_90',
			'ussr:T62A',
			'ussr:IS-7',
			'france:AMX_50_100',
		],
		'forced_skill': 'hard',
		'ace_tuning': {
			'reload_mult': 0.88,
			'aim_mult': 0.78,
			'vision_mult': 1.10,
			'hp_mult': 1.06,
			'crew_level': 115.0,
		},
	},
	{
		'name': 'KorbenDallas',
		'role_ru': u'Михаил WoT — блогер по оффлайн-модам и участник ЗБТ',
		'role_en': u'Mikhail WoT — Offline Mods Blogger & CBT Tester',
		'favorite_vehicles': [
			'germany:E-100',
			'germany:Maus',
			'germany:E-75',
			'germany:VK4502P',
		],
		'forced_skill': 'hard',
		'ace_tuning': {
			'reload_mult': 0.88,
			'aim_mult': 0.78,
			'vision_mult': 1.10,
			'hp_mult': 1.06,
			'crew_level': 115.0,
		},
	},
	{
		'name': 'AnatoliKarpov',
		'role_ru': u'Разработчик приватного сервера, администратор и участник ЗБТ',
		'role_en': u'Private Server Developer, Community Admin & CBT Tester',
		'favorite_vehicles': [
			'usa:T57_58',
			'usa:T54E1',
			'usa:T69',
			'usa:T71',
		],
		'forced_skill': 'hard',
		'ace_tuning': {
			'reload_mult': 0.88,
			'aim_mult': 0.78,
			'vision_mult': 1.10,
			'hp_mult': 1.06,
			'crew_level': 115.0,
		},
	},
	{
		'name': 'Brigadier',
		'role_ru': u'Модератор сообщества Михаил WoT и участник ЗБТ',
		'role_en': u'Community Moderator & CBT Tester',
		'favorite_vehicles': [
			'france:AMX_50Fosh_155',
			'germany:JagdPz_E100',
			'france:F10_AMX_50B',
			'france:Bat_Chatillon25t',
			'france:AMX_50_100',
			'france:AMX_13_90',
		],
		'forced_skill': 'hard',
		'ace_tuning': {
			'reload_mult': 0.88,
			'aim_mult': 0.78,
			'vision_mult': 1.10,
			'hp_mult': 1.06,
			'crew_level': 115.0,
		},
	},
]

CBT_VETERANS_SCHEMA_VERSION = 2


def load_cbt_veteran_bots():
	"""Load the Hall-of-Fame / Veteran Bots list (merging defaults + offhangar_user/cbt_veterans.json)."""
	veterans = [dict(v) for v in DEFAULT_CBT_VETERAN_BOTS]
	seen_names = dict((v['name'].lower(), idx) for idx, v in enumerate(veterans))
	needs_save = False
	try:
		if not os.path.exists(CBT_VETERANS_FILE):
			needs_save = True
		else:
			with open(CBT_VETERANS_FILE, 'r') as f:
				data = json.load(f)
			if isinstance(data, dict):
				file_schema = int(data.get('schema_version', 1) or 1)
				if file_schema < CBT_VETERANS_SCHEMA_VERSION:
					needs_save = True
				for entry in (data.get('veterans') or ()):
					if isinstance(entry, dict) and entry.get('name'):
						nm_low = str(entry['name']).strip().lower()
						if nm_low not in seen_names:
							veterans.append(entry)
							seen_names[nm_low] = len(veterans) - 1
						elif file_schema >= CBT_VETERANS_SCHEMA_VERSION:
							veterans[seen_names[nm_low]] = entry
		if needs_save:
			if not os.path.isdir('offhangar_user'):
				os.makedirs('offhangar_user')
			with open(CBT_VETERANS_FILE, 'w') as f:
				json.dump({
					'schema_version': CBT_VETERANS_SCHEMA_VERSION,
					'_comment_ru': 'Зал славы разработчиков, консультантов и участников ЗБТ Offline Battles 2.0. Каждый участник получает любимые танки, скилл hard (Ас) и индивидуальные бонусы ТТХ!',
					'_comment_en': 'Offline Battles 2.0 Hall of Fame (Developers, Consultants & CBT Veterans). Each contributor gets signature favorite tanks, forced hard (Ace) AI skill, and custom vehicle tuning!',
					'spawn_chance_per_battle': 0.65,
					'veterans': veterans,
				}, f, indent=2)
	except Exception:
		pass
	return veterans


def get_cbt_veteran_by_name(bot_name):
	if not bot_name:
		return None
	nm_low = str(bot_name).strip().lower()
	for v in load_cbt_veteran_bots():
		if str(v.get('name', '')).strip().lower() == nm_low:
			return v
	return None


# ===========================================================================
# Authentic 2012-Era Bot Nickname Database (520+ RU & 520+ EU/EN Nicknames)
# Strictly obeys official WoT nickname rules ([A-Za-z0-9_], 3..24 chars)
# ===========================================================================

BOT_NICKS_RU_2012 = (
	# --- 1. Visual Cyrillic / Leet-Latin 2012 Style (читаются визуально как русские слова) ---
	'CTPEJIOK_1945', 'T_A_H_K_U_C_T', 'CAH9I_CHAUIIEP', 'LLI_M_E_JI_b', 'K_O_M_B_A_T',
	'B_O_JI_K_O_D_A_B', 'D_E_D_M_A_3_A_U', 'II_P_O_P_B_E_M_C_9I', 'C_T_A_JI_b_H_O_U', 'X_A_H_T_E_P',
	'K_P_A_C_H_bI_U', 'B_O_E_B_O_D_A', 'M_E_D_B_E_D_b', '3_B_E_P_O_B_O_U', 'C_E_B_E_P_H_bI_U',
	'II_O_JI_K_O_B_H_U_K', 'B_P_O_H_E_B_O_U', 'T_A_P_A_H_77', 'CTAJIbHOU_KYJIAK', 'IIPu3PAK_CTEIu',
	'BOPOH_HO4HOu', 'rPOM_C_HE6A', '4EPHbIu_DEMOH', '6EJbIu_TIrP', 'KOCAPb_PAKOB',
	'IIOJIbCKUu_BOuH', 'CAH9I_B_TAHKE', 'JIEXA_HA_KB', 'CEPEXA_1978', 'MIXA_T34',
	'BOBAH_TAruJI', 'D9ID9I_BAC9I', 'C_H_A_U_II_E_P_88', 'r_B_A_P_D_E_E_C', 'K_A_II_U_T_A_H',
	'M_A_U_O_P_B_U_X_P_b', 'C_T_A_P_LLI_U_H_A', 'B_E_T_E_P_A_H_BOB', 'O_D_U_H_O_4_K_A', 'II_A_P_T_U_3_A_H',
	'3_A_CA_D_A_B_KYCTAX', 'H_E_II_P_O_6_U_JI', 'P_U_K_O_LLI_E_T', '6_O_E_K_O_M_II_JIEKT', 'r_Y_C_JI_9I_C6UTA',
	'C_B_E_T_JI_9I_4_O_K', '4_E_M_O_D_A_H_APTbI', 'K_Y_C_T_O_B_O_U', '6_P_O_H_E_6_O_U_LLI_UK', '3_A_P_9I_D_U_JI_O_F',
	'II_E_P_E_3_A_P_9I_D_K_A', 'H_A_B_O_D_4_U_K_6Or', 'M_E_X_B_O_D_OT_6OrA', 'K_O_M_A_H_D_U_P_POTbI', 'C_U_6_U_P_9I_K_54',
	'Y_P_A_JI_E_C_66', 'K_A_3_A_K_D_O_H_C_K_O_U', 'M_O_P_II_E_X_1942', 'D_E_C_A_H_T_H_U_K_BDB', 'P_A_3_B_E_D_4_U_K_07',
	'C_M_E_P_4_B_CTEIu', '6_Y_P_9I_B_IIYCTbIHE', 'C_T_A_JI_U_H_r_P_A_D', 'K_Y_P_C_K_A_9I_DYrA', 'II_P_O_X_O_P_O_B_K_A',
	'6_A_r_P_A_T_U_O_H_44', 'M_A_P_LLI_A_JI_3YKOB', 'K_O_JIO_6_A_H_O_B_KB1', 'JIA_B_P_U_H_E_H_K_O', 'O_C_K_U_H_T34',
	'6_E_C_II_O_LLI_A_D_HbIu', 'r_P_O_3_A_TUrPOB', '3_B_E_P_O_6_O_U_152', 'C_O_B_E_T_C_K_U_U_C0I03', 'H_A_C_H_E_D_O_r_O_H_9IT',
	'II_O_E_X_A_JI_U_B_6OU', 'C_JII_U_B_A_H_E_6YDET', 'T_A_LLI_U_M_6_O_U', 'B_A_H_LLI_O_T_OT_APTbI', 'K_A_P_T_O_LLI_K_A_PRO',
	'O_JI_E_H_E_B_O_D_2012', 'K_P_A_6_B_TAHKE', 'P_A_K_O_O_6_P_A_3_HbIu', 'C_T_A_T_U_C_T_58', 'H_A_r_U_6_A_T_O_P_RU1',
	'B_O_U_H_C_B_E_T_A', 'T_E_M_H_bI_U_JII_O_P_D', 'C_T_A_JI_K_E_P_3OHbI', 'M_O_H_O_JI_U_T_2012', 'C_B_O_6_O_D_A_RU',
	'D_O_JI_r_O_B_E_C', '6_A_T_9I_B_3DAHUu', 'D_E_D_H_A_OCOTA', 'C_bI_H_IIOJIKA', '6_P_A_T_U_LLI_K_A',
	'3_E_M_JI_9I_K_74', 'K_Y_3_6_A_C_C_42', 'A_JI_T_A_U_22_RU', '6_A_U_K_A_JI_38', 'E_H_U_C_E_U_24',
	'B_O_JI_r_A_64', 'K_Y_6_A_H_b_23', 'D_O_H_61_CTC', 'H_E_B_A_78_SPB', 'M_O_C_K_B_A_77',
	'M_U_H_C_K_BY', 'K_U_E_B_1943', 'K_A_P_A_r_A_H_D_A', 'B_JI_A_D_U_B_O_C_T_O_K', 'X_A_6_A_P_O_B_C_K_27',
	'M_Y_P_M_A_H_C_K_51', 'A_P_X_A_H_r_E_JI_b_CK', 'C_E_B_A_C_T_O_II_O_JI_b', 'K_A_JI_U_H_U_H_r_P_A_D', 'T_Y_JI_A_OPy3EuHUK',
	'U_3_E_B_C_K_AK47', 'H_O_4_H_O_U_D_O_3_O_P', 'D_H_E_B_H_O_U_D_O_3_O_P', 'C_Y_M_P_A_K_2012', 'T_U_X_U_U_O_M_Y_T',
	'4_E_P_T_U_3_TA6AKEPKu', 'JII_E_C_H_U_K_B_KYCTAX', 'r_P_U_6_H_U_K_HA_IIT', 'O_X_O_T_H_U_K_3A_APTOu', 'P_bI_6_A_K_1975',
	'LLI_A_X_T_E_P_DONBASS', 'M_E_T_A_JI_JI_Y_P_r', 'C_B_A_P_LLI_U_K_6PA3P', 'T_O_K_A_P_b_C_3ABODA', 'K_P_A_H_O_B_LLI_U_K',
	'D_A_JI_b_H_O_6_O_U_LLI_UK', 'M_A_LLI_U_H_U_C_T_P3D', 'JII_E_T_4_U_K_AC', 'LLI_T_Y_P_M_O_B_U_K_UJI2', 'K_P_E_U_C_E_P_BAP9Ir',
	'A_D_M_U_P_A_JI_KOJIL4AK', 'r_E_H_E_P_A_JI_M_O_P_O_3', '3_U_M_H_U_U_C_H_E_r', 'B_I0_r_A_C_E_B_E_P_A', '6_Y_P_A_H_O_P_E_H_6YPr',
	'T_A_U_r_A_C_U_6_U_P_b', 'A_M_Y_P_C_K_U_U_TUrP', 'C_H_E_3_H_bI_U_6APC', 'C_T_E_II_H_O_U_O_P_E_JI', 'C_O_K_O_JI_9I_C_H_bI_U',
	'6_E_P_K_Y_T_K_A_B_K_A_3', 'K_O_P_LLI_Y_H_II_O_JI_E_U', '9I_C_T_P_E_6_A_T_A_K_U', 'B_O_P_O_H_C_T_A_JI_U', 'C_O_B_A_H_O_4_H_A_9I',
	'K_A_6_A_H_C_E_K_A_4', 'JII_O_C_b_T_A_E_3_H_bI_U', 'P_O_C_O_M_A_X_A_86', 'P_bI_C_b_JII_E_C_H_A_9I', 'M_A_H_Y_JI_3_JII_O_U',
	'K_O_T_6_E_r_E_M_O_T', 'M_A_C_T_E_P_U_M_A_P_rO', 'LLI_A_P_U_K_O_B_1925', 'II_P_O_F_IIPEO6PA3EHCKUu', 'O_C_T_A_II_6_E_H_D_E_P',
	'K_U_C_A_B_O_P_O_6_b9IHUHOB', 'LLI_Y_P_A_6_A_JI_A_r_A_H_OB', 'II_A_H_U_K_O_B_C_K_U_U', 'r_JII_E_6_3_E_r_JII_O_B', 'B_O_JII_O_D_9I_LLIAPAJIOB',
	'M_E_C_T_b_B_C_T_P_E_4_U', '6_E_JII_O_E_C_O_JII_H_C_E', 'B_E_P_E_LLI_A_r_U_H_12', 'T_O_B_A_P_U_LLI_C_Y_X_O_B', 'C_A_U_D_C_T_P_E_JI_9I_JI',
	'r_I0_JI_b_4_A_T_A_U', 'A_6_D_Y_JI_JI_A_6A3A', 'K_U_H_D_3_A_D_3_A_KY', 'r_P_A_B_U_C_A_II_A_2012', 'II_E_II_E_JII_A_C_JII_E_T_U_T',
	'C_E_M_H_A_D_C_A_T_b_M_rH', 'LLI_T_U_P_JII_U_C_1945', 'M_I0_JII_JII_E_P_r_E_C_T', 'B_6_O_U_U_D_Y_T_CTAPUKu', 'M_A_E_C_T_P_O_T_U_T_A_P_E_HKO',
	'K_Y_3_H_E_4_U_K_C_6_U_JI', 'C_M_Y_r_JII_9I_H_K_A_44', 'H_A_B_O_U_H_E_KAK_HA_BOuHE', 'M_A_JII_E_LLI_K_U_H_CY100', '6_9I_K_O_C_K_U_U_HA_T34',
	'3_A_B_O_D_4_A_H_U_H_RU', 'II_O_C_JII_E_C_M_E_H_bI', 'II_O_D_II_U_B_K_O_2012', 'C_O_C_E_D_C_IIEPFOPATOPOM', 'r_A_P_A_3_H_bI_U_6AT9I',
	'K_A_P_6_I0_P_A_T_O_P_2106', '3_U_r_Y_JII_U_K_O_II_E_U_KA', 'H_U_B_A_4X4_B_rP9I3U', 'Y_A_3_6_Y_X_A_H_K_A', 'K_A_M_A_3_M_A_C_T_E_P',
	'6_E_JI_A_3_K_A_P_b_E_P', 'T_P_A_K_T_O_P_6_E_JI_A_PYC', 'K_U_P_O_B_E_C_K700', '4_E_JI_9I_6_U_H_E_C_T100', 'Y_P_A_JI_JII_E_C_O_B_O_3',
	'6_P_O_H_E_II_O_E_3_D_14', 'T_A_4_A_H_K_A_P_O_C_T_OB', '6_Y_D_E_H_O_B_E_C_1920', 'K_O_H_A_P_M_U_9I_1', '4_A_II_A_E_B_E_C_YPAJI',
	'II_A_H_F_U_JII_O_B_E_C_28', '3_A_M_O_C_K_B_Y_1941', 'H_U_LLI_A_r_Y_H_A_3_A_D', '3_A_P_O_D_U_H_Y_1943', 'H_A_6_E_P_JII_U_H_1945',
	'3_H_A_M_9I_II_O_6_E_D_bI', 'r_B_A_P_D_U_U_C_E_P_3AHT', 'C_T_A_P_LLI_U_U_JIEuTEHAHT', 'K_O_M_6_A_T_6AT9IH9I', 'P_O_T_H_bI_U_1986',
	'D_E_M_6_E_JI_b_1989', 'K_A_H_T_E_M_U_P_O_B_E_C', 'T_A_M_A_H_E_C_rBAPDU9I', 'D_3_E_P_3_U_H_E_C_OMCH', 'B_U_T_9I_3_b_CIIECHA3',
	'A_JI_b_F_A_A_H_T_U_T_E_P', 'B_bI_M_II_E_JI_1981', 'C_A_T_Y_P_H_IIO4TUGHEBHbIu', 'K_O_C_M_O_H_A_B_T_CCCP', 'B_O_C_T_O_K_1_1961',
	'6_Y_P_A_H_1988_CCCP', 'M_U_P_C_T_A_H_C_U_9I', 'C_II_Y_T_H_U_K_1957', 'JII_Y_H_O_X_O_D_1970', 'M_A_P_C_O_X_O_D_RU',
	'K_Y_3_b_K_U_H_A_M_A_T_b', 'C_A_P_b_II_Y_LLI_K_A', 'A_T_O_M_H_bI_U_JIE_D_OKOJ', 'JII_E_H_U_H_1959_ICE', 'A_P_K_T_U_K_A_IIOJI9IP',
	'C_E_B_M_O_P_II_Y_T_b', 'K_O_JII_bI_M_A_MArADAH', '9I_K_Y_T_C_K_M_O_P_O_3', 'H_O_P_U_JI_b_C_K_HUKEJIb', 'B_O_P_K_Y_T_A_YrOJIb',
	'C_Y_P_r_Y_T_H_E_F_T_b', 'T_I0_M_E_H_b_72_RU', 'O_M_C_K_55_ABAHrAPD', 'H_O_B_O_C_U_6_U_P_C_K_54', 'K_P_A_C_H_O_9I_P_C_K_24',
	'U_P_K_Y_T_C_K_A_H_r_A_PA', '4_U_T_A_3A6AuKAJIbE', 'Y_JII_A_H_Y_D_E_03', '6_JII_A_r_O_B_E_LLI_EHCK', 'I0_3_H_O_C_A_X_A_JII_UHCK',
	'II_E_T_P_O_II_A_B_JII_OBCK', 'E_K_A_T_E_P_U_H_6_Y_P_r', 'H_U_3_H_U_U_T_A_r_U_JI', 'M_A_r_H_U_T_O_r_O_P_C_K', '3_JII_A_T_O_Y_C_T_74',
	'M_U_A_C_C_Y_P_A_JI', 'K_Y_P_r_A_H_45_RU', 'O_P_E_H_6_Y_P_r_56', 'Y_F_A_C_A_JII_A_B_A_T', 'K_A_3_A_H_b_AK_6APC',
	'H_A_6_E_P_E_3_H_bIE_4EJI', 'C_A_M_A_P_A_rOPODOK', 'T_O_JII_b_9I_T_T_U_BA3', 'C_A_P_A_T_O_B_64_BOJIrA', 'B_O_JII_r_O_r_P_A_D_34',
	'A_C_T_P_A_X_A_H_b_30', 'P_O_C_T_O_B_II_A_II_A', 'K_P_A_C_H_O_D_A_P_93', 'C_O_4_U_2014_RU', 'H_O_B_O_P_O_C_C_U_U_C_K',
	'C_T_A_B_P_O_II_O_JI_b_26', 'B_O_P_O_H_E_3_36_RU', 'JII_U_II_E_C_K_48_MET', 'T_A_M_6_O_B_C_K_U_U_BOJIK', 'K_Y_P_C_K_46_C_O_JII_OBEu',
	'6_E_JI_r_O_P_O_D_31', 'O_P_E_JI_57_rBAPDU9I', '6_P_9I_H_C_K_U_U_JII_EC', 'C_M_O_JII_E_H_C_K_67', 'T_B_E_P_b_69_BOJIrA',
	'9I_P_O_C_JII_A_B_JI_b_76', 'K_O_C_T_P_O_M_A_44', 'U_B_A_H_O_B_O_37', 'B_JII_A_D_U_M_U_P_33', 'P_9I_3_A_H_b_BDB_62',
	'K_A_JII_Y_r_A_40_KOCMOC', 'H_U_3_H_U_U_HOBROPOD_52', 'K_U_P_O_B_B_9I_T_K_A_43', 'C_bI_K_T_bI_B_K_A_P_11', 'B_O_JII_O_r_D_A_35',
	'4_E_P_E_II_O_B_E_C_CTAJIb', 'II_C_K_O_B_76_DBD', 'B_E_JII_U_K_U_U_HOBrOPOD', 'I_O_LLI_K_A_P_O_JII_A_12', '4_E_6_O_K_C_A_P_bI_21',
	'C_A_P_A_H_C_K_13_RU', 'II_E_H_3_A_58_CYPA', 'Y_JII_b_9I_H_O_B_C_K_73',

	# --- 2. Classic 2012 RU Translit, Meme, Uncle & Tank Culture Nicknames ---
	'xXx_Vasyan_xXx', 'SLAVA_TANKAM', 'TIGER_YOLOKA', 'Ne_Slivay_Nub', 'ZaSSSR_1945',
	'Suvorov_Krut', 'Kotik_Murzik', 'Feed_Menya_Feed', 'Zolotoy_Fyodor', 'ANTIRASH',
	'GAGARIN_LETAL', 'Boroda_Tankista', 'Yoba_Strelok', 'PIVO_I_TANKI', 'Vasilich_Pyanyy',
	'Krutoy_Perec', 'DIVAN_VOIN', 'Kavo_Tam_Slivaem', 'Sanya_Iz_Barnaula', 'ELITBOT',
	'HUD_S_GORY', 'Malinovka_Bog', 'ProTankist_98', 'SVAROG_UBIVAET', 'Mamkin_Navodchik',
	'Dedushka_Vanya', 'Nagibator2012', 'Zapasnoy_Vyhod', 'Ya_Iz_Kolomny', 'Batya_Prishel',
	'Nikita_Pobeditel', 'CAMPER_KING', 'Zheka_The_Best', 'Vodka_Medved', 'Serega_Bombit',
	'TANKODROM_HOZAIN', 'Rezinovaya_Utka', 'ARTA_ZAORALA', 'Kolya_Bezobidnyi', 'YoburgTank',
	'Poehali_Che', 'ZaVODU_v_BOY', 'Zvonkiy_Klyuch', 'Chuvak_Iz_Podvala', 'STALIN_BY_ODOBRIL',
	'Ryba_S_Trolleybusa', 'Grustnyi_Panda', 'PolzuchiyTapok', 'Dima_Ne_Slivay', 'Kentavr_1990',
	'Serega_1976_Ural', 'Petrovich_Na_KV', 'Mihaly4_Tankist', 'Sanek_1988_RU', 'Dimon_Pro_Tanki',
	'Andrey_Krasnodar', 'Leha_Sibiryak', 'Vovan_Nizhniy', 'Zheka_Rostov_61', 'Kolyan_Tula_71',
	'Igorek_Samara', 'Romario_SPB', 'Max_Kamchatka', 'Gena_Chelyabinsk', 'Valera_Nastalo_Vremya',
	'Oleg_Tankodrom', 'Yuriy_Gagarin_61', 'Slavik_Brest', 'Artur_Pirozhkov_12', 'Vitalik_1985',
	'Evgeniy_Omsk_55', 'Pavel_Ekaterinburg', 'Denis_Voronezh_36', 'Anton_Perm_59', 'Ruslan_Kazan_16',
	'Timur_Ufa_02', 'Marat_Saratov', 'Gleb_Kaliningrad', 'Ilya_Muromets_33', 'Dobrynya_Nikitich',
	'Alyosha_Popovich', 'Ded_Pakhto', 'Komdiv_Chapaev', 'Petka_Pulemetchik', 'Krasnoarmeec_Suhoff',
	'Marshal_Rokossovskiy', 'General_Panfilov', 'Katukova_Gvardiya', 'Tankist_1941_1945', 'Uralvagonzavod_Tagil',
	'Chelyabinskiy_Traktor', 'Kirovskiy_Zavod_KV', 'Kharkovskiy_T34', 'Sormovskiy_Rabochiy', 'Zavodchanin_Posle_Smeny',
	'Pod_Pivko_Na_Arte', 'S_Zavoda_V_Boy', 'Tamozhnya_Daet_Dobro', 'Beloe_Solnce_Pustyni', 'Brigada_Tankistov',
	'Brat_2_Danila', 'V_Chem_Sila_Brat', 'Schas_Spoyu_Volk', 'Nu_Pogodi_Zayac', 'Kot_Matroskin_Pro',
	'Sharik_Prostokvashino', 'Pochtalion_Pechkin', 'Dyadya_Fedor_T62', 'Kuzma_Domovoy', 'Babayka_Pod_Krovatyu',
	'Kiber_Kotleta_2012', 'Statist_Iz_Roty', 'Komandir_Absolyutki', 'Chempionskaya_Rota', 'Srednyaya_Rota_KV1S',
	'Kvas_Kak_Tank', 'Hellcat_Kustovoy', 'T50_2_Motorchik', 'Type59_Staya', 'Maus_Na_Malinovke',
	'IS7_Dedushka', 'BatChat_Baraban', 'GwPanther_Snaiper', 'Hummel_Chertik', 'Buratino_S51_Bum',
	'Holodilnik_SU14', 'Valenok_Valentine', 'Utkonos_M6', 'Tapok_VK4502P', 'Ferdi_Slonik',
	'Jagdtiger_128mm', 'Obekt_704_Blyamba', 'ISU152_BL10_Privet', 'KV2_Shaytan_Truba', 'Hetzer_Fugaska',
	'MS1_Groznyi', 'Traktor_Ltraktor', 'BDR_G1B_Krasavec', 'ELC_AMX_Tarakan', 'Chaffee_Svetlyak',
	'Tolstopard_VK2801', 'Borshch_V_Kustah', 'Sovok_Nagnul', 'Nemec_Snaiper', 'Francuz_Na_Kolesah',
	'Amerikanec_Bashnya', 'Kustovoy_ Выползень'.replace(' Выползень', '_Vypolzen'), 'Olenemetr_Zashkalil', 'Bez_Shansov_Na_Pobedu', 'Svetlyak_Po_Centru',
	'Arta_Ne_Spi', 'Kto_Svetit_Pole', 'Ne_Berite_Bazu', 'Dobivayte_Vseh', 'Ostavte_Mne_Voina',
	'Medal_Kolobanova', 'Voin_Na_KV1', 'Razvedchik_Malinovki', 'Stalnaya_Stena_IS4', 'Snaiper_Prohorovki',
	'Kosa_Smerti_2012', 'Zveroboy_SU152', 'Groza_Myshey', 'Lev_Sinaya', 'Ohotnik_Na_Tigrov',
	'Belyi_Tigr_2012', 'Chernaya_Akula_50', 'Nochnoy_Ohotnik_28', 'Aligator_K52', 'Berkut_Su47',
	'Russkiy_Vityaz', 'Strizhi_Kubinka', 'Tamanskiy_Polk', 'Kantemirovskaya_Div', 'Pskovskiy_Desant',
	'Morskaya_Pehota_SF', 'Tihookeanskiy_Flot', 'Chernomorskiy_Moryak', 'Baltiyskiy_Veter', 'Severnyi_Flot_69',
	'Kapitan_Nemo_RU', 'Mayor_Grom_SPB', 'Polkovnik_Isaev', 'General_Karbyshev', 'Admiral_Ushakov',
	'Admiral_Nakhimov', 'Aleksandr_Nevskiy_42', 'Dmitriy_Donskoy_80', 'Kuzma_Minin_NN', 'Dmitriy_Pozharskiy',
	'Ivan_Susanin_44', 'Denis_Davydov_1812', 'Mihail_Kutuzov_12', 'Petr_Bagration_12', 'Aleksey_Brusilov_16',
	'Vasiliy_Zaycev_42', 'Ivan_Kozhedub_62', 'Aleksandr_Pokryshkin', 'Aleksey_Maresyev', 'Viktor_Talalihin',
	'Nikolay_Gastello', 'Zoya_Kosmodemyanskaya', 'Valeriy_Chkalov_37', 'Mihail_Gromov_37', 'Georgiy_Baevskiy',
	'Dmitriy_Lavrichenko', 'Zinoviy_Kolobanov', 'Aleksandr_Oskin_44', 'Pavel_Gudзь'.replace('зь', 'z'), 'Semen_Konovalov_KV',
	'Vladimir_Bochkovskiy', 'Ivan_Lyubushkin', 'Konstantin_Samohin', 'Mihail_Katukov_1GTA', 'Pavel_Rotmistrov_5GTA',
	'Pavel_Rybalko_3GTA', ' Semen_Bogdanov'.strip(), 'Dmitriy_Lelyushenko', 'Ivan_Konev_1UF', 'Konstantin_Rokossovskiy',
	'Georgiy_Zhukov_1BF', 'Aleksandr_Vasilevskiy', 'Rodion_Malinovskiy', 'Fedor_Tolbuhin', 'Leonid_Govorov_Len',
	'Ivan_Bagramyan_1PF', 'Kirill_Mereckov', 'Ivan_Chernyahovskiy', 'Nikolay_Vatutin', 'Andrey_Eremenko',
	'Matvey_Zaharov', 'Semen_Timoshenko', 'Kliment_Voroshilov_KV', 'Semen_Budennyi_Kon', 'Mihail_Tuhachevskiy',
	'Boris_Shaposhnikov', 'Aleksey_Antonov', 'Sergey_Shtemenko', 'Pavel_Sudoplatov', 'Nikolay_Kuznecov_R',
	'Rihard_Zorge_Ramsay', 'Yan_Berzin_GRU', 'Lev_Mehlis', 'Yakov_Fedorenko_GABTU', 'Zhosef_Kotin_KB',
	'Mihail_Koshkin_T34', 'Aleksandr_Morozov_HKB', 'Nikolay_Duhov_KV', 'Vasiliy_Grabin_ZIS', 'Fedor_Petrov_D25T',
	'Boris_Shavyrin_Min', 'Georgiy_Shpagin_PPSh', 'Vasiliy_Degtyarev_DP', 'Mihail_Kalashnikov_47', 'Sergey_Simonov_SKS',
	'Fedor_Tokarev_TT', 'Nikolay_Makarov_PM', 'Igor_Stechkin_APS', 'Evgeniy_Dragunov_SVD', 'Pavel_oha_Tank'.replace('oha_', ''),
	'Ded_V_Shleme', 'Babulya_Na_Arte', 'Vnuk_Tankista', 'Syn_Polka_1943', 'Starshina_Vaskov',
	'Zhenya_Komelkova', 'Rita_Osyanina', 'Liza_Brichkina', 'Sonya_Gurvich', 'Galya_Chetvertak',
	'A_Zori_Zdes_Tihie', 'Oni_Srazhalis_Za', 'Goryachiy_Sneg_42', 'Batalony_Prosyat_Ognya', 'Zhivye_I_Mertvye',
	'Serpilin_Komdiv', 'Sintsov_Poltruk', 'Osvobozhdenie_1970', 'Tsvetaev_Kapitan', 'Zharkoe_Leto_43',
	'Chetyre_Tankista_Pes', 'Yanek_Kos_Rudy102', 'Grigoriy_Saakashvili', 'Gustlik_Yelen', 'Sharik_Ovcharka',
	'Ekipazh_Mashiny_Boevoy', 'Tri_Tankista_Druga', 'Na_Granice_Tuchi', 'Bronya_Krepka_I_Tanki', 'Marsh_Sovetskih_Tank',
	'Proshchanie_Slavyanki', 'Den_Pobedy_9Maya', 'Katyusha_Na_Beregu', 'Smuglyanka_Moldavanka', 'Temnaya_Noch_1943',
	'V_Zemlyanke_Ogon', 'Sinij_Platochek_42', 'Zhuravli_Gamzatov', 'Nam_Nuzhna_Odna_Pobeda', 'Desantnyi_Batalon',
)

BOT_NICKS_EN_2012 = (
	# --- 1. 2012 EU/NA Historical Tank Aces, Commanders & Armor Buffs ---
	'Otto_Carius_217', 'Kurt_Knispel_168', 'Michael_Wittmann_007', 'Lafayette_Pool_US', 'In_The_Mood_Sherman',
	'Creighton_Abrams_37', 'George_Patton_3rd', 'Erwin_Rommel_DAK', 'Heinz_Guderian_XIX', 'Erich_von_Manstein',
	'Bernard_Montgomery_UK', 'Desert_Rat_7thArmd', 'Bovington_Curator', 'Kubinka_Researcher', 'Aberdeen_Tester',
	'Saumur_Char_B1', 'Munster_Panzer_DE', 'Tiger_131_Tunisia', 'Wardaddy_Collier', 'Oddball_KellysHeroes',
	'Moriarty_NoNegWaves', 'Big_Joe_Kellys', 'Crapgame_Hustler', 'Ghost_Division_7th', 'Screaming_Eagles_101',
	'Red_Ball_Express_44', 'Big_Red_One_1stID', 'Spearhead_3rdArmored', 'Hell_On_Wheels_2nd', 'Super_Sixth_Armored',
	'Sherwood_Rangers_UK', 'Black_Watch_Highland', 'Coldstream_Guards_UK', 'Royal_Tank_Regiment', 'Eighth_Army_Alamein',
	'Hussar_Winged_PL', 'Rudy_102_Szarik', 'Janek_Kos_102', 'Gustlik_Jelen_PL', 'Maczek_1stArmored_PL',
	'Leclerc_2eDB_FR', 'Billotte_Eure_1940', 'De_Gaulle_Montcornet', 'Bir_Hakeim_1942', 'Normandy_Bocage_44',
	'Ardennes_Bastogne_44', 'El_Alamein_1942', 'Tobruk_Rats_1941', 'Kursk_Citadel_1943', 'Villers_Bocage_44',
	'Arracourt_Patton_44', 'Falaise_Pocket_1944', 'Remagen_Bridge_1945', 'Seelow_Heights_1945', 'Monte_Cassino_1944',
	'Market_Garden_Arnhem', 'Operation_Goodwood', 'Operation_Totalize', 'Operation_Cobra_44', 'Operation_Crusader',
	'Operation_Compass_40', 'Operation_Torch_42', 'Operation_Husky_43', 'Operation_Overlord', 'Operation_Bagration',
	'Kasserine_Pass_1943', 'Sidi_Bou_Zid_1943', 'El_Guettar_M10_TD', 'Anzio_Beachhead_44', 'Salerno_Landings_43',
	'Caen_Carpiquet_1944', 'Hill_112_Normandy', 'Mortain_Counter_44', 'St_Vith_Defense_44', 'Elsenborn_Ridge_44',
	'Celles_Panzers_Stop', 'Foy_Easy_Company', 'Carentan_Paratrooper', 'Sainte_Mere_Eglise', 'Utah_Beach_4thID',
	'Omaha_Beach_29thID', 'Gold_Beach_50thDiv', 'Juno_Beach_Canada', 'Sword_Beach_3rdUK', 'Dieppe_Raid_Churchill',
	'Dunkirk_Rearguard_40', 'Arras_Matilda_1940', 'Sedan_Crossing_1940', 'Hannut_Tank_Battle', 'Gembloux_Gap_1940',
	'Beda_Fomm_1941', 'Halfaya_Pass_88mm', 'Gazala_Cauldron_42', 'Mersa_Matruh_1942', 'Alam_Halfa_Ridge',
	'Mareth_Line_Flank', 'Medenine_6pdr_ Trap'.replace(' ', ''), 'Tebourba_Tunisia_42', 'Ortona_Little_Stalingrad', 'Rimini_Gothic_Line',

	# --- 2. 2012 EU/NA Public Match Memes, Slang & Community Archetypes ---
	'Siema_PL_1998', 'Kto_PL_2012', 'Pozdro_Z_Polski', 'Husaria_Pancerna', 'CzoIgi_Na_Baze',
	'Moin_Meister_DE', 'Panzerknacker_DE', 'Stahlgewitter_88', 'Jagdpanzer_Hans', 'Sauerkraut_Power',
	'Bratwurst_Kommandant', 'Kruppstahl_Enjoyer', 'Acht_Acht_Flak_DE', 'Schwerer_Gustav_DE', 'Donnerbalken_89',
	'Tea_And_Crumpets_UK', 'Jolly_Good_Shot_UK', 'Keep_Calm_And_Tank', 'Rule_Britannia_Cent', 'Boiling_Vessel_Ready',
	'Pound_6_Popgun', 'Pound_17_Firefly', 'Royal_Ordnance_L7', 'HMS_Tog_II_Captain', 'Landship_Mark_IV',
	'Baguette_Autoloader', 'HonHon_AMX_13_90', 'Croissant_Sniper_FR', 'Lorraine_40t_Clip', 'BatChat_Assassin',
	'Foch_155_Clip_Out', 'Somua_S35_Classic', 'Char_B1_Bis_Boss', 'ELC_AMX_PocketRocket', 'AMX_50B_HeavyMedium',
	'Czech_Mate_Praha', 'Skoda_Works_Pilsen', 'Hetzer_Praga_38t', 'Svejk_Good_Soldier', 'Pilsner_Tanker_CZ',
	'IKEA_Stridsvagn_SE', 'Carolus_Rex_Sweden', 'Bofors_40mm_Auto', 'Viking_Raider_NO', 'Suomi_Sisu_T26E',
	'White_Death_Simo', 'Molotov_Cocktail_39', 'Magyar_Huszar_HU', 'Toldi_Turul_Bp', 'Balkan_Tigers_RS',
	'Campinovka_Camper', 'Himmelsdorf_Hill_King', 'El_Halluf_Sniper_Ridge', 'Murovanka_MagicForest', 'Prokhorovka_BushScout',
	'Komarin_Island_Push', 'Erlenberg_Bridge_Guard', 'Mines_Center_Rush', 'Ensk_City_Brawler', 'Lakeville_Valley_Noob',
	'Ruinberg_Field_Flank', 'cliff_lighthouse_lt', 'Sand_River_Dune_King', 'Westfield_Viaduct_TD', 'Karelia_Swamp_Hold',
	'Steppes_Open_Field', 'Redshire_Castle_Hill', 'Fjords_South_Corner', 'Mountain_Pass_Bridge', 'Arctic_Region_Corner',
	'Fishermans_Bay_Mid', 'Siegfried_Line_Bunker', 'Live_Oaks_Rail_Push', 'Highway_Field_Scout', 'Widepark_Railroad',
	'Serene_Coast_Peninsula', 'Dragon_Ridge_Terrace', 'South_Coast_Beach', 'Port_Rail_Yard_82', 'Airfield_Rocky_Hill',
	'Lemming_Train_Driver', 'No_Cap_Kill_All', 'Dont_Cap_More_XP', 'Cap_Fast_They_Coming', 'Defend_The_Base_Now',
	'Gold_Ammo_Spotted', 'Type59_Wolfpack_Leader', 'Lowe_Credit_Farmer', 'KV5_R2D2_Hide_It', 'T34_HullDown_Beast',
	'SuperPershing_Spaced', 'Jagdtiger_88_Printer', 'IS6_Angled_Pike', 'Churchill_III_XP', 'Matilda_IV_Bounce',
	'T14_Angled_Hull', 'Ram_II_PewPew_Gun', 'T2_Light_SpeedDemon', 'M22_Locust_Swarm', 'T127_Mini_Mouse',
	'Minimaus_Pz38H_735f', 'Hotchkiss_Tier2_God', 'Tetrarch_NewYear_Gift', 'M3_Stuart_LendLease', 'Valentine_II_Scout',
	'A_32_PreOrder_Vet', 'M6A2E1_Mutant_Beta', 'PzV_IV_Alpha_Tester', 'KV_220_Beta_Tester', 'BT_SV_Speed_Box',
	'PzII_J_Unpenetrable', 'MTLS_1G14_TwinGun', 'SU_85I_StuG_Sister', 'Dicker_Max_Depression', 'FCM_36_Pak_40_Vision',
	'LeFH18B2_PermaTrack', 'Sexton_Artillery_UK', 'T1_Cunningham_Pro', 'LOLtraktor_Sniper', 'MS_1_Master_Tanker',
	'Derp_KV2_152mm', 'KV1S_122mm_D25T_OP', 'Hetzer_105mm_OneShot', 'M4_Sherman_105_Derp', 'PzIV_105mm_GoldDerp',
	'VK2801_Cougar_Derp', 'T50_2_Physics_Drift', 'Chaffee_600m_Optics', 'M18_Hellcat_InvisiTD', 'T49_Speedy_TurretTD',
	'Slugger_M36_90mm', 'T25_AT_Fast_Jagd', 'T28_Prototype_Turret', 'T95_Doom_Turtle_13kmh', 'T30_155mm_Boomstick',
	'T110E4_Turreted_155', 'T110E3_LowerPlate_Who', 'T110E5_Cupola_Dance', 'M48A1_Patton_RoF', 'M46_Patton_SnapShot',
	'M26_Pershing_Mantlet', 'T20_Flanker_90mm', 'T29_God_Tier7_Heavy', 'T32_Unbreakable_Turret', 'M103_Heat_Slinger',
	'T57_Heavy_4_Shell_Clip', 'T54E1_Autoloader_Med', 'T69_90mm_Drum_Clip', 'T71_Drift_Scout_76', 'T21_Light_Flanker',
	'IS7_Stalinium_Turret', 'IS4_Side_Armor_King', 'IS8_Fast_Heavy_T10', 'IS3_Magical_SideArmor', 'IS_122mm_Brawler',
	'KV4_Fortress_107mm', 'ST_I_HullDown_Monster', 'KV3_Fire_Extinguisher', 'T_150_Angled_Brick', 'KV1_Bogatyr_Armor',
	'T_62A_Laser_Gun_Med', 'T_54_Heat_Brawler', 'T_44_Wet_Ammo_Rack', 'T_43_Workhorse_Med', 'T_34_85_Rudy_Legend',
	'T_34_57mm_Zis4_DPM', 'Object_268_Camo_TD', 'Object_704_BL10_Fear', 'ISU_152_TrollCannon', 'SU_152_Zveroboy_HE',
	'SU_100_122mm_Alpha', 'SU_85_107mm_Memories', 'Object_263_Open_FastTD', 'SU_122_54_DPM_Sniper', 'SU_101_Uralmash_1',
	'Object_261_AP_Sniper', 'Object_212_Splash_King', 'S_51_Unicorn_203mm', 'SU_14_Schoolbus_Arty', 'SU_8_Over_The_Hill',
	'SU_26_Rotating_Turret', 'Maus_3000HP_Superheavy', 'E100_150mm_Alpha_Slap', 'E75_King_Of_Tier9', 'Tiger_II_Konigstiger',
	'Tiger_I_Long_88mm', 'VK3601H_Konisch_Gun', 'VK4502P_Rear_Turret', 'VK4502A_Heavium_Push', 'Lowe_Sniper_Heavy',
	'E50M_Ultimate_Rammer', 'E50_Tier9_LaserGun', 'Panther_II_Long_88', 'Panther_L100_Needle', 'VK3002DB_Sloped_Med',
	'JagdPz_E100_170mm_Boom', 'Jagdtiger_Pak44_DPM', 'Ferdinand_Elefant_200', 'Jagdpanther_II_HullDown', 'Jagdpanther_88_L71',
	'StuG_III_Bush_Ninja', 'Marder_II_76mm_Pak36', 'GW_Type_E_Orbital', 'GW_Tiger_210mm_Mortar', 'GW_Panther_Wide_Arc',
	'Hummel_Mobile_Arty', 'Grille_150mm_Short', 'Wespe_CounterBattery', 'Centurion_Mk7_1_HESH', 'FV4202_HESH_Slinger',
	'Centurion_Mk1_20pdr', 'Comet_Gun_Depression', 'Cromwell_Wolfpack_Speed', 'Crusader_Light_Flank', 'FV215b_Rear_Laser_HT',
	'Conqueror_120mm_L1A1', 'Caernarvon_HullDown', 'Black_Prince_SlowPush', 'Churchill_VII_TrackHit', 'Churchill_I_TopGun',

	# --- 3. Classic Early-2010s Western Gamer & Clan Tags ---
	'xX_ShadowSniper_Xx', 'xX_DarkKnight_Xx', 'Ghost_Recon_EU', 'Silent_Assassin_99', 'Iron_Wolf_84',
	'Steel_Viper_UK', 'Night_Stalker_DE', 'Thunder_Bolt_PL', 'Silver_Bullet_77', 'Lone_Wolf_Tanker',
	'Cpt_Price_SAS_22', 'Soap_MacTavish_UK', 'Master_Chief_117', 'Marcus_Fenix_COG', 'Gordon_Freeman_MIT',
	'Cmdr_Shepard_N7', 'Geralt_of_Rivia_PL', 'Ezio_Auditore_IT', 'Dovahkiin_2011', 'Niko_Bellic_LC',
	'Max_Payne_Noir', 'Solid_Snake_Fox', 'Duke_Nukem_3D', 'Serious_Sam_CRO', 'Doom_Marine_1993',
	'Quake_Railgun_99', 'Unreal_FlakCannon', 'Counter_Strike_16', 'Leeroy_Jenkins_Pal', 'Arthas_Menethil_WC3',
	'Jim_Raynor_Terran', 'Siege_Tank_Arclite', 'Kirov_Reporting_RA2', 'Apocalypse_Tank_Sov', 'Prism_Tank_Allied',
	'Mammoth_Tank_GDI', 'Kane_Lives_In_Death', 'Wololo_Monk_AoE2', 'Teutonic_Knight_DE', 'Longbowman_Agincourt',
	'SirFoch_Salt_Mine', 'Jingles_Salt_Mines', 'QuickyBaby_Fan_UK', 'BohemianEagle_Navy', 'SilentStalker_FTR',
	'Overlord_Dev_Blog', 'SerB_Moonbase_Nerf', 'How_Terrible_SerB', 'Working_As_Intended', 'RNGesus_25_Percent',
	'Sixth_Sense_Bulb', 'Brothers_In_Arms_5', 'Repair_Kit_On_Cooldown', 'Jack_Of_All_Trades', 'Safe_Stowage_Loader',
	'Snap_Shot_Gunner', 'Smooth_Ride_Driver', 'Clutch_Braking_Turn', 'Off_Road_Driving_Pro', 'Deadeye_Crit_Master',
	'Eagle_Eye_Commander', 'Designated_Target_2s', 'Camo_Net_And_Binocs', 'Rammer_Stab_Optics', 'Vents_Rammer_GLD',
	'Top_Gun_6_Kills', 'Radley_Walters_8', 'Pools_Medal_10_Kills', 'Kolobanov_1v5_Win', 'Fadin_Last_Shell',
	'Halonen_Medal_TD', 'Pascucci_Arty_Hunter', 'Burda_Medal_5_SPGs', 'Dumitru_Medal_4_SPGs', 'Orlik_Light_Tank_Ace',
	'Tamada_Yoshio_Scout', 'Tarczay_5_Kills', 'Bruno_Pietro_Medal', 'Billotte_Survived', 'Steel_Wall_11_Hits',
	'Defender_Base_Reset', 'Invader_100_Cap', 'Scout_9_Spotted', 'Patrol_Duty_Assist', 'Confederate_6_Damaged',
	'Sniper_85_Accuracy', 'Kamikaze_Higher_Tier', 'Reaper_Streak_10', 'Master_Gunner_Streak', 'Sharpshooter_Streak',
)


def pick_localized_bot_pool(style='era'):
	"""Return the appropriate 520+ bot nickname tuple for the active client language."""
	if str(style or '').lower() == 'callsign':
		return (
			'Atlas', 'Badger', 'Comet', 'Echo', 'Falcon', 'Frost',
			'Hawk', 'Jade', 'Kestrel', 'Lynx', 'Meteor', 'Nomad',
			'Orion', 'Raven', 'Rook', 'Saber', 'Scout', 'Talon', 'Viper', 'Wolf'
		)
	return BOT_NICKS_RU_2012 if is_ru_locale() else BOT_NICKS_EN_2012


# ===========================================================================
# Localized In-Battle Bot Chat & Callout Tables (RU & EU/EN)
# ===========================================================================

CHAT_LINES_EN = {
	'greet': [
		u'Good luck guys, let\'s win this!', u'Roll out boys, don\'t camp at spawn',
		u'GL team! Heavies take the main push, mediums flank',
		u'Play smart guys, no yolo rush please', u'Hey team, let\'s carry this one!',
	],
	'greet_common': [
		u'GL HF everyone!', u'Good luck and have fun :)', u'GL to everyone except the red team ;)',
		u'Hello tankers! Let\'s have a great battle', u'gl hf all', u'Red team, surrender now while you still can :)',
	],
	'greet_common_reply': [
		u'GL HF to you too :)', u'We\'ll see about that, %(speaker)s!', u'Haha, come and get us ;)',
		u'Good luck! See you in my sights', u'Keep dreaming, %(speaker)s :)',
	],
	'frag': [
		u'Got him! %(victim)s sent back to the garage', u'Back to garage, %(victim)s!',
		u'One down (%(victim)s), keep pushing!', u'Target destroyed!',
	],
	'player_frag': [
		u'Nice shot, %(player)s!', u'Great kill, %(player)s!',
		u'Clean finish, %(player)s!', u'Well played %(player)s, respect!',
	],
	'died': [
		u'Damn, they got me... hold the flank guys', u'Ugh, almost angled in time. Finish them off!',
		u'I\'m out, avenge me team!', u'Perma-tracked in the open, unlucky...',
	],
	'ally_lost': [
		u'Careful, %(victim)s just went down there', u'Watch out for %(killer)s, don\'t peek one by one',
		u'Man down, don\'t feed %(killer)s free shots',
	],
	'player_died': [
		u'Unlucky %(player)s, we\'ll avenge you!', u'Hold the line boys, let\'s win this for %(player)s!',
		u'Good try %(player)s, we got it from here',
	],
	'taunt': [u'Catch that :)', u'Straight back to the garage!', u'Don\'t sit in the open ;)'],
	'enemy_punish_taunt': [
		u'Missed me, %(player)s! Now it\'s my turn :)',
		u'Bounced? Alright %(player)s, here I come!',
		u'Shouldn\'t have fired that shot, %(player)s ;)',
		u'Thanks for the reload window, pushing in!',
	],
	'common_banter': [
		u'Hey red team, did you grow roots in those bushes? :)',
		u'%(enemy)s, come out from behind that rock and fight!',
		u'What kind of stalinium armor is on that %(enemy)s?!',
		u'Now this is a proper brawl, great fight!',
	],
	'common_banter_reply': [
		u'Be right with you, %(speaker)s, hold your horses :)',
		u'You push first, our tea is still hot ;)',
		u'Pure angled steel, %(speaker)s, get used to it!',
		u'Yeah, awesome match so far!',
	],
	'low_hp': [
		u'I\'m a one-shot, pulling back to cover', u'Low HP here, someone take the hit for me!',
		u'Barely alive, playing second line support now',
	],
	'hold': [u'Hold the ridge and let them push into our guns', u'Don\'t overextend, let them come to us'],
	'advance': [u'They\'re weak on this flank, push now!', u'Press the attack while they\'re disorganized!'],
	'advance_cleanup': [u'Let\'s clean up the last ones, push in!', u'Move in and finish the survivors!'],
	'cant_pen': [u'Can\'t pen his front at all, need to flank or track him'],
	'help_call': [u'Need help on this flank, we\'re getting overrun!', u'Getting pushed hard here, need backup!', u'Help needed on my position!'],
	'help_go': [u'On my way, hang in there for a few seconds!', u'Rolling in to back you up!', u'Aiming at your flank now, hold on!'],
	'attack_target': [u'Focus fire on %(target)s!', u'Everyone shoot %(target)s, he\'s overextended!'],
	'support_fire': [u'Need support fire on %(target)s!', u'Help me take down %(target)s!'],
	'warn_stop': [
		u'%(target)s, hold up! %(enemy)s is pre-aimed around that corner!',
		u'%(target)s, stop, don\'t peek — %(enemy)s is waiting for you!',
		u'%(target)s, careful, %(enemy)s has that lane locked down!',
	],
	'warn_strong': [
		u'%(target)s, don\'t brawl %(enemy)s head-on, wait for support!',
		u'%(target)s, watch out, %(enemy)s is heavily armored there!',
		u'%(target)s, don\'t trade HP with %(enemy)s alone!',
	],
	'warn_back': [
		u'%(target)s, pull back now, %(enemy)s has a shot on you!',
		u'%(target)s, reverse into cover, don\'t stay exposed to %(enemy)s!',
	],
	'warn_arty': [
		u'%(target)s, get arty safe, SPG is aiming at you!',
		u'%(target)s, keep moving, you\'re spotted in the open for arty!',
		u'%(target)s, hug hard cover, enemy arty is dialed in on you!',
	],
	'warn_ack': [
		u'Copy that, reversing now!',
		u'Roger, holding position, thanks mate!',
		u'Pulled back behind cover, ty!',
	],
	'punish_push': [
		u'%(target)s just fired! Push him while he\'s reloading!',
		u'Nice, %(target)s missed! Rush him on reload!',
		u'%(target)s is on cooldown, let\'s punish him now!',
	],
	'arty_bait_request': [
		u'%(ally)s, bait %(target)s out from cover for a second, I\'m fully aimed!',
		u'Team, keep %(target)s spotted in the open — arty shell ready!',
		u'%(ally)s, light up %(target)s behind that house, SPG ready to fire!',
	],
	'arty_bait_ack': [
		u'Baiting him into the open for you now, hold your aim!',
		u'Copy that arty, drawing him out of cover!',
		u'Watch the corner, he\'s about to chase me into the open!',
	],
	'arty_reposition': [
		u'Hill is blocking my arc, relocating for a clear shot',
		u'Moving to a better firing angle, no arc from here',
	],
	'hit_relocate': [
		u'Taking unspotted fire from the bushes! Relocating now',
		u'Getting sniped from stealth, pulling back behind the ridge',
		u'Ouch, invisible TD in the bushes! Backing off',
	],
	'hit_watch_map': [
		u'Took a side hit from stealth, holding cover — scouts check the bushes!',
		u'Enemy TD camping that bush line, waiting for a spot',
	],
	'arty_pie_hunt': [
		u'Enemy arty spotted! Dibs on the SPG :)',
		u'I see %(target)s arty, going in for the kill!',
		u'Arty lit up, leave that juicy target to me! :)',
	],
	'tracer_blind': [
		u'Saw an enemy tracer from that bush, sending a blind shot!',
		u'Caught your tracer in the bushes, catch this counter-battery shell!',
		u'Blind-firing that bush where the tracer came from!',
	],
	'banter_reload': [
		u'%(ally)s, cover me for a moment, reloading (%(sec)s s left)!',
		u'Reloading for %(sec)s sec, hold the corner guys!',
		u'On reload right now, keep them off me!',
	],
	'banter_reload_ack': [
		u'Got you covered, %(ally)s, holding the lane!',
		u'Reload safely, blocking the corner for you!',
		u'Copy that, watching the angle!',
	],
	'banter_flank': [
		u'%(ally)s, help me finish off %(enemy)s here!',
		u'We\'ve got %(enemy)s isolated, put shots into his side!',
	],
	'banter_flank_ack': [
		u'On it, aiming at him now!',
		u'Let\'s send this %(enemy)s straight to the garage!',
	],
	'banter_winning': [
		u'Great push boys! Just keep an eye on our base cap :)',
		u'Rolling them up nicely, keep the pressure on!',
	],
	'banter_losing': [
		u'Stay sharp team! Play hull-down from defense and we can turn this around',
		u'Don\'t peek alone, group up behind cover and focus fire!',
	],
	'ack': [u'+', u'Copy that', u'Roger, on it', u'Affirmative', u'Got it!'],
}

CHAT_REPLIES_EN = [
	((u'help', u'sos', u'cover', u'\u043f\u043e\u043c\u043e\u0433', u'\u0445\u0435\u043b\u043f'), [u'On my way!', u'Coming to help, hang on!', u'Rolling in to support you']),
	((u'hi', u'hello', u'hey', u'gl', u'\u043f\u0440\u0438\u0432\u0435\u0442', u'\u043a\u0443'), [u'Hey there!', u'GL HF mate!', u'Hello commander!']),
	((u'attack', u'push', u'go', u'rush', u'\u0430\u0442\u0430\u043a', u'\u0432\u043f\u0435\u0440\u0435\u0434', u'\u0434\u0430\u0432\u0438\u043c'), [u'Let\'s push!', u'+', u'Right behind you!']),
	((u'back', u'base', u'def', u'reset', u'\u043d\u0430\u0437\u0430\u0434', u'\u0431\u0430\u0437', u'\u0434\u0435\u0444'), [u'Falling back to defend base', u'Copy, heading back to reset']),
	((u'gg', u'wp', u'\u0433\u0433'), [u'GG!', u'gg wp']),
	((u'?',), [u'Not sure, check the minimap', u'Watch the flanks']),
	((), [u'+', u'Roger', u'Copy that', u'Affirmative']),
]
