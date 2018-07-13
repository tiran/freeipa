#!/usr/bin/python3 -E
# Authors: Karl MacMillan <kmacmillan@mentalrootkit.com>
#
# Copyright (C) 2007  Red Hat
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

import re
import ldap
import socket
import traceback

# pylint: disable=import-error
from six.moves.urllib.parse import urlparse
from six.moves.xmlrpc_client import MAXINT
# pylint: enable=import-error

from ipaclient.install import ipadiscovery
from ipapython import ipautil
from ipaserver.install import replication, dsinstance, installutils
from ipaserver.install import bindinstance, cainstance
from ipaserver.install import opendnssecinstance, dnskeysyncinstance
from ipapython import version, ipaldap
from ipalib import api, errors
from ipalib.util import has_managed_topology, verify_host_resolvable
from ipapython.ipa_log_manager import standard_logging_setup
from ipapython.dn import DN
from ipapython.config import IPAOptionParser
from ipaplatform.paths import paths

logger = logging.getLogger(os.path.basename(__file__))

# dict of command name and tuples of min/max num of args needed
commands = {
    "list":(0, 1, "[master fqdn]", ""),
    "list-ruv":(0, 0, "", ""),
    "connect":(1, 2, "<master fqdn> [other master fqdn]",
                    "must provide the name of the servers to connect"),
    "disconnect":(1, 2, "<master fqdn> [other master fqdn]",
                    "must provide the name of the server to disconnect"),
    "del":(1, 1, "<master fqdn>",
                    "must provide hostname of master to delete"),
    "re-initialize":(0, 0, "", ""),
    "force-sync":(0, 0, "", ""),
    "clean-ruv":(1, 1, "Replica ID of to clean", "must provide replica ID to clean"),
    "abort-clean-ruv":(1, 1, "Replica ID to abort cleaning", "must provide replica ID to abort cleaning"),
    "list-clean-ruv":(0, 0, "", ""),
    "clean-dangling-ruv":(0, 0, "", ""),
    "dnarange-show":(0, 1, "[master fqdn]", ""),
    "dnanextrange-show":(0, 1, "", ""),
    "dnarange-set":(2, 2, "<master fqdn> <range>", "must provide a master and ID range"),
    "dnanextrange-set":(2, 2, "<master fqdn> <range>", "must provide a master and ID range"),
}

# tuple of commands that work with ca tree and need Directory Manager password
dirman_passwd_req_commands = ("list-ruv", "clean-ruv", "abort-clean-ruv",
                              "clean-dangling-ruv")


class NoRUVsFound(Exception):
    pass


def parse_options():
    parser = IPAOptionParser(version=version.VERSION)
    parser.add_option("-H", "--host", dest="host", help="starting host")
    parser.add_option("-p", "--password", dest="dirman_passwd", help="Directory Manager password")
    parser.add_option("-v", "--verbose", dest="verbose", action="store_true", default=False,
                      help="provide additional information")
    parser.add_option("-d", "--debug", dest="debug", action="store_true", default=False,
                      help="provide additional debug information")
    parser.add_option("-f", "--force", dest="force", action="store_true", default=False,
                      help="ignore some types of errors")
    parser.add_option("-c", "--cleanup", dest="cleanup", action="store_true", default=False,
                      help="DANGER: clean up references to a ghost master")
    parser.add_option("--binddn", dest="binddn", default=None, type="dn",
                      help="Bind DN to use with remote server")
    parser.add_option("--bindpw", dest="bindpw", default=None,
                      help="Password for Bind DN to use with remote server")
    parser.add_option("--winsync", dest="winsync", action="store_true", default=False,
                      help="This is a Windows Sync Agreement")
    parser.add_option("--cacert", dest="cacert", default=None,
                      help="Full path and filename of CA certificate to use with TLS/SSL to the remote server")
    parser.add_option("--win-subtree", dest="win_subtree", default=None,
                      help="DN of Windows subtree containing the users you want to sync (default cn=Users,<domain suffix)")
    parser.add_option("--passsync", dest="passsync", default=None,
                      help="Password for the IPA system user used by the Windows PassSync plugin to synchronize passwords")
    parser.add_option("--from", dest="fromhost", help="Host to get data from")
    parser.add_option("--no-lookup", dest="nolookup", action="store_true", default=False,
                      help="do not perform DNS lookup checks")

    options, args = parser.parse_args()

    valid_syntax = False

    if len(args):
        n = len(args) - 1
        k = commands.keys()
        for cmd in k:
            if cmd == args[0]:
                v = commands[cmd]
                err = None
                if n < v[0]:
                    err = v[3]
                elif n > v[1]:
                    err = "too many arguments"
                else:
                    valid_syntax = True
                if err:
                    parser.error("Invalid syntax: %s\nUsage: %s [options] %s" % (err, cmd, v[2]))

    if not valid_syntax:
        cmdstr = " | ".join(commands.keys())
        parser.error("must provide a command [%s]" % cmdstr)

    return options, args

def test_connection(realm, host, nolookup=False):
    """
    Make a GSSAPI connection to the remote LDAP server to test out credentials.

    This is used so we can fall back to promping for the DM password.

    returns True if connection successful, False otherwise
    """
    try:
        if not nolookup:
            enforce_host_existence(host)
        replman = replication.ReplicationManager(realm, host, None)
        replman.find_replication_agreements()
        del replman
        return True
    except errors.ACIError:
        return False
    except errors.NotFound:
        # We do a search in cn=config. NotFound in this case means no
        # permission
        return False
    except ldap.LOCAL_ERROR:
        # more than likely a GSSAPI error
        return False

