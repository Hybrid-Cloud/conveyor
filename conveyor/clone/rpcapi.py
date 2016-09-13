# Copyright 2012, Intel, Inc.
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

"""
Client side of the conveyor RPC API.
"""

from oslo.config import cfg
import oslo.messaging as messaging
from oslo.serialization import jsonutils
from conveyor.common import log as logging

from conveyor import rpc


CONF = cfg.CONF

LOG = logging.getLogger(__name__)

class CloneAPI(object):
    '''Client side of the volume rpc API.

    API version history:

        1.0 - Initial version.
    '''

    BASE_RPC_API_VERSION = '1.0'

    def __init__(self, topic=None):
        super(CloneAPI, self).__init__()
        target = messaging.Target(topic=CONF.birdie_topic,
                                  version=self.BASE_RPC_API_VERSION)
        self.client = rpc.get_client(target, '1.23', serializer=None)

    def start_template_clone(self, ctxt, template):
        
        LOG.debug("Clone template start in Clone rpcapi mode")
        cctxt = self.client.prepare(version='1.18')
        cctxt.cast(ctxt, 'start_template_clone', template=template)
        
    def export_clone_template(self, ctxt, id, clone_element):
        
        LOG.debug("start call rpc api export_clone_template")
        cctxt = self.client.prepare(version='1.18')
        cctxt.cast(ctxt, 'export_clone_template',id=id,clone_element=clone_element)

    
    def clone(self, ctxt, id, destination, update_resources):
        
        LOG.debug("start call rpc api clone")
        cctxt = self.client.prepare(version='1.18')
        cctxt.cast(ctxt, 'clone', id=id, destination=destination, update_resources=update_resources)
        
        
    def export_migrate_template(self, ctxt, id):
        
        LOG.debug("start call rpc api export_migrate_template")
        cctxt = self.client.prepare(version='1.18')
        cctxt.cast(ctxt, 'export_migrate_template', id=id)

    
    def migrate(self, ctxt, id, destination):
        
        LOG.debug("start call rpc api migrate")
        cctxt = self.client.prepare(version='1.18')
        cctxt.cast(ctxt, 'migrate', id=id, destination=destination)
        
    def download_template(self, ctxt, id):
        
        LOG.debug("start call rpc api download_template")
        cctxt = self.client.prepare(version='1.18')
        return cctxt.call(ctxt, 'download_template', id=id)
