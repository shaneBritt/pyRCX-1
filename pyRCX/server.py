import logging
import os
import re
import socket
import sys
import threading
import time
import traceback
from copy import copy
from hashlib import sha256
from pickle import dumps, loads
from random import random
from select import select
from traceback import extract_tb
from typing import Dict, List
from zlib import compress, decompress

from .access import AccessInformation
from .channel import Channel
from .commands.channel import JoinCommand
from .filtering import FilterEntry, Filtering
from .nickserv import NickServEntry
from .operator import OperatorEntry
from .raw import Raw
from .server_context import ServerContext

from .statistics import Statistics
# This class needs a major re-work including the nested hierarchy of threading/run methods
from .user import User

import pyRCX.access as access_helper

server_context: ServerContext = ServerContext()

filtering: Filtering = server_context.configuration.filtering

disabled_functionality: Dict[str, int] = {}

statistics: Statistics = Statistics(server_context)

raw_messages = Raw(server_context.configuration, statistics, disabled_functionality)

# Commands
join_command: JoinCommand = JoinCommand(server_context, raw_messages)
# Here are some settings, these can be coded into the conf later I suppose

character_encoding = "latin1"

NickfloodAmount = 5
NickfloodWait = 30
MaxServerEntries = 0
MaxUserEntries = 0
MaxChannelEntries = 0
HostMasking = 0
HostMaskingParam = ""
ipaddress = ""
PrefixChar = ""
AdminPassword = ""
ServerAddress = ""
NetworkName = ""
ServerAdmin1 = ""
ServerAdmin2 = ""
ServerPassword = ""
passmsg = ""
MaxUsers = 10
MaxUsersPerConnection = 3
NickservParam = ""
defconMode = 1

Ports = []
temp_noopers = []
operlines = []
connections = []

connectionsExempt = []

createmute = {}
nickmute = {}

currentports = {}
NickservIPprotection = True

writeUsers_lock = False


def stripx01(badstring):
    return badstring.replace("\x01", "")


def load_nickserv_database():
    logger = logging.getLogger('NICKSERV')

    try:
        with open(server_context.configuration.nickserv_database_file, "rb") as file:
            rdata = file.read()

            if rdata != "":
                server_context.nickserv_entries = loads(decompress(rdata))
    except Exception as e:
        logger.info("Could not load NickServ database, an empty in-memory database will be used")
        logger.debug(e)
        server_context.nickserv_entries = {}


def WriteUsers(nicksv=True, chans=True, access=False):
    logger = logging.getLogger('PERSISTENCE')

    global writeUsers_lock
    if writeUsers_lock == False:
        writeUsers_lock = True
        try:
            statistics.save()

            if nicksv:
                with open(server_context.configuration.nickserv_database_file, "wb") as file:
                    file.write(compress(dumps(server_context.nickserv_entries)))

            if chans:
                with open(server_context.configuration.channels_database_file, "wb") as file:
                    schan = copy(server_context.channel_entries)
                    for each in schan:
                        chanid = server_context.channel_entries[each.lower()]
                        if chanid.MODE_registered:
                            file.write(
                                ("C=%s\x01=%s\x01=%s\x01=%s\x01=%s\x01=%s\r\n" %
                                 (stripx01(chanid.channelname),
                                  stripx01(chanid.GetChannelModes(0, True)),
                                  stripx01(chanid._topic),
                                  chanid._founder, compress(dumps(chanid._prop)).hex(),
                                  compress(dumps(chanid.ChannelAccess)).hex())).encode(character_encoding))

            if access:
                with open(server_context.configuration.access_database_file, "wb") as file:
                    file.write(dumps(server_context.server_access_entries))

        except Exception as exception:
            logger.error(exception)

        writeUsers_lock = False


def rehash(par=1):  # this information will be rehashed by any operator with level 4 privlidges (Administrator)
    myfile = open(server_context.configuration.server_config_file, "r")
    try:
        global ServerAddress, server_name, NetworkName, connectionsExempt, operlines, Ports
        global MaxUsers, MaxUsersPerConnection, NickfloodAmount, NickfloodWait
        global NickservParam, ipaddress, ServerAdmin1, ServerAdmin2, AdminPassword, ServerPassword
        global passmsg, HostMaskingParam, HostMasking, PrefixChar, MaxServerEntries, MaxChannelEntries, MaxUserEntries
        global defconMode, UserDefaultModes

        operlines = []
        Ports = []
        connectionsExempt = []

        line_num = 0

        # TODO this does not fix an existing race condition where the filters may be bypassed whilst a rehash is occurring
        filtering.clear_filters()
        disabled_functionality.clear()

        for lineStr in myfile.readlines():

            line_num += 1
            if lineStr[0] == "S":
                s_line = lineStr.split(":")

                ServerAddress = s_line[1]
                server_context.configuration.server_name = s_line[2]
                NetworkName = s_line[3].split(";")[0]

            if lineStr[0] == "E":
                s_line = lineStr.split(":")
                connectionsExempt.append(s_line[1].split(";")[0])

            if lineStr[0] == "U":
                s_line = lineStr.split(":")
                MaxUsers = s_line[1]
                MaxUsersPerConnection = int(s_line[2])
                UserDefaultModes = s_line[3].split(";")[0]

            if lineStr[0] == "N":
                s_line = lineStr.split(":")
                NickfloodAmount = int(s_line[1])
                NickfloodWait = int(s_line[2].split(";")[0])

            if lineStr[0] == "n":
                s_line = lineStr.split(":")
                NickservParam = s_line[1]
                defconMode = int(s_line[2].split(";")[0])
                if defconMode != 1 and defconMode != 2 and defconMode != 3:
                    defconMode = 1

            if lineStr[0] == "T":
                # Currently unused until linking
                pass

            if lineStr[0] == "I":
                s_line = lineStr.split(":")
                ipaddress = s_line[1].split(";")[0]

            if lineStr[0] == "A":
                s_line = lineStr.split(":")
                ServerAdmin1 = s_line[1]
                ServerAdmin2 = s_line[2]
                AdminPassword = s_line[3].split(";")[0]

            if lineStr[0] == "P":
                s_line = lineStr.split(":")
                ServerPassword = s_line[1]
                passmsg = s_line[2].split(";")[0]

            if lineStr[0] == "p":
                s_line = lineStr.split(":")
                Ports.append(s_line[1].split(";")[0])

            if lineStr[0] == "f":
                s_line = lineStr.split(":")
                server_context.configuration.flooding_exempt_commands.append(s_line[1].upper().split(";")[0])

            if lineStr[0] == "D":
                s_line = lineStr.split(":")
                if len(s_line) == 2:
                    value = 0
                else:
                    value = s_line[2].split(";")[0]

                disabled_functionality[s_line[1].upper()] = value

            if lineStr[0] == "H":
                s_line = lineStr.split(":")
                HostMasking = s_line[1]
                HostMaskingParam = s_line[2].split(";")[0]

            if lineStr[0] == "s":
                s_line = lineStr.split(":")
                PrefixChar = s_line[1].split(";")[0]

            if lineStr[0] == "X":
                s_line = lineStr.split(":")
                MaxServerEntries = s_line[1]
                MaxChannelEntries = s_line[2]
                MaxUserEntries = s_line[3].split(";")[0]

            if lineStr[0] == "C":
                s_line = lineStr.split(":")
                server_context.configuration.default_modes = s_line[1]
                server_context.configuration.max_channels = int(s_line[2])
                server_context.configuration.max_channels_per_user = int(s_line[3])
                server_context.configuration.channel_lockdown = int(s_line[4].split(";")[0])

            if lineStr[0] == "c":
                s_line = lineStr.split(":")
                server_context.configuration.channel_prefix = s_line[1].split(";")[0]

            if lineStr[0] == "O":
                s_line = lineStr.split(":")
                operlines.append(OperatorEntry(s_line[1], s_line[2], s_line[3], s_line[4].split(";")[0]))

            if lineStr[0] == "F":
                s_line = lineStr.split(":")
                if s_line[1] == "profanity":
                    # TODO this should really just be part of the filtering still not another global variable
                    server_context.configuration.profanity_entries.append(s_line[2])
                else:
                    filtering.add_filter(FilterEntry(s_line[1], s_line[2], s_line[3].split(";")[0]))

            lineStr = myfile.readline()

        myfile.close()
    except:
        tuError = sys.exc_info()
        _lastError.append([tuError, [time.strftime("%a %b %d %H:%M:%S %Y GMT", time.localtime()),
                                     "System rehash on line: " + str(line_num)], extract_tb(tuError[2])])
        print("Rehash error, line: " + str(line_num))


def myint(strdata):
    try:
        return int(strdata)
    except:
        return 0


def iif(state, stateiftrue, stateiffalse):
    if state == "" or state == False:
        return stateiffalse
    else:
        return stateiftrue


def isSecret(channel, extra="", extra2=""):
    if channel.MODE_secret:
        return True
    if channel.MODE_servicechan:
        return True
    if extra == "hidden" or extra2 == "hidden":
        if channel.MODE_hidden:
            return True

    if extra == "private" or extra2 == "private":
        if channel.MODE_private:
            return True

    return False


def isAdmin(nick):
    opid = getOperOBJ(nick.lower())
    if opid:
        if opid.operator_level >= 3:
            return opid.username
        else:
            return ""
    else:
        return ""


# this isOp will help


def isOp(nick, channel):  # return true or false depending upon whether nick is oper
    chan = getChannelOBJ(channel.lower())
    if nick.lower() in chan._op or nick.lower() in chan._owner or getOperOBJ(nick.lower()):
        return True
    else:
        return False


def InChannel(s, them):
    for eachchan in them._channels:
        chanid = getChannelOBJ(eachchan.lower())
        if s._nickname.lower() in chanid._users:
            return True

    return False


def Whouser(_whouser, chan, selfn):
    if len(_whouser._channels) > 0:
        if chan != "":
            _whochan_ = server_context.channel_entries[chan]
        else:
            _whochan_ = server_context.channel_entries[_whouser._channels[0].lower()]

        if isSecret(_whochan_, "private",
                    "hidden") and selfn._nickname.lower() not in _whochan_._users and selfn._nickname.lower() not in server_context.operator_entries:
            _whochan = "*"
        else:
            if chan != "":
                _whochan_ = server_context.channel_entries[chan.lower()]
            _whochan = _whochan_.channelname
    else:
        _whochan = "*"  # not in any channels

    if _whouser._nickname.lower() in server_context.operator_entries:
        opid = server_context.operator_entries[_whouser._nickname.lower()]
        if opid.hidden:
            _isoper = ""
        else:
            if opid.guide:
                _isoper = "g"
            else:
                if opid.operator_level > 2:
                    _isoper = "a"
                else:
                    _isoper = "*"
    else:
        _isoper = ""

    if _whouser._away == "":
        _whoaway = "H"
    else:
        _whoaway = "G"

    _whomode = ""
    if _whochan != "*":
        if _whouser._nickname.lower() in _whochan_._op:
            _whomode = "@"
        if _whouser._nickname.lower() in _whochan_._owner:
            _whomode = "."
        if _whouser._nickname.lower() in _whochan_._voice:
            _whomode += "+"

    if _whochan != "*":
        if _whochan_.MODE_auditorium and isOp(
                selfn._nickname, _whochan_.channelname) == False and isOp(
            _whouser._nickname, _whochan_.channelname) == False and _whouser != selfn:
            _whochan = "*"

        if _whouser._nickname.lower() in _whochan_._watch and _whouser != selfn and selfn._nickname.lower() not in server_context.operator_entries:
            _whochan = "*"

    if chan != "":
        if _whochan_.MODE_auditorium and isOp(
                selfn._nickname, _whochan_.channelname) == False and isOp(
            _whouser._nickname, _whochan_.channelname) == False and _whouser != selfn:
            return ""
        if _whouser._nickname.lower() in _whochan_._watch and _whouser != selfn and selfn._nickname.lower() not in server_context.operator_entries:
            return ""

    whostring = "%s %s %s %s %s %s%s%s :0 %s" % (
        _whochan, _whouser._username, _whouser._hostmask, server_context.configuration.server_name, _whouser._nickname,
        _whoaway,
        _isoper, _whomode,
        _whouser._fullname)

    return whostring


def SendComChan(_channels, _self, _cid, _send, param):
    sendto = []
    sendto.extend((_self, _cid))

    # non IRCX clients don't understand KILL
    nonIRCXsend = ":%s!%s@%s QUIT :Killed by %s (%s)\r\n" % (_cid._nickname,
                                                             _cid._username, _cid._hostmask, _self._nickname, param)

    if _self._IRCX:
        _self.send(_send)
    else:
        _self.send(nonIRCXsend)

    if _cid != _self:
        if _cid._IRCX:
            _cid.send(_send)
        else:
            _cid.send(nonIRCXsend)

    for each in copy(_channels):  # for each in selfs comchannels
        chan = server_context.channel_entries[each.lower()]
        for n in chan._users:
            if n in server_context.nickname_to_client_mapping_entries:
                nick = server_context.nickname_to_client_mapping_entries[n.lower()]
                if nick not in sendto:
                    if _cid._nickname.lower() not in chan._watch:
                        if chan.MODE_auditorium == False or isOp(n, chan.channelname):
                            sendto.append(nick)
                            if nick._IRCX:
                                nick.send(_send)
                            else:
                                nick.send(nonIRCXsend)

    del sendto


def compilemodestr(modes, chan=False):
    retval = ""
    il = 0
    while il < len(modes):
        if il == 32:
            return retval
        if chan:
            if modes[il] == "+" or modes[il] == "-" or modes[il] == "q" or modes[il] == "o" or modes[il] == "v" or \
                    modes[il] == "k" or modes[il] == "l" or modes[il] == "b":
                retval = retval + modes[il]

        elif modes[il] == "+" or modes[il] == "-":
            retval = retval + modes[il]
            il += 1

        if modes[il] not in retval:
            retval = retval + modes[il]

        il += 1

    return retval


def CheckServerAccess(nickid=False):
    for each in list(server_context.server_access_entries):
        if int(GetEpochTime()) >= each._expires:
            if each._deleteafterexpire:
                server_context.server_access_entries.remove(each)
                WriteUsers(False, False, True)


Noop = False

_lastError = []


def getGlobalChannels():
    for each in server_context.channel_entries:
        yield server_context.channel_entries[each]


def getUserOBJ(nick):
    if nick.lower() in server_context.nickname_to_client_mapping_entries:
        return server_context.nickname_to_client_mapping_entries[nick.lower()]

    return None


def getOperOBJ(nick):
    if nick.lower() in server_context.operator_entries:
        return server_context.operator_entries[nick.lower()]

    return None


def getChannelOBJ(chan):
    schannels = copy(server_context.channel_entries)
    if chan.lower() in schannels:
        return schannels[chan.lower()]

    return None


def sendWatchOpers(details):
    for each in server_context.operator_entries:
        opid = server_context.operator_entries[each.lower()]
        if opid.watchserver:
            scid = server_context.nickname_to_client_mapping_entries[each.lower()]
            scid.send(":%s NOTICE %s :*** %s" % (server_context.configuration.server_name, scid._nickname, details))


def sendNickservOpers(details):
    for each in server_context.operator_entries:
        opid = server_context.operator_entries[each.lower()]
        if opid.watchnickserv:
            scid = server_context.nickname_to_client_mapping_entries[each.lower()]
            scid.send(":%s NOTICE %s :*** %s" % (server_context.configuration.server_name, scid._nickname, details))


def sendAdminOpers(details):
    for each in server_context.operator_entries:
        opid = server_context.operator_entries[each.lower()]
        if opid.operator_level >= 3:
            scid = server_context.nickname_to_client_mapping_entries[each.lower()]
            scid.send(details)


