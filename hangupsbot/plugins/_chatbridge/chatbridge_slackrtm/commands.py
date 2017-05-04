from functools import wraps
import logging
import re

from .core import HANGOUTS, SLACK, inv


logger = logging.getLogger(__name__)


bridge = None

def set_bridge(br):
    """
    Obtain a reference to the bridge instance for use with commands.
    """
    global bridge
    bridge = br


def _html_to_slack(text):
    text = re.sub(r"<b>(.*?)</b>", r"*\1*", text)
    text = re.sub(r"<i>(.*?)</i>", r"_\1_", text)
    return text

def _resolve_channel(team, query):
    # Match Slack channel hyperlinks.
    match = re.match(r"<#(.*?)\|.*?>", query)
    if match:
        query = match.group(1)
    if query in bridge.slacks[team].channels:
        return bridge.slacks[team].channels[query]
    if query.startswith("#"):
        query = query[1:]
    for channel in bridge.slacks[team].channels.values():
        if channel["name"] == query:
            return channel
    raise KeyError(query)


def identify(source, sender, team, query=None, clear=False):
    """
    Create one side of an identity link, either Hangouts->Slack or Slack->Hangouts.
    """
    dest = inv(source)
    idents = bridge.idents[team]
    if query:
        if source == HANGOUTS:
            for user_id, user in bridge.users[team].items():
                if query == user["id"] or query.lower() == user["name"]:
                    user_name = user["name"]
                    break
            else:
                return "No user in <b>{}</b> called <b>{}</b>.".format(team, query)
        else:
            user = bridge.bot.get_hangups_user(query)
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


def sync(team, channel, hangout):
    """
    Store a new Hangouts<->Slack sync, taking immediate effect.
    """
    try:
        channel = _resolve_channel(team, channel)
    except KeyError:
        return "No such channel <b>{}</b> on <b>{}</b>.".format(channel, team)
    for sync in bridge.configuration["syncs"]:
        if sync["channel"] == [team, channel["id"]] and sync["hangout"] == hangout:
            return "This channel/hangout pair is already being synced."
    bridge.configuration["syncs"].append({"channel": [team, channel["id"]], "hangout": hangout})
    bridge.bot.config.set_by_path(["slackrtm", "syncs"], bridge.configuration["syncs"])
    return "Now syncing <b>#{}</b> on <b>{}</b> to hangout <b>{}</b>.".format(channel["name"], team, hangout)

def unsync(team, channel, hangout):
    """
    Remove an existing Hangouts<->Slack sync, taking immediate effect.
    """
    try:
        channel = _resolve_channel(team, channel)
    except KeyError:
        return "No such channel <b>{}</b> on <b>{}</b>.".format(channel, team)
    for sync in bridge.configuration["syncs"]:
        if sync["channel"] == [team, channel["id"]] and sync["hangout"] == hangout:
            bridge.configuration["syncs"].remove(sync)
            bridge.bot.config.set_by_path(["slackrtm", "syncs"], bridge.configuration["syncs"])
            return "No longer syncing <b>#{}</b> on <b>{}</b> to hangout <b>{}</b>.".format(channel["name"], team, hangout)
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
    @wraps(fn)
    def wrap(msg, slack, team):
        resp = _html_to_slack(fn(msg, slack, team))
        if not resp:
            return
        slack.api_call("chat.postMessage", channel=msg.channel, as_user=True, text=resp)
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
def slack_sync(bot, event, *args):
    ("""Link a Slack channel to a hangout.\nUsage: <b>slack_sync <i>team</i> <i>channel</i> to <i>hangout</i></b>, """
     """or just <b>slack_sync <i>team</i> <i>channel</i></b> for the current hangout.""")
    if not len(args) == 2 or len(args) == 4 and args[2] == "to":
        return ("Usage: <b>slack_sync <i>team channel</i> to <i>hangout</i></b>, "
                "or just <b>slack_sync <i>team channel</i></b> for the current hangout.")
    return sync(args[0], args[1], event.conv.id_ if len(args) == 2 else args[3])

@reply_hangouts
def slack_unsync(bot, event, *args):
    ("""Unlink a Slack channel from a hangout.\nUsage: <b>slack_unsync <i>team</i> <i>channel</i> from <i>hangout</i></b>, """
     """or just <b>slack_unsync <i>team</i> <i>channel</i></b> for the current hangout.""")
    if not len(args) == 2 or len(args) == 4 and args[2] == "from":
        return "Usage: *slack_sync _team_ _channel_ to _hangout_*, or just *slack_sync _team_ _channel_* for the current hangout."
    return unsync(args[0], args[1], event.conv.id_ if len(args) == 2 else args[3])


@reply_slack
def run_slack_command(msg, slack, team):
    args = msg.text.split()
    try:
        name = args.pop(0)
    except IndexError:
        return
    if name == "identify":
        if not len(args) or (args[0].lower(), len(args)) not in [("as", 2), ("clear", 1)]:
            return "Usage: <b>identify as <i>user</i></b> to link, <b>identify clear</b> to unlink"
        if args[0].lower() == "as":
            kwargs = {"query": args[1]}
        else:
            kwargs = {"clear": True}
        return identify(SLACK, msg.user, team, **kwargs)
    elif msg.user in bridge.configuration["teams"][team]["admins"]:
        if name == "sync":
            if not (len(args) == 3 and args[1] == "to"):
                return "Usage: <b>sync <i>channel</i> to <i>hangout</i></b>"
            return sync(team, args[0], args[2])
        elif name == "sync":
            if not (len(args) == 3 and args[1] == "from"):
                return "Usage: <b>unsync <i>channel</i> from <i>hangout</i></b>"
            return unsync(team, args[0], args[2])
