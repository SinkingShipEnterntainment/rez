"""
rez-config

rez is a tool for managing package configuration.

'package': a unit of software, or configuration information, which is
installed under a common base path, and may be available as several
variants. A specific version of software is regarded as a package - ie,
'boost' is not a package, but 'boost-1.36' is.

'package family': label for a family of versioned packages. 'boost' is a
package family, whereas 'boost-1.36' is a package.

'package base path': The path under which all variants of a package are
installed. For example, boost-1.36 and its variants might be found under
'/server/boost/1.36/'.

NOTES
---------
'Dependency transitivity' is the assumption that if a package A has a dependent
package B, then an earlier versioned A will have a dependency on an equal or
earlier version of B. For example, given the relationship:
A-3.5 dependsOn B-6.4
then we assume that:
A-3.4 dependsOn B-<=6.4

It follows that we also assume that a later version of A will have a dependency
on an equal or later version of B:
A-3.5 dependsOn B-6.4
then we assume that:
A-3.6 dependsOb B->=6.4

Examples of cases where this assumption is wrong are:
let:
A-3.5 dependsOn B-6.4
then the following cases break the assumption:
'A-3.4 dependsOn B-7.0'
'A-3.4 dependsOn B' (since 'B' is the superset of all versions of B)
'A-3.4 NOT dependsOn B'
"""

import os
import time
import sys
import random
import itertools
from packages import ResolvedPackage, split_name, package_in_range, package_family, iter_packages_in_range
from versions import *
from public_enums import *
from rez_exceptions import *
from rez_metafile import *
from rez_memcached import *
import rez_filesys
import rez_util



##############################################################################
# Public Classes
##############################################################################

class PackageRequest(object):
	"""
	A request for a package. 
	
	Parameters
	----------
	name : str
		name of the package.
		If the package name starts with '!', then this is an ANTI-package request -
		ie, a requirement that this package, in this version range, is not allowed.
		This feature exists so that packages can describe conflicts with other packages,
		that can't be described by conflicting dependencies.
		If the package name starts with '~' then this is a WEAK package request. It
		means, "I don't need this package, but if it exists then it must fall within
		this version range." A weak request is actually converted to a normal anti-
		package: eg, "~foo-1.3" is equivalent to "!foo-0+<1.3|1.4+".
	version_range : str
		may be inexact (for eg '5.4+')
	latest : bool or None
		If None, resolving the package on disk is delayed until later. Otherwise,
		the request will immediately attempt to resolve, and sorted based on
		the value of 'latest': if True, the package with the latest version is
		returned, otherwise, the earliest.
	"""
	def __init__(self, name, version_range, latest=True, timestamp=0):
		self.name = name

		self.version_range = VersionRange(version_range)
		if self.is_weak():
			# convert into an anti-package
			self.version_range = self.version_range.get_inverse()
			self.name = anti_name(self.name)
		self.timestamp = timestamp
		self.latest = latest
		self.version = str(self.version_range)

	def is_anti(self):
		return (self.name[0] == '!')

	def is_weak(self):
		return (self.name[0] == '~')

	def short_name(self):
		if (len(self.version) == 0):
			return self.name
		else:
			return self.name + '-' + self.version

	def __str__(self):
		return str((self.name, self.version))

	def __repr__(self):
		return '%s(%r, %r)' % (self.__class__.__name__, self.name, self.version)

class PackageConflict(object):
	"""
	A package conflict. This can occur between a package (possibly a specific
	variant) and a package request
	"""
	def __init__(self, pkg_req_conflicting, pkg_req, variant = None ):
		self.pkg_req = pkg_req
		self.pkg_req_conflicting = pkg_req_conflicting
		self.variant = variant

	def __str__(self):
		tmpstr = str(self.pkg_req)
		if self.variant:
			tmpstr += " variant:" + str(self.variant)
		tmpstr += " <--!--> " + str(self.pkg_req_conflicting)
		return tmpstr

