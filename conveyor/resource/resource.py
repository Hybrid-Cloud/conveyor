'''
@author: g00357909
'''

import datetime
import copy
import six
from oslo.config import cfg

from conveyor.common import timeutils
from conveyor.common import log as logging
from conveyor.common import plan_status as p_status
from conveyor import exception

LOG = logging.getLogger(__name__)

resource_type = ['OS::Nova::Server',
                 'OS::Cinder::Volume',
                 'OS::Neutron::Net',
                 'OS::Neutron::Router',
                 'OS::Neutron::LoadBalancer',
                 'OS::Heat::Stack']


instance_allowed_search_opts = ['reservation_id', 'name', 'status', 'image', 'flavor',
                               'tenant_id', 'ip', 'changes-since', 'all_tenants']

volume_allowed_search_opts = []

network_allowed_search_opts = []

resource_opts = [
    cfg.IntOpt('plan_expire_time',
               default=60,
               help='If a plan still was not be cloned or migrated \
                       after plan_expire_time minutes, the plan will be deleted',
               deprecated_group='DEFAULT',
               deprecated_name=''),
]

CONF = cfg.CONF
CONF.register_opts(resource_opts)


class Resource(object):
    """Describes an OpenStack resource."""

    def __init__(self, name, type, id, properties=None, extra_properties=None, parameters=None):
        self.name = name
        self.type = type
        self.id = id or ""
        self.properties = properties or {}
        self.extra_properties = extra_properties or {}
        self.parameters = parameters or {}

    def add_parameter(self, name, description, parameter_type='string',
                      constraints=None, default=None):
        data = {
            'type': parameter_type,
            'description': description,
        }

        if default:
            data['default'] = default

        self.parameters[name] = data
        
    def add_property(self, key, value):
        self.properties[key] = value

    def add_extra_property(self, key, value):
        self.extra_properties[key] = value
        
    @property
    def template_resource(self):
        extra_properties = copy.deepcopy(self.extra_properties) 
        extra_properties['id'] = self.id
        return {
            self.name: {
                'type': self.type,
                'properties': self.properties,
                'extra_properties':extra_properties
            }
        }
        
    @property
    def template_parameter(self):
        return self.parameters
    
    def to_dict(self):
        resource = {
                  "id": self.id,
                  "name": self.name,
                  "type": self.type,
                  "properties": self.properties,
                  "parameters": self.parameters
                  }
        return resource
    
    @classmethod
    def from_dict(cls, resource_dict):
        self = cls(resource_dict['name'], 
                   resource_dict['type'], resource_dict['id'], 
                   properties=resource_dict.get('properties'), 
                   extra_properties=resource_dict.get('extra_properties'),
                   parameters=resource_dict.get('parameters'))
        return self
    
    def rebuild_parameter(self, parameters):
        
        def get_params(properties):
            if isinstance(properties, dict) and len(properties) == 1:
                key = properties.keys()[0]
                value = properties[key]
                if key == "get_param":
                    if isinstance(value, six.string_types) and value in parameters.keys():
                        param = parameters[value]
                        self.add_parameter(value, 
                                           param.get('description', ''), 
                                           parameter_type=param.get('type', 'string'),
                                           constraints=param.get('constraints', ''),
                                           default=param.get('default', ''))
                else:
                    get_params(properties[key])
            elif isinstance(properties, dict):
                for p in properties.values():
                    get_params(p)
            elif isinstance(properties, list):
                for p in properties:
                    get_params(p)
        
        if not isinstance(parameters, dict):
            return
        self.parameters = {}
        get_params(self.properties)
        

class ResourceDependency(object):
    def __init__(self, id, name, name_in_template, type, dependencies = None):
        self.id = id
        self.name = name
        self.name_in_template = name_in_template
        self.type = type
        self.dependencies = dependencies or []

    def add_dependency(self, res_name):
        if res_name not in self.dependencies:
            self.dependencies.append(res_name)
        
    def to_dict(self):
        dep = {
               "id": self.id,
               "name": self.name,
               "type": self.type,
               "name_in_template": self.name_in_template,
               "dependencies": self.dependencies
               }
                
        return dep

    @classmethod
    def from_dict(cls, dep_dict):
        self = cls(dep_dict['id'], 
                   dep_dict['name'], 
                   dep_dict['name_in_template'], 
                   dep_dict['type'],
                   dependencies=dep_dict.get('dependencies'))
        return self


