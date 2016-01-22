#!/usr/bin/python3

# ################################################################################################ #
# AccuRev to Git conversion script                                                                 #
# Author: Lazar Sumar                                                                              #
# Date:   06/11/2014                                                                               #
#                                                                                                  #
# This script is intended to convert an entire AccuRev depot into a git repository converting      #
# workspaces and streams into branches and respecting merges.                                      #
# ################################################################################################ #

import sys
import argparse
import os
import os.path
import shutil
import subprocess
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timedelta
import time
import re
import types
import copy
import codecs
import json
import pytz
import tempfile

from collections import OrderedDict

import accurev
import git
import git_stitch

# ################################################################################################ #
# Script Classes                                                                                   #
# ################################################################################################ #
class Config(object):
    class Logger(object):
        def __init__(self):
            self.referenceTime = None
            self.isDbgEnabled = False
            self.isInfoEnabled = True
            self.isErrEnabled = True

            self.logFile = None
            self.logFileDbgEnabled = False
            self.logFileInfoEnabled = True
            self.logFileErrorEnabled = True
        
        def _FormatMessage(self, messages):
            outMessage = ""
            if self.referenceTime is not None:
                # Custom formatting of the timestamp
                m, s = divmod((datetime.now() - self.referenceTime).total_seconds(), 60)
                h, m = divmod(m, 60)
                d, h = divmod(h, 24)
                
                if d > 0:
                    outMessage += "{d: >2d}d, ".format(d=int(d))
                
                outMessage += "{h: >2d}:{m:0>2d}:{s:0>5.2f}# ".format(h=int(h), m=int(m), s=s)
            
            outMessage += " ".join([str(x) for x in messages])
            
            return outMessage
        
        def info(self, *message):
            if self.isInfoEnabled:
                print(self._FormatMessage(message))

            if self.logFile is not None and self.logFileInfoEnabled:
                self.logFile.write(self._FormatMessage(message))
                self.logFile.write("\n")

        def dbg(self, *message):
            if self.isDbgEnabled:
                print(self._FormatMessage(message))

            if self.logFile is not None and self.logFileDbgEnabled:
                self.logFile.write(self._FormatMessage(message))
                self.logFile.write("\n")
        
        def error(self, *message):
            if self.isErrEnabled:
                sys.stderr.write(self._FormatMessage(message))
                sys.stderr.write("\n")

            if self.logFile is not None and self.logFileErrorEnabled:
                self.logFile.write(self._FormatMessage(message))
                self.logFile.write("\n")
        
    class AccuRev(object):
        @classmethod
        def fromxmlelement(cls, xmlElement):
            if xmlElement is not None and xmlElement.tag == 'accurev':
                depot    = xmlElement.attrib.get('depot')
                username = xmlElement.attrib.get('username')
                password = xmlElement.attrib.get('password')
                startTransaction = xmlElement.attrib.get('start-transaction')
                endTransaction   = xmlElement.attrib.get('end-transaction')
                commandCacheFilename = xmlElement.attrib.get('command-cache-filename')
                
                streamMap = None
                streamListElement = xmlElement.find('stream-list')
                if streamListElement is not None:
                    streamMap = OrderedDict()
                    streamElementList = streamListElement.findall('stream')
                    for streamElement in streamElementList:
                        streamName = streamElement.text
                        branchName = streamElement.attrib.get("branch-name")
                        if branchName is None:
                            branchName = streamName

                        streamMap[streamName] = branchName
                
                return cls(depot, username, password, startTransaction, endTransaction, streamMap, commandCacheFilename)
            else:
                return None
            
        def __init__(self, depot = None, username = None, password = None, startTransaction = None, endTransaction = None, streamMap = None, commandCacheFilename = None):
            self.depot    = depot
            self.username = username
            self.password = password
            self.startTransaction = startTransaction
            self.endTransaction   = endTransaction
            self.streamMap = streamMap
            self.commandCacheFilename = commandCacheFilename
    
        def __repr__(self):
            str = "Config.AccuRev(depot=" + repr(self.depot)
            str += ", username="          + repr(self.username)
            str += ", password="          + repr(self.password)
            str += ", startTransaction="  + repr(self.startTransaction)
            str += ", endTransaction="    + repr(self.endTransaction)
            if streamMap is not None:
                str += ", streamMap="    + repr(self.streamMap)
            str += ")"
            
            return str

        def UseCommandCache(self):
            return self.commandCacheFilename is not None
            
    class Git(object):
        @classmethod
        def fromxmlelement(cls, xmlElement):
            if xmlElement is not None and xmlElement.tag == 'git':
                repoPath = xmlElement.attrib.get('repo-path')
                finalize = xmlElement.attrib.get('finalize')
                if finalize is not None:
                    if finalize.lower() == "true":
                        finalize = True
                    elif finalize.lower() == "false":
                        finalize = False
                    else:
                        Exception("Error, could not parse finalize attribute '{0}'. Valid values are 'true' and 'false'.".format(finalize))
                
                remoteMap = OrderedDict()
                remoteElementList = xmlElement.findall('remote')
                for remoteElement in remoteElementList:
                    remoteName     = remoteElement.attrib.get("name")
                    remoteUrl      = remoteElement.attrib.get("url")
                    remotePushUrl  = remoteElement.attrib.get("push-url")
                    
                    remoteMap[remoteName] = git.GitRemoteListItem(name=remoteName, url=remoteUrl, pushUrl=remotePushUrl)

                return cls(repoPath=repoPath, finalize=finalize, remoteMap=remoteMap)
            else:
                return None
            
        def __init__(self, repoPath, finalize=None, remoteMap=None):
            self.repoPath  = repoPath
            self.finalize  = finalize
            self.remoteMap = remoteMap

        def __repr__(self):
            str = "Config.Git(repoPath=" + repr(self.repoPath)
            str += ", finalize="         + repr(self.finalize)
            str += ", remoteMap="        + repr(self.remoteMap)
            str += ")"
            
            return str
            
    class UserMap(object):
        @classmethod
        def fromxmlelement(cls, xmlElement):
            if xmlElement is not None and xmlElement.tag == 'map-user':
                accurevUsername = None
                gitName         = None
                gitEmail        = None
                timezone        = None
                
                accurevElement = xmlElement.find('accurev')
                if accurevElement is not None:
                    accurevUsername = accurevElement.attrib.get('username')
                gitElement = xmlElement.find('git')
                if gitElement is not None:
                    gitName  = gitElement.attrib.get('name')
                    gitEmail = gitElement.attrib.get('email')
                    timezone = gitElement.attrib.get('timezone')
                
                return cls(accurevUsername=accurevUsername, gitName=gitName, gitEmail=gitEmail, timezone=timezone)
            else:
                return None
            
        def __init__(self, accurevUsername, gitName, gitEmail, timezone=None):
            self.accurevUsername = accurevUsername
            self.gitName         = gitName
            self.gitEmail        = gitEmail
            self.timezone        = timezone
    
        def __repr__(self):
            str = "Config.UserMap(accurevUsername=" + repr(self.accurevUsername)
            str += ", gitName="                     + repr(self.gitName)
            str += ", gitEmail="                    + repr(self.gitEmail)
            str += ", timezone="                    + repr(self.timezone)
            str += ")"
            
            return str
            
    class Include(object):
        @classmethod
        def fromxmlelement(cls, xmlElement):
            if xmlElement is not None and xmlElement.tag == 'include':
                filename = xmlElement.attrib.get('filename')

        def __init__(self, filename):
            self.filename = filename

        def __repr(self):
            str = "Config.Include(filename=" + repr(self.filename)
            str += ")"

            return str

    @staticmethod
    def FilenameFromScriptName(scriptName):
        (root, ext) = os.path.splitext(scriptName)
        return root + '.config.xml'

    @classmethod
    def fromxmlstring(cls, xmlString):
        # Load the XML
        xmlRoot = ElementTree.fromstring(xmlString)
        
        if xmlRoot is not None and xmlRoot.tag == "accurev2git":
            accurev = Config.AccuRev.fromxmlelement(xmlRoot.find('accurev'))
            git     = Config.Git.fromxmlelement(xmlRoot.find('git'))
            
            method = "diff" # Defaults to diff
            methodElem = xmlRoot.find('method')
            if methodElem is not None:
                method = methodElem.text

            logFilename = None
            logFileElem = xmlRoot.find('logfile')
            if logFileElem is not None:
                logFilename = logFileElem.text

            usermaps = []
            userMapsElem = xmlRoot.find('usermaps')
            if userMapsElem is not None:
                for userMapElem in userMapsElem.findall('map-user'):
                    usermaps.append(Config.UserMap.fromxmlelement(userMapElem))
            
            includes = []
            for includeElem in xmlRoot.findall('include'):
                includes.append(Config.Include.fromxmlelement(includeElem))

            return cls(accurev=accurev, git=git, usermaps=usermaps, method=method, logFilename=logFilename, includes=includes)
        else:
            # Invalid XML for an accurev2git configuration file.
            return None

    @staticmethod
    def fromfile(filename):
        config = None
        if os.path.exists(filename):
            with codecs.open(filename) as f:
                configXml = f.read()
                config = Config.fromxmlstring(configXml)
            if config is not None and len(config.includes) != 0:
                print("WARNING: Ignoring includes. Not yet implemented!", file=sys.stderr)
        return config

    def __init__(self, accurev = None, git = None, usermaps = None, method = None, logFilename = None, includes = []):
        self.accurev     = accurev
        self.git         = git
        self.usermaps    = usermaps
        self.method      = method
        self.logFilename = logFilename
        self.logger      = Config.Logger()
        self.includes    = includes
        
    def __repr__(self):
        str = "Config(accurev=" + repr(self.accurev)
        str += ", git="         + repr(self.git)
        str += ", usermaps="    + repr(self.usermaps)
        str += ")"
        
        return str

