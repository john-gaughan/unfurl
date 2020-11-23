"""
This module implements creating and cloning project and ensembles as well Unfurl runtimes.
"""
import uuid
import os
import os.path
import datetime
import sys
import shutil
from . import DefaultNames, getHomeConfigPath
from .tosca import TOSCA_VERSION
from .repo import Repo, GitRepo, splitGitUrl, isURLorGitPath
from .util import UnfurlError, getBaseDir
from .localenv import LocalEnv, Project
import random
import string

_templatePath = os.path.join(os.path.abspath(os.path.dirname(__file__)), "templates")


def renameForBackup(dir):
    ctime = datetime.datetime.fromtimestamp(os.stat(dir).st_ctime)
    new = dir + "." + ctime.strftime("%Y-%m-%d-%H-%M-%S")
    os.rename(dir, new)
    return new


def get_random_password(count=12, prefix="uv"):
    srandom = random.SystemRandom()
    start = string.ascii_letters + string.digits
    source = string.ascii_letters + string.digits + "%&()*+,-./:<>?=@[]^_`{}~"
    return prefix + "".join(
        srandom.choice(source if i else start) for i in range(count)
    )


def _writeFile(folder, filename, content):
    if not os.path.isdir(folder):
        os.makedirs(os.path.normpath(folder))
    filepath = os.path.join(folder, filename)
    with open(filepath, "w") as f:
        f.write(content)
    return filepath


def writeTemplate(folder, filename, template, vars, templateDir=None):
    from .runtime import NodeInstance
    from .eval import RefContext
    from .support import applyTemplate

    if templateDir and not os.path.isabs(templateDir):
        templateDir = os.path.join(_templatePath, templateDir)
    if not templateDir or not os.path.exists(os.path.join(templateDir, template)):
        templateDir = _templatePath  # default

    with open(os.path.join(templateDir, template)) as f:
        source = f.read()
    instance = NodeInstance()
    instance._baseDir = _templatePath
    content = applyTemplate(source, RefContext(instance, vars))
    return _writeFile(folder, filename, content)


def writeProjectConfig(
    projectdir,
    filename=DefaultNames.LocalConfig,
    templatePath=DefaultNames.LocalConfig + ".j2",
    vars=None,
    templateDir=None,
):
    _vars = dict(include="", manifestPath=None)
    if vars:
        _vars.update(vars)
    return writeTemplate(projectdir, filename, templatePath, _vars, templateDir)


def renderHome(homePath):
    homedir, filename = os.path.split(homePath)
    writeTemplate(homedir, DefaultNames.Ensemble, "manifest.yaml.j2", {}, "home")
    configPath = writeProjectConfig(
        homedir, filename, "unfurl.yaml.j2", templateDir="home"
    )
    writeTemplate(homedir, ".gitignore", "gitignore.j2", {})
    return configPath


def createHome(home=None, render=False, replace=False, **kw):
    """
    Create the home project if missing
    """
    homePath = getHomeConfigPath(home)
    if not homePath:
        return None
    exists = os.path.exists(homePath)
    if exists and not replace:
        return None

    homedir, filename = os.path.split(homePath)
    if render:  # just render
        repo = None
    else:
        if exists:
            renameForBackup(homedir)
        repo = _createRepo(homedir)

    configPath = renderHome(homePath)
    if not render and not kw.get("no_runtime"):
        initEngine(homedir, kw.get("runtime") or "venv:")
    if repo:
        createProjectRepo(homedir, repo, True, addDefaults=False, templateDir="home")
        repo.repo.git.add("--all")
        repo.repo.index.commit("Create the unfurl home repository")
        repo.repo.git.branch("rendered")  # now create a branch
    return configPath


def _createRepo(gitDir, ignore=True):
    import git

    if not os.path.isdir(gitDir):
        os.makedirs(gitDir)
    repo = git.Repo.init(gitDir)
    repo.index.add(addHiddenGitFiles(gitDir))
    repo.index.commit("Initial Commit for %s" % uuid.uuid1())

    if ignore:
        Repo.ignoreDir(gitDir)
    return GitRepo(repo)


def writeServiceTemplate(projectdir):
    vars = dict(version=TOSCA_VERSION)
    return writeTemplate(
        projectdir, "service-template.yaml", "service-template.yaml.j2", vars
    )


