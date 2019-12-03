"""
A Job is generated by comparing a list of specs with the last known state of the system.
Job runs tasks, each of which has a configuration spec that is executed on the running system
Each task tracks and records its modifications to the system's state
"""

import collections
import datetime
import types
from .support import Status, Priority, AttributeManager
from .result import serializeValue, ChangeRecord
from .util import UnfurlError, UnfurlTaskError, mergeDicts, ansibleDisplay, toEnum
from .runtime import OperationalInstance
from .configurator import TaskView, ConfiguratorResult
from .plan import Plan

import logging

logger = logging.getLogger("unfurl")


class ConfigChange(OperationalInstance, ChangeRecord):
    """
  Represents a configuration change made to the system.
  It has a operating status and a list of dependencies that contribute to its status.
  There are two kinds of dependencies:
    1. Live resource attributes that the configuration's inputs depend on.
    2. Other configurations and resources it relies on to function properly.
  """

    def __init__(self, status=None, **kw):
        OperationalInstance.__init__(self, status, **kw)
        ChangeRecord.__init__(self)


class TaskRequest(object):
    def __init__(self, configSpec, resource, persist, required):
        self.configSpec = configSpec
        self.target = resource
        self.persist = persist
        self.required = required


class JobRequest(object):
    def __init__(self, resources, errors):
        self.resources = resources
        self.errors = errors


