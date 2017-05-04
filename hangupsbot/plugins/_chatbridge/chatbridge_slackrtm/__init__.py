import plugins

from .bridge import BridgeInstance
from .core import Base, Slack
from .commands import slack_identify, slack_members, slack_sync, slack_unsync
from .utils import convert_legacy_config


def _initialise(bot):
    convert_legacy_config(bot)
    plugins.register_user_command(["slack_identify", "slack_members"])
    plugins.register_admin_command(["slack_sync", "slack_unsync"])
    root = bot.get_config_option("slackrtm") or {}
    Base.bot = bot
    for team, config in root.get("teams", {}).items():
        Base.add_slack(team, Slack(config["token"]))
    for sync in root.get("syncs", []):
        Base.add_bridge(BridgeInstance(bot, "slackrtm", sync))
    for slack in Base.slacks.values():
        plugins.start_asyncio_task(slack.rtm())
