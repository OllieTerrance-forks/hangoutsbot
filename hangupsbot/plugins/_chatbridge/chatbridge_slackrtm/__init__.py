import asyncio
import logging

from slackclient import SlackClient

from webbridge import WebFramework
import plugins


logger = logging.getLogger(__name__)


class SlackMsg(object):

    def __init__(self, event):
        self.event = event
        self.channel = self.event.get("channel", self.event.get("group"))
        self.edited = self.event.get("subtype") == "message_changed"
        self.msg = self.event["message"] if self.edited else self.event
        self.user = self.msg.get("user", self.msg.get("comment", {}).get("user"))
        self.text = self.msg.get("text", self.msg.get("comment", {}).get("text"))


class BridgeInstance(WebFramework):

    def setup_plugin(self):
        self.plugin_name = "SlackRTM"
        self.slacks = {}

    def applicable_configuration(self, conv_id):
        configs = []
        for sync in self.configuration["syncs"]:
            if conv_id in sync["hangouts"]:
                configs.append({"trigger": conv_id, "config.json": sync})
        return configs

    @asyncio.coroutine
    def _send_to_external_chat(self, config, event):
        for channel in config["config.json"]["slack"]:
            slack = self.slacks[channel["team"]]
            user = event.passthru["original_request"]["user"]
            bridge_user = self._get_user_details(user, {"event": event})
            message = event.passthru["original_request"]["message"]
            slack.api_call("chat.postMessage",
                           channel=channel["channel"],
                           username=bridge_user["preferred_name"],
                           icon_url=bridge_user["photo_url"],
                           text=message,
                           link_names=True)

    def start_listening(self, bot):
        for team, config in self.configuration["teams"].items():
            plugins.start_asyncio_task(self._rtm_listen, team, config)

    @asyncio.coroutine
    def _rtm_listen(self, bot, team, config):
        logger.info("Starting RTM session for team: {}".format(team))
        slack = SlackClient(config["token"])
        self.slacks[team] = slack
        slack.rtm_connect()
        while True:
            events = slack.rtm_read()
            if not events:
                yield from asyncio.sleep(0.5)
                continue
            for event in events:
                if event["type"] == "message":
                    yield from self._handle_msg(event, team, config)

    @asyncio.coroutine
    def _handle_msg(self, event, team, config):
        msg = SlackMsg(event)
        for sync in self.configuration["syncs"]:
            for channel in sync["slack"]:
                if msg.channel == channel["channel"] and team == channel["team"]:
                    for conv_id in sync["hangouts"]:
                        yield from self._send_to_internal_chat(conv_id, msg.text,
                                                               {"source_user": msg.user,
                                                                "source_uid": msg.user,
                                                                "source_gid": msg.channel,
                                                                "source_title": msg.channel})


def _initialise(bot):
    BridgeInstance(bot, "slackrtm")