class ClientConnecting(threading.Thread, User):  # TODO remove this multiple inheritance nonsense

    def __init__(self, client, details, port):

        User.__init__(self, server_context.configuration)

        self._server = server_context.configuration.server_name
        self.die = False
        self.client = client
        self.details = details
        self.flooding = 0
        self.lastcommand = int(GetEpochTime())
        self.pmflooding = 0
        self.pmlastcommand = int(GetEpochTime())
        self.port = port
        self.quittype = 0
        self._pingr = int(GetEpochTime() + 30)
        self._rping = 1
        self._ptries = 0
        self._server = server_context.configuration.server_name
        self._signontime = int(GetEpochTime())
        self._welcome = False
        self._idletime = int(GetEpochTime())

        self.logger = logging.getLogger('CLIENT_THREAD')

        threading.Thread.__init__(self)

    def close(self):
        try:
            self.client.shutdown(1)
        except:
            pass

    def send(self, data):
        try:
            r, w, e = select([], [self.client], [], 1)
            if w:
                self.client.sendall(data.encode(character_encoding))
        except:
            pass

    def _reportError(self, tuError):
        for each in _lastError:
            if str([each[2][0]]) == str(extract_tb(tuError[2])):
                self.send(
                    ":%s NOTICE LINK :*** Bug found and has already been reported, if this becomes a problem, please alert an administrator\r\n" % (
                        server_context.configuration.server_name))
                return

        _lastError.append(
            [tuError,
             [time.strftime("%a %b %d %H:%M:%S %Y GMT", time.localtime()),
              self._nickname + "!" + self._username + "@" + self._hostmask + "/" + self.details[0]],
             extract_tb(tuError[2])])
        self.send(
            ":%s NOTICE LINK :*** Bug found, please report the following:\r\n:%s NOTICE LINK :*** %s\r\n:%s NOTICE LINK :*** %s\r\n:%s NOTICE LINK *** End of bug report\r\n" %
            (server_context.configuration.server_name, server_context.configuration.server_name, str(tuError[0]),
             server_context.configuration.server_name,
             str(tuError[1]), server_context.configuration.server_name))

    def selfaccess(self, cclientid):
        access_helper.CheckSelfExpiry(cclientid)
        if self._nickname.lower() in server_context.operator_entries:
            return True  # can't ignore opers!!!
        for each in cclientid._access:
            if each._level == "DENY":
                ret = access_helper.MatchAccess(each._mask, self)
                if ret == 1:
                    for findgrant in cclientid._access:
                        if findgrant._level == "GRANT":
                            gret = access_helper.MatchAccess(findgrant._mask, self)
                            if gret == 1:
                                return True
                    return False
        return True

    def _validate(self, text):
        check = re.compile("^[a-z0-9A-Z\_\-\^\|\`\'\[\]\\\~\{\}\x7F-\xFF]{1,32}$")
        if check.match(text) is None:
            return False
        else:
            return True

    def _validatefullname(self, text):
        check = re.compile("^[\x01-\xFF]{1,256}$")
        if check.match(text) == None:
            return False
        else:
            return True

    def _sendlusers(self):
        raw_messages.raw(self, "251", self._nickname)
        raw_messages.raw(self, "252", self._nickname)
        raw_messages.raw(self, "253", self._nickname)
        raw_messages.raw(self, "254", self._nickname)
        raw_messages.raw(self, "255", self._nickname)
        raw_messages.raw(self, "265", self._nickname)
        raw_messages.raw(self, "266", self._nickname)

    def _sendmotd(self, filename):

        try:
            myfile = open(filename, 'r')
        except:
            raw_messages.raw(self, "422", self._nickname)
            return False

        raw_messages.raw(self, "375", self._nickname)

        for lineStr in myfile.readlines():
            raw_messages.raw(self, "372", self._nickname, lineStr)
            lineStr = myfile.readline()

        raw_messages.raw(self, "376", self._nickname)

        myfile.close()

        return True

    def _sendwelcome(self):
        CheckServerAccess()
        grantfound = False
        for each in server_context.server_access_entries:
            if each._level == "DENY":
                ret = access_helper.MatchAccess(each._mask, self)
                if ret == 1:
                    for findgrant in server_context.server_access_entries:
                        if findgrant._level == "GRANT":
                            gret = access_helper.MatchAccess(findgrant._mask, self)
                            if gret == 1:
                                grantfound = True
                                break

                    if grantfound:
                        break

                    raw_messages.raw(self, "465", self._nickname, each._reason[1:])
                    self.die = True
                    self.quittype = 9
                    self.close()
                    return 1

        sendWatchOpers(
            "Notice -- User Connecting on port %s (%s!%s@%s) [%s] \r\n" %
            (self.port, self._nickname, self._username, self._hostmask, self.details[0]))

        server_context.unknown_connection_entries.remove(self)

        raw_messages.raw(self, "001", self._nickname, NetworkName)
        raw_messages.raw(self, "002", self._nickname, server_context.configuration.server_name)
        raw_messages.raw(self, "003", self._nickname)
        raw_messages.raw(self, "004", self._nickname, NetworkName)
        raw_messages.raw(self, "005", self._nickname, NetworkName)
        self._sendlusers()
        self._sendmotd(f"./{server_context.configuration.motd_config_file}")

        if self._MODE_register:
            self._MODE_register = False
            self._MODE_.replace("r", "")
            self.send(":%s!%s@%s MODE %s -r\r\n" % ("NickServ", "NickServ", NetworkName, self._nickname))
            if self._username[0] != PrefixChar and self._nickname.lower() not in server_context.operator_entries:
                self._username = PrefixChar + self._username

        is_groupednick = False

        for groupnicks in list(server_context.nickserv_entries.values()):
            if self._nickname.lower() in groupnicks.grouped_nicknames:
                is_groupednick = True
                break

        if self._nickname.lower() in server_context.nickserv_entries and self._nickname.lower() in server_context.nickname_to_client_mapping_entries and self._nosendnickserv == False:
            self.send(
                ":%s!%s@%s NOTICE %s :That nickname is owned by somebody else\r\n:%s!%s@%s NOTICE %s :If this is your nickname, you can identify with \x02/nickserv IDENTIFY \x1Fpassword\x1F\x02\r\n" %
                ("NickServ", "NickServ", NetworkName, self._nickname, "NickServ", "NickServ", NetworkName,
                 self._nickname))
            is_groupednick = False

        if is_groupednick:
            self.send(
                ":%s!%s@%s NOTICE %s :That nickname is owned by somebody else\r\n:%s!%s@%s NOTICE %s :If this is your nickname, you can identify with \x02/nickserv IDENTIFY \x1Fpassword\x1F\x02\r\n" %
                ("NickServ", "NickServ", NetworkName, self._nickname, "NickServ", "NickServ", NetworkName,
                 self._nickname))

        # UserDefaultModes

        Mode_function(self, ["MODE", self._nickname, UserDefaultModes])

    def _logoncheck(self):
        if self._username != "" and self._nickname != "" and self._welcome == False and self._nickname.lower() not in server_context.nickname_to_client_mapping_entries:
            if ServerPassword != "" and self._password == False:
                return False

            self._welcome = True
            server_context.nickname_to_client_mapping_entries[
                self._nickname.lower()] = self  # update entry from dictionary

            if self._nickname.lower() in nickmute:
                del nickmute[self._nickname.lower()]  # log on affirmed, now nicknames can take over

            return True
        else:
            return False

    def _isDisabled(self, command):
        if command in disabled_functionality:

            val = disabled_functionality[command]
            operlevel = 0
            if self._nickname.lower() in server_context.operator_entries:
                opid = server_context.operator_entries[self._nickname.lower()]
                operlevel = opid.operator_level

            if val == 0:
                return -1

            if operlevel == 0:
                raw_messages.raw(self, "481", self._nickname, "Permission Denied - You're not a System Operator")
                return -2

            if val == 1:
                if operlevel < 1:
                    raw_messages.raw(self, "481", self._nickname, "Permission Denied - You're not a System Operator")
                    return -2
                else:
                    return 1

            if val == 2:
                if operlevel < 2:
                    raw_messages.raw(self, "481", self._nickname,
                                     "Permission Denied - You're not a System Chat Manager")
                    return -2
                else:
                    return 1

            if val == 3:
                if operlevel < 3:
                    raw_messages.raw(self, "481", self._nickname,
                                     "Permission Denied - You're not a Server Administrator")
                    return -2
                else:
                    return 1

            if val == 4:
                if operlevel < 3:
                    raw_messages.raw(self, "481", self._nickname,
                                     "Permission Denied - You're not the Network Administrator")
                    return -2
                else:
                    return 1

        else:
            return 1

    def run(self):
        server_context.unknown_connection_entries.add(self)
        connections.append(self)
        self.logger.debug(f"Connection accepted from '{self.details[0]}' users [{len(connections)}/{MaxUsers}]")

        if str(len(connections) - 1) == str(MaxUsers):
            self.logger.debug("Connection closed '", self.details[0], "', server is full")
            self.send(
                ":" + server_context.configuration.server_name + " NOTICE AUTH :*** Sorry, this server is full, you can try reconnecting\r\n")
            self.send("ERROR :Closing Link: %s (Server is full)\r\n" % (self.details[0]))
            self.close()
            connections.remove(self)
            server_context.unknown_connection_entries.remove(self)
        else:
            calcuseramount = -1
            for v in connections:
                if v.details[0] == self.details[0]:
                    calcuseramount += 1

            userdetails = self.details[0]  # store their ip in here

            if self.details[0] == "127.0.0.1":
                userdetails = ipaddress

            exemptFromConnectionKiller = False

            try:
                for each in globals()["connectionsExempt"]:
                    if each == "":
                        continue

                    chk = re.compile("^" + each + "$")
                    if chk.match(userdetails) != None:
                        exemptFromConnectionKiller = True
                        break
            except:
                print(sys.exc_info())

            if str(MaxUsersPerConnection) == str(
                    calcuseramount) and ipaddress != userdetails and exemptFromConnectionKiller == False:
                server_context.unknown_connection_entries.remove(self)
                self.logger.debug("Connection closed '", self.details[0], "', too many connections")

                self.send(
                    ":" + server_context.configuration.server_name + " NOTICE AUTH :*** Sorry, your client is restricted to %d clones\r\n" %
                    (MaxUsersPerConnection))

                self.send("ERROR :Closing Link: %s (Too many connections)\r\n" % (self.details[0]))

                self.quittype = 10
            else:
                if str(HostMasking) == "2":
                    self.send(
                        ":" + server_context.configuration.server_name + " NOTICE AUTH :*** Looking up your hostname...\r\n")

                    try:
                        self._hostname = socket.gethostbyaddr(self.details[0])[0]
                        if socket.gethostbyname(self._hostname) == self.details[0]:
                            self.send(
                                ":" + server_context.configuration.server_name + " NOTICE AUTH :*** Found your hostname\r\n")
                        else:
                            raise Exception
                    except:
                        self._hostname = self.details[0]
                        self.send(
                            ":" + server_context.configuration.server_name +
                            " NOTICE AUTH :*** Could not find your hostname, using your IP instead\r\n")

                if str(HostMasking) == "0":
                    self._hostmask = self.details[0]
                elif str(HostMasking) == "1":
                    self._hostmask = self.details[0].split(".")[0] + "." + self.details[0].split(".")[1] + ".XXX.XXX"

                elif str(HostMasking) == "2":
                    shortmask = sha256((self.details[0] + HostMaskingParam).encode('utf-8')).hexdigest().upper()[:5]

                    if self._hostname == self.details[0]:  # 127.0.0.1 - 127.0.A4EFF
                        maskstart = self._hostname.split(".", 2)[2]
                        self._hostmask = maskstart + "." + shortmask
                    else:
                        maskstart = self._hostname.split(".", 1)[0]
                        try:
                            self._hostmask = shortmask + "." + self._hostname.split(".", 1)[1]
                        except:
                            self._hostmask = shortmask

                elif str(HostMasking) == "3":
                    self._hostmask = HostMaskingParam

                elif str(HostMasking) == "4":
                    self._hostmask = sha256(
                        (self.details[0] + HostMaskingParam).encode('utf-8')).hexdigest().upper()[:16]

                elif str(HostMasking) == "5":
                    self._hostmask = HostMaskingParam

                elif str(HostMasking) == "6":
                    shastring = sha256((self.details[0] + HostMaskingParam).encode('utf-8')).hexdigest().upper()
                    self._hostmask = self.details[0].split(
                        ".")[0] + "." + self.details[0].split(".")[1] + "." + shastring[0:3] + "." + shastring[3:6]
                else:
                    self._hostmask = self.details[0]

                if ServerPassword != "":
                    self.send(":" + server_context.configuration.server_name + " NOTICE AUTH :*** " + passmsg + "\r\n")

                self.send("PING :" + server_context.configuration.server_name + "\r\n")
                self.client.setblocking(0)

                while True:
                    # read line code
                    c = ""
                    strdata = ""
                    closesock = False
                    while True:
                        if int(GetEpochTime()) >= self._pingr:
                            if self._rping != 0 and self._welcome:
                                self.send("PING :" + server_context.configuration.server_name + "\r\n")
                                self._rping -= 1
                                self._pingr = (int(GetEpochTime()) + 10)
                            else:
                                if self._welcome:
                                    self.send("ERROR :Closing Link: " + self.details[0] + " (Ping timeout)\r\n")
                                else:
                                    self.send("ERROR :Closing Link: " + self.details[0] + " (Log on failed)\r\n")
                                self.quittype = 3
                                self.die = True

                        r, w, e = select([self.client], [], [self.client], 1)
                        try:
                            if r:
                                c = self.client.recv(1)
                                if not c:
                                    self.quittype = 1
                                    closesock = True
                                    break

                                elif c == "\n".encode() or c == "\r".encode():
                                    c = ""
                                    # read a line
                                    break

                                else:
                                    strdata += c.decode(character_encoding)

                        except socket.error as xxx_todo_changeme:
                            (value, message) = xxx_todo_changeme.args
                            if errno.ECONNABORTED == value or errno.ECONNRESET == value:
                                self.quittype = 0
                                closesock = True
                                break

                        if e:
                            self.quittype = 0
                            closesock = True

                        if self.die:
                            closesock = True
                            break

                    # end of readline code

                    if closesock:
                        break

                    if len(strdata) >= 480:
                        strdata = ""
                        raw_messages.raw(self, "263", self._nickname)

                    try:
                        if strdata == "":
                            param = [""]
                        else:
                            while strdata[0] == " ":
                                strdata = strdata[1:]
                            param = strdata.replace("\r", "").replace("\n", "").split(" ")  # don't need both
                            while "" in param:
                                param.remove("")
                            if param != []:
                                param[0] = param[0].upper()
                            else:
                                param = [""]
                    except IndexError:
                        pass

                    except:
                        self.logger.debug(traceback.format_exc())

                    self.logger.debug(f"[{','.join(param)}]")
                    
                    _sleep = "%.4f" % (random() / 9)
                    _disabled = self._isDisabled(param[0])
                    if param[0].upper() != "NOTICE" and param[0].upper() != "PRIVMSG" and param[0].upper() != "JOIN" and \
                            param[0] != "":

                        if param[0] == "MODE" and len(param) == 2:
                            pass
                        else:

                            if param[0] not in server_context.configuration.flooding_exempt_commands:

                                # if current time - time the last command was sent = 0, meaning data is being sent far too fast, add,
                                if int((
                                               int(GetEpochTime()) - self.lastcommand) * 1000) <= 500:  # let's work in ms shall we?
                                    self.flooding += 1
                                else:
                                    self.flooding = 0

                                self.lastcommand = int(GetEpochTime())

                                if self.flooding == 20:  # 15 commands per 1000 miliseconds, anymore than that will kill the user
                                    self.quittype = 4
                                    self.send("ERROR :Closing Link: " + self.details[0] + " (Input flooding)\r\n")
                                    self.die = True
                                    self.close()

                        # end flooding protection

                        # time.sleep(0.04)

                    try:

                        if param[0] == "":
                            pass

                        elif _disabled < 1:
                            if _disabled == -1:
                                raw_messages.raw(self, "446", self._nickname, param[0])

                        elif param[0] == "AUTH":
                            try:
                                raw_messages.raw(self, "912", self._nickname, param[1])
                            except:
                                raw_messages.raw(self, "461", self._nickname, param[0])

                        elif param[0] == "NICK":
                            Nick_function(self, param)

                        elif param[0] == "PASS":  # PASS password
                            if ServerPassword != "":
                                if self._password:
                                    raw_messages.raw(self, "462", self._nickname)
                                else:
                                    if param[1] == ServerPassword:
                                        self._password = True
                                        self.send(
                                            ":" + server_context.configuration.server_name + " NOTICE AUTH :*** Password accepted\r\n")
                                        if self._logoncheck():
                                            self._sendwelcome()
                                    else:
                                        self.send(
                                            ":" + server_context.configuration.server_name + " NOTICE AUTH :*** Invalid password\r\n")
                                        self._ptries += 1
                                        if self._ptries == 3:
                                            if self._nickname != "":
                                                self.send(
                                                    ":" + server_context.configuration.server_name + " KILL " + self._nickname +
                                                    " :Too many invalid passwords\r\n")
                                            else:
                                                self.send(":" + server_context.configuration.server_name +
                                                          " NOTICE AUTH :*** Too many invalid passwords\r\n")
                                            break
                            else:
                                self.send(
                                    ":" + server_context.configuration.server_name + " NOTICE AUTH :*** PASS has been disabled\r\n")

                        elif param[0] == "PONG":
                            if self._rping < 2 and self._welcome:
                                self._rping += 1

                        elif param[0] == "USER":  # USER ident mode[8] unused :fullname
                            if self._username != "":
                                raw_messages.raw(self, "462", self._nickname)
                            else:
                                ustr = self._validate(param[1].replace(".", ""))
                                if ustr == False:
                                    param[1] = self._nickname

                                if self._validate(param[1].replace(".", "")) and param[4]:

                                    if len(strdata.split(":", 1)) == 2:
                                        _fn = strdata.split(":", 1)[1][:256]
                                    else:
                                        _fn = ""
                                    if self._validatefullname(_fn.replace(" ", "")) or _fn == "":
                                        self._fullname = _fn
                                        if str(HostMasking) != "5":
                                            self._username = PrefixChar + param[1].replace(PrefixChar, "")

                                        elif str(HostMasking) == "5":
                                            self._username = PrefixChar + sha256(
                                                (self.details[0] + HostMaskingParam).encode(
                                                    'utf-8')).hexdigest().upper()[:16]

                                        if self._logoncheck():
                                            self._sendwelcome()
                                    else:
                                        raw_messages.raw(self, "434", self._nickname, param[1].replace(':', ''))
                                else:
                                    raw_messages.raw(self, "434", self._nickname, param[1].replace(':', ''))

                        elif param[0] == "QUIT":  # QUIT :die reasons
                            try:
                                msg = param[1]
                                if msg[0] == ":":
                                    msg = strdata.split(" ", 1)[1][1:]
                            except:
                                msg = ":"

                            if msg == ":":
                                self.quittype = 2
                                self.quitmsg = ""
                            else:
                                self.quittype = 2
                                self.quitmsg = msg

                            try:  # sometimes the client exits too fast
                                self.send("ERROR :Closing Link: " + self.details[0] + " (Client Quit)\r\n")
                                break
                                # self.client = None
                            except:
                                pass

                        elif param[0] == "PING":
                            try:
                                ret = param[1]
                                if ret[0] == ":":
                                    ret = strdata.split(" ", 1)[1][1:]
                                self.send(
                                    ":%s PONG %s :%s\r\n" % (
                                    server_context.configuration.server_name, server_context.configuration.server_name,
                                    ret))
                            except:
                                raw_messages.raw(self, "409", self._nickname)

                        elif param[0] == "IRCX":
                            raw_messages.raw(self, "800", self._nickname, "1")
                            self._IRCX = True

                        elif param[0] == "ISIRCX":
                            raw_messages.raw(self, "800", self._nickname, "0")

                        else:
                            if self._welcome:
                                self._pingr = (int(GetEpochTime()) + 100)

                                opid, chanid, copid = None, None, None
                                try:
                                    chanid = getChannelOBJ(param[1].lower())
                                    cid = getUserOBJ(param[1].lower())
                                    if cid:
                                        copid = getOperOBJ(cid._nickname.lower())

                                except IndexError:
                                    pass

                                if self._nickname.lower() in server_context.operator_entries:
                                    opid = server_context.operator_entries[self._nickname.lower()]

                                if param[0] == "NOOPER":  # Any admin can disable the oper command
                                    global Noop, temp_noopers

                                    if len(param) == 2:
                                        if param[1] == AdminPassword:
                                            Noop = False
                                            self.send(
                                                ":" + server_context.configuration.server_name + " NOTICE SERVER :*** Oper is now enabled\r\n")
                                    else:
                                        if opid:
                                            if opid.operator_level >= 3:
                                                if Noop == False:
                                                    Noop = True
                                                    odict = dict(server_context.operator_entries)
                                                    for each in server_context.nickname_to_client_mapping_entries:
                                                        cid = server_context.nickname_to_client_mapping_entries[each]
                                                        if cid._nickname.lower() in odict:
                                                            opid2 = odict[cid._nickname.lower()]
                                                            if opid2.operator_level < opid.operator_level:
                                                                temp_noopers.append(cid)
                                                                cid.send(
                                                                    ":%s MODE %s -%s\r\n" %
                                                                    (server_context.configuration.server_name,
                                                                     cid._nickname,
                                                                     opid2.flags))
                                                                cid.send(
                                                                    ":" + server_context.configuration.server_name +
                                                                    " NOTICE SERVER :*** Your o-line has been disabled temporarily\r\n")
                                                                del server_context.operator_entries[each]

                                                    self.send(
                                                        ":" + server_context.configuration.server_name +
                                                        " NOTICE SERVER :*** All opers have been disabled\r\n")

                                                else:
                                                    Noop = False
                                                    self.send(":" + server_context.configuration.server_name +
                                                              " NOTICE SERVER :*** Oper is now enabled\r\n")
                                                    for each in temp_noopers:
                                                        each.send(
                                                            ":" + server_context.configuration.server_name +
                                                            " NOTICE SERVER :*** Your o-line has been enabled, please re-oper\r\n")

                                                    temp_noopers = []

                                            else:
                                                raw_messages.raw(self, "481", self._nickname,
                                                                 "Permission Denied - You're not the Network Administrator")
                                        else:
                                            raw_messages.raw(self, "481", self._nickname,
                                                             "Permission Denied - You're not a System Operator")

                                elif param[0] == "WATCH":
                                    if opid:
                                        if chanid:
                                            self._watch.append(chanid.channelname)
                                            chanid.join(self._nickname)
                                            self.send(
                                                ":" + server_context.configuration.server_name + " NOTICE WATCH :*** You are now watching " +
                                                chanid.channelname +
                                                ", to join the conversation, part and re-join\r\n")
                                        else:
                                            raw_messages.raw(self, "403", self._nickname, param[1])
                                    else:
                                        raw_messages.raw(self, "481", self._nickname,
                                                         "Permission Denied - You're not a System operator")

                                elif param[0] == "KILL":
                                    msg = param[2]
                                    if msg[0] == ":":
                                        msg = strdata.split(" ", 2)[2][1:]

                                    if opid:
                                        if cid:
                                            if copid:
                                                copid = server_context.operator_entries[cid._nickname.lower()]
                                                if copid.operator_level > opid.operator_level and param[
                                                    1].lower() != self._nickname.lower():
                                                    raw_messages.raw(self, "481", self._nickname,
                                                                     "Permission Denied - You do not have the correct privileges to kill this oper")
                                                else:
                                                    cid.quittype = -1
                                                    cid.quitmsg = " by " + self._nickname
                                                    cid.die = True
                                                    SendComChan(
                                                        cid._channels, self, cid, ":%s!%s@%s KILL %s :%s\r\n" %
                                                                                  (self._nickname, self._username,
                                                                                   self._hostmask, cid._nickname,
                                                                                   msg),
                                                        msg)
                                            else:
                                                cid.quittype = -1
                                                cid.die = True
                                                cid.quitmsg = " by " + self._nickname
                                                SendComChan(
                                                    cid._channels, self, cid, ":%s!%s@%s KILL %s :%s\r\n" %
                                                                              (self._nickname, self._username,
                                                                               self._hostmask, cid._nickname,
                                                                               msg),
                                                    msg)

                                        elif chanid:
                                            if opid.operator_level > 1:
                                                if chanid.MODE_registered or chanid.MODE_servicechan:
                                                    raw_messages.raw(self, "481", self._nickname,
                                                                     "Permission Denied - You cannot kill a registered channel")
                                                else:
                                                    if self._IRCX:
                                                        self.send(":%s!%s@%s KILL %s :%s\r\n" %
                                                                  (self._nickname, self._username, self._hostmask,
                                                                   chanid.channelname, msg))
                                                    else:
                                                        self.send(
                                                            ":" + server_context.configuration.server_name + " NOTICE SERVER :*** Killed channel " +
                                                            chanid.channelname + " (" + strdata.split(" ", 2)[2] +
                                                            ")\r\n")

                                                    for each in chanid._users:
                                                        cid = server_context.nickname_to_client_mapping_entries[each]
                                                        cid._channels.remove(chanid.channelname)
                                                        raw_messages.raw(cid, "934", cid._nickname, chanid.channelname)

                                                    chanid._users = {}
                                                    chanid.resetchannel(True)
                                            else:
                                                raw_messages.raw(self, "481", self._nickname,
                                                                 "Permission Denied - You do not have the correct privileges to kill a channel")

                                        else:
                                            raw_messages.raw(self, "401", self._nickname, param[0])

                                    else:
                                        raw_messages.raw(self, "481", self._nickname,
                                                         "Permission Denied - You're not a System operator")

                                elif param[0] == "DIE":
                                    if opid:
                                        if opid.operator_level == 4:
                                            if param[1] == AdminPassword:
                                                for each in server_context.nickname_to_client_mapping_entries:
                                                    e = server_context.nickname_to_client_mapping_entries[each]
                                                    e.send(
                                                        ":" + server_context.configuration.server_name +
                                                        " NOTICE SERVER :*** This Server has been closed by the Network Administrator\r\n")
                                                    e.client.shutdown(1)

                                                os._exit(1)
                                            else:
                                                raw_messages.raw(self, "908", self._nickname)
                                        else:
                                            raw_messages.raw(self, "481", self._nickname,
                                                             "Permission Denied - You're not the Network Administrator")

                                elif param[0] == "STATS":
                                    if param[1] == "G":
                                        self.send(
                                            ":" + server_context.configuration.server_name + " NOTICE STATS :*** Viewing Online guides '" +
                                            param[1]
                                            [0] + "' \r\n")
                                        foundguide = False
                                        for each in server_context.nickname_to_client_mapping_entries:
                                            nickid = server_context.nickname_to_client_mapping_entries[each.lower()]
                                            if nickid._nickname.lower() in server_context.operator_entries:
                                                opid = server_context.operator_entries[nickid._nickname.lower()]
                                                if opid.guide:
                                                    foundguide = True
                                                    self.send(
                                                        ":" + server_context.configuration.server_name + " NOTICE STATS :*** " + nickid._nickname +
                                                        " is available for help\r\n")

                                        if foundguide == False:
                                            self.send(
                                                ":" + server_context.configuration.server_name +
                                                " NOTICE STATS :*** Sorry, there are no guides available\r\n")

                                    elif opid:
                                        if opid.operator_level < 3:
                                            raw_messages.raw(self, "481", self._nickname,
                                                             "Permission Denied - You're not a Server Administrator")
                                        else:
                                            if param[1] == "U":
                                                self.send(
                                                    ":" + server_context.configuration.server_name + " NOTICE STATS :*** Viewing User statistics '" +
                                                    param[1][0] + "' \r\n")
                                                self.send(
                                                    ":" + server_context.configuration.server_name + " 212 " + self._nickname + " :Max Users: " + MaxUsers + "\r\n")
                                                self.send(
                                                    ":" + server_context.configuration.server_name + " 212 " + self._nickname +
                                                    " :Max Users per connection: " + str(
                                                        MaxUsersPerConnection) + "\r\n")

                                            elif param[1] == "E":
                                                self.send(
                                                    ":" + server_context.configuration.server_name + " NOTICE STATS :*** Viewing Error statistics '" +
                                                    param[1][0] + "' \r\n")
                                                for every in _lastError:
                                                    self.send(
                                                        ":" + server_context.configuration.server_name + " NOTICE STATS :*** Error found on " +
                                                        every[1][0] + " by " + every[1][1] + "\r\n")
                                                    self.send(
                                                        ":" + server_context.configuration.server_name + " 212 " + self._nickname + " :%s\r\n" %
                                                        (str(every[2][0])))
                                                    self.send(
                                                        ":" + server_context.configuration.server_name + " 212 " + self._nickname + " :%s\r\n" %
                                                        (str(every[0][0])))
                                                    self.send(
                                                        ":" + server_context.configuration.server_name + " 212 " + self._nickname + " :%s\r\n" %
                                                        (str(every[0][1])))

                                                self.send(
                                                    ":" + server_context.configuration.server_name + " NOTICE STATS :*** Finished listing\r\n")

                                            elif param[1] == "O":
                                                self.send(":" + server_context.configuration.server_name +
                                                          " NOTICE STATS :*** Viewing Operator statistics '" +
                                                          param[1][0] + "' \r\n")
                                                for oline in operlines:
                                                    if "A" in oline.flags:
                                                        self.send(
                                                            ":" + server_context.configuration.server_name + " 212 " + self._nickname + " :[A] - " + oline.username + " - " + oline.flags + " (Network Administrator)\r\n")
                                                    elif "O" in oline.flags:
                                                        self.send(
                                                            ":" + server_context.configuration.server_name + " 212 " + self._nickname + " :[O] - " + oline.username + " - " + oline.flags + " (Server Administrator)\r\n")
                                                    elif "a" in oline.flags:
                                                        self.send(
                                                            ":" + server_context.configuration.server_name + " 212 " + self._nickname + " :[a] - " + oline.username + " - " + oline.flags + " (System Chat Manager)\r\n")
                                                    elif "o" in oline.flags:
                                                        self.send(
                                                            ":" + server_context.configuration.server_name + " 212 " + self._nickname + " :[o] - " + oline.username + " - " + oline.flags + " (System Operator)\r\n")

                                            elif param[1] == "P":
                                                self.send(
                                                    ":" + server_context.configuration.server_name + " NOTICE STATS :*** Viewing Port statistics '" +
                                                    param[1][0] + "' \r\n")
                                                for each in list(currentports.keys()):
                                                    self.send(
                                                        ":" + server_context.configuration.server_name + " 212 " + self._nickname +
                                                        " :Running server on: " + each + "\r\n")

                                            elif param[1] == "F":
                                                self.send(":" + server_context.configuration.server_name +
                                                          " NOTICE STATS :*** Viewing Filter statistics '" +
                                                          param[1][0] + "' \r\n")

                                                # TODO violating encapsulation of filters still
                                                for f in filtering._filters:
                                                    if f.filter_type == "chan":
                                                        self.send(
                                                            ":" + server_context.configuration.server_name + " 212 " + self._nickname +
                                                            " :Channel filter - '" + f.filter_string + "' - Level "
                                                            + f.override + " overrides\r\n")
                                                    elif f.level == "nick":
                                                        self.send(
                                                            ":" + server_context.configuration.server_name + " 212 " + self._nickname +
                                                            " :Nickname filter - '" + f.filter_string +
                                                            "' - Level " + f.override + " overrides\r\n")
                                                    elif f.filter_type == "profanity":
                                                        self.send(
                                                            ":" + server_context.configuration.server_name + " 212 " + self._nickname +
                                                            " :Profanity filter - '" + f.filter_string +
                                                            "' - Level " + f.override + " overrides\r\n")

                                            elif param[1] == "D":
                                                self.send(":" + server_context.configuration.server_name +
                                                          " NOTICE STATS :*** Viewing Disabled statistics '" +
                                                          param[1][0] + "' \r\n")
                                                for each in disabled_functionality:
                                                    self.send(
                                                        ":" + server_context.configuration.server_name + " 212 " + self._nickname + " :" + each + "\r\n")

                                            elif param[1] == "C":
                                                self.send(":" + server_context.configuration.server_name +
                                                          " NOTICE STATS :*** Viewing Channel statistics '" +
                                                          param[1][0] + "' \r\n")
                                                self.send(
                                                    ":" + server_context.configuration.server_name + " 212 " + self._nickname +
                                                    " :Creation Modes: " + DefaultModes + "\r\n")
                                                self.send(
                                                    ":" + server_context.configuration.server_name + " 212 " + self._nickname + " :Max Channels: " +
                                                    server_context.configuration.max_channels + "\r\n")
                                                self.send(
                                                    ":" + server_context.configuration.server_name + " 212 " + self._nickname +
                                                    " :Max Channels per User: " + server_context.configuration.max_channels_per_user + "\r\n")

                                            else:
                                                self.send(":" + server_context.configuration.server_name +
                                                          " NOTICE STATS :*** No statistics available for '" +
                                                          param[1][0] + "' \r\n")

                                    else:
                                        raw_messages.raw(self, "481", self._nickname,
                                                         "Permission Denied - You're not a System operator")

                                elif param[0] == "REHASH":
                                    if opid:
                                        if opid.operator_level >= 4:
                                            sendAdminOpers(
                                                ":" + server_context.configuration.server_name + " NOTICE CONFIG :*** " + self._nickname +
                                                " is rehashing the server config file\r\n")
                                            rehash()
                                            sendAdminOpers(":" + server_context.configuration.server_name +
                                                           " NOTICE CONFIG :*** The server has been rehashed\r\n")

                                        else:
                                            raw_messages.raw(self, "481", self._nickname,
                                                             "Permission Denied - You're not the Network Administrator")
                                    else:
                                        raw_messages.raw(self, "481", self._nickname,
                                                         "Permission Denied - You're not a System Operator")

                                elif param[0] == "GAG":
                                    if opid:
                                        if cid:
                                            if cid in server_context.operator_entries:
                                                raw_messages.raw(self, "481", self._nickname,
                                                                 "Permission Denied - Can't /GAG another Operator")
                                            else:
                                                cid.send(
                                                    ":%s MODE %s +z\r\n" % (
                                                    server_context.configuration.server_name, cid._nickname))
                                                self.send(
                                                    ":" + server_context.configuration.server_name + " NOTICE GAG :*** " + self._nickname +
                                                    " Added " + cid._nickname + " to the GAG list\r\n")
                                                cid._MODE_gag = True
                                                if "z" not in cid._MODE_:
                                                    cid._MODE_ = cid._MODE_ + "z"
                                        else:
                                            raw_messages.raw(self, "401", self._nickname, param[1])
                                    else:
                                        raw_messages.raw(self, "481", self._nickname,
                                                         "Permission Denied - You're not a System operator")

                                elif param[0] == "UNGAG":
                                    if opid:
                                        if cid:
                                            if cid in server_context.operator_entries:
                                                raw_messages.raw(self, "481", self._nickname,
                                                                 "Permission Denied - Can't use /UNGAG with another Operator")
                                            else:
                                                cid.send(
                                                    ":%s MODE %s -z\r\n" % (
                                                    server_context.configuration.server_name, cid._nickname))
                                                self.send(
                                                    ":" + server_context.configuration.server_name + " NOTICE GAG :*** " + self._nickname +
                                                    " Removed " + cid._nickname + " from the GAG list\r\n")
                                                cid._MODE_gag = False
                                                cid._MODE_.replace("z", "")
                                        else:
                                            raw_messages.raw(self, "401", self._nickname, param[1])
                                    else:
                                        raw_messages.raw(self, "481", self._nickname,
                                                         "Permission Denied - You're not a System operator")

                                elif param[0] == "GLOBAL":
                                    if opid:
                                        if opid.operator_level > 2:
                                            for each in server_context.nickname_to_client_mapping_entries:
                                                cid = server_context.nickname_to_client_mapping_entries[each.lower()]
                                                cid.send(
                                                    ":" + NetworkName + " NOTICE " + cid._nickname + " :" +
                                                    strdata.split(" ", 1)[1] + "\r\n")

                                        else:
                                            raw_messages.raw(self, "481", self._nickname,
                                                             "Permission Denied - You're not an Administrator")
                                    else:
                                        raw_messages.raw(self, "481", self._nickname,
                                                         "Permission Denied - You're not a System operator")

                                elif param[0] == "SNAME":
                                    if opid:
                                        if cid:
                                            if copid and copid != opid:
                                                self.send(
                                                    ":" + server_context.configuration.server_name +
                                                    " NOTICE SERVER :*** Cannot use /SNAME on another operator\r\n")
                                            else:
                                                if len(param) == 2:
                                                    cid._friendlyname = ""
                                                    self.send(
                                                        ":" + server_context.configuration.server_name +
                                                        " NOTICE SERVER :*** Removed friendly name of " + cid._nickname +
                                                        "\r\n")
                                                    cid.send(":%s MODE %s -X\r\n" % (
                                                        server_context.configuration.server_name, cid._nickname))
                                                    cid._MODE_ = cid._MODE_.replace("X", "")
                                                else:
                                                    cid._friendlyname = " ".join(param).split(" ", 2)[2]
                                                    cid.send(":%s MODE %s +X\r\n" % (
                                                        server_context.configuration.server_name, cid._nickname))
                                                    self.send(
                                                        ":" + server_context.configuration.server_name + " NOTICE SERVER :*** Changed the friendly name of " + cid._nickname + " to '" + cid._friendlyname + "'\r\n")
                                                    if "X" not in cid._MODE_:
                                                        cid._MODE_ = cid._MODE_ + "X"

                                        else:
                                            raw_messages.raw(self, "401", self._nickname, param[1])
                                    else:
                                        raw_messages.raw(self, "481", self._nickname,
                                                         "Permission Denied - You're not a System operator")

                                elif param[0] == "CHGIDENT":
                                    if opid:
                                        if opid.operator_level > 2:
                                            if cid:
                                                if self._validate(param[2]):
                                                    if copid:
                                                        self.send(
                                                            ":" + server_context.configuration.server_name +
                                                            " NOTICE SERVER :*** Cannot use /CHGIDENT on another operator\r\n")
                                                    else:
                                                        cid._username = param[2]
                                                        self.send(
                                                            ":" + server_context.configuration.server_name + " NOTICE SERVER :*** Changed the username of " + cid._nickname + " to '" +
                                                            param[2] + "'\r\n")
                                                else:
                                                    raw_messages.raw(self, "434", self._nickname, param[1])
                                            else:
                                                raw_messages.raw(self, "401", self._nickname, param[1])
                                        else:
                                            raw_messages.raw(self, "481", self._nickname,
                                                             "Permission Denied - You're not an Administrator")
                                    else:
                                        raw_messages.raw(self, "481", self._nickname,
                                                         "Permission Denied - You're not a System operator")

                                elif param[0] == "CHGHOST":
                                    if opid:
                                        if opid.operator_level > 2:
                                            if cid:
                                                if self._validate(param[2].replace(".", "a").replace("/", "a")):
                                                    if copid:
                                                        self.send(
                                                            ":" + server_context.configuration.server_name +
                                                            " NOTICE SERVER :*** Cannot use /CHGHOST on another operator\r\n")
                                                    else:
                                                        cid._hostmask = param[2]
                                                        self.send(
                                                            ":" + server_context.configuration.server_name + " NOTICE SERVER :*** Changed the hostmask of " + cid._nickname + " to '" +
                                                            param[2] + "'\r\n")
                                                else:
                                                    self.send(
                                                        ":" + server_context.configuration.server_name +
                                                        " NOTICE SERVER :Invalid hosname, hostname must contain only letters and numbers\r\n")
                                            else:
                                                raw_messages.raw(self, "401", self._nickname, param[1])
                                        else:
                                            raw_messages.raw(self, "481", self._nickname,
                                                             "Permission Denied - You're not an Administrator")
                                    else:
                                        raw_messages.raw(self, "481", self._nickname,
                                                         "Permission Denied - You're not a System operator")

                                elif param[0] == "CHGNAME":
                                    if opid:
                                        if opid.operator_level > 2:
                                            if cid:
                                                if self._validatefullname(strdata.split(" ", 2)[2].replace(".", "a")):
                                                    if copid:
                                                        self.send(
                                                            ":" + server_context.configuration.server_name +
                                                            " NOTICE SERVER :*** Cannot use /CHGNAME on another operator\r\n")
                                                    else:
                                                        cid._fullname = strdata.split(" ", 2)[2].replace("  ", "")
                                                        self.send(
                                                            ":" + server_context.configuration.server_name + " NOTICE SERVER :*** Changed the fullname of " + cid._nickname + " to '" +
                                                            strdata.split(" ", 2)[2] + "'\r\n")
                                                else:
                                                    self.send(
                                                        ":" + server_context.configuration.server_name + " NOTICE SERVER :Invalid fullname\r\n")
                                            else:
                                                raw_messages.raw(self, "401", self._nickname, param[1])
                                        else:
                                            raw_messages.raw(self, "481", self._nickname,
                                                             "Permission Denied - You're not an Administrator")
                                    else:
                                        raw_messages.raw(self, "481", self._nickname,
                                                         "Permission Denied - You're not a System operator")

                                elif param[0] == "SETIDENT":
                                    if opid:
                                        if self._validate(param[1]):
                                            self._username = param[1]

                                            self.send(
                                                ":" + server_context.configuration.server_name + " NOTICE SERVER :*** Your username is now '" +
                                                param[1] + "'\r\n")
                                        else:
                                            raw_messages.raw(self, "434", self._nickname, param[1])
                                    else:
                                        raw_messages.raw(self, "481", self._nickname,
                                                         "Permission Denied - You're not a System operator")

                                elif param[0] == "SETHOST":
                                    if opid:
                                        if self._validate(param[1].replace(".", "a").replace("/", "a")):
                                            self._hostmask = param[1]

                                            self.send(
                                                ":" + server_context.configuration.server_name + " NOTICE SERVER :*** Your hostname is now '" +
                                                param[1] + "'\r\n")
                                        else:
                                            self.send(
                                                ":" + server_context.configuration.server_name +
                                                " NOTICE SERVER :Invalid hosname, hostname must contain only letters and numbers\r\n")
                                    else:
                                        raw_messages.raw(self, "481", self._nickname,
                                                         "Permission Denied - You're not a System operator")

                                elif param[0] == "SETNAME":
                                    if opid:
                                        if self._validatefullname(
                                                strdata.split(" ", 1)[1].replace(" ", "a").replace("/", "a")):
                                            self._fullname = strdata.split(" ", 1)[1].replace("  ", "")
                                            self.send(
                                                ":" + server_context.configuration.server_name + " NOTICE SERVER :*** Your fullname is now '" +
                                                strdata.split(" ", 1)[1] + "'\r\n")

                                        else:
                                            self.send(
                                                ":" + server_context.configuration.server_name +
                                                " NOTICE SERVER :Invalid fullname, please choose a more suitable fullname\r\n")
                                    else:
                                        raw_messages.raw(self, "481", self._nickname,
                                                         "Permission Denied - You're not a System operator")

                                elif param[0] == "MODE":
                                    Mode_function(self, param, strdata)

                                elif param[0] == "TOPIC":
                                    if len(param) == 2:
                                        if chanid:
                                            if isSecret(chanid,
                                                        "private") and self._nickname.lower() not in server_context.operator_entries and self._nickname.lower() not in chanid._users:
                                                raw_messages.raw(self, "331", self._nickname, chanid.channelname)
                                            else:
                                                if chanid._topic != "":
                                                    raw_messages.raw(self, "332", self._nickname, chanid.channelname,
                                                                     chanid._topic)
                                                    raw_messages.raw(self, "333", self._nickname, chanid.channelname,
                                                                     chanid._topic_nick, chanid._topic_time)

                                                else:
                                                    raw_messages.raw(self, "331", self._nickname, chanid.channelname)
                                        else:
                                            raw_messages.raw(self, "403", self._nickname, param[1])

                                    else:
                                        if chanid:
                                            if self._nickname.lower() in chanid._users:
                                                dotopic = False

                                                if chanid.MODE_optopic == False or self._nickname.lower() in chanid._op or self._nickname.lower() in chanid._owner:
                                                    dotopic = True

                                                if dotopic:
                                                    if chanid.MODE_ownertopic and self._nickname.lower() not in chanid._owner:
                                                        raw_messages.raw(self, "485", self._nickname,
                                                                         chanid.channelname)
                                                    else:
                                                        chanid._topic = param[2]
                                                        if chanid._topic[0] == ":":
                                                            chanid._topic = strdata.split(" ", 2)[2][1:]

                                                        if chanid._topic == "":
                                                            chanid._topic = ""
                                                        else:
                                                            chanid._topic_nick = self._nickname
                                                            chanid._topic_time = GetEpochTime()

                                                        for each in chanid._users:
                                                            clientid = \
                                                                server_context.nickname_to_client_mapping_entries[each]
                                                            clientid.send(
                                                                ":%s!%s@%s TOPIC %s :%s\r\n" % (
                                                                    self._nickname, self._username, self._hostmask,
                                                                    chanid.channelname, chanid._topic))
                                                else:
                                                    raw_messages.raw(self, "482", self._nickname, chanid.channelname)
                                            else:
                                                raw_messages.raw(self, "442", self._nickname, chanid.channelname)
                                        else:
                                            raw_messages.raw(self, "403", self._nickname, param[1])

                                elif param[0] == "OPER":
                                    Oper_function(self, param)

                                elif param[0] == "WHISPER":
                                    if chanid:
                                        if self._nickname.lower() in chanid._users:
                                            if cid:
                                                if param[2].lower() in chanid._users:
                                                    wmsg = param[3]
                                                    if wmsg[0] == ":":
                                                        wmsg = strdata.split(" ", 3)[3][1:]
                                                    if wmsg != ":":

                                                        if self.selfaccess(cid) == True:
                                                            if chanid.MODE_whisper:
                                                                if cid._nickname.lower() in chanid._op or cid._nickname.lower() in chanid._owner or self._nickname.lower() in chanid._op or self._nickname.lower() in chanid._owner:
                                                                    if self._MODE_nowhisper and self._nickname.lower() not in server_context.operator_entries:
                                                                        self.send(
                                                                            ":" + server_context.configuration.server_name +
                                                                            " NOTICE SERVER :*** You cannot whisper if +P is set\r\n")
                                                                    else:
                                                                        if cid._MODE_nowhisper:
                                                                            self.send(
                                                                                ":" + server_context.configuration.server_name +
                                                                                " NOTICE SERVER :*** This user has chosen not to receive whispers\r\n")
                                                                        else:
                                                                            cid.send(
                                                                                ":%s!%s@%s WHISPER %s %s :%s\r\n" % (
                                                                                    self._nickname, self._username,
                                                                                    self._hostmask, chanid.channelname,
                                                                                    cid._nickname, wmsg))
                                                                else:
                                                                    raw_messages.raw(self, "923", self._nickname,
                                                                                     chanid.channelname)
                                                            else:
                                                                if self._MODE_nowhisper and self._nickname.lower() not in server_context.operator_entries:
                                                                    self.send(
                                                                        ":" + server_context.configuration.server_name +
                                                                        " NOTICE SERVER :*** You cannot whisper if +P is set\r\n")
                                                                else:
                                                                    if cid._MODE_nowhisper:
                                                                        self.send(
                                                                            ":" + server_context.configuration.server_name +
                                                                            " NOTICE SERVER :*** This user has chosen not to receive whispers\r\n")
                                                                    else:
                                                                        cid.send(
                                                                            ":%s!%s@%s WHISPER %s %s :%s\r\n" % (
                                                                                self._nickname, self._username,
                                                                                self._hostmask, chanid.channelname,
                                                                                cid._nickname, wmsg))
                                                    else:
                                                        raw_messages.raw(self, "412", self._nickname, param[0])
                                                else:
                                                    raw_messages.raw(self, "441", self._nickname, chanid.channelname)
                                            else:
                                                raw_messages.raw(self, "401", self._nickname, param[2])
                                        else:
                                            raw_messages.raw(self, "442", self._nickname, chanid.channelname)
                                    else:
                                        raw_messages.raw(self, "403", self._nickname, param[1])

                                elif param[0] == "PRIVMSG" or param[0] == "NOTICE":
                                    try:
                                        if len(param) == 2:
                                            raw_messages.raw(self, "412", self._nickname, param[0])  # no text to send
                                        elif param[2] == ":" and len(param) == 3:
                                            raw_messages.raw(self, "412", self._nickname, param[0])  # no text to send

                                        elif param[1].upper() == "NICKSERV":  # support for /msg nickserv
                                            Nickserv_function(self, param[1:], param[0])
                                        else:

                                            iloop = 0
                                            chans = []
                                            if param[1].lower() not in server_context.channel_entries and param[
                                                1].lower() not in server_context.nickname_to_client_mapping_entries:
                                                self.pmflooding += 1

                                            while iloop < len(param[1].split(",")):
                                                if iloop == 32:
                                                    break
                                                recipient = param[1].split(",")[iloop].lower()

                                                if recipient not in server_context.operator_entries and self._MODE_gag:
                                                    self.send(
                                                        ":" + server_context.configuration.server_name +
                                                        " NOTICE GAG :*** You are unable to participate because you are on the server GAG list\r\n")
                                                else:

                                                    recip = getUserOBJ(recipient.lower())

                                                    if recipient.lower() not in chans:
                                                        chans.append(recipient.lower())
                                                        self._idletime = GetEpochTime()
                                                        msg = param[2]
                                                        if msg[0] == ":":
                                                            msg = strdata.split(" ", 2)[2][1:]

                                                        if recipient.lower() in server_context.channel_entries:  # channel exists
                                                            chanclass = server_context.channel_entries[recipient]
                                                            chanclass.communicate(self._nickname, param[0], msg)
                                                            if self._nickname.lower() not in server_context.operator_entries:
                                                                if isOp(self._nickname.lower(), chanclass.channelname):
                                                                    floodtime = 1000
                                                                else:
                                                                    floodtime = 2000

                                                                # let's work in ms shall we?
                                                                if int((
                                                                               GetEpochTime() - self.pmlastcommand) * 1000) <= floodtime:
                                                                    if param[
                                                                        1].lower() in server_context.channel_entries:
                                                                        self.pmflooding += 1

                                                                else:
                                                                    self.pmflooding = 0

                                                                self.pmlastcommand = GetEpochTime()

                                                                if isOp(self._nickname.lower(), chanclass.channelname):
                                                                    floodlimit = 50
                                                                else:
                                                                    floodlimit = 30

                                                                if self.pmflooding == floodlimit and "PRIVMSG" not in server_context.configuration.flooding_exempt_commands:  # 15 commands per 1000 miliseconds, anymore than that will kill the user
                                                                    print("Input flooding!!")
                                                                    self.quittype = 4
                                                                    self.send(
                                                                        "ERROR :Closing Link: " + self.details[0] +
                                                                        " (Input flooding)\r\n")
                                                                    self.die = True
                                                                    self.close()

                                                                if myint(
                                                                        chanclass._prop.lag) != 0 and isOp(
                                                                    self._nickname.lower(),
                                                                    chanclass.channelname) == False:
                                                                    time.sleep(chanclass._prop.lag)

                                                        # cannot ignore server messages using access
                                                        elif recipient[0] == "*" or recipient[0] == "$":
                                                            if opid:
                                                                if recipient[0] == "$":
                                                                    for n in server_context.nickname_to_client_mapping_entries:
                                                                        cclientid = \
                                                                            server_context.nickname_to_client_mapping_entries[
                                                                                n.lower()]
                                                                        cclientid.send(":%s!%s@%s %s %s :%s\r\n" %
                                                                                       (self._nickname, self._username,
                                                                                        self._hostmask,
                                                                                        param[0].upper(),
                                                                                        cclientid._nickname, msg))
                                                                else:
                                                                    if opid.operator_level > 2:
                                                                        for n in server_context.nickname_to_client_mapping_entries:
                                                                            cclientid = \
                                                                                server_context.nickname_to_client_mapping_entries[
                                                                                    n.lower()]
                                                                            cclientid.send(
                                                                                ":%s!%s@%s %s %s :%s\r\n" % (
                                                                                    self._nickname, self._username,
                                                                                    self._hostmask, param[0].upper(),
                                                                                    cclientid._nickname, msg))

                                                                    else:
                                                                        raw_messages.raw(self, "481", self._nickname,
                                                                                         "Permission Denied - You're not an Administratorr")
                                                            else:
                                                                raw_messages.raw(self, "481", self._nickname,
                                                                                 "Permission Denied - You're not a System operator")

                                                        elif recip:
                                                            floodtime = 1000
                                                            if int(
                                                                    (
                                                                            GetEpochTime() - self.pmlastcommand) * 1000) <= floodtime:  # let's work in ms shall we?
                                                                if param[
                                                                    1].lower() in server_context.nickname_to_client_mapping_entries:
                                                                    self.pmflooding += 1
                                                            else:
                                                                self.pmflooding = 0

                                                            self.pmlastcommand = GetEpochTime()

                                                            floodlimit = 20

                                                            if self.pmflooding == floodlimit:  # 15 commands per 1000 miliseconds, anymore than that will kill the user
                                                                print("Input flooding!!")
                                                                self.quittype = 4
                                                                self.send(
                                                                    "ERROR :Closing Link: " + self.details[0] +
                                                                    " (Input flooding)\r\n")
                                                                self.die = True
                                                                self.close()

                                                            if self._MODE_private and self._nickname.lower() not in server_context.operator_entries:
                                                                self.send(
                                                                    ":" + server_context.configuration.server_name +
                                                                    " NOTICE SERVER :*** You cannot send private messages if +p is set\r\n")
                                                            else:
                                                                if recip._MODE_private == False or self._nickname.lower() in server_context.operator_entries:  # opers can send messages to users with private set
                                                                    sendprivmsg = True
                                                                    if self.selfaccess(recip) == False:
                                                                        sendprivmsg = False
                                                                    if recip._MODE_filter:
                                                                        foundprofanity = False
                                                                        for all in server_context.configuration.profanity_entries:
                                                                            tmsg = re.compile(all.lower().replace(
                                                                                ".", r"\.").replace("*", "(.+|)"))
                                                                            if tmsg.match(msg.lower()):
                                                                                foundprofanity = True
                                                                                break

                                                                        if foundprofanity:
                                                                            self.send(
                                                                                ":" + server_context.configuration.server_name +
                                                                                " NOTICE SERVER :*** This user has chosen not to receive filtered content\r\n")
                                                                            sendprivmsg = False

                                                                    if sendprivmsg:

                                                                        if recip._MODE_registerchat and self._MODE_register == False and self._nickname.lower() not in server_context.operator_entries and self != recip:
                                                                            self.send(
                                                                                ":" + server_context.configuration.server_name +
                                                                                " NOTICE SERVER :*** Cannot send a message to this user, you must register or identify your nickname to services first\r\n")
                                                                        else:
                                                                            if recipient.lower() in server_context.nickname_to_client_mapping_entries:
                                                                                recip.send(
                                                                                    ":%s!%s@%s %s %s :%s\r\n" %
                                                                                    (self._nickname, self._username,
                                                                                     self._hostmask, param[0].upper(),
                                                                                     recip._nickname, msg))

                                                                else:
                                                                    self.send(
                                                                        ":" + server_context.configuration.server_name +
                                                                        " NOTICE SERVER :*** This user has chosen not to receive  filtered content\r\n")

                                                        else:
                                                            raw_messages.raw(self, "401", self._nickname, recipient)

                                                iloop += 1

                                            del chans

                                    except IndexError:
                                        raw_messages.raw(self, "411", self._nickname, param[0])

                                elif param[0] == "INVITE":
                                    if self._MODE_inviteblock:
                                        raw_messages.raw(self, "998", self._nickname, self._nickname, "*")
                                    else:
                                        if param[2].lower() in server_context.channel_entries:
                                            chanid = server_context.channel_entries[param[2].lower()]
                                            if self._nickname.lower() in chanid._users:
                                                if param[
                                                    1].lower() in server_context.nickname_to_client_mapping_entries:
                                                    if self._nickname.lower() in chanid._op or self._nickname.lower() in chanid._owner:
                                                        cid = server_context.nickname_to_client_mapping_entries[
                                                            param[1].lower()]
                                                        if cid._MODE_inviteblock:
                                                            raw_messages.raw(self, "998", self._nickname,
                                                                             cid._nickname, chanid.channelname)
                                                        else:
                                                            if param[1].lower() in chanid._users and param[
                                                                1].lower() not in chanid._watch:
                                                                raw_messages.raw(self, "443", self._nickname,
                                                                                 cid._nickname, chanid.channelname)
                                                            else:

                                                                sendinvite = True
                                                                if self.selfaccess(cid) == False:
                                                                    sendinvite = False

                                                                if sendinvite:
                                                                    raw_messages.raw(self, "341", self._nickname,
                                                                                     cid._nickname, chanid.channelname)
                                                                    cid.send(
                                                                        ":%s!%s@%s INVITE %s :%s\r\n" %
                                                                        (self._nickname, self._username, self._hostmask,
                                                                         cid._nickname, chanid.channelname))
                                                                    cid._invites.append(chanid.channelname.lower())
                                                    else:
                                                        raw_messages.raw(self, "482", self._nickname,
                                                                         chanid.channelname)
                                                else:
                                                    raw_messages.raw(self, "401", self._nickname, param[1])
                                            else:
                                                raw_messages.raw(self, "442", self._nickname, chanid.channelname)
                                        else:
                                            raw_messages.raw(self, "403", self._nickname, param[2])

                                elif param[0] == "PART":
                                    iloop = 0
                                    while iloop < len(param[1].split(",")):
                                        chan = getChannelOBJ(param[1].split(",")[iloop].lower())
                                        if chan:
                                            chan.part(self._nickname)
                                        else:
                                            raw_messages.raw(self, "403", self._nickname, param[1].split(",")[iloop])

                                        iloop += 1

                                elif param[0] == "NAMES":
                                    if chanid:
                                        chanid.sendnames(self._nickname)  # send when requested
                                    elif param[1][0] == "*":
                                        raw_messages.raw(self, "481", self._nickname, "Permission Denied")
                                    else:
                                        raw_messages.raw(self, "403", self._nickname, param[1])

                                elif param[0] == "LISTC":
                                    raw_messages.raw(self, "321", self._nickname)
                                    for chanid in getGlobalChannels():
                                        chanusers = str(len(chanid._users) - len(chanid._watch))
                                        if chanid.MODE_auditorium and self._nickname.lower() not in server_context.operator_entries and isOp(
                                                self._nickname.lower(), chanid.channelname) == False:
                                            chanusers = str((len(chanid._op) + len(chanid._owner)))
                                        if len(param) == 2:
                                            sub = param[1]
                                        else:
                                            sub = ""

                                        if chanid._prop.subject.upper() == sub.upper():
                                            if isSecret(chanid, "hidden"):
                                                if self._nickname.lower() in chanid._users or self._nickname.lower() in server_context.operator_entries:
                                                    raw_messages.raw(self, "322", self._nickname,
                                                                     chanid.channelname, chanusers, chanid._topic)
                                            else:
                                                raw_messages.raw(self, "322", self._nickname,
                                                                 chanid.channelname, chanusers, chanid._topic)

                                    raw_messages.raw(self, "323", self._nickname)

                                elif param[0] == "LIST" or param[0] == "LISTX":
                                    try:
                                        raw_messages.raw(self, "321", self._nickname)
                                        for chanid in getGlobalChannels():
                                            chanusers = str(len(chanid._users) - len(chanid._watch))
                                            if chanid.MODE_auditorium and self._nickname.lower() not in server_context.operator_entries and isOp(
                                                    self._nickname.lower(), chanid.channelname) == False:
                                                chanusers = str((len(chanid._op) + len(chanid._owner)))

                                            if isSecret(chanid, "hidden"):
                                                if self._nickname.lower() in chanid._users or self._nickname.lower() in server_context.operator_entries:
                                                    raw_messages.raw(self, "322", self._nickname,
                                                                     chanid.channelname, chanusers, chanid._topic)
                                            else:
                                                if param[0] == "LISTX" and len(param) == 2:

                                                    if "<" in param[1]:
                                                        if len(param[1].split("<")) == 2:
                                                            lowerthanparam = param[1].split("<")[1].split(",")[0]
                                                        if myint(chanusers) < myint(lowerthanparam):
                                                            raw_messages.raw(self, "322", self._nickname,
                                                                             chanid.channelname, chanusers,
                                                                             chanid._topic)

                                                    elif ">" in param[1]:
                                                        if len(param[1].split(">")) == 2:
                                                            lowerthanparam = param[1].split(">")[1].split(",")[0]
                                                        if myint(chanusers) > myint(lowerthanparam):
                                                            raw_messages.raw(self, "322", self._nickname,
                                                                             chanid.channelname, chanusers,
                                                                             chanid._topic)
                                                    elif "R=0" == param[1]:
                                                        if chanid.MODE_registered == False:
                                                            raw_messages.raw(self, "322", self._nickname,
                                                                             chanid.channelname, chanusers,
                                                                             chanid._topic)

                                                    elif "IRCX=0" == param[1]:
                                                        if chanid.MODE_noircx:
                                                            raw_messages.raw(self, "322", self._nickname,
                                                                             chanid.channelname, chanusers,
                                                                             chanid._topic)

                                                    elif "IRCX=1" == param[1]:
                                                        if chanid.MODE_noircx == False:
                                                            raw_messages.raw(self, "322", self._nickname,
                                                                             chanid.channelname, chanusers,
                                                                             chanid._topic)

                                                    elif "R=1" == param[1]:
                                                        if chanid.MODE_registered:
                                                            raw_messages.raw(self, "322", self._nickname,
                                                                             chanid.channelname, chanusers,
                                                                             chanid._topic)

                                                    elif "N=" in param[1]:
                                                        try:
                                                            matchstring = param[1].split("=", 1)[1].lower()
                                                            if matchstring in chanid.channelname.lower():
                                                                raw_messages.raw(self, "322", self._nickname,
                                                                                 chanid.channelname, chanusers,
                                                                                 chanid._topic)

                                                        except:
                                                            pass

                                                    elif "T=" in param[1]:
                                                        try:
                                                            matchstring = param[1].split("=", 1)[1].lower()
                                                            if matchstring in chanid._topic.lower():
                                                                raw_messages.raw(self, "322", self._nickname,
                                                                                 chanid.channelname, chanusers,
                                                                                 chanid._topic)

                                                        except:
                                                            pass

                                                    else:
                                                        raw_messages.raw(self, "322", self._nickname,
                                                                         chanid.channelname, chanusers, chanid._topic)

                                                else:
                                                    raw_messages.raw(self, "322", self._nickname,
                                                                     chanid.channelname, chanusers, chanid._topic)
                                    except:
                                        pass

                                    raw_messages.raw(self, "323", self._nickname)

                                elif param[0] == "ACCESS":
                                    if chanid:
                                        access_helper.CheckChannelExpiry(chanid)
                                        if len(param) == 2:
                                            if chanid.MODE_noircx and self._nickname.lower() not in server_context.operator_entries:
                                                raw_messages.raw(self, "997", self._nickname, chanid.channelname,
                                                                 param[0])
                                            else:
                                                if isOp(self._nickname, chanid.channelname) == False:
                                                    raw_messages.raw(self, "913", self._nickname, chanid.channelname)
                                                else:
                                                    raw_messages.raw(self, "803", self._nickname, chanid.channelname)
                                                    for each in chanid.ChannelAccess:
                                                        if each._deleteafterexpire == False:
                                                            exp = 0
                                                        else:
                                                            exp = (each._expires - GetEpochTime()) / 60
                                                            if exp < 1:
                                                                exp = 0

                                                        stringinf = "%s %s %s %d %s %s" % (
                                                            chanid.channelname, each._level, each._mask, exp,
                                                            each._setby, each._reason)
                                                        raw_messages.raw(self, "804", self._nickname, stringinf)

                                                    raw_messages.raw(self, "805", self._nickname, chanid.channelname)
                                        else:
                                            try:
                                                if chanid.MODE_noircx and self._nickname.lower() not in server_context.operator_entries:
                                                    raw_messages.raw(self, "997", self._nickname, chanid.channelname,
                                                                     param[0])

                                                elif chanid.MODE_ownersetaccess and self._nickname.lower() not in chanid._owner and self._nickname.lower() not in server_context.operator_entries:
                                                    raw_messages.raw(self, "485", self._nickname, chanid.channelname)

                                                elif param[2].upper() == "ADD":
                                                    if param[3].upper() == "DENY" or param[3].upper() == "GRANT" or \
                                                            param[3].upper() == "VOICE" or param[3].upper() == "HOST" or \
                                                            param[3].upper() == "OWNER":
                                                        if len(chanid.ChannelAccess) > myint(MaxChannelEntries):
                                                            raw_messages.raw(self, "916", self._nickname,
                                                                             chanid.channelname)
                                                        else:  # ACCESS # ADD OWNER
                                                            if len(param) == 4:
                                                                param.append("*!*@*$*")
                                                            _mask = access_helper.CreateMaskString(param[4])
                                                            if _mask == -1:
                                                                raw_messages.raw(self, "906", self._nickname, param[4])
                                                            elif _mask == -2:
                                                                raw_messages.raw(self, "909", self._nickname)
                                                            else:
                                                                tag, exp = "", 0
                                                                if len(param) >= 6:
                                                                    exp = myint(param[5])
                                                                if len(param) >= 7:
                                                                    if param[6][0] == ":":
                                                                        tag = strdata.split(" ", 6)[6]
                                                                    else:
                                                                        tag = param[6]

                                                                _addrec = access_helper.AddRecord(
                                                                    self, chanid.channelname, param[3].upper(),
                                                                    _mask, exp, tag)
                                                                if _addrec == 1:
                                                                    stringinf = "%s %s %s %d %s %s" % (
                                                                        chanid.channelname, param[3].upper(),
                                                                        _mask, exp, self._hostmask, tag)
                                                                    raw_messages.raw(self, "801", self._nickname,
                                                                                     stringinf)

                                                                elif _addrec == -1:
                                                                    raw_messages.raw(self, "914", self._nickname,
                                                                                     chanid.channelname)

                                                                elif _addrec == -2:
                                                                    raw_messages.raw(self, "913", self._nickname,
                                                                                     chanid.channelname)
                                                                else:
                                                                    pass
                                                    else:
                                                        raw_messages.raw(self, "903", self._nickname,
                                                                         chanid.channelname)

                                                elif param[2].upper() == "DELETE":
                                                    if len(param) < 4:
                                                        raw_messages.raw(self, "903", self._nickname,
                                                                         chanid.channelname)
                                                    else:
                                                        if param[3].upper() == "DENY" or param[3].upper() == "GRANT" or \
                                                                param[3].upper() == "VOICE" or param[
                                                            3].upper() == "HOST" or param[3].upper() == "OWNER":
                                                            if len(param) == 4:
                                                                param.append("*!*@*$*")
                                                            _mask = access_helper.CreateMaskString(param[4])
                                                            if _mask == -1:
                                                                raw_messages.raw(self, "906", self._nickname, param[4])
                                                            elif _mask == -2:
                                                                raw_messages.raw(self, "909", self._nickname)
                                                            else:
                                                                _delrec = access_helper.DelRecord(
                                                                    self, chanid.channelname, param[3].upper(), _mask)
                                                                if _delrec == 1:
                                                                    stringinf = "%s %s %s" % (
                                                                        chanid.channelname, param[3].upper(), _mask)
                                                                    raw_messages.raw(self, "802", self._nickname,
                                                                                     stringinf)

                                                                elif _delrec == -1:
                                                                    raw_messages.raw(self, "915", self._nickname,
                                                                                     chanid.channelname)
                                                                elif _delrec == -2:
                                                                    raw_messages.raw(self, "913", self._nickname,
                                                                                     chanid.channelname)
                                                        else:
                                                            raw_messages.raw(self, "903", self._nickname,
                                                                             chanid.channelname)

                                                elif param[2].upper() == "CLEAR":
                                                    if len(param) > 3:
                                                        if param[3].upper() != "DENY" and param[
                                                            3].upper() != "GRANT" and param[3].upper() != "VOICE" and \
                                                                param[3].upper() != "HOST" and param[
                                                            3].upper() != "OWNER":
                                                            raw_messages.raw(self, "900", self._nickname, param[3])
                                                        else:
                                                            access_helper.ClearRecords(
                                                                chanid.channelname, self, param[3].upper())

                                                    elif len(param) > 2:
                                                        access_helper.ClearRecords(chanid.channelname, self)

                                                elif param[2].upper() == "LIST":
                                                    if isOp(self._nickname, chanid.channelname) == False:
                                                        raw_messages.raw(self, "913", self._nickname,
                                                                         chanid.channelname)
                                                    else:
                                                        raw_messages.raw(self, "803", self._nickname,
                                                                         chanid.channelname)
                                                        for each in chanid.ChannelAccess:
                                                            if each._deleteafterexpire == False:
                                                                exp = 0
                                                            else:
                                                                exp = (each._expires - GetEpochTime()) / 60
                                                                if exp < 1:
                                                                    exp = 0

                                                            stringinf = "%s %s %s %d %s %s" % (
                                                                chanid.channelname, each._level, each._mask, exp,
                                                                each._setby, each._reason)
                                                            raw_messages.raw(self, "804", self._nickname, stringinf)

                                                        raw_messages.raw(self, "805", self._nickname,
                                                                         chanid.channelname)

                                                elif param[2].upper() == "REGISTER":
                                                    operuser = isAdmin(self._nickname)
                                                    if operuser != "":
                                                        if chanid.MODE_registered == False:
                                                            chanid.MODE_registered = True
                                                            chanid._prop.registered = operuser
                                                            _founder = ""
                                                            if len(param) == 4:
                                                                _founder = access_helper.CreateMaskString(param[3])
                                                                if _founder == -1:
                                                                    _founder = ""
                                                                    raw_messages.raw(self, "906", self._nickname,
                                                                                     param[4])
                                                                elif _founder == -2:
                                                                    _founder = ""
                                                                    raw_messages.raw(self, "909", self._nickname)

                                                                else:
                                                                    _addrec = access_helper.AddRecord(
                                                                        "", chanid.channelname.lower(),
                                                                        "OWNER", _founder, 0, "")
                                                                    stringinf = "%s %s %s %d %s %s" % (
                                                                        chanid.channelname, "FOUNDER", _founder, 0,
                                                                        server_context.configuration.server_name, "")
                                                                    raw_messages.raw(self, "801", self._nickname,
                                                                                     stringinf)

                                                            # Channel=#testModes=ntfrdSl 25Subject=AdultTopic=Chat related difficultiesfounderaccess=&chris

                                                            chanid._founder = _founder

                                                            for each in chanid._users:
                                                                cclientid = \
                                                                    server_context.nickname_to_client_mapping_entries[
                                                                        each]
                                                                cclientid.send(
                                                                    ":%s MODE %s +r\r\n" %
                                                                    (server_context.configuration.server_name,
                                                                     chanid.channelname))

                                                            sendWatchOpers(
                                                                "Notice -- The channel, '%s' has been registered (%s!%s@%s) [%s] \r\n" % (
                                                                    chanid.channelname, self._nickname, self._username,
                                                                    self._hostmask, self.details[0]))

                                                            WriteUsers(False, True)
                                                        else:
                                                            self.send(
                                                                ":%s NOTICE %s :*** Notice -- Channel is already registered\r\n" % (
                                                                    server_context.configuration.server_name,
                                                                    self._nickname))
                                                    else:
                                                        raw_messages.raw(self, "908", self._nickname)

                                                elif param[2].upper() == "UNREGISTER":
                                                    operuser = isAdmin(self._nickname)
                                                    if operuser != "":
                                                        if chanid.MODE_registered == True:
                                                            chanid.MODE_registered = False
                                                            chanid._prop.registered = ""
                                                            chanid._founder = ""

                                                            access_helper.ClearRecords(chanid.channelname, self,
                                                                                       "OWNER")

                                                            for each in chanid._users:
                                                                cclientid = \
                                                                    server_context.nickname_to_client_mapping_entries[
                                                                        each]
                                                                cclientid.send(
                                                                    ":%s MODE %s -r\r\n" %
                                                                    (server_context.configuration.server_name,
                                                                     chanid.channelname))

                                                            sendWatchOpers(
                                                                "Notice -- The channel, '%s' has been unregistered (%s!%s@%s) [%s] \r\n" % (
                                                                    chanid.channelname, self._nickname, self._username,
                                                                    self._hostmask, self.details[0]))

                                                            if len(chanid._users) == 0:
                                                                chanid.resetchannel()

                                                            WriteUsers(False, True)
                                                        else:
                                                            self.send(
                                                                ":%s NOTICE %s :*** Notice -- Channel is not registered\r\n" % (
                                                                    server_context.configuration.server_name,
                                                                    self._nickname))

                                                    else:
                                                        raw_messages.raw(self, "908", self._nickname)
                                                else:
                                                    raw_messages.raw(self, "900", self._nickname, param[1])
                                            except:
                                                raw_messages.raw(self, "903", self._nickname, param[1])

                                    elif param[1] == "*" or param[1] == "$" or param[
                                        1].upper() == self._nickname.upper():

                                        if param[1] != "*" and param[1] != "$":
                                            ret = self._nickname
                                            _list = self._access
                                        else:
                                            CheckServerAccess()
                                            ret = "*"
                                            _list = server_context.server_access_entries

                                        access_helper.CheckSelfExpiry(self)

                                        if opid or param[1] != "*":
                                            operlvl = False
                                            if param[1] == "*":
                                                if opid.operator_level > 1:
                                                    operlvl = True

                                            if operlvl == False and param[1] == "*":
                                                raw_messages.raw(self, "913", self._nickname, param[1])
                                            else:
                                                if len(param) == 2:
                                                    raw_messages.raw(self, "803", self._nickname, ret)
                                                    for each in _list:
                                                        if each._deleteafterexpire == False:
                                                            exp = 0
                                                        else:
                                                            exp = (each._expires - GetEpochTime()) / 60
                                                            if exp < 1:
                                                                exp = 0

                                                        stringinf = "%s %s %s %d %s %s" % (
                                                            ret, each._level, each._mask, exp, each._setby,
                                                            each._reason)
                                                        raw_messages.raw(self, "804", self._nickname, stringinf)

                                                    raw_messages.raw(self, "805", self._nickname, ret)

                                                else:

                                                    try:
                                                        if param[2].upper() == "ADD":  # access * add deny test
                                                            if len(param) < 4:
                                                                raw_messages.raw(self, "903", self._nickname, ret)
                                                            else:
                                                                if param[3].upper() == "DENY" or param[
                                                                    3].upper() == "GRANT":
                                                                    if ret == "*":
                                                                        _entries = MaxServerEntries
                                                                    else:
                                                                        _entries = MaxUserEntries

                                                                    if len(_list) > myint(_entries):
                                                                        raw_messages.raw(self, "916", self._nickname,
                                                                                         ret)
                                                                    else:
                                                                        _mask = access_helper.CreateMaskString(param[4])
                                                                        if _mask == -1:
                                                                            raw_messages.raw(self, "906",
                                                                                             self._nickname, param[4])
                                                                        elif _mask == -2:
                                                                            raw_messages.raw(self, "909",
                                                                                             self._nickname)
                                                                        else:
                                                                            tag, exp = "", 0
                                                                            if len(param) >= 6:
                                                                                exp = myint(param[5])
                                                                            if len(param) >= 7:
                                                                                if param[6][0] == ":":
                                                                                    tag = strdata.split(" ", 6)[6]
                                                                                else:
                                                                                    tag = param[6]

                                                                            _addrec = access_helper.AddRecord(
                                                                                self, ret, param[3].upper(),
                                                                                _mask, exp, tag)
                                                                            if _addrec == 1:
                                                                                stringinf = "%s %s %s %d %s %s" % (
                                                                                    ret, param[3].upper(),
                                                                                    _mask, exp, self._hostmask, tag)
                                                                                raw_messages.raw(self, "801",
                                                                                                 self._nickname,
                                                                                                 stringinf)
                                                                                if ret == "*":
                                                                                    sendWatchOpers(
                                                                                        "Notice -- The record, '%s %s' has been added to server access by (%s!%s@%s) [%s] \r\n" % (
                                                                                            param[3].upper(), _mask,
                                                                                            self._nickname,
                                                                                            self._username,
                                                                                            self._hostmask,
                                                                                            self.details[0]))

                                                                                WriteUsers(False, False, True)

                                                                            elif _addrec == -1:
                                                                                raw_messages.raw(self, "914",
                                                                                                 self._nickname,
                                                                                                 param[1])
                                                                            else:
                                                                                pass

                                                                else:
                                                                    raw_messages.raw(self, "903", self._nickname, ret)

                                                        elif param[2].upper() == "DELETE":
                                                            if len(param) < 4:
                                                                raw_messages.raw(self, "903", self._nickname, ret)
                                                            else:
                                                                if param[3].upper() == "DENY" or param[
                                                                    3].upper() == "GRANT":
                                                                    _mask = access_helper.CreateMaskString(param[4])
                                                                    if _mask == -1:
                                                                        raw_messages.raw(self, "906", self._nickname,
                                                                                         param[4])
                                                                    elif _mask == -2:
                                                                        raw_messages.raw(self, "909", self._nickname)
                                                                    else:
                                                                        _delrec = access_helper.DelRecord(
                                                                            self, ret, param[3].upper(), _mask)
                                                                        if _delrec == 1:
                                                                            stringinf = "%s %s %s" % (
                                                                                ret, param[3].upper(), _mask)
                                                                            raw_messages.raw(self, "802",
                                                                                             self._nickname, stringinf)
                                                                            if ret == "*":
                                                                                sendWatchOpers(
                                                                                    "Notice -- The record, '%s %s' was requested to be deleted (%s!%s@%s) [%s] \r\n" % (
                                                                                        param[3].upper(), _mask,
                                                                                        self._nickname, self._username,
                                                                                        self._hostmask,
                                                                                        self.details[0]))

                                                                            WriteUsers(False, False, True)

                                                                        elif _delrec == -1:
                                                                            raw_messages.raw(self, "915",
                                                                                             self._nickname, ret)
                                                                        elif _delrec == -2:
                                                                            raw_messages.raw(self, "913",
                                                                                             self._nickname, ret)
                                                                else:
                                                                    raw_messages.raw(self, "903", self._nickname, ret)

                                                        elif param[2].upper() == "CLEAR":  # access # clear [deny]
                                                            if len(param) > 3:
                                                                if param[3].upper() != "DENY" and param[
                                                                    3].upper() != "GRANT":
                                                                    raw_messages.raw(self, "900", self._nickname,
                                                                                     param[3])
                                                                else:
                                                                    access_helper.ClearRecords(ret, self,
                                                                                               param[3].upper())
                                                                    if ret == "*":
                                                                        sendWatchOpers(
                                                                            "Notice -- Server access clear, '%s' has been cleared by (%s!%s@%s) [%s] \r\n" % (
                                                                                param[3].upper(), self._nickname,
                                                                                self._username, self._hostmask,
                                                                                self.details[0]))

                                                                    WriteUsers(False, False, True)

                                                            elif len(param) > 2:
                                                                access_helper.ClearRecords(ret, self)
                                                                if ret == "*":
                                                                    sendWatchOpers(
                                                                        "Notice -- Server access has been cleared (%s!%s@%s) [%s] \r\n" % (
                                                                            self._nickname, self._username,
                                                                            self._hostmask, self.details[0]))

                                                                WriteUsers(False, False, True)

                                                        elif param[2].upper() == "LIST":
                                                            raw_messages.raw(self, "803", self._nickname, ret)
                                                            for each in _list:
                                                                if each._deleteafterexpire == False:
                                                                    exp = 0
                                                                else:
                                                                    exp = (each._expires - GetEpochTime()) / 60
                                                                    if exp < 1:
                                                                        exp = 0

                                                                stringinf = "%s %s %s %d %s %s" % (
                                                                    ret, each._level, each._mask, exp, each._setby,
                                                                    each._reason)
                                                                raw_messages.raw(self, "804", self._nickname, stringinf)

                                                            raw_messages.raw(self, "805", self._nickname, ret)
                                                        else:
                                                            raw_messages.raw(self, "900", self._nickname, ret)
                                                    except:
                                                        raw_messages.raw(self, "903", self._nickname, ret)
                                        else:
                                            raw_messages.raw(self, "913", self._nickname, ret)

                                    elif cid:
                                        raw_messages.raw(self, "925", self._nickname, param[1])

                                    else:
                                        raw_messages.raw(self, "924", self._nickname, param[1])

                                elif param[0] == "PROP":
                                    if len(param) > 2:
                                        if chanid:
                                            if chanid.MODE_noircx and self._nickname.lower() not in server_context.operator_entries:
                                                raw_messages.raw(self, "997", self._nickname, chanid.channelname,
                                                                 param[1])

                                            elif param[2].upper() == "*":
                                                if isSecret(chanid,
                                                            "private") == False or self._nickname.lower() in server_context.operator_entries or self._nickname.lower() in chanid._users:
                                                    raw_messages.raw(self, "818", self._nickname,
                                                                     "%s OID :0" % (chanid.channelname))
                                                    raw_messages.raw(self, "818", self._nickname, "%s Name :%s" %
                                                                     (chanid.channelname, chanid.channelname))

                                                    if self._nickname.lower() in server_context.operator_entries:
                                                        if chanid._prop.account:
                                                            raw_messages.raw(self, "818", self._nickname,
                                                                             "%s Account :%s!%s@%s (%s)" % (
                                                                                 chanid.channelname,
                                                                                 chanid._prop.account_name,
                                                                                 chanid._prop.account_user,
                                                                                 chanid._prop.account_hostmask,
                                                                                 chanid._prop.account_address))
                                                        else:
                                                            raw_messages.raw(self, "818", self._nickname,
                                                                             "%s Account :%s" %
                                                                             (chanid.channelname,
                                                                              server_context.configuration.server_name))

                                                    if self._nickname.lower() in server_context.operator_entries and chanid.MODE_registered:
                                                        raw_messages.raw(self, "818", self._nickname,
                                                                         "%s Registered :%s" %
                                                                         (chanid.channelname, chanid._prop.registered))

                                                    raw_messages.raw(self, "818", self._nickname, "%s Creation :%s" %
                                                                     (chanid.channelname, chanid._prop.creation))
                                                    if chanid._prop.ownerkey != "" and self._nickname.lower() in chanid._owner or self._nickname.lower() in server_context.operator_entries and chanid._prop.ownerkey != "":
                                                        raw_messages.raw(self, "818", self._nickname,
                                                                         "%s Ownerkey :%s" %
                                                                         (chanid.channelname, chanid._prop.ownerkey))
                                                    if chanid._prop.hostkey != "" and self._nickname.lower() in chanid._owner or self._nickname.lower() in server_context.operator_entries and chanid._prop.hostkey != "":
                                                        raw_messages.raw(self, "818", self._nickname, "%s Hostkey :%s" %
                                                                         (chanid.channelname, chanid._prop.hostkey))
                                                    if chanid.MODE_key != "" and self._nickname.lower() in chanid._users:
                                                        raw_messages.raw(self, "818", self._nickname,
                                                                         "%s Memberkey :%s" %
                                                                         (chanid.channelname, chanid.MODE_key))

                                                    if chanid._prop.reset != 0:
                                                        raw_messages.raw(self, "818", self._nickname, "%s Reset :%d" %
                                                                         (chanid.channelname, chanid._prop.reset))

                                                    if chanid._prop.language != "":
                                                        raw_messages.raw(self, "818", self._nickname,
                                                                         "%s Language :%s" %
                                                                         (chanid.channelname, chanid._prop.language))

                                                    if chanid._topic != "":
                                                        if chanid._topic[0] == ":":
                                                            raw_messages.raw(self, "818", self._nickname,
                                                                             "%s Topic %s" %
                                                                             (chanid.channelname, chanid._topic))
                                                        else:
                                                            raw_messages.raw(self, "818", self._nickname,
                                                                             "%s Topic :%s" %
                                                                             (chanid.channelname, chanid._topic))
                                                    if chanid._prop.client != "":
                                                        raw_messages.raw(self, "818", self._nickname, "%s Client :%s" %
                                                                         (chanid.channelname, chanid._prop.client))
                                                    if chanid._prop.lag != "" and myint(chanid._prop.lag) != 0:
                                                        raw_messages.raw(self, "818", self._nickname, "%s Lag :%s" %
                                                                         (chanid.channelname, chanid._prop.lag))
                                                    if chanid._prop.onjoin != "":
                                                        raw_messages.raw(self, "818", self._nickname, "%s Onjoin :%s" %
                                                                         (chanid.channelname, chanid._prop.onjoin))
                                                    if chanid._prop.onpart != "":
                                                        raw_messages.raw(self, "818", self._nickname, "%s Onpart :%s" %
                                                                         (chanid.channelname, chanid._prop.onpart))
                                                    if chanid._prop.subject != "":
                                                        raw_messages.raw(self, "818", self._nickname, "%s Subject :%s" %
                                                                         (chanid.channelname, chanid._prop.subject))

                                                raw_messages.raw(self, "819", self._nickname, chanid.channelname)

                                            # add elif for if prop is disabled for owners

                                            elif chanid.MODE_ownersetprop and self._nickname.lower() not in chanid._owner and len(
                                                    param) > 3 and self._nickname.lower() not in server_context.operator_entries:
                                                raw_messages.raw(self, "485", self._nickname, chanid.channelname)

                                            elif param[2].upper() == "CLIENT":
                                                if len(param) == 3:
                                                    if chanid._prop.client != "":
                                                        if isSecret(chanid,
                                                                    "private") == False or self._nickname.lower() in server_context.operator_entries or self._nickname.lower() in chanid._users:
                                                            raw_messages.raw(self, "818", self._nickname,
                                                                             "%s Client :%s" %
                                                                             (chanid.channelname, chanid._prop.client))

                                                    raw_messages.raw(self, "819", self._nickname, chanid.channelname)
                                                else:
                                                    chanid._prop._client(chanid, self, param[3])

                                            elif param[2].upper() == "SUBJECT":
                                                if len(param) == 3:
                                                    if chanid._prop.subject != "":
                                                        if isSecret(chanid,
                                                                    "private") == False or self._nickname.lower() in server_context.operator_entries or self._nickname.lower() in chanid._users:
                                                            raw_messages.raw(self, "818", self._nickname,
                                                                             "%s Subject :%s" %
                                                                             (chanid.channelname, chanid._prop.subject))

                                                    raw_messages.raw(self, "819", self._nickname, chanid.channelname)
                                                else:
                                                    chanid._prop._subject(chanid, self, param[3])

                                            elif param[2].upper() == "LAG":
                                                if len(param) == 3:
                                                    if myint(chanid._prop.lag) != 0:
                                                        if isSecret(chanid,
                                                                    "private") == False or self._nickname.lower() in server_context.operator_entries or self._nickname.lower() in chanid._users:
                                                            raw_messages.raw(self, "818", self._nickname, "%s Lag :%s" %
                                                                             (chanid.channelname, chanid._prop.lag))

                                                    raw_messages.raw(self, "819", self._nickname, chanid.channelname)
                                                else:
                                                    chanid._prop._lag(chanid, self, param[3])

                                            elif param[2].upper() == "LANGUAGE":
                                                if len(param) == 3:
                                                    if chanid._prop.language != "":
                                                        if isSecret(chanid,
                                                                    "private") == False or self._nickname.lower() in server_context.operator_entries or self._nickname.lower() in chanid._users:
                                                            raw_messages.raw(self, "818", self._nickname,
                                                                             "%s Language :%s" %
                                                                             (
                                                                                 chanid.channelname,
                                                                                 chanid._prop.language))

                                                    raw_messages.raw(self, "819", self._nickname, chanid.channelname)
                                                else:
                                                    chanid._prop._language(chanid, self, param[3])

                                            elif param[2].upper() == "ACCOUNT":
                                                if len(param) == 3:
                                                    if self._nickname.lower() in server_context.operator_entries:
                                                        if chanid._prop.account:
                                                            raw_messages.raw(self, "818", self._nickname,
                                                                             "%s Account :%s!%s@%s (%s)" % (
                                                                                 chanid.channelname,
                                                                                 chanid._prop.account_name,
                                                                                 chanid._prop.account_user,
                                                                                 chanid._prop.account_hostmask,
                                                                                 chanid._prop.account_address))
                                                        else:
                                                            raw_messages.raw(self, "818", self._nickname,
                                                                             "%s Account :%s" %
                                                                             (chanid.channelname,
                                                                              server_context.configuration.server_name))

                                                        raw_messages.raw(self, "819", self._nickname,
                                                                         chanid.channelname)
                                                    else:
                                                        raw_messages.raw(self, "908", self._nickname)
                                                else:
                                                    raw_messages.raw(self, "908", self._nickname)

                                            elif param[2].upper() == "TOPIC":
                                                if len(param) == 3:
                                                    if isSecret(chanid,
                                                                "private") == False or self._nickname.lower() in server_context.operator_entries or self._nickname.lower() in chanid._users:
                                                        if chanid._topic != "":
                                                            raw_messages.raw(self, "332", self._nickname,
                                                                             chanid.channelname, chanid._topic)
                                                            raw_messages.raw(self, "333", self._nickname,
                                                                             chanid.channelname,
                                                                             chanid._topic_nick, chanid._topic_time)

                                                        else:
                                                            raw_messages.raw(self, "331", self._nickname,
                                                                             chanid.channelname)

                                                    raw_messages.raw(self, "819", self._nickname, chanid.channelname)
                                                else:
                                                    chanid._prop._topic(
                                                        chanid, self, param[3],
                                                        strdata.split(" ", 3)[3][1:])

                                            elif param[2].upper() == "MEMBERKEY":
                                                if len(param) == 3:
                                                    if self._nickname.lower() in server_context.operator_entries or self._nickname.lower() in chanid._users:
                                                        raw_messages.raw(self, "818", self._nickname,
                                                                         "%s Memberkey :%s" %
                                                                         (chanid.channelname, chanid.MODE_key))

                                                    raw_messages.raw(self, "819", self._nickname, chanid.channelname)
                                                else:
                                                    chanid._prop._memberkey(chanid, self, param[3])

                                            elif param[2].upper() == "HOSTKEY":
                                                if len(param) == 3:
                                                    if chanid._prop.hostkey != "":
                                                        if self._nickname.lower() in server_context.operator_entries or self._nickname.lower() in chanid._owner:
                                                            raw_messages.raw(self, "818", self._nickname,
                                                                             "%s Hostkey :%s" %
                                                                             (chanid.channelname, chanid._prop.hostkey))
                                                            raw_messages.raw(self, "819", self._nickname,
                                                                             chanid.channelname)
                                                        else:
                                                            raw_messages.raw(self, "908", self._nickname)
                                                            pass
                                                else:
                                                    chanid._prop._hostkey(chanid, self, param[3])

                                            elif param[2].upper() == "OWNERKEY":
                                                if len(param) == 3:
                                                    if chanid._prop.ownerkey != "":
                                                        if self._nickname.lower() in server_context.operator_entries or self._nickname.lower() in chanid._owner:
                                                            raw_messages.raw(self, "818", self._nickname,
                                                                             "%s Ownerkey :%s" %
                                                                             (
                                                                                 chanid.channelname,
                                                                                 chanid._prop.ownerkey))
                                                            raw_messages.raw(self, "819", self._nickname,
                                                                             chanid.channelname)
                                                        else:
                                                            raw_messages.raw(self, "908", self._nickname)
                                                            pass
                                                else:
                                                    chanid._prop._ownerkey(chanid, self, param[3])

                                            elif param[2].upper() == "REGISTERED":
                                                if len(param) == 3:
                                                    if self._nickname.lower() in server_context.operator_entries:
                                                        raw_messages.raw(self, "818", self._nickname,
                                                                         "%s Registered :%s" %
                                                                         (chanid.channelname, chanid._prop.registered))
                                                    else:
                                                        raw_messages.raw(self, "908", self._nickname)
                                                else:
                                                    raw_messages.raw(self, "908", self._nickname)

                                            elif param[2].upper() == "NAME":
                                                if len(param) == 3:
                                                    if isSecret(chanid,
                                                                "private") == False or self._nickname.lower() in server_context.operator_entries or self._nickname.lower() in chanid._users:
                                                        raw_messages.raw(self, "818", self._nickname, "%s Name :%s" %
                                                                         (chanid.channelname, chanid.channelname))

                                                    raw_messages.raw(self, "819", self._nickname, chanid.channelname)
                                                else:
                                                    chanid._prop._name(chanid, self, param[3])

                                            elif param[2].upper() == "RESET":
                                                if len(param) == 3:
                                                    if isSecret(chanid,
                                                                "private") == False or self._nickname.lower() in server_context.operator_entries or self._nickname.lower() in chanid._users:
                                                        raw_messages.raw(self, "818", self._nickname, "%s Reset :%d" %
                                                                         (chanid.channelname, chanid._prop.reset))

                                                    raw_messages.raw(self, "819", self._nickname, chanid.channelname)
                                                else:
                                                    chanid._prop._reset(chanid, self, param[3])

                                            elif param[2].upper() == "OID":
                                                if len(param) == 3:
                                                    if isSecret(chanid,
                                                                "private") == False or self._nickname.lower() in server_context.operator_entries or self._nickname.lower() in chanid._users:
                                                        raw_messages.raw(self, "818", self._nickname, "%s OID :0" %
                                                                         (chanid.channelname))

                                                    raw_messages.raw(self, "819", self._nickname, chanid.channelname)
                                                else:
                                                    raw_messages.raw(self, "908", self._nickname)

                                            elif param[2].upper() == "CREATION":
                                                if len(param) == 3:
                                                    if isSecret(chanid,
                                                                "private") == False or self._nickname.lower() in server_context.operator_entries or self._nickname.lower() in chanid._users:
                                                        raw_messages.raw(self, "818", self._nickname,
                                                                         "%s Creation :%s" %
                                                                         (chanid.channelname, chanid._prop.creation))

                                                    raw_messages.raw(self, "819", self._nickname, chanid.channelname)
                                                else:
                                                    raw_messages.raw(self, "908", self._nickname)

                                            elif param[2].upper() == "ONJOIN":
                                                if len(param) == 3:
                                                    if chanid._prop.onjoin != "":
                                                        if isSecret(chanid,
                                                                    "private") == False or self._nickname.lower() in server_context.operator_entries or self._nickname.lower() in chanid._users:
                                                            raw_messages.raw(self, "818", self._nickname,
                                                                             "%s Onjoin :%s" %
                                                                             (chanid.channelname, chanid._prop.onjoin))

                                                    raw_messages.raw(self, "819", self._nickname, chanid.channelname)
                                                else:
                                                    chanid._prop._onmessage(
                                                        chanid, self, param[3],
                                                        strdata.split(" ", 3)[3],
                                                        "ONJOIN")

                                            elif param[2].upper() == "ONPART":
                                                if len(param) == 3:
                                                    if chanid._prop.onpart != "":
                                                        if isSecret(chanid,
                                                                    "private") == False or self._nickname.lower() in server_context.operator_entries or self._nickname.lower() in chanid._users:
                                                            raw_messages.raw(self, "818", self._nickname,
                                                                             "%s Onpart :%s" %
                                                                             (chanid.channelname, chanid._prop.onpart))

                                                    raw_messages.raw(self, "819", self._nickname, chanid.channelname)
                                                else:
                                                    chanid._prop._onmessage(
                                                        chanid, self, param[3],
                                                        strdata.split(" ", 3)[3],
                                                        "ONPART")

                                            elif param[2].upper() == "PICS":
                                                if len(param) == 3:
                                                    if isSecret(chanid,
                                                                "private") == False or self._nickname.lower() in server_context.operator_entries or self._nickname.lower() in chanid._users:
                                                        raw_messages.raw(self, "818", self._nickname, "%s PICS :0" %
                                                                         (chanid.channelname))

                                                    raw_messages.raw(self, "819", self._nickname, chanid.channelname)
                                                else:
                                                    raw_messages.raw(self, "908", self._nickname)

                                            else:
                                                raw_messages.raw(self, "905", self._nickname, chanid.channelname)
                                        else:
                                            raw_messages.raw(self, "403", self._nickname, param[1])
                                    else:
                                        raw_messages.raw(self, "461", self._nickname, param[0])

                                elif param[0] == "IDENTIFY":
                                    if chanid:
                                        okey = chanid._prop.ownerkey
                                        if self._nickname.lower() in chanid._users:
                                            isop = False
                                            isowner = False
                                            if okey == param[2]:
                                                if self._nickname.lower() not in chanid._owner:
                                                    chanid._owner.append(self._nickname.lower())
                                                if self._nickname.lower() in chanid._op:
                                                    chanid._op.remove(self._nickname.lower())
                                                    isop = True

                                                for each in chanid._users:
                                                    cid = server_context.nickname_to_client_mapping_entries[each]
                                                    if isop:
                                                        cid.send(":%s!%s@%s MODE %s -o %s\r\n" %
                                                                 (self._nickname, self._username, self._hostmask,
                                                                  chanid.channelname, self._nickname))

                                                    cid.send(":%s!%s@%s MODE %s +q %s\r\n" % (self._nickname,
                                                                                              self._username,
                                                                                              self._hostmask,
                                                                                              chanid.channelname,
                                                                                              self._nickname))

                                            elif chanid._prop.hostkey == param[2]:
                                                if self._nickname.lower() not in chanid._op:
                                                    chanid._op.append(self._nickname.lower())
                                                if self._nickname.lower() in chanid._owner:
                                                    chanid._owner.remove(self._nickname.lower())
                                                    isowner = True

                                                for each in chanid._users:
                                                    cid = server_context.nickname_to_client_mapping_entries[each]
                                                    if isowner:
                                                        cid.send(":%s!%s@%s MODE %s -q %s\r\n" %
                                                                 (self._nickname, self._username, self._hostmask,
                                                                  chanid.channelname, self._nickname))

                                                    cid.send(":%s!%s@%s MODE %s +o %s\r\n" % (self._nickname,
                                                                                              self._username,
                                                                                              self._hostmask,
                                                                                              chanid.channelname,
                                                                                              self._nickname))

                                            else:
                                                raw_messages.raw(self, "908", self._nickname)
                                        else:
                                            raw_messages.raw(self, "442", self._nickname, chanid.channelname)
                                    else:
                                        raw_messages.raw(self, "403", self._nickname, param[1])

                                elif param[0] == "AWAY":  # note : add to WHO, send to all on channel once
                                    try:
                                        if len(param) == 1:
                                            raw_messages.raw(self, "305", self._nickname)
                                            self._away = ""

                                        else:
                                            if strdata.split(" ", 1)[1].__len__() > 128:
                                                raw_messages.raw(self, "906", self._nickname, param[0])
                                            else:
                                                self._away = param[1]
                                                if self._away[0] == ":":
                                                    self._away = strdata.split(" ", 1)[1][1:]
                                                raw_messages.raw(self, "306", self._nickname)

                                    except:
                                        pass

                                elif param[0] == "KICK":
                                    if chanid:
                                        if self._nickname.lower() in chanid._users:
                                            iloop = 0

                                            if len(param) > 3:
                                                kickmsg = param[3]
                                                if kickmsg[0] == ":":
                                                    kickmsg = strdata.split(" ", 3)[3][1:]
                                            else:
                                                kickmsg = ""

                                            while iloop < len(param[2].split(",")):
                                                _kicknick = param[2].split(",")[iloop].lower()

                                                if _kicknick in server_context.nickname_to_client_mapping_entries:
                                                    if _kicknick in chanid._users:

                                                        if len(kickmsg) < 128:
                                                            cid = server_context.nickname_to_client_mapping_entries[
                                                                _kicknick]

                                                            if cid._nickname.lower() in server_context.operator_entries and self._nickname.lower() not in server_context.operator_entries:
                                                                raw_messages.raw(self, "481", self._nickname,
                                                                                 "Permission Denied - You're not a System operator")
                                                            elif cid._nickname.lower() in server_context.operator_entries and self._nickname.lower() in server_context.operator_entries:
                                                                opid = server_context.operator_entries[
                                                                    self._nickname.lower()]
                                                                sopid = server_context.operator_entries[
                                                                    cid._nickname.lower()]
                                                                if opid.operator_level >= sopid.operator_level:
                                                                    chanid.kick(self, cid._nickname, kickmsg)
                                                                else:
                                                                    raw_messages.raw(self, "481", self._nickname,
                                                                                     "Permission Denied - Insufficient oper priviledges")
                                                                # opers can kick other opers but they have to be equal levels or higher
                                                            else:
                                                                if self._nickname.lower() in chanid._op:
                                                                    if cid._nickname.lower() in chanid._owner or chanid.MODE_ownerkick:
                                                                        raw_messages.raw(self, "485", self._nickname,
                                                                                         chanid.channelname)
                                                                    else:
                                                                        chanid.kick(self, cid._nickname, kickmsg)

                                                                elif self._nickname.lower() in chanid._owner:
                                                                    chanid.kick(self, cid._nickname, kickmsg)
                                                                else:
                                                                    if cid._nickname.lower() in chanid._owner:
                                                                        raw_messages.raw(self, "485", self._nickname,
                                                                                         chanid.channelname)
                                                                    else:
                                                                        raw_messages.raw(self, "482", self._nickname,
                                                                                         chanid.channelname)
                                                        else:
                                                            raw_messages.raw(self, "906", self._nickname,
                                                                             chanid.channelname)
                                                    else:
                                                        raw_messages.raw(self, "441", self._nickname,
                                                                         chanid.channelname)
                                                else:
                                                    raw_messages.raw(self, "401", self._nickname, param[2])

                                                iloop += 1
                                        else:
                                            raw_messages.raw(self, "442", self._nickname, chanid.channelname)
                                    else:
                                        raw_messages.raw(self, "403", self._nickname, param[1])

                                elif param[0] == "CREATE":
                                    _sleep = "%.4f" % (random() / 9)

                                    if param[1].lower() in server_context.channel_entries:
                                        raw_messages.raw(self, "705", self._nickname, param[1])
                                    else:
                                        if len(self._channels) >= server_context.configuration.max_channels_per_user:
                                            raw_messages.raw(self, "405", self._nickname, param[1])
                                        else:
                                            if len(server_context.channel_entries) >= server_context.configuration.max_channels:
                                                raw_messages.raw(self, "710", self._nickname,
                                                                 server_context.configuration.max_channels)
                                            else:
                                                if len(param) == 2:
                                                    creationmodes = "0"
                                                else:
                                                    creationmodes = strdata.split(" ", 2)[2]

                                                if self._nickname.lower() in server_context.operator_entries:
                                                    creationmodes = creationmodes.replace("r", "").replace("e", "")
                                                else:
                                                    creationmodes = creationmodes.replace(
                                                        "r", "").replace(
                                                        "N", "").replace(
                                                        "A", "").replace(
                                                        "a", "").replace(
                                                        "d", "").replace(
                                                        "e", "")

                                                if param[1].lower() not in createmute:
                                                    createmute[param[1].lower()] = self
                                                    chanclass = Channel(
                                                        server_context,
                                                        raw_messages,
                                                        param[1],
                                                        self._nickname, creationmodes)  # create
                                                    if chanclass.channelname != "":
                                                        server_context.channel_entries[param[1].lower()] = chanclass

                                                    del createmute[param[1].lower()]
                                                else:
                                                    raw_messages.raw(self, "705", self._nickname, param[1])

                                elif param[0] == "JOIN":
                                    join_command.execute(self, param[1:])

                                elif param[0] == "FINDS":
                                    if chanid:
                                        raw_messages.raw(self, "613", self._nickname, chanid.channelname, "")
                                    else:
                                        raw_messages.raw(self, "702", self._nickname, param[1])

                                elif param[0] == "ISON":
                                    ison_nicknames = ""
                                    iloop = 1
                                    while iloop < len(strdata.split(" ")):

                                        t_nick = getUserOBJ(strdata.split(" ")[iloop].lower())
                                        if t_nick:
                                            ison_nicknames += " " + t_nick._nickname

                                        iloop += 1

                                    raw_messages.raw(self, "303", self._nickname, ison_nicknames[1:])

                                elif param[0] == "USERHOST" or param[0] == "USERIP":
                                    iloop = 1
                                    while iloop < len(strdata.split(" ")):
                                        t_nick = strdata.split(" ")[iloop].lower()
                                        if self._nickname.lower() == t_nick:
                                            raw_messages.raw(self, "302", self._nickname, self, True)
                                        else:
                                            cid = getUserOBJ(t_nick)
                                            if cid:
                                                boolShowIP = False
                                                if opid:
                                                    topid = 0
                                                    if t_nick in server_context.operator_entries:
                                                        _topid = server_context.operator_entries[t_nick]
                                                        topid = _topid.operator_level

                                                    if opid.operator_level > topid:
                                                        boolShowIP = True

                                                raw_messages.raw(self, "302", self._nickname,
                                                                 server_context.nickname_to_client_mapping_entries[
                                                                     t_nick], boolShowIP)
                                            else:
                                                self.send(
                                                    ":" + server_context.configuration.server_name + " 302 " + self._nickname + " :\r\n")

                                        iloop += 1

                                elif param[0] == "CREDITS":
                                    raw_messages.raw(self, "955", self._nickname)

                                elif param[0] == "TIME":
                                    raw_messages.raw(self, "391", self._nickname)

                                elif param[0] == "INFO":
                                    raw_messages.raw(self, "371", self._nickname, NetworkName)
                                    raw_messages.raw(self, "374", self._nickname)

                                elif param[0] == "GENPASS":
                                    secPass = ""
                                    while len(secPass) < 64:
                                        c = 33
                                        print(len(secPass))
                                        while c > 32:
                                            c = int(random() * 255)
                                            secPass += chr(c)
                                            break

                                    mkshapass = sha256(secPass.encode('utf-8'))

                                    self.send(
                                        ":" + server_context.configuration.server_name +
                                        " NOTICE GENPASS :*** Your securely generated password is: %s\r\n" %
                                        (mkshapass.hexdigest()))

                                elif param[0] == "WHO":
                                    _who = param[1].lower()
                                    if _who[0] == "#" or _who[0] == "%" or _who[0] == "&":
                                        if _who in server_context.channel_entries:
                                            chanid = server_context.channel_entries[_who]
                                            if isSecret(chanid, "private",
                                                        "hidden") == False or self._nickname.lower() in chanid._users or self._nickname.lower() in server_context.operator_entries:
                                                for each in chanid._users:
                                                    _whouser = server_context.nickname_to_client_mapping_entries[each]
                                                    whostring = Whouser(_whouser, chanid.channelname.lower(), self)
                                                    if whostring != "":
                                                        raw_messages.raw(self, "352", self._nickname, whostring)

                                    else:
                                        _whouser = getUserOBJ(_who)
                                        if _whouser:
                                            if _whouser._MODE_invisible == False or self._nickname.lower() in server_context.operator_entries or InChannel(
                                                    self, _whouser) or self == _whouser:
                                                whostring = Whouser(_whouser, "", self)
                                                if whostring != "":
                                                    raw_messages.raw(self, "352", self._nickname, whostring)

                                        else:
                                            useIP = True
                                            if self._nickname.lower() in server_context.operator_entries:
                                                useIP = False
                                            who_count = 0
                                            param[1] = access_helper.CreateMaskString(_who)
                                            for each in server_context.nickname_to_client_mapping_entries:
                                                nickid = server_context.nickname_to_client_mapping_entries[each]
                                                if access_helper.MatchAccess(param[1], nickid, useIP):
                                                    who_count += 1
                                                    if who_count == 20 and self._nickname.lower() not in server_context.operator_entries:
                                                        raw_messages.raw(self, "416", self._nickname, "WHO")
                                                        break

                                                    if nickid._MODE_invisible == False or self._nickname.lower() in server_context.operator_entries or InChannel(
                                                            self, nickid) or self == nickid:
                                                        whostring = Whouser(nickid, "", self)
                                                        if whostring != "":
                                                            raw_messages.raw(self, "352", self._nickname, whostring)

                                    raw_messages.raw(self, "315", self._nickname, param[1])

                                elif param[0] == "KILLMASK":
                                    if opid:
                                        if opid.operator_level >= 3:
                                            msg = param[2]
                                            if msg[0] == ":":
                                                msg = strdata.split(" ", 2)[2][1:]
                                            kill_count = 0
                                            param[1] = access_helper.CreateMaskString(param[1].lower())
                                            for each in server_context.nickname_to_client_mapping_entries:
                                                nickid = server_context.nickname_to_client_mapping_entries[each]
                                                if access_helper.MatchAccess(param[1], nickid):
                                                    kill_count += 1
                                                    if kill_count == 5:
                                                        raw_messages.raw(self, "416", self._nickname, "KILLMASK")
                                                        break
                                                    else:
                                                        if nickid._nickname.lower() in server_context.operator_entries:
                                                            opnickid = server_context.operator_entries[
                                                                nickid._nickname.lower()]
                                                            if opid.operator_level < opnickid.operator_level:
                                                                kill_count -= 1
                                                                continue

                                                        if nickid == self:
                                                            self.send(
                                                                ":" + server_context.configuration.server_name +
                                                                " NOTICE KILLMASK :*** You cannot kill yourself using KILLMASK\r\n")
                                                            kill_count -= 1
                                                            continue

                                                        nickid.quitmsg = " by " + self._nickname
                                                        SendComChan(nickid._channels, self, nickid,
                                                                    ":%s!%s@%s KILL %s :%s\r\n" %
                                                                    (self._nickname, self._username, self._hostmask,
                                                                     nickid._nickname, msg),
                                                                    msg)
                                                        nickid.quittype = -1
                                                        nickid.die = True

                                            sendAdminOpers(
                                                ":" + server_context.configuration.server_name + " NOTICE KILLMASK :*** " + self._nickname +
                                                " has just used KILLMASK to kill " + str(kill_count) +
                                                " connections with parameter \"" + param[1] + "\"\r\n")

                                        else:
                                            raw_messages.raw(self, "481", self._nickname,
                                                             "Permission Denied - You're not an Administrator")
                                    else:
                                        raw_messages.raw(self, "481", self._nickname,
                                                         "Permission Denied - You're not a System Operator")

                                elif param[0] == "LINKS":
                                    raw_messages.raw(self, "365", self._nickname)

                                elif param[0] == "WHOIS":
                                    iloop = 0
                                    while iloop < len(param[1].split(",")):
                                        _whois = param[1].split(",")[iloop]
                                        _whoisuser = getUserOBJ(_whois.lower())
                                        if _whoisuser:
                                            if _whoisuser._MODE_invisible == False or self._nickname.lower() in server_context.operator_entries or InChannel(
                                                    self, _whoisuser) or self == _whoisuser:
                                                raw_messages.raw(self, "311", self._nickname, _whoisuser)

                                                if self._nickname.lower() in server_context.operator_entries:
                                                    xopid = 0
                                                    opid = server_context.operator_entries[self._nickname.lower()]
                                                    if _whoisuser._nickname.lower() in server_context.operator_entries:
                                                        sopid = server_context.operator_entries[
                                                            _whoisuser._nickname.lower()]
                                                        xopid = sopid.operator_level

                                                    if opid.operator_level >= xopid:
                                                        raw_messages.raw(self, "378", self._nickname, _whoisuser)

                                                w_channels = ""
                                                cnick = self._nickname.lower()
                                                cid = getUserOBJ(cnick)
                                                for c in _whoisuser._channels:

                                                    if len(w_channels.split(" ")) == 10:
                                                        raw_messages.raw(self, "319", self._nickname, _whoisuser,
                                                                         w_channels[1:])

                                                        self.send(
                                                            ":" + server_context.configuration.server_name + " 319 " + self._nickname + " " + _whoisuser._nickname + " :" + w_channels[
                                                                                                                                                                            1:] + "\r\n")
                                                        w_channels = ""

                                                    chanid = getChannelOBJ(c.lower())
                                                    if chanid:
                                                        if isSecret(chanid, "private",
                                                                    "hidden") != True or cnick in chanid._users or getOperOBJ(
                                                            cnick):
                                                            if chanid.MODE_auditorium == False or isOp(
                                                                    cnick, chanid.channelname) or isOp(
                                                                _whoisuser._nickname, chanid.channelname):

                                                                # cid is me
                                                                # param4 is them, param 5 is the one who should be hidden if watching channel

                                                                if chanid.channelname in _whoisuser._watch and cid != _whoisuser:
                                                                    pass

                                                                elif _whoisuser._nickname.lower() in chanid._voice and _whoisuser._nickname.lower() not in chanid._op and _whoisuser._nickname.lower() not in chanid._owner:
                                                                    w_channels = w_channels + " +" + c

                                                                elif _whoisuser._nickname.lower() in chanid._op:
                                                                    w_channels = w_channels + " @" + c

                                                                elif _whoisuser._nickname.lower() in chanid._owner:
                                                                    if cid._IRCX:
                                                                        w_channels = w_channels + " ." + c
                                                                    else:
                                                                        w_channels = w_channels + " @" + c
                                                                else:
                                                                    w_channels = w_channels + " " + c

                                                if w_channels[1:] != "":
                                                    raw_messages.raw(self, "319", self._nickname, _whoisuser,
                                                                     w_channels[1:])

                                                if _whoisuser._MODE_register:
                                                    raw_messages.raw(self, "307", self._nickname, _whoisuser)
                                                if "z" in _whoisuser._MODE_:
                                                    raw_messages.raw(self, "316", self._nickname, _whoisuser._nickname)

                                                raw_messages.raw(self, "313", self._nickname, _whoisuser,
                                                                 _whoisuser._MODE_)
                                                raw_messages.raw(self, "320", self._nickname, _whoisuser)
                                                raw_messages.raw(self, "312", self._nickname, _whoisuser, NetworkName)
                                                if _whoisuser._away != "":
                                                    raw_messages.raw(self, "301", self._nickname, _whoisuser,
                                                                     _whoisuser._away)

                                                # TODO simplify all of this but simplify raws at the same time
                                                if _whoisuser._MODE_invisible != True or self._nickname in server_context.operator_entries:
                                                    raw_messages.raw(self, "317", self._nickname, _whoisuser)

                                        elif _whois.lower() == "nickserv":
                                            self.send(
                                                ":%s!%s@%s %s %s :\x02pyRCX nickname services\x02 (currently %d registered users)\r\n:%s!%s@%s %s %s :Type \x1F/nickserv HELP\x1F for more information\r\n" % (
                                                    "NickServ", "NickServ", NetworkName, "NOTICE", self._nickname,
                                                    len(server_context.nickserv_entries), "NickServ", "NickServ",
                                                    NetworkName,
                                                    "NOTICE", self._nickname))

                                        else:
                                            raw_messages.raw(self, "401", self._nickname, _whois)

                                        iloop += 1

                                    raw_messages.raw(self, "318", self._nickname, param[1])

                                elif param[0] == "ADMIN":
                                    raw_messages.raw(self, "258", self._nickname, ServerAdmin1)
                                    raw_messages.raw(self, "259", self._nickname, ServerAdmin2)

                                elif param[0] == "VERSION":
                                    raw_messages.raw(self, "256", self._nickname, NetworkName)
                                    raw_messages.raw(self, "257", self._nickname)

                                elif param[0] == "LUSERS":
                                    self._sendlusers()

                                elif param[0] == "MOTD":
                                    try:
                                        self._sendmotd(f"./{server_context.configuration.motd_config_file}")
                                    except:
                                        pass

                                elif param[0] == "DATA" or param[0] == "REPLY" or param[0] == "REQUEST":
                                    if self._MODE_gag:
                                        raw_messages.raw(self, "908", self._nickname)
                                    else:
                                        recips = []
                                        tag = param[2]
                                        data = param[3]
                                        if data[0] == ":":
                                            data = strdata.split(" ", 3)[3][1:]
                                        if data == "":
                                            raw_messages.raw(self, "412", self._nickname, param[0])
                                        else:
                                            iloop = 0
                                            while iloop < len(param[1].split(",")):
                                                _recipient = param[1].split(",")[iloop].lower()
                                                if _recipient.lower() not in recips:
                                                    recips.append(_recipient.lower())
                                                    if _recipient in server_context.nickname_to_client_mapping_entries:
                                                        nick = server_context.nickname_to_client_mapping_entries[
                                                            _recipient]
                                                        if self.selfaccess(nick):
                                                            nick.send(
                                                                ":%s!%s@%s %s %s %s :%s\r\n" %
                                                                (self._nickname, self._username, self._hostmask,
                                                                 param[0],
                                                                 _recipient, tag, data))

                                                    elif _recipient in server_context.channel_entries:
                                                        chan = server_context.channel_entries[_recipient]
                                                        if chan.isBanned(self) and chan.MODE_gagonban:
                                                            raw_messages.raw(self, "404", self._nickname, _recipient,
                                                                             "Cannot send to channel whilst banned")
                                                        else:
                                                            if self._nickname.lower() in chan._users or chan.MODE_externalmessages == False:
                                                                if chan.MODE_moderated == False or isOp(
                                                                        self._nickname.lower(),
                                                                        chan.channelname.lower()) or self._nickname.lower() in chan._voice:
                                                                    for each in chan._users:
                                                                        cid = \
                                                                            server_context.nickname_to_client_mapping_entries[
                                                                                each.lower()]
                                                                        if cid != self:  # x  ! x  @ x DATA target
                                                                            cid.send(":%s!%s@%s %s %s %s :%s\r\n" %
                                                                                     (self._nickname, self._username,
                                                                                      self._hostmask, param[0],
                                                                                      _recipient, tag, data))
                                                            else:
                                                                raw_messages.raw(self, "404", self._nickname,
                                                                                 _recipient, "Cannot send to channel")

                                                    else:
                                                        raw_messages.raw(self, "401", self._nickname, _recipient)

                                                iloop += 1

                                elif param[0] == "NICKSERV" or param[0] == "NS":  # ns register <email> <password>
                                    Nickserv_function(self, param)

                                else:
                                    if self._nickname == "":
                                        raw_messages.raw(self, "421", "*", param[0])
                                    else:
                                        raw_messages.raw(self, "421", self._nickname, param[0])

                            else:
                                raw_messages.raw(self, "451", self._nickname)

                    except IndexError:
                        raw_messages.raw(self, "461", self._nickname, param[0])

                    except Exception as e:
                        self.logger.error(traceback.format_exc())

        try:
            self.logger.info(f"Connection closed from '{self.details[0]}', {self._nickname} left the server")
            quit = ""

            if self.quittype == 0:
                quit = "Connection reset by peer"
            elif self.quittype == 1:
                quit = "Client exited"
            elif self.quittype == 2:
                quit = "Quit"
            elif self.quittype == 3:
                quit = "Ping timeout"
            elif self.quittype == 4:
                quit = "Flooding"
            elif self.quittype == -1:
                quit = "Killed" + self.quitmsg

            if self.quittype != -1:

                sendto = []
                for each in copy(self._channels):
                    try:
                        chan = server_context.channel_entries[each.lower()]
                        temp = dict(chan._users)
                        for n in temp:
                            if n in server_context.nickname_to_client_mapping_entries:
                                nick = server_context.nickname_to_client_mapping_entries[n.lower()]
                                if nick not in sendto and nick._nickname.lower() != self._nickname.lower():
                                    if self._nickname.lower() not in chan._watch:
                                        if chan.MODE_auditorium == False or isOp(
                                                nick._nickname, chan.channelname) or isOp(
                                            self._nickname, chan.channelname):
                                            sendto.append(nick)
                                            try:  # keep this here, some clients exit too fast
                                                if self.quittype == 0:
                                                    nick.send(
                                                        ":" + self._nickname + "!" + self._username + "@" + self._hostmask +
                                                        " QUIT :Connection reset by peer\r\n")
                                                elif self.quittype == 1:
                                                    nick.send(
                                                        ":" + self._nickname + "!" + self._username + "@" + self._hostmask +
                                                        " QUIT :Client exited\r\n")
                                                elif self.quittype == 2:
                                                    if self.quitmsg == "":
                                                        nick.send(
                                                            ":" + self._nickname + "!" + self._username + "@" + self._hostmask + " QUIT :Quit\r\n")
                                                    else:
                                                        nick.send(
                                                            ":" + self._nickname + "!" + self._username + "@" + self._hostmask + " QUIT :Quit: " + self.quitmsg + "\r\n")

                                                elif self.quittype == 3:
                                                    nick.send(
                                                        ":" + self._nickname + "!" + self._username + "@" + self._hostmask +
                                                        " QUIT :Ping timeout\r\n")

                                                elif self.quittype == 4:
                                                    nick.send(
                                                        ":" + self._nickname + "!" + self._username + "@" + self._hostmask +
                                                        " QUIT :Flooding\r\n")

                                                elif self.quittype == 5:
                                                    nick.send(
                                                        ":" + self._nickname + "!" + self._username + "@" + self._hostmask +
                                                        " QUIT :Nickname collision on server link\r\n")

                                            except:
                                                self.logger.debug(traceback.format_exc())
                    except:
                        self.logger.error(traceback.format_exc())

            temp_opers = dict(server_context.operator_entries)
            for each in temp_opers:
                opid = temp_opers[each.lower()]
                if opid.watchserver or opid.watchbans:
                    cid = server_context.nickname_to_client_mapping_entries[each.lower()]
                    try:
                        if self.quittype == 9:
                            if opid.watchbans:
                                cid.send(
                                    ":%s NOTICE %s :*** Notice -- User tried connecting but is banned (%s!%s@%s) [%s] \r\n" % (
                                        server_context.configuration.server_name, cid._nickname, self._nickname,
                                        self._username,
                                        self._hostmask,
                                        self.details[0]))
                        else:
                            if self._nickname != "" and opid.watchserver and quit != "":
                                cid.send(":%s NOTICE %s :*** Notice -- User Disconnected (%s!%s@%s) [%s] (%s)\r\n" % (
                                    server_context.configuration.server_name, cid._nickname, self._nickname,
                                    self._username,
                                    self._hostmask,
                                    self.details[0], quit))
                    except:
                        pass

            del temp_opers

            # remove all existance of this user in channels

        except:
            tuError = sys.exc_info()
            print(tuError)

        for each in copy(self._channels):
            try:
                server_context.channel_entries[each.lower()].quit(self._nickname)
            except:
                print("some channel error")

        if self in server_context.invisible_client_entries:
            server_context.invisible_client_entries.remove(self)

        if self._nickname.lower() in server_context.operator_entries:
            opid = server_context.operator_entries[self._nickname.lower()]
            opid.usage = False
            del server_context.operator_entries[self._nickname.lower()]

        if self._nickname.lower() in nickmute:
            del nickmute[self._nickname.lower()]  # log on affirmed, now nicknames can take over
        if self._nickname.lower() in server_context.nickname_to_client_mapping_entries:
            del server_context.nickname_to_client_mapping_entries[self._nickname.lower()]

        if self in connections:
            connections.remove(self)
        if self in server_context.unknown_connection_entries:
            server_context.unknown_connection_entries.remove(self)
        if self in temp_noopers:
            temp_noopers.remove(self)
        if self in server_context.secret_client_entries:
            server_context.secret_client_entries.remove(self)
        try:
            del self._watch, self._access
            self.close()
        except:
            pass

        del self


