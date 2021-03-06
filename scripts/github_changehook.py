#!/usr/bin/env python
"""
github_changehook.py is based on github_buildbot.py

github_changehook.py will determine the repository information from the JSON 
HTTP POST it receives from github.com and refresh the buildmaster

"""

import tempfile
import logging
import os
import re
import sys
import traceback
from twisted.web import server, resource
from twisted.internet import reactor, utils, defer
from twisted.spread import pb
from twisted.cred import credentials
from optparse import OptionParser

try:
    import json
except ImportError:
    import simplejson as json


class GitHubChangeListener(resource.Resource):
    """
    GitHubChangeListener creates the webserver that responds to the GitHub Service
    Hook.
    """
    isLeaf = True
    port = None
    
    def render_POST(self, request):
        """
        Reponds only to POST events and starts the build process
        
        :arguments:
            request
                the http request object
        """
        try:
            payload = json.loads(request.args['payload'][0])
            user = payload['repository']['owner']['name']
            repo = payload['repository']['name']
            repo_url = payload['repository']['url']
            self.private = payload['repository']['private']
            project = request.args.get('project', None)
            if project:
                project = project[0]
            logging.debug("Payload: " + str(payload))
            self.process_change(payload, user, repo, repo_url, project)
        except Exception:
            logging.error("Encountered an exception:")
            for msg in traceback.format_exception(*sys.exc_info()):
                logging.error(msg.strip())

    def process_change(self, payload, user, repo, repo_url, project):
        """
        Consumes the JSON as a python object and actually starts the build.
        
        :arguments:
            payload
                Python Object that represents the JSON sent by GitHub Service
                Hook.
        """
        changes = []
        newrev = payload['after']
        refname = payload['ref']
        
        # We only care about regular heads, i.e. branches
        match = re.match(r"^refs\/heads\/(.+)$", refname)
        if not match:
            logging.info("Ignoring refname `%s': Not a branch" % refname)

        branch = match.group(1)
        # Find out if the branch was created, deleted or updated. Branches
        # being deleted aren't really interesting.
        if re.match(r"^0*$", newrev):
            logging.info("Branch `%s' deleted, ignoring" % branch)
        else: 
            for commit in payload['commits']:
                files = []
                files.extend(commit['added'])
                files.extend(commit['modified'])
                files.extend(commit['removed'])
                change = {'revision': commit['id'],
                     'revlink': commit['url'],
                     'comments': commit['message'],
                     'branch': branch,
                     'who': commit['author']['name'] 
                            + " <" + commit['author']['email'] + ">",
                     'files': files,
                     'links': [commit['url']],
                     'repository': repo_url,
                     'project': project,
                }
                changes.append(change)
        
        # Submit the changes, if any
        if not changes:
            logging.warning("No changes found")
            return

        self.process_changes(changes)

    def connectFailed(self, error):
        """
        If connection is failed.  Logs the error.
        """
        logging.error("Could not connect to master: %s"
                % error.getErrorMessage())
        return error

    def addChange(self, dummy, remote, changei):
        """
        Sends changes from the commit to the buildmaster.
        """
        logging.debug("addChange %s, %s" % (repr(remote), repr(changei)))
        try:
            change = changei.next()
        except StopIteration:
            remote.broker.transport.loseConnection()
            return None
    
        logging.info("New revision: %s" % change['revision'][:8])
        for key, value in change.iteritems():
            logging.debug("  %s: %s" % (key, value))
    
        deferred = remote.callRemote('addChange', change)
        deferred.addCallback(self.addChange, remote, changei)
        return deferred

    def connected(self, remote, changes):
        """
        Reponds to the connected event.
        """
        return self.addChange(None, remote, changes.__iter__())

class GitHubBot(GitHubChangeListener):
    def __init__(self, master_dir, src_dir):
        self.master_dir = master_dir
        self.src_dir = src_dir
        GitHubChangeListener.__init__(self)

    @defer.deferredGenerator
    def process_changes(self, changes):

        logging.debug(">>>>>>>> processing changes: %s" % changes)
        logging.debug(">>>>>>>> git fetch:")
        x = defer.waitForDeferred(
            utils.getProcessOutputAndValue(
                '/usr/bin/git', ['--no-pager', 'fetch', '--all'], path=self.src_dir))
        yield x
        out,err,code = x.getResult()
        if code != 0: 
            logging.error('(%s) in git fetch: %s' % (code, err))
            return

        logging.debug(">>>>>>>> git reset:")
        logging.debug("processing changes: %s" % changes)
        x = defer.waitForDeferred(
            utils.getProcessOutputAndValue(
                '/usr/bin/git', ['--no-pager', 'reset', '--hard', 'origin/master' ], path=self.src_dir))
        yield x
        out,err,code = x.getResult()
        if code != 0: 
            logging.error('(%s) in git reset: %s' % (code, err))
            return

        logging.debug(">>>>>>>> git submodule:")
        x = defer.waitForDeferred(
            utils.getProcessOutputAndValue(
                '/usr/bin/git', ['--no-pager', 'submodule', 'update', '--init' ], path=self.src_dir))
        yield x
        out,err,code = x.getResult()
        if code != 0: 
            logging.error('(%s) in git submodule: %s' % (code, err))
            return

        logging.debug(">>>>>>>> buildbot reconfig:")
        x = utils.getProcessOutputAndValue(
                '/usr/bin/buildbot', ['reconfig', self.master_dir])
        yield x
        if code != 0: 
            logging.error('(%s) in buildbot reconfig: %s' % (code, err))
            return
        logging.debug(">>>>>>>> done.")

def main():
    """
    The main event loop that starts the server and configures it.
    """
    usage = "usage: %prog [options]"
    parser = OptionParser(usage)
        
    parser.add_option("-p", "--port", 
        help="Port the HTTP server listens to for the GitHub Service Hook"
            + " [default: %default]", default=4000, type=int, dest="port")
        
    parser.add_option("-l", "--log", 
        help="The absolute path, including filename, to save the log to"
            + " [default: %default]", 
            default = tempfile.gettempdir() + "/github_buildbot.log",
            dest="log")
        
    parser.add_option("-L", "--level", 
        help="The logging level: debug, info, warn, error, fatal [default:" 
            + " %default]", default='warn', dest="level")
        
    parser.add_option("--pidfile",
        help="Write the process identifier (PID) to this file on start."
            + " The file is removed on clean exit. [default: %default]",
        default=None,
        dest="pidfile")

    parser.add_option("-m", "--master", 
        help="The absolute path to the buildmaster directory",
            dest="master")
        
    parser.add_option("-s", "--src", 
        help="The absolute path to the source directory that needs to be updated from github",
            dest="src")
        
    (options, _) = parser.parse_args()

    if options.pidfile:
        with open(options.pidfile, 'w') as f:
            f.write(str(os.getpid()))

    levels = {
        'debug':logging.DEBUG,
        'info':logging.INFO,
        'warn':logging.WARNING,
        'error':logging.ERROR,
        'fatal':logging.FATAL,
    }
    
    filename = options.log
    log_format = "%(asctime)s - %(levelname)s - %(message)s" 
    logging.basicConfig(filename=filename, format=log_format, 
                        level=levels[options.level])
    
    github_bot = GitHubBot(master_dir = options.master, src_dir = options.src)
    
    site = server.Site(github_bot)
    reactor.listenTCP(options.port, site)
    reactor.run()

    if options.pidfile and os.path.exists(options.pidfile):
        os.unlink(options.pidfile)

if __name__ == '__main__':
    main()
