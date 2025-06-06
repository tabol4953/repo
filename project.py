# -*- coding:utf-8 -*-
#
# Copyright (C) 2008 The Android Open Source Project
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

from __future__ import print_function
import errno
import filecmp
import glob
import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import requests

from color import Coloring
from git_command import GitCommand, git_require
from git_config import GitConfig, IsId, GetSchemeFromUrl, GetUrlCookieFile, \
    ID_RE
from settings import GITEE_SSH, GITEE_REPO_API, GITEE_USER_API, TIMEOUT
from error import GitError, HookError, UploadError, DownloadError, PullRequestError, ForkProjectError
from error import ManifestInvalidRevisionError, ManifestInvalidPathError
from error import NoManifestException
import platform_utils
import progress
from repo_trace import IsTrace, Trace

from git_refs import GitRefs, HEAD, R_HEADS, R_TAGS, R_PUB, R_M, R_WORKTREE_M

from pyversion import is_python3
if is_python3():
  import urllib.parse
else:
  import imp
  import urlparse
  urllib = imp.new_module('urllib')
  urllib.parse = urlparse
  input = raw_input  # noqa: F821


# Maximum sleep time allowed during retries.
MAXIMUM_RETRY_SLEEP_SEC = 3600.0
# +-10% random jitter is added to each Fetches retry sleep duration.
RETRY_JITTER_PERCENT = 0.1


def _lwrite(path, content):
  lock = '%s.lock' % path

  with open(lock, 'w') as fd:
    fd.write(content)

  try:
    platform_utils.rename(lock, path)
  except OSError:
    platform_utils.remove(lock)
    raise


def _error(fmt, *args):
  msg = fmt % args
  print('error: %s' % msg, file=sys.stderr)


def _warn(fmt, *args):
  msg = fmt % args
  print('warn: %s' % msg, file=sys.stderr)


def not_rev(r):
  return '^' + r


def sq(r):
  return "'" + r.replace("'", "'\''") + "'"


_project_hook_list = None


def _ProjectHooks():
  """List the hooks present in the 'hooks' directory.

  These hooks are project hooks and are copied to the '.git/hooks' directory
  of all subprojects.

  This function caches the list of hooks (based on the contents of the
  'repo/hooks' directory) on the first call.

  Returns:
    A list of absolute paths to all of the files in the hooks directory.
  """
  global _project_hook_list
  if _project_hook_list is None:
    d = platform_utils.realpath(os.path.abspath(os.path.dirname(__file__)))
    d = os.path.join(d, 'hooks')
    _project_hook_list = [os.path.join(d, x) for x in platform_utils.listdir(d)]
  return _project_hook_list


class DownloadedChange(object):
  _commit_cache = None

  def __init__(self, project, base, change_id, ps_id, commit):
    self.project = project
    self.base = base
    self.change_id = change_id
    self.ps_id = ps_id
    self.commit = commit

  @property
  def commits(self):
    if self._commit_cache is None:
      self._commit_cache = self.project.bare_git.rev_list('--abbrev=8',
                                                          '--abbrev-commit',
                                                          '--pretty=oneline',
                                                          '--reverse',
                                                          '--date-order',
                                                          not_rev(self.base),
                                                          self.commit,
                                                          '--')
    return self._commit_cache


class ReviewableBranch(object):
  _commit_cache = None
  _base_exists = None

  def __init__(self, project, branch, base):
    self.project = project
    self.branch = branch
    self.base = base

  @property
  def name(self):
    return self.branch.name

  @property
  def commits(self):
    if self._commit_cache is None:
      args = ('--abbrev=8', '--abbrev-commit', '--pretty=oneline', '--reverse',
              '--date-order', not_rev(self.base), R_HEADS + self.name, '--')
      try:
        self._commit_cache = self.project.bare_git.rev_list(*args)
      except GitError:
        # We weren't able to probe the commits for this branch.  Was it tracking
        # a branch that no longer exists?  If so, return no commits.  Otherwise,
        # rethrow the error as we don't know what's going on.
        if self.base_exists:
          raise

        self._commit_cache = []

    return self._commit_cache

  @property
  def unabbrev_commits(self):
    r = dict()
    for commit in self.project.bare_git.rev_list(not_rev(self.base),
                                                 R_HEADS + self.name,
                                                 '--'):
      r[commit[0:8]] = commit
    return r

  @property
  def date(self):
    return self.project.bare_git.log('--pretty=format:%cd',
                                     '-n', '1',
                                     R_HEADS + self.name,
                                     '--')

  @property
  def base_exists(self):
    """Whether the branch we're tracking exists.

    Normally it should, but sometimes branches we track can get deleted.
    """
    if self._base_exists is None:
      try:
        self.project.bare_git.rev_parse('--verify', not_rev(self.base))
        # If we're still here, the base branch exists.
        self._base_exists = True
      except GitError:
        # If we failed to verify, the base branch doesn't exist.
        self._base_exists = False

    return self._base_exists

  def UploadForReview(self, people,
                      dryrun=False,
                      auto_topic=False,
                      hashtags=(),
                      labels=(),
                      private=False,
                      notify=None,
                      wip=False,
                      dest_branch=None,
                      validate_certs=True,
                      push_options=None):
    self.project.UploadForReview(branch=self.name,
                                 people=people,
                                 dryrun=dryrun,
                                 auto_topic=auto_topic,
                                 hashtags=hashtags,
                                 labels=labels,
                                 private=private,
                                 notify=notify,
                                 wip=wip,
                                 dest_branch=dest_branch,
                                 validate_certs=validate_certs,
                                 push_options=push_options)

  def GetPublishedRefs(self):
    refs = {}
    output = self.project.bare_git.ls_remote(
        self.branch.remote.SshReviewUrl(self.project.UserEmail),
        'refs/changes/*')
    for line in output.split('\n'):
      try:
        (sha, ref) = line.split()
        refs[sha] = ref
      except ValueError:
        pass

    return refs


class StatusColoring(Coloring):

  def __init__(self, config):
    Coloring.__init__(self, config, 'status')
    self.project = self.printer('header', attr='bold')
    self.branch = self.printer('header', attr='bold')
    self.nobranch = self.printer('nobranch', fg='red')
    self.important = self.printer('important', fg='red')

    self.added = self.printer('added', fg='green')
    self.changed = self.printer('changed', fg='red')
    self.untracked = self.printer('untracked', fg='red')


class DiffColoring(Coloring):

  def __init__(self, config):
    Coloring.__init__(self, config, 'diff')
    self.project = self.printer('header', attr='bold')
    self.fail = self.printer('fail', fg='red')


class _Annotation(object):

  def __init__(self, name, value, keep):
    self.name = name
    self.value = value
    self.keep = keep


def _SafeExpandPath(base, subpath, skipfinal=False):
  """Make sure |subpath| is completely safe under |base|.

  We make sure no intermediate symlinks are traversed, and that the final path
  is not a special file (e.g. not a socket or fifo).

  NB: We rely on a number of paths already being filtered out while parsing the
  manifest.  See the validation logic in manifest_xml.py for more details.
  """
  # Split up the path by its components.  We can't use os.path.sep exclusively
  # as some platforms (like Windows) will convert / to \ and that bypasses all
  # our constructed logic here.  Especially since manifest authors only use
  # / in their paths.
  resep = re.compile(r'[/%s]' % re.escape(os.path.sep))
  components = resep.split(subpath)
  if skipfinal:
    # Whether the caller handles the final component itself.
    finalpart = components.pop()

  path = base
  for part in components:
    if part in {'.', '..'}:
      raise ManifestInvalidPathError(
          '%s: "%s" not allowed in paths' % (subpath, part))

    path = os.path.join(path, part)
    if platform_utils.islink(path):
      raise ManifestInvalidPathError(
          '%s: traversing symlinks not allow' % (path,))

    if os.path.exists(path):
      if not os.path.isfile(path) and not platform_utils.isdir(path):
        raise ManifestInvalidPathError(
            '%s: only regular files & directories allowed' % (path,))

  if skipfinal:
    path = os.path.join(path, finalpart)

  return path


class _CopyFile(object):
  """Container for <copyfile> manifest element."""

  def __init__(self, git_worktree, src, topdir, dest):
    """Register a <copyfile> request.

    Args:
      git_worktree: Absolute path to the git project checkout.
      src: Relative path under |git_worktree| of file to read.
      topdir: Absolute path to the top of the repo client checkout.
      dest: Relative path under |topdir| of file to write.
    """
    self.git_worktree = git_worktree
    self.topdir = topdir
    self.src = src
    self.dest = dest

  def _Copy(self):
    src = _SafeExpandPath(self.git_worktree, self.src)
    dest = _SafeExpandPath(self.topdir, self.dest)

    if platform_utils.isdir(src):
      raise ManifestInvalidPathError(
          '%s: copying from directory not supported' % (self.src,))
    if platform_utils.isdir(dest):
      raise ManifestInvalidPathError(
          '%s: copying to directory not allowed' % (self.dest,))

    # copy file if it does not exist or is out of date
    if not os.path.exists(dest) or not filecmp.cmp(src, dest):
      try:
        # remove existing file first, since it might be read-only
        if os.path.exists(dest):
          platform_utils.remove(dest)
        else:
          dest_dir = os.path.dirname(dest)
          if not platform_utils.isdir(dest_dir):
            os.makedirs(dest_dir)
        shutil.copy(src, dest)
        # make the file read-only
        mode = os.stat(dest)[stat.ST_MODE]
        mode = mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        os.chmod(dest, mode)
      except IOError:
        _error('Cannot copy file %s to %s', src, dest)


class _LinkFile(object):
  """Container for <linkfile> manifest element."""

  def __init__(self, git_worktree, src, topdir, dest):
    """Register a <linkfile> request.

    Args:
      git_worktree: Absolute path to the git project checkout.
      src: Target of symlink relative to path under |git_worktree|.
      topdir: Absolute path to the top of the repo client checkout.
      dest: Relative path under |topdir| of symlink to create.
    """
    self.git_worktree = git_worktree
    self.topdir = topdir
    self.src = src
    self.dest = dest

  def __linkIt(self, relSrc, absDest):
    # link file if it does not exist or is out of date
    if not platform_utils.islink(absDest) or (platform_utils.readlink(absDest) != relSrc):
      try:
        # remove existing file first, since it might be read-only
        if os.path.lexists(absDest):
          platform_utils.remove(absDest)
        else:
          dest_dir = os.path.dirname(absDest)
          if not platform_utils.isdir(dest_dir):
            os.makedirs(dest_dir)
        platform_utils.symlink(relSrc, absDest)
      except IOError:
        _error('Cannot link file %s to %s', relSrc, absDest)

  def _Link(self):
    """Link the self.src & self.dest paths.

    Handles wild cards on the src linking all of the files in the source in to
    the destination directory.
    """
    # Some people use src="." to create stable links to projects.  Lets allow
    # that but reject all other uses of "." to keep things simple.
    if self.src == '.':
      src = self.git_worktree
    else:
      src = _SafeExpandPath(self.git_worktree, self.src)

    if not glob.has_magic(src):
      # Entity does not contain a wild card so just a simple one to one link operation.
      dest = _SafeExpandPath(self.topdir, self.dest, skipfinal=True)
      # dest & src are absolute paths at this point.  Make sure the target of
      # the symlink is relative in the context of the repo client checkout.
      relpath = os.path.relpath(src, os.path.dirname(dest))
      self.__linkIt(relpath, dest)
    else:
      dest = _SafeExpandPath(self.topdir, self.dest)
      # Entity contains a wild card.
      if os.path.exists(dest) and not platform_utils.isdir(dest):
        _error('Link error: src with wildcard, %s must be a directory', dest)
      else:
        for absSrcFile in glob.glob(src):
          # Create a releative path from source dir to destination dir
          absSrcDir = os.path.dirname(absSrcFile)
          relSrcDir = os.path.relpath(absSrcDir, dest)

          # Get the source file name
          srcFile = os.path.basename(absSrcFile)

          # Now form the final full paths to srcFile. They will be
          # absolute for the desintaiton and relative for the srouce.
          absDest = os.path.join(dest, srcFile)
          relSrc = os.path.join(relSrcDir, srcFile)
          self.__linkIt(relSrc, absDest)


class RemoteSpec(object):

  def __init__(self,
               name,
               url=None,
               pushUrl=None,
               review=None,
               revision=None,
               orig_name=None,
               fetchUrl=None):
    self.name = name
    self.url = url
    self.pushUrl = pushUrl
    self.review = review
    self.revision = revision
    self.orig_name = orig_name
    self.fetchUrl = fetchUrl


