"""
This module defines the core model and implements the runtime operations of the model.

The state of the system is represented as a collection of Resources
Each resource have a status; inert attributes that describe its state;
 and a list of configurations that manage its state.

Each configurations has a status, list of dependencies and an associated spec
Two kinds of dependencies:
 1. live attributes that the configuration's parameters depend on
 2. other configurations and resources it relies on to function properly and so it's status depends on them

A Job is generated by comparing a list of specs with the last known state of the system
Job runs tasks, each of which has a configuration spec that is executed on the running system
Each task is responsible for running one configuration and records its modifications to the system's state

Resource
Configurator
Configuration
ConfigurationSpec

Task
Job
JobOptions
Runner
"""

import six
import copy
import collections
import datetime
import sys
from itertools import chain
from enum import IntEnum
from .util import *

# question: if a configuration failed to apply should that affect the status of the configuration?
# OTOH the previous version of the configuration status is still in effect
# But if the configuration did fail could mean error at global level
# treat failed as a separate dependent configuration that can contribute to aggregate status
# and participate in resource graph
# XXX2 add upgrade required field?
# XXX2 for dependencies checking add a revision field that increments everytime configuration changes?

# XXX2 doc: notpresent is a positive assertion of non-existence while notapplied just indicates non-liveness
# notapplied is therefore the default initial state
S = Status = IntEnum("Status", "ok degraded error notapplied notpresent", module=__name__)

# ignore may must
Priority = IntEnum("Priority", "ignore optional required", module=__name__)

# omit discover exist
A = Action = IntEnum("Action", "discover instantiate revert", module=__name__)

class Defaults(object):
  shouldRun = Priority.optional
  canRun = True
  intent = Action.instantiate

# for configuration: same as last task run, but also responsible for its adopted child resources?
# status refers to current state of system not what happened when object was last applied
# semantics of priority / shouldRun / skip
# semantics of notapplied

class Operational(object):
  """
  This is an abstract base class for Jobs, Resources, and Configurations all have a Status associated with them
  and all use the same algorithm to compute their status from their dependent resouces, tasks, and configurations

  # operational: boolean: ok or degraded
  # status: ok, degraded, error, notpresent
  # degraded: non-fatal errors or didn't provide required attributes or if couldnt upgrade
  """

  # XXX2 add repairable, messages?

  # core properties to override
  @property
  def priority(self):
    return Priority.optional

  @property
  def computedStatus(self):
    return Status.notapplied

  def getOperationalDependencies(self):
    return ()

  @property
  def manualOverideStatus(self):
    return None

  # derived properties:
  @property
  def operational(self):
    return self.status == Status.ok or self.status == Status.degraded

  @property
  def status(self):
    if self.manualOverideStatus is not None:
      status = self.manualOverideStatus
    else:
      status = self.computedStatus
    if status >= Status.error:
      return status
    return self.aggregateStatus(self.getOperationalDependencies(), status)

  @property
  def required(self):
    return self.priority == Priority.required

  @staticmethod
  def aggregateStatus(statuses, defaultStatus = Status.ok):
    # error if a configuration is required and not operational
    # error if a not configuration managed child resource is required and not operational
    # notpresent if not present
    # degraded non-required configurations and resources are not operational
    #          or required configurations and resources are degraded
    # ok otherwise
    state = defaultStatus
    for status in statuses:
      assert isinstance(status, Operational), status
      if status.priority == Priority.ignore:
        continue
      if status.required:
        if not status.operational:
          state = Status.error
          break
        elif status.status == Status.degraded:
          state = Status.degraded
      elif not status.operational:
          state = Status.degraded

    return state