class ConfigTask(ConfigChange, TaskView, AttributeManager):
    """
    receives a configSpec and a target node instance
    instantiates and runs Configurator
    updates Configurator's target's status and lastConfigChange
  """

    def __init__(self, job, configSpec, target, parentId=None, reason=None):
        ConfigChange.__init__(self, lastConfigChange=target.lastConfigChange)
        TaskView.__init__(self, job.runner.manifest, configSpec, target, reason)
        AttributeManager.__init__(self)
        self.parentId = parentId or job.changeId
        self.changeId = self.parentId
        self.startTime = job.startTime or datetime.datetime.now()
        self.errors = []
        self.dryRun = job.dryRun
        self.generator = None
        self.job = job
        self.changeList = []
        self.result = None

        # set the attribute manager on the root resource
        # XXX refcontext in attributeManager should define $TARGET $HOST etc.
        # self.configuratorResource.root.attributeManager = self
        self.target.root.attributeManager = self

        # XXX set self.configSpec.target.readyState = 'pending'
        self.configurator = self.configSpec.create()

    def priority():
        doc = "The priority property."

        def fget(self):
            if self._priority is None:
                return self.configSpec.shouldRun()
            else:
                return self._priority

        def fset(self, value):
            self._priority = value

        def fdel(self):
            del self._priority

        return locals()

    priority = property(**priority())

    fallbacks = {"create": "add", "add": "update"}

    def startRun(self):
        generator = getattr(self.configurator, "run" + self.configSpec.action, None)
        fallback = self.configSpec.action
        while not generator:
            fallback = self.fallbacks.get(fallback)
            if not fallback:
                break
            generator = getattr(self.configurator, "run" + fallback, None)
        if not generator:
            generator = self.configurator.run

        # XXX remove dryRun
        self.generator = generator(self)
        assert isinstance(self.generator, types.GeneratorType)

    def send(self, change):
        result = None
        try:
            result = self.generator.send(change)
        finally:
            # serialize configuration changes
            self.commitChanges()
        return result

    def start(self):
        self.startRun()

    def _setConfigStatus(self, config, result):
        statusChanged = config.localStatus != result.readyState
        if result.configChanged is None:
            # not set so try to deduce
            if self.reason in ["config changed", "all"]:
                configChanged = (
                    statusChanged or self.changeList or self.dependenciesChanged
                )
            else:  # be conservative, assume the worse
                configChanged = True
        else:
            # setting result.configChanged will override change detection
            configChanged = result.configChanged

        if configChanged:
            config._lastConfigChange = self.changeId
        if statusChanged:
            config.localStatus = result.readyState
        logger.debug(
            "task %s statusChanged: %s configChanged %s",
            self,
            statusChanged and config.localStatus,
            configChanged and config._lastConfigChange,
        )

    def processResult(self, result):
        """
    Update the target instance with the result.

    `result.applied` indicates this configuration is active
    (essentially, the owner of the instance's configuration)
    `result.modified` indicates if a "physical" change to this system was made.
     (All combinations of these two are permissible -- modified and not applied
     means changes were made to the system that couldn't be undone.
    """
        instance = self.target
        if result.modified:
            instance._lastStateChange = self.changeId

        if result.applied:
            assert (
                result.readyState and result.readyState != Status.notapplied
            ), result.readyState
            self._setConfigStatus(instance, result)
        else:
            if instance._lastConfigChange is None:
                # if this has never been set before, record this to indicate we tried
                instance._lastConfigChange = self.changeId
            # configurator wasn't able to apply so leave the instance state as is
            # except in the case where explicitly set another status
            # e.g. if it left the instance in an error state
            if result.readyState and result.readyState != Status.notapplied:
                instance.localStatus = result.readyState

    def finished(self, result):
        assert result
        if self.generator:
            self.generator.close()
            self.generator = None

        # don't set the changeId until we're finish so that we have a higher changeid
        # than nested tasks and jobs that ran (avoids spurious config changed tasks)
        self.changeId = self.job.runner.incrementChangeId()
        # XXX2 if attributes changed validate using attributesSchema
        # XXX2 Check that configuration provided the metadata that it declared (check postCondition)
        self.processResult(result)
        resource = self.target

        if (result.applied or result.modified) and self.changeList:
            # merge changes together (will be saved with changeset)
            changes = self.changeList
            accum = changes.pop(0)
            while changes:
                accum = mergeDicts(accum, changes.pop(0))

            self._resourceChanges.updateChanges(accum, self.statuses, resource)

        self.result = result
        if result.readyState:
            self.localStatus = result.readyState
        return self

    def commitChanges(self):
        """
    This can be called multiple times if the configurator yields multiple times.
    Save the changes made each time.
    """
        changes = AttributeManager.commitChanges(self)
        self.changeList.append(changes)
        return changes

    def hasInputsChanged(self):
        """
    Evaluate configuration spec's inputs and compare with the current inputs' values
    """
        # XXX this is really a "reconfiguration" operation, which can be distinct from 'configure'
        _parameters = None
        if self.lastConfigChange:  # XXX this is never set
            changeset = self._manifest.loadConfigChange(self.lastConfigChange)
            _parameters = changeset.inputs
        if not _parameters:
            return not not self.inputs

        if set(self.inputs.keys()) != set(_parameters.keys()):
            return True  # params were added or removed

        # XXX3 not all parameters need to be live
        # add an optional liveParameters attribute to config spec to specify which ones to check

        # compare old with new
        for name, val in self.inputs.items():
            if serializeValue(val) != _parameters[name]:
                return True
            # XXX if the value changed since the last time we checked
            # if Dependency.hasValueChanged(val, lastChecked):
            #  return True
        return False

    def hasDependenciesChanged(self):
        return any(d.hasChanged(self) for d in self.dependencies.values())

    def refreshDependencies(self):
        for d in self.dependencies.values():
            d.refresh(self)

    def summary(self):
        if self.target.name != self.target.template.name:
            rname = "%s (%s)" % (self.target.name != self.target.template.name)
        else:
            rname = self.target.name

        if self.configSpec.name != self.configSpec.className:
            cname = "%s (%s)" % (self.configSpec.name, self.configSpec.className)
        else:
            cname = self.configSpec.name
        return (
            "Run {action} on resource {rname} (type {rtype}, status {rstatus}) "
            + "using configurator {cname}, priority {priority}, reason: {reason}"
        ).format(
            action=self.configSpec.action,
            rname=rname,
            rtype=self.target.template.type,
            rstatus=self.target.status.name,
            cname=cname,
            priority=self.priority.name,
            reason=self.reason,
        )

    def __repr__(self):
        return "ConfigTask(%s:%s %s)" % (
            self.target,
            self.configSpec.name,
            self.reason or "unknown",
        )


