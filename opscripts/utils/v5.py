# vim: set fileencoding=utf-8 :

"""OpScripts utilities library
"""

# Standard Library
from __future__ import absolute_import, division, print_function
import logging
import os
import random
import re
import select
import subprocess
import sys
import tempfile
import traceback


LOG = logging.getLogger(__name__)


class Fatal(Exception):
    def __init__(self, message, code=None):
        self.code = code if code else 1
        message = "({0}) {1}".format(self.code, message)
        super(Fatal, self).__init__(message)


def _exec_cmd_base(cmd_args, cwd=None, uid=None, gid=None):
    """INTERNAL/PRIVATE
    Execute specified command with optional working directory, uid, and gid.

    Returns exit status, STDOUT, and STDERR.
    """
    def switch_uid_gid():
        if uid is not None:
            os.setuid(uid)
        if gid is not None:
            os.setgid(gid)
    job = subprocess.Popen(cmd_args, cwd=cwd, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE,
                           preexec_fn=switch_uid_gid)
    exit_status = job.wait()
    stdout = job.stdout.read().strip().decode("utf-8")
    stderr = job.stderr.read().strip().decode("utf-8")
    return [exit_status, stdout, stderr]


def _exec_cmd_base_spec(cmd_args, cwd=None, uid=None, gid=None):
    """INTERNAL/PRIVATE
    Assembles a string description of the specified command and returns it.
    """
    cmd_spec = "Executing {0}".format(cmd_args)
    if cwd is not None:
        cmd_spec = "{0} within {1}".format(cmd_spec, cwd)
    if uid is not None:
        euid = uid
    else:
        euid = os.getuid()
    if gid is not None:
        egid = gid
    else:
        egid = os.getegid()
    cmd_spec = "{0} as {1}:{2}".format(cmd_spec, euid, egid)
    return cmd_spec


def _check_logging():
    """Check for logging handlers.
    """
    if LOG.handlers:
        return True
    elif logging.getLogger().handlers:
        return True
    return False


def atomic_replace_file(file, content, follow_symlink=False):
    """Replaces the original content with new content, maintaining
    file ownership/perms if executed with sufficient privileges.

    :param file - Full path to file
    :param content - String to be written as new content
    """
    if not follow_symlink and os.path.abspath(file) != os.path.realpath(file):
        return False

    file_dir = os.path.dirname(file)
    orig_stat = os.stat(file)
    orig_perms = orig_stat.st_mode & 0777
    orig_uid = orig_stat.st_uid
    orig_gid = orig_stat.st_gid

    # Create temp file with same ownerships and permissions
    file_temp_name = write_tempfile(file_dir, content)
    os.chown(file_temp_name, orig_uid, orig_gid)
    os.chmod(file_temp_name, orig_perms)

    # Backup file
    file_backup = back_up_file(file)
    # Attempt swap
    os.unlink(file)
    try:
        LOG.debug("Linking temp file to original")
        os.link(file_temp_name, file)
    except:
        LOG.debug("Linking backup file to original")
        os.link(file_backup, file)
        return False

    LOG.debug("Deleting temp file and backup")
    os.unlink(file_temp_name)
    os.unlink(file_backup)

    return True


def back_up_file(file_path, target_dir=None, suffix="_orig"):
    """Create a backup of a file, via os.link. Will not clobber
    existing backups. If no target directory is supplied, will
    use directory of file to be backed up.

    :param file - Full or relative path to file requiring backup
    :param target_dir - Where to put the backup
    :param suffix - Suffix to apply to differentiate backup
    """
    if target_dir is None:
        target_dir = os.path.dirname(os.path.abspath(file_path))

    try:
        _, filename = os.path.split(file_path)
    except AttributeError as e:
        LOG.error(e)
        return False

    if not filename:
        LOG.error("Invalid file supplied for backup: {}".format(file_path))
        return False

    file_backup = os.path.join(target_dir, filename + suffix)

    try:
        os.link(file_path, file_backup)
    except OSError as e:
        LOG.error(e)
        return False

    return file_backup