class OperationalInstance(Operational):
  def __init__(self, status=None, priority=None, manualOveride=None):
    self._computedStatus = toEnum(Status, status)
    self._manualOverideStatus = toEnum(Status, manualOveride)
    self._priority = toEnum(Priority, priority)
    self.dependencies = []
    #self.repairable = False # XXX2
    #self.messages = [] # XXX2

  def getOperationalDependencies(self):
    return self.dependencies

  def computedStatus():
    doc = "The computedStatus property."
    def fget(self):
      return self._computedStatus
    def fset(self, value):
      self._computedStatus = value
    def fdel(self):
      del self._computedStatus
    return locals()
  computedStatus = property(**computedStatus())

  def manualOverideStatus():
    doc = "The manualOverideStatus property."
    def fget(self):
      return self._manualOverideStatus
    def fset(self, value):
      self._manualOverideStatus = value
    def fdel(self):
      del self._manualOverideStatus
    return locals()
  manualOverideStatus = property(**manualOverideStatus())

  def priority():
    doc = "The priority property."
    def fget(self):
      return self._priority
    def fset(self, value):
      self._priority = value
    def fdel(self):
      del self._priority
    return locals()
  priority = property(**priority())

class Resource(Operational):
  def __init__(self, name='', attributes=None, configurations=None, parent=None, children=None):
    self.name = name # XXX2 guarantee name uniqueness
    self.attributes = attributes or {}
    self.configurations = dict( (c.name, c) for c in (configurations or []) )
    # affirmatively absent configurations -- i.e. failed to apply or explicitly not present
    # we don't want any notapplied or notpresent in configurations

    # excludedConfigurations are configurations intended as notpresent
    # or an update to a configuration that failed to apply
    self.excludedConfigurations = {}
    self.container = parent
    if parent:
      self.resources.append(self)
    self.resources = children or []

  def getOperationalDependencies(self):
    return self.configurations.values()

  def getSelfAndDescendents(self):
    "Recursive descendent including self"
    yield self
    for r in self.resources:
      for descendent in r.yieldDescendents():
        yield descendent

  @property
  def descendents(self):
    return list(self.getSelfAndDescendents())

  def findResource(self, resourceid):
    if self.name == resourceid:
      return self
    for r in self.resources:
      if r.name == resourceid:
        return match
    return None

  def addResource(self, resource):
    assert resource.container == self
    self.resources.append(resource)

  def yieldParents(self):
    "yield self and ancestors"
    resource = self
    while resource:
      yield resource
      resource = resource.container

  @property
  def ancestors(self):
    return list(self.yieldParents())

  @property
  def root(self):
    return self.ancestors[-1]

  def setConfiguration(self, configuration):
    if configuration.status == Status.notpresent and configuration.intent == Action.revert:
      self.configurations.pop(configuration.name, None)
      self.excludedConfigurations[configuration.name] = configuration
    else:
      self.configurations[configuration.name] = configuration

  def getAllConfigurationsDeep(self):
    for resource in self.getSelfAndDescendents():
      for config in resource.allConfigurations:
        yield config

  @property
  def allConfigurations(self):
    return chain(self.configurations.values(), self.excludedConfigurations.values())

@six.add_metaclass(AutoRegisterClass)
class Configurator(object):

  def __init__(self, configurationSpec):
    self.configurationSpec = configurationSpec

  def run(self, task):
    yield None

  def dryRun(self, task):
    yield None

  def canRun(self, task):
    """
    Does this configurator support the requested action and parameters
    given the current state of the resource?
    (e.g. can we upgrade from the previous configuration?)
    """
    return Defaults.canRun

  def shouldRun(self, task):
    """Does this configuration need to be run?"""
    return task.newConfiguration.configurationSpec.shouldRun(task.newConfiguration)

  def checkConfigurationStatus(self, task):
    """Is this configuration still valid?"""
    # XXX2
    # should be called during when checking dependencies
    return Status.ok

# XXX3 document versions:
# configurator api version (encoded in api namespace): semantics of the interface giterop uses
# configurator version: breaking change if interpretation of configuration parameters change
# configuration spec version: encompasses installed version -- what is installed