# Prescribed recepie:
# - Get the list of tracked streams from the config file.
# - For each stream in the list
#   + If this stream is new (there is no data in git for it yet)
#     * Create the git branch for the stream
#     * Get the stream create (mkstream) transaction number and set it to be the start-transaction. Note: The first stream in the depot has no mkstream transaction.
#   + otherwise
#     * Get the last processed transaction number and set that to be the start-transaction.
#     * Obtain a diff from accurev listing all of the files that have changed and delete them all.
#   + Get the end-transaction from the user or from accurev's highest/now keyword for the hist command.
#   + For all transactions between the start-transaction and end-transaction
#     * Checkout the git branch at latest (or just checkout if no-commits yet).
#     * Populate the retrieved transaction with the recursive option but without the overwrite option (quick).
#     * Preserve empty directories by adding .gitignore files.
#     * Commit the current state of the directory but don't respect the .gitignore file contents. (in case it was added to accurev in the past).
#     * Increment the transaction number by one
#     * Obtain a diff from accurev listing all of the files that have changed and delete them all.
class AccuRev2Git(object):
    gitNotesRef_AccurevHistXml = 'accurev/xml/hist'
    gitNotesRef_AccurevHist    = 'accurev/hist'

    commandFailureRetryCount = 3

    def __init__(self, config):
        self.config = config
        self.cwd = None
        self.gitRepo = None
        self.gitBranchList = None

    # Returns True if the path was deleted, otherwise false
    def DeletePath(self, path):
        if os.path.lexists(path):
            if os.path.islink(path):
                os.unlink(path)
            elif os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
            
        return not os.path.lexists(path)
   
    def ClearGitRepo(self):
        # Delete everything except the .git folder from the destination (git repo)
        self.config.logger.dbg( "Clear git repo." )
        for root, dirs, files in os.walk(self.gitRepo.path, topdown=False):
            for name in files:
                path = os.path.join(root, name)
                if git.GetGitDirPrefix(path) is None:
                    self.DeletePath(path)
            for name in dirs:
                path = os.path.join(root, name)
                if git.GetGitDirPrefix(path) is None:
                    self.DeletePath(path)

    def PreserveEmptyDirs(self):
        preservedDirs = []
        for root, dirs, files in os.walk(self.gitRepo.path, topdown=True):
            for name in dirs:
                path = os.path.join(root, name).replace('\\','/')
                # Preserve empty directories that are not under the .git/ directory.
                if git.GetGitDirPrefix(path) is None and len(os.listdir(path)) == 0:
                    filename = os.path.join(path, '.gitignore')
                    with codecs.open(filename, 'w', 'utf-8') as file:
                        #file.write('# accurev2git.py preserve empty dirs\n')
                        preservedDirs.append(filename)
                    if not os.path.exists(filename):
                        self.config.logger.error("Failed to preserve directory. Couldn't create '{0}'.".format(filename))
        return preservedDirs

    def DeleteEmptyDirs(self):
        deletedDirs = []
        for root, dirs, files in os.walk(self.gitRepo.path, topdown=True):
            for name in dirs:
                path = os.path.join(root, name).replace('\\','/')
                # Delete empty directories that are not under the .git/ directory.
                if git.GetGitDirPrefix(path) is None:
                    dirlist = os.listdir(path)
                    count = len(dirlist)
                    delete = (len(dirlist) == 0)
                    if len(dirlist) == 1 and '.gitignore' in dirlist:
                        with codecs.open(os.path.join(path, '.gitignore')) as gi:
                            contents = gi.read().strip()
                            delete = (len(contents) == 0)
                    if delete:
                        if not self.DeletePath(path):
                            self.config.logger.error("Failed to delete empty directory '{0}'.".format(path))
                            raise Exception("Failed to delete '{0}'".format(path))
                        else:
                            deletedDirs.append(path)
        return deletedDirs

    def GetGitUserFromAccuRevUser(self, accurevUsername):
        if accurevUsername is not None:
            for usermap in self.config.usermaps:
                if usermap.accurevUsername == accurevUsername:
                    return "{0} <{1}>".format(usermap.gitName, usermap.gitEmail)
        state.config.logger.error("Cannot find git details for accurev username {0}".format(accurevUsername))
        return accurevUsername

    def GetGitTimezoneFromDelta(self, time_delta):
        seconds = time_delta.total_seconds()
        absSec = abs(seconds)
        offset = (int(absSec / 3600) * 100) + (int(absSec / 60) % 60)
        if seconds < 0:
            offset = -offset
        return offset

    def GetDeltaFromGitTimezone(self, timezone):
        # Git timezone strings follow the +0100 format
        tz = int(timezone)
        tzAbs = abs(tz)
        tzdelta = timedelta(seconds=((int(tzAbs / 100) * 3600) + ((tzAbs % 100) * 60)))
        return tzdelta

    def GetGitDatetime(self, accurevUsername, accurevDatetime):
        usertime = accurevDatetime
        tz = None
        if accurevUsername is not None:
            for usermap in self.config.usermaps:
                if usermap.accurevUsername == accurevUsername:
                    tz = usermap.timezone
                    break

        if tz is None:
            # Take the following default times 48 hours from Epoch as reference to compute local time.
            refTimestamp = 172800
            utcRefTime = datetime.utcfromtimestamp(refTimestamp)
            refTime = datetime.fromtimestamp(refTimestamp)

            tzdelta = (refTime - utcRefTime)
            usertime = accurevDatetime + tzdelta
            
            tz = self.GetGitTimezoneFromDelta(tzdelta)
        else:
            match = re.match(r'^[+-][0-9]{4}$', tz)
            if match:
                # This is the git style format
                tzdelta = self.GetDeltaFromGitTimezone(tz)
                usertime = accurevDatetime + tzdelta
                tz = int(tz)
            else:
                # Assuming it is an Olson timezone format
                userTz = pytz.timezone(tz)
                usertime = userTz.localize(accurevDatetime)
                tzdelta = usertime.utcoffset() # We need two aware times to get the datetime.timedelta.
                usertime = accurevDatetime + tzdelta # Adjust the time by the timezone since localize din't.
                tz = self.GetGitTimezoneFromDelta(tzdelta)

        return usertime, tz
    
    def GetGitDatetimeStr(self, accurevUsername, accurevDatetime):
        usertime, tz = self.GetGitDatetime(accurevUsername=accurevUsername, accurevDatetime=accurevDatetime)

        gitDatetimeStr = None
        if usertime is not None:
            gitDatetimeStr = "{0}".format(usertime.isoformat())
            if tz is not None:
                gitDatetimeStr = "{0} {1:+05}".format(gitDatetimeStr, tz)
        return gitDatetimeStr

    # Adds a JSON string respresentation of `stateDict` to the given commit using `git notes add`.
    def AddScriptStateNote(self, depotName, stream, transaction, commitHash, ref, committer=None, committerDate=None, committerTimezone=None):
        stateDict = { "depot": depotName, "stream": stream.name, "stream_number": stream.streamNumber, "transaction_number": transaction.id, "transaction_kind": transaction.Type }
        notesFilePath = None
        with tempfile.NamedTemporaryFile(mode='w+', prefix='ac2git_note_', delete=False) as notesFile:
            notesFilePath = notesFile.name
            notesFile.write(json.dumps(stateDict))


        if notesFilePath is not None:
            rv = self.gitRepo.notes.add(messageFile=notesFilePath, obj=commitHash, ref=ref, force=True, committer=committer, committerDate=committerDate, committerTimezone=committerTimezone, author=committer, authorDate=committerDate, authorTimezone=committerTimezone)
            os.remove(notesFilePath)

            if rv is not None:
                self.config.logger.dbg( "Added script state note for {0}.".format(commitHash) )
            else:
                self.config.logger.error( "Failed to add script state note for {0}, tr. {1}".format(commitHash, transaction.id) )
                self.config.logger.error(self.gitRepo.lastStderr)
            
            return rv
        else:
            self.config.logger.error( "Failed to create temporary file for script state note for {0}, tr. {1}".format(commitHash, transaction.id) )
        
        return None

    def AddAccurevHistNote(self, commitHash, ref, depot, transaction, committer=None, committerDate=None, committerTimezone=None, isXml=False):
        # Write the commit notes consisting of the accurev hist xml output for the given transaction.
        # Note: It is important to use the depot instead of the stream option for the accurev hist command since if the transaction
        #       did not occur on that stream we will get the closest transaction that was a promote into the specified stream instead of an error!
        arHistXml = accurev.raw.hist(depot=depot, timeSpec="{0}.1".format(transaction.id), isXmlOutput=isXml)
        notesFilePath = None
        with tempfile.NamedTemporaryFile(mode='w+', prefix='ac2git_note_', delete=False) as notesFile:
            notesFilePath = notesFile.name
            if arHistXml is None or len(arHistXml) == 0:
                self.config.logger.error('accurev hist returned an empty xml for transaction {0} (commit {1})'.format(transaction.id, commitHash))
                return False
            else:
                notesFile.write(arHistXml)
        
        if notesFilePath is not None:        
            rv = self.gitRepo.notes.add(messageFile=notesFilePath, obj=commitHash, ref=ref, force=True, committer=committer, committerDate=committerDate, committerTimezone=committerTimezone, author=committer, authorDate=committerDate, authorTimezone=committerTimezone)
            os.remove(notesFilePath)
        
            if rv is not None:
                self.config.logger.dbg( "Added accurev hist{0} note for {1}.".format(' xml' if isXml else '', commitHash) )
            else:
                self.config.logger.error( "Failed to add accurev hist{0} note for {1}".format(' xml' if isXml else '', commitHash) )
                self.config.logger.error(self.gitRepo.lastStderr)
            
            return rv
        else:
            self.config.logger.error( "Failed to create temporary file for accurev hist{0} note for {1}".format(' xml' if isXml else '', commitHash) )

        return None

    def GetFirstTransaction(self, depot, streamName, startTransaction=None, endTransaction=None):
        # Get the stream creation transaction (mkstream). Note: The first stream in the depot doesn't have an mkstream transaction.
        for i in range(0, AccuRev2Git.commandFailureRetryCount):
            mkstream = accurev.hist(stream=streamName, transactionKind="mkstream", timeSpec="now")
            if mkstream is not None:
                break
        if mkstream is None:
            return None

        tr = None
        if len(mkstream.transactions) == 0:
            self.config.logger.info( "The root stream has no mkstream transaction. Starting at transaction 1." )
            # the assumption is that the depot name matches the root stream name (for which there is no mkstream transaction)
            firstTr = self.TryHist(depot=depot, trNum="1")
            if firstTr is None or len(firstTr.transactions) == 0:
                raise Exception("Error: assumption that the root stream has the same name as the depot doesn't hold. Aborting...")
            tr = firstTr.transactions[0]
        else:
            tr = mkstream.transactions[0]
            if len(mkstream.transactions) != 1:
                self.config.logger.error( "There seem to be multiple mkstream transactions for this stream... Using {0}".format(tr.id) )

        if startTransaction is not None:
            startTrHist = self.TryHist(depot=depot, trNum=startTransaction)
            if startTrHist is None:
                return None

            startTr = startTrHist.transactions[0]
            if tr.id < startTr.id:
                self.config.logger.info( "The first transaction (#{0}) for stream {1} is earlier than the conversion start transaction (#{2}).".format(tr.id, streamName, startTr.id) )
                tr = startTr
        if endTransaction is not None:
            endTrHist = self.TryHist(depot=depot, trNum=endTransaction)
            if endTrHist is None:
                return None

            endTr = endTrHist.transactions[0]
            if endTr.id < tr.id:
                self.config.logger.info( "The first transaction (#{0}) for stream {1} is later than the conversion end transaction (#{2}).".format(tr.id, streamName, startTr.id) )
                tr = None

        return tr

    def GetLastCommitHash(self, branchName=None):
        cmd = []
        for i in range(0, AccuRev2Git.commandFailureRetryCount):
            cmd = [u'git', u'log', u'-1', u'--format=format:%H']
            if branchName is not None:
                cmd.append(branchName)
            commitHash = self.gitRepo.raw_cmd(cmd)
            if commitHash is not None:
                commitHash = commitHash.strip()
                if len(commitHash) == 0:
                    commitHash = None
                else:
                    break

        if commitHash is None:
            self.config.logger.error("Failed to retrieve last git commit hash. Command `{0}` failed.".format(' '.join(cmd)))

        return commitHash

    def GetStateForCommit(self, commitHash, branchName=None):
        stateObj = None

        # Try and search the branch namespace.
        if branchName is None:
            branchName = AccuRev2Git.gitNotesRef_AccurevHistXml

        stateJson = None
        for i in range(0, AccuRev2Git.commandFailureRetryCount):
            stateJson = self.gitRepo.notes.show(obj=commitHash, ref=branchName)
            if stateJson is not None:
                break

        if stateJson is not None:
            stateJson = stateJson.strip()
            stateObj = json.loads(stateJson)
        else:
            self.config.logger.error("Failed to load the last transaction for commit {0} from {1} notes.".format(commitHash, branchName))
            self.config.logger.error("  i.e git notes --ref={0} show {1}    - returned nothing.".format(branchName, commitHash))

        return stateObj

    def GetHistForCommit(self, commitHash, branchName=None, stateObj=None):
        #self.config.logger.dbg("GetHistForCommit(commitHash={0}, branchName={1}, stateObj={2}".format(commitHash, branchName, stateObj))
        if stateObj is None:
            stateObj = self.GetStateForCommit(commitHash=commitHash, branchName=branchName)
        if stateObj is not None:
            trNum = stateObj["transaction_number"]
            depot = stateObj["depot"]
            if trNum is not None and depot is not None:
                hist = accurev.hist(depot=depot, timeSpec=trNum, useCache=self.config.accurev.UseCommandCache())
                return hist
        return None

    def CreateCleanGitBranch(self, branchName):
        # Create the git branch.
        self.config.logger.info( "Creating {0}".format(branchName) )
        self.gitRepo.checkout(branchName=branchName, isOrphan=True)

        # Clear the index as it may contain the [start-point] info...
        self.gitRepo.rm(fileList=['.'], force=True, recursive=True)
        self.ClearGitRepo()

    def Commit(self, depot, stream, transaction, branchName=None, isFirstCommit=False, allowEmptyCommit=False, noNotes=False, messageOverride=None):
        self.PreserveEmptyDirs()

        # Add all of the files to the index
        self.gitRepo.add(force=True, all=True, gitOpts=[u'-c', u'core.autocrlf=false'])

        # Make the first commit
        messageFilePath = None
        with tempfile.NamedTemporaryFile(mode='w+', prefix='ac2git_commit_', delete=False) as messageFile:
            messageFilePath = messageFile.name
            if messageOverride is not None:
                messageFile.write(messageOverride)
            elif transaction.comment is None or len(transaction.comment) == 0:
                messageFile.write(' ') # White-space is always stripped from commit messages. See the git commit --cleanup option for details.
            else:
                # In git the # at the start of the line indicate that this line is a comment inside the message and will not be added.
                # So we will just add a space to the start of all the lines starting with a # in order to preserve them.
                messageFile.write(transaction.comment)
        
        if messageFilePath is None:
            self.config.logger.error("Failed to create temporary file for commit message for transaction {0}, stream {1}, branch {2}".format(transaction.id, stream, branchName))
            return None

        committer = self.GetGitUserFromAccuRevUser(transaction.user)
        committerDate, committerTimezone = self.GetGitDatetime(accurevUsername=transaction.user, accurevDatetime=transaction.time)
        if not isFirstCommit:
            lastCommitHash = self.GetLastCommitHash()
            if lastCommitHash is None:
                self.config.logger.info("No last commit hash available. Non-fatal error, continuing.")
        else:
            lastCommitHash = None
        commitHash = None

        # Since the accurev.obj namespace is populated from the XML output of accurev commands all times are given in UTC.
        # For now just force the time to be UTC centric but preferrably we would have this set-up to either use the local timezone
        # or allow each user to be given a timezone for geographically distributed teams...
        # The PyTz library should be considered for the timezone conversions. Do not roll your own...
        if self.gitRepo.commit(messageFile=messageFilePath, committer=committer, committer_date=committerDate, committer_tz=committerTimezone, author=committer, date=committerDate, tz=committerTimezone, allow_empty_message=True, allow_empty=allowEmptyCommit, gitOpts=[u'-c', u'core.autocrlf=false']):
            commitHash = self.GetLastCommitHash()
            if commitHash is not None:
                if lastCommitHash != commitHash:
                    self.config.logger.dbg( "Committed {0}".format(commitHash) )
                    if not noNotes:
                        xmlNoteWritten = False
                        for i in range(0, AccuRev2Git.commandFailureRetryCount):
                            ref = branchName
                            if ref is None:
                                ref = AccuRev2Git.gitNotesRef_AccurevHistXml
                                self.config.logger.error("Commit to an unspecified branch. Using default `git notes` ref for the script [{0}] at current time.".format(ref))
                            stateNoteWritten = ( self.AddScriptStateNote(depotName=depot, stream=stream, transaction=transaction, commitHash=commitHash, ref=ref, committer=committer, committerDate=committerDate, committerTimezone=committerTimezone) is not None )
                            if stateNoteWritten:
                                break
                        if not stateNoteWritten:
                            # The XML output in the notes is how we track our conversion progress. It is not acceptable for it to fail.
                            # Undo the commit and print an error.
                            branchName = 'HEAD'
                            self.config.logger.error("Couldn't record last transaction state. Undoing the last commit {0} with `git reset --hard {1}^`".format(commitHash, branchName))
                            self.gitRepo.raw_cmd([u'git', u'reset', u'--hard', u'{0}^'.format(branchName)])
    
                            return None
                else:
                    self.config.logger.error("Commit command returned True when nothing was committed...? Last commit hash {0} didn't change after the commit command executed.".format(lastCommitHash))
                    return None
            else:
                self.config.logger.error("Failed to commit! No last hash available.")
                return None
        elif "nothing to commit" in self.gitRepo.lastStdout:
            self.config.logger.dbg( "nothing to commit after populating transaction {0}...?".format(transaction.id) )
        else:
            self.config.logger.error( "Failed to commit transaction {0}".format(transaction.id) )
            self.config.logger.error( "\n{0}\n{1}\n".format(self.gitRepo.lastStdout, self.gitRepo.lastStderr) )
        os.remove(messageFilePath)

        return commitHash

    def TryDiff(self, streamName, firstTrNumber, secondTrNumber):
        for i in range(0, AccuRev2Git.commandFailureRetryCount):
            diff = accurev.diff(all=True, informationOnly=True, verSpec1=streamName, verSpec2=streamName, transactionRange="{0}-{1}".format(firstTrNumber, secondTrNumber), useCache=self.config.accurev.UseCommandCache())
            if diff is not None:
                break
        if diff is None:
            self.config.logger.error( "accurev diff failed! stream: {0} time-spec: {1}-{2}".format(streamName, firstTrNumber, secondTrNumber) )
        return diff
    
    def FindNextChangeTransaction(self, streamName, startTrNumber, endTrNumber, deepHist=None):
        # Iterate over transactions in order using accurev diff -a -i -v streamName -V streamName -t <lastProcessed>-<current iterator>
        if self.config.method == "diff":
            nextTr = startTrNumber + 1
            diff = self.TryDiff(streamName=streamName, firstTrNumber=startTrNumber, secondTrNumber=nextTr)
            if diff is None:
                return (None, None)
    
            # Note: This is likely to be a hot path. However, it cannot be optimized since a revert of a transaction would not show up in the diff even though the
            #       state of the stream was changed during that period in time. Hence to be correct we must iterate over the transactions one by one unless we have
            #       explicit knowlege of all the transactions which could affect us via some sort of deep history option...
            while nextTr <= endTrNumber and len(diff.elements) == 0:
                nextTr += 1
                diff = self.TryDiff(streamName=streamName, firstTrNumber=startTrNumber, secondTrNumber=nextTr)
                if diff is None:
                    return (None, None)
        
            self.config.logger.dbg("FindNextChangeTransaction diff: {0}".format(nextTr))
            return (nextTr, diff)
        elif self.config.method == "deep-hist":
            if deepHist is None:
                raise Exception("Script error! deepHist argument cannot be none when running a deep-hist method.")
            # Find the next transaction
            for tr in deepHist:
                if tr.id > startTrNumber:
                    diff = self.TryDiff(streamName=streamName, firstTrNumber=startTrNumber, secondTrNumber=tr.id)
                    if diff is None:
                        return (None, None)
                    elif len(diff.elements) > 0:
                        self.config.logger.dbg("FindNextChangeTransaction deep-hist: {0}".format(tr.id))
                        return (tr.id, diff)
                    else:
                        self.config.logger.dbg("FindNextChangeTransaction deep-hist skipping: {0}, diff was empty...".format(tr.id))

            diff = self.TryDiff(streamName=streamName, firstTrNumber=startTrNumber, secondTrNumber=endTrNumber)
            return (endTrNumber + 1, diff) # The end transaction number is inclusive. We need to return the one after it.
        elif self.config.method == "pop":
            self.config.logger.dbg("FindNextChangeTransaction pop: {0}".format(startTrNumber + 1))
            return (startTrNumber + 1, None)
        else:
            self.config.logger.error("Method is unrecognized, allowed values are 'pop', 'diff' and 'deep-hist'")
            raise Exception("Invalid configuration, method unrecognized!")

    def DeleteDiffItemsFromRepo(self, diff):
        # Delete all of the files which are even mentioned in the diff so that we can do a quick populate (wouth the overwrite option)
        deletedPathList = []
        for element in diff.elements:
            for change in element.changes:
                for stream in [ change.stream1, change.stream2 ]:
                    if stream is not None and stream.name is not None:
                        name = stream.name.replace('\\', '/').lstrip('/')
                        path = os.path.join(self.gitRepo.path, name)
                        if os.path.lexists(path): # Ensure that broken links are also deleted!
                            if not self.DeletePath(path):
                                self.config.logger.error("Failed to delete '{0}'.".format(path))
                                raise Exception("Failed to delete '{0}'".format(path))
                            else:
                                deletedPathList.append(path)

        return deletedPathList

    def TryHist(self, depot, trNum):
        for i in range(0, AccuRev2Git.commandFailureRetryCount):
            trHist = accurev.hist(depot=depot, timeSpec="{0}.1".format(trNum), useCache=self.config.accurev.UseCommandCache(), expandedMode=True, verboseMode=True)
            if trHist is not None:
                break
        return trHist

    def TryPop(self, streamName, transaction, overwrite=False):
        for i in range(0, AccuRev2Git.commandFailureRetryCount):
            popResult = accurev.pop(verSpec=streamName, location=self.gitRepo.path, isRecursive=True, isOverride=overwrite, timeSpec=transaction.id, elementList='.')
            if popResult:
                break
            else:
                self.config.logger.error("accurev pop failed:")
                for message in popResult.messages:
                    if message.error is not None and message.error:
                        self.config.logger.error("  {0}".format(message.text))
                    else:
                        self.config.logger.info("  {0}".format(message.text))
        
        return popResult

    def ProcessStream(self, depot, stream, branchName, startTransaction, endTransaction):
        self.config.logger.info( "Processing {0} -> {1} : {2} - {3}".format(stream.name, branchName, startTransaction, endTransaction) )

        # Find the matching git branch
        branch = None
        for b in self.gitBranchList:
            if branchName == b.name:
                branch = b
                break

        status = self.gitRepo.status()
        if status is None:
            raise Exception("Failed to get status of git repository!")

        self.config.logger.dbg( "On branch {branch} - {staged} staged, {changed} changed, {untracked} untracked files{initial_commit}.".format(branch=status.branch, staged=len(status.staged), changed=len(status.changed), untracked=len(status.untracked), initial_commit=', initial commit' if status.initial_commit else '') )
        if branch is not None:
            # Get the last processed transaction
            self.config.logger.dbg( "Clean current branch - '{br}'".format(br=status.branch) )
            self.gitRepo.clean(directories=True, force=True, forceSubmodules=True, includeIgnored=True)
            self.config.logger.dbg( "Reset current branch - '{br}'".format(br=status.branch) )
            self.gitRepo.reset(isHard=True)
            self.config.logger.dbg( "Checkout branch {branchName}".format(branchName=branchName) )
            self.gitRepo.checkout(branchName=branchName)
            status = self.gitRepo.status()
            self.config.logger.dbg( "On branch {branch} - {staged} staged, {changed} changed, {untracked} untracked files{initial_commit}.".format(branch=status.branch, staged=len(status.staged), changed=len(status.changed), untracked=len(status.untracked), initial_commit=', initial commit' if status.initial_commit else '') )
            if status is None:
                raise Exception("Invalid initial state! The status command return is invalid.")
            if status.branch is None or status.branch != branchName:
                raise Exception("Invalid initial state! The status command returned an invalid name for current branch. Expected {branchName} but got {statusBranch}.".format(branchName=branchName, statusBranch=status.branch))
            if len(status.staged) != 0 or len(status.changed) != 0 or len(status.untracked) != 0:
                raise Exception("Invalid initial state! There are changes in the tracking repository. Staged {staged}, changed {changed}, untracked {untracked}.".format(staged=status.staged, changed=status.changed, untracked=status.untracked))
        else:
            self.config.logger.dbg( "The branch list doesn't contain the branch '{br}'.".format(br=branchName) )

        tr = None
        commitHash = None
        if status.branch != branchName or status.initial_commit:
            # We are tracking a new stream:
            tr = self.GetFirstTransaction(depot=depot, streamName=stream.name, startTransaction=startTransaction, endTransaction=endTransaction)
            if tr is not None:
                if branch is None:
                    self.CreateCleanGitBranch(branchName=branchName)
                try:
                    destStream = self.GetDestinationStreamName(history=hist, depot=None)
                except:
                    destStream = None
                self.config.logger.dbg( "{0} pop (init): {1} {2}{3}".format(stream.name, tr.Type, tr.id, " to {0}".format(destStream) if destStream is not None else "") )
                popResult = self.TryPop(streamName=stream.name, transaction=tr, overwrite=True)
                if not popResult:
                    return (None, None)
                
                stream = accurev.show.streams(depot=depot, stream=stream.streamNumber, timeSpec=tr.id).streams[0]
                commitHash = self.Commit(depot=depot, stream=stream, transaction=tr, branchName=branchName, isFirstCommit=True)
                if not commitHash:
                    self.config.logger.dbg( "{0} first commit has failed. Is it an empty commit? Continuing...".format(stream.name) )
                else:
                    self.config.logger.info( "stream {0}: tr. #{1} {2} into {3} -> commit {4} on {5}".format(stream.name, tr.id, tr.Type, destStream if destStream is not None else 'unknown', commitHash[:8], branchName) )
            else:
                self.config.logger.info( "Failed to get the first transaction for {0} from accurev. Won't process any further.".format(stream.name) )
                return (None, None)
        else:
            # Get the last processed transaction
            commitHash = self.GetLastCommitHash(branchName=branchName)
            hist = self.GetHistForCommit(commitHash=commitHash, branchName=branchName)

            # This code should probably be controlled with some flag in the configuration/command line...
            if hist is None:
                self.config.logger.error("Repo in invalid state. Attempting to auto-recover.")
                resetCmd = ['git', 'reset', '--hard', '{0}^'.format(branchName)]
                self.config.logger.error("Deleting last commit from this branch using, {0}".format(' '.join(resetCmd)))
                try:
                    subprocess.check_call(resetCmd)
                except subprocess.CalledProcessError:
                    self.config.logger.error("Failed to reset branch. Aborting!")
                    return (None, None)

                commitHash = self.GetLastCommitHash(branchName=branchName)
                hist = self.GetHistForCommit(commitHash=commitHash, branchName=branchName)

                if hist is None:
                    self.config.logger.error("Repo in invalid state. Please reset this branch to a previous commit with valid notes.")
                    self.config.logger.error("  e.g. git reset --hard {0}~1".format(branchName))
                    return (None, None)

            tr = hist.transactions[0]
            stream = accurev.show.streams(depot=depot, stream=stream.streamNumber, timeSpec=tr.id).streams[0]
            self.config.logger.dbg("{0}: last processed transaction was #{1}".format(stream.name, tr.id))

        endTrHist = self.TryHist(depot=depot, trNum=endTransaction)
        if endTrHist is None:
            self.config.logger.dbg("accurev hist -p {0} -t {1}.1 failed.".format(depot, endTransaction))
            return (None, None)
        endTr = endTrHist.transactions[0]
        self.config.logger.info("{0}: processing transaction range #{1} - #{2}".format(stream.name, tr.id, endTr.id))

        deepHist = None
        if self.config.method == "deep-hist":
            ignoreTimelocks=False # The code for the timelocks is not tested fully yet. Once tested setting this to false should make the resulting set of transactions smaller
                                 # at the cost of slightly larger number of upfront accurev commands called.
            self.config.logger.dbg("accurev.ext.deep_hist(depot={0}, stream={1}, timeSpec='{2}-{3}', ignoreTimelocks={4})".format(depot, stream.name, tr.id, endTr.id, ignoreTimelocks))
            deepHist = accurev.ext.deep_hist(depot=depot, stream=stream.name, timeSpec="{0}-{1}".format(tr.id, endTr.id), ignoreTimelocks=ignoreTimelocks)
            self.config.logger.info("Deep-hist returned {count} transactions to process.".format(count=len(deepHist)))
            if deepHist is None:
                raise Exception("accurev.ext.deep_hist() failed to return a result!")
        while True:
            nextTr, diff = self.FindNextChangeTransaction(streamName=stream.name, startTrNumber=tr.id, endTrNumber=endTr.id, deepHist=deepHist)
            if nextTr is None:
                self.config.logger.dbg( "FindNextChangeTransaction(streamName='{0}', startTrNumber={1}, endTrNumber={2}, deepHist={3}) failed!".format(stream.name, tr.id, endTr.id, deepHist) )
                return (None, None)

            self.config.logger.dbg( "{0}: next transaction {1} (end tr. {2})".format(stream.name, nextTr, endTr.id) )
            if nextTr <= endTr.id:
                # Right now nextTr is an integer representation of our next transaction.
                # Delete all of the files which are even mentioned in the diff so that we can do a quick populate (wouth the overwrite option)
                popOverwrite = (self.config.method == "pop")
                deletedPathList = None
                if self.config.method == "pop":
                    self.ClearGitRepo()
                else:
                    if diff is None:
                        return (None, None)
                    
                    try:
                        deletedPathList = self.DeleteDiffItemsFromRepo(diff=diff)
                    except:
                        popOverwrite = True
                        self.config.logger.info("Error trying to delete changed elements. Fatal, aborting!")
                        # This might be ok only in the case when the files/directories were changed but not in the case when there
                        # was a deletion that occurred. Abort and be safe!
                        # TODO: This must be solved somehow since this could hinder this script from continuing at all!
                        return (None, None)

                    # Remove all the empty directories (this includes directories which contain an empty .gitignore file since that's what we is done to preserve them)
                    try:
                        self.DeleteEmptyDirs()
                    except:
                        popOverwrite = True
                        self.config.logger.info("Error trying to delete empty directories. Fatal, aborting!")
                        # This might be ok only in the case when the files/directories were changed but not in the case when there
                        # was a deletion that occurred. Abort and be safe!
                        # TODO: This must be solved somehow since this could hinder this script from continuing at all!
                        return (None, None)

                # The accurev hist command here must be used with the depot option since the transaction that has affected us may not
                # be a promotion into the stream we are looking at but into one of its parent streams. Hence we must query the history
                # of the depot and not the stream itself.
                hist = self.TryHist(depot=depot, trNum=nextTr)
                if hist is None:
                    self.config.logger.dbg("accurev hist -p {0} -t {1}.1 failed.".format(depot, endTransaction))
                    return (None, None)
                tr = hist.transactions[0]
                stream = accurev.show.streams(depot=depot, stream=stream.streamNumber, timeSpec=tr.id).streams[0]

                # Populate
                #destStream = self.GetDestinationStreamName(history=hist, depot=depot) # Slower: This performes an extra accurev.show.streams() command for correct stream names.
                destStream = self.GetDestinationStreamName(history=hist, depot=None) # Quicker: This does not perform an extra accurev.show.streams() command for correct stream names.
                self.config.logger.dbg( "{0} pop: {1} {2}{3}".format(stream.name, tr.Type, tr.id, " to {0}".format(destStream) if destStream is not None else "") )

                popResult = self.TryPop(streamName=stream.name, transaction=tr, overwrite=popOverwrite)
                if not popResult:
                    return (None, None)

                # Commit
                commitHash = self.Commit(depot=depot, stream=stream, transaction=tr, branchName=branchName, isFirstCommit=False)
                if commitHash is None:
                    if"nothing to commit" in self.gitRepo.lastStdout:
                        if diff is not None:
                            self.config.logger.dbg( "diff info ({0} elements):".format(len(diff.elements)) )
                            for element in diff.elements:
                                for change in element.changes:
                                    self.config.logger.dbg( "  what changed: {0}".format(change.what) )
                                    self.config.logger.dbg( "  original: {0}".format(change.stream1) )
                                    self.config.logger.dbg( "  new:      {0}".format(change.stream2) )
                        if deletedPathList is not None:
                            self.config.logger.dbg( "deleted {0} files:".format(len(deletedPathList)) )
                            for p in deletedPathList:
                                self.config.logger.dbg( "  {0}".format(p) )
                            self.config.logger.dbg( "populated {0} files:".format(len(popResult.elements)) )
                            for e in popResult.elements:
                                self.config.logger.dbg( "  {0}".format(e.location) )
                        self.config.logger.info("stream {0}: tr. #{1} is a no-op. Potential but unlikely error. Continuing.".format(stream.name, tr.id))
                    else:
                        break # Early return from processing this stream. Restarting should clean everything up.
                else:
                    self.config.logger.info( "stream {0}: tr. #{1} {2} into {3} -> commit {4} on {5}".format(stream.name, tr.id, tr.Type, destStream if destStream is not None else 'unknown', commitHash[:8], branchName) )
            else:
                self.config.logger.info( "Reached end transaction #{0} for {1} -> {2}".format(endTr.id, stream.name, branchName) )
                break

        return (tr, commitHash)

    def ProcessStreams(self):
        if self.config.accurev.commandCacheFilename is not None:
            accurev.ext.enable_command_cache(self.config.accurev.commandCacheFilename)
        
        for stream in self.config.accurev.streamMap:
            branch = self.config.accurev.streamMap[stream]
            depot  = self.config.accurev.depot
            streamInfo = None
            try:
                streamInfo = accurev.show.streams(depot=depot, stream=stream).streams[0]
            except IndexError:
                self.config.logger.error( "Failed to get stream information. `accurev show streams -p {0} -s {1}` returned no streams".format(depot, stream) )
                return
            except AttributeError:
                self.config.logger.error( "Failed to get stream information. `accurev show streams -p {0} -s {1}` returned None".format(depot, stream) )
                return

            if depot is None or len(depot) == 0:
                depot = streamInfo.depotName
            tr, commitHash = self.ProcessStream(depot=depot, stream=streamInfo, branchName=branch, startTransaction=self.config.accurev.startTransaction, endTransaction=self.config.accurev.endTransaction)
            if tr is None or commitHash is None:
                self.config.logger.error( "Error while processing stream {0}, branch {1}".format(stream, branch) )

            if self.config.git.remoteMap is not None:
                refspec = "refs/heads/{branch}:refs/heads/{branch} refs/notes/{branch}:refs/notes/{branch}".format(branch=branch)
                for remoteName in self.config.git.remoteMap:
                    pushOutput = None
                    try:
                        pushCmd = "git push {remote} {refspec}".format(remote=remoteName, refspec=refspec)
                        pushOutput = subprocess.check_output(pushCmd.split(), stderr=subprocess.STDOUT).decode('utf-8')
                        self.config.logger.info("Push to '{remote}' succeeded:".format(remote=remoteName))
                        self.config.logger.info(pushOutput)
                    except subprocess.CalledProcessError as e:
                        self.config.logger.error("Push to '{remote}' failed!".format(remote=remoteName))
                        self.config.logger.dbg("'{cmd}', returned {returncode} and failed with:".format(cmd="' '".join(e.cmd), returncode=e.returncode))
                        self.config.logger.dbg("{output}".format(output=e.output.decode('utf-8')))
        
        if self.config.accurev.commandCacheFilename is not None:
            accurev.ext.disable_command_cache()

    def AppendCommitMessageSuffixStreamInfo(self, suffixList, linePrefix, stream):
        if stream is not None:
            suffixList.append('{linePrefix}-id: {id}'.format(linePrefix=linePrefix, id=stream.streamNumber))
            suffixList.append('{linePrefix}-name: {name}'.format(linePrefix=linePrefix, name=stream.name))
            if stream.prevName is not None:
                suffixList.append('{linePrefix}-prev-name: {name}'.format(linePrefix=linePrefix, name=stream.prevName))
            suffixList.append('{linePrefix}-basis-id: {id}'.format(linePrefix=linePrefix, id=stream.basisStreamNumber))
            suffixList.append('{linePrefix}-basis-name: {name}'.format(linePrefix=linePrefix, name=stream.basis))
            if stream.prevBasis is not None and len(stream.prevBasis) > 0:
                suffixList.append('{linePrefix}-prev-basis-id: {id}'.format(linePrefix=linePrefix, id=stream.prevBasisStreamNumber))
                suffixList.append('{linePrefix}-prev-basis-name: {name}'.format(linePrefix=linePrefix, name=stream.prevBasis))


    def GenerateCommitMessageSuffix(self, transaction, dstStream=None, srcStream=None):
        suffixList = []
        suffixList.append('Accurev-transaction-id: {id}'.format(id=transaction.id))
        suffixList.append('Accurev-transaction-type: {t}'.format(t=transaction.Type))
        if dstStream is not None:
            self.AppendCommitMessageSuffixStreamInfo(suffixList=suffixList, linePrefix='Accurev-stream', stream=dstStream)
        if srcStream is not None:
            self.AppendCommitMessageSuffixStreamInfo(suffixList=suffixList, linePrefix='Accurev-from-stream', stream=srcStream)
        
        return '\n'.join(suffixList)

    def GenerateCommitMessage(self, transaction, dstStream=None, srcStream=None, title=None, friendlyMessage=None):
        messageSections = []

        if title is not None:
            messageSections.append(title)
        if transaction.comment is not None:
            messageSections.append(transaction.comment)
        if friendlyMessage is not None:
            messageSections.append(friendlyMessage)
        suffix = self.GenerateCommitMessageSuffix(transaction=transaction, dstStream=dstStream, srcStream=srcStream)
        if suffix is not None:
            messageSections.append(suffix)
        
        return '\n\n'.join(messageSections)

    def GitCommitOrMerge(self, depot, dstStream, srcStream, tr, messageOverride=None):
        # Perform the git merge of the 'from stream' into the 'to stream' but only if they have the same contents.
        dstBranchName = self.SanitizeBranchName(dstStream.name)
        srcBranchName = self.SanitizeBranchName(srcStream.name)
        if self.gitRepo.checkout(branchName=dstBranchName) is None:
            raise Exception("git checkout branch {br}, failed!".format(br=dstBranchName))
        
        diff = self.TryDiff(streamName=dstStream.name, firstTrNumber=(tr.id - 1), secondTrNumber=tr.id)
        deletedPathList = self.DeleteDiffItemsFromRepo(diff=diff)
        popResult = self.TryPop(streamName=dstStream.name, transaction=tr)

        commitHash = self.Commit(depot=depot, stream=dstStream, transaction=tr, branchName=dstBranchName, allowEmptyCommit=True, noNotes=True, messageOverride=messageOverride)
        if commitHash is None:
            raise Exception("Failed to commit promote {tr}!".format(tr=tr.id))
        diff = self.gitRepo.raw_cmd([u'git', u'diff', u'--stat', dstBranchName, srcBranchName])
        if diff is None:
            raise Exception("Failed to get tree for new branch {br}!".format(br=dstBranchName))
        
        if len(diff.strip()) == 0:
            # Merge
            if self.gitRepo.reset(branch="HEAD^") is None:
                raise Exception("Failed to undo commit! git reset HEAD^, failed with: {err}".format(err=self.gitRepo.lastStderr))
            if self.gitRepo.raw_cmd([u'git', u'merge', u'--no-ff', u'--no-commit', u'-s', u'ours', srcBranchName]) is None:
                raise Exception("Failed to merge! Failed with: {err}".format(err=self.gitRepo.lastStderr))
            
            diff = self.TryDiff(streamName=dstStream.name, firstTrNumber=(tr.id - 1), secondTrNumber=tr.id)
            deletedPathList = self.DeleteDiffItemsFromRepo(diff=diff)
            popResult = self.TryPop(streamName=dstStream.name, transaction=tr)

            commitHash = self.Commit(depot=depot, stream=dstStream, transaction=tr, branchName=dstBranchName, allowEmptyCommit=True, noNotes=True, messageOverride=messageOverride)
            if commitHash is None:
                raise Exception("Failed to re-commit merged promote {tr}!".format(tr=tr.id))
            self.config.logger.info("Merged, branch {src} into {dst}, {commit}".format(src=srcBranchName, dst=dstBranchName, commit=commitHash))
        else:
            # This too could be a merge. What we should check is if it is possible to find a commit on the srcStream "branch" 
            # whose diff against the merge base of this branch introduces "the same" changes as this promote...
            # Since it is too hard for now, just ignore this potential case and continue...
            # Cherry-pick
            self.config.logger.info("Cherry-picked, branch {src} into {dst}, {commit}".format(src=srcBranchName, dst=dstBranchName, commit=commitHash))

        return commitHash

    def SanitizeBranchName(self, name):
        name = name.replace(' ', '_')
        return name

    def ProcessTransaction(self, depot, transaction):
        trHist = self.TryHist(depot=depot, trNum=transaction)
        if trHist is None or len(trHist.transactions) == 0 is None:
            raise Exception("Couldn't get history for transaction {tr}. Aborting!".format(tr=transaction))
        
        tr = trHist.transactions[0]
        self.config.logger.dbg( "Transaction #{tr} - {Type} by {user} to {stream} at {time}".format(tr=tr.id, Type=tr.Type, time=tr.time, user=tr.user, stream=tr.toStream()[0]) )
        
        if tr.Type == "mkstream":
            # Old versions of accurev don't tell you the name of the stream that was created in the mkstream transaction.
            # The only way to find out what stream was created is to diff the output of the `accurev show streams` command
            # between the mkstream transaction and the one that preceedes it.
            streams = accurev.show.streams(depot=depot, timeSpec=transaction)
            newStream = None
            if transaction == 1:
                newStream = streams.streams[0]
            else:
                streamSet = set()
                oldStreams = accurev.show.streams(depot=depot, timeSpec=(transaction - 1))
                for stream in oldStreams.streams:
                    streamSet.add(stream.name)
                for stream in streams.streams:
                    if stream.name not in streamSet:
                        newStream = stream
                        break
                basisBranchName = self.SanitizeBranchName(newStream.basis)
                if self.gitRepo.checkout(branchName=basisBranchName) is None:
                    raise Exception("Failed to checkout basis stream branch {bsBr} for stream {s}".format(bsBr=basisBranchName, s=newStream.name))
            
            newBranchName = self.SanitizeBranchName(newStream.name)
            if self.gitRepo.checkout(branchName=newBranchName, isNewBranch=True) is None:
                raise Exception("Failed to create new branch {br}. Error: {err}".format(br=newBranchName, err=self.gitRepo.lastStderr))
            self.config.logger.info("mkstream name={name}, number={num}, basis={basis}, basis-number={basisNumber}".format(name=newStream.name, num=newStream.streamNumber, basis=newStream.basis, basisNumber=newStream.basisStreamNumber))
            # Modify the commit message
            commitMessage = self.GenerateCommitMessage(transaction=tr, dstStream=newStream, title="Created {name} based on {basis}".format(name=newBranchName, basis='-' if newStream.basis is None else basisBranchName))
            commitHash = self.Commit(depot=depot, stream=newStream, transaction=tr, branchName=newBranchName, isFirstCommit=True, allowEmptyCommit=True, noNotes=True, messageOverride=commitMessage)
            if commitHash is None:
                raise Exception("Failed to add empty mkstream commit")
        
        elif tr.Type == "chstream":
            #streamName, streamNumber = tr.affectedStream()
            #stream = accurev.show.streams(depot=depot, stream=streamNumber, timeSpec=tr.id).streams[0]
            
            branchName = self.SanitizeBranchName(tr.stream.name)
            if tr.stream.prevName is not None and len(tr.stream.prevName.strip()) > 0:
                # if the stream has been renamed, use its new name from now on.
                prevBranchName = self.SanitizeBranchName(tr.stream.prevName)
                if self.gitRepo.raw_cmd([ u'git', u'branch', u'-m', prevBranchName, branchName ]) is None:
                    raise Exception("Failed to rename branch {old} to {new}. Err: {err}".format(old=prevBranchName, new=branchName, err=self.gitRepo.lastStderr))
                self.config.logger.info("Renamed branch {oldName} to {newName}".format(oldName=prevBranchName, newName=branchName))
                
            if self.gitRepo.checkout(branchName=branchName) is None:
                raise Exception("Failed to checkout branch {br}".format(br=branchName))

            if tr.stream.prevBasis is not None and len(tr.stream.prevBasis) > 0:
                # We need to change where our stream is parented, i.e. rebase it...
                basisBranchName = self.SanitizeBranchName(tr.stream.basis)
                prevBasisBranchName = self.SanitizeBranchName(tr.stream.prevBasis)

                newBasisCommitHash = self.GetLastCommitHash(branchName=basisBranchName)
                if newBasisCommitHash is None:
                    raise Exception("Failed to retrieve the last commit hash for new basis stream branch {bs}".format(bs=basisBranchName))
                if self.gitRepo.raw_cmd([ u'git', u'reset', u'--hard', newBasisCommitHash ]) is None:
                    raise Exception("Failed to rebase branch {br} from {old} to {new}. Err: {err}".format(br=branchName, old=prevBasisBranchName, new=basisBranchName, err=self.gitRepo.lastStderr))
                self.config.logger.info("Rebased branch {name} from {oldBasis} to {newBasis}".format(name=branchName, oldBasis=prevBasisBranchName, newBasis=basisBranchName))
                
            diff = self.TryDiff(streamName=tr.stream.name, firstTrNumber=(tr.id - 1), secondTrNumber=tr.id)
            deletedPathList = self.DeleteDiffItemsFromRepo(diff=diff)
            popResult = self.TryPop(streamName=tr.stream.name, transaction=tr)

            commitMessage = self.GenerateCommitMessage(transaction=tr, dstStream=tr.stream)
            commitHash = self.Commit(depot=depot, stream=tr.stream, transaction=tr, branchName=branchName, allowEmptyCommit=True, noNotes=True, messageOverride=commitMessage)
            if commitHash is None:
                raise Exception("Failed to add chstream commit")

        elif tr.Type == "add":
            streamName, streamNumber = tr.affectedStream()
            stream = accurev.show.streams(depot=depot, stream=streamNumber, timeSpec=tr.id).streams[0]
            branchName = self.SanitizeBranchName(stream.name)
            if self.gitRepo.checkout(branchName=branchName) is None:
                raise Exception("Failed to checkout branch {br}!".format(br=branchName))
            elif stream.Type != "workspace":
                raise Exception("Invariant error! Assumed that a {Type} transaction can only occur on a workspace. Stream {name}, type {streamType}".format(Type=tr.type, name=stream.name, streamType=stream.Type))
            # The add command only introduces new files so we can safely use only `accurev pop` to get the changes.
            self.TryPop(streamName=stream.name, transaction=tr)
            
            commitMessage = self.GenerateCommitMessage(transaction=tr, dstStream=stream)
            self.Commit(depot=depot, stream=stream, transaction=tr, branchName=branchName, noNotes=True, messageOverride=commitMessage)
            
        elif tr.Type in [ "keep", "co", "move" ]:
            streamName, streamNumber = tr.affectedStream()
            stream = accurev.show.streams(depot=depot, stream=streamNumber, timeSpec=tr.id).streams[0]
            branchName = self.SanitizeBranchName(stream.name)
            if self.gitRepo.checkout(branchName=branchName) is None:
                raise Exception("Failed to checkout branch {br}!".format(br=branchName))
            elif stream.Type != "workspace":
                raise Exception("Invariant error! Assumed that a {Type} transaction can only occur on a workspace. Stream {name}, type {streamType}".format(Type=tr.type, name=stream.name, streamType=stream.Type))
            
            diff = self.TryDiff(streamName=stream.name, firstTrNumber=(tr.id - 1), secondTrNumber=tr.id)
            deletedPathList = self.DeleteDiffItemsFromRepo(diff=diff)
            popResult = self.TryPop(streamName=stream.name, transaction=tr)

            commitMessage = self.GenerateCommitMessage(transaction=tr, dstStream=stream)
            commitHash = self.Commit(depot=depot, stream=stream, transaction=tr, branchName=branchName, allowEmptyCommit=True, noNotes=True, messageOverride=commitMessage)
            if commitHash is None:
                raise Exception("Failed to commit a `{Type}`! tr={tr}".format(tr=tr.id, Type=tr.Type))
            
        elif tr.Type == "promote":
            # Promotes can be thought of as merges or cherry-picks in git and deciding which one we are dealing with
            # is the key to having a good conversion.
            # There are 4 situations that we should consider:
            #   1. A promote from a child stream to a parent stream that promotes everything from that stream.
            #      This trivial case is the easiest to reason about and is obviously a merge.
            #   2. A promote from a child stream to a parent stream that promotes only some of the things from that
            #      stream. (i.e. one of 2 transactions is promoted up, or a subset of files).
            #      This is slightly trickier to reason about since the transactions could have been promoted in order
            #      (from earliest to latest) in which case it is a sequence of merges or in any other case it should be
            #      a cherry-pick.
            #   3. A promote from either an indirect descendant stream to this stream (a.k.a. cross-promote).
            #      This case can be considered as either a merge or a cherry-pick, but we will endevour to make it a merge.
            #   4. A promote from either a non-descendant stream to this stream (a.k.a. cross-promote).
            #      This case is most obviously a cherry-pick.

            # Determine the stream to which the files in this this transaction were promoted.
            dstStreamName, dstStreamNumber = trHist.toStream()
            if dstStreamName is None:
                raise Exception("Error! Could not determine the destination stream for promote {tr}.".format(tr=tr.id))
            dstStream = accurev.show.streams(depot=depot, stream=dstStreamName, timeSpec=tr.id)
            if dstStream is None or dstStream.streams is None or len(dstStream.streams) == 0:
                raise Exception("Error! accurev show streams -p {d} -s {s} -t {t} failed!".format(d=depot, s=dstStreamName, t=tr.id))
            dstStream = dstStream.streams[0]
            dstBranchName = self.SanitizeBranchName(dstStream.name)

            # Determine the stream from which the files in this this transaction were promoted.
            srcStreamName, srcStreamNumber = trHist.fromStream()
            srcStream = None
            if srcStreamName is None:
                # We have failed to determine the stream from which this transaction came. Hence we now must treat this as a cherry-pick instead of a merge...
                self.config.logger.error("Error! Could not determine the source stream for promote {tr}. Treating as a cherry-pick.".format(tr=tr.id))

                if self.gitRepo.checkout(branchName=dstBranchName) is None:
                    raise Exception("Failed to checkout branch {br}!".format(br=dstBranchName))

                diff = self.TryDiff(streamName=dstStream.name, firstTrNumber=(tr.id - 1), secondTrNumber=tr.id)
                deletedPathList = self.DeleteDiffItemsFromRepo(diff=diff)
                popResult = self.TryPop(streamName=dstStream.name, transaction=tr)

                commitMessage = self.GenerateCommitMessage(transaction=tr, dstStream=dstStream)
                commitHash = self.Commit(depot=depot, stream=dstStream, transaction=tr, branchName=dstBranchName, allowEmptyCommit=True, noNotes=True, messageOverride=commitMessage)
                if commitHash is None:
                    raise Exception("Failed to commit a `{Type}`! tr={tr}".format(Type=tr.Type, tr=tr.id))
            else:
                # The source stream is almost always a workspace in which the transaction was generated. This is not ideal, but it is the best we can get.
                srcStream = accurev.show.streams(depot=depot, stream=srcStreamName, timeSpec=tr.id)
                if srcStream is None or srcStream.streams is None or len(srcStream.streams) == 0:
                    raise Exception("Error! accurev show streams -p {d} -s {s} -t {t} failed!".format(d=depot, s=srcStreamName, t=tr.id))
                srcStream = srcStream.streams[0]
                srcBranchName = self.SanitizeBranchName(srcStream.name)

                # Perform the git merge of the 'from stream' into the 'to stream' but only if they have the same contents.
                commitMessage = self.GenerateCommitMessage(transaction=tr, dstStream=dstStream, srcStream=srcStream, title="Merged {src} into {dst} - accurev promote.".format(src=srcBranchName, dst=dstBranchName))
                self.GitCommitOrMerge(depot=depot, dstStream=dstStream, srcStream=srcStream, tr=tr, messageOverride=commitMessage)
            
            affectedStreams = accurev.ext.affected_streams(depot=depot, transaction=tr.id, includeWorkspaces=True, ignoreTimelocks=False, doDiffs=True, useCache=self.config.accurev.UseCommandCache())
            for stream in affectedStreams:
                branchName = self.SanitizeBranchName(stream.name)
                if stream.streamNumber != dstStream.streamNumber and (srcStream is None or stream.streamNumber != srcStream.streamNumber):
                    commitMessage = self.GenerateCommitMessage(transaction=tr, dstStream=stream, srcStream=dstStream, title="Merged {src} into {dst} - accurev parent stream inheritance.".format(src=dstBranchName, dst=branchName), friendlyMessage="Accurev auto-inherited upstream changes.")
                    self.GitCommitOrMerge(depot=depot, dstStream=stream, srcStream=dstStream, tr=tr, messageOverride=commitMessage)

        elif tr.Type in [ "defunct", "purge" ]:
            streamName, streamNumber = tr.affectedStream()
            stream = accurev.show.streams(depot=depot, stream=streamNumber, timeSpec=tr.id).streams[0]
            branchName = self.SanitizeBranchName(stream.name)
            if self.gitRepo.checkout(branchName=branchName) is None:
                raise Exception("Failed to checkout branch {br}!".format(br=branchName))
            
            diff = self.TryDiff(streamName=stream.name, firstTrNumber=(tr.id - 1), secondTrNumber=tr.id)
            deletedPathList = self.DeleteDiffItemsFromRepo(diff=diff)
            popResult = self.TryPop(streamName=stream.name, transaction=tr)

            commitMessage = self.GenerateCommitMessage(transaction=tr, dstStream=stream)
            commitHash = self.Commit(depot=depot, stream=stream, transaction=tr, branchName=branchName, allowEmptyCommit=True, noNotes=True, messageOverride=commitMessage)
            if commitHash is None:
                raise Exception("Failed to commit a `{Type}`! tr={tr}".format(Type=tr.Type, tr=tr.id))
            
            if stream.Type != "workspace":
                self.config.logger.info("Note: {trType} transaction {id} on stream {stream} ({streamType}). Merging down-stream. Usually defuncts occur on workspaces!".format(trType=tr.Type, id=tr.id, stream=stream.name, streamType=stream.Type))
                affectedStreams = accurev.ext.affected_streams(depot=depot, transaction=tr.id, includeWorkspaces=True, ignoreTimelocks=False, doDiffs=True, useCache=self.config.accurev.UseCommandCache())
                for s in affectedStreams:
                    if s.streamNumber != stream.streamNumber:
                        bName = self.SanitizeBranchName(s.name)
                        commitMessage = self.GenerateCommitMessage(transaction=tr, dstStream=s, srcStream=stream, title="Merged {src} into {dst} - accurev parent stream inheritance ({trType}).".format(src=branchName, dst=bName, trType=tr.Type), friendlyMessage="Accurev auto-inherited upstream changes.")
                        self.GitCommitOrMerge(depot=depot, dstStream=s, srcStream=stream, tr=tr, messageOverride=commitMessage)
                
            
        elif tr.Type == "defcomp":
            self.config.logger.info("Ignoring transaction #{id} - {Type}".format(id=tr.id, Type=tr.Type))

        else:
            message = "Not yet implemented! Unrecognized transaction type {type}".format(type=tr.Type)
            self.config.logger.info(message)
            raise Exception(message)

    def ProcessTransactions(self):
        stateRefspec = u'refs/ac2git/state'
        # Determine the last processed transaction, if any.
        stateStr = self.gitRepo.raw_cmd([u'git', u'show', stateRefspec])
        state = { "transaction": 0, "branch_list": None }
        if stateStr is not None and len(stateStr) > 0:
            state = json.loads(stateStr.strip())
            self.config.logger.dbg( "Loaded last state at transaction {tr} as:".format(tr=state["transaction"]) )
            for br in state["branch_list"]:
                self.config.logger.dbg( " - Branch {br} at {hash}{current}.".format(br=br["name"], hash=br["commit"], current=", current" if br["is_current"] else "") )
            
            # Determine the last processed transaction's associated branch, if any (for first transaction there is none).
            if state["transaction"] != 0 and state["branch_list"] is not None and len(state["branch_list"]) != 0:
                # Reset the repo to the last branch's tip (which should be equivalent to our last transaction).
                self.config.logger.dbg( "Clean current branch" )
                self.gitRepo.clean(directories=True, force=True, forceSubmodules=True, includeIgnored=True)
                self.config.logger.dbg( "Reset current branch" )
                self.gitRepo.reset(isHard=True)
                
                # Restore all branches to the last saved state but do the branch that was current at the time last.
                currentBranch = None
                for br in state["branch_list"]:
                    if not br["is_current"]:
                        self.config.logger.dbg( "Restore branch {branchName} at commit {commit}".format(branchName=br["name"], commit=br["commit"]) )
                        result = self.gitRepo.raw_cmd([u'git', u'checkout', u'-B', br["name"], br["commit"]])
                        if result is None:
                            raise Exception("Failed to restore last state. git checkout -B {br} {c}; failed.".format(br=br["name"], c=br["commit"]))
                    else:
                        currentBranch = br
                if currentBranch is None:
                    raise Exception("Invariant error! There must have been at least one current branch saved.")
                self.config.logger.dbg( "Checkout last processed transaction #{tr} on branch {branchName} at commit {commit}".format(tr=state["transaction"], branchName=currentBranch["name"], commit=currentBranch["commit"]) )
                result = self.gitRepo.raw_cmd([u'git', u'checkout', u'-B', currentBranch["name"], currentBranch["commit"]])
                if result is None:
                    raise Exception("Failed to restore last state. git checkout -B {br} {c}; failed.".format(br=currentBranch["name"], c=currentBranch["commit"]))

                # Print out and validate current state as clean.
                status = self.gitRepo.status()
                self.config.logger.dbg( "Status of {branch} - {staged} staged, {changed} changed, {untracked} untracked files{initial_commit}.".format(branch=status.branch, staged=len(status.staged), changed=len(status.changed), untracked=len(status.untracked), initial_commit=', initial commit' if status.initial_commit else '') )
                if status is None:
                    raise Exception("Invalid initial state! The status command return is invalid.")
                if status.branch is None or status.branch != currentBranch["name"]:
                    raise Exception("Invalid initial state! The status command returned an invalid name for current branch. Expected {branchName} but got {statusBranch}.".format(branchName=currentBranch["name"], statusBranch=status.branch))
                if len(status.staged) != 0 or len(status.changed) != 0 or len(status.untracked) != 0:
                    raise Exception("Invalid initial state! There are changes in the tracking repository. Staged {staged}, changed {changed}, untracked {untracked}.".format(staged=status.staged, changed=status.changed, untracked=status.untracked))
            else:
                raise Exception("Invalid state! Information found for previous run but is incomplete, corrupt or incorrect.")
        else:
            self.config.logger.dbg( "No last state!" )

        # Get the configured end transaction and convert it into a number by calling accurev hist.
        endTrHist = self.TryHist(depot=self.config.accurev.depot, trNum=self.config.accurev.endTransaction)
        if endTrHist is None or len(endTrHist.transactions) == 0 is None:
            raise Exception("Couldn't determine the end transaction for the conversion. Aborting!")
        endTr = endTrHist.transactions[0]

        # Begin processing of the transactions
        startTransaction = state["transaction"] + 1
        currentTransaction = startTransaction
        self.config.logger.info( "Processing transaction range #{tr_start}-{tr_end}".format(tr_start=currentTransaction, tr_end=endTr.id) )

        startTime = datetime.now()
        lastPushTime = startTime
        while currentTransaction < endTr.id:
            self.config.logger.dbg( "Started processing transaction #{tr}".format(tr=currentTransaction) )
            self.ProcessTransaction(depot=self.config.accurev.depot, transaction=currentTransaction)
            
            # Validate that there are no pending changes (i.e. everything has been committed)
            status = self.gitRepo.status()
            self.config.logger.dbg( "Status of {branch} - {staged} staged, {changed} changed, {untracked} untracked files{initial_commit}.".format(branch=status.branch, staged=len(status.staged), changed=len(status.changed), untracked=len(status.untracked), initial_commit=', initial commit' if status.initial_commit else '') )
            if status is None:
                raise Exception("Invalid initial state! The status command return is invalid.")
            if status.branch is None:
                raise Exception("Invalid initial state! The status command returned an invalid name for current branch.")
            if len(status.staged) != 0 or len(status.changed) != 0 or len(status.untracked) != 0:
                raise Exception("Invalid initial state! There are changes in the tracking repository. Staged {staged}, changed {changed}, untracked {untracked}.".format(staged=status.staged, changed=status.changed, untracked=status.untracked))

            # Save our current state
            lastCommitHash = self.GetLastCommitHash(branchName=status.branch)
            if lastCommitHash is None or len(lastCommitHash) == 0:
                raise Exception("Failed to retrieve last commit hash!")
            
            state["transaction"] = currentTransaction
            # Record the current position of all the branches.
            state["branch_list"] = []
            for br in self.gitRepo.branch_list():
                if br.isCurrent:
                    if br.name != status.branch.strip() or not lastCommitHash.strip().startswith(br.shortHash):
                        raise Exception("Invariant error! git status and git branch --list reconciliation failed. Status: {statusName} ({statusHash}), Br. list: {listName} ({listHash}).".format(statusName=status.branch.strip(), statusHash=lastCommitHash, listName=br.name, listHash=br.shortHash))
                brHash = OrderedDict()
                brHash["name"] = br.name
                brHash["commit"] = br.shortHash
                brHash["is_current"] = br.isCurrent
                state["branch_list"].append(brHash)

            
            stateFilePath = None
            with tempfile.NamedTemporaryFile(mode='w+', prefix='ac2git_state_', delete=False) as stateFile:
                stateFilePath = stateFile.name
                stateFile.write(json.dumps(state))
            if stateFilePath is not None:
                cmd = [ u'git', u'hash-object', u'-w', u'{0}'.format(stateFilePath) ]
                stateObjHash = ''
                tryCount = 0
                while stateObjHash is not None and len(stateObjHash) == 0 and tryCount < AccuRev2Git.commandFailureRetryCount:
                    stateObjHash = self.gitRepo.raw_cmd(cmd)
                    stateObjHash = stateObjHash.strip()
                    tryCount += 1
                os.remove(stateFilePath)
                updateRefRetr = None
                if stateObjHash is not None:
                    cmd = [ u'git', u'update-ref', stateRefspec, stateObjHash ]
                    updateRefRetr = self.gitRepo.raw_cmd(cmd)
                if stateObjHash is None or updateRefRetr is None:
                    self.config.logger.dbg("Error! Command {cmd}".format(cmd=' '.join(str(x) for x in cmd)))
                    self.config.logger.dbg("  Failed with: {err}".format(err=self.gitRepo.lastStderr))
                    self.config.logger.error("Failed to record current state, aborting!")
                    raise Exception("Error! Failed to record current state, aborting!")
            else:
                self.config.logger.error("Failed to create temporary file for state of transaction {0}".format(tr.id))
                raise Exception("Error! Failed to record current state, aborting!")
                

            self.config.logger.dbg( "Finished processing transaction #{tr}".format(tr=currentTransaction) )
            currentTransaction += 1

            finishTime = datetime.now()
            # Do a push every 5 min or at the end of processing the transactions...
            if currentTransaction == endTr.id or (finishTime - lastPushTime).total_seconds() > 300:
                lastPushTime = finishTime
                if self.config.git.remoteMap is not None:
                    for remoteName in self.config.git.remoteMap:
                        pushOutput = None
                        try:
                            pushCmd = "git push {remote} --all".format(remote=remoteName)
                            pushOutput = subprocess.check_output(pushCmd.split(), stderr=subprocess.STDOUT).decode('utf-8')
                            pushCmd = "git push {remote} +{refspec}:{refspec}".format(remote=remoteName, refspec=stateRefspec)
                            pushOutput = subprocess.check_output(pushCmd.split(), stderr=subprocess.STDOUT).decode('utf-8')
                            self.config.logger.info("Push to '{remote}' succeeded:".format(remote=remoteName))
                            self.config.logger.info(pushOutput)
                        except subprocess.CalledProcessError as e:
                            self.config.logger.error("Push to '{remote}' failed!".format(remote=remoteName))
                            self.config.logger.dbg("'{cmd}', returned {returncode} and failed with:".format(cmd="' '".join(e.cmd), returncode=e.returncode))
                            self.config.logger.dbg("{output}".format(output=e.output.decode('utf-8')))
            
            # Print the progress message
            processedTransactions = currentTransaction - startTransaction
            runningTime = (finishTime - startTime).total_seconds()
            totalTransactions = endTr.id - state["transaction"]
            eta = (runningTime/processedTransactions) * (totalTransactions - processedTransactions)
            etaDays, etaHours, etaMin, etaSec = int(eta / 60 / 60 / 24), int((eta / 60 / 60) % 24), int((eta / 60) % 60), (eta % 60)
            self.config.logger.info("Progress {progress: >5.2f}%, {processed}/{total}, avg. {throughput:.2f} tr/s ({timeTaken:.2f} s/tr). ETA {etaDays}d {etaHours}:{etaMin:0>2d}:{etaSec:0>5.2f} (h:mm:ss.ss).".format(progress=(processedTransactions/totalTransactions), processed=processedTransactions, total=totalTransactions, throughput=(processedTransactions/runningTime), timeTaken=(runningTime/processedTransactions), etaDays=etaDays, etaHours=etaHours, etaMin=etaMin, etaSec=etaSec))

            
    def InitGitRepo(self, gitRepoPath):
        gitRootDir, gitRepoDir = os.path.split(gitRepoPath)
        if os.path.isdir(gitRootDir):
            if git.isRepo(gitRepoPath):
                # Found an existing repo, just use that.
                self.config.logger.info( "Using existing git repository." )
                return True
        
            self.config.logger.info( "Creating new git repository" )
            
            # Create an empty first commit so that we can create branches as we please.
            if git.init(path=gitRepoPath) is not None:
                self.config.logger.info( "Created a new git repository." )
            else:
                self.config.logger.error( "Failed to create a new git repository." )
                sys.exit(1)
                
            return True
        else:
            self.config.logger.error("{0} not found.\n".format(gitRootDir))
            
        return False

    # Returns a string representing the name of the stream on which a transaction was performed.
    # If the history (an accurev.obj.History object) is given then it is attempted to retrieve it from the stream list first and
    # should this fail then the history object's transaction's virtual version specs are used.
    # If the transaction (an accurev.obj.Transaction object) is given it is attempted to retrieve the name of the stream from the
    # virtual version spec.
    # The `depot` argument is used both for the accurev.show.streams() command and to control its use. If it is None then the
    # command isn't used at all which could mean a quicker conversion. When specified it indicates that the name of the stream
    # from the time of the transaction should be retrieved. Otherwise the current name of the stream is returned (assumint it was
    # renamed at some point).
    def GetDestinationStreamName(self, history=None, transaction=None, depot=None):
        # depot given as None indicates that accurev.show.streams() command is not to be run.
        if history is not None:
            if depot is None and len(history.streams) == 1:
                return history.streams[0].name
            elif len(history.transactions) > 0:
                rv = self.GetDestinationStreamName(history=None, transaction=history.transactions[0], depot=depot)
                if rv is not None:
                    return rv

        if transaction is not None:
            streamName, streamNumber = transaction.affectedStream()
            if streamNumber is not None and depot is not None:
                try:
                    stream = accurev.show.streams(depot=depot, stream=streamNumber, timeSpec=transaction.id).streams[0] # could be expensive
                    if stream is not None and stream.name is not None:
                        return stream.name
                except:
                    pass
            return streamName
        return None

    def GetStreamNameFromBranch(self, branchName):
        if branchName is not None:
            for stream in self.config.accurev.streamMap:
                if branchName == self.config.accurev.streamMap[stream]:
                    return stream
        return None

    # Arranges the stream1 and stream2 into a tuple of (parent, child) according to accurev information
    def GetParentChild(self, stream1, stream2, timeSpec=u'now', onlyDirectChild=False):
        parent = None
        child = None
        if stream1 is not None and stream2 is not None:
            #print ("self.GetParentChild(stream1={0}, stream2={1}, timeSpec={2}".format(str(stream1), str(stream2), str(timeSpec)))
            stream1Children = accurev.show.streams(depot=self.config.accurev.depot, stream=stream1, timeSpec=timeSpec, listChildren=True)
            stream2Children = accurev.show.streams(depot=self.config.accurev.depot, stream=stream2, timeSpec=timeSpec, listChildren=True)

            found = False
            for stream in stream1Children.streams:
                if stream.name == stream2:
                    if not onlyDirectChild or stream.basis == stream1:
                        parent = stream1
                        child = stream2
                    found = True
                    break
            if not found:
                for stream in stream2Children.streams:
                    if stream.name == stream1:
                        if not onlyDirectChild or stream.basis == stream2:
                            parent = stream2
                            child = stream1
                        break
        return (parent, child)

    def GetStreamName(self, state=None, commitHash=None):
        if state is None:
            return None
        stream = state.get('stream')
        if stream is None:
            self.config.logger.error("Could not get stream name from state {0}. Trying to reverse map from the containing branch name.".format(state))
            if commitHash is not None:
                branches = self.gitRepo.branch_list(containsCommit=commitHash) # This should only ever return one branch since we are processing things in order...
                if branches is not None and len(branches) == 1:
                    branch = branches[0]
                    stream = self.GetStreamNameFromBranch(branchName=branch.name)
                    if stream is None:
                        self.config.logger.error("Could not get stream name for branch {0}.".format(branch.name))
        
        return stream

    def StitchBranches(self):
        self.config.logger.dbg("Getting branch revision map from git_stitch.py")
        branchRevMap = git_stitch.GetBranchRevisionMap(self.config.git.repoPath)
        
        self.config.logger.info("Stitching git branches")
        commitRewriteMap = OrderedDict()
        if branchRevMap is not None:
            commitStateMap = {}
            # Build a dictionary that will act as our "squashMap". Both the key and value are a commit hash.
            # The commit referenced by the key will be replaced by the commit referenced by the value in this map.
            aliasMap = {}
            for tree_hash in branchRevMap:
                for commit in branchRevMap[tree_hash]:
                    if not commit or re.match("^[0-9A-Fa-f]+$", commit[u'hash']) is None:
                        raise Exception("Commit {commit} is not a valid hash!".format(commit=commit))
                    aliasMap[commit[u'hash']] = commit[u'hash'] # Initially each commit maps to itself.

            for tree_hash in branchRevMap:
                if len(branchRevMap[tree_hash]) > 1:
                    # We should make some decisions about how to merge these commits which reference the same tree
                    # and what their ideal parents are. Once we decide we will write it to file in a nice bash friendly
                    # format and use the git filter-branch --parent-filter ... to fix it all up!
                    inOrder = sorted(branchRevMap[tree_hash], key=lambda x: int(x[u'committer'][u'time']))
                    #print(u'tree: {0}'.format(tree_hash))
                    
                    for i in range(0, len(inOrder) - 1):
                        first = inOrder[i]
                        second = inOrder[i + 1]
                        
                        firstTime = int(first[u'committer'][u'time'])
                        secondTime = int(second[u'committer'][u'time'])
    
                        wereSwapped = False
                        if firstTime == secondTime:
                            # Normally both commits would have originated from the same transaction. However, if not, let's try and order them by transaciton number first.
                            firstState = self.GetStateForCommit(commitHash=first[u'hash'], branchName=first[u'branch'].name)
                            secondState = self.GetStateForCommit(commitHash=second[u'hash'], branchName=second[u'branch'].name)

                            commitStateMap[first[u'hash']] = firstState
                            commitStateMap[second[u'hash']] = secondState

                            firstTrId = firstState["transaction_number"]
                            secondTrId = secondState["transaction_number"]

                            if firstTrId < secondTrId:
                                # This should really never be true given that AccuRev is centralized and synchronous and that firstTime == secondTime above...
                                pass # Already in the correct order
                            elif firstTrId > secondTrId:
                                # This should really never be true given that AccuRev is centralized and synchronous and that firstTime == secondTime above...
                                # Swap them
                                wereSwapped = True
                                first, second = second, first
                                firstState, secondState = secondState, firstState
                            else:
                                # The same transaction affected both commits (the id's are unique in accurev)...
                                # Must mean that they are substreams of eachother or sibling substreams of a third stream. Let's see which it is.

                                # Get the information for the first stream
                                firstStream = self.GetStreamName(state=firstState, commitHash=first[u'hash'])
                                if firstStream is None:
                                    self.config.logger.error("Branch stitching error: incorrect state. Could not get stream name for branch {0}.".format(firstBranch))
                                    raise Exception("Branch stitching failed!")

                                # Get the information for the second stream
                                secondStream = self.GetStreamName(state=secondState, commitHash=second[u'hash'])
                                if secondStream is None:
                                    self.config.logger.error("Branch stitching error: incorrect state. Could not get stream name for branch {0}.".format(secondBranch))
                                    raise Exception("Branch stitching failed!")

                                # Find which one is the parent of the other. They must be inline since they were affected by the same transaction (since the times match)
                                parentStream, childStream = self.GetParentChild(stream1=firstStream, stream2=secondStream, timeSpec=firstTrId, onlyDirectChild=False)
                                if parentStream is not None and childStream is not None:
                                    if firstStream == childStream:
                                        aliasMap[first[u'hash']] = second[u'hash']
                                        self.config.logger.info(u'  squashing: {0} ({1}/{2}) as equiv. to {3} ({4}/{5}). tree {6}.'.format(first[u'hash'][:8], firstStream, firstTrId, second[u'hash'][:8], secondStream, secondTrId, tree_hash[:8]))
                                    elif secondStream == childStream:
                                        aliasMap[second[u'hash']] = first[u'hash']
                                        self.config.logger.info(u'  squashing: {3} ({4}/{5}) as equiv. to {0} ({1}/{2}). tree {6}.'.format(first[u'hash'][:8], firstStream, firstTrId, second[u'hash'][:8], secondStream, secondTrId, tree_hash[:8]))
                                    else:
                                        Exception("Invariant violation! Either (None, None), (firstStream, secondStream) or (secondStream, firstStream) should be possible")
                                else:
                                    self.config.logger.info(u'  unrelated: {0} ({1}/{2}) is equiv. to {3} ({4}/{5}). tree {6}.'.format(first[u'hash'][:8], firstStream, firstTrId, second[u'hash'][:8], secondStream, secondTrId, tree_hash[:8]))
                                    
                        elif firstTime < secondTime:
                            # Already in the correct order...
                            pass
                        else:
                            raise Exception(u'Error: wrong sort order!')

                        if first is not None and second is not None:
                            if second[u'hash'] not in commitRewriteMap:
                                # Mark the commit for rewriting.
                                commitRewriteMap[second[u'hash']] = OrderedDict() # We need a set (meaning no duplicates) but we also need them to be in order so lets use an OrderedDict().
                                # Add the existing parrents
                                if u'parents' in second:
                                    for parent in second[u'parents']:
                                        commitRewriteMap[second[u'hash']][parent] = True
                            # Add the new parent
                            commitRewriteMap[second[u'hash']][first[u'hash']] = True
                            self.config.logger.info(u'  merge:     {0} as parent of {1}. tree {2}. parents {3}'.format(first[u'hash'][:8], second[u'hash'][:8], tree_hash[:8], [x[:8] for x in commitRewriteMap[second[u'hash']].keys()] ))

            # Reduce the aliasMap to only the items that are actually aliased and remove indirect links to the non-aliased commit (aliases of aliases).
            reducedAliasMap = {}
            for alias in aliasMap:
                if alias != aliasMap[alias]:
                    finalAlias = aliasMap[alias]
                    while finalAlias != aliasMap[finalAlias]:
                        if finalAlias not in aliasMap:
                            raise Exception("Invariant error! The aliasMap contains a value '{0}' but no key for it!".format(finalAlias))
                        if finalAlias == alias:
                            raise Exception("Invariant error! Circular reference in aliasMap for key '{0}'!".format(alias))
                        finalAlias = aliasMap[finalAlias]
                    reducedAliasMap[alias] = finalAlias

            # Write the reduced alias map to file.
            aliasFilePath = os.path.join(self.cwd, 'commit_alias_list.txt')
            self.config.logger.info("Writing the commit alias mapping to '{0}'.".format(aliasFilePath))
            with codecs.open(aliasFilePath, 'w', 'ascii') as f:
                for alias in reducedAliasMap:
                    original = reducedAliasMap[alias]

                    aliasState = None
                    if alias in commitStateMap:
                        aliasState = commitStateMap[alias]
                    originalState = None
                    if original in commitStateMap:
                        originalState = commitStateMap[original]
                    f.write('original: {original} -> alias: {alias}, original state: {original_state} -> alias state: {alias_state}\n'.format(original=original, original_state=originalState, alias=alias, alias_state=aliasState))

            self.config.logger.info("Remapping aliased commits.")
            # Remap the commitRewriteMap keys w.r.t. the aliases in the aliasMap
            discardedRewriteCommits = []
            for commitHash in commitRewriteMap:
                # Find the non-aliased commit
                if commitHash in reducedAliasMap:
                    if commitHash == reducedAliasMap[commitHash]:
                        raise Exception("Invariant error! The reducedAliasMap must not contain non-aliased commits!")

                    # Aliased commit.
                    discardedRewriteCommits.append(commitHash) # mark for deletion from map.
                    
                    h = reducedAliasMap[commitHash]
                    if h not in commitRewriteMap:
                        commitRewriteMap[h] = commitRewriteMap[commitHash]
                    else:
                        for parent in commitRewriteMap[commitHash]:
                            commitRewriteMap[h][parent] = True
                else:
                    Exception("Invariant falacy! aliasMap should contain every commit that we have processed.")

            # Delete aliased keys
            for commitHash in discardedRewriteCommits:
                del commitRewriteMap[commitHash]
            
            
            self.config.logger.info("Remapping aliased parent commits.")
            # Remap the commitRewriteMap values (parents) w.r.t. the aliases in the aliasMap
            for commitHash in commitRewriteMap:
                discardedParentCommits = []
                for parent in commitRewriteMap[commitHash]:
                    if parent in reducedAliasMap:
                        if parent == reducedAliasMap[parent]:
                            raise Exception("Invariant error! The reducedAliasMap must not contain non-aliased commits!")
                            
                        # Aliased parent commit.
                        discardedParentCommits.append(parent)

                        # Remap the parent
                        p = reducedAliasMap[parent]
                        commitRewriteMap[commitHash][p] = True # Add the non-aliased parent
                    else:
                        Exception("Invariant falacy! aliasMap should contain every commit that we have processed.")

                # Delete the aliased parents
                for parent in discardedParentCommits:
                    del commitRewriteMap[commitHash][parent]

            # Write parent filter shell script
            parentFilterPath = os.path.join(self.cwd, 'parent_filter.sh')
            self.config.logger.info("Writing parent filter '{0}'.".format(parentFilterPath))
            with codecs.open(parentFilterPath, 'w', 'ascii') as f:
                # http://www.tutorialspoint.com/unix/case-esac-statement.htm
                f.write('#!/bin/sh\n\n')
                f.write('case "$GIT_COMMIT" in\n')
                for commitHash in commitRewriteMap:
                    parentString = ''
                    for parent in commitRewriteMap[commitHash]:
                        parentString += '"{parent}" '.format(parent=parent)
                    f.write('    "{commit_hash}") echo "res="echo"; for x in {parent_str}; do res=\\"\\$res -p \\$(map "\\$x")\\"; done; \\$res"\n'.format(commit_hash=commitHash, parent_str=parentString))
                    f.write('    ;;\n')
                f.write('    *) echo "cat < /dev/stdin"\n') # If we don't have the commit mapping then just print out whatever we are given on stdin...
                f.write('    ;;\n')
                f.write('esac\n\n')

            # Write the commit filter shell script
            commitFilterPath = os.path.join(self.cwd, 'commit_filter.sh')
            self.config.logger.info("Writing commit filter '{0}'.".format(commitFilterPath))
            with codecs.open(commitFilterPath, 'w', 'ascii') as f:
                # http://www.tutorialspoint.com/unix/case-esac-statement.htm
                f.write('#!/bin/sh\n\n')
                f.write('case "$GIT_COMMIT" in\n')
                for commitHash in aliasMap:
                    if commitHash != aliasMap[commitHash]:
                        # Skip this commit
                        f.write('    "{0}") echo skip_commit \\$@\n'.format(commitHash))
                        f.write('    ;;\n')
                f.write('    *) echo git_commit_non_empty_tree \\$@;\n') # If we don't want to skip this commit then just commit it...
                f.write('    ;;\n')
                f.write('esac\n\n')

            stitchScriptPath = os.path.join(self.cwd, 'stitch_branches.sh')
            self.config.logger.info("Writing branch stitching script '{0}'.".format(stitchScriptPath))
            with codecs.open(stitchScriptPath, 'w', 'ascii') as f:
                # http://www.tutorialspoint.com/unix/case-esac-statement.htm
                f.write('#!/bin/sh\n\n')
                f.write('chmod +x {0}\n'.format(parentFilterPath))
                f.write('chmod +x {0}\n'.format(commitFilterPath))
                f.write('cd {0}\n'.format(self.config.git.repoPath))

                rewriteHeads = ""
                branchList = self.gitRepo.branch_list()
                for branch in branchList:
                    rewriteHeads += " {0}".format(branch.name)
                f.write("git filter-branch --parent-filter 'eval $({parent_filter})' --commit-filter 'eval $({commit_filter})' -- {rewrite_heads}\n".format(parent_filter=parentFilterPath, commit_filter=commitFilterPath, rewrite_heads=rewriteHeads))
                f.write('cd -\n')

            self.config.logger.info("Branch stitching script generated: {0}".format(stitchScriptPath))
            self.config.logger.info("To apply execute the following command:")
            self.config.logger.info("  chmod +x {0}".format(stitchScriptPath))

    # Start
    #   Begins a new AccuRev to Git conversion process discarding the old repository (if any).
    def Start(self, isRestart=False):
        global maxTransactions

        if not os.path.exists(self.config.git.repoPath):
            self.config.logger.error( "git repository directory '{0}' doesn't exist.".format(self.config.git.repoPath) )
            self.config.logger.error( "Please create the directory and re-run the script.".format(self.config.git.repoPath) )
            return 1
        
        if isRestart:
            self.config.logger.info( "Restarting the conversion operation." )
            self.config.logger.info( "Deleting old git repository." )
            git.delete(self.config.git.repoPath)
            
        # From here on we will operate from the git repository.
        if self.config.accurev.commandCacheFilename is not None:
            self.config.accurev.commandCacheFilename = os.path.abspath(self.config.accurev.commandCacheFilename)
        self.cwd = os.getcwd()
        os.chdir(self.config.git.repoPath)
        
        # This try/catch/finally block is here to ensure that we change directory back to self.cwd in order
        # to allow other scripts to safely call into this method.
        if self.InitGitRepo(self.config.git.repoPath):
            self.gitRepo = git.open(self.config.git.repoPath)
            self.gitBranchList = self.gitRepo.branch_list()
            if self.gitBranchList is None:
                raise Exception("Failed to get branch list!")
            elif len(self.gitBranchList) == 0:
                status = self.gitRepo.status()
                if status is None or not status.initial_commit:
                    raise Exception("Invalid state! git branch returned {branchList} (an empty list of branches) and we are not on an initial commit? Aborting!".format(branchList=self.gitBranchList))
                else:
                    self.config.logger.dbg( "New git repository. Initial commit on branch {br}".format(br=status.branch) )
 
            # Configure the remotes
            if self.config.git.remoteMap is not None and len(self.config.git.remoteMap) > 0:
                remoteList = self.gitRepo.remote_list()
                remoteAddList = [x for x in self.config.git.remoteMap.keys()]
                for remote in remoteList:
                    if remote.name in self.config.git.remoteMap:
                        r = self.config.git.remoteMap[remote.name]
                        pushUrl1 = r.url if r.pushUrl is None else r.pushUrl
                        pushUrl2 = remote.url if remote.pushUrl is None else remote.pushUrl
                        if r.url != remote.url or pushUrl1 != pushUrl2:
                            raise Exception("Configured remote {r}'s urls don't match.\nExpected:\n{r1}\nGot:\n{r2}".format(r=remote.name, r1=r, r2=remote))
                        remoteAddList.remove(remote.name)
                    else:
                        self.config.logger.dbg( "Unspecified remote {remote} ({url}) found. Ignoring...".format(remote=remote.name, url=remote.url) )
                for remote in remoteAddList:
                    r = self.config.git.remoteMap[remote]
                    if self.gitRepo.remote_add(name=r.name, url=r.url) is None:
                        raise Exception("Failed to add remote {remote} ({url})!".format(remote=r.name, url=r.url))
                    self.config.logger.info( "Added remote: {remote} ({url}).".format(remote=r.name, url=r.url) )
                    if r.pushUrl is not None and r.url != r.pushUrl:
                        if self.gitRepo.remote_set_url(name=r.name, url=r.pushUrl, isPushUrl=True) is None:
                            raise Exception("Failed to set push url {url} for {remote}!".format(url=r.pushUrl, remote=r.name))
                        self.config.logger.info( "Added push url: {remote} ({url}).".format(remote=r.name, url=r.pushUrl) )

            if not isRestart:
                #self.gitRepo.reset(isHard=True)
                self.gitRepo.clean(force=True)
            
            acInfo = accurev.info()
            isLoggedIn = False
            if self.config.accurev.username is None:
                # When a username isn't specified we will use any logged in user for the conversion.
                isLoggedIn = accurev.ext.is_loggedin(infoObj=acInfo)
            else:
                # When a username is specified that specific user must be logged in.
                isLoggedIn = (acInfo.principal == self.config.accurev.username)
            
            doLogout = False
            if not isLoggedIn:
                # Login the requested user
                if accurev.ext.is_loggedin(infoObj=acInfo):
                    # Different username, logout the other user first.
                    logoutSuccess = accurev.logout()
                    self.config.logger.info("Accurev logout for '{0}' {1}".format(acInfo.principal, 'succeeded' if logoutSuccess else 'failed'))
    
                loginResult = accurev.login(self.config.accurev.username, self.config.accurev.password)
                if loginResult:
                    self.config.logger.info("Accurev login for '{0}' succeeded.".format(self.config.accurev.username))
                else:
                    self.config.logger.error("AccuRev login for '{0}' failed.\n".format(self.config.accurev.username))
                    self.config.logger.error("AccuRev message:\n{0}".format(loginResult.errorMessage))
                    return 1
                
                doLogout = True
            else:
                self.config.logger.info("Accurev user '{0}', already logged in.".format(acInfo.principal))
            
            # If this script is being run on a replica then ensure that it is up-to-date before processing the streams.
            accurev.replica.sync()

            if self.config.git.finalize is not None and self.config.git.finalize:
                self.StitchBranches()
            else:
                self.gitRepo.raw_cmd([u'git', u'config', u'--local', u'gc.auto', u'0'])
                if self.config.method == 'transactions':
                    self.ProcessTransactions()
                else:
                    self.ProcessStreams()
                self.gitRepo.raw_cmd([u'git', u'config', u'--local', u'--unset-all', u'gc.auto'])
              
            if doLogout:
                if accurev.logout():
                    self.config.logger.info( "Accurev logout successful." )
                else:
                    self.config.logger.error("Accurev logout failed.\n")
                    return 1
        else:
            self.config.logger.error( "Could not create git repository." )

        # Restore the working directory.
        os.chdir(self.cwd)
        
        return 0
            
