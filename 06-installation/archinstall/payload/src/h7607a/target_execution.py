"""Offline target execution without host systemd transient units."""
from __future__ import annotations

import codecs
import os
from pathlib import Path
import selectors
import shlex
import signal
import subprocess
import sys
import tempfile
import time


class TargetCommandError(RuntimeError):
    """A target command failed; its actual output remains in the target log."""


def target_root(target):
    root = Path(target).absolute()
    if root == Path('/') or not root.is_dir() or root.resolve() != root:
        raise ValueError('A canonical target directory other than / is required')
    return root


def chroot_argv(target, *args, bind_dns=False):
    """Keep input/output with the caller and all temporary mounts private."""
    root = target_root(target)
    if not args or any(not isinstance(arg, str) or '\0' in arg for arg in args):
        raise ValueError('Explicit target command arguments are required')
    return ['unshare', '--mount', '--propagation', 'private', '--fork', '--pid',
            '--kill-child=KILL', '--', 'arch-chroot', *([] if bind_dns else ['-r']),
            str(root), *args]


def _logfile(target):
    root = target_root(target)
    directory = root / 'var/log/h7607a'
    # Do not follow a target-controlled symlink outside its installation.
    for parent in (root / 'var', root / 'var/log', directory):
        if parent.is_symlink() or not parent.resolve().is_relative_to(root):
            raise ValueError('Target command log path escapes its installation')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='mkinitcpio-', suffix='.log', dir=directory)
    return os.fdopen(fd, 'wb'), Path(name)


def _stop(child):
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired as error:
        raise TargetCommandError('Target process did not exit after SIGKILL; inspect kernel/storage errors') from error


def run_mkinitcpio(target, args, *, timeout=600):
    """Stream the real build output and retain it, including nonzero status.

    TargetCommandError deliberately differs from Archinstall's SysCallError:
    the upstream mkinitcpio wrapper must not reduce it to an unexplained False.
    Passwords never pass through this runner; native account input is preserved
    by chroot_argv and Archinstall's own subprocess interface.
    """
    args = list(args)
    if not args or args[0] not in ('mkinitcpio', '/usr/bin/mkinitcpio'):
        raise ValueError('Only mkinitcpio uses the build-log runner')
    if timeout <= 0:
        raise ValueError('A positive target command timeout is required')
    command = chroot_argv(target, *args)
    stream, logfile = _logfile(target)
    print('Generating target initramfs; full output: ' + str(logfile), flush=True)
    tail = b''
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    with stream:
        stream.write(('COMMAND ' + shlex.join(command) + '\n').encode())
        stream.flush()
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as poller:
                poller.register(child.stdout, selectors.EVENT_READ)
                while poller.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, timeout)
                    for key, _ in poller.select(min(remaining, 1)):
                        data = os.read(key.fd, 65536)
                        if not data:
                            poller.unregister(key.fileobj)
                            continue
                        stream.write(data)
                        stream.flush()
                        tail = (tail + data)[-8192:]
                        sys.stdout.write(decoder.decode(data))
                        sys.stdout.flush()
                child.wait(timeout=max(0.001, deadline - time.monotonic()))
        except BaseException as error:
            try:
                _stop(child)
            except TargetCommandError as cleanup:
                error.add_note(str(cleanup))
            try:
                stream.write(('\nSTOPPED ' + type(error).__name__ + '\n').encode())
                stream.flush()
                os.fsync(stream.fileno())
            except OSError as logging_error:
                error.add_note('Could not finish build log: ' + str(logging_error))
            if isinstance(error, KeyboardInterrupt):
                raise
            if isinstance(error, subprocess.TimeoutExpired):
                raise TargetCommandError('mkinitcpio timed out; full output: ' + str(logfile)
                                         + '\n' + tail.decode('utf-8', 'replace')) from error
            raise
        finally:
            child.stdout.close()
        sys.stdout.write(decoder.decode(b'', final=True))
        sys.stdout.flush()
        stream.write(('\nEXIT_STATUS ' + str(child.returncode) + '\n').encode())
        stream.flush()
        os.fsync(stream.fileno())
    if child.returncode:
        raise TargetCommandError('mkinitcpio failed with exit status ' + str(child.returncode)
                                 + '; full output: ' + str(logfile)
                                 + '\n' + tail.decode('utf-8', 'replace'))
    return subprocess.CompletedProcess(command, child.returncode)


def run_native(target, command, *, runner, peek_output=False):
    """Preserve native SysCommand behavior while replacing its transport."""
    args = shlex.split(command)
    if args and args[0] in ('mkinitcpio', '/usr/bin/mkinitcpio'):
        return run_mkinitcpio(target, args)
    return runner(chroot_argv(target, *args), peek_output=peek_output)
