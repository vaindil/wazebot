import asyncio
import logging

from collections import namedtuple

import plugins
import threadmanager

from sinks import aiohttp_start
from sinks.base_bot_request_handler import AsyncRequestHandler as IncomingRequestHandler


logger = logging.getLogger(__name__)

class FakeEvent:
    def __init__(self, text, user, passthru, conv_id=None):
        self.text = text
        self.user = user
        self.passthru = passthru
        self.conv_id = conv_id

FakeUser = namedtuple( 'user', [ 'full_name',
                                 'id_' ])

FakeUserID = namedtuple( 'userID', [ 'chat_id',
                                     'gaia_id' ])

class WebFramework:
    def __init__(self, bot, configkey, RequestHandler=IncomingRequestHandler):
        self.plugin_name = False
        self.bot = self._bot = bot
        self.configkey = configkey
        self.RequestHandler = RequestHandler

        if not self.load_configuration(configkey):
            logger.info("no configuration for {}, not running".format(self.configkey))
            return

        self.setup_plugin()

        if not self.plugin_name:
            logger.warning("plugin_name not defined in code, not running")
            return

        plugins.register_handler(self._broadcast, type="sending")
        plugins.register_handler(self._repeat, type="allmessages")

        self.start_listening(bot)

    def load_configuration(self, configkey):
        self.configuration = self.bot.get_config_option(self.configkey)
        return self.configuration

    def setup_plugin(self):
        logger.warning("setup_plugin should be overridden by derived class")

    def applicable_configuration(self, conv_id):
        """standardised configuration structure:

            "<EXTERNAL_CHAT_NAME>": [
                {
                    "bot_api_key": <api key(s) for bot>,
                    "hangouts": [
                        "<at least 1 internal chat/group/team id>"
                    ],
                    "<EXTERNAL_CHAT_NAME>": [
                        "<at least 1 external chat/group/team id>"
                    ]
                }
            ]

        """

        self.load_configuration(self.configkey)

        applicable_configurations = []
        for configuration in self.configuration:
            if conv_id in configuration["hangouts"]:
                applicable_configurations.append({ "trigger": conv_id,
                                                   "config.json": configuration })

        return applicable_configurations

    @asyncio.coroutine
    def _broadcast(self, bot, broadcast_list, context):
        conv_id = broadcast_list[0][0]
        message = broadcast_list[0][1]
        image_id = broadcast_list[0][2]

        applicable_configurations = self.applicable_configuration(conv_id)
        if not applicable_configurations:
            return

        passthru = context["passthru"]

        if "norelay" not in passthru:
            passthru["norelay"] = []
        if self.plugin_name in passthru["norelay"]:
            # prevent message broadcast duplication
            logger.info("_broadcast:NORELAY:{}@{}".format(passthru["norelay"], self.plugin_name))
            return
        else:
            # halt messaging handler from re-relaying
            passthru["norelay"].append(self.plugin_name)

        user = self.bot._user_list._self_user
        chat_id = user.id_.chat_id

        # context preserves as much of the original request as possible
        if "original_request" in passthru:
            message = passthru["original_request"]["message"]
            image_id = passthru["original_request"]["image_id"]
            segments = passthru["original_request"]["segments"]
            if "user" in passthru["original_request"]:
                if(isinstance(passthru["original_request"]["user"], str)):
                    user = FakeUser( full_name = str,
                                     id_ = FakeUserID( chat_id = chat_id,
                                                       gaia_id = chat_id ))
                else:
                    user = passthru["original_request"]["user"]
            else:
                # add bot if no user is present
                passthru["original_request"]["user"] = user
        else:
            # bot is sending a message
            passthru["original_request"] = { "message": message,
                                             "image_id": None,
                                             "segments": None,
                                             "user": user }
            passthru["chatbridge"] = { "source_title": bot.conversations.get_name(conv_id),
                                       "source_user": user }

        # for messages from other plugins, relay them
        for config in applicable_configurations:
            yield from self._send_to_external_chat(
                config,
                FakeEvent(
                    text = message,
                    user = user,
                    passthru = passthru ))

    @asyncio.coroutine
    def _repeat(self, bot, event, command):
        conv_id = event.conv_id

        applicable_configurations = self.applicable_configuration(conv_id)
        if not applicable_configurations:
            return

        passthru = event.passthru

        if "norelay" not in passthru:
            passthru["norelay"] = []
        if self.plugin_name in passthru["norelay"]:
            # prevent message relay duplication
            logger.info("_repeat:NORELAY:{}@{}".format(passthru["norelay"],self.plugin_name))
            return
        else:
            # halt sending handler from re-relaying
            passthru["norelay"].append(self.plugin_name)

        user = event.user
        message = event.text
        image_id = None

        if "original_request" in passthru:
            message = passthru["original_request"]["message"]
            image_id = passthru["original_request"]["image_id"]
            segments = passthru["original_request"]["segments"]
            # user is only assigned once, upon the initial event
            if "user" in passthru["original_request"]:
                user = passthru["original_request"]["user"]
            else:
                passthru["original_request"]["user"] = user
        else:
            # user raised an event
            passthru["original_request"] = { "message": event.text,
                                             "image_id": None, # XXX: should be attachments
                                             "segments": event.conv_event.segments,
                                             "user": event.user }
            passthru["chatbridge"] = { "source_title": bot.conversations.get_name(conv_id),
                                       "source_user": event.user }

        for config in applicable_configurations:
            yield from self._send_to_external_chat(config, event)

    @asyncio.coroutine
    def _send_to_external_chat(self, config, event):
        pass

    @asyncio.coroutine
    def _send_to_internal_chat(self, conv_id, message, external_context, image_id=None):
        formatted_message = self.format_incoming_message(message, external_context)

        from_user = self.plugin_name
        if "from_user" in external_context and external_context["from_user"]:
            from_user = external_context["from_user"]
        from_chat = self.plugin_name
        if "from_chat" in external_context and external_context["from_chat"]:
            from_chat = external_context["from_chat"]

        passthru =  {
            "original_request": {
                "message": message,
                "image_id": image_id,
                "segments": None,
                "user": from_user },
            "chatbridge": {
                "source_title": from_chat },
            "norelay": [ self.plugin_name ] }

        yield from self.bot.coro_send_message(
            conv_id,
            formatted_message,
            image_id = image_id,
            context = { "passthru": passthru })

    def format_incoming_message(self, message, external_context):
        if "from_user" in external_context and external_context["from_user"]:
            from_user = external_context["from_user"]
        else:
            from_user = self.plugin_name

        bridge_user = self._get_user_details(from_user)

        if "from_user" in external_context:
            from_chat = external_context["from_chat"]
        else:
            from_chat = self.plugin_name

        if from_chat:
            formatted = "+{} ({})+: {}".format(bridge_user["preferred_name"], from_chat, message)
        else:
            formatted = "+{}+: {}".format(bridge_user["preferred_name"], message)

        return formatted

    def format_outgoing_message(self, message, internal_context):
        formatted = message

        return formatted

    def _get_user_details(self, user, additional_context=None):
        chat_id = None # guaranteed
        preferred_name = None # guaranteed
        full_name = None
        nickname = None
        photo_url = None

        if isinstance(user, str):
            full_name = user
        else:
            chat_id = user.id_.chat_id
            permauser = self.bot.get_hangups_user(chat_id)
            nickname = self.bot.get_memory_suboption(chat_id, 'nickname') or None
            if isinstance(permauser, dict):
                full_name = permauser["full_name"]
                if "photo_url" in permauser:
                    photo_url = permauser["photo_url"]
            else:
                full_name = permauser.full_name
                photo_url = permauser.photo_url
            if photo_url and not photo_url.startswith("http"):
                photo_url = "https:" + photo_url

        if nickname:
            preferred_name = nickname
        else:
            preferred_name = full_name

        if not chat_id:
            chat_id = preferred_name

        return { "chat_id": chat_id,
                 "preferred_name": preferred_name,
                 "nickname": nickname,
                 "full_name": full_name,
                 "photo_url": photo_url }

    def _format_message(self, message, user, userwrap="MARKDOWN_BOLD2"):
        if userwrap == "MARKDOWN_BOLD": # telegram/slack
            userwrap_left = "*"
            userwrap_right = "*"
        elif userwrap == "MARKDOWN_BOLD2": # github/hangups/hangoutsbot
            userwrap_left = "**"
            userwrap_right = "**"
        elif userwrap == "HTML_BOLD":
            userwrap_left = "<b>"
            userwrap_right = "</b>"
        else:
            userwrap_left = ""
            userwrap_right = ""

        if isinstance(user, str):
            formatted_message = "{2}{0}{3}: {1}".format(user, message, userwrap_left, userwrap_right)
        else:
            bridge_user = self._get_user_details(user)
            formatted_message = "{2}{0}{3}: {1}".format(bridge_user["preferred_name"], message, userwrap_left, userwrap_right)

        return formatted_message

    def start_listening(self, bot):
        loop = asyncio.get_event_loop()

        itemNo = -1
        threads = []

        if isinstance(self.configuration, list):
            for listener in self.configuration:
                itemNo += 1

                try:
                    certfile = listener["certfile"]
                    if not certfile:
                        logger.warning("config.{}[{}].certfile must be configured".format(self.configkey, itemNo))
                        continue
                    name = listener["name"]
                    port = listener["port"]
                except KeyError as e:
                    logger.warning("config.{}[{}] missing keyword".format(self.configkey, itemNo))
                    continue

                aiohttp_start(
                    bot,
                    name,
                    port,
                    certfile,
                    self.RequestHandler,
                    "webbridge." + self.configkey)

        logger.info("webbridge.sinks: {} thread(s) started for {}".format(itemNo + 1, self.configkey))