class RepoHook(object):

  """A RepoHook contains information about a script to run as a hook.

  Hooks are used to run a python script before running an upload (for instance,
  to run presubmit checks).  Eventually, we may have hooks for other actions.

  This shouldn't be confused with files in the 'repo/hooks' directory.  Those
  files are copied into each '.git/hooks' folder for each project.  Repo-level
  hooks are associated instead with repo actions.

  Hooks are always python.  When a hook is run, we will load the hook into the
  interpreter and execute its main() function.
  """

  def __init__(self,
               hook_type,
               hooks_project,
               topdir,
               manifest_url,
               abort_if_user_denies=False):
    """RepoHook constructor.

    Params:
      hook_type: A string representing the type of hook.  This is also used
          to figure out the name of the file containing the hook.  For
          example: 'pre-upload'.
      hooks_project: The project containing the repo hooks.  If you have a
          manifest, this is manifest.repo_hooks_project.  OK if this is None,
          which will make the hook a no-op.
      topdir: Repo's top directory (the one containing the .repo directory).
          Scripts will run with CWD as this directory.  If you have a manifest,
          this is manifest.topdir
      manifest_url: The URL to the manifest git repo.
      abort_if_user_denies: If True, we'll throw a HookError() if the user
          doesn't allow us to run the hook.
    """
    self._hook_type = hook_type
    self._hooks_project = hooks_project
    self._manifest_url = manifest_url
    self._topdir = topdir
    self._abort_if_user_denies = abort_if_user_denies

    # Store the full path to the script for convenience.
    if self._hooks_project:
      self._script_fullpath = os.path.join(self._hooks_project.worktree,
                                           self._hook_type + '.py')
    else:
      self._script_fullpath = None

  def _GetHash(self):
    """Return a hash of the contents of the hooks directory.

    We'll just use git to do this.  This hash has the property that if anything
    changes in the directory we will return a different has.

    SECURITY CONSIDERATION:
      This hash only represents the contents of files in the hook directory, not
      any other files imported or called by hooks.  Changes to imported files
      can change the script behavior without affecting the hash.

    Returns:
      A string representing the hash.  This will always be ASCII so that it can
      be printed to the user easily.
    """
    assert self._hooks_project, "Must have hooks to calculate their hash."

    # We will use the work_git object rather than just calling GetRevisionId().
    # That gives us a hash of the latest checked in version of the files that
    # the user will actually be executing.  Specifically, GetRevisionId()
    # doesn't appear to change even if a user checks out a different version
    # of the hooks repo (via git checkout) nor if a user commits their own revs.
    #
    # NOTE: Local (non-committed) changes will not be factored into this hash.
    # I think this is OK, since we're really only worried about warning the user
    # about upstream changes.
    return self._hooks_project.work_git.rev_parse('HEAD')

  def _GetMustVerb(self):
    """Return 'must' if the hook is required; 'should' if not."""
    if self._abort_if_user_denies:
      return 'must'
    else:
      return 'should'

  def _CheckForHookApproval(self):
    """Check to see whether this hook has been approved.

    We'll accept approval of manifest URLs if they're using secure transports.
    This way the user can say they trust the manifest hoster.  For insecure
    hosts, we fall back to checking the hash of the hooks repo.

    Note that we ask permission for each individual hook even though we use
    the hash of all hooks when detecting changes.  We'd like the user to be
    able to approve / deny each hook individually.  We only use the hash of all
    hooks because there is no other easy way to detect changes to local imports.

    Returns:
      True if this hook is approved to run; False otherwise.

    Raises:
      HookError: Raised if the user doesn't approve and abort_if_user_denies
          was passed to the consturctor.
    """
    if self._ManifestUrlHasSecureScheme():
      return self._CheckForHookApprovalManifest()
    else:
      return self._CheckForHookApprovalHash()

  def _CheckForHookApprovalHelper(self, subkey, new_val, main_prompt,
                                  changed_prompt):
    """Check for approval for a particular attribute and hook.

    Args:
      subkey: The git config key under [repo.hooks.<hook_type>] to store the
          last approved string.
      new_val: The new value to compare against the last approved one.
      main_prompt: Message to display to the user to ask for approval.
      changed_prompt: Message explaining why we're re-asking for approval.

    Returns:
      True if this hook is approved to run; False otherwise.

    Raises:
      HookError: Raised if the user doesn't approve and abort_if_user_denies
          was passed to the consturctor.
    """
    hooks_config = self._hooks_project.config
    git_approval_key = 'repo.hooks.%s.%s' % (self._hook_type, subkey)

    # Get the last value that the user approved for this hook; may be None.
    old_val = hooks_config.GetString(git_approval_key)

    if old_val is not None:
      # User previously approved hook and asked not to be prompted again.
      if new_val == old_val:
        # Approval matched.  We're done.
        return True
      else:
        # Give the user a reason why we're prompting, since they last told
        # us to "never ask again".
        prompt = 'WARNING: %s\n\n' % (changed_prompt,)
    else:
      prompt = ''

    # Prompt the user if we're not on a tty; on a tty we'll assume "no".
    if sys.stdout.isatty():
      prompt += main_prompt + ' (yes/always/NO)? '
      response = input(prompt).lower()
      print()

      # User is doing a one-time approval.
      if response in ('y', 'yes'):
        return True
      elif response == 'always':
        hooks_config.SetString(git_approval_key, new_val)
        return True

    # For anything else, we'll assume no approval.
    if self._abort_if_user_denies:
      raise HookError('You must allow the %s hook or use --no-verify.' %
                      self._hook_type)

    return False

  def _ManifestUrlHasSecureScheme(self):
    """Check if the URI for the manifest is a secure transport."""
    secure_schemes = ('file', 'https', 'ssh', 'persistent-https', 'sso', 'rpc')
    parse_results = urllib.parse.urlparse(self._manifest_url)
    return parse_results.scheme in secure_schemes

  def _CheckForHookApprovalManifest(self):
    """Check whether the user has approved this manifest host.

    Returns:
      True if this hook is approved to run; False otherwise.
    """
    return self._CheckForHookApprovalHelper(
        'approvedmanifest',
        self._manifest_url,
        'Run hook scripts from %s' % (self._manifest_url,),
        'Manifest URL has changed since %s was allowed.' % (self._hook_type,))

  def _CheckForHookApprovalHash(self):
    """Check whether the user has approved the hooks repo.

    Returns:
      True if this hook is approved to run; False otherwise.
    """
    prompt = ('Repo %s run the script:\n'
              '  %s\n'
              '\n'
              'Do you want to allow this script to run')
    return self._CheckForHookApprovalHelper(
        'approvedhash',
        self._GetHash(),
        prompt % (self._GetMustVerb(), self._script_fullpath),
        'Scripts have changed since %s was allowed.' % (self._hook_type,))

  @staticmethod
  def _ExtractInterpFromShebang(data):
    """Extract the interpreter used in the shebang.

    Try to locate the interpreter the script is using (ignoring `env`).

    Args:
      data: The file content of the script.

    Returns:
      The basename of the main script interpreter, or None if a shebang is not
      used or could not be parsed out.
    """
    firstline = data.splitlines()[:1]
    if not firstline:
      return None

    # The format here can be tricky.
    shebang = firstline[0].strip()
    m = re.match(r'^#!\s*([^\s]+)(?:\s+([^\s]+))?', shebang)
    if not m:
      return None

    # If the using `env`, find the target program.
    interp = m.group(1)
    if os.path.basename(interp) == 'env':
      interp = m.group(2)

    return interp

  def _ExecuteHookViaReexec(self, interp, context, **kwargs):
    """Execute the hook script through |interp|.

    Note: Support for this feature should be dropped ~Jun 2021.

    Args:
      interp: The Python program to run.
      context: Basic Python context to execute the hook inside.
      kwargs: Arbitrary arguments to pass to the hook script.

    Raises:
      HookError: When the hooks failed for any reason.
    """
    # This logic needs to be kept in sync with _ExecuteHookViaImport below.
    script = """
import json, os, sys
path = '''%(path)s'''
kwargs = json.loads('''%(kwargs)s''')
context = json.loads('''%(context)s''')
sys.path.insert(0, os.path.dirname(path))
data = open(path).read()
exec(compile(data, path, 'exec'), context)
context['main'](**kwargs)
""" % {
        'path': self._script_fullpath,
        'kwargs': json.dumps(kwargs),
        'context': json.dumps(context),
    }

    # We pass the script via stdin to avoid OS argv limits.  It also makes
    # unhandled exception tracebacks less verbose/confusing for users.
    cmd = [interp, '-c', 'import sys; exec(sys.stdin.read())']
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    proc.communicate(input=script.encode('utf-8'))
    if proc.returncode:
      raise HookError('Failed to run %s hook.' % (self._hook_type,))

  def _ExecuteHookViaImport(self, data, context, **kwargs):
    """Execute the hook code in |data| directly.

    Args:
      data: The code of the hook to execute.
      context: Basic Python context to execute the hook inside.
      kwargs: Arbitrary arguments to pass to the hook script.

    Raises:
      HookError: When the hooks failed for any reason.
    """
    # Exec, storing global context in the context dict.  We catch exceptions
    # and convert to a HookError w/ just the failing traceback.
    try:
      exec(compile(data, self._script_fullpath, 'exec'), context)
    except Exception:
      raise HookError('%s\nFailed to import %s hook; see traceback above.' %
                      (traceback.format_exc(), self._hook_type))

    # Running the script should have defined a main() function.
    if 'main' not in context:
      raise HookError('Missing main() in: "%s"' % self._script_fullpath)

    # Call the main function in the hook.  If the hook should cause the
    # build to fail, it will raise an Exception.  We'll catch that convert
    # to a HookError w/ just the failing traceback.
    try:
      context['main'](**kwargs)
    except Exception:
      raise HookError('%s\nFailed to run main() for %s hook; see traceback '
                      'above.' % (traceback.format_exc(), self._hook_type))

  def _ExecuteHook(self, **kwargs):
    """Actually execute the given hook.

    This will run the hook's 'main' function in our python interpreter.

    Args:
      kwargs: Keyword arguments to pass to the hook.  These are often specific
          to the hook type.  For instance, pre-upload hooks will contain
          a project_list.
    """
    # Keep sys.path and CWD stashed away so that we can always restore them
    # upon function exit.
    orig_path = os.getcwd()
    orig_syspath = sys.path

    print("\nStartExecuteHook: %s" % self._script_fullpath, file=sys.stdout)

    try:
      # Always run hooks with CWD as topdir.
      os.chdir(self._topdir)

      # Put the hook dir as the first item of sys.path so hooks can do
      # relative imports.  We want to replace the repo dir as [0] so
      # hooks can't import repo files.
      sys.path = [os.path.dirname(self._script_fullpath)] + sys.path[1:]

      # Initial global context for the hook to run within.
      context = {'__file__': self._script_fullpath}

      # Add 'hook_should_take_kwargs' to the arguments to be passed to main.
      # We don't actually want hooks to define their main with this argument--
      # it's there to remind them that their hook should always take **kwargs.
      # For instance, a pre-upload hook should be defined like:
      #   def main(project_list, **kwargs):
      #
      # This allows us to later expand the API without breaking old hooks.
      kwargs = kwargs.copy()
      kwargs['hook_should_take_kwargs'] = True

      # See what version of python the hook has been written against.
      data = open(self._script_fullpath).read()
      interp = self._ExtractInterpFromShebang(data)
      reexec = False
      if interp:
        prog = os.path.basename(interp)
        if prog.startswith('python2') and sys.version_info.major != 2:
          reexec = True
        elif prog.startswith('python3') and sys.version_info.major == 2:
          reexec = True

      # Attempt to execute the hooks through the requested version of Python.
      if reexec:
        try:
          self._ExecuteHookViaReexec(interp, context, **kwargs)
        except OSError as e:
          if e.errno == errno.ENOENT:
            # We couldn't find the interpreter, so fallback to importing.
            reexec = False
          else:
            raise

      # Run the hook by importing directly.
      if not reexec:
        self._ExecuteHookViaImport(data, context, **kwargs)
    finally:
      # Restore sys.path and CWD.
      sys.path = orig_syspath
      os.chdir(orig_path)

  def Run(self, user_allows_all_hooks, **kwargs):
    """Run the hook.

    If the hook doesn't exist (because there is no hooks project or because
    this particular hook is not enabled), this is a no-op.

    Args:
      user_allows_all_hooks: If True, we will never prompt about running the
          hook--we'll just assume it's OK to run it.
      kwargs: Keyword arguments to pass to the hook.  These are often specific
          to the hook type.  For instance, pre-upload hooks will contain
          a project_list.

    Raises:
      HookError: If there was a problem finding the hook or the user declined
          to run a required hook (from _CheckForHookApproval).
    """
    # No-op if there is no hooks project or if hook is disabled.
    if ((not self._hooks_project) or (self._hook_type not in
                                      self._hooks_project.enabled_repo_hooks)):
      return

    # Bail with a nice error if we can't find the hook.
    if not os.path.isfile(self._script_fullpath):
      raise HookError('Couldn\'t find repo hook: "%s"' % self._script_fullpath)

    # Make sure the user is OK with running the hook.
    if (not user_allows_all_hooks) and (not self._CheckForHookApproval()):
      return

    # Run the hook with the same version of python we're using.
    self._ExecuteHook(**kwargs)


