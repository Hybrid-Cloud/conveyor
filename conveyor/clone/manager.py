# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# Copyright 2011 Justin Santa Barbara
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
import copy
import functools
import json
import time
import yaml

from oslo_config import cfg
from oslo_log import log as logging
import oslo_messaging as messaging
from oslo_utils import importutils
from oslo_utils import uuidutils

from cinderclient import exceptions as cinderclient_exceptions
from neutronclient.common import exceptions as neutronclient_exceptions
from novaclient import exceptions as novaclient_exceptions

from conveyor import compute
from conveyor import exception
from conveyor import manager
from conveyor import network
from conveyor import utils
from conveyor import volume

from conveyor.brick import base
from conveyor.common import loopingcall
from conveyor.common import plan_status
from conveyor.conveyorheat.api import api as heat
from conveyor.db import api as db_api
from conveyor.i18n import _LE
from conveyor.objects import plan as plan_cls
from conveyor.plan import api as plan_api
from conveyor.resource import resource


resource_from_dict = resource.Resource.from_dict

manager_opts = [
    cfg.ListOpt('resource_managers',
                default=['instance=conveyor.clone.resources.instance.'
                         'manager.CloneManager',
                         'volume=conveyor.clone.resources.volume.'
                         'manager.CloneManager'],
                help='DEPRECATED. each resource manager class path.')
]
clone_opts = [
    cfg.StrOpt('clone_driver',
               default='conveyor.clone.drivers.openstack.driver.'
                       'OpenstackDriver',
               help='Driver to connect cloud')
]


CONF = cfg.CONF
CONF.register_opts(manager_opts)
CONF.register_opts(clone_opts)


LOG = logging.getLogger(__name__)


template_skeleton = '''
heat_template_version: 2013-05-23
description: Generated template
parameters:
resources:
'''

add_destination_res_type = ['OS::Nova::Server', 'OS::Cinder::Volume']
no_action_res_type = ['OS::Neutron::FloatingIP']

RESOURCE_MAPPING = {
    'OS::Neutron::Net': 'network',
    'OS::Neutron::Subnet': 'subnet',
    'OS::Neutron::Port': 'port',
    'OS::Neutron::Router': 'router',
    'OS::Neutron::SecurityGroup': 'securitygroup',
    'OS::Cinder::VolumeType': 'volumeType',
    'OS::Cinder::Volume': 'volume',
    'OS::Nova::Flavor': 'flavor',
    'OS::Nova::KeyPair': 'keypair',
    'OS::Nova::Server': 'instance',
    'OS::Neutron::FloatingIP': 'floatingIp',
    'OS::Neutron::Vip': 'loadbalanceVip',
    'OS::Neutron::Pool': 'loadbalancePool',
    'OS::Neutron::Listener': 'loadbalanceListener',
    'OS::Neutron::PoolMember': 'loadbalanceMember',
    'OS::Neutron::HealthMonitor': 'loadbalanceHealthMonitor',
    'OS::Cinder::ConsistencyGroup': 'consistencyGroup',
    'OS::Cinder::Qos': 'qos'
}


STATE_MAP = {
    'CREATE_IN_PROGRESS': 'cloning',
    'CREATE_COMPLETE': 'finished',
    'CREATE_FAILED': 'error',
}


def manager_dict_from_config(named_manager_config, *args, **kwargs):
    '''create manager class by config file, and set key with class'''
    manager_registry = dict()

    for manager_str in named_manager_config:
        manager_type, _sep, manager = manager_str.partition('=')
        manager_class = importutils.import_class(manager)
        manager_registry[manager_type] = manager_class(*args, **kwargs)

    return manager_registry


