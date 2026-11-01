# -*- coding: utf-8 -*-
"""Stub for lan_waiting_room to eliminate legacy BigWorld GUI overlay that broke fullscreen cursor."""

_active = False
_player = None
_selected_map = None
_status = ''

def is_active():
	return False

def selected_map():
	return _selected_map

def set_status(message):
	global _status
	_status = str(message or '')

def open(player):
	return False

def update(player=None):
	return False

def close():
	global _active, _player
	_active = False
	_player = None