class Resolver(object):
	"""
	Where all the action happens. This class performs a package resolve.
	"""
	def __init__(self, resolve_mode, quiet=False, verbosity=0, max_fails=-1, time_epoch=0,
		build_requires=False, assume_dt=False, caching=True):
		"""
		resolve_mode: one of: RESOLVE_MODE_EARLIEST, RESOLVE_MODE_LATEST
		quiet: if True then hides unnecessary output (such as the progress dots)
		verbosity: print extra debugging info. One of: 0, 1, 2
		max_fails: return after N failed configuration attempts, default -1 (no limit)
		time_epoch: ignore packages newer than this time-date. Default = 0 which is a special
			case, meaning do not ignore any packages
		assume_dt: Assume dependency transitivity
		caching: If True, resolve info is read from and written to a memcache daemon if possible.
		"""
		if not time_epoch:
			time_epoch = int(time.time())

		self.rctxt = _ResolvingContext()
		self.rctxt.resolve_mode = resolve_mode
		self.rctxt.verbosity = verbosity
		self.rctxt.max_fails = max_fails
		self.rctxt.quiet = quiet
		self.rctxt.build_requires = build_requires
		self.rctxt.assume_dt = assume_dt
		self.rctxt.time_epoch = time_epoch
		self.rctxt.caching = caching

	def guarded_resolve(self, pkg_req_strs, no_os=False, no_path_append=False, is_wrapper=False,
		meta_vars=None, shallow_meta_vars=None, dot_file=None, print_dot=False):
		"""
		Just a wrapper for resolve() which does some command-line friendly stuff and has some 
		extra options for convenience.
		@return None on failure, same as resolve() otherwise.
		"""
		try:
			result = self.resolve(pkg_req_strs, no_os, no_path_append, is_wrapper, \
				meta_vars, shallow_meta_vars)

		except PkgSystemError, e:
			sys.stderr.write(str(e)+'\n')
			return None
		except VersionError, e:
			sys.stderr.write(str(e)+'\n')
			return None
		except PkgFamilyNotFoundError, e:
			sys.stderr.write("Could not find the package family '" + e.family_name + "'\n")
			return None
		except PkgNotFoundError, e:
			sys.stderr.write("Could not find the package '" + e.pkg_req.short_name() + "'\n")
			return None
		except PkgConflictError, e:
			sys.stderr.write("The following conflicts occurred:\n")
			for c in e.pkg_conflicts:
				sys.stderr.write(str(c)+'\n')

			# we still produce a dot-graph on failure
			if e.last_dot_graph:
				if dot_file:
					rez_util.gen_dotgraph_image(e.last_dot_graph, dot_file)
				if print_dot:
					print(e.last_dot_graph)
			return None
		except PkgsUnresolvedError, e:
			sys.stderr.write("The following packages could not be resolved:\n")
			for p in e.pkg_reqs:
				sys.stderr.write(str(p)+'\n')
			return None
		except PkgCommandError, e:
			sys.stderr.write("There was a problem with the resolved command list:\n")
			sys.stderr.write(str(e)+'\n')
			return None
		except PkgCyclicDependency, e:
			sys.stderr.write("\nCyclic dependency(s) were detected:\n")
			sys.stderr.write(str(e) + "\n")

			import tempfile
			# write graphs to file
			tmpf = tempfile.mkstemp(suffix='.dot')
			os.write(tmpf[0], str(e))
			os.close(tmpf[0])
			sys.stderr.write("\nThis graph has been written to:\n")
			sys.stderr.write(tmpf[1] + "\n")

			tmpf = tempfile.mkstemp(suffix='.dot')
			os.write(tmpf[0], e.dot_graph)
			os.close(tmpf[0])
			sys.stderr.write("\nThe whole graph (with cycles highlighted) has been written to:\n")
			sys.stderr.write(tmpf[1] + "\n")

			# we still produce a dot-graph on failure
			if dot_file:
				rez_util.gen_dotgraph_image(e.dot_graph, dot_file)
			if print_dot:
				print(e.dot_graph)

			return None

		except PkgConfigNotResolvedError, e:
			sys.stderr.write("The configuration could not be resolved:\n")
			for p in e.pkg_reqs:
				sys.stderr.write(str(p)+'\n')
			sys.stderr.write("The failed configuration attempts were:\n")
			for s in e.fail_config_list:
				sys.stderr.write(s+'\n')

			# we still produce a dot-graph on failure
			if dot_file:
				rez_util.gen_dotgraph_image(e.last_dot_graph, dot_file)
			if print_dot:
				print(e.last_dot_graph)

			return None

		pkg_res_list, env_cmds, dot_graph, nfails = result

		if print_dot:
			print(dot_graph)

		if dot_file:
			rez_util.gen_dotgraph_image(dot_graph, dot_file)

		return result

	def resolve(self, pkg_reqs, no_os=False, no_path_append=False, is_wrapper=False,
		meta_vars=None, shallow_meta_vars=None):
		"""
		Perform a package resolve.
		Parameters
		----------
		pkg_reqs: list of str or PackageRequest
			packages to resolve into a configuration
		no_os: bool
			whether to include the OS package.
		no_path_append: bool
			whether to append OS-specific paths to PATH when printing an environment
		is_wrapper: bool
			If this env is being resolved for a wrapper, then some very slight changes
			are needed to a normal env, so that wrappers can see one another.
		meta_vars: list of str
			each string is a key whos value will be saved into an
			env-var named REZ_META_<KEY> (lists are comma-separated).
		shallow_meta_vars: list of str
			same as meta-vars, but only the values from those packages directly
			requested are baked into the env var REZ_META_SHALLOW_<KEY>.
		@returns
		(a) a list of ResolvedPackage objects, representing the resolved config;
		(b) a list of commands which, when run, should configure the environment;
		(c) a dot-graph representation of the config resolution, as a string;
		(d) the number of failed config attempts before the successful one was found
		-OR-
		raise the relevant exception, if config resolution is not possible
		"""
		if not no_os:
			os_pkg_req = str_to_pkg_req(rez_filesys._g_os_pkg, self.rctxt.time_epoch, self.rctxt.resolve_mode==RESOLVE_MODE_LATEST)
			pkg_reqs = [os_pkg_req] + pkg_reqs

		if not pkg_reqs:
			return ([], [], "digraph g{}", 0)

		pkg_reqs = [pkg_request(x, self.rctxt.time_epoch, self.rctxt.resolve_mode==RESOLVE_MODE_LATEST) for x in pkg_reqs]
		# get the resolve, possibly read/write cache
		result = self.get_cached_resolve(pkg_reqs)
		if not result:
			result = self.resolve_base(pkg_reqs)
			self.set_cached_resolve(pkg_reqs, result)

		env_cmds = []

		if not is_wrapper:
			env_cmds.append("export REZ_IN_WRAPPER=")
			env_cmds.append("export REZ_WRAPPER_PATH=")

		pkg_res_list, env_cmds_, dot_graph, nfails = result
		env_cmds_ = env_cmds_[:]

		# we need to inject system paths here. They're not there already because they can't be cached
		sys_paths = [os.environ["REZ_PATH"]+"/bin"]
		if not no_path_append:
			sys_paths += rez_filesys._g_os_paths

		sys_paths_added = ("export PATH=" in env_cmds_)
		if sys_paths_added:
			i = env_cmds_.index("export PATH=")
			env_cmds_[i] = "export PATH=%s" % str(':').join(sys_paths)

		env_cmds += env_cmds_

		# add wrapper stuff
		if is_wrapper:
			env_cmds.append("export REZ_IN_WRAPPER=1")
			env_cmds.append("export PATH=$PATH:$REZ_WRAPPER_PATH")

		# add meta env vars
		pkg_req_fam_set = set([x.name for x in pkg_reqs if not x.is_anti()])
		meta_envvars = {}
		shallow_meta_envvars = {}

		for pkg_res in pkg_res_list:
			def _add_meta_vars(mvars, target):
				for key in mvars:
					if key in pkg_res.stripped_metadata:
						val = pkg_res.stripped_metadata[key]
						if isinstance(val, list):
							val = ','.join(val)
						if key not in target:
							target[key] = []
						target[key].append(pkg_res.name + ':' + val)

			if meta_vars:
				_add_meta_vars(meta_vars, meta_envvars)

			if shallow_meta_vars and pkg_res.name in pkg_req_fam_set:
				_add_meta_vars(shallow_meta_vars, shallow_meta_envvars)

		for k,v in meta_envvars.iteritems():
			env_cmds.append("export REZ_META_" + k.upper() + "='" + str(' ').join(v) + "'")
		for k,v in shallow_meta_envvars.iteritems():
			env_cmds.append("export REZ_META_SHALLOW_" + k.upper() + "='" + str(' ').join(v) + "'")

		# this here for backwards compatibility
		if not sys_paths_added:
			env_cmds.append("export PATH=$PATH:%s" % str(':').join(sys_paths))

		return pkg_res_list, env_cmds, dot_graph, nfails

	def resolve_base(self, pkg_reqs):
		config = _Configuration(self.rctxt)
		full_req_str = ' '.join([x.short_name() for x in pkg_reqs])

		for pkg_req in pkg_reqs:
			config.add_package(pkg_req)

		for pkg_req in pkg_reqs:
			name = pkg_req.short_name()
			if name.startswith("__wrapper_"):
				name2 = name.replace("__wrapper_", "")
				config.add_dot_graph_verbatim('"' + name +
					'" [label="%s" style="filled" shape=folder fillcolor="rosybrown1"] ;' \
					% (name2))
			else:
				config.add_dot_graph_verbatim('"' + name +
					'" [style=filled shape=box fillcolor="rosybrown1"] ;')

		if (self.rctxt.verbosity != 0):
			print
			print "initial config:"
		if (self.rctxt.verbosity == 1):
			print str(config)
		elif (self.rctxt.verbosity == 2):
			config.dump()

		# do the config resolve - all the action happens here!
		pkg_res_list = config.resolve_packages()

		# color resolved packages in graph
		for pkg_res in pkg_res_list:
			config.add_dot_graph_verbatim('"' + pkg_res.short_name() + \
				'" [style=filled fillcolor="darkseagreen1"] ;')

		if (self.rctxt.verbosity != 0):
			print
			print "final config:"
		if (self.rctxt.verbosity == 1):
			print str(config)
			print
		elif (self.rctxt.verbosity == 2):
			config.dump()
			print

		# build the environment commands
		env_cmds = []
		res_pkg_strs = [x.short_name() for x in pkg_res_list]

		# special case env-vars
		env_cmds.append("export PATH=")
		env_cmds.append("export REZ_USED=" + rez_filesys._g_rez_path)
		env_cmds.append("export REZ_PREV_REQUEST=$REZ_REQUEST")
		env_cmds.append("export REZ_REQUEST='" + full_req_str + "'")
		env_cmds.append("export REZ_RAW_REQUEST='" + full_req_str + "'")
		env_cmds.append("export PYTHONPATH=%s/python" % rez_filesys._g_rez_path)
		env_cmds.append("export REZ_RESOLVE='"+ str(" ").join(res_pkg_strs)+"'")
		env_cmds.append("export REZ_RESOLVE_MODE=" + self.rctxt.resolve_mode)
		env_cmds.append("export REZ_FAILED_ATTEMPTS=" + str(len(self.rctxt.config_fail_list)) )
		env_cmds.append("export REZ_REQUEST_TIME=" + str(self.rctxt.time_epoch))

		# packages: base/root/version, and commands
		env_cmds.append("#### START of package commands ####")

		for pkg_res in pkg_res_list:
			env_cmds.append("# Commands from package %s" % pkg_res.name)

			prefix = "REZ_" + pkg_res.name.upper()
			env_cmds.append("export " + prefix + "_VERSION=" + pkg_res.version)
			env_cmds.append("export " + prefix + "_BASE=" + pkg_res.base)
			env_cmds.append("export " + prefix + "_ROOT=" + pkg_res.root)

			if pkg_res.commands:
				for cmd in pkg_res.commands:
					env_cmds.append([cmd, pkg_res.short_name()])

		env_cmds.append("#### END of package commands ####")

		# process the commands
		env_cmds = process_commands(env_cmds)

		# build the dot-graph representation
		dot_graph = config.get_dot_graph_as_string()

		# here we remove unnecessary data, because if caching is on then it's gonna be sent over
		# the network, and we want to minimise traffic.
		for pkg_res in pkg_res_list:
			pkg_res.strip()

		result = (pkg_res_list, env_cmds, dot_graph, len(self.rctxt.config_fail_list))

		# we're done
		return result

	def set_cached_resolve(self, pkg_reqs, result):
		if not get_memcache().caching_enabled():
			return

		# if any local packages are involved, don't cache
		pkg_res_list = result[0]
		for pkg_res in pkg_res_list:
			if pkg_res.base.startswith(rez_filesys._g_local_pkgs_path):
				return

		get_memcache().store_resolve(rez_filesys._g_syspaths_nolocal, pkg_reqs,
									 result, self.rctxt.time_epoch)

	def get_cached_resolve(self, pkg_reqs):
		# the 'cache timestamp' is the most recent timestamp of all the resolved packages. Between
		# here and rctxt.time_epoch, the resolve will be the same.
		if not get_memcache().caching_enabled():
			return None

		result, cache_timestamp = get_memcache().get_resolve(
			rez_filesys._g_syspaths_nolocal, pkg_reqs, self.rctxt.time_epoch)
		
		if not result:
			return None

		pkg_res_list = result[0]

		# discard cache if any version of any resolved pkg is also present as a local pkg,
		# unless the versions fall outside of that pkg's max bounds.
		if rez_filesys._g_local_pkgs_path in rez_filesys._g_syspaths:
			for pkg_res in pkg_res_list:
				fam_path = os.path.join(rez_filesys._g_local_pkgs_path, pkg_res.name)
				if os.path.isdir(fam_path):
					# todo max bounds check
					print_cache_warning(("Presence of local package directory %s " + \
						"caused cache miss") % fam_path)
					return None

		"""
		# if any version of any resolved packages also appear in a local package path, and that 
		# path has been modified since the cache timestamp, then discard the cached resolve.
		# TODO incorrect, time has no effect. Can only discard based on 'pkg max bounds'
		if rez_filesys._g_local_pkgs_path in rez_filesys._g_syspaths:
			for pkg_res in pkg_res_list:
				fam_path = os.path.join(rez_filesys._g_local_pkgs_path, pkg_res.name)
				if os.path.isdir(fam_path):
					path_modtime = int(os.path.getmtime(fam_path))
					if path_modtime >= cache_timestamp:
						print >> sys.stderr, "LOCAL package forced no cache resolve!"
						return None
		"""

		env_cmds = result[1]
		env_cmds.append("export REZ_RESOLVE_FROM_CACHE=1")
		env_cmds.append("export REZ_CACHE_TIMESTAMP=%d" % cache_timestamp)

		return result


