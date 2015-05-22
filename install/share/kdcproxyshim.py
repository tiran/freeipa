# Authors:
#   Christian Heimes <cheimes@redhat.com>
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
"""
WSGI appliction for KDC Proxy
"""
import errno
import os
import sys

from ipalib import api, errors
from ipalib.session import get_ipa_ccache_name
from ipapython.ipa_log_manager import standard_logging_setup
from ipalib.krb_utils import krb5_format_service_principal_name
from ipapython.ipaldap import IPAdmin
from ipapython.dn import DN
from ipapython import ipautil
from ipaplatform.paths import paths


DEBUG = True
TIME_LIMIT = 2
KDCPROXY_CONFIG = '/etc/ipa/kdcproxy.conf'


class CheckError(Exception):
    """An unrecoverable error has occured"""


class KDCProxyConfig(object):
    ipaconfig_flag = 'ipaKDCProxyEnabled'

    def __init__(self, time_limit=TIME_LIMIT):
        self.time_limit = time_limit
        self.con = None
        self.log = api.log

        self.ldap_uri = api.env.ldap_uri
        self.principal = krb5_format_service_principal_name(
            'HTTP', api.env.host, api.env.realm)
        self.keytab = paths.IPA_KEYTAB
        # XXX is this the correct ccache?
        self.ccache = get_ipa_ccache_name()

        self.kdcproxy_dn = DN(('cn', 'KDCPROXY'), ('cn', api.env.host),
                              ('cn', 'masters'), ('cn', 'ipa'), ('cn', 'etc'),
                              api.env.basedn)

    def _kinit(self):
        """Get a krb5 ticket with Apache's keytab"""
        self.log.debug('krb5 principal %s, keytab %s, ccache %s',
                       self.principal, self.keytab, self.ccache)
        try:
            if hasattr(ipautil, 'kinit_keytab'):
                # FreeIPA 4.2
                os.environ['KRB5CCNAME'] = self.ccache
                ipautil.kinit_keytab(self.principal, self.keytab, self.ccache)
            else:
                # FreeIPA 4.1
                ccache_dir = os.path.dirname(self.ccache.split(':', 1)[-1])
                ipautil.kinit_hostprincipal(  # pylint: disable=no-member
                    self.keytab, ccache_dir, str(self.principal))
        except Exception as e:
            msg = "kinit failed: %s" % e
            self.log.exception(msg)
            raise CheckError(msg)

    def _kdestroy(self):
        """Release krb5 ccache"""
        ccache = os.environ['KRB5CCNAME']
        del os.environ['KRB5CCNAME']
        self.log.debug('kdestroy %s', ccache)
        try:
            os.unlink(ccache)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise

    def _ldap_con(self):
        """Establish LDAP connection"""
        self.log.debug('ldap_uri: %s', self.ldap_uri)
        try:
            self.con = IPAdmin(ldap_uri=self.ldap_uri)
            self.con.do_sasl_gssapi_bind()
        except errors.NetworkError as e:
            msg = 'Failed to get setting from dirsrv: %s' % e
            self.log.exception(msg)
            raise CheckError(msg)
        except Exception as e:
            msg = ('Unknown error while retrieving setting from %s: %s' %
                   (self.ldap_uri, e))
            self.log.exception(msg)
            raise CheckError(msg)

    def _get_entry(self, dn, attrs):
        """Get an LDAP entry, handles NotFound"""
        try:
            return self.con.get_entry(dn,
                                      attrs,
                                      time_limit=self.time_limit)
        except errors.NotFound:
            self.log.debug('Entry not found: %s', dn)
            return None
        except Exception as e:
            msg = ('Unknown error while retrieving setting from %s: %s' %
                   (self.ldap_uri, e))
            self.log.exception(msg)
            raise CheckError(msg)

    def _find_entry(self, dn, attrs, filter, scope=IPAdmin.SCOPE_BASE):
        """Find an LDAP entry, handles NotFound and Limit"""
        try:
            entries, truncated = self.con.find_entries(
                filter, attrs, dn, scope, time_limit=self.time_limit)
            if truncated:
                raise errors.LimitsExceeded()
        except errors.NotFound:
            self.log.debug('Entry not found: %s', dn)
            return None
        except Exception as e:
            msg = ('Unknown error while retrieving setting from %s: %s' %
                   (self.ldap_uri, e))
            self.log.exception(msg)
            raise CheckError(msg)
        return entries[0]

    def host_enabled(self):
        """Check replica specific flag"""
        # needs ACI (targetfilter="(ipaConfigString=enabledService)")(targetattr="ipaConfigString")(version 3.0; acl "Compare enabledService access to masters"; allow(search, compare) userdn = "ldap:///all";)
        self.log.debug('Read settings from dn: %s', self.kdcproxy_dn)
        #srcfilter = self.con.make_filter({'ipaConfigString': u'enabledService'})
        #entry = self._find_entry(self.kdcproxy_dn, ['cn'], srcfilter)
        #self.log.debug('%s ipaConfigString: %s', self.kdcproxy_dn, entry)
        #return entry is not None
        entry = self._get_entry(self.kdcproxy_dn, ['ipaConfigString'])
        if entry is not None:
            values = entry['ipaConfigString']
        else:
            values = ()
        self.log.debug('%s ipaConfigString: %s', self.kdcproxy_dn, values)
        return any(value == 'enabledService' for value in values)

    def __enter__(self):
        self._kinit()
        self._ldap_con()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._kdestroy()
        if self.con is not None:
            self.con.unbind()
            self.con = None


def check_enabled(debug=DEBUG, time_limit=TIME_LIMIT):
    # initialize API without file logging
    if not api.isdone('bootstrap'):
        api.bootstrap(context='kdcproxyshim', log=None, debug=debug)
        standard_logging_setup(verbose=True, debug=debug)

    with KDCProxyConfig(time_limit) as cfg:
        if cfg.host_enabled():
            api.log.info('kdcproxy ENABLED')
            return True
        else:
            api.log.info('kdcproxy DISABLED')
            return False


ENABLED = check_enabled()

# override config location
if 'kdcproxy' in sys.modules:
    raise CheckError('kdcproxy already imported')
os.environ['KDCPROXY_CONFIG'] = KDCPROXY_CONFIG
import kdcproxy


def application(environ, start_response):
    if not ENABLED:
        code = b'404 Not Found'
        msg = b'KDC over HTTPS proxy service is not available.'
        headers = [
            ('Content-Type', 'text/plain; charset=utf-8'),
            ('Content-Length', str(len(msg))),
        ]
        start_response(code, headers)
        return [msg]
    else:
        return kdcproxy.application(environ, start_response)