def exec_cmd_fail_hard(cmd_args, cwd=None, uid=None, gid=None):
    """Execute a command and Fatal if it fails (as well as print STDERR).

    Returns exit status, STDOUT, and STDERR.
    """
    cmd_spec = _exec_cmd_base_spec(cmd_args, cwd, uid, gid)
    results = _exec_cmd_base(cmd_args, cwd, uid, gid)
    if results[0] != 0:
        LOG.error("STDERR: {0}".format(results[2]))
        raise Fatal("{0} failed ({1}).".format(cmd_spec, results[0]),
                    results[0])
    return results


def exec_cmd_fail_prompt(cmd_args, cwd=None, uid=None, gid=None,
                         opt_force=None, opt_yes=None):
    """Execute a command and if it fails:
    - Quit if opt_yes is True
    - Continue if opt_force is True
    - Prompt to continue

    Returns exit status, STDOUT, and STDERR.
    """
    cmd_spec = _exec_cmd_base_spec(cmd_args, cwd, uid, gid)
    results = _exec_cmd_base(cmd_args, cwd, uid, gid)
    if results[0] != 0:
        LOG.error("STDERR: {0}".format(results[2]))
        if opt_yes:
            raise Fatal("{0} failed ({1}). Yes option used. Exiting."
                        .format(cmd_spec, results[0]), results[0])
        elif opt_force:
            LOG.debug("{0} failed ({1}). Force option used. Continuing."
                      .format(cmd_spec, results[0]))
        else:
            LOG.error("{0} failed ({1})".format(cmd_spec, results[0]))
            request_y_to_continue()
    return results


def exec_cmd_debug(cmd_args, cwd=None, uid=None, gid=None):
    """Execute a command and provide all available information about it via
    debug.

    Returns exit status, STDOUT, and STDERR.
    """
    cmd_spec = _exec_cmd_base_spec(cmd_args, cwd, uid, gid)
    LOG.debug(cmd_spec)
    results = _exec_cmd_base(cmd_args, cwd, uid, gid)
    LOG.debug("exit status: {0}".format(results[0]))
    LOG.debug("STDOUT: {0}".format(results[1]))
    LOG.debug("STDERR: {0}".format(results[2]))
    return results


def format_columns(rows, align=None):
    """Convert a list (rows) of lists (columns) to a formatted list of lines.
    When joined with newlines and printed, the output is similar to
    `column -t`.

    The optional align may be a list of alignment formatters.

    Based on solution provided by antak in http://stackoverflow.com/a/12065663
    """
    lines = list()
    widths = [max(map(len, map(str, col))) for col in zip(*rows)]
    for row in rows:
        formatted = list()
        for i, col in enumerate(row):
            if align and align[i].lower() in (">", "r"):
                formatted.append(str(col).rjust(widths[i]))
            elif align and align[i].lower() in ("^", "c"):
                formatted.append(str(col).center(widths[i]))
            else:
                formatted.append(str(col).ljust(widths[i]))
        lines.append("  ".join(formatted))
    return lines


def is_valid_hostname(hostname):
    """Validate hostname syntax:
    - Entire hostname must
        - not exceed 253 characters
        - not be all-numeric (so that it can't be confused with an IP address)
    - Each label must:
      - contain at least 1 and not more than 63 characters
      - not begin or end with a hyphen
      - not contain illegal characters

    http://stackoverflow.com/questions/2532053/validate-a-hostname-string
    """
    disallowed = re.compile("[^A-Z\d-]", re.IGNORECASE)
    # strip exactly one dot from the right, if present
    if hostname.endswith("."):
        hostname = hostname[:-1]
    # hostname must not exceed 253 characters
    if len(hostname) > 253:
        return False
    # hostnbame must be not all-numeric (so that it can't be confused with an
    # IP address)
    if re.match(r"[\d.]+$", hostname):
        return False
    for label in hostname.split("."):
        # label must contain at least 1 and not more than 63 characters
        if len(label) == 0 or len(label) > 63:
            return False
        # label must not begin or end with a hyphen
        if label.startswith("-") or label.endswith("-"):
            return False
        # label must not contain illegal characters
        if disallowed.search(label):
            return False
    return True


