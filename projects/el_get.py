from fossbot.bbot.procedures import GitHubElisp
from fossbot.bbot.status import IRC, GitHubWebStatus, MailNotifier
from fossbot.bbot.repository import GitHub

transitions={'successToFailure' : 1,'failureToSuccess' : 1, 'exception':1}

name = 'el-get'

repositories=[GitHub('dimitri/el-get'), GitHub('dabrahams/el-get')]

include_properties=['emacs']
            
build_procedures=[GitHubElisp('dimitri/el-get')]

status=[
    IRC(host="irc.freenode.net", nick="elgetbot",
        notify_events=transitions,
        channels=["#el-get"]),

    GitHubWebStatus('dimitri/el-get', http_port='tcp:8010:interface=127.0.0.1'),

    MailNotifier(fromaddr="buildbot@boostpro.com",
                 extraRecipients=["el-get-devel@tapoueh.org"],
                 mode='problem')]