class Project(object):
  # These objects can be shared between several working trees.
  shareable_files = ['description', 'info']
  shareable_dirs = ['hooks', 'objects', 'rr-cache', 'svn']
  # These objects can only be used by a single working tree.
  working_tree_files = ['config', 'packed-refs', 'shallow']
  working_tree_dirs = ['logs', 'refs']
  mirror_url_mapping = {
    "openharmony": "https://github.com/openharmony/"
  }
  default_source_url = 'https://gitee.com/openharmony/'

  def __init__(self,
               manifest,
               name,
               remote,
               gitdir,
               objdir,
               worktree,
               relpath,
               revisionExpr,
               revisionId,
               rebase=True,
               groups=None,
               sync_c=False,
               sync_s=False,
               sync_tags=True,
               clone_depth=None,
               upstream=None,
               parent=None,
               use_git_worktrees=False,
               is_derived=False,
               dest_branch=None,
               optimized_fetch=False,
               retry_fetches=0,
               old_revision=None):
    """Init a Project object.

    Args:
      manifest: The XmlManifest object.
      name: The `name` attribute of manifest.xml's project element.
      remote: RemoteSpec object specifying its remote's properties.
      gitdir: Absolute path of git directory.
      objdir: Absolute path of directory to store git objects.
      worktree: Absolute path of git working tree.
      relpath: Relative path of git working tree to repo's top directory.
      revisionExpr: The `revision` attribute of manifest.xml's project element.
      revisionId: git commit id for checking out.
      rebase: The `rebase` attribute of manifest.xml's project element.
      groups: The `groups` attribute of manifest.xml's project element.
      sync_c: The `sync-c` attribute of manifest.xml's project element.
      sync_s: The `sync-s` attribute of manifest.xml's project element.
      sync_tags: The `sync-tags` attribute of manifest.xml's project element.
      upstream: The `upstream` attribute of manifest.xml's project element.
      parent: The parent Project object.
      use_git_worktrees: Whether to use `git worktree` for this project.
      is_derived: False if the project was explicitly defined in the manifest;
                  True if the project is a discovered submodule.
      dest_branch: The branch to which to push changes for review by default.
      optimized_fetch: If True, when a project is set to a sha1 revision, only
                       fetch from the remote if the sha1 is not present locally.
      retry_fetches: Retry remote fetches n times upon receiving transient error
                     with exponential backoff and jitter.
      old_revision: saved git commit id for open GITC projects.
    """
    self.manifest = manifest
    self.name = name
    self.remote = remote
    self.gitdir = gitdir.replace('\\', '/')
    self.objdir = objdir.replace('\\', '/')
    if worktree:
      self.worktree = os.path.normpath(worktree).replace('\\', '/')
    else:
      self.worktree = None
    self.relpath = relpath
    self.revisionExpr = revisionExpr

    if revisionId is None \
            and revisionExpr \
            and IsId(revisionExpr):
      self.revisionId = revisionExpr
    else:
      self.revisionId = revisionId

    self.rebase = rebase
    self.groups = groups
    self.sync_c = sync_c
    self.sync_s = sync_s
    self.sync_tags = sync_tags
    self.clone_depth = clone_depth
    self.upstream = upstream
    self.parent = parent
    # NB: Do not use this setting in __init__ to change behavior so that the
    # manifest.git checkout can inspect & change it after instantiating.  See
    # the XmlManifest init code for more info.
    self.use_git_worktrees = use_git_worktrees
    self.is_derived = is_derived
    self.optimized_fetch = optimized_fetch
    self.retry_fetches = max(0, retry_fetches)
    self.subprojects = []

    self.snapshots = {}
    self.copyfiles = []
    self.linkfiles = []
    self.annotations = []
    self.config = GitConfig.ForRepository(gitdir=self.gitdir,
                                          defaults=self.manifest.globalConfig)

    if self.worktree:
      self.work_git = self._GitGetByExec(self, bare=False, gitdir=gitdir)
    else:
      self.work_git = None
    self.bare_git = self._GitGetByExec(self, bare=True, gitdir=gitdir)
    self.bare_ref = GitRefs(gitdir)
    self.bare_objdir = self._GitGetByExec(self, bare=True, gitdir=objdir)
    self.dest_branch = dest_branch
    self.old_revision = old_revision

    # This will be filled in if a project is later identified to be the
    # project containing repo hooks.
    self.enabled_repo_hooks = []
    self.mirror_url = ''

  def SetMirrorUrl(self):
    try:
      namespace = ''
      remote_url = self.remote.url
      if remote_url:
        if remote_url.find('github') == -1:
          if remote_url.find('git@') != -1:
            match = re.search(r':([^/]+)', remote_url)
            if match:
              namespace = match.group(1).strip()
          else:
            namespace = remote_url.split('/')[-2]
  
          gitee_platform = True if remote_url.find('gitee.com') != -1 else False
          gitcode_platform = True if remote_url.find('gitcode.com') != -1 else False
          if (gitee_platform or gitcode_platform) and namespace in self.mirror_url_mapping.keys():
            self.mirror_url = self.mirror_url_mapping.get(namespace, '') + self.name + '.git'
        else:
          if remote_url.find('git@') != -1:
            match = re.search(r':([^/]+)', remote_url)
            if match:
              namespace = match.group(1).strip()
          else:
            namespace = remote_url.split('/')[-2]
  
          if namespace in self.mirror_url_mapping.keys():
            self.mirror_url = self.remote.url
            self.remote.url = self.default_source_url + self.name + '.git'
    except IndexError:
      pass

  @property
  def Derived(self):
    return self.is_derived

  @property
  def Exists(self):
    return platform_utils.isdir(self.gitdir) and platform_utils.isdir(self.objdir)

  @property
  def CurrentBranch(self):
    """Obtain the name of the currently checked out branch.

    The branch name omits the 'refs/heads/' prefix.
    None is returned if the project is on a detached HEAD, or if the work_git is
    otheriwse inaccessible (e.g. an incomplete sync).
    """
    try:
      b = self.work_git.GetHead()
    except NoManifestException:
      # If the local checkout is in a bad state, don't barf.  Let the callers
      # process this like the head is unreadable.
      return None
    if b.startswith(R_HEADS):
      return b[len(R_HEADS):]
    return None

  def IsRebaseInProgress(self):
    return (os.path.exists(self.work_git.GetDotgitPath('rebase-apply')) or
            os.path.exists(self.work_git.GetDotgitPath('rebase-merge')) or
            os.path.exists(os.path.join(self.worktree, '.dotest')))

  def IsDirty(self, consider_untracked=True):
    """Is the working directory modified in some way?
    """
    self.work_git.update_index('-q',
                               '--unmerged',
                               '--ignore-missing',
                               '--refresh')
    if self.work_git.DiffZ('diff-index', '-M', '--cached', HEAD):
      return True
    if self.work_git.DiffZ('diff-files'):
      return True
    if consider_untracked and self.work_git.LsOthers():
      return True
    return False

  _userident_name = None
  _userident_email = None

  @property
  def UserName(self):
    """Obtain the user's personal name.
    """
    if self._userident_name is None:
      self._LoadUserIdentity()
    return self._userident_name

  @property
  def UserEmail(self):
    """Obtain the user's email address.  This is very likely
       to be their Gerrit login.
    """
    if self._userident_email is None:
      self._LoadUserIdentity()
    return self._userident_email

  def _LoadUserIdentity(self):
    u = self.bare_git.var('GIT_COMMITTER_IDENT')
    m = re.compile("^(.*) <([^>]*)> ").match(u)
    if m:
      self._userident_name = m.group(1)
      self._userident_email = m.group(2)
    else:
      self._userident_name = ''
      self._userident_email = ''

  def GetRemote(self, name):
    """Get the configuration for a single remote.
    """
    return self.config.GetRemote(name)

  def GetBranch(self, name):
    """Get the configuration for a single branch.
    """
    return self.config.GetBranch(name)

  def GetBranches(self):
    """Get all existing local branches.
    """
    current = self.CurrentBranch
    all_refs = self._allrefs
    heads = {}

    for name, ref_id in all_refs.items():
      if name.startswith(R_HEADS):
        name = name[len(R_HEADS):]
        b = self.GetBranch(name)
        b.current = name == current
        b.published = None
        b.revision = ref_id
        heads[name] = b

    for name, ref_id in all_refs.items():
      if name.startswith(R_PUB):
        name = name[len(R_PUB):]
        b = heads.get(name)
        if b:
          b.published = ref_id

    return heads

  def MatchesGroups(self, manifest_groups):
    """Returns true if the manifest groups specified at init should cause
       this project to be synced.
       Prefixing a manifest group with "-" inverts the meaning of a group.
       All projects are implicitly labelled with "all".

       labels are resolved in order.  In the example case of
       project_groups: "all,group1,group2"
       manifest_groups: "-group1,group2"
       the project will be matched.

       The special manifest group "default" will match any project that
       does not have the special project group "notdefault"
    """
    expanded_manifest_groups = manifest_groups or ['default']
    expanded_project_groups = ['all'] + (self.groups or [])
    if 'notdefault' not in expanded_project_groups:
      expanded_project_groups += ['default']

    matched = False
    for group in expanded_manifest_groups:
      if group.startswith('-') and group[1:] in expanded_project_groups:
        matched = False
      elif group in expanded_project_groups:
        matched = True

    return matched

# Status Display ##
  def UncommitedFiles(self, get_all=True):
    """Returns a list of strings, uncommitted files in the git tree.

    Args:
      get_all: a boolean, if True - get information about all different
               uncommitted files. If False - return as soon as any kind of
               uncommitted files is detected.
    """
    details = []
    self.work_git.update_index('-q',
                               '--unmerged',
                               '--ignore-missing',
                               '--refresh')
    if self.IsRebaseInProgress():
      details.append("rebase in progress")
      if not get_all:
        return details

    changes = self.work_git.DiffZ('diff-index', '--cached', HEAD).keys()
    if changes:
      details.extend(changes)
      if not get_all:
        return details

    changes = self.work_git.DiffZ('diff-files').keys()
    if changes:
      details.extend(changes)
      if not get_all:
        return details

    changes = self.work_git.LsOthers()
    if changes:
      details.extend(changes)

    return details

  def HasChanges(self):
    """Returns true if there are uncommitted changes.
    """
    if self.UncommitedFiles(get_all=False):
      return True
    else:
      return False

  def PrintWorkTreeStatus(self, output_redir=None, quiet=False):
    """Prints the status of the repository to stdout.

    Args:
      output_redir: If specified, redirect the output to this object.
      quiet:  If True then only print the project name.  Do not print
              the modified files, branch name, etc.
    """
    if not platform_utils.isdir(self.worktree):
      if output_redir is None:
        output_redir = sys.stdout
      print(file=output_redir)
      print('project %s/' % self.relpath, file=output_redir)
      print('  missing (run "repo sync")', file=output_redir)
      return

    self.work_git.update_index('-q',
                               '--unmerged',
                               '--ignore-missing',
                               '--refresh')
    rb = self.IsRebaseInProgress()
    di = self.work_git.DiffZ('diff-index', '-M', '--cached', HEAD)
    df = self.work_git.DiffZ('diff-files')
    do = self.work_git.LsOthers()
    if not rb and not di and not df and not do and not self.CurrentBranch:
      return 'CLEAN'

    out = StatusColoring(self.config)
    if output_redir is not None:
      out.redirect(output_redir)
    out.project('project %-40s', self.relpath + '/ ')

    if quiet:
      out.nl()
      return 'DIRTY'

    branch = self.CurrentBranch
    if branch is None:
      out.nobranch('(*** NO BRANCH ***)')
    else:
      out.branch('branch %s', branch)
    out.nl()

    if rb:
      out.important('prior sync failed; rebase still in progress')
      out.nl()

    paths = list()
    paths.extend(di.keys())
    paths.extend(df.keys())
    paths.extend(do)

    for p in sorted(set(paths)):
      try:
        i = di[p]
      except KeyError:
        i = None

      try:
        f = df[p]
      except KeyError:
        f = None

      if i:
        i_status = i.status.upper()
      else:
        i_status = '-'

      if f:
        f_status = f.status.lower()
      else:
        f_status = '-'

      if i and i.src_path:
        line = ' %s%s\t%s => %s (%s%%)' % (i_status, f_status,
                                           i.src_path, p, i.level)
      else:
        line = ' %s%s\t%s' % (i_status, f_status, p)

      if i and not f:
        out.added('%s', line)
      elif (i and f) or (not i and f):
        out.changed('%s', line)
      elif not i and not f:
        out.untracked('%s', line)
      else:
        out.write('%s', line)
      out.nl()

    return 'DIRTY'

  def PrintWorkTreeDiff(self, absolute_paths=False):
    """Prints the status of the repository to stdout.
    """
    out = DiffColoring(self.config)
    cmd = ['diff']
    if out.is_on:
      cmd.append('--color')
    cmd.append(HEAD)
    if absolute_paths:
      cmd.append('--src-prefix=a/%s/' % self.relpath)
      cmd.append('--dst-prefix=b/%s/' % self.relpath)
    cmd.append('--')
    try:
      p = GitCommand(self,
                     cmd,
                     capture_stdout=True,
                     capture_stderr=True)
    except GitError as e:
      out.nl()
      out.project('project %s/' % self.relpath)
      out.nl()
      out.fail('%s', str(e))
      out.nl()
      return False
    has_diff = False
    for line in p.process.stdout:
      if not hasattr(line, 'encode'):
        line = line.decode()
      if not has_diff:
        out.nl()
        out.project('project %s/' % self.relpath)
        out.nl()
        has_diff = True
      print(line[:-1])
    return p.Wait() == 0

# Publish / Upload ##
  def WasPublished(self, branch, all_refs=None):
    """Was the branch published (uploaded) for code review?
       If so, returns the SHA-1 hash of the last published
       state for the branch.
    """
    key = R_PUB + branch
    if all_refs is None:
      try:
        return self.bare_git.rev_parse(key)
      except GitError:
        return None
    else:
      try:
        return all_refs[key]
      except KeyError:
        return None

  def CleanPublishedCache(self, all_refs=None):
    """Prunes any stale published refs.
    """
    if all_refs is None:
      all_refs = self._allrefs
    heads = set()
    canrm = {}
    for name, ref_id in all_refs.items():
      if name.startswith(R_HEADS):
        heads.add(name)
      elif name.startswith(R_PUB):
        canrm[name] = ref_id

    for name, ref_id in canrm.items():
      n = name[len(R_PUB):]
      if R_HEADS + n not in heads:
        self.bare_git.DeleteRef(name, ref_id)

  def GetUploadableBranches(self, selected_branch=None):
    """List any branches which can be uploaded for review.
    """
    heads = {}
    pubed = {}

    for name, ref_id in self._allrefs.items():
      if name.startswith(R_HEADS):
        heads[name[len(R_HEADS):]] = ref_id
      elif name.startswith(R_PUB):
        pubed[name[len(R_PUB):]] = ref_id

    ready = []
    for branch, ref_id in heads.items():
      if branch in pubed and pubed[branch] == ref_id:
        continue
      if selected_branch and branch != selected_branch:
        continue

      rb = self.GetUploadableBranch(branch)
      if rb:
        ready.append(rb)
    return ready

  def GetUploadableBranch(self, branch_name):
    """Get a single uploadable branch, or None.
    """
    branch = self.GetBranch(branch_name)
    base = branch.LocalMerge
    if branch.LocalMerge:
      rb = ReviewableBranch(self, branch, base)
      if rb.commits:
        return rb
    return None

  def GetPushableBranch(self, branch_name):
    """Get a single pushable branch, or None.
    """
    branch = self.GetBranch(branch_name)
    base = branch.LocalMerge
    if branch.LocalMerge:
      rb = ReviewableBranch(self, branch, base)
      return rb
    return None

  def  UploadNoReview(self, opt, peoples, branch=None):
    """If not review server defined, uploads the named branch directly to git server.
    """
    if branch is None:
      branch = self.CurrentBranch
    if branch is None:
      raise GitError('not currently on a branch')

    branch = self.GetBranch(branch)

    if not branch.LocalMerge:
      raise GitError('branch %s does not track a remote' % branch.name)

    # if not opt.ignore_review and branch.remote.review:
    #   raise GitError('remote %s has review url, use `repo upload` instead or use `repo push --`.' % branch.remote.name)

    if opt.new_branch:
      dest_branch = branch.name
    else:
      dest_branch = branch.merge

    if dest_branch.startswith(R_TAGS):
      raise GitError('Can not push to TAGS (%s)! Run repo push with --new flag to create new feature branch.' % dest_branch)
    if not dest_branch.startswith(R_HEADS):
      dest_branch = R_HEADS + dest_branch

    if not branch.remote.projectname:
      branch.remote.projectname = self.name
      branch.remote.Save()

    # save git config branch.name.merge
    if opt.new_branch:
      branch.merge = dest_branch
      branch.Save()

    ref_spec = '%s:%s' % (R_HEADS + branch.name, dest_branch)
    pushurl = self.manifest.manifestProject.config.GetString('repo.%s.pushurl'
              % branch.remote.name)
    if not pushurl:
      pushurl = self.manifest.manifestProject.config.GetString('repo.pushurl')
    if not pushurl:
      html_url = self._UserUrl().rstrip('/') + '/'
      namespace = self._GiteeNamespace(html_url, type='upload')
      pushurl = ':'.join([GITEE_SSH, namespace])
      self.manifest.manifestProject.config.SetString('repo.pushurl', pushurl)
      # pushurl = branch.remote.name
    pushurl = pushurl.rstrip('/') + '/' + self.name
      # remote = self.manifest.remotes.get(branch.remote.name)
      # if remote and remote.autodotgit is not False:
      #   pushurl += ".git"

    cmd = ['push']
    if opt.force:
      cmd.append('--force')
    cmd.append(pushurl)
    cmd.append(ref_spec)

    if GitCommand(self, cmd).Wait() != 0:
      raise UploadError('Upload failed')

    if branch.LocalMerge and branch.LocalMerge.startswith('refs/remotes'):
      self.bare_git.UpdateRef(branch.LocalMerge,
                              R_HEADS + branch.name)

  def PullRequest(self, opt, branch, peoples):
    """example test
    curl -X POST --header 'Content-Type: application/json;charset=UTF-8'
    'https://gitee.com/api/v5/repos/MarineJ/AS-Test/pulls'
    -d '{"access_token":"token",
    "title":"test_repo","head":"repo_test","base":"master"}'
    use remote.url to generate post_url
    """
    if opt.dest_branch:
      base_branch = opt.dest_branch
    elif self.revisionExpr:
      base_branch = self.revisionExpr
      print("project revisionExpr %s" % self.revisionExpr)
    else:
      print("default revisionExpr %s" % self.manifest.default.revisionExpr)
      base_branch = self.manifest.default.revisionExpr
    # print("your config reviewers are: %s" % peoples)
    namespace = self._GiteeNamespace()
    token = self.manifest.manifestProject.config.GetString('repo.token')
    if not token:
      token = GitConfig.ForUser().GetString('repo.token')
      if not token:
        raise PullRequestError('repo.token is None, Please set it before pushing, you need `repo config -h`')
    post_url = '/'.join([GITEE_REPO_API, namespace, self.name, 'pulls'])
    pushurl = self.manifest.manifestProject.config.GetString('repo.pushurl')
    if not pushurl:
      head = branch
    else:
      pushurl = pushurl.rstrip('/') + '/'
      head = ':'.join([self._GiteeNamespace(pushurl), branch])
    payload = {"access_token": token, "title": opt.title or 'Gitee Review - {}'.format(branch), "head": head,
               "base": base_branch, "assignees": ','.join(peoples)}
    if opt.content:
      payload['body'] = opt.content
    try:
      r = requests.post(post_url, json=payload, timeout=TIMEOUT)
    except requests.exceptions.RequestException as e:
      raise PullRequestError('requests error: %s' % e)

    r_j = r.json()
    if r.status_code != 201:
      error_message = r_j['message']
      raise PullRequestError('pull request %s  code :%s  error: %s' %
                             (post_url, r.status_code, error_message))
    return r_j['html_url']

  def ForkProject(self, token=None):
    if not token:
      token = self.manifest.manifestProject.config.GetString('repo.token')
      if not token:
        token = GitConfig.ForUser().GetString('repo.token')
        if not token:
          raise ForkProjectError('repo.token is None, Please set it before pushing, you need `repo config -h`')
    namespace = self._GiteeNamespace(type='forkproject')
    post_url = '/'.join([GITEE_REPO_API, namespace, self.name, 'forks'])
    payload = {"access_token": token}
    try:
      r = requests.post(post_url, json=payload, timeout=TIMEOUT)
    except requests.exceptions.RequestException as e:
      raise ForkProjectError('requests error: %s' % e)
    msg = r.json()
    return r.status_code, msg

  def _GiteeNamespace(self, url=None, type='pullrequest'):
    check_url = url if url is not None else self.remote.url
    regex1 = r'^git@gitee.com:(.*?)/.*'
    regex2 = r'^https://.*gitee.com/(.*?)/.*'
    name1 = re.match(regex1, check_url)
    name2 = re.match(regex2, check_url)
    if name1:
      return name1.group(1)
    elif name2:
      return name2.group(1)
    else:
      if type == 'pullrequest':
        raise PullRequestError("remote.url: %s doesn't belong to gitee" % check_url)
      elif type == 'forkproject':
        raise ForkProjectError("remote.url: %s doesn't belong to gitee" % check_url)
      else:
        raise UploadError("remote.url: %s doesn't belong to gitee" % check_url)

  def _UserUrl(self):
    token = self.manifest.manifestProject.config.GetString('repo.token')
    if not token:
      token = GitConfig.ForUser().GetString('repo.token')
      if not token:
        raise UploadError('repo.token is None, Please set it, you need `repo config -h`')
    payload = {'access_token': token}
    try:
      r = requests.get(GITEE_USER_API, params=payload, timeout=TIMEOUT)
    except requests.exceptions.RequestException as e:
      raise UploadError('requests error: %s' % e)
    if r.status_code != 200:
      raise UploadError('repo.token is Error, Please reset')
    return r.json()['html_url']

  def UploadForReview(self, branch=None,
                      people=([], []),
                      dryrun=False,
                      auto_topic=False,
                      hashtags=(),
                      labels=(),
                      private=False,
                      notify=None,
                      wip=False,
                      dest_branch=None,
                      validate_certs=True,
                      push_options=None):
    """Uploads the named branch for code review.
    """
    if branch is None:
      branch = self.CurrentBranch
    if branch is None:
      raise GitError('not currently on a branch')

    branch = self.GetBranch(branch)
    if not branch.LocalMerge:
      raise GitError('branch %s does not track a remote' % branch.name)
    if not branch.remote.review:
      raise GitError('remote %s has no review url' % branch.remote.name)

    if dest_branch is None:
      dest_branch = self.dest_branch
    if dest_branch is None:
      dest_branch = branch.merge
    if not dest_branch.startswith(R_HEADS):
      dest_branch = R_HEADS + dest_branch

    if not branch.remote.projectname:
      branch.remote.projectname = self.name
      branch.remote.Save()

    url = branch.remote.ReviewUrl(self.UserEmail, validate_certs)
    if url is None:
      raise UploadError('review not configured')
    cmd = ['push']
    if dryrun:
      cmd.append('-n')

    if url.startswith('ssh://'):
      cmd.append('--receive-pack=gerrit receive-pack')

    for push_option in (push_options or []):
      cmd.append('-o')
      cmd.append(push_option)

    cmd.append(url)

    if dest_branch.startswith(R_HEADS):
      dest_branch = dest_branch[len(R_HEADS):]

    ref_spec = '%s:refs/for/%s' % (R_HEADS + branch.name, dest_branch)
    opts = []
    if auto_topic:
      opts += ['topic=' + branch.name]
    opts += ['t=%s' % p for p in hashtags]
    opts += ['l=%s' % p for p in labels]

    opts += ['r=%s' % p for p in people[0]]
    opts += ['cc=%s' % p for p in people[1]]
    if notify:
      opts += ['notify=' + notify]
    if private:
      opts += ['private']
    if wip:
      opts += ['wip']
    if opts:
      ref_spec = ref_spec + '%' + ','.join(opts)
    cmd.append(ref_spec)

    if GitCommand(self, cmd, bare=True).Wait() != 0:
      raise UploadError('Upload failed')

    msg = "posted to %s for %s" % (branch.remote.review, dest_branch)
    self.bare_git.UpdateRef(R_PUB + branch.name,
                            R_HEADS + branch.name,
                            message=msg)