class ConfigurationSpec(object):
  def __init__(self, name=None, target=None, className=None, majorVersion=None, minorVersion='',
      intent=Defaults.intent):
    assert name and target and className and majorVersion is not None, "missing required arguments"
    self.name = name
    self.target = target # name of owner resource
    self.className = className
    self.majorVersion = majorVersion
    self.minorVersion = minorVersion
    self.intent = intent
    # XXX2 add ensures

  def validateParameters(self, parameters):
    return True

  def create(self):
    return lookupClass(self.className)(self)

  def canRun(self, configuration):
    return Defaults.canRun

  def shouldRun(self, configuration):
    return Defaults.shouldRun

  def resolveParameters(self, configuration):
    return {}

  def getPostConditions(self):
    return {}

  def copy(self, **mods):
    args = self.__dict__.copy()
    args.update(mods)
    return ConfigurationSpec(**args)

  def __eq__(self, other):
    if not isinstance(other, ConfigurationSpec):
      return False
    return (self.name == other.name and self.target == other.target and self.className == other.className
      and self.majorVersion == other.majorVersion and self.minorVersion == other.minorVersion
      and self.intent == other.intent)

class Configuration(OperationalInstance):
  def __init__(self, spec, resource, status=Status.notapplied, dependencies=None):
    super(Configuration, self).__init__(status)
    self.configurationSpec = spec
    assert resource and spec and resource.name == spec.target
    self.resource = resource
    self.dependencies = dependencies or {}
    self.parameters = None

  def priority():
    doc = "The priority property."
    def fget(self):
      if self._priority is None:
        return self.configurationSpec.shouldRun(self)
      else:
        return self._priority
    def fset(self, value):
      self._priority = value
    def fdel(self):
      del self._priority
    return locals()
  priority = property(**priority())

  def getOperationalDependencies(self):
    conditions = chain(self.dependencies.values(), self.configurationSpec.getPostConditions().values())
    for conditionPredicate in conditions:
      yield conditionPredicate(self)

  @property
  def name(self):
    return self.configurationSpec.name

  @property
  def intent(self):
    return self.configurationSpec.intent

  def refreshParameters(self):
    self.parameters = self.configurationSpec.resolveParameters(self)
    return self.parameters

  def hasParametersChanged(self):
    return self.parameters is not None and self.configurationSpec.resolveParameters(self) != self.parameters

  # XXX2
  # @property
  # def outdated(self):
  #   if self._outdated:
  #     return True
  #   for config in self.dependencies:
  #     if config.outdated:
  #       return True
  #   return False

  #XXX2 like outdated
  #@property
  #def obsolete(self):