def writeEnsembleManifest(
    destDir, manifestName, specRepo, specDir=None, extraVars=None
):
    if extraVars is None:
        # default behaviour is to include the ensembleTemplate
        # in the root of the specDir
        extraVars = dict(ensembleTemplate=DefaultNames.EnsembleTemplate)

    if specDir:
        specDir = os.path.abspath(specDir)
    else:
        specDir = ""
    vars = dict(specRepoUrl=specRepo.getGitLocalUrl(specDir, "spec"))
    vars.update(extraVars)
    return writeTemplate(destDir, manifestName, "manifest.yaml.j2", vars)


def addHiddenGitFiles(gitDir):
    # write .gitignore and  .gitattributes
    gitIgnorePath = writeTemplate(gitDir, ".gitignore", "gitignore.j2", {})
    gitAttributesContent = "**/*%s merge=union\n" % DefaultNames.JobsLog
    gitAttributesPath = _writeFile(gitDir, ".gitattributes", gitAttributesContent)
    return [os.path.abspath(gitIgnorePath), os.path.abspath(gitAttributesPath)]


def createProjectRepo(
    projectdir,
    repo,
    mono,
    addDefaults=True,
    projectConfigTemplate=None,
    templateDir=None,
    submodule=False,
):
    """
    Creates a folder named `projectdir` with a git repository with the following files:

    unfurl.yaml
    local/unfurl.yaml
    ensemble-template.yaml
    ensemble/ensemble.yaml

    Returns the absolute path to unfurl.yaml
    """
    # write the project files
    localConfigFilename = DefaultNames.LocalConfig
    manifestPath = os.path.join(DefaultNames.EnsembleDirectory, DefaultNames.Ensemble)

    vars = dict(vaultpass=get_random_password())
    # manifestPath should be in local if ensemble is a separate repo and not a submodule
    if addDefaults and not (mono or submodule):
        vars["manifestPath"] = manifestPath
    writeProjectConfig(
        os.path.join(projectdir, "local"),
        localConfigFilename,
        "unfurl.local.yaml.j2",
        vars,
        templateDir,
    )

    localInclude = "+?include: " + os.path.join("local", localConfigFilename)
    vars = dict(include=localInclude)
    # manifestPath should here if ensemble is part of the repo or a submodule
    if addDefaults and (mono or submodule):
        vars["manifestPath"] = manifestPath
    projectConfigPath = writeProjectConfig(
        projectdir,
        DefaultNames.LocalConfig,
        projectConfigTemplate or DefaultNames.LocalConfig + ".j2",
        vars,
        templateDir,
    )
    files = [projectConfigPath]

    if addDefaults:
        # write ensemble-template.yaml
        ensembleTemplatePath = writeTemplate(
            projectdir, DefaultNames.EnsembleTemplate, "manifest-template.yaml.j2", {}
        )
        files.append(ensembleTemplatePath)
        ensembleDir = os.path.join(projectdir, DefaultNames.EnsembleDirectory)
        manifestName = DefaultNames.Ensemble
        if mono:
            ensembleRepo = repo
        else:
            ensembleRepo = _createRepo(ensembleDir, not submodule)
        extraVars = dict(
            ensembleUri=ensembleRepo.getUrlWithPath(
                os.path.abspath(os.path.join(ensembleDir, manifestName))
            )
        )
        # write ensemble/ensemble.yaml
        manifestPath = writeEnsembleManifest(
            ensembleDir, manifestName, repo, extraVars=extraVars
        )
        if mono:
            files.append(manifestPath)
        else:
            ensembleRepo.repo.index.add([os.path.abspath(manifestPath)])
            ensembleRepo.repo.index.commit("Default ensemble repository boilerplate")

        if submodule:
            repo.addSubModule(ensembleDir)

    repo.commitFiles(files, "Create a new unfurl repository")
    return projectConfigPath


def createProject(
    projectdir, home=None, mono=False, existing=False, empty=False, submodule=False, **kw
):
    if existing:
        repo = Repo.findContainingRepo(projectdir)
        if not repo:
            raise UnfurlError("Could not find an existing repository")
    else:
        repo = None
    # creates home if it doesn't exist already:
    newHome = createHome(home, **kw)

    if repo:
        repo.repo.index.add(addHiddenGitFiles(projectdir))
        repo.repo.index.commit("Adding Unfurl project")
    else:
        repo = _createRepo(projectdir)

    # XXX add project to ~/.unfurl_home/unfurl.yaml
    projectConfigPath = createProjectRepo(
        projectdir, repo, mono, not empty, submodule=submodule
    )

    if not newHome and not kw.get("no_runtime") and kw.get("runtime"):
        # if runtime was explicitly set and we aren't creating the home project
        # then initialize the runtime here
        initEngine(projectdir, kw.get("runtime"))

    return newHome, projectConfigPath