# Sync ##
  def _ExtractArchive(self, tarpath, path=None):
    """Extract the given tar on its current location

    Args:
        - tarpath: The path to the actual tar file

    """
    try:
      with tarfile.open(tarpath, 'r') as tar:
        tar.extractall(path=path)
        return True
    except (IOError, tarfile.TarError) as e:
      _error("Cannot extract archive %s: %s", tarpath, str(e))
    return False

  def Sync_NetworkHalf(self,
                       quiet=False,
                       verbose=False,
                       is_new=None,
                       current_branch_only=False,
                       force_sync=False,
                       clone_bundle=True,
                       tags=True,
                       archive=False,
                       optimized_fetch=False,
                       retry_fetches=0,
                       prune=False,
                       submodules=False,
                       clone_filter=None,
                       use_mirror=False):
    """Perform only the network IO portion of the sync process.
       Local working directory/branch state is not affected.
    """
    if archive and not isinstance(self, MetaProject):
      if self.remote.url.startswith(('http://', 'https://')):
        _error("%s: Cannot fetch archives from http/https remotes.", self.name)
        return False

      name = self.relpath.replace('\\', '/')
      name = name.replace('/', '_')
      tarpath = '%s.tar' % name
      topdir = self.manifest.topdir

      try:
        self._FetchArchive(tarpath, cwd=topdir)
      except GitError as e:
        _error('%s', e)
        return False

      # From now on, we only need absolute tarpath
      tarpath = os.path.join(topdir, tarpath)

      if not self._ExtractArchive(tarpath, path=topdir):
        return False
      try:
        platform_utils.remove(tarpath)
      except OSError as e:
        _warn("Cannot remove archive %s: %s", tarpath, str(e))
      self._CopyAndLinkFiles()
      return True
    if is_new is None:
      is_new = not self.Exists
    if is_new:
      self._InitGitDir(force_sync=force_sync, quiet=quiet)
    else:
      self._UpdateHooks(quiet=quiet)
    self._InitRemote()

    if is_new:
      alt = os.path.join(self.gitdir, 'objects/info/alternates')
      try:
        with open(alt) as fd:
          # This works for both absolute and relative alternate directories.
          alt_dir = os.path.join(self.objdir, 'objects', fd.readline().rstrip())
      except IOError:
        alt_dir = None
    else:
      alt_dir = None

    if (clone_bundle
            and alt_dir is None
            and self._ApplyCloneBundle(initial=is_new, quiet=quiet, verbose=verbose)):
      is_new = False

    if not current_branch_only:
      if self.sync_c:
        current_branch_only = True
      elif not self.manifest._loaded:
        # Manifest cannot check defaults until it syncs.
        current_branch_only = False
      elif self.manifest.default.sync_c:
        current_branch_only = True

    if not self.sync_tags:
      tags = False

    if self.clone_depth:
      depth = self.clone_depth
    else:
      depth = self.manifest.manifestProject.config.GetString('repo.depth')

    # See if we can skip the network fetch entirely.
    if not (optimized_fetch and
            (ID_RE.match(self.revisionExpr) and
             self._CheckForImmutableRevision())):
      if not self._RemoteFetch(
              initial=is_new, quiet=quiet, verbose=verbose, alt_dir=alt_dir,
              current_branch_only=current_branch_only,
              tags=tags, prune=prune, depth=depth,
              submodules=submodules, force_sync=force_sync,
              clone_filter=clone_filter, retry_fetches=retry_fetches, use_mirror=use_mirror):
        return False

    mp = self.manifest.manifestProject
    dissociate = mp.config.GetBoolean('repo.dissociate')
    if dissociate:
      alternates_file = os.path.join(self.gitdir, 'objects/info/alternates')
      if os.path.exists(alternates_file):
        cmd = ['repack', '-a', '-d']
        if GitCommand(self, cmd, bare=True).Wait() != 0:
          return False
        platform_utils.remove(alternates_file)

    if self.worktree:
      self._InitMRef()
    else:
      self._InitMirrorHead()
      try:
        platform_utils.remove(os.path.join(self.gitdir, 'FETCH_HEAD'))
      except OSError:
        pass
    return True

  def PostRepoUpgrade(self):
    self._InitHooks()

  def _CopyAndLinkFiles(self):
    if self.manifest.isGitcClient:
      return
    for copyfile in self.copyfiles:
      copyfile._Copy()
    for linkfile in self.linkfiles:
      linkfile._Link()

  def GetCommitRevisionId(self):
    """Get revisionId of a commit.

    Use this method instead of GetRevisionId to get the id of the commit rather
    than the id of the current git object (for example, a tag)

    """
    if not self.revisionExpr.startswith(R_TAGS):
      return self.GetRevisionId(self._allrefs)

    try:
      return self.bare_git.rev_list(self.revisionExpr, '-1')[0]
    except GitError:
      raise ManifestInvalidRevisionError('revision %s in %s not found' %
                                         (self.revisionExpr, self.name))

  def GetRevisionId(self, all_refs=None):
    if self.revisionId:
      return self.revisionId

    rem = self.GetRemote(self.remote.name)
    rev = rem.ToLocal(self.revisionExpr)

    if all_refs is not None and rev in all_refs:
      return all_refs[rev]

    try:
      return self.bare_git.rev_parse('--verify', '%s^0' % rev)
    except GitError:
      raise ManifestInvalidRevisionError('revision %s in %s not found' %
                                         (self.revisionExpr, self.name))

  def Sync_LocalHalf(self, syncbuf, force_sync=False, submodules=False):
    """Perform only the local IO portion of the sync process.
       Network access is not required.
    """
    if not os.path.exists(self.gitdir):
      syncbuf.fail(self,
                   'Cannot checkout %s due to missing network sync; Run '
                   '`repo sync -n %s` first.' %
                   (self.name, self.name))
      return

    self._InitWorkTree(force_sync=force_sync, submodules=submodules)
    all_refs = self.bare_ref.all
    self.CleanPublishedCache(all_refs)
    revid = self.GetRevisionId(all_refs)

    def _doff():
      self._FastForward(revid)
      self._CopyAndLinkFiles()

    def _dosubmodules():
      self._SyncSubmodules(quiet=True)

    head = self.work_git.GetHead()
    if head.startswith(R_HEADS):
      branch = head[len(R_HEADS):]
      try:
        head = all_refs[head]
      except KeyError:
        head = None
    else:
      branch = None

    if branch is None or syncbuf.detach_head:
      # Currently on a detached HEAD.  The user is assumed to
      # not have any local modifications worth worrying about.
      #
      if self.IsRebaseInProgress():
        syncbuf.fail(self, _PriorSyncFailedError())
        return

      if head == revid:
        # No changes; don't do anything further.
        # Except if the head needs to be detached
        #
        if not syncbuf.detach_head:
          # The copy/linkfile config may have changed.
          self._CopyAndLinkFiles()
          return
      else:
        lost = self._revlist(not_rev(revid), HEAD)
        if lost:
          syncbuf.info(self, "discarding %d commits", len(lost))

      try:
        self._Checkout(revid, quiet=True)
        if submodules:
          self._SyncSubmodules(quiet=True)
      except GitError as e:
        syncbuf.fail(self, e)
        return
      self._CopyAndLinkFiles()
      return

    if head == revid:
      # No changes; don't do anything further.
      #
      # The copy/linkfile config may have changed.
      self._CopyAndLinkFiles()
      return

    branch = self.GetBranch(branch)

    if not branch.LocalMerge:
      # The current branch has no tracking configuration.
      # Jump off it to a detached HEAD.
      #
      syncbuf.info(self,
                   "leaving %s; does not track upstream",
                   branch.name)
      try:
        self._Checkout(revid, quiet=True)
        if submodules:
          self._SyncSubmodules(quiet=True)
      except GitError as e:
        syncbuf.fail(self, e)
        return
      self._CopyAndLinkFiles()
      return

    upstream_gain = self._revlist(not_rev(HEAD), revid)

    # See if we can perform a fast forward merge.  This can happen if our
    # branch isn't in the exact same state as we last published.
    try:
      self.work_git.merge_base('--is-ancestor', HEAD, revid)
      # Skip the published logic.
      pub = False
    except GitError:
      pub = self.WasPublished(branch.name, all_refs)

    if pub:
      not_merged = self._revlist(not_rev(revid), pub)
      if not_merged:
        if upstream_gain:
          # The user has published this branch and some of those
          # commits are not yet merged upstream.  We do not want
          # to rewrite the published commits so we punt.
          #
          syncbuf.fail(self,
                       "branch %s is published (but not merged) and is now "
                       "%d commits behind" % (branch.name, len(upstream_gain)))
        return
      elif pub == head:
        # All published commits are merged, and thus we are a
        # strict subset.  We can fast-forward safely.
        #
        syncbuf.later1(self, _doff)
        if submodules:
          syncbuf.later1(self, _dosubmodules)
        return

    # Examine the local commits not in the remote.  Find the
    # last one attributed to this user, if any.
    #
    local_changes = self._revlist(not_rev(revid), HEAD, format='%H %ce')
    last_mine = None
    cnt_mine = 0
    for commit in local_changes:
      commit_id, committer_email = commit.split(' ', 1)
      if committer_email == self.UserEmail:
        last_mine = commit_id
        cnt_mine += 1

    if not upstream_gain and cnt_mine == len(local_changes):
      return

    if self.IsDirty(consider_untracked=False):
      syncbuf.fail(self, _DirtyError())
      return

    # If the upstream switched on us, warn the user.
    #
    if branch.merge != self.revisionExpr:
      if branch.merge and self.revisionExpr:
        syncbuf.info(self,
                     'manifest switched %s...%s',
                     branch.merge,
                     self.revisionExpr)
      elif branch.merge:
        syncbuf.info(self,
                     'manifest no longer tracks %s',
                     branch.merge)

    if cnt_mine < len(local_changes):
      # Upstream rebased.  Not everything in HEAD
      # was created by this user.
      #
      syncbuf.info(self,
                   "discarding %d commits removed from upstream",
                   len(local_changes) - cnt_mine)

    branch.remote = self.GetRemote(self.remote.name)
    if not ID_RE.match(self.revisionExpr):
      # in case of manifest sync the revisionExpr might be a SHA1
      branch.merge = self.revisionExpr
      if not branch.merge.startswith('refs/'):
        branch.merge = R_HEADS + branch.merge
    branch.Save()

    if cnt_mine > 0 and self.rebase:
      def _docopyandlink():
        self._CopyAndLinkFiles()

      def _dorebase():
        self._Rebase(upstream='%s^1' % last_mine, onto=revid)
      syncbuf.later2(self, _dorebase)
      if submodules:
        syncbuf.later2(self, _dosubmodules)
      syncbuf.later2(self, _docopyandlink)
    elif local_changes:
      try:
        self._ResetHard(revid)
        if submodules:
          self._SyncSubmodules(quiet=True)
        self._CopyAndLinkFiles()
      except GitError as e:
        syncbuf.fail(self, e)
        return
    else:
      syncbuf.later1(self, _doff)
      if submodules:
        syncbuf.later1(self, _dosubmodules)

  def AddCopyFile(self, src, dest, topdir):
    """Mark |src| for copying to |dest| (relative to |topdir|).

    No filesystem changes occur here.  Actual copying happens later on.

    Paths should have basic validation run on them before being queued.
    Further checking will be handled when the actual copy happens.
    """
    self.copyfiles.append(_CopyFile(self.worktree, src, topdir, dest))

  def AddLinkFile(self, src, dest, topdir):
    """Mark |dest| to create a symlink (relative to |topdir|) pointing to |src|.

    No filesystem changes occur here.  Actual linking happens later on.

    Paths should have basic validation run on them before being queued.
    Further checking will be handled when the actual link happens.
    """
    self.linkfiles.append(_LinkFile(self.worktree, src, topdir, dest))

  def AddAnnotation(self, name, value, keep):
    self.annotations.append(_Annotation(name, value, keep))

  def DownloadPatchSet(self, change_id, patch_id):
    """Download a single patch set of a single change to FETCH_HEAD.
    """
    remote = self.GetRemote(self.remote.name)

    cmd = ['fetch', remote.name]
    cmd.append('refs/changes/%2.2d/%d/%d'
               % (change_id % 100, change_id, patch_id))
    if GitCommand(self, cmd, bare=True).Wait() != 0:
      return None
    return DownloadedChange(self,
                            self.GetRevisionId(),
                            change_id,
                            patch_id,
                            self.bare_git.rev_parse('FETCH_HEAD'))

  def DeleteWorktree(self, quiet=False, force=False):
    """Delete the source checkout and any other housekeeping tasks.

    This currently leaves behind the internal .repo/ cache state.  This helps
    when switching branches or manifest changes get reverted as we don't have
    to redownload all the git objects.  But we should do some GC at some point.

    Args:
      quiet: Whether to hide normal messages.
      force: Always delete tree even if dirty.

    Returns:
      True if the worktree was completely cleaned out.
    """
    if self.IsDirty():
      if force:
        print('warning: %s: Removing dirty project: uncommitted changes lost.' %
              (self.relpath,), file=sys.stderr)
      else:
        print('error: %s: Cannot remove project: uncommitted changes are '
              'present.\n' % (self.relpath,), file=sys.stderr)
        return False

    if not quiet:
      print('%s: Deleting obsolete checkout.' % (self.relpath,))

    # Unlock and delink from the main worktree.  We don't use git's worktree
    # remove because it will recursively delete projects -- we handle that
    # ourselves below.  https://crbug.com/git/48
    if self.use_git_worktrees:
      needle = platform_utils.realpath(self.gitdir)
      # Find the git worktree commondir under .repo/worktrees/.
      output = self.bare_git.worktree('list', '--porcelain').splitlines()[0]
      assert output.startswith('worktree '), output
      commondir = output[9:]
      # Walk each of the git worktrees to see where they point.
      configs = os.path.join(commondir, 'worktrees')
      for name in os.listdir(configs):
        gitdir = os.path.join(configs, name, 'gitdir')
        with open(gitdir) as fp:
          relpath = fp.read().strip()
        # Resolve the checkout path and see if it matches this project.
        fullpath = platform_utils.realpath(os.path.join(configs, name, relpath))
        if fullpath == needle:
          platform_utils.rmtree(os.path.join(configs, name))

    # Delete the .git directory first, so we're less likely to have a partially
    # working git repository around. There shouldn't be any git projects here,
    # so rmtree works.

    # Try to remove plain files first in case of git worktrees.  If this fails
    # for any reason, we'll fall back to rmtree, and that'll display errors if
    # it can't remove things either.
    try:
      platform_utils.remove(self.gitdir)
    except OSError:
      pass
    try:
      platform_utils.rmtree(self.gitdir)
    except OSError as e:
      if e.errno != errno.ENOENT:
        print('error: %s: %s' % (self.gitdir, e), file=sys.stderr)
        print('error: %s: Failed to delete obsolete checkout; remove manually, '
              'then run `repo sync -l`.' % (self.relpath,), file=sys.stderr)
        return False

    # Delete everything under the worktree, except for directories that contain
    # another git project.
    dirs_to_remove = []
    failed = False
    for root, dirs, files in platform_utils.walk(self.worktree):
      for f in files:
        path = os.path.join(root, f)
        try:
          platform_utils.remove(path)
        except OSError as e:
          if e.errno != errno.ENOENT:
            print('error: %s: Failed to remove: %s' % (path, e), file=sys.stderr)
            failed = True
      dirs[:] = [d for d in dirs
                 if not os.path.lexists(os.path.join(root, d, '.git'))]
      dirs_to_remove += [os.path.join(root, d) for d in dirs
                         if os.path.join(root, d) not in dirs_to_remove]
    for d in reversed(dirs_to_remove):
      if platform_utils.islink(d):
        try:
          platform_utils.remove(d)
        except OSError as e:
          if e.errno != errno.ENOENT:
            print('error: %s: Failed to remove: %s' % (d, e), file=sys.stderr)
            failed = True
      elif not platform_utils.listdir(d):
        try:
          platform_utils.rmdir(d)
        except OSError as e:
          if e.errno != errno.ENOENT:
            print('error: %s: Failed to remove: %s' % (d, e), file=sys.stderr)
            failed = True
    if failed:
      print('error: %s: Failed to delete obsolete checkout.' % (self.relpath,),
            file=sys.stderr)
      print('       Remove manually, then run `repo sync -l`.', file=sys.stderr)
      return False

    # Try deleting parent dirs if they are empty.
    path = self.worktree
    while path != self.manifest.topdir:
      try:
        platform_utils.rmdir(path)
      except OSError as e:
        if e.errno != errno.ENOENT:
          break
      path = os.path.dirname(path)

    return True

