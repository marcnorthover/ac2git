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
                repoPath     = xmlElement.attrib.get('repo-path')
                messageStyle = xmlElement.attrib.get('message-style')
                
                remoteMap = OrderedDict()
                remoteElementList = xmlElement.findall('remote')
                for remoteElement in remoteElementList:
                    remoteName     = remoteElement.attrib.get("name")
                    remoteUrl      = remoteElement.attrib.get("url")
                    remotePushUrl  = remoteElement.attrib.get("push-url")
                    
                    remoteMap[remoteName] = git.GitRemoteListItem(name=remoteName, url=remoteUrl, pushUrl=remotePushUrl)

                return cls(repoPath=repoPath, messageStyle=messageStyle, remoteMap=remoteMap)
            else:
                return None
            
        def __init__(self, repoPath, messageStyle=None, remoteMap=None):
            self.repoPath     = repoPath
            self.messageStyle = messageStyle
            self.remoteMap    = remoteMap

        def __repr__(self):
            str = "Config.Git(repoPath=" + repr(self.repoPath)
            if self.messageStyle is not None:
                str += ", messageStyle=" + repr(self.messageStyle)
            if self.remoteMap is not None:
                str += ", remoteMap="    + repr(self.remoteMap)
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

    @ staticmethod
    def GetBooleanAttribute(xmlElement, attribute):
        if xmlElement is None or attribute is None:
            return None
        value = xmlElement.attrib.get(attribute)
        if value is not None:
            if value.lower() == "true":
                value = True
            elif value.lower() == "false":
                value = False
            else:
                Exception("Error, could not parse {attr} attribute of tag {tag}. Expected 'true' or 'false', but got '{value}'.".format(attr=attribute, tag=xmlElement.tag, value=value))

        return value
                

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

            mergeStrategy = "normal" # Defaults to normal
            mergeStrategyElem = xmlRoot.find('merge-strategy')
            if mergeStrategyElem is not None:
                mergeStrategy = mergeStrategyElem.text

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

            return cls(accurev=accurev, git=git, usermaps=usermaps, method=method, mergeStrategy=mergeStrategy, logFilename=logFilename, includes=includes)
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

    def __init__(self, accurev = None, git = None, usermaps = None, method = None, mergeStrategy = None, logFilename = None, includes = []):
        self.accurev       = accurev
        self.git           = git
        self.usermaps      = usermaps
        self.method        = method
        self.mergeStrategy = mergeStrategy
        self.logFilename   = logFilename
        self.logger        = Config.Logger()
        self.includes      = includes
        
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
    gitRefsNamespace = 'refs/ac2git/'
    gitNotesRef_state = 'ac2git'
    gitNotesRef_accurevInfo = 'accurev'

    commandFailureRetryCount = 3
    commandFailureSleepSeconds = 3

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
                    return (usermap.gitName, usermap.gitEmail)
        state.config.logger.error("Cannot find git details for accurev username {0}".format(accurevUsername))
        return (accurevUsername, None)

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

    def GetFirstTransaction(self, depot, streamName, startTransaction=None, endTransaction=None, useCache=False):
        # Get the stream creation transaction (mkstream). Note: The first stream in the depot doesn't have an mkstream transaction.
        mkstream, mkstreamXml = self.TryHist(depot=depot, timeSpec="now", streamName=streamName, transactionKind="mkstream")
        if mkstream is None:
            return None

        tr = None
        if len(mkstream.transactions) == 0:
            self.config.logger.info( "The root stream has no mkstream transaction. Starting at transaction 1." )
            # the assumption is that the depot name matches the root stream name (for which there is no mkstream transaction)
            mkstream, mkstreamXml = self.TryHist(depot=depot, timeSpec="1")
            if mkstream is None or len(mkstream.transactions) == 0:
                raise Exception("Error: assumption that the root stream has the same name as the depot doesn't hold. Aborting...")
            tr = mkstream.transactions[0]
        else:
            tr = mkstream.transactions[0]
            if len(mkstream.transactions) != 1:
                self.config.logger.error( "There seem to be multiple mkstream transactions for this stream... Using {0}".format(tr.id) )

        hist = mkstream
        histXml = mkstreamXml

        if startTransaction is not None:
            startTrHist, startTrXml = self.TryHist(depot=depot, timeSpec=startTransaction)
            if startTrHist is None:
                return None

            startTr = startTrHist.transactions[0]
            if tr.id < startTr.id:
                self.config.logger.info( "The first transaction (#{0}) for stream {1} is earlier than the conversion start transaction (#{2}).".format(tr.id, streamName, startTr.id) )
                tr = startTr
                hist = startTrHist
                histXml = startTrXml

        if endTransaction is not None:
            endTrHist, endTrHistXml = self.TryHist(depot=depot, timeSpec=endTransaction)
            if endTrHist is None:
                return None

            endTr = endTrHist.transactions[0]
            if endTr.id < tr.id:
                self.config.logger.info( "The first transaction (#{0}) for stream {1} is later than the conversion end transaction (#{2}).".format(tr.id, streamName, startTr.id) )
                tr = None
                return None

        return hist, histXml

    def GetLastCommitHash(self, branchName=None, ref=None):
        cmd = []
        commitHash = None
        if ref is not None:
            for i in range(0, AccuRev2Git.commandFailureRetryCount):
                cmd = [ u'git', u'show-ref', u'--hash', ref ]
                commitHash = self.gitRepo.raw_cmd(cmd)
                if commitHash is None or len(commitHash.strip()) > 0:
                    break
        else:
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
                time.sleep(AccuRev2Git.commandFailureSleepSeconds)

        if commitHash is None:
            self.config.logger.error("Failed to retrieve last git commit hash. Command `{0}` failed.".format(' '.join(cmd)))
        else:
            commitHash = commitHash.strip()

        return commitHash

    def UpdateAndCheckoutRef(self, ref, commitHash, checkout=True):
        if ref is not None and commitHash is not None and len(ref) > 0 and len(commitHash) > 0:
            if ref.startswith('refs/heads/'):
                # refs/heads are branches which are updated automatically when you commit to them (provided we have them checked out).
                # so at least raise a warning for the user.
                self.config.logger.info( "Warning: overwriting ref {ref} which is a branch.".format(ref=ref) )

            # If we were asked to update a ref, not updating it is considered a failure to commit.
            if self.gitRepo.raw_cmd([ u'git', u'update-ref', ref, commitHash ]) is None:
                self.config.logger.error( "Failed to update ref {ref} to commit {hash}".format(ref=ref, hash=commitHash) )
                return False
            if checkout and ref != 'HEAD' and self.gitRepo.checkout(branchName=ref) is None: # no point in checking out HEAD if that's what we've updated!
                self.config.logger.error( "Failed to checkout ref {ref} to commit {hash}".format(ref=ref, hash=commitHash) )
                return False

            return True

        return None

    def SafeCheckout(self, ref, doReset=False, doClean=False):
        status = self.gitRepo.status()
        if doReset:
            self.config.logger.dbg( "Reset current branch - '{br}'".format(br=status.branch) )
            self.gitRepo.reset(isHard=True)
        if doClean:
            self.config.logger.dbg( "Clean current branch - '{br}'".format(br=status.branch) )
            self.gitRepo.clean(directories=True, force=True, forceSubmodules=True, includeIgnored=True)
            pass
        if ref is not None and status.branch != ref:
            self.config.logger.dbg( "Checkout {ref}".format(ref=ref) )
            self.gitRepo.checkout(branchName=ref)
            status = self.gitRepo.status()
            self.config.logger.dbg( "On branch {branch} - {staged} staged, {changed} changed, {untracked} untracked files{initial_commit}.".format(branch=status.branch, staged=len(status.staged), changed=len(status.changed), untracked=len(status.untracked), initial_commit=', initial commit' if status.initial_commit else '') )
            if status is None:
                raise Exception("Invalid initial state! The status command return is invalid.")
            if status.branch is None or status.branch != ref:
                # The parser for the status isn't very smart and git doesn't necessarily report the name of the ref that you have checked out. So, check if the current HEAD points to the desired ref by comparing hashes.
                headHash = self.gitRepo.raw_cmd(['git', 'log', '--format=%H', 'HEAD', '-1'])
                refHash = self.gitRepo.raw_cmd(['git', 'log', '--format=%H', ref, '-1'])
                if headHash is None:
                    raise Exception("Failed to determine the hash of the HEAD commit!")
                elif refHash is None:
                    raise Exception("Failed to determine the hash of the {ref} commit!".format(ref=ref))
                elif refHash != headHash:
                    raise Exception("Invalid initial state! The status command returned an invalid name for current branch. Expected {ref} but got {statusBranch}.".format(ref=ref, statusBranch=status.branch))
            if len(status.staged) != 0 or len(status.changed) != 0 or len(status.untracked) != 0:
                raise Exception("Invalid initial state! There are changes in the tracking repository. Staged {staged}, changed {changed}, untracked {untracked}.".format(staged=status.staged, changed=status.changed, untracked=status.untracked))

    def Commit(self, transaction=None, allowEmptyCommit=False, messageOverride=None, parents=None, treeHash=None, ref=None, checkout=True):
        usePlumbing = (parents is not None or treeHash is not None)
        isFirstCommit = (parents is not None and len(parents) == 0)

        # Custom messages for when we have a transaction.
        trMessage, forTrMessage = '', ''
        if transaction is not None:
            trMessage = ' transaction {0}'.format(transaction.id)
            forTrMessage = ' for{0}'.format(trMessage)

        # Begin the commit processing.
        if treeHash is None:
            self.PreserveEmptyDirs()

            # Add all of the files to the index
            self.gitRepo.add(force=True, all=True, git_opts=[u'-c', u'core.autocrlf=false'])

        # Create temporary file for the commit message.
        messageFilePath = None
        with tempfile.NamedTemporaryFile(mode='w+', prefix='ac2git_commit_', delete=False) as messageFile:
            messageFilePath = messageFile.name
            emptyMessage = True
            if messageOverride is not None:
                if len(messageOverride) > 0:
                    messageFile.write(messageOverride)
                    emptyMessage = False
            elif transaction is not None and transaction.comment is not None and len(transaction.comment) > 0:
                # In git the # at the start of the line indicate that this line is a comment inside the message and will not be added.
                # So we will just add a space to the start of all the lines starting with a # in order to preserve them.
                messageFile.write(transaction.comment)
                emptyMessage = False

            if emptyMessage:
                # `git commit` and `git commit-tree` commands, when given an empty file for the commit message, seem to revert to
                # trying to read the commit message from the STDIN. This is annoying since we don't want to be opening a pipe to
                # the spawned process all the time just to write an EOF character so instead we will just add a single space as the
                # message and hope the user doesn't notice.
                # For the `git commit` command it's not as bad since white-space is always stripped from commit messages. See the 
                # `git commit --cleanup` option for details.
                messageFile.write(' ')
        
        if messageFilePath is None:
            self.config.logger.error("Failed to create temporary file for commit message{0}".format(forTrMessage))
            return None

        # Get the author's and committer's name, email and timezone information.
        committerName, committerEmail = None, None
        committerDate, committerTimezone = None, None
        if transaction is not None:
            committerName, committerEmail = self.GetGitUserFromAccuRevUser(transaction.user)
            committerDate, committerTimezone = self.GetGitDatetime(accurevUsername=transaction.user, accurevDatetime=transaction.time)
        if not isFirstCommit:
            lastCommitHash = self.GetLastCommitHash(ref=ref) # If ref is None, it will get the last commit hash from the HEAD ref.
            if usePlumbing and parents is None:
                if lastCommitHash is None:
                    self.config.logger.info("No last commit hash available. Fatal error, aborting!")
                    os.remove(messageFilePath)
                    return None
                parents = [ lastCommitHash ]
            elif lastCommitHash is None:
                self.config.logger.info("No last commit hash available. Non-fatal error, continuing.")
        else:
            lastCommitHash = None

        # Make the commit.
        commitHash = None
        if usePlumbing:
            if treeHash is None:
                treeHash = self.gitRepo.write_tree()
            if treeHash is not None and len(treeHash.strip()) > 0:
                treeHash = treeHash.strip()
                commitHash = self.gitRepo.commit_tree(tree=treeHash, parents=parents, message_file=messageFilePath, committer_name=committerName, committer_email=committerEmail, committer_date=committerDate, committer_tz=committerTimezone, author_name=committerName, author_email=committerEmail, author_date=committerDate, author_tz=committerTimezone, allow_empty=allowEmptyCommit, git_opts=[u'-c', u'core.autocrlf=false'])
                if commitHash is None:
                    self.config.logger.error( "Failed to commit tree {0}{1}".format(treeHash, forTrMessage) )
                else:
                    commitHash = commitHash.strip()
            else:
                self.config.logger.error( "Failed to write tree{0}".format(forTrMessage) )
        else:
            commitResult = self.gitRepo.commit(message_file=messageFilePath, committer_name=committerName, committer_email=committerEmail, committer_date=committerDate, committer_tz=committerTimezone, author_name=committerName, author_email=committerEmail, author_date=committerDate, author_tz=committerTimezone, allow_empty_message=True, allow_empty=allowEmptyCommit, cleanup='whitespace', git_opts=[u'-c', u'core.autocrlf=false'])
            if commitResult is not None:
                commitHash = commitResult.shortHash
                if commitHash is None:
                    commitHash = self.GetLastCommitHash()
            elif "nothing to commit" in self.gitRepo.lastStdout:
                self.config.logger.dbg( "nothing to commit{0}...?".format(forTrMessage) )
            else:
                self.config.logger.error( "Failed to commit".format(trMessage) )
                self.config.logger.error( "\n{0}\n{1}\n".format(self.gitRepo.lastStdout, self.gitRepo.lastStderr) )

        # For detached head states (which occur when you're updating a ref and not a branch, even if checked out) we need to make sure to update the HEAD. Either way it doesn't hurt to
        # do this step whether we are using plumbing or not...
        if commitHash is not None:
            if ref is None:
                ref = 'HEAD'
            if self.UpdateAndCheckoutRef(ref=ref, commitHash=commitHash, checkout=(checkout and ref != 'HEAD')) != True:
                self.config.logger.error( "Failed to update ref {ref} with commit {h}{forTr}".format(ref=ref, h=commitHash, forTr=forTrMessage) )
                commitHash = None

        os.remove(messageFilePath)

        if commitHash is not None:
            if lastCommitHash == commitHash:
                self.config.logger.error("Commit command returned True when nothing was committed...? Last commit hash {0} didn't change after the commit command executed.".format(lastCommitHash))
                commitHash = None # Invalidate return value
        else:
            self.config.logger.error("Failed to commit{tr}.".format(tr=trMessage))

        return commitHash

    def GetStreamMap(self):
        streamMap = self.config.accurev.streamMap
        if streamMap is None or len(streamMap) == 0:
            # When the stream map is missing or empty we intend to process all streams
            streams = accurev.show.streams(depot=self.config.accurev.depot)
            for stream in streams.streams:
                streamMap[stream.name] = stream.name
        return streamMap

    def FindNextChangeTransaction(self, streamName, startTrNumber, endTrNumber, deepHist=None):
        # Iterate over transactions in order using accurev diff -a -i -v streamName -V streamName -t <lastProcessed>-<current iterator>
        if self.config.method == "diff":
            nextTr = startTrNumber + 1
            diff, diffXml = self.TryDiff(streamName=streamName, firstTrNumber=startTrNumber, secondTrNumber=nextTr)
            if diff is None:
                return (None, None)
    
            # Note: This is likely to be a hot path. However, it cannot be optimized since a revert of a transaction would not show up in the diff even though the
            #       state of the stream was changed during that period in time. Hence to be correct we must iterate over the transactions one by one unless we have
            #       explicit knowlege of all the transactions which could affect us via some sort of deep history option...
            while nextTr <= endTrNumber and len(diff.elements) == 0:
                nextTr += 1
                diff, diffXml = self.TryDiff(streamName=streamName, firstTrNumber=startTrNumber, secondTrNumber=nextTr)
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
                    diff, diffXml = self.TryDiff(streamName=streamName, firstTrNumber=startTrNumber, secondTrNumber=tr.id)
                    if diff is None:
                        return (None, None)
                    elif len(diff.elements) > 0:
                        self.config.logger.dbg("FindNextChangeTransaction deep-hist: {0}".format(tr.id))
                        return (tr.id, diff)
                    else:
                        self.config.logger.dbg("FindNextChangeTransaction deep-hist skipping: {0}, diff was empty...".format(tr.id))

            diff, diffXml = self.TryDiff(streamName=streamName, firstTrNumber=startTrNumber, secondTrNumber=endTrNumber)
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

    def TryDiff(self, streamName, firstTrNumber, secondTrNumber):
        for i in range(0, AccuRev2Git.commandFailureRetryCount):
            diffXml = accurev.raw.diff(all=True, informationOnly=True, verSpec1=streamName, verSpec2=streamName, transactionRange="{0}-{1}".format(firstTrNumber, secondTrNumber), isXmlOutput=True, useCache=self.config.accurev.UseCommandCache())
            if diffXml is not None:
                diff = accurev.obj.Diff.fromxmlstring(diffXml)
                if diff is not None:
                    break
        if diff is None:
            self.config.logger.error( "accurev diff failed! stream: {0} time-spec: {1}-{2}".format(streamName, firstTrNumber, secondTrNumber) )
        return diff, diffXml

    def TryHist(self, depot, timeSpec, streamName=None, transactionKind=None):
        trHist = None
        for i in range(0, AccuRev2Git.commandFailureRetryCount):
            trHistXml = accurev.raw.hist(depot=depot, stream=streamName, timeSpec=timeSpec, transactionKind=transactionKind, useCache=self.config.accurev.UseCommandCache(), isXmlOutput=True, expandedMode=True, verboseMode=True)
            if trHistXml is not None:
                trHist = accurev.obj.History.fromxmlstring(trHistXml)
                if trHist is not None:
                    break
        return trHist, trHistXml

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

    def TryStreams(self, depot, timeSpec):
        streams = None
        for i in range(0, AccuRev2Git.commandFailureRetryCount):
            streamsXml = accurev.raw.show.streams(depot=depot, timeSpec=timeSpec, isXmlOutput=True, includeDeactivatedItems=True, includeHasDefaultGroupAttribute=True, useCache=self.config.accurev.UseCommandCache())
            if streamsXml is not None:
                streams = accurev.obj.Show.Streams.fromxmlstring(streamsXml)
                if streams is not None:
                    break
        return streams, streamsXml

    def TryDepots(self):
        depots = None
        for i in range(0, AccuRev2Git.commandFailureRetryCount):
            depotsXml = accurev.raw.show.depots(isXmlOutput=True, includeDeactivatedItems=True)
            if depotsXml is not None:
                depots = accurev.obj.Show.Depots.fromxmlstring(depotsXml)
                if depots is not None:
                    break
        return depots, depotsXml

    def WriteInfoFiles(self, path, depot, transaction, streamsXml=None, histXml=None, streamName=None, diffXml=None, useCommandCache=False):
        streams = None
        hist = None
        diff = None

        if streamsXml is not None:
            streams = accurev.obj.Show.Streams.fromxmlstring(streamsXml)
        
        if streams is None or streamsXml is None:
            streams, streamsXml = self.TryStreams(depot=depot, timeSpec=transaction)
            if streams is None or streamsXml is None:
                return (None, None, None)

        if histXml is not None:
            hist = accurev.obj.History.fromxmlstring(histXml)
        if hist is None or histXml is None:
            hist, histXml = self.TryHist(depot=depot, timeSpec=transaction)
            if hist is None or histXml is None:
                return (None, None)

        tr = hist.transactions[0]
        if tr.id > 1 and tr.Type != "mkstream":
            if diffXml is not None:
                diff = accurev.obj.Diff.fromxmlstring(streamsXml)
            
            if diff is None or diffXml is None:
                if streamName is not None:
                    diff, diffXml = self.TryDiff(streamName=streamName, firstTrNumber=tr.id, secondTrNumber=(tr.id - 1))
                    if diff is None or diffXml is None:
                        return (None, None)
                else:
                    return (None, None)

            diffFilePath = os.path.join(self.gitRepo.path, 'diff.xml')
            with codecs.open(diffFilePath, 'w') as f:
                f.write(re.sub('TaskId="[0-9]+"', 'TaskId="0"', diffXml))

        streamsFilePath = os.path.join(path, 'streams.xml')
        with codecs.open(streamsFilePath, 'w') as f:
            f.write(re.sub('TaskId="[0-9]+"', 'TaskId="0"', streamsXml))
        
        histFilePath = os.path.join(path, 'hist.xml')
        with codecs.open(histFilePath, 'w') as f:
            f.write(re.sub('TaskId="[0-9]+"', 'TaskId="0"', histXml))

    def GetDepot(self, depot):
        depotNumber = None
        depotName = None
        try:
            depotNumber = int(depot)
        except:
            if isinstance(depot, str):
                depotName = depot
        depotsRef = '{refsNS}depots'.format(refsNS=AccuRev2Git.gitRefsNamespace)

        # Check if the ref exists!
        commitHash = self.GetLastCommitHash(self, ref=depotsRef)
        haveCommitted = False
        if commitHash is None:
            # It doesn't exist, we can create it.        
            self.config.logger.dbg( "Ref '{br}' doesn't exist.".format(br=depotsRef) )

            # Delete everything in the index and working directory.
            self.gitRepo.rm(fileList=['.'], force=True, recursive=True)
            self.ClearGitRepo()

            depots, depotsXml = self.TryDepots()
            if depots is None or depotsXml is None:
                return None

            depotsFilePath = os.path.join(self.gitRepo.path, 'depots.xml')
            with codecs.open(depotsFilePath, 'w') as f:
                f.write(re.sub('TaskId="[0-9]+"', 'TaskId="0"', depotsXml))

            commitHash = self.Commit(transaction=None, messageOverride="depots at ac2git invocation.", parents=[], ref=depotsRef)
            if commitHash is None:
                self.config.logger.dbg( "First commit on the depots ref ({ref}) has failed. Aborting!".format(ref=depotsRef) )
                return None
            else:
                self.config.logger.info( "Depots ref updated {ref} -> commit {hash}".format(hash=commitHash[:8], ref=depotsRef) )
                haveCommitted = True
        else:
            depotsXml, depots = self.GetDepotsInfo(ref=commitHash)

        # Try and find the depot in the list of existing depots.
        for d in depots.depots:
            if depotName is not None and d.name == depotName:
                return d
            elif depotNumber is not None and d.number == depotNumber:
                return d

        if haveCommitted:
            self.config.logger.info( "Failed to find depot {d} on depots ref {r} at commit {h}".format(d=depot, h=commitHash[:8], r=depotsRef) )
            return None

        # We haven't committed anything yet so a depot might have been renamed since we started. Run the depots command again and commit it if there have been any changes.

        self.gitRepo.checkout(branchName=depotsRef)

        # Delete everything in the index and working directory.
        self.gitRepo.rm(fileList=['.'], force=True, recursive=True)
        self.ClearGitRepo()

        depots, depotsXml = self.TryDepots()
        if depots is None or depotsXml is None:
            return None

        depotsFilePath = os.path.join(self.gitRepo.path, 'depots.xml')
        with codecs.open(depotsFilePath, 'w') as f:
            f.write(re.sub('TaskId="[0-9]+"', 'TaskId="0"', depotsXml))

        commitHash = self.Commit(transaction=None, messageOverride="depots at ac2git invocation.".format(trId=tr.id), ref=depotsRef)
        if commitHash is None:
            self.config.logger.dbg( "Commit on the depots ref ({ref}) has failed. Couldn't find the depot {d}. Aborting!".format(ref=depotsRef, d=depot) )
            return None
        else:
            self.config.logger.info( "Depots ref updated {ref} -> commit {hash}".format(hash=commitHash[:8], ref=depotsRef) )
            haveCommitted = True

            # Try and find the depot in the list of existing depots.
            for d in depots.depots:
                if depotName is not None and d.name == depotName:
                    return d
                elif depotNumber is not None and d.number == depotNumber:
                    return d

        return None

    def GetStreamRefsNamespace(self, depot):
        d = self.GetDepot(depot)
        if d is not None:
            return '{refsNS}{depot}/streams/'.format(refsNS=AccuRev2Git.gitRefsNamespace, depot=d.number)
        return None

    def GetStreamRefs(self, depot, streamNumber):
        stateRef, dataRef, hwmRef = None, None, None
        ns = self.GetStreamRefsNamespace(depot)
        if ns is not None and streamNumber is not None and isinstance(streamNumber, int):
            refPrefix = '{ns}stream_{streamNumber}'.format(ns=ns, streamNumber=streamNumber)
            dataRef  = '{prefix}_data'.format(prefix=refPrefix)
            stateRef = '{prefix}_info'.format(prefix=refPrefix)
            hwmRef = '{prefix}_hwm'.format(prefix=refPrefix) # High-water mark ref.
        return (stateRef, dataRef, hwmRef)

    # Gets the diff.xml contents and parsed accurev.obj.Diff object from the given \a ref (git ref or hash).
    def GetDiffInfo(self, ref):
        # Get the diff information. (if any)
        diff = None
        diffXml = self.gitRepo.raw_cmd(['git', 'show', '{hash}:diff.xml'.format(hash=ref)]) # Doesn't exist for the mkstream transaction (first commit)
        if diffXml is not None:
            diff = accurev.obj.Diff.fromxmlstring(diffXml)
        elif diffXml is None or len(diffXml) == 0:
            raise Exception("Command failed! git show {hash}:diff.xml".format(hash=ref))
        return (diffXml, diff)

    # Gets the hist.xml contents and parsed accurev.obj.History object from the given \a ref (git ref or hash).
    def GetHistInfo(self, ref):
        # Get the hist information.
        hist = None
        histXml = self.gitRepo.raw_cmd(['git', 'show', '{hash}:hist.xml'.format(hash=ref)])
        if histXml is not None or len(histXml) != 0:
            hist = accurev.obj.History.fromxmlstring(histXml)
        else:
            raise Exception("Command failed! git show {hash}:hist.xml".format(hash=ref))
        return (histXml, hist)

    # Gets the streams.xml contents and parsed accurev.obj.Show.Streams object from the given \a ref (git ref or hash).
    def GetStreamsInfo(self, ref):
        # Get the stream information.
        streams = None
        streamsXml = self.gitRepo.raw_cmd(['git', 'show', '{hash}:streams.xml'.format(hash=ref)])
        if streamsXml is not None or len(streamsXml) != 0:
            streams = accurev.obj.Show.Streams.fromxmlstring(streamsXml)
        else:
            raise Exception("Command failed! git show {hash}:streams.xml".format(hash=ref))
        return (streamsXml, streams)

    # Gets the depots.xml contents and parsed accurev.obj.Show.Streams object from the given \a ref (git ref or hash).
    def GetDepotsInfo(self, ref):
        # Get the stream information.
        depots = None
        depotsXml = self.gitRepo.raw_cmd(['git', 'show', '{hash}:depots.xml'.format(hash=ref)])
        if depotsXml is not None or len(depotsXml) != 0:
            depots = accurev.obj.Show.Depots.fromxmlstring(depotsXml)
        else:
            raise Exception("Command failed! git show {hash}:depots.xml".format(hash=ref))
        return (depotsXml, depots)

    def RetrieveStreamInfo(self, depot, stream, stateRef, startTransaction, endTransaction):
        self.config.logger.info( "Processing Accurev state for {0} : {1} - {2}".format(stream.name, startTransaction, endTransaction) )

        # Check if the ref exists!
        stateRefObj = self.gitRepo.raw_cmd(['git', 'show-ref', stateRef])
        if stateRefObj is not None and len(stateRefObj) == 0:
            raise Exception("Invariant error! Expected non-empty string returned by git show-ref, but got '{s}'".format(s=stateRefObj))

        # Either checkout last state or make the initial commit for a new stateRef.
        tr = None
        commitHash = None
        if stateRefObj is not None:
            # This means that the ref already exists so we should switch to it.
            self.SafeCheckout(ref=stateRef, doReset=True, doClean=True)
            histXml, hist = self.GetHistInfo(ref=stateRef)
            tr = hist.transactions[0]
        else:
            self.config.logger.dbg( "Ref '{br}' doesn't exist.".format(br=stateRef) )
            # We are tracking a new stream
            firstHist, firstHistXml = self.GetFirstTransaction(depot=depot, streamName=stream.name, startTransaction=startTransaction, endTransaction=endTransaction)
            if firstHist is not None and len(firstHist.transactions) > 0:
                tr = firstHist.transactions[0]
                try:
                    destStream = self.GetDestinationStreamName(history=hist, depot=None)
                except:
                    destStream = None

                # Delete everything in the index and working directory.
                self.gitRepo.rm(fileList=['.'], force=True, recursive=True)
                self.ClearGitRepo()

                self.WriteInfoFiles(path=self.gitRepo.path, depot=depot, streamName=stream.name, transaction=tr.id, useCommandCache=self.config.accurev.UseCommandCache())

                commitHash = self.Commit(transaction=tr, messageOverride="transaction {trId}".format(trId=tr.id), parents=[], ref=stateRef)
                if commitHash is None:
                    self.config.logger.dbg( "{0} first commit has failed. Is it an empty commit? Aborting!".format(stream.name) )
                    return (None, None)
                else:
                    self.config.logger.info( "stream {streamName}: tr. #{trId} {trType} -> commit {hash} on {ref}".format(streamName=stream.name, trId=tr.id, trType=tr.Type, hash=commitHash[:8], ref=stateRef) )
            else:
                self.config.logger.info( "Failed to get the first transaction for {0} from accurev. Won't retrieve any further.".format(stream.name) )
                return (None, None)

        # Get the end transaction.
        endTrHist, endTrHistXml = self.TryHist(depot=depot, timeSpec=endTransaction)
        if endTrHist is None:
            self.config.logger.dbg("accurev hist -p {0} -t {1}.1 failed.".format(depot, endTransaction))
            return (None, None)
        endTr = endTrHist.transactions[0]
        self.config.logger.info("{0}: retrieving transaction range #{1} - #{2}".format(stream.name, tr.id, endTr.id))

        # Iterate over all of the transactions that affect the stream we are interested in and maybe the "chstream" transactions (which affect the streams.xml).
        deepHist = None
        if self.config.method == "deep-hist":
            ignoreTimelocks=False # The code for the timelocks is not tested fully yet. Once tested setting this to false should make the resulting set of transactions smaller
                                 # at the cost of slightly larger number of upfront accurev commands called.
            self.config.logger.dbg("accurev.ext.deep_hist(depot={0}, stream={1}, timeSpec='{2}-{3}', ignoreTimelocks={4})".format(depot, stream.name, tr.id, endTr.id, ignoreTimelocks))
            deepHist = accurev.ext.deep_hist(depot=depot, stream=stream.name, timeSpec="{0}-{1}".format(tr.id, endTr.id), ignoreTimelocks=ignoreTimelocks, useCache=self.config.accurev.UseCommandCache())
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
                if self.config.method == "pop":
                    self.ClearGitRepo()
                else:
                    if diff is None:
                        return (None, None)

                # The accurev hist command here must be used with the depot option since the transaction that has affected us may not
                # be a promotion into the stream we are looking at but into one of its parent streams. Hence we must query the history
                # of the depot and not the stream itself.
                hist, histXml = self.TryHist(depot=depot, timeSpec=nextTr)
                if hist is None:
                    self.config.logger.dbg("accurev hist -p {0} -t {1}.1 failed.".format(depot, endTransaction))
                    return (None, None)
                tr = hist.transactions[0]
                stream = accurev.show.streams(depot=depot, stream=stream.streamNumber, timeSpec=tr.id, useCache=self.config.accurev.UseCommandCache()).streams[0]

                self.WriteInfoFiles(path=self.gitRepo.path, depot=depot, streamName=stream.name, transaction=tr.id, useCommandCache=self.config.accurev.UseCommandCache())
                    
                # Commit
                commitHash = self.Commit(transaction=tr, messageOverride="transaction {trId}".format(trId=tr.id), ref=stateRef)
                if commitHash is None:
                    if "nothing to commit" in self.gitRepo.lastStdout:
                        self.config.logger.info("stream {streamName}: tr. #{trId} is a no-op. Potential but unlikely error. Continuing.".format(streamName=stream.name, trId=tr.id))
                    else:
                        break # Early return from processing this stream. Restarting should clean everything up.
                else:
                    if self.UpdateAndCheckoutRef(ref=stateRef, commitHash=commitHash) != True:
                        return (None, None)
                    self.config.logger.info( "stream {streamName}: tr. #{trId} {trType} -> commit {hash} on {ref}".format(streamName=stream.name, trId=tr.id, trType=tr.Type, hash=commitHash[:8], ref=stateRef) )
            else:
                self.config.logger.info( "Reached end transaction #{trId} for {streamName} -> {ref}".format(trId=endTr.id, streamName=stream.name, ref=stateRef) )
                break

        return (tr, commitHash)

    def GetHashForTransaction(self, ref, trNum):
        # Find the commit hash on our ref that corresponds to the provided transaction number.
        cmd = ['git', 'log', '--format=%H', '--grep', '^transaction {trId}$'.format(trId=trNum), ref]
        lastCommitHash = self.gitRepo.raw_cmd(cmd)
        if lastCommitHash is None:
            raise Exception("Couldn't query {ref} for Accurev state information at transaction {trId}. {cmd}".format(ref=ref, trId=trNum, cmd=' '.join(cmd)))
        lastCommitHash = lastCommitHash.strip()

        if len(lastCommitHash) == 0:
            self.config.logger.error( "Failed to load transaction ({trId}) from ref {ref}. '{cmd}' returned empty.".format(trId=trNum, ref=ref, cmd=' '.join(cmd)) )
            return None
        return lastCommitHash

    def GetTransactionForRef(self, ref):
        # Find the last transaction number that we processed.
        lastCommitInfo = self.gitRepo.raw_cmd(['git', 'log', '--pretty=oneline', ref, '-1'])
        if lastCommitInfo is None:
            raise Exception("Couldn't load last transaction for ref: {ref}".format(ref=ref))
        lastCommitInfo = lastCommitInfo.strip()
        if len(lastCommitInfo) == 0:
            raise Exception("Couldn't load last transaction for ref: {ref} (empty result)".format(ref=ref))
        lastCommitInfo = lastCommitInfo.split(' ')
        if len(lastCommitInfo) != 3:
            raise Exception("Unexpected format for last commit message! Expected 3 space separated fields but read: {info}".format(info=' '.join(lastCommitInfo)))
        return int(lastCommitInfo[2])

    def GetGitLogList(self, ref, afterCommitHash=None, gitLogFormat=None):
        # Get the list of new hashes that have been committed to the stateRef but we haven't processed on the ref just yet.
        cmd = ['git', 'log']
        if gitLogFormat is not None:
            cmd.append('--format={f}'.format(f=gitLogFormat))
        cmd.append(ref)
        if afterCommitHash is not None:
            cmd.append('^{lastHash}'.format(lastHash=afterCommitHash))

        hashList = self.gitRepo.raw_cmd(cmd)
        if hashList is None:
            self.config.logger.dbg("Couldn't get the commit hash list from the ref {ref}. '{cmd}'".format(ref=ref, cmd=' '.join(cmd)))
            return None

        hashList = hashList.strip()
        if len(hashList) == 0:
            return []

        return hashList.split('\n')

    # Uses the stateRef information to fetch the contents of the stream for each transaction that whose information was committed to the stateRef and commits it to the dataRef.
    def RetrieveStreamData(self, stream, dataRef, stateRef):
        # Check if the ref exists!
        dataRefObj = self.gitRepo.raw_cmd(['git', 'show-ref', dataRef])
        if dataRefObj is not None and len(dataRefObj) == 0:
            raise Exception("Invariant error! Expected non-empty string returned by git show-ref, but got '{str}'".format(s=dataRefObj))

        # Either checkout last state or make the initial commit for a new dataRef.
        lastTrId = None
        stateHashList = None
        if dataRefObj is not None:
            # This means that the ref already exists so we should switch to it.
            self.SafeCheckout(ref=dataRef, doReset=True, doClean=True)

            # Find the last transaction number that we processed on the dataRef.
            lastTrId = self.GetTransactionForRef(ref=dataRef)

            # Find the commit hash on our stateRef that corresponds to our last transaction number.
            lastStateCommitHash = self.GetHashForTransaction(ref=stateRef, trNum=lastTrId)
            if lastStateCommitHash is None:
                self.config.logger.error( "{dataRef} is pointing to transaction {trId} which wasn't found on the state ref {stateRef}.".format(trId=lastTrId, dataRef=dataRef, stateRef=stateRef) )
                return (None, None)

            # Get the list of new hashes that have been committed to the stateRef but we haven't processed on the dataRef just yet.
            stateHashList = self.GetGitLogList(ref=stateRef, afterCommitHash=lastStateCommitHash, gitLogFormat='%H')
            if stateHashList is None:
                raise Exception("Couldn't get the commit hash list to process from the Accurev state ref {stateRef}.".format(stateRef=stateRef))
            elif len(stateHashList) == 0:
                self.config.logger.error( "{dataRef} is upto date. Couldn't load any more transactions after tr. ({trId}) from Accurev state ref {stateRef}.".format(trId=lastTrId, dataRef=dataRef, stateRef=stateRef, lastHash=lastStateCommitHash) )

                # Get the first transaction that we are about to process.
                trHistXml, trHist = self.GetHistInfo(ref=lastStateCommitHash)
                tr = trHist.transactions[0]

                commitHash = self.GetHashForTransaction(ref=dataRef, trNum=tr.id)

                return (tr, commitHash)

        else:
            # Get all the hashes from the stateRef since we need to process them all.
            stateHashList = self.GetGitLogList(ref=stateRef, gitLogFormat='%H')
            if stateHashList is None:
                raise Exception("Couldn't get the commit hash list to process from the Accurev state ref {stateRef}.".format(stateRef=stateRef))

            if len(stateHashList) == 0:
                self.config.logger.error( "{dataRef} is upto date. No transactions available in Accurev state ref {stateRef}. git log {stateRef} returned empty.".format(dataRef=dataRef, stateRef=stateRef) )
                return (None, None)

            # Remove the first hash (last item) from the processing list and process it immediately.
            stateHash = stateHashList.pop()
            if stateHash is None or len(stateHash) == 0:
                raise Exception("Invariant error! We shouldn't have empty strings in the stateHashList")
            self.config.logger.info( "No {dr} found. Processing {h} on {sr} first.".format(dr=dataRef, h=stateHash, sr=stateRef) )

            # Get the first transaction that we are about to process.
            trHistXml, trHist = self.GetHistInfo(ref=stateHash)
            tr = trHist.transactions[0]

            lastTrId = tr.id

            # Delete everything in the index and working directory.
            self.gitRepo.rm(fileList=['.'], force=True, recursive=True)
            self.ClearGitRepo()

            # Populate the stream contents from accurev
            popResult = self.TryPop(streamName=stream.name, transaction=tr, overwrite=True)
            if not popResult:
                self.config.logger.error( "accurev pop failed for {trId} on {dataRef}".format(trId=tr.id, dataRef=dataRef) )
                return (None, None)

            # Make first commit.
            commitHash = self.Commit(transaction=tr, allowEmptyCommit=True, messageOverride="transaction {trId}".format(trId=tr.id), parents=[], ref=dataRef)
            if commitHash is None:
                # The first streams mkstream transaction will be empty so we may end up with an empty commit.
                self.config.logger.dbg( "{0} first commit has failed.".format(stream.name) )
                return (None, None)
            else:
                if self.gitRepo.checkout(branchName=dataRef) is None:
                    self.config.logger.dbg( "{0} failed to checkout data ref {1}. Aborting!".format(stream.name, dataRef) )
                    return (None, None)

                self.config.logger.info( "stream {streamName}: tr. #{trId} {trType} -> commit {hash} on {ref}".format(streamName=stream.name, trId=tr.id, trType=tr.Type, hash=commitHash[:8], ref=dataRef) )

        # Find the last transaction number that we processed on the dataRef.
        lastStateTrId = self.GetTransactionForRef(ref=stateRef)
        if lastStateTrId is None:
            self.config.logger.error( "Failed to get last transaction processed on the {ref}.".format(ref=stateRef) )
            return (None, None)
        # Notify the user what we are processing.
        self.config.logger.info( "Processing stream data for {0} : {1} - {2}".format(stream.name, lastTrId, lastStateTrId) )

        # Process all the hashes in the list
        for stateHash in reversed(stateHashList):
            if stateHash is None:
                raise Exception("Invariant error! Hashes in the stateHashList cannot be none here!")
            elif len(stateHash) == 0:
                raise Exception("Invariant error! Excess new lines returned by `git log`? Probably safe to skip but shouldn't happen.")

            # Get the diff information. (if any)
            diffXml, diff = self.GetDiffInfo(ref=stateHash)

            # Get the hist information.
            histXml, hist = self.GetHistInfo(ref=stateHash)

            # Get the stream information.
            streamsXml, streams = self.GetStreamsInfo(ref=stateHash)

            popOverwrite = (self.config.method == "pop")
            deletedPathList = None
            if self.config.method == "pop":
                self.ClearGitRepo()
            else:
                if diff is None:
                    self.config.logger.error( "No diff available for {trId} on {dataRef}".format(trId=tr.id, dataRef=dataRef) )
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

            tr = hist.transactions[0]
            streamAtTr = streams.getStream(stream.streamNumber)
            if streamAtTr is None:
                raise Exception("Failed to find stream {name} ({num}) in {list}".format(name=stream.name, num=stream.streamNumber, list=[(s.name, s.streamNumber) for s in streams]))
            else:
                stream = streamAtTr

            # Work out the source and destination streams for the promote (for the purposes of the commit message info).
            destStreamName, destStreamNumber = hist.toStream()
            destStream = None
            if destStreamNumber is not None:
                destStream = streams.getStream(destStreamNumber)
                if destStream is None:
                    raise Exception("Failed to find stream {name} ({num}) in {list}".format(name=destStreamName, num=destStreamNumber, list=[(s.name, s.streamNumber) for s in streams]))

            srcStream = None
            try:
                srcStreamName, srcStreamNumber = hist.fromStream()
                if srcStreamNumber is not None:
                    srcStream = streams.getStream(srcStreamNumber)
                    if srcStream is None:
                        raise Exception("Failed to find stream {name} ({num}) in {list}".format(name=srcStreamName, num=srcStreamNumber, list=[(s.name, s.streamNumber) for s in streams]))
            except:
                srcStreamName, srcStreamNumber = None, None

            # Populate
            self.config.logger.dbg( "{0} pop: {1} {2}{3}".format(stream.name, tr.Type, tr.id, " to {0}".format(destStreamName) if destStreamName is not None else "") )
            popResult = self.TryPop(streamName=stream.name, transaction=tr, overwrite=popOverwrite)
            if not popResult:
                self.config.logger.error( "accurev pop failed for {trId} on {dataRef}".format(trId=tr.id, dataRef=dataRef) )
                return (None, None)

            # Make the commit. Empty commits are allowed so that we match the state ref exactly (transaction for transaction).
            # Reasoning: Empty commits are cheap and since these are not intended to be seen by the user anyway so we may as well make them to have a simpler mapping.
            commitHash = self.Commit(transaction=tr, allowEmptyCommit=True, messageOverride="transaction {trId}".format(trId=tr.id), ref=dataRef)
            if commitHash is None:
                self.config.logger.error( "Commit failed for {trId} on {dataRef}".format(trId=tr.id, dataRef=dataRef) )
                return (None, None)
            else:
                self.config.logger.info( "stream {streamName}: tr. #{trId} {trType} -> commit {hash} on {ref} (end tr. {endTrId})".format(streamName=stream.name, trId=tr.id, trType=tr.Type, hash=commitHash[:8], ref=dataRef, endTrId=lastStateTrId) )

        return (tr, commitHash)

    # Retrieves all of the stream information from accurev, needed for later processing, and stores it in git using the \a dataRef and \a stateRef.
    # The retrieval and processing of the accurev information is separated in order to optimize processing of subsets of streams in a depot. For example,
    # if we have processed 7 streams in a depot and now wish to add an 8th we would have to start processing from the beginning because the merge points
    # between branches will now most likely need to be reconsidered. If the retrieval of information from accurev is a part of the processing step then we
    # have to redo a lot of the work that we have already done for the 7 streams. Instead we have the two steps decoupled so that all we need to do is
    # download the 8th stream information from accurev (which we don't yet have) and do the reprocessing by only looking for information already in git.
    def RetrieveStream(self, depot, stream, dataRef, stateRef, hwmRef, startTransaction, endTransaction):
        self.config.logger.info( "Retrieving stream {0} info from Accurev for transaction range : {1} - {2}".format(stream.name, startTransaction, endTransaction) )
        stateTr, stateHash = self.RetrieveStreamInfo(depot=depot, stream=stream, stateRef=stateRef, startTransaction=startTransaction, endTransaction=endTransaction)
        self.config.logger.info( "Retrieving stream {0} data from Accurev for transaction range : {1} - {2}".format(stream.name, startTransaction, endTransaction) )
        dataTr,  dataHash  = self.RetrieveStreamData(stream=stream, dataRef=dataRef, stateRef=stateRef)

        if stateTr is not None and dataTr is not None:
            if stateTr.id != dataTr.id:
                self.config.logger.error( "Missmatch while retrieving stream {streamName} (id: streamId), the data ref ({dataRef}) is on tr. {dataTr} while the state ref ({stateRef}) is on tr. {stateTr}.".format(streamName=stream.name, streamId=stream.streamNumber, dataTr=dataTr.id, stateTr=stateTr.id, dataRef=dataRef, stateRef=stateRef) )
            # Success! Update the high water mark for the stream.
            metadata = { "high-water-mark": int(endTransaction) }
            if self.WriteFileRef(ref=hwmRef, text=json.dumps(metadata)) != True:
                self.config.logger.error( "Failed to write the high-water-mark to ref {ref}".format(ref=hwmRef) )
            else:
                self.config.logger.info( "Updated the high-water-mark to ref {ref} as {trId}".format(ref=hwmRef, trId=endTransaction) )
        elif stateTr is not None and dataTr is None:
            self.config.logger.error( "Missmatch while retrieving stream {streamName} (id: streamId), the state ref ({stateRef}) is on tr. {stateTr} but the data ref ({dataRef}) wasn't retrieved.".format(streamName=stream.name, streamId=stream.streamNumber, stateTr=stateTr.id, dataRef=dataRef, stateRef=stateRef) )
        elif stateTr is None:
            self.config.logger.error( "While retrieving stream {streamName} (id: streamId), the state ref ({stateRef}) failed.".format(streamName=stream.name, streamId=stream.streamNumber, dataRef=dataRef, stateRef=stateRef) )

        return dataTr, dataHash

    def RetrieveStreams(self):
        if self.config.accurev.commandCacheFilename is not None:
            accurev.ext.enable_command_cache(self.config.accurev.commandCacheFilename)
        
        streamMap = self.GetStreamMap()

        depot  = self.config.accurev.depot
        endTrHist = accurev.hist(depot=depot, timeSpec=self.config.accurev.endTransaction)
        endTr = endTrHist.transactions[0]

        # Retrieve stream information from Accurev and store it inside git.
        for stream in streamMap:
            streamInfo = None
            try:
                streamInfo = accurev.show.streams(depot=depot, stream=stream, useCache=self.config.accurev.UseCommandCache()).streams[0]
            except IndexError:
                self.config.logger.error( "Failed to get stream information. `accurev show streams -p {0} -s {1}` returned no streams".format(depot, stream) )
                return
            except AttributeError:
                self.config.logger.error( "Failed to get stream information. `accurev show streams -p {0} -s {1}` returned None".format(depot, stream) )
                return

            if depot is None or len(depot) == 0:
                depot = streamInfo.depotName

            stateRef, dataRef, hwmRef  = self.GetStreamRefs(depot=depot, streamNumber=streamInfo.streamNumber)
            if stateRef is None or dataRef is None or len(stateRef) == 0 or len(dataRef) == 0:
                raise Exception("Invariant error! The state ({sr}) and data ({dr}) refs must not be None!".format(sr=stateRef, dr=dataRef))
            tr, commitHash = self.RetrieveStream(depot=depot, stream=streamInfo, dataRef=dataRef, stateRef=stateRef, hwmRef=hwmRef, startTransaction=self.config.accurev.startTransaction, endTransaction=endTr.id)

            if self.config.git.remoteMap is not None:
                refspec = "{dataRef}:{dataRef} {stateRef}:{stateRef}".format(dataRef=dataRef, stateRef=stateRef)
                for remoteName in self.config.git.remoteMap:
                    pushOutput = None
                    self.config.logger.info("Pushing '{refspec}' to '{remote}'...".format(remote=remoteName, refspec=refspec))
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

    # Lists the .git/... directory that contains all the stream refs and returns the file list as its result
    def GetAllKnownStreamRefs(self, depot):
        refsPrefix = self.GetStreamRefsNamespace(depot)
        if refsPrefix is not None:
            refsPath = '.git/{prefix}'.format(prefix=refsPrefix)

            repoRefsPath = os.path.join(self.gitRepo.path, refsPath)
            if os.path.exists(repoRefsPath):
                rv = []
                for r in os.listdir(repoRefsPath):
                    rv.append(refsPrefix + r)
                return rv
        return None

    # Tries to get the stream name from the data that we have stored in git.
    def GetStreamByName(self, depot, streamName):
        refsPrefix = self.GetStreamRefsNamespace(depot)

        refList = self.GetAllKnownStreamRefs(depot)
        if refList is None:
            refList = []
        # The stream with the lowest number is most likely to have a transaction with a streams.xml that contains
        # our stream name. Only if we are really unlucky will we have to search more than the lowest numbered stream.
        # So, parse and extract the number from the ..._info refs, and remove the ..._data refs.
        infoRefList = []
        for ref in refList:
            match = re.match(r'^.*stream_(\d+)_info$', ref)
            if match is not None:
                infoRefList.append( (int(match.group(1)), ref) ) # The stream number is extracted and put as the first element for sorting.
        infoRefList.sort()

        for streamNumber, ref in infoRefList:
            # Execute a `git log -S` command with the pickaxe option to find the stream name in the streams.xml
            cmd = [ 'git', 'log', '--format=%H', '-Sname="{n}"'.format(n=streamName), ref, '--', 'streams.xml' ]
            hashList = self.gitRepo.raw_cmd(cmd)
            if hashList is not None:
                hashList = hashList.strip()
                if len(hashList) != 0:
                    hashList = hashList.split()
                    # If there is more than one element then the stream has probably been renamed so we will take the earliest commit in which
                    # the stream name appears.
                    commitHash = hashList[-1]
                    streamsXml, streams = self.GetStreamsInfo(ref=commitHash)
                    for s in streams.streams:
                        if s.name == streamName:
                            return s
                    raise Exception("Invariant error! We successfully found that the hash {h} on ref {r} mentions the stream {sn} but couldn't match it?!".format(h=commitHash, r=ref, sn=streamName))
        return None

    def GetRefMap(self, ref, mapType, afterCommitHash=None):
        allowedMapTypes = [ "commit2tr", "tr2commit" ]
        if ref is None or mapType is None:
            raise Exception("None type arguments not allowed! ref: {ref}, mapType: {mapType}".format(ref=ref, mapType=mapType))
        elif mapType not in allowedMapTypes:
            raise Exception("mapType must be one of {types}".format(types=', '.join(allowedMapTypes)))

        cmd = [ 'git', 'log', '--pretty=oneline', ref ]
        if afterCommitHash is not None:
            cmd.append( '^{lastHash}'.format(lastHash=afterCommitHash) )
        cmdResult = self.gitRepo.raw_cmd(cmd)
        strList = None
        if cmdResult is not None:
            cmdResult = cmdResult.strip()
            if len(cmdResult) > 0:
                strList = cmdResult.split('\n')
            else:
                self.config.logger.dbg("GetRefMap(ref={ref}, mapType={t}) - command result is empty.".format(ref=ref, t=mapType))
                return None
        else:
            self.config.logger.dbg("GetRefMap(ref={ref}, mapType={t}) - command result was None.".format(ref=ref, t=mapType))
            return None
 
        refMap = OrderedDict()
        if strList is not None:
            for s in strList:
                columns = s.split(' ')
                if mapType == "commit2tr":
                    refMap[columns[0]] = int(columns[2])
                elif mapType == "tr2commit":
                    refMap[int(columns[2])] = columns[0]

        return refMap

    def GetStateForCommit(self, commitHash, notesRef):
        stateObj = None

        stateJson = None
        for i in range(0, AccuRev2Git.commandFailureRetryCount):
            stateJson = self.gitRepo.notes.show(obj=commitHash, ref=notesRef)
            if stateJson is not None:
                stateJson = stateJson.strip()
                if len(stateJson) > 0:
                    break
                else:
                    stateJson = None
            time.sleep(AccuRev2Git.commandFailureSleepSeconds)

        if stateJson is not None:
            stateJson = stateJson.strip()
            try:
                stateObj = json.loads(stateJson)
            except:
                self.config.logger.error("While getting state for commit {hash} (notes ref {ref}). Failed to parse JSON string [{json}].".format(hash=commitHash, ref=notesRef, json=stateJson))
                raise Exception("While getting state for commit {hash} (notes ref {ref}). Failed to parse JSON string [{json}].".format(hash=commitHash, ref=notesRef, json=stateJson))
        else:
            self.config.logger.error("Failed to load the last transaction for commit {hash} from {ref} notes.".format(hash=commitHash, ref=notesRef))
            self.config.logger.error("  i.e git notes --ref={ref} show {hash}    - returned nothing.".format(ref=notesRef, hash=commitHash))

        return stateObj


    def AddNote(self, transaction, commitHash, ref, note, committerName=None, committerEmail=None, committerDate=None, committerTimezone=None):
        notesFilePath = None
        if note is not None:
            with tempfile.NamedTemporaryFile(mode='w+', prefix='ac2git_note_', delete=False) as notesFile:
                notesFilePath = notesFile.name
                notesFile.write(note)

        # Get the author's and committer's name, email and timezone information.
        if transaction is not None:
            committerName, committerEmail = self.GetGitUserFromAccuRevUser(transaction.user)
            committerDate, committerTimezone = self.GetGitDatetime(accurevUsername=transaction.user, accurevDatetime=transaction.time)

        if notesFilePath is not None:
            rv = self.gitRepo.notes.add(messageFile=notesFilePath, obj=commitHash, ref=ref, force=True, committerName=committerName, committerEmail=committerEmail, committerDate=committerDate, committerTimezone=committerTimezone, authorName=committerName, authorEmail=committerEmail, authorDate=committerDate, authorTimezone=committerTimezone)
            os.remove(notesFilePath)

            if rv is not None:
                self.config.logger.dbg( "Added{ref} note for {hash}.".format(ref='' if ref is None else ' '+str(ref), hash=commitHash) )
            else:
                self.config.logger.error( "Failed to add{ref} note for {hash}{trStr}".format(ref='' if ref is None else ' '+str(ref), hash=commitHash, trStr='' if transaction is None else ', tr. ' + str(transaction.id)) )
                self.config.logger.error(self.gitRepo.lastStderr)
            
            return rv
        else:
            self.config.logger.error( "Failed to create temporary file for script state note for {0}, tr. {1}".format(commitHash, transaction.id) )
        
        return None

    # Adds a JSON string respresentation of `stateDict` to the given commit using `git notes add`.
    def AddScriptStateNote(self, depotName, stream, transaction, commitHash, ref, dstStream=None, srcStream=None):
        stateDict = { "depot": depotName, "stream": stream.name, "stream_number": stream.streamNumber, "transaction_number": transaction.id, "transaction_kind": transaction.Type }
        if dstStream is not None:
            stateDict["dst_stream"]        = dstStream.name
            stateDict["dst_stream_number"] = dstStream.streamNumber
        if srcStream is not None:
            stateDict["src_stream"]        = srcStream.name
            stateDict["src_stream_number"] = srcStream.streamNumber

        return self.AddNote(transaction=transaction, commitHash=commitHash, ref=ref, note=json.dumps(stateDict))

    def ProcessStream(self, stream, branchName):
        if stream is not None:
            stateRef, dataRef, hwmRef = self.GetStreamRefs(depot=stream.depotName, streamNumber=stream.streamNumber)
            if stateRef is None or dataRef is None or len(stateRef) == 0 or len(dataRef) == 0:
                raise Exception("Invariant error! The state ({sr}) and data ({dr}) refs must not be None!".format(sr=stateRef, dr=dataRef))

            if branchName is None:
                branchName = self.SanitizeBranchName(stream.name)
            else:
                branchName = self.SanitizeBranchName(branchName)

            branchList = self.gitRepo.branch_list()
            if branchList is None:
                return None

            commitHash = None
            lastDataCommitHash = None
            if branchName in [ br.name if br is not None else None for br in branchList ]:
                self.SafeCheckout(ref=branchName, doReset=True, doClean=True)
                commitHash = self.GetLastCommitHash(branchName=branchName)
                commitState = self.GetStateForCommit(commitHash=commitHash, notesRef=AccuRev2Git.gitNotesRef_state)

                # If we have failed to retrieve the state then auto-recover the last known good state.
                lastGoodCommitHash = commitHash
                badCount = 0
                while commitState is None:
                    # See if the parent commit has anything better?
                    parentHash = self.gitRepo.raw_cmd(['git', 'log', '--format=%P', lastGoodCommitHash, '-1'])
                    if parentHash is None or len(parentHash.strip()) == 0 or len(parentHash.strip().split(' ')) != 1:
                        # We can't go beyond merges or commits without parents.
                        lastGoodCommitHash = None
                        break
                    lastGoodCommitHash = parentHash.strip() # Follow first parent only.
                    commitState = self.GetStateForCommit(commitHash=lastGoodCommitHash, notesRef=AccuRev2Git.gitNotesRef_state)
                    badCount += 1

                if lastGoodCommitHash is None:
                    self.config.logger.error("Couldn't find commit state information in the {notesRef} notes for the commit. Aborting!".format(notesRef=AccuRev2Git.gitNotesRef_state))
                    return None
                elif lastGoodCommitHash != commitHash:
                    self.config.logger.info("The last commit for which state information was found was {gh}, which means that {count} commits are bad.".format(gh=lastGoodCommitHash, count=badCount))
                    linesToDelete = self.gitRepo.raw_cmd(['git', 'log', '--format=%h %s', commitHash, '^{h}'.format(h=lastGoodCommitHash)])
                    self.config.logger.info("The following commits are being discarded:\n{lines}".format(lines=linesToDelete))
                    if self.gitRepo.raw_cmd(['git', 'reset', '--hard', lastGoodCommitHash]) is None:
                        self.config.logger.error("Failed to restore last known good state, aborting!")
                        return None

                # Here we know that the state must exist and be good!
                lastTrId = commitState["transaction_number"]
                # Find the commit hash on our dataRef that corresponds to our last transaction number.
                lastDataCommitHash = self.GetHashForTransaction(ref=dataRef, trNum=lastTrId)
                if lastDataCommitHash is None:
                    return None
            else:
                # TODO: Implement the fractal method option here! i.e. Create the branch and root it to its parent.
                self.config.logger.info( "Creating orphan branch {0}".format(branchName) )
                self.gitRepo.checkout(branchName=branchName, isOrphan=True)

            # Get the list of new hashes that have been committed to the dataRef but we haven't processed on the dataRef just yet.
            dataHashList = self.GetGitLogList(ref=dataRef, afterCommitHash=lastDataCommitHash, gitLogFormat='%H %s %T')
            if dataHashList is None:
                raise Exception("Couldn't get the commit hash list to process from the Accurev data ref {dataRef}.".format(dataRef=dataRef))
            elif len(dataHashList) == 0:
                self.config.logger.error( "{b} is upto date. Couldn't load any more transactions after tr. ({trId}).".format(trId=lastTrId, b=branchName) )

                return self.GetLastCommitHash(branchName=branchName)

            # Get the stateRef map of transaction numbers to commit hashes.
            stateMap = self.GetRefMap(ref=stateRef, mapType="tr2commit")
            if stateMap is None:
                raise Exception("Invariant error! If the dataMap is not None then neither should the stateMap be!")

            # Commit the new data with the correct commit messages.
            for line in reversed(dataHashList):
                columns = line.split(' ')
                trId, treeHash = int(columns[2]), columns[3]
                # Get the transaction info.
                stateHash = stateMap[trId]
                if stateHash is None:
                    raise Exception("Failed to retrieve state information for tr. {trId}".format(trId))
                trHistXml, trHist = self.GetHistInfo(ref=stateHash)
                tr = trHist.transactions[0]

                streamsXml, streams = self.GetStreamsInfo(ref=stateHash)
                dstStreamName, dstStreamNumber = trHist.toStream()
                dstStream = streams.getStream(dstStreamNumber)
                srcStreamName, srcStreamNumber = trHist.fromStream()
                srcStream = streams.getStream(srcStreamNumber)

                commitMessage, notes = self.GenerateCommitMessage(transaction=tr, stream=stream, dstStream=dstStream, srcStream=srcStream)
                parents = []
                if commitHash is not None:
                    parents = [ commitHash ]
                commitHash = self.Commit(transaction=tr, allowEmptyCommit=True, messageOverride=commitMessage, treeHash=treeHash, parents=parents)
                if commitHash is None:
                    self.config.logger.error("Failed to commit transaction {trId} to {br}.".format(trId=tr.id, br=branchName))
                    return None
                if self.AddScriptStateNote(depotName=stream.depotName, stream=stream, transaction=tr, commitHash=commitHash, ref=AccuRev2Git.gitNotesRef_state, dstStream=dstStream, srcStream=srcStream) is None:
                    self.config.logger.error("Failed to add note for commit {h} (transaction {trId}) to {br}.".format(trId=tr.id, br=branchName, h=commitHash))
                    return None
                if notes is not None and self.AddNote(transaction=tr, commitHash=commitHash, ref=AccuRev2Git.gitNotesRef_accurevInfo, note=notes) is None:
                    self.config.logger.error("Failed to add note for commit {h} (transaction {trId}) to {br}.".format(trId=tr.id, br=branchName, h=commitHash))
                    return None

                self.config.logger.info("Committed transaction {trId} to {br}. Commit {h}".format(trId=tr.id, br=branchName, h=commitHash))

            return True
        return None

    def ProcessStreams(self, orderByStreamNumber=False):
        depot  = self.config.accurev.depot

        # Get the stream information for the configured streams from accurev (this is because stream names can change and accurev doesn't care about this while we do).
        processingList = []
        for stream in self.config.accurev.streamMap:
            streamInfo = self.GetStreamByName(depot=depot, streamName=stream)
            if depot is None or len(depot) == 0:
                depot = streamInfo.depotName
            elif depot != streamInfo.depotName:
                self.config.logger.info("Stream {name} (id: {id}) is in depot {streamDepot} which is different than the configured depot {depot}. Ignoring...".format(name=streamInfo.name, id=streamInfo.streamNumber, streamDepot=streamInfo.depotName, depot=depot))

            processingList.append( (streamInfo.streamNumber, streamInfo, self.config.accurev.streamMap[stream]) )

        if orderByStreamNumber:
            processingList.sort()

        for streamNumber, stream, branchName in processingList:
            self.ProcessStream(stream=stream, branchName=branchName)

            if self.config.git.remoteMap is not None:
                formatOptions = { "accurevNotes": AccuRev2Git.gitNotesRef_accurevInfo, "ac2gitNotes": AccuRev2Git.gitNotesRef_state, "branchName": branchName }
                refspec = "{branchName}".format(**formatOptions)
                if self.gitRepo.raw_cmd(['git', 'show-ref', '--hash', 'refs/notes/{accurevNotes}'.format(**formatOptions)]) is not None:
                    refspec += " refs/notes/{accurevNotes}:refs/notes/{accurevNotes}".format(**formatOptions)
                if self.gitRepo.raw_cmd(['git', 'show-ref', '--hash', 'refs/notes/{ac2gitNotes}'.format(**formatOptions)]) is not None:
                    refspec += " refs/notes/{ac2gitNotes}:refs/notes/{ac2gitNotes}".format(**formatOptions)
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
        
    def AppendCommitMessageSuffixStreamInfo(self, suffixList, linePrefix, stream):
        if stream is not None:
            suffixList.append( ('{linePrefix}:'.format(linePrefix=linePrefix), '{name} (id: {id}; type: {Type})'.format(id=stream.streamNumber, name=stream.name, Type=stream.Type)) )
            if stream.prevName is not None:
                suffixList.append( ('{linePrefix}-prev-name:'.format(linePrefix=linePrefix), '{name}'.format(name=stream.prevName)) )
            if stream.basis is not None:
                suffixList.append( ('{linePrefix}-basis:'.format(linePrefix=linePrefix), '{name} (id: {id})'.format(name=stream.basis, id=stream.basisStreamNumber)) )
            if stream.prevBasis is not None and len(stream.prevBasis) > 0:
                suffixList.append( ('{linePrefix}-prev-basis:'.format(linePrefix=linePrefix), '{name} (id: {id})'.format(name=stream.prevBasis, id=stream.prevBasisStreamNumber)) )
            if stream.time is not None:
                suffixList.append( ('{linePrefix}-timelock:'.format(linePrefix=linePrefix), '{time} (UTC)'.format(time=stream.time)) )
            if stream.prevTime is not None:
                suffixList.append( ('{linePrefix}-prev-timelock:'.format(linePrefix=linePrefix), '{prevTime} (UTC)'.format(time=stream.prevTime)) )

    def GenerateCommitMessageSuffix(self, transaction, stream=None, dstStream=None, srcStream=None):
        suffixList = []
        suffixList.append( ('Accurev-transaction:', '{id} (type: {Type})'.format(id=transaction.id, Type=transaction.Type)) )
        if stream is not None:
            self.AppendCommitMessageSuffixStreamInfo(suffixList=suffixList, linePrefix='Accurev-stream', stream=stream)
        if dstStream is not None:
            self.AppendCommitMessageSuffixStreamInfo(suffixList=suffixList, linePrefix='Accurev-dst-stream', stream=dstStream)
        if srcStream is not None:
            self.AppendCommitMessageSuffixStreamInfo(suffixList=suffixList, linePrefix='Accurev-src-stream', stream=srcStream)
        
        # Ensure that all the items are nicely column aligned by padding the titles with spaces after the colon.
        longestSuffixTitle = 0
        for suffix in suffixList:
            if longestSuffixTitle < len(suffix[0]):
                longestSuffixTitle = len(suffix[0])
        suffixFormat = '{suffix: <' + str(longestSuffixTitle) + '} {info}'
        lineList = []
        for suffix in suffixList:
            lineList.append(suffixFormat.format(suffix=suffix[0], info=suffix[1]))
            
        return '\n'.join(lineList)

    def GenerateCommitMessage(self, transaction, stream=None, dstStream=None, srcStream=None, title=None, friendlyMessage=None):
        messageSections = []
        
        style = "normal"
        if self.config.git.messageStyle is not None:
            style = self.config.git.messageStyle.lower()

        if style == "clean":
            return (transaction.comment, None)
        elif style in [ "normal", "notes" ]:
            if title is not None:
                messageSections.append(title)
            if transaction.comment is not None:
                messageSections.append(transaction.comment)
            if friendlyMessage is not None:
                messageSections.append(friendlyMessage)
            
            notes = None
            suffix = self.GenerateCommitMessageSuffix(transaction=transaction, stream=stream, dstStream=dstStream, srcStream=srcStream)
            if suffix is not None:
                if style == "normal":
                    messageSections.append(suffix)
                elif style == "notes":
                    notes = suffix

            return ('\n\n'.join(messageSections), notes)

        raise Exception("Unrecognized git message style '{s}'".format(s=style))

    def SanitizeBranchName(self, name):
        name = name.replace(' ', '_').strip()
        return name

    def GetBranchNameFromStreamOrMap(self, streamName, streamNumber, streamNumberMap):
        if streamNumberMap is None or len(streamNumberMap) == 0:
            return self.SanitizeBranchName(streamName)
        else:
            return streamNumberMap.get(streamNumber)
        raise Exception("How did you hit this exception? We should have returned!")

    def GetBranchNameFromStream(self, stream, streamNumberMap=None):
        return self.GetBranchNameFromStreamOrMap(streamName=stream.name, streamNumber=stream.streamNumber, streamNumberMap=streamNumberMap)

    def GetBranchNameFromStreamBasis(self, stream, streamNumberMap=None):
        return self.GetBranchNameFromStreamOrMap(streamName=stream.basis, streamNumber=stream.basisStreamNumber, streamNumberMap=streamNumberMap)

    def GetBranchNameFromStreamPrevBasis(self, stream, streamNumberMap=None):
        return self.GetBranchNameFromStreamOrMap(streamName=stream.prevBasis, streamNumber=stream.prevBasisStreamNumber, streamNumberMap=streamNumberMap)

    def GitCommitOrMerge(self, depot, dstStream, srcStream, tr, commitMessageOverride=None, mergeMessageOverride=None, streamNumberMap=None):
        # Perform the git merge of the 'from stream' into the 'to stream' but only if they have the same contents.
        dstBranchName = self.GetBranchNameFromStream(dstStream, streamNumberMap)
        srcBranchName = self.GetBranchNameFromStream(srcStream, streamNumberMap)
        if dstBranchName is None:
            return None

        if self.gitRepo.checkout(branchName=dstBranchName) is None:
            raise Exception("git checkout branch {br}, failed!".format(br=dstBranchName))
        
        diff = self.TryDiff(streamName=dstStream.name, firstTrNumber=(tr.id - 1), secondTrNumber=tr.id)
        deletedPathList = self.DeleteDiffItemsFromRepo(diff=diff)
        popResult = self.TryPop(streamName=dstStream.name, transaction=tr)

        commitHash = self.Commit(depot=depot, stream=dstStream, transaction=tr, branchName=dstBranchName, allowEmptyCommit=True, noNotes=True, messageOverride=commitMessageOverride, dstStream=dstStream, srcStream=srcStream)
        if commitHash is None:
            raise Exception("Failed to commit promote {tr}!".format(tr=tr.id))
        diff = None
        if streamNumberMap is None or srcStream.streamNumber in streamNumberMap:
            if srcBranchName is None:
                raise Exception("Invariant error! Source branch should exist but was {b}. Stream {s}".format(b=srcBranchName, s=srcStream.name))
            diff = self.gitRepo.raw_cmd([u'git', u'diff', u'--stat', dstBranchName, srcBranchName, u'--' ])
            if diff is None:
                raise Exception("Failed to diff new branch {nBr} to old branch {oBr}! Err: {err}".format(nBr=dstBranchName, oBr=srcBranchName, err=self.gitRepo.lastStderr))
        
        if diff is not None and len(diff.strip()) == 0:
            # Merge
            if self.gitRepo.reset(branch="HEAD^") is None:
                raise Exception("Failed to undo commit! git reset HEAD^, failed with: {err}".format(err=self.gitRepo.lastStderr))
            if self.gitRepo.raw_cmd([u'git', u'merge', u'--no-ff', u'--no-commit', u'-s', u'ours', srcBranchName]) is None:
                raise Exception("Failed to merge! Failed with: {err}".format(err=self.gitRepo.lastStderr))
            
            diff = self.TryDiff(streamName=dstStream.name, firstTrNumber=(tr.id - 1), secondTrNumber=tr.id)
            deletedPathList = self.DeleteDiffItemsFromRepo(diff=diff)
            popResult = self.TryPop(streamName=dstStream.name, transaction=tr)

            commitHash = self.Commit(depot=depot, stream=dstStream, transaction=tr, branchName=dstBranchName, allowEmptyCommit=True, noNotes=True, messageOverride=mergeMessageOverride, dstStream=dstStream, srcStream=srcStream)
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

    def ProcessTransaction(self, depot, transaction, streamNumberMap):
        trHist = self.TryHist(depot=depot, trNum=transaction)
        if trHist is None or len(trHist.transactions) == 0 is None:
            raise Exception("Couldn't get history for transaction {tr}. Aborting!".format(tr=transaction))
        
        tr = trHist.transactions[0]
        self.config.logger.dbg( "Transaction #{tr} - {Type} by {user} to {stream} at {time}".format(tr=tr.id, Type=tr.Type, time=tr.time, user=tr.user, stream=tr.toStream()[0]) )
        
        if tr.Type == "mkstream":
            # Old versions of accurev don't tell you the name of the stream that was created in the mkstream transaction.
            # The only way to find out what stream was created is to diff the output of the `accurev show streams` command
            # between the mkstream transaction and the one that preceedes it.
            streams = accurev.show.streams(depot=depot, timeSpec=transaction, useCache=self.config.accurev.UseCommandCache())
            newStream = None
            if transaction == 1:
                newStream = streams.streams[0]
            else:
                streamSet = set()
                oldStreams = accurev.show.streams(depot=depot, timeSpec=(transaction - 1), useCache=self.config.accurev.UseCommandCache())
                for stream in oldStreams.streams:
                    streamSet.add(stream.name)
                for stream in streams.streams:
                    if stream.name not in streamSet:
                        newStream = stream
                        break
                basisBranchName = self.GetBranchNameFromStreamBasis(newStream, streamNumberMap)
                if basisBranchName is not None and self.gitRepo.checkout(branchName=basisBranchName) is None:
                    raise Exception("Failed to checkout basis stream branch {bsBr} for stream {s}".format(bsBr=basisBranchName, s=newStream.name))
            
            newBranchName = self.GetBranchNameFromStream(newStream, streamNumberMap)
            if newBranchName is not None:
                if self.gitRepo.checkout(branchName=newBranchName, isNewBranch=True) is None:
                    raise Exception("Failed to create new branch {br}. Error: {err}".format(br=newBranchName, err=self.gitRepo.lastStderr))
                self.config.logger.info("mkstream name={name}, number={num}, basis={basis}, basis-number={basisNumber}".format(name=newStream.name, num=newStream.streamNumber, basis=newStream.basis, basisNumber=newStream.basisStreamNumber))
                # Modify the commit message
                commitMessage = self.GenerateCommitMessage(transaction=tr, stream=newStream, title="Created {name} based on {basis}".format(name=newBranchName, basis='-' if newStream.basis is None else basisBranchName))
                commitHash = self.Commit(depot=depot, stream=newStream, transaction=tr, branchName=newBranchName, isFirstCommit=True, allowEmptyCommit=True, noNotes=True, messageOverride=commitMessage)
                if commitHash is None:
                    raise Exception("Failed to add empty mkstream commit")
        
        elif tr.Type == "chstream":
            #streamName, streamNumber = tr.affectedStream()
            #stream = accurev.show.streams(depot=depot, stream=streamNumber, timeSpec=tr.id, useCache=self.config.accurev.UseCommandCache()).streams[0]
            
            branchName = self.GetBranchNameFromStream(tr.stream, streamNumberMap)
            if branchName is not None:
                if streamNumberMap is None and tr.stream.prevName is not None and len(tr.stream.prevName.strip()) > 0:
                    # if the stream has been renamed, use its new name from now on.
                    prevBranchName = self.SanitizeBranchName(tr.stream.prevName)
                    if self.gitRepo.raw_cmd([ u'git', u'branch', u'-m', prevBranchName, branchName ]) is None:
                        raise Exception("Failed to rename branch {old} to {new}. Err: {err}".format(old=prevBranchName, new=branchName, err=self.gitRepo.lastStderr))
                    self.config.logger.info("Renamed branch {oldName} to {newName}".format(oldName=prevBranchName, newName=branchName))
                    
                if self.gitRepo.checkout(branchName=branchName) is None:
                    raise Exception("Failed to checkout branch {br}".format(br=branchName))

                if tr.stream.prevBasis is not None and len(tr.stream.prevBasis) > 0:
                    # We need to change where our stream is parented, i.e. rebase it...
                    basisBranchName = self.GetBranchNameFromStreamBasis(tr.stream, streamNumberMap)
                    prevBasisBranchName = self.GetBranchNameFromStreamPrevBasis(tr.stream, streamNumberMap)

                    if basisBranchName is not None:
                        # If the prevBasisBranchName is None, this would mean that we haven't been tracking that stream and that this stream is potentially not rooted/merged
                        # anywhere (i.e. it is an orphaned branch). If we just reset the branch we will likely lose all the history up until this point. But do we care?
                        # We could drop a tag at the current point to preserve the history but is it necessary? All the relevant changes will appear on the new branch anyway,
                        # since we are not doing a rebase. Which conveniently mentions the other option, rebase, which could be used here.
                        newBasisCommitHash = self.GetLastCommitHash(branchName=basisBranchName)
                        if newBasisCommitHash is None:
                            raise Exception("Failed to retrieve the last commit hash for new basis stream branch {bs}".format(bs=basisBranchName))
                        if self.gitRepo.raw_cmd([ u'git', u'reset', u'--hard', newBasisCommitHash ]) is None:
                            raise Exception("Failed to rebase branch {br} from {old} to {new}. Err: {err}".format(br=branchName, old=prevBasisBranchName, new=basisBranchName, err=self.gitRepo.lastStderr))
                        self.config.logger.info("Rebased branch {name} from {oldBasis} to {newBasis}".format(name=branchName, oldBasis=prevBasisBranchName, newBasis=basisBranchName))
                    
                diff = self.TryDiff(streamName=tr.stream.name, firstTrNumber=(tr.id - 1), secondTrNumber=tr.id)
                deletedPathList = self.DeleteDiffItemsFromRepo(diff=diff)
                popResult = self.TryPop(streamName=tr.stream.name, transaction=tr)

                commitMessage = self.GenerateCommitMessage(transaction=tr, stream=tr.stream)
                commitHash = self.Commit(depot=depot, stream=tr.stream, transaction=tr, branchName=branchName, allowEmptyCommit=True, noNotes=True, messageOverride=commitMessage)
                if commitHash is None:
                    raise Exception("Failed to add chstream commit")

        elif tr.Type == "add":
            streamName, streamNumber = tr.affectedStream()
            stream = accurev.show.streams(depot=depot, stream=streamNumber, timeSpec=tr.id, useCache=self.config.accurev.UseCommandCache()).streams[0]
            branchName = self.GetBranchNameFromStream(stream, streamNumberMap)
            if branchName is not None:
                if self.gitRepo.checkout(branchName=branchName) is None:
                    raise Exception("Failed to checkout branch {br}!".format(br=branchName))
                elif stream.Type != "workspace":
                    raise Exception("Invariant error! Assumed that a {Type} transaction can only occur on a workspace. Stream {name}, type {streamType}".format(Type=tr.type, name=stream.name, streamType=stream.Type))
                # The add command only introduces new files so we can safely use only `accurev pop` to get the changes.
                self.TryPop(streamName=stream.name, transaction=tr)
                
                commitMessage = self.GenerateCommitMessage(transaction=tr, stream=stream)
                self.Commit(depot=depot, stream=stream, transaction=tr, branchName=branchName, noNotes=True, messageOverride=commitMessage)
            
        elif tr.Type in [ "keep", "co", "move" ]:
            streamName, streamNumber = tr.affectedStream()
            stream = accurev.show.streams(depot=depot, stream=streamNumber, timeSpec=tr.id, useCache=self.config.accurev.UseCommandCache()).streams[0]
            branchName = self.GetBranchNameFromStream(stream, streamNumberMap)
            if branchName is not None:
                if self.gitRepo.checkout(branchName=branchName) is None:
                    raise Exception("Failed to checkout branch {br}!".format(br=branchName))
                if stream.Type != "workspace":
                    self.config.logger.info("Note: {trType} transaction {id} on stream {stream} ({streamType}). Merging down-stream. Usually {trType}s occur on workspaces!".format(trType=tr.Type, id=tr.id, stream=stream.name, streamType=stream.Type))
                
                diff = self.TryDiff(streamName=stream.name, firstTrNumber=(tr.id - 1), secondTrNumber=tr.id)
                deletedPathList = self.DeleteDiffItemsFromRepo(diff=diff)
                popResult = self.TryPop(streamName=stream.name, transaction=tr)

                commitMessage = self.GenerateCommitMessage(transaction=tr, stream=stream)
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
            if dstStreamNumber is not None:
                dstStream = accurev.show.streams(depot=depot, stream=dstStreamNumber, timeSpec=tr.id, useCache=self.config.accurev.UseCommandCache())
            elif dstStreamName is not None:
                dstStream = accurev.show.streams(depot=depot, stream=dstStreamName, timeSpec=tr.id, useCache=self.config.accurev.UseCommandCache())
            else:
                raise Exception("Error! Could not determine the destination stream for promote {tr}.".format(tr=tr.id))

            if dstStream is None or dstStream.streams is None or len(dstStream.streams) == 0:
                raise Exception("Error! accurev show streams -p {d} -s {s} -t {t} failed!".format(d=depot, s=dstStreamName, t=tr.id))
            dstStream = dstStream.streams[0]
            dstBranchName = self.GetBranchNameFromStream(dstStream, streamNumberMap)

            # Determine the stream from which the files in this this transaction were promoted.
            srcStreamName, srcStreamNumber = trHist.fromStream()
            srcStream = None

            if srcStreamName is None and srcStreamNumber is None:
                # We have failed to determine the stream from which this transaction came. Hence we now must treat this as a cherry-pick instead of a merge...
                self.config.logger.error("Error! Could not determine the source stream for promote {tr}. Treating as a cherry-pick.".format(tr=tr.id))

                if self.gitRepo.checkout(branchName=dstBranchName) is None:
                    raise Exception("Failed to checkout branch {br}!".format(br=dstBranchName))

                diff = self.TryDiff(streamName=dstStream.name, firstTrNumber=(tr.id - 1), secondTrNumber=tr.id)
                deletedPathList = self.DeleteDiffItemsFromRepo(diff=diff)
                popResult = self.TryPop(streamName=dstStream.name, transaction=tr)

                commitMessage = self.GenerateCommitMessage(transaction=tr, stream=dstStream)
                commitHash = self.Commit(depot=depot, stream=dstStream, transaction=tr, branchName=dstBranchName, allowEmptyCommit=True, noNotes=True, messageOverride=commitMessage, dstStream=dstStream, srcStream=srcStream)
                if commitHash is None:
                    raise Exception("Failed to commit a `{Type}`! tr={tr}".format(Type=tr.Type, tr=tr.id))
            else:
                # The source stream is almost always a workspace in which the transaction was generated. This is not ideal, but it is the best we can get.
                if srcStreamNumber is not None:
                    srcStream = accurev.show.streams(depot=depot, stream=srcStreamNumber, timeSpec=tr.id, useCache=self.config.accurev.UseCommandCache())
                elif srcStreamName is not None:
                    srcStream = accurev.show.streams(depot=depot, stream=srcStreamName, timeSpec=tr.id, useCache=self.config.accurev.UseCommandCache())
                if srcStream is None or srcStream.streams is None or len(srcStream.streams) == 0:
                    raise Exception("Error! accurev show streams -p {d} -s {s} -t {t} failed!".format(d=depot, s=srcStreamName if srcStreamName is not None else srcStreamNumber, t=tr.id))
                srcStream = srcStream.streams[0]
                srcBranchName = self.GetBranchNameFromStream(srcStream, streamNumberMap)

                # Perform the git merge of the 'from stream' into the 'to stream' but only if they have the same contents.
                mergeCommitMessage = self.GenerateCommitMessage(transaction=tr, stream=dstStream, srcStream=srcStream, friendlyMessage="Merged {src} into {dst} - accurev promote.".format(src=srcBranchName, dst=dstBranchName))
                cherryPickCommitMessage = self.GenerateCommitMessage(transaction=tr, stream=dstStream, srcStream=srcStream, friendlyMessage="Cherry-picked {src} into {dst} - accurev promote.".format(src=srcBranchName, dst=dstBranchName))
                self.GitCommitOrMerge(depot=depot, dstStream=dstStream, srcStream=srcStream, tr=tr, commitMessageOverride=cherryPickCommitMessage, mergeMessageOverride=mergeCommitMessage, streamNumberMap=streamNumberMap)
            
            affectedStreams = accurev.ext.affected_streams(depot=depot, transaction=tr.id, includeWorkspaces=True, ignoreTimelocks=False, doDiffs=True, useCache=self.config.accurev.UseCommandCache())
            for stream in affectedStreams:
                branchName = self.GetBranchNameFromStream(stream, streamNumberMap)
                if stream.streamNumber != dstStream.streamNumber and (srcStream is None or stream.streamNumber != srcStream.streamNumber) and (streamNumberMap is None or stream.streamNumber in streamNumberMap):
                    mergeCommitMessage = self.GenerateCommitMessage(transaction=tr, stream=stream, dstStream=dstStream, srcStream=srcStream, friendlyMessage="Merged {src} into {dst} - accurev parent stream inheritance.".format(src=dstBranchName, dst=branchName))
                    cherryPickCommitMessage = self.GenerateCommitMessage(transaction=tr, stream=stream, dstStream=dstStream, srcStream=srcStream, friendlyMessage="Cherry-picked {src} into {dst} - accurev parent stream inheritance.".format(src=dstBranchName, dst=branchName))
                    self.GitCommitOrMerge(depot=depot, dstStream=stream, srcStream=dstStream, tr=tr, commitMessageOverride=cherryPickCommitMessage, mergeMessageOverride=mergeCommitMessage, streamNumberMap=streamNumberMap)

        elif tr.Type in [ "defunct", "purge" ]:
            streamName, streamNumber = tr.affectedStream()
            stream = accurev.show.streams(depot=depot, stream=streamNumber, timeSpec=tr.id, useCache=self.config.accurev.UseCommandCache()).streams[0]
            branchName = self.GetBranchNameFromStream(stream, streamNumberMap)
            if branchName is not None:
                if self.gitRepo.checkout(branchName=branchName) is None:
                    raise Exception("Failed to checkout branch {br}!".format(br=branchName))
                
                diff = self.TryDiff(streamName=stream.name, firstTrNumber=(tr.id - 1), secondTrNumber=tr.id)
                deletedPathList = self.DeleteDiffItemsFromRepo(diff=diff)
                popResult = self.TryPop(streamName=stream.name, transaction=tr)

                commitMessage = self.GenerateCommitMessage(transaction=tr, stream=stream)
                commitHash = self.Commit(depot=depot, stream=stream, transaction=tr, branchName=branchName, allowEmptyCommit=True, noNotes=True, messageOverride=commitMessage)
                if commitHash is None:
                    raise Exception("Failed to commit a `{Type}`! tr={tr}".format(Type=tr.Type, tr=tr.id))
                
                if stream.Type != "workspace":
                    self.config.logger.info("Note: {trType} transaction {id} on stream {stream} ({streamType}). Merging down-stream. Usually {trType}s occur on workspaces!".format(trType=tr.Type, id=tr.id, stream=stream.name, streamType=stream.Type))
                    affectedStreams = accurev.ext.affected_streams(depot=depot, transaction=tr.id, includeWorkspaces=True, ignoreTimelocks=False, doDiffs=True, useCache=self.config.accurev.UseCommandCache())
                    for s in affectedStreams:
                        if s.streamNumber != stream.streamNumber and (streamNumberMap is None or stream.streamNumber in streamNumberMap):
                            bName = self.GetBranchNameFromStream(s, streamNumberMap)
                            mergeCommitMessage = self.GenerateCommitMessage(transaction=tr, stream=s, dstStream=stream, friendlyMessage="Merged {src} into {dst} - accurev parent stream inheritance ({trType}).".format(src=branchName, dst=bName, trType=tr.Type))
                            cherryPickCommitMessage = self.GenerateCommitMessage(transaction=tr, stream=s, dstStream=stream, friendlyMessage="Merged {src} into {dst} - accurev parent stream inheritance ({trType}).".format(src=branchName, dst=bName, trType=tr.Type))
                            self.GitCommitOrMerge(depot=depot, dstStream=s, srcStream=stream, tr=tr, commitMessageOverride=cherryPickCommitMessage, mergeMessageOverride=commitMessage, streamNumberMap=streamNumberMap)
            
        elif tr.Type == "defcomp":
            self.config.logger.info("Ignoring transaction #{id} - {Type}".format(id=tr.id, Type=tr.Type))

        else:
            message = "Not yet implemented! Unrecognized transaction type {type}".format(type=tr.Type)
            self.config.logger.info(message)
            raise Exception(message)

    def InitialCommitStreams(self, depot, streams, stream=None, tr=None, streamNumberMap=None):
        if stream is None:
            for s in streams:
                if s.basisStreamNumber is None:
                    return self.InitialCommitStreams(depot=depot, stream=s, streams=streams, tr=tr, streamNumberMap=streamNumberMap)
        else:
            # Checkout basis stream (if any).
            basisBranchName = None
            if stream.basisStreamNumber is not None:
                basisBranchName = self.GetBranchNameFromStreamBasis(stream, streamNumberMap)
                if basisBranchName is not None and self.gitRepo.checkout(branchName=basisBranchName) is None:
                    raise Exception("Failed to checkout basis stream branch {bsBr} for stream {s}".format(bsBr=basisBranchName, s=stream.name))
            
            # Create branch whith a single empty commit.
            branchName = self.GetBranchNameFromStream(stream, streamNumberMap)
            if branchName is not None:
                if basisBranchName != branchName and self.gitRepo.checkout(branchName=branchName, isNewBranch=True) is None:
                    raise Exception("Failed to create new branch {br}. Error: {err}".format(br=branchName, err=self.gitRepo.lastStderr))
                self.config.logger.info("Initial commit name={name}, number={num}, basis={basis}, basis-number={basisNumber}".format(name=stream.name, num=stream.streamNumber, basis=stream.basis, basisNumber=stream.basisStreamNumber))
                # Modify the commit message
                commitMessage = self.GenerateCommitMessage(transaction=tr, stream=stream, title="Created {name} based on {basis}".format(name=branchName, basis='-' if stream.basis is None else basisBranchName))
                commitHash = self.Commit(depot=depot, stream=stream, transaction=tr, branchName=branchName, isFirstCommit=True, allowEmptyCommit=True, noNotes=True, messageOverride=commitMessage)
                if commitHash is None:
                    raise Exception("Failed to add empty commit")

            # Create all child branches.
            for s in streams:
                if s.basisStreamNumber == stream.streamNumber:
                    self.InitialCommitStreams(depot=depot, stream=s, streams=streams, tr=tr, streamNumberMap=streamNumberMap)
                
        return False

    def ReadFileRef(self, ref):
        rv = None
        for i in range(0, AccuRev2Git.commandFailureRetryCount):
            rv = self.gitRepo.raw_cmd([u'git', u'show', ref])
            if rv is None:
                return None # Non-zero return code means that the ref likely doesn't exist.
            elif len(rv) > 0: # The processes sometimes return empty strings via Popen.communicate()... Need to retry.
                return rv
        return rv

    def WriteFileRef(self, ref, text):
        if ref is not None and text is not None and len(text) > 0:
            filePath = None
            with tempfile.NamedTemporaryFile(mode='w+', prefix='ac2git_ref_file_', delete=False) as f:
                filePath = f.name
                f.write(text)
            if filePath is not None:
                cmd = [ u'git', u'hash-object', u'-w', u'{0}'.format(filePath) ]
                objHash = ''
                tryCount = 0
                while objHash is not None and len(objHash) == 0 and tryCount < AccuRev2Git.commandFailureRetryCount:
                    objHash = self.gitRepo.raw_cmd(cmd)
                    objHash = objHash.strip()
                    tryCount += 1
                os.remove(filePath)
                updateRefRetr = None
                if objHash is not None:
                    cmd = [ u'git', u'update-ref', ref, objHash ]
                    updateRefRetr = self.gitRepo.raw_cmd(cmd)
                if objHash is None or updateRefRetr is None:
                    self.config.logger.dbg("Error! Command {cmd}".format(cmd=' '.join(str(x) for x in cmd)))
                    self.config.logger.dbg("  Failed with: {err}".format(err=self.gitRepo.lastStderr))
                    self.config.logger.error("Failed to record text for ref {r}, aborting!".format(r=ref))
                    raise Exception("Error! Failed to record text for ref {r}, aborting!".format(r=ref))
            else:
                self.config.logger.error("Failed to create temporary file for writing text to {r}".format(r=ref))
                raise Exception("Error! Failed to record current state, aborting!")
            return True
        return False

    def GetDepotHighWaterMark(self, depot):
        streamRefs = self.GetAllKnownStreamRefs(depot)
        lowestHwm = None
        for sRef in streamRefs:
            match = re.match(r'.*/stream_(\d+)_hwm$', sRef)
            if match is not None:
                text = self.ReadFileRef(ref=sRef)
                if text is None:
                    self.config.logger.error("Failed to read ref {r}!".format(r=sRef))
                hwm = json.loads(text)
                if lowestHwm is None or hwm["high-water-mark"] < lowestHwm:
                    lowestHwm = hwm["high-water-mark"]
        return lowestHwm

    def ProcessTransactions(self):
        # Git refspec for the state ref in which we will store a blob.
        stateRefspec = u'{refsNS}processing_state'.format(refsNS=AccuRev2Git.gitRefsNamespace)

        stateText = self.ReadFileRef(ref=stateRefspec)
        if stateText is not None:
            state = json.loads(stateText)
        else:
            depot = self.GetDepot(self.config.accurev.depot)
            if depot is None:
                raise Exception("Failed to get depot {depot}!".format(depot=self.config.accurev.depot))

            streamMap = OrderedDict()
            for configStream in self.config.accurev.streamMap:
                branchName = self.config.accurev.streamMap[configStream]
                stream = self.GetStreamByName(depot.number, configStream)
                streamMap[str(stream.streamNumber)] = { "stream_name": configStream, "branch_name": branchName }

            # Default state
            state = { "depot": depot.number,
                      "stream_map": streamMap,
                      "transaction": int(self.config.accurev.startTransaction), # Dynamic
                      "branch_list": None,                                      # Dynamic
                      "next_transaction_map": None }                            # Dynamic

        print("State:\n{0}".format(state))
        raise Exception("Not yet implemented!")

        # Other state variables
        startTransaction = state["transaction"]
        endTransaction = self.GetDepotHighWaterMark(self.config.accurev.depot)
        try:
            endTransaction = min(endTransaction, int(self.config.accurev.endTransaction))
        except:
            pass # keywords highest, now or date time are ignored. We only read the config in case
                 # that the configured end transaction is lower than the lowest high-water-mark we
                 # have for the depot.
        currentTransaction = None

            
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
                    stream = accurev.show.streams(depot=depot, stream=streamNumber, timeSpec=transaction.id, useCache=self.config.accurev.UseCommandCache()).streams[0] # could be expensive
                    if stream is not None and stream.name is not None:
                        return stream.name
                except:
                    pass
            return streamName
        return None

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
                self.gitRepo.reset(isHard=True)
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

            self.gitRepo.raw_cmd([u'git', u'config', u'--local', u'gc.auto', u'0'])

            if self.config.method in [ "deep-hist", "diff", "pop" ]:
                self.config.logger.info("Retrieveing stream information from Accurev into hidden refs.")
                self.RetrieveStreams()
            elif self.config.method in [ "skip" ]:
                self.config.logger.info("Skipping retrieval of stream information from Accurev.")
            else:
                raise Exception("Unrecognized method '{method}'".format(method=self.config.method))

            if self.config.mergeStrategy in [ "normal" ]:
                self.config.logger.info("Processing transactions from hidden refs. Merge strategy '{strategy}'.".format(strategy=self.config.mergeStrategy))
                self.ProcessTransactions()
            elif self.config.mergeStrategy in [ "orphanage" ]:
                self.config.logger.info("Processing streams from hidden refs. Merge strategy '{strategy}'.".format(strategy=self.config.mergeStrategy))
                self.ProcessStreams(orderByStreamNumber=False)
            elif self.config.mergeStrategy in [ "skip", None ]:
                self.config.logger.info("Skipping processing of Accurev data. No git branches will be generated/updated. Merge strategy '{strategy}'.".format(strategy=self.config.mergeStrategy))
                pass # Skip the merge step.
            else:
                raise Exception("Unrecognized merge strategy '{strategy}'".format(strategy=self.config.mergeStrategy))

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
    <git repo-path="/put/the/git/repo/here" message-style="normal" >  <!-- The system path where you want the git repo to be populated. Note: this folder should already exist. 
                                                                           The message-style attribute can either be "normal", "clean" or "notes". When set to "normal" accurev transaction information is included
                                                                           at the end (in the footer) of each commit message. When set to "clean" the transaction comment is the commit message without any
                                                                           additional information. When set to "notes" a note is added to each commit in the "accurev" namespace (to show them use `git log --notes=accurev`),
                                                                           with the same accurev information that would have been shown in the commit message footer in the "normal" mode.
                                                                      -->
        <remote name="origin" url="https://github.com/orao/ac2git.git" push-url="https://github.com/orao/ac2git.git" /> <!-- Optional: Specifies the remote to which the converted
                                                                                                                             branches will be pushed. The push-url attribute is optional. -->
        <remote name="backup" url="https://github.com/orao/ac2git.git" />
    </git>
    <method>deep-hist</method> <!-- The method specifies what approach is taken to retrieve information from Accurev. Allowed values are 'deep-hist', 'diff', 'pop' and 'skip'.
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
                                     - skip: This will skip the querying of the Accurev server for information about the streams. It makes sense in an already converted repo
                                             for which you only want to reprocess the already retrieved information without getting anything new.
                               -->
    <merge-strategy>normal</merge-strategy> <!-- The merge-strategy specified how the information downloaded from the streams in accurev is processed to form git branches.
                                                 It can be one of the following options ["skip", "normal", "orphanage"]:
                                             'skip' - Skips the processing step. The git repo won't have any visible git branches but will have hidden internal state which
                                                      tracks the accurev depot. When a merge strategy is next set to something other than 'skip' already retrieved information
                                                      won't be redownloaded from accurev and will be processed without executing any accurev commands (won't query the accurev server).
                                              'normal' - Performs merges using a straightforward but imprefect algorithm. The algorithm has the preferred balance between performance
                                                         the resulting merges in git.
                                              'orphanage' - Performs no merges but adds orphaned git branches which track the accurev streams. This is the old conversion method and is
                                                            here for legacy reasons. If streams are added later the resulting git repository commit hashes do not change but it will be
                                                            difficult to merge the branches in git at a later stage.
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

        streams = accurev.show.streams(depot=config.accurev.depot, useCache=self.config.accurev.UseCommandCache())
        if streams is not None and streams.streams is not None:
            for stream in streams.streams:
                if not (preserveConfig and stream in config.accurev.streamMap):
                    file.write("""
            <stream branch-name="accurev/{stream_name}">{stream_name}</stream>""".format(stream_name=stream.name))
                    # TODO: Add depot and start/end transaction overrides for each stream...

        file.write("""
        </stream-list>
    </accurev>
    <git repo-path="{git_repo_path}" message-style="{message_style}" >  <!-- The system path where you want the git repo to be populated. Note: this folder should already exist.
                                                                             The message-style attribute can either be "normal", "clean" or "notes". When set to "normal" accurev transaction information is included
                                                                           at the end (in the footer) of each commit message. When set to "clean" the transaction comment is the commit message without any
                                                                           additional information. When set to "notes" a note is added to each commit in the "accurev" namespace (to show them use `git log --notes=accurev`),
                                                                           with the same accurev information that would have been shown in the commit message footer in the "normal" mode.
                                                                        -->""".format(git_repo_path=config.git.repoPath, message_style=config.git.messageStyle if config.git.messageStyle is not None else 'normal'))
        if config.git.remoteMap is not None:
            for remoteName in remoteMap:
                remote = remoteMap[remoteName]
                file.write("""        <remote name="{name}" url="{url}"{push_url_string} />""".format(name=remote.name, url=name.url, push_url_string='' if name.pushUrl is None else ' push-url="{url}"'.format(url=name.pushUrl)))
        
        file.write("""    </git>
    <method>{method}</method>
    <merge-strategy>{merge_strategy}</merge-strategy>
    <logfile>{log_filename}<logfile>
    <!-- The user maps are used to convert users from AccuRev into git. Please spend the time to fill them in properly. -->""".format(method=config.method, merge_strategy=config.mergeStrategy, log_filename=config.logFilename))
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
    if args.conversionMethod is not None:
        config.method = args.conversionMethod
    if args.mergeStrategy is not None:
        config.mergeStrategy = args.mergeStrategy
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

