# Copyright (c) 2013 OpenStack Foundation
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
from conveyor.common import log as logging

from conveyor.api.common import ViewBuilder

LOG = logging.getLogger(__name__)

class ViewBuilder(ViewBuilder):
    
    def list(self, results):
        objs = {
            "resources": [{
                "id": "1234",
                "name":"conveyor-test1234",
            },
            {
                "id": "1235",
                "name":"conveyor-test1235",
            }]
        }
         
        return objs
    
    def show(self,result):
        
        LOG.debug(', '.join(['%s:%s' % item for item in result.__dict__.items()]) )
        
        obj = {
            'resource':{
                'id': result.id,
                'name': result.name,
                'status': result.status
                }
            }
        
        return obj
    