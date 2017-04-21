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

import functools
import json

from oslo_config import cfg
from oslo_log import log as logging

from conveyor.clone.drivers import driver
from conveyor.conveyoragentclient.v1 import client as birdiegatewayclient
from conveyor import exception
from conveyor.i18n import _LE
from conveyor import utils


CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class OpenstackDriver(driver.BaseDriver):
    def __init__(self):
        super(OpenstackDriver, self).__init__()

    def handle_resources(self, context, plan_id, resource_map, sys_clone):
        LOG.debug('Begin handle resources')
        undo_mgr = utils.UndoManager()
        try:
            self._add_extra_properties(context, resource_map, sys_clone,
                                       undo_mgr)
            self._set_resources_state(context, resource_map)
            return undo_mgr
        except Exception as e:
            LOG.error(_LE('The generate template of plan %s failed, and rollback operations,\
                      the error is %s'), plan_id, str(e))
            undo_mgr._rollback()
            raise exception.ExportTemplateFailed(id=plan_id, msg=str(e))

    def _set_resources_state(self, context, resource_map):
        for key, value in resource_map.items():
            resource_type = value.type
            resource_id = value.id
            if resource_type == 'OS::Nova::Server':
                self.compute_api.reset_state(context, resource_id, 'cloning')
            elif resource_type == 'OS::Cinder::Volume':
                self.volume_api.reset_state(context, resource_id, 'cloning')
            elif resource_type == 'OS::Heat::Stack':
                self._set_resources_state_for_stack(context, value)

    def _set_resources_state_for_stack(self, context, resource):
        template_str = resource.properties.get('template')
        template = json.loads(template_str)

        def _set_state(template):
            temp_res = template.get('resources')
            for key, value in temp_res.items():
                res_type = value.get('type')
                if res_type == 'OS::Cinder::Volume':
                    vid = value.get('extra_properties', {}).get('id')
                    if vid:
                        self.volume_api.reset_state(context, vid, 'cloning')
                elif res_type == 'OS::Nova::Server':
                    sid = value.get('extra_properties', {}).get('id')
                    if sid:
                        self.compute_api.reset_state(context, sid, 'cloning')
                elif res_type and res_type.startswith('file://'):
                    son_template = value.get('content')
                    son_template = json.loads(son_template)
                    _set_state(son_template)
        _set_state(template)

    def add_extra_properties_for_server(self, context, resource, resource_map,
                                        sys_clone, undo_mgr):
        migrate_net_map = CONF.migrate_net_map
        server_properties = resource.properties
        server_id = resource.id
        server_extra_properties = resource.extra_properties
        server_az = server_properties.get('availability_zone')
        vm_state = server_extra_properties.get('vm_state')
        gw_url = server_extra_properties.get('gw_url')
        if not gw_url:
            if vm_state == 'stopped':
                gw_id, gw_ip = utils.get_next_vgw(server_az)
                if not gw_id or not gw_ip:
                    raise exception.V2vException(message='no vgw host found')
                gw_url = gw_ip + ':' + str(CONF.v2vgateway_api_listen_port)
                resource.extra_properties.update({"gw_url": gw_url,
                                                  "gw_id": gw_id})
                resource.extra_properties['sys_clone'] = sys_clone
                resource.extra_properties['is_deacidized'] = True
                block_device_mapping = server_properties.get(
                    'block_device_mapping_v2')
                if block_device_mapping:
                    for block_device in block_device_mapping:
                        volume_name = block_device.get('volume_id').get(
                            'get_resource')
                        volume_resource = resource_map.get(volume_name)
                        volume_resource.extra_properties['gw_url'] = gw_url
                        volume_resource.extra_properties['is_deacidized'] = \
                            True
                        boot_index = block_device.get('boot_index')
                        dev_name = block_device.get('device_name')
                        if boot_index == 0 or boot_index == '0':
                            volume_resource.extra_properties['sys_clone'] = \
                                sys_clone
                            if sys_clone:
                                self._handle_sv_for_svm(context,
                                                        volume_resource,
                                                        gw_id, gw_ip, undo_mgr)
                        else:
                            self._handle_dv_for_svm(context, volume_resource,
                                                    server_id, dev_name,
                                                    gw_id, gw_ip, undo_mgr)
            else:
                if migrate_net_map:
                    # get the availability_zone of server
                    server_az = server_properties.get('availability_zone')
                    if not server_az:
                        LOG.error(_LE('Not get the availability_zone'
                                      'of server %s') % resource.id)
                        raise exception.AvailabilityZoneNotFound(
                            server_uuid=resource.id)
                    migrate_net_id = migrate_net_map.get(server_az)
                    if not migrate_net_id:
                        LOG.error(_LE('Not get the migrate net of server %s')
                                  % resource.id)
                        raise exception.NoMigrateNetProvided(
                            server_uuid=resource.id)
                    # attach interface
                    LOG.debug('Attach a port of net %s to server %s',
                              migrate_net_id,
                              server_id)
                    obj = self.compute_api.interface_attach(context, server_id,
                                                            migrate_net_id,
                                                            None,
                                                            None)
                    interface_attachment = obj._info
                    if interface_attachment:
                        LOG.debug('The interface attachment info is %s '
                                  % str(interface_attachment))
                        migrate_fix_ip = interface_attachment.get('fixed_ips')[0] \
                                                             .get('ip_address')
                        migrate_port_id = interface_attachment.get('port_id')
                        undo_mgr.undo_with(functools.partial
                                           (self.compute_api.interface_detach,
                                            context,
                                            server_id,
                                            migrate_port_id))
                        gw_url = migrate_fix_ip + ':' + str(
                            CONF.v2vgateway_api_listen_port)
                        extra_properties = {}
                        extra_properties['gw_url'] = gw_url
                        extra_properties['is_deacidized'] = True
                        extra_properties['migrate_port_id'] = migrate_port_id
                        extra_properties['sys_clone'] = sys_clone
                        resource.extra_properties.update(extra_properties)
                        # waiting port attach finished, and can ping this vm
                        self._await_port_status(context, migrate_port_id,
                                                migrate_fix_ip)
                else:
                    interfaces = self.neutron_api.port_list(
                        context, device_id=server_id)
                    host_ip = None
                    for infa in interfaces:
                        if host_ip:
                            break
                        binding_profile = infa.get("binding:profile", [])
                        if binding_profile:
                            host_ip = binding_profile.get('host_ip')
                    if not host_ip:
                        LOG.error(_LE('Not find the clone data ip for server'))
                        raise exception.NoMigrateNetProvided(
                            server_uuid=resource.id
                        )
                    gw_url = host_ip + ':' + str(
                        CONF.v2vgateway_api_listen_port)
                    extra_properties = {}
                    extra_properties['gw_url'] = gw_url
                    extra_properties['sys_clone'] = sys_clone
                    resource.extra_properties.update(extra_properties)
                block_device_mapping = server_properties.get(
                    'block_device_mapping_v2')
                if block_device_mapping:
                    gw_urls = gw_url.split(':')
                    client = birdiegatewayclient.get_birdiegateway_client(
                        gw_urls[0], gw_urls[1])
                    for block_device in block_device_mapping:
                        device_name = block_device.get('device_name')
                        volume_name = block_device.get('volume_id').get(
                            'get_resource')
                        volume_resource = resource_map.get(volume_name)
                        boot_index = block_device.get('boot_index')
                        if boot_index == 0 or boot_index == '0':
                            volume_resource.extra_properties['sys_clone'] = \
                                sys_clone
                        src_dev_format = client.vservices.get_disk_format(device_name) \
                                                         .get('disk_format')
                        src_mount_point = client.vservices.get_disk_mount_point(device_name) \
                                                          .get('mount_point')
                        volume_resource.extra_properties['guest_format'] = \
                            src_dev_format
                        volume_resource.extra_properties['mount_point'] = \
                            src_mount_point
                        volume_resource.extra_properties['gw_url'] = gw_url
                        volume_resource.extra_properties['is_deacidized'] = \
                            True
                        sys_dev_name = client.vservices.get_disk_name(volume_resource.id) \
                                                       .get('dev_name')
                        if not sys_dev_name:
                            sys_dev_name = device_name
                        volume_resource.extra_properties['sys_dev_name'] = \
                            sys_dev_name

    def _handle_sv_for_svm(self, context, vol_res,
                           gw_id, gw_ip, undo_mgr):
        volume_id = vol_res.id
        LOG.debug('Set the volume %s shareable', volume_id)
        self.volume_api.set_volume_shareable(context, volume_id, True)
        undo_mgr.undo_with(functools.partial(self._set_volume_shareable,
                                             context,
                                             volume_id,
                                             False))

        client = birdiegatewayclient.get_birdiegateway_client(
            gw_ip,
            str(CONF.v2vgateway_api_listen_port)
        )
        disks = set(client.vservices.get_disk_name().get('dev_name'))

        attach_resp = self.compute_api.attach_volume(context,
                                                     gw_id,
                                                     volume_id,
                                                     None)
        LOG.debug('Attach the volume %s to gw host %s ', volume_id, gw_id)
        undo_mgr.undo_with(functools.partial(self._detach_volume,
                                             context,
                                             gw_id,
                                             volume_id))
        self._wait_for_volume_status(context, volume_id, gw_id,
                                     'in-use')
        n_disks = set(client.vservices.get_disk_name().get('dev_name'))

        diff_disk = n_disks - disks
        LOG.debug('Begin get info for volume,the vgw ip %s' % gw_ip)
        client = birdiegatewayclient.get_birdiegateway_client(
            gw_ip, str(CONF.v2vgateway_api_listen_port))
