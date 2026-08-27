import AccountCommands

from gui.mods.offhangar.logging import LOG_DEBUG


class RequestResult(object):
	"""Transport-neutral command result."""
	__slots__ = ('resultID', 'errorStr', 'data')

	def __init__(self, resultID, errorStr='', data=None):
		self.resultID = resultID
		self.errorStr = errorStr
		self.data = data


class CommandRouter(object):
	"""Central command router: cmd -> handler."""
	def __init__(self):
		super(CommandRouter, self).__init__()
		self._handlers = {}
		self._fallback_handler = None

	def register(self, cmd, handler):
		# First registration wins: avoids battle stubs overwriting core cmds when numeric ids collide.
		if cmd in self._handlers:
			LOG_DEBUG('CommandRouter.register.skip', cmd, 'handler already set')
			return
		self._handlers[cmd] = handler

	def set_fallback(self, handler):
		self._fallback_handler = handler

	def dispatch(self, fake_server, requestID, cmd, args):
		from gui.mods.offhangar.logging import LOG_DEBUG
		handler = self._handlers.get(cmd, self._fallback_handler)
		LOG_DEBUG('Router.dispatch', requestID, cmd, 'handler:', handler)
		if handler is None:
			return requestID, AccountCommands.RES_SUCCESS, '', None
		result = handler(fake_server, requestID, cmd, args)
		return requestID, result.resultID, result.errorStr, result.data


_DEFAULT_ROUTER = None


def get_default_router():
	global _DEFAULT_ROUTER
	if _DEFAULT_ROUTER is None:
		from gui.mods.offhangar import command_handlers
		_DEFAULT_ROUTER = CommandRouter()
		command_handlers.configure_router(_DEFAULT_ROUTER)
		LOG_DEBUG('CommandRouter.ready', len(_DEFAULT_ROUTER._handlers))
	return _DEFAULT_ROUTER
