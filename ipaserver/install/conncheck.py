#
# Copyright (C) 2016  FreeIPA Contributors see COPYING for license
#

"""
Connection check module
"""

import os
import functools
from socket import SOCK_STREAM, SOCK_DGRAM

from ipalib import api
from ipalib.install import service
from ipalib.install.service import enroll_only, replica_install_only
from ipaplatform.services import service as service_factory
from ipapython import ipautil
from ipapython.install.core import knob


class ConnCheckInterface(service.ServiceAdminInstallInterface):
    """
    Interface common to all installers which perform connection check to the
    remote master.
    """

    skip_conncheck = knob(
        None,
        description="skip connection check to remote master",
    )
    skip_conncheck = enroll_only(skip_conncheck)
    skip_conncheck = replica_install_only(skip_conncheck)


@functools.total_ordering
class CheckedPort:
    def __init__(self, port, port_type, description, service_name=None,
                 localhost=False):
        assert port_type in (SOCK_STREAM, SOCK_DGRAM)
        self.port = port
        self.port_type = port_type
        self.description = description
        # see ipaplatform.services.service()
        self.service_name = service_name
        self.localhost = localhost

    def __hash__(self):
        return hash((self.port, self.port_type))

    def __eq__(self, other):
        if not isinstance(other, CheckedPort):
            return NotImplemented
        return (self.port, self.port_type) == (other.port, other.port_type)

    def __lt__(self, other):
        if not isinstance(other, CheckedPort):
            return NotImplemented
        return (self.port, self.port_type) < (other.port, other.port_type)

    def __repr__(self):
        port_name = "TCP" if self.port_type == SOCK_STREAM else "UDP"
        return (
            "<{self.__class__.__name__} {self.port}/{port_name} "
            "{self.description}>"
        ).format(self=self, port_name=port_name)

    @property
    def service(self):
        """Get systemd service instance"""
        return service_factory(self.service_name, api=api)

    def is_bindable(self):
        """Check if a port is free and not bound by any other application
        """
        return ipautil.check_port_bindable(self.port, self.port_type)

    def check_host_connect(self, host, socket_timeout=None, **kwargs):
        """Check if port on remote host can be reached
        """
        return ipautil.host_port_open(
            host, self.port, socket_type=self.port_type,
            socket_timeout=socket_timeout, **kwargs
        )


DS_PORTS = [
    CheckedPort(389, SOCK_STREAM, "389-DS: insecure port", 'dirsv'),
    CheckedPort(636, SOCK_STREAM, "389-DS: secure port", 'dirsv'
    ),
]

KERBEROS_PORTS = [
    CheckedPort(88, SOCK_STREAM, "Kerberos KDC: TCP", 'krb5kdc'),
    CheckedPort(88, SOCK_DGRAM, "Kerberos KDC: UDP", 'krb5kdc'),
    CheckedPort(464, SOCK_STREAM, "Kerberos KPasswd: TCP", 'krb5kdc'),
    CheckedPort(464, SOCK_DGRAM, "Kerberos KPasswd: UDP", 'krb5kdc'),
    CheckedPort(749, SOCK_STREAM, "Kerberos Admin: TCP", 'krb5kdc'),
]

HTTP_PORTS = [
    CheckedPort(80, SOCK_STREAM, "HTTP Server: Insecure port", 'httpd'),
    CheckedPort(443, SOCK_STREAM, "HTTP Server: Secure port", 'httpd'),
]

CA_PORTS = [
    CheckedPort(
        8005, SOCK_STREAM, "PKI-CA: Tomcat shutdown", 'pki_tomcatd',
        localhost=True
    ),
    CheckedPort(
        8009, SOCK_STREAM, "PKI-CA: Tomcat AJP", 'pki_tomcatd',
        localhost=True
    ),
    CheckedPort(8080, SOCK_STREAM, "PKI-CA: Insecure port", 'pki_tomcatd'),
    CheckedPort(8443, SOCK_STREAM, "PKI-CA: Secure port",  'pki_tomcatd'),
]

CA_LEGACY_PORTS = [
    # New installations use the same 389-DS instance as IPA
    CheckedPort(7389, SOCK_STREAM, "PKI-CA: Directory Service port"),
]

NAMED_PORTS = [
    CheckedPort(53, SOCK_STREAM, "Bind DNS: TCP", 'named-pkcs11'),
    CheckedPort(53, SOCK_DGRAM, "Bind DNS: UDP", 'named-pkcs11'),
    CheckedPort(
        953, SOCK_DGRAM, "Bind DNS: remote control", 'named-pkcs11',
        localhost=True
    ),
]

BASE_PORTS = DS_PORTS + KERBEROS_PORTS + HTTP_PORTS
IPA_PORTS = BASE_PORTS + CA_PORTS + NAMED_PORTS


def test():
    root = os.getegid() == 0
    if not root:
        print("Not root, cannot detect ports < 1024.")
    for cp in sorted(IPA_PORTS):
        if cp.port < 1024 and not root:
            bindable = "unknown"
        else:
            bindable = repr(cp.is_bindable())
        print(cp, "\t", bindable)


if __name__ == '__main__':
    test()