def list_replicas(realm, host, replica, dirman_passwd, verbose, nolookup=False):

    if not nolookup:
        enforce_host_existence(host)
        if replica is not None:
            enforce_host_existence(replica)

    is_replica = False
    winsync_peer = None
    peers = {}

    try:
        ldap_uri = ipaldap.get_ldap_uri(host, 636, cacert=paths.IPA_CA_CRT)
        conn = ipaldap.LDAPClient(ldap_uri, cacert=paths.IPA_CA_CRT)
        if dirman_passwd:
            conn.simple_bind(bind_dn=ipaldap.DIRMAN_DN,
                             bind_password=dirman_passwd)
        else:
            conn.gssapi_bind()
    except Exception as e:
        print("Failed to connect to host '%s': %s" % (host, str(e)))
        return

    dn = DN(('cn', 'masters'), ('cn', 'ipa'), ('cn', 'etc'), ipautil.realm_to_suffix(realm))
    try:
        entries = conn.get_entries(dn, conn.SCOPE_ONELEVEL)
    except Exception as e:
        print("Failed to read master data from '%s': %s" % (host, str(e)))
        return
    else:
        for ent in entries:
            peers[ent.single_value['cn']] = ['master', '']

    dn = DN(('cn', 'replicas'), ('cn', 'ipa'), ('cn', 'etc'), ipautil.realm_to_suffix(realm))
    try:
        entries = conn.get_entries(dn, conn.SCOPE_ONELEVEL)
    except Exception:
        pass
    else:
        for ent in entries:
            config_string = ent.single_value['ipaConfigString']
            peers[ent.single_value['cn']] = config_string.split(':')

    if not replica:
        for k, p in peers.items():
            print('%s: %s' % (k, p[0]))
        return

    # ok we are being ask for info about a specific replica
    for k, p in peers.items():
        if replica == k:
            is_replica = True
            if p[0] == 'winsync':
                winsync_peer = p[1]

    if not is_replica:
        print("Cannot find %s in public server list" % replica)
        return

    try:
        if winsync_peer:
            repl = replication.ReplicationManager(realm, winsync_peer,
                                                  dirman_passwd)
            _cn, dn = repl.agreement_dn(replica)
            entries = repl.conn.get_entries(
                dn, conn.SCOPE_BASE,
                "(objectclass=nsDSWindowsReplicationAgreement)")
            ent_type = 'winsync'
        else:
            repl = replication.ReplicationManager(realm, replica,
                                                  dirman_passwd)
            entries = repl.find_replication_agreements()
            ent_type = 'replica'
    except Exception as e:
        print("Failed to get data from '%s': %s" % (replica, e))
        return

    for entry in entries:
        print('%s: %s' % (entry.single_value.get('nsds5replicahost'), ent_type))

        if verbose:
            print("  last init status: %s" % entry.single_value.get(
                'nsds5replicalastinitstatus'))
            print("  last init ended: %s" % str(ipautil.parse_generalized_time(
                entry.single_value['nsds5replicalastinitend'])))
            print("  last update status: %s" % entry.single_value.get(
                'nsds5replicalastupdatestatus'))
            print("  last update ended: %s" % str(
                ipautil.parse_generalized_time(
                    entry.single_value['nsds5replicalastupdateend'])))

def del_link(realm, replica1, replica2, dirman_passwd, force=False):
    """
    Delete a replication agreement from host A to host B.

    @realm: the Kerberos realm
    @replica1: the hostname of master A
    @replica2: the hostname of master B
    @dirman_passwd: the Directory Manager password
    @force: force deletion even if one server is down
    """

    repl2 = None
    what = "Removal of IPA replication agreement"
    managed_topology = has_managed_topology(api)

    try:
        repl1 = replication.ReplicationManager(realm, replica1, dirman_passwd)
        type1 = repl1.get_agreement_type(replica2)
    except errors.NotFound:
        # it's possible that the agreement could not have been found because of
        # the new topology plugin naming convention: <A>-to-<B> instead of
        # meTo<B>.
        if managed_topology:
            print("'%s' has no winsync replication agreement for '%s'" % (replica1, replica2))
            exit_on_managed_topology(what)
        else:
            print("'%s' has no replication agreement for '%s'" % (replica1, replica2))
        return False
    except Exception as e:
        print("Failed to determine agreement type for '%s': %s" % (replica2, e))

    if type1 == replication.IPA_REPLICA and managed_topology:
        exit_on_managed_topology(what)

    repl_list = repl1.find_ipa_replication_agreements()
    if not force and len(repl_list) <= 1 and type1 == replication.IPA_REPLICA:
        print("Cannot remove the last replication link of '%s'" % replica1)
        print("Please use the 'del' command to remove it from the domain")
        return False

    if type1 == replication.IPA_REPLICA:
        try:
            repl2 = replication.ReplicationManager(realm, replica2, dirman_passwd)

            repl_list = repl2.find_ipa_replication_agreements()
            if not force and len(repl_list) <= 1:
                print("Cannot remove the last replication link of '%s'" % replica2)
                print("Please use the 'del' command to remove it from the domain")
                return False

        except errors.NotFound:
            print("'%s' has no replication agreement for '%s'" % (replica2, replica1))
            if not force:
                return False
        except Exception as e:
            print("Failed to get list of agreements from '%s': %s" % (replica2, e))
            if not force:
                return False

    if repl2 and type1 == replication.IPA_REPLICA:
        failed = False
        try:
            repl2.set_readonly(readonly=True)
            repl2.force_sync(repl2.conn, replica1)
            _cn, dn = repl2.agreement_dn(repl1.conn.host)
            repl2.wait_for_repl_update(repl2.conn, dn, 30)
            (range_start, range_max) = repl2.get_DNA_range(repl2.conn.host)
            (next_start, next_max) = repl2.get_DNA_next_range(repl2.conn.host)
            if range_start is not None:
                if not store_DNA_range(repl1, range_start, range_max, repl2.conn.host, realm, dirman_passwd):
                    print("Unable to save DNA range %d-%d" % (range_start, range_max))
            if next_start is not None:
                if not store_DNA_range(repl1, next_start, next_max, repl2.conn.host, realm, dirman_passwd):
                    print("Unable to save DNA range %d-%d" % (next_start, next_max))
            repl2.set_readonly(readonly=False)
            repl2.delete_agreement(replica1)
            repl2.delete_referral(replica1)
            repl2.set_readonly(readonly=False)
        except Exception as e:
            print("Unable to remove agreement on %s: %s" % (replica2, e))
            failed = True

        if failed:
            if force:
                print("Forcing removal on '%s'" % replica1)
                print("Any DNA range on '%s' will be lost" % replica2)
            else:
                return False

    if not repl2 and force:
        print("Forcing removal on '%s'" % replica1)
        print("Any DNA range on '%s' will be lost" % replica2)

    repl1.delete_agreement(replica2)
    repl1.delete_referral(replica2)

    if type1 == replication.WINSYNC:
        try:
            dn = DN(('cn', replica2), ('cn', 'replicas'), ('cn', 'ipa'), ('cn', 'etc'),
                    ipautil.realm_to_suffix(realm))
            entries = repl1.conn.get_entries(dn, repl1.conn.SCOPE_SUBTREE)
            if entries:
                entries.sort(key=lambda x: len(x.dn), reverse=True)
                for entry in entries:
                    repl1.conn.delete_entry(entry)
        except Exception as e:
            print("Error deleting winsync replica shared info: %s" % e)

    print("Deleted replication agreement from '%s' to '%s'" % (replica1, replica2))

    return True

def get_ruv(realm, host, dirman_passwd, nolookup=False, ca=False):
    """
    Return the RUV entries as a list of tuples: (hostname, rid)
    """

    if not nolookup:
        enforce_host_existence(host)

    try:
        if ca:
            thisrepl = replication.get_cs_replication_manager(realm, host, dirman_passwd)
        else:
            thisrepl = replication.ReplicationManager(realm, host, dirman_passwd)
    except Exception as e:
        logger.debug("%s", traceback.format_exc())
        raise RuntimeError("Failed to connect to server {host}: {err}"
                           .format(host=host, err=e))

    search_filter = '(&(nsuniqueid=ffffffff-ffffffff-ffffffff-ffffffff)(objectclass=nstombstone))'
    try:
        entries = thisrepl.conn.get_entries(
            thisrepl.db_suffix, thisrepl.conn.SCOPE_SUBTREE, search_filter,
            ['nsds50ruv'])
    except errors.NotFound:
        logger.debug("%s", traceback.format_exc())
        raise NoRUVsFound("No RUV records found.")

    servers = []
    for e in entries:
        for ruv in e['nsds50ruv']:
            if ruv.startswith('{replicageneration'):
                continue
            data = re.match('\{replica (\d+) (ldap://.*:\d+)\}(\s+\w+\s+\w*){0,1}', ruv)
            if data:
                rid = data.group(1)
                (
                    _scheme, netloc, _path, _params, _query, _fragment
                ) = urlparse(data.group(2))
                servers.append((netloc, rid))
            else:
                print("unable to decode: %s" % ruv)

    return servers


