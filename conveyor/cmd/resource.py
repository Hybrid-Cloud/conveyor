#!/usr/bin/python
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
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

"""Starter script for Cinder Volume."""

import os

import eventlet

if os.name == 'nt':
    # eventlet monkey patching the os module causes subprocess.Popen to fail
    # on Windows when using pipes due to missing non-blocking IO support.
    eventlet.monkey_patch(os=False)
else:
    eventlet.monkey_patch()

import sys
import warnings

warnings.simplefilter('once', DeprecationWarning)

from oslo.config import cfg

# If ../cinder/__init__.py exists, add ../ to Python search path, so that
# it will override what happens to be installed in /usr/(local/)lib/python...
possible_topdir = os.path.normpath(os.path.join(os.path.abspath(sys.argv[0]),
                                   os.pardir,
                                   os.pardir))
if os.path.exists(os.path.join(possible_topdir, 'conveyor', '__init__.py')):
    sys.path.insert(0, possible_topdir)

from conveyor import i18n
i18n.enable_lazy()

# Need to register global_opts
from conveyor.common import config  # noqa
from conveyor.common import log as logging
from conveyor import service
from conveyor import utils
from conveyor import version


host_opt = cfg.StrOpt('host',
                      help='Backend override of host value.')
CONF = cfg.CONF


def main():
    CONF(sys.argv[1:], project='conveyor',
         version=version.version_string())
    logging.setup("conveyor")
    utils.monkey_patch()
    server = service.Service.create(binary='conveyor-resource')
    service.serve(server)
    service.wait()
