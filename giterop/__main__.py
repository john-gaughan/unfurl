#!/usr/bin/env python
"""
Applies a GitErOp manifest

For each configuration, run it if required, then record the result
"""
from __future__ import print_function

from .yamlmanifest import runJob
from .support import Status
from . import __version__, initLogging
import click
import sys
import os
import os.path
import traceback
import logging

@click.group()
@click.pass_context
@click.option('--home', default='', type=click.Path(exists=False), help='path to .giterop home')
@click.option('-v', '--verbose', count=True, help="verbose mode (-vvv for more)")
@click.option('-q', '--quiet', default=False, is_flag=True, help='Only output errors to the stdout')
@click.option('--logfile', default=None,
                              help='Log file for messages during quiet operation')
def cli(ctx, verbose=0, quiet=False, logfile=None, **kw):
  # ensure that ctx.obj exists and is a dict (in case `cli()` is called
  # by means other than the `if` block below
  ctx.ensure_object(dict)
  ctx.obj['verbose'] = verbose
  ctx.obj.update(kw)
  if quiet:
    logLevel = logging.CRITICAL
  else:
    # TRACE (5)
    levels = [logging.INFO, logging.DEBUG, 5, 5, 5]
    logLevel = levels[min(verbose,3)]
  
  initLogging(logLevel, logfile)

#giterop run foo:create -- terraform blah
# --append
# --replace
# giterop run foo:check 'terraform blah' # save lastChangeId so we can recreate history for the target
# each command builds a config (unless replace ) --replace
# giterop add repo
# giterop add image
# giterop intervene # apply manual changes to status (create a change set and commit)
@cli.command()
@click.pass_context
@click.argument('action', default='*:upgrade')
@click.argument('use', nargs=1, default='') # use:configurator
@click.option('--manifest', default='', type=click.Path(exists=False))
@click.option('--append', default=True, is_flag=True, help="add this command to the previous")
@click.option('--replace', default=True, is_flag=True, help="replace the previous command")
@click.option('--dryrun', default=False, is_flag=True, help='Do not modify anything, just do a dry run.')
@click.option('--jobexitcode', type=click.Choice(['error', 'degraded', 'never']),
              default='never', help='Set exitcode if job status is not ok.')
@click.argument('cmdline', nargs=-1)
def run(ctx, action, use=None, cmdline=None, **options):
  options.update(ctx.obj)
  return _run(options.pop('manifest'), options)

def _run(manifest, options):
  job = runJob(manifest, options)
  if job.unexpectedAbort:
    if options['verbose']:
      print(job.unexpectedAbort.getStackTrace(), file=sys.stderr)
      raise job.unexpectedAbort
  else:
    click.echo(job.summary())

  if options['jobexitcode'] != 'never' and Status[options['jobexitcode']] <= job.status:
    if options.get('standalone_mode') is False:
      return 1
    else:
      sys.exit(1)
  else:
    return 0

@cli.command()
@click.pass_context
@click.argument('manifest', default='', type=click.Path(exists=False))
@click.option('--resource', help="name of resource to start with")
@click.option('--add', default=True, is_flag=True, help="run newly added configurations")
@click.option('--update', default=True, is_flag=True, help="run configurations that whose spec has changed but don't require a major version change")
@click.option('--repair', type=click.Choice(['error', 'degraded', 'notapplied', 'none']),
  default="error", help="re-run configurations that are in an error or degraded state")
@click.option('--upgrade', default=False, is_flag=True, help="run configurations with major version changes or whose spec has changed")
@click.option('--all', default=False, is_flag=True, help="(re)run all configurations")
@click.option('--dryrun', default=False, is_flag=True, help='Do not modify anything, just do a dry run.')
@click.option('--jobexitcode', type=click.Choice(['error', 'degraded', 'never']),
              default='never', help='Set exitcode if job status is not ok.')
def deploy(ctx, manifest=None, **options):
  options.update(ctx.obj)
  return _run(manifest, options)

@cli.command()
@click.pass_context
@click.argument('projectdir', default='.', type=click.Path(exists=False))
def init(ctx, projectdir, **options):
  """
giterop init [project] # creates a giterop project with new spec and instance repos
"""
  options.update(ctx.obj)
  from .init import createProject

  if os.path.exists(projectdir):
    if not os.path.isdir(projectdir):
      raise click.ClickException(projectdir + ": file already exists")
    elif os.listdir(projectdir):
      raise click.ClickException(projectdir + " is not empty")

  homePath, projectPath = createProject(projectdir, options['home'])
  if homePath:
    click.echo("giterop home created at %s" % homePath)
  click.echo("New GitErOp project created at %s" % projectPath)

#gitop clone [instance or spec repo] # clones repos into new project
#gitop newinstance # create new instance repo using manifest-template.yaml

@cli.command()
def version():
  click.echo("giterop version %s" % __version__)

@cli.command()
def plan():
  click.echo("coming soon") # XXX

def printHelp():
  ctx = cli.make_context('giterop', [])
  click.echo(cli.get_help(ctx))

def main():
  obj = {'standalone_mode': False}
  try:
    rv = cli(standalone_mode=False, obj=obj)
    sys.exit(rv or 0)
  except click.UsageError as err:
    click.echo("Error: %s" % err)
    printHelp()
    sys.exit(err.exit_code)
  except click.Abort:
    click.echo('Aborted!', file=sys.stderr)
    sys.exit(1)
  except click.ClickException as e:
    if obj.get('verbose'):
      traceback.print_exc(file=sys.stderr)
    e.show()
    sys.exit(e.exit_code)
  except Exception as err:
    if obj.get('verbose'):
      traceback.print_exc(file=sys.stderr)
    else:
      click.echo(str(err), file=sys.stderr)
    sys.exit(1)

if __name__ == '__main__':
  main()
