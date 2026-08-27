import cPickle
import json
import os

import BigWorld
from debug_utils import LOG_DEBUG, LOG_ERROR
import sys

CONFIG_OPTIONS = {}
try:
    from gui.mods.offhangar import paths as _offh_paths
    CONFIG_OPTIONS, _new_keys, _cfg_errors = _offh_paths.load_config()
    for _msg in _cfg_errors:
        LOG_ERROR('offhangar config: ' + _msg)
    if _new_keys:
        LOG_DEBUG('offhangar config: options added since your %s was created '
                  '(defaults in effect): %s' % (_offh_paths.USER_CONFIG_FILE, ', '.join(_new_keys)))
    LOG_DEBUG('Config loaded successfully: ' + str(CONFIG_OPTIONS))
except Exception, e:
    LOG_ERROR('Failed to load config: ' + str(e))

from chat_shared import CHAT_RESPONSES

OFFLINE_SERVER_ADDRESS = 'offline.loc'
OFFLINE_NICKNAME = str(CONFIG_OPTIONS.get('nickname', 'Defaultplayer'))
OFFLINE_LOGIN = OFFLINE_NICKNAME + '@' + OFFLINE_SERVER_ADDRESS
OFFLINE_PWD = '1'
OFFLINE_DBID = 13028161

OFFLINE_GUI_CTX = cPickle.dumps({
    'databaseID': OFFLINE_DBID,
    'logUXEvents': False,
    'aogasStartedAt': 0,
    'sessionStartedAt': 0,
    'isAogasEnabled': False,
    'collectUiStats': False,
    'isLongDisconnectedFromCenter': False,
}, cPickle.HIGHEST_PROTOCOL)

OFFLINE_SERVER_SETTINGS = {
    'regional_settings': {'starting_day_of_a_new_week': 0, 'starting_time_of_a_new_game_day': 0, 'starting_time_of_a_new_day': 0, 'starting_day_of_a_new_weak': 0},
    'xmpp_enabled': False,
    'xmpp_port': 0,
    'xmpp_host': '',
    'xmpp_muc_enabled': False,
    'xmpp_muc_services': [],
    'xmpp_resource': '',
    'xmpp_bosh_connections': [],
    'xmpp_connections': [],
    'xmpp_alt_connections': [],
    'file_server': {'clan_emblems': {'url_template': ''}, 'rare_achievements_images': {'url_template': ''}, 'rare_achievements_texts': {'url_template': ''}},
    'voipDomain': '',
    'voipUserDomain': ''
}

CHAT_ACTION_DATA = {
    'requestID': None,
    'action': None,
    'actionResponse': CHAT_RESPONSES.internalError.index(),
    'time': 0,
    'sentTime': 0,
    'channel': 0,
    'originator': 0,
    'originatorNickName': '',
    'group': 0,
    'data': {},
    'flags': 0
}

REQUEST_CALLBACK_TIME = 0.5