# ################################################################################################ #
# Script Functions                                                                                 #
# ################################################################################################ #
def DumpExampleConfigFile(outputFilename):
    with codecs.open(outputFilename, 'w') as file:
        file.write("""<accurev2git>
    <!-- AccuRev details:
            username:             The username that will be used to log into AccuRev and retrieve and populate the history. This is optional and if it isn't provided you will need to login before running this script.
            password:             The password for the given username. Note that you can pass this in as an argument which is safer and preferred! This too is optional. You can login before running this script and it will work.
            depot:                The depot in which the stream/s we are converting are located
            start-transaction:    The conversion will start at this transaction. If interrupted the next time it starts it will continue from where it stopped.
            end-transaction:      Stop at this transaction. This can be the keword "now" if you want it to convert the repo up to the latest transaction.
            command-cache-filename: The filename which will be given to the accurev.py script to use as a local command result cache for the accurev hist, accurev diff and accurev show streams commands.
    -->
    <accurev 
        username="joe_bloggs" 
        password="joanna" 
        depot="Trunk" 
        start-transaction="1" 
        end-transaction="now" 
        command-cache-filename="command_cache.sqlite3" >
        <!-- The stream-list is optional. If not given all streams are processed -->
        <!-- The branch-name attribute is also optional for each stream element. If provided it specifies the git branch name to which the stream will be mapped. -->
        <stream-list>
            <stream branch-name="some_branch">some_stream</stream>
            <stream>some_other_stream</stream>
        </stream-list>
    </accurev>
    <git repo-path="/put/the/git/repo/here" finalize="false" >  <!-- The system path where you want the git repo to be populated. Note: this folder should already exist. 
                                                                     The finalize attribute switches this script from converting accurev transactions to independent orphaned
                                                                     git branches to the "branch stitching" mode which should be activated only once the conversion is completed.
                                                                     Make sure to have a backup of your repo just in case. Once finalize is set to true this script will rewrite
                                                                     the git history in an attempt to recreate merge points.
                                                                -->
        <remote name="origin" url="https://github.com/orao/ac2git.git" push-url="https://github.com/orao/ac2git.git" /> <!-- Optional: Specifies the remote to which the converted
                                                                                                                             branches will be pushed. The push-url attribute is optional. -->
        <remote name="backup" url="https://github.com/orao/ac2git.git" />
    </git>
    <method>deep-hist</method> <!-- The method specifies what approach is taken to perform the conversion. Allowed values are 'deep-hist', 'diff' and 'pop'.
                                     - deep-hist: Works by using the accurev.ext.deep_hist() function to return a list of transactions that could have affected the stream.
                                                  It then performs a diff between the transactions and only populates the files that have changed like the 'diff' method.
                                                  It is the quickest method but is only as reliable as the information that accurev.ext.deep_hist() provides.
                                     - diff: This method's first commit performs a full `accurev pop` command on either the streams `mkstream` transaction or the start
                                             transaction (whichever is highest). Subsequently it increments the transaction number by one and performs an
                                             `accurev diff -a -i -v <stream> -V <stream>` to find all changed files. If not files have changed it takes the next transaction
                                             and performs the diff again. Otherwise, any files returned by the diff are deleted and an `accurev pop -R` performed which only
                                             downloads the changed files. This is slower than the 'deep-hist' method but faster than the 'pop' method by a large margin.
                                             It's reliability is directly dependent on the reliability of the `accurev diff` command.
                                     - pop: This is the naive method which doesn't care about changes and always performs a full deletion of the whole tree and a complete
                                            `accurev pop` command. It is a lot slower than the other methods for streams with a lot of files but should work even with older
                                            accurev releases. This is the method originally implemented by Ryan LaNeve in his https://github.com/rlaneve/accurev2git repo.
                                     - transactions: Works transaction by transaction and is intended to generate an accurate representation of the complete accurev history
                                                     in git.
                               -->
    <logfile>accurev2git.log</logfile>
    <!-- The user maps are used to convert users from AccuRev into git. Please spend the time to fill them in properly. -->
    <usermaps>
         <!-- The timezone attribute is optional. All times are retrieved in UTC from AccuRev and will converted to the local timezone by default.
             If you want to override this behavior then set the timezone to either an Olson timezone string (e.g. Europe/Belgrade) or a git style
             timezone string (e.g. +0100, sign and 4 digits required). -->
        <map-user><accurev username="joe_bloggs" /><git name="Joe Bloggs" email="joe@bloggs.com" timezone="Europe/Belgrade" /></map-user>
        <map-user><accurev username="joanna_bloggs" /><git name="Joanna Bloggs" email="joanna@bloggs.com" timezone="+0500" /></map-user>
        <map-user><accurev username="joey_bloggs" /><git name="Joey Bloggs" email="joey@bloggs.com" /></map-user>
    </usermaps>
</accurev2git>
        """)
        return 0
    return 1

