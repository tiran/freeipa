# Authors: Rob Crittenden <rcritten@redhat.com>
#
# Copyright (C) 2011  Red Hat
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

from __future__ import print_function

import logging
import sys
import os
import shutil
import tempfile

from ipalib.install.kinit import kinit_keytab
from ipapython import ipautil

from ipaclient.install import ipa_certupdate
from ipaserver.install import installutils
from ipaserver.install.installutils import create_replica_config
from ipaserver.install.installutils import check_creds, ReplicaConfig
from ipaserver.install import dsinstance, ca
from ipaserver.install import cainstance, service
from ipaserver.install import custodiainstance
from ipapython import version
from ipalib import api
from ipalib.constants import DOMAIN_LEVEL_0
from ipapython.config import IPAOptionParser
from ipapython.ipa_log_manager import standard_logging_setup
from ipaplatform.paths import paths

logger = logging.getLogger(os.path.basename(__file__))

log_file_name = paths.IPAREPLICA_CA_INSTALL_LOG
REPLICA_INFO_TOP_DIR = None

def parse_options():
    usage = "%prog [options] [REPLICA_FILE]"
    parser = IPAOptionParser(usage=usage, version=version.VERSION)
    parser.add_option("-d", "--debug", dest="debug", action="store_true",
                      default=False, help="gather extra debugging information")
    parser.add_option("-p", "--password", dest="password", sensitive=True,
                      help="Directory Manager (existing master) password")
    parser.add_option("-w", "--admin-password", dest="admin_password", sensitive=True,
                      help="Admin user Kerberos password used for connection check")
    parser.add_option("--no-host-dns", dest="no_host_dns", action="store_true",
                      default=False,
                      help="Do not use DNS for hostname lookup during installation")
    parser.add_option("--skip-conncheck", dest="skip_conncheck", action="store_true",
                      default=False, help="skip connection check to remote master")
    parser.add_option("--skip-schema-check", dest="skip_schema_check", action="store_true",
                      default=False, help="skip check for updated CA DS schema on the remote master")
    parser.add_option("-U", "--unattended", dest="unattended", action="store_true",
                      default=False, help="unattended installation never prompts the user")
    parser.add_option("--external-ca", dest="external_ca", action="store_true",
                      default=False, help="Generate a CSR to be signed by an external CA")
    ext_cas = tuple(x.value for x in cainstance.ExternalCAType)
    parser.add_option("--external-ca-type", dest="external_ca_type",
                      type="choice", choices=ext_cas,
                      metavar="{{{0}}}".format(",".join(ext_cas)),
                      help="Type of the external CA. Default: generic")
    parser.add_option("--external-ca-profile", dest="external_ca_profile",
                      type='constructor', constructor=cainstance.ExternalCAProfile,
                      default=None, metavar="PROFILE-SPEC",
                      help="Specify the certificate profile/template to use "
                           "at the external CA")
    parser.add_option("--external-cert-file", dest="external_cert_files",
                      action="append", metavar="FILE",
                      help="File containing the IPA CA certificate and the external CA certificate chain")
    ca_algos = ('SHA1withRSA', 'SHA256withRSA', 'SHA512withRSA')
    parser.add_option("--ca-signing-algorithm", dest="ca_signing_algorithm",
                      type="choice", choices=ca_algos,
                      metavar="{{{0}}}".format(",".join(ca_algos)),
                      help="Signing algorithm of the IPA CA certificate")
    parser.add_option("-P", "--principal", dest="principal", sensitive=True,
                      default=None, help="User allowed to manage replicas")
    parser.add_option("--subject-base", dest="subject_base",
                      default=None,
                      help=(
                          "The certificate subject base "
                          "(default O=<realm-name>).  "
                          "RDNs are in LDAP order (most specific RDN first)."))
    parser.add_option("--ca-subject", dest="ca_subject",
                      default=None,
                      help=(
                          "The CA certificate subject DN "
                          "(default CN=Certificate Authority,O=<realm-name>). "
                          "RDNs are in LDAP order (most specific RDN first)."))

    options, args = parser.parse_args()
    safe_options = parser.get_safe_opts(options)

    if args:
        filename = args[0]

        if len(args) != 1:
            parser.error("you must provide a file generated by "
                         "ipa-replica-prepare")

        options.external_ca = None
        options.external_cert_files = None
    else:
        filename = None

        if options.external_ca:
            if options.external_cert_files:
                parser.error("You cannot specify --external-cert-file "
                             "together with --external-ca")

        if options.external_ca_type and not options.external_ca:
            parser.error(
                "You cannot specify --external-ca-type without --external-ca")

        if options.external_ca_profile and not options.external_ca:
            parser.error(
                "You cannot specify --external-ca-profile "
                "without --external-ca")

    return safe_options, options, filename