def get_ruv_both_suffixes(realm, host, dirman_passwd, verbose, nolookup=False):
    """
    Get RUVs for both domain and ipaca suffixes
    """
    ruvs = {}
    fail_gracefully = True

    try:
        ruvs['ca'] = get_ruv(realm, host, dirman_passwd, nolookup, True)
    except (NoRUVsFound, RuntimeError) as e:
        err = "Failed to get CS-RUVs from {host}: {err}".format(host=host,
                                                                err=e)
        if isinstance(e, RuntimeError):
            fail_gracefully = False
            if verbose:
                print(err)
        logger.debug('%s', err)
    try:
        ruvs['domain'] = get_ruv(realm, host, dirman_passwd, nolookup)
    except (NoRUVsFound, RuntimeError) as e:
        err = "Failed to get RUVs from {host}: {err}".format(host=host, err=e)
        if isinstance(e, RuntimeError):
            if not fail_gracefully:
                raise
            if verbose:
                print(err)
        logger.debug('%s', err)

    if not ruvs.keys():
        raise NoRUVsFound("No RUV records found.")

    return ruvs


def list_ruv(realm, host, dirman_passwd, verbose, nolookup=False):
    """
    List the Replica Update Vectors on this host to get the available
    replica IDs.
    """
    try:
        servers = get_ruv_both_suffixes(realm, host, dirman_passwd,
                                        verbose, nolookup)
    except (NoRUVsFound, RuntimeError) as e:
        print(e)
        sys.exit(0 if isinstance(e, NoRUVsFound) else 1)

    print('Replica Update Vectors:')
    if servers.get('domain'):
        for netloc, rid in servers['domain']:
            print("\t{name}: {id}".format(name=netloc, id=rid))
    else:
        print('\tNo RUVs found.')

    print('Certificate Server Replica Update Vectors:')
    if servers.get('ca'):
        for netloc, rid in servers['ca']:
            print("\t{name}: {id}".format(name=netloc, id=rid))
    else:
        print('\tNo CS-RUVs found.')


def get_rid_by_host(realm, sourcehost, host, dirman_passwd, nolookup=False):
    """
    Try to determine the RID by host name.
    """
    try:
        servers = get_ruv(realm, sourcehost, dirman_passwd, nolookup)
    except RuntimeError as e:
        print(e)
        sys.exit(1)
    except NoRUVsFound as e:
        print(e)
        servers = []
    for (netloc, rid) in servers:
        if '%s:389' % host == netloc:
            return int(rid)


def clean_ruv(realm, ruv, options):
    """
    Given an RID create a CLEANALLRUV task to clean it up.
    """
    try:
        ruv = int(ruv)
    except ValueError:
        sys.exit("Replica ID must be an integer: %s" % ruv)

    try:
        servers = get_ruv_both_suffixes(realm, options.host,
                                        options.dirman_passwd,
                                        options.verbose,
                                        options.nolookup)
    except (NoRUVsFound, RuntimeError) as e:
        print(e)
        sys.exit(0 if isinstance(e, NoRUVsFound) else 1)

    tree_found = None
    for tree, ruvs in servers.items():
        for netloc, rid in ruvs:
            if ruv == int(rid):
                tree_found = tree
                hostname = netloc
                break
        if tree_found:
            break

    if not tree_found:
        sys.exit("Replica ID %s not found" % ruv)

    if tree_found == 'ca':
        print("Clean the Certificate Server Replication Update Vector for %s"
              % hostname)
    else:
        print("Clean the Replication Update Vector for %s" % hostname)

    if not options.force:
        print()
        print("Cleaning the wrong replica ID will cause that server to no")
        print("longer replicate so it may miss updates while the process")
        print("is running. It would need to be re-initialized to maintain")
        print("consistency. Be very careful.")
        if not ipautil.user_input("Continue to clean?", False):
            sys.exit("Aborted")

    if tree_found == 'ca':
        thisrepl = replication.get_cs_replication_manager(realm, options.host,
                                                          options.dirman_passwd)
    else:
        thisrepl = replication.ReplicationManager(realm, options.host,
                                                  options.dirman_passwd)
    thisrepl.cleanallruv(ruv)
    print("Cleanup task created")


def abort_clean_ruv(realm, ruv, options):
    """
    Given an RID abort a CLEANALLRUV task.
    """
    try:
        ruv = int(ruv)
    except ValueError:
        sys.exit("Replica ID must be an integer: %s" % ruv)

    try:
        servers = get_ruv_both_suffixes(realm, options.host,
                                        options.dirman_passwd,
                                        options.verbose,
                                        options.nolookup)
    except (NoRUVsFound, RuntimeError) as e:
        print(e)
        sys.exit(0 if isinstance(e, NoRUVsFound) else 1)

    tree_found = None
    for tree, ruvs in servers.items():
        for netloc, rid in ruvs:
            if ruv == int(rid):
                tree_found = tree
                hostname = netloc
                break
        if tree_found:
            break

    if not tree_found:
        sys.exit("Replica ID %s not found" % ruv)

    print("Aborting the clean Replication Update Vector task for %s" % hostname)
    print()
    if tree_found == 'ca':
        thisrepl = replication.get_cs_replication_manager(realm, options.host,
                                                          options.dirman_passwd)
    else:
        thisrepl = replication.ReplicationManager(realm, options.host,
                                                  options.dirman_passwd)
    thisrepl.abortcleanallruv(ruv, options.force)

    print("Cleanup task stopped")


def list_clean_ruv(realm, host, dirman_passwd, verbose, nolookup=False):
    """
    List all clean RUV tasks.
    """

    if not nolookup:
        enforce_host_existence(host)

    repl = replication.ReplicationManager(realm, host, dirman_passwd)
    dn = DN(('cn', 'cleanallruv'),('cn', 'tasks'), ('cn', 'config'))
    try:
        entries = repl.conn.get_entries(dn, repl.conn.SCOPE_ONELEVEL)
    except errors.NotFound:
        print("No CLEANALLRUV tasks running")
    else:
        print("CLEANALLRUV tasks")
        for entry in entries:
            name = entry.single_value['cn'].replace('clean ', '')
            status = entry.single_value.get('nsTaskStatus')
            print("RID %s: %s" % (name, status))
            if verbose:
                print(str(dn))
                print(entry.single_value.get('nstasklog'))

    print()

    dn = DN(('cn', 'abort cleanallruv'),('cn', 'tasks'), ('cn', 'config'))
    try:
        entries = repl.conn.get_entries(dn, repl.conn.SCOPE_ONELEVEL)
    except errors.NotFound:
        print("No abort CLEANALLRUV tasks running")
    else:
        print("Abort CLEANALLRUV tasks")
        for entry in entries:
            name = entry.single_value['cn'].replace('abort ', '')
            status = entry.single_value.get('nsTaskStatus')
            print("RID %s: %s" % (name, status))
            if verbose:
                print(str(dn))
                print(entry.single_value.get('nstasklog'))


