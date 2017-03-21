import asyncio
import json
import html
import logging
import mimetypes
import os
import pprint
import re
import threading
import time
import urllib.request
import hangups
import emoji

import hangups_shim as hangups

from slackclient import SlackClient
from websocket import WebSocketConnectionClosedException

from .bridgeinstance import ( BridgeInstance,
                              FakeEvent )
from .commands_slack import slackCommandHandler
from .utils import  ( _slackrtms,
                      _slackrtm_conversations_set,
                      _slackrtm_conversations_get )


logger = logging.getLogger(__name__)


# fix for simple_smile support
emoji.EMOJI_UNICODE[':simple_smile:'] = emoji.EMOJI_UNICODE[':smiling_face:']
emoji.EMOJI_ALIAS_UNICODE[':simple_smile:'] = emoji.EMOJI_UNICODE[':smiling_face:']

def chatMessageEvent2SlackText(event):
    def renderTextSegment(segment):
        out = ''
        if segment.is_bold:
            out += ' *'
        if segment.is_italic:
            out += ' _'
        out += segment.text
        if segment.is_italic:
            out += '_ '
        if segment.is_bold:
            out += '* '
        return out

    lines = ['']
    for segment in event.segments:
        if segment.type_ == hangups.schemas.SegmentType.TEXT:
            lines[-1] += renderTextSegment(segment)
        elif segment.type_ == hangups.schemas.SegmentType.LINK:
            lines[-1] += segment.text
        elif segment.type_ == hangups.schemas.SegmentType.LINE_BREAK:
            lines.append('')
        else:
            logger.warning('Ignoring unknown chat message segment type: %s', segment.type_)
    lines.extend(event.attachments)
    return '\n'.join(lines)


class ParseError(Exception):
    pass


class AlreadySyncingError(Exception):
    pass


class NotSyncingError(Exception):
    pass


class ConnectionFailedError(Exception):
    pass


class IncompleteLoginError(Exception):
    pass