def AutoConfigFile(filename, args, preserveConfig=False):
    if os.path.exists(filename):
        # Backup the file
        backupNumber = 1
        backupFilename = "{0}.{1}".format(filename, backupNumber)
        while os.path.exists(backupFilename):
            backupNumber += 1
            backupFilename = "{0}.{1}".format(filename, backupNumber)

        shutil.copy2(filename, backupFilename)

    config = Config.fromfile(filename=args.configFilename)
    
    if config is None:
        config = Config(accurev=Config.AccuRev(), git=Config.Git(), usermaps=[], logFilename=None)
    elif not preserveConfig:
        # preserve only the accurev username and passowrd
        arUsername = config.accurev.username
        arPassword = config.accurev.password
        
        # reset config
        config = Config(accurev=Config.AccuRev(), git=Config.Git(repoPath=None), usermaps=[], logFilename=None)

        config.accurev.username = arUsername
        config.accurev.password = arPassword


    SetConfigFromArgs(config, args)
    if config.accurev.username is None:
        if config.accurev.username is None:
            config.logger.error("No accurev username provided for auto-configuration.")
        return 1
    else:
        info = accurev.info()
        if info.principal != config.accurev.username:
            if config.accurev.password is None:
                config.logger.error("No accurev password provided for auto-configuration. You can either provide one on the command line, in the config file or just login to accurev before running the script.")
                return 1
            if not accurev.login(config.accurev.username, config.accurev.password):
                config.logger.error("accurev login for '{0}' failed.".format(config.accurev.username))
                return 1
        elif config.accurev.password is None:
            config.accurev.password = ''

    if config.accurev.depot is None:
        depots = accurev.show.depots()
        if depots is not None and depots.depots is not None and len(depots.depots) > 0:
            config.accurev.depot = depots.depots[0].name
            config.logger.info("No depot specified. Selecting first depot available: {0}.".format(config.accurev.depot))
        else:
            config.logger.error("Failed to find an accurev depot. You can specify one on the command line to resolve the error.")
            return 1

    if config.git.repoPath is None:
        config.git.repoPath = './{0}'.format(config.accurev.depot)

    if config.logFilename is None:
        config.logFilename = 'ac2git.log'

    with codecs.open(filename, 'w') as file:
        file.write("""<accurev2git>
    <!-- AccuRev details:
            username:             The username that will be used to log into AccuRev and retrieve and populate the history
            password:             The password for the given username. Note that you can pass this in as an argument which is safer and preferred!
            depot:                The depot in which the stream/s we are converting are located
            start-transaction:    The conversion will start at this transaction. If interrupted the next time it starts it will continue from where it stopped.
            end-transaction:      Stop at this transaction. This can be the keword "now" if you want it to convert the repo up to the latest transaction.
            command-cache-filename: The filename which will be given to the accurev.py script to use as a local command result cache for the accurev hist, accurev diff and accurev show streams commands.
    -->
    <accurev 
        username="{accurev_username}" 
        password="{accurev_password}" 
        depot="{accurev_depot}" 
        start-transaction="{start_transaction}" 
        end-transaction="{end_transaction}" 
        command-cache-filename="command_cache.sqlite3" >
        <!-- The stream-list is optional. If not given all streams are processed -->
        <!-- The branch-name attribute is also optional for each stream element. If provided it specifies the git branch name to which the stream will be mapped. -->
        <stream-list>""".format(accurev_username=config.accurev.username, accurev_password=config.accurev.password, accurev_depot=config.accurev.depot, start_transaction=1, end_transaction="now"))

        if preserveConfig:
            for stream in config.accurev.streamMap:
                file.write("""
            <stream branch-name="{branch_name}">{stream_name}</stream>""".format(stream_name=stream, branch_name=config.accurev.streamMap[stream]))

        streams = accurev.show.streams(depot=config.accurev.depot)
        if streams is not None and streams.streams is not None:
            for stream in streams.streams:
                if not (preserveConfig and stream in config.accurev.streamMap):
                    file.write("""
            <stream branch-name="accurev/{stream_name}">{stream_name}</stream>""".format(stream_name=stream.name))
                    # TODO: Add depot and start/end transaction overrides for each stream...

        file.write("""
        </stream-list>
    </accurev>
    <git repo-path="{git_repo_path}" finalize="false" >  <!-- The system path where you want the git repo to be populated. Note: this folder should already exist.
                                                              The finalize attribute switches this script from converting accurev transactions to independent orphaned
                                                              git branches to the "branch stitching" mode which should be activated only once the conversion is completed.
                                                              Make sure to have a backup of your repo just in case. Once finalize is set to true this script will rewrite
                                                              the git history in an attempt to recreate merge points.
                                                         -->""".format(git_repo_path=config.git.repoPath))
        if config.git.remoteMap is not None:
            for remoteName in remoteMap:
                remote = remoteMap[remoteName]
                file.write("""        <remote name="{name}" url="{url}"{push_url_string} />""".format(name=remote.name, url=name.url, push_url_string='' if name.pushUrl is None else ' push-url="{url}"'.format(url=name.pushUrl)))
        
        file.write("""    </git>
    <method>{method}</method>
    <logfile>{log_filename}<logfile>
    <!-- The user maps are used to convert users from AccuRev into git. Please spend the time to fill them in properly. -->""".format(method=config.method, log_filename=config.logFilename))
        file.write("""
    <usermaps>
         <!-- The timezone attribute is optional. All times are retrieved in UTC from AccuRev and will converted to the local timezone by default.
             If you want to override this behavior then set the timezone to either an Olson timezone string (e.g. Europe/Belgrade) or a git style
             timezone string (e.g. +0100, sign and 4 digits required). -->
        <!-- e.g.
        <map-user><accurev username="joe_bloggs" /><git name="Joe Bloggs" email="joe@bloggs.com" timezone="Europe/Belgrade" /></map-user>
        <map-user><accurev username="joanna_bloggs" /><git name="Joanna Bloggs" email="joanna@bloggs.com" timezone="+0500" /></map-user>
        <map-user><accurev username="joey_bloggs" /><git name="Joey Bloggs" email="joey@bloggs.com" /></map-user>
        -->""")

        if preserveConfig:
            for usermap in config.usermaps:
                file.write("""
        <map-user><accurev username="{accurev_username}" /><git name="{git_name}" email="{git_email}"{timezone_tag} /></map-user>""".format(accurev_username=usermap.accurevUsername, git_name=usermap.gitName, git_email=usermap.gitEmail, timezone_tag="" if usermap.timezone is None else ' timezone="{0}"'.format(usermap.timezone)))


        users = accurev.show.users()
        if users is not None and users.users is not None:
            for user in users.users:
                if not (preserveConfig and user.name in [x.accurevUsername for x in config.usermaps]):
                    file.write("""
        <map-user><accurev username="{accurev_username}" /><git name="{accurev_username}" email="" /></map-user>""".format(accurev_username=user.name))

        file.write("""
    </usermaps>
</accurev2git>
        """)
        return 0
    return 1

