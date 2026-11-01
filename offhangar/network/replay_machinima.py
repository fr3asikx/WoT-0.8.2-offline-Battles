# -*- coding: utf-8 -*-
"""
replay_machinima.py — Полноценная система записи и воспроизведения реплеев боёв WoT 0.8.2 (`offhangar`)
и песочница многодублевой анимации (Machinima) для съёмки сценок и технодемок.

Особенности v5.7:
1. Поддержка чтения как `.offh_replay.json`, так и бинарных контейнеров `.wotreplay` (извлечение JSON-блока #2),
   а также перехват запуска `.wotreplay` двойным кликом из Проводника Windows (`BigWorld._offh_autostart_replay_path`)
   с ожиданием полной готовности Ангара (`g_hangarSpace.spaceInited`), автоматическим выбором танка и команды из реплея.
2. Полная блокировка живого ИИ и физики (`player_physics` / `bot_loop` / `simulate_frame`) во время воспроизведения реплея (`F9`)
   с прямой подачей координат в `WGVehicleFilter2` (`fast_seed_and_output`) и матрицы `Servo`, чтобы все 30 танков,
   башни, орудия, прицел и выстрелы воспроизводились 1-в-1.
3. Режим многодублевой записи машинимы (`F11` — наложение нового дубля поверх уже записанных дорожек танков),
   позволяющий в одиночку анимировать любые сценки в песочнице.
4. Горячие клавиши в бою:
   - `F9`: Старт / Стоп воспроизведения последнего (или открытого через `.wotreplay`) реплея
   - `F10`: Старт / Стоп / Сохранение записи в `replays/`
   - `F11`: Многодублевая запись Машинимы (проигрывает записанные танки и параллельно пишет ваш текущий танк!)
   - `Space`: Пауза / Продолжить (в режиме реплея)
   - `Стрелки Вверх/Вниз`: Скорость воспроизведения (0.1x .. 4.0x)
   - `Стрелки Влево/Вправо`: Перемотка на -10 / +10 секунд
   - `V`: Свободная камера оператора (полёт на WASD + Q/E + вращение I/J/K/L)
"""

import glob
import json
import math
import os
import struct
import sys
import threading
import time
import zlib

try:
	basestring
except NameError:
	basestring = str

try:
	import BigWorld
	import Math
	import Keys
except Exception:
	BigWorld = None
	Math = None
	Keys = None

try:
	from gui.mods.offhangar.logging import LOG_NOTE, LOG_DEBUG, LOG_ERROR
except Exception:
	def LOG_NOTE(*args): pass
	def LOG_DEBUG(*args): pass
	def LOG_ERROR(*args): pass


def _lerp(a, b, t):
	return a + (b - a) * t


def _lerp_angle(a, b, t):
	diff = (b - a + math.pi) % (2.0 * math.pi) - math.pi
	return a + diff * t


_USER_SETTING_OVERRIDE = None
_SETTINGS_HOOKED = False
_REPLAY_META_CACHE = {}
_REPLAY_PAYLOAD_CACHE = {}
_SAVE_LOCK = threading.Lock()


def is_offhangar_replay_file(filepath):
	"""Быстрая (< 0.1 мс) проверка по первым байтам: наш ли это реплей `offhangar` или ванильный 0.8.2 `.wotreplay`."""
	if not filepath or not isinstance(filepath, basestring) or not os.path.isfile(filepath):
		return False
	low = filepath.lower()
	if low.endswith('.offh_replay.json'):
		return True
	try:
		with open(filepath, 'rb') as rf:
			head = rf.read(16384)
		if len(head) < 16:
			return False
		magic, block_count = struct.unpack('<II', head[:8])
		if magic != 0x11343212:
			return b'"tracks"' in head or b'"offhangar_replay' in head
		b1_len = struct.unpack('<I', head[8:12])[0]
		b1_end = 12 + b1_len
		b1_slice = head[12:min(len(head), b1_end)]
		if b'"offhangar_meta"' in b1_slice or b'"offhangar_replay' in b1_slice:
			return True
		if block_count >= 2:
			if b1_end + 8 <= len(head):
				b2_sig = head[b1_end + 4:min(len(head), b1_end + 128)]
				if b2_sig.startswith(b'OFZ3') or b'"offhangar_replay' in b2_sig or b'"tracks"' in b2_sig:
					return True
			else:
				with open(filepath, 'rb') as rf2:
					rf2.seek(b1_end + 4)
					b2_sig = rf2.read(128)
				if b2_sig.startswith(b'OFZ3') or b'"offhangar_replay' in b2_sig or b'"tracks"' in b2_sig:
					return True
	except Exception:
		pass
	return False


def _extract_cli_wotreplay_path():
	"""Ищет путь к реплею `offhangar` (`.wotreplay` или `.offh_replay.json`), переданный при запуске WorldOfTanks.exe.
	Ванильные зашифрованные `.wotreplay` пропускаются напрямую в нативный C++ `BattleReplay`."""
	try:
		if BigWorld is not None:
			if getattr(BigWorld, '_offh_vanilla_wotreplay_path', None):
				return None
			p = getattr(BigWorld, '_offh_autostart_replay_path', None)
			if p and os.path.isfile(p):
				if is_offhangar_replay_file(p):
					return p
				BigWorld._offh_vanilla_wotreplay_path = p
				BigWorld._offh_autostart_replay_path = None
				return None
	except Exception:
		pass
	try:
		import BattleReplay
		ctrl = getattr(BattleReplay, 'g_replayCtrl', None)
		if ctrl is not None:
			native_ctrl = getattr(ctrl, '_BattleReplay__replayCtrl', None)
			if native_ctrl is not None and hasattr(native_ctrl, 'getAutoStartFileName'):
				fn = native_ctrl.getAutoStartFileName()
				if fn and os.path.isfile(fn):
					if is_offhangar_replay_file(fn):
						if BigWorld is not None:
							BigWorld._offh_autostart_replay_path = fn
						return fn
					else:
						if BigWorld is not None:
							BigWorld._offh_vanilla_wotreplay_path = fn
						return None
	except Exception:
		pass
	try:
		for arg in (sys.argv or ())[1:]:
			if isinstance(arg, basestring):
				clean = arg.strip('"').strip("'")
				low = clean.lower()
				if (low.endswith('.wotreplay') or low.endswith('.offh_replay.json')) and os.path.isfile(clean):
					if is_offhangar_replay_file(clean):
						if BigWorld is not None:
							BigWorld._offh_autostart_replay_path = clean
						return clean
					else:
						if BigWorld is not None:
							BigWorld._offh_vanilla_wotreplay_path = clean
						return None
	except Exception:
		pass
	return None


def sync_replay_setting_on_startup():
	"""Синхронизирует и перехватывает галочку «Записывать бои» и автозапуск `.wotreplay`."""
	global _SETTINGS_HOOKED, _USER_SETTING_OVERRIDE
	_extract_cli_wotreplay_path()
	if _SETTINGS_HOOKED:
		return
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS
		cfg_default = bool(CONFIG_OPTIONS.get('record_replays', True))
	except Exception:
		cfg_default = True

	try:
		import Settings
		prefs = getattr(Settings.g_instance, 'userPrefs', None)
		if prefs is not None and prefs.has_key('scriptsPreferences'):
			sp = prefs['scriptsPreferences']
			if cfg_default:
				try:
					sp.writeBool('enableAutoRecordingBattles', True)
				except Exception:
					pass
	except Exception:
		pass

	try:
		import BattleReplay
		if not getattr(BattleReplay.BattleReplay, '_offh_replay_hooked', False):
			orig_enable = getattr(BattleReplay.BattleReplay, 'enableAutoRecordingBattles', None)
			if callable(orig_enable):
				def _hooked_enable_auto_recording(self_ctrl, enable, *a, **kw):
					global _USER_SETTING_OVERRIDE
					_USER_SETTING_OVERRIDE = True
					try:
						import Settings
						prefs2 = getattr(Settings.g_instance, 'userPrefs', None)
						if prefs2 is not None and prefs2.has_key('scriptsPreferences'):
							prefs2['scriptsPreferences'].writeBool('enableAutoRecordingBattles', True)
					except Exception:
						pass
					return orig_enable(self_ctrl, True, *a, **kw)
				BattleReplay.BattleReplay.enableAutoRecordingBattles = _hooked_enable_auto_recording

			orig_auto_start = getattr(BattleReplay.BattleReplay, 'autoStartBattleReplay', None)
			if callable(orig_auto_start):
				def _hooked_auto_start_replay(self_ctrl, *a, **kw):
					try:
						native_c = getattr(self_ctrl, '_BattleReplay__replayCtrl', None)
						if native_c is not None and hasattr(native_c, 'getAutoStartFileName'):
							fn = native_c.getAutoStartFileName()
							if fn and os.path.isfile(fn):
								if is_offhangar_replay_file(fn):
									if BigWorld is not None:
										BigWorld._offh_autostart_replay_path = fn
									get_replay_manager()._was_cli_autostart = True
									LOG_NOTE('REPLAY_AUTOSTART: detected offhangar replay %s -> routing directly to battle playback!' % fn)
									return False
								else:
									if BigWorld is not None:
										BigWorld._offh_vanilla_wotreplay_path = fn
						offh_rp = _extract_cli_wotreplay_path()
						if offh_rp:
							get_replay_manager()._was_cli_autostart = True
							LOG_NOTE('REPLAY_AUTOSTART: detected offhangar replay %s -> routing directly to battle playback!' % offh_rp)
							return False
					except Exception:
						pass
					return orig_auto_start(self_ctrl, *a, **kw)
				BattleReplay.BattleReplay.autoStartBattleReplay = _hooked_auto_start_replay

			orig_stop = getattr(BattleReplay.BattleReplay, 'stop', None)
			if callable(orig_stop):
				def _hooked_br_stop(self_ctrl, *a, **kw):
					try:
						from gui.mods.offhangar._constants import CONFIG_OPTIONS as _BR_CFG
						if bool(_BR_CFG.get('replay_return_to_login', False)):
							self_ctrl._BattleReplay__quitAfterStop = False
							res = orig_stop(self_ctrl, *a, **kw)
							try:
								from gui.WindowsManager import g_windowsManager
								if g_windowsManager is not None:
									BigWorld.callback(0.1, g_windowsManager.showLogin)
							except Exception:
								pass
							return res
					except Exception:
						pass
					return orig_stop(self_ctrl, *a, **kw)
				BattleReplay.BattleReplay.stop = _hooked_br_stop

			BattleReplay.BattleReplay._offh_replay_hooked = True
		ctrl = getattr(BattleReplay, 'g_replayCtrl', None)
		if ctrl is not None:
			try:
				ctrl._BattleReplay__isAutoRecordingEnabled = bool(cfg_default)
			except Exception:
				pass
			_SETTINGS_HOOKED = True
	except Exception:
		pass