#         sys_dev_name = client.vservices.get_disk_name(volume_id).get(
#             'dev_name')
#         sys_dev_name = device_name
#        sys_dev_name = attach_resp._info.get('device')
        sys_dev_name = list(diff_disk)[0] if len(diff_disk) == 1 else None
        vol_res.extra_properties['sys_dev_name'] = sys_dev_name
        guest_format = client.vservices.get_disk_format(sys_dev_name)\
                             .get('disk_format')
        if guest_format:
            vol_res.extra_properties['guest_format'] = guest_format
            mount_point = client.vservices.force_mount_disk(
                sys_dev_name, "/opt/" + volume_id)
            vol_res.extra_properties['mount_point'] = mount_point.get(
                'mount_disk')

    def add_extra_properties_for_stack(self, context, resource, undo_mgr):
        res_prop = resource.properties
        stack_id = resource.id
        template = json.loads(res_prop.get('template'))

        def is_file_type(t):
            return t and t.startswith('file://')

        def _add_extra_prop(template, stack_id):
            temp_res = template.get('resources')
            for key, value in temp_res.items():
                res_type = value.get('type')
                if res_type == 'OS::Cinder::Volume':
                    v_prop = value.get('properties')
                    v_exra_prop = value.get('extra_properties', {})
                    if not v_exra_prop or not v_exra_prop.get('gw_url'):
                        heat_res = self.heat_api.get_resource(context,
                                                              stack_id,
                                                              key)
                        phy_id = heat_res.physical_resource_id
                        res_info = self.volume_api.get(context, phy_id)
                        az = res_info.get('availability_zone')
                        gw_id, gw_ip = utils.get_next_vgw(az)
                        if not gw_id or not gw_ip:
                            raise exception.V2vException(
                                message='no vgw host found')
                        gw_url = gw_ip + ':' + str(
                            CONF.v2vgateway_api_listen_port)
                        v_exra_prop.update({"gw_url": gw_url, "gw_id": gw_id})
                        v_exra_prop['is_deacidized'] = True
                        v_exra_prop['id'] = phy_id
                        volume_status = res_info['status']
                        v_exra_prop['status'] = volume_status
                        value['extra_properties'] = v_exra_prop
                        value['id'] = phy_id
                        self._handle_volume_for_stack(context, value, gw_id,
                                                      gw_ip, undo_mgr)
                elif res_type == 'OS::Nova::Server':
                    heat_res = self.heat_api.get_resource(context,
                                                          stack_id,
                                                          key)
                    phy_id = heat_res.physical_resource_id
                    server_info = self.compute_api.get_server(context, phy_id)
                    vm_state = server_info.get('OS-EXT-STS:vm_state', None)
                    v_exra_prop = value.get('extra_properties', {})
                    v_exra_prop['vm_state'] = vm_state
                    v_exra_prop['id'] = phy_id
                    value['extra_properties'] = v_exra_prop
                elif res_type and res_type.startswith('file://'):
                    son_template = value.get('content')
                    son_template = json.loads(son_template)
                    son_stack_id = value.get('id')
                    _add_extra_prop(son_template, son_stack_id)
                    value['content'] = json.dumps(son_template)