class Task(object):
  """
  Configurator records the changes to the system's state via the Task interface

  Find resources
  record new resources
  modify / delete resources
  record / update / remove dependencies expressed as conditions
   (by updating and/or instantiating the ones defined in the spec)

  Configurator's only other interface to modifying the system is through createSubTask()
  """
  def __init__(self, job, spec, currentConfiguration):
    self.job = job
    self.oldConfiguration = currentConfiguration
    if currentConfiguration:
      self.currentConfiguration = currentConfiguration
      # new configuration's status starts out as the previous one
      self.newConfiguration = Configuration(spec, currentConfiguration.resource, currentConfiguration.status)
    else:
      resource = self.findResource(spec.target)
      self.newConfiguration = Configuration(spec, resource, Status.notapplied)
      self.currentConfiguration = self.newConfiguration
    self.configurator = spec.create()
    self.updateResources = {}
    self.messages = []
    self.addedResources = []
    self.removedResources = []
    self.errors = []
    self.startTime = job.startTime or datetime.datetime.now()

  def validateParameters(self):
    spec = self.newConfiguration.configurationSpec
    return spec.validateParameters(
                  spec.resolveParameters(self.newConfiguration))

  def start(self):
    if self.job.dryRun:
      generator = self.configurator.dryRun(self)
    else:
      generator = self.configurator.run(self)
    # set currentConfiguration (on the target resource too)
    config = self.newConfiguration
    self.currentConfiguration = config
    config.resource.setConfiguration(config)
    config.refreshParameters()
    return generator

  def finished(self, result):
    #XXX2 Check that configuration provided the metadata that it declared it would provide
    self.currentConfiguration.computedStatus = result
    resource = self.currentConfiguration.resource
    if result == Status.notapplied:
      # if the status is notapplied when finished set currentConfiguration back to the previous one
      if self.oldConfiguration:
        assert self.oldConfiguration.name == self.newConfiguration.name
        self.currentConfiguration = self.oldConfiguration
        resource.setConfiguration(self.oldConfiguration)
        resource.excludedConfigurations[self.newConfiguration.name] = self.newConfiguration
      self.revertChanges()
    elif result == Status.notpresent and self.currentConfiguration.intent == Action.revert:
      # set again, it might not have been in excludedConfigurations
      resource.setConfiguration(self.newConfiguration)

    return self.newConfiguration

  def revertChanges(self):
    # XXX2 attributes set by configurations should be per configuration
    # mark which ones are exposed as public resource attribues and subject to merge conflicts

    # N.B. this will revert any changes made by other tasks run at the same time
    # which should only be subtasks and jobs
    for (resource, attributes) in self.updateResources.values():
      resource.attributes = attributes
    self.updateResources = {}

  def addResource(self, templateName, name, metadata):
    # XXX2 should indicate what kind of dependency
    # instantiate new resource and a job that will run it
    resource = self.job.runner.manifest.createResource(templateName, name, metadata)
    # configurator can yield the returned job if it wants it to be run right away
    # otherwise it will be run later
    self.addedResources.append(resource)
    return self.job.addChildJob(self, resource)

  def removeResource(self, resource):
    # XXX2 should indicate what kind of dependency
    self.removedResources.append(resource)
    # XXX2 only do this if its orphaned:
    resource.container.resources.remove(resource)

  def addMessage(self, message):
    self.messages.append(message)

  def updateResource(self, resource, updated={}, deleted=()):
    # XXX2 should indicate what kind of dependency, including if the attributes changes are important
    # save original
    if resource.name not in self.updateResources:
      self.updateResources[resource.name] = (resource, copy.copy(resource.attributes))
    # update the resource
    for key in deleted:
      resource.attributes.pop(key, None)
    resource.attributes.update(updated)

  def updateDependency(self, name, dependencyTemplateName, args=None):
    """
    Dynamically update the conditions this configuration depends on.
    """
    if dependencyTemplateName:
      dependency = self.job.runner.manifest.createDependency(self.currentConfiguration.configurationSpec, dependencyTemplateName, args)
      self.currentConfiguration.dependencies.update(name, dependency)
    else:
      self.currentConfiguration.dependencies.pop(name, None)

  def findResource(self, name):
     return self.job.runner.manifest.getRootResource().findResource(name)

  # XXX2 need a way to associate resource templates with constructor / controller configs
  # for now need to create task that creates it
  #def createResource(self, resource):
  #  return Task(self, resource)

  def createConfigurationSpec(self, configurationTemplateName, configurationkws=None):
    return self.job.runner.manifest.createConfigurationSpec(
        configurationTemplateName, configurationkws)

  # configurations created by subtasks are transient insofar as the are not part of the spec,
  # but they are persistent in that they recorded as part of the resource's state and status
  def createSubTask(self, configSpec):
    return Task(self.job, configSpec, None)

  def __str__(self):
    return "Task: " + str(self.configuration)

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
    out=sys.stdout,

    resource=None,
    configuration=None,

    # default options:
    add=True, # run newly added configurations
    update=True, # run configurations that whose spec has changed but don't require a major version change
    repair="error", # or 'degraded', run configurations that are not operational and/or degraded

    upgrade=False, # run configurations with major version changes or whose spec has changed
    all=False, # (re)run all configurations
    verify=False, # XXX2 discover first and set status if it differs from expected state
    readOnly=False, # only run configurations that won't alter the system
    dryRun=False, # XXX2
    requiredOnly=False,
    revertObsolete=False #revert
    )

  def __init__(self, **kw):
    options = self.defaults.copy()
    options.update(kw)
    self.__dict__.update(options)

