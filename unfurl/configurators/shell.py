# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
environment:
timeout:
inputs:
 command: "--switch {{ '.::foo' | ref }}"
 cwd
 dryrun
 shell
 keeplines
 done
 resultTemplate: # available vars: cmd, stdout, stderr, returncode, error, timeout
   eval:
    file:
      ./handleResult.tpl
   foreach: contents # get the file contents
"""


# see also 13.4.1 Shell scripts p 360
# XXX add support for a stdin parameter

from ..configurator import Status
from ..util import which
from . import TemplateConfigurator
import os
import sys
import shlex
import six
import re
from click.termui import unstyle

if os.name == "posix" and sys.version_info[0] < 3:
    import subprocess32 as subprocess
else:
    import subprocess
# cf https://github.com/opsmop/opsmop/blob/master/opsmop/core/command.py


class _PrintOnAppendList(list):
    def append(self, data):
        list.append(self, data)
        try:
            sys.stdout.write(data.decode())
        except:
            pass


def _run(*args, **kwargs):
    timeout = kwargs.pop("timeout", None)
    input = kwargs.pop("input", None)
    with subprocess.Popen(*args, **kwargs) as process:
        try:
            stdout = None
            stderr = None
            _save_input = process._save_input
            # _save_input is called after _fileobj2output is setup but before reading
            def _save_input_hook_hack(input):
                if process.stdout:
                    process._fileobj2output[process.stdout] = _PrintOnAppendList()
                if process.stderr:
                    process._fileobj2output[process.stderr] = _PrintOnAppendList()
                _save_input(input)

            process._save_input = _save_input_hook_hack
            process.communicate(input, timeout=timeout)
            if process.stdout:
                stdout = b"".join(process._fileobj2output[process.stdout])
            if process.stderr:
                stderr = b"".join(process._fileobj2output[process.stderr])
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        except:  # Including KeyboardInterrupt, communicate handled that.
            process.kill()
            # We don't call process.wait() as .__exit__ does that for us.
            raise
        retcode = process.poll()
    return subprocess.CompletedProcess(process.args, retcode, stdout, stderr)


def _truncate(s):
    if not s:
        return ""
    if len(s) > 1000:
        return f"{s[:500]} [{len(s)} omitted...]  {s[-500:]}"
    return s


def clean_output(value: str) -> str:
    return re.sub(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]", "", unstyle(value))


# XXX we should know if cmd if not os.access(implementation, os.X):
class ShellConfigurator(TemplateConfigurator):
    exclude_from_digest = TemplateConfigurator.exclude_from_digest + ("cwd", "echo")
    _default_cmd = None

    @staticmethod
    def _cmd(cmd, keeplines):
        if not isinstance(cmd, six.string_types):
            cmdStr = " ".join(cmd)
        else:
            if not keeplines:
                cmd = cmd.replace("\n", " ")
            cmdStr = cmd
            cmd = shlex.split(cmd)
        return cmdStr, cmd

    def run_process(
        self,
        cmd,
        shell=False,
        timeout=None,
        env=None,
        cwd=None,
        keeplines=False,
        echo=True,
    ):
        """
        Returns an object with the following attributes:

        cmd
        timeout (None unless timeout occurred)
        stderr
        stdout
        returncode (None if the process didn't complete)
        error if an exception was raised
        """
        cmdStr, cmd = self._cmd(cmd, keeplines)
        try:
            # hack to echo results
            if echo and hasattr(subprocess.Popen, "_save_input"):
                run = _run
            else:
                # Windows and 2.7 don't have _save_input
                run = subprocess.run
            if shell and isinstance(shell, six.string_types):
                executable = shell
            else:
                executable = None
            completed = run(
                # follow recommendation to use string with shell, list without
                cmdStr if shell else cmd,
                shell=shell,
                executable=executable,
                env=env,
                cwd=cwd,
                timeout=timeout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                input=None,
            )

            # try to convert stdout and stderr to strings but leave as binary if that fails
            try:
                completed.stdout = completed.stdout.decode()
            except:
                pass
            try:
                completed.stderr = completed.stderr.decode()
            except:
                pass
            completed.cmd = cmdStr
            completed.timeout = None
            completed.error = None
            return completed
        except subprocess.TimeoutExpired as err:
            err.cmd = cmdStr
            err.timeout = timeout
            err.returncode = None
            err.error = None
            return err
        except Exception as err:
            err.cmd = cmdStr
            err.timeout = None
            err.stderr = None
            err.stdout = None
            err.returncode = None
            err.error = err
            return err

    def _handle_result(self, task, result, cwd, successCodes=(0,)):
        error = result.error or result.returncode not in successCodes or result.timeout
        if error:
            task.logger.warning('shell task run failure: "%s" in %s', result.cmd, cwd)
            if result.error:
                task.logger.info("shell task error", exc_info=result.error)
            else:
                task.logger.info(
                    "shell task return code: %s, stderr: %s",
                    result.returncode,
                    _truncate(result.stderr),
                )
        else:
            task.logger.info("shell task run success: %s", result.cmd)
            task.logger.debug("shell task output: %s", _truncate(result.stdout))
        # strips terminal escapes after we printed output via logger
        result.stdout = clean_output(result.stdout or "")
        result.stderr = clean_output(result.stderr or "")
        return not error

    def _process_result(self, task, result, cwd):
        success = self._handle_result(task, result, cwd)
        resultDict = result.__dict__.copy()
        resultDict["success"] = success
        self.process_result_template(task, resultDict)
        if task._errors:
            return False
        return success

    def can_run(self, task):
        params = task.inputs
        cmd = params.get("command", self._default_cmd)
        if not cmd:
            return "missing command to execute"
        if isinstance(cmd, list) and not params.get("shell") and not which(cmd[0]):
            return f"'{cmd[0]}' is not executable"
        return True

    def can_dry_run(self, task):
        return task.inputs.get("dryrun")

    def render(self, task):
        params = task.inputs
        cmd = params["command"]
        cwd = params.get("cwd")
        if cwd:
            # if cwd is relative, make it relative to task.cwd
            assert isinstance(cwd, six.string_types)
            cwd = os.path.abspath(os.path.join(task.cwd, cwd))
        else:
            cwd = task.cwd
        cmd = self.resolve_dry_run(cmd, task)
        return [cmd, cwd]

    def run(self, task):
        cmd, cwd = task.rendered
        task.logger.trace("executing %s", cmd)
        params = task.inputs
        isString = isinstance(cmd, six.string_types)
        # default for shell: True if command is a string otherwise False
        shell = params.get("shell", isString)
        env = task.get_environment(False)
        task.logger.trace("shell using env %s", env)
        keeplines = params.get("keeplines")
        echo = params.get("echo", task.verbose > -1)
        result = self.run_process(
            cmd,
            shell=shell,
            timeout=task.configSpec.timeout,
            env=env,
            cwd=cwd,
            keeplines=keeplines,
            echo=echo,
        )
        success = self._process_result(task, result, cwd)
        yield self.done(task, success=success, result=result.__dict__)

    def resolve_dry_run(self, cmd, task):
        is_string = isinstance(cmd, six.string_types)
        if task.dry_run and isinstance(task.inputs.get("dryrun"), six.string_types):
            dry_run_arg = task.inputs["dryrun"]
            if "%dryrun%" in cmd:  # replace %dryrun%
                if is_string:
                    cmd = cmd.replace("%dryrun%", dry_run_arg)
                else:
                    cmd[cmd.index("%dryrun%")] = dry_run_arg
            else:  # append dry_run_arg
                if is_string:
                    cmd += " " + dry_run_arg
                else:
                    cmd.append(dry_run_arg)
        elif "%dryrun%" in cmd:
            if is_string:
                cmd = cmd.replace("%dryrun%", "")
            else:
                cmd.remove("%dryrun%")
        return cmd
