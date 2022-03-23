"""NOTE (Marcelo Sercheli - 2021/May/06)

This config file will override some of the configurations coming from the installed
rezconfig.py (/mnt/rez/install/lib/python2.7/site-packages/rez/rezconfig.py).
For full options and explanations, please refer to the original config.
It is made available using the environment variable $REZ_CONFIG_FILE

DO NOT MODIFY THE ORIGINAL FILE IN THE INSTALL LOCATION.

Any customization should be done in this file or local rezconfig.py.
If you want to test, play around or have individual config, please
create a copy of the original in $HOME/.rezconfig directory. It will
be stronger than this one.

The original comments were preserved here for easy understanding of each option.
"""

import os


os.environ["SSE_REZ_REPO_LOCAL"] = "~/rez/local"
os.environ["SSE_REZ_REPO_RELEASE_INT"] = "/mnt/rez/release/int"
os.environ["SSE_REZ_REPO_RELEASE_EXT"] = "/mnt/rez/release/ext"

os.environ["REZ_REPO_PAYLOAD_DIR"] = "/mnt/rez/payload"


###############################################################################
# Paths
###############################################################################

# The package search path. Rez uses this to find packages. A package with the
# same name and version in an earlier path takes precedence.
packages_path = [
    "${SSE_REZ_REPO_LOCAL}", # locally installed pkgs, not yet deployed
    "${SSE_REZ_REPO_RELEASE_INT}", # internally developed pkgs, deployed
    "${SSE_REZ_REPO_RELEASE_EXT}", # external (3rd party) pkgs, such as houdini, boost
]

# The path that Rez will locally install packages to when rez-build is used
local_packages_path = "${SSE_REZ_REPO_LOCAL}"

# The path that Rez will deploy packages to when rez-release is used. For
# production use, you will probably want to change this to a site-wide location.
# NOTE (Marcelo Sercheli - 2021/May/06): this will be overriden by each package.py
release_packages_path = "${SSE_REZ_REPO_RELEASE_EXT}"

###############################################################################
# Package Caching
#
# Note: "package caching" refers to copying variant payloads to a path on local
# disk, and using those payloads instead. It is a way to avoid fetching files
# over shared storage, and is unrelated to memcached-based caching of resolves
# and package definitions as seen in the "Caching" config section.
#
###############################################################################

# Whether a package is cachable or not, if it does not explicitly state with
# the 'cachable' attribute in its package definition file. If None, defaults
# to packages' relocatability (ie cachable will == relocatable).
default_cachable = True

# If > 0, spend up to this many seconds cleaning the cache every time the cache
# is updated. This is a way to keep the cache size under control without having
# to periodically run 'rez-pkg-cache --clean'. Set to -1 to disable.
package_cache_clean_limit = 1.0

###############################################################################
# Package Resolution
###############################################################################
# Override platform values from Platform.os and arch.
# This is useful as Platform.os might show different
# values depending on the availability of lsb-release on the system.
# The map supports regular expression e.g. to keep versions.
# Please note that following examples are not necessarily recommendations.
#
#     platform_map = {
#         "os": {
#             r"Scientific Linux-(.*)": r"Scientific-\1",                 # Scientific Linux-x.x -> Scientific-x.x
#             r"Ubuntu-14.\d": r"Ubuntu-14",                              # Any Ubuntu-14.x      -> Ubuntu-14
#             r'CentOS Linux-(\d+)\.(\d+)(\.(\d+))?': r'CentOS-\1.\2', '  # Centos Linux-X.Y.Z -> CentOS-X.Y
#         },
#         "arch": {
#             "x86_64": "64bit",                                          # Maps both x86_64 and amd64 -> 64bit
#             "amd64": "64bit",
#         },
#     }
platform_map = {
    "os": {
        r'CentOS': r'centos',
    }
}

# Variant select mode. This determines which variants in a package are preferred
# during a solve. Valid options are:
# - version_priority: Prefer variants that contain higher versions of packages
#   present in the request;
# - intersection_priority: Prefer variants that contain the most number of
#   packages that are present in the request.
#
# As an example, suppose you have a package foo which has two variants:
#
#     variants = [
#         ["bar-3.0", "baz-2.1"],
#         ["bar-2.8", "burgle-1.0"]
#     ]
#
# if you do:
#
#     rez-env foo bar
#
# ...then, in either variant_select_mode, it will prefer the first variant,
# ["bar-3.0", "baz-2.1"], because it has a higher version of the first variant
# requirement (bar). However, if we instead do:
#
#     rez-env foo bar burgle
#
# ...we get different behavior. version_priority mode will still return
# ["bar-3.0", "baz-2.1"], because the first requirement's version is higher.
#
# However, intersection_priority mode will pick the second variant,
# ["bar-2.8", "burgle-1.0"], because it contains more packages that were in the
# original request (burgle).
variant_select_mode = "intersection_priority"

