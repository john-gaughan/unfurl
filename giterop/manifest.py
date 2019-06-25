import collections
import os.path
from ruamel.yaml.comments import CommentedMap
from .tosca import ToscaSpec

from .support import ResourceChanges, AttributeManager, Status, Priority, Action, Defaults
from .runtime import OperationalInstance, Resource, Capability, Relationship
from .util import GitErOpError, toEnum
from .configurator import Dependency, ConfigurationSpec
from .repo import RevisionManager, findGitRepo
from .yamlloader import YamlConfig, loadFromRepo
from .job import ConfigChange

import logging
logger = logging.getLogger('giterop')

ChangeRecordAttributes = CommentedMap([
   ('changeId', 0),
   ('parentId', None),
   ('commitId', ''),
   ('startTime', ''),
]);

class Manifest(AttributeManager):
  """
  Loads a model from dictionary representing the manifest
  """
  def __init__(self, spec, path, localEnv=None):
    super(Manifest, self).__init__()
    self.localEnv = localEnv
    self.repo = localEnv and localEnv.instanceRepo
    self.currentCommitId = self.repo and self.repo.revision
    self.tosca = self.loadSpec(spec, path)
    self.specDigest = self.getSpecDigest(spec)
    self.revisions = RevisionManager(self)

  def loadSpec(self, spec, path):
    if 'tosca' in spec:
      toscaDef = spec['tosca']
    elif 'node_templates' in spec:
      # allow node_templates shortcut
      toscaDef = {'node_templates': spec['node_templates']}
    else:
      toscaDef = {}
    if "node_templates" in toscaDef:
      # shortcut
      toscaDef = dict(tosca_definitions_version='tosca_simple_yaml_1_0',
                  topology_template=toscaDef)
    else:
      # make sure this is present
      toscaDef['tosca_definitions_version']='tosca_simple_yaml_1_0'

    # hack so we can sneak through manifest to the yamlloader
    toscaDef = CommentedMap(toscaDef.items())
    toscaDef.manifest = self
    return ToscaSpec(toscaDef, spec.get('inputs'), spec, path)

  def _ready(self, rootResource, lastChangeId=0):
    self.rootResource = rootResource
    rootResource.attributeManager = self
    self.lastChangeId = lastChangeId

  def getRootResource(self):
    return self.rootResource

  def getBaseDir(self):
    return '.'

  def saveJob(self, job):
    pass

  def loadTemplate(self, name, lastChange=None):
    if lastChange and lastChange.specDigest != self.specDigest:
      return self.revisions.getRevision(lastChange.commitId).tosca.getTemplate(name)
    else:
      return self.tosca.getTemplate(name)

