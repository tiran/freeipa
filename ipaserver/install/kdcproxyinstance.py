# Authors: Christian Heimes <cheimes@redhat.com>
#
# Copyright (C) 2015  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from ipapython import ipautil
from ipaplatform import services
import service


class KDCProxyInstance(service.Service):
    suffix = ipautil.dn_attribute_property('_ldap_suffix')

    def __init__(self):
        service.SimpleServiceInstance.__init__(self, "kdcproxy")
        # KDC proxy runs inside Apache HTTPD
        self.service = None
        self.httpd_service_name = 'httpd'
        self.httpd_service = services.service(self.httpd_service_name)

    # HTTPD
    def restart_httpd(self, instance_name="", capture_output=True, wait=True):
        self.httpd_service.restart(instance_name,
                                   capture_output=capture_output,
                                   wait=wait)

    def is_httpd_running(self):
        return self.httpd_service.is_running()

    def is_httpd_enabled(self):
        return self.httpd_service.is_enabled()

    def is_configured(self):
        state = self.ldap_is_enabled(self.gensvc_name, self.fqdn,
                                     self.dm_password, self.suffix)
        return state is not None

    def disable(self):
        pass

    def create_instance(self, gensvc_name=None, fqdn=None, dm_password=None,
                        ldap_suffix=None, realm=None):
        self.gensvc_name = gensvc_name
        self.fqdn = fqdn
        self.dm_password = dm_password
        self.suffix = ldap_suffix
        self.realm = realm
        if not realm:
            self.ldapi = False

        self.step("configuring %s WSGI application" % self.service_name,
                  self.__enable_wsgi)
        self.step("check %s is started on boot" % self.httpd_service_name,
                  self.__check_httpd)
        self.step("(re)starting %s " % self.httpd_service_name,
                  self.__restart_httpd)
        self.start_creation("Configuring %s" % self.service_name)

    def __enable_wsgi(self):
        self.ldap_enable(self.gensvc_name, self.fqdn,
                         self.dm_password, self.suffix)

    def __check_httpd(self):
        if not self.is_httpd_enabled():
            self.print_msg("WARNING, %s is not enabled, but %s requires it" %
                           (self.httpd_service_name, self.service_name))

    def __restart_httpd(self):
        self.backup_state("running", self.is_httpd_running())
        self.restart_httpd()

    def uninstall(self):
        pass