# Branch Management ##
  def StartBranch(self, name, branch_merge='', revision=None):
    """Create a new branch off the manifest's revision.
    """
    if not branch_merge:
      branch_merge = self.revisionExpr
    head = self.work_git.GetHead()
    if head == (R_HEADS + name):
      return True

    all_refs = self.bare_ref.all
    if R_HEADS + name in all_refs:
      return GitCommand(self,
                        ['checkout', name, '--'],
                        capture_stdout=True,
                        capture_stderr=True).Wait() == 0

    branch = self.GetBranch(name)
    branch.remote = self.GetRemote(self.remote.name)
    branch.merge = branch_merge
    if not branch.merge.startswith('refs/') and not ID_RE.match(branch_merge):
      branch.merge = R_HEADS + branch_merge

    if revision is None:
      revid = self.GetRevisionId(all_refs)
    else:
      revid = self.work_git.rev_parse(revision)

    if head.startswith(R_HEADS):
      try:
        head = all_refs[head]
      except KeyError:
        head = None
    if revid and head and revid == head:
      ref = R_HEADS + name
      self.work_git.update_ref(ref, revid)
      self.work_git.symbolic_ref(HEAD, ref)
      branch.Save()
      return True

    if GitCommand(self,
                  ['checkout', '-b', branch.name, revid],
                  capture_stdout=True,
                  capture_stderr=True).Wait() == 0:
      branch.Save()
      return True
    return False

  def CheckoutBranch(self, name):
    """Checkout a local topic branch.

        Args:
          name: The name of the branch to checkout.

        Returns:
          True if the checkout succeeded; False if it didn't; None if the branch
          didn't exist.
    """
    rev = R_HEADS + name
    head = self.work_git.GetHead()
    if head == rev:
      # Already on the branch
      #
      return True

    all_refs = self.bare_ref.all
    try:
      revid = all_refs[rev]
    except KeyError:
      # Branch does not exist in this project
      #
      return None

    if head.startswith(R_HEADS):
      try:
        head = all_refs[head]
      except KeyError:
        head = None

    if head == revid:
      # Same revision; just update HEAD to point to the new
      # target branch, but otherwise take no other action.
      #
      _lwrite(self.work_git.GetDotgitPath(subpath=HEAD),
              'ref: %s%s\n' % (R_HEADS, name))
      return True

    return GitCommand(self,
                      ['checkout', name, '--'],
                      capture_stdout=True,
                      capture_stderr=True).Wait() == 0

  def AbandonBranch(self, name):
    """Destroy a local topic branch.

    Args:
      name: The name of the branch to abandon.

    Returns:
      True if the abandon succeeded; False if it didn't; None if the branch
      didn't exist.
    """
    rev = R_HEADS + name
    all_refs = self.bare_ref.all
    if rev not in all_refs:
      # Doesn't exist
      return None

    head = self.work_git.GetHead()
    if head == rev:
      # We can't destroy the branch while we are sitting
      # on it.  Switch to a detached HEAD.
      #
      head = all_refs[head]

      revid = self.GetRevisionId(all_refs)
      if head == revid:
        _lwrite(self.work_git.GetDotgitPath(subpath=HEAD), '%s\n' % revid)
      else:
        self._Checkout(revid, quiet=True)

    return GitCommand(self,
                      ['branch', '-D', name],
                      capture_stdout=True,
                      capture_stderr=True).Wait() == 0

  def PruneHeads(self):
    """Prune any topic branches already merged into upstream.
    """
    cb = self.CurrentBranch
    kill = []
    left = self._allrefs
    for name in left.keys():
      if name.startswith(R_HEADS):
        name = name[len(R_HEADS):]
        if cb is None or name != cb:
          kill.append(name)

    rev = self.GetRevisionId(left)
    if cb is not None \
       and not self._revlist(HEAD + '...' + rev) \
       and not self.IsDirty(consider_untracked=False):
      self.work_git.DetachHead(HEAD)
      kill.append(cb)

    if kill:
      old = self.bare_git.GetHead()

      try:
        self.bare_git.DetachHead(rev)

        b = ['branch', '-d']
        b.extend(kill)
        b = GitCommand(self, b, bare=True,
                       capture_stdout=True,
                       capture_stderr=True)
        b.Wait()
      finally:
        if ID_RE.match(old):
          self.bare_git.DetachHead(old)
        else:
          self.bare_git.SetHead(old)
        left = self._allrefs

      for branch in kill:
        if (R_HEADS + branch) not in left:
          self.CleanPublishedCache()
          break

    if cb and cb not in kill:
      kill.append(cb)
    kill.sort()

    kept = []
    for branch in kill:
      if R_HEADS + branch in left:
        branch = self.GetBranch(branch)
        base = branch.LocalMerge
        if not base:
          base = rev
        kept.append(ReviewableBranch(self, branch, base))
    return kept

# Submodule Management ##
  def GetRegisteredSubprojects(self):
    result = []

    def rec(subprojects):
      if not subprojects:
        return
      result.extend(subprojects)
      for p in subprojects:
        rec(p.subprojects)
    rec(self.subprojects)
    return result

  def _GetSubmodules(self):
    # Unfortunately we cannot call `git submodule status --recursive` here
    # because the working tree might not exist yet, and it cannot be used
    # without a working tree in its current implementation.

    def get_submodules(gitdir, rev):
      # Parse .gitmodules for submodule sub_paths and sub_urls
      sub_paths, sub_urls = parse_gitmodules(gitdir, rev)
      if not sub_paths:
        return []
      # Run `git ls-tree` to read SHAs of submodule object, which happen to be
      # revision of submodule repository
      sub_revs = git_ls_tree(gitdir, rev, sub_paths)
      submodules = []
      for sub_path, sub_url in zip(sub_paths, sub_urls):
        try:
          sub_rev = sub_revs[sub_path]
        except KeyError:
          # Ignore non-exist submodules
          continue
        submodules.append((sub_rev, sub_path, sub_url))
      return submodules

    re_path = re.compile(r'^submodule\.(.+)\.path=(.*)$')
    re_url = re.compile(r'^submodule\.(.+)\.url=(.*)$')

    def parse_gitmodules(gitdir, rev):
      cmd = ['cat-file', 'blob', '%s:.gitmodules' % rev]
      try:
        p = GitCommand(None, cmd, capture_stdout=True, capture_stderr=True,
                       bare=True, gitdir=gitdir)
      except GitError:
        return [], []
      if p.Wait() != 0:
        return [], []

      gitmodules_lines = []
      fd, temp_gitmodules_path = tempfile.mkstemp()
      try:
        os.write(fd, p.stdout.encode('utf-8'))
        os.close(fd)
        cmd = ['config', '--file', temp_gitmodules_path, '--list']
        p = GitCommand(None, cmd, capture_stdout=True, capture_stderr=True,
                       bare=True, gitdir=gitdir)
        if p.Wait() != 0:
          return [], []
        gitmodules_lines = p.stdout.split('\n')
      except GitError:
        return [], []
      finally:
        platform_utils.remove(temp_gitmodules_path)

      names = set()
      paths = {}
      urls = {}
      for line in gitmodules_lines:
        if not line:
          continue
        m = re_path.match(line)
        if m:
          names.add(m.group(1))
          paths[m.group(1)] = m.group(2)
          continue
        m = re_url.match(line)
        if m:
          names.add(m.group(1))
          urls[m.group(1)] = m.group(2)
          continue
      names = sorted(names)
      return ([paths.get(name, '') for name in names],
              [urls.get(name, '') for name in names])

    def git_ls_tree(gitdir, rev, paths):
      cmd = ['ls-tree', rev, '--']
      cmd.extend(paths)
      try:
        p = GitCommand(None, cmd, capture_stdout=True, capture_stderr=True,
                       bare=True, gitdir=gitdir)
      except GitError:
        return []
      if p.Wait() != 0:
        return []
      objects = {}
      for line in p.stdout.split('\n'):
        if not line.strip():
          continue
        object_rev, object_path = line.split()[2:4]
        objects[object_path] = object_rev
      return objects

    try:
      rev = self.GetRevisionId()
    except GitError:
      return []
    return get_submodules(self.gitdir, rev)

  def GetDerivedSubprojects(self):
    result = []
    if not self.Exists:
      # If git repo does not exist yet, querying its submodules will
      # mess up its states; so return here.
      return result
    for rev, path, url in self._GetSubmodules():
      name = self.manifest.GetSubprojectName(self, path)
      relpath, worktree, gitdir, objdir = \
          self.manifest.GetSubprojectPaths(self, name, path)
      project = self.manifest.paths.get(relpath)
      if project:
        result.extend(project.GetDerivedSubprojects())
        continue

      if url.startswith('..'):
        url = urllib.parse.urljoin("%s/" % self.remote.url, url)
      remote = RemoteSpec(self.remote.name,
                          url=url,
                          pushUrl=self.remote.pushUrl,
                          review=self.remote.review,
                          revision=self.remote.revision)
      subproject = Project(manifest=self.manifest,
                           name=name,
                           remote=remote,
                           gitdir=gitdir,
                           objdir=objdir,
                           worktree=worktree,
                           relpath=relpath,
                           revisionExpr=rev,
                           revisionId=rev,
                           rebase=self.rebase,
                           groups=self.groups,
                           sync_c=self.sync_c,
                           sync_s=self.sync_s,
                           sync_tags=self.sync_tags,
                           parent=self,
                           is_derived=True)
      result.append(subproject)
      result.extend(subproject.GetDerivedSubprojects())
    return result