def Oper_function(self, param):
    if self._nickname.lower() in server_context.operator_entries:
        raw_messages.raw(self, "381", self._nickname, "You are already logged in")
    else:
        if str(len(param)) != str(3):
            raw_messages.raw(self, "461", self._nickname, param[0])
        else:
            if globals()["Noop"]:
                self.send(
                    ":" + server_context.configuration.server_name + " NOTICE SERVER :*** OPER has been disabled\r\n")
            else:
                _login = False
                for k in operlines:

                    if k.username == param[1] and k.password == param[2]:
                        if k.usage:
                            _login = "inuse"
                        else:
                            # opers dictionary file [ nickname ]
                            server_context.operator_entries[self._nickname.lower()] = k
                            _login = True

                if _login == True:
                    opid = server_context.operator_entries[self._nickname.lower()]
                    opid.guide = False
                    opid.hidden = False
                    opid.usage = True
                    opid.watchserver = False
                    opid.watchbans = False
                    if self._MODE_register == False:  # oper does not need to display whether he/she is oper
                        self._username = self._username[1:]

                    if "s" not in opid.flags:
                        self._hostmask = NetworkName
                        self._username = opid.username

                    self._sendmotd("./" + opid.filename)

                    self.send(
                        ":%s!%s@%s MODE %s +%s\r\n" %
                        (self._nickname, self._username, self._hostmask, self._nickname, opid.flags))

                    if "A" in opid.flags:
                        if "A" not in self._MODE_:
                            self._MODE_ = self._MODE_ + "aAoO"
                        opid.operator_level = 4
                        raw_messages.raw(self, "381", self._nickname, "You are now a Network Administrator")
                    elif "O" in opid.flags:
                        if "O" not in self._MODE_:
                            self._MODE_ = self._MODE_ + "aoO"
                        opid.operator_level = 3
                        raw_messages.raw(self, "381", self._nickname, "You are now an Administrator")
                    elif "a" in opid.flags:
                        if "a" not in self._MODE_:
                            self._MODE_ = self._MODE_ + "ao"
                        opid.operator_level = 2
                        raw_messages.raw(self, "381", self._nickname, "You are now a System Chat Manager")
                    elif "o" in opid.flags:
                        if "o" not in self._MODE_:
                            self._MODE_ = self._MODE_ + "o"
                        opid.operator_level = 1
                        raw_messages.raw(self, "381", self._nickname, "You are now a System Operator")

                    if "w" in opid.flags:
                        opid.watchserver = True
                    if "g" in opid.flags:
                        opid.guide = True
                    if "b" in opid.flags:
                        opid.watchbans = True
                    if "n" in opid.flags:
                        opid.watchnickserv = True
                    if "s" in opid.flags:
                        opid.hidden = True
                        return

                    sendWatchOpers(
                        "Notice -- Oper signed in (%s!%s@%s) [%s] \r\n" %
                        (self._nickname, self._username, self._hostmask, self.details[0]))

                elif _login == "inuse":
                    raw_messages.raw(self, "481", self._nickname, "Permission Denied - You're login is already in use")
                else:
                    sendWatchOpers(
                        "Notice -- Oper attempt failed (%s!%s@%s) [%s] \r\n" %
                        (self._nickname, self._username, self._hostmask, self.details[0]))

                    raw_messages.raw(self, "491", self._nickname, "No O-lines for your host")


