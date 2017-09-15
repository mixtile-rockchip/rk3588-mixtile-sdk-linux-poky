#
# Copyright (c) 2017, Intel Corporation.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms and conditions of the GNU General Public License,
# version 2, as published by the Free Software Foundation.
#
# This program is distributed in the hope it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
"""Functionality for analyzing buildstats"""
import json
import logging
import os
import re
from collections import namedtuple
from statistics import mean


log = logging.getLogger()


taskdiff_fields = ('pkg', 'pkg_op', 'task', 'task_op', 'value1', 'value2',
                   'absdiff', 'reldiff')
TaskDiff = namedtuple('TaskDiff', ' '.join(taskdiff_fields))


class BSError(Exception):
    """Error handling of buildstats"""
    pass


class BSTask(dict):
    def __init__(self, *args, **kwargs):
        self['start_time'] = None
        self['elapsed_time'] = None
        self['status'] = None
        self['iostat'] = {}
        self['rusage'] = {}
        self['child_rusage'] = {}
        super(BSTask, self).__init__(*args, **kwargs)

    @property
    def cputime(self):
        """Sum of user and system time taken by the task"""
        rusage = self['rusage']['ru_stime'] + self['rusage']['ru_utime']
        if self['child_rusage']:
            # Child rusage may have been optimized out
            return rusage + self['child_rusage']['ru_stime'] + self['child_rusage']['ru_utime']
        else:
            return rusage

    @property
    def walltime(self):
        """Elapsed wall clock time"""
        return self['elapsed_time']

    @property
    def read_bytes(self):
        """Bytes read from the block layer"""
        return self['iostat']['read_bytes']

    @property
    def write_bytes(self):
        """Bytes written to the block layer"""
        return self['iostat']['write_bytes']

    @property
    def read_ops(self):
        """Number of read operations on the block layer"""
        if self['child_rusage']:
            # Child rusage may have been optimized out
            return self['rusage']['ru_inblock'] + self['child_rusage']['ru_inblock']
        else:
            return self['rusage']['ru_inblock']

    @property
    def write_ops(self):
        """Number of write operations on the block layer"""
        if self['child_rusage']:
            # Child rusage may have been optimized out
            return self['rusage']['ru_oublock'] + self['child_rusage']['ru_oublock']
        else:
            return self['rusage']['ru_oublock']

    @classmethod
    def from_file(cls, buildstat_file):
        """Read buildstat text file"""
        bs_task = cls()
        log.debug("Reading task buildstats from %s", buildstat_file)
        end_time = None
        with open(buildstat_file) as fobj:
            for line in fobj.readlines():
                key, val = line.split(':', 1)
                val = val.strip()
                if key == 'Started':
                    start_time = float(val)
                    bs_task['start_time'] = start_time
                elif key == 'Ended':
                    end_time = float(val)
                elif key.startswith('IO '):
                    split = key.split()
                    bs_task['iostat'][split[1]] = int(val)
                elif key.find('rusage') >= 0:
                    split = key.split()
                    ru_key = split[-1]
                    if ru_key in ('ru_stime', 'ru_utime'):
                        val = float(val)
                    else:
                        val = int(val)
                    ru_type = 'rusage' if split[0] == 'rusage' else \
                                                      'child_rusage'
                    bs_task[ru_type][ru_key] = val
                elif key == 'Status':
                    bs_task['status'] = val
        if end_time is not None and start_time is not None:
            bs_task['elapsed_time'] = end_time - start_time
        else:
            raise BSError("{} looks like a invalid buildstats file".format(buildstat_file))
        return bs_task


class BSTaskAggregate(object):
    """Class representing multiple runs of the same task"""
    properties = ('cputime', 'walltime', 'read_bytes', 'write_bytes',
                  'read_ops', 'write_ops')

    def __init__(self, tasks=None):
        self._tasks = tasks or []
        self._properties = {}

    def __getattr__(self, name):
        if name in self.properties:
            if name not in self._properties:
                # Calculate properties on demand only. We only provide mean
                # value, so far
                self._properties[name] = mean([getattr(t, name) for t in self._tasks])
            return self._properties[name]
        else:
            raise AttributeError("'BSTaskAggregate' has no attribute '{}'".format(name))

    def append(self, task):
        """Append new task"""
        # Reset pre-calculated properties
        assert isinstance(task, BSTask), "Type is '{}' instead of 'BSTask'".format(type(task))
        self._properties = {}
        self._tasks.append(task)


class BSRecipe(object):
    """Class representing buildstats of one recipe"""
    def __init__(self, name, epoch, version, revision):
        self.name = name
        self.epoch = epoch
        self.version = version
        self.revision = revision
        if epoch is None:
            self.nevr = "{}-{}-{}".format(name, version, revision)
        else:
            self.nevr = "{}-{}_{}-{}".format(name, epoch, version, revision)
        self.tasks = {}

    def aggregate(self, bsrecipe):
        """Aggregate data of another recipe buildstats"""
        if self.nevr != bsrecipe.nevr:
            raise ValueError("Refusing to aggregate buildstats, recipe version "
                             "differs: {} vs. {}".format(self.nevr, bsrecipe.nevr))
        if set(self.tasks.keys()) != set(bsrecipe.tasks.keys()):
            raise ValueError("Refusing to aggregate buildstats, set of tasks "
                             "in {} differ".format(self.name))

        for taskname, taskdata in bsrecipe.tasks.items():
            if not isinstance(self.tasks[taskname], BSTaskAggregate):
                self.tasks[taskname] = BSTaskAggregate([self.tasks[taskname]])
            self.tasks[taskname].append(taskdata)