def clean_dangling_ruvs(realm, host, options):
    """
    Cleans all RUVs and CS-RUVs that are left in the system from
    uninstalled replicas
    """
    ldap_uri = ipaldap.get_ldap_uri(host, 636, cacert=paths.IPA_CA_CRT)
    conn = ipaldap.LDAPClient(ldap_uri, cacert=paths.IPA_CA_CRT)
    try:
        conn.simple_bind(bind_dn=ipaldap.DIRMAN_DN,
                         bind_password=options.dirman_passwd)

        # get all masters
        masters_dn = DN(api.env.container_masters, api.env.basedn)
        masters = conn.get_entries(masters_dn, conn.SCOPE_ONELEVEL)
        info = {}

        # check whether CAs are configured on those masters
        for master in masters:
            info[master.single_value['cn']] = {
                'online': False,       # is the host online?
                'ca': False,           # does the host have ca configured?
                'ruvs': set(),         # ruvs on the host
                'csruvs': set(),       # csruvs on the host
                'clean_ruv': set(),    # ruvs to be cleaned from the host
                'clean_csruv': set()   # csruvs to be cleaned from the host
                }
            try:
                ca_dn = DN(('cn', 'ca'), master.dn)
                conn.get_entry(ca_dn)
                info[master.single_value['cn']]['ca'] = True
            except errors.NotFound:
                continue

    except Exception as e:
        sys.exit(
            "Failed to get data from '{host}' while trying to "
            "list replicas: {error}"
            .format(host=host, error=e)
            )
    finally:
        conn.unbind()

    replica_dn = DN(('cn', 'replica'), ('cn', api.env.basedn),
                    ('cn', 'mapping tree'), ('cn', 'config'))

    csreplica_dn = DN(('cn', 'replica'), ('cn', 'o=ipaca'),
                      ('cn', 'mapping tree'), ('cn', 'config'))

    ruvs = set()
    csruvs = set()
    offlines = set()
    for master_cn, master_info in info.items():
        try:
            ldap_uri = ipaldap.get_ldap_uri(master_cn, 636, cacert=paths.IPA_CA_CRT)
            conn = ipaldap.LDAPClient(ldap_uri, cacert=paths.IPA_CA_CRT)
            conn.simple_bind(bind_dn=ipaldap.DIRMAN_DN,
                             bind_password=options.dirman_passwd)
            master_info['online'] = True
        except Exception:
            print("The server '{host}' appears to be offline."
                  .format(host=master_cn))
            offlines.add(master_cn)
            continue
        try:
            try:
                entry = conn.get_entry(replica_dn)
                ruv = (master_cn, entry.single_value.get('nsDS5ReplicaID'))
                # the check whether ruv is already in ruvs is performed
                # by the set type
                ruvs.add(ruv)
            except errors.NotFound:
                pass

            if master_info['ca']:
                try:
                    entry = conn.get_entry(csreplica_dn)
                    csruv = (master_cn,
                             entry.single_value.get('nsDS5ReplicaID'))
                    csruvs.add(csruv)
                except errors.NotFound:
                    pass

            try:
                ruv_dict = get_ruv_both_suffixes(realm, master_cn,
                                                 options.dirman_passwd,
                                                 options.verbose,
                                                 options.nolookup)
            except (RuntimeError, NoRUVsFound):
                continue

            # get_ruv_both_suffixes returns server names with :port
            # This needs needs to be split off
            if ruv_dict.get('domain'):
                master_info['ruvs'] = set([
                    (re.sub(':\d+', '', x), y)
                    for (x, y) in ruv_dict['domain']
                    ])
            if ruv_dict.get('ca'):
                master_info['csruvs'] = set([
                    (re.sub(':\d+', '', x), y)
                    for (x, y) in ruv_dict['ca']
                    ])
        except Exception as e:
            sys.exit("Failed to obtain information from '{host}': {error}"
                     .format(host=master_cn, error=str(e)))
        finally:
            conn.unbind()

    dangles = False
    # get the dangling RUVs
    for master_info in info.values():
        if master_info['online']:
            for ruv in master_info['ruvs']:
                if (ruv not in ruvs) and (ruv[0] not in offlines):
                    master_info['clean_ruv'].add(ruv)
                    dangles = True

            # if ca is not configured, there will be no csruvs in master_info
            for csruv in master_info['csruvs']:
                if (csruv not in csruvs) and (csruv[0] not in offlines):
                    master_info['clean_csruv'].add(csruv)
                    dangles = True

    if not dangles:
        print('No dangling RUVs found')
        sys.exit(0)

    print('These RUVs are dangling and will be removed:')
    for master_cn, master_info in info.items():
        if master_info['online'] and (master_info['clean_ruv'] or
                                      master_info['clean_csruv']):
            print('Host: {m}'.format(m=master_cn))
            print('\tRUVs:')
            for ruv in master_info['clean_ruv']:
                print('\t\tid: {id}, hostname: {host}'
                      .format(id=ruv[1], host=ruv[0]))

            print('\tCS-RUVs:')
            for csruv in master_info['clean_csruv']:
                print('\t\tid: {id}, hostname: {host}'
                      .format(id=csruv[1], host=csruv[0]))

    if not options.force and not ipautil.user_input("Proceed with cleaning?", False):
        sys.exit("Aborted")

    options.force = True
    cleaned = set()
    for master_cn, master_info in info.items():
        options.host = master_cn
        for ruv in master_info['clean_ruv']:
            if ruv[1] not in cleaned:
                cleaned.add(ruv[1])
                clean_ruv(realm, ruv[1], options)
        for csruv in master_info['clean_csruv']:
            if csruv[1] not in cleaned:
                cleaned.add(csruv[1])
                clean_ruv(realm, csruv[1], options)


def check_last_link(delrepl, realm, dirman_passwd, force):
    """
    We don't want to orphan a server when deleting another one. If you have
    a topology that looks like this:

             A     B
             |     |
             |     |
             |     |
             C---- D

    If we try to delete host D it will orphan host B.

    What we need to do is if the master being deleted has only a single
    agreement, connect to that master and make sure it has agreements with
    more than just this master.

    @delrepl: a ReplicationManager object of the master being deleted

    returns: hostname of orphaned server or None
    """
    replica_entries = delrepl.find_ipa_replication_agreements()

    replica_names = [rep.single_value.get('nsds5replicahost')
                     for rep in replica_entries]

    orphaned = []
    # Connect to each remote server and see what agreements it has
    for replica in replica_names:
        try:
            repl = replication.ReplicationManager(realm, replica, dirman_passwd)
        except errors.NetworkError:
            print("Unable to validate that '%s' will not be orphaned." % replica)

            if not force and not ipautil.user_input("Continue to delete?", False):
                sys.exit("Aborted")
            continue

        entries = repl.find_ipa_replication_agreements()
        names = [rep.single_value.get('nsds5replicahost')
                 for rep in entries]

        if len(names) == 1 and names[0] == delrepl.hostname:
            orphaned.append(replica)

    if len(orphaned):
        return ', '.join(orphaned)
    else:
        return None


