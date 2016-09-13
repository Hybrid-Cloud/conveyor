# Copyright 2011 OpenStack Foundation
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

import time
from webob import exc

from conveyor import context
from conveyor.common import log as logging
from conveyor.api.wsgi import wsgi
from conveyor.api import extensions

from conveyor.common import timeutils
from conveyor.clone import api

from conveyor.i18n import _

LOG = logging.getLogger(__name__)

  
class MigrateActionController(wsgi.Controller):
    
    def __init__(self, ext_mgr=None, *args, **kwargs):
        super(MigrateActionController, self).__init__(*args, **kwargs)
        self.clone_api = api.API()
        self.ext_mgr = ext_mgr
    
    
    
    @wsgi.response(202)
    @wsgi.action('export_migrate_template')
    def _export_migrate_template(self, req, id, body):  
        LOG.debug(" start exporting migrate template in API")
        context = req.environ['conveyor.context']
        self.clone_api.export_migrate_template(context, id)
        
        
    @wsgi.response(202)
    @wsgi.action('migrate')
    def _migrate(self, req, id, body):  
        LOG.debug(" start execute migrate plan in API,the plan id is %s" %id)
        if not self.is_valid_body(body, 'migrate'):
            LOG.debug("migrate request body has not key:migrate")
            raise exc.HTTPUnprocessableEntity() 
          
        migrate_body = body['migrate']
        destination = migrate_body.get('destination')
        context = req.environ['conveyor.context']
        self.clone_api.migrate(context, id, destination)
        
        


class Migrate(extensions.ExtensionDescriptor):
    """Enable admin actions."""

    name = "Migrate"
    alias = "conveyor-Migrate"
    namespace = "http://docs.openstack.org/conveyor/ext/migrate/api/v1"
    updated = "2016-01-29T00:00:00+00:00"
    
    
    #extend exist resource
    def get_controller_extensions(self):
        controller = MigrateActionController(self.ext_mgr)
        extension = extensions.ControllerExtension(self, 'migrates', controller)
        return [extension]    