class SlackMessage(object):
    def __init__(self, slackrtm, reply):
        self.text = None
        self.user = None
        self.username = None
        self.username4ho = None
        self.realname4ho = None
        self.tag_from_slack = None
        self.edited = None
        self.from_ho_id = None
        self.sender_id = None
        self.channel = None
        self.file_attachment = None

        if 'type' not in reply:
            raise ParseError('no "type" in reply: %s' % str(reply))

        if reply['type'] in ['pong', 'presence_change', 'user_typing', 'file_shared', 'file_public', 'file_comment_added', 'file_comment_deleted', 'message_deleted']:
            # we ignore pong's as they are only answers for our pings
            raise ParseError('not a "message" type reply: type=%s' % reply['type'])

        text = u''
        username = ''
        edited = ''
        from_ho_id = ''
        sender_id = ''
        channel = None
        is_joinleave = False
        # only used during parsing
        user = ''
        is_bot = False
        if reply['type'] == 'message' and 'subtype' in reply and reply['subtype'] == 'message_changed':
            if 'edited' in reply['message']:
                edited = '(Edited)'
                user = reply['message']['edited']['user']
                text = reply['message']['text']
            else:
                # sent images from HO got an additional message_changed subtype without an 'edited' when slack renders the preview
                if 'username' in reply['message']:
                    # we ignore them as we already got the (unedited) message
                    raise ParseError('ignore "edited" message from bot, possibly slack-added preview')
                else:
                    raise ParseError('strange edited message without "edited" member:\n%s' % str(reply))
        elif reply['type'] == 'message' and 'subtype' in reply and reply['subtype'] == 'file_comment':
            user = reply['comment']['user']
            text = reply['text']
        elif reply['type'] == 'file_comment_added':
            user = reply['comment']['user']
            text = reply['comment']['comment']
        else:
            if reply['type'] == 'message' and 'subtype' in reply and reply['subtype'] == 'bot_message' and 'user' not in reply:
                is_bot = True
                # this might be a HO relayed message, check if username is set and use it as username
                username = reply['username']
            elif 'text' not in reply or 'user' not in reply:
                raise ParseError('no text/user in reply:\n%s' % str(reply))
            else:
                user = reply['user']
            if 'text' not in reply or not len(reply['text']):
                # IFTTT?
                if 'attachments' in reply:
                    if 'text' in reply['attachments'][0]:
                        text = reply['attachments'][0]['text']
                    else:
                        raise ParseError('strange message without text in attachments:\n%s' % pprint.pformat(reply))
                    if 'fields' in reply['attachments'][0]:
                        for field in reply['attachments'][0]['fields']:
                            text += "\n*%s*\n%s" % (field['title'], field['value'])
                else:
                    raise ParseError('strange message without text and without attachments:\n%s' % pprint.pformat(reply))
            else:
                text = reply['text']
        file_attachment = None
        if 'file' in reply:
            if 'url_private_download' in reply['file']:
                file_attachment = reply['file']['url_private_download']

        # now we check if the message has the hidden ho relay tag, extract and remove it
        hoidfmt = re.compile(r'^(.*) <ho://([^/]+)/([^|]+)\| >$', re.MULTILINE | re.DOTALL)
        match = hoidfmt.match(text)
        if match:
            text = match.group(1)
            from_ho_id = match.group(2)
            sender_id = match.group(3)
            if 'googleusercontent.com' in text:
                gucfmt = re.compile(r'^(.*)<(https?://[^\s/]*googleusercontent.com/[^\s]*)>$', re.MULTILINE | re.DOTALL)
                match = gucfmt.match(text)
                if match:
                    text = match.group(1)
                    file_attachment = match.group(2)

        # text now contains the real message, but html entities have to be dequoted still
        text = html.unescape(text)

        username4ho = username
        realname4ho = username
        tag_from_slack = False # XXX: prevents key not defined on unmonitored channels
        if not is_bot:
            domain = slackrtm.get_slack_domain()
            username = slackrtm.get_username(user, user)
            realname = slackrtm.get_realname(user,username)

            username4ho = u'{2}'.format(domain, username, username)
            realname4ho = u'{2}'.format(domain, username, username)
            tag_from_slack = True
        elif sender_id != '':
            username4ho = u'{1}'.format(sender_id, username)
            realname4ho = u'{1}'.format(sender_id, username)
            tag_from_slack = False

        if 'channel' in reply:
            channel = reply['channel']
        elif 'group' in reply:
            channel = reply['group']
        if not channel:
            raise ParseError('no channel found in reply:\n%s' % pprint.pformat(reply))

        if reply['type'] == 'message' and 'subtype' in reply and reply['subtype'] in ['channel_join', 'channel_leave', 'group_join', 'group_leave']:
            is_joinleave = True

        self.text = text
        self.user = user
        self.username = username
        self.username4ho = username4ho
        self.realname4ho = realname4ho
        self.tag_from_slack = tag_from_slack
        self.edited = edited
        self.from_ho_id = from_ho_id
        self.sender_id = sender_id
        self.channel = channel
        self.file_attachment = file_attachment
        self.is_joinleave = is_joinleave


class SlackRTMSync(object):
    def __init__(self, channelid, hangoutid, hotag, slacktag, sync_joins=True, image_upload=True, showslackrealnames=False):
        self.channelid = channelid
        self.hangoutid = hangoutid
        self.hotag = hotag
        self.sync_joins = sync_joins
        self.image_upload = image_upload
        self.slacktag = slacktag
        self.showslackrealnames = showslackrealnames

    @staticmethod
    def fromDict(sync_dict):
        sync_joins = True
        if 'sync_joins' in sync_dict and not sync_dict['sync_joins']:
            sync_joins = False
        image_upload = True
        if 'image_upload' in sync_dict and not sync_dict['image_upload']:
            image_upload = False
        slacktag = None
        if 'slacktag' in sync_dict:
            slacktag = sync_dict['slacktag']
        else:
            slacktag = 'NOT_IN_CONFIG'
        realnames = True
        if 'showslackrealnames' in sync_dict and not sync_dict['showslackrealnames']:
            realnames = False
        return SlackRTMSync(sync_dict['channelid'], sync_dict['hangoutid'], sync_dict['hotag'], slacktag, sync_joins, image_upload, realnames)

    def toDict(self):
        return {
            'channelid': self.channelid,
            'hangoutid': self.hangoutid,
            'hotag': self.hotag,
            'sync_joins': self.sync_joins,
            'image_upload': self.image_upload,
            'slacktag': self.slacktag,
            'showslackrealnames': self.showslackrealnames,
            }

    def getPrintableOptions(self):
        return 'hotag="%s", sync_joins=%s, image_upload=%s, slacktag=%s, showslackrealnames=%s' % (
            self.hotag if self.hotag else 'NONE',
            self.sync_joins,
            self.image_upload,
            self.slacktag if self.slacktag else 'NONE',
            self.showslackrealnames,
            )