def TryGetAccurevUserlist(username, password):
    info = accurev.info()
    
    isLoggedIn = False
    if username is not None and info.principal != username:
        if password is not None:
            isLoggedIn = accurev.login(username, password)
    else:
        isLoggedIn = accurev.ext.is_loggedin()

    userList = None
    if isLoggedIn:
        users = accurev.show.users()
        if users is not None:
            userList = []
            for user in users.users:
                userList.append(user.name)
    
    return userList

def GetMissingUsers(config):
    # Try and validate accurev usernames
    userList = TryGetAccurevUserlist(config.accurev.username, config.accurev.password)
    missingList = None

    if config is not None and config.usermaps is not None:
        missingList = []
        if userList is not None and len(userList) > 0:
            for user in userList:
                found = False
                for usermap in config.usermaps:
                    if user == usermap.accurevUsername:
                        found = True
                        break
                if not found:
                    missingList.append(user)

    return missingList

def PrintMissingUsers(config):
    missingUsers = GetMissingUsers(config)
    if missingUsers is not None:
        if len(missingUsers) > 0:
            missingUsers.sort()
            config.logger.info("Unmapped accurev users:")
            for user in missingUsers:
                config.logger.info("    {0}".format(user))

def SetConfigFromArgs(config, args):
    if args.accurevUsername is not None:
        config.accurev.username = args.accurevUsername
    if args.accurevPassword is not None:
        config.accurev.password = args.accurevPassword
    if args.accurevDepot is not None:
        config.accurev.depot    = args.accurevDepot
    if args.gitRepoPath is not None:
        config.git.repoPath     = args.gitRepoPath
    if args.finalize is not None:
        config.git.finalize     = args.finalize
    if args.conversionMethod is not None:
        config.method = args.conversionMethod
    if args.logFile is not None:
        config.logFilename      = args.logFile

