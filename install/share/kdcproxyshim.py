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
import sys

from ipaserver.install.installutils import is_ipa_configured
from ipalib import api, errors
from ipalib.session import get_ipa_ccache_name
from ipapython.ipa_log_manager import standard_logging_setup
from ipalib.krb_utils import krb5_format_service_principal_name
from ipapython.ipaldap import IPAdmin
from ipapython.dn import DN
from ipapython import ipautil
from ipaplatform import services
from ipaplatform.paths import paths


DEBUG = False
TIME_LIMIT = 2
KDCPROXY_CONFIG = '/etc/ipa/kdcproxy.conf'


class CheckError(Exception):
    pass


def check_enabled(debug=False, time_limit=2):
    # initialize API without file logging
    if not api.isdone('bootstrap'):
        api.bootstrap(context='kdcproxyshim', log=None, debug=debug)
        standard_logging_setup(verbose=True, debug=debug)

    dn = DN(('cn', 'ipaConfig'), ('cn', 'etc'), api.env.basedn)
    flag = 'ipaKDCProxyEnabled'
    principal = krb5_format_service_principal_name(
        'HTTP', api.env.host, api.env.realm)
    keytab = paths.IPA_KEYTAB
    ccache = get_ipa_ccache_name()

    api.log.debug('ldap_uri: %s', api.env.ldap_uri)
    api.log.debug('Read settings from %s dn: %s', flag, dn)
    api.log.debug('krb5 principal %s, keytab %s, ccache %s',
                  principal, keytab, ccache)

    if debug > 1:
        # costly checks
        if not is_ipa_configured():
            api.log.error('FreeIPA is not configured')
            return False
        dirsrv = services.knownservices.dirsrv
        if not dirsrv.is_running():
            api.log.error('dirsrv is not running')
            return False

    # Use IPA's keytab to acquire a Kerberos ticket for SASL GSSAPI bind
    try:
        if hasattr(ipautil, 'kinit_keytab'):
            # FreeIPA 4.2
            os.environ['KRB5CCNAME'] = ccache
            ipautil.kinit_keytab(principal, keytab, ccache)
        else:
            # FreeIPA 4.1
            ccache_dir = os.path.dirname(ccache.split(':', 1)[-1])
            ipautil.kinit_hostprincipal(keytab, ccache_dir, str(principal))
    except Exception as e:
        msg = "kinit failed: %s" % e
        api.log.exception(msg)
        raise CheckError(msg)

    # query LDAP for the switch
    try:
        con = IPAdmin(ldap_uri=api.env.ldap_uri)
        con.do_sasl_gssapi_bind()
        entry = con.get_entry(dn, [flag], time_limit=time_limit)
    except errors.NetworkError as e:
        msg = 'Failed to get setting from dirsrv: %s' % e
        api.log.exception(msg)
        raise CheckError(msg)
    except errors.NotFound:
        api.log.warn('%s not found, disable kdcproxy', dn)
        return False
    except Exception as e:
        msg = ('Unknown error while retrieving setting from %s: %s' %
               (api.env.ldap_uri, e))
        api.log.exception(msg)
        raise CheckError(msg)

    # finally get switch status
    value = entry.single_value.get(flag)
    if value is None:
        api.log.warn('disable kdcproxy (%s not found in %s)', flag, dn)
        return False
    elif value == 'TRUE':
        api.log.info('enable kdcproxy (%s==%s in %s)', flag, value, dn)
        return True
    else:
        api.log.info('disable kdcproxy (%s==%s in %s)', flag, value, dn)
        return False


ENABLED = check_enabled(DEBUG, TIME_LIMIT)

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