##############################################################################
# Public Functions
##############################################################################

def parse_pkg_req_str(pkg_str):
	"""
	Helper function: parses a package request string (eg 'boost-1.36').
	Note that a version string ending in '=e','=l' will result in a package request
	that immediately resolves to earliest/latest version.
	"""
	if pkg_str.endswith("=l"):
		latest = True
	elif pkg_str.endswith("=e"):
		latest = False
	else:
		latest = None
	pkg_str = pkg_str.rsplit("=", 1)[0]
	name, verrange = split_name(pkg_str)
	return name, verrange, latest

def pkg_request(req, timestamp, latest=True):
	"""
	Helper function: turns a package string (eg 'boost-1.36') into a `PackageRequest`.
	Note that a version string ending in '=e','=l' will result in a package request
	that immediately resolves to earliest/latest version.
	"""
	if isinstance(req, PackageRequest):
		return req
	return str_to_pkg_req(req, timestamp, latest)

def str_to_pkg_req(pkg_str, timestamp, latest=True):
	"""
	Helper function: turns a package string (eg 'boost-1.36') into a `PackageRequest`.
	Note that a version string ending in '=e','=l' will result in a package request
	that immediately resolves to earliest/latest version.
	"""
	name, verrange, latest_override = parse_pkg_req_str(pkg_str)
	if latest_override is not None:
		# goto filesystem and resolve version immediately
		name_ = name.lstrip('!')

		pkg = package_in_range(name_, verrange, latest=latest_override,
							   timestamp=timestamp)

		if pkg is None:
			raise PkgsUnresolvedError([PackageRequest(name, verrange)])

		verrange = VersionRange(_versions=[pkg.version])
		latest = latest_override
	return PackageRequest(name, verrange, latest, timestamp)

def anti_name(pkg):
	"""
	Return the name of the anti-package for the given package.
	
	pkg may be a PackageRequest, _Package, or string
	"""
	if isinstance(pkg, (PackageRequest, _Package)):
		name = pkg.name
	else:
		name = pkg
	if name[0] == '!':
		raise RezError("Already an anti-package: %r" % name)
	if name[0] == '~':
		return '!' + name[1:]
	return '!' + name

def get_pkg(pkg_str):
	"""
	Return a `Package` instance for the given package string
	"""
	# TODO: prevent anti and weak package strings?
	name, verrange, latest = parse_pkg_req_str(pkg_str)
	latest = True if latest is None else latest
	pkg = package_in_range(name, VersionRange(verrange), latest)
	if not pkg:
		raise PkgNotFoundError(pkg_str)

	return pkg

def make_random_color_string():
	cols = []
	cols.append(random.randint(0,255))
	cols.append(random.randint(0,255))
	cols.append(random.randint(0,255))
	if(cols[0]+cols[1]+cols[2] > 400):
		cols[random.randint(0,2)] = random.randint(0,100)
	s = "#"
	for c in cols:
		h = hex(c)[2:]
		if len(h) == 1:
			h = '0' + h
		s = s + h
	return s


##############################################################################
# Internal Classes
##############################################################################

class _ResolvingContext(object):
	"""
	Resolving context
	"""
	def __init__(self):
		self.resolve_mode = RESOLVE_MODE_NONE
		self.verbosity = 0
		self.max_fails = -1
		self.config_fail_list = []
		self.config_uid = 0
		self.last_fail_dot_graph = None
		self.time_epoch = 0
		self.quiet = False
		self.build_requires = False
		self.assume_dt = False

class _PackageVariant(object):
	"""
	A package variant. The 'working list' member is a list of dependencies that are
	removed during config resolution - a variant with an empty working_list is fully
	resolved. This class has been written with foward compatibility in mind - currently
	a variant is just a list of dependencies, but it may later become a dict, with
	more info than just dependencies.
	"""
	def __init__(self, metadata_node, _working_list=None):
		self.metadata = metadata_node
		if _working_list is not None:
			self.working_list = _working_list[:]
		elif type(self.metadata) == list:
			self.working_list = self.metadata[:]
		else:
			raise PkgSystemError("malformed variant metadata: " + str(self.metadata))

	def copy(self):
		return _PackageVariant(self.metadata, self.working_list)

	def __str__(self):
		return str(self.metadata)


class _Package(object):
	"""
	Internal package representation
	"""
	def __init__(self, pkg_req):
		self.is_transitivity = False
		self.has_added_transitivity = False
		self.base_path = None
		self.metadata = None
		self.variants = None
		self.root_path = None
		self.timestamp = None
		if pkg_req:
			self.name = pkg_req.name
			self.version_range = pkg_req.version_range
			self.pkg_req = pkg_req
			self.pkg_iter = iter_packages_in_range(self.name, self.version_range,
												   self.pkg_req.latest,
												   self.pkg_req.timestamp)
			if not self.is_anti() and not package_family(self.name):
				raise PkgFamilyNotFoundError(self.name)
		else:
			self.name = None
			self.version_range = None
			self.pkg_req = None
			self.pkg_iter = None

	def copy(self, skip_version_range=False):
		p = _Package(None)
		p.is_transitivity = self.is_transitivity
		p.has_added_transitivity = self.has_added_transitivity
		p.name = self.name
		p.base_path = self.base_path
		p.root_path = self.root_path
		p.metadata = self.metadata
		p.timestamp = self.timestamp
		p.pkg_req = self.pkg_req
		# split the iterator
		self.pkg_iter, p.pkg_iter = itertools.tee(self.pkg_iter)
		if not skip_version_range:
			p.version_range = self.version_range.copy()

		p.variants = None
		if self.variants is not None:
			p.variants = [x.copy() for x in self.variants]
		return p

	def next_request(self):
		try:
			pkg = next(self.pkg_iter)
			return PackageRequest(pkg.name, pkg.version,
								  self.pkg_req.latest,
								  self.pkg_req.timestamp)
		except StopIteration:
			return None

	def get_variants(self):
		"""
		Return package variants, if any
		"""
		return self.variants

	def as_package_request(self):
		"""
		Return this package as a package-request
		"""
		return self.pkg_req

	def is_anti(self):
		"""
		Return True if this is an anti-package
		"""
		return (self.name[0] == '!')

	def short_name(self):
		"""
		Return a short string representation, eg 'boost-1.36'
		"""
		if self.version_range.is_any():
			return self.name
		else:
			return self.name + '-' + str(self.version_range)

		return self.name + '-' + str(self.version_range)

	def is_metafile_resolved(self):
		"""
		Return True if this package has had its metafile resolved
		"""
		return (self.base_path != None)

	def is_resolved(self):
		"""
		Return True if this package has been resolved (ie, there are either no
		variants, or a specific variant has been chosen)
		"""
		return (self.root_path != None)

	def resolve(self, root_path):
		"""
		Resolve this package, ie set its root path

		.. todo::
			 optimisation: just do this right at the end of resolve_packages
		"""
		self.root_path = root_path

	# Get commands with string-replacement
	def get_resolved_commands(self):
		"""
		NOTE: this is deprecated with the addition of the python rex execution language

		Get commands with string replacement
		"""
		if self.is_resolved() and self.metadata.commands:
			version = str(self.version_range)
			vernums = version.split('.') + ['', '']
			major_version = vernums[0]
			minor_version = vernums[1]
			user = os.getenv("USER", "UNKNOWN_USER")

			new_cmds = []
			for cmd in self.metadata.commands:
				cmd = cmd.replace("!VERSION!", version)
				cmd = cmd.replace("!MAJOR_VERSION!", major_version)
				cmd = cmd.replace("!MINOR_VERSION!", minor_version)
				cmd = cmd.replace("!BASE!", self.base_path)
				cmd = cmd.replace("!ROOT!", self.root_path)
				cmd = cmd.replace("!USER!", user)
				new_cmds.append(cmd)
			return new_cmds
		else:
			return None

	def get_package(self, latest=True, exact=False, timestamp=0):
		return package_in_range(self.name, self.version_range,
								timestamp=timestamp,
								latest=latest, exact=exact)

	def resolve_metafile(self, timestamp=0):
		"""
		attempt to resolve the metafile, the metadata member will be set if
		successful, and True will be returned. If the package has no variants,
		then its root-path is set and this package is regarded as fully-resolved.
		"""
		is_any = self.version_range.is_any()
		if not is_any and self.version_range.is_inexact():
			return False

		if not self.base_path:
			pkg = self.get_package(exact=True, timestamp=timestamp)
			if pkg is not None:
				self.timestamp = pkg.timestamp
				self.base_path = pkg.base
				self.metadata = pkg.stripped_metadata
				metafile_variants = self.metadata.variants
				if metafile_variants:
					# convert variants from metafile into _PackageVariants
					self.variants = []
					for metavar in metafile_variants:
						pkg_var = _PackageVariant(metavar)
						self.variants.append(pkg_var)
				else:
					# no variants, we're fully resolved
					self.resolve(self.base_path)

		return (self.base_path != None)

	def get_metadata(self, latest=True, timestamp=0):
		pkg = self.get_package(latest=latest, exact=False, timestamp=timestamp)

		if not pkg:
			return
		return pkg.stripped_metadata

	def __str__(self):
		l = [ self.short_name() ]
		if self.root_path:
			l.append('R' + self.root_path)
		elif self.base_path:
			l.append('B' + self.base_path)
		if(self.is_transitivity):
			l.append('t')

		variants = self.get_variants()
		if (variants):
			vars = []
			for var in variants:
				vars.append(var.working_list)
			l.append("working_vars:" + str(vars))
		return str(l)