class CloneManager(manager.Manager):
    """Manages the clone resources from clone to destruction."""

    target = messaging.Target(version='1.18')

    # How long to wait in seconds before re-issuing a shutdown
    # signal to a instance during power off.  The overall
    # time to wait is set by CONF.shutdown_timeout.
    SHUTDOWN_RETRY_INTERVAL = 10

    def __init__(self, *args, **kwargs):
        """Load configuration options and connect to the hypervisor."""

        self.clone_managers = manager_dict_from_config(
            CONF.resource_managers, self)
        self.volume_api = volume.API()
        self.compute_api = compute.API()
        self.neutron_api = network.API()
        self.heat_api = heat.API()
        self._last_host_check = 0
        self._last_bw_usage_poll = 0
        self._bw_usage_supported = True
        self._last_bw_usage_cell_update = 0

        self._resource_tracker_dict = {}
        self._syncs_in_progress = {}
        self.plan_api = plan_api.PlanAPI()
        clone_driver_class = importutils.import_class(CONF.clone_driver)
        self.clone_driver = clone_driver_class()

        super(CloneManager, self).__init__(service_name="clone",
                                           *args, **kwargs)
        self.conveyor_cmd = base.MigrationCmd()

    def start_template_clone(self, context, template):
        LOG.debug("Clone resources start in clone manager")
        stack = None
        # (1. resolute template,and generate the dependences topo)
        # (2. generate TaskFlow according to dependences topo(first,
        # execute leaf node resource))
        clone_type = CONF.clone_migrate_type
        if 'cold' == clone_type:
            template = self._cold_clone(context, template,
                                        plan_status.STATE_MAP)
        else:
            template = self._live_clone(context, template,
                                        plan_status.STATE_MAP)
        # 1. remove the self-defined keys in template to generate heat template
        src_template = copy.deepcopy(template.get('template'))
        try:
            LOG.error('begin time of heat create resource is %s'
                      % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
            stack = self._create_resource_by_heat(context, template,
                                                  plan_status.STATE_MAP)
            LOG.error('end time of heat create resource is %s'
                      % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        except Exception as e:
            LOG.error("Heat create resource error: %s", e)
            if not template.get('disable_rollback') and stack:
                stack_id = stack.get('stack').get('id')
                self.heat_api.delete_stack(context, stack_id,
                                           template.get('plan_id'))
            return None
        # if plan status is error after create resources
        plan_id = template.get('plan_id')
        plan = self.plan_api.get_plan_by_id(context, plan_id, detail=False)
        plan_state = plan.get('plan_status')
        if 'error' == plan_state:
            LOG.error("Plans deploy error in resources create.")
            if not template.get('disable_rollback') and stack:
                stack_id = stack.get('stack').get('id')
                self.heat_api.delete_stack(context, stack_id, plan_id)
            return None
        stack_info = stack.get('stack')
        # 5. after stack creating success,  start copy data and other steps
        # 5.1 according to resource type get resource manager,
        # then call this manager clone template function
        src_resources = src_template.get("resources")
        if not src_resources:
            values = {}
            values['plan_status'] = plan_status.STATE_MAP.get('FINISHED')
            self.plan_api.update_plan(context, plan_id, values)
            LOG.warning("Clone resource warning: clone resource is empty.")
            return stack_info['id']
        LOG.debug("After pop self define info, resources: %s", src_resources)
        try:
            for key, r_resource in src_resources.items():
                res_type = r_resource['type']
                if res_type in no_action_res_type:
                    values = {}
                    values['plan_status'] = plan_status.STATE_MAP.get(
                        'DATA_TRANS_FINISHED')
                    self.plan_api.update_plan(context, plan_id, values)
                    continue
                # 5.2 resource create successful, get resource ID,
                # and add to resource
                heat_resource = self.heat_api.get_resource(context,
                                                           stack_info['id'],
                                                           key)
                r_resource['id'] = heat_resource.physical_resource_id
                src_template['stack_id'] = stack_info['id']
                # after step need update plan status
                src_template['plan_id'] = plan_id
                # 5.3 call resource manager clone fun
                manager_type = RESOURCE_MAPPING.get(res_type)
                if not manager_type:
                    values = {}
                    values['plan_status'] = plan_status.STATE_MAP.get(
                        'DATA_TRANS_FINISHED')
                    self.plan_api.update_plan(context, plan_id, values)
                    continue
                rs_maganger = self.clone_managers.get(manager_type)
                if not rs_maganger:
                    values = {}
                    values['plan_status'] = plan_status.STATE_MAP.get(
                        'DATA_TRANS_FINISHED')
                    self.plan_api.update_plan(context, plan_id, values)
                    continue
                rs_maganger.start_template_clone(context, key, src_template)
        except Exception as e:
            LOG.error("Clone resource error: %s", e)
            # if clone failed and rollback parameter is true,
            # rollback all resource
            if not template.get('disable_rollback'):
                self.heat_api.delete_stack(context, stack_info['id'], plan_id)

            # set plan status is error
            values = {}
            values['plan_status'] = plan_status.STATE_MAP.get(
                'DATA_TRANS_FAILED')
            self.plan_api.update_plan(context, plan_id, values)
            return None

        # all resources clone success, set plan status finished
        values = {}
        values['plan_status'] = plan_status.STATE_MAP.get('FINISHED')
        self.plan_api.update_plan(context, plan_id, values)
        LOG.debug("Clone resources end in clone manager")
        return stack_info['id']

    def _export_template(self, context, id, sys_clone=False, copy_data=True):
        # get plan info
        plan = self.plan_api.get_plan_by_id(context, id)
        if not plan:
            LOG.error(_LE('get plan %s failed') % id)
            raise exception.PlanNotFound(plan_id=id)
        expire_time = plan.get('expire_at')
        plan_type = plan.get('plan_type')
        resource_map = plan.get('updated_resources')
        LOG.debug("The resource_map is %s" % resource_map)
        for key, value in resource_map.items():
            resource_map[key] = resource_from_dict(value)
        # add migrate port
        if resource_map:
            undo_mgr = None
            try:
                undo_mgr = self.clone_driver.handle_resources(
                    context, id, resource_map, sys_clone, copy_data)
                self._update_plan_resources(context, resource_map, id)
                self._format_template(context, resource_map, id,
                                      expire_time, plan_type)
            except Exception as e:
                LOG.error('The generate template of plan %s failed, and rollback operations,\
                          the error is %s', id, str(e))
                if undo_mgr:
                    undo_mgr._rollback()
                raise exception.ExportTemplateFailed(id=id, msg=str(e))
        self.plan_api.update_plan(context, id,
                                  {'plan_status': plan_status.AVAILABLE,
                                   'sys_clone': sys_clone,
                                   'copy_data': copy_data})
        LOG.debug("Export template end in clone manager")
        return resource_map, expire_time

    def _update_plan_resources(self, context, resource_map, plan_id):
        updated_resources = {}
        for k, v in resource_map.items():
            updated_resources[k] = v.to_dict()
        self.plan_api.update_plan(context, plan_id,
                                  {'updated_resources': updated_resources})

    def export_clone_template(self, context, id, sys_clone, copy_data):
        LOG.debug("Export clone template start in clone manager")
        return self._export_template(context, id, sys_clone, copy_data)

    def export_template_and_clone(self, context, id, destination,
                                  update_resources, sys_clone=False,
                                  copy_data=True):
        LOG.debug('Export template and clone start in clone manager')
        if update_resources:
            self.plan_api.update_plan_resources(context, id,
                                                update_resources)
        self._export_template(context, id, sys_clone, copy_data)
        self.clone(context, id, destination, sys_clone)

    def _format_template(self, context, resource_map, plan_id,
                         expire_time, plan_type):

        resources = resource_map.values()
        template = yaml.load(template_skeleton)
        template['resources'] = {}
        template['parameters'] = {}
        template['expire_time'] = expire_time
        template['plan_type'] = plan_type
        for r_resource in resources:
            template['resources'].update(r_resource.template_resource)
            template['parameters'].update(r_resource.template_parameter)
        plan_template = plan_cls.PlanTemplate(plan_id, template)
        db_api.plan_template_create(context, plan_template.to_dict())

    def _build_rules(self, rules):
        brules = []
        for rule in rules:
            if rule['protocol'] == 'any':
                del rule['protocol']
            rg_id = rule['remote_group_id']
            if rg_id is not None:
                rule['remote_mode'] = "remote_group_id"
                if rg_id == rule['security_group_id']:
                    del rule['remote_group_id']
            if rule.get('tenant_id'):
                del rule['tenant_id']
            if rule.get('tenant_id'):
                del rule['id']
            if rule.get('security_group_id'):
                del rule['security_group_id']
            rule = dict((k, v) for k, v in rule.items() if v is not None)
            brules.append(rule)
        return brules

    def clone(self, context, id, destination, sys_clone):
        LOG.debug("Execute clone plan in clone manager")
        self.plan_api.update_plan(context, id,
                                  {'plan_status': plan_status.CLONING})
        # get plan info
        plan = self.plan_api.get_plan_by_id(context, id)
        if not plan:
            LOG.error(_LE('Get plan %s failed') % id)
            raise exception.PlanNotFound(plan_id=id)
        expire_time = plan.get('expire_at')
        resource_map = plan.get('updated_resources')
        LOG.debug("The resource_map is %s" % resource_map)
        for key, value in resource_map.items():
            resource_map[key] = resource_from_dict(value)
        resource_cb_map = {
            'OS::Nova::Flavor': self.compute_api.get_flavor,
            'OS::Neutron::FloatingIP': self.neutron_api.get_floatingip,
            'OS::Neutron::Net': self.neutron_api.get_network,
            'OS::Neutron::SecurityGroup': self.neutron_api.get_security_group,
            'OS::Neutron::Subnet': self.neutron_api.get_subnet,
            'OS::Nova::KeyPair': self.compute_api.get_keypair,
            'OS::Neutron::Router': self.neutron_api.get_router,
            'OS::Neutron::RouterInterface': self.neutron_api.get_port,
            'OS::Cinder::VolumeType': self.volume_api.get_volume_type,
            'OS::Cinder::Qos': self.volume_api.get_qos_specs
        }
        description_map = {
            'OS::Nova::Flavor': 'Flavor description',
            'OS::Neutron::FloatingIP': 'FloatingIP description',
            'OS::Neutron::Net': 'Network description',
            'OS::Neutron::SecurityGroup': 'Security group description',
            'OS::Neutron::Subnet': 'Subnet description',
            'OS::Neutron::Port': 'Port description',
            'OS::Nova::KeyPair': 'KeyPair description',
            'OS::Neutron::Router': 'Router description',
            'OS::Neutron::RouterInterface': 'RouterInterface description',
            'OS::Cinder::VolumeType': 'VolumeType description',
            'OS::Cinder::Qos': 'Volume Qos description'
        }
        resources = resource_map.values()
        template = yaml.load(template_skeleton)
        template['resources'] = template_resource = {}
        template['parameters'] = {}
        template['expire_time'] = expire_time
        for key, value in resource_map.items():
            if value.type == 'OS::Heat::Stack':
                resource_map.pop(key)
                # stack_prop = value.properties
                self._clone_stack(context, key, value, id, destination)

        if not resource_map:
            self.plan_api.update_plan(
                context, id, {'plan_status': plan_status.FINISHED})
            return
        for r_resource in resources:
            template['resources'].update(
                copy.deepcopy(r_resource.template_resource))
            template['parameters'].update(
                copy.deepcopy(r_resource.template_parameter))

        # handle for lb
        self._handle_lb(template_resource)
        for key in list(template_resource):
            # key would be port_0, subnet_0, etc...
            resource = template_resource[key]
            # change az for volume server
            if resource['type'] in add_destination_res_type:
                resource.get('properties')['availability_zone'] = destination
            if resource['type'] == 'OS::Neutron::Port':
                resource.get('properties').pop('mac_address')
                _pop_need = True
                resource_id = resource_map[key].id
                if not resource_id:
                    _pop_need = False
                else:
                    net_template = resource.get('properties').get('network_id')
                    net_key = net_template.get('get_resource') or \
                        net_template.get('get_param')
                    net_id = resource_map[net_key].id
                    is_exist = self._judge_resource_exist(
                        context, self.neutron_api.get_network, net_id)
                    _pop_need = is_exist
                # maybe exist problem if the network not exist
                for _fix_ip in resource.get('properties', {})\
                                       .get('fixed_ips', []):
                    if _pop_need or not _fix_ip.get('ip_address'):
                        _fix_ip.pop('ip_address', None)
            cb = resource_cb_map.get(resource['type'])
            exist_resource_type = ['OS::Cinder::Volume']
            resource_exist = False
            if resource['type'] in exist_resource_type and \
               resource.get('extra_properties').get('exist'):
                resource_exist = True
            if cb or resource_exist:
                try:
                    resource_id = resource_map[key].id
                    if not resource_id:
                        continue
                    resource_result = cb(context, resource_id)
                    LOG.debug(" resource %s exists", key)
                    # special treatment for floatingip
                    if resource['type'] == 'OS::Neutron::FloatingIP':
                        if resource_result.get('fixed_ip_address'):
                            self.plan_api.update_plan(
                                context, id,
                                {'plan_status': plan_status.ERROR})
                            error_message = 'the floatingip %s exist and be used' \
                                % resource_id
                            raise exception.PlanCloneFailed(id=id,
                                                            msg=error_message)
                    # add parameters into template if exists
                    template['parameters'][key] =\
                        {
                            'default': resource_id,
                            'description': description_map[resource['type']],
                            'type': 'string'
                        }

                    def _update_resource(r_list):
                        if type(r_list) not in (tuple, list):
                            r_list = [r_list]
                        for r in r_list:
                            if type(r) is dict:
                                for _k in list(r):
                                    if _k == 'get_resource':
                                        if r[_k] == key:
                                            r.pop(_k)
                                            r['get_param'] = key
                                    else:
                                        _update_resource(r[_k])

                    _update_resource(template_resource)
                    template_resource.pop(key)
                except (novaclient_exceptions.NotFound,
                        neutronclient_exceptions.NotFound,
                        cinderclient_exceptions.NotFound):
                    pass
        template = {
            "template": {
                "heat_template_version": '2013-05-23',
                "description": "clone template",
                "parameters": template['parameters'],
                "resources": template_resource
            },
            "plan_id": id
        }
        LOG.debug("The template is  %s ", template)
        stack_id = self.start_template_clone(context, template)
        if not stack_id:
            LOG.error(_LE('Clone template error'))
            self.plan_api.update_plan(context, id,
                                          {'plan_status': plan_status.ERROR})

        def _wait_for_plan_finished(context):
            """Called at an interval until the plan status finished"""
            plan = self.plan_api.get_plan_by_id(context, id)
            LOG.debug("Get plan info: %s", plan)
            status = plan.get('plan_status')
            if status in [plan_status.FINISHED, plan_status.ERROR]:
                LOG.info("Plan status: %s.", status)
                raise loopingcall.LoopingCallDone()
        timer = loopingcall.FixedIntervalLoopingCall(_wait_for_plan_finished,
                                                     context)
        timer.start(interval=0.5).wait()

        # add reback resource status after clone finished (wanggang)
        plan = self.plan_api.get_plan_by_id(context, id)
        clone_resources = plan.get('updated_resources', {})
        self.clone_driver.reset_resources(context, clone_resources)
        LOG.debug('end time of clone is %s' %
                  (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))

    def _clone_stack(self, context, key, stack_info, plan_id, des):
        LOG.debug('begin clone stack')
        self.plan_api.update_plan(context, plan_id,
                                  {'plan_status': plan_status.CLONING})
        stack_in = stack_info.to_dict()
        template = stack_in['properties'].get('template')
        template_dict = json.loads(template)
        origin_template_dict = copy.deepcopy(template_dict)
        disable_rollback = stack_in['properties'].pop('disable_rollback',
                                                      None)
        stack_name = stack_in['properties'].pop('stack_name', None)
        stack_in['properties'].pop('parameters', None)
        stack_in.pop('extra_properties', None)
        stack_in.pop('parameters', None)
        stack_in.pop('id', None)
        stack_in.pop('name', None)
        stack_in['properties']['template'], son_file_template = \
            self._get_template_contents(template_dict, des)
        stack_in['properties']['template'] = \
            json.dumps(stack_in['properties']['template'])
        res = {key: stack_in}
        heat_template = {
            "heat_template_version": '2013-05-23',
            "description": "clone template",
            "resources": res
        }
        stack_kwargs = dict(
            stack_name=stack_name + '-' + uuidutils.generate_uuid(),
            disable_rollback=disable_rollback,
            template=heat_template,
            files=son_file_template
        )
        stack = None
        try:
            stack = self.heat_api.create_stack(context, **stack_kwargs)
            db_api.plan_stack_create(context,
                                     {'stack_id': stack.get('stack').get('id'),
                                      'plan_id': plan_id})
            origin_template_dict['stack_id'] = stack.get('stack').get('id')
            LOG.debug("Create stack info: %s", stack)
        except Exception as e:
            LOG.debug(("Deploy plan %(plan_id)s, with stack error %(error)s."),
                      {'plan_id': plan_id, 'error': e})
            self.plan_api.update_plan(context, plan_id,
                                      {'plan_status': plan_status.ERROR})
            if stack:
                stack_id = stack.get('stack').get('id')
                self.heat_api.delete_stack(context, stack_id, plan_id)
            raise exception.PlanDeployError(plan_id=plan_id)
        stack_id = stack.get('stack').get('id')

        def _wait_for_boot():
            """Called at an interval until the resources are deployed ."""
            stack = self.heat_api.get_stack(context, stack_id)
            state = stack.stack_status
            if state in ["CREATE_COMPLETE", "CREATE_FAILED"]:
                LOG.info("Plane deployed: %s.", state)
                raise loopingcall.LoopingCallDone()
        timer = loopingcall.FixedIntervalLoopingCall(_wait_for_boot)
        timer.start(interval=0.5).wait()

        stack = self.heat_api.get_stack(context, stack_id)
        state = stack.stack_status
        if state == 'CREATE_FAILED':
            self.heat_api.delete_stack(context, stack_id, plan_id)
            self.plan_api.update_plan(context, plan_id,
                                      {'plan_status': plan_status.ERROR})
            raise exception.PlanDeployError(plan_id=plan_id)
        self._copy_data_for_stack(context, origin_template_dict,
                                  stack_id, plan_id)

    def _copy_data_for_stack(self, context, template, stack_id, plan_id):
        volume_template = copy.deepcopy(template)
        template['stack_id'] = stack_id
        volume_resource = {}
        reses = template.get('resources')
        for key, res in reses.items():
            res_type = res.get('type')
            if res_type == 'OS::Cinder::Volume':
                volume_resource[key] = res

            elif res_type and res_type.startswith('file://'):
                son_template = json.loads(res.get('content'))
                son_stack = self.heat_api.get_resource(context,
                                                       stack_id,
                                                       key)
                son_stack_id = son_stack.physical_resource_id
                son_template['stack_id'] = son_stack_id
                self._copy_data_for_stack(context, son_template,
                                          son_stack_id, plan_id)
#             else:
#                 r = res.get('properties',{}).get('resource')
#                 if not r or not r.get('type').startswith('file://'):
#                     continue
#                 ### get_new stack_id
#                 son_template = json.loads(r.get('content'))
#                 son_stack_id = r.get('id')
#                 son_template['stack_id'] = son_stack_id
#                  self._copy_data_for_stack(context, son_template,
#                                            son_stack_id, plan_id)

        if volume_resource:
            volume_template['resources'] = volume_resource
            template = {}
            template['template'] = copy.deepcopy(volume_template)
            template['plan_id'] = plan_id
            self._afther_resource_created_handler(context, template, stack_id)
            plan = self.plan_api.get_plan_by_id(context, plan_id)
            if plan.get('plan_status') == plan_status.ERROR:
                raise exception.PlanCloneFailed(id=id, msg='')

    def _get_template_contents(self, template_dict, des):
        LOG.debug('the origin template is %s', template_dict)
        resources = template_dict.get('resources')
        file_template = {}
        for key, res in resources.items():
            if 'extra_properties' in res:
                res.pop('extra_properties')
            if 'id' in res:
                res.pop('id')
            res_type = res.get('type')
            if 'availability_zone' in res.get('properties', {}):
                res['properties']['availability_zone'] = des
            if res_type and res_type.startswith('file://'):
                file_template[res_type] = res.get('content')
                res.pop('content')
            if res_type == 'OS::Heat::AutoScalingGroup':
                r = res.get('properties', {}).get('resource')
                if r and r.get('type').startswith('file://'):
                    file_template[r.get('type')] = r.get('content')
                    r.pop('content')

        def _get_file_template(file_template):
            final_file_template = {}
            for k, v in file_template.items():
                son_file_template = {}
                son_template_dict = json.loads(v)
                son_res = son_template_dict.get('resources')
                for key, res in son_res.items():
                    if 'extra_properties' in res:
                        res.pop('extra_properties')
                    if 'id' in res:
                        res.pop('id')
                    res_type = res.get('type')
                    if 'availability_zone' in res.get('properties', {}):
                        res['properties']['availability_zone'] = des
                    if res_type and res_type.startswith('file://'):
                        son_file_template[res_type] = res.get('content')
                        res.pop('content')
                final_file_template[k] = json.dumps(son_template_dict)
                if son_file_template:
                    final_son_file_template = _get_file_template(
                        son_file_template)
                    final_file_template.update(final_son_file_template)
            return final_file_template
        return template_dict, _get_file_template(file_template)

    def _handle_lb(self, template_resource):
        vip_pool_dict = {}
        vip_listener_dict = {}
        # get the relation for vip
        for key in list(template_resource):
            resource = template_resource[key]
            if resource['type'] == 'OS::Neutron::Vip':
                pools = []
                listeners = []
                for key_inner in list(template_resource):
                    resoure_inner = template_resource[key_inner]
                    if resoure_inner['type'] == 'OS::Neutron::Pool':
                        proper = resoure_inner.get('properties')
                        vip = proper.get('vip')
                        if vip:
                            if vip.get('get_resource') == key:
                                pools.append(key_inner)
                    if resoure_inner['type'] == 'OS::Neutron::Listener':
                        proper = resoure_inner.get('properties')
                        vip = proper.get('vip_id')
                        if vip:
                            if vip.get('get_resource') == key:
                                listeners.append(key_inner)
                    vip_pool_dict[key] = pools
                    vip_listener_dict[key] = listeners
        for key, value in vip_pool_dict.items():
            vip_resource = template_resource.get(key)
            proper = vip_resource.get('properties')
            vip_properties = {}
            if proper.get('connection_limit'):
                vip_properties['connection_limit'] = proper.get(
                    'connection_limit')
            if proper.get('subnet'):
                vip_properties['subnet'] = proper.get('subnet')
            if proper.get('address'):
                vip_properties['address'] = proper.get('address')
            if proper.get('protocol_port'):
                vip_properties['protocol_port'] = proper.get('protocol_port')
            if proper.get('name'):
                vip_properties['name'] = proper.get('name')
            if proper.get('session_persistence'):
                vip_properties['session_persistence'] = proper.get(
                    'session_persistence')
            for pool_key in value:
                pool_res = template_resource.get(pool_key)
                pool_res.get('properties')['vip'] = vip_properties
        for key, value in vip_listener_dict.items():
            for listener_key in value:
                template_resource.pop(listener_key)
        for key in list(template_resource):
            resource = template_resource[key]
            if resource['type'] == 'OS::Neutron::Vip':
                template_resource.pop(key)

    def _clear_migrate_port(self, context, resource_map):
        for key, value in resource_map.items():
            resource_type = value.type
            if resource_type == 'OS::Nova::Server':
                server_id = value.id
                extra_properties = value.extra_properties
                if extra_properties:
                    migrate_port_id = extra_properties.get('migrate_port_id')
                    if migrate_port_id:
                        try:
                            self.compute_api.interface_detach(context,
                                                              server_id,
                                                              migrate_port_id)
                        except Exception as e:
                            LOG.warning('detach the interface %s from server %s \
                                        error,the exception is %s'
                                        % (migrate_port_id, server_id,
                                           e.msg))

    def _update_plan_task_status(self, context, plan_id, stack_id):
        try:
            events = self.heat_api.events_list(context, stack_id)
        except Exception as e:
            raise exception.V2vException(message=e)
        if not events:
            return
        event = events[0]
        res_name = event.resource_name
        res_status = event.resource_status
        res_event = res_name + ": " + res_status
        values = {}
        values['task_status'] = res_event
        try:
            self.plan_api.update_plan(context, plan_id, values)
        except Exception as e:
            LOG.debug("Update plan %(plan_id) task status error:%(error)s",
                      {'plan_id': plan_id, 'error': e})
            raise exception.PlanUpdateError

    def export_migrate_template(self, context, id,):
        LOG.debug("Export migrate template start in clone manager")
        return self._export_template(context, id)

    def start_template_migrate(self, context, template):
        LOG.debug("Migrate resources start in clone manager")
        src_template = copy.deepcopy(template.get('template'))
        try:
            LOG.error(_LE('begin time of migrate'
                          'create resource is %s')
                      % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
            stack = self._create_resource_by_heat(
                context, template,
                plan_status.MIGRATE_STATE_MAP)
            LOG.error('end time of migrate create resource is %s' %
                      (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        except Exception as e:
            LOG.error(_LE("Heat create resource error: %s"), e)
            return None
        stack_info = stack.get('stack')
        src_resources = src_template.get("resources")
        LOG.debug("After pop self define info, resources: %s", src_resources)
        try:
            for key, r_resource in src_resources.items():
                res_type = r_resource['type']
                # 5.2 resource create successful, get resource ID,
                # and add to resource
                heat_resource = self.heat_api.get_resource(context,
                                                           stack_info['id'],
                                                           key)
                r_resource['id'] = heat_resource.physical_resource_id
                src_template['stack_id'] = stack_info['id']
                src_template['plan_id'] = template.get('plan_id')
                # 5.3 call resource manager clone fun
                manager_type = RESOURCE_MAPPING.get(res_type)
                if not manager_type:
                    continue
                rs_maganger = self.clone_managers.get(manager_type)
                if not rs_maganger:
                    continue
                rs_maganger.start_template_migrate(context, key, src_template)
            LOG.debug("Migrate resources end in clone manager")
            return stack_info['id']
        except Exception as e:
            LOG.error(_LE("Migrate resource error: %s"), e)
            # if clone failed and rollback parameter is true,
            # rollback all resource
            if not template.get('disable_rollback'):
                self.heat_api.delete_stack(context, stack_info['id'],
                                           template.get('plan_id'))
            return None

    def _create_resource_by_heat(self, context, template, state_map):
        # 1. remove the self-defined keys in template to generate heat template
        stack_template = template['template']
        resources = stack_template['resources']
        # remove the self-defined keys in template
        for key, res in resources.items():
            if 'extra_properties' in res:
                res.pop('extra_properties')
        son_file_template, f_template = self._parse_floatingip(template)
        # 2. call heat create stack interface
        plan_id = template.get('plan_id')
        disable_rollback = template.get('disable_rollback') or True
        stack_name = 'stack-' + uuidutils.generate_uuid()
        stack_kwargs = dict(stack_name=stack_name,
                            disable_rollback=disable_rollback,
                            template=f_template,
                            files=son_file_template
                            )
        try:
            stack = self.heat_api.create_stack(context, **stack_kwargs)
            db_api.plan_stack_create(context,
                                     {'stack_id': stack.get('stack').get('id'),
                                      'plan_id': plan_id})
            LOG.debug("Create stack info: %s", stack)
        except Exception as e:
            LOG.debug(("Deploy plan %(plan_id)s, with stack error %(error)s."),
                      {'plan_id': plan_id, 'error': e})
            raise exception.PlanDeployError(plan_id=plan_id)
        # 3. update plan info in plan table
        values = {}
        stack_info = stack.get('stack')
        values['stack_id'] = stack_info['id']
        self.plan_api.update_plan(context, plan_id, values)
        # 4. check stack status and update plan status

        def _wait_for_boot():
            values = {}
            """Called at an interval until the resources are deployed ."""
            stack = self.heat_api.get_stack(context, stack_info['id'])
            state = stack.stack_status
            values['plan_status'] = state_map.get(state)
            # update plan status
            self.plan_api.update_plan(context, plan_id, values)
            # update plan task status
            self._update_plan_task_status(context, plan_id, stack_info['id'])
            if state in ["CREATE_COMPLETE", "CREATE_FAILED"]:
                LOG.info("Plane deployed: %s.", state)
                raise loopingcall.LoopingCallDone()
        timer = loopingcall.FixedIntervalLoopingCall(_wait_for_boot)
        timer.start(interval=0.5).wait()
        return stack

    def migrate(self, context, id, destination):
        LOG.debug("execute migrate plan in clone manager")
        self.plan_api.update_plan(context, id,
                                  {'plan_status': plan_status.MIGRATING})
        # call export_migrate_template
        resource_map = None
        expire_time = None
        try:
            resource_map, expire_time = self.export_migrate_template(context,
                                                                     id)
        except (exception.ExportTemplateFailed, exception.PlanNotFound):
            self.plan_api.update_plan(context, id,
                                      {'plan_status': plan_status.ERROR})
            return
        resource_cb_map = {
            'OS::Nova::Flavor': self.compute_api.get_flavor,
            'OS::Neutron::Net': self.neutron_api.get_network,
            'OS::Neutron::SecurityGroup': self.neutron_api.get_security_group,
            'OS::Neutron::Subnet': self.neutron_api.get_subnet,
            'OS::Nova::KeyPair': self.compute_api.get_keypair,
            'OS::Neutron::Router': self.neutron_api.get_router,
            'OS::Neutron::RouterInterface': self.neutron_api.get_port,
            'OS::Cinder::VolumeType': self.volume_api.get_volume_type
        }
        description_map = {
            'OS::Nova::Flavor': 'Flavor description',
            'OS::Neutron::Net': 'Network description',
            'OS::Neutron::SecurityGroup': 'Security group description',
            'OS::Neutron::Subnet': 'Subnet description',
            'OS::Nova::KeyPair': 'KeyPair description',
            'OS::Neutron::Router': 'Router description',
            'OS::Neutron::RouterInterface': 'RouterInterface description',
            'OS::Cinder::VolumeType': 'VolumeType description'
        }
        resources = resource_map.values()
        template = yaml.load(template_skeleton)
        template['resources'] = template_resource = {}
        template['parameters'] = {}
        # add expire_time
        template['expire_time'] = expire_time
        for r_resource in resources:
            template['resources'].update(copy.deepcopy(
                r_resource.template_resource))
            template['parameters'].update(copy.deepcopy(
                r_resource.template_parameter))
        # { 'server_0': ('server_0.id, [('port_0','port_0.id']) }
        original_server_port_map = {}
        for name in template_resource:
            r = template_resource[name]
            if r['type'] == 'OS::Nova::Server':
                port_list = []
                for p in r['properties'].get('networks', []):
                    port_name = p.get('port', {}).get('get_resource')
                    port_id = template_resource[port_name]\
                        .get('extra_properties')['id']
                    net_name = template_resource[port_name].get('properties')\
                                                           .get('network_id')\
                                                           .get('get_resource')
                    net_id = template_resource[net_name] \
                        .get('extra_properties')['id']
                    is_exist = self._judge_resource_exist(
                        context, self.neutron_api.get_network, net_id)
                    if is_exist:
                        port_list.append((port_name, port_id))
                original_server_port_map[name] = (r.get('extra_properties')
                                                  ['id'], port_list)
        # port & fp map {'port_0':[('floatingip_0','floatingip_0.id','fix_ip')]
        exist_port_fp_map = {}
        unassociate_floationgip = []
        for key in list(template_resource):
            resource = template_resource[key]
            if resource['type'] == 'OS::Neutron::Port':
                fp_list = []
                for name in list(template_resource):
                    resource_roll = template_resource[name]
                    if resource_roll['type'] == 'OS::Neutron::FloatingIP':
                        if resource_roll['properties'].get('port_id') \
                                .get('get_resource') == key:
                            floatingip_net_key = resource_roll['properties']\
                                .get('floating_network_id', {}) \
                                .get('get_resource')
                            net_id = template_resource[floatingip_net_key]\
                                .get('extra_properties')['id']
                            is_exist = self._judge_resource_exist(
                                context,
                                self.neutron_api.get_network,
                                net_id)
                            if is_exist:
                                fix_ip_index = resource_roll['properties']\
                                    .get('fixed_ip_address', {}) \
                                    .get('get_attr')[2]
                                fix_ip = resource['properties'].get('fixed_ips')[fix_ip_index]\
                                    .get('ip_address')
                                fp_list.append(
                                    (name,
                                     resource_roll.get('extra_properties')
                                     ['id'],
                                     fix_ip))
                                floatingip_id = \
                                    resource_roll.get('extra_properties')['id']
                                floatingip_info = \
                                    self.neutron_api.get_floatingip(
                                        floatingip_id)
                                floationgip_port_id = floatingip_info.get(
                                    'port_id')
                                if not floationgip_port_id:
                                    unassociate_floationgip.append(
                                        floatingip_id)
                exist_port_fp_map[key] = fp_list

        for key in list(template_resource):
            # need pop from template ,such as floatingip
            resource = template_resource[key]
            if resource['type'] == 'OS::Neutron::FloatingIP':
                floatingip_net_key = resource['properties'] \
                    .get('floating_network_id', {}).get('get_resource')
                net_resource = resource_map[floatingip_net_key]
                net_resource_id = net_resource.id
                cb = resource_cb_map.get(net_resource.type)
                if cb:
                    try:
                        if not net_resource_id:
                            continue
                        cb(context, net_resource_id)
                        LOG.debug("Resource %s exists, remove the floatingip",
                                  floatingip_net_key)
                        template_resource.pop(key)
                        continue
                    except neutronclient_exceptions.NotFound:
                        pass
        for key in list(template_resource):
            # key would be port_0, subnet_0, etc...
            resource = template_resource[key]
            # change az for volume server
            if resource['type'] in add_destination_res_type:
                resource.get('properties')['availability_zone'] = destination
            cb = resource_cb_map.get(resource['type'])
            if cb:
                try:
                    resource_id = resource_map[key].id
                    if not net_resource_id:
                        continue
                    cb(context, resource_id)
                    LOG.debug(" resource %s exists", key)
                    # if the network exists,pop the ip_address
                    if resource['type'] == 'OS::Neutron::Net':
                        for _k in template_resource:
                            _r = template_resource[_k]
                            if _r['type'] == 'OS::Neutron::Port' and \
                                _r['properties'].get('network_id', {})\
                                                .get('get_resource') == key:
                                _r.get('properties').pop('mac_address')
                                for _f in _r['properties'].get('fixed_ips',
                                                               []):
                                    _f.pop('ip_address', None)
                    # add parameters into template if exists
                    template['parameters'][key] = {
                        'default': resource_id,
                        'description': description_map[resource['type']],
                        'type': 'string'
                    }

                    def _update_resource(r_list):
                        if type(r_list) not in (tuple, list):
                            r_list = [r_list]
                        for r in r_list:
                            if type(r) is dict:
                                for _k in list(r):
                                    if _k == 'get_resource':
                                        if r[_k] == key:
                                            r.pop(_k)
                                            r['get_param'] = key
                                    else:
                                        _update_resource(r[_k])

                    _update_resource(template_resource)
                    template_resource.pop(key)
                except (novaclient_exceptions.NotFound,
                        neutronclient_exceptions.NotFound):
                    pass
        template = {
            "template":
                {
                    "heat_template_version": '2013-05-23',
                    "description": "clone template",
                    "parameters": template['parameters'],
                    "resources": template_resource
                },
            "plan_id": id
        }
        LOG.debug("the template is  %s ", template)
        stack_id = self.start_template_migrate(context, template)
        if not stack_id:
            LOG.error('clone template error')
            self.plan_api.update_plan(context, id,
                                      {'plan_status': plan_status.ERROR})
            raise exception.PlanCloneFailed(id=id, msg='')
        # after finish the clone plan ,detach migrate port
        if CONF.migrate_net_map:
            self._clear_migrate_port(context, resource_map)
        plan = self.plan_api.get_plan_by_id(context, id)
        if plan.get('plan_status') == plan_status.ERROR:
            raise exception.PlanMigrateFailed(id=id)
        self._realloc_port_floating_ip(context, id, original_server_port_map,
                                       exist_port_fp_map,
                                       unassociate_floationgip,
                                       resource_map, stack_id)
        self._clear(context, resource_map)
        self.plan_api.update_plan(context, id,
                                  {'plan_status': plan_status.FINISHED})
        LOG.error('begin time of migrate is %s' %
                  (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))

    def _judge_resource_exist(self, context, cb, resource_id):
        is_exist = False
        try:
            cb(context, resource_id)
            LOG.debug('the resource %s exist' % resource_id)
            is_exist = True
        except (novaclient_exceptions.NotFound,
                neutronclient_exceptions.NotFound):
            pass
        return is_exist

    def _realloc_port_floating_ip(self, context, id,
                                  server_port_map, port_fp_map,
                                  unassociate_floationgip, resource_map,
                                  stack_id):
        key_dst_heat_resource = {}

        def _get_resource_id_by_key(key):
            r = key_dst_heat_resource.get(key)
            if not r:
                r = self.heat_api.get_resource(context, stack_id, key)
                key_dst_heat_resource[key] = r
            return r.physical_resource_id
        # { 'server_0': ('server_0.id, [('port_0','port_0.id']) }

        for server_key in list(server_port_map):
            server_id, port_list = server_port_map[server_key]
            port_temp_map = {}

            def _associate_fip(fp_id, port_key):
                port_id = port_temp_map[port_key]
                self.neutron_api.associate_floating_ip(context, fp_id, port_id)

            def _delete_port(port_id):
                self.neutron_api.delete_port(context, port_id)

            def _detach_port(server_id, port_id):
                self.compute_api.interface_detach(context, server_id, port_id)

            def _create_attach_port(server_id, port_key, port_param):
                try:
                    port_id_new = self.neutron_api.create_port(
                        context, {'port': port_params})
                    port_temp_map[port_key] = port_id_new
                except (neutronclient_exceptions.IpAddressInUseClient,
                        neutronclient_exceptions.MacAddressInUseClient):
                    port_id_new = port_temp_map[port_key]
                self.compute_api.interface_attach(context, server_id,
                                                  None, port_id_new)

            undo_mgr = utils.UndoManager()
            dst_server_id = _get_resource_id_by_key(server_key)
            try:
                for port_key, port_id in port_list:
                    # {'port_0':[('floatingip_0','floatingip_0.id', 'fix_ip')]
                    fp_list = port_fp_map.get(port_key)
                    port_temp_map[port_key] = port_id
                    for (fp_key, fp_id, fix_ip) in fp_list:
                        # disassociate floating_ip
                        LOG.debug('Begin disassociate floating_ip %s' % fp_id)
                        if fp_id not in unassociate_floationgip:
                            self.neutron_api.disassociate_floating_ip(context,
                                                                      fp_id)
                            undo_mgr.undo_with(functools.partial(
                                _associate_fip, fp_id, port_key))

                    LOG.debug('Begin detach interface %s form server %s ' %
                              (port_id, server_id))
                    # Note interface_detach will delete the port.
                    self.compute_api.interface_detach(context, server_id,
                                                      port_id)
                    port_info = resource_map.get(port_key)
                    net_key = port_info.properties.get('network_id')\
                                                  .get('get_resource')
                    security_group_list = port_info.properties.get(
                        'security_groups')
                    security_group_ids = []
                    for security_group in security_group_list:
                        security_group_key = security_group.get('get_resource')
                        security_group_id = resource_map.get(
                            security_group_key).id
                        security_group_ids.append(security_group_id)
                    port_params = {'network_id': resource_map.get(net_key).id,
                                   'security_groups': security_group_ids,
                                   'admin_state_up': True,
                                   'mac_address': port_info.properties.get(
                        'mac_address')
                        }
                    for fix in port_info.properties.get('fixed_ips', []):
                        subnet_key = fix.get('subnet_id', {}) \
                                        .get('get_resource')
                        subnet_id = resource_map.get(subnet_key).id
                        fix_param = {'subnet_id': subnet_id,
                                     'ip_address': fix.get('ip_address')}
                        port_params.setdefault('fixed_ips', []) \
                                   .append(fix_param)

                    # interface_detach rolling back callback
                    undo_mgr.undo_with(functools.partial(_create_attach_port,
                                                         server_id, port_key,
                                                         port_params))
                    create_port_attempt = 150
                    for i in range(create_port_attempt):
                        try:
                            LOG.debug('Begin create new port %s', port_params)
                            port_id_new = self.neutron_api.create_port(
                                context, {'port': port_params})
                            LOG.debug('Create new port success the port is %s',
                                      port_id_new)
                            port_temp_map[port_key] = port_id_new
                            undo_mgr.undo_with(functools.partial(_delete_port,
                                                                 port_id_new))
                            break
                        except (neutronclient_exceptions.IpAddressInUseClient,
                                neutronclient_exceptions.MacAddressInUseClient
                                ):
                            time.sleep(1)
                            pass
                    if not port_id_new:
                        port_id_new = port_id
                    dst_server_port_id = _get_resource_id_by_key(port_key)
                    LOG.debug('begin detach interface %s from server %s'
                              % (dst_server_port_id, dst_server_id))
                    # no rolling back for dst server.
                    self.compute_api.interface_detach(context, dst_server_id,
                                                      dst_server_port_id)
                    LOG.debug('begin attach interface %s to server %s'
                              % (port_id_new, dst_server_id))
                    # no rolling back for dst server.
                    self.compute_api.interface_attach(context, dst_server_id,
                                                      None, port_id_new)
                    # no rolling back for dst server.
                    if fp_list:
                        LOG.debug('begin associate floating_ip %s to server %s'
                                  % (fp_id, dst_server_id))
                        self.neutron_api.associate_floating_ip(
                            context,
                            fp_id,
                            port_id_new,
                            fixed_address=fix_ip)
                        undo_mgr.undo_with(functools.partial(
                            self.heat_api.delete_stack, context, stack_id, id))
            except Exception as e:
                LOG.exception("Failed migrate server_id %s due to %s,"
                              "so rollback it.",
                              server_id, str(e.msg))
                LOG.error("START rollback for %s ......", server_id)
                undo_mgr._rollback()
                self.plan_api.update_plan(
                    context, id, {'plan_status': plan_status.ERROR})
                try:
                    self.heat_api.delete_stack(context, stack_id, id)
                except Exception:
                    pass
                LOG.error("END rollback for %s ......", server_id)
                raise exception.PlanMigrateFailed(id=id)

    def _clear(self, context, resource_map):
        for key, value in resource_map.items():
            resource_type = value.type
            if resource_type == 'OS::Nova::Server':
                server_id = value.id
                server = self.compute_api.get_server(context, server_id)
                volume_ids = []
                if getattr(server, 'os-extended-volumes:volumes_attached', ''):
                    volumes = getattr(server,
                                      'os-extended-volumes:volumes_attached',
                                      [])
                    for v in volumes:
                        if v.get('id'):
                            volume_ids.append(v.get('id'))
                self.compute_api.delete_server(context, server_id)
                timer = loopingcall.FixedIntervalLoopingCall(
                    self._wait_for_server_termination, context, server_id)
                timer.start(interval=0.5).wait()
                for v_id in volume_ids:
                    self.volume_api.delete(context, v_id)

    def download_template(self, context, plan_id):
        try:
            plan_template = db_api.plan_template_get(context, plan_id)
            content = plan_template.get('template', {})
            return {'template': content}
        except Exception as e:
            LOG.error(_LE('Download template for %(id)s failed: %(err)s '),
                      {'id': plan_id, 'err': e})
            raise exception.DownloadTemplateFailed(id=plan_id,
                                                   msg=IOError.message)

    def _wait_for_server_termination(self, context, server_id):
        while True:
            try:
                self.compute_api.delete_server(context, server_id)
                server = self.compute_api.get_server(context, server_id)
            except novaclient_exceptions.NotFound:
                LOG.debug('the server %s deleted ' % server_id)
                raise loopingcall.LoopingCallDone()
            server_status = server.status
            if server_status == 'ERROR':
                LOG.debug('the server %s delete failed' % server_id)
                loopingcall.LoopingCallDone()

    def _parse_floatingip(self, template_resource):
        t = template_resource.get('template')
        plan_id = template_resource.get('plan_id')
        if not t:
            return None, None
        file_name = 'file://' + CONF.plan_file_path + plan_id \
                              + '.floatingIp.template'
        fip_properties = {}
        fip_template_resource = {
            'type': file_name,
            'properties': fip_properties
        }
        idx = 0
        fip_prefix = 'floatingip'
        net_prefix = 'floating_network_id'
        subnet_prefix = 'subnet_id'
        resources = t.get('resources', {})
        new_resources = {}
        is_floatingIp = False
        fip_file_template = {
            'description': 'Generated template',
            'heat_template_version': '2013-05-23',
            'parameters': {},
            'resources': {},
        }
        for name, rs in resources.items():
            if rs.get('type') != 'OS::Neutron::FloatingIP':
                new_resources[name] = rs
                continue
            is_floatingIp = True
            net_id = rs.get('properties', {}).get('floating_network_id', {})\
                                             .get('get_resource', None)
            if not net_id:
                new_resources[name] = rs
                continue
            # found the first subnet
            subnet_id = None
            for _name, _rs in resources.items():
                if _rs.get('type') == 'OS::Neutron::Subnet' and \
                        _rs.get('properties', {}) \
                           .get('network_id', {}) \
                           .get('get_resource') == net_id:
                    subnet_id = _name
                    break

            fip_properties.update({
                net_prefix + '_%d' % idx: {'get_resource': net_id},
                subnet_prefix + '_%d' % idx: {'get_resource': subnet_id},
            })

            fip_file_template['parameters'].update({
                net_prefix + '_%d' % idx: {'type': 'string',
                                           'description': 'network'},
                subnet_prefix + '_%d' % idx: {'type': 'string',
                                              'description': 'subnet'},
            })
            fip_file_template['resources'].update({
                fip_prefix + '_%d' % idx: {
                    'type': rs.get('type'),
                    'properties': {
                        net_prefix: {'get_param': net_prefix + '_%d' % idx}
                    }
                }
            })
            idx += 1

        fip_temp = None
        if is_floatingIp:
            new_resources[fip_prefix] = fip_template_resource
            fip_temp = {file_name: json.dumps(fip_file_template)}

        return fip_temp,  \
            {'description': 'Generated template',
             'heat_template_version': '2013-05-23',
             'parameters': t.get('parameters', {}),
             'resources': new_resources}

    def _cold_clone(self, context, template, state_map):
        '''volume dependence volume type, consistencygroup'''
        '''and volume type dependence qos'''

        LOG.debug('Code clone start for %s', template)
        default_parameter = {'default': '',
                             'type': 'string',
                             'description': ''}
        # 1. define all volume related resources
        volume_res_type = ['OS::Cinder::VolumeType',
                           'OS::Cinder::Volume',
                           'OS::Cinder::Qos',
                           'OS::Cinder::ConsistencyGroup']

        volume_template = copy.deepcopy(template)
        stack_template = template['template']
        volume_resources = volume_template['template'].get('resources', {})
        stack_resources = stack_template['resources']
        parameter = stack_template.get('parameters', {})
        resources = copy.deepcopy(volume_resources)
        vol_res_name = []
        sys_vol_name = []
        # 2. generate volumes template
        for k, res in resources.items():
            res_type = res.get('type')

            # remove not volume needed resource
            if res_type not in volume_res_type:
                volume_resources.pop(k)
            else:
                # remove volume related resource from template
                stack_resources.pop(k)
                parameter[k] = copy.deepcopy(default_parameter)
                vol_res_name.append(k)
            # if clone system volume, remove volume image info
            if 'OS::Cinder::Volume' == res_type:
                    ext_properties = res.get('extra_properties')
                    sys_clone = ext_properties.get('sys_clone')
                    if sys_clone:
                        properties = volume_resources.get(k).get('properties',
                                                                 {})
                        # properties.pop('image', None)
                        properties['image'] = CONF.sys_image
                        sys_vol_name.append(k)
        origin_template = copy.deepcopy(volume_template)
        for key, res in volume_resources.items():
            if 'extra_properties' in res:
                res.pop('extra_properties')

        LOG.debug('Code clone volume for %s', volume_template)
        stack_id = None
        if volume_resources:
            try:
                # 3. deploy volumes template
                stack = self._create_resource_by_heat(context,
                                                      volume_template,
                                                      state_map)
                # 4. copy data
                stack_id = stack.get('stack').get('id')
                self._afther_resource_created_handler(context,
                                                      origin_template,
                                                      stack_id)
#                for sys_vol in sys_vol_name:
#                    heat_resource = self.heat_api.get_resource(context,
#                                                               stack_id,
#                                                               sys_vol)
#                    res_id = heat_resource.physical_resource_id
#                    self.volume_api.set_volume_bootable(context, res_id, True)
            except Exception as e:
                LOG.error('Code clone error: %s', e)
                raise
        # 5. modify input template
        for name in vol_res_name:
            heat_resource = self.heat_api.get_resource(context, stack_id, name)
            res_id = heat_resource.physical_resource_id
            parameter.get(name)['default'] = res_id
            for r_n, res in stack_resources.items():
                res_type = res.get('type')
                properties = res.get('properties')
                ext_properties = res.get('extra_properties')
                if 'OS::Nova::Server' == res_type:
                    bdms = properties.get('block_device_mapping_v2', {})
                    for bdm in bdms:
                        volume_id = bdm.get('volume_id')
                        if isinstance(volume_id, dict):
                            n = volume_id.get('get_resource')
                            if n == name:
                                bdm['volume_id'] = {'get_param': name}
#                         boot_index = bdm.get('boot_index')
#                         # if system disk is volume, remove image info
#                         sys_clone = ext_properties.get('sys_clone')
#                         if boot_index in [0, '0'] and sys_clone:
#                             properties.pop('image')
                else:
                    continue

        # 6. return modify
        LOG.debug('Code clone template for finishing volumes: %s', template)
        return template

    def _live_clone(self, context, template, state_map):

        LOG.debug('Live clone start for %s', template)
        default_parameter = {'default': '',
                             'type': 'string',
                             'description': ''}

        # 1. define all volume related resources
        stack_template = template['template']
        stack_resources = stack_template.get('resources', {})
        sys_volumes = self._system_volumes_to_clone(stack_resources)
        if not sys_volumes:
            return template

        volume_template = copy.deepcopy(template)
        volume_resources = volume_template['template'].get('resources', {})
        parameter = stack_template.get('parameters', {})
        vol_res_name = []
        sys_resources = {}
        # 2. generate volumes template and modify input template
        for vm, v_volume in sys_volumes.items():
            volume_resource = volume_resources.get(v_volume)
            properties = volume_resource.get('properties')
            # remove volume image info
            properties.pop('image')
            # add volume to new template
            sys_resources[v_volume] = volume_resource
            vol_res_name.append(v_volume)
            # remove volume in source template
            stack_resources.pop(v_volume)
            parameter[v_volume] = copy.deepcopy(default_parameter)
            # change 'get_resource' to get_param'
            self._change_resource_to_param(template, v_volume)
            # if volume dependence volume type, add it to volume
            # template. Then delete it in source template, and modify
            # other dependence this volume type resources to 'get param'
            # 2.1 add volume dependence volume type
            volume_type = properties.get('volume_type', None)
            if volume_type and isinstance(volume_type, dict):
                for key, type_name in volume_type.items():
                    if 'get_resource' == key:
                        type_res = volume_resources.get(type_name)
                        # add volume type to new template
                        sys_resources[type_name] = type_res
                        vol_res_name.append(type_name)
                        # remove volume type in source template
                        stack_resources.pop(type_name)
                        parameter[type_name] = copy.deepcopy(default_parameter)
                        # change 'get_resource' to 'get_param'
                        self._change_resource_to_param(template, type_name)
                        # 2.2 add volume type dependence qos
                        type_properties = type_res.get('properties')
                        qos_id = type_properties.get('qos_specs_id')
                        if qos_id and isinstance(qos_id, dict):
                            for qos_key, qos_name in qos_id.items():
                                if 'get_resource' == qos_key:
                                    qos_res = volume_resources.get(qos_name)
                                    # add qos to new template
                                    sys_resources[qos_name] = qos_res
                                    vol_res_name.append(qos_name)
                                    # remove qos in source template
                                    stack_resources.pop(qos_name)
                                    parameter[qos_name] = copy.deepcopy(
                                        default_parameter)
                                    # change 'get_resource' to 'get_param'
                                    self._change_resource_to_param(template,
                                                                   qos_name)
        volume_template['template']['resources'] = sys_resources
        LOG.debug('Live clone volume for %s', volume_template)
        origin_template = copy.deepcopy(volume_template)
        for key, res in volume_resources.items():
            if 'extra_properties' in res:
                res.pop('extra_properties')
        stack_id = None
        if sys_resources:
            try:
                # 3. deploy new(volumes) template
                stack = self._create_resource_by_heat(context,
                                                      volume_template,
                                                      state_map)
                # 4. copy data
                stack_id = stack.get('stack').get('id')
                self._afther_resource_created_handler(context,
                                                      origin_template,
                                                      stack_id)
                for k, v in sys_volumes.items():
                    heat_resource = self.heat_api.get_resource(context,
                                                               stack_id, v)
                    res_id = heat_resource.physical_resource_id
                    self.volume_api.set_volume_bootable(context, res_id, True)
            except Exception as e:
                LOG.error('Live clone error: %s', e)
                raise

        # 5. use new template resource id update source template
        # parameters info after new template deployed,
        # resource id has generated,
        # then add this id in source template and deploy source template
        for res_name in vol_res_name:
            heat_resource = self.heat_api.get_resource(context,
                                                       stack_id,
                                                       res_name)
            res_id = heat_resource.physical_resource_id
            parameter.get(res_name)['default'] = res_id

        # 6. return modify
        LOG.debug('Live clone template for finishing volumes: %s', template)
        return template

    def _afther_resource_created_handler(self, context, template, stack_id):

        src_template = template['template']
        src_resources = src_template.get('resources')
        plan_id = template.get('plan_id')
        try:
            for key, r_resource in src_resources.items():
                res_type = r_resource['type']
                if res_type in no_action_res_type:
                    values = {}
                    values['plan_status'] = plan_status.STATE_MAP.get(
                        'DATA_TRANS_FINISHED')
                    self.plan_api.update_plan(context, plan_id, values)
                    continue
                # 5.2 resource create successful, get resource ID,
                # and add to resource
                heat_resource = self.heat_api.get_resource(context, stack_id,
                                                           key)
                r_resource['id'] = heat_resource.physical_resource_id
                src_template['stack_id'] = stack_id
                # after step need update plan status
                src_template['plan_id'] = plan_id
                # 5.3 call resource manager clone fun
                manager_type = RESOURCE_MAPPING.get(res_type)
                if not manager_type:
                    values = {}
                    values['plan_status'] = plan_status.STATE_MAP.get(
                        'DATA_TRANS_FINISHED'
                    )
                    self.plan_api.update_plan(context, plan_id, values)
                    continue
                rs_maganger = self.clone_managers.get(manager_type)
                if not rs_maganger:
                    values = {}
                    values['plan_status'] = plan_status.STATE_MAP.get(
                        'DATA_TRANS_FINISHED')
                    self.plan_api.update_plan(context, plan_id, values)
                    continue
                rs_maganger.start_template_clone(context, key, src_template)
        except Exception as e:
            LOG.error("Clone resource error: %s", e)
            # if clone failed and rollback parameter is true,
            # rollback all resource
            if not template.get('disable_rollback'):
                self.heat_api.delete_stack(context, stack_id, plan_id)

            # set plan status is error
            values = {}
            values['plan_status'] = plan_status.STATE_MAP.get(
                'DATA_TRANS_FAILED')
            self.plan_api.update_plan(context, plan_id, values)
            return None

    def _system_volumes_to_clone(self, resources):
        '''list all vm and it need to clone sys volume:{'vmname':'volname'}'''

        vm_sys_dict = {}
        for k, res in resources.items():
            res_type = res.get('type')
            # remove not volume needed resource
            if 'OS::Nova::Server' == res_type:
                properties = res.get('properties', {})
                ext_properties = res.get('extra_properties')
                sys_clone = ext_properties.get('sys_clone')
                bdms = properties.get('block_device_mapping_v2', {})
                for bdm in bdms:
                    volume_id = bdm.get('volume_id')
                    if isinstance(volume_id, dict):
                        vol_name = volume_id.get('get_resource')
                    boot_index = bdm.get('boot_index')
                    if boot_index in [0, '0'] and sys_clone:
                        vm_sys_dict[k] = vol_name
                        break
        return vm_sys_dict

    def _change_resource_to_param(self, template, res_name):

        stack_template = template['template']
        stack_resources = stack_template.get('resources', {})
        for r_name, res in stack_resources.items():
            properties = res.get('properties', {})
            for p_key, p_value in properties.items():
                if isinstance(p_value, dict):
                    for k, v in p_value.items():
                        if v == res_name and k == 'get_resource':
                            p_value.pop(k)
                            p_value['get_param'] = v
            res_type = res.get('type')
            if 'OS::Nova::Server' == res_type:
                bdms = properties.get('block_device_mapping_v2', {})
                for bdm in bdms:
                    volume_id = bdm.get('volume_id')
                    if isinstance(volume_id, dict):
                        for k, v in volume_id.items():
                            if v == res_name and k == 'get_resource':
                                volume_id.pop(k)
                                volume_id['get_param'] = v