class JobOptions(object):
    """
  Options available to select which tasks are run, e.g. read-only

  does the config apply to the action?
  is it out of date?
  is it in a ok state?
  """

    defaults = dict(
        parentJob=None,
        startTime=None,
        out=None,
        verbose=0,
        resource=None,
        resources=None,
        template=None,
        useConfigurator=False,
        # default options:
        add=True,  # add new templates
        update=True,  # run configurations that whose spec has changed but don't require a major version change
        repair="error",  # or 'degraded' or "notapplied" or "none", run configurations that are not operational and/or degraded
        upgrade=False,  # run configurations with major version changes or whose spec has changed
        all=False,  # (re)run all configurations
        verify=False,  # XXX3 discover first and set status if it differs from expected state
        readonly=False,  # only run configurations that won't alter the system
        dryrun=False,  # XXX2
        planOnly=False,
        requiredOnly=False,
        revertObsolete=False,  # revert
        append=None,
        replace=None,
        cmdline=None,
    )

    def __init__(self, **kw):
        options = self.defaults.copy()
        options.update(kw)
        self.__dict__.update(options)


class Job(ConfigChange):
    """
  runs ConfigTasks and Jobs
  """

    def __init__(self, runner, rootResource, plan, jobOptions):
        super(Job, self).__init__(Status.ok)
        assert isinstance(jobOptions, JobOptions)
        self.__dict__.update(jobOptions.__dict__)
        self.dryRun = jobOptions.dryrun
        if self.startTime is None:
            self.startTime = datetime.datetime.now()
        self.jobOptions = jobOptions
        self.runner = runner
        self.plan = plan
        self.rootResource = rootResource
        self.jobRequestQueue = []
        self.unexpectedAbort = None
        # note: tasks that never run will all share this changeid
        self.changeId = runner.incrementChangeId()
        self.parentId = self.parentJob.changeId if self.parentJob else None
        self.workDone = collections.OrderedDict()

    def createTask(self, configSpec, target, parentId=None, reason=None):
        # XXX2 if 'via'/runsOn set, create remote task instead
        task = ConfigTask(self, configSpec, target, parentId, reason=reason)
        # if configSpec.hasBatchConfigurator():
        # search targets parents for a batchConfigurator
        # XXX how to associate a batchConfigurator with a resource and when is its task created?
        # batchConfigurator tasks more like a job because they have multiple changeids
        #  batchConfiguratorJob = findBatchConfigurator(configSpec, target)
        #  batchConfiguratorJob.add(task)
        #  return None

        return task

    def filterConfig(self, config, target):
        opts = self.jobOptions
        if opts.readonly and config.intent != "discover":
            return None, "read only"
        if opts.requiredOnly and not config.required:
            return None, "required"
        if opts.resource and target.name != opts.resource:
            return None, "resource"
        if opts.resources and target.name not in opts.resources:
            return None, "resources"
        return config, None

    def getCandidateTasks(self):
        # XXX plan might call job.runJobRequest(configuratorJob) before yielding
        for (configSpec, target, reason) in self.plan.executePlan():
            configSpecName = configSpec.name
            configSpec, filterReason = self.filterConfig(configSpec, target)
            if not configSpec:
                logger.debug(
                    "skipping configspec %s for %s: doesn't match %s filter",
                    configSpecName,
                    target.name,
                    filterReason,
                )
                continue

            if self.runner.isConfigAlreadyHandled(configSpec):
                # configuration may have premptively run while executing another task
                logger.debug(
                    "configspec %s for target %s already handled",
                    configSpecName,
                    target.name,
                )
                continue

            yield self.createTask(configSpec, target, reason=reason)

    def run(self):
        for task in self.getCandidateTasks():
            self.runner.addWork(task)
            if not self.shouldRunTask(task):
                continue

            if self.jobOptions.planOnly:
                if not self.cantRunTask(task):
                    logger.info(task.summary())
            else:
                logger.info("Running task %s", task)
                self.runTask(task)

            if self.shouldAbort(task):
                return self.rootResource

        # the only jobs left will be those that were added to resources already iterated over
        # and were not yielding inside runTask
        while self.jobRequestQueue:
            jobRequest = self.jobRequestQueue[0]
            job = self.runJobRequest(jobRequest)
            if self.shouldAbort(job):
                return self.rootResource

        # XXX
        # if not self.parentJob:
        #   # create a job that will re-run configurations whose parameters or runtime dependencies have changed
        #   # ("config changed" tasks)
        #   # XXX3 check for orphaned resources and mark them as orphaned
        #   #  (a resource is orphaned if it was added as a dependency and no longer has dependencies)
        #   #  (orphaned resources can be deleted by the configuration that created them or manages that type)
        #   maxloops = 10 # XXX3 better loop detection
        #   for count in range(maxloops):
        #     jobOptions = JobOptions(parentJob=self, repair='none')
        #     plan = Plan(self.rootResource, self.runner.manifest.tosca, jobOptions)
        #     job = Job(self.runner, self.rootResource, plan, jobOptions)
        #     job.run()
        #     # break when there are no more tasks to run
        #     if not len(job.workDone) or self.shouldAbort(job):
        #       break
        #   else:
        #     raise UnfurlError("too many final dependency runs")

        return self.rootResource

    def runJobRequest(self, jobRequest):
        self.jobRequestQueue.remove(jobRequest)
        resourceNames = [r.name for r in jobRequest.resources]
        jobOptions = JobOptions(parentJob=self, repair="none", resources=resourceNames)
        plan = Plan(self.rootResource.root, self.runner.manifest.tosca, jobOptions)
        childJob = Job(self.runner, self.rootResource.root, plan, jobOptions)
        assert childJob.parentJob is self
        childJob.run()
        return childJob

    def shouldRunTask(self, task):
        """
    Checked at runtime right before each task is run
    """
        try:
            priority = task.configurator.shouldRun(task)
        except Exception:
            # unexpected error don't run this
            UnfurlTaskError(task, "shouldRun failed unexpectedly", True)
            return False

        priority = toEnum(Priority, priority, Priority.ignore)
        if priority != task.priority:
            logger.debug(
                "configurator changed task %s priority from %s to %s",
                task,
                task.priority,
                priority,
            )
            task.priority = priority
        return priority > Priority.ignore

    def cantRunTask(self, task):
        """
    Checked at runtime right before each task is run

    * validate inputs
    * check pre-conditions to see if it can be run
    * check task if it can be run
    """
        try:
            canRun = False
            reason = ""
            missing = []
            skipDependencyCheck = False
            if not skipDependencyCheck:
                dependencies = list(task.target.getOperationalDependencies())
                missing = [
                    dep for dep in dependencies if not dep.operational and dep.required
                ]
            if missing:
                reason = "missing required dependencies: %s" % ",".join(
                    [dep.name for dep in missing]
                )
            else:
                errors = task.configSpec.findInvalidateInputs(task.inputs)
                if errors:
                    reason = "invalid inputs: %s" % str(errors)
                else:
                    preErrors = task.configSpec.findInvalidPreconditions(task.target)
                    if preErrors:
                        reason = "invalid preconditions: %s" % str(preErrors)
                    else:
                        errors = task.configurator.cantRun(task)
                        if errors:
                            reason = "configurator declined: %s" % str(errors)
                        else:
                            canRun = True
        except Exception:
            UnfurlTaskError(task, "cantRun failed unexpectedly", True)
            reason = "unexpected exception in cantRun"
            canRun = False

        if canRun:
            return False
        else:
            logger.info("could not run task %s: %s", task, reason)
            return "could not run: " + reason

    def shouldAbort(self, task):
        return False  # XXX3

    def summary(self):
        outputString = ""
        outputs = self.getOutputs()
        if outputs:
            outputString = "\nOutputs:\n    " + "\n    ".join(
                "%s: %s" % (name, value)
                for name, value in serializeValue(outputs).items()
            )

        if not self.workDone:
            return "Job %s completed: %s. Found nothing to do. %s" % (
                self.changeId,
                self.status.name,
                outputString,
            )

        def format(name, task):
            required = "[required]" if task.required else ""
            return "%s: %s:%s: %s" % (
                name,
                required,
                task.status.name,
                task.result or "skipped",
            )

        line1 = "Job %s completed: %s. Tasks:\n    " % (self.changeId, self.status.name)
        tasks = "\n    ".join(
            format(name, task) for name, task in self.workDone.items()
        )
        return line1 + tasks + outputString

    def getOperationalDependencies(self):
        # XXX3 this isn't right, root job might have too many and child job might not have enough
        # plus dynamic configurations probably shouldn't be included if yielded by a configurator
        for task in self.workDone.values():
            yield task

    def getOutputs(self):
        return self.rootResource.outputs.attributes

    def runQuery(self, query, trace=0):
        from .eval import evalForFunc, RefContext

        return evalForFunc(query, RefContext(self.rootResource, trace=trace))

    def runTask(self, task):
        """
    During each task run:
    * Notification of metadata changes that reflect changes made to resources
    * Notification of add or removing dependency on a resource or properties of a resource
    * Notification of creation or deletion of a resource
    * Requests a resource with requested metadata, if it doesn't exist, a task is run to make it so
    (e.g. add a dns entry, install a package).
    """
        # XXX3 recursion or loop detection
        errors = self.cantRunTask(task)
        if errors:
            return task.finished(ConfiguratorResult(False, False, result=errors))

        task.start()
        change = None
        while True:
            try:
                result = task.send(change)
            except Exception:
                UnfurlTaskError(task, "configurator.run failed", True)
                # assume the worst
                return task.finished(ConfiguratorResult(True, True, Status.error))
            if isinstance(result, TaskRequest):
                subtask = self.createTask(
                    result.configSpec, result.target, self.changeId
                )
                self.runner.addWork(subtask)
                change = self.runTask(subtask)  # returns a ConfiguratorResult
            elif isinstance(result, JobRequest):
                job = self.runJobRequest(result)
                change = job
            elif isinstance(result, ConfiguratorResult):
                retVal = task.finished(result)
                logger.info(
                    "finished running task %s: %s; %s", task, task.target.status, result
                )
                return retVal
            else:
                UnfurlTaskError(task, "unexpected result from configurator", True)
                return task.finished(ConfiguratorResult(True, True, Status.error))