def ValidateConfig(config):
    # Validate the program args and configuration up to this point.
    isValid = True
    if config.accurev.depot is None:
        config.logger.error("No AccuRev depot specified.\n")
        isValid = False
    if config.git.repoPath is None:
        config.logger.error("No Git repository specified.\n")
        isValid = False

    return isValid

def LoadConfigOrDefaults(configFilename):
    config = Config.fromfile(configFilename)

    if config is None:
        config = Config(accurev=Config.AccuRev(None), git=Config.Git(None), usermaps=[], logFilename=None)
        
    return config

def PrintConfigSummary(config):
    if config is not None:
        config.logger.info('Config info:')
        config.logger.info('  now: {0}'.format(datetime.now()))
        config.logger.info('  git')
        config.logger.info('    repo path: {0}'.format(config.git.repoPath))
        if config.git.remoteMap is not None:
            for remoteName in config.git.remoteMap:
                remote = config.git.remoteMap[remoteName]
                config.logger.info('    remote: {name} {url}{push_url}'.format(name=remote.name, url=remote.url, push_url = '' if remote.pushUrl is None or remote.url == remote.pushUrl else ' (push:{push_url})'.format(push_url=remote.pushUrl)))
                
        config.logger.info('    finalize:  {0}'.format(config.git.finalize))
        config.logger.info('  accurev:')
        config.logger.info('    depot: {0}'.format(config.accurev.depot))
        if config.accurev.streamMap is not None:
            config.logger.info('    stream list:')
            for stream in config.accurev.streamMap:
                config.logger.info('      - {0} -> {1}'.format(stream, config.accurev.streamMap[stream]))
        else:
            config.logger.info('    stream list: all included')
        config.logger.info('    start tran.: #{0}'.format(config.accurev.startTransaction))
        config.logger.info('    end tran.:   #{0}'.format(config.accurev.endTransaction))
        config.logger.info('    username: {0}'.format(config.accurev.username))
        config.logger.info('    command cache: {0}'.format(config.accurev.commandCacheFilename))
        config.logger.info('  method: {0}'.format(config.method))
        config.logger.info('  usermaps: {0}'.format(len(config.usermaps)))
        config.logger.info('  log file: {0}'.format(config.logFilename))
        config.logger.info('  verbose:  {0}'.format(config.logger.isDbgEnabled))
    