class _Configuration(object):
	"""
	Internal configuration representation
	"""
	def __init__(self, rctxt, inc_uid = False):
		# resolving context
		self.rctxt = rctxt
		# packages map, for quick lookup
		self.pkgs = {}
		# packages list, for order retention wrt resolving
		self.families = []
		# connections in a dot graph
		self.dot_graph = []
		# uid
		if inc_uid:
			rctxt.config_uid += 1
		self.uid = rctxt.config_uid

	def get_num_packages(self):
		"""
		return number of packages
		"""
		num = 0
		for name,pkg in self.pkgs.iteritems():
			if not pkg.is_anti():
				num += 1
		return num

	def get_num_resolved_packages(self):
		"""
		return number of resolved packages
		"""
		num = 0
		for name,pkg in self.pkgs.iteritems():
			if pkg.is_resolved():
				num += 1
		return num

	def all_resolved(self):
		"""
		returns True if all packages are resolved
		"""
		return (self.get_num_resolved_packages() == self.get_num_packages())

	ADDPKG_CONFLICT 	= 0
	ADDPKG_ADD 			= 1
	ADDPKG_NOEFFECT		= 2

	def test_pkg_req_add(self, pkg_req, create_pkg_add):
		"""
		test the water to see what adding a package request would do to the config.
		
		Returns an ADDPKG_* constant and a _Package instance (or None).

		Possible results are:

		- (ADDPKG_CONFLICT, pkg_conflicting):
			The package cannot be added because it would conflict with
			pkg_conflicting
		- (ADDPKG_NOEFFECT, None):
			The package doesn't need to be added, there is an identical package
			already there
		- (ADDPKG_ADD, pkg_add):
			The package can be added, and the config updated accordingly by
			adding pkg_add (replacing a package with the same family name if it
			already exists in the config)

		.. note::
			that if 'create_pkg_add' is False, then 'pkg_add' will always be None.
		"""

		# do a shortcut and test pkg short-names, if they're identical then we can often
		# return 'NOEFFECT'. Sometimes short names can mismatch, but actually be identical,
		# but this is of no real consequence, and testing on short-name is a good optimisation
		# (testing VersionRanges for equality is not trivial)
		pkg_shortname = pkg_req.short_name()

		pkg_req_ver_range = pkg_req.version_range

		if pkg_req.is_anti():

			if pkg_req.name[1:] in self.pkgs:
				config_pkg = self.pkgs[pkg_req.name[1:] ]

				# if anti and existing non-anti don't overlap then no effect
				ver_range_intersect = config_pkg.version_range.get_intersection(pkg_req_ver_range)
				if not ver_range_intersect:
					return (_Configuration.ADDPKG_NOEFFECT, None)

				# if (inverse of anti) and non-anti intersect, then reduce existing non-anti,
				# otherwise there is a conflict
				pkg_req_inv_ver_range = pkg_req_ver_range.get_inverse()
				ver_range_intersect = config_pkg.version_range.get_intersection(pkg_req_inv_ver_range)
				if ver_range_intersect:
					pkg_add = None
					if create_pkg_add:
						pkg_add = config_pkg.copy(True)
						pkg_add.version_range = ver_range_intersect
						return (_Configuration.ADDPKG_ADD, pkg_add)
				else:
					return (_Configuration.ADDPKG_CONFLICT, config_pkg)

			# union with anti if one already exists
			if pkg_req.name in self.pkgs:
				config_pkg = self.pkgs[pkg_req.name]
				if (config_pkg.short_name() == pkg_shortname):
					return (_Configuration.ADDPKG_NOEFFECT, None)

				ver_range_union = config_pkg.version_range.get_union(pkg_req_ver_range)
				pkg_add = None
				if create_pkg_add:
					pkg_add = config_pkg.copy(True)
					pkg_add.version_range = ver_range_union
				return (_Configuration.ADDPKG_ADD, pkg_add)
		else:
			try:
				config_pkg = self.pkgs[anti_name(pkg_req)]
			except KeyError:
				# does not exist. move on
				pass
			else:
				# if non-anti and existing anti don't overlap then pkg can be added
				ver_range_intersect = config_pkg.version_range.get_intersection(pkg_req_ver_range)
				if not ver_range_intersect:
					pkg_add = None
					if create_pkg_add:
						pkg_add = _Package(pkg_req)
					return (_Configuration.ADDPKG_ADD, pkg_add)

				# if non-anti and (inverse of anti) intersect, then add reduced anti,
				# otherwise there is a conflict
				config_pkg_inv_ver_range = config_pkg.version_range.get_inverse()
				ver_range_intersect = config_pkg_inv_ver_range.get_intersection(pkg_req_ver_range)
				if ver_range_intersect:
					pkg_add = None
					if create_pkg_add:
						pkg_add = _Package(pkg_req)
						pkg_add.version_range = ver_range_intersect
						return (_Configuration.ADDPKG_ADD, pkg_add)
				else:
					return (_Configuration.ADDPKG_CONFLICT, config_pkg)

			# intersect with non-anti if one already exists, and conflict if no intersection
			if pkg_req.name in self.pkgs:
				config_pkg = self.pkgs[pkg_req.name]
				if (config_pkg.short_name() == pkg_shortname):
					return (_Configuration.ADDPKG_NOEFFECT, None)

				ver_range_intersect = config_pkg.version_range.get_intersection(pkg_req_ver_range)
				if ver_range_intersect:
					pkg_add = None
					if create_pkg_add:
						pkg_add = config_pkg.copy(True)
						pkg_add.version_range = ver_range_intersect
					return (_Configuration.ADDPKG_ADD, pkg_add)
				else:
					return (_Configuration.ADDPKG_CONFLICT, config_pkg)

		# package can be added directly, doesn't overlap with anything
		pkg_add = None
		if create_pkg_add:
			pkg_add = _Package(pkg_req)
		return (_Configuration.ADDPKG_ADD, pkg_add)

	def get_conflicting_package(self, pkg_req):
		"""
		return a package in the current configuration that 'pkg' would conflict with, or
		None if no conflict would occur
		"""
		result, pkg_conflict = self.test_pkg_req_add(pkg_req, False)
		if (result == _Configuration.ADDPKG_CONFLICT):
			return pkg_conflict
		else:
			return None

	PKGCONN_REDUCE 		= 0
	PKGCONN_RESOLVE 	= 1
	PKGCONN_REQUIRES 	= 2
	PKGCONN_CONFLICT	= 3
	PKGCONN_VARIANT		= 4
	PKGCONN_CYCLIC		= 5
	PKGCONN_TRANSITIVE	= 6

	def add_package(self, pkg_req, parent_pkg=None, dot_connection_type=0):
		"""
		add a package request to this configuration, optionally describing the 'parent'
		package (ie the package that requires it), and the type of dot-graph connection,
		if the pkg has a parent pkg.
		"""
		# test to see what adding this package would do
		result, pkg = self.test_pkg_req_add(pkg_req, True)

		self._add_package_to_dot_graph(pkg_req.short_name(), pkg, result,
									   parent_pkg, dot_connection_type)

		if (result == _Configuration.ADDPKG_CONFLICT):
			pkg_conflict = PackageConflict(pkg.as_package_request(), pkg_req)
			raise PkgConflictError([ pkg_conflict ], self.rctxt.last_fail_dot_graph)

		elif (result == _Configuration.ADDPKG_ADD) and pkg:
			if dot_connection_type == _Configuration.PKGCONN_TRANSITIVE:
				pkg.is_transitivity = True

			# add pkg, possibly replacing existing pkg. This is to retain order of package addition,
			# since package resolution is sensitive to this
			if (not pkg.is_anti()) and (not (pkg.name in self.pkgs)):
				self.families.append(pkg.name)
			self.pkgs[pkg.name] = pkg

			# if pkg is non-anti then remove its anti from the config, if it's there. Adding a
			# non-anti pkg to the config without a conflict occurring always means we can safely
			# remove the anti pkg, if it exists.
			if not pkg.is_anti():
				if anti_name(pkg) in self.pkgs:
					del self.pkgs[anti_name(pkg)]

	def _add_package_to_dot_graph(self, short_name, pkg, result, parent_pkg=None,
								  dot_connection_type=0):
		if parent_pkg:
			if dot_connection_type == _Configuration.PKGCONN_TRANSITIVE:
				connt = _Configuration.PKGCONN_TRANSITIVE
				self.add_dot_graph_verbatim('"' + short_name + \
					'" [ shape=octagon ] ;')
			else:
				connt = _Configuration.PKGCONN_REQUIRES
			self.dot_graph.append( ( parent_pkg.short_name(), ( short_name, connt ) ) )

		if (result == _Configuration.ADDPKG_CONFLICT):
			self.dot_graph.append( ( pkg.short_name(), ( short_name, \
				_Configuration.PKGCONN_CONFLICT ) ) )
			self.rctxt.last_fail_dot_graph = self.get_dot_graph_as_string()

		elif (result == _Configuration.ADDPKG_ADD) and pkg:

			# update dot-graph
			pkgname = pkg.short_name()
			if pkg.name in self.pkgs:
				connt = dot_connection_type
				if (connt != _Configuration.PKGCONN_RESOLVE):
					connt = _Configuration.PKGCONN_REDUCE

				pkgname_existing = self.pkgs[pkg.name].short_name()
				# if pkg and pkg-existing have same short-name, then a further-reduced package was already
				# in the config (eg, we added 'python' to a config with 'python-2.5')
				if (pkgname_existing == pkgname):
					self.dot_graph.append( ( short_name, ( pkgname_existing, connt ) ) )
				else:
					self.dot_graph.append( ( pkgname_existing, ( pkgname, connt ) ) )
			self.dot_graph.append( ( pkgname, None ) )

	def get_dot_graph_as_string(self):
		"""
		return a string-representation of the dot-graph. You should be able to
		write this to file, and view it in a dot viewer, such as dotty or graphviz
		"""
		dotstr = "digraph g { \n"
		conns = set()

		for connection in self.dot_graph:
			if type(connection) == type(""):
				verbatim_txt = connection
				dotstr += verbatim_txt + '\n';
			else:
				if connection not in conns:
					if connection[1]:
						dep, conntype = connection[1]
						dotstr += '"' + connection[0] + '" -> "' + dep + '" '
						if(conntype == _Configuration.PKGCONN_REQUIRES):
							col = make_random_color_string()
							conn_style = '[label=needs color="' + col + '" fontcolor="' + col + '"]'
						elif(conntype == _Configuration.PKGCONN_TRANSITIVE):
							col = make_random_color_string()
							conn_style = '[label=willneed color="' + col + '" fontcolor="' + col + '"]'
						elif(conntype == _Configuration.PKGCONN_RESOLVE):
							conn_style = '[label=resolve color="green4" fontcolor="green4" style="bold"]'
						elif(conntype == _Configuration.PKGCONN_REDUCE):
							conn_style = '[label=reduce color="grey30" fontcolor="grey30" style="dashed"]'
						elif(conntype == _Configuration.PKGCONN_VARIANT):
							conn_style = '[label=variant color="grey30" fontcolor="grey30" style="dashed"]'
						elif(conntype == _Configuration.PKGCONN_CYCLIC):
							conn_style = '[label=CYCLE color="red" fontcolor="red" fontsize="30" style="bold"]'
						else:
							conn_style = '[label=CONFLICT color="red" fontcolor="red" fontsize="30" style="bold"]'
						dotstr += conn_style + ' ;\n'
					else:
						dotstr += '"' + connection[0] + '" ;\n'
					conns.add(connection)

		dotstr += "}\n"
		return dotstr

	def add_dot_graph_verbatim(self, txt):
		"""
		add a verbatim string to the dot-graph output
		"""
		self.dot_graph.append(txt)

	def copy(self):
		"""
		return a shallow copy
		"""
		confcopy = _Configuration(self.rctxt)
		confcopy.pkgs = self.pkgs.copy()
		confcopy.families = self.families[:]
		confcopy.dot_graph = self.dot_graph[:]
		return confcopy

	def deep_copy(self):
		confcopy = _Configuration(self.rctxt)
		confcopy.families = self.families[:]
		confcopy.dot_graph = self.dot_graph[:]

		confcopy.pkgs = {}
		for k,v in self.pkgs.iteritems():
			confcopy.pkgs[k] = v.copy()

		return confcopy

	def swap(self, a):
		"""
		swap this config's contents with another
		"""
		self.pkgs, a.pkgs = a.pkgs, self.pkgs
		self.families, a.families = a.families, self.families
		self.dot_graph, a.dot_graph = a.dot_graph, self.dot_graph

	def get_unresolved_packages_as_package_requests(self):
		"""
		return a list of unresolved packages as package requests
		"""
		pkg_reqs = []
		for name,pkg in self.pkgs.iteritems():
			if (not pkg.is_resolved()) and (not pkg.is_anti()):
				pkg_reqs.append(pkg.as_package_request())
		return pkg_reqs

	def get_all_packages_as_package_requests(self):
		"""
		return a list of all packages as package requests
		"""
		pkg_reqs = []
		for name,pkg in self.pkgs.iteritems():
			pkg_reqs.append(pkg.as_package_request())
		return pkg_reqs

	def resolve_packages(self):
		"""
		resolve the current configuration - all the action happens here. On success,
		a resolved package list is returned. This function should only fail via an
		exception - if an infinite loop results then there is a bug somewheres.
		Please note that the returned list order is important. Required packages appear
		first, and requirees later... since a package's commands may refer to env-vars set
		in a required package's commands.
		"""

		while (not self.all_resolved()) and \
		    ((self.rctxt.max_fails == -1) or (len(self.rctxt.config_fail_list) <= self.rctxt.max_fails)):

			# do an initial resolve pass
			self.resolve_packages_no_filesys()
			if self.all_resolved():
				break

			# fail if not all resolved and mode=none
			if (not self.all_resolved()) and (self.rctxt.resolve_mode == RESOLVE_MODE_NONE):
				pkg_reqs = self.get_unresolved_packages_as_package_requests()
				raise PkgsUnresolvedError(pkg_reqs)

			# add transitive dependencies
			self.add_transitive_dependencies()

			# this shouldn't happen here but just in case...
			if self.all_resolved():
				break

			# find first package with unresolved metafile. Note that self.families exists in
			# order to retain package order, because different package order can result
			# in different configuration resolution.
			pkg = None
			for name in self.families:
				pkg_ = self.pkgs[name]
				if not pkg_.is_metafile_resolved():
					pkg = pkg_
					break

			if not pkg:
				# The remaining unresolved packages must have more than one variant each. So
				# find that variant, out of all remaining packages, that is 'least suitable',
				# and remove it. 'least suitable' means that the variant has largest number
				# of packages that do not intersect with anything in the config.
				if (self.rctxt.verbosity != 0):
					print
					print "Ran out of concrete resolution choices, yet unresolved packages still remain:"
					if (self.rctxt.verbosity == 1):
						print str(self)
					elif (self.rctxt.verbosity == 2):
						self.dump()

				self.remove_least_suitable_variant()

			else:

				valid_config_found = False

				# attempt to resolve a copy of the current config with this package resolved
				# as closely as possible to desired (eg in mode=latest, start with latest and
				# work down). The first config to resolve represents the most desirable. Note
				# that resolve_packages will be called recursively
				num_version_searches = 0
				while ((self.rctxt.max_fails == -1) or \
		            	(len(self.rctxt.config_fail_list) <= self.rctxt.max_fails)):

					num_version_searches += 1

					# resolve package to as closely desired as possible
					pkg_req_ = pkg.next_request()
					if pkg_req_ is None:
						# FIXME: don't have easy access to the sub-version-range that we failed on
						if(num_version_searches == 1):