def cloneLocalRepos(manifest, sourceProject, targetProject):
    # We need to clone repositories that are local to the source project
    # otherwise we won't be able to find them
    for repoSpec in manifest.tosca.template.repositories.values():
        if repoSpec.name == "self":
            continue
        repo = sourceProject.findRepository(repoSpec)
        if repo:
            targetProject.findOrClone(repo)


def _createEnsembleRepo(manifest):
    destDir = os.path.dirname(manifest.manifest.path)
    repo = _createRepo(destDir)

    manifest.metadata["uri"] = repo.getUrlWithPath(manifest.manifest.path)
    with open(manifest.manifest.path, "w") as f:
        manifest.dump(f)

    repo.repo.index.add([manifest.manifest.path])
    repo.repo.index.commit("Default ensemble repository boilerplate")
    return repo


def _looksLike(path, name):
    # in case path is a directory:
    if os.path.isfile(os.path.join(path, name)):
        return path, name
    if path.endswith(name):  # name is explicit so don't need to check if file exists
        return os.path.split(path)
    return None


def _getEnsemblePaths(sourcePath, sourceProject):
    """
    Returns either a pointer to the ensemble to clone
    or a dict of variables to pass to an ensemble template to create a new one

    if sourcePath doesn't exist, return {}

    look for an ensemble given sourcePath (unless sourcePath looks like a service template)
    if that fails look for (ensemble-template, service-template) if sourcePath is a directory
    otherwise
        return {}
    """
    template = None
    if not os.path.exists(sourcePath or '.'):
        return {}
    isServiceTemplate = sourcePath.endswith(DefaultNames.ServiceTemplate)
    if not isServiceTemplate:
        # we only support cloning TOSCA service templates if their names end in "service-template.yaml"
        try:
            localEnv = LocalEnv(sourcePath, project=sourceProject)
            sourceDir = sourceProject.getRelativePath(os.path.dirname(localEnv.manifestPath))
            return dict(sourceDir=sourceDir, localEnv=localEnv)
        except:
            pass

    # didn't find the specified file (or the default ensemble if none was specified)
    # so if sourcePath was a directory try for one of the default template files
    if isServiceTemplate or os.path.isdir(sourcePath):
        # look for an ensemble-template or service-template in source path
        template = _looksLike(sourcePath, DefaultNames.EnsembleTemplate)
        if template:
            sourceDir = sourceProject.getRelativePath(template[0])
            return dict(sourceDir=sourceDir, ensembleTemplate=template[1])
        template = _looksLike(sourcePath, DefaultNames.ServiceTemplate)
        if template:
            sourceDir = sourceProject.getRelativePath(template[0])
            return dict(sourceDir=sourceDir, serviceTemplate=template[1])
        # nothing valid found
    return {}


def createNewEnsemble(templateVars, project, targetPath):
    """
    If "localEnv" is in templateVars, clone that ensemble;
    otherwise create one from a template with templateVars
    """
    # targetPath is relative to the project root
    from unfurl import yamlmanifest

    assert not os.path.isabs(targetPath)
    if not targetPath:
        destDir, manifestName = DefaultNames.EnsembleDirectory, DefaultNames.Ensemble
    elif targetPath.endswith(".yaml") or targetPath.endswith(".yml"):
        destDir, manifestName = os.path.split(targetPath)
    else:
        destDir = targetPath
        manifestName = DefaultNames.Ensemble
    # choose a destDir that doesn't conflict with an existing folder
    # (i.e. if default ensemble already exists)
    destDir = project.getUniquePath(destDir)
    # destDir is now absolute
    targetPath = os.path.normpath(os.path.join(destDir, manifestName))

    if "localEnv" not in templateVars:
        # we found a template file to clone
        assert project
        sourceDir = os.path.normpath(os.path.join(project.projectRoot, templateVars["sourceDir"]))
        specRepo, relPath, revision, bare = project.findPathInRepos(sourceDir)
        if not specRepo:
            raise UnfurlError(
                '"%s" is not in a git repository. Cloning from plain file directories not yet supported'
                % os.path.abspath(sourceDir)
            )
        manifestPath = writeEnsembleManifest(
            os.path.join(project.projectRoot, destDir), manifestName, specRepo, sourceDir, templateVars
        )
        localEnv = LocalEnv(manifestPath, project=project)
        manifest = yamlmanifest.ReadOnlyManifest(localEnv=localEnv)
    elif templateVars:
        # didn't find a template file
        # look for an ensemble at the given path or use the source project's default
        manifest = yamlmanifest.clone(templateVars["localEnv"], targetPath)
    else:
        raise UnfurlError("can't find anything to clone")
    _createEnsembleRepo(manifest)
    return destDir, manifest
    # XXX need to add manifest to unfurl.yaml