def enforce_host_existence(host, message=None):
    if host is None:
        return

    try:
        verify_host_resolvable(host)
    except errors.DNSNotARecordError as ex:
        if message is None:
            message = "Unknown host %s: %s" % (host, ex)
        sys.exit(message)

def ensure_last_services(conn, hostname, masters, options):
    """
    1. When deleting master, check if there will be at least one remaining
       DNS and CA server.
    2. Pick CA renewal master

    Return this_services, other_services, ca_hostname
    """

    this_services = []
    other_services = []
    ca_hostname = None

    for master in masters:
        master_cn = master['cn'][0]
        try:
            services = conn.get_entries(master['dn'], conn.SCOPE_ONELEVEL)
        except errors.NotFound:
            continue
        services_cns = [s.single_value['cn'] for s in services]
        if master_cn == hostname:
            this_services = services_cns
        else:
            other_services.append(services_cns)
            if ca_hostname is None and 'CA' in services_cns:
                ca_hostname = master_cn

    if 'CA' in this_services and not any(['CA' in o for o in other_services]):
        print("Deleting this server is not allowed as it would leave your installation without a CA.")
        sys.exit(1)

    other_dns = True
    if 'DNS' in this_services and not any(['DNS' in o for o in other_services]):
        other_dns = False
        print("Deleting this server will leave your installation without a DNS.")
        if not options.force and not ipautil.user_input("Continue to delete?", False):
            sys.exit("Deletion aborted")

    # test if replica is not DNSSEC master
    # allow to delete it if is last DNS server
    if 'DNS' in this_services and other_dns and not options.force:
        dnssec_masters = opendnssecinstance.get_dnssec_key_masters(conn)
        if hostname in dnssec_masters:
            print("Replica is active DNSSEC key master. Uninstall could break your DNS system.")
            print("Please disable or replace DNSSEC key master first.")
            sys.exit("Deletion aborted")

    ca = cainstance.CAInstance(api.env.realm)
    if ca.is_renewal_master(hostname):
        try:
            ca.set_renewal_master(options.host)
        except errors.NotFound:
            ca.set_renewal_master(ca_hostname)

    return this_services, other_services, ca_hostname


def cleanup_server_dns_entries(realm, hostname, suffix, options):
    try:
        if bindinstance.dns_container_exists(suffix):
            bindinstance.remove_master_dns_records(hostname, realm)
            dnskeysyncinstance.remove_replica_public_keys(hostname)
    except Exception as e:
        print("Failed to cleanup %s DNS entries: %s" % (hostname, e))
        print("You may need to manually remove them from the tree")


def del_master(realm, hostname, options):

    if has_managed_topology(api):
        del_master_managed(realm, hostname, options)
    else:
        del_master_direct(realm, hostname, options)

def del_master_managed(realm, hostname, options):
    """
    Removing of master in managed_topology
    """

    hostname_u = ipautil.fsdecode(hostname)
    if hostname == options.host:
        print("Can't remove itself: %s" % (options.host))
        sys.exit(1)

    server_del_options = dict(
        force=options.cleanup,
        ignore_topology_disconnect=options.force,
        ignore_last_of_role=options.force
    )

    try:
        replication.run_server_del_as_cli(
            api, hostname_u, **server_del_options)
    except Exception as e:
        sys.exit(e)


def del_master_direct(realm, hostname, options):
    """
    Removing of master for realm without managed topology
    (domain level < DOMAIN_LEVEL_1)
    """

    force_del = False
    delrepl = None

    # 1. Connect to the local server
    try:
        thisrepl = replication.ReplicationManager(realm, options.host,
                                                  options.dirman_passwd)
    except Exception as e:
        print("Failed to connect to server %s: %s" % (options.host, e))
        sys.exit(1)

    # 2. Ensure we have an agreement with the master
    agreement = thisrepl.get_replication_agreement(hostname)
    if agreement is None:
        if options.cleanup:
            """
            We have no agreement with the current master, so this is a
            candidate for cleanup. This is VERY dangerous to do because it
            removes that master from the list of masters. If the master
            were to try to come back online it wouldn't work at all.
            """
            print("Cleaning a master is irreversible.")
            print("This should not normally be require, so use cautiously.")
            if not ipautil.user_input("Continue to clean master?", False):
                sys.exit("Cleanup aborted")
            thisrepl.replica_cleanup(hostname, realm, force=True)
            sys.exit(0)
        else:
            sys.exit("'%s' has no replication agreement for '%s'" % (options.host, hostname))

    # 3. If an IPA agreement connect to the master to be removed.
    repltype = thisrepl.get_agreement_type(hostname)
    if repltype == replication.IPA_REPLICA:
        winsync = False
        try:
            delrepl = replication.ReplicationManager(realm, hostname, options.dirman_passwd)
        except Exception as e:
            print("Connection to '%s' failed: %s" % (hostname, e))
            if not options.force:
                print("Unable to delete replica '%s'" % hostname)
                sys.exit(1)
            else:
                print("Forcing removal of %s" % hostname)
                force_del = True

        if force_del:
            dn = DN(('cn', 'masters'), ('cn', 'ipa'), ('cn', 'etc'), thisrepl.suffix)
            entries = thisrepl.conn.get_entries(
                dn, thisrepl.conn.SCOPE_ONELEVEL)
            replica_names = []
            for entry in entries:
                replica_names.append(entry.single_value['cn'])
            # The host we're removing gets included in this list, remove it.
            # Otherwise we try to delete an agreement from the host to itself.
            try:
                replica_names.remove(hostname)
            except ValueError:
                pass
        else:
            # Get list of agreements.
            replica_entries = delrepl.find_ipa_replication_agreements()
            replica_names = [rep.single_value.get('nsds5replicahost')
                             for rep in replica_entries]
    else:
        # WINSYNC replica, delete agreement from current host
        winsync = True
        replica_names = [options.host]

    if not winsync and not options.force:
        print("Deleting a master is irreversible.")
        print("To reconnect to the remote master you will need to prepare " \
              "a new replica file")
        print("and re-install.")
        if not ipautil.user_input("Continue to delete?", False):
            sys.exit("Deletion aborted")

    # Check for orphans if the remote server is up.
    if delrepl and not winsync:
        try:
            masters = api.Command.server_find(
                '', sizelimit=0, no_members=False)['result']
        except Exception as e:
            masters = []
            print("Failed to read masters data from '%s': %s" % (
                delrepl.hostname, e))
            print("Skipping calculation to determine if one or more masters would be orphaned.")
            if not options.force:
                sys.exit(1)

        # This only applies if we have more than 2 IPA servers, otherwise
        # there is no chance of an orphan.
        if len(masters) > 2:
            orphaned_server = check_last_link(delrepl, realm, options.dirman_passwd, options.force)
            if orphaned_server is not None:
                print("Deleting this server will orphan '%s'. " % orphaned_server)
                print("You will need to reconfigure your replication topology to delete this server.")
                sys.exit(1)

        # 4. Check that we are not leaving the installation without CA and/or DNS
        #    And pick new CA master.
        ensure_last_services(thisrepl.conn, hostname, masters, options)
    else:
        print("Skipping calculation to determine if one or more masters would be orphaned.")

    # Save the RID value before we start deleting
    if repltype == replication.IPA_REPLICA:
        rid = get_rid_by_host(realm, options.host, hostname,
                              options.dirman_passwd, options.nolookup)

    # 4. Remove each agreement

    print("Deleting replication agreements between %s and %s" % (hostname, ', '.join(replica_names)))
    for r in replica_names:
        try:
            if not del_link(realm, r, hostname, options.dirman_passwd, force=True):
                print("Unable to remove replication agreement for %s from %s." % (hostname, r))
        except Exception as e:
            print(("There were issues removing a connection for %s "
                "from %s: %s" % (hostname, r, e)))

    # 5. Clean RUV for the deleted master
    if repltype == replication.IPA_REPLICA and rid is not None:
        try:
            thisrepl.cleanallruv(rid)
        except KeyboardInterrupt:
            print("Wait for task interrupted. It will continue to run in the background")

    # 6. Finally clean up the removed replica common entries.
    try:
        thisrepl.replica_cleanup(hostname, realm, force=True)
    except Exception as e:
        print("Failed to cleanup %s entries: %s" % (hostname, e))
        print("You may need to manually remove them from the tree")

    # 7. And clean up the removed replica DNS entries if any.
    cleanup_server_dns_entries(realm, hostname, thisrepl.suffix, options)