def _get_dirman_password(password=None, unattended=False):
    # sys.exit() is used on purpose, because otherwise user is advised to
    # uninstall the component, even though it is not needed
    if not password:
        if unattended:
            sys.exit('Directory Manager password required')
        password = installutils.read_password(
            "Directory Manager (existing master)", confirm=False,
            validate=False)
    try:
        installutils.validate_dm_password_ldap(password)
    except ValueError:
        sys.exit("Directory Manager password is invalid")

    return password


def install_replica(safe_options, options, filename):
    if options.ca_subject:
        sys.exit("--ca-subject cannot be used when installing a CA replica")
    if options.subject_base:
        sys.exit("--subject-base cannot be used when installing a CA replica")

    if options.promote:
        if filename is not None:
            sys.exit("Too many parameters provided. "
                     "No replica file is required")
    else:
        if filename is None:
            sys.exit("A replica file is required")
        if not os.path.isfile(filename):
            sys.exit("Replica file %s does not exist" % filename)

    if not options.promote:
        # Check if we have admin creds already, otherwise acquire them
        check_creds(options, api.env.realm)

    # get the directory manager password
    dirman_password = _get_dirman_password(
        options.password, options.unattended)

    if (not options.promote and not options.admin_password and
            not options.skip_conncheck and options.unattended):
        sys.exit('admin password required')

    # Run ipa-certupdate to ensure we have the CA cert.  This is
    # necessary if the admin has just promoted the topology from
    # CA-less to CA-ful, and ipa-certupdate has not been run yet.
    ipa_certupdate.run_with_args(api)

    # CertUpdate restarts DS causing broken pipe on the original
    # connection, so reconnect the backend.
    api.Backend.ldap2.disconnect()
    api.Backend.ldap2.connect()

    if options.promote:
        config = ReplicaConfig()
        config.ca_host_name = None
        config.realm_name = api.env.realm
        config.host_name = api.env.host
        config.domain_name = api.env.domain
        config.dirman_password = dirman_password
        config.ca_ds_port = 389
        config.top_dir = tempfile.mkdtemp("ipa")
        config.dir = config.top_dir
        cafile = paths.IPA_CA_CRT
    else:
        config = create_replica_config(dirman_password, filename, options)
        config.ca_host_name = config.master_host_name
        cafile = config.dir + '/ca.crt'

    global REPLICA_INFO_TOP_DIR
    REPLICA_INFO_TOP_DIR = config.top_dir
    config.setup_ca = True

    if config.subject_base is None:
        attrs = api.Backend.ldap2.get_ipa_config()
        config.subject_base = attrs.get('ipacertificatesubjectbase')[0]

    if config.ca_host_name is None:
        config.ca_host_name = \
            service.find_providing_server('CA', api.Backend.ldap2, api.env.ca_host)

    options.realm_name = config.realm_name
    options.domain_name = config.domain_name
    options.dm_password = config.dirman_password
    options.host_name = config.host_name
    options.ca_host_name = config.ca_host_name
    if os.path.exists(cafile):
        options.ca_cert_file = cafile
    else:
        options.ca_cert_file = None

    ca.install_check(True, config, options)

    custodia = custodiainstance.get_custodia_instance(
        options, custodiainstance.CustodiaModes.CA_PEER)
    ca.install(True, config, options, custodia=custodia)