def is_sandbox_machinima_mode():
	"""Возвращает True, когда включены реплеи или песочница машинимы (по умолчанию включено)."""
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS
		if bool(CONFIG_OPTIONS.get('sandbox_machinima_mode', False)):
			return True
		if bool(CONFIG_OPTIONS.get('enable_battle_replays', True)):
			return True
		if bool(CONFIG_OPTIONS.get('record_replays', True)):
			return True
	except Exception:
		pass
	return True


def is_replay_recording_enabled():
	"""Возвращает True, если включена запись боёв (`record_replays` или `enable_battle_replays`)."""
	sync_replay_setting_on_startup()
	if _USER_SETTING_OVERRIDE is not None:
		return bool(_USER_SETTING_OVERRIDE)
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS
		return bool(CONFIG_OPTIONS.get('record_replays', True) or CONFIG_OPTIONS.get('enable_battle_replays', True))
	except Exception:
		return True


def _resolve_replay_dirs(save_dir='replays'):
	"""Находит абсолютные пути к папке `replays/` в корне клиента WoT и `offhangar_user/replays/`."""
	dirs = []
	try:
		here = os.path.abspath(__file__)
		parts = here.replace('\\', '/').split('/')
		if 'res_mods' in parts:
			idx = parts.index('res_mods')
			if idx > 0:
				wot_root = '/'.join(parts[:idx])
				root_replays = os.path.normpath(os.path.join(wot_root, 'replays'))
				if root_replays not in dirs:
					dirs.append(root_replays)
	except Exception:
		pass
	try:
		cwd_replays = os.path.abspath(save_dir or 'replays')
		if cwd_replays not in dirs:
			dirs.append(cwd_replays)
	except Exception:
		if (save_dir or 'replays') not in dirs:
			dirs.append(save_dir or 'replays')
	try:
		cwd_user = os.path.abspath(os.path.join('offhangar_user', 'replays'))
		if cwd_user not in dirs:
			dirs.append(cwd_user)
	except Exception:
		pass
	return dirs


def _build_offhangar_meta(payload_dict):
	"""Формирует компактный (< 3 КБ) заголовок метаданных и ростера для Блока 1 `.wotreplay`,
	чтобы `peek_replay_metadata` и `peek_replay_roster` читали только первые 4 КБ файла без распаковки Блока 2."""
	map_name = str(payload_dict.get('map_name') or 'battle')
	p_id = payload_dict.get('player_id')
	p_veh = str(payload_dict.get('player_vehicle') or 'ussr:T-34')
	p_cd_hex = str(payload_dict.get('player_compact_descr_hex') or '')
	p_max_hp = 0
	p_team = 1
	roster = {}
	tracks = payload_dict.get('tracks') or {}
	for aid_str, trk in tracks.items():
		try:
			aid = int(aid_str)
			frames = trk.get('frames') or ()
			f0 = frames[0] if frames else None
			pos0 = [round(float(f0[1]), 2), round(float(f0[2]), 2), round(float(f0[3]), 2)] if (f0 and len(f0) >= 5) else [0.0, 0.0, 0.0]
			yaw0 = round(float(f0[4]), 4) if (f0 and len(f0) >= 5) else 0.0
			vtype = str(trk.get('vehicle_type') or p_veh)
			cd_hex = str(trk.get('compact_descr_hex') or '')
			mhp = int(trk.get('max_health', 1000) or 1000)
			tm = int(trk.get('team', 1) or 1)
			if (p_id is not None and aid == int(p_id)) or (p_id is None and aid < 1000):
				p_team = tm
				p_max_hp = mhp
				if cd_hex and not p_cd_hex:
					p_cd_hex = cd_hex
				if not p_veh or p_veh == 'player':
					p_veh = vtype
			roster[str(aid)] = {
				'actor_id': aid,
				'vehicle_type': vtype,
				'type_name': vtype,
				'compact_descr_hex': cd_hex,
				'team': tm,
				'name': str(trk.get('name') or ('Bot_%d' % aid)),
				'max_health': mhp,
				'position': pos0,
				'yaw': yaw0,
				'first_pose': [pos0[0], pos0[1], pos0[2], yaw0],
			}
		except Exception:
			pass
	return {
		'version': 'offhangar_replay_v3',
		'map_name': map_name,
		'player_id': int(p_id if p_id is not None else 1),
		'player_vehicle': p_veh,
		'player_compact_descr_hex': p_cd_hex,
		'player_max_health': int(p_max_hp),
		'player_team': int(p_team),
		'combat_start_time': float(payload_dict.get('combat_start_time', 0.0) or 0.0),
		'duration': float(payload_dict.get('duration', 0.0) or 0.0),
		'roster': roster,
	}


def _write_wotreplay_container(filepath, payload_dict):
	"""Записывает компактный нативный файл `.wotreplay` (~0.4–0.9 МБ вместо 50 МБ!) со стандартным заголовком
	WoT 0.8.2 (`0x11343212`), мгновенным ростером в Блоке 1 и zlib-сжатым (`OFZ3`) Блоком 2."""
	date_str = time.strftime('%d.%m.%Y %H:%M:%S')
	map_short = str(payload_dict.get('map_name') or 'battle').split('/')[-1]
	veh_full = str(payload_dict.get('player_vehicle') or 'ussr:T-34')
	offh_meta = _build_offhangar_meta(payload_dict)
	block1 = {
		'playerID': int(offh_meta['player_id']),
		'playerName': 'Player',
		'playerVehicle': veh_full.replace(':', '-'),
		'mapName': map_short,
		'mapDisplayName': map_short,
		'dateTime': date_str,
		'gameplayID': 'ctf',
		'battleType': 1,
		'clientVersionFromExe': '0, 8, 2, 0',
		'clientVersionFromXml': 'v.0.8.2 #543',
		'vehicles': {},
		'offhangar_meta': offh_meta,
	}
	for aid_str, r_info in offh_meta['roster'].items():
		block1['vehicles'][str(aid_str)] = {
			'name': r_info.get('name', 'Bot'),
			'team': int(r_info.get('team', 1)),
			'vehicleType': str(r_info.get('vehicle_type') or veh_full),
			'isAlive': 1,
		}
	block1_bytes = json.dumps(block1, separators=(',', ':')).encode('utf-8')
	raw_b2 = json.dumps(payload_dict, separators=(',', ':')).encode('utf-8')
	block2_bytes = b'OFZ3' + zlib.compress(raw_b2, 6)
	with _SAVE_LOCK:
		tmp_path = '%s.%d.tmp' % (filepath, threading.current_thread().ident or 0)
		with open(tmp_path, 'wb') as wf:
			wf.write(struct.pack('<II', 0x11343212, 2))
			wf.write(struct.pack('<I', len(block1_bytes)))
			wf.write(block1_bytes)
			wf.write(struct.pack('<I', len(block2_bytes)))
			wf.write(block2_bytes)
			wf.write(struct.pack('<II', 0, 0))
		for _retry in range(5):
			try:
				if os.path.exists(filepath):
					os.remove(filepath)
				os.rename(tmp_path, filepath)
				break
			except Exception:
				if _retry == 4:
					try:
						if os.path.exists(tmp_path):
							os.remove(tmp_path)
					except Exception:
						pass
					raise
				time.sleep(0.06)