def add_link(realm, replica1, replica2, dirman_passwd, options):

    if not options.nolookup:
        for check_host in [replica1,replica2]:
            enforce_host_existence(check_host)

    if options.winsync:
        if not options.binddn or not options.bindpw or not options.cacert or not options.passsync:
            logger.error("The arguments --binddn, --bindpw, --passsync and "
                         "--cacert are required to create a winsync agreement")
            sys.exit(1)
        if os.getegid() != 0:
            logger.error("winsync agreements need to be created as root")
            sys.exit(1)
    elif has_managed_topology(api):
        exit_on_managed_topology("Creation of IPA replication agreement")

    try:
        repl = replication.ReplicationManager(realm, replica1, dirman_passwd)
    except errors.NotFound:
        print("Cannot find replica '%s'" % replica1)
        return
    except Exception as e:
        print("Failed to connect to '%s': %s" % (replica1, e))
        return

    # See if we already have an agreement with this host
    try:
        if repl.get_agreement_type(replica2) == replication.WINSYNC:
            agreement = repl.get_replication_agreement(replica2)
            sys.exit("winsync agreement already exists on subtree %s" %
                agreement.single_value.get('nsds7WindowsReplicaSubtree'))
        else:
            sys.exit("A replication agreement to %s already exists" % replica2)
    except errors.NotFound:
        pass

    if options.cacert:
        # have to install the given CA cert before doing anything else
        ds = dsinstance.DsInstance(realm_name=realm)
        if not ds.add_ca_cert(options.cacert):
            print("Could not load the required CA certificate file [%s]" % options.cacert)
            return
        else:
            print("Added CA certificate %s to certificate database for %s" % (options.cacert, replica1))

    # need to wait until cacert is installed as that command may restart
    # the directory server and kill the connection
    try:
        repl1 = replication.ReplicationManager(realm, replica1, dirman_passwd)
    except errors.NotFound:
        print("Cannot find replica '%s'" % replica1)
        return
    except Exception as e:
        print("Failed to connect to '%s': %s" % (replica1, e))
        return

    if options.winsync:
        repl1.setup_winsync_replication(replica2,
                                        options.binddn, options.bindpw,
                                        options.passsync, options.win_subtree,
                                        options.cacert)
    else:
        # Check if the master entry exists for both servers.
        # If one of the tree misses one of the entries, it means one of the
        # replicas was fully deleted previously and needs to be reinstalled
        # from scratch
        try:
            masters_dn = DN(('cn', 'masters'), ('cn', 'ipa'), ('cn', 'etc'), (api.env.basedn))
            master1_dn = DN(('cn', replica1), masters_dn)
            master2_dn = DN(('cn', replica2), masters_dn)

            repl1.conn.get_entry(master1_dn)
            repl1.conn.get_entry(master2_dn)

            repl2 = replication.ReplicationManager(realm, replica2, dirman_passwd)
            repl2.conn.get_entry(master1_dn)
            repl2.conn.get_entry(master2_dn)

        except errors.NotFound:
            standard_logging_setup(console_format='%(message)s')

            ds = ipadiscovery.IPADiscovery()
            ret = ds.search(servers=[replica2])

            if ret == ipadiscovery.NOT_IPA_SERVER:
                sys.exit("Connection unsuccessful: %s is not an IPA Server." %
                    replica2)
            elif ret == 0:  # success
                sys.exit("Connection unsuccessful: %s is an IPA Server, "
                    "but it might be unknown, foreign or previously deleted "
                    "one." % replica2)
            else:
                sys.exit("Connection to %s unsuccessful." % replica2)

        repl1.setup_gssapi_replication(replica2, DN(('cn', 'Directory Manager')), dirman_passwd)
    print("Connected '%s' to '%s'" % (replica1, replica2))

def re_initialize(realm, thishost, fromhost, dirman_passwd, nolookup=False):

    if not nolookup:
        for check_host in [thishost, fromhost]:
            enforce_host_existence(check_host)

    thisrepl = replication.ReplicationManager(realm, thishost, dirman_passwd)
    agreement = thisrepl.get_replication_agreement(fromhost)
    if agreement is None:
        sys.exit("'%s' has no replication agreement for '%s'" % (thishost, fromhost))
    repltype = thisrepl.get_agreement_type(fromhost)
    if repltype == replication.WINSYNC:
        # With winsync we don't have a "remote" agreement, it is all local
        repl = replication.ReplicationManager(realm, thishost, dirman_passwd)
        repl.initialize_replication(agreement.dn, repl.conn)
        repl.wait_for_repl_init(repl.conn, agreement.dn)
    else:
        repl = replication.ReplicationManager(realm, fromhost, dirman_passwd)
        agreement = repl.get_replication_agreement(thishost)

        try:
            thisrepl.enable_agreement(fromhost)
            repl.enable_agreement(thishost)
        except errors.NotFound as e:
            sys.exit(e)

        repl.force_sync(repl.conn, thishost)

        repl.initialize_replication(agreement.dn, repl.conn)
        repl.wait_for_repl_init(repl.conn, agreement.dn)

        # If the agreement doesn't have nsDS5ReplicatedAttributeListTotal it means
        # we did not replicate memberOf, do so now.
        if not agreement.single_value.get('nsDS5ReplicatedAttributeListTotal'):
            ds = dsinstance.DsInstance(realm_name=realm)
            ds.ldapi = os.getegid() == 0
            ds.init_memberof()