# 							# this means that rather than running out of versions of this lib to try, there
# 							# were never any versions found at all - which means this package doesn't exist
# 							self.add_dot_graph_verbatim('"' + \
# 								pkg_req_.short_name() + ' NOT FOUND' + \
# 								'" [style=filled fillcolor="orangered"] ;')
# 							self.add_dot_graph_verbatim('"' + \
# 								pkg_req_.short_name() + '" -> "' + \
# 								pkg_req_.short_name() + ' NOT FOUND" ;')
# 							self.rctxt.last_fail_dot_graph = self.get_dot_graph_as_string()
# 
# 							sys.stderr.write("Warning! Package not found: " + str(pkg_req_) + "\n")
							raise PkgNotFoundError(pkg.as_package_request())

						if (self.uid == 0):
							print "BREAK!"
							# we're the topmost configuration, and there are no more packages to try -
							# all possible configuration attempts have failed at this point
							break
						else:
							raise PkgsUnresolvedError([pkg.as_package_request()])

					pkg_resolve_str = pkg.short_name() + " --> " + pkg_req_.short_name()

					# create config copy, bit of fiddling though cause we want a proper guid
					config2 =_Configuration(self.rctxt, inc_uid=True)
					config2 = self.deep_copy()
					config2.uid = config2.uid

					if (self.rctxt.verbosity != 0):
						print
						print "SPAWNED NEW CONFIG #" + str(config2.uid) + " FROM PARENT #" + str(self.uid) + \
							" BASED ON FILESYS RESOLUTION: " + pkg_resolve_str

					# attempt to add package to config copy
					try:
						config2.add_package(pkg_req_, None, _Configuration.PKGCONN_RESOLVE)
					except PkgConflictError, e:
						self.rctxt.last_fail_dot_graph = config2.get_dot_graph_as_string()

						if (self.rctxt.verbosity != 0):
							print
							print "CONFIG #" + str(config2.uid) + " FAILED (" + e.__class__.__name__ + "):"
							print str(e)
							print
							print "ROLLING BACK TO CONFIG #" + self.uid
						continue

					if (self.rctxt.verbosity != 0):
						print
						print "config after applying: " + pkg_resolve_str
						if (self.rctxt.verbosity == 1):
							print str(config2)
						elif (self.rctxt.verbosity == 2):
							config2.dump()

					# now fully resolve config copy
					try:
						config2.resolve_packages()
					except ( \
						PkgConfigNotResolvedError, \
						PkgsUnresolvedError, \
						PkgConflictError, \
						PkgNotFoundError, \
						PkgFamilyNotFoundError, \
						PkgSystemError), e:

						# store fail reason into list, unless it's a PkgConfigNotResolvedError - this error just
						# tells us that the sub-config failed because its sub-config failed.
						if (type(e) not in [PkgConfigNotResolvedError, PkgsUnresolvedError]):

							sys.stderr.write("conflict " + str(len(self.rctxt.config_fail_list)) + \
								": " + config2.short_str() + '\n')
							sys.stderr.flush()

							this_fail = "config: (" + str(config2).strip() + "): " + \
								e.__class__.__name__ + ": " + str(e)

							if(self.rctxt.max_fails >= 0):
								if(len(self.rctxt.config_fail_list) <= self.rctxt.max_fails):
									self.rctxt.config_fail_list.append(this_fail)
									if(len(self.rctxt.config_fail_list) > self.rctxt.max_fails):
										self.rctxt.config_fail_list.append( \
											"Maximum configuration failures reached.")
										pkg_reqs_ = self.get_all_packages_as_package_requests()
										raise PkgConfigNotResolvedError(pkg_reqs_, \
											self.rctxt.config_fail_list, self.rctxt.last_fail_dot_graph)
							else:
								self.rctxt.config_fail_list.append(this_fail)

						if (self.rctxt.verbosity != 0):
							print
							print "CONFIG #" + str(config2.uid) + " FAILED (" + e.__class__.__name__ + "):"
							print str(e)
							print
							print "ROLLING BACK TO CONFIG #" + str(self.uid)

						continue

					# if we got here then we have a valid config yay!
					self.swap(config2)
					valid_config_found = True
					break

				if not valid_config_found:
					# we're exhausted the possible versions of this package to try
					fail_msg = "No more versions to be found on filesys: " + pkg.short_name()
					if (self.rctxt.verbosity != 0):
						print
						print fail_msg

					pkg_reqs_ = self.get_all_packages_as_package_requests()
					raise PkgConfigNotResolvedError(pkg_reqs_, \
						self.rctxt.config_fail_list, self.rctxt.last_fail_dot_graph)

		#################################################
		# woohoo, we have a fully resolved configuration!
		#################################################

		# check for cyclic dependencies
		cyclic_deps = self.detect_cyclic_dependencies()
		if len(cyclic_deps) > 0:
			# highlight cycles in the dot-graph
			for pkg1, pkg2 in cyclic_deps:
				self.dot_graph.append( ( pkg1, ( pkg2, _Configuration.PKGCONN_CYCLIC ) ) )

			dot_str = self.get_dot_graph_as_string()
			raise PkgCyclicDependency(cyclic_deps, dot_str)

		# convert packages into a list of package resolutions, forcing them into the correct 
		# order wrt command sourcing
		ordered_fams = self.get_ordered_families()

		pkg_ress = []
		for name in ordered_fams:
			pkg = self.pkgs[name]
			if not pkg.is_anti():
				resolved_cmds = pkg.get_resolved_commands()
				pkg_res = ResolvedPackage(name, str(pkg.version_range), pkg.base_path, \
                    pkg.root_path, resolved_cmds, pkg.metadata, pkg.timestamp)
				pkg_ress.append(pkg_res)

		return pkg_ress

	def _create_family_dependency_tree(self):
		"""
		From the dot-graph, extract a dependency tree containing unversioned pkgs (ie families),
		and a set of all existing families
		"""
		deps = set()
		fams = set()
		for conn in self.dot_graph:
			if (type(conn) != type("")) and \
				(conn[0][0] != '!'):
				fam1 = conn[0].split('-',1)[0]
				fams.add(fam1)
				if (conn[1] != None) and \
					(conn[1][1] == _Configuration.PKGCONN_REQUIRES) and \
					(conn[1][0][0] != '!'):
					fam2 = conn[1][0].split('-',1)[0]
					fams.add(fam2)
					if fam1 != fam2:
						deps.add( (fam1, fam2) )

		return deps, fams

	def get_ordered_families(self):
		"""
		Return the families of all packages in such an order that required packages appear
		before requirees. This means we can properly order package command construction -
		if A requires B, then A's commands might refer to an env-var set in B's commands.
		"""
		fam_list = []
		deps, fams = self._create_family_dependency_tree()

		while len(deps) > 0:
			parents = set()
			children = set()
			for dep in deps:
				parents.add(dep[0])
				children.add(dep[1])

			leaf_fams = children - parents
			if len(leaf_fams) == 0:
				break 	# if we hit this then there are cycle(s) somewhere

			for fam in leaf_fams:
				fam_list.append(fam)

			del_deps = set()
			for dep in deps:
				if dep[1] in leaf_fams:
					del_deps.add(dep)
			deps -= del_deps

			fams -= leaf_fams

		# anything left in the fam set is a topmost node
		for fam in fams:
			fam_list.append(fam)

		return fam_list


	def detect_cyclic_dependencies(self):
		"""
		detect cyclic dependencies, if they exist
		"""
		# extract dependency tree from dot-graph
		deps = self._create_family_dependency_tree()[0]

		# remove leaf nodes
		while len(deps) > 0:
			parents = set()
			children = set()
			for dep in deps:
				parents.add(dep[0])
				children.add(dep[1])

			leaf_fams = children - parents
			if len(leaf_fams) == 0:
				break

			del_deps = set()
			for dep in deps:
				if dep[1] in leaf_fams:
					del_deps.add(dep)
			deps -= del_deps

		# remove topmost nodes
		while len(deps) > 0:
			parents = set()
			children = set()
			for dep in deps:
				parents.add(dep[0])
				children.add(dep[1])

			top_fams = parents - children
			if len(top_fams) == 0:
				break

			del_deps = set()
			for dep in deps:
				if dep[0] in top_fams:
					del_deps.add(dep)
			deps -= del_deps

		# anything left is part of a cyclic loop...

		if len(deps) > 0:
			# inject pkg versions into deps list
			deps2 = set()
			for dep in deps:
				pkg1 = self.pkgs[ dep[0] ].short_name()
				pkg2 = self.pkgs[ dep[1] ].short_name()
				deps2.add( (pkg1, pkg2) )
			deps = deps2

		return deps

	def resolve_packages_no_filesys(self):
		"""
		resolve current packages as far as possible without querying the file system
		"""

		nresolved_metafiles = -1
		nresolved_common_variant_pkgs = -1
		nconflicting_variants_removed = -1
		nresolved_single_variant_pkgs = -1

		while ((( \
				nresolved_metafiles + \
				nresolved_common_variant_pkgs + \
				nconflicting_variants_removed + \
				nresolved_single_variant_pkgs) != 0) and
				(not self.all_resolved())):

			# resolve metafiles
			nresolved_metafiles = self.resolve_metafiles()

			# remove conflicting variants
			nconflicting_variants_removed = self.remove_conflicting_variants()

			# resolve common variant packages
			nresolved_common_variant_pkgs = self.resolve_common_variants()

			# resolve packages with a single, fully-resolved variant
			nresolved_single_variant_pkgs = self.resolve_single_variant_packages()

	def remove_least_suitable_variant(self):
		"""
		remove one variant from any remaining unresolved packages, such that that variant is
		'least suitable' - that is, has the greatest number of packages which do not appear
		in the current configuration
		TODO remove this I think, error instead
		"""

		bad_pkg = None
		bad_variant = None
		bad_variant_score = -1

		for name,pkg in self.pkgs.iteritems():
			if (not pkg.is_resolved()) and (not pkg.is_anti()):
				for variant in pkg.get_variants():
					sc = self.get_num_unknown_pkgs(variant.working_list)
					if (sc > bad_variant_score):
						bad_pkg = pkg
						bad_variant = variant
						bad_variant_score = sc

		bad_pkg.get_variants().remove(bad_variant)

		if (self.rctxt.verbosity != 0):
			print
			print "removed least suitable variant:"
			print bad_pkg.short_name() + " variant:" + str(bad_variant)

	def get_num_unknown_pkgs(self, pkg_strs):
		"""
		given a list of package strings, return the number of packages in the list
		which do not appear in the current configuration
		"""
		num = 0
		for pkg_str in pkg_strs:
			pkg_req = str_to_pkg_req(pkg_str, self.rctxt.time_epoch, self.rctxt.resolve_mode==RESOLVE_MODE_LATEST)
			if pkg_req.name not in self.pkgs:
				num += 1

		return num

	def resolve_metafiles(self):
		"""
		for each package, resolve metafiles until no more can be resolved, returning
		the number of metafiles that were resolved.
		"""
		num = 0
		config2 = None

		for name, pkg in self.pkgs.iteritems():
			if (pkg.metadata == None):
				if pkg.resolve_metafile(self.rctxt.time_epoch):
					num += 1

					if (self.rctxt.verbosity != 0):
						print
						print "resolved metafile for " + pkg.short_name() + ":"
					if (self.rctxt.verbosity == 2):
						print str(pkg)

					# add required packages to the configuration, this may
					# reduce wrt existing packages (eg: foo-1 -> foo-1.2 is a reduction)
					if self.rctxt.build_requires:
						requires = (pkg.metadata.build_requires or []) + (pkg.metadata.requires or [])
					else:
						requires = pkg.metadata.requires

					if requires:
						for pkg_str in requires:
							pkg_req = str_to_pkg_req(pkg_str, self.rctxt.time_epoch, self.rctxt.resolve_mode==RESOLVE_MODE_LATEST)

							if (self.rctxt.verbosity != 0):
								print
								print "adding " + pkg.short_name() + \
									"'s required package " + pkg_req.short_name() + '...'

							if not config2:
								config2 = self.copy()
							config2.add_package(pkg_req, pkg)

							if (self.rctxt.verbosity != 0):
								print "config after adding " + pkg.short_name() + \
									"'s required package " + pkg_req.short_name() + ':'
							if (self.rctxt.verbosity == 1):
								print str(config2)
							elif (self.rctxt.verbosity == 2):
								config2.dump()

		if config2:
			self.swap(config2)
		return num


	def add_transitive_dependencies(self):
		"""
		for each package that is inexact and not resolved, calculate the package ranges that
		it must eventually pull in anyway, assuming dependency transitivity, and add those to
		the current configuration.
		"""
		if not self.rctxt.assume_dt:
			return
		while (self._add_transitive_dependencies() > 0):
			pass


	def _add_transitive_dependencies(self):

		num = 0
		config2 = None

		for name, pkg in self.pkgs.iteritems():
			if pkg.is_metafile_resolved():
				continue
			if pkg.is_anti():
				continue
			if pkg.has_added_transitivity:
				continue

			# get the requires lists for the earliest and latest versions of this pkg
			metafile_e = pkg.get_metadata(latest=False, timestamp=self.rctxt.time_epoch)
			if not metafile_e:
				continue

			metafile_l = pkg.get_metadata(latest=True, timestamp=self.rctxt.time_epoch)
			if not metafile_l:
				continue

			pkg.has_added_transitivity = True

			requires_e = metafile_e.requires
			requires_l = metafile_l.requires
			if (not requires_e) or (not requires_l):
				continue

			# find pkgs that exist in the requires of both, and add these to the current
			# config as 'transitivity' packages
			for pkg_str_e in requires_e:
				if (pkg_str_e[0] == '!') or (pkg_str_e[0] == '~'):
					continue

				pkg_req_e = str_to_pkg_req(pkg_str_e, self.rctxt.time_epoch, self.rctxt.resolve_mode==RESOLVE_MODE_LATEST)

				for pkg_str_l in requires_l:
					pkg_req_l = str_to_pkg_req(pkg_str_l, self.rctxt.time_epoch, self.rctxt.resolve_mode==RESOLVE_MODE_LATEST)
					if (pkg_req_e.name == pkg_req_l.name):
						pkg_req = pkg_req_e
						if (pkg_req_e.version != pkg_req_l.version):
							# calc version range
							v_e = Version(pkg_req_e.version)
							v_l = Version(pkg_req_l.version)
							if(not v_e.ge < v_l.lt):
								continue
							v = Version()
							v.ge = v_e.ge
							v.lt = v_l.lt
							if (v.ge == Version.NEG_INF) and (v.lt != Version.INF):
								v.ge = [0]
							pkg_req = PackageRequest(pkg_req_e.name, str(v),
													 self.rctxt.resolve_mode==RESOLVE_MODE_LATEST,
													 self.rctxt.time_epoch)

						if not config2:
							config2 = self.copy()
						config2.add_package(pkg_req, pkg, _Configuration.PKGCONN_TRANSITIVE)
						num = num + 1

			# find common variants that exist in both. Note that this code is somewhat redundant,
			# v similar work is done in resolve_common_variants - fix this in rez V2
			variants_e = metafile_e.variants
			variants_l = metafile_l.variants
			if (not variants_e) or (not variants_l):
				continue

			common_pkg_fams = None
			pkg_vers = {}

			for variant in (variants_e + variants_l):
				comm_fams = set()
				for pkgstr in variant:
					pkgreq = str_to_pkg_req(pkgstr, self.rctxt.time_epoch, self.rctxt.resolve_mode==RESOLVE_MODE_LATEST)
					comm_fams.add(pkgreq.name)
					if pkgreq.name in pkg_vers:
						pkg_vers[pkgreq.name].append(pkgreq.version)
					else:
						pkg_vers[pkgreq.name] = [ pkgreq.version ]

				if (common_pkg_fams == None):
					common_pkg_fams = comm_fams
				else:
					common_pkg_fams &= comm_fams

				if len(common_pkg_fams) == 0:
					break

			if (common_pkg_fams != None):
				for pkg_fam in common_pkg_fams:
					ver_range = VersionRange(str("|").join(pkg_vers[pkg_fam]))
					v = Version()
					if len(ver_range.versions) > 0:
						v.ge = ver_range.versions[0].ge
						v.lt = ver_range.versions[-1].lt
						if (v.ge == Version.NEG_INF) and (v.lt != Version.INF):
							v.ge = [0]

						pkg_req = PackageRequest(pkg_fam, str(v),
												 self.rctxt.resolve_mode==RESOLVE_MODE_LATEST,
												 self.rctxt.time_epoch)

						if not config2:
							config2 = self.copy()
						config2.add_package(pkg_req, pkg, _Configuration.PKGCONN_TRANSITIVE)
						num = num + 1

		if config2:
			self.swap(config2)
		return num


	def remove_conflicting_variants(self):
		"""
		for each package, remove those variants which contain one or more packages which
		conflict with the current configuration. If a package has all of its variants
		removed in this way, then a pkg-conflict exception will be raised.
		"""

		if (self.rctxt.verbosity == 2):
			print
			print "removing conflicting variants..."

		num = 0

		for name,pkg in self.pkgs.iteritems():

			variants = pkg.get_variants()
			if variants != None:
				conflicts = []

				conflicting_variants = set()
				for variant in variants:
					for pkgstr in variant.metadata:
						pkg_req_ = str_to_pkg_req(pkgstr, self.rctxt.time_epoch, self.rctxt.resolve_mode==RESOLVE_MODE_LATEST)
						pkg_conflicting = self.get_conflicting_package(pkg_req_)
						if pkg_conflicting:
							pkg_req_conflicting = pkg_conflicting.as_package_request()
							pkg_req_this = pkg.as_package_request()
							pc = PackageConflict(pkg_req_conflicting, pkg_req_this, variant.metadata)
							conflicts.append(pc)
							conflicting_variants.add(variant)
							num += 1
							break

				if (len(conflicts) > 0):
					if (len(conflicts) == len(variants)):	# all variants conflict

						self.add_dot_graph_verbatim(\
							'subgraph cluster_variants {\n' + \
							'style=filled ;\n' + \
							'label=variants ;\n' + \
							'fillcolor="lightcyan1" ;' )

						# show all variants and conflicts in dot-graph
						for variant in variants:
							varstr = str(", ").join(variant.metadata)
							self.add_dot_graph_verbatim('"' + varstr + '" [style=filled fillcolor="white"] ;')

						self.add_dot_graph_verbatim('}')

						for variant in variants:
							varstr = str(", ").join(variant.metadata)
							self.dot_graph.append( ( pkg_req_this.short_name(), \
								( varstr, _Configuration.PKGCONN_VARIANT ) ) )
							self.dot_graph.append( ( pkg_req_conflicting.short_name(), \
								( varstr, _Configuration.PKGCONN_CONFLICT ) ) )

						self.rctxt.last_fail_dot_graph = self.get_dot_graph_as_string()
						raise PkgConflictError(conflicts)
					else:
						for cv in conflicting_variants:
							variants.remove(cv)

						if (self.rctxt.verbosity == 2):
							print
							print "removed conflicting variants from " + pkg.short_name() + ':'
							for conflict in conflicts:
								print str(conflict)
		return num


	def resolve_common_variants(self):
		"""
		for each package, find common package families within its variants, and add these to
		the configuration. For eg, if a pkg has 2 variants 'python-2.5' and 'python-2.6',
		then the inexact package 'python-2.5|2.6' will be added to the configuration
		(but only if ALL variants reference a 'python' package). Return the number of
		common package families resolved. Note that if a package contains a single variant,
		this this function will add every package in the variant to the configuration.
		"""

		num = 0
		config2 = self.copy()

		for name,pkg in self.pkgs.iteritems():

			variants = pkg.get_variants()
			if variants != None:

				# find common package families
				pkgname_sets = []
				pkgname_versions = {}
				pkgname_entries = {}

				for variant in variants:
					if (len(variant.working_list) > 0):
						pkgname_set = set()
						for pkgstr in variant.working_list:
							pkg_req = str_to_pkg_req(pkgstr, self.rctxt.time_epoch, self.rctxt.resolve_mode==RESOLVE_MODE_LATEST)
							pkgname_set.add(pkg_req.name)
							if not (pkg_req.name in pkgname_versions):
								pkgname_versions[pkg_req.name] = []
								pkgname_entries[pkg_req.name] = []
							pkgname_versions[pkg_req.name].append(pkg_req.version)
							pkgname_entries[pkg_req.name].append([ variant.working_list, pkgstr ])
						pkgname_sets.append(pkgname_set)

				if (len(pkgname_sets) > 0):
					common_pkgnames = pkgname_sets[0]
					for pkgname_set in pkgname_sets[1:]:
						common_pkgnames = common_pkgnames.intersection(pkgname_set)

					num += len(common_pkgnames)

					# add the union of each common package to the configuration,
					# and remove the packages from the variants' working lists
					for common_pkgname in common_pkgnames:
						ored_pkgs_str = common_pkgname + '-' + '|'.join(pkgname_versions[common_pkgname])
						pkg_req_ = str_to_pkg_req(ored_pkgs_str, self.rctxt.time_epoch, self.rctxt.resolve_mode==RESOLVE_MODE_LATEST)

						config2.add_package(pkg_req_, pkg)

						for entry in pkgname_entries[common_pkgname]:
							entry[0].remove(entry[1])

						if (self.rctxt.verbosity != 0):
							print
							print "removed common package family '" + common_pkgname + "' from " + pkg.short_name() + \
								"'s variants; config after adding " + pkg_req_.short_name() + ':'
						if (self.rctxt.verbosity == 1):
							print str(config2)
						elif (self.rctxt.verbosity == 2):
							config2.dump()

		self.swap(config2)
		return num

	def resolve_single_variant_packages(self):
		"""
		find packages which have one non-conflicting, fully-resolved variant. These
		packages can now be fully resolved
		"""

		num = 0
		for name,pkg in self.pkgs.iteritems():
			if pkg.is_resolved():
				continue

			variants = pkg.get_variants()
			if (variants != None) and (len(variants) == 1):
				variant = variants[0]
				if (len(variant.working_list) == 0):

					# check resolved path exists
					root_path = os.path.join(pkg.base_path, *variant.metadata)
					if not os.path.isdir(root_path):
						pkg_req_ = pkg.as_package_request()

						self.add_dot_graph_verbatim('"' + \
							pkg_req_.short_name() + ' NOT FOUND' + \
							'" [style=filled fillcolor="orangered"] ;')
						self.add_dot_graph_verbatim('"' + \
							pkg_req_.short_name() + '" -> "' + \
							pkg_req_.short_name() + ' NOT FOUND" ;')
						self.rctxt.last_fail_dot_graph = self.get_dot_graph_as_string()

						sys.stderr.write("Warning! Package not found: " + str(pkg_req_) + "\n")
						raise PkgNotFoundError(pkg_req_, root_path)

					pkg.resolve(root_path)
					num += 1

					if (self.rctxt.verbosity != 0):
						print
						print "resolved single-variant package " + pkg.short_name() + ':'
					if (self.rctxt.verbosity == 1):
						print str(self)
					elif (self.rctxt.verbosity == 2):
						print str(pkg)
		return num

	def dump(self):
		"""
		debug printout
		"""
		for name in self.families:
			pkg = self.pkgs[name]
			if (pkg.metadata == None):
				print pkg.short_name()
			else:
				print str(pkg)

	def __str__(self):
		"""
		short printout
		"""
		str_ = ""
		for name in self.families:
			pkg = self.pkgs[name]
			str_ += pkg.short_name()

			modif="("
			if pkg.is_resolved():
				modif += "r"
			elif pkg.is_metafile_resolved():
				modif += "b"
			else:
				modif += "u"
			if pkg.is_transitivity:
				modif += "t"
			str_ += modif + ") "

		return str_

	def short_str(self):
		"""
		even shorter printout
		"""
		str_ = ""
		for name in self.families:
			pkg = self.pkgs[name]
			str_ += pkg.short_name() + " "
		return str_