def _peek_fast_block1_meta(filepath):
	"""Читает ТОЛЬКО Блок 1 (< 8 КБ) из `.wotreplay` за < 0.1 мс, не трогая многомегабайтный Блок 2."""
	if not filepath or not os.path.isfile(filepath):
		return None
	ap = os.path.abspath(filepath)
	try:
		mtime = os.path.getmtime(ap)
	except Exception:
		mtime = 0.0
	cached = _REPLAY_META_CACHE.get(ap)
	if cached is not None and cached[0] == mtime:
		return cached[1]
	try:
		with open(ap, 'rb') as rf:
			hdr = rf.read(12)
			if len(hdr) < 12:
				return None
			magic, block_count, b1_len = struct.unpack('<III', hdr)
			if magic != 0x11343212 or b1_len <= 2 or b1_len > 262144:
				return None
			b1_bytes = rf.read(b1_len)
		b1_obj = json.loads(b1_bytes.decode('utf-8'))
		if isinstance(b1_obj, dict) and isinstance(b1_obj.get('offhangar_meta'), dict):
			meta = b1_obj['offhangar_meta']
			_REPLAY_META_CACHE[ap] = (mtime, meta)
			return meta
	except Exception:
		pass
	return None


def _read_replay_payload_from_file(filepath):
	"""Читает и кэширует словарь реплея из сжатого (`OFZ3`) или несжатого `.wotreplay` / `.offh_replay.json`."""
	if not filepath or not os.path.isfile(filepath):
		return None
	ap = os.path.abspath(filepath)
	try:
		mtime = os.path.getmtime(ap)
	except Exception:
		mtime = 0.0
	cached = _REPLAY_PAYLOAD_CACHE.get(ap)
	if cached is not None and cached[0] == mtime:
		return cached[1]
	with open(ap, 'rb') as rf:
		raw = rf.read()
	if len(raw) < 8:
		return None
	magic = struct.unpack('<I', raw[:4])[0]
	if magic == 0x11343212 and len(raw) >= 16:
		block_count = struct.unpack('<I', raw[4:8])[0]
		pos = 8
		for b_idx in range(min(block_count, 4)):
			if pos + 4 > len(raw):
				break
			b_len = struct.unpack('<I', raw[pos:pos + 4])[0]
			pos += 4
			if pos + b_len > len(raw):
				break
			b_bytes = raw[pos:pos + b_len]
			pos += b_len
			try:
				if b_bytes.startswith(b'OFZ3'):
					b_bytes = zlib.decompress(b_bytes[4:])
				obj = json.loads(b_bytes.decode('utf-8'))
				if isinstance(obj, dict) and ('tracks' in obj or obj.get('version') in ('offhangar_replay_v2', 'offhangar_replay_v3')):
					_REPLAY_PAYLOAD_CACHE[ap] = (mtime, obj)
					_REPLAY_META_CACHE[ap] = (mtime, _build_offhangar_meta(obj))
					return obj
			except Exception:
				pass
		return None
	try:
		if raw.startswith(b'OFZ3'):
			raw = zlib.decompress(raw[4:])
		obj = json.loads(raw.decode('utf-8'))
		if isinstance(obj, dict) and ('tracks' in obj or obj.get('version') in ('offhangar_replay_v2', 'offhangar_replay_v3')):
			_REPLAY_PAYLOAD_CACHE[ap] = (mtime, obj)
			_REPLAY_META_CACHE[ap] = (mtime, _build_offhangar_meta(obj))
			return obj
	except Exception:
		pass
	return None


def peek_replay_metadata(filepath=None):
	"""Мгновенно (< 0.1 мс из заголовка Блока 1 или кэша) возвращает `(map_name, player_vehicle, filepath, player_team, player_compact_descr_hex, player_max_health)`."""
	try:
		target = filepath or _extract_cli_wotreplay_path()
		if not target:
			return None
		meta = _peek_fast_block1_meta(target)
		if isinstance(meta, dict):
			r_p = (meta.get('roster') or {}).get(str(meta.get('player_id', 1))) or {}
			return (
				str(meta.get('map_name') or ''),
				str(meta.get('player_vehicle') or ''),
				target,
				int(meta.get('player_team', 1) or 1),
				str(meta.get('player_compact_descr_hex') or r_p.get('compact_descr_hex') or ''),
				int(meta.get('player_max_health') or r_p.get('max_health') or 0))
		data = _read_replay_payload_from_file(target)
		if isinstance(data, dict):
			meta = _build_offhangar_meta(data)
			r_p = (meta.get('roster') or {}).get(str(meta.get('player_id', 1))) or {}
			return (
				str(meta.get('map_name') or ''),
				str(meta.get('player_vehicle') or ''),
				target,
				int(meta.get('player_team', 1) or 1),
				str(meta.get('player_compact_descr_hex') or r_p.get('compact_descr_hex') or ''),
				int(meta.get('player_max_health') or r_p.get('max_health') or 0))
	except Exception:
		pass
	return None


def peek_replay_roster(filepath=None):
	"""Мгновенно возвращает полный состав машин `{int(actor_id): dict}` со всеми ключами (`position`, `yaw`, `type_name`, `first_pose`)."""
	try:
		target = filepath or _extract_cli_wotreplay_path()
		if not target:
			return None
		meta = _peek_fast_block1_meta(target)
		if not isinstance(meta, dict) or not isinstance(meta.get('roster'), dict):
			data = _read_replay_payload_from_file(target)
			if isinstance(data, dict):
				meta = _build_offhangar_meta(data)
		if isinstance(meta, dict) and isinstance(meta.get('roster'), dict):
			out = {}
			for k, v in meta['roster'].items():
				try:
					aid = int(k)
					fp = v.get('first_pose')
					pos = v.get('position') or (fp[:3] if (fp and len(fp) >= 3) else (0.0, 0.0, 0.0))
					yaw = float(v.get('yaw', fp[3] if (fp and len(fp) >= 4) else 0.0) or 0.0)
					vtype = str(v.get('type_name') or v.get('vehicle_type') or 'ussr:T-34')
					out[aid] = {
						'actor_id': aid,
						'vehicle_type': vtype,
						'type_name': vtype,
						'compact_descr_hex': str(v.get('compact_descr_hex') or ''),
						'team': int(v.get('team', 1) or 1),
						'name': str(v.get('name') or ('Bot_%d' % aid)),
						'max_health': int(v.get('max_health', 1000) or 1000),
						'position': (float(pos[0]), float(pos[1]), float(pos[2])),
						'yaw': yaw,
						'first_pose': (float(pos[0]), float(pos[1]), float(pos[2]), yaw),
					}
				except Exception:
					pass
			return out
	except Exception:
		pass
	return None