def cloneRemoteProject(source, destDir):
    # check if source is a git url
    repoURL, filePath, revision = splitGitUrl(source)
    # not yet supported: add repo to local project
    # destRoot = Project.findPath(destDir)
    # if destRoot:
    #     # destination is in an existing project, use that one
    #     sourceProject = Project(destRoot)
    #     repo = sourceProject.findGitRepo(repoURL, revision)
    #     if not repo:
    #         repo = sourceProject.createWorkingDir(repoURL, revision)
    #     source = os.path.join(repo.workingDir, filePath)
    # else: # otherwise clone to dest
    if os.path.exists(destDir) and os.listdir(destDir):
        raise UnfurlError(
            'Can not clone project into "%s": folder is not empty' % destDir
        )
    Repo.createWorkingDir(repoURL, destDir, revision) # clone source repo
    targetDir = os.path.join(destDir, filePath)
    sourceRoot = Project.findPath(targetDir)
    if not sourceRoot:
        raise UnfurlError('Error: cloned "%s" to "%s" but couldn\'t find an Unfurl project'
                % (source, destDir))
    sourceProject = Project(sourceRoot)
    return sourceProject, targetDir


def getSourceProject(source):
    sourceRoot = Project.findPath(source)
    if sourceRoot:
        return Project(sourceRoot)
    return None


def isEnsembleInProjectRepo(project, paths):
    # check if source points to an ensemble that is part of the project repo
    if not project.projectRepo or "localEnv" not in paths:
        return False
    sourceDir = paths["sourceDir"]
    assert not os.path.isabs(sourceDir)
    pathToEnsemble = os.path.join(project.projectRoot, sourceDir)
    if not os.path.isdir(pathToEnsemble):
        return False
    if project.projectRepo.isPathExcluded(sourceDir):
        return False
    return True


def clone(source, dest, includeLocal=False, **options):
    """
    Clone the `source` ensemble to `dest`. If `dest` isn't in a project, create one.
    `source` can point to an ensemble_template, a service_template, an existing ensemble
    or a folder containing one of those. If it points to a project its default ensemble will be cloned.

    Referenced `repositories` will be cloned if a git repository or copied if a regular file folder,
    If the folders already exist they will be copied to new folder unless the git repositories have the same HEAD.
    but the local repository names will remain the same.

    ================ =============================================
    dest             result
    ================ =============================================
    Inside project   new ensemble
    new or empty dir clone or create project (depending on source)
    another project  error (not yet supported)
    other            error
    ================ =============================================

    """
    if not dest:
        dest = Repo.getPathForGitRepo(source) # choose dest based on source url
    # XXX else: # we're assuming dest is directory

    isRemote = isURLorGitPath(source)
    if isRemote:
        clonedProject, source = cloneRemoteProject(source, dest)
        # source is now a path inside the cloned project
        paths = _getEnsemblePaths(source, clonedProject)
    else:
        sourceProject = getSourceProject(source)
        sourceNotInProject = not sourceProject
        if sourceNotInProject:
            # source wasn't in a project
            raise UnfurlError(
                "Can't clone \"%s\": it isn't in an Unfurl project or repository"
                % source)
            # XXX create a new project from scratch for the ensemble
            # if os.path.exists(dest) and os.listdir(dest):
            #     raise UnfurlError(
            #         'Can not create a project in "%s": folder is not empty' % dest
            #     )
            # newHome, projectConfigPath = createProject(
            #     dest, emtpy=True, **options
            # )
            # sourceProject = Project(projectConfigPath)

        relDestDir = sourceProject.getRelativePath(dest)
        paths = _getEnsemblePaths(source, sourceProject)
        if not relDestDir.startswith(".."):
            # dest is in the source project (or its a new project)
            # so don't need to clone, just need to create an ensemble
            destDir, manifest = createNewEnsemble(paths, sourceProject, relDestDir)
            return 'Created ensemble in %s project: "%s"' % (
                "new" if sourceNotInProject else "existing",
                os.path.absolute(destDir),
            )
        else:
            # XXX we are not trying to adjust the clone location to be a parent of dest
            sourceProject.projectRepo.clone(dest)
            relPathToProject = sourceProject.projectRepo.findRepoPath(sourceProject.projectRoot)
            # adjust if project is not at the root of its repo:
            dest = Project.normalizePath(os.path.join(dest, relPathToProject))
            clonedProject = Project(dest)

    # pass in "" as dest because we already "consumed" dest by cloning the project to that location
    manifest, message = _createInClonedProject(paths, clonedProject, "")
    if not isRemote and manifest:
        # we need to clone referenced local repos so the new project has access to them
        cloneLocalRepos(manifest, sourceProject, clonedProject)
    return message