# Package filter. One or more filters can be listed, each with a list of
# exclusion and inclusion rules. These filters are applied to each package
# during a resolve, and if any filter excludes a package, that package is not
# included in the resolve. Here is a simple example:
#
#     package_filter:
#         excludes:
#         - glob(*.beta)
#         includes:
#         - glob(foo-*)
#
# This is an example of a single filter with one exclusion rule and one inclusion
# rule. The filter will ignore all packages with versions ending in '.beta',
# except for package 'foo' (which it will accept all versions of). A filter will
# only exclude a package iff that package matches at least one exclusion rule,
# and does not match any inclusion rule.
#
# Here is another example, which excludes all beta packages, and all packages
# except 'foo' that are released after a certain date. Note that in order to
# use multiple filters, you need to supply a list of dicts, rather than just a
# dict:
#
#     package_filter:
#     - excludes:
#       - glob(*.beta)
#     - excludes:
#       - after(1429830188)
#       includes:
#       - foo  # same as range(foo), same as glob(foo-*)
#
# This example shows why multiple filters are supported - with only one filter,
# it would not be possible to exclude all beta packages (including foo), but also
# exclude all packages after a certain date, except for foo.
#
# Following are examples of all the possible rules:
#
# example             | description
# --------------------|----------------------------------------------------
# glob(*.beta)        | Matches packages matching the glob pattern.
# regex(.*-\\.beta)   | Matches packages matching re-style regex.
# requirement(foo-5+) | Matches packages within the given requirement.
# before(1429830188)  | Matches packages released before the given date.
# after(1429830188)   | Matches packages released after the given date.
# *.beta              | Same as glob(*.beta)
# foo-5+              | Same as range(foo-5+)
package_filter = {
    "excludes": [
        "glob(*.dev)"
    ]
}

# If True, unversioned packages are allowed. Solve times are slightly better if
# this value is False.
allow_unversioned_packages = False

###############################################################################
# Environment Resolution
###############################################################################

# Rez's default behaviour is to overwrite variables on first reference. This
# prevents unconfigured software from being used within the resolved environment.
# For example, if PYTHONPATH were to be appended to and not overwritten, then
# python modules from the parent environment would be (incorrectly) accessible
# within the Rez environment.
#
# "Parent variables" override this behaviour - they are appended/prepended to,
# rather than being overwritten. If you set "all_parent_variables" to true, then
# all variables are considered parent variables, and the value of "parent_variables"
# is ignored. Be aware that if you make variables such as PATH, PYTHONPATH or
# app plugin paths parent variables, you are exposing yourself to potentially
# incorrect behaviour within a resolved environment.
all_parent_variables = False

parent_variables = [
    "SSE_SG_POST_DATA",
    # Shotgrid Desktop App sets this before rez-env runs
    'PYTHONPATH',  # Shotgrid Desktop App is clearing this before polluting
    'SHOTGUN_SITE',
    'SHOTGUN_ENTITY_ID',
    'SHOTGUN_BUNDLE_CACHE_FALLBACK_PATHS',
    'MAYA_MODULE_PATH',
    'SGTK_LOAD_MAYA_PLUGINS',
    'SHOTGUN_ENTITY_TYPE',
]

###############################################################################
# Package Build/Release
###############################################################################

# The default working directory for a package build, either absolute path or
# relative to the package source directory (this is typically where temporary
# build files are written).
build_directory = "_rez_build"

###############################################################################
# Rez-1 Compatibility
###############################################################################

# Warn or disallow when a package is found to contain old rez-1-style commands.
warn_old_commands = True
error_old_commands = True

# If True, Rez will continue to generate the given environment variables in
# resolved environments, even though their use has been deprecated in Rez-2.
# The variables in question, and their Rez-2 equivalent (if any) are:
#
# REZ-1              | REZ-2
# -------------------|-----------------
# REZ_REQUEST        | REZ_USED_REQUEST
# REZ_RESOLVE        | REZ_USED_RESOLVE
# REZ_VERSION        | REZ_USED_VERSION
# REZ_PATH           | REZ_USED
# REZ_RESOLVE_MODE   | not set
# REZ_RAW_REQUEST    | not set
# REZ_IN_REZ_RELEASE | not set
rez_1_environment_variables = False

# If True, Rez will continue to generate the given CMake variables at build and
# release time, even though their use has been deprecated in Rez-2.  The
# variables in question, and their Rez-2 equivalent (if any) are:
#
# REZ-1   | REZ-2
# --------|---------------
# CENTRAL | REZ_BUILD_TYPE
rez_1_cmake_variables = False

# If True, override all compatibility-related settings so that Rez-1 support is
# deprecated. This means that:
# * All warn/error settings in this section of the config will be set to
#   warn=False, error=True;
# * rez_1_environment_variables will be set to False.
# * rez_1_cmake_variables will be set to False.
# You should aim to do this - it will mean your packages are more strictly
# validated, and you can more easily use future versions of Rez.
disable_rez_1_compatibility = True

###############################################################################
###############################################################################
# GUI
###############################################################################
###############################################################################

# All of the settings listed here onwards apply only to the GUIs available in
# the "rezgui" part of the module.

# Setting either of these options to true will force rez to select that qt
# binding. If both are false, the qt binding is detected. Setting both to true
# will cause an error.
use_pyside = False
use_pyqt = False

# The number of threads a build system should use, eg the make '-j' option.
# If the string values "logical_cores" or "physical_cores", it is set to the
# detected number of logical / physical cores on the host system.
# (Logical cores are the number of cores reported to the OS, physical are the
# number of actual hardware processor cores.  They may differ if, ie, the CPUs
# support hyperthreading, in which case logical_cores == 2 * physical_cores).
# This setting is exposed as the environment variable $REZ_BUILD_THREAD_COUNT
# during builds.
# NOTE: (Marcelo Sercheli): `make` in CentOS 7 seams to hang in random steps
# during a build if the job count is > 1.
build_thread_count = 1