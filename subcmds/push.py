# -*- coding:utf-8 -*-
#
# Copyright (C) 2010 JiangXin@ossxp.com
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import unicode_literals
from __future__ import print_function
import copy
import re
import sys
import time
from command import InteractiveCommand
from editor import Editor
from error import UploadError, GitError, PullRequestError, ForkProjectError
from color import Coloring

from pyversion import is_python3
if not is_python3():
  input = raw_input  # noqa: F821
else:
  unicode = str

def _ConfirmManyUploads(multiple_branches=False):
  if multiple_branches:
    print("ATTENTION: One or more branches has an unusually high number of commits.")
  else:
    print("ATTENTION: You are uploading an unusually high number of commits.")
  print("YOU PROBABLY DO NOT MEAN TO DO THIS. (Did you rebase across branches?)")
  answer = input("If you are sure you intend to do this, type 'yes': ").strip()
  return answer == "yes"

def _die(fmt, *args):
  msg = fmt % args
  print('error: %s' % msg, file=sys.stderr)
  sys.exit(1)

def _SplitUsers(values):
  result = []
  for value in values:
    result.extend([s.strip() for s in value.split(',')])
  return result

class PushColoring(Coloring):

  def __init__(self, config):
    Coloring.__init__(self, config, 'status')
    self.fork = self.printer('fork', fg='green', ot='stderr')

