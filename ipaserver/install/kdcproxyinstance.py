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
import os
import pwd

import ldap

from ipalib import errors
from ipalib.krb_utils import krb5_format_service_principal_name
from ipapython import ipautil
from ipapython import sysrestore
from ipapython.dn import DN
from ipapython.ipa_log_manager import root_logger
from ipaplatform.paths import paths
from ipaplatform import services

import installutils
import service


class KDCProxyInstance(service.SimpleServiceInstance):
    def __init__(self, fstore):
        service.SimpleServiceInstance.__init__(self, "kdcproxy")
        if fstore:
            self.fstore = fstore
        else:
            self.fstore = sysrestore.FileStore(paths.SYSRESTORE)
        # KDC proxy runs inside Apache HTTPD
        self.service = None
        self.httpd_service_name = 'httpd'
        self.httpd_service = services.service(self.httpd_service_name)

    # HTTPD
    def start_httpd(self, instance_name="", capture_output=True, wait=True):
        self.httpd_service.start(instance_name, capture_output=capture_output,
                                 wait=wait)

    def stop_httpd(self, instance_name="", capture_output=True):
        self.httpd_service.stop(instance_name, capture_output=capture_output)

    def restart_httpd(self, instance_name="", capture_output=True, wait=True):
        self.httpd_service.restart(instance_name,
                                   capture_output=capture_output,
                                   wait=wait)

    def is_httpd_running(self):
        return self.httpd_service.is_running()

    def is_httpd_enabled(self):
        return self.httpd_service.is_enabled()

    def is_configured(self):
        return (os.path.isfile(paths.HTTPD_IPA_KDC_PROXY_CONF)
                and os.path.isfile(paths.KDCPROXY_KEYTAB))

    def disable(self):
        pass

    def create_instance(self, gensvc_name=None, fqdn=None, dm_password=None,
                        ldap_suffix=None, realm=None):
        assert gensvc_name == 'KDCPROXY'
        self.gensvc_name = gensvc_name
        self.fqdn = fqdn
        self.dm_password = dm_password
        self.suffix = ldap_suffix
        self.realm = realm
        self.principal = krb5_format_service_principal_name(
            gensvc_name, self.fqdn, self.realm)
        if not realm:
            self.ldapi = False
        self.sub_dict = dict(
            REALM=realm,
            FQDN=fqdn,
        )
        if not self.admin_conn:
            self.ldap_connect()

        self.step("creating a keytab for %s" % self.service_name,
                  self.__create_kdcproxy_keytab)
        self.step("Enable %s in KDC" % self.service_name,
                  self.__enable_kdcproxy)
        self.step("configuring httpd", self.__configure_http)
        self.step("(re)starting %s " % self.httpd_service_name,
                  self.__restart_httpd)
        self.start_creation("Configuring %s" % self.service_name)

    def __create_kdcproxy_keytab(self):
        # create a principal for KDCPROXY
        installutils.kadmin_addprinc(self.principal)
        # create a keytab for the KDCPROXY principal
        self.fstore.backup_file(paths.KDCPROXY_KEYTAB)
        installutils.create_keytab(paths.KDCPROXY_KEYTAB, self.principal)
        # ... and secure it
        pent = pwd.getpwnam("kdcproxy")
        os.chown(paths.KDCPROXY_KEYTAB, pent.pw_uid, pent.pw_gid)
        os.chmod(paths.KDCPROXY_KEYTAB, 0400)

        # move the principal to cn=services,cn=accounts
        principal_dn = self.move_service(self.principal)
        if principal_dn is None:
            # already moved
            principal_dn = DN(('krbprincipalname', self.principal),
                              ('cn', 'services'), ('cn', 'accounts'),
                              self.suffix)

        # add a privilege to the KDCPROXY service principal, so it can read
        # the ipaConfigString=kdcProxyEnabled attribute
        privilege = DN(('cn', 'IPA Masters KDC Proxy Readers'),
                       ('cn', 'privileges'), ('cn', 'pbac'), self.suffix)

        mod = [(ldap.MOD_ADD, 'member', principal_dn)]
        try:
            self.admin_conn.modify_s(privilege, mod)
        except ldap.TYPE_OR_VALUE_EXISTS:
            pass
        except Exception as e:
            root_logger.critical("Could not modify principal's %s entry: %s",
                                 principal_dn, str(e))
            raise

    def __enable_kdcproxy(self):
        entry_name = DN(('cn', 'KDC'), ('cn', self.fqdn), ('cn', 'masters'),
                        ('cn', 'ipa'), ('cn', 'etc'), self.suffix)
        attr_name = 'kdcProxyEnabled'

        try:
            entry = self.admin_conn.get_entry(entry_name, ['ipaConfigString'])
        except errors.NotFound:
            pass
        else:
            if any(attr_name.lower() == val.lower()
                   for val in entry.get('ipaConfigString', [])):
                root_logger.debug("service KDCPROXY already enabled")
                return

            entry.setdefault('ipaConfigString', []).append(attr_name)
            try:
                self.admin_conn.update_entry(entry)
            except errors.EmptyModlist:
                root_logger.debug("service KDCPROXY already enabled")
                return
            except:
                root_logger.debug("failed to enable service KDCPROXY")
                raise

            root_logger.debug("service KDCPROXY enabled")
            return

        entry = self.admin_conn.make_entry(
            entry_name,
            objectclass=["nsContainer", "ipaConfigObject"],
            cn=['KDC'],
            ipaconfigstring=[attr_name]
        )

        try:
            self.admin_conn.add_entry(entry)
        except errors.DuplicateEntry:
            root_logger.debug("failed to add service KDCPROXY entry")
            raise

    def __configure_http(self):
        target_fname = paths.HTTPD_IPA_KDC_PROXY_CONF
        http_txt = ipautil.template_file(
            ipautil.SHARE_DIR + "ipa-kdc-proxy.conf", self.sub_dict)
        self.fstore.backup_file(target_fname)
        with open(target_fname, 'w') as f:
            f.write(http_txt)
        os.chmod(target_fname, 0644)

    def __restart_httpd(self):
        self.backup_state("running", self.is_httpd_running())
        self.restart_httpd()

    def uninstall(self):
        if self.is_configured():
            self.print_msg("Unconfiguring %s" % self.service_name)

        self.stop_httpd()

        running = self.restore_state("running")
        installutils.remove_file(paths.HTTPD_IPA_KDC_PROXY_CONF)

        if running:
            self.start_httpd()