def Nick_function(self: ClientConnecting, param):
    operator_level = 0
    if self._nickname.lower() in server_context.operator_entries:
        operator_level = server_context.operator_entries[self._nickname.lower()].operator_level

    if self._validate(param[1].replace(':', '')) and not filtering.filter(param[1].replace(':', ''), "nick",
                                                                          operator_level):

        if int((GetEpochTime() - self._nickflood) * 1000) <= 10000:
            self._nickamount += 1
        else:
            self._nickamount = 0

        self._nickflood = GetEpochTime()
        if self._nickamount == NickfloodAmount:  # nick changes
            self._nicklock = int(GetEpochTime() + NickfloodWait)
            self._nickamount = 0

        if self._nicklock == 0 or int(GetEpochTime()) >= self._nicklock or NickfloodAmount == 0 or NickfloodWait == 0:
            if self._nicklock != 0:
                self._nicklock = 0
                self._nickamount = 0

            found_deny = False
            found_grant = False

            schannels = copy(self._channels)

            for gagcheck in schannels:
                gagchan = server_context.channel_entries[gagcheck.lower()]
                if gagchan.MODE_gagonban and self._nickname.lower() in gagchan._users:
                    for each in gagchan.ChannelAccess:
                        ret = access_helper.MatchAccess(each._mask, self)
                        if ret == 1:
                            if each._level.upper() == "DENY":
                                found_deny = True
                            else:
                                found_grant = True
                                break

                    if found_deny == True and found_grant == False:
                        raw_messages.raw(self, "437", self._nickname, gagchan.channelname)
                        return

            temp_nick = param[1].replace(':', '')
            nickobj = getUserOBJ(temp_nick.lower())
            if nickobj:
                if nickobj == self:
                    if temp_nick == self._nickname:
                        pass
                    else:
                        self.send(
                            ":" + self._nickname + "!" + self._username + "@" + self._hostmask + " NICK :" + temp_nick + "\r\n")

                        sendto = []

                        for each in schannels:
                            chan = server_context.channel_entries[each.lower()]
                            copyn = dict(chan._users)
                            for copyn in chan._users:
                                nick = getUserOBJ(copyn)
                                if nick:
                                    if self._nickname.lower() not in chan._watch:
                                        if nick not in sendto and nick._nickname.lower() != self._nickname.lower():
                                            if chan.MODE_auditorium == False or isOp(
                                                    nick._nickname, chan.channelname) or isOp(
                                                self._nickname, chan.channelname):
                                                sendto.append(nick)
                                                nick.send(
                                                    ":" + self._nickname + "!" + self._username + "@" + self._hostmask +
                                                    " NICK :" + temp_nick + "\r\n")

                            chan.updateuser(self._nickname, temp_nick)

                        del sendto
                        self._nickname = temp_nick
                else:
                    raw_messages.raw(self, "433", self._nickname, param[1].replace(':', ''))
            else:

                # start of nickname checks

                if self._nickname.lower() in nickmute:
                    del nickmute[self._nickname.lower()]  # remove last name from nickmute if not removed yet

                if temp_nick.lower() not in nickmute:
                    nickmute[temp_nick.lower()] = self

                    if self._nickname != "":
                        self.send(
                            ":" + self._nickname + "!" + self._username + "@" + self._hostmask + " NICK :" + temp_nick + "\r\n")

                    sendto = []
                    for each in schannels:
                        chan = getChannelOBJ(each.lower())
                        i = 0
                        for copyn in chan._users:
                            nick = getUserOBJ(copyn)
                            if nick:
                                if self._nickname.lower() not in chan._watch:
                                    if nick not in sendto and nick._nickname.lower() != self._nickname.lower():
                                        if chan.MODE_auditorium == False or isOp(
                                                nick._nickname, chan.channelname) or isOp(
                                            self._nickname, chan.channelname):
                                            sendto.append(nick)
                                            nick.send(
                                                ":" + self._nickname + "!" + self._username + "@" + self._hostmask +
                                                " NICK :" + temp_nick + "\r\n")

                        chan.updateuser(self._nickname, temp_nick)

                    del sendto

                    if self._nickname.lower() in server_context.operator_entries:
                        server_context.operator_entries[temp_nick.lower()] = server_context.operator_entries[
                            self._nickname.lower()]
                        del server_context.operator_entries[self._nickname.lower()]

                    if self._nickname.lower() in server_context.nickname_to_client_mapping_entries:
                        del server_context.nickname_to_client_mapping_entries[self._nickname.lower()]

                    temp_oldnick = self._nickname

                    self._nickname = temp_nick

                    if self._welcome == True:
                        server_context.nickname_to_client_mapping_entries[
                            self._nickname.lower()] = self  # update entry from dictionary

                    if self._logoncheck():
                        self._sendwelcome()

                    if self._nickname.lower() in server_context.nickname_to_client_mapping_entries:

                        is_groupednick = False

                        for groupnicks in list(server_context.nickserv_entries.values()):
                            if self._nickname.lower() in groupnicks.grouped_nicknames or self._nickname.lower() == groupnicks._nickname.lower():
                                if temp_oldnick.lower() in groupnicks.grouped_nicknames or temp_oldnick.lower() == groupnicks._nickname.lower():
                                    if self._MODE_register:
                                        is_groupednick = True
                                        break

                        if self._MODE_register and is_groupednick == False:
                            self._MODE_register = False
                            self._MODE_.replace("r", "")
                            self.send(":%s!%s@%s MODE %s -r\r\n" %
                                      ("NickServ", "NickServ", NetworkName, self._nickname))
                            if self._username[
                                0] != PrefixChar and self._nickname.lower() not in server_context.operator_entries:
                                self._username = PrefixChar + self._username

                        if temp_nick.lower() in server_context.nickserv_entries or is_groupednick:
                            if self._MODE_register == False:
                                self.send(
                                    ":%s!%s@%s NOTICE %s :That nickname is owned by somebody else\r\n:%s!%s@%s NOTICE %s :If this is your nickname, you can identify with \x02/nickserv IDENTIFY \x1Fpassword\x1F\x02\r\n" % (
                                        "NickServ", "NickServ", NetworkName, self._nickname, "NickServ", "NickServ",
                                        NetworkName, self._nickname))

                else:
                    raw_messages.raw(self, "433", self._nickname, temp_nick)
                # end of after nickname checks

        else:
            raw_messages.raw(self, "438", self._nickname)
    else:
        raw_messages.raw(self, "432", self._nickname, param[1].replace(':', ''))