class Plan(object):
    def __init__(self, plan_id, plan_type, project_id, user_id, stack_id=None,
                 created_at=None, updated_at=None, deleted_at=None,
                 expire_at=None, deleted=None, plan_status=None, task_status=None, 
                 original_resources=None, updated_resources=None, 
                 original_dependencies=None, updated_dependencies=None):
        
        self.plan_id = plan_id
        self.plan_type = plan_type
        self.project_id = project_id
        self.user_id = user_id
        self.stack_id = stack_id
        
        self.created_at = created_at or timeutils.utcnow()
        self.updated_at = updated_at or self.created_at
        self.deleted_at = deleted_at or ''
        self.expire_at = expire_at or \
                            timeutils.utc_after_given_minutes(CONF.plan_expire_time)
        
        self.deleted = deleted or False
        self.plan_status = plan_status or p_status.CREATING
        self.task_status = task_status or ''
        
        self.original_resources = original_resources or {}
        self.updated_resources = updated_resources or {}
        self.original_dependencies = original_dependencies or {}
        self.updated_dependencies = updated_dependencies or {}
        

    def update(self, values):
        if not isinstance(values, dict):
            msg = "Update plan failed. 'values' attribute must be a dict."
            LOG.error(msg)
            raise exception.PlanUpdateException(message=msg)
        allowed_properties = ['task_status', 'plan_status', 'expire_at',
                              'updated_resources', 'updated_dependencies']
        for k,v in values.items():
            if k not in allowed_properties:
                msg = "Update plan failed. %s attribute \
                        not found or unsupported to update." % k
                LOG.error(msg)
                raise exception.PlanUpdateException(message=msg)
            elif k == 'plan_status' and v not in p_status.PLAN_STATUS:
                msg = "Update plan failed. '%s' plan_status unsupported." % v
                LOG.error(msg)
                raise exception.PlanUpdateException(message=msg)
        for k,v in values.items():
            setattr(self, k, v)
            if k == 'updated_resources':
                self.rebuild_dependencies()
    

    def rebuild_dependencies(self, is_original=False):
        
        def get_dependencies(properties, deps):
            if isinstance(properties, dict) and len(properties) == 1:
                key = properties.keys()[0]
                value = properties[key]
                if key == "get_resource":
                    if isinstance(value, six.string_types) \
                                and value in resources.keys():
                        deps.append(value)
                elif key == "get_attr":
                    if isinstance(value, list) and len(value) >=1 \
                                and isinstance(value[0], six.string_types) \
                                and value[0] in resources.keys():
                        deps.append(value[0])
                        
                else:
                    get_dependencies(properties[key], deps)
            elif isinstance(properties, dict):
                for p in properties.values():
                    get_dependencies(p, deps)
            elif isinstance(properties, list):
                for p in properties:
                    get_dependencies(p, deps)
                    
        resources = self.original_resources if is_original \
                                    else self.updated_resources
        dependencies = self.original_dependencies if is_original \
                                    else self.updated_dependencies
        
        if not resources:
            return
        
        #if resource has not been modified, there is no need to update dependencies
        if len(resources) == len(dependencies):
            is_same = True
            for res_name in resources.keys():
                if res_name not in dependencies.keys():
                    is_same = False
                    break
            if is_same:
                return
        #begin to rebuild
        dependencies = {}
        for res in resources.values():
            deps = []
            get_dependencies(res.properties, deps)
            #remove duplicate dependencies
            deps = {}.fromkeys(deps).keys()
            new_dependencies = ResourceDependency(res.id, 
                                                  res.properties.get('name', ''),
                                                  res.name, res.type,
                                                  dependencies=deps)
            dependencies[res.name] = new_dependencies
        
        if is_original:
            self.original_dependencies = dependencies
        else:
            self.updated_dependencies = dependencies
            
        
    def delete(self):
        self.deleted = True
        self.deleted_at = timeutils.utcnow()
        
    def to_dict(self):
        
        def trans_from_obj_dict(object_dict):
            res = {}
            if object_dict and isinstance(object_dict, dict):
                for k, v in object_dict.items():
                    res[k] = v.to_dict()
            return res
            
        plan = {'plan_id': self.plan_id,
                'plan_type': self.plan_type,
                'project_id': self.project_id,
                'user_id': self.user_id,
                'stack_id': self.stack_id,
                'created_at': self.created_at,
                'updated_at': self.updated_at,
                'expire_at': self.expire_at,
                'deleted_at': self.deleted_at,
                'deleted': self.deleted,
                'task_status': self.task_status,
                'plan_status': self.plan_status,
                'original_resources': trans_from_obj_dict(self.original_resources),
                'updated_resources': trans_from_obj_dict(self.original_resources),
                'original_dependencies': trans_from_obj_dict(self.original_dependencies),
                'updated_dependencies': trans_from_obj_dict(self.updated_dependencies)
                }
        return plan

    @classmethod
    def from_dict(cls, plan_dict):
        
        def trans_to_obj_dict(r_dict, obj_name):
            obj_dict = {}
            key = 'name'
            if obj_name == 'ResourceDependency':
                key = 'name_in_template'
            for rd in r_dict:
                obj_dict[rd[key]] = eval(obj_name).from_dict(rd)
            return obj_dict
                
        ori_res = plan_dict.get('original_resources')
        ori_dep = plan_dict.get('original_dependencies')
        upd_res = plan_dict.get('updated_resources')
        upd_dep = plan_dict.get('updated_dependencies')
        
        ori_res = trans_to_obj_dict(ori_res, 'Resource') if ori_res else {}
        ori_dep = trans_to_obj_dict(ori_dep, 'ResourceDependency') if ori_dep else {}
        upd_res = trans_to_obj_dict(upd_res, 'Resource') if upd_res else {}
        upd_dep = trans_to_obj_dict(upd_dep, 'ResourceDependency') if upd_dep else {}
        
        plan = {
            'plan_id': '',
            'plan_type': '',
            'project_id': '',
            'user_id': '',
            'stack_id': '',
            'created_at': '',
            'updated_at': '',
            'expire_at': '',
            'deleted_at': '',
            'deleted': '',
            'task_status': '',
            'plan_status': '',
        }
        
        for key in plan.keys():
            plan[key] = plan_dict[key]
            
        plan['original_resources'] = ori_res
        plan['updated_resources'] = upd_res
        plan['original_dependencies'] = ori_dep
        plan['updated_dependencies'] = upd_dep
        
        self = cls(**plan)
        return self


    
class TaskStatus():
    
    """
    creating server_0
    creating volume_0
    ...
    """
    DEPLOYING = 'deploying'
    FINISHED = 'finished'
    FAILED = 'failed'
    

def volume_to_dict(volume):
    volume_dict = {
        'id': '',
        'status': '',
        'size': '',
        'availability_zone': '',
        'created_at': '',
        'attachments': '',
        'name': '',
        'description': '',
        'volume_type': '',
        'snapshot_id': '',
        'source_volid': '',
        'metadata': '',
        'user_id': '',
        'bootable': '',
        'encrypted': '',
        'replication_status': '',
        'consistencygroup_id': '',
        'shareable': '',
        'links': ''
    }
    return _get_attr(volume, volume_dict)

def volume_type_to_dict(volume_type):
    volume_type_dict = {
        'id': '',
        'name': '',
        'extra_specs': ''
        }
    return _get_attr(volume_type, volume_type_dict)

def _get_attr(obj, items):
    for key in items.keys():
        items[key] = getattr(obj, key, None)
    return items    
    