def PrintConfigSummary(config):
    if config is not None:
        config.logger.info('Config info:')
        config.logger.info('  now: {0}'.format(datetime.now()))
        config.logger.info('  git')
        config.logger.info('    repo path: {0}'.format(config.git.repoPath))
        config.logger.info('    message style: {0}'.format(config.git.messageStyle))
        if config.git.remoteMap is not None:
            for remoteName in config.git.remoteMap:
                remote = config.git.remoteMap[remoteName]
                config.logger.info('    remote: {name} {url}{push_url}'.format(name=remote.name, url=remote.url, push_url = '' if remote.pushUrl is None or remote.url == remote.pushUrl else ' (push:{push_url})'.format(push_url=remote.pushUrl)))
                
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
        config.logger.info('  merge strategy: {0}'.format(config.mergeStrategy))
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
    parser.add_argument('-M', '--method', dest='conversionMethod', choices=['skip', 'pop', 'diff', 'deep-hist'], metavar='<conversion-method>', help="Specifies the method which is used to perform the conversion. Can be either 'pop', 'diff' or 'deep-hist'. 'pop' specifies that every transaction is populated in full. 'diff' specifies that only the differences are populated but transactions are iterated one at a time. 'deep-hist' specifies that only the differences are populated and that only transactions that could have affected this stream are iterated.")
    parser.add_argument('-j', '--merge-strategy', dest='mergeStrategy', choices=['skip', 'normal', 'orphanage'], metavar='<merge-strategy>', help="Sets the merge strategy which dictates how the git repository branches are generated. Depending on the value chosen the branches can be orphan branches ('orphanage' strategy) or have merges where promotes have occurred with the 'normal' strategy. The 'skip' strategy forces the script to skip making the git branches and will cause it to only do the retrieving of information from accurev for use with some strategy at a later date.")
    parser.add_argument('-r', '--restart',    dest='restart', action='store_const', const=True, help="Discard any existing conversion and start over.")
    parser.add_argument('-v', '--verbose',    dest='debug',   action='store_const', const=True, help="Print the script debug information. Makes the script more verbose.")
    parser.add_argument('-L', '--log-file',   dest='logFile', metavar='<log-filename>',         help="Sets the filename to which all console output will be logged (console output is still printed).")
    parser.add_argument('-q', '--no-log-file', dest='disableLogFile',  action='store_const', const=True, help="Do not log info to the log file. Alternatively achieved by not specifying a log file filename in the configuration file.")
    parser.add_argument('-l', '--reset-log-file', dest='resetLogFile', action='store_const', const=True, help="Instead of appending new log info to the file truncate it instead and start over.")
    parser.add_argument('--example-config', nargs='?', dest='exampleConfigFilename', const=defaultExampleConfigFilename, default=None, metavar='<example-config-filename>', help="Generates an example configuration file and exits. If the filename isn't specified a default filename '{0}' is used. Commandline arguments, if given, override all options in the configuration file.".format(defaultExampleConfigFilename, configFilename))
    parser.add_argument('-m', '--check-missing-users', dest='checkMissingUsers', action='store_const', const=True, help="It will print a list of usernames that are in accurev but were not found in the usermap.")
    parser.add_argument('--auto-config', nargs='?', dest='autoConfigFilename', const=configFilename, default=None, metavar='<config-filename>', help="Auto-generate the configuration file from known AccuRev information. It is required that an accurev username and password are provided either in an existing config file or via the -u and -p options. If there is an existing config file it is backed up and only the accurev username and password will be copied to the new configuration file. If you wish to preserve the config but add more information to it then it is recommended that you use the --fixup-config option instead.")
    parser.add_argument('--fixup-config', nargs='?', dest='fixupConfigFilename', const=configFilename, default=None, metavar='<config-filename>', help="Fixup the configuration file by adding updated AccuRev information. It is the same as the --auto-config option but the existing configuration file options are preserved. Other command line arguments that are provided will override the existing configuration file options for the new configuration file.")
    parser.add_argument('-T', '--track',    dest='track', action='store_const', const=True, help="Tracking mode. Sets the 'tracking' flag which makes the script run continuously in a loop. The configuration file is reloaded on each iteration so changes are picked up. Only makes sense for when you want this script to continuously track the accurev depot's newest transactions (i.e. you're using 'highest' or 'now' as your end transactions).")
    parser.add_argument('-I', '--tracking-intermission', nargs='?', dest='intermission', type=int, const=300, default=0, metavar='<intermission-sec>', help="Sets the intermission (in seconds) between consecutive iterations of the script in 'tracking' mode. The script sleeps for <intermission-sec> seconds before continuing the next conversion. This is useless if the --track option is not used.")
    
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
    
    while True:
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
        if not args.track:
            break
        elif args.intermission is not None:
            print("Tracking mode enabled: sleep for {0} seconds.".format(args.intermission))
            time.sleep(args.intermission)
        print("Tracking mode enabled: Continuing conversion.")

    return rv
        
# ################################################################################################ #
# Script Start                                                                                     #
# ################################################################################################ #
if __name__ == "__main__":
    AccuRev2GitMain(sys.argv)