# ################################################################################################ #
# Script Main                                                                                      #
# ################################################################################################ #
def AccuRev2GitMain(argv):
    global state
    
    configFilename = Config.FilenameFromScriptName(argv[0])
    defaultExampleConfigFilename = '{0}.example.xml'.format(configFilename)
    
    # Set-up and parse the command line arguments. Examples from https://docs.python.org/dev/library/argparse.html
    parser = argparse.ArgumentParser(description="Conversion tool for migrating AccuRev repositories into Git. Configuration of the script is done with a configuration file whose filename is `{0}` by default. The filename can be overridden by providing the `-c` option described below. Command line arguments, if given, override the equivalent options in the configuration file.".format(configFilename))
    parser.add_argument('-c', '--config', dest='configFilename', default=configFilename, metavar='<config-filename>', help="The XML configuration file for this script. This file is required for the script to operate. By default this filename is set to be `{0}`.".format(configFilename))
    parser.add_argument('-u', '--accurev-username',  dest='accurevUsername', metavar='<accurev-username>',  help="The username which will be used to retrieve and populate the history from AccuRev.")
    parser.add_argument('-p', '--accurev-password',  dest='accurevPassword', metavar='<accurev-password>',  help="The password for the provided accurev username.")
    parser.add_argument('-t', '--accurev-depot', dest='accurevDepot',        metavar='<accurev-depot>',     help="The AccuRev depot in which the streams that are being converted are located. This script currently assumes only one depot is being converted at a time.")
    parser.add_argument('-g', '--git-repo-path', dest='gitRepoPath',         metavar='<git-repo-path>',     help="The system path to an existing folder where the git repository will be created.")
    parser.add_argument('-f', '--finalize',      dest='finalize', action='store_const', const=True,         help="Finalize the git repository by creating branch merge points. This flag will trigger this scripts 'branch stitching' mode and should only be used once the conversion has been completed. It won't work as expected if the repo continues to be processed after this step. The script will attempt to collapse commits which are a result of a promotion into a parent stream where the diff between the parent and the child is empty. It will also try to link promotions correctly into a merge commit from the child into the parent.")
    parser.add_argument('-M', '--method', dest='conversionMethod', choices=['pop', 'diff', 'deep-hist', 'transactions'], metavar='<conversion-method>', help="Specifies the method which is used to perform the conversion. Can be either 'pop', 'diff' or 'deep-hist'. 'pop' specifies that every transaction is populated in full. 'diff' specifies that only the differences are populated but transactions are iterated one at a time. 'deep-hist' specifies that only the differences are populated and that only transactions that could have affected this stream are iterated. 'transactions' is a completely different type of conversion that iterates over transactions and strives to generate the most accurate representation of the accurev streams in git.")
    parser.add_argument('-r', '--restart',    dest='restart', action='store_const', const=True, help="Discard any existing conversion and start over.")
    parser.add_argument('-v', '--verbose',    dest='debug',   action='store_const', const=True, help="Print the script debug information. Makes the script more verbose.")
    parser.add_argument('-L', '--log-file',   dest='logFile', metavar='<log-filename>',         help="Sets the filename to which all console output will be logged (console output is still printed).")
    parser.add_argument('-q', '--no-log-file', dest='disableLogFile',  action='store_const', const=True, help="Do not log info to the log file. Alternatively achieved by not specifying a log file filename in the configuration file.")
    parser.add_argument('-l', '--reset-log-file', dest='resetLogFile', action='store_const', const=True, help="Instead of appending new log info to the file truncate it instead and start over.")
    parser.add_argument('--example-config', nargs='?', dest='exampleConfigFilename', const=defaultExampleConfigFilename, default=None, metavar='<example-config-filename>', help="Generates an example configuration file and exits. If the filename isn't specified a default filename '{0}' is used. Commandline arguments, if given, override all options in the configuration file.".format(defaultExampleConfigFilename, configFilename))
    parser.add_argument('-m', '--check-missing-users', dest='checkMissingUsers', action='store_const', const=True, help="It will print a list of usernames that are in accurev but were not found in the usermap.")
    parser.add_argument('--auto-config', nargs='?', dest='autoConfigFilename', const=configFilename, default=None, metavar='<config-filename>', help="Auto-generate the configuration file from known AccuRev information. It is required that an accurev username and password are provided either in an existing config file or via the -u and -p options. If there is an existing config file it is backed up and only the accurev username and password will be copied to the new configuration file. If you wish to preserve the config but add more information to it then it is recommended that you use the --fixup-config option instead.")
    parser.add_argument('--fixup-config', nargs='?', dest='fixupConfigFilename', const=configFilename, default=None, metavar='<config-filename>', help="Fixup the configuration file by adding updated AccuRev information. It is the same as the --auto-config option but the existing configuration file options are preserved. Other command line arguments that are provided will override the existing configuration file options for the new configuration file.")
    
    args = parser.parse_args()
    
    # Dump example config if specified
    doEarlyReturn = False
    earlyReturnCode = 0
    if args.exampleConfigFilename is not None:
        earlyReturnCode = DumpExampleConfigFile(args.exampleConfigFilename)
        doEarlyReturn = True

    if args.autoConfigFilename is not None:
        earlyReturnCode = AutoConfigFile(filename=args.autoConfigFilename, args=args, preserveConfig=False)
        doEarlyReturn = True

    if args.fixupConfigFilename is not None:
        earlyReturnCode = AutoConfigFile(filename=args.fixupConfigFilename, args=args, preserveConfig=True)
        doEarlyReturn = True

    if doEarlyReturn:
        return earlyReturnCode
    
    # Load the config file
    config = Config.fromfile(filename=args.configFilename)
    if config is None:
        sys.stderr.write("Config file '{0}' not found.\n".format(args.configFilename))
        return 1
    elif config.git is not None:
        if not os.path.isabs(config.git.repoPath):
            config.git.repoPath = os.path.abspath(config.git.repoPath)

    # Set the overrides for in the configuration from the arguments
    SetConfigFromArgs(config=config, args=args)
    
    if not ValidateConfig(config):
        return 1
    
    config.logger.isDbgEnabled = ( args.debug == True )

    state = AccuRev2Git(config)
    
    if config.logFilename is not None and not args.disableLogFile:
        mode = 'a'
        if args.resetLogFile:
            mode = 'w'
        with codecs.open(config.logFilename, mode, 'utf-8') as f:
            f.write(u'{0}\n'.format(u" ".join(sys.argv)))
            state.config.logger.logFile = f
            state.config.logger.logFileDbgEnabled = ( args.debug == True )
    
            PrintConfigSummary(state.config)
            if args.checkMissingUsers:
                PrintMissingUsers(state.config)
            state.config.logger.info("Restart:" if args.restart else "Start:")
            state.config.logger.referenceTime = datetime.now()
            rv = state.Start(isRestart=args.restart)
    else:
        PrintConfigSummary(state.config)
        if args.checkMissingUsers:
            PrintMissingUsers(state.config)
        state.config.logger.info("Restart:" if args.restart else "Start:")
        state.config.logger.referenceTime = datetime.now()
        rv = state.Start(isRestart=args.restart)

    return rv
        
# ################################################################################################ #
# Script Start                                                                                     #
# ################################################################################################ #
if __name__ == "__main__":
    AccuRev2GitMain(sys.argv)