class BuildStats(dict):
    """Class representing buildstats of one build"""

    @property
    def num_tasks(self):
        """Get number of tasks"""
        num = 0
        for recipe in self.values():
            num += len(recipe.tasks)
        return num

    @classmethod
    def from_json(cls, bs_json):
        """Create new BuildStats object from JSON object"""
        buildstats = cls()
        for recipe in bs_json:
            if recipe['name'] in buildstats:
                raise BSError("Cannot handle multiple versions of the same "
                              "package ({})".format(recipe['name']))
            bsrecipe = BSRecipe(recipe['name'], recipe['epoch'],
                                recipe['version'], recipe['revision'])
            for task, data in recipe['tasks'].items():
                bsrecipe.tasks[task] = BSTask(data)

            buildstats[recipe['name']] = bsrecipe

        return buildstats

    @staticmethod
    def from_file_json(path):
        """Load buildstats from a JSON file"""
        with open(path) as fobj:
            bs_json = json.load(fobj)
        return BuildStats.from_json(bs_json)


    @staticmethod
    def split_nevr(nevr):
        """Split name and version information from recipe "nevr" string"""
        n_e_v, revision = nevr.rsplit('-', 1)
        match = re.match(r'^(?P<name>\S+)-((?P<epoch>[0-9]{1,5})_)?(?P<version>[0-9]\S*)$',
                         n_e_v)
        if not match:
            # If we're not able to parse a version starting with a number, just
            # take the part after last dash
            match = re.match(r'^(?P<name>\S+)-((?P<epoch>[0-9]{1,5})_)?(?P<version>[^-]+)$',
                             n_e_v)
        name = match.group('name')
        version = match.group('version')
        epoch = match.group('epoch')
        return name, epoch, version, revision

    @classmethod
    def from_dir(cls, path):
        """Load buildstats from a buildstats directory"""
        if not os.path.isfile(os.path.join(path, 'build_stats')):
            raise BSError("{} does not look like a buildstats directory".format(path))

        log.debug("Reading buildstats directory %s", path)

        buildstats = cls()
        subdirs = os.listdir(path)
        for dirname in subdirs:
            recipe_dir = os.path.join(path, dirname)
            if not os.path.isdir(recipe_dir):
                continue
            name, epoch, version, revision = cls.split_nevr(dirname)
            bsrecipe = BSRecipe(name, epoch, version, revision)
            for task in os.listdir(recipe_dir):
                bsrecipe.tasks[task] = BSTask.from_file(
                    os.path.join(recipe_dir, task))
            if name in buildstats:
                raise BSError("Cannot handle multiple versions of the same "
                              "package ({})".format(name))
            buildstats[name] = bsrecipe

        return buildstats

    def aggregate(self, buildstats):
        """Aggregate other buildstats into this"""
        if set(self.keys()) != set(buildstats.keys()):
            raise ValueError("Refusing to aggregate buildstats, set of "
                             "recipes is different")
        for pkg, data in buildstats.items():
            self[pkg].aggregate(data)


def diff_buildstats(bs1, bs2, stat_attr, min_val=None, min_absdiff=None):
    """Compare the tasks of two buildstats"""
    tasks_diff = []
    pkgs = set(bs1.keys()).union(set(bs2.keys()))
    for pkg in pkgs:
        tasks1 = bs1[pkg].tasks if pkg in bs1 else {}
        tasks2 = bs2[pkg].tasks if pkg in bs2 else {}
        if not tasks1:
            pkg_op = '+'
        elif not tasks2:
            pkg_op = '-'
        else:
            pkg_op = ' '

        for task in set(tasks1.keys()).union(set(tasks2.keys())):
            task_op = ' '
            if task in tasks1:
                val1 = getattr(bs1[pkg].tasks[task], stat_attr)
            else:
                task_op = '+'
                val1 = 0
            if task in tasks2:
                val2 = getattr(bs2[pkg].tasks[task], stat_attr)
            else:
                val2 = 0
                task_op = '-'

            if val1 == 0:
                reldiff = float('inf')
            else:
                reldiff = 100 * (val2 - val1) / val1

            if min_val and max(val1, val2) < min_val:
                log.debug("Filtering out %s:%s (%s)", pkg, task,
                          max(val1, val2))
                continue
            if min_absdiff and abs(val2 - val1) < min_absdiff:
                log.debug("Filtering out %s:%s (difference of %s)", pkg, task,
                          val2-val1)
                continue
            tasks_diff.append(TaskDiff(pkg, pkg_op, task, task_op, val1, val2,
                                       val2-val1, reldiff))
    return tasks_diff
