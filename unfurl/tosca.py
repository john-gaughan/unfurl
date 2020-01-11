"""
TOSCA implementation

Differences with TOSCA 1.1:

 * Entity type can allow properties that don't need to be declared
 * Added "any" datatype
 * Interface "implementation" values can be a node template name, and the corresponding
   instance will be used to execute the operation.
"""
from .tosca_plugins import TOSCA_VERSION
from .util import UnfurlValidationError
from .eval import Ref
from .yamlloader import resolvePathToToscaImport
from toscaparser.tosca_template import ToscaTemplate
from toscaparser.elements.entity_type import EntityType
import toscaparser.workflow
from toscaparser.common.exception import ExceptionCollector, ValidationError
import logging

logger = logging.getLogger("unfurl")

from toscaparser import functions


class RefFunc(functions.Function):
    def result(self):
        return {self.name: self.args}

    def validate(self):
        pass


functions.function_mappings["eval"] = RefFunc
functions.function_mappings["ref"] = RefFunc

toscaIsFunction = functions.is_function


def is_function(function):
    return toscaIsFunction(function) or Ref.isRef(function)


functions.is_function = is_function


def createDefaultTopology():
    tpl = dict(
        tosca_definitions_version=TOSCA_VERSION,
        topology_template=dict(
            node_templates={"_default": {"type": "tosca.nodes.Root"}},
            relationship_templates={"_default": {"type": "tosca.relationships.Root"}},
        ),
    )
    return ToscaTemplate(yaml_dict_tpl=tpl)


class ToscaSpec(object):
    ConfiguratorType = "unfurl.nodes.Configurator"
    InstallerType = "unfurl.nodes.Installer"

    def __init__(self, toscaDef, inputs=None, instances=None, path=None):
        if isinstance(toscaDef, ToscaTemplate):
            self.template = toscaDef
        else:
            topology_tpl = toscaDef.get("topology_template")
            if not topology_tpl:
                toscaDef["topology_template"] = dict(
                    node_templates={}, relationship_templates={}
                )
            else:
                for section in ["node_templates", "relationship_templates"]:
                    if not topology_tpl.get(section):
                        topology_tpl[section] = {}

            if instances:
                self.loadInstances(toscaDef, instances)

            logger.info("Validating TOSCA template at %s", path)
            try:
                # need to set a path for the import loader
                self.template = ToscaTemplate(
                    path=path, parsed_params=inputs, yaml_dict_tpl=toscaDef
                )
            except ValidationError:
                message = "\n".join(ExceptionCollector.getExceptionsReport(False))
                raise UnfurlValidationError(
                    "TOSCA validation failed for %s: \n%s" % (path, message),
                    ExceptionCollector.getExceptions(),
                )

        self.nodeTemplates = {}
        self.installers = {}
        self.relationshipTemplates = {}
        if hasattr(self.template, "nodetemplates"):
            for template in self.template.nodetemplates:
                nodeTemplate = NodeSpec(template, self)
                if template.is_derived_from(self.InstallerType):
                    self.installers[template.name] = nodeTemplate
                self.nodeTemplates[template.name] = nodeTemplate

            # user-declared RelationshipTemplates, source and target will be None
            for template in self.template.relationship_templates:
                relTemplate = RelationshipSpec(template)
                self.relationshipTemplates[template.name] = relTemplate

        self.topology = TopologySpec(self.template.topology_template, inputs)
        self.load_workflows()

    def load_workflows(self):
        # we want to let different types defining standard workflows like deploy
        # so we need support importing workflows
        workflows = {
            name: [Workflow(w)]
            for name, w in self.template.topology_template.workflows.items()
        }
        for import_tpl in self.template.nested_tosca_tpls.values():
            importedWorkflows = import_tpl.get("topology_template", {}).get("workflows")
            if importedWorkflows:
                for name, val in importedWorkflows.items():
                    workflows.setdefault(name, []).append(
                        Workflow(toscaparser.workflow.Workflow(name, val))
                    )

        self._workflows = workflows

    def getWorkflow(self, workflow):
        # XXX need api to get all the workflows with the same name
        wfs = self._workflows.get(workflow)
        if wfs:
            return wfs[0]
        else:
            return None

    def resolveArtifactPath(self, artifact_tpl, path=None):
        return resolvePathToToscaImport(
            path or self.template.path, self.template.tpl, artifact_tpl
        )

    def getTemplate(self, name):
        if name == "#topology":
            return self.topology
        elif "#c#" in name:
            nodeName, capability = name.split("#c#")
            nodeTemplate = self.nodeTemplates.get(nodeName)
            if not nodeTemplate:
                return None
            return nodeTemplate.getCapability(capability)
        elif "#r#" in name:
            nodeName, requirement = name.split("#r#")
            if nodeName:
                nodeTemplate = self.nodeTemplates.get(nodeName)
                if not nodeTemplate:
                    return None
                return nodeTemplate.getRequirement(requirement)
            else:
                return self.relationshipTemplates.get(name)
        else:
            return self.nodeTemplates.get(name)

    def isTypeName(self, typeName):
        return (
            typeName in self.template.topology_template.custom_defs
            or typeName in EntityType.TOSCA_DEF
        )

    def findMatchingTemplates(self, typeName):
        for template in self.nodeTemplates:
            if template.isCompatibleType(typeName):
                yield template

    def loadInstances(self, toscaDef, tpl):
        """
    Creates node templates for any instances defined in the spec

    .. code-block:: YAML

      spec:
            instances:
              test:
                install: test
            installers:
              test:
                operations:
                  default:
                    implementation: TestConfigurator
                    inputs:
"""
        node_templates = toscaDef["topology_template"]["node_templates"]
        for name, impl in tpl.get("installers", {}).items():
            if name not in node_templates:
                node_templates[name] = dict(type=self.InstallerType, properties=impl)
            else:
                raise UnfurlValidationError(
                    'can not add installer "%s", there is already a node template with that name'
                    % name
                )

        for name, impl in tpl.get("instances", {}).items():
            if name not in node_templates and impl is not None:
                node_templates[name] = self.loadInstance(impl.copy())

    def loadInstance(self, impl):
        if "type" not in impl:
            impl["type"] = "unfurl.nodes.Default"
        installer = impl.pop("install", None)
        if installer:
            impl["requirements"] = [{"install": installer}]
        return impl


_defaultTopology = createDefaultTopology()

# represents a node, capability or relationship
class EntitySpec(object):
    def __init__(self, toscaNodeTemplate):
        self.toscaEntityTemplate = toscaNodeTemplate
        self.name = toscaNodeTemplate.name
        self.type = toscaNodeTemplate.type
        # nodes have both properties and attributes
        # as do capability properties and relationships
        # but only property values are declared
        self.properties = {
            prop.name: prop.value for prop in toscaNodeTemplate.get_properties_objects()
        }
        if toscaNodeTemplate.type_definition:
            attrDefs = toscaNodeTemplate.type_definition.get_attributes_def_objects()
            self.defaultAttributes = {
                prop.name: prop.default for prop in attrDefs if prop.default is not None
            }
            propDefs = toscaNodeTemplate.type_definition.get_properties_def()
            propDefs.update(toscaNodeTemplate.type_definition.get_attributes_def())
            self.attributeDefs = propDefs
        else:
            self.defaultAttributes = {}
            self.attributeDefs = {}

    def getInterfaces(self):
        return self.toscaEntityTemplate.interfaces

    def getGroups(self):
        # XXX return the groups this entity is in
        return []

    def isCompatibleTarget(self, targetStr):
        if self.name == targetStr:
            return True
        return self.toscaEntityTemplate.is_derived_from(targetStr)

    def isCompatibleType(self, typeStr):
        return self.toscaEntityTemplate.is_derived_from(typeStr)

    def getUri(self):
        return self.name  # XXX

    def __repr__(self):
        return "%s('%s')" % (self.__class__.__name__, self.name)


class NodeSpec(EntitySpec):
    # has attributes: tosca_id, tosca_name, state, (3.4.1 Node States p.61)
    def __init__(self, template=None, spec=None):
        if not template:
            template = _defaultTopology.topology_template.nodetemplates[0]
            self.spec = ToscaSpec(_defaultTopology)
        else:
            assert spec
            self.spec = spec
        EntitySpec.__init__(self, template)
        self._capabilities = None
        self._requirements = None
        self._relationships = None

    @property
    def requirements(self):
        if self._requirements is None:
            self._requirements = {}
            nodeTemplate = self.toscaEntityTemplate
            for req, targetNode in zip(
                nodeTemplate.requirements, nodeTemplate.relationships.values()
            ):
                name, values = list(req.items())[0]
                reqSpec = RequirementSpec(name, values, self)
                nodeSpec = self.spec.getTemplate(targetNode.name)
                assert nodeSpec
                nodeSpec.addRelationship(reqSpec)
                self._requirements[name] = reqSpec
        return self._requirements

    def getRequirement(self, name):
        return self.requirements.get(name)

    @property
    def relationships(self):
        """
        returns a list of RelationshipSpecs that are targeting this node template.
        """
        for r in self.toscaEntityTemplate.relationship_tpl:
            assert r.source
            # calling requirement property will ensure the RelationshipSpec is property linked
            self.spec.getTemplate(r.source.name).requirements
        return self._getRelationshipSpecs()

    def _getRelationshipSpecs(self):
        if self._relationships is None:
            # relationship_tpl is a list of RelationshipTemplates that target the node
            self._relationships = [
                RelationshipSpec(r) for r in self.toscaEntityTemplate.relationship_tpl
            ]
        return self._relationships

    def getCapabilityInterfaces(self):
      idefs = [r.getInterfaces() for r in self._getRelationshipSpecs()]
      return [i for elem in idefs for i in elem if i.name != 'default']

    def getRequirementInterfaces(self):
      idefs = [r.getInterfaces() for r in self.requirements.values()]
      return [i for elem in idefs for i in elem if i.name != 'default']

    @property
    def capabilities(self):
        if self._capabilities is None:
            self._capabilities = {
                c.name: CapabilitySpec(self, c)
                for c in self.toscaEntityTemplate.get_capabilities_objects()
            }
        return self._capabilities

    def getCapability(self, name):
        return self.capabilities.get(name)

    def addRelationship(self, reqSpec):
        # find the relationship for this requirement:
        for relSpec in self._getRelationshipSpecs():
            # the RelationshipTemplate should have had the source node assigned by the tosca parser
            # XXX this won't distinguish between more than one relationship between the same two nodes
            # to fix this have the RelationshipTemplate remember the name of the requirement
            if (
                relSpec.toscaEntityTemplate.source
                is reqSpec.parentNode.toscaEntityTemplate
            ):
                assert not reqSpec.relationship or reqSpec.relationship is relSpec
                reqSpec.relationship = relSpec
                assert not relSpec.requirement or relSpec.requirement is reqSpec
                relSpec.requirement = reqSpec
                break
        else:
            raise UnfurlValidationError(
                "relationship not found for requirement %s" % reqSpec.name
            )

        # figure out which capability the relationship targets:
        for capability in self.capabilities.values():
            if reqSpec.isCapable(capability):
                assert reqSpec.relationship
                assert (
                    not reqSpec.relationship.capability
                    or reqSpec.relationship.capability is capability
                )
                reqSpec.relationship.capability = capability
                break
        else:
            raise UnfurlValidationError(
                "capability not found for requirement %s" % reqSpec.name
            )


class RelationshipSpec(EntitySpec):
    def __init__(self, template=None, capability=None, requirement=None):
        # template is a RelationshipTemplate
        # It is a full-fledged entity with a name, type, properties, attributes, interfaces, and metadata.
        # its RelationshipType has valid_target_types (and (hackish) capability_name)
        if not template:
            template = _defaultTopology.topology_template.relationship_templates[0]
        EntitySpec.__init__(self, template)
        self.requirement = requirement
        self.capability = capability

    @property
    def source(self):
        return self.requirement.parentNode if self.requirement else None

    @property
    def target(self):
        return self.capability.parentNode if self.capability else None

    def getUri(self):
        return "#r#" + self.name


class RequirementSpec(object):
    """
    A Requirement shares a Relationship with a Capability.
    """

    def __init__(self, name, req, parent):
        self.parentNode = parent
        self.source = parent
        self.name = name
        self.req = req
        self.relationship = None
        # req may specify:
        # capability (definition name or type name), node (template name or type name), and node_filter,
        # relationship (template name or type name or inline relationship template)
        # occurrences

    def getUri(self):
        return self.parentNode.name + "#r#" + self.name

    def getInterfaces(self):
        return self.relationship.getInterfaces() if self.relationship else []

    def isCapable(self, capability):
        # XXX consider self.req.name, capability, node_filter
        if self.relationship:
            t = self.relationship.toscaEntityTemplate.type_definition
            return (
                t.capability_name == capability.name
                or capability.type in t.valid_target_types
            )
        return False


class CapabilitySpec(EntitySpec):
    def __init__(self, parent=None, capability=None):
        if not parent:
            parent = NodeSpec()
            capability = parent.toscaEntityTemplate.get_capabilities_objects()[0]
        self.parentNode = parent
        assert capability
        # capabilities.Capability isn't an EntityTemplate but duck types with it
        EntitySpec.__init__(self, capability)
        self._relationships = None

    def getInterfaces(self):
        # capabilities don't have their own interfaces
        return self.parentNode.interfaces

    def getUri(self):
        # capabilities aren't standalone templates
        # this is demanagled by getTemplate()
        return self.parentNode.name + "#c#" + self.name

    @property
    def relationships(self):
        return [r for r in self.parentNode.relationships if r.capability is self]


# XXX
# class GroupSpec(EntitySpec):
#  getNodeTemplates() getInstances(), getChildren()


class TopologySpec(EntitySpec):
    # has attributes: tosca_id, tosca_name, state, (3.4.1 Node States p.61)
    def __init__(self, template=None, inputs=None):
        if not template:
            template = _defaultTopology.topology_template
        inputs = inputs or {}

        self.toscaEntityTemplate = template
        self.name = "#topology"
        self.type = "#topology"
        self.inputs = {
            input.name: inputs.get(input.name, input.default)
            for input in template.inputs
        }
        self.outputs = {output.name: output.value for output in template.outputs}
        self.properties = {}
        self.defaultAttributes = {}
        self.attributeDefs = {}

    def getInterfaces(self):
        # doesn't have any interfaces
        return []


class Workflow(object):
    def __init__(self, workflow):
        self.workflow = workflow

    def initialSteps(self):
        preceeding = set()
        for step in self.workflow.steps.values():
            preceeding.update(step.on_success + step.on_failure)
        return [
            step for step in self.workflow.steps.values() if step.name not in preceeding
        ]

    def getStep(self, stepName):
        return self.workflow.steps.get(stepName)

    def matchStepFilter(self, stepName, resource):
        step = self.getStep(stepName)
        if step:
            return all(filter.evaluate(resource.attributes) for filter in step.filter)
        return None

    def matchPreconditions(self, resource):
        for precondition in self.workflow.preconditions:
            target = resource.root.findResource(precondition.target)
            # XXX if precondition.target_relationship
            if not target:
                # XXX target can be a group
                return False
            if not all(
                filter.evaluate(target.attributes) for filter in precondition.condition
            ):
                return False
        return True