def force_sync(realm, thishost, fromhost, dirman_passwd, nolookup=False):

    if not nolookup:
        for check_host in [thishost, fromhost]:
            enforce_host_existence(check_host)

    thisrepl = replication.ReplicationManager(realm, thishost, dirman_passwd)
    agreement = thisrepl.get_replication_agreement(fromhost)
    if agreement is None:
        sys.exit("'%s' has no replication agreement for '%s'" % (thishost, fromhost))
    repltype = thisrepl.get_agreement_type(fromhost)
    if repltype == replication.WINSYNC:
        # With winsync we don't have a "remote" agreement, it is all local
        repl = replication.ReplicationManager(realm, thishost, dirman_passwd)
        repl.force_sync(repl.conn, fromhost)
    else:
        ds = dsinstance.DsInstance(realm_name=realm)
        ds.ldapi = os.getegid() == 0
        ds.replica_manage_time_skew(prevent=False)
        repl = replication.ReplicationManager(realm, fromhost, dirman_passwd)
        repl.force_sync(repl.conn, thishost)
        agreement = repl.get_replication_agreement(thishost)
        repl.wait_for_repl_init(repl.conn, agreement.dn)
        ds.replica_manage_time_skew(prevent=True)

def show_DNA_ranges(hostname, master, realm, dirman_passwd, nextrange=False,
                    nolookup=False):
    """
    Display the DNA ranges for all current masters.

    hostname: hostname of the master we're listing from
    master: specific master to show, or None for all
    realm: our realm, needed to create a connection
    dirman_passwd: the DM password, needed to create a connection
    nextrange: if False then show main range, if True then show next

    Returns nothing
    """

    if not nolookup:
        enforce_host_existence(hostname)
        if master is not None:
            enforce_host_existence(master)

    try:
        repl = replication.ReplicationManager(realm, hostname, dirman_passwd)
    except Exception as e:
        sys.exit("Connection failed: %s" % e)
    dn = DN(('cn', 'masters'), ('cn', 'ipa'), ('cn', 'etc'), repl.suffix)
    try:
        entries = repl.conn.get_entries(dn, repl.conn.SCOPE_ONELEVEL)
    except Exception:
        return False

    for ent in entries:
        remote = ent.single_value['cn']
        if master is not None and remote != master:
            continue
        try:
            repl2 = replication.ReplicationManager(realm, remote, dirman_passwd)
        except Exception as e:
            print("%s: Connection failed: %s" % (remote, e))
            continue
        if not nextrange:
            try:
                (start, max) = repl2.get_DNA_range(remote)
            except errors.NotFound:
                print("%s: No permission to read DNA configuration" % remote)
                continue
            if start is None:
                print("%s: No range set" % remote)
            else:
                print("%s: %s-%s" % (remote, start, max))
        else:
            try:
                (next_start, next_max) = repl2.get_DNA_next_range(remote)
            except errors.NotFound:
                print("%s: No permission to read DNA configuration" % remote)
                continue
            if next_start is None:
                print("%s: No on-deck range set" % remote)
            else:
                print("%s: %s-%s" % (remote, next_start, next_max))

    return False


def store_DNA_range(repl, range_start, range_max, deleted_master, realm,
                    dirman_passwd):
    """
    Given a DNA range try to save it in a remaining master in the
    on-deck (dnaNextRange) value.

    Return True if range was saved, False if not

    This function focuses on finding an available master.

    repl: ReplicaMaster object for the master we're deleting from
    range_start: The DNA next value
    range_max: The DNA max value
    deleted_master: The hostname of the master to be deleted
    realm: our realm, needed to create a connection
    dirman_passwd: the DM password, needed to create a connection
    """
    dn = DN(('cn', 'masters'), ('cn', 'ipa'), ('cn', 'etc'), repl.suffix)
    try:
        entries = repl.conn.get_entries(dn, repl.conn.SCOPE_ONELEVEL)
    except Exception:
        return False

    for ent in entries:
        candidate = ent.single_value['cn']
        if candidate == deleted_master:
            continue
        try:
            repl2 = replication.ReplicationManager(realm, candidate, dirman_passwd)
        except Exception as e:
            print("Connection failed: %s" % e)
            continue
        next_start, _next_max = repl2.get_DNA_next_range(candidate)
        if next_start is None:
            try:
                return repl2.save_DNA_next_range(range_start, range_max)
            except Exception as e:
                print('%s: %s' % (candidate, e))

    return False


def set_DNA_range(hostname, range, realm, dirman_passwd, next_range=False,
                  nolookup=False):
    """
    Given a DNA range try to change it on the designated master.

    The range must not overlap with any other ranges and must be within
    one of the IPA local ranges as defined in cn=ranges.

    Setting an on-deck range of 0-0 removes the range.

    Return True if range was saved, False if not

    hostname: hostname of the master to set the range on
    range: The DNA range to set
    realm: our realm, needed to create a connection
    dirman_passwd: the DM password, needed to create a connection
    next_range: if True then setting a next-range, otherwise a DNA range.
    """
    def validate_range(range, allow_all_zero=False):
        """
        Do some basic sanity checking on the range.

        Returns None if ok, a string if an error.
        """
        try:
            (dna_next, dna_max) = range.split('-', 1)
        except ValueError:
            return "Invalid range, must be the form x-y"

        try:
            dna_next = int(dna_next)
            dna_max = int(dna_max)
        except ValueError:
            return "The range must consist of integers"

        if dna_next == 0 and dna_max == 0 and allow_all_zero:
            return None

        if dna_next <= 0 or dna_max <= 0 or dna_next >= MAXINT or dna_max >= MAXINT:
            return "The range must consist of positive integers between 1 and %d" % MAXINT

        if dna_next >= dna_max:
            return "Invalid range"

        return None

    def range_intersection(s1, s2, r1, r2):
        return max(s1, r1) <= min(s2, r2)

    if not nolookup:
        enforce_host_existence(hostname)

    err = validate_range(range, allow_all_zero=next_range)
    if err is not None:
        sys.exit(err)

    # Normalize the range
    (dna_next, dna_max) = range.split('-', 1)
    dna_next = int(dna_next)
    dna_max = int(dna_max)

    try:
        repl = replication.ReplicationManager(realm, hostname, dirman_passwd)
    except Exception as e:
        sys.exit("Connection failed: %s" % e)
    if dna_next > 0:
        # Verify that the new range doesn't overlap with an existing range
        dn = DN(('cn', 'masters'), ('cn', 'ipa'), ('cn', 'etc'), repl.suffix)
        try:
            entries = repl.conn.get_entries(dn, repl.conn.SCOPE_ONELEVEL)
        except Exception as e:
            sys.exit("Failed to read master data from '%s': %s" % (repl.conn.host, str(e)))
        else:
            for ent in entries:
                master = ent.single_value['cn']
                if master == hostname and not next_range:
                    continue
                try:
                    repl2 = replication.ReplicationManager(realm, master, dirman_passwd)
                except Exception as e:
                    print("Connection to %s failed: %s" % (master, e))
                    print("Overlap not checked.")
                    continue
                try:
                    (entry_start, entry_max) = repl2.get_DNA_range(master)
                except errors.NotFound:
                    print("%s: No permission to read DNA configuration" % master)
                    continue
                if (entry_start is not None and
                    range_intersection(entry_start, entry_max,
                                       dna_next, dna_max)):
                    sys.exit("New range overlaps the DNA range on %s" % master)
                (entry_start, entry_max) = repl2.get_DNA_next_range(master)
                if (entry_start is not None and
                    range_intersection(entry_start, entry_max,
                                       dna_next, dna_max)):
                    sys.exit("New range overlaps the DNA next range on %s" % master)
                del(repl2)

        # Verify that this is within one of the IPA domain ranges.
        dn = DN(('cn','ranges'), ('cn','etc'), repl.suffix)
        try:
            entries = repl.conn.get_entries(dn, repl.conn.SCOPE_ONELEVEL,
                                        "(objectclass=ipaDomainIDRange)")
        except errors.NotFound as e:
            sys.exit('Unable to load IPA ranges: {err}'.format(err=e))

        for ent in entries:
            entry_start = int(ent.single_value['ipabaseid'])
            entry_max = entry_start + int(ent.single_value['ipaidrangesize'])
            if dna_next >= entry_start and dna_max <= entry_max:
                break
        else:
            sys.exit("New range does not fit within existing IPA ranges. See ipa help idrange command")

        # If this falls within any of the AD ranges then it fails.
        try:
            entries = repl.conn.get_entries(dn, repl.conn.SCOPE_BASE,
                                            "(objectclass=ipatrustedaddomainrange)")
        except errors.NotFound:
            entries = []

        for ent in entries:
            entry_start = int(ent.single_value['ipabaseid'])
            entry_max = entry_start + int(ent.single_value['ipaidrangesize'])
            if range_intersection(dna_next, dna_max, entry_start, entry_max):
                sys.exit("New range overlaps with a Trust range. See ipa help idrange command")

    if next_range:
        try:
            if not repl.save_DNA_next_range(dna_next, dna_max):
                sys.exit("Updating next range failed")
        except errors.EmptyModlist:
            sys.exit("No changes to make")
        except errors.NotFound:
                sys.exit("No permission to update ranges")
        except Exception as e:
            sys.exit("Updating next range failed: %s" % e)
    else:
        try:
            if not repl.save_DNA_range(dna_next, dna_max):
                sys.exit("Updating range failed")
        except errors.EmptyModlist:
            sys.exit("No changes to make")
        except errors.NotFound:
                sys.exit("No permission to update ranges")
        except Exception as e:
            sys.exit("Updating range failed: %s" % e)