# Direct Git Commands ##
  def EnableRepositoryExtension(self, key, value='true', version=1):
    """Enable git repository extension |key| with |value|.

    Args:
      key: The extension to enabled.  Omit the "extensions." prefix.
      value: The value to use for the extension.
      version: The minimum git repository version needed.
    """
    # Make sure the git repo version is new enough already.
    found_version = self.config.GetInt('core.repositoryFormatVersion')
    if found_version is None:
      found_version = 0
    if found_version < version:
      self.config.SetString('core.repositoryFormatVersion', str(version))

    # Enable the extension!
    self.config.SetString('extensions.%s' % (key,), value)

  def _CheckForImmutableRevision(self):
    try:
      # if revision (sha or tag) is not present then following function
      # throws an error.
      self.bare_git.rev_parse('--verify', '%s^0' % self.revisionExpr)
      return True
    except GitError:
      # There is no such persistent revision. We have to fetch it.
      return False

  def _FetchArchive(self, tarpath, cwd=None):
    cmd = ['archive', '-v', '-o', tarpath]
    cmd.append('--remote=%s' % self.remote.url)
    cmd.append('--prefix=%s/' % self.relpath)
    cmd.append(self.revisionExpr)

    command = GitCommand(self, cmd, cwd=cwd,
                         capture_stdout=True,
                         capture_stderr=True)

    if command.Wait() != 0:
      raise GitError('git archive %s: %s' % (self.name, command.stderr))

  def _RemoteFetch(self, name=None,
                   current_branch_only=False,
                   initial=False,
                   quiet=False,
                   verbose=False,
                   alt_dir=None,
                   tags=True,
                   prune=False,
                   depth=None,
                   submodules=False,
                   force_sync=False,
                   clone_filter=None,
                   retry_fetches=2,
                   retry_sleep_initial_sec=4.0,
                   retry_exp_factor=2.0,
                   use_mirror=False):
    is_sha1 = False
    tag_name = None
    # The depth should not be used when fetching to a mirror because
    # it will result in a shallow repository that cannot be cloned or
    # fetched from.
    # The repo project should also never be synced with partial depth.
    if self.manifest.IsMirror or self.relpath == '.repo/repo':
      depth = None

    if depth:
      current_branch_only = True

    if ID_RE.match(self.revisionExpr) is not None:
      is_sha1 = True

    if current_branch_only:
      if self.revisionExpr.startswith(R_TAGS):
        # this is a tag and its sha1 value should never change
        tag_name = self.revisionExpr[len(R_TAGS):]

      if is_sha1 or tag_name is not None:
        if self._CheckForImmutableRevision():
          if verbose:
            print('Skipped fetching project %s (already have persistent ref)'
                  % self.name)
          return True
      if is_sha1 and not depth:
        # When syncing a specific commit and --depth is not set:
        # * if upstream is explicitly specified and is not a sha1, fetch only
        #   upstream as users expect only upstream to be fetch.
        #   Note: The commit might not be in upstream in which case the sync
        #   will fail.
        # * otherwise, fetch all branches to make sure we end up with the
        #   specific commit.
        if self.upstream:
          current_branch_only = not ID_RE.match(self.upstream)
        else:
          current_branch_only = False

    if not name:
      name = self.remote.name

    ssh_proxy = False
    remote = self.GetRemote(name)
    if remote.PreConnectFetch():
      ssh_proxy = True

    if initial:
      if alt_dir and 'objects' == os.path.basename(alt_dir):
        ref_dir = os.path.dirname(alt_dir)
        packed_refs = os.path.join(self.gitdir, 'packed-refs')
        remote = self.GetRemote(name)

        all_refs = self.bare_ref.all
        ids = set(all_refs.values())
        tmp = set()

        for r, ref_id in GitRefs(ref_dir).all.items():
          if r not in all_refs:
            if r.startswith(R_TAGS) or remote.WritesTo(r):
              all_refs[r] = ref_id
              ids.add(ref_id)
              continue

          if ref_id in ids:
            continue

          r = 'refs/_alt/%s' % ref_id
          all_refs[r] = ref_id
          ids.add(ref_id)
          tmp.add(r)

        tmp_packed_lines = []
        old_packed_lines = []

        for r in sorted(all_refs):
          line = '%s %s\n' % (all_refs[r], r)
          tmp_packed_lines.append(line)
          if r not in tmp:
            old_packed_lines.append(line)

        tmp_packed = ''.join(tmp_packed_lines)
        old_packed = ''.join(old_packed_lines)
        _lwrite(packed_refs, tmp_packed)
      else:
        alt_dir = None

    cmd = ['fetch']

    if clone_filter:
      git_require((2, 19, 0), fail=True, msg='partial clones')
      cmd.append('--filter=%s' % clone_filter)
      self.EnableRepositoryExtension('partialclone', self.remote.name)

    if depth:
      cmd.append('--depth=%s' % depth)
    else:
      # If this repo has shallow objects, then we don't know which refs have
      # shallow objects or not. Tell git to unshallow all fetched refs.  Don't
      # do this with projects that don't have shallow objects, since it is less
      # efficient.
      if os.path.exists(os.path.join(self.gitdir, 'shallow')):
        cmd.append('--depth=2147483647')

    if not verbose:
      cmd.append('--quiet')
    if not quiet and sys.stdout.isatty():
      cmd.append('--progress')
    if not self.worktree:
      cmd.append('--update-head-ok')

    if use_mirror and self.mirror_url:
      name_mirror = self.remote.name + '_mirror'
      cmd.append(name_mirror)
    else:
      cmd.append(name)

    if force_sync:
      cmd.append('--force')

    if prune:
      cmd.append('--prune')

    if submodules:
      cmd.append('--recurse-submodules=on-demand')

    spec = []
    if not current_branch_only:
      # Fetch whole repo
      spec.append(str((u'+refs/heads/*:') + remote.ToLocal('refs/heads/*')))
    elif tag_name is not None:
      spec.append('tag')
      spec.append(tag_name)

    if self.manifest.IsMirror and not current_branch_only:
      branch = None
    else:
      branch = self.revisionExpr
    if (not self.manifest.IsMirror and is_sha1 and depth
            and git_require((1, 8, 3))):
      # Shallow checkout of a specific commit, fetch from that commit and not
      # the heads only as the commit might be deeper in the history.
      spec.append(branch)
    else:
      if is_sha1:
        branch = self.upstream
      if branch is not None and branch.strip():
        if not branch.startswith('refs/'):
          branch = R_HEADS + branch
        spec.append(str((u'+%s:' % branch) + remote.ToLocal(branch)))

    # If mirroring repo and we cannot deduce the tag or branch to fetch, fetch
    # whole repo.
    if self.manifest.IsMirror and not spec:
      spec.append(str((u'+refs/heads/*:') + remote.ToLocal('refs/heads/*')))

    # If using depth then we should not get all the tags since they may
    # be outside of the depth.
    if not tags or depth:
      cmd.append('--no-tags')
    else:
      cmd.append('--tags')
      spec.append(str((u'+refs/tags/*:') + remote.ToLocal('refs/tags/*')))

    cmd.extend(spec)

    # At least one retry minimum due to git remote prune.
    retry_fetches = max(retry_fetches, 2)
    retry_cur_sleep = retry_sleep_initial_sec
    ok = prune_tried = False
    for try_n in range(retry_fetches):
      start_time = time.time()
      gitcmd = GitCommand(self, cmd, bare=True, ssh_proxy=ssh_proxy,
                          merge_output=True, capture_stdout=quiet)
      ret = gitcmd.Wait()
      fetch_time = round(time.time() - start_time )

      if ret == 0:
        if use_mirror and self.mirror_url:
          print("Fetch {0},{1} from mirror, use {2} s".format(self.name, self.revisionExpr, fetch_time))
        else:
          print("Fetch {0},{1} from source, use {2} s".format(self.name, self.revisionExpr, fetch_time))
      else:
        if use_mirror and self.mirror_url:
          print("Fetch {0},{1} from mirror failed {2}, use {3} s".format(self.name, self.revisionExpr, ret, fetch_time))
        else:
          print("Fetch {0},{1} from source failed {2}, use {3} s".format(self.name, self.revisionExpr, ret, fetch_time))
      if ret == 0:
        ok = True
        break

      # Retry later due to HTTP 429 Too Many Requests.
      elif ('error:' in gitcmd.stderr and
            'HTTP 429' in gitcmd.stderr):
        if not quiet:
          print('429 received, sleeping: %s sec' % retry_cur_sleep,
                file=sys.stderr)
        time.sleep(retry_cur_sleep)
        retry_cur_sleep = min(retry_exp_factor * retry_cur_sleep,
                              MAXIMUM_RETRY_SLEEP_SEC)
        retry_cur_sleep *= (1 - random.uniform(-RETRY_JITTER_PERCENT,
                                               RETRY_JITTER_PERCENT))
        continue

      # If this is not last attempt, try 'git remote prune'.
      elif (try_n < retry_fetches - 1 and
            'error:' in gitcmd.stderr and
            'git remote prune' in gitcmd.stderr and
            not prune_tried):
        prune_tried = True
        prunecmd = GitCommand(self, ['remote', 'prune', name if not use_mirror else self.remote.name + '_mirror'], bare=True,
                              ssh_proxy=ssh_proxy)
        ret = prunecmd.Wait()
        if ret:
          break
        continue
      elif current_branch_only and is_sha1 and ret == 128:
        # Exit code 128 means "couldn't find the ref you asked for"; if we're
        # in sha1 mode, we just tried sync'ing from the upstream field; it
        # doesn't exist, thus abort the optimization attempt and do a full sync.
        break
      elif ret < 0:
        # Git died with a signal, exit immediately
        break
      if not verbose:
        print('%s:\n%s' % (self.name, gitcmd.stdout), file=sys.stderr)
      time.sleep(random.randint(30, 45))

    if initial:
      if alt_dir:
        if old_packed != '':
          _lwrite(packed_refs, old_packed)
        else:
          platform_utils.remove(packed_refs)
      self.bare_git.pack_refs('--all', '--prune')

    if ok and is_sha1 and self._CheckForImmutableRevision():
      return ok

    if use_mirror and self.mirror_url:
      return self._RemoteFetch(initial=False, quiet=quiet, verbose=verbose, alt_dir=alt_dir,
                               current_branch_only=current_branch_only,
                               tags=tags, prune=prune, depth=depth,
                               submodules=submodules, force_sync=force_sync,
                               clone_filter=clone_filter, retry_fetches=retry_fetches, use_mirror=False
                               )

    if is_sha1 and current_branch_only:
      # We just synced the upstream given branch; verify we
      # got what we wanted, else trigger a second run of all
      # refs.
      if not self._CheckForImmutableRevision():
        # Sync the current branch only with depth set to None.
        # We always pass depth=None down to avoid infinite recursion.
        return self._RemoteFetch(
            name=name, quiet=quiet, verbose=verbose,
            current_branch_only=current_branch_only and depth,
            initial=False, alt_dir=alt_dir,
            depth=None, clone_filter=clone_filter)

    return ok

  def _ApplyCloneBundle(self, initial=False, quiet=False, verbose=False):
    if initial and \
        (self.manifest.manifestProject.config.GetString('repo.depth') or
         self.clone_depth):
      return False

    remote = self.GetRemote(self.remote.name)
    bundle_url = remote.url + '/clone.bundle'
    bundle_url = GitConfig.ForUser().UrlInsteadOf(bundle_url)
    if GetSchemeFromUrl(bundle_url) not in ('http', 'https',
                                            'persistent-http',
                                            'persistent-https'):
      return False

    bundle_dst = os.path.join(self.gitdir, 'clone.bundle')
    bundle_tmp = os.path.join(self.gitdir, 'clone.bundle.tmp')

    exist_dst = os.path.exists(bundle_dst)
    exist_tmp = os.path.exists(bundle_tmp)

    if not initial and not exist_dst and not exist_tmp:
      return False

    if not exist_dst:
      exist_dst = self._FetchBundle(bundle_url, bundle_tmp, bundle_dst, quiet,
                                    verbose)
    if not exist_dst:
      return False

    cmd = ['fetch']
    if not verbose:
      cmd.append('--quiet')
    if not quiet and sys.stdout.isatty():
      cmd.append('--progress')
    if not self.worktree:
      cmd.append('--update-head-ok')
    cmd.append(bundle_dst)
    for f in remote.fetch:
      cmd.append(str(f))
    cmd.append('+refs/tags/*:refs/tags/*')

    ok = GitCommand(self, cmd, bare=True).Wait() == 0
    if os.path.exists(bundle_dst):
      platform_utils.remove(bundle_dst)
    if os.path.exists(bundle_tmp):
      platform_utils.remove(bundle_tmp)
    return ok

  def _FetchBundle(self, srcUrl, tmpPath, dstPath, quiet, verbose):
    if os.path.exists(dstPath):
      platform_utils.remove(dstPath)

    cmd = ['curl', '--fail', '--output', tmpPath, '--netrc', '--location']
    if quiet:
      cmd += ['--silent', '--show-error']
    if os.path.exists(tmpPath):
      size = os.stat(tmpPath).st_size
      if size >= 1024:
        cmd += ['--continue-at', '%d' % (size,)]
      else:
        platform_utils.remove(tmpPath)
    with GetUrlCookieFile(srcUrl, quiet) as (cookiefile, proxy):
      if cookiefile:
        cmd += ['--cookie', cookiefile]
      if proxy:
        cmd += ['--proxy', proxy]
      elif 'http_proxy' in os.environ and 'darwin' == sys.platform:
        cmd += ['--proxy', os.environ['http_proxy']]
      if srcUrl.startswith('persistent-https'):
        srcUrl = 'http' + srcUrl[len('persistent-https'):]
      elif srcUrl.startswith('persistent-http'):
        srcUrl = 'http' + srcUrl[len('persistent-http'):]
      cmd += [srcUrl]

      if IsTrace():
        Trace('%s', ' '.join(cmd))
      if verbose:
        print('%s: Downloading bundle: %s' % (self.name, srcUrl))
      stdout = None if verbose else subprocess.PIPE
      stderr = None if verbose else subprocess.STDOUT
      try:
        proc = subprocess.Popen(cmd, stdout=stdout, stderr=stderr)
      except OSError:
        return False

      (output, _) = proc.communicate()
      curlret = proc.returncode

      if curlret == 22:
        # From curl man page:
        # 22: HTTP page not retrieved. The requested url was not found or
        # returned another error with the HTTP error code being 400 or above.
        # This return code only appears if -f, --fail is used.
        if verbose:
          print('%s: Unable to retrieve clone.bundle; ignoring.' % self.name)
          if output:
            print('Curl output:\n%s' % output)
        return False
      elif curlret and not verbose and output:
        print('%s' % output, file=sys.stderr)

    if os.path.exists(tmpPath):
      if curlret == 0 and self._IsValidBundle(tmpPath, quiet):
        platform_utils.rename(tmpPath, dstPath)
        return True
      else:
        platform_utils.remove(tmpPath)
        return False
    else:
      return False

  def _IsValidBundle(self, path, quiet):
    try:
      with open(path, 'rb') as f:
        if f.read(16) == b'# v2 git bundle\n':
          return True
        else:
          if not quiet:
            print("Invalid clone.bundle file; ignoring.", file=sys.stderr)
          return False
    except OSError:
      return False

  def _Checkout(self, rev, quiet=False):
    cmd = ['checkout']
    if quiet:
      cmd.append('-q')
    cmd.append(rev)
    cmd.append('--')
    if GitCommand(self, cmd).Wait() != 0:
      if self._allrefs:
        raise GitError('%s checkout %s ' % (self.name, rev))

  def _CherryPick(self, rev, ffonly=False, record_origin=False):
    cmd = ['cherry-pick']
    if ffonly:
      cmd.append('--ff')
    if record_origin:
      cmd.append('-x')
    cmd.append(rev)
    cmd.append('--')
    if GitCommand(self, cmd).Wait() != 0:
      if self._allrefs:
        raise GitError('%s cherry-pick %s ' % (self.name, rev))

  def _LsRemote(self, refs):
    cmd = ['ls-remote', self.remote.name, refs]
    p = GitCommand(self, cmd, capture_stdout=True)
    if p.Wait() == 0:
      return p.stdout
    return None

  def _Revert(self, rev):
    cmd = ['revert']
    cmd.append('--no-edit')
    cmd.append(rev)
    cmd.append('--')
    if GitCommand(self, cmd).Wait() != 0:
      if self._allrefs:
        raise GitError('%s revert %s ' % (self.name, rev))

  def _ResetHard(self, rev, quiet=True):
    cmd = ['reset', '--hard']
    if quiet:
      cmd.append('-q')
    cmd.append(rev)
    if GitCommand(self, cmd).Wait() != 0:
      raise GitError('%s reset --hard %s ' % (self.name, rev))

  def _SyncSubmodules(self, quiet=True):
    cmd = ['submodule', 'update', '--init', '--recursive']
    if quiet:
      cmd.append('-q')
    if GitCommand(self, cmd).Wait() != 0:
      raise GitError('%s submodule update --init --recursive %s ' % self.name)

  def _Rebase(self, upstream, onto=None):
    cmd = ['rebase']
    if onto is not None:
      cmd.extend(['--onto', onto])
    cmd.append(upstream)
    if GitCommand(self, cmd).Wait() != 0:
      raise GitError('%s rebase %s ' % (self.name, upstream))

  def _FastForward(self, head, ffonly=False):
    cmd = ['merge', '--no-stat', head]
    if ffonly:
      cmd.append("--ff-only")
    if GitCommand(self, cmd).Wait() != 0:
      raise GitError('%s merge %s ' % (self.name, head))

  def _InitGitDir(self, mirror_git=None, force_sync=False, quiet=False):
    init_git_dir = not os.path.exists(self.gitdir)
    init_obj_dir = not os.path.exists(self.objdir)
    try:
      # Initialize the bare repository, which contains all of the objects.
      if init_obj_dir:
        os.makedirs(self.objdir)
        self.bare_objdir.init()

        if self.use_git_worktrees:
          # Set up the m/ space to point to the worktree-specific ref space.
          # We'll update the worktree-specific ref space on each checkout.
          if self.manifest.branch:
            self.bare_git.symbolic_ref(
                '-m', 'redirecting to worktree scope',
                R_M + self.manifest.branch,
                R_WORKTREE_M + self.manifest.branch)

          # Enable per-worktree config file support if possible.  This is more a
          # nice-to-have feature for users rather than a hard requirement.
          if git_require((2, 20, 0)):
            self.EnableRepositoryExtension('worktreeConfig')

      # If we have a separate directory to hold refs, initialize it as well.
      if self.objdir != self.gitdir:
        if init_git_dir:
          os.makedirs(self.gitdir)

        if init_obj_dir or init_git_dir:
          self._ReferenceGitDir(self.objdir, self.gitdir, share_refs=False,
                                copy_all=True)
        try:
          self._CheckDirReference(self.objdir, self.gitdir, share_refs=False)
        except GitError as e:
          if force_sync:
            print("Retrying clone after deleting %s" %
                  self.gitdir, file=sys.stderr)
            try:
              platform_utils.rmtree(platform_utils.realpath(self.gitdir))
              if self.worktree and os.path.exists(platform_utils.realpath
                                                  (self.worktree)):
                platform_utils.rmtree(platform_utils.realpath(self.worktree))
              return self._InitGitDir(mirror_git=mirror_git, force_sync=False,
                                      quiet=quiet)
            except Exception:
              raise e
          raise e

      if init_git_dir:
        mp = self.manifest.manifestProject
        ref_dir = mp.config.GetString('repo.reference') or ''

        if ref_dir or mirror_git:
          if not mirror_git:
            mirror_git = os.path.join(ref_dir, self.name + '.git')
          repo_git = os.path.join(ref_dir, '.repo', 'projects',
                                  self.relpath + '.git')
          worktrees_git = os.path.join(ref_dir, '.repo', 'worktrees',
                                       self.name + '.git')

          if os.path.exists(mirror_git):
            ref_dir = mirror_git
          elif os.path.exists(repo_git):
            ref_dir = repo_git
          elif os.path.exists(worktrees_git):
            ref_dir = worktrees_git
          else:
            ref_dir = None

          if ref_dir:
            if not os.path.isabs(ref_dir):
              # The alternate directory is relative to the object database.
              ref_dir = os.path.relpath(ref_dir,
                                        os.path.join(self.objdir, 'objects'))
            _lwrite(os.path.join(self.gitdir, 'objects/info/alternates'),
                    os.path.join(ref_dir, 'objects') + '\n')

        self._UpdateHooks(quiet=quiet)

        m = self.manifest.manifestProject.config
        for key in ['user.name', 'user.email']:
          if m.Has(key, include_defaults=False):
            self.config.SetString(key, m.GetString(key))
        self.config.SetString('filter.lfs.smudge', 'git-lfs smudge --skip -- %f')
        self.config.SetString('filter.lfs.process', 'git-lfs filter-process --skip')
        if self.manifest.IsMirror:
          self.config.SetString('core.bare', 'true')
        else:
          self.config.SetString('core.bare', None)
    except Exception:
      if init_obj_dir and os.path.exists(self.objdir):
        platform_utils.rmtree(self.objdir)
      if init_git_dir and os.path.exists(self.gitdir):
        platform_utils.rmtree(self.gitdir)
      raise

  def _UpdateHooks(self, quiet=False):
    if os.path.exists(self.gitdir):
      self._InitHooks(quiet=quiet)

  def _InitHooks(self, quiet=False):
    hooks = platform_utils.realpath(self._gitdir_path('hooks'))
    if not os.path.exists(hooks):
      os.makedirs(hooks)
    for stock_hook in _ProjectHooks():
      name = os.path.basename(stock_hook)

      if name in ('commit-msg',) and not self.remote.review \
              and self is not self.manifest.manifestProject:
        # Don't install a Gerrit Code Review hook if this
        # project does not appear to use it for reviews.
        #
        # Since the manifest project is one of those, but also
        # managed through gerrit, it's excluded
        continue

      dst = os.path.join(hooks, name)
      if platform_utils.islink(dst):
        continue
      if os.path.exists(dst):
        # If the files are the same, we'll leave it alone.  We create symlinks
        # below by default but fallback to hardlinks if the OS blocks them.
        # So if we're here, it's probably because we made a hardlink below.
        if not filecmp.cmp(stock_hook, dst, shallow=False):
          if not quiet:
            _warn("%s: Not replacing locally modified %s hook",
                  self.relpath, name)
        continue
      try:
        platform_utils.symlink(
            os.path.relpath(stock_hook, os.path.dirname(dst)), dst)
      except OSError as e:
        if e.errno == errno.EPERM:
          try:
            os.link(stock_hook, dst)
          except OSError:
            raise GitError(self._get_symlink_error_message())
        else:
          raise

  def _InitRemote(self):
    if self.remote.url:
      remote = self.GetRemote(self.remote.name)
      remote.url = self.remote.url
      remote.pushUrl = self.remote.pushUrl
      remote.review = self.remote.review
      remote.projectname = self.name

      if self.worktree:
        remote.ResetFetch(mirror=False)
      else:
        remote.ResetFetch(mirror=True)
      remote.Save()

      if self.mirror_url:
        mirror_name = self.remote.name + '_mirror'
        remote_mirror = self.GetRemote(mirror_name)
        remote_mirror.url = self.mirror_url
        remote_mirror.review = self.remote.review
        remote_mirror.projectname = self.name
        remote_mirror.fetch = []
        remote_mirror.Save()

  def _InitMRef(self):
    if self.manifest.branch:
      if self.use_git_worktrees:
        # We can't update this ref with git worktrees until it exists.
        # We'll wait until the initial checkout to set it.
        if not os.path.exists(self.worktree):
          return

        base = R_WORKTREE_M
        active_git = self.work_git
      else:
        base = R_M
        active_git = self.bare_git

      self._InitAnyMRef(base + self.manifest.branch, active_git)

  def _InitMirrorHead(self):
    self._InitAnyMRef(HEAD, self.bare_git)

  def _InitAnyMRef(self, ref, active_git):
    cur = self.bare_ref.symref(ref)

    if self.revisionId:
      if cur != '' or self.bare_ref.get(ref) != self.revisionId:
        msg = 'manifest set to %s' % self.revisionId
        dst = self.revisionId + '^0'
        active_git.UpdateRef(ref, dst, message=msg, detach=True)
    else:
      remote = self.GetRemote(self.remote.name)
      dst = remote.ToLocal(self.revisionExpr)
      if cur != dst:
        msg = 'manifest set to %s' % self.revisionExpr
        active_git.symbolic_ref('-m', msg, ref, dst)

  def _CheckDirReference(self, srcdir, destdir, share_refs):
    # Git worktrees don't use symlinks to share at all.
    if self.use_git_worktrees:
      return

    symlink_files = self.shareable_files[:]
    symlink_dirs = self.shareable_dirs[:]
    if share_refs:
      symlink_files += self.working_tree_files
      symlink_dirs += self.working_tree_dirs
    to_symlink = symlink_files + symlink_dirs
    for name in set(to_symlink):
      # Try to self-heal a bit in simple cases.
      dst_path = os.path.join(destdir, name)
      src_path = os.path.join(srcdir, name)

      if name in self.working_tree_dirs:
        # If the dir is missing under .repo/projects/, create it.
        if not os.path.exists(src_path):
          os.makedirs(src_path)

      elif name in self.working_tree_files:
        # If it's a file under the checkout .git/ and the .repo/projects/ has
        # nothing, move the file under the .repo/projects/ tree.
        if not os.path.exists(src_path) and os.path.isfile(dst_path):
          platform_utils.rename(dst_path, src_path)

      # If the path exists under the .repo/projects/ and there's no symlink
      # under the checkout .git/, recreate the symlink.
      if name in self.working_tree_dirs or name in self.working_tree_files:
        if os.path.exists(src_path) and not os.path.exists(dst_path):
          platform_utils.symlink(
              os.path.relpath(src_path, os.path.dirname(dst_path)), dst_path)

      dst = platform_utils.realpath(dst_path)
      if os.path.lexists(dst):
        src = platform_utils.realpath(src_path)
        # Fail if the links are pointing to the wrong place
        if src != dst:
          _error('%s is different in %s vs %s', name, destdir, srcdir)
          raise GitError('--force-sync not enabled; cannot overwrite a local '
                         'work tree. If you\'re comfortable with the '
                         'possibility of losing the work tree\'s git metadata,'
                         ' use `repo sync --force-sync {0}` to '
                         'proceed.'.format(self.relpath))

  def _ReferenceGitDir(self, gitdir, dotgit, share_refs, copy_all):
    """Update |dotgit| to reference |gitdir|, using symlinks where possible.

    Args:
      gitdir: The bare git repository. Must already be initialized.
      dotgit: The repository you would like to initialize.
      share_refs: If true, |dotgit| will store its refs under |gitdir|.
          Only one work tree can store refs under a given |gitdir|.
      copy_all: If true, copy all remaining files from |gitdir| -> |dotgit|.
          This saves you the effort of initializing |dotgit| yourself.
    """
    symlink_files = self.shareable_files[:]
    symlink_dirs = self.shareable_dirs[:]
    if share_refs:
      symlink_files += self.working_tree_files
      symlink_dirs += self.working_tree_dirs
    to_symlink = symlink_files + symlink_dirs

    to_copy = []
    if copy_all:
      to_copy = platform_utils.listdir(gitdir)

    dotgit = platform_utils.realpath(dotgit)
    for name in set(to_copy).union(to_symlink):
      try:
        src = platform_utils.realpath(os.path.join(gitdir, name))
        dst = os.path.join(dotgit, name)

        if os.path.lexists(dst):
          continue

        # If the source dir doesn't exist, create an empty dir.
        if name in symlink_dirs and not os.path.lexists(src):
          os.makedirs(src)

        if name in to_symlink:
          platform_utils.symlink(
              os.path.relpath(src, os.path.dirname(dst)), dst)
        elif copy_all and not platform_utils.islink(dst):
          if platform_utils.isdir(src):
            shutil.copytree(src, dst)
          elif os.path.isfile(src):
            shutil.copy(src, dst)

        # If the source file doesn't exist, ensure the destination
        # file doesn't either.
        if name in symlink_files and not os.path.lexists(src):
          try:
            platform_utils.remove(dst)
          except OSError:
            pass

      except OSError as e:
        if e.errno == errno.EPERM:
          raise DownloadError(self._get_symlink_error_message())
        else:
          raise

  def _InitGitWorktree(self):
    """Init the project using git worktrees."""
    self.bare_git.worktree('prune')
    self.bare_git.worktree('add', '-ff', '--checkout', '--detach', '--lock',
                           self.worktree, self.GetRevisionId())

    # Rewrite the internal state files to use relative paths between the
    # checkouts & worktrees.
    dotgit = os.path.join(self.worktree, '.git')
    with open(dotgit, 'r') as fp:
      # Figure out the checkout->worktree path.
      setting = fp.read()
      assert setting.startswith('gitdir:')
      git_worktree_path = setting.split(':', 1)[1].strip()
    # Some platforms (e.g. Windows) won't let us update dotgit in situ because
    # of file permissions.  Delete it and recreate it from scratch to avoid.
    platform_utils.remove(dotgit)
    # Use relative path from checkout->worktree.
    with open(dotgit, 'w') as fp:
      print('gitdir:', os.path.relpath(git_worktree_path, self.worktree),
            file=fp)
    # Use relative path from worktree->checkout.
    with open(os.path.join(git_worktree_path, 'gitdir'), 'w') as fp:
      print(os.path.relpath(dotgit, git_worktree_path), file=fp)

    self._InitMRef()

  def _InitWorkTree(self, force_sync=False, submodules=False):
    realdotgit = os.path.join(self.worktree, '.git')
    tmpdotgit = realdotgit + '.tmp'
    init_dotgit = not os.path.exists(realdotgit)
    if init_dotgit:
      if self.use_git_worktrees:
        self._InitGitWorktree()
        self._CopyAndLinkFiles()
        return

      dotgit = tmpdotgit
      platform_utils.rmtree(tmpdotgit, ignore_errors=True)
      os.makedirs(tmpdotgit)
      self._ReferenceGitDir(self.gitdir, tmpdotgit, share_refs=True,
                            copy_all=False)
    else:
      dotgit = realdotgit

    try:
      self._CheckDirReference(self.gitdir, dotgit, share_refs=True)
    except GitError as e:
      if force_sync and not init_dotgit:
        try:
          platform_utils.rmtree(dotgit)
          return self._InitWorkTree(force_sync=False, submodules=submodules)
        except Exception:
          raise e
      raise e

    if init_dotgit:
      _lwrite(os.path.join(tmpdotgit, HEAD), '%s\n' % self.GetRevisionId())

      # Now that the .git dir is fully set up, move it to its final home.
      platform_utils.rename(tmpdotgit, realdotgit)

      # Finish checking out the worktree.
      cmd = ['read-tree', '--reset', '-u']
      cmd.append('-v')
      cmd.append(HEAD)
      if GitCommand(self, cmd).Wait() != 0:
        raise GitError('Cannot initialize work tree for ' + self.name)

      if submodules:
        self._SyncSubmodules(quiet=True)
      self._CopyAndLinkFiles()

  def _get_symlink_error_message(self):
    if platform_utils.isWindows():
      return ('Unable to create symbolic link. Please re-run the command as '
              'Administrator, or see '
              'https://github.com/git-for-windows/git/wiki/Symbolic-Links '
              'for other options.')
    return 'filesystem must support symlinks'

  def _gitdir_path(self, path):
    return platform_utils.realpath(os.path.join(self.gitdir, path))

  def _revlist(self, *args, **kw):
    a = []
    a.extend(args)
    a.append('--')
    return self.work_git.rev_list(*a, **kw)

  @property
  def _allrefs(self):
    return self.bare_ref.all

  def _getLogs(self, rev1, rev2, oneline=False, color=True, pretty_format=None):
    """Get logs between two revisions of this project."""
    comp = '..'
    if rev1:
      revs = [rev1]
      if rev2:
        revs.extend([comp, rev2])
      cmd = ['log', ''.join(revs)]
      out = DiffColoring(self.config)
      if out.is_on and color:
        cmd.append('--color')
      if pretty_format is not None:
        cmd.append('--pretty=format:%s' % pretty_format)
      if oneline:
        cmd.append('--oneline')

      try:
        log = GitCommand(self, cmd, capture_stdout=True, capture_stderr=True)
        if log.Wait() == 0:
          return log.stdout
      except GitError:
        # worktree may not exist if groups changed for example. In that case,
        # try in gitdir instead.
        if not os.path.exists(self.worktree):
          return self.bare_git.log(*cmd[1:])
        else:
          raise
    return None

  def getAddedAndRemovedLogs(self, toProject, oneline=False, color=True,
                             pretty_format=None):
    """Get the list of logs from this revision to given revisionId"""
    logs = {}
    selfId = self.GetRevisionId(self._allrefs)
    toId = toProject.GetRevisionId(toProject._allrefs)

    logs['added'] = self._getLogs(selfId, toId, oneline=oneline, color=color,
                                  pretty_format=pretty_format)
    logs['removed'] = self._getLogs(toId, selfId, oneline=oneline, color=color,
                                    pretty_format=pretty_format)
    return logs

  class _GitGetByExec(object):

    def __init__(self, project, bare, gitdir):
      self._project = project
      self._bare = bare
      self._gitdir = gitdir

    def LsOthers(self):
      p = GitCommand(self._project,
                     ['ls-files',
                      '-z',
                      '--others',
                      '--exclude-standard'],
                     bare=False,
                     gitdir=self._gitdir,
                     capture_stdout=True,
                     capture_stderr=True)
      if p.Wait() == 0:
        out = p.stdout
        if out:
          # Backslash is not anomalous
          return out[:-1].split('\0')
      return []

    def DiffZ(self, name, *args):
      cmd = [name]
      cmd.append('-z')
      cmd.append('--ignore-submodules')
      cmd.extend(args)
      p = GitCommand(self._project,
                     cmd,
                     gitdir=self._gitdir,
                     bare=False,
                     capture_stdout=True,
                     capture_stderr=True)
      try:
        out = p.process.stdout.read()
        if not hasattr(out, 'encode'):
          out = out.decode()
        r = {}
        if out:
          out = iter(out[:-1].split('\0'))
          while out:
            try:
              info = next(out)
              path = next(out)
            except StopIteration:
              break

            class _Info(object):

              def __init__(self, path, omode, nmode, oid, nid, state):
                self.path = path
                self.src_path = None
                self.old_mode = omode
                self.new_mode = nmode
                self.old_id = oid
                self.new_id = nid

                if len(state) == 1:
                  self.status = state
                  self.level = None
                else:
                  self.status = state[:1]
                  self.level = state[1:]
                  while self.level.startswith('0'):
                    self.level = self.level[1:]

            info = info[1:].split(' ')
            info = _Info(path, *info)
            if info.status in ('R', 'C'):
              info.src_path = info.path
              info.path = next(out)
            r[info.path] = info
        return r
      finally:
        p.Wait()

    def GetDotgitPath(self, subpath=None):
      """Return the full path to the .git dir.

      As a convenience, append |subpath| if provided.
      """
      if self._bare:
        dotgit = self._gitdir
      else:
        dotgit = os.path.join(self._project.worktree, '.git')
        if os.path.isfile(dotgit):
          # Git worktrees use a "gitdir:" syntax to point to the scratch space.
          with open(dotgit) as fp:
            setting = fp.read()
          assert setting.startswith('gitdir:')
          gitdir = setting.split(':', 1)[1].strip()
          dotgit = os.path.normpath(os.path.join(self._project.worktree, gitdir))

      return dotgit if subpath is None else os.path.join(dotgit, subpath)

    def GetHead(self):
      """Return the ref that HEAD points to."""
      path = self.GetDotgitPath(subpath=HEAD)
      try:
        with open(path) as fd:
          line = fd.readline()
      except IOError as e:
        raise NoManifestException(path, str(e))
      try:
        line = line.decode()
      except AttributeError:
        pass
      if line.startswith('ref: '):
        return line[5:-1]
      return line[:-1]

    def SetHead(self, ref, message=None):
      cmdv = []
      if message is not None:
        cmdv.extend(['-m', message])
      cmdv.append(HEAD)
      cmdv.append(ref)
      self.symbolic_ref(*cmdv)

    def DetachHead(self, new, message=None):
      cmdv = ['--no-deref']
      if message is not None:
        cmdv.extend(['-m', message])
      cmdv.append(HEAD)
      cmdv.append(new)
      self.update_ref(*cmdv)

    def UpdateRef(self, name, new, old=None,
                  message=None,
                  detach=False):
      cmdv = []
      if message is not None:
        cmdv.extend(['-m', message])
      if detach:
        cmdv.append('--no-deref')
      cmdv.append(name)
      cmdv.append(new)
      if old is not None:
        cmdv.append(old)
      self.update_ref(*cmdv)

    def DeleteRef(self, name, old=None):
      if not old:
        old = self.rev_parse(name)
      self.update_ref('-d', name, old)
      self._project.bare_ref.deleted(name)

    def rev_list(self, *args, **kw):
      if 'format' in kw:
        cmdv = ['log', '--pretty=format:%s' % kw['format']]
      else:
        cmdv = ['rev-list']
      cmdv.extend(args)
      p = GitCommand(self._project,
                     cmdv,
                     bare=self._bare,
                     gitdir=self._gitdir,
                     capture_stdout=True,
                     capture_stderr=True)
      if p.Wait() != 0:
        raise GitError('%s rev-list %s: %s' %
                       (self._project.name, str(args), p.stderr))
      return p.stdout.splitlines()

    def __getattr__(self, name):
      """Allow arbitrary git commands using pythonic syntax.

      This allows you to do things like:
        git_obj.rev_parse('HEAD')

      Since we don't have a 'rev_parse' method defined, the __getattr__ will
      run.  We'll replace the '_' with a '-' and try to run a git command.
      Any other positional arguments will be passed to the git command, and the
      following keyword arguments are supported:
        config: An optional dict of git config options to be passed with '-c'.

      Args:
        name: The name of the git command to call.  Any '_' characters will
            be replaced with '-'.

      Returns:
        A callable object that will try to call git with the named command.
      """
      name = name.replace('_', '-')

      def runner(*args, **kwargs):
        cmdv = []
        config = kwargs.pop('config', None)
        for k in kwargs:
          raise TypeError('%s() got an unexpected keyword argument %r'
                          % (name, k))
        if config is not None:
          for k, v in config.items():
            cmdv.append('-c')
            cmdv.append('%s=%s' % (k, v))
        cmdv.append(name)
        cmdv.extend(args)
        p = GitCommand(self._project,
                       cmdv,
                       bare=self._bare,
                       gitdir=self._gitdir,
                       capture_stdout=True,
                       capture_stderr=True)
        if p.Wait() != 0:
          raise GitError('%s %s: %s' %
                         (self._project.name, name, p.stderr))
        r = p.stdout
        if r.endswith('\n') and r.index('\n') == len(r) - 1:
          return r[:-1]
        return r
      return runner