class SlackRTM(object):
    def __init__(self, sink_config, bot, loop, threaded=False, bridgeinstance=False):
        self.bot = bot
        self.loop = loop
        self.config = sink_config
        self.apikey = self.config['key']
        self.threadname = None
        self.lastimg = ''
        self._bridgeinstance = bridgeinstance

        self.slack = SlackClient(self.apikey)
        if not self.slack.rtm_connect():
            raise ConnectionFailedError
        for key in ['self', 'team', 'users', 'channels', 'groups']:
            if key not in self.slack.server.login_data:
                raise IncompleteLoginError
        if threaded:
            if 'name' in self.config:
                self.name = self.config['name']
            else:
                self.name = '%s@%s' % (self.slack.server.login_data['self']['name'], self.slack.server.login_data['team']['domain'])
                logger.warning('no name set in config file, using computed name %s', self.name)
            self.threadname = 'SlackRTM:' + self.name
            threading.current_thread().name = self.threadname
            logger.info('started RTM connection for SlackRTM thread %s', pprint.pformat(threading.current_thread()))
            for t in threading.enumerate():
                if t.name == self.threadname and t != threading.current_thread():
                    logger.info('old thread found: %s - killing it', pprint.pformat(t))
                    t.stop()

        self.update_userinfos(self.slack.server.login_data['users'])
        self.update_channelinfos(self.slack.server.login_data['channels'])
        self.update_groupinfos(self.slack.server.login_data['groups'])
        self.update_teaminfos(self.slack.server.login_data['team'])
        self.dminfos = {}
        self.my_uid = self.slack.server.login_data['self']['id']

        self.admins = []
        if 'admins' in self.config:
            for a in self.config['admins']:
                if a not in self.userinfos:
                    logger.warning('userid %s not found in user list, ignoring', a)
                else:
                    self.admins.append(a)
        if not len(self.admins):
            logger.warning('no admins specified in config file')

        self.hangoutids = {}
        self.hangoutnames = {}
        for c in self.bot.list_conversations():
            name = self.bot.conversations.get_name(c, truncate=True)
            self.hangoutids[name] = c.id_
            self.hangoutnames[c.id_] = name

        self.syncs = []
        syncs = _slackrtm_conversations_get(self.bot, self.name)
        if not syncs:
            syncs = []
        for s in syncs:
            sync = SlackRTMSync.fromDict(s)
            if sync.slacktag == 'NOT_IN_CONFIG':
                sync.slacktag = self.get_teamname()
            self.syncs.append(sync)
        if 'synced_conversations' in self.config and len(self.config['synced_conversations']):
            logger.warning('defining synced_conversations in config is deprecated')
            for conv in self.config['synced_conversations']:
                if len(conv) == 3:
                    hotag = conv[2]
                else:
                    if conv[1] not in self.hangoutnames:
                        logger.error("could not find conv %s in bot's conversations, but used in (deprecated) synced_conversations in config!", conv[1])
                        hotag = conv[1]
                    else:
                        hotag = self.hangoutnames[conv[1]]
                self.syncs.append(SlackRTMSync(conv[0], conv[1], hotag, self.get_teamname()))

    # As of https://github.com/slackhq/python-slackclient/commit/ac343caf6a3fd8f4b16a79246264a05a7d257760
    # SlackClient.api_call returns a pre-parsed json object (a dict).
    # Wrap this call in a compatibility duck-hunt.
    def api_call(self, *args, **kwargs):
        response = self.slack.api_call(*args, **kwargs)
        if isinstance(response, str):
            try:
                response = response.decode('utf-8')
            except:
                pass
            response = json.loads(response)

        return response

    def get_slackDM(self, userid):
        if not userid in self.dminfos:
            self.dminfos[userid] = self.api_call('im.open', user = userid)['channel']
        return self.dminfos[userid]['id']

    def update_userinfos(self, users=None):
        if users is None:
            response = self.api_call('users.list')
            users = response['members']
        userinfos = {}
        for u in users:
            userinfos[u['id']] = u
        self.userinfos = userinfos

    def get_channel_users(self, channelid, default=None):
        channelinfo = None
        if channelid.startswith('C'):
            if not channelid in self.channelinfos:
                self.update_channelinfos()
            if not channelid in self.channelinfos:
                logger.error('get_channel_users: Failed to find channel %s' % channelid)
                return None
            else:
                channelinfo = self.channelinfos[channelid]
        else:
            if not channelid in self.groupinfos:
                self.update_groupinfos()
            if not channelid in self.groupinfos:
                logger.error('get_channel_users: Failed to find private group %s' % channelid)
                return None
            else:
                channelinfo = self.groupinfos[channelid]

        channelusers = channelinfo['members']
        users = {}
        for u in channelusers:
            username = self.get_username(u)
            realname = self.get_realname(u, "No real name")
            if username:
                users[username+" "+u] = realname

        return users

    def update_teaminfos(self, team=None):
        if team is None:
            response = self.api_call('team.info')
            team = response['team']
        self.team = team

    def get_teamname(self):
        # team info is static, no need to update
        return self.team['name']

    def get_slack_domain(self):
        # team info is static, no need to update
        return self.team['domain']

    def get_realname(self, user, default=None):
        if user not in self.userinfos:
            logger.debug('user not found, reloading users')
            self.update_userinfos()
            if user not in self.userinfos:
                logger.warning('could not find user "%s" although reloaded', user)
                return default
        if not self.userinfos[user]['real_name']:
            return default
        return self.userinfos[user]['real_name']


    def get_username(self, user, default=None):
        if user not in self.userinfos:
            logger.debug('user not found, reloading users')
            self.update_userinfos()
            if user not in self.userinfos:
                logger.warning('could not find user "%s" although reloaded', user)
                return default
        return self.userinfos[user]['name']

    def update_channelinfos(self, channels=None):
        if channels is None:
            response = self.api_call('channels.list')
            channels = response['channels']
        channelinfos = {}
        for c in channels:
            channelinfos[c['id']] = c
        self.channelinfos = channelinfos

    def get_channelname(self, channel, default=None):
        if channel not in self.channelinfos:
            logger.debug('channel not found, reloading channels')
            self.update_channelinfos()
            if channel not in self.channelinfos:
                logger.warning('could not find channel "%s" although reloaded', channel)
                return default
        return self.channelinfos[channel]['name']

    def update_groupinfos(self, groups=None):
        if groups is None:
            response = self.api_call('groups.list')
            groups = response['groups']
        groupinfos = {}
        for c in groups:
            groupinfos[c['id']] = c
        self.groupinfos = groupinfos

    def get_groupname(self, group, default=None):
        if group not in self.groupinfos:
            logger.debug('group not found, reloading groups')
            self.update_groupinfos()
            if group not in self.groupinfos:
                logger.warning('could not find group "%s" although reloaded', group)
                return default
        return self.groupinfos[group]['name']

    def get_syncs(self, channelid=None, hangoutid=None):
        syncs = []
        for sync in self.syncs:
            if channelid == sync.channelid:
                syncs.append(sync)
            elif hangoutid == sync.hangoutid:
                syncs.append(sync)
        return syncs

    def rtm_read(self):
        return self.slack.rtm_read()

    def ping(self):
        return self.slack.server.ping()

    def matchReference(self, match):
        out = ""
        linktext = ""
        if match.group(5) == '|':
            linktext = match.group(6)
        if match.group(2) == '@':
            if linktext != "":
                out = linktext
            else:
                out = "@%s" % self.get_username(match.group(3), 'unknown:%s' % match.group(3))
        elif match.group(2) == '#':
            if linktext != "":
                out = "#%s" % linktext
            else:
                out = "#%s" % self.get_channelname(match.group(3), 'unknown:%s' % match.group(3))
        else:
            linktarget = match.group(1)
            if linktext == "":
                linktext = linktarget
            out = '<a href="%s">%s</a>' % (linktarget, linktext)
        out = out.replace('_', '%5F')
        out = out.replace('*', '%2A')
        out = out.replace('`', '%60')
        return out

    def textToHtml(self, text):
        reffmt = re.compile(r'<((.)([^|>]*))((\|)([^>]*)|([^>]*))>')
        text = reffmt.sub(self.matchReference, text)
        text = emoji.emojize(text, use_aliases=True)
        text = ' %s ' % text
        bfmt = re.compile(r'([\s*_`])\*([^*]*)\*([\s*_`])')
        text = bfmt.sub(r'\1<b>\2</b>\3', text)
        ifmt = re.compile(r'([\s*_`])_([^_]*)_([\s*_`])')
        text = ifmt.sub(r'\1<i>\2</i>\3', text)
        pfmt = re.compile(r'([\s*_`])```([^`]*)```([\s*_`])')
        text = pfmt.sub(r'\1"\2"\3', text)
        cfmt = re.compile(r'([\s*_`])`([^`]*)`([\s*_`])')
        text = cfmt.sub(r"\1'\2'\3", text)
        text = text.replace("\r\n", "\n")
        text = text.replace("\n", " <br/>")
        if text[0] == ' ' and text[-1] == ' ':
            text = text[1:-1]
        else:
            logger.warning('leading or trailing space missing: "%s"', text)
        return text

    @asyncio.coroutine
    def upload_image(self, hoid, image):
        try:
            token = self.apikey
            logger.info('downloading %s', image)
            filename = os.path.basename(image)
            request = urllib.request.Request(image)
            request.add_header("Authorization", "Bearer %s" % token)
            image_response = urllib.request.urlopen(request)
            content_type = image_response.info().get_content_type()
            filename_extension = mimetypes.guess_extension(content_type)
            if filename[-(len(filename_extension)):] != filename_extension:
                logger.info('No correct file extension found, appending "%s"' % filename_extension)
                filename += filename_extension
            logger.info('uploading as %s', filename)
            image_id = yield from self.bot._client.upload_image(image_response, filename=filename)
            logger.info('sending HO message, image_id: %s', image_id)
            self.bot.send_message_segments(hoid, None, image_id=image_id)
        except Exception as e:
            logger.exception('upload_image: %s(%s)', type(e), str(e))

    def config_syncto(self, channel, hangoutid, shortname):
        for sync in self.syncs:
            if sync.channelid == channel and sync.hangoutid == hangoutid:
                raise AlreadySyncingError

        sync = SlackRTMSync(channel, hangoutid, shortname, self.get_teamname())
        logger.info('adding sync: %s', sync.toDict())
        self.syncs.append(sync)
        syncs = _slackrtm_conversations_get(self.bot, self.name)
        if not syncs:
            syncs = []
        logger.info('storing sync: %s', sync.toDict())
        syncs.append(sync.toDict())
        _slackrtm_conversations_set(self.bot, self.name, syncs)
        return

    def config_disconnect(self, channel, hangoutid):
        sync = None
        for s in self.syncs:
            if s.channelid == channel and s.hangoutid == hangoutid:
                sync = s
                logger.info('removing running sync: %s', s)
                self.syncs.remove(s)
        if not sync:
            raise NotSyncingError

        syncs = _slackrtm_conversations_get(self.bot, self.name)
        if not syncs:
            syncs = []
        for s in syncs:
            if s['channelid'] == channel and s['hangoutid'] == hangoutid:
                logger.info('removing stored sync: %s', s)
                syncs.remove(s)
        _slackrtm_conversations_set(self.bot, self.name, syncs)
        return

    def config_setsyncjoinmsgs(self, channel, hangoutid, enable):
        sync = None
        for s in self.syncs:
            if s.channelid == channel and s.hangoutid == hangoutid:
                sync = s
        if not sync:
            raise NotSyncingError

        logger.info('setting sync_joins=%s for sync=%s', enable, sync.toDict())
        sync.sync_joins = enable

        syncs = _slackrtm_conversations_get(self.bot, self.name)
        if not syncs:
            syncs = []
        for s in syncs:
            if s['channelid'] == channel and s['hangoutid'] == hangoutid:
                syncs.remove(s)
        logger.info('storing new sync=%s with changed sync_joins', s)
        syncs.append(sync.toDict())
        _slackrtm_conversations_set(self.bot, self.name, syncs)
        return

    def config_sethotag(self, channel, hangoutid, hotag):
        sync = None
        for s in self.syncs:
            if s.channelid == channel and s.hangoutid == hangoutid:
                sync = s
        if not sync:
            raise NotSyncingError

        logger.info('setting hotag="%s" for sync=%s', hotag, sync.toDict())
        sync.hotag = hotag

        syncs = _slackrtm_conversations_get(self.bot, self.name)
        if not syncs:
            syncs = []
        for s in syncs:
            if s['channelid'] == channel and s['hangoutid'] == hangoutid:
                syncs.remove(s)
        logger.info('storing new sync=%s with changed hotag', s)
        syncs.append(sync.toDict())
        _slackrtm_conversations_set(self.bot, self.name, syncs)
        return

    def config_setimageupload(self, channel, hangoutid, upload):
        sync = None
        for s in self.syncs:
            if s.channelid == channel and s.hangoutid == hangoutid:
                sync = s
        if not sync:
            raise NotSyncingError

        logger.info('setting image_upload=%s for sync=%s', upload, sync.toDict())
        sync.image_upload = upload

        syncs = _slackrtm_conversations_get(self.bot, self.name)
        if not syncs:
            syncs = []
        for s in syncs:
            if s['channelid'] == channel and s['hangoutid'] == hangoutid:
                syncs.remove(s)
        logger.info('storing new sync=%s with changed hotag', s)
        syncs.append(sync.toDict())
        _slackrtm_conversations_set(self.bot, self.name, syncs)
        return

    def config_setslacktag(self, channel, hangoutid, slacktag):
        sync = None
        for s in self.syncs:
            if s.channelid == channel and s.hangoutid == hangoutid:
                sync = s
        if not sync:
            raise NotSyncingError

        logger.info('setting slacktag="%s" for sync=%s', slacktag, sync.toDict())
        sync.slacktag = slacktag

        syncs = _slackrtm_conversations_get(self.bot, self.name)
        if not syncs:
            syncs = []
        for s in syncs:
            if s['channelid'] == channel and s['hangoutid'] == hangoutid:
                syncs.remove(s)
        logger.info('storing new sync=%s with changed hotag', s)
        syncs.append(sync.toDict())
        _slackrtm_conversations_set(self.bot, self.name, syncs)
        return

    def config_showslackrealnames(self, channel, hangoutid, realnames):
        sync = None
        for s in self.syncs:
            if s.channelid == channel and s.hangoutid == hangoutid:
                sync = s
        if not sync:
            raise NotSyncingError

        logger.info('setting showslackrealnames=%s for sync=%s', realnames, sync.toDict())
        sync.showslackrealnames = realnames

        syncs = _slackrtm_conversations_get(self.bot, self.name)
        if not syncs:
            syncs = []
        for s in syncs:
            if s['channelid'] == channel and s['hangoutid'] == hangoutid:
                syncs.remove(s)
        logger.info('storing new sync=%s with changed hotag', s)
        syncs.append(sync.toDict())
        _slackrtm_conversations_set(self.bot, self.name, syncs)
        return

    def handle_reply(self, reply):
        """handle incoming replies from slack"""

        try:
            msg = SlackMessage(self, reply)
        except ParseError as e:
            return
        except Exception as e:
            logger.exception('error parsing Slack reply: %s(%s)', type(e), str(e))
            return

        syncs = self.get_syncs(channelid=msg.channel)
        if not syncs:
            """since slackRTM listens to everything, we need a quick way to filter out noise. this also
            has the added advantage of making slackrtm play well with other slack plugins"""
            return

        msg_html = self.textToHtml(msg.text)

        try:
            slackCommandHandler(self, msg)
        except Exception as e:
            logger.exception('error in handleCommands: %s(%s)', type(e), str(e))

        for sync in syncs:
            if not sync.sync_joins and msg.is_joinleave:
                continue

            if msg.from_ho_id != sync.hangoutid:
                user = msg.realname4ho if sync.showslackrealnames else msg.username4ho

                if msg.file_attachment:
                    if sync.image_upload:
                        self.loop.call_soon_threadsafe(asyncio.async, self.upload_image(sync.hangoutid, msg.file_attachment))
                        self.lastimg = os.path.basename(msg.file_attachment)
                    else:
                        # we should not upload the images, so we have to send the url instead
                        response += msg.file_attachment

                channel_name = self.get_channelname(msg.channel)

                self.loop.call_soon_threadsafe(
                    asyncio.async,
                    self._bridgeinstance._send_to_internal_chat(
                        sync.hangoutid,
                        msg_html,
                        {   "sync": sync,
                            "from_user": user,
                            "from_chat": channel_name }))

    @asyncio.coroutine
    def _send_deferred_photo(self, image_link, sync, full_name, link_names, photo_url, fragment):
        self.api_call('chat.postMessage',
                      channel = sync.channelid,
                      text = "{} {}".format(image_link, fragment),
                      username = full_name,
                      link_names = True,
                      icon_url = photo_url)

    @asyncio.coroutine
    def handle_ho_message(self, event, conv_id):
        user = event.passthru["original_request"]["user"]
        message = event.passthru["original_request"]["message"]

        # XXX: rudimentary conversion of html to markdown
        message = re.sub(r"</?b>", "*", message)
        message = re.sub(r"</?i>", "_", message)
        message = re.sub(r"</?pre>", "`", message)

        bridge_user = self._bridgeinstance._get_user_details(user)

        for sync in self.get_syncs(hangoutid=conv_id):
            display_name = bridge_user["preferred_name"]

            if sync.hotag:
                if sync.hotag is True:
                    if "chatbridge" in event.passthru and event.passthru["chatbridge"]["source_title"]:
                        chat_title = event.passthru["chatbridge"]["source_title"]
                        display_name += " ({})".format(chat_title)
                elif sync.hotag is not True and sync.hotag:
                    display_name += " ({})".format(sync.hotag)

            slackrtm_fragment = "<ho://{}/{}| >".format(conv_id, bridge_user["chat_id"])

            message = "{} {}".format(message, slackrtm_fragment)

            """XXX: deferred image sending

            this plugin leverages existing storage in hangouts - since there isn't a direct means
            to acquire the public url of a hangups-upload file we need to wait for other handlers to post
            the image in hangouts, which generates the public url, which we will send in a deferred post.

            handlers.image_uri_from() is packaged as a task to wait for an image link to be associated with
            an image id that this handler sees
            """

            if( "image_id" in event.passthru["original_request"]
                    and event.passthru["original_request"]["image_id"] ):

                if( "conv_event" in event
                        and "attachments" in event.conv_event
                        and len(event.conv_event.attachments) == 1 ):

                    message = "shared an image: {}".format(event.conv_event.attachments[0])
                else:
                    # without attachments, create a deferred post until the public image url becomes available
                    image_id = event.passthru["original_request"]["image_id"]

                    loop = asyncio.get_event_loop()
                    task = loop.create_task(
                        self.bot._handlers.image_uri_from(
                            image_id,
                            self._send_deferred_photo,
                            sync,
                            display_name,
                            True,
                            bridge_user["photo_url"],
                            slackrtm_fragment ))

            """standard message relay"""

            logger.debug("sending to channel %s: %s", sync.channelid, message.encode('utf-8'))
            self.api_call('chat.postMessage',
                          channel = sync.channelid,
                          text = message,
                          username = display_name,
                          link_names = True,
                          icon_url = bridge_user["photo_url"])

    def handle_ho_membership(self, event):
        # Generate list of added or removed users
        links = []
        for user_id in event.conv_event.participant_ids:
            user = event.conv.get_user(user_id)
            links.append(u'<https://plus.google.com/%s/about|%s>' % (user.id_.chat_id, user.full_name))
        names = u', '.join(links)

        for sync in self.get_syncs(hangoutid=event.conv_id):
            if not sync.sync_joins:
                continue
            if sync.hotag:
                honame = sync.hotag
            else:
                honame = self.bot.conversations.get_name(event.conv)
            # JOIN
            if event.conv_event.type_ == hangups.MembershipChangeType.JOIN:
                invitee = u'<https://plus.google.com/%s/about|%s>' % (event.user_id.chat_id, event.user.full_name)
                if invitee == names:
                    message = u'%s has joined %s' % (invitee, honame)
                else:
                    message = u'%s has added %s to %s' % (invitee, names, honame)
            # LEAVE
            else:
                message = u'%s has left _%s_' % (names, honame)
            message = u'%s <ho://%s/%s| >' % (message, event.conv_id, event.user_id.chat_id)
            logger.debug("sending to channel/group %s: %s", sync.channelid, message)
            self.api_call('chat.postMessage',
                          channel=sync.channelid,
                          text=message,
                          as_user=True,
                          link_names=True)

    def handle_ho_rename(self, event):
        name = self.bot.conversations.get_name(event.conv, truncate=False)

        for sync in self.get_syncs(hangoutid=event.conv_id):
            invitee = u'<https://plus.google.com/%s/about|%s>' % (event.user_id.chat_id, event.user.full_name)
            hotagaddendum = ''
            if sync.hotag:
                hotagaddendum = ' _%s_' % sync.hotag
            message = u'%s has renamed the Hangout%s to _%s_' % (invitee, hotagaddendum, name)
            message = u'%s <ho://%s/%s| >' % (message, event.conv_id, event.user_id.chat_id)
            logger.debug("sending to channel/group %s: %s", sync.channelid, message)
            self.api_call('chat.postMessage',
                          channel=sync.channelid,
                          text=message,
                          as_user=True,
                          link_names=True)