def Mode_function(self, param, strdata=""):
    if param[1][0] == "#" or param[1][0] == "%" or param[1][0] == "&":  # is a channel
        schannels = copy(server_context.channel_entries)
        if param[1].lower() in schannels:
            chan = schannels[param[1].lower()]
            if len(param) == 2:
                if isSecret(chan,
                            "private") == False or self._nickname.lower() in chan._users or self._nickname.lower() in server_context.operator_entries:
                    raw_messages.raw(self, "324", self._nickname, chan.channelname,
                                     chan.GetChannelModes(self._nickname.lower()))
                else:
                    self.send(
                        ":" + server_context.configuration.server_name + " 324 " + self._nickname + " " + chan.channelname + " +\r\n")
            else:
                if self._nickname.lower() in chan._users:
                    iloop = 0
                    paramloop = 2
                    param[2] = compilemodestr(param[2], True)
                    SetMode = True
                    Override = False
                    if self._nickname.lower() in server_context.operator_entries:
                        opid = server_context.operator_entries[self._nickname.lower()]
                        if opid.operator_level >= 3:
                            Override = True

                    while iloop < len(param[2]):
                        szModestr = ""
                        if param[2][iloop] == "+":
                            SetMode = True
                        elif param[2][iloop] == "-":
                            SetMode = False

                        elif chan.MODE_nomodechanges and self._nickname.lower() not in server_context.operator_entries and \
                                param[2][
                                    iloop] != "b" and param[2][iloop] != "q" and param[2][iloop] != "o" and param[2][
                            iloop] != "v":
                            raw_messages.raw(self, "908", self._nickname)

                        elif chan.MODE_ownersetmode and self._nickname.lower() not in chan._owner and param[2][
                            iloop] != "b":
                            raw_messages.raw(self, "485", self._nickname, chan.channelname)

                        elif param[2][iloop] == "b" or param[2][iloop] == "l" or param[2][iloop] == "k" or param[2][
                            iloop] == "q" or param[2][iloop] == "o" or param[2][iloop] == "v":
                            paramloop += 1  # now param[paramloop] is the parameter for each of the modes
                            try:
                                if self._nickname.lower() in chan._op or self._nickname.lower() in chan._owner or Override:
                                    if param[2][iloop] == "k":
                                        if SetMode:
                                            if len(param[paramloop]) <= 16:
                                                chan.MODE_key = str(param[paramloop])
                                                szModestr = ":%s!%s@%s MODE %s +k %s\r\n" % (
                                                    self._nickname, self._username, self._hostmask, chan.channelname,
                                                    param[paramloop])
                                            else:
                                                raw_messages.raw(self, "906", self._nickname,
                                                                 "MODE +%s" % (param[2][iloop]))
                                        else:
                                            chan.MODE_key = ""
                                            szModestr = ":%s!%s@%s MODE %s -k\r\n" % (
                                                self._nickname, self._username, self._hostmask, chan.channelname)

                                        for each in chan._users:
                                            cclientid = server_context.nickname_to_client_mapping_entries[each]
                                            cclientid.send(szModestr)

                                    elif param[2][iloop] == "l":
                                        if SetMode:
                                            if myint(param[paramloop]) <= 65535 and myint(param[paramloop]) > 0:
                                                chan.MODE_limit = True
                                                chan.MODE_limitamount = str(myint(param[paramloop]))
                                                szModestr = ":%s!%s@%s MODE %s +l %s\r\n" % (
                                                    self._nickname, self._username, self._hostmask, chan.channelname,
                                                    param[paramloop])
                                            else:
                                                raw_messages.raw(self, "906", self._nickname,
                                                                 "MODE +%s" % (param[2][iloop]))
                                        else:
                                            chan.MODE_limit = False
                                            szModestr = ":%s!%s@%s MODE %s -l\r\n" % (
                                                self._nickname, self._username, self._hostmask, chan.channelname)

                                        for each in chan._users:
                                            cclientid = server_context.nickname_to_client_mapping_entries[each]
                                            cclientid.send(szModestr)

                                    elif param[2][iloop] == "o":
                                        isowner = False
                                        if param[
                                            paramloop].lower() in server_context.nickname_to_client_mapping_entries:
                                            cid = server_context.nickname_to_client_mapping_entries[
                                                param[paramloop].lower()]
                                            if cid._nickname.lower() in chan._users:
                                                if self._nickname.lower() in chan._op and cid._nickname.lower() in chan._owner and cid != self:
                                                    raw_messages.raw(self, "485", self._nickname, chan.channelname)
                                                else:
                                                    opid = 0
                                                    copid = 0
                                                    operok = True
                                                    if cid._nickname.lower() in server_context.operator_entries:
                                                        copid = server_context.operator_entries[cid._nickname.lower()]
                                                    if self._nickname.lower() in server_context.operator_entries:
                                                        opid = server_context.operator_entries[self._nickname.lower()]

                                                    if copid != 0 and opid == 0:
                                                        raw_messages.raw(self, "908", self._nickname)
                                                    else:
                                                        if copid != 0 and opid != 0:
                                                            if opid.operator_level >= copid.operator_level:
                                                                operok = True
                                                            else:
                                                                operok = False

                                                        if operok:
                                                            if chan.MODE_auditorium:
                                                                for x in chan._users:
                                                                    if x.lower() in chan._op:
                                                                        pass
                                                                    elif x.lower() in chan._owner:
                                                                        pass
                                                                    else:
                                                                        nickid = \
                                                                            server_context.nickname_to_client_mapping_entries[
                                                                                x]
                                                                        if cid != nickid:
                                                                            if isOp(cid._nickname,
                                                                                    chan.channelname) == False and SetMode:
                                                                                cid.send(
                                                                                    ":%s!%s@%s JOIN :%s\r\n" %
                                                                                    (nickid._nickname, nickid._username,
                                                                                     nickid._hostmask,
                                                                                     chan.channelname))

                                                                            # if opnick is op and they are deoping then
                                                                            elif isOp(cid._nickname,
                                                                                      chan.channelname) and SetMode == False:
                                                                                cid.send(
                                                                                    ":%s!%s@%s PART :%s\r\n" %
                                                                                    (nickid._nickname, nickid._username,
                                                                                     nickid._hostmask,
                                                                                     chan.channelname))

                                                                            if isOp(nickid._nickname,
                                                                                    chan.channelname) == False and SetMode:
                                                                                if isOp(cid._nickname,
                                                                                        chan.channelname) == False:
                                                                                    nickid.send(
                                                                                        ":%s!%s@%s JOIN :%s\r\n" % (
                                                                                            cid._nickname,
                                                                                            cid._username,
                                                                                            cid._hostmask,
                                                                                            chan.channelname))

                                                                            elif isOp(nickid._nickname,
                                                                                      chan.channelname) == False and SetMode == False:
                                                                                if isOp(
                                                                                        cid._nickname,
                                                                                        chan.channelname):
                                                                                    nickid.send(
                                                                                        ":%s!%s@%s PART :%s\r\n" % (
                                                                                            cid._nickname,
                                                                                            cid._username,
                                                                                            cid._hostmask,
                                                                                            chan.channelname))

                                                            if cid._nickname.lower() in chan._owner:
                                                                isowner = True

                                                            for each in chan._users:
                                                                cclientid = \
                                                                    server_context.nickname_to_client_mapping_entries[
                                                                        each.lower()]
                                                                # if chan.MODE_auditorium == False or isOp(cclientid._nickname,chan.channelname) or cid == cclientid:
                                                                if chan.MODE_auditorium and SetMode == False and isOp(
                                                                        cclientid._nickname, chan.channelname) == False:
                                                                    pass
                                                                else:
                                                                    if isowner and cclientid._IRCX:
                                                                        cclientid.send(
                                                                            ":%s!%s@%s MODE %s -q %s\r\n" % (
                                                                                self._nickname, self._username,
                                                                                self._hostmask, chan.channelname,
                                                                                cid._nickname))

                                                                    cclientid.send(
                                                                        ":%s!%s@%s MODE %s %so %s\r\n" %
                                                                        (self._nickname, self._username, self._hostmask,
                                                                         chan.channelname, iif(SetMode, "+", "-"),
                                                                         cid._nickname))

                                                            if isowner:
                                                                chan._owner.remove(cid._nickname.lower())
                                                            if SetMode:
                                                                if cid._nickname.lower() not in chan._op:
                                                                    # channel now knows that cid is a channel operator
                                                                    chan._op.append(cid._nickname.lower())
                                                            else:
                                                                if cid._nickname.lower() in chan._op:
                                                                    chan._op.remove(cid._nickname.lower())

                                            else:
                                                raw_messages.raw(self, "441", self._nickname, chan.channelname)
                                        else:
                                            raw_messages.raw(self, "401", self._nickname, param[paramloop])

                                    elif param[2][iloop] == "v":
                                        if param[
                                            paramloop].lower() in server_context.nickname_to_client_mapping_entries:
                                            cid = server_context.nickname_to_client_mapping_entries[
                                                param[paramloop].lower()]
                                            if cid._nickname.lower() in chan._users:
                                                if self._nickname.lower() in chan._op and cid._nickname.lower() in chan._owner and SetMode == False:
                                                    raw_messages.raw(self, "485", self._nickname, chan.channelname)
                                                else:
                                                    if SetMode:
                                                        if cid._nickname.lower() not in chan._voice:
                                                            # channel now knows that cid is a channel voice
                                                            chan._voice.append(cid._nickname.lower())
                                                    else:
                                                        if cid._nickname.lower() in chan._voice:
                                                            chan._voice.remove(cid._nickname.lower())

                                                    for each in chan._users:
                                                        cclientid = server_context.nickname_to_client_mapping_entries[
                                                            each.lower()]
                                                        if chan.MODE_auditorium == False or isOp(
                                                                cclientid._nickname,
                                                                chan.channelname) or cclientid == cid:
                                                            cclientid.send(
                                                                ":%s!%s@%s MODE %s %sv %s\r\n" %
                                                                (self._nickname, self._username, self._hostmask, chan.
                                                                 channelname, iif(SetMode, "+", "-"),
                                                                 cid._nickname))
                                            else:
                                                raw_messages.raw(self, "441", self._nickname, chan.channelname)
                                        else:
                                            raw_messages.raw(self, "401", self._nickname, param[paramloop])

                                    elif param[2][iloop] == "q" and self._IRCX:
                                        if chan.MODE_noircx:
                                            raw_messages.raw(self, "997", self._nickname, chan.channelname,
                                                             "MODE %sq" % (iif(SetMode, "+", "-")))
                                        else:
                                            isop = False
                                            if self._nickname.lower() in chan._owner or Override:
                                                if param[
                                                    paramloop].lower() in server_context.nickname_to_client_mapping_entries:
                                                    cid = server_context.nickname_to_client_mapping_entries[
                                                        param[paramloop].lower()]
                                                    if cid._nickname.lower() in chan._users:
                                                        opid = 0
                                                        copid = 0
                                                        operok = True
                                                        if cid._nickname.lower() in server_context.operator_entries:
                                                            copid = server_context.operator_entries[
                                                                cid._nickname.lower()]
                                                            copid = copid.operator_level

                                                        if self._nickname.lower() in server_context.operator_entries:
                                                            opid = server_context.operator_entries[
                                                                self._nickname.lower()]
                                                            opid = opid.operator_level

                                                        if copid != 0 and opid == 0:
                                                            raw_messages.raw(self, "908", self._nickname)
                                                        else:
                                                            if copid != 0 and opid != 0:
                                                                if opid >= copid:
                                                                    operok = True
                                                                else:
                                                                    operok = False
                                                                    raw_messages.raw(self, "908", self._nickname)

                                                            if operok:
                                                                if chan.MODE_auditorium:
                                                                    for x in chan._users:
                                                                        if x.lower() in chan._op:
                                                                            pass
                                                                        elif x.lower() in chan._owner:
                                                                            pass
                                                                        else:
                                                                            nickid = \
                                                                                server_context.nickname_to_client_mapping_entries[
                                                                                    x]
                                                                            if cid != nickid:
                                                                                if isOp(cid._nickname,
                                                                                        chan.channelname) == False and SetMode:
                                                                                    cid.send(
                                                                                        ":%s!%s@%s JOIN :%s\r\n" % (
                                                                                            nickid._nickname,
                                                                                            nickid._username,
                                                                                            nickid._hostmask,
                                                                                            chan.channelname))

                                                                                # if opnick is op and they are deoping then
                                                                                elif isOp(cid._nickname,
                                                                                          chan.channelname) and SetMode == False:
                                                                                    cid.send(
                                                                                        ":%s!%s@%s PART :%s\r\n" % (
                                                                                            nickid._nickname,
                                                                                            nickid._username,
                                                                                            nickid._hostmask,
                                                                                            chan.channelname))

                                                                                if isOp(nickid._nickname,
                                                                                        chan.channelname) == False and SetMode:
                                                                                    if isOp(cid._nickname,
                                                                                            chan.channelname) == False:
                                                                                        nickid.send(
                                                                                            ":%s!%s@%s JOIN :%s\r\n" % (
                                                                                                cid._nickname,
                                                                                                cid._username,
                                                                                                cid._hostmask,
                                                                                                chan.channelname))

                                                                                elif isOp(nickid._nickname,
                                                                                          chan.channelname) == False and SetMode == False:
                                                                                    if isOp(cid._nickname,
                                                                                            chan.channelname):
                                                                                        nickid.send(
                                                                                            ":%s!%s@%s PART :%s\r\n" % (
                                                                                                cid._nickname,
                                                                                                cid._username,
                                                                                                cid._hostmask,
                                                                                                chan.channelname))

                                                                if cid._nickname.lower() in chan._op:
                                                                    isop = True

                                                                for each in chan._users:
                                                                    cclientid = \
                                                                        server_context.nickname_to_client_mapping_entries[
                                                                            each]
                                                                    if chan.MODE_auditorium and SetMode == False and isOp(
                                                                            cclientid._nickname,
                                                                            chan.channelname) == False:
                                                                        pass
                                                                    else:
                                                                        if isop and cclientid._IRCX:
                                                                            cclientid.send(
                                                                                ":%s!%s@%s MODE %s -o %s\r\n" % (
                                                                                    self._nickname, self._username,
                                                                                    self._hostmask, chan.channelname,
                                                                                    cid._nickname))

                                                                        if cclientid._IRCX:
                                                                            cclientid.send(
                                                                                ":%s!%s@%s MODE %s %sq %s\r\n" %
                                                                                (self._nickname, self._username, self.
                                                                                 _hostmask, chan.channelname,
                                                                                 iif(SetMode, "+", "-"),
                                                                                 cid._nickname))
                                                                        else:
                                                                            cclientid.send(
                                                                                ":%s!%s@%s MODE %s %so %s\r\n" %
                                                                                (self._nickname, self._username, self.
                                                                                 _hostmask, chan.channelname,
                                                                                 iif(SetMode, "+", "-"),
                                                                                 cid._nickname))

                                                                if isop:
                                                                    chan._op.remove(cid._nickname.lower())
                                                                if SetMode:
                                                                    if cid._nickname.lower() not in chan._owner:
                                                                        # channel now knows that cid is a channel operator
                                                                        chan._owner.append(cid._nickname.lower())
                                                                else:
                                                                    if cid._nickname.lower() in chan._owner:
                                                                        chan._owner.remove(cid._nickname.lower())

                                                                    if cid._nickname.lower() in chan._op:
                                                                        chan._op.remove(cid._nickname.lower())
                                                    else:
                                                        raw_messages.raw(self, "441", self._nickname, chan.channelname)
                                                else:
                                                    raw_messages.raw(self, "401", self._nickname, param[paramloop])
                                            else:
                                                raw_messages.raw(self, "485", self._nickname, chan.channelname)

                                    elif param[2][iloop] == "b":
                                        _rec = ""
                                        if SetMode:
                                            _mask = access_helper.CreateMaskString(param[paramloop].lower())
                                            if _mask == -1:
                                                raw_messages.raw(self, "906", self._nickname, param[paramloop].lower())
                                            elif _mask == -2:
                                                raw_messages.raw(self, "909", self._nickname)
                                            else:
                                                tag, exp = "", 0
                                                _rec = access_helper.AddRecord(self, chan.channelname,
                                                                               "DENY", _mask, exp, tag)
                                                if _rec == 1:
                                                    stringinf = "%s %s %s %d %s %s" % (
                                                        chan.channelname, "DENY", _mask, exp, self._hostmask, tag)
                                                    raw_messages.raw(self, "801", self._nickname, stringinf)

                                                elif _rec == -1:
                                                    raw_messages.raw(self, "914", self._nickname, chan.channelname)

                                                elif _rec == -2:
                                                    raw_messages.raw(self, "913", self._nickname, chan.channelname)
                                                else:
                                                    pass
                                        else:
                                            _mask = access_helper.CreateMaskString(param[paramloop].lower())
                                            if _mask == -1:
                                                raw_messages.raw(self, "906", self._nickname, param[paramloop].lower())
                                            elif _mask == -2:
                                                raw_messages.raw(self, "909", self._nickname)
                                            else:
                                                _rec = access_helper.DelRecord(self, chan.channelname, "DENY", _mask)
                                                if _rec == 1:
                                                    stringinf = "%s %s %s" % (chan.channelname, "DENY", _mask)
                                                    raw_messages.raw(self, "802", self._nickname, stringinf)

                                                elif _rec == -1:
                                                    raw_messages.raw(self, "915", self._nickname, chan.channelname)
                                                elif _rec == -2:
                                                    raw_messages.raw(self, "913", self._nickname, chan.channelname)
                                        if _rec == 1:
                                            for each in chan._users:
                                                cclientid = server_context.nickname_to_client_mapping_entries[each]
                                                cclientid.send(
                                                    ":%s!%s@%s MODE %s %sb %s\r\n" %
                                                    (self._nickname, self._username, self._hostmask, chan.channelname,
                                                     iif(SetMode, "+", "-"),
                                                     _mask))

                                else:
                                    raw_messages.raw(self, "482", self._nickname, chan.channelname)

                            except IndexError:
                                if param[2][iloop] == "b" and SetMode:
                                    for each in chan.ChannelAccess:
                                        if each._level == "DENY":
                                            if each._deleteafterexpire == False:
                                                exp = 0
                                            else:
                                                exp = (each._expires - int(GetEpochTime())) / 60
                                                if exp < 1:
                                                    exp = 0

                                            raw_messages.raw(self, "367", self._nickname, chan.channelname,
                                                             each._mask, each._setby, str(each._setat))

                                    raw_messages.raw(self, "368", self._nickname, chan.channelname)
                                else:
                                    raw_messages.raw(self, "461", self._nickname, "MODE %s%s" %
                                                     (iif(SetMode, "+", "-"), param[2][iloop]))

                        elif param[2][iloop] == "X":
                            if chan.MODE_noircx:
                                raw_messages.raw(self, "997", self._nickname, chan.channelname,
                                                 "MODE %sX" % (iif(SetMode, "+", "-")))
                            else:
                                if self._nickname.lower() in chan._owner or Override:
                                    if SetMode:
                                        chan.MODE_ownersetaccess = True
                                    else:
                                        chan.MODE_ownersetaccess = False

                                    for each in chan._users:
                                        cclientid = server_context.nickname_to_client_mapping_entries[each]
                                        cclientid.send(
                                            ":%s!%s@%s MODE %s %s%s\r\n" %
                                            (self._nickname, self._username, self._hostmask, chan.channelname,
                                             iif(SetMode, "+", "-"),
                                             param[2][iloop]))
                                else:
                                    raw_messages.raw(self, "485", self._nickname, chan.channelname)

                        elif param[2][iloop] == "Z":
                            raw_messages.raw(self, "472", self._nickname, "Z")

                        elif param[2][iloop] == "M":
                            if chan.MODE_noircx:
                                raw_messages.raw(self, "997", self._nickname, chan.channelname,
                                                 "MODE %sM" % (iif(SetMode, "+", "-")))
                            else:
                                if self._nickname.lower() in chan._owner or Override:
                                    if SetMode:
                                        chan.MODE_ownersetmode = True
                                    else:
                                        chan.MODE_ownersetmode = False

                                    for each in chan._users:
                                        cclientid = server_context.nickname_to_client_mapping_entries[each]
                                        cclientid.send(
                                            ":%s!%s@%s MODE %s %s%s\r\n" %
                                            (self._nickname, self._username, self._hostmask, chan.channelname,
                                             iif(SetMode, "+", "-"),
                                             param[2][iloop]))
                                else:
                                    raw_messages.raw(self, "485", self._nickname, chan.channelname)

                        elif param[2][iloop] == "P":
                            if chan.MODE_noircx:
                                raw_messages.raw(self, "997", self._nickname, chan.channelname,
                                                 "MODE %sP" % (iif(SetMode, "+", "-")))
                            else:
                                if self._nickname.lower() in chan._owner or Override:
                                    if SetMode:
                                        chan.MODE_ownersetprop = True
                                    else:
                                        chan.MODE_ownersetprop = False

                                    for each in chan._users:
                                        cclientid = server_context.nickname_to_client_mapping_entries[each]
                                        cclientid.send(
                                            ":%s!%s@%s MODE %s %s%s\r\n" %
                                            (self._nickname, self._username, self._hostmask, chan.channelname,
                                             iif(SetMode, "+", "-"),
                                             param[2][iloop]))
                                else:
                                    raw_messages.raw(self, "485", self._nickname, chan.channelname)

                        elif param[2][iloop] == "T":
                            if chan.MODE_noircx:
                                raw_messages.raw(self, "997", self._nickname, chan.channelname,
                                                 "MODE %sT" % (iif(SetMode, "+", "-")))
                            else:
                                if self._nickname.lower() in chan._owner or Override:
                                    unsetother = ""
                                    if SetMode:
                                        if chan.MODE_optopic:
                                            unsetother = "-t"
                                            chan.MODE_optopic = False

                                        chan.MODE_ownertopic = True
                                    else:
                                        chan.MODE_ownertopic = False

                                    for each in chan._users:
                                        cclientid = server_context.nickname_to_client_mapping_entries[each]
                                        cclientid.send(
                                            ":%s!%s@%s MODE %s %s%s%s\r\n" %
                                            (self._nickname, self._username, self._hostmask, chan.channelname,
                                             unsetother, iif(SetMode, "+", "-"),
                                             param[2][iloop]))
                                else:
                                    raw_messages.raw(self, "485", self._nickname, chan.channelname)

                        elif param[2][iloop] == "r" or param[2][iloop] == "x" or param[2][iloop] == "S" or param[2][
                            iloop] == "e":
                            raw_messages.raw(self, "468", self._nickname, chan.channelname)

                        elif param[2][iloop] == "Q":
                            if chan.MODE_noircx:
                                raw_messages.raw(self, "997", self._nickname, chan.channelname,
                                                 "MODE %sQ" % (iif(SetMode, "+", "-")))
                            else:
                                if self._nickname.lower() in chan._owner or Override:
                                    if SetMode:
                                        chan.MODE_ownerkick = True
                                    else:
                                        chan.MODE_ownerkick = False

                                    for each in chan._users:
                                        cclientid = server_context.nickname_to_client_mapping_entries[each]
                                        cclientid.send(
                                            ":%s!%s@%s MODE %s %s%s\r\n" %
                                            (self._nickname, self._username, self._hostmask, chan.channelname,
                                             iif(SetMode, "+", "-"),
                                             param[2][iloop]))
                                else:
                                    raw_messages.raw(self, "485", self._nickname, chan.channelname)

                        elif param[2][iloop] == "d":
                            if self._nickname.lower() in server_context.operator_entries:
                                if SetMode:
                                    chan.MODE_createclone = True
                                else:
                                    chan.MODE_createclone = False

                                for each in chan._users:
                                    cclientid = server_context.nickname_to_client_mapping_entries[each]
                                    cclientid.send(
                                        ":%s!%s@%s MODE %s %s%s\r\n" %
                                        (self._nickname, self._username, self._hostmask, chan.channelname,
                                         iif(SetMode, "+", "-"),
                                         param[2][iloop]))
                            else:
                                raw_messages.raw(self, "481", self._nickname,
                                                 "Permission Denied - You're not a System operator")

                        elif param[2][iloop] == "a":
                            if self._nickname.lower() in server_context.operator_entries:

                                if SetMode:
                                    chan.MODE_authenticatedclients = True
                                else:
                                    chan.MODE_authenticatedclients = False

                                for each in chan._users:
                                    cclientid = server_context.nickname_to_client_mapping_entries[each]
                                    cclientid.send(
                                        ":%s!%s@%s MODE %s %s%s\r\n" %
                                        (self._nickname, self._username, self._hostmask, chan.channelname,
                                         iif(SetMode, "+", "-"),
                                         param[2][iloop]))
                            else:
                                raw_messages.raw(self, "481", self._nickname,
                                                 "Permission Denied - You're not a System operator")

                        elif param[2][iloop] == "N":  # Service channel
                            if self._nickname.lower() in server_context.operator_entries:

                                if SetMode:
                                    chan.MODE_servicechan = True
                                else:
                                    chan.MODE_servicechan = False

                                for each in chan._users:
                                    cclientid = server_context.nickname_to_client_mapping_entries[each]
                                    cclientid.send(
                                        ":%s!%s@%s MODE %s %s%s\r\n" %
                                        (self._nickname, self._username, self._hostmask, chan.channelname,
                                         iif(SetMode, "+", "-"),
                                         param[2][iloop]))
                            else:
                                raw_messages.raw(self, "481", self._nickname,
                                                 "Permission Denied - You're not a System operator")

                        elif param[2][iloop] == "A":  # Service channel
                            if self._nickname.lower() in server_context.operator_entries:
                                opid = server_context.operator_entries[self._nickname.lower()]
                                if opid.operator_level >= 3:
                                    if SetMode:
                                        chan.MODE_Adminonly = True
                                    else:
                                        chan.MODE_Adminonly = False

                                    for each in chan._users:
                                        cclientid = server_context.nickname_to_client_mapping_entries[each]
                                        cclientid.send(
                                            ":%s!%s@%s MODE %s %s%s\r\n" %
                                            (self._nickname, self._username, self._hostmask, chan.channelname,
                                             iif(SetMode, "+", "-"),
                                             param[2][iloop]))
                                else:
                                    raw_messages.raw(self, "481", self._nickname,
                                                     "Permission Denied - You're not an Administrator")
                            else:
                                raw_messages.raw(self, "481", self._nickname,
                                                 "Permission Denied - You're not a System operator")
                        else:
                            if self._nickname.lower() in chan._op or self._nickname.lower() in chan._owner or Override:
                                if param[2][iloop] == "c":
                                    unsetother = ""
                                    if SetMode:
                                        if chan.MODE_stripcolour:
                                            unsetother = "-C"
                                        chan.MODE_nocolour = True
                                        chan.MODE_stripcolour = False
                                    else:
                                        chan.MODE_nocolour = False

                                    szModestr = ":%s!%s@%s MODE %s %s%s%s\r\n" % (
                                        self._nickname, self._username, self._hostmask, chan.channelname, unsetother,
                                        iif(SetMode, "+", "-"),
                                        param[2][iloop])

                                elif param[2][iloop] == "C":
                                    unsetother = ""
                                    if SetMode:
                                        if chan.MODE_nocolour:
                                            unsetother = "-c"
                                        chan.MODE_stripcolour = True
                                        chan.MODE_nocolour = False
                                    else:
                                        chan.MODE_stripcolour = False

                                    szModestr = ":%s!%s@%s MODE %s %s%s%s\r\n" % (
                                        self._nickname, self._username, self._hostmask, chan.channelname, unsetother,
                                        iif(SetMode, "+", "-"),
                                        param[2][iloop])

                                elif param[2][iloop] == "e":
                                    if SetMode:
                                        raw_messages.raw(self, "472", self._nickname, "e")

                                elif param[2][iloop] == "f":
                                    if SetMode:
                                        chan.MODE_profanity = True
                                    else:
                                        chan.MODE_profanity = False
                                    szModestr = ":%s!%s@%s MODE %s %s%s\r\n" % (
                                        self._nickname, self._username, self._hostmask, chan.channelname,
                                        iif(SetMode, "+", "-"),
                                        param[2][iloop])

                                elif param[2][iloop] == "G":
                                    if SetMode:
                                        chan.MODE_gagonban = True
                                    else:
                                        chan.MODE_gagonban = False
                                    szModestr = ":%s!%s@%s MODE %s %s%s\r\n" % (
                                        self._nickname, self._username, self._hostmask, chan.channelname,
                                        iif(SetMode, "+", "-"),
                                        param[2][iloop])

                                elif param[2][iloop] == "h":
                                    extra = ""
                                    if SetMode:
                                        chan.MODE_hidden = True
                                        if chan.MODE_secret:
                                            extra = "-s"
                                            chan.MODE_secret = False

                                        elif chan.MODE_private:
                                            extra = "-p"
                                            chan.MODE_private = False
                                    else:
                                        chan.MODE_hidden = False

                                    szModestr = ":%s!%s@%s MODE %s %s%s%s\r\n" % (
                                        self._nickname, self._username, self._hostmask, chan.channelname, extra,
                                        iif(SetMode, "+", "-"),
                                        param[2][iloop])

                                elif param[2][iloop] == "i":
                                    if SetMode:
                                        chan.MODE_inviteonly = True
                                    else:
                                        chan.MODE_inviteonly = False
                                    szModestr = ":%s!%s@%s MODE %s %s%s\r\n" % (
                                        self._nickname, self._username, self._hostmask, chan.channelname,
                                        iif(SetMode, "+", "-"),
                                        param[2][iloop])

                                elif param[2][iloop] == "I":
                                    if SetMode:
                                        raw_messages.raw(self, "472", self._nickname, "I")

                                elif param[2][iloop] == "K":
                                    if SetMode:
                                        chan.MODE_noclones = True
                                    else:
                                        chan.MODE_noclones = False

                                    szModestr = ":%s!%s@%s MODE %s %s%s\r\n" % (
                                        self._nickname, self._username, self._hostmask, chan.channelname,
                                        iif(SetMode, "+", "-"),
                                        param[2][iloop])

                                elif param[2][iloop] == "m":
                                    if SetMode:
                                        chan.MODE_moderated = True
                                    else:
                                        chan.MODE_moderated = False
                                    szModestr = ":%s!%s@%s MODE %s %s%s\r\n" % (
                                        self._nickname, self._username, self._hostmask, chan.channelname,
                                        iif(SetMode, "+", "-"),
                                        param[2][iloop])

                                elif param[2][iloop] == "n":
                                    if SetMode:
                                        chan.MODE_externalmessages = True
                                    else:
                                        chan.MODE_externalmessages = False

                                    szModestr = ":%s!%s@%s MODE %s %s%s\r\n" % (
                                        self._nickname, self._username, self._hostmask, chan.channelname,
                                        iif(SetMode, "+", "-"),
                                        param[2][iloop])

                                elif param[2][iloop] == "p":
                                    extra = ""
                                    if SetMode:
                                        chan.MODE_private = True
                                        if chan.MODE_hidden:
                                            extra = "-h"
                                            chan.MODE_hidden = False

                                        elif chan.MODE_secret:
                                            extra = "-s"
                                            chan.MODE_secret = False
                                    else:
                                        chan.MODE_private = False

                                    szModestr = ":%s!%s@%s MODE %s %s%s%s\r\n" % (
                                        self._nickname, self._username, self._hostmask, chan.channelname, extra,
                                        iif(SetMode, "+", "-"),
                                        param[2][iloop])

                                elif param[2][iloop] == "R":
                                    if SetMode:
                                        chan.MODE_registeredonly = True
                                    else:
                                        chan.MODE_registeredonly = False
                                    szModestr = ":%s!%s@%s MODE %s %s%s\r\n" % (
                                        self._nickname, self._username, self._hostmask, chan.channelname,
                                        iif(SetMode, "+", "-"),
                                        param[2][iloop])

                                elif param[2][iloop] == "s":
                                    extra = ""
                                    if SetMode:
                                        if chan.MODE_hidden:
                                            extra = "-h"
                                            chan.MODE_hidden = False

                                        elif chan.MODE_private:
                                            extra = "-p"
                                            chan.MODE_private = False

                                        chan.MODE_secret = True
                                    else:
                                        chan.MODE_secret = False
                                    szModestr = ":%s!%s@%s MODE %s %s%s%s\r\n" % (
                                        self._nickname, self._username, self._hostmask, chan.channelname, extra,
                                        iif(SetMode, "+", "-"),
                                        param[2][iloop])

                                elif param[2][iloop] == "t":
                                    oTopic = True
                                    unsetother = ""
                                    if SetMode:
                                        if chan.MODE_ownertopic:
                                            if self._nickname.lower() in chan._owner:
                                                unsetother = "-T"
                                                chan.MODE_ownertopic = False
                                            else:
                                                oTopic = False
                                                raw_messages.raw(self, "485", self._nickname, chan.channelname)

                                        chan.MODE_optopic = True
                                    else:
                                        chan.MODE_optopic = False
                                    if oTopic:
                                        szModestr = ":%s!%s@%s MODE %s %s%s%s\r\n" % (
                                            self._nickname, self._username, self._hostmask, chan.channelname,
                                            unsetother, iif(SetMode, "+", "-"),
                                            param[2][iloop])

                                elif param[2][iloop] == "u":
                                    if SetMode:
                                        chan.MODE_knock = True
                                    else:
                                        chan.MODE_knock = False

                                    szModestr = ":%s!%s@%s MODE %s %s%s\r\n" % (
                                        self._nickname, self._username, self._hostmask, chan.channelname,
                                        iif(SetMode, "+", "-"),
                                        param[2][iloop])

                                elif param[2][iloop] == "w":
                                    if SetMode:
                                        chan.MODE_whisper = True
                                    else:
                                        chan.MODE_whisper = False
                                    szModestr = ":%s!%s@%s MODE %s %s%s\r\n" % (
                                        self._nickname, self._username, self._hostmask, chan.channelname,
                                        iif(SetMode, "+", "-"),
                                        param[2][iloop])

                                if szModestr:
                                    for each in chan._users:
                                        cclientid = server_context.nickname_to_client_mapping_entries[each]
                                        cclientid.send(szModestr)

                            else:
                                raw_messages.raw(self, "482", self._nickname, chan.channelname)

                            if szModestr == "":
                                raw_messages.raw(self, "501", self._nickname)

                        iloop += 1

                    time.sleep(0.3)
                else:
                    raw_messages.raw(self, "442", self._nickname, chan.channelname)
        else:
            raw_messages.raw(self, "403", self._nickname, param[1])

    elif param[1].lower() == self._nickname.lower():

        if len(param) == 2:
            raw_messages.raw(self, "221", self._nickname, self._MODE_)
        else:
            iloop = 0
            param[2] = compilemodestr(param[2])
            SetMode = True
            while iloop < len(param[2]):
                if param[2][iloop] == "+":
                    SetMode = True

                elif param[2][iloop] == "-":
                    SetMode = False

                elif param[2][iloop] == "i":
                    if SetMode:
                        if "i" not in self._MODE_:
                            self._MODE_ = self._MODE_ + "i"
                        if self not in server_context.invisible_client_entries:
                            server_context.invisible_client_entries.add(self)
                        self._MODE_invisible = True

                    else:
                        self._MODE_ = self._MODE_.replace("i", "")
                        if self in server_context.invisible_client_entries:
                            server_context.invisible_client_entries.remove(self)
                        self._MODE_invisible = False

                    self.send(":%s!%s@%s MODE %s %s%s\r\n" % (self._nickname, self._username,
                                                              self._hostmask, self._nickname, iif(SetMode, "+", "-"),
                                                              param[2][iloop]))

                elif param[2][iloop] == "f":
                    if SetMode:
                        if "f" not in self._MODE_:
                            self._MODE_ = self._MODE_ + "f"
                        self._MODE_filter = True
                    else:
                        self._MODE_ = self._MODE_.replace("f", "")
                        self._MODE_filter = False

                    self.send(":%s!%s@%s MODE %s %s%s\r\n" % (self._nickname, self._username,
                                                              self._hostmask, self._nickname, iif(SetMode, "+", "-"),
                                                              param[2][iloop]))

                elif param[2][iloop] == "R":
                    if SetMode:
                        if "R" not in self._MODE_:
                            self._MODE_ = self._MODE_ + "R"
                        self._MODE_registerchat = True
                    else:
                        self._MODE_ = self._MODE_.replace("R", "")
                        self._MODE_registerchat = False

                    self.send(":%s!%s@%s MODE %s %s%s\r\n" % (self._nickname, self._username,
                                                              self._hostmask, self._nickname, iif(SetMode, "+", "-"),
                                                              param[2][iloop]))

                elif param[2][iloop] == "p":
                    if SetMode:
                        if "p" not in self._MODE_:
                            self._MODE_ = self._MODE_ + "p"
                        self._MODE_private = True
                    else:
                        self._MODE_ = self._MODE_.replace("p", "")
                        self._MODE_private = False

                    self.send(":%s!%s@%s MODE %s %s%s\r\n" % (self._nickname, self._username,
                                                              self._hostmask, self._nickname, iif(SetMode, "+", "-"),
                                                              param[2][iloop]))

                elif param[2][iloop] == "P":
                    if SetMode:
                        if "P" not in self._MODE_:
                            self._MODE_ = self._MODE_ + "P"
                        self._MODE_nowhisper = True
                    else:
                        self._MODE_ = self._MODE_.replace("P", "")
                        self._MODE_nowhisper = False

                    self.send(":%s!%s@%s MODE %s %s%s\r\n" % (self._nickname, self._username,
                                                              self._hostmask, self._nickname, iif(SetMode, "+", "-"),
                                                              param[2][iloop]))

                elif param[2][iloop] == "z":
                    raw_messages.raw(self, "908", self._nickname)

                elif param[2][iloop] == "I":
                    if SetMode:
                        if "I" not in self._MODE_:
                            self._MODE_ = self._MODE_ + "I"
                        self._MODE_inviteblock = True
                    else:
                        self._MODE_ = self._MODE_.replace("I", "")
                        self._MODE_inviteblock = False

                    self.send(":%s!%s@%s MODE %s %s%s\r\n" % (self._nickname, self._username,
                                                              self._hostmask, self._nickname, iif(SetMode, "+", "-"),
                                                              param[2][iloop]))

                elif param[2][iloop] == "z":
                    raw_messages.raw(self, "908", self._nickname)

                elif param[2][iloop] == "h":  # MODE <nick> +h <pass>
                    if len(param) >= 4:
                        if SetMode:
                            identify = False
                            for each in server_context.channel_entries:
                                isowner = False
                                isop = False
                                # we need to scan through each channel to check if they are oper
                                chanid = server_context.channel_entries[each.lower()]
                                if self._nickname.lower() in chanid._owner:
                                    isowner = True
                                if self._nickname.lower() in chanid._op:
                                    isop = True
                                if param[3] == chanid._prop.ownerkey:
                                    if self._IRCX == False:
                                        pass
                                    else:
                                        if self._nickname.lower() in chanid._op:
                                            chanid._op.remove(self._nickname.lower())
                                        if self._nickname.lower() not in chanid._owner:
                                            chanid._owner.append(self._nickname.lower())
                                        identify = True
                                        for nick in chanid._users:
                                            nickid = server_context.nickname_to_client_mapping_entries[nick]
                                            if isop:
                                                nickid.send(
                                                    ":%s!%s@%s MODE %s -o %s\r\n" %
                                                    (self._nickname, self._username, self._hostmask, chanid.channelname,
                                                     self._nickname))

                                            nickid.send(
                                                ":%s!%s@%s MODE %s +q %s\r\n" %
                                                (self._nickname, self._username, self._hostmask, chanid.channelname,
                                                 self._nickname))

                                elif param[3] == chanid._prop.hostkey:
                                    if self._nickname.lower() in chanid._owner:
                                        chanid._owner.remove(self._nickname.lower())
                                    if self._nickname.lower() not in chanid._op:
                                        chanid._op.append(self._nickname.lower())
                                    identify = True
                                    for nick in chanid._users:
                                        nickid = server_context.nickname_to_client_mapping_entries[nick]
                                        if isowner:
                                            nickid.send(
                                                ":%s!%s@%s MODE %s -q %s\r\n" %
                                                (self._nickname, self._username, self._hostmask, chanid.channelname,
                                                 self._nickname))

                                        nickid.send(":%s!%s@%s MODE %s +o %s\r\n" % (self._nickname,
                                                                                     self._username, self._hostmask,
                                                                                     chanid.channelname,
                                                                                     self._nickname))

                            if identify == False:
                                raw_messages.raw(self, "908", self._nickname)
                    else:
                        raw_messages.raw(self, "461", self._nickname, "MODE +h")

                elif param[2][iloop] == "g":
                    if self._nickname.lower() in server_context.operator_entries:
                        opid = server_context.operator_entries[self._nickname.lower()]
                        if SetMode:
                            opid.guide = True
                            if "g" not in self._MODE_:
                                self._MODE_ = self._MODE_ + "g"
                            self._username = "Guide"
                        else:
                            self._username = opid.username
                            opid.guide = False
                            self._MODE_ = self._MODE_.replace("g", "")

                        self.send(":%s!%s@%s MODE %s %s%s\r\n" % (self._nickname, self._username,
                                                                  self._hostmask, self._nickname,
                                                                  iif(SetMode, "+", "-"), param[2][iloop]))
                    else:
                        raw_messages.raw(self, "481", self._nickname,
                                         "Permission Denied - You're not a System operator")

                elif param[2][iloop] == "X":
                    if self._nickname.lower() in server_context.operator_entries:
                        if SetMode:
                            if "X" not in self._MODE_:
                                self._MODE_ = self._MODE_ + "X"
                            self._friendlyname = " ".join(param).split(" ", 3)[3]
                        else:
                            self._friendlyname = ""
                            self._MODE_ = self._MODE_.replace("X", "")

                        self.send(":%s!%s@%s MODE %s %s%s\r\n" % (self._nickname, self._username,
                                                                  self._hostmask, self._nickname,
                                                                  iif(SetMode, "+", "-"), param[2][iloop]))
                    else:
                        raw_messages.raw(self, "481", self._nickname,
                                         "Permission Denied - You're not a System operator")

                elif param[2][iloop] == "w":
                    if self._nickname.lower() in server_context.operator_entries:
                        opid = server_context.operator_entries[self._nickname.lower()]
                        if opid.operator_level >= 2:
                            if SetMode:
                                opid.watchserver = True
                                if "w" not in self._MODE_:
                                    self._MODE_ = self._MODE_ + "w"
                            else:
                                opid.watchserver = False
                                self._MODE_ = self._MODE_.replace("w", "")

                            self.send(
                                ":%s!%s@%s MODE %s %s%s\r\n" %
                                (self._nickname, self._username, self._hostmask, self._nickname, iif(
                                    SetMode, "+", "-"),
                                 param[2][iloop]))
                        else:
                            raw_messages.raw(self, "481", self._nickname,
                                             "Permission Denied - You're not an Administrator")
                    else:
                        raw_messages.raw(self, "481", self._nickname, "Permission Denied - You're not an Administrator")

                elif param[2][iloop] == "b":
                    if self._nickname.lower() in server_context.operator_entries:
                        opid = server_context.operator_entries[self._nickname.lower()]
                        if opid.operator_level >= 2:
                            if SetMode:
                                opid.watchbans = True
                                if "b" not in self._MODE_:
                                    self._MODE_ = self._MODE_ + "b"
                            else:
                                self._MODE_ = self._MODE_.replace("b", "")
                                opid.watchbans = False

                            self.send(
                                ":%s!%s@%s MODE %s %s%s\r\n" %
                                (self._nickname, self._username, self._hostmask, self._nickname, iif(
                                    SetMode, "+", "-"),
                                 param[2][iloop]))
                        else:
                            raw_messages.raw(self, "481", self._nickname,
                                             "Permission Denied - You're not an Administrator")
                    else:
                        raw_messages.raw(self, "481", self._nickname, "Permission Denied - You're not an Administrator")

                elif param[2][iloop] == "n":
                    if self._nickname.lower() in server_context.operator_entries:
                        opid = server_context.operator_entries[self._nickname.lower()]
                        if opid.operator_level >= 2:
                            if SetMode:
                                opid.watchnickserv = True
                                if "n" not in self._MODE_:
                                    self._MODE_ = self._MODE_ + "n"
                            else:
                                self._MODE_ = self._MODE_.replace("n", "")
                                opid.watchnickserv = False

                            self.send(
                                ":%s!%s@%s MODE %s %s%s\r\n" %
                                (self._nickname, self._username, self._hostmask, self._nickname, iif(
                                    SetMode, "+", "-"),
                                 param[2][iloop]))
                        else:
                            raw_messages.raw(self, "481", self._nickname,
                                             "Permission Denied - You're not an Administrator")
                    else:
                        raw_messages.raw(self, "481", self._nickname, "Permission Denied - You're not an Administrator")

                elif param[2][iloop] == "s":
                    if self._nickname.lower() in server_context.operator_entries:
                        opid = server_context.operator_entries[self._nickname.lower()]
                        if SetMode:
                            opid.hidden = True
                            if self not in server_context.secret_client_entries:
                                server_context.secret_client_entries.add(self)
                            if "s" not in self._MODE_:
                                self._MODE_ = self._MODE_ + "s"
                        else:
                            opid.hidden = False
                            if self in server_context.secret_client_entries:
                                server_context.secret_client_entries.remove(self)
                            self._MODE_ = self._MODE_.replace("s", "")

                        self.send(":%s!%s@%s MODE %s %s%s\r\n" % (self._nickname, self._username,
                                                                  self._hostmask, self._nickname,
                                                                  iif(SetMode, "+", "-"), param[2][iloop]))
                    else:
                        raw_messages.raw(self, "481", self._nickname,
                                         "Permission Denied - You're not a System operator")

                elif param[2][iloop] == "o" or param[2][iloop] == "O" or param[2][iloop] == "a" or param[2][
                    iloop] == "A":
                    if self._nickname.lower() in server_context.operator_entries:

                        opid = server_context.operator_entries[self._nickname.lower()]

                        if SetMode:
                            if param[2][iloop] in opid.flags:
                                self.send(
                                    ":" + server_context.configuration.server_name + " NOTICE SERVER :*** Cannot modify usermode '" +
                                    param[2][iloop] + "'\r\n")
                            else:
                                raw_messages.raw(self, "491", self._nickname,
                                                 "Permission denied - Not enough priviledges")
                        else:
                            if param[2][iloop] == "o":
                                if opid.hidden:
                                    self.send(
                                        ":%s!%s@%s MODE %s -s\r\n" %
                                        (self._nickname, self._username, self._hostmask, self._nickname))
                                if opid.watchserver:
                                    self.send(
                                        ":%s!%s@%s MODE %s -w\r\n" %
                                        (self._nickname, self._username, self._hostmask, self._nickname))
                                if opid.guide:
                                    self.send(
                                        ":%s!%s@%s MODE %s -g\r\n" %
                                        (self._nickname, self._username, self._hostmask, self._nickname))
                                if opid.watchbans:
                                    self.send(
                                        ":%s!%s@%s MODE %s -b\r\n" %
                                        (self._nickname, self._username, self._hostmask, self._nickname))
                                if opid.watchnickserv:
                                    self.send(
                                        ":%s!%s@%s MODE %s -n\r\n" %
                                        (self._nickname, self._username, self._hostmask, self._nickname))

                                self.send(
                                    ":%s!%s@%s MODE %s -%s\r\n" %
                                    (self._nickname, self._username, self._hostmask, self._nickname, opid.flags))
                                self.send(
                                    ":" + server_context.configuration.server_name +
                                    " NOTICE SERVER :*** You are no longer an operator on this server\r\n")
                                for mode in opid.flags:
                                    self._MODE_ = self._MODE_.replace(mode, "")

                                self._MODE_ = self._MODE_.replace("g", "")
                                self._MODE_ = self._MODE_.replace("s", "")
                                self._MODE_ = self._MODE_.replace("w", "")
                                self._MODE_ = self._MODE_.replace("b", "")
                                self._MODE_ = self._MODE_.replace("n", "")
                                if self._MODE_register == False:  # no longer oper, conform to nickserv modes
                                    self._username = PrefixChar + self._username

                                if self in server_context.secret_client_entries:
                                    server_context.secret_client_entries.remove(self)
                                opid.guide = False
                                opid.usage = False
                                opid.hidden = False
                                opid.watchserver = False
                                opid.watchban = False
                                opid.watchnickserv = False

                                del server_context.operator_entries[self._nickname.lower()]
                            else:
                                if param[2][iloop].lower() in opid.flags:
                                    self.send(
                                        ":" + server_context.configuration.server_name + " NOTICE SERVER :*** Cannot remove usermode '" +
                                        param[2][iloop] + "', please use the conf\r\n")
                                else:
                                    raw_messages.raw(self, "481", self._nickname,
                                                     "Permission Denied - You're not a System operator")
                    else:
                        raw_messages.raw(self, "481", self._nickname,
                                         "Permission Denied - You're not a System operator")

                else:
                    raw_messages.raw(self, "501", self._nickname)

                iloop += 1

    elif param[1].lower() in server_context.nickname_to_client_mapping_entries:
        if len(param) == 2:
            if self._nickname.lower() in server_context.operator_entries:
                raw_messages.raw(self, "221",
                                 server_context.nickname_to_client_mapping_entries[param[1].lower()]._nickname,
                                 server_context.nickname_to_client_mapping_entries[param[1].lower()]._MODE_)
            else:
                raw_messages.raw(self, "481", self._nickname, "Permission Denied - You're not a System operator")
        else:
            raw_messages.raw(self, "502", self._nickname)

    else:
        raw_messages.raw(self, "401", self._nickname, param[1])


