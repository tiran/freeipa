#
# Copyright (C) 2020  FreeIPA Contributors see COPYING for license
#
"""IPA extensions for Sphinx
"""
from sphinx.directives import ObjectDescription
from sphinx.directives.other import Author
from sphinx.domains import Domain, ObjType
from sphinx.roles import XRefRole
from sphinx.locale import _


class IPAObject(ObjectDescription):
    pass


class IPAPermission(IPAObject):
    pass


class IPAXRefRole(XRefRole):
    pass


class IPADomain(Domain):
    name = "ipa"
    label = "IPA"

    object_types = {
        "permission": ObjType(_("permission"), "permission"),
    }

    directives = {
        "permission": IPAPermission,
    }

    roles = {
        "permission": IPAXRefRole(),
    }


class LDAPObject(ObjectDescription):
    pass


class LDAPAttribute(LDAPObject):
    pass


class LDAPObjectClass(LDAPObject):
    pass


class LDAPOID(LDAPObject):
    pass


class LDAPXRefRole(XRefRole):
    pass


class LDAPDomain(Domain):
    name = "ldap"
    label = "LDAP"

    object_types = {
        "attribute": ObjType(_("attribute"), "attr"),
        "objectclass": ObjType(_("object class"), "objcls"),
        "oid": ObjType(_("OID"), "oid"),
    }

    directives = {
        "attribute": LDAPAttribute,
        "objectclass": LDAPObjectClass,
        "oid": LDAPOID,
    }

    roles = {
        "attr": LDAPXRefRole(),
        "objcls": LDAPXRefRole(),
        "oid": LDAPXRefRole(),
    }


def setup(app):
    app.add_domain(IPADomain)
    app.add_domain(LDAPDomain)

    app.add_directive("designauthor", Author)

    return {"version": "1.0", "parallel_read_safe": True}
