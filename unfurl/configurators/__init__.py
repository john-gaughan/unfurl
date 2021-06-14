# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import six
from ..configurator import Configurator, Status
from ..result import ResultsMap
from ..util import registerShortNames

# need to define these now because these configurators are lazily imported
# and so won't register themselves through AutoRegisterClass
registerShortNames(
    {
        name: "unfurl.configurators.%s.%sConfigurator" % (name.lower(), name)
        for name in "Ansible Shell Supervisor Terraform".split()
    }
)


class TemplateConfigurator(Configurator):
    excludeFromDigest = ("resultTemplate", "done", "run", "dryrun")

    def processResultTemplate(self, task, result):
        """
        for both the ansible and shell configurators
        result can include: "returncode", "msg", "error", "stdout", "stderr"
        Ansible also includes "outputs"
        """
        # get the resultTemplate without evaluating it
        resultTemplate = task.inputs._attributes.get("resultTemplate")
        if resultTemplate:  # evaluate it now with the result
            if isinstance(resultTemplate, six.string_types):
                query = dict(template=resultTemplate)
            else:
                query = resultTemplate

            # workaround for jinja template processing setting Result when getting items
            if not isinstance(result, ResultsMap):
                vars = ResultsMap(result, task.inputs.context)
                vars.doFullResolve = True
            else:
                vars = result

            try:
                results = task.query({"eval": query}, vars=vars, throw=True)
            except Exception as e:
                results = None
                errors = [e]
            else:
                if results:
                    jr, errors = task.updateResources(results)
            if errors:
                task.logger.warning(
                    "error processing resultTemplate for %s: %s",
                    task.target.name,
                    errors[0],
                )

    def canDryRun(self, task):
        return not not task.inputs.get("dryrun")

    def render(self, task):
        if task.dryRun:
            runResult = task.inputs.get("dryrun")
            if not isinstance(runResult, dict):
                runResult = task.inputs.get("run")
        else:
            runResult = task.inputs.get("run")
        return runResult

    def run(self, task):
        runResult = task.renderState
        done = task.inputs.get("done", {})
        if "result" not in done:
            if not isinstance(runResult, dict):
                done["result"] = {"run": runResult}
            else:
                done["result"] = runResult
        self.processResultTemplate(task, done.get("result"))
        yield task.done(**done)


class DelegateConfigurator(Configurator):
    def canDryRun(self, task):
        return True  # ok because this will also be called on the subtask

    def run(self, task):
        subtaskRequest = task.createSubTask(
            task.inputs["operation"], task.inputs.get("target")
        )
        assert subtaskRequest
        # note: this will call canRun() and if needed canDryRun() on subtask but not shouldRun()
        subtask = yield subtaskRequest
        yield subtask.result