class ActorTrack(object):
	"""Хранит записанную траекторию одного танка (20 Гц с плавной интерполяцией до 120+ FPS)."""

	def __init__(self, actor_id, vehicle_type='', team=1, name='Actor', max_health=1000, compact_descr_hex=''):
		self.actor_id = int(actor_id)
		self.vehicle_type = str(vehicle_type or '')
		self.team = int(team or 1)
		self.name = str(name or ('Actor_%d' % self.actor_id))
		self.max_health = int(max_health or 1000)
		self.compact_descr_hex = str(compact_descr_hex or '')
		# frames: (t, x, y, z, yaw, pitch, roll, tyaw, gpitch, vel, hp, alive, spot)
		self.frames = []
		self.events = []

	def add_frame(self, t, x, y, z, yaw, pitch, roll, tyaw, gpitch, vel=0.0, hp=1000, alive=1, spot=1):
		nf = (
			round(float(t), 3),
			round(float(x), 2),
			round(float(y), 2),
			round(float(z), 2),
			round(float(yaw), 3),
			round(float(pitch), 3),
			round(float(roll), 3),
			round(float(tyaw), 3),
			round(float(gpitch), 3),
			round(float(vel), 2),
			int(hp),
			1 if alive else 0,
			1 if spot else 0,
		)
		# Stationary frame deduplication: if the previous two frames have identical pose/HP/state,
		# update the tail timestamp in-place so linear interpolation stays 100% exact while cutting frame count by ~65%!
		if len(self.frames) >= 2:
			f_prev = self.frames[-1]
			f_prev2 = self.frames[-2]
			if (f_prev[10:] == nf[10:] and f_prev2[10:] == nf[10:] and
					abs(f_prev[1] - nf[1]) <= 0.015 and abs(f_prev[2] - nf[2]) <= 0.015 and abs(f_prev[3] - nf[3]) <= 0.015 and
					abs(f_prev2[1] - nf[1]) <= 0.015 and abs(f_prev2[2] - nf[2]) <= 0.015 and abs(f_prev2[3] - nf[3]) <= 0.015 and
					abs(f_prev[4] - nf[4]) <= 0.003 and abs(f_prev[7] - nf[7]) <= 0.003 and abs(f_prev[8] - nf[8]) <= 0.003 and
					abs(f_prev2[4] - nf[4]) <= 0.003 and abs(f_prev2[7] - nf[7]) <= 0.003 and abs(f_prev2[8] - nf[8]) <= 0.003 and
					(nf[0] - f_prev2[0]) < 2.5):
				self.frames[-1] = nf
				return
		self.frames.append(nf)

	def add_event(self, t, kind, **payload):
		ev = {'t': round(float(t), 4), 'kind': str(kind)}
		ev.update(payload)
		self.events.append(ev)

	def sample_at(self, t):
		"""Интерполирует позу и состояние танка в момент времени `t` (секунды от начала записи)."""
		n = len(self.frames)
		if n == 0:
			return None
		if t <= self.frames[0][0] or n == 1:
			f = self.frames[0]
			return (f[1], f[2], f[3], f[4], f[5], f[6], f[7], f[8], f[9],
			        f[10] if len(f) > 10 else self.max_health,
			        f[11] if len(f) > 11 else 1,
			        f[12] if len(f) > 12 else 1)
		if t >= self.frames[-1][0]:
			f = self.frames[-1]
			return (f[1], f[2], f[3], f[4], f[5], f[6], f[7], f[8], f[9],
			        f[10] if len(f) > 10 else self.max_health,
			        f[11] if len(f) > 11 else 1,
			        f[12] if len(f) > 12 else 1)

		lo = 0
		hi = n - 1
		while lo + 1 < hi:
			mid = (lo + hi) // 2
			if self.frames[mid][0] <= t:
				lo = mid
			else:
				hi = mid

		f0 = self.frames[lo]
		f1 = self.frames[hi]
		dt = f1[0] - f0[0]
		alpha = (t - f0[0]) / dt if dt > 1e-5 else 0.0
		alpha = max(0.0, min(1.0, alpha))
		hp = f0[10] if len(f0) > 10 else self.max_health
		if len(f1) > 10 and f1[10] < hp and (f1[0] - t) <= 0.22:
			hp = f1[10]
		alive = (0 if hp <= 0 else (f0[11] if len(f0) > 11 else 1))
		spot = f0[12] if len(f0) > 12 else 1
		return (
			_lerp(f0[1], f1[1], alpha),
			_lerp(f0[2], f1[2], alpha),
			_lerp(f0[3], f1[3], alpha),
			_lerp_angle(f0[4], f1[4], alpha),
			_lerp_angle(f0[5], f1[5], alpha),
			_lerp_angle(f0[6], f1[6], alpha),
			_lerp_angle(f0[7], f1[7], alpha),
			_lerp_angle(f0[8], f1[8], alpha),
			_lerp(f0[9], f1[9], alpha),
			hp,
			alive,
			spot,
		)

	def to_dict(self):
		return {
			'actor_id': self.actor_id,
			'vehicle_type': self.vehicle_type,
			'compact_descr_hex': self.compact_descr_hex,
			'team': self.team,
			'name': self.name,
			'max_health': self.max_health,
			'frames': self.frames,
			'events': self.events,
		}

	@classmethod
	def from_dict(cls, data):
		trk = cls(
			data.get('actor_id', 0),
			data.get('vehicle_type', ''),
			data.get('team', 1),
			data.get('name', 'Actor'),
			data.get('max_health', 1000),
			data.get('compact_descr_hex', ''))
		trk.frames = [tuple(f) for f in (data.get('frames') or ())]
		trk.events = list(data.get('events') or ())
		return trk