class Push(InteractiveCommand):
  common = True
  helpSummary = "Upload changes for create pull requests on Gitee"
  helpUsage="""
%prog [--re --cc] {[<project>]... | --replace <project>}
"""
  helpDescription = """
The '%prog' command is used to send changes to the Gerrit Code
Review system.  It searches for topic branches in local projects
that have not yet been published for review.  If multiple topic
branches are found, '%prog' opens an editor to allow the user to
select which branches to upload.

'%prog' searches for uploadable changes in all projects listed at
the command line.  Projects can be specified either by name, or by
a relative or absolute path to the project's local directory. If no
projects are specified, '%prog' will search for uploadable changes
in all projects listed in the manifest.

If the --reviewers or --cc options are passed, those emails are
added to the respective list of users, and emails are sent to any
new users.  Users passed as --reviewers must already be registered
with the code review system, or the upload will fail.

If the --replace option is passed the user can designate which
existing change(s) in Gerrit match up to the commits in the branch
being uploaded.  For each matched pair of change,commit the commit
will be added as a new patch set, completely replacing the set of
files and description associated with the change in Gerrit.

Configuration
-------------

review.URL.autoupload:

To disable the "Upload ... (y/n)?" prompt, you can set a per-project
or global Git configuration option.  If review.URL.autoupload is set
to "true" then repo will assume you always answer "y" at the prompt,
and will not prompt you further.  If it is set to "false" then repo
will assume you always answer "n", and will abort.

review.URL.autocopy:

To automatically copy a user or mailing list to all uploaded reviews,
you can set a per-project or global Git option to do so. Specifically,
review.URL.autocopy can be set to a comma separated list of reviewers
who you always want copied on all uploads with a non-empty --re
argument.

review.URL.username:

Override the username used to connect to Gerrit Code Review.
By default the local part of the email address is used.

The URL must match the review URL listed in the manifest XML file,
or in the .git/config within the project.  For example:

  [remote "origin"]
    url = git://git.example.com/project.git
    review = http://review.example.com/

  [review "http://review.example.com/"]
    autoupload = true
    autocopy = johndoe@company.com,my-team-alias@company.com

References
----------

Gerrit Code Review:  http://code.google.com/p/gerrit/

"""

  def _Options(self, p):
    p.add_option('--new_branch',
                 dest='new_branch', action='store_true',
                 help='create new feature branch on git server.')
    p.add_option('-p', '--pr_force',
                 dest='pr_force', action='store_true',
                 help='creation pull request without configuration.')
    p.add_option('--title',
                 type='string', action='store', dest='title',
                 help='title of pull request.')
    p.add_option('--content',
                 type='string', action='store', dest='content',
                 help='content of pull request.')
    p.add_option('-f', '--force',
                 dest='force',  action='store_true',
                 help='push without rewind check.')
    p.add_option('--d', '--dest_branch',
                 type='string', action='store',
                 dest='dest_branch',
                 help='dest_branch of pr')
    p.add_option('--re', '--reviewers',
                 type='string', action='append',
                 dest='reviewers',
                 help='request reviews from these people.')
    p.add_option('--br',
                 type='string', action='store', dest='branch',
                 help='branch to push.')
    # p.add_option('--ignore_review',
    #              dest='ignore_review', action='store_true',
    #              help='run even has review defined.')

  def _SingleBranch(self, opt, branch, peoples):
    project = branch.project
    name = branch.name
    remote = project.GetBranch(name).remote

    key = 'review.%s.autoupload' % remote.review
    answer = project.config.GetBoolean(key)

    if answer is False:
      _die("upload blocked by %s = false" % key)

    if answer is None:
      date = branch.date
      list = branch.commits

      print('Upload project %s/:' % project.relpath)
      print('  branch %s (%2d commit%s, %s):' % (
                    name,
                    len(list),
                    len(list) != 1 and 's' or '',
                    date))
      for commit in list:
        print( '         %s' % commit)

      pushurl = project.manifest.manifestProject.config.GetString('repo.pushurl')
      sys.stdout.write('to %s (y/n)? ' % (pushurl and 'server: ' + pushurl or 'remote') )
      sys.stdout.flush()
      answer = sys.stdin.readline().strip()
      answer = answer in ('y', 'Y', 'yes', '1', 'true', 't')

    if answer:
      self._UploadAndReport(opt, [branch], peoples)
    else:
      _die("upload aborted by user")

  def _MultipleBranches(self, opt, pending, peoples):
    projects = {}
    branches = {}

    script = []
    script.append('# Uncomment the branches to upload:')
    for project, avail in pending:
      script.append('#')
      script.append('# project %s/:' % project.relpath)

      b = {}
      for branch in avail:
        name = branch.name
        date = branch.date
        list = branch.commits

        if b:
          script.append('#')
        script.append('#  branch %s (%2d commit%s, %s):' % (
                      name,
                      len(list),
                      len(list) != 1 and 's' or '',
                      date))
        for commit in list:
          script.append('#         %s' % commit)
        b[name] = branch

      projects[project.relpath] = project
      branches[project.name] = b
    script.append('')

    script = Editor.EditString("\n".join(script)).split("\n")

    project_re = re.compile(r'^#?\s*project\s*([^\s]+)/:$')
    branch_re = re.compile(r'^\s*branch\s*([^\s(]+)\s*\(.*')

    project = None
    todo = []

    for line in script:
      m = project_re.match(line)
      if m:
        name = m.group(1)
        project = projects.get(name)
        if not project:
          _die('project %s not available for upload', name)
        continue

      m = branch_re.match(line)
      if m:
        name = m.group(1)
        if not project:
          _die('project for branch %s not in script', name)
        branch = branches[project.name].get(name)
        if not branch:
          _die('branch %s not in %s', name, project.relpath)
        todo.append(branch)
    if not todo:
      _die("nothing uncommented for upload")

    self._UploadAndReport(opt, todo, peoples)

  def _UploadAndReport(self, opt, todo, peoples):
    out = PushColoring(self.manifest.manifestProject.config)
    exist_regex = r'^ 已存在相同源分支.*'
    have_errors = False
    for branch in todo:
      branch.have_pr_errors = False
      branch.have_pr = False
      try:
        # Check if there are local changes that may have been forgotten
        if branch.project.HasChanges():
            key = 'review.%s.autoupload' % branch.project.remote.review
            answer = branch.project.config.GetBoolean(key)

            # if they want to auto upload, let's not ask because it could be automated
            if answer is None:
                sys.stdout.write('Uncommitted changes in ' + branch.project.name + ' (did you forget to amend?). Continue uploading? (y/n) ')
                a = sys.stdin.readline().strip().lower()
                if a not in ('y', 'yes', 't', 'true', 'on'):
                    print("skipping upload", file=sys.stderr)
                    branch.uploaded = False
                    branch.error = 'User aborted'
                    continue
        branch.project.UploadNoReview(opt, peoples, branch=branch.name)
        branch.uploaded = True
        pull_request = self.manifest.manifestProject.config.GetString('repo.pullrequest')
        if not (pull_request and pull_request == 'False') or opt.pr_force:
          branch.have_pr = True
          branch.pull_requested = True
          times = 3
          while True:
            try:
              branch.pr_url = branch.project.PullRequest(opt, branch.name, peoples)
              break
            except PullRequestError as e:
              if (times and re.search('源分支.*不存在', unicode(e))):
                times -= 1
                print('Created PR failed due to push hook may still execute. Retry after 2 seconds', file=sys.stderr)
                time.sleep(2)
                continue
              else:
                raise e
      except UploadError as e:
        branch.error = e
        branch.uploaded = False
        have_errors = True
      except GitError as e:
        print("Error: "+ unicode(e), file=sys.stderr)
        sys.exit(1)
      except PullRequestError as e:
        branch.pr_error = e
        branch.pull_requested = False
        have_errors = True
        branch.have_pr_errors = True



    print(file=sys.stderr)
    print('----------------------------------------------------------------------', file=sys.stderr)

    if have_errors:
      for branch in todo:
        if not branch.uploaded:
          if len(unicode(branch.error)) <= 30:
            fmt = ' (%s)'
          else:
            fmt = '\n       (%s)'
          print(('[PUSH  FAILED] %-15s %-15s' + fmt) % (
              branch.project.relpath + '/',
              branch.name,
              unicode(branch.error)),
              file=sys.stderr)
          # TODO optimize ForkProjectError
          try:
              status_code, msg = branch.project.ForkProject()
              if status_code == 201:
                  hfk = out.fork
                  fork_info = 'Remote repository is syncing code, please wait for a while'
                  hfk('[FORK      OK] %-15s %-15s (%s) \n' % (
                         branch.project.relpath + '/',
                         branch.name, fork_info))
              else:
                  print(('[FORK  FAILED] %-15s %-15s (%s)') % (
                      branch.project.relpath + '/',
                      branch.name,
                      unicode(msg['message'])),
                        file=sys.stderr)
          except ForkProjectError as e:
              print(('[FORK  FAILED] %-15s %-15s (%s)') % (
                  branch.project.relpath + '/',
                  branch.name,
                  unicode(e)),
                    file=sys.stderr)




        if branch.have_pr_errors:
            if not branch.pull_requested:
              check_error = unicode(branch.pr_error).split(':')
              if len(check_error) >= 4 and re.match(exist_regex, check_error[3]):
                continue
              if len(unicode(branch.pr_error)) <= 30:
                fmt = ' (%s)'
              else:
                fmt = '\n       (%s)'
              print(('[PR    FAILED] %-15s %-15s' + fmt) % (
                     branch.project.relpath + '/',
                     branch.name,
                     unicode(branch.pr_error)),
                     file=sys.stderr)

      print("'if your PR FAILED or FORK OK, `repo push` again to create PR after handling the thing'", file=sys.stderr)
      print()

    for branch in todo:
        if branch.uploaded:
          print('[PUSH      OK] %-15s %s ' % (
                 branch.project.relpath + '/',
                 branch.name),
                 file=sys.stderr)
        if branch.have_pr:
            if branch.pull_requested:
              print('[PR        OK] %-15s %s pr_url: %s' % (
                     branch.project.relpath + '/',
                     branch.name, branch.pr_url),
                     file=sys.stderr)

    if have_errors:
      sys.exit(1)

  def Execute(self, opt, args):
    opt.new_branch = True
    project_list = self.GetProjects(args)
    pending = []
    reviewers = []
    branch = None
    # force push only allow one project

    if opt.branch:
        branch = opt.branch

    if opt.force:
      if len(project_list) != 1:
        print('error: --force requires exactly one project', file=sys.stderr)
        sys.exit(1)

    # if not create new branch, check whether branch has new commit.
    for project in project_list:
        branch_tmp = branch if branch else project.CurrentBranch
        if (not opt.new_branch and
                project.GetUploadableBranch(branch_tmp) is None):
            continue
        rb = project.GetPushableBranch(branch_tmp)
        if rb:
            pending.append((project, [rb]))

    if opt.reviewers:
      reviewers = _SplitUsers(opt.reviewers)
    # run git push
    if not pending:
      print("no branches ready for upload", file=sys.stderr)
    elif len(pending) == 1 and len(pending[0][1]) == 1:
      self._SingleBranch(opt, pending[0][1][0], reviewers)
    else:
      self._MultipleBranches(opt, pending, reviewers)