##############################################################################
# Internal Functions
##############################################################################

def process_commands(cmds):
	"""
	Given a list of commands which represent a configuration context,

	a) Find the first forms of X=$X:<something_else>, and drop the leading $X so
		that values aren't inherited from the existing environment;
	b) Find variable overwrites and raise an exception if found (ie, consecutive
		commands of form "X=something, X=something_else".

	This function returns the altered commands. Order of commands is retained.
	"""
	set_vars = {}
	new_cmds = []

	for cmd_ in cmds:

		if type(cmd_) == type([]):
			cmd = cmd_[0]
			pkgname = cmd_[1]
		else:
			cmd = cmd_
			pkgname = None

		if cmd.split()[0] == "export":

			# parse name, value
			var_val = cmd[len("export"):].split('=',1)
			if (len(var_val) != 2):
				raise PkgCommandError("invalid command:'" + cmd + "'")
			varname = var_val[0].split()[0]
			val = var_val[1]

			# has value already been set?
			val_is_set = (varname in set_vars)

			# check for variable self-reference (eg X=$X:foo etc)
			pos = val.find('$'+varname)
			if (pos == -1):
				if val_is_set:
					# no self-ref but previous val, this is a val overwrite
					raise PkgCommandError("the command set by '" + str(pkgname) + "':\n" + cmd + \
						"\noverwrites the variable set in a previous command by '" + str(set_vars[varname]) + "'")
			elif not val_is_set:
				# self-ref but no previous val, so strip self-ref out
				val = val.replace('$'+varname,'')

			# special case. CMAKE_MODULE_PATH is such a common case, but unusually uses ';' rather
			# than ':' to delineate, that I just allow ':' and do the switch here. Using ';' causes
			# probs because in bash it needs to be single-quoted, and users will forget to do that
			# in their package.yamls.
			if(varname == "CMAKE_MODULE_PATH"):
				val = val.strip(':;')
				val = val.replace(':', "';'")

			set_vars[varname] = pkgname
			new_cmds.append("export " + varname + '=' + val)

		else:
			new_cmds.append(cmd)

	return new_cmds





#    Copyright 2008-2012 Dr D Studios Pty Limited (ACN 127 184 954) (Dr. D Studios)
#
#    This file is part of Rez.
#
#    Rez is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Lesser General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Rez is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public License
#    along with Rez.  If not, see <http://www.gnu.org/licenses/>.
