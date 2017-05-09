import logging

import plugins

from webbridge import WebFramework


logger = logging.getLogger(__name__)


def _initialise(bot):
    plugins.register_user_command(["getmembers"])


def getmembers(bot, event, *args):
    """List all members in this conversation, including bridged chats.  Takes a hangout ID, or defaults to the caller's conversation."""
    conv_id = args[0] if args else event.conv.id_
    info = {}
    try:
        bridges = WebFramework.find_conv_for_hangout(conv_id, *bot.shared.get("webbridge", []))
    except KeyError:
        yield from bot.coro_send_message(event.conv.id_, "<i>This hangout isn't bridged.</i>")
        return
    for bridge in bridges:
        info.update(bridge.get_all_external_info(conv_id))
    from pprint import pformat
    logger.info(pformat(info))
    yield from bot.coro_send_message(event.conv.id_, "<i>Check logs for info.</i>")
