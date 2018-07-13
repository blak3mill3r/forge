# Copyright 2017 datawire. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy, errno, fnmatch, hashlib, jsonschema, os, pathspec, util, yaml
from collections import OrderedDict
from forge import service_info
from .jinja2 import render, renders
from .kubernetes import is_yaml_file
from .schema import SchemaError
from .tasks import sh, task, TaskError
from .github import Github
from forge import yamlutil

def load_service_yaml(path, **vars):
    with open(path, "read") as f:
        return load_service_yamls(path, f.read(), **vars)

def _dump_and_raise(rendered, e):
    task.echo("==unparseable service yaml==")
    for idx, line in enumerate(rendered.splitlines()):
        task.echo("%s: %s" % (idx + 1, line))
    task.echo("============================")
    raise TaskError("error parsing service yaml: %s" % e)

@task()
def load_service_yamls(name, content, **vars):
    if "env" not in vars:
        vars["env"] = os.environ
    rendered = renders(name, content, **vars)
    try:
        return service_info.load(name, rendered)
    except SchemaError, e:
        _dump_and_raise(rendered, TaskError(str(e)))
    except yaml.parser.ParserError, e:
        _dump_and_raise(rendered, e)
    except yaml.scanner.ScannerError, e:
        _dump_and_raise(rendered, e)

def get_ignores(directory):
    ignorefiles = [os.path.join(directory, ".gitignore"),
                   os.path.join(directory, ".forgeignore")]
    ignores = []
    for path in ignorefiles:
        if os.path.exists(path):
            with open(path) as fd:
                ignores.extend(fd.readlines())
    return ignores

def get_ancestors(path, stop="/"):
    path = os.path.abspath(path)
    stop = os.path.abspath(stop)
    if os.path.samefile(path, stop):
        return
    else:
        parent = os.path.dirname(path)
        for d in get_ancestors(parent, stop):
            yield d
        yield parent

def get_search_path(forge, svc):
    for p in svc.search_path:
        yield os.path.join(forge.base, p)

def is_service_descriptor(path):
    try:
        objs = yamlutil.load(path)
    except yaml.parser.ParserError, e:
        return True
    except yaml.scanner.ScannerError, e:
        return True
    if objs:
        first = objs[0]
        if "apiVersion" in first and "kind" in first and "metadata" in first:
            return False
    return True

class Discovery(object):

    def __init__(self, forge):
        self.forge = forge
        self.services = OrderedDict()

    @task()
    def search(self, directory, shallow=False):
        directory = os.path.abspath(directory)
        if not os.path.exists(directory):
            raise TaskError("no such directory: %s" % directory)
        if not os.path.isdir(directory):
            raise TaskError("not a directory: %s" % directory)

        base_ignores = [".git", ".forge"]
        gitdir = util.search_parents(".git", directory)
        if gitdir is None:
            gitroot = directory
        else:
            gitroot = os.path.dirname(gitdir)

        for d in get_ancestors(directory, gitroot):
            base_ignores.extend(get_ignores(d))

        found = []
        def descend(path, parent, ignores):
            if not os.path.exists(path): return
            ignores = ignores[:]

            ignores += get_ignores(path)
            spec = pathspec.PathSpec.from_lines('gitwildmatch', ignores)
            names = [n for n in os.listdir(path) if not spec.match_file(os.path.relpath(os.path.join(path, n),
                                                                                        directory))]

            if "service.yaml" in names:
                candidate = os.path.join(path, "service.yaml")
                if is_service_descriptor(candidate):
                    svc = Service(self.forge, candidate, shallow=shallow)
                    if svc.name not in self.services:
                        self.services[svc.name] = svc
                    found.append(svc)
                    parent = svc

            if "Dockerfile" in names and parent:
                parent.dockerfiles.append(os.path.relpath(os.path.join(path, "Dockerfile"), parent.root))

            for n in names:
                child = os.path.join(path, n)
                if os.path.isdir(child):
                    descend(child, parent, ignores)
                elif parent:
                    parent.files.append(os.path.relpath(child, parent.root))

        descend(directory, None, base_ignores)
        return found

    def resolve(self, svc, dep):
        for path in get_search_path(self.forge, svc):
            found = self.search(path)
            if dep in [svc.name for svc in found]:
                return True

        gh = Github(None)
        target = os.path.join(svc.forgeroot, ".forge", dep)
        if not os.path.exists(target):
            url = gh.remote(svc.root)
            if url is None: return False
            parts = url.split("/")
            prefix = "/".join(parts[:-1])
            remote = prefix + "/" + dep + ".git"
            if gh.exists(remote):
                task.echo("cloning %s->%s" % (remote, os.path.relpath(target, os.getcwd())))
                gh.clone(remote, target)
            else:
                raise TaskError("cannot resolve dependency: %s" % dep)
        found = self.search(target, shallow=True)
        return dep in [svc.name for svc in found]

    @task()
    def dependencies(self, targets):
        todo = [self.services[t] for t in targets]
        root = todo[0]
        visited = set()
        added = []
        missing = []
        while todo:
            svc = todo.pop()
            if svc in visited:
                continue
            visited.add(svc)
            for r in svc.requires:
                if r not in self.services:
                    if not self.resolve(root, r):
                        if r not in missing: missing.append(r)
                if r not in targets and r not in added:
                    added.append(r)
                if r in self.services:
                    todo.append(self.services[r])

        if missing:
            raise TaskError("required service(s) missing: %s" % ", ".join(missing))
        else:
            return added