def exit_on_managed_topology(what):
    sys.exit("{0} is deprecated with managed IPA replication topology. "
             "Please use `ipa topologysegment-*` commands to manage "
             "the topology.".format(what))

def main(options, args):
    if os.getegid() == 0:
        installutils.check_server_configuration()
    elif not os.path.exists(paths.IPA_DEFAULT_CONF):
        sys.exit("IPA is not configured on this system.")

    api.bootstrap(
        context='cli', confdir=paths.ETC_IPA,
        in_server=True, verbose=options.verbose, debug=options.debug
    )
    api.finalize()

    dirman_passwd = None
    realm = api.env.realm

    if options.host:
        host = options.host
    else:
        host = installutils.get_fqdn()

    options.host = host

    if options.dirman_passwd:
        dirman_passwd = options.dirman_passwd
    else:
        if (not test_connection(realm, host, options.nolookup) or
           args[0] in dirman_passwd_req_commands):
            dirman_passwd = installutils.read_password("Directory Manager",
                confirm=False, validate=False, retry=False)
            if dirman_passwd is None or (
               not dirman_passwd and args[0] in dirman_passwd_req_commands):
                sys.exit("Directory Manager password required")

    options.dirman_passwd = dirman_passwd

    # Initialize the LDAP connection
    api.Backend.ldap2.connect(bind_pw=options.dirman_passwd)

    if args[0] == "list":
        replica = None
        if len(args) == 2:
            replica = args[1]
        list_replicas(realm, host, replica, dirman_passwd, options.verbose,
                      options.nolookup)
    elif args[0] == "list-ruv":
        list_ruv(realm, host, dirman_passwd, options.verbose, options.nolookup)
    elif args[0] == "del":
        del_master(realm, args[1], options)
    elif args[0] == "re-initialize":
        if not options.fromhost:
            print("re-initialize requires the option --from <host name>")
            sys.exit(1)
        re_initialize(realm, host, options.fromhost, dirman_passwd,
                      options.nolookup)
    elif args[0] == "force-sync":
        if not options.fromhost:
            print("force-sync requires the option --from <host name>")
            sys.exit(1)
        force_sync(realm, host, options.fromhost, options.dirman_passwd,
                   options.nolookup)
    elif args[0] == "connect":
        if len(args) == 3:
            replica1 = args[1]
            replica2 = args[2]
        elif len(args) == 2:
            replica1 = host
            replica2 = args[1]
        add_link(realm, replica1, replica2, dirman_passwd, options)
    elif args[0] == "disconnect":
        if len(args) == 3:
            replica1 = args[1]
            replica2 = args[2]
        elif len(args) == 2:
            replica1 = host
            replica2 = args[1]
        del_link(realm, replica1, replica2, dirman_passwd)
    elif args[0] == "clean-ruv":
        clean_ruv(realm, args[1], options)
    elif args[0] == "abort-clean-ruv":
        abort_clean_ruv(realm, args[1], options)
    elif args[0] == "list-clean-ruv":
        list_clean_ruv(realm, host, dirman_passwd, options.verbose,
                       options.nolookup)
    elif args[0] == "clean-dangling-ruv":
        clean_dangling_ruvs(realm, host, options)
    elif args[0] == "dnarange-show":
        if len(args) == 2:
            master = args[1]
        else:
            master = None
        show_DNA_ranges(host, master, realm, dirman_passwd, False,
                        options.nolookup)
    elif args[0] == "dnanextrange-show":
        if len(args) == 2:
            master = args[1]
        else:
            master = None
        show_DNA_ranges(host, master, realm, dirman_passwd, True,
                        options.nolookup)
    elif args[0] == "dnarange-set":
        set_DNA_range(args[1], args[2], realm, dirman_passwd, next_range=False,
                      nolookup=options.nolookup)
    elif args[0] == "dnanextrange-set":
        set_DNA_range(args[1], args[2], realm, dirman_passwd, next_range=True,
                      nolookup=options.nolookup)

    api.Backend.ldap2.disconnect()

try:
    options, args = parse_options()
    main(options, args)
except KeyboardInterrupt:
    sys.exit(1)
except SystemExit as e:
    sys.exit(e)
except RuntimeError as e:
    sys.exit(e)
except socket.timeout:
    print("Connection timed out.")
    sys.exit(1)
except Exception as e:
    if options.verbose:
        traceback.print_exc(file=sys.stdout)
    else:
        print(
            "Re-run {} with --verbose option to get more information".format(
                sys.argv[0])
        )

    print("Unexpected error: %s" % str(e))
    sys.exit(1)