class Runner(object):
    def __init__(self, manifest):
        self.manifest = manifest
        assert self.manifest.tosca
        self.lastChangeId = manifest.lastChangeId
        self.currentJob = None

    def addWork(self, task):
        key = "%s:%s:%s:%s" % (
            task.target.name,
            task.configSpec.name,
            task.configSpec.action,
            task.changeId,
        )
        self.currentJob.workDone[key] = task
        task.job.workDone[key] = task

    def isConfigAlreadyHandled(self, configSpec):
        return False  # XXX
        # return configSpec.name in self.currentJob.workDone

    def createJob(self, joboptions):
        """
    Selects task to run based on job options and starting state of manifest
    """
        root = self.manifest.getRootResource()
        assert self.manifest.tosca
        plan = Plan(root, self.manifest.tosca, joboptions)
        return Job(self, root, plan, joboptions)

    def incrementChangeId(self):
        self.lastChangeId += 1
        return self.lastChangeId

    def run(self, jobOptions=None):
        """
    """
        if jobOptions is None:
            jobOptions = JobOptions()
        job = self.createJob(jobOptions)
        self.currentJob = job
        try:
            ansibleDisplay.verbosity = jobOptions.verbose
            job.run()
        except Exception:
            job.localStatus = Status.error
            job.unexpectedAbort = UnfurlError(
                "unexpected exception while running job", True, True
            )
        self.currentJob = None
        self.manifest.commitJob(job)
        return job