def install_master(safe_options, options):
    dm_password = _get_dirman_password(
        options.password, options.unattended)

    options.realm_name = api.env.realm
    options.domain_name = api.env.domain
    options.dm_password = dm_password
    options.host_name = api.env.host

    if not options.subject_base:
        options.subject_base = str(
            installutils.default_subject_base(api.env.realm))
    if not options.ca_subject:
        options.ca_subject = str(
            installutils.default_ca_subject_dn(options.subject_base))

    try:
        ca.subject_validator(ca.VALID_SUBJECT_BASE_ATTRS, options.subject_base)
    except ValueError as e:
        sys.exit("Subject base: {}".format(e))
    try:
        ca.subject_validator(ca.VALID_SUBJECT_ATTRS, options.ca_subject)
    except ValueError as e:
        sys.exit("CA subject: {}".format(e))

    ca.install_check(True, None, options)

    ca.print_ca_configuration(options)
    print()

    if not options.unattended:
        if not ipautil.user_input(
                "Continue to configure the CA with these values?", False):
            sys.exit("Installation aborted")

    # No CA peer available yet.
    custodia = custodiainstance.get_custodia_instance(
        options, custodiainstance.CustodiaModes.STANDALONE)
    ca.install(True, None, options, custodia=custodia)

    # Run ipa-certupdate to add the new CA certificate to
    # certificate databases on this server.
    logger.info("Updating certificate databases.")
    ipa_certupdate.run_with_args(api)


def install(safe_options, options, filename):
    options.promote = False

    try:
        if filename is None:
            install_master(safe_options, options)
        else:
            install_replica(safe_options, options, filename)

    finally:
        # Clean up if we created custom credentials
        created_ccache_file = getattr(options, 'created_ccache_file', None)
        if created_ccache_file is not None:
            try:
                os.unlink(created_ccache_file)
            except OSError:
                pass


def promote(safe_options, options, filename):
    options.promote = True

    with ipautil.private_ccache():
        ccache = os.environ['KRB5CCNAME']

        kinit_keytab(
            'host/{env.host}@{env.realm}'.format(env=api.env),
            paths.KRB5_KEYTAB,
            ccache)

        ca_host = service.find_providing_server('CA', api.Backend.ldap2)
        if ca_host is None:
            install_master(safe_options, options)
        else:
            install_replica(safe_options, options, filename)


def _main():
    safe_options, options, filename = parse_options()

    if os.geteuid() != 0:
        sys.exit("\nYou must be root to run this script.\n")

    if not dsinstance.DsInstance().is_configured():
        sys.exit("IPA server is not configured on this system.\n")

    if (not options.external_cert_files and
            cainstance.is_ca_installed_locally()):
        sys.exit("CA is already installed on this host.")

    standard_logging_setup(log_file_name, debug=options.debug)
    logger.debug("%s was invoked with options: %s,%s",
                 sys.argv[0], safe_options, filename)
    logger.debug("IPA version %s", version.VENDOR_VERSION)

    # override ra_plugin setting read from default.conf so that we have
    # functional dogtag backend plugins during CA install
    api.bootstrap(
        context='install', confdir=paths.ETC_IPA,
        in_server=True, ra_plugin='dogtag'
    )
    api.finalize()
    api.Backend.ldap2.connect()
    domain_level = dsinstance.get_domain_level(api)

    if domain_level > DOMAIN_LEVEL_0:
        promote(safe_options, options, filename)
    else:
        install(safe_options, options, filename)

    # pki-spawn restarts 389-DS, reconnect
    api.Backend.ldap2.close()
    api.Backend.ldap2.connect()

    # Enable configured services and update DNS SRV records
    service.enable_services(api.env.host)
    api.Command.dns_update_system_records()
    api.Backend.ldap2.disconnect()

    # execute ipactl to refresh services status
    ipautil.run([paths.IPACTL, 'start', '--ignore-service-failures'],
                raiseonerr=False)


fail_message = '''
Your system may be partly configured.
Run /usr/sbin/ipa-server-install --uninstall to clean up.
'''


def main():
    try:
        installutils.run_script(_main, log_file_name=log_file_name,
                                operation_name='ipa-ca-install',
                                fail_message=fail_message)
    finally:
        # always try to remove decrypted replica file
        try:
            if REPLICA_INFO_TOP_DIR:
                shutil.rmtree(REPLICA_INFO_TOP_DIR)
        except OSError:
            pass


if __name__ == '__main__':
    main()