class ReplayMachinimaManager(object):
	"""Единый менеджер записи и воспроизведения реплеев боя и многодублевой машинимы."""

	SPEED_STEPS = (0.0625, 0.125, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
	SPEED_STRS = ('1/16', '1/8', '1/4', '1/2', '1', '2', '4', '8', '16')

	def __init__(self):
		self.recording_replay = False
		self.playing_replay = False
		self.machinima_active = False
		self.recording_actor_id = None
		self.start_time = 0.0
		self.sample_interval = 0.10  # 10 Гц (каноничная частота пакетов 0x0A в реплеях 0.8.2 с плавной 120+ FPS интерполяцией)
		self._last_sample_t = -1.0
		self._last_checkpoint_t = 0.0
		self._session_stamp = ''
		self.map_name = ''
		self.player_vehicle_name = ''
		self.player_id = None
		self.recorded_player_id = None
		self.tracks = {}
		self.camera_frames = []  # (t, cx, cy, cz, cyaw, cpitch, is_sniper, disp, fps)
		self.global_events = []
		# Playback state
		self.playback_time = 0.0
		self.playback_duration = 0.0
		self.combat_start_time = 20.0
		self.playback_paused = False
		self.playback_speed = 1.0
		self.playback_free_cam = False
		self.playback_free_look = False
		self._last_wall_t = None
		self._event_cursor = 0
		self._free_cam_pos = None
		self._free_cam_ypr = [0.0, -0.18, 0.0]
		self.last_saved_path = None
		self._callbacks = {}
		self._autostart_consumed = False
		self._was_cli_autostart = False
		self._finish_dialog_shown = False
		self._camera_synced_on_start = False

	@property
	def is_active(self):
		return self.recording_replay or self.playing_replay or self.machinima_active

	def has_pending_cli_autostart(self):
		cli_rp = _extract_cli_wotreplay_path()
		return bool(cli_rp and not self._autostart_consumed)

	def register_battle_hooks(self, hooks_dict):
		"""Регистрирует коллбэки из `offline_battle.py` для спавна трассеров, звуков, урона и чата."""
		if isinstance(hooks_dict, dict):
			self._callbacks.update(hooks_dict)

	def _notify_hud(self, text_utf8, colour='green'):
		cb = self._callbacks.get('push_message')
		if callable(cb):
			try:
				cb(text_utf8, colour)
				return
			except Exception:
				pass
		LOG_NOTE('REPLAY_HUD: %s' % text_utf8)

	def maybe_autostart_cli_replay(self, mock_vehicles=None, battle_window_ready=False):
		"""Запускает воспроизведение CLI `.wotreplay` сразу после того, как окно боя и боты прогрузились."""
		if self._autostart_consumed or self.playing_replay or self.machinima_active:
			return False
		cli_rp = _extract_cli_wotreplay_path()
		if not cli_rp:
			return False
		if not battle_window_ready:
			return False
		# v7.4: Запускаем воспроизведение СРАЗУ с первого кадра боя (без 3.5-секундной задержки, во время которой
		# игрок мог ездить как в обычном бою!). Машины ботов подхватывают свои дорожки мгновенно по мере спавна.
		self._autostart_consumed = True
		if self.recording_replay:
			self.recording_replay = False
		if self.load_replay_file(cli_rp):
			self.playing_replay = True
			self._was_cli_autostart = True
			self._finish_dialog_shown = False
			self.playback_paused = False
			self.playback_speed = 1.0
			self.playback_time = max(0.0, float(getattr(self, 'combat_start_time', 20.0) or 20.0) - 20.0)
			self._event_cursor = 0
			self.playback_free_cam = False
			self.playback_free_look = False
			self._camera_synced_on_start = False
			self._last_wall_t = float(BigWorld.time()) if BigWorld is not None else time.time()
			self._autostart_wall_t = self._last_wall_t
			LOG_NOTE('REPLAY_AUTOSTART: playback active for %s (duration=%.1fs, tracks=%d)' % (
				cli_rp, self.playback_duration, len(self.tracks)))
			self._show_native_replay_msg('replayControlsHelp1')
			self._show_native_replay_msg('replayControlsHelp2')
			self._show_native_replay_msg('replayControlsHelp3')
			return True
		return False

	def start_replay_recording(self, map_name='', player_id=None, player_vehicle_name='', sample_hz=10.0):
		if self.playing_replay or self.machinima_active:
			return False
		# Если ожидается автозапуск CLI .wotreplay — не начинаем поверх него запись нового боя
		if self.has_pending_cli_autostart():
			self.map_name = str(map_name or '')
			self.player_id = player_id
			self.player_vehicle_name = str(player_vehicle_name or '')
			return False
		if not is_replay_recording_enabled():
			LOG_NOTE('REPLAY_MACHINIMA: recording skipped (disabled in game settings)')
			return False
		self.tracks.clear()
		self.camera_frames = []
		self.global_events = []
		self.map_name = str(map_name or '')
		self.player_id = player_id
		self.recorded_player_id = player_id
		self.player_vehicle_name = str(player_vehicle_name or '')
		self.sample_interval = 1.0 / max(5.0, min(60.0, float(sample_hz or 20.0)))
		self.start_time = float(BigWorld.time()) if BigWorld is not None else time.time()
		self._last_sample_t = -1.0
		self._last_checkpoint_t = 0.0
		self._recorded_combat_start = None
		self._session_stamp = time.strftime('%Y%m%d_%H%M%S')
		self.recording_replay = True
		LOG_NOTE('REPLAY_MACHINIMA: auto-recording started map=%s veh=%s hz=%.1f stamp=%s' % (
			self.map_name, self.player_vehicle_name, 1.0 / self.sample_interval, self._session_stamp))
		return True

	def _write_replay_files(self, save_dir='replays', is_checkpoint=False, async_save=True):
		"""v7.4: Асинхронно в фоновом потоке сохраняет единый компактный `.wotreplay` (сжатый `OFZ3`, ~0.4–0.9 МБ)
		без дубликатов `.offh_replay.json` и без единой миллисекунды фриза рендера!"""
		if not self.tracks:
			return None
		# Skip mid-battle disk checkpoints so the disk is never hammered during combat; save once at battle end or F10.
		if is_checkpoint:
			return self.last_saved_path
		duration = 0.0
		for trk in self.tracks.values():
			if trk.frames:
				duration = max(duration, float(trk.frames[-1][0]))
		self.playback_duration = duration
		if duration < 0.2:
			return None
		try:
			stamp = self._session_stamp or time.strftime('%Y%m%d_%H%M%S')
			safe_map = (self.map_name or 'battle').split('/')[-1].replace(':', '_')
			safe_veh = (self.player_vehicle_name or 'tank').split(':')[-1].replace('/', '_')
			tracks_snap = dict((str(k), v.to_dict()) for k, v in self.tracks.items())
			cam_snap = list(self.camera_frames)
			ev_snap = list(self.global_events)
			# Immediately release live recording buffers so 50,000+ frame tuples do not leak into Hangar RAM
			self.tracks.clear()
			del self.camera_frames[:]
			del self.global_events[:]
			_REPLAY_PAYLOAD_CACHE.clear()
			if len(_REPLAY_META_CACHE) > 8:
				_REPLAY_META_CACHE.clear()
			_rec_pid_val = self.recorded_player_id if self.recorded_player_id is not None else self.player_id
			_p_trk_dict = tracks_snap.get(str(_rec_pid_val)) or tracks_snap.get('1') or {}
			payload = {
				'version': 'offhangar_replay_v3',
				'map_name': self.map_name,
				'player_id': _rec_pid_val,
				'player_vehicle': self.player_vehicle_name,
				'player_compact_descr_hex': str(_p_trk_dict.get('compact_descr_hex') or ''),
				'combat_start_time': round(float(getattr(self, '_recorded_combat_start', None) or 20.0), 2),
				'created_at': stamp,
				'duration': round(duration, 2),
				'sample_interval': self.sample_interval,
				'tracks': tracks_snap,
				'camera_frames': cam_snap,
				'events': ev_snap,
			}
			dirs = _resolve_replay_dirs(save_dir)
			folder = dirs[0] if dirs else 'replays'
			if not os.path.isdir(folder):
				os.makedirs(folder)
			wot_fname = os.path.abspath(os.path.join(folder, '%s_%s_%s.wotreplay' % (stamp, safe_veh, safe_map)))
			self.last_saved_path = wot_fname
			_REPLAY_META_CACHE[wot_fname] = (time.time(), _build_offhangar_meta(payload))

			def _bg_save_worker(_path=wot_fname, _pl=payload, _dur=duration, _ntrk=len(tracks_snap), _nev=len(ev_snap)):
				try:
					_write_wotreplay_container(_path, _pl)
					_mtime = os.path.getmtime(_path)
					_REPLAY_META_CACHE[_path] = (_mtime, _build_offhangar_meta(_pl))
					_REPLAY_PAYLOAD_CACHE.clear()
					LOG_NOTE('REPLAY_MACHINIMA: async saved compact replay (%.1fs, %d tracks, %d events, %.1f KB) -> %s' % (
						_dur, _ntrk, _nev, float(os.path.getsize(_path)) / 1024.0, _path))
				except Exception as _bg_err:
					LOG_ERROR('REPLAY_MACHINIMA: async save err: %s' % str(_bg_err))

			if async_save:
				t_save = threading.Thread(target=_bg_save_worker, name='OffhReplaySave')
				t_save.daemon = True
				t_save.start()
			else:
				_bg_save_worker()
			return wot_fname
		except Exception as err:
			LOG_ERROR('REPLAY_MACHINIMA: save failed: %s' % str(err))
		return None

	def flush_checkpoint(self, save_dir='replays'):
		if not self.recording_replay:
			return None
		return self._write_replay_files(save_dir=save_dir, is_checkpoint=True)

	def stop_replay_recording(self, save_dir='replays'):
		if not self.recording_replay:
			return self.last_saved_path
		self.recording_replay = False
		return self._write_replay_files(save_dir=save_dir, is_checkpoint=False)

	def load_replay_file(self, path=None):
		"""Загружает реплей из `.offh_replay.json` ИЛИ напрямую из `.wotreplay`."""
		candidates = []
		if path:
			candidates.append(path)
		cli_p = _extract_cli_wotreplay_path()
		if cli_p and cli_p not in candidates:
			candidates.append(cli_p)
		if self.last_saved_path and self.last_saved_path not in candidates:
			candidates.append(self.last_saved_path)
		for d in _resolve_replay_dirs('replays'):
			last_j = os.path.join(d, 'last_replay.offh_replay.json')
			if last_j not in candidates:
				candidates.append(last_j)
			try:
				found = []
				for pat in ('*.offh_replay.json', '*.wotreplay'):
					for fp in glob.glob(os.path.join(d, pat)):
						if os.path.basename(fp) != 'last_replay.offh_replay.json':
							try:
								found.append((os.path.getmtime(fp), fp))
							except Exception:
								found.append((0.0, fp))
				found.sort(reverse=True)
				for _, fp in found:
					if fp not in candidates:
						candidates.append(fp)
			except Exception:
				pass

		for chosen in candidates:
			if not chosen or not os.path.isfile(chosen):
				continue
			try:
				data = _read_replay_payload_from_file(chosen)
				if not isinstance(data, dict) or not data.get('tracks'):
					continue
				if self.map_name and data.get('map_name') and str(data.get('map_name')) != str(self.map_name):
					LOG_NOTE('REPLAY_MACHINIMA: replay map=%s (current map=%s)' % (
						data.get('map_name'), self.map_name))
				self.recorded_player_id = data.get('player_id')
				self.tracks.clear()
				for k, v in (data.get('tracks') or {}).items():
					self.tracks[int(k)] = ActorTrack.from_dict(v)
				# Если recorded_player_id не задан или отсутствует в tracks, ищем дорожку с ID < 1000 (игрок)
				if self.recorded_player_id is None or int(self.recorded_player_id) not in self.tracks:
					for tk in sorted(self.tracks.keys()):
						if int(tk) < 1000:
							self.recorded_player_id = int(tk)
							break
				self.camera_frames = [tuple(cf) for cf in (data.get('camera_frames') or ())]
				self.global_events = list(data.get('events') or ())
				self.global_events.sort(key=lambda ev: float(ev.get('t', 0.0)))
				self.playback_duration = float(data.get('duration', 0.0) or 0.0)
				if self.playback_duration <= 0.0:
					for trk in self.tracks.values():
						if trk.frames:
							self.playback_duration = max(self.playback_duration, float(trk.frames[-1][0]))
				self.combat_start_time = float(data.get('combat_start_time', 0.0) or 0.0)
				if self.combat_start_time < 10.0:
					self.combat_start_time = 20.0
					try:
						_first_mv_t = 999.0
						for _trk_m in self.tracks.values():
							_frs_m = _trk_m.frames or ()
							if not _frs_m:
								continue
							_ref_f = _frs_m[0]
							for _fr_r in _frs_m:
								if float(_fr_r[0]) >= 14.0:
									_ref_f = _fr_r
									break
							for _fr_m in _frs_m:
								_ft_m = float(_fr_m[0])
								if _ft_m < 15.0:
									continue
								_dx_m = float(_fr_m[1]) - float(_ref_f[1])
								_dz_m = float(_fr_m[3]) - float(_ref_f[3])
								if math.hypot(_dx_m, _dz_m) > 2.5 and len(_fr_m) >= 10 and abs(float(_fr_m[9])) > 1.0:
									_first_mv_t = min(_first_mv_t, max(15.0, _ft_m - 0.8))
									break
						if _first_mv_t < 65.0:
							self.combat_start_time = round(_first_mv_t, 2)
					except Exception:
						self.combat_start_time = 20.0
				self.last_saved_path = chosen
				LOG_NOTE('REPLAY_MACHINIMA: loaded %s (%.1fs, combat_start=%.1fs, %d tracks, %d events, rec_pid=%s)' % (
					chosen, self.playback_duration, self.combat_start_time, len(self.tracks), len(self.global_events), self.recorded_player_id))
				return True
			except Exception as err:
				LOG_ERROR('REPLAY_MACHINIMA: load failed (%s): %s' % (chosen, str(err)))
		return False

	def toggle_playback(self, mock_vehicles=None):
		"""Включает или выключает режим воспроизведения реплея (`F9`)."""
		if self.playing_replay or self.machinima_active:
			self.stop_playback()
			return False
		loaded_from_disk = self.load_replay_file()
		if not loaded_from_disk:
			now = float(BigWorld.time()) if BigWorld is not None else time.time()
			recorded_len = (now - self.start_time) if self.recording_replay else 0.0
			if self.recording_replay and recorded_len >= 2.0 and len(self.tracks) > 0:
				self.stop_replay_recording()
			else:
				self._notify_hud('[РЕПЛЕЙ] В папке replays/ пока нет записей. Нажмите F10 для сохранения текущего боя!', 'yellow')
				return False
		else:
			if self.recording_replay:
				self.recording_replay = False
		self.playing_replay = True
		self.playback_paused = False
		self.playback_speed = 1.0
		self.playback_time = max(0.0, float(getattr(self, 'combat_start_time', 20.0) or 20.0) - 20.0)
		self._event_cursor = 0
		self._camera_synced_on_start = False
		self._last_wall_t = float(BigWorld.time()) if BigWorld is not None else time.time()
		self.playback_free_cam = False
		fname_short = os.path.basename(self.last_saved_path) if self.last_saved_path else 'реплей'
		self._notify_hud(
			'[РЕПЛЕЙ: %s] Воспроизведение (%.0f сек). Пробел: Пауза | Стрелки: Скорость/Перемотка | V: Камера | F9: Выход' % (
				fname_short, self.playback_duration), 'green')
		return True

	def toggle_machinima_overdub(self, player_id=None):
		"""Многодублевая запись Машинимы (`F11`): проигрывает все ранее записанные дорожки танков
		и одновременно записывает движение/стрельбу вашего текущего танк поверх них!"""
		if self.machinima_active:
			self.machinima_active = False
			self.recording_actor_id = None
			saved = self._write_replay_files(save_dir='replays', is_checkpoint=False)
			self._notify_hud(
				'[МАШИНИМА] Дубль сохранён (%s)! Нажмите F9 для просмотра сцены или F11 для ещё одного дубля.' % (
					os.path.basename(saved) if saved else 'ok',), 'green')
			return False
		if self.recording_replay:
			self.stop_replay_recording()
		if not self.tracks:
			self.load_replay_file()
		act_id = int(player_id if player_id is not None else (self.player_id or 1))
		if act_id in self.tracks and self.tracks[act_id].frames:
			old_trk = self.tracks[act_id]
			for b_cand in range(1000, 1030):
				if b_cand not in self.tracks or not self.tracks[b_cand].frames:
					old_trk.actor_id = b_cand
					self.tracks[b_cand] = old_trk
					break
			del self.tracks[act_id]
		self.recording_actor_id = act_id
		self.machinima_active = True
		self.playing_replay = False
		self.playback_paused = False
		self.playback_speed = 1.0
		self.playback_time = 0.0
		self.start_time = float(BigWorld.time()) if BigWorld is not None else time.time()
		self._last_sample_t = -1.0
		self._last_wall_t = self.start_time
		self._event_cursor = 0
		self._notify_hud(
			'[МАШИНИМА: ДУБЛЬ] Все прошлые дубли запущены! Управляйте танком — ваш дубль пишется поверх сцены (F11 — сохранить дубль).',
			'green')
		return True

	def _show_native_replay_msg(self, msg_key, args=None):
		"""Показывает штатное локализованное уведомление реплея WoT 0.8.2 через vMsgsPanel."""
		try:
			from gui.WindowsManager import g_windowsManager
			bw = getattr(g_windowsManager, 'battleWindow', None) if g_windowsManager is not None else None
			vmp = getattr(bw, 'vMsgsPanel', None) if bw is not None else None
			if vmp is not None and hasattr(vmp, 'showMessage'):
				if args is not None:
					vmp.showMessage(msg_key, args)
				else:
					vmp.showMessage(msg_key)
		except Exception:
			pass

	def _speed_str(self, spd):
		for idx, val in enumerate(self.SPEED_STEPS):
			if abs(float(spd) - val) < 1e-4:
				return self.SPEED_STRS[idx]
		return str(spd)

	def _on_replay_stopped_dialog_closed(self, *args):
		"""Вызывается после нажатия ОК в штатном окне 'Воспроизведение записи боя завершено' (#battle:dialogs/replayStopped)."""
		self.stop_playback()
		return_to_login = False
		try:
			from gui.mods.offhangar._constants import CONFIG_OPTIONS as _RP_CFG
			return_to_login = bool(_RP_CFG.get('replay_return_to_login', False))
		except Exception:
			return_to_login = False
		if self._was_cli_autostart and not return_to_login:
			try:
				if BigWorld is not None:
					BigWorld.quit()
					return
			except Exception:
				pass
		leave_cb = self._callbacks.get('leave_arena')
		if callable(leave_cb):
			try:
				leave_cb()
			except Exception:
				pass
		try:
			from gui.WindowsManager import g_windowsManager
			if g_windowsManager is not None and hasattr(g_windowsManager, 'showLogin') and return_to_login:
				BigWorld.callback(0.2, g_windowsManager.showLogin)
		except Exception:
			pass

	def stop_playback(self):
		if not (self.playing_replay or self.machinima_active):
			return
		self.playing_replay = False
		self.machinima_active = False
		self.recording_actor_id = None
		self.playback_paused = False
		self.playback_free_cam = False
		self.playback_free_look = False
		self._last_wall_t = None

	def handle_key_event(self, key, is_down, mock_vehicles=None):
		"""Обрабатывает нативные клавиши управления реплеем WoT 0.8.2 (ЛКМ, Пробел, Стрелки, Home/End)."""
		if not is_down or Keys is None:
			return False
		try:
			from gui.mods.offhangar._constants import CONFIG_OPTIONS as _M_CFG
			_mach_on = bool(_M_CFG.get('sandbox_machinima_mode', False))
		except Exception:
			_mach_on = False
		if _mach_on:
			if key == getattr(Keys, 'KEY_F9', 67):
				self.toggle_playback(mock_vehicles)
				return True
			if key == getattr(Keys, 'KEY_F11', 87):
				self.toggle_machinima_overdub(self.player_id)
				return True
		if not self.playing_replay:
			return False

		if key == getattr(Keys, 'KEY_LEFTMOUSE', 256):
			try:
				import gui.Scaleform.CursorDelegator as _cd_rp
				if getattr(_cd_rp.g_cursorDelegator, '_CursorDelegator__activated', False):
					return False
			except Exception:
				pass
			self.playback_free_look = not self.playback_free_look
			self._show_native_replay_msg(
				'replayFreeCameraActivated' if self.playback_free_look else 'replaySavedCameraActivated')
			return True
		if key == getattr(Keys, 'KEY_SPACE', 57):
			self.playback_paused = not self.playback_paused
			if self.playback_paused:
				self._show_native_replay_msg('replayPaused')
			else:
				self._show_native_replay_msg('replaySpeedChange', {'speed': self._speed_str(self.playback_speed)})
			return True
		if key == getattr(Keys, 'KEY_UPARROW', 200) or key == getattr(Keys, 'KEY_RBRACKET', 27):
			for s in self.SPEED_STEPS:
				if s > self.playback_speed + 1e-4:
					self.playback_speed = s
					break
			self.playback_paused = False
			self._show_native_replay_msg('replaySpeedChange', {'speed': self._speed_str(self.playback_speed)})
			return True
		if key == getattr(Keys, 'KEY_DOWNARROW', 208) or key == getattr(Keys, 'KEY_LBRACKET', 26):
			for s in reversed(self.SPEED_STEPS):
				if s < self.playback_speed - 1e-4:
					self.playback_speed = s
					break
			self.playback_paused = False
			self._show_native_replay_msg('replaySpeedChange', {'speed': self._speed_str(self.playback_speed)})
			return True
		if key == getattr(Keys, 'KEY_LEFTARROW', 203):
			self.seek(self.playback_time - 20.0)
			return True
		if key == getattr(Keys, 'KEY_RIGHTARROW', 205):
			self.seek(self.playback_time + 20.0)
			return True
		if key == getattr(Keys, 'KEY_HOME', 199):
			self.seek(0.0)
			return True
		if key == getattr(Keys, 'KEY_END', 207):
			self.seek(max(0.0, self.playback_duration - 0.5))
			return True
		if key == getattr(Keys, 'KEY_V', 47):
			self.playback_free_cam = not self.playback_free_cam
			if self.playback_free_cam and BigWorld is not None and BigWorld.camera() is not None:
				try:
					self._free_cam_pos = Math.Vector3(BigWorld.camera().position)
					d = BigWorld.camera().direction
					self._free_cam_ypr = [math.atan2(d.x, d.z), -math.asin(max(-0.99, min(0.99, d.y))), 0.0]
				except Exception:
					pass
			return True
		return False

	def seek(self, new_time):
		self.playback_time = max(0.0, min(self.playback_duration, float(new_time)))
		self._finish_dialog_shown = False
		idx = 0
		n = len(self.global_events)
		while idx < n and float(self.global_events[idx].get('t', 0.0)) < self.playback_time:
			idx += 1
		self._event_cursor = idx

	def record_event(self, kind, **kwargs):
		if not (self.recording_replay or self.machinima_active):
			return
		now = float(BigWorld.time()) if BigWorld is not None else time.time()
		rel_t = max(0.0, now - self.start_time)
		ev = {'t': round(rel_t, 4), 'kind': str(kind)}
		ev.update(kwargs)
		self.global_events.append(ev)

	def record_camera_frame(self, rel_t, cam_pos, cam_dir, is_sniper=False, disp=0.01):
		try:
			yaw = math.atan2(float(cam_dir[0]), float(cam_dir[2]))
			pitch = -math.asin(max(-0.999, min(0.999, float(cam_dir[1]))))
			rec_fps = 60
			try:
				if BigWorld is not None and hasattr(BigWorld, 'getFPS'):
					rec_fps = max(1, int(round(float(BigWorld.getFPS()[1]))))
			except Exception:
				rec_fps = 60
			self.camera_frames.append((
				round(float(rel_t), 4),
				round(float(cam_pos[0]), 3),
				round(float(cam_pos[1]), 3),
				round(float(cam_pos[2]), 3),
				round(float(yaw), 4),
				round(float(pitch), 4),
				1 if is_sniper else 0,
				round(float(disp), 5),
				int(rec_fps),
			))
		except Exception:
			pass

	def get_recorded_fps_at(self, t):
		"""Возвращает записанный FPS реплея в момент `t` для нативного отображения `FPS: реплея(наш)` в DebugPanel."""
		n = len(self.camera_frames)
		if n == 0:
			return 60
		if t <= self.camera_frames[0][0] or n == 1:
			f = self.camera_frames[0]
			return int(f[8]) if len(f) > 8 and int(f[8]) > 0 else 60
		if t >= self.camera_frames[-1][0]:
			f = self.camera_frames[-1]
			return int(f[8]) if len(f) > 8 and int(f[8]) > 0 else 60
		lo = 0
		hi = n - 1
		while lo + 1 < hi:
			mid = (lo + hi) // 2
			if self.camera_frames[mid][0] <= t:
				lo = mid
			else:
				hi = mid
		f0 = self.camera_frames[lo]
		return int(f0[8]) if len(f0) > 8 and int(f0[8]) > 0 else 60

	def sample_camera_at(self, t):
		"""Интерполирует записанную камеру игрока `(cx, cy, cz, cyaw, cpitch, is_sniper, disp)` в момент `t`."""
		n = len(self.camera_frames)
		if n == 0:
			return None
		if t <= self.camera_frames[0][0] or n == 1:
			f = self.camera_frames[0]
			return (f[1], f[2], f[3], f[4], f[5], bool(f[6]) if len(f) > 6 else False, f[7] if len(f) > 7 else 0.01)
		if t >= self.camera_frames[-1][0]:
			f = self.camera_frames[-1]
			return (f[1], f[2], f[3], f[4], f[5], bool(f[6]) if len(f) > 6 else False, f[7] if len(f) > 7 else 0.01)
		lo = 0
		hi = n - 1
		while lo + 1 < hi:
			mid = (lo + hi) // 2
			if self.camera_frames[mid][0] <= t:
				lo = mid
			else:
				hi = mid
		f0 = self.camera_frames[lo]
		f1 = self.camera_frames[hi]
		dt = f1[0] - f0[0]
		alpha = max(0.0, min(1.0, (t - f0[0]) / dt if dt > 1e-5 else 0.0))
		return (
			_lerp(f0[1], f1[1], alpha),
			_lerp(f0[2], f1[2], alpha),
			_lerp(f0[3], f1[3], alpha),
			_lerp_angle(f0[4], f1[4], alpha),
			_lerp_angle(f0[5], f1[5], alpha),
			bool(f0[6]) if len(f0) > 6 else False,
			_lerp(f0[7] if len(f0) > 7 else 0.01, f1[7] if len(f1) > 7 else 0.01, alpha),
		)

	def _dispatch_replay_event(self, ev, mock_vehicles):
		kind = ev.get('kind')
		try:
			if kind == 'shot':
				cb = self._callbacks.get('replay_shot')
				if callable(cb):
					cb(ev, mock_vehicles)
			elif kind == 'hit':
				cb = self._callbacks.get('replay_hit')
				if callable(cb):
					cb(ev, mock_vehicles)
			elif kind == 'kill':
				cb = self._callbacks.get('replay_kill')
				if callable(cb):
					cb(ev, mock_vehicles)
			elif kind == 'chat':
				cb = self._callbacks.get('push_message')
				if callable(cb) and ev.get('text'):
					cb(ev.get('text'), ev.get('colour', 'green'))
		except Exception as err:
			LOG_DEBUG('REPLAY_EVENT dispatch err (%s): %s' % (kind, str(err)))

	def _apply_pose_to_vehicle(self, m_veh, x, y, z, yaw, pitch, roll, tyaw, gpitch, vel, hp, alive, spot, now):
		"""Напрямую обновляет 3D-позу танка/бота (включая WGVehicleFilter2, Servo и башню/орудие)."""
		try:
			m_veh.position = Math.Vector3(x, y, z)
			m_veh.yaw = yaw
			m_veh.pitch = pitch
			m_veh.roll = roll
			m_veh._turret_yaw = tyaw
			m_veh._gun_pitch = gpitch
			m_veh._veh_velocity = vel
			m_veh.health = int(hp)
			m_veh._spot_visible = True
			for mdl in (getattr(m_veh, '_chassis_model', None), getattr(m_veh, '_hull_model', None),
			            getattr(m_veh, '_turret_model', None), getattr(m_veh, '_gun_model', None),
			            getattr(m_veh, 'model', None)):
				if mdl is not None:
					try:
						mdl.visible = True
						mdl.visibleAttachments = True
					except Exception:
						pass
			if getattr(m_veh, 'matrix', None) is not None:
				m_veh.matrix.setRotateYPR((yaw, pitch, roll))
				m_veh.matrix.translation = m_veh.position
			if getattr(m_veh, '_wreck_mat', None) is not None:
				m_veh._wreck_mat.setRotateYPR((yaw, pitch, roll))
				m_veh._wreck_mat.translation = m_veh.position
			np_st = getattr(m_veh, '_offh_native_bot_physics_state', None)
			if isinstance(np_st, dict):
				np_st['frame_pose'] = (x, y, z, yaw, pitch, roll)
				np_st['last_pose'] = (x, y, z, yaw, pitch, roll)
				flt = np_st.get('filter')
				sp_id = int(np_st.get('space_id', 0) or 0)
				if flt is not None and sp_id > 0:
					try:
						from gui.mods.offhangar.offh_multithread import get_coordinator
						get_coordinator().fast_seed_and_output(flt, float(now), sp_id, x, y, z, yaw, pitch, roll)
					except Exception:
						pass
			if getattr(m_veh, '_t_mat', None) is not None:
				m_veh._t_mat.setRotateYPR((tyaw, 0.0, 0.0))
			if getattr(m_veh, '_g_mat', None) is not None:
				m_veh._g_mat.setRotateYPR((0.0, gpitch, 0.0))
		except Exception:
			pass

	def tick_frame(self, mock_vehicles, player_id=None, now=None, player_pose_cb=None):
		"""Вызывается один раз за кадр в `_aih_tick`."""
		if player_id is not None:
			self.player_id = player_id
		if not (self.recording_replay or self.playing_replay or self.machinima_active):
			return False
		if now is None:
			now = float(BigWorld.time()) if BigWorld is not None else time.time()

		# 1. Запись живого боя ИЛИ запись нового актёра в режиме Машинимы (F11)
		if self.recording_replay or (self.machinima_active and self.recording_actor_id is not None):
			rel_t = max(0.0, float(now) - self.start_time)
			if getattr(self, '_recorded_combat_start', None) is None and BigWorld is not None:
				try:
					_pl_rc = BigWorld.player()
					if getattr(getattr(_pl_rc, 'arena', None), 'period', 0) == 3:
						self._recorded_combat_start = round(rel_t, 2)
						self.combat_start_time = self._recorded_combat_start
				except Exception:
					pass
			if (rel_t - self._last_sample_t) >= self.sample_interval:
				self._last_sample_t = rel_t
				for vid, m_veh in (mock_vehicles or {}).items():
					if m_veh is None:
						continue
					if (self.machinima_active and not self.recording_replay and
							int(vid) != int(self.recording_actor_id)):
						continue
					pos = getattr(m_veh, 'position', None)
					if pos is None:
						continue
					trk = self.tracks.get(int(vid))
					if trk is None:
						td = getattr(m_veh, 'typeDescriptor', None)
						if td is None and BigWorld is not None and player_id is not None and int(vid) == int(player_id):
							td = getattr(BigWorld.player(), 'vehicleTypeDescriptor', None) or getattr(BigWorld.player(), '_offhangar_td', None)
						vtype = getattr(getattr(td, 'type', None), 'name', '') if td else ''
						cd_hex = ''
						try:
							if td is not None and hasattr(td, 'makeCompactDescr'):
								raw_cd = td.makeCompactDescr()
								if isinstance(raw_cd, str) and len(raw_cd) >= 10:
									cd_hex = raw_cd.encode('hex')
						except Exception:
							cd_hex = ''
						pinfo = getattr(m_veh, 'publicInfo', None) or {}
						trk = ActorTrack(
							int(vid), vtype,
							int(getattr(m_veh, '_bot_team', pinfo.get('team', 1)) or 1),
							pinfo.get('name', 'Tank_%s' % vid),
							int(getattr(m_veh, 'maxHealth', 1000) or 1000),
							cd_hex)
						self.tracks[int(vid)] = trk
					trk.add_frame(
						rel_t, pos[0], pos[1], pos[2],
						float(getattr(m_veh, 'yaw', 0.0) or 0.0),
						float(getattr(m_veh, 'pitch', 0.0) or 0.0),
						float(getattr(m_veh, 'roll', 0.0) or 0.0),
						float(getattr(m_veh, '_turret_yaw', 0.0) or 0.0),
						float(getattr(m_veh, '_gun_pitch', 0.0) or 0.0),
						float(getattr(m_veh, '_veh_velocity', 0.0) or 0.0),
						int(getattr(m_veh, 'health', 0) or 0),
						1 if getattr(m_veh, 'isAlive', True) and (getattr(m_veh, 'health', 0) or 0) > 0 else 0,
						1 if getattr(m_veh, '_spot_visible', True) else 0)
				try:
					if BigWorld is not None and BigWorld.camera() is not None:
						cam = BigWorld.camera()
						self.record_camera_frame(rel_t, cam.position, cam.direction, False, 0.01)
				except Exception:
					pass
			if not self.machinima_active:
				return False

		# 2. Воспроизведение реплея (F9) ИЛИ проигрывание фоновых дублей в Машиниме (F11)
		if self.playing_replay or self.machinima_active:
			if self._last_wall_t is None:
				self._last_wall_t = float(now)
			wall_dt = max(0.0, min(0.1, float(now) - self._last_wall_t))
			self._last_wall_t = float(now)
			# Wait for bot entities from self.tracks to finish async model spawning (up to 4.2s) before advancing playback_time
			_expected_tracks = min(len(self.tracks), 15)
			_spawn_wait_active = (
				self.playing_replay and
				self.playback_time < 0.15 and
				len(mock_vehicles or {}) < _expected_tracks and
				(float(now) - float(getattr(self, '_autostart_wall_t', now) or now)) < 4.2
			)
			if not self.playback_paused and not _spawn_wait_active:
				self.playback_time += wall_dt * self.playback_speed
				if self.playing_replay and self.playback_duration > 0.0 and self.playback_time >= self.playback_duration:
					self.playback_time = self.playback_duration
					self.playback_paused = True
					if not self._finish_dialog_shown:
						self._finish_dialog_shown = True
						try:
							from gui.Scaleform.utils import functions as _sf_funcs
							_sf_funcs.showInformationDialog('replayStopped', self._on_replay_stopped_dialog_closed, ns='battle')
						except Exception:
							self._on_replay_stopped_dialog_closed()

			rel_t = self.playback_time
			n_ev = len(self.global_events)
			while self._event_cursor < n_ev:
				ev = self.global_events[self._event_cursor]
				if float(ev.get('t', 0.0)) <= rel_t:
					self._event_cursor += 1
					if not self.playback_paused:
						self._dispatch_replay_event(ev, mock_vehicles)
				else:
					break

			mvs = mock_vehicles or {}
			bot_eids = sorted([int(k) for k in mvs.keys() if player_id is None or int(k) != int(player_id)])
			rec_pid = self.recorded_player_id if self.recorded_player_id is not None else player_id
			used_bot_eids = set()

			# Сначала размещаем дорожку игрока и те дорожки ботов, чьи ID напрямую совпадают с mvs
			unmatched_tracks = []
			for vid, trk in sorted(self.tracks.items()):
				if self.machinima_active and self.recording_actor_id is not None and int(vid) == int(self.recording_actor_id):
					continue
				sample = trk.sample_at(rel_t)
				if sample is None:
					continue
				if self.playing_replay and ((rec_pid is not None and int(vid) == int(rec_pid)) or int(vid) < 1000) and player_id is not None:
					target_vid = int(player_id)
				elif int(vid) in mvs and (not self.playing_replay or player_id is None or int(vid) != int(player_id)):
					target_vid = int(vid)
					used_bot_eids.add(target_vid)
				else:
					unmatched_tracks.append((vid, sample))
					continue

				x, y, z, yaw, pitch, roll, tyaw, gpitch, vel, hp, alive, spot = sample
				m_veh = mvs.get(target_vid)
				if m_veh is not None:
					self._apply_pose_to_vehicle(m_veh, x, y, z, yaw, pitch, roll, tyaw, gpitch, vel, hp, alive, spot, now)
				if self.playing_replay and player_id is not None and int(target_vid) == int(player_id) and callable(player_pose_cb):
					try:
						player_pose_cb(x, y, z, yaw, pitch, roll, tyaw, gpitch, vel, hp)
					except Exception:
						pass

			# Оставшиеся дорожки раздаём свободным ботам
			free_bots = [bid for bid in bot_eids if bid not in used_bot_eids]
			for idx_u, (_, sample) in enumerate(unmatched_tracks):
				if idx_u >= len(free_bots):
					break
				target_vid = free_bots[idx_u]
				x, y, z, yaw, pitch, roll, tyaw, gpitch, vel, hp, alive, spot = sample
				m_veh = mvs.get(target_vid)
				if m_veh is not None:
					self._apply_pose_to_vehicle(m_veh, x, y, z, yaw, pitch, roll, tyaw, gpitch, vel, hp, alive, spot, now)

			# Свободный полёт камеры оператора (клавиша V во время реплея)
			if self.playing_replay and self.playback_free_cam and BigWorld is not None and Keys is not None:
				try:
					if self._free_cam_pos is None and BigWorld.camera() is not None:
						self._free_cam_pos = Math.Vector3(BigWorld.camera().position)
					if self._free_cam_pos is not None:
						fly_spd = 36.0 * wall_dt
						if BigWorld.isKeyDown(getattr(Keys, 'KEY_LSHIFT', 42)):
							fly_spd *= 2.5
						cyaw, cpitch, croll = self._free_cam_ypr
						if BigWorld.isKeyDown(getattr(Keys, 'KEY_J', 36)):
							cyaw -= 1.8 * wall_dt
						if BigWorld.isKeyDown(getattr(Keys, 'KEY_L', 38)):
							cyaw += 1.8 * wall_dt
						if BigWorld.isKeyDown(getattr(Keys, 'KEY_I', 23)):
							cpitch = max(-1.4, cpitch - 1.4 * wall_dt)
						if BigWorld.isKeyDown(getattr(Keys, 'KEY_K', 37)):
							cpitch = min(1.4, cpitch + 1.4 * wall_dt)
						self._free_cam_ypr = [cyaw, cpitch, croll]
						fx = math.sin(cyaw) * math.cos(cpitch)
						fy = -math.sin(cpitch)
						fz = math.cos(cyaw) * math.cos(cpitch)
						rx = math.cos(cyaw)
						rz = -math.sin(cyaw)
						if BigWorld.isKeyDown(getattr(Keys, 'KEY_W', 17)):
							self._free_cam_pos += Math.Vector3(fx * fly_spd, fy * fly_spd, fz * fly_spd)
						if BigWorld.isKeyDown(getattr(Keys, 'KEY_S', 31)):
							self._free_cam_pos -= Math.Vector3(fx * fly_spd, fy * fly_spd, fz * fly_spd)
						if BigWorld.isKeyDown(getattr(Keys, 'KEY_D', 32)):
							self._free_cam_pos += Math.Vector3(rx * fly_spd, 0.0, rz * fly_spd)
						if BigWorld.isKeyDown(getattr(Keys, 'KEY_A', 30)):
							self._free_cam_pos -= Math.Vector3(rx * fly_spd, 0.0, rz * fly_spd)
						if BigWorld.isKeyDown(getattr(Keys, 'KEY_E', 18)):
							self._free_cam_pos.y += fly_spd
						if BigWorld.isKeyDown(getattr(Keys, 'KEY_Q', 16)):
							self._free_cam_pos.y -= fly_spd
				except Exception:
					pass

			return bool(self.playing_replay)
		return False


_GLOBAL_REPLAY_MANAGER = None


def get_replay_manager():
	global _GLOBAL_REPLAY_MANAGER
	if _GLOBAL_REPLAY_MANAGER is None:
		_GLOBAL_REPLAY_MANAGER = ReplayMachinimaManager()
	return _GLOBAL_REPLAY_MANAGER
