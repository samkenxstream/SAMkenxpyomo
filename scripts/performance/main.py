#!/usr/bin/env python
#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright (c) 2008-2022
#  National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________

import argparse
import importlib
import gc
import logging
import os
import platform
import sys
import time
try:
    import ujson as json
except ImportError:
    import json

from collections import OrderedDict

import pyomo.common.unittest as unittest
from pyomo.common.timing import TicTocTimer


class TimingHandler(logging.Handler):
    def __init__(self):
        super(TimingHandler, self).__init__()
        self._testRecord = None
        self.enabled = True

    def setTest(self, testRecord):
        self._testRecord = testRecord['timing'] = OrderedDict()

    def clearTest(self):
        self._testRecord = None

    def emit(self, record):
        if self._testRecord is None:
            return
        cat_name = record.msg.__class__.__name__
        if cat_name in self._testRecord:
            cat_data = self._testRecord[cat_name]
        else:
            cat_data = self._testRecord[cat_name] = OrderedDict()
        try:
            name = record.msg.name
            val = record.msg.timer
        except AttributeError:
            name = None
            val = str(record.msg)
        if name in cat_data:
            try:
                cat_data[name].append(val)
            except AttributeError:
                cat_data[name] = [cat_data[name], val]
        else:
            cat_data[name] = val


class DataRecorder(object):
    def __init__(self, data):
        self._data = data
        self._timer = TicTocTimer()
        self._category = {}

        self._timingHandler = TimingHandler()
        timing_logger = logging.getLogger('pyomo.common.timing')
        timing_logger.setLevel(logging.INFO)
        timing_logger.addHandler(self._timingHandler)

    @unittest.pytest.fixture(autouse=True)
    def add_data_attribute(self, request):
        # set a class attribute on the invoking test context
        request.cls.testdata = OrderedDict()
        self._data[request.node.nodeid] = request.cls.testdata
        yield
        request.cls.testdata = None

    @unittest.pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        self._timingHandler.setTest(self._data[item.nodeid])
        # Trigger garbage collection (try and get a "clean" environment)
        gc.collect()
        gc.collect()
        self._timer.tic("")
        # Run the test
        yield
        # Collect the timing and clean up
        self._data[item.nodeid]['test_time'] = self._timer.toc("")
        self._timingHandler.clearTest()


def getProjectInfo(project):
    cwd = os.getcwd()
    sha = None
    diffs = []
    branch = None
    version = None
    try:
        _module = importlib.import_module(project)
        os.chdir(os.path.dirname(_module.__file__))
        sha = os.popen('git rev-parse HEAD').read().strip()
        diffs = os.popen('git diff-index --name-only HEAD').read()
        diffs = diffs.strip().split()
        branch = os.popen('git symbolic-ref -q --short HEAD').read().strip()
        version = _module.__version__
    finally:
        os.chdir(cwd)
    return {
        'branch': branch,
        'sha': sha,
        'diffs': diffs,
        'version': version,
    }


def getRunInfo(options):
    info = {
        'time': time.time(),
        'python_implementation': platform.python_implementation(),
        'python_version': tuple(sys.version_info),
        'python_build': platform.python_build(),
        'platform': platform.system(),
        'hostname': platform.node(),
    }
    if info['python_implementation'].lower() == 'pypy':
        info['pypy_version'] = tuple(sys.pypy_version_info)
    if options.cython:
        import Cython
        info['cython'] = tuple(int(x) for x in Cython.__version__.split('.'))
    for project in options.projects:
        info[project] = getProjectInfo(project)
    return info


def run_tests(options, argv):
    gc.collect()
    gc.collect()
    results = ( getRunInfo(options), OrderedDict() )
    recorder = DataRecorder(results[1])
    unittest.pytest.main(argv, plugins=[recorder])
    gc.collect()
    gc.collect()
    return results


def main(argv):
    parser = argparse.ArgumentParser(
        epilog="Remaining arguments are passed to pytest"
    )
    parser.add_argument(
        '-o', '--output',
        action='store',
        dest='output',
        default=None,
        help='Store the test results to the specified file.'
    )
    parser.add_argument(
        '-d', '--dir',
        action='store',
        dest='output_dir',
        default=None,
        help='Store the test results in the specified directory.  If -o '
        'is not specified, then a file name is automatically generated '
        'based on the first "main project" git branch and hash.'
    )
    parser.add_argument(
        '-p', '--project',
        action='append',
        dest='projects',
        default=[],
        help='Main project (used for generating and recording SHA and '
        'DIFF information)'
    )
    parser.add_argument(
        '-n', '--replicates',
        action='store',
        dest='replicates',
        type=int,
        default=1,
        help='Number of replicates to run.'
    )
    parser.add_argument(
        '--with-cython',
        action='store_true',
        dest='cython',
        help='Cythonization enabled.'
    )

    options, argv = parser.parse_known_args(argv)
    if not options.projects:
        options.projects.append('pyomo')
    # Pytest really, really wants the initial script to belong to the
    # "main" project being tested.
    argv[0] = options.projects[0]
    argv.append('-W ignore::Warning')

    results = tuple(run_tests(options, argv) for i in range(options.replicates))
    results = (results[0][0],) + tuple(r[1] for r in results)

    if options.output_dir:
        if not options.output:
            options.output = 'perf-%s-%s-%s-%s.json' % (
                results[0][options.projects[0]]['branch'],
                results[0][options.projects[0]]['sha'][:7] + (
                    '_mod' if results[0][options.projects[0]]['diffs'] else ''),
                results[0]['python_implementation'].lower() + (
                    '.'.join(str(i) for i in results[0]['python_version'][:3])),
                time.strftime('%y%m%d_%H%M', time.localtime())
            )
        options.output = os.path.join(options.output_dir, options.output)
    if options.output:
        print(f"Writing results to {options.output}")
        ostream = open(options.output, 'w')
        close_ostream = True
    else:
        ostream = sys.stdout
        close_ostream = False
    try:
        # Note: explicitly specify sort_keys=False so that ujson
        # preserves the OrderedDict keys in the JSON
        json.dump(results, ostream, indent=2, sort_keys=False)
    finally:
        if close_ostream:
            ostream.close()
    print("Performance run complete.")

if __name__ == '__main__':
    main(sys.argv)
