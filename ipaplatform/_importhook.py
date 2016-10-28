# Copyright (C) 2016  Red Hat
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

import importlib
import sys

import ipaplatform


class IpaMetaImporter(object):
    """Meta import hook and platform detector.

    The meta import hook uses /etc/os-release to auto-detects the best
    matching ipaplatform provider. It is compatible with external namespace
    packages, too.
    """
    modules = {
        'ipaplatform.constants',
        'ipaplatform.paths',
        'ipaplatform.services',
        'ipaplatform.tasks'
    }
    bsd_family = ('freebsd', 'openbsd', 'netbsd', 'dragonfly', 'gnukfreebsd')

    def __init__(self):
        self.platform_ids = self._get_platform_ids()
        self.platform = self._get_platform(self.platform_ids)
        # fix modules that have been loaded
        for module in self.modules:
            if module in sys.modules:
                self.load_module(module)

    def _get_platform_ids(self):
        if sys.platform.startswith('linux'):
            # Linux, get distribution from /etc/os-release
            return self._read_osrelease()
        elif sys.platform == 'win32':
            # Windows 32 or 64bit platform
            return ['win32']
        elif sys.platform == 'darwin':
            # macOS
            return ['macos']
        elif sys.platform.startswith(self.bsd_family):
            # BSD family, look for e.g. ['freebsd10', 'freebsd']
            return [sys.platform, sys.platform.rstrip('0123456789')]
        else:
            raise ValueError(sys.platform)

    def _read_osrelease(self, filename='/etc/os-release'):
        platforms = []
        with open(filename) as f:
            for line in f:
                key, value = line.rstrip('\n').split('=', 1)
                if value.startswith(('"', "'")):
                    value = value[1:-1]
                if key == 'ID':
                    platforms.insert(0, value)
                # fallback to base distro, centos has ID_LIKE="rhel fedora"
                if key == 'ID_LIKE':
                    platforms.extend(value.split(' '))
        return platforms

    def _get_platform(self, platform_ids):
        for platform in platform_ids:
            try:
                importlib.import_module('ipaplatform.{}'.format(platform))
            except ImportError:
                pass
            else:
                return platform
        raise ImportError('No ipaplatform available for "{}"'.format(
                          ', '.join(platform_ids)))

    def find_module(self, fullname, path=None):
        """Meta importer hook"""
        if fullname in self.modules:
            return self
        return None

    def load_module(self, fullname):
        """Meta importer hook"""
        suffix = fullname.split('.', 1)[1]
        alias = 'ipaplatform.{}.{}'.format(self.platform, suffix)
        platform_mod = importlib.import_module(alias)
        base_mod = sys.modules.get(fullname)
        if base_mod is not None:
            # module has been imported before, update its __dict__
            base_mod.__dict__.clear()
            base_mod.__dict__.update(platform_mod.__dict__)
        else:
            sys.modules[fullname] = platform_mod
        return platform_mod


metaimporter = IpaMetaImporter()
sys.meta_path.insert(0, metaimporter)

if ipaplatform.NAME is None:
    ipaplatform.NAME = metaimporter.platform