#                 else:
#                     r = value.get('properties',{}).get('resource')
#                     if not r or not is_file_type(r.get('type')):
#                         continue
#                     son_template = r.get('content')
#                     son_template = json.loads(son_template)
#                     son_stack_id = r.get('id')
#                     _add_extra_prop(son_template, son_stack_id)
#                     r['content'] = json.dumps(son_template)
        _add_extra_prop(template, stack_id)
        res_prop['template'] = json.dumps(template)

    def _handle_volume_for_stack(self, context, vol_res,
                                 gw_id, gw_ip, undo_mgr):
        volume_id = vol_res.get('id')
        volume_info = self.volume_api.get(context, volume_id)
        volume_status = volume_info['status']
        v_shareable = volume_info['shareable']
        if v_shareable == 'false' and volume_status == 'in-use':
            vol_res.get('extra_properties')['set_shareable'] = True
            self.volume_api.set_volume_shareable(context, volume_id, True)
            undo_mgr.undo_with(functools.partial(self._set_volume_shareable,
                                                 context,
                                                 volume_id,
                                                 False))
        LOG.debug('Attach volume %s to gw host %s', volume_id, gw_id)
        attach_resp = self.compute_api.attach_volume(context,
                                                     gw_id,
                                                     volume_id,
                                                     None)
        client = birdiegatewayclient.get_birdiegateway_client(
            gw_ip,
            str(CONF.v2vgateway_api_listen_port)
            )
        disks = set(client.vservices.get_disk_name().get('dev_name'))
        LOG.debug('The volume attachment info is %s '
                  % str(attach_resp))
        undo_mgr.undo_with(functools.partial(self._detach_volume,
                                             context,
                                             gw_id,
                                             volume_id))
        self._wait_for_volume_status(context, volume_id, gw_id,
                                     'in-use')
        n_disks = set(client.vservices.get_disk_name().get('dev_name'))
        diff_disk = n_disks - disks
        vol_res.get('extra_properties')['status'] = 'in-use'
        LOG.debug('Begin get info for volume,the vgw ip %s' % gw_ip)
        sys_dev_name = list(diff_disk)[0] if len(diff_disk) == 1 else None
#         device_name = attach_resp._info.get('device')
#         sys_dev_name = client.vservices.get_disk_name(volume_id).get(
#             'dev_name')
#        sys_dev_name = device_name
        vol_res.get('extra_properties')['sys_dev_name'] = sys_dev_name
        guest_format = client.vservices.get_disk_format(sys_dev_name)\
                             .get('disk_format')
        if guest_format:
            vol_res.get('extra_properties')['guest_format'] = guest_format
            mount_point = client.vservices.force_mount_disk(
                sys_dev_name, "/opt/" + volume_id)
            vol_res.get('extra_properties')['mount_point'] = mount_point.get(
                'mount_disk')