class Job(OperationalInstance):
  def __init__(self, runner, rootResource, specs, jobOptions):
    super(Job, self).__init__(Status.ok)
    assert isinstance(jobOptions, JobOptions)
    self.__dict__.update(jobOptions.__dict__)
    self.runner = runner
    self.wantedSpecs = specs
    self.rootResource = rootResource
    self.jobQueue = []
    self.unexpectedAbort = None

  def addChildJob(self, resource, specs=None):
    jobOptions = JobOptions(parentJob=self, repair=None)
    childJob = Job(self.runner, resource, specs or [], jobOptions)
    assert childJob.parentJob is self
    # print('adding', childJob, 'to', self)
    self.jobQueue.append(childJob)
    return childJob

  def removeFromParentQueue(self):
    if self.parentJob:
      # print('removing', self, 'from', self.parentJob)
      self.parentJob.jobQueue.remove(self)

  def includeTask(self, config, lastChange):
    """
spec (config):
  intent: discover instantiate revert
  config
  version

status (lastChange):
  state: ok degraded error notpresent
  Current runtime state compared to requirements for last applied spec:
    no longer needed, misconfigured / parameters changed, error/degraded, missing/should be applied
    (only discovered at runtime as state is updated but persisted for next run)
  action: discover instantiate revert

status compared to current spec is different: compare difference for each:
  config: same different missing orphan
  intent vs. action: (d, i) (i, d) (i, r) (r, i) (d, r) (r, d)
  version: newer older
    """
    assert config or lastChange
    if self.all and config:
      return config
    if config and not lastChange:
      if self.add:
        # XXX2 what if config.intent == A.revert? return None
        return config
      else:
        return None
    spec = lastChange.configurationSpec
    if lastChange and not config:
      if self.revertObsolete:
        return spec.copy(intent=A.revert)
      if self.all:
        return spec
    elif spec != config:
      # the user changed the configuration:
      if config.intent == A.revert and lastChange.status == S.notpresent:
        return None # nothing to revert
      if self.upgrade:
        return config
      if lastChange.status == S.notpresent and spec.intent != config.intent and self.add:
        # this case is essentially a re-added config, so re-run it
        return config
      if self.update:
        # apply the new configuration unless it will trigger a major version change
        if config.intent != A.revert and spec.majorVersion != config.majorVersion:
          return config
    # there isn't a new config to run, see if the last applied config needs to be re-run
    return self.checkForRepair(lastChange)

  def checkForRepair(self, lastChange):
    assert lastChange
    spec = lastChange.configurationSpec
    if lastChange.hasParametersChanged() and self.update:
      return spec
    if lastChange.status == S.ok or not self.repair:
        # XXX2 what if status is notapplied or notpresent ??
        return None
    if self.repair == "degraded":
      assert lastChange.status > S.ok, lastChange.status
      return spec # repair this
    elif lastChange.status == S.degraded:
      assert self.repair == 'error', self.repair
      return None # skip repairing this
    else:
      assert self.repair == 'error', "repair: %s status: %s" % (self.repair, lastChange.status)
      # XXX2 what if status is notapplied or notpresent ??
      return spec # repair this

  def getCurrentConfigurations(self):
    return self.rootResource.getAllConfigurationsDeep()

  def findConfigurations(self, resource):
    for config in self.wantedSpecs:
      if config.target == resource.name:
        yield config
      # XXX3 to support rule-based configurations:
      # if config.matches(resource):
      #   if config.isTemplate:
      #     yield config.copy(target=resource.name)
      #   else:
      #     yield config

  # predictability, clarity, static analysis
  # correctness: state changes are live
  # simplicity / understandable, easy to use and implement
  # state changes may place prior configurations in a obsolete, error/degraded, or outdated/misconfigured state
  def getCandidateTasks(self):
    """
    Find candidate tasks

    Given declared spec, current status, and job options, generate selector

    does the config apply to the action?
    is it out of date?
    is it in a ok state?
    has its configuration changed?
    has its dependencies changed?
    are the resources it modifies in need of repair?
    manual override (include / skip)

    # intent: discover instantiate revert
    # version
    # configuration
    """
    # list of resources yielded here may dynamically change if tasks are run during iteration
    # we do this so as to reflect the state of the system as accurately as possible when tasks are run
    # but this means added resources whose parent has already been iterated over would be skipped
    # and updates to a resource already iterated over may render it inconsistent with the last run
    for resource in self.rootResource.getSelfAndDescendents():
      existing = resource.configurations.copy()
      for config in self.findConfigurations(resource):
        if self.runner.isConfigAlreadyHandled(config):
          # configuration may have premptively run while executing another task
          continue
        lastChange = existing.pop(config.name, None)
        config = self.includeTask(config, lastChange)
        if config and self.filterConfig(config):
            yield Task(self, config, lastChange)

      if self.all or self.revertObsolete:
        for change in existing.values():
          # it's an orphaned config
          config = self.includeTask(None, change)
          if self.runner.isConfigAlreadyHandled(config):
            # configuration may have premptively run while executing another task
            continue
          if config and not self.filterConfig(config):
            yield Task(self, config, change)

  def filterConfig(self, config):
    if self.readOnly and config.intent != 'discover':
      return None
    if self.requiredOnly and not config.required:
      return None
    if self.resource and config.target != self.resource:
      return None
    if self.configuration and config.name != self.configuration:
      return None
    return config

  def runTask(self, task):
    """
    During each task run:
    * Notification of metadata changes that reflect changes made to resources
    * Notification of add or removing dependency on a resource or properties of a resource
    * Notification of creation or deletion of a resource
    * Requests a resource with requested metadata, if it doesn't exist, a task is run to make it so
    (e.g. add a dns entry, install a package).
    XXX2 need a way for configurator to declare that is the manager of a particular resource or type of resource or metadata so we know to handle that request
    """
    # XXX2 recursion or loop detection
    if not self.canRunTask(task):
      return Status.notapplied

    generator = task.start()
    change = None
    while True:
      try:
        result = generator.send(change)
      except Exception as err:
        generator.close()
        err = GitErOpTaskError(task, "configurator.run failed")
        return task.finished(Status.error)
      if isinstance(result, Task):
        change = self.runTask(result)
      elif isinstance(result, Job):
        change = result.run()
      elif isinstance(result, Status):
        generator.close()
        return task.finished(result)
      else:
        generator.close()
        GitErOpTaskError(task, 'unexpected result from configurator')
        return task.finished(Status.error)

  def run(self):
    self.removeFromParentQueue()
    for task in self.getCandidateTasks():
      self.runner.addWork(task)
      if not self.shouldRunTask(task):
        continue

      self.runTask(task)
      if not self.parentJob:
        # only check when running top level tasks
        self.computedStatus = self.checkStatusAfterRun()

      if self.shouldAbort(task):
        return self.rootResource

    # the only tasks and jobs left will be those added to parent resources already iterated over
    # and also not yielded to runTask
    if not self.parentJob:
      # default child job will only rerun configurations whose parameters have changed
      # (XXX3 but we only do this once, should we keep checking?)
      self.addChildJob(self.rootResource)
      while self.jobQueue:
        job = self.jobQueue[0]
        job.run()
        if self.shouldAbort(job):
          return self.rootResource

    return self.rootResource

  def shouldRunTask(self, task):
    """
    Checked at runtime right before each task is run

    * check "when" conditions to see if it should be run
    * check task if it should be run
    """
    try:
      priority = task.configurator.shouldRun(task)
    except Exception as err:
      #unexpected error don't run this
      GitErOpTaskError(task, "shouldRun failed")
      return False

    task.newConfiguration.priority = priority
    return priority > Priority.ignore

  def canRunTask(self, task):
    """
    Checked at runtime right before each task is run

    * validate parameters
    * check "required"/pre-conditions to see if it can be run
    * check task if it can be run
    """
    try:
      canRun = (task.validateParameters()
              and task.newConfiguration.configurationSpec.canRun(task.newConfiguration)
              and task.configurator.canRun(task))
    except Exception as err:
      GitErOpTaskError(task, "shouldRun failed")
      canRun = False

    if canRun:
      return True
    else:
      config = task.newConfiguration
      config.computedStatus = Status.notapplied
      if task.oldConfiguration:
        config.resource.excludedConfigurations[config.name] = config
      return False

  def shouldAbort(self, task):
    return False #XXX2

  def summary(self):
    return "XXX2"

  def checkStatusAfterRun(self):
    """
    After each task has run:
    * Check dependencies:
    ** check that runtime-(post-)conditions still hold for configurations that might have been affected by changes
    ** check for configurations whose parameters might have been affected by changes, mark them as "configuration changed"
    (simple implementation for both: check all configurations (requires saving previous inputs))
    ** XXX3 check for orphaned resources and mark them as orphaned
      (a resource is orphaned if it was added as a dependency and no longer has dependencies)
      (orphaned resources can be deleted by the configuration/configurator that created them or manages that type)
    """
    def yieldAllDependencies():
      for configuration in self.getCurrentConfigurations():
        for dependency in configuration.getOperationalDependencies():
          yield dependency

    return Operational.aggregateStatus(yieldAllDependencies())

  def getOperationalDependencies(self):
    for task in self.runner.workDone.values():
      yield task.currentConfiguration