class SlackRTMThread(threading.Thread):
    def __init__(self, bot, loop, config):
        super(SlackRTMThread, self).__init__()
        self._stop = threading.Event()
        self._bot = bot
        self._loop = loop
        self._config = config
        self._listener = None
        self._bridgeinstance = BridgeInstance(bot, "slackrtm")

    def run(self):
        logger.debug('SlackRTMThread.run()')
        asyncio.set_event_loop(self._loop)

        try:
            if self._listener and self._listener in _slackrtms:
                _slackrtms.remove(self._listener)
            self._listener = SlackRTM(self._config, self._bot, self._loop, threaded=True, bridgeinstance=self._bridgeinstance)
            _slackrtms.append(self._listener)
            last_ping = int(time.time())
            while True:
                if self.stopped():
                    return
                replies = self._listener.rtm_read()
                if replies:
                    if 'type' in replies[0]:
                        if replies[0]['type'] == 'hello':
                        # print('slackrtm: ignoring first replies including type=hello message to avoid message duplication: %s...' % str(replies)[:30])
                            continue
                    for reply in replies:
                        try:
                            self._listener.handle_reply(reply)
                        except Exception as e:
                            logger.exception('error during handle_reply(): %s\n%s', str(e), pprint.pformat(reply))
                now = int(time.time())
                if now > last_ping + 30:
                    self._listener.ping()
                    last_ping = now
                time.sleep(.1)
        except KeyboardInterrupt:
            # close, nothing to do
            return
        except WebSocketConnectionClosedException as e:
            logger.exception('WebSocketConnectionClosedException(%s)', str(e))
            return self.run()
        except IncompleteLoginError:
            logger.exception('IncompleteLoginError, restarting')
            time.sleep(1)
            return self.run()
        except (ConnectionFailedError, TimeoutError):
            logger.exception('Connection failed or Timeout, waiting 10 sec trying to restart')
            time.sleep(10)
            return self.run()
        except ConnectionResetError:
            logger.exception('ConnectionResetError, attempting to restart')
            time.sleep(1)
            return self.run()
        except Exception as e:
            logger.exception('SlackRTMThread: unhandled exception: %s', str(e))
        return

    def stop(self):
        if self._listener and self._listener in _slackrtms:
            _slackrtms.remove(self._listener)
        self._stop.set()

    def stopped(self):
        return self._stop.isSet()
