import asyncio
from functools import wraps
import logging
import re

from .core import HANGOUTS, SLACK, inv, Base
from .parser import from_hangups


logger = logging.getLogger(__name__)


def _resolve_channel(team, query):
    # Match Slack channel hyperlinks.
    match = re.match(r"<#(.*?)\|.*?>", query)
    if match:
        query = match.group(1)
    if query in Base.slacks[team].channels:
        return Base.slacks[team].channels[query]
    if query.startswith("#"):
        query = query[1:]
    for channel in Base.slacks[team].channels.values():
        if channel["name"] == query:
            return channel
    raise KeyError(query)


def identify(source, sender, team, query=None, clear=False):
    """
    Create one side of an identity link, either Hangouts->Slack or Slack->Hangouts.
    """
    dest = inv(source)
    idents = Base.idents[team]
    slack = Base.slacks[team]
    if query:
        if source == HANGOUTS:
            for user_id, user in slack.users[team].items():
                if query == user["id"] or query.lower() == user["name"]:
                    user_name = user["name"]
                    break
            else:
                return "No user in <b>{}</b> called <b>{}</b>.".format(team, query)
        else:
            user = Base.bot.get_hangups_user(query)
            if not user.definitionsource:
                return "No user in Hangouts with ID <b>{}</b>.".format(query)
            user_id = user.id_.chat_id
            user_name = user.full_name
        if idents.get(source, sender) == user_id:
            resp = "You are already identified as <b>{}</b>.".format(user_name)
            if not idents.get(dest, user_id) == sender:
                resp += "\nBut you still need to confirm your identity from {}.".format(dest)
            return resp
        idents.add(source, sender, user_id)
        resp = "You have identified as <b>{}</b>.".format(user_name)
        if not idents.get(dest, user_id) == sender:
            resp += "\nNow you need to confirm your identity from {}.".format(dest)
        return resp
    elif clear:
        if idents.get(source, sender):
            idents.remove(source, sender)
            return "{} identity cleared.".format(dest)
        else:
            return "No identity set."


def members(source, room):
    # XXX: This would be better suited as a chatbridge-wide feature -- only channels connected
    # directly to the source channel will be enumerated.
    if source == SLACK:
        team, channel = room
        try:
            channel = _resolve_channel(team, channel)
        except KeyError:
            return "No such channel <b>{}</b>.".format(room[1])
    else:
        hangout = Base.bot.get_hangups_conversation(room)
    channels = set()
    hangouts = set()
    for team, bridges in Base.bridges.items():
        for bridge in bridges:
            if ((source == SLACK and bridge.team == team and bridge.channel == channel["id"]) or
                    (source == HANGOUTS and bridge.hangout == room)):
                channels.add((bridge.team, bridge.channel))
                hangouts.add(bridge.hangout)
    if not channels and not hangouts:
        return "This channel/hangout isn't currently being synced."
    lines = []
    for team, channel in channels:
        channel = Base.slacks[team].channels[channel]
        lines.append("<b>#{}</b> ({} Slack):".format(channel["name"], team))
        for member in channel["members"]:
            lines.append(Base.slacks[team].users[member]["name"])
    for hangout in hangouts:
        hangout = Base.bot.get_hangups_conversation(hangout)
        lines.append("<b>{}</b> (Hangouts):".format(hangout._conversation.name))
        for member in hangout.users:
            lines.append(member.full_name)
    return "\n".join(lines)

def sync(team, channel, hangout):
    """
    Store a new Hangouts<->Slack sync, taking immediate effect.
    """
    try:
        channel = _resolve_channel(team, channel)
    except KeyError:
        return "No such channel <b>{}</b> on <b>{}</b>.".format(channel, team)
    # Make sure this team/channel/hangout combination isn't already configured.
    for team, bridges in Base.bridges.items():
        for bridge in bridges:
            if bridge.team == team and bridge.channel == channel["id"] and bridge.hangout == hangout:
                return "This channel/hangout pair is already being synced."
    # Create a new bridge, and register it with the Slack connection.
    # XXX: Circular dependency on bridge.BridgeInstance, commands.run_slack_command.
    from .bridge import BridgeInstance
    sync = {"channel": [team, channel["id"]], "hangout": hangout}
    Base.add_bridge(BridgeInstance(Base.bot, "slackrtm", sync))
    # Add the new sync to the config list.
    syncs = Base.bot.config.get_by_path(["slackrtm", "syncs"])
    syncs.append(sync)
    Base.bot.config.set_by_path(["slackrtm", "syncs"], syncs)
    return "Now syncing <b>#{}</b> on <b>{}</b> to hangout <b>{}</b>.".format(channel["name"], team, hangout)

def unsync(team, channel, hangout):
    """
    Remove an existing Hangouts<->Slack sync, taking immediate effect.
    """
    try:
        channel = _resolve_channel(team, channel)
    except KeyError:
        return "No such channel <b>{}</b> on <b>{}</b>.".format(channel, team)
    # Make sure this team/channel/hangout combination isn't already configured.
    for team, bridges in Base.bridges.items():
        for bridge in bridges:
            if bridge.team == team and bridge.channel == channel["id"] and bridge.hangout == hangout:
                # Destroy the bridge and its event callback.
                Base.remove_bridge(bridge)
                # Remove the sync from the config list.
                syncs = Base.bot.config.get_by_path(["slackrtm", "syncs"])
                syncs.remove(bridge.sync)
                Base.bot.config.set_by_path(["slackrtm", "syncs"], syncs)
                return ("No longer syncing <b>#{}</b> on <b>{}</b> to hangout <b>{}</b>."
                        .format(channel["name"], team, hangout))
    return "This channel/hangout pair isn't currently being synced."


