# -*- coding: utf-8 -*-
import functools
import cPickle

import AccountCommands
import BigWorld

from gui.mods.offhangar._constants import CHAT_ACTION_DATA
from gui.mods.offhangar.command_router import get_default_router
from gui.mods.offhangar.logging import *

class FakeServer(object):
	def __call__(self, *args, **kwargs):
		if not self.__isMuted:
			LOG_DEBUG('%s ( %s, %s )' % (self.__name, args, kwargs))

	def __init__(self, name='Server', isMuted=False):
		super(FakeServer, self).__init__()
		self.__isMuted = isMuted
		self.__name = name
		self.__router = get_default_router()

	def __getattr__(self, name):
		try:
			return super(FakeServer, self).__getattribute__(name)
		except AttributeError:
			return FakeServer(name='%s.%s' % (self.__name, name), isMuted=self.__isMuted)

	def chatCommand(self, _leadingNone, requestID, action, channelID, int64Arg, int16Arg, stringArg1, stringArg2):
		# v4.78: Handle both text chat (broadcast) and radio shortcuts (userChatCommand)
		try:
			LOG_DEBUG('[RADIO_ENTRY] action=%r requestID=%r channelID=%r' % (action, requestID, channelID))
		except Exception:
			pass
		from chat_shared import CHAT_COMMANDS, CHAT_ACTIONS, CHAT_RESPONSES
		command_name = None
		# Enumeration is not a plain dict: dir() only yields methods, so iterate
		# the actual enum items via all() and read their own name()/index().
		for _cmd_item in CHAT_COMMANDS.all():
			try:
				if int(_cmd_item.index()) == int(action):
					command_name = _cmd_item.name()
					break
			except (TypeError, ValueError, AttributeError):
				continue

		player = BigWorld.player()
		player_veh_id = getattr(player, 'playerVehicleID', 0)
		arena = getattr(player, 'arena', None)
		vinfo = arena.vehicles.get(player_veh_id, {}) if (arena and hasattr(arena, 'vehicles')) else {}
		db_id = vinfo.get('accountDBID', getattr(player, 'id', 0))
		nick = vinfo.get('name', getattr(player, 'name', 'Player'))
		import time as _tm
		now_t = _tm.time()

		if channelID <= 0:
			channelID = 1

		if command_name == 'broadcast':
			# Player typed text in chat and hit Enter
			bcData = {
				'requestID': requestID,
				'action': CHAT_ACTIONS.broadcast.index(),
				'actionResponse': CHAT_RESPONSES.success.index(),
				'channel': channelID,
				'originator': db_id,
				'originatorNickName': nick,
				'data': stringArg1,
				'time': now_t,
				'sentTime': now_t,
				'group': 0,
				'flags': 0
			}
			try:
				player.onChatAction(bcData)
			except Exception:
				LOG_CURRENT_EXCEPTION()
			try:
				from gui.mods.offhangar import offline_battle
				fn = getattr(offline_battle, 'g_offh_onBroadcast', None)
				if fn is not None:
					fn(bcData)
			except Exception:
				pass
			try:
				from messenger.gui import MessengerDispatcher
				from messenger.wrappers import ChatActionWrapper
				m = getattr(MessengerDispatcher, 'g_instance', None)
				bm = getattr(m, 'battleMessenger', None) or getattr(m, 'currentWindow', None)
				_dd_ok = True
				try:
					from gui.mods.offhangar import offline_battle as _dd_ob
					_dd_ok = _dd_ob._offh_chat_once('bc', bcData)
				except Exception:
					_dd_ok = True
				if bm is not None and _dd_ok:
					bm.addChannelMessage(ChatActionWrapper(**bcData))
			except Exception:
				pass
			return

		if command_name is not None and command_name != 'requestUsersRoster':
			# Forward to _offh_dispatch_radio_command which maps raw target IDs (e.g. 1073741850 -> 1003)
			# and emits the formatted userChatCommand ONCE to UI/Chat/Minimap!
			self._offh_dispatch_radio_command(
				player, action, channelID, int64Arg, int16Arg, stringArg1, stringArg2, command_name=command_name, requestID=requestID)
			return

		# Default fallback for other chat commands (e.g. roster request)
		chatActionData = CHAT_ACTION_DATA.copy()
		chatActionData['requestID'] = requestID
		chatActionData['action'] = action
		chatActionData['actionResponse'] = CHAT_RESPONSES.success.index()
		try:
			player.onChatAction(chatActionData)
		except Exception:
			pass

	chatCommandFromClient = chatCommand

	def _offh_dispatch_radio_command(self, player, action, channelID, int64Arg,
			int16Arg, stringArg1, stringArg2, command_name=None, requestID=0):
		"""Forward one player radio command into the bot AI director.

		action is CHAT_COMMANDS(...).index() - the integer enum position the
		real client always sends, decoded back to a name via the shipped
		chat_shared module (never hardcoded, so a client build with a
		reordered enum still resolves correctly). Only tactically meaningful
		commands (bot_ai.RADIO_COMMANDS) are forwarded.
		"""
		try:
			director = None
			import sys
			offline_battle = sys.modules.get('gui.mods.offhangar.offline_battle')
			if offline_battle is not None:
				director = getattr(offline_battle, 'g_offh_bot_director', None)
			try:
				LOG_DEBUG('[RADIO_DISPATCH] action=%r cmd=%r director=%s module=%s' % (
					action, command_name, director is not None, offline_battle is not None))
			except Exception:
				pass
			if director is None:
				return
			from chat_shared import CHAT_COMMANDS, CHAT_ACTIONS, CHAT_RESPONSES
			if command_name is None:
				for _cmd_item in CHAT_COMMANDS.all():
					try:
						if int(_cmd_item.index()) == int(action):
							command_name = _cmd_item.name()
							break
					except (TypeError, ValueError, AttributeError):
						continue
			if command_name is None:
				return
			from gui.mods.offhangar.bot_ai import (
				RADIO_COMMANDS, RADIO_TARGET_COMMANDS, RADIO_ALLY_TARGET_COMMANDS,
				RADIO_COMMAND_ATTENTION_TO_CELL)
			if command_name not in RADIO_COMMANDS:
				return
			issuer_id = int(getattr(player, 'playerVehicleID', -1))
			issuer_team = int(getattr(
				player, '_offhangar_team',
				getattr(player, '_offhangar_network_team', 1)) or 1)
			mocks = getattr(offline_battle, 'G_MOCK_VEHICLES', None) or {}
			local = mocks.get(issuer_id)
			issuer_position = None
			if local is not None and getattr(local, 'position', None) is not None:
				issuer_position = (float(local.position.x), float(local.position.y), float(local.position.z))
			elif hasattr(player, 'getOwnVehiclePosition') and player.getOwnVehiclePosition() is not None:
				p_pos = player.getOwnVehiclePosition()
				issuer_position = (float(p_pos[0]), float(p_pos[1]), float(p_pos[2]))
			elif hasattr(player, 'position') and player.position is not None:
				p_pos = player.position
				issuer_position = (float(p_pos[0]), float(p_pos[1]), float(p_pos[2]))
			else:
				issuer_position = (0.0, 0.0, 0.0)
			now = float(BigWorld.time())
			cell_index = None
			target_id = None
			if command_name == RADIO_COMMAND_ATTENTION_TO_CELL:
				cell_index = int16Arg
			elif command_name in RADIO_TARGET_COMMANDS or command_name in RADIO_ALLY_TARGET_COMMANDS:
				target_id = int(int64Arg) if int64Arg else None
				# the client targets the OfflineEntity (id 1073741xxx), bots and
				# arena.vehicles know the mock id (1000+). Map it, else drop the target.
				if target_id is not None and target_id not in mocks:
					_raw_target = target_id
					target_id = None
					for _mid, _mv in mocks.items():
						try:
							_bw = getattr(_mv, 'bw_entity', None)
							if _bw is not None and int(getattr(_bw, 'id', -1)) == _raw_target:
								target_id = int(_mid)
								break
						except Exception:
							continue
					LOG_NOTE('RADIO_TARGET_MAP raw=%s -> %s' % (_raw_target, target_id))
				if command_name in RADIO_ALLY_TARGET_COMMANDS:
					_tmv = mocks.get(target_id) if target_id is not None else None
					if _tmv is None or int(getattr(_tmv, '_bot_team', 0) or 0) != int(issuer_team):
						target_id = None
						_best_ally_id = None
						_best_ally_score = 1e18
						try:
							import Math as _srv_math
							_cam = BigWorld.camera()
							_cpos = _cam.position if _cam is not None else _srv_math.Vector3(*issuer_position)
							_cdir = _cam.direction if _cam is not None else None
						except Exception:
							_cpos = None
							_cdir = None
						for _mid, _mv in mocks.items():
							try:
								if int(_mid) == int(issuer_id):
									continue
								if int(getattr(_mv, '_bot_team', 0) or 0) != int(issuer_team):
									continue
								if float(getattr(_mv, 'health', 0) or 0) <= 0:
									continue
								_mp = getattr(_mv, 'position', None)
								if _mp is None:
									continue
								_dx = float(_mp[0]) - float(issuer_position[0])
								_dz = float(_mp[2]) - float(issuer_position[2])
								_dist = (_dx * _dx + _dz * _dz) ** 0.5
								_score = _dist
								if _cpos is not None and _cdir is not None:
									_vx = float(_mp[0]) - float(_cpos.x)
									_vy = float(_mp[1]) + 1.5 - float(_cpos.y)
									_vz = float(_mp[2]) - float(_cpos.z)
									_vlen = (_vx * _vx + _vy * _vy + _vz * _vz) ** 0.5
									if _vlen > 0.5:
										_cos = (_vx * float(_cdir.x) + _vy * float(_cdir.y) + _vz * float(_cdir.z)) / _vlen
										if _cos >= 0.96:
											_score = (1.0 - _cos) * 500.0 + _dist * 0.05
								if _score < _best_ally_score and _dist <= 350.0:
									_best_ally_score = _score
									_best_ally_id = int(_mid)
							except Exception:
								continue
						target_id = _best_ally_id
				if target_id is None:
					LOG_NOTE('RADIO_CMD_NOTARGET cmd=%s - not sent' % command_name)
					return
			LOG_NOTE('RADIO_CMD issuer=%s team=%s cmd=%s pos=%s cell=%s target=%s'
				% (issuer_id, issuer_team, command_name, issuer_position,
					cell_index, target_id))
			accepted = director.apply_radio_command(
				command_name, issuer_id, issuer_team, issuer_position, now,
				cell_index=cell_index, target_id=target_id)
			LOG_NOTE('RADIO_CMD issuer=%s cmd=%s accepted_by=%s (%d bots)'
				% (issuer_id, command_name, accepted, len(accepted or ())))

			# Broadcast player's own command to UI / Chat / Minimap / Markers
			p_dbid = getattr(player, 'accountDBID', None) or (getattr(player, 'arena', None) and player.arena.vehicles.get(issuer_id, {}).get('accountDBID')) or 10000000
			p_nick = getattr(player, 'name', None) or (getattr(player, 'arena', None) and player.arena.vehicles.get(issuer_id, {}).get('name')) or 'Player'
			playerCmdData = {
				'requestID': int(requestID or 0),
				'action': CHAT_ACTIONS.userChatCommand.index(),
				'actionResponse': CHAT_RESPONSES.success.index(),
				'channel': channelID if channelID > 0 else 1,
				'originator': p_dbid,
				'originatorNickName': p_nick,
				'data': [command_name, int(target_id) if target_id else 0, int(cell_index) if cell_index is not None else 0],
				'time': now,
				'sentTime': now,
				'group': 0,
				'flags': 0
			}
			try:
				fn = getattr(offline_battle, 'g_offh_onUserChatCommand', None)
				if fn is not None:
					fn(playerCmdData)
				elif hasattr(player, 'onChatAction'):
					player.onChatAction(playerCmdData)
			except Exception:
				pass

			# Bot acknowledgement radio response!
			if accepted and len(accepted) > 0:
				arena = getattr(player, 'arena', None)
				bot_id = accepted[0]
				bot_vinfo = arena.vehicles.get(bot_id, {}) if (arena and hasattr(arena, 'vehicles')) else {}
				bot_dbid = bot_vinfo.get('accountDBID', 10000 + bot_id)
				bot_nick = bot_vinfo.get('name', 'Bot_%d' % bot_id)
				reply_cmd = 'POSITIVE'

				def _bot_ack_callback(_b_dbid=bot_dbid, _b_nick=bot_nick, _r_cmd=reply_cmd, _ch=channelID):
					try:
						import time as _btm
						p = BigWorld.player()
						if p is None:
							return
						# POSITIVE ('Так точно!') takes 0 target arguments in i18n (#ingame_gui:chat_shortcuts/positive)
						ackData = {
							'requestID': 0,
							'action': CHAT_ACTIONS.userChatCommand.index(),
							'actionResponse': CHAT_RESPONSES.success.index(),
							'channel': _ch if _ch > 0 else 1,
							'originator': _b_dbid,
							'originatorNickName': _b_nick,
							'data': [_r_cmd, 0, 0],
							'time': _btm.time(),
							'sentTime': _btm.time(),
							'group': 0,
							'flags': 0
						}
						try:
							import sys as _sys_ack
							_ob_ack = _sys_ack.modules.get('gui.mods.offhangar.offline_battle')
							fn = getattr(_ob_ack, 'g_offh_onUserChatCommand', None) if _ob_ack is not None else None
							if fn is not None:
								fn(ackData)
							elif hasattr(p, 'onChatAction'):
								p.onChatAction(ackData)
						except Exception:
							pass
					except Exception:
						pass

				BigWorld.callback(0.7, _bot_ack_callback)
		except Exception:
			try:
				LOG_DEBUG('OfflineBattle.radio_command dispatch failed', action)
			except Exception:
				pass

	def doCmdStr(self, requestID, cmd, str):
		LOG_DEBUG('Server.doCmdStr', requestID, str)
		self.__doCmd(requestID, cmd, str)

	def doCmdIntStr(self, requestID, cmd, int, str):
		LOG_DEBUG('Server.doCmdIntStr', requestID, cmd, int, str)
		self.__doCmd(requestID, cmd, int, str)

	def doCmdInt3(self, requestID, cmd, int1, int2, int3):
		LOG_DEBUG('Server.doCmdInt3', requestID, cmd, int1, int2, int3)
		enqueueCmd = getattr(AccountCommands, 'CMD_ENQUEUE_RANDOM', None)
		if enqueueCmd is not None and cmd == enqueueCmd:
			player = BigWorld.player()
			if player is not None and getattr(player, 'isOffline', False):
				LOG_DEBUG('Offline.FakeServer.ENQUEUE_RANDOM IGNORED', requestID, int1, int2, int3)
		self.__doCmd(requestID, cmd, int1, int2, int3)

	def doCmdInt4(self, requestID, cmd, int1, int2, int3, int4):
		LOG_DEBUG('Server.doCmdInt4', requestID, cmd, int1, int2, int3, int4)
		self.__doCmd(requestID, cmd, int1, int2, int3, int4)

	def doCmdInt2Str(self, requestID, cmd, int1, int2, str):
		LOG_DEBUG('Server.doCmdInt2Str', requestID, cmd, int1, int2, str)
		self.__doCmd(requestID, cmd, int1, int2, str)

	def doCmdIntArr(self, requestID, cmd, arr):
		LOG_DEBUG('Server.doCmdIntArr', requestID, cmd, arr)
		self.__doCmd(requestID, cmd, arr)

	def doCmdIntArrStrArr(self, requestID, cmd, intArr, strArr):
		LOG_DEBUG('Server.doCmdIntArrStrArr', requestID, cmd, intArr, strArr)
		self.__doCmd(requestID, cmd, intArr, strArr)

	def __doCmd(self, requestID, cmd, *args):
		try:
			from gui.mods.offhangar import command_handlers as _handlers
			_cmd_name = _handlers._ACCOUNT_CMD_INDEX.get(cmd, 'cmd=%s' % cmd)
		except Exception:
			_cmd_name = 'cmd=%s' % cmd
		LOG_DEBUG('PURCHASE.command.begin req=%s cmd=%s name=%s' % (
			requestID, cmd, _cmd_name))
		try:
			requestID, resultID, errorStr, ext = self.__router.dispatch(self, requestID, cmd, args)
		except Exception:
			LOG_CURRENT_EXCEPTION()
			LOG_DEBUG('PURCHASE.command.dispatch_exception req=%s cmd=%s name=%s' % (
				requestID, cmd, _cmd_name))
			raise

		LOG_DEBUG('PURCHASE.command.response_ready req=%s cmd=%s result=%s error=%s' % (
			requestID, cmd, resultID, errorStr))
		LOG_DEBUG('Server.__doCmd scheduling callback', requestID, resultID, errorStr)
		if ext is not None:
			cb = functools.partial(BigWorld.player().onCmdResponseExt, requestID, resultID, errorStr, cPickle.dumps(ext))
		else:
			cb = functools.partial(BigWorld.player().onCmdResponse, requestID, resultID, errorStr)

		def wrapped_callback():
			LOG_DEBUG('Server.__doCmd executing callback', requestID)
			try:
				cb()
				LOG_DEBUG('PURCHASE.command.callback_done req=%s cmd=%s' % (
					requestID, cmd))
			finally:
				LOG_DEBUG('Server.__doCmd callback done', requestID)

		BigWorld.callback(0.02, wrapped_callback)