def Nickserv_function(self, param, msgtype=""):
    logger = logging.getLogger('NICKSERV')

    try:
        replyType = "NOTICE"

        if msgtype != "":
            if param[1][0] == ":":
                param[1] = param[1][1:]
                replyType = msgtype

        param[1] = param[1].upper()

        if param[1] == "REGISTER":
            try:
                if self._MODE_register == True:
                    self.send(":%s!%s@%s %s %s :Error: You are already registered\r\n" %
                              ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                elif self._nickname.lower() not in server_context.operator_entries and (
                        (self._signontime - GetEpochTime()) < -300) == False and defconMode == 2:
                    self.send(
                        ":%s!%s@%s %s %s :Error: NickServ requires you to stay on this server a minimum amount of time before registering your nickname\r\n"
                        % ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                    sendNickservOpers(
                        "Notice -- \x02NickServ\x02 - (%s!%s@%s) [%s] has tried to registered their nickname (not online long enough, defcon 2 is active)\r\n"
                        % (self._nickname, self._username, self._hostmask, self.details[0]))
                elif self._nickname.lower() not in server_context.operator_entries and defconMode == 3:
                    self.send(
                        ":%s!%s@%s %s %s :Error: NickServ will not allow nicknames to be registered at this time\r\n" %
                        ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                    sendNickservOpers(
                        "Notice -- \x02NickServ\x02 - (%s!%s@%s) [%s] has tried to registered their nickname (nickserv disabled, defcon 3 is active)\r\n"
                        % (self._nickname, self._username, self._hostmask, self.details[0]))
                else:
                    passw = param[2]
                    emaila = param[3]
                    checkemail = emaila.split("@")[1].split(".")[1]
                    toomanynicks = 0
                    exemptFromConnectionKiller = False
                    for registered_nicknames in server_context.nickserv_entries:
                        mydetails_obj = server_context.nickserv_entries[registered_nicknames.lower()]
                        mydetails = mydetails_obj._details
                        if mydetails == self.details[0]:
                            toomanynicks += 1

                    try:
                        for each in globals()["connectionsExempt"]:
                            if each == "":
                                continue

                            chk = re.compile("^" + each + "$")
                            if chk.match(self.details[0]) != None:
                                exemptFromConnectionKiller = True
                                break
                    except:
                        print(sys.exc_info())

                    if NickservIPprotection == False:
                        exemptFromConnectionKiller = True

                    grouped_nick = False
                    for groupnicks in list(server_context.nickserv_entries.values()):
                        if self._nickname.lower() in groupnicks.grouped_nicknames:
                            grouped_nick = True
                            break

                    if self._nickname.lower() in server_context.nickserv_entries or grouped_nick == True:
                        self.send(":%s!%s@%s %s %s :Error: That nickname has already been registered\r\n" %
                                  ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

                    elif toomanynicks >= 1 and exemptFromConnectionKiller == False:
                        self.send(
                            ":%s!%s@%s %s %s :Error: You can only register one nickname, you can group nicknames though\r\n" %
                            ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                    else:
                        olevel = 0
                        if self._nickname.lower() in server_context.operator_entries:
                            opid = server_context.operator_entries[self._nickname.lower()]
                            olevel = opid.operator_level

                        writehash = sha256((passw + NickservParam).encode('utf-8'))

                        server_context.nickserv_entries[self._nickname.lower()] = NickServEntry(self._nickname,
                                                                                                writehash.hexdigest(
                                                                                                ), emaila,
                                                                                                GetEpochTime(),
                                                                                                self.details[0], "",
                                                                                                olevel,
                                                                                                False)  # add to the nickname database

                        self.send(
                            ":%s!%s@%s %s %s :\x02Registration complete\x02\r\n:%s!%s@%s %s %s :Your nickname has been registered with the address *@%s\r\n" % (
                                "NickServ", "NickServ", NetworkName, replyType, self._nickname, "NickServ", "NickServ",
                                NetworkName, replyType, self._nickname, self._hostmask))
                        self.send(
                            ":%s!%s@%s %s %s :Your password is \x02%s\x02, please remember to keep this safe\r\n" %
                            ("NickServ", "NickServ", NetworkName, replyType, self._nickname, passw))
                        self._MODE_register = True

                        WriteUsers(True, False)
                        if self._username[0] == PrefixChar:
                            self._username = self._username[1:]
                        if "r" not in self._MODE_:
                            self._MODE_ += "r"
                        self.send(":%s!%s@%s MODE %s +r\r\n" % ("NickServ", "NickServ", NetworkName, self._nickname))
                        sendNickservOpers(
                            "Notice -- \x02NickServ\x02 - (%s!%s@%s) [%s] has registered their nickname\r\n" %
                            (self._nickname, self._username, self._hostmask, self.details[0]))

            except Exception as exception:
                logger.debug(exception)
                self.send(":%s!%s@%s %s %s :Syntax: \x02REGISTER \x1Fpassword\x1F \x1Femail\x1F\x02\r\n" %
                          ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

        elif param[1] == "HELLO":
            self.send(":%s!%s@%s %s %s :hello to you too!\r\n" %
                      ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

        elif param[1] == "IPLOCK":
            if len(param) == 2:
                if globals()["NickservIPprotection"] == False:
                    methodIS = "\x02Off\x02"
                else:
                    methodIS = "\x02On\x02"

                self.send(":%s!%s@%s %s %s :IPLOCK is currently %s\r\n" %
                          ("NickServ", "NickServ", NetworkName, replyType, self._nickname, methodIS))
            else:
                if self._nickname.lower() in server_context.operator_entries:
                    opid = server_context.operator_entries[self._nickname.lower()]
                    if opid.operator_level > 2:
                        if param[2].upper() == "ON":
                            globals()["NickservIPprotection"] = True
                            defconDesc = "disallow nicknames to be registered by duplicate IP addresses \x02(high protection)\x02"
                        elif param[2].upper() == "OFF":
                            globals()["NickservIPprotection"] = False
                            defconDesc = "allow nicknames to be registered regardless of whether their IP has registered before \x02(low protection)\x02"
                        else:
                            self.send(":%s!%s@%s %s %s :No such IPLOCK mode\r\n" %
                                      ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                            return

                        self.send(":%s!%s@%s %s %s :NickServ will now %s\r\n" %
                                  ("NickServ", "NickServ", NetworkName, replyType, self._nickname, defconDesc))
                        sendNickservOpers(
                            "Notice -- \x02NickServ\x02 - IPLOCK changed by %s, NickServ will now %s\r\n" %
                            (self._nickname, defconDesc))
                    else:
                        self.send(":%s!%s@%s %s %s :Error: Access denied\r\n" %
                                  ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                else:
                    self.send(":%s!%s@%s %s %s :Error: Access denied\r\n" %
                              ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

        elif param[1] == "DEFCON":
            if len(param) == 2:
                self.send(":%s!%s@%s %s %s :DEFCON is currently operating on level %d\r\n" %
                          ("NickServ", "NickServ", NetworkName, replyType, self._nickname, globals()["defconMode"]))
            else:
                if self._nickname.lower() in server_context.operator_entries:
                    opid = server_context.operator_entries[self._nickname.lower()]
                    if opid.operator_level > 2:
                        if param[2] == "1":
                            globals()["defconMode"] = 1
                            defconDesc = "allow nicknames to be registered at any time with no restrictions \x02(low protection)\x02"
                        elif param[2] == "2":
                            globals()["defconMode"] = 2
                            defconDesc = "allow nicknames to be registered after the user has been online for 5 minutes \x02(high protection)\x02"
                        elif param[2] == "3":
                            globals()["defconMode"] = 3
                            defconDesc = "disallow any new registrations \x02(disabled)\x02"
                        else:
                            self.send(":%s!%s@%s %s %s :No such DEFCON level available\r\n" %
                                      ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                            return

                        self.send(":%s!%s@%s %s %s :NickServ will now %s\r\n" %
                                  ("NickServ", "NickServ", NetworkName, replyType, self._nickname, defconDesc))
                        sendNickservOpers(
                            "Notice -- \x02NickServ\x02 - DEFCON changed by %s, NickServ will now %s\r\n" %
                            (self._nickname, defconDesc))
                    else:
                        self.send(":%s!%s@%s %s %s :Error: Access denied\r\n" %
                                  ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                else:
                    self.send(":%s!%s@%s %s %s :Error: Access denied\r\n" %
                              ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

        elif param[1] == "IDENTIFY":
            try:
                if self._MODE_register:
                    self.send(":%s!%s@%s %s %s :Error: You are already identified\r\n" %
                              ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                else:
                    passw = param[2]
                    grouped_nick = None
                    for groupnicks in list(server_context.nickserv_entries.values()):
                        if self._nickname.lower() in groupnicks.grouped_nicknames:
                            grouped_nick = groupnicks
                            break

                    if self._nickname.lower() in server_context.nickserv_entries or grouped_nick != None:
                        if grouped_nick != None:
                            ns = grouped_nick
                        else:
                            ns = server_context.nickserv_entries[self._nickname.lower()]

                        writehash1 = sha256((passw + NickservParam).encode('utf-8'))

                        if writehash1.hexdigest() == ns._password:
                            self._MODE_register = True
                            if "r" not in self._MODE_:
                                self._MODE_ += "r"

                            if self._username[0] == PrefixChar:
                                self._username = self._username[1:]
                            self.send(":%s!%s@%s MODE %s +r\r\n" %
                                      ("NickServ", "NickServ", NetworkName, self._nickname))
                            self.send(":%s!%s@%s %s %s :Welcome back %s\r\n" %
                                      ("NickServ", "NickServ", NetworkName, replyType, self._nickname, self._nickname))

                            if ns.virtual_host != "":
                                self._hostmask = ns.virtual_host
                                self.send(":%s!%s@%s %s %s :Your \x02vhost\x02 has been activated\r\n" %
                                          ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

                        else:
                            self.send(":%s!%s@%s %s %s :Error: Invalid password\r\n" %
                                      ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                    else:
                        self.send(":%s!%s@%s %s %s :Error: Your nick isn't registered\r\n" %
                                  ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

            except:
                self.send(":%s!%s@%s %s %s :Syntax: \x02IDENTIFY \x1Fpassword\x1F\x02\r\n" %
                          ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

        elif param[1] == "GHOST":
            try:
                nickn = param[2]
                passw = param[3]

                groupnick = None
                for groupnicks in list(server_context.nickserv_entries.values()):
                    if nickn.lower() in groupnicks.grouped_nicknames:
                        groupnick = groupnicks
                        break

                if nickn.lower() in server_context.nickserv_entries or groupnick:
                    if groupnick:
                        ns = groupnick
                    else:
                        ns = server_context.nickserv_entries[nickn.lower()]

                    writehash1 = sha256((passw + NickservParam).encode('utf-8'))

                    if writehash1.hexdigest() == ns._password:
                        if nickn.lower() in server_context.nickname_to_client_mapping_entries:

                            cid = server_context.nickname_to_client_mapping_entries[nickn.lower()]

                            sendto = [cid]

                            cid.send(
                                ":%s!%s@%s %s %s :A ghost command has been used on your nickname, it may be because someone has already registered your name\r\n"
                                % ("NickServ", "NickServ", NetworkName, replyType, cid._nickname))

                            # non IRCX clients don't understand KILL
                            nonIRCXsend = ":%s!%s@%s QUIT :Killed by %s (%s)\r\n" % (
                                cid._nickname, cid._username, cid._hostmask, "NickServ", "Ghosted nickname")
                            _send = ":%s!%s@%s KILL %s :Ghost nickname\r\n" % (
                                "NickServ", "NickServ", NetworkName, cid._nickname)
                            if cid._IRCX:
                                cid.send(_send)
                            else:
                                cid.send(nonIRCXsend)

                            for each in cid._channels:
                                chan = server_context.channel_entries[each.lower()]
                                for n in chan._users:
                                    if n in server_context.nickname_to_client_mapping_entries:
                                        nick = server_context.nickname_to_client_mapping_entries[n.lower()]
                                        if nick not in sendto:
                                            if cid._nickname.lower() not in chan._watch:
                                                if chan.MODE_auditorium == False or isOp(n, chan.channelname):
                                                    sendto.append(nick)
                                                    if nick._IRCX:
                                                        nick.send(_send)
                                                    else:
                                                        nick.send(nonIRCXsend)

                            sendto = []

                            cid.quittype = -1
                            cid.quitmsg = " by NickServ"
                            cid.die = True

                            self.send(":%s!%s@%s %s %s :The ghosted nickname has been killed\r\n" %
                                      ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

                        else:
                            self.send(":%s!%s@%s %s %s :Error: Your nickname is free\r\n" %
                                      ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                    else:
                        self.send(":%s!%s@%s %s %s :Error: Invalid password\r\n" %
                                  ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                else:
                    self.send(":%s!%s@%s %s %s :Error: That nick isn't registered\r\n" %
                              ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

            except:
                self.send(":%s!%s@%s %s %s :Syntax: \x02GHOST \x1Fnickname\x1F \x1Fpassword\x1F\x02\r\n" %
                          ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

        elif param[1] == "INFO":
            try:
                nickn = param[2]
                grouped_nick = None
                for groupnicks in list(server_context.nickserv_entries.values()):
                    if nickn.lower() in groupnicks.grouped_nicknames:
                        grouped_nick = groupnicks
                        break

                if nickn.lower() in server_context.nickserv_entries or grouped_nick != None:
                    if grouped_nick != None:
                        ns = grouped_nick
                    else:
                        ns = server_context.nickserv_entries[nickn.lower()]

                    self.send(":%s!%s@%s %s %s :\x02Nickname Information\x02 for %s\r\n" %
                              ("NickServ", "NickServ", NetworkName, replyType, self._nickname, ns._nickname))
                    if len(ns.grouped_nicknames) != 0:
                        self.send(":%s!%s@%s %s %s :Grouped nicknames: %s\r\n" % ("NickServ", "NickServ",
                                                                                  NetworkName, replyType,
                                                                                  self._nickname,
                                                                                  ", ".join(ns.grouped_nicknames)))
                    self.send(":%s!%s@%s %s %s :Registered: %s\r\n" % ("NickServ", "NickServ", NetworkName,
                                                                       replyType, self._nickname,
                                                                       time.ctime(float(ns.registration_time))))
                    if ns.show_email == "True" or self._nickname.lower() in server_context.operator_entries:
                        emailaddress = ns._email
                    else:
                        emailaddress = "hidden"

                    self.send(":%s!%s@%s %s %s :Email address: %s\r\n" %
                              ("NickServ", "NickServ", NetworkName, replyType, self._nickname, emailaddress))
                    onlineStatus = "Online but not identified (could be a clone)"
                    if nickn.lower() in server_context.nickname_to_client_mapping_entries:
                        nick_id = server_context.nickname_to_client_mapping_entries[nickn.lower()]
                        if "r" in nick_id._MODE_:
                            onlineStatus = "\x02Online and identified!\x02"
                    else:
                        onlineStatus = "Offline"

                    self.send(
                        ":%s!%s@%s %s %s :User is: %s\r\n" %
                        ("NickServ", "NickServ", NetworkName, replyType, self._nickname, onlineStatus))

                    if self._nickname.lower() in server_context.operator_entries:
                        opid = server_context.operator_entries[self._nickname.lower()]
                        if opid.operator_level > ns._level:
                            self.send(
                                ":%s!%s@%s %s %s :Address: %s\r\n" %
                                ("NickServ", "NickServ", NetworkName, replyType, self._nickname, ns._details))
                else:
                    self.send(":%s!%s@%s %s %s :Error: That nick isn't registered\r\n" %
                              ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

            except:
                self.send(":%s!%s@%s %s %s :Syntax: \x02INFO \x1Fnickname\x1F\x02\r\n" %
                          ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

        elif param[1] == "SET":
            try:
                nickn = param[2]
                if nickn.upper() == "HELP":
                    if self._nickname.lower() in server_context.operator_entries:
                        self.send(":%s!%s@%s %s %s :SET <nickname> \x02VHOST\x02 \x1Fmask\x1F\r\n" %
                                  ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

                    self.send(
                        ":%s!%s@%s %s %s :SET <nickname> \x02PASSWORD\x02 \x1Fold password\x1F \x1Fnew password\x1F\r\n" %
                        ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                    self.send(":%s!%s@%s %s %s :SET <nickname> \x02SHOWEMAIL\x02 \x1Fon/off\x1F\r\n" %
                              ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

                elif nickn.lower() in server_context.nickserv_entries:
                    option = param[3].upper()
                    nid = server_context.nickserv_entries[nickn.lower()]

                    try:
                        value = param[4]
                    except:
                        if option == "VHOST":
                            if nid.virtual_host != "":
                                self.send(":%s!%s@%s %s %s :Nickserv will no longer assign a vhost to %s\r\n" %
                                          ("NickServ", "NickServ", NetworkName, replyType, self._nickname,
                                           nid._nickname))
                                nid.virtual_host = ""
                                WriteUsers(True, False)
                            else:
                                self.send(":%s!%s@%s %s %s :%s does not have a \x02vhost\x02 assigned\r\n" %
                                          ("NickServ", "NickServ", NetworkName, replyType, self._nickname,
                                           nid._nickname))

                            option = ""

                    if option == "":
                        pass

                    elif option == "VHOST":
                        if self._nickname.lower() in server_context.operator_entries:
                            opid = server_context.operator_entries[self._nickname.lower()]
                            if opid.operator_level >= nid._level or self._nickname.lower() == nid._nickname.lower() and self._MODE_register:
                                if self._validate(value.replace(".", "a").replace("/", "a")):
                                    nid.virtual_host = value
                                    WriteUsers(True, False)
                                    self.send(":%s!%s@%s %s %s :A \x02vhost\x02 has been assigned to %s\r\n" %
                                              ("NickServ", "NickServ", NetworkName, replyType, self._nickname,
                                               nid._nickname))
                                    if nickn.lower() in server_context.nickname_to_client_mapping_entries:
                                        cid = server_context.nickname_to_client_mapping_entries[nickn.lower()]
                                        if cid._MODE_register and cid != self:  # if they are registered
                                            cid.send(
                                                ":%s!%s@%s %s %s :A \x02vhost\x02 has been assigned to your registered nickname\r\n" %
                                                ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                                            cid._hostmask = value
                                else:
                                    self.send(":%s!%s@%s %s %s :Error: Invalid vhost\r\n" %
                                              ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                            else:
                                self.send(":%s!%s@%s %s %s :Error: Access denied\r\n" %
                                          ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                        else:
                            self.send(":%s!%s@%s %s %s :Error: Access denied\r\n" %
                                      ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

                    elif option == "SHOWEMAIL":
                        if nickn.lower() == self._nickname.lower() and "r" in self._MODE_:
                            if value == "on":
                                nid.show_email = "True"
                                self.send(
                                    ":%s!%s@%s %s %s :Nickserv will now display your email on information requests\r\n" %
                                    ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                                WriteUsers(True, False)
                            elif value == "off":
                                nid.show_email = "False"
                                self.send(":%s!%s@%s %s %s :Nickserv will no longer display your email\r\n" %
                                          ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                                WriteUsers(True, False)
                            else:
                                self.send(":%s!%s@%s %s %s :SET <nickname> \x02SHOWEMAIL\x02 \x1Fon/off\x1F\r\n" %
                                          ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                        else:
                            self.send(":%s!%s@%s %s %s :Error: Access denied\r\n" %
                                      ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

                    elif option == "PASSWORD":
                        value1 = param[5]
                        if nickn.lower() in server_context.nickserv_entries:
                            nid = server_context.nickserv_entries[nickn.lower()]

                            writehash1 = sha256((value + NickservParam).encode('utf-8'))
                            writehash2 = sha256((value1 + NickservParam).encode('utf-8'))

                            if writehash1.hexdigest() == nid._password:
                                nid._password = writehash2.hexdigest()
                                WriteUsers(True, False)
                                self.send(":%s!%s@%s %s %s :Nickserv password has been changed successfully\r\n" %
                                          ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                                if nickn.lower() in server_context.nickname_to_client_mapping_entries:
                                    cid = server_context.nickname_to_client_mapping_entries[nickn.lower()]
                                    if cid._MODE_register:
                                        cid.send(
                                            ":%s!%s@%s %s %s :Your nickname \x02password\x02 has been changed to \x02%s\x02\r\n" %
                                            ("NickServ", "NickServ", NetworkName, replyType, self._nickname, value1))
                            else:
                                self.send(":%s!%s@%s %s %s :Error: Invalid password\r\n" %
                                          ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                        else:
                            self.send(":%s!%s@%s %s %s :Error: That nick isn't registered\r\n" %
                                      ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                    else:
                        self.send(":%s!%s@%s %s %s :Error: Unknown property\r\n" %
                                  ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                else:
                    self.send(
                        ":%s!%s@%s %s %s :Error: That nick isn't registered or you are not using your primary nickname\r\n" %
                        ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
            except:
                self.send(":%s!%s@%s %s %s :Syntax Error: \x02SET \x1Fhelp\x1F\x02\r\n" %
                          ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

        elif param[1] == "UNGROUP":  # NS GROUP nickname <password>
            try:
                if param[2].lower() in server_context.nickserv_entries:
                    nid = server_context.nickserv_entries[param[2].lower()]

                    writehash1 = sha256((param[3] + NickservParam).encode('utf-8'))
                    if writehash1.hexdigest() == nid._password:
                        if self._nickname.lower() not in nid.grouped_nicknames:
                            self.send(":%s!%s@%s %s %s :Error: No such nickname grouped to %s\r\n" %
                                      ("NickServ", "NickServ", NetworkName, replyType, self._nickname, nid._nickname))
                        else:
                            nid.grouped_nicknames.remove(self._nickname.lower())
                            self.send(":%s!%s@%s %s %s :That nickname is now ungrouped\r\n" %
                                      ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                            WriteUsers(True, False)
                    else:
                        self.send(":%s!%s@%s %s %s :Error: Invalid password\r\n" %
                                  ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

                else:
                    self.send(
                        ":%s!%s@%s %s %s :Error: The nickname you're trying to group with does not exist!\r\n" %
                        ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

            except IndexError:
                self.send(":%s!%s@%s %s %s :Syntax Error: \x02GROUP \x1Fprimary nickname\x1F \x1Fpassword\x1F\r\n" %
                          ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

        elif param[1] == "GROUP":  # NS GROUP nickname <password>
            try:
                if param[2].lower() in server_context.nickserv_entries:
                    nid = server_context.nickserv_entries[param[2].lower()]
                    writehash1 = sha256((param[3] + NickservParam).encode('utf-8'))
                    if writehash1.hexdigest() == nid._password:
                        if len(nid.grouped_nicknames) == 2:
                            self.send(":%s!%s@%s %s %s :Error: You can only \x02group\x02 two nicknames\r\n" %
                                      ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                        else:
                            grouped_already = False
                            for groupnicks in list(server_context.nickserv_entries.values()):
                                if self._nickname.lower() in groupnicks.grouped_nicknames:
                                    self.send(
                                        ":%s!%s@%s %s %s :Error: This nickname is already grouped/registered\r\n" %
                                        ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                                    grouped_already = True
                                    break

                            if grouped_already == False:
                                nid.grouped_nicknames.append(self._nickname.lower())
                                self.send(
                                    ":%s!%s@%s %s %s :\x02Grouping complete\x02\r\n:%s!%s@%s %s %s :%s has been \x02grouped\x02 with the registered nickname: %s\r\n" % (
                                        "NickServ", "NickServ", NetworkName, replyType, self._nickname, "NickServ",
                                        "NickServ", NetworkName, replyType, self._nickname, self._nickname,
                                        nid._nickname))
                                WriteUsers(True, False)
                                self._MODE_register = True

                                if self._username[0] == PrefixChar:
                                    self._username = self._username[1:]
                                if "r" not in self._MODE_:
                                    self._MODE_ += "r"
                                self.send(":%s!%s@%s MODE %s +r\r\n" %
                                          ("NickServ", "NickServ", NetworkName, self._nickname))
                                sendNickservOpers(
                                    "Notice -- \x02NickServ\x02 - (%s!%s@%s) [%s] has grouped their nickname with \x02%s\x02\r\n" % (
                                        self._nickname, self._username, self._hostmask, self.details[0], nid._nickname))

                    else:
                        self.send(":%s!%s@%s %s %s :Error: Invalid password\r\n" %
                                  ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

                else:
                    self.send(
                        ":%s!%s@%s %s %s :Error: The nickname you're trying to group with does not exist!\r\n" %
                        ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

            except IndexError:
                self.send(":%s!%s@%s %s %s :Syntax: \x02GROUP \x1Fprimary nickname\x1F \x1Fpassword\x1F\r\n" %
                          ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

        elif param[1] == "DROP":
            try:
                nickn = param[2]
                try:
                    passw = param[3]
                except:
                    passw = ""

                grouped_nick = False
                for groupnicks in list(server_context.nickserv_entries.values()):
                    if nickn.lower() in groupnicks.grouped_nicknames:
                        grouped_nick = True
                        self.send(
                            ":%s!%s@%s %s %s :Error: You cannot \x02drop\x02 a grouped nickname, please use \x1FUNGROUP\x1F\r\n" %
                            ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                        break

                if grouped_nick == False:
                    if nickn.lower() in server_context.nickserv_entries:
                        ns = server_context.nickserv_entries[nickn.lower()]

                        writehash1 = sha256((passw + NickservParam).encode('utf-8'))

                        if ns._password == writehash1.hexdigest() or self._nickname.lower() in server_context.operator_entries:

                            if ns._password == writehash1.hexdigest():
                                dropn = True
                            else:
                                opid = server_context.operator_entries[self._nickname.lower()]

                                if opid.operator_level > ns._level:
                                    dropn = True
                                else:
                                    dropn = False

                            if dropn:
                                if ns._nickname.lower() in server_context.nickname_to_client_mapping_entries:
                                    cid = server_context.nickname_to_client_mapping_entries[ns._nickname.lower()]
                                    if cid._MODE_register:
                                        cid._MODE_.replace("r", "")
                                        cid._MODE_register = False
                                        if cid._username[
                                            0] != PrefixChar and cid._nickname.lower() not in server_context.operator_entries:
                                            cid._username = PrefixChar + cid._username[1:]

                                        cid.send(
                                            ":%s!%s@%s MODE %s -r\r\n" %
                                            ("NickServ", "Nickserv", NetworkName, cid._nickname))
                                        if cid != self:
                                            cid.send(":%s!%s@%s %s %s :Your nickname has been dropped\r\n" %
                                                     ("NickServ", "NickServ", NetworkName, replyType, cid._nickname))

                                del server_context.nickserv_entries[nickn.lower()]
                                WriteUsers(True, False)
                                self.send(":%s!%s@%s %s %s :The nickname \x02%s\x02 has been dropped\r\n" %
                                          (
                                              "NickServ", "NickServ", NetworkName, replyType, self._nickname,
                                              ns._nickname))
                            else:
                                self.send(":%s!%s@%s %s %s :Error: Access denied\r\n" %
                                          ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                        else:
                            self.send(":%s!%s@%s %s %s :Error: Access denied\r\n" %
                                      ("NickServ", "NickServ", NetworkName, replyType, self._nickname))
                    else:
                        self.send(":%s!%s@%s %s %s :Error: That nick isn't registered\r\n" %
                                  ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

            except:
                self.send(
                    ":%s!%s@%s %s %s :Syntax: \x02DROP \x1Fnickname\x1F \x1F[password]\x1F\x02\r\n" %
                    ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

        elif param[1] == "HELP":
            self.send(
                ":%s!%s@%s %s %s :REGISTER register a nickname\r\n:%s!%s@%s %s %s :IDENTIFY identify yourself with a password\r\n:%s!%s@%s %s %s :GHOST Disconnect a user using your nickname \r\n" %
                ("NickServ", "NickServ", NetworkName, replyType, self._nickname, "NickServ", "NickServ", NetworkName,
                 replyType, self._nickname, "NickServ", "NickServ", NetworkName, replyType, self._nickname))
            self.send(
                ":%s!%s@%s %s %s :INFO get information about a nickname\r\n:%s!%s@%s %s %s :DROP release nickname from services, this means other users can register this nick\r\n" %
                ("NickServ", "NickServ", NetworkName, replyType, self._nickname, "NickServ", "NickServ", NetworkName,
                 replyType, self._nickname))
            self.send(
                ":%s!%s@%s %s %s :GROUP/UNGROUP groups alternative nicknames with your primary nickname\r\n:%s!%s@%s %s %s :SET help\r\n" %
                ("NickServ", "NickServ", NetworkName, replyType, self._nickname, "NickServ", "NickServ", NetworkName,
                 replyType, self._nickname))
            self.send(
                ":%s!%s@%s %s %s :DEFCON view or modify the DEFCON settings\r\n:%s!%s@%s %s %s :IPLOCK view or modify the IP lock settings\r\n" %
                ("NickServ", "NickServ", NetworkName, replyType, self._nickname, "NickServ", "NickServ", NetworkName,
                 replyType, self._nickname))

        else:
            self.send(":%s!%s@%s %s %s :Error: Unknown command\r\n" %
                      ("NickServ", "NickServ", NetworkName, replyType, self._nickname))

    except:
        self.send(
            ":%s!%s@%s %s %s :REGISTER register a nickname\r\n:%s!%s@%s %s %s :IDENTIFY identify yourself with a password\r\n:%s!%s@%s %s %s :GHOST Disconnect a user using your nickname \r\n" %
            ("NickServ", "NickServ", NetworkName, replyType, self._nickname, "NickServ", "NickServ", NetworkName,
             replyType, self._nickname, "NickServ", "NickServ", NetworkName, replyType, self._nickname))
        self.send(
            ":%s!%s@%s %s %s :INFO get information about a nickname\r\n:%s!%s@%s %s %s :DROP release nickname from services, this means other users can register this nick\r\n" %
            ("NickServ", "NickServ", NetworkName, replyType, self._nickname, "NickServ", "NickServ", NetworkName,
             replyType, self._nickname))
        self.send(
            ":%s!%s@%s %s %s :GROUP/UNGROUP groups alternative nicknames with your primary nickname\r\n:%s!%s@%s %s %s :SET help\r\n" %
            ("NickServ", "NickServ", NetworkName, replyType, self._nickname, "NickServ", "NickServ", NetworkName,
             replyType, self._nickname))
        self.send(
            ":%s!%s@%s %s %s :DEFCON view or modify the DEFCON settings\r\n:%s!%s@%s %s %s :IPLOCK view or modify the IP lock settings\r\n" %
            ("NickServ", "NickServ", NetworkName, replyType, self._nickname, "NickServ", "NickServ", NetworkName,
             replyType, self._nickname))


def load_channel_history():  # this is information such as channels, max users etc
    logger = logging.getLogger('HISTORY')

    try:
        with open(server_context.configuration.channels_database_file, 'rb') as channels_file:
            for bytes_line in channels_file.readlines():
                s_line = bytes_line.strip().split(b'\x01')
                if s_line[0].split(b'=')[0].upper() == b'C':
                    s_chan = s_line[0].split(b'=')[1].decode(character_encoding)
                    s_modes = s_line[1].split(b'=')[1].decode(character_encoding)
                    s_topic = s_line[2].split(b'=', 1, )[1].decode(character_encoding)
                    s_founder = s_line[3].split(b'=', 1, )[1].decode(character_encoding)
                    s_prop = bytes.fromhex(s_line[4].split(b'=', 1, )[1].decode(character_encoding))
                    s_ax = bytes.fromhex(s_line[5].split(b'=', 1, )[1].decode(character_encoding))

                    chanclass = Channel(server_context, raw_messages, s_chan, "", s_modes)  # create

                    if chanclass.channelname != "":
                        _founder = ""
                        server_context.channel_entries[s_chan.lower()] = chanclass
                        if "r" in s_modes:
                            chanclass._prop.registered = server_context.configuration.server_name
                        if s_founder != "":
                            _founder = access_helper.CreateMaskString(s_founder, True)

                        chanclass._founder = _founder
                        chanclass._topic = s_topic

                        chanclass._topic_nick = server_context.configuration.server_name
                        chanclass._topic_time = int(time.time())

                        chanclass.ChannelAccess = loads(decompress(s_ax.strip()))
                        chanclass._prop = loads(decompress(s_prop))
                        if s_founder != "":
                            _addrec = access_helper.AddRecord("", chanclass.channelname.lower(), "OWNER", _founder, 0, "")
    except Exception as e:
        logger.info("No channel history found")
        logger.debug(e)

    try:
        with open(server_context.configuration.access_database_file, 'rb') as file:
             server_context.server_access_entries = loads(file.read())
    except Exception as e:
        logger.info("No access entries history found")
        logger.debug(e)
        server_context.server_access_entries = []


class ServerListen(threading.Thread):

    def __init__(self, port):
        self.port = port
        threading.Thread.__init__(self)
        self.logger = logging.getLogger('SERVER')

    def run(self):

        try:
            smain = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            smain.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            smain.bind((ipaddress, int(self.port)))
            smain.settimeout(5.0)
            smain.listen(100)

            self.logger.info("Listening on port " + str(self.port) + " at '" + (ipaddress.strip() or "localhost") + "'")

            while True:
                time.sleep(0.1)
                try:
                    try:
                        (clientsocket, address) = smain.accept()
                        ClientConnecting(clientsocket, address, self.port).start()
                    except:
                        if self.port not in Ports:
                            print("*** Terminating server on port " + self.port)
                            break

                except:
                    print("There was an error whilst a user was connecting")
        except:
            print("*** ERROR: Socket error on port " + str(self.port) + "(Bind Error)")

        if self.port in currentports:
            del currentports[self.port]


def GetEpochTime():
    # actual time from an NTP server is Time
    return int(time.time())
    # + timeDifference


def SetupListeningSockets():
    global currentports

    for p in Ports:
        if p not in currentports:  # If the port isn't already running, set it up, old ports will automatically timeout after five seconds
            currentports[p] = ServerListen(p).start()



import logging

def start():
    logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

    logger = logging.getLogger('START')

    logger.info("  _____  __    __  _____    _____  __    __ ")
    logger.info(" |  _  \ \ \  / / |  _  \  /  ___| \ \  / / ")
    logger.info(" | |_| |  \ \/ /  | |_| |  | |      \ \/ /  ")
    logger.info(" |  ___/   \  /   |  _  /  | |       }  {   ")
    logger.info(" | |       / /    | | \ \  | |___   / /\ \  ")
    logger.info(" |_|      /_/     |_|  \_\ \_____| /_/  \_\ " + server_context.configuration.VERSION)
    logger.info(" __________________________________________")
    logger.info("")
    logger.info(" GitHub: https://github.com/cwebbtw/pyRCX")
    logger.info(" __________________________________________")
    logger.info("")

    logger.info("Loading configuration")

    statistics.load()

    load_nickserv_database()
    load_channel_history()

    rehash()

    logger.info("Configuration loaded")

    SetupListeningSockets()

    if NickservParam == "":
        raise Exception("Cannot run server without Nickserv security, please add an n:line to your config")

    while True:
        time.sleep(50)

# if __name__ == '__main__':
# 	if hasattr(os,"fork"):

# 		try:
# 			pid = os.fork()
# 			if pid > 0: sys.exit(0)
# 		except OSError:
# 			sys.exit(1)

# 		os.setsid()
# 		os.umask(0)

# 		try:
# 			pid = os.fork()
# 			if pid > 0:
# 				sys.exit(0)

# 		except OSError:
# 			sys.exit(1)

# 	else:
# 		pass

# 	main()