def reply_hangouts(fn):
    """
    Decorator: run a bot comand, and send the result privately to the calling Hangouts user.
    """
    @wraps(fn)
    def wrap(bot, event, *args):
        resp = fn(bot, event, *args)
        if not resp:
            return
        conv = yield from bot.get_1to1(event.user.id_.chat_id)
        # Replace uses of /bot with the bot's alias.
        botalias = (bot.memory.get("bot.command_aliases") or ["/bot"])[0]
        yield from bot.coro_send_message(conv, re.sub(r"(^|\s|>)/bot\b", r"\1{}".format(botalias), resp))
    return wrap

def reply_slack(fn):
    """
    Decorator: run a Slack command, and send the result privately to the calling Slack user.
    """
    @asyncio.coroutine
    def wrap(msg, slack, team):
        resp = fn(msg, slack, team)
        if not resp:
            return
        yield from slack.msg(channel=msg.channel, as_user=True, text=from_hangups.convert(resp))
    return wrap


@reply_hangouts
def slack_identify(bot, event, *args):
    ("""Link your Hangouts identity to a Slack team.\nUsage: """
     """<b>slack_identify as <i>team</i> <i>user</i></b> to link, <b>slack_identify clear <i>team</i></b> to unlink.""")
    if not len(args) or (args[0].lower(), len(args)) not in [("as", 3), ("clear", 2)]:
        return "Usage: <b>slack_identify as <i>team user</i></b> to link, <b>slack_identify clear <i>team</i></b> to unlink"
    if args[0].lower() == "as":
        kwargs = {"query": args[2]}
    else:
        kwargs = {"clear": True}
    return identify(HANGOUTS, event.user.id_.chat_id, args[1], **kwargs)

@reply_hangouts
def slack_members(bot, event, *args):
    ("""List all Slack and Hangouts members in a synced chat.\nUsage: <b>slack_members <i>hangout</i></b>, """
     """or just <b>slack_members</b> for the current hangout.""")
    if len(args) > 1:
        return "Usage: <b>slack_members <i>channel/hangout</i></b>, or just <b>slack_members</b> for the current hangout."
    return members(HANGOUTS, args[0] if args else event.conv.id_)

@reply_hangouts
def slack_sync(bot, event, *args):
    ("""Link a Slack channel to a hangout.\nUsage: <b>slack_sync <i>team</i> <i>channel</i> to <i>hangout</i></b>, """
     """or just <b>slack_sync <i>team</i> <i>channel</i></b> for the current hangout.""")
    if not (len(args) == 2 or len(args) == 4 and args[2] == "to"):
        return ("Usage: <b>slack_sync <i>team channel</i> to <i>hangout</i></b>, "
                "or just <b>slack_sync <i>team channel</i></b> for the current hangout.")
    return sync(args[0], args[1], event.conv.id_ if len(args) == 2 else args[3])

@reply_hangouts
def slack_unsync(bot, event, *args):
    ("""Unlink a Slack channel from a hangout.\nUsage: <b>slack_unsync <i>team</i> <i>channel</i> from <i>hangout</i></b>, """
     """or just <b>slack_unsync <i>team</i> <i>channel</i></b> for the current hangout.""")
    if not (len(args) == 2 or len(args) == 4 and args[2] == "from"):
        return ("Usage: <b>slack_unsync <i>team channel</i> from <i>hangout</i></b>, "
                "or just <b>slack_unsync <i>team channel</i></b> for the current hangout.")
    return unsync(args[0], args[1], event.conv.id_ if len(args) == 2 else args[3])


@reply_slack
def run_slack_command(msg, slack, team):
    args = msg.text.split()
    try:
        name = args.pop(0)
    except IndexError:
        return
    try:
        admins = Base.bot.config.get_by_path(["slackrtm", "teams", team, "admins"])
    except (KeyError, TypeError):
        admins = []
    if name == "identify":
        if not len(args) or (args[0].lower(), len(args)) not in [("as", 2), ("clear", 1)]:
            return "Usage: <b>identify as <i>user</i></b> to link, <b>identify clear</b> to unlink"
        if args[0].lower() == "as":
            kwargs = {"query": args[1]}
        else:
            kwargs = {"clear": True}
        return identify(SLACK, msg.user, team, **kwargs)
    elif name == "members":
        if not len(args) == 1:
            return "Usage: <b>members <i>channel</i></b>"
        return members(SLACK, [team, args[0]])
    elif msg.user in admins:
        if name == "sync":
            if not (len(args) == 3 and args[1] == "to"):
                return "Usage: <b>sync <i>channel</i> to <i>hangout</i></b>"
            return sync(team, args[0], args[2])
        elif name == "unsync":
            if not (len(args) == 3 and args[1] == "from"):
                return "Usage: <b>unsync <i>channel</i> from <i>hangout</i></b>"
            return unsync(team, args[0], args[2])