def _createInClonedProject(paths, clonedProject, dest):
    """
    Called by `clone` when cloning an ensemble.

    ================================   ========================
    source ensemble                    result
    ================================   ========================
    project root or ensemble in repo   git clone only
    local ensemble or template         git clone + new ensemble
    ================================   ========================

    """
    from unfurl import yamlmanifest

    ensembleInProjectRepo = isEnsembleInProjectRepo(clonedProject, paths)
    if ensembleInProjectRepo:
        # the ensemble is already part of the source project repository or a submodule
        # we're done
        manifest = yamlmanifest.ReadOnlyManifest(
            localEnv=paths["localEnv"]
        )
        return manifest, "Cloned project to " + clonedProject.projectRoot
    else:
        # create local/unfurl.yaml in the new project
        # XXX vaultpass should only be set for the new ensemble being created
        writeProjectConfig(
            os.path.join(clonedProject.projectRoot, "local"),
            DefaultNames.LocalConfig,
            "unfurl.local.yaml.j2",
            dict(vaultpass=get_random_password()),
        )
        # dest: should be a path relative to the clonedProject's root
        assert not os.path.isabs(dest)
        destDir, manifest = createNewEnsemble(paths, clonedProject, dest)
        return manifest, 'Created new ensemble at "%s" in cloned project at "%s"' % (
            destDir,
            clonedProject.projectRoot,
        )


def initEngine(projectDir, runtime):
    kind, sep, rest = runtime.partition(":")
    if kind == "venv":
        return createVenv(projectDir, rest)
    # elif kind == 'docker'
    # XXX return 'unrecoginized runtime string: "%s"'
    return False


def _addUnfurlToVenv(projectdir):
    # this is hacky
    # can cause confusion if it exposes more packages than unfurl
    base = os.path.dirname(os.path.dirname(_templatePath))
    sitePackageDir = None
    libDir = os.path.join(projectdir, os.path.join(".venv", "lib"))
    for name in os.listdir(libDir):
        sitePackageDir = os.path.join(libDir, name, "site-packages")
        if os.path.isdir(sitePackageDir):
            break
    else:
        # XXX report error: can't find site-packages folder
        return
    _writeFile(sitePackageDir, "unfurl.pth", base)
    _writeFile(sitePackageDir, "unfurl.egg-link", base)


def createVenv(projectDir, pipfileLocation):
    """Create a virtual python environment for the given project."""
    os.environ["PIPENV_IGNORE_VIRTUALENVS"] = "1"
    os.environ["PIPENV_VENV_IN_PROJECT"] = "1"
    if "PIPENV_PYTHON" not in os.environ:
        os.environ["PIPENV_PYTHON"] = sys.executable

    try:
        cwd = os.getcwd()
        os.chdir(projectDir)
        # need to set env vars and change current dir before importing pipenv
        from pipenv.core import do_install, ensure_python
        from pipenv.utils import python_version

        pythonPath = str(ensure_python())
        assert pythonPath, pythonPath
        if not pipfileLocation:
            versionStr = python_version(pythonPath)
            assert versionStr, versionStr
            version = versionStr.rpartition(".")[0]  # 3.8.1 => 3.8
            # version = subprocess.run([pythonPath, "-V"]).stdout.decode()[
            #     7:10
            # ]  # e.g. Python 3.8.1 => 3.8
            pipfileLocation = os.path.join(
                _templatePath, "python" + version
            )  # e.g. templates/python3.8

        if not os.path.isdir(pipfileLocation):
            # XXX 'Pipfile location is not a valid directory: "%s" % pipfileLocation'
            return False

        # copy Pipfiles to project root
        if os.path.abspath(projectDir) != os.path.abspath(pipfileLocation):
            for filename in ["Pipfile", "Pipfile.lock"]:
                path = os.path.join(pipfileLocation, filename)
                if os.path.isfile(path):
                    shutil.copy(path, projectDir)

        # create the virtualenv and install the dependencies specified in the Pipefiles
        sys_exit = sys.exit
        try:
            retcode = -1

            def noexit(code):
                retcode = code

            sys.exit = noexit

            do_install(python=pythonPath)
            # this doesn't actually install the unfurl so link to this one
            _addUnfurlToVenv(projectDir)
        finally:
            sys.exit = sys_exit

        return not retcode  # retcode means error
    finally:
        os.chdir(cwd)