def shafiles(root, files):
    result = hashlib.sha1()
    result.update("files %s\0" % len(files))
    for name in files:
        result.update("file %s\0" % name)
        try:
            with open(os.path.join(root, name)) as fd:
                result.update(fd.read())
        except IOError, e:
            if e.errno != errno.ENOENT:
                raise
    return result.hexdigest()

def is_git(path):
    if os.path.exists(os.path.join(path, ".git")):
        return True
    elif path not in ('', '/'):
        return is_git(os.path.dirname(path))
    else:
        return False

def get_version(path, dirty):
    if is_git(path):
        result = sh("git", "diff", "--quiet", "HEAD", ".", cwd=path, expected=(0, 1))
        if result.code == 0:
            line = sh("git", "log", "--no-color", "-n1", "--format=oneline", "--", ".", cwd=path).output.strip()
            if line:
                version = line.split()[0]
                return "%s.git" % version
    return dirty

class Service(object):

    def __init__(self, forge, descriptor, shallow=False):
        self.forge = forge
        self.descriptor = descriptor
        self.dockerfiles = []
        self.files = []
        self._info = None
        self._version = None
        self.shallow = shallow
        gitdir = util.search_parents(".git", self.root)
        if gitdir:
            self.gitroot = os.path.dirname(gitdir)
            self.is_git = True
        else:
            self.gitroot = None
            self.is_git = False
        if forge.branch:
            self.branch = forge.branch
        elif self.is_git:
            output = sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=self.root).output.strip()
            self.branch = None if output == "HEAD" else output
        else:
            self.branch = None
        self.forgeroot = os.path.dirname(util.search_parents("service.yaml", self.root, root=True))

    @property
    def root(self):
        return os.path.dirname(self.descriptor)

    @property
    def name(self):
        info = self.info()
        if "name" in info:
            return info["name"]
        else:
            return os.path.basename(self.root)

    @property
    def version(self):
        if self._version is None:
            self._version = get_version(self.root, "%s.sha" % shafiles(self.root, self.files))
        return self._version

    @property
    def repo(self):
        gh = Github(None)
        return gh.remote(self.root)

    @property
    def rel_descriptor(self):
        if self.is_git:
            return os.path.relpath(self.descriptor, self.gitroot)
        else:
            return os.path.relpath(self.descriptor, self.forgeroot)

    def image(self, container):
        pfx = os.path.dirname(container)
        name = os.path.join(self.name, pfx) if pfx else self.name
        name = name.replace("/", "-")
        return name


    @task()
    def pull(self, pulled):
        if self.is_git and self.shallow:
            if self.gitroot not in pulled:
                pulled[self.gitroot] = True
                sh("git", "pull", "--update-shallow", cwd=self.gitroot)

    @property
    def profile(self):
        svc = self.info()
        if self.forge.profile is None:
            profile = "default"
            branches = svc.get("branches", {})
            if self.branch:
                for k, v in branches.items():
                    if fnmatch.fnmatch(self.branch, k):
                        profile = v
                        break
            else:
                if "*" in branches:
                    profile = branches["*"]
        else:
            profile = self.forge.profile
        return profile

    @property
    def forge_profile(self):
        if self.profile in self.forge.profiles:
            return self.forge.profiles[self.profile]
        else:
            return self.forge.profiles["default"]

    @property
    def docker(self):
        return self.forge_profile.docker

    @property
    def search_path(self):
        return self.forge_profile.search_path

    def metadata(self):
        metadata = OrderedDict()

        metadata["env"] = os.environ

        svc = self.info()
        if "name" not in svc:
            svc["name"] = self.name
        metadata["service"] = svc

        build = OrderedDict()
        metadata["build"] = build

        build["branch"] = self.branch


        build["version"] = self.version
        prof = copy.deepcopy(svc.get("profiles", {}).get(self.profile, {}))
        build["profile"] = prof
        if "name" not in prof:
            prof["name"] = self.profile

        build["name"] = "%s-%s" % (svc["name"], prof["name"])

        build["images"] = OrderedDict()
        for container in self.containers:
            img = self.docker.image(container.image, self.version)
            build["images"][container.dockerfile] = img
            build["images"][container.name] = img

        return metadata

    @property
    def manifest_dir(self):
        return os.path.join(self.root, "k8s")

    @property
    def manifest_target_dir(self):
        return os.path.join(self.root, ".forge", "k8s", self.name)

    def deployment(self):
        metadata = self.metadata()
        render(self.manifest_dir, self.manifest_target_dir, is_yaml_file, **metadata)

    def info(self):
        if self._info is None:
            self._info = load_service_yaml(self.descriptor, branch=self.branch)
            v = self._info.get("istio", None)
            if v in (True, False):
                self._info["istio"] = OrderedDict(enabled=v)
        return self._info

    @property
    def requires(self):
        value = self.info().get("requires", ())
        if isinstance(value, basestring):
            return [value]
        else:
            return value

    @property
    def containers(self):
        info = self.info()
        containers = info.get("containers", self.dockerfiles)
        for idx, c in enumerate(containers):
            if isinstance(c, basestring):
                yield Container(self, c, index=idx)
            else:
                yield Container(self, c["dockerfile"], c.get("context", None), c.get("args", None),
                                c.get("rebuild", None), c.get("name", None), index=idx,
                                builder=c.get("builder"))

    def json(self):
        return {'name': self.name,
                'owner': self.name,
                'version': self.version,
                'descriptor': self.info(),
                'tasks': []}

    def __repr__(self):
        return "%s:%s" % (self.name, self.version)