class _PriorSyncFailedError(Exception):

  def __str__(self):
    return 'prior sync failed; rebase still in progress'


class _DirtyError(Exception):

  def __str__(self):
    return 'contains uncommitted changes'


class _InfoMessage(object):

  def __init__(self, project, text):
    self.project = project
    self.text = text

  def Print(self, syncbuf):
    syncbuf.out.info('%s/: %s', self.project.relpath, self.text)
    syncbuf.out.nl()


class _Failure(object):

  def __init__(self, project, why):
    self.project = project
    self.why = why

  def Print(self, syncbuf):
    syncbuf.out.fail('error: %s/: %s',
                     self.project.relpath,
                     str(self.why))
    syncbuf.out.nl()


class _Later(object):

  def __init__(self, project, action):
    self.project = project
    self.action = action

  def Run(self, syncbuf):
    out = syncbuf.out
    out.project('project %s/', self.project.relpath)
    out.nl()
    try:
      self.action()
      out.nl()
      return True
    except GitError:
      out.nl()
      return False


class _SyncColoring(Coloring):

  def __init__(self, config):
    Coloring.__init__(self, config, 'reposync')
    self.project = self.printer('header', attr='bold')
    self.info = self.printer('info')
    self.fail = self.printer('fail', fg='red')


class SyncBuffer(object):

  def __init__(self, config, detach_head=False):
    self._messages = []
    self._failures = []
    self._later_queue1 = []
    self._later_queue2 = []

    self.out = _SyncColoring(config)
    self.out.redirect(sys.stderr)

    self.detach_head = detach_head
    self.clean = True
    self.recent_clean = True

  def info(self, project, fmt, *args):
    self._messages.append(_InfoMessage(project, fmt % args))

  def fail(self, project, err=None):
    self._failures.append(_Failure(project, err))
    self._MarkUnclean()

  def later1(self, project, what):
    self._later_queue1.append(_Later(project, what))

  def later2(self, project, what):
    self._later_queue2.append(_Later(project, what))

  def Finish(self):
    self._PrintMessages()
    self._RunLater()
    self._PrintMessages()
    return self.clean

  def Recently(self):
    recent_clean = self.recent_clean
    self.recent_clean = True
    return recent_clean

  def _MarkUnclean(self):
    self.clean = False
    self.recent_clean = False

  def _RunLater(self):
    for q in ['_later_queue1', '_later_queue2']:
      if not self._RunQueue(q):
        return

  def _RunQueue(self, queue):
    for m in getattr(self, queue):
      if not m.Run(self):
        self._MarkUnclean()
        return False
    setattr(self, queue, [])
    return True

  def _PrintMessages(self):
    if self._messages or self._failures:
      if os.isatty(2):
        self.out.write(progress.CSI_ERASE_LINE)
      self.out.write('\r')

    for m in self._messages:
      m.Print(self)
    for m in self._failures:
      m.Print(self)

    self._messages = []
    self._failures = []


class MetaProject(Project):

  """A special project housed under .repo.
  """

  def __init__(self, manifest, name, gitdir, worktree):
    Project.__init__(self,
                     manifest=manifest,
                     name=name,
                     gitdir=gitdir,
                     objdir=gitdir,
                     worktree=worktree,
                     remote=RemoteSpec('origin'),
                     relpath='.repo/%s' % name,
                     revisionExpr='refs/heads/master',
                     revisionId=None,
                     groups=None)

  def PreSync(self):
    if self.Exists:
      cb = self.CurrentBranch
      if cb:
        base = self.GetBranch(cb).merge
        if base:
          self.revisionExpr = base
          self.revisionId = None

  def MetaBranchSwitch(self, submodules=False):
    """ Prepare MetaProject for manifest branch switch
    """

    # detach and delete manifest branch, allowing a new
    # branch to take over
    syncbuf = SyncBuffer(self.config, detach_head=True)
    self.Sync_LocalHalf(syncbuf, submodules=submodules)
    syncbuf.Finish()

    return GitCommand(self,
                      ['update-ref', '-d', 'refs/heads/default'],
                      capture_stdout=True,
                      capture_stderr=True).Wait() == 0

  @property
  def LastFetch(self):
    try:
      fh = os.path.join(self.gitdir, 'FETCH_HEAD')
      return os.path.getmtime(fh)
    except OSError:
      return 0

  @property
  def HasChanges(self):
    """Has the remote received new commits not yet checked out?
    """
    if not self.remote or not self.revisionExpr:
      return False

    all_refs = self.bare_ref.all
    revid = self.GetRevisionId(all_refs)
    head = self.work_git.GetHead()
    if head.startswith(R_HEADS):
      try:
        head = all_refs[head]
      except KeyError:
        head = None

    if revid == head:
      return False
    elif self._revlist(not_rev(HEAD), revid):
      return True
    return False