class Manifest(object):
  def __init__(self, rootResource, specs, templates=None):
    self.rootResource = rootResource
    self.specs = specs
    self.templates = templates or {}

  def getRootResource(self):
    return self.rootResource

  def getConfigurationSpecs(self):
    return self.specs

  def createConfigurationSpec(self, configurationTemplateName, configurationKws):
    configurationkws = self.templates.get(configurationTemplateName, {})
    configurationkws.update(configurationKws or {})
    return ConfigurationSpec(**configurationkws)

  def createResource(self, templateName=None, name=None, attributes=None, parent=None, configurationSpecs=None):
    assert name, "must specify a name"
    assert not self.rootResource.findResource(name), "name %s isn't unique" % name
    resource = Resource(name, attributes, parent=parent or self.getRootResource())
    if configurationSpecs:
      for spec in configurationSpecs:
        resource.setConfiguration(Configuration(spec, resource))
    return resource

  def createDependency(self, configurationSpec, dependencyTemplateName, args=None):
    return None

  def saveJob(self, job, workDone):
    pass

class Runner(object):
  def __init__(self, manifest):
    self.manifest = manifest
    self.workDone = collections.OrderedDict()

  def addWork(self, task):
    config = task.currentConfiguration
    self.workDone[(config.resource.name, config.name)] = task

  def isConfigAlreadyHandled(self, configSpec):
    return (configSpec.target, configSpec.name) in self.workDone

  def createJob(self, joboptions):
    """
    Selects task to run based on job options and starting state of manifest
    """
    return Job(self, self.manifest.getRootResource(), self.manifest.getConfigurationSpecs(), joboptions)

  def run(self, joboptions):
    """
    """
    job = self.createJob(joboptions)
    try:
      job.run()
    except Exception:
      job.computedStatus = Status.error
      job.unexpectedAbort = GitErOpError("unexpected exception while running job", True)

    self.manifest.saveJob(job, self.workDone)
    return job