class Container(object):

    def __init__(self, service, dockerfile, context=None, args=None, rebuild=None, name=None, index=None, builder=None):
        self.service = service
        self.dockerfile = dockerfile
        self.context = context or os.path.dirname(self.dockerfile)
        self.args = args or {}
        self.sources_relative = rebuild.get("sources_relative") if rebuild else None
        self.rebuild_root = rebuild.get("root", "/") if rebuild else None
        self.rebuild_sources = rebuild.get("sources", ()) if rebuild else ()
        self.rebuild_command = rebuild.get("command") if rebuild else None
        self.builder = builder
        self.name = name
        self.index = index

    @property
    def version(self):
        return self.service.version

    @property
    def image(self):
        if self.name:
            return self.name
        else:
            return self.service.image(self.dockerfile)

    @property
    def abs_dockerfile(self):
        return os.path.join(self.service.root, self.dockerfile)

    @property
    def abs_context(self):
        return os.path.join(self.service.root, self.context)

    @property
    def rebuild(self):
        return self.rebuild_sources or self.rebuild_command

    @task()
    def build(self):
        if self.rebuild:
            builder = self.service.docker.builder(self.abs_context, self.abs_dockerfile, self.image, self.version, self.args, builder=self.builder)
            builder.run("mkdir", "-p", self.rebuild_root)
            for src in self.rebuild_sources:
                abs_src = os.path.abspath( os.path.join(self.service.root, self.sources_relative, src) )
                tgt_src = os.path.join(self.rebuild_root, src)
                if os.path.isdir(abs_src):
                    builder.run("rm", "-rf", tgt_src)
                builder.cp(abs_src, tgt_src)
            if self.rebuild_command:
                builder.run("/bin/sh", "-c", self.rebuild_command)
            builder.commit(self.image, self.version)
        else:
            self.service.docker.build(self.abs_context, self.abs_dockerfile, self.image, self.version, self.args, builder=self.builder)
