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
import os
from subprocess import check_call
import sys

from ipalib import api, errors
from ipapython.ipa_log_manager import standard_logging_setup
from ipapython.ipaldap import IPAdmin
from ipapython.dn import DN
from ipaplatform.paths import paths


DEBUG = True
TIME_LIMIT = 2


class CheckError(Exception):
    """An unrecoverable error has occured"""


class KDCProxyConfig(object):
    ipaconfig_flag = 'ipaKDCProxyEnabled'

    def __init__(self, time_limit=TIME_LIMIT):
        self.time_limit = time_limit
        self.con = None
        self.log = api.log
        self.ldap_uri = api.env.ldap_uri
        self.ccache = 'MEMORY:kdcproxy_%i' % os.getpid()
        self.kdc_dn = DN(('cn', 'KDC'), ('cn', api.env.host),
                         ('cn', 'masters'), ('cn', 'ipa'), ('cn', 'etc'),
                         api.env.basedn)

    def _kinit(self):
        """Setup env for a krb5 ticket with own keytab"""
        self.log.debug('Setup env for krb5 client keytab %s, ccache %s',
                       paths.KDCPROXY_KEYTAB, self.ccache)
        os.environ['KRB5CCNAME'] = self.ccache
        os.environ['KRB5_CLIENT_KTNAME'] = paths.KDCPROXY_KEYTAB

    def _kdestroy(self):
        """Release krb5 ccache"""
        self.log.debug('kdestroy %s', self.ccache)
        try:
            check_call([paths.KDESTROY, '-A', '-q', '-c', self.ccache])
        finally:
            del os.environ['KRB5CCNAME']
            del os.environ['KRB5_CLIENT_KTNAME']

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
        self.log.debug('Read settings from dn: %s', self.kdc_dn)
        srcfilter = self.con.make_filter(
            {'ipaConfigString': u'kdcProxyEnabled'}
        )
        entry = self._find_entry(self.kdc_dn, ['cn'], srcfilter)
        self.log.debug('%s ipaConfigString: %s', self.kdc_dn, entry)
        return entry is not None

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
os.environ['KDCPROXY_CONFIG'] = paths.KDCPROXY_CONFIG
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