#  load instances
#    create a resource with the given template
#  or generate a template setting interface with the referenced implementations

  @staticmethod
  def loadStatus(status, instance=None):
    if not instance:
      instance = OperationalInstance()
    if not status:
      return instance

    instance._priority = toEnum(Priority, status.get('priority'))
    instance._lastStateChange = status.get('lastStateChange')
    instance._lastConfigChange = status.get('lastConfigChange')

    readyState = status.get('readyState')
    if not isinstance(readyState, collections.Mapping):
      instance._localStatus = toEnum(Status, readyState)
    else:
      instance._localStatus = toEnum(Status, readyState.get('local'))

    return instance

  @staticmethod
  def loadResourceChanges(changes):
    resourceChanges = ResourceChanges()
    if changes:
      for k, change in changes.items():
        status = change.pop('.status', None)
        resourceChanges[k] = [
          None if status is None else Manifest.loadStatus(status).localStatus,
          change.pop('.added', None),
          change
        ]
    return resourceChanges

  def loadConfigChange(self, changeId):
    """
    Reconstruct the Configuration that was applied in the past
    """
    changeSet = self.changeSets.get(changeId)
    if not changeSet:
      raise GitErOpError("can not find changeset for changeid %s" % changeId)

    configChange = ConfigChange()
    Manifest.loadStatus(changeSet, configChange)
    for (k,v) in ChangeRecordAttributes.items():
      setattr(self, k, changeSet.get(k, v))

    configChange.inputs = changeSet.get('inputs')

    configChange.dependencies = {}
    for val in changeSet.get('dependencies', []):
      key = val.get('name') or val['ref']
      assert key not in configChange.dependencies
      configChange.dependencies[key] = Dependency(val['ref'], val.get('expected'),
        val.get('schema'), val.get('name'), val.get('required'), val.get('wantList', False))

    if 'changes' in changeSet:
      configChange.resourceChanges = self.loadResourceChanges(changeSet['changes'])

    configChange.result = changeSet.get('result')
    configChange.messages = changeSet.get('messages', [])

    # XXX
    # ('action', ''),
    # ('target', ''), # nodeinstance key
    # implementationType: configurator resource | artifact | configurator class
    # implementation: repo:key#commitid | className:version
    return configChange

  # find config spec from potentially old version of the tosca template
  # get template then get node template name
  # but we shouldn't need this, except maybe to revert?
  def loadConfigSpec(self, configName, spec):
    return ConfigurationSpec(configName, spec['action'], spec['className'],
          spec.get('majorVersion'), spec.get('minorVersion',''),
          intent=toEnum(Action, spec.get('intent', Defaults.intent)),
          inputs=spec.get('inputs'), inputSchema=spec.get('inputSchema'),
          preConditions=spec.get('preConditions'), postConditions=spec.get('postConditions'))

  def loadResource(self, rname, resourceSpec, parent=None):
    # if parent property is set it overrides the parent argument
    pname = resourceSpec.get('parent')
    if pname:
      parent = self.getRootResource().findResource(pname)
      if parent is None:
        raise GitErOpError('can not find parent resource %s' % pname)

    resource = self._createNodeInstance(Resource, rname, resourceSpec, parent)
    return resource

  def _createNodeInstance(self, ctor, name, status, parent):
    operational = self.loadStatus(status)
    templateName = status.get('template', name)
    template = self.loadTemplate(templateName)
    if template is None:
      raise GitErOpError('missing resource template %s' % templateName)
    logger.debug('template %s: %s', templateName, template)

    resource = ctor(name, status.get('attributes'), parent, template, operational)
    if status.get('createdOn'):
      changeset = self.changeSets.get(status['createdOn'])
      resource.createdOn = changeset.changeRecord if changeset else None
    resource.createdFrom = status.get('createdFrom')

    for key, val in status.get('capabilities', {}).items():
      self._createNodeInstance(Capability, key, val, resource)

    for key, val in status.get('requirements', {}).items():
      self._createNodeInstance(Relationship, key, val, resource)

    for key, val in status.get('resources', {}).items():
      self._createNodeInstance(Resource, key, val, resource)

    return resource

  def findRepoFromGitUrl(self, path, isFile=True, importLoader=None, willUse=False):
    repoURL, filePath, revision = findGitRepo(path, isFile, importLoader)
    if not repoURL or not self.localEnv:
      return None, None, None, None
    basePath = importLoader.path #XXX check if dir or not
    #if not revision: #XXX
    #  revision = findPinned(repoURL) self.repoStatus
    repo, filePath, revision, bare = self.localEnv.findOrCreateWorkingDir(repoURL, isFile, revision, basePath)
    # XXX if willUse: self.updateRepoStatus(repo, revision)
    return repo, filePath, revision, bare

  def findPathInRepos(self, path, importLoader=None, willUse=False):
    """
    File path is inside a folder that is managed by repo.
    If the revision is pinned and doesn't match the repo, it might be bare
    """
    candidate = None
    if self.repo: #gets first crack
      filePath, revision, bare = self.repo.findPath(path, importLoader)
      if filePath:
        if not bare:
          return self.repo, filePath, revision, bare
        else:
          candidate = (self.repo, filePath, revision, bare)
    if self.localEnv:
      repo, filePath, revision, bare = self.localEnv.findPathInRepos(path, importLoader)
      if repo:
        if bare and candidate:
          return candidate
        else:
          # XXX if willUse: self.updateRepoStatus(repo, revision)
          return repo, filePath, revision, bare
    return None, None, None, None

  def loadHook(self, yamlConfig, templatePath, baseDir):
    self.repoStatus = yamlConfig.config.get('status', {}).get('repositories')

    name = 'spec'
    repositories = {}
    if isinstance(templatePath, dict):
      templatePath = templatePath.copy()
      # a full repository spec maybe part of the include
      repo = templatePath.get('repository', {}).copy()
      name = repo.pop('name', name)
      if repo:
        # replace spec with just its name
        templatePath['repository'] = name
        repositories = {name: repo}
    else:
      templatePath = dict(file = templatePath)

    return loadFromRepo(name, templatePath, baseDir, repositories, self)

class SnapShotManifest(Manifest):
  def __init__(self, manifest, commitId):
    self.commitId = commitId
    oldManifest = manifest.repo.git.show(commitId+':'+manifest.path)
    self.repo = manifest.repo
    self.localEnv =  manifest.localEnv
    self.manifest = YamlConfig(oldManifest, manifest.path,
                                    loadHook=self.loadHook)
    manifest = self.manifest.expanded
    spec = manifest.get('spec', {})
    super(SnapShotManifest, self).__init__(spec, self.manifest.path, manifest.localEnv)
    # just needs the spec, not root resource
    self._ready(None)
