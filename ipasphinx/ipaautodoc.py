from sphinx.ext.autodoc import ModuleDocumenter, ClassDocumenter
from sphinx.domains.python import PyClasslike, PyModule, PythonDomain

from ipalib.frontend import Object


class IPAPluginModuleDocumenter(ModuleDocumenter):
    # .. autoipapluginmodule:: ipaserver.plugins.aci
    objtype = "ipapluginmodule"


class IPAPluginDocumenter(ClassDocumenter):
    objtype = "ipaplugin"

    # def format_args(self, **kwargs):
    #     # suppress signature 'cmd(api)'
    #     return None


def _ipa_plugin_docstring(app, what, name, obj, options, lines):
    if what != IPAPluginModuleDocumenter.objtype:
        return
    if not lines:
        return
    title = lines[0]
    if title:
        command = name.rsplit(".", 1)[-1]
        newtitle = f"[{command}] {title}"
        lines.insert(0, "=" * len(newtitle))
        lines[1] = newtitle
        lines.insert(2, "=" * len(newtitle))

    for entry in obj.register:
        plugin = entry["plugin"]
        if issubclass(plugin, Object):
            continue
        # autodoc uses __init__ for class signature
        plugin.__init__.__signature__ = plugin.__signature__
        lines.append(f".. autoipaplugin:: {plugin.__name__}")


def setup(app):
    app.setup_extension("ipasphinx.ipabase")
    app.setup_extension("sphinx.ext.autodoc")
    app.add_autodocumenter(IPAPluginModuleDocumenter)
    app.add_autodocumenter(IPAPluginDocumenter)
    app.connect("autodoc-process-docstring", _ipa_plugin_docstring)
    app.add_directive_to_domain(
        PythonDomain.name, IPAPluginModuleDocumenter.objtype, PyModule
    )
    app.add_directive_to_domain(
        PythonDomain.name, IPAPluginDocumenter.objtype, PyClasslike
    )

    return {
        "version": "0.1",
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }
