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
from oslo.config import cfg
from conveyor.common import log as logging

from conveyor.server import rpcapi

CONF = cfg.CONF
LOG = logging.getLogger(__name__)

class MigrationAPI(object):
    
    def __init__(self):
        
        self.migration_rpcapi = rpcapi.MigrationAPI();
        
        super(MigrationAPI, self).__init__()
        
    
    
    def get_all(self, context):
        
        LOG.debug("migration api start")
        host = CONF.host
        self.migration_rpcapi.get_all(context, host)
        
        
    
    