def log_ctrlc_and_exit():
    print(file=sys.stderr)
    if _check_logging():
        LOG.info("(130) Halted via KeyboardInterrupt.")
    else:
        print("CRITICAL No handlers could be found for logger \"{0}\""
              .format(__name__), file=sys.stderr)
        print("INFO (130) Halted via KeyboardInterrupt.", file=sys.stderr)
    sys.exit(130)


def log_exception():
    exc_type, exc_value, exc_traceback = sys.exc_info()
    name = exc_value.__class__.__name__
    data = traceback.extract_tb(exc_traceback)
    trace_bottom = ": ".join(str(i) for i in data[0])
    trace_top = ": ".join(str(i) for i in data[-1])
    if _check_logging():
        LOG.critical("(1) {0}: {1}:  {2}  ...  {3}"
                     .format(name, exc_value, trace_top, trace_bottom))
    else:
        print("CRITICAL No handlers could be found for logger \"{0}\""
              .format(__name__), file=sys.stderr)
        print("CRITICAL (1) {0}: {1}:  {2}  ...  {3}"
              .format(name, exc_value, trace_top, trace_bottom),
              file=sys.stderr)


def log_exception_and_exit(exit_status=1):
    log_exception()
    sys.exit(exit_status)


def log_fatal_and_exit():
    exc_type, exc_value, exc_traceback = sys.exc_info()
    if _check_logging():
        LOG.critical(exc_value)
    else:
        print("CRITICAL No handlers could be found for logger \"{0}\""
              .format(__name__), file=sys.stderr)
        print("CRITICAL {0}".format(exc_value), file=sys.stderr)
    sys.exit(exc_value.code)


def request_confirmation(timeout=30):
    """Request the operator input the displayed random number to confirm the
    script should make the changes requested. Each attempt fails if the
    timeout is reached.

    Exits with an error after five failures.
    """
    try:
        timeout = float(timeout)
    except:
        raise Fatal("request_confirmation timeout must be a number",
                    os.EX_NOPERM)
    random_int = random.randint(10000, 99999)
    message = ("To continue, please enter the number '%d' within %d"
               " seconds:" % (random_int, timeout))
    i = 0
    while i < 5:
        i += 1
        print(message,)
        read_obj, w, x = select.select([sys.stdin], list(), list(), timeout)
        if read_obj:
            response = sys.stdin.readline().strip()
            try:
                response = int(response)
            except ValueError:
                pass
            except:
                raise
            if response == random_int:
                return
    raise Fatal("Failed to provide confirmation input.", os.EX_NOPERM)


def request_y_to_continue(timeout=30):
    """Request the operator input "y" to continue. Each attempt fails if the
    timeout is reached.

    Exits with an error after five failures.
    """
    try:
        timeout = float(timeout)
    except:
        raise Fatal("request_confirmation timeout must be a number",
                    os.EX_NOPERM)
    message = "Do you want to continue? (y/n):"
    i = 0
    while i < 5:
        i += 1
        print(message,)
        read_obj, w, x = select.select([sys.stdin], list(), list(), timeout)
        if read_obj:
            response = sys.stdin.readline().strip()[0:].lower()
            try:
                response = str(response)
            except ValueError:
                pass
            except:
                raise
            if response == "y":
                return
            elif response == "n":
                raise Fatal("Response received: \"{0}\". Exiting."
                            .format(response))
    raise Fatal("Failed to provide confirmation input.", os.EX_NOPERM)


def verify_root():
    """Verify script is being run as root.
    """
    if not os.geteuid() == 0:
        raise Fatal("Must be root or equivalent (ex. sudo).", os.EX_NOPERM)


def write_tempfile(directory, content):
    file_temp_fd, file_temp_name = tempfile.mkstemp(dir=directory)
    LOG.debug("Created temp file: {}".format(file_temp_name))
    file_temp_fo = os.fdopen(file_temp_fd, "w")
    file_temp_fo.writelines(content)
    file_temp_fo.close()

    return file_temp_name
