import os
import re
import sys
import json
import copy
import base64
import shlex
import _common
import _metric_resolvers as mr
import _test_const as consts
import _requirements as reqs
import StringIO
import subprocess
import collections

import ymake


BLOCK_SEPARATOR = '============================================================='


def ontest_data(unit, *args):
    ymake.report_configure_error("TEST_DATA is removed in favour of DATA")


def save_in_file(filepath, data):
    if filepath:
        with open(filepath, 'a') as file_handler:
            if os.stat(filepath).st_size == 0:
                print >>file_handler,  BLOCK_SEPARATOR
            print >> file_handler, data


def prepare_recipes(data):
    data = data.replace('"USE_RECIPE_DELIM"', "\n")
    data = data.replace("$TEST_RECIPES_VALUE", "")
    return base64.b64encode(data or "")


def prepare_env(data):
    data = data.replace("$TEST_ENV_VALUE", "")
    return serialize_list(shlex.split(data))


def validate_test(kw, is_fuzz_test):
    def get_list(key):
        return deserialize_list(kw.get(key, ""))

    valid_kw = copy.deepcopy(kw)
    errors = []
    has_fatal_error = False

    if valid_kw.get('SCRIPT-REL-PATH') == 'boost.test':
        project_path = valid_kw.get('BUILD-FOLDER-PATH', "")
        if not project_path.startswith(("mail", "maps", "metrika", "devtools")):
            errors.append("BOOSTTEST is not allowed here")
            has_fatal_error = True
    elif valid_kw.get('SCRIPT-REL-PATH') == 'ytest.py':
        project_path = valid_kw.get('BUILD-FOLDER-PATH', "")
        if not project_path.startswith("yweb/antispam") and not project_path.startswith("devtools"):
            errors.append("FLEUR test is not allowed here")
            has_fatal_error = True
    elif valid_kw.get('SCRIPT-REL-PATH') == 'gtest':
        project_path = valid_kw.get('BUILD-FOLDER-PATH', "")
        if not project_path.startswith(("mail", "devtools")):
            errors.append("GTEST is not allowed here")
            has_fatal_error = True

    size_timeout = collections.OrderedDict(sorted(consts.TestSize.DefaultTimeouts.items(), key=lambda t: t[1]))

    size = valid_kw.get('SIZE', consts.TestSize.Small).lower()
    tags = get_list("TAG")
    is_fat = 'ya:fat' in tags
    requirements = {}
    valid_requirements = {'cpu', 'disk_usage', 'ram', 'ram_disk', 'container', 'sb', 'sb_vault'}
    for req in get_list("REQUIREMENTS"):
        if ":" in req:
            req_name, req_value = req.split(":", 1)
            if req_name not in valid_requirements:
                errors.append("Unknown requirement: [[imp]]{}[[rst]], choose from [[imp]]{}[[rst]]".format(req_name, ", ".join(sorted(valid_requirements))))
                continue
            elif req_name in ('disk_usage'):
                if mr.resolve_value(req_value.lower()) is None:
                    errors.append("Cannot convert [[imp]]{}[[rst]] to the proper requirement value".format(req_value))
                    continue
            # TODO: Remove this special rules for ram and cpu requirements of FAT-tests
            elif is_fat and req_name in ('ram', 'cpu', 'ram_disk'):
                if req_value.strip() == 'all':
                    pass
                elif mr.resolve_value(req_value) is None:
                    errors.append("Cannot convert [[imp]]{}[[rst]]: [[imp]]{}[[rst]] to the proper requirement value".format(req_name, req_value))
                    continue
            elif req_name == 'ram_disk':
                if req_value.strip() == 'all':
                    pass
                else:
                    ram_disk_errors = reqs.check_ram_disk(mr.resolve_value(req_value), size)
                    # XXX
                    # errors += ram_disk_errors
                    if ram_disk_errors:
                        req_value = str(consts.TestSize.get_default_requirements(size).get(consts.TestRequirements.RamDisk))
            elif req_name == 'ram':
                if req_value.strip() == 'all':
                    pass
                else:
                    ram_errors = reqs.check_ram(mr.resolve_value(req_value), size)
                    errors += ram_errors
                    if ram_errors:
                        req_value = str(consts.TestSize.get_default_requirements(size).get(consts.TestRequirements.Ram))
            elif req_name == 'cpu':
                if req_value.strip() == 'all' and is_fuzz_test:
                    pass
                else:
                    cpu_errors = reqs.check_cpu(mr.resolve_value(req_value), size)
                    # XXX
                    # errors += cpu_errors
                    if cpu_errors:
                        req_value = str(consts.TestSize.get_default_requirements(size).get(consts.TestRequirements.Cpu))
            elif req_name == "sb_vault":
                if not re.match("\w+=(value|file)\:\w+\:\w+", req_value):
                    errors.append("sb_vault value '{}' should follow pattern <ENV_NAME>=:<value|file>:<owner>:<vault key>".format(req_value))
                    continue
                req_value = ",".join(filter(None, [requirements.get(req_name), req_value]))
            requirements[req_name] = req_value
        else:
            errors.append("Invalid requirement syntax [[imp]]{}[[rst]]: expect <requirement>:<value>".format(req))

    in_autocheck = "ya:not_autocheck" not in tags and 'ya:manual' not in tags
    invalid_requirements_for_distbuild = [requirement for requirement in requirements.keys() if requirement not in ('ram', 'cpu')]
    sb_tags = [tag for tag in tags if tag.startswith('sb:')]
    # XXX remove when the dust settles
    if 'ya:force_distbuild' in tags:
        errors.append("Don't use ya:force_distbuild tag. Distbuild is default build system for autocheck. You can specify only ya:force_sandbox if you want")
        has_fatal_error = True

    if is_fat:
        if in_autocheck and 'ya:force_sandbox' not in tags:
            if invalid_requirements_for_distbuild:
                errors.append("'{}' REQUIREMENTS options can be used only for FAT tests with ya:force_sandbox tag. Add TAG(ya:force_sandbox) or remove option.".format(invalid_requirements_for_distbuild))
                has_fatal_error = True
            if sb_tags:
                errors.append("You can set sandbox tags '{}' only for FAT tests with ya:force_sandbox. Add TAG(ya:force_sandbox) or remove sandbox tags.".format(sb_tags))
                has_fatal_error = True
    else:
        if 'ya:force_sandbox' in tags:
            errors.append('ya:force_sandbox can be used with LARGE tests only')
            has_fatal_error = True

    if 'ya:privileged' in tags and 'container' not in requirements:
        errors.append("Only tests with 'container' requirement can have 'ya:privileged' tag")
        has_fatal_error = True

    if 'ya:privileged' in tags and not is_fat:
        errors.append("Only fat tests can have 'ya:privileged' tag")
        has_fatal_error = True

    if size not in size_timeout:
        errors.append("Unknown test size: [[imp]]{}[[rst]], choose from [[imp]]{}[[rst]]".format(size.upper(), ", ".join([sz.upper() for sz in size_timeout.keys()])))
        has_fatal_error = True
    else:
        try:
            timeout = int(valid_kw.get('TEST-TIMEOUT', size_timeout[size]) or size_timeout[size])
            script_rel_path = valid_kw.get('SCRIPT-REL-PATH')
            if timeout < 0:
                raise Exception("Timeout must be > 0")
            if size_timeout[size] < timeout and in_autocheck and script_rel_path != 'java.style':
                suggested_size = None
                for s, t in size_timeout.items():
                    if timeout <= t:
                        suggested_size = s
                        break

                if suggested_size:
                    suggested_size = ", suggested size: [[imp]]{}[[rst]]".format(suggested_size.upper())
                else:
                    suggested_size = ""
                errors.append("Max allowed timeout for test size [[imp]]{}[[rst]] is [[imp]]{} sec[[rst]]{}".format(size.upper(), size_timeout[size], suggested_size))
                has_fatal_error = True
        except Exception as e:
            errors.append("Error when parsing test timeout: [[bad]]{}[[rst]]".format(e))
            has_fatal_error = True

        for req in ["container", "disk"]:
            if req in requirements and not is_fat:
                errors.append("Only [[imp]]FAT[[rst]] tests can have [[imp]]{}[[rst]] requirement".format(req))
                has_fatal_error = True

        if in_autocheck and size == consts.TestSize.Large and not is_fat:
            errors.append("LARGE test must have ya:fat tag")
            has_fatal_error = True

        if is_fat and size != consts.TestSize.Large:
            errors.append("Only LARGE test may have ya:fat tag")
            has_fatal_error = True

        requiremtens_list = []
        for req_name, req_value in requirements.iteritems():
            requiremtens_list.append(req_name + ":" + req_value)
        valid_kw['REQUIREMENTS'] = serialize_list(requiremtens_list)

    if valid_kw.get("FUZZ-OPTS"):
        for option in get_list("FUZZ-OPTS"):
            if not option.startswith("-"):
                errors.append("Unrecognized fuzzer option '[[imp]]{}[[rst]]'. All fuzzer options should start with '-'".format(option))
                has_fatal_error = True
                break
            eqpos = option.find("=")
            if eqpos == -1 or len(option) == eqpos + 1:
                errors.append("Unrecognized fuzzer option '[[imp]]{}[[rst]]'. All fuzzer options should obtain value specified after '='".format(option))
                has_fatal_error = True
                break
            if option[eqpos - 1] == " " or option[eqpos + 1] == " ":
                errors.append("Spaces are not allowed: '[[imp]]{}[[rst]]'".format(option))
                has_fatal_error = True
                break
            if option[:eqpos] in ("-runs", "-dict", "-jobs", "-workers", "-artifact_prefix", "-print_final_stats"):
                errors.append("You can't use '[[imp]]{}[[rst]]' - it will be automatically calculated or configured during run".format(option))
                has_fatal_error = True
                break

    if valid_kw.get("USE_ARCADIA_PYTHON") == "yes" and valid_kw.get("SCRIPT-REL-PATH") == "py.test":
        errors.append("PYTEST_SCRIPT is deprecated")
        has_fatal_error = True

    if valid_kw.get('SPLIT-FACTOR'):
        if valid_kw.get('FORK-MODE') == 'none':
            errors.append('SPLIT_FACTOR must be use with FORK_TESTS() or FORK_SUBTESTS() macro')
            has_fatal_error = True
        try:
            value = int(valid_kw.get('SPLIT-FACTOR'))
            if value <= 0:
                raise ValueError("must be > 0")
        except ValueError as e:
            errors.append('Incorrect SPLIT_FACTOR value: {}'.format(e))
            has_fatal_error = True

    if has_fatal_error:
        return None, errors

    return valid_kw, errors


def dump_test(kw, is_fuzz_test=False):
    valid_kw, errors = validate_test(kw, is_fuzz_test)
    if errors:
        for e in errors:
            ymake.report_configure_error(e)
    if valid_kw is None:
        return None
    string_handler = StringIO.StringIO()
    for k, v in valid_kw.iteritems():
        print >>string_handler, k + ': ' + v
    print >>string_handler, BLOCK_SEPARATOR
    data = string_handler.getvalue()
    string_handler.close()
    return data


def serialize_list(lst):
    lst = filter(None, lst)
    return '\"' + ';'.join(lst) + '\"' if lst else ''


def deserialize_list(val):
    return filter(None, val.replace('"', "").split(";"))


def get_values_list(unit, key):
    res = map(str.strip, (unit.get(key) or '').replace('$' + key, '').strip().split())
    return [r for r in res if r and r not in ['""', "''"]]


def strip_roots(path):
    for prefix in ["$B/", "$S/"]:
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def get_unit_list_variable(unit, name):
    items = unit.get(name)
    if items:
        items = items.split(' ')
        assert items[0] == "${}".format(name), (items, name)
        return items[1:]
    return []


def implies(a, b):
    return bool((not a) or b)


def match_coverage_extractor_requirements(unit):
    # we shouldn't add test if
    return all([
        # tests are not requested
        unit.get("TESTS_REQUESTED"),
        # build doesn't imply clang coverage, which supports segment extraction from the binaries
        unit.get("CLANG_COVERAGE"),
        # contrib wasn't requested
        implies(strip_roots(unit.path()).startswith("contrib/"), unit.get("ENABLE_CONTRIB_COVERAGE")),
    ])


def onadd_ytest(unit, *args):
    keywords = {"DEPENDS": -1, "DATA": -1, "TIMEOUT": 1, "FORK_MODE": 1, "SPLIT_FACTOR": 1,
                "FORK_SUBTESTS": 0, "FORK_TESTS": 0}
    flat_args, spec_args = _common.sort_by_keywords(keywords, args)

    if flat_args[1] == "fuzz.test":
        unit.ondata("arcadia/fuzzing/{}/corpus.json".format(strip_roots(unit.path())))
    elif flat_args[1] == "coverage.extractor" and not match_coverage_extractor_requirements(unit):
        # XXX
        # Current ymake implementation doesn't allow to call macro inside the 'when' body
        # that's why we add ADD_YTEST(coverage.extractor) to every PROGRAM entry and check requirements later
        return

    fork_mode = []
    if 'FORK_SUBTESTS' in spec_args:
        fork_mode.append('subtests')
    if 'FORK_TESTS' in spec_args:
        fork_mode.append('tests')
    fork_mode = fork_mode or spec_args.get('FORK_MODE', []) or unit.get('TEST_FORK_MODE').split()
    fork_mode = ' '.join(fork_mode) if fork_mode else ''

    test_record = {
        'TEST-NAME': flat_args[0],
        'SCRIPT-REL-PATH': flat_args[1],
        'TESTED-PROJECT-NAME': unit.name(),
        'TESTED-PROJECT-FILENAME': unit.filename(),
        'SOURCE-FOLDER-PATH': unit.resolve(unit.path()),
        'BUILD-FOLDER-PATH': strip_roots(unit.path()),
        'BINARY-PATH': strip_roots(os.path.join(unit.path(), unit.filename())),
        'CUSTOM-DEPENDENCIES': ' '.join(spec_args.get('DEPENDS', []) + get_values_list(unit, 'TEST_DEPENDS_VALUE')),
        'TEST-RECIPES': prepare_recipes(unit.get("TEST_RECIPES_VALUE")),
        'TEST-ENV': prepare_env(unit.get("TEST_ENV_VALUE")),
        #  'TEST-PRESERVE-ENV': 'da',
        'TEST-DATA': serialize_list(_common.filter_out_by_keyword(spec_args.get('DATA', []) + get_values_list(unit, 'TEST_DATA_VALUE'), 'AUTOUPDATED')),
        'TEST-TIMEOUT': ''.join(spec_args.get('TIMEOUT', [])) or unit.get('TEST_TIMEOUT') or '',
        'FORK-MODE': fork_mode,
        'SPLIT-FACTOR': ''.join(spec_args.get('SPLIT_FACTOR', [])) or unit.get('TEST_SPLIT_FACTOR') or '',
        'SIZE': ''.join(spec_args.get('SIZE', [])) or unit.get('TEST_SIZE_NAME') or '',
        'TAG': serialize_list(spec_args.get('TAG', []) + get_values_list(unit, 'TEST_TAGS_VALUE')),
        'REQUIREMENTS': serialize_list(spec_args.get('REQUIREMENTS', []) + get_values_list(unit, 'TEST_REQUIREMENTS_VALUE')),
        'TEST-CWD': unit.get('TEST_CWD_VALUE') or '',
        'FUZZ-DICTS': serialize_list(spec_args.get('FUZZ_DICTS', []) + get_unit_list_variable(unit, 'FUZZ_DICTS_VALUE')),
        'FUZZ-OPTS': serialize_list(spec_args.get('FUZZ_OPTS', []) + get_unit_list_variable(unit, 'FUZZ_OPTS_VALUE')),
        'BLOB': unit.get('TEST_BLOB_DATA') or '',
        'SKIP_TEST': unit.get('SKIP_TEST_VALUE') or '',
    }

    is_fuzz_test = flat_args[1] == 'fuzz.test' and unit.get('FUZZING') == 'yes'
    if is_fuzz_test:
        # use all cores if fuzzing requested
        test_record['REQUIREMENTS'] = serialize_list(filter(None, deserialize_list(test_record['REQUIREMENTS']) + ["cpu:all", "ram:all"]))

    data = dump_test(test_record, is_fuzz_test=is_fuzz_test)
    if data:
        unit.set_property(["DART_DATA", data])
        save_in_file(unit.get('TEST_DART_OUT_FILE'), data)


def onadd_test(unit, *args):
    flat_args, spec_args = _common.sort_by_keywords({"DEPENDS": -1, "TIMEOUT": 1, "DATA": -1, "TAG": -1, "REQUIREMENTS": -1, "FORK_MODE": 1,
                                                     "SPLIT_FACTOR": 1, "FORK_SUBTESTS": 0, "FORK_TESTS": 0, "SIZE": 1}, args)
    test_type = flat_args[0]
    test_files = flat_args[1:]
    if test_type in ["PEP8", "PY_FLAKES"]:
        return
        # unit_path = unit.path()
        # paths = []
        # for test_file in test_files:
        #     if test_file == ".":
        #         path_to_check = unit_path
        #     else:
        #         path_to_check = os.path.join(unit_path, test_file)
        #     paths.append(path_to_check)
        # return onadd_check(unit, *tuple([test_type] + sorted(paths)))

    custom_deps = spec_args.get('DEPENDS', [])
    timeout = spec_args.get("TIMEOUT", [])
    if timeout:
        timeout = timeout[0]
    else:
        timeout = '0'
    fork_mode = []
    if 'FORK_SUBTESTS' in spec_args:
        fork_mode.append('subtests')
    if 'FORK_TESTS' in spec_args:
        fork_mode.append('tests')
    fork_mode = fork_mode or spec_args.get('FORK_MODE', [])
    split_factor = ''.join(spec_args.get('SPLIT_FACTOR', [])) or ''
    test_size = ''.join(spec_args.get('SIZE', [])) or 'SMALL'
    test_dir = unit.resolve(os.path.join(unit.path()))
    tags = spec_args.get('TAG', []) + get_values_list(unit, 'TEST_TAGS_VALUE')
    requirements = spec_args.get('REQUIREMENTS', []) + get_values_list(unit, 'TEST_REQUIREMENTS_VALUE')
    test_data = spec_args.get("DATA", []) + get_values_list(unit, 'TEST_DATA_VALUE')
    python_paths = get_values_list(unit, 'TEST_PYTHON_PATH_VALUE')
    if test_type == "PY_TEST":
        old_pytest = True
    else:
        old_pytest = False

    _dump_test(unit, test_type, test_files, timeout, test_dir, custom_deps, test_data, python_paths, split_factor, fork_mode, test_size, tags, requirements, None, old_pytest)


def onadd_check(unit, *args):
    flat_args, spec_args = _common.sort_by_keywords({"DEPENDS": -1, "TIMEOUT": 1, "DATA": -1, "TAG": -1, "REQUIREMENTS": -1, "FORK_MODE": 1,
                                                     "SPLIT_FACTOR": 1, "FORK_SUBTESTS": 0, "FORK_TESTS": 0, "SIZE": 1}, args)
    check_type = flat_args[0]
    test_dir = unit.resolve(os.path.join(unit.path()))

    test_timeout = ''
    if check_type in ["PEP8", "PYFLAKES", "PY_FLAKES"]:
        script_rel_path = "py.lint.pylint"
    elif check_type == "JAVA_STYLE":
        if len(flat_args) < 2:
            raise Exception("Not enough arguments for JAVA_STYLE check")
        check_level = flat_args[1]
        allowed_levels = {
            'base': '/yandex_checks.xml',
            'strict': '/yandex_checks_strict.xml',
        }
        if check_level not in allowed_levels:
            raise Exception('{} is not allowed in LINT(), use one of {}'.format(check_level, allowed_levels.keys()))
        flat_args[1] = allowed_levels[check_level]
        script_rel_path = "java.style"
        test_timeout = '120'
    else:
        script_rel_path = check_type

    use_arcadia_python = unit.get('USE_ARCADIA_PYTHON')
    test_record = {
        'TEST-NAME': check_type.lower(),
        'TEST-TIMEOUT': test_timeout,
        'SCRIPT-REL-PATH': script_rel_path,
        'TESTED-PROJECT-NAME': os.path.basename(test_dir),
        'SOURCE-FOLDER-PATH': test_dir,
        'CUSTOM-DEPENDENCIES': " ".join(spec_args.get('DEPENDS', [])),
        'TEST-DATA': '',
        'SPLIT-FACTOR': '',
        'FORK-MODE': '',
        'FORK-TEST-FILES': '',
        'SIZE': 'SMALL',
        'TAG': '',
        'REQUIREMENTS': '',
        'USE_ARCADIA_PYTHON': use_arcadia_python or '',
        'OLD_PYTEST': 'no',
        'PYTHON-PATHS': '',
        'FILES': serialize_list(flat_args[1:])
    }
    data = dump_test(test_record)
    if data:
        unit.set_property(["DART_DATA", data])
        save_in_file(unit.get('TEST_DART_OUT_FILE'), data)


def onadd_check_py_imports(unit, *args):
    if unit.get('NO_CHECK_IMPORTS_FOR_VALUE').strip() == "":
        return
    check_type = "py.imports"
    test_dir = unit.resolve(os.path.join(unit.path()))

    use_arcadia_python = unit.get('USE_ARCADIA_PYTHON')
    test_record = {
        'TEST-NAME': "pyimports",
        'TEST-TIMEOUT': '',
        'SCRIPT-REL-PATH': check_type,
        'TESTED-PROJECT-NAME': os.path.basename(test_dir),
        'SOURCE-FOLDER-PATH': test_dir,
        'CUSTOM-DEPENDENCIES': '',
        'TEST-DATA': '',
        'SPLIT-FACTOR': '',
        'FORK-MODE': '',
        'FORK-TEST-FILES': '',
        'SIZE': 'SMALL',
        'TAG': '',
        'USE_ARCADIA_PYTHON': use_arcadia_python or '',
        'OLD_PYTEST': 'no',
        'PYTHON-PATHS': '',
        'FILES': serialize_list(["{}/{}".format(strip_roots(unit.path()), unit.filename())])
    }
    if unit.get('NO_CHECK_IMPORTS_FOR_VALUE') != "None":
        test_record["NO-CHECK"] = serialize_list(get_values_list(unit, 'NO_CHECK_IMPORTS_FOR_VALUE') or ["*"])
    else:
        test_record["NO-CHECK"] = ''
    data = dump_test(test_record)
    if data:
        unit.set_property(["DART_DATA", data])
        save_in_file(unit.get('TEST_DART_OUT_FILE'), data)


def onadd_pytest_script(unit, *args):
    unit.set(["PYTEST_BIN", "no"])
    custom_deps = get_values_list(unit, 'TEST_DEPENDS_VALUE')
    timeout = filter(None, [unit.get(["TEST_TIMEOUT"])])
    if timeout:
        timeout = timeout[0]
    else:
        timeout = '0'
    test_type = args[0]
    fork_mode = unit.get('TEST_FORK_MODE').split() or ''
    split_factor = unit.get('TEST_SPLIT_FACTOR') or ''
    test_size = unit.get('TEST_SIZE_NAME') or ''

    test_dir = unit.resolve(os.path.join(unit.path()))
    test_files = get_values_list(unit, 'TEST_SRCS_VALUE')
    tags = get_values_list(unit, 'TEST_TAGS_VALUE')
    requirements = get_values_list(unit, 'TEST_REQUIREMENTS_VALUE')
    test_data = get_values_list(unit, 'TEST_DATA_VALUE')
    python_paths = get_values_list(unit, 'TEST_PYTHON_PATH_VALUE')
    binary_path = None
    test_cwd = unit.get('TEST_CWD_VALUE') or ''
    _dump_test(unit, test_type, test_files, timeout, test_dir, custom_deps, test_data, python_paths, split_factor, fork_mode, test_size, tags, requirements, binary_path, test_cwd=test_cwd)


def onadd_pytest_bin(unit, *args):
    flat, kws = _common.sort_by_keywords({'RUNNER_BIN': 1}, args)
    if flat:
        ymake.report_configure_error(
            'Unknown arguments found while processing add_pytest_bin macro: {!r}'
            .format(flat)
        )

    runner_bin = kws.get('RUNNER_BIN', [None])[0]
    add_test_to_dart(unit, "pytest.bin", runner_bin=runner_bin)


def add_test_to_dart(unit, test_type, binary_path=None, runner_bin=None):
    custom_deps = get_values_list(unit, 'TEST_DEPENDS_VALUE')
    timeout = filter(None, [unit.get(["TEST_TIMEOUT"])])
    if timeout:
        timeout = timeout[0]
    else:
        timeout = '0'
    fork_mode = unit.get('TEST_FORK_MODE').split() or ''
    split_factor = unit.get('TEST_SPLIT_FACTOR') or ''
    test_size = unit.get('TEST_SIZE_NAME') or ''
    test_cwd = unit.get('TEST_CWD_VALUE') or ''

    test_dir = unit.resolve(os.path.join(unit.path()))
    test_files = get_values_list(unit, 'TEST_SRCS_VALUE')
    tags = get_values_list(unit, 'TEST_TAGS_VALUE')
    requirements = get_values_list(unit, 'TEST_REQUIREMENTS_VALUE')
    test_data = get_values_list(unit, 'TEST_DATA_VALUE')
    python_paths = get_values_list(unit, 'TEST_PYTHON_PATH_VALUE')
    if not binary_path:
        binary_path = os.path.join(unit.path(), unit.filename())
    _dump_test(unit, test_type, test_files, timeout, test_dir, custom_deps, test_data, python_paths, split_factor, fork_mode, test_size, tags, requirements, binary_path, test_cwd=test_cwd, runner_bin=runner_bin)


def extract_java_system_properties(unit, args):
    props = []

    if len(args) % 2:
        print>>sys.stderr, 'wrong use of SYSTEM_PROPERTIES in {}: odd number of arguments'.format(unit.path())  # TODO: configure error
        return []

    for x, y in zip(args[::2], args[1::2]):
        if x == 'FILE':
            if y.startswith('${BINDIR}') or y.startswith('${ARCADIA_BUILD_ROOT}') or y.startswith('/'):
                print>>sys.stderr, 'wrong use of SYSTEM_PROPERTIES in {}: absolute/build file path {}'.format(unit.path(), y)  # TODO: configure error
                continue

            y = _common.rootrel_arc_src(y, unit)
            if not os.path.exists(unit.resolve('$S/' + y)):
                print>>sys.stderr, 'wrong use of SYSTEM_PROPERTIES in {}: can\'t resolve {}'.format(unit.path(), y)  # TODO: configure error
                continue

            y = '${ARCADIA_ROOT}/' + y

            props.append({'type': 'file', 'path': y})

        else:
            props.append({'type': 'inline', 'key': x, 'value': y})

    return props


def onjava_test(unit, *args):
    assert unit.get('MODULE_TYPE') is not None

    if unit.get('MODULE_TYPE') == 'JTEST_FOR':
        if not unit.get('UNITTEST_DIR'):
            print>>sys.stderr, 'skip JTEST_FOR in {}: no args provided'.format(unit.path())  # TODO: configure error
            return  # do not add such tests into dart

    path = strip_roots(unit.path())

    test_data = get_values_list(unit, 'TEST_DATA_VALUE')
    test_data.append('arcadia/build/scripts')

    props = extract_java_system_properties(unit, get_values_list(unit, 'SYSTEM_PROPERTIES_VALUE'))
    for prop in props:
        if prop['type'] == 'file':
            test_data.append(prop['path'].replace('${ARCADIA_ROOT}', 'arcadia'))

    props = base64.b64encode(json.dumps(props, encoding='utf-8'))

    test_cwd = unit.get('TEST_CWD_VALUE') or ''  # TODO: validate test_cwd value

    script_rel_path = 'testng.test' if unit.get('MODULE_TYPE') == 'TESTNG' else 'junit.test'

    test_record = {
        'SOURCE-FOLDER-PATH': path,
        'TEST-NAME': '-'.join([os.path.basename(os.path.dirname(path)), os.path.basename(path)]),
        'SCRIPT-REL-PATH': script_rel_path,
        'TEST-TIMEOUT': unit.get('TEST_TIMEOUT') or '',
        'TESTED-PROJECT-NAME': path,
        'TEST-ENV': prepare_env(unit.get("TEST_ENV_VALUE")),
        #  'TEST-PRESERVE-ENV': 'da',
        'TEST-DATA': serialize_list(_common.filter_out_by_keyword(test_data, 'AUTOUPDATED')),
        'FORK-MODE': unit.get('TEST_FORK_MODE') or '',
        'SPLIT-FACTOR': unit.get('TEST_SPLIT_FACTOR') or '',
        'CUSTOM-DEPENDENCIES': ' '.join(get_values_list(unit, 'TEST_DEPENDS_VALUE')),
        'TAG': serialize_list(get_values_list(unit, 'TEST_TAGS_VALUE')),
        'SIZE': unit.get('TEST_SIZE_NAME') or '',
        'REQUIREMENTS': serialize_list(get_values_list(unit, 'TEST_REQUIREMENTS_VALUE')),
        'TEST-RECIPES': prepare_recipes(unit.get("TEST_RECIPES_VALUE")),

        # JTEST/JTEST_FOR only
        'MODULE_TYPE': unit.get('MODULE_TYPE'),
        'UNITTEST_DIR': unit.get('UNITTEST_DIR') or '',
        'JVM_ARGS': serialize_list(get_values_list(unit, 'JVM_ARGS_VALUE')),
        'SYSTEM_PROPERTIES': props,
        'TEST-CWD': test_cwd,
        'SKIP_TEST': unit.get('SKIP_TEST_VALUE') or '',
    }

    data = dump_test(test_record)
    if data:
        unit.set_property(['DART_DATA', data])


def onjava_test_deps(unit, *args):
    assert unit.get('MODULE_TYPE') is not None

    path = strip_roots(unit.path())

    test_record = {
        'SOURCE-FOLDER-PATH': path,
        'TEST-NAME': '-'.join([os.path.basename(os.path.dirname(path)), os.path.basename(path), 'dependencies']).strip('-'),
        'SCRIPT-REL-PATH': 'java.dependency.test',
        'TEST-TIMEOUT': '',
        'TESTED-PROJECT-NAME': path,
        'TEST-DATA': '',
        'FORK-MODE': '',
        'SPLIT-FACTOR': '',
        'CUSTOM-DEPENDENCIES': ' '.join(get_values_list(unit, 'TEST_DEPENDS_VALUE')),
        'TAG': '',
        'SIZE': 'SMALL',

        # JTEST/JTEST_FOR only
        'MODULE_TYPE': unit.get('MODULE_TYPE'),
        'UNITTEST_DIR': '',
        'SYSTEM_PROPERTIES': '',
        'TEST-CWD': '',
    }

    data = dump_test(test_record)
    unit.set_property(['DART_DATA', data])


def _dump_test(
        unit,
        test_type,
        test_files,
        timeout,
        test_dir,
        custom_deps,
        test_data,
        python_paths,
        split_factor,
        fork_mode,
        test_size,
        tags,
        requirements,
        binary_path='',
        old_pytest=False,
        test_cwd=None,
        runner_bin=None
):

    if test_type == "PY_TEST":
        script_rel_path = "py.test"
    elif test_type == "FLEUR":
        script_rel_path = "ytest.py"
    elif test_type == "PEP8":
        script_rel_path = "py.test.pep8"
    elif test_type == "PY_FLAKES":
        script_rel_path = "py.test.flakes"
    else:
        script_rel_path = test_type

    fork_test_files = unit.get('FORK_TEST_FILES_MODE')
    fork_mode = ' '.join(fork_mode) if fork_mode else ''
    use_arcadia_python = unit.get('USE_ARCADIA_PYTHON')
    if test_cwd:
        test_cwd = test_cwd.replace("$TEST_CWD_VALUE", "").replace('"MACRO_CALLS_DELIM"', "").strip()
    if binary_path:
        if fork_test_files == 'on':
            tests = test_files
        else:
            tests = [os.path.basename(binary_path)]
    else:
        tests = test_files
    for test_name in tests:
        test_record = {
            'TEST-NAME': os.path.splitext(test_name)[0],
            'TEST-TIMEOUT': timeout,
            'SCRIPT-REL-PATH': script_rel_path,
            'TESTED-PROJECT-NAME': test_name,
            'SOURCE-FOLDER-PATH': test_dir,
            'CUSTOM-DEPENDENCIES': " ".join(custom_deps),
            'TEST-ENV': prepare_env(unit.get("TEST_ENV_VALUE")),
            #  'TEST-PRESERVE-ENV': 'da',
            'TEST-DATA': serialize_list(_common.filter_out_by_keyword(test_data, 'AUTOUPDATED')),
            'TEST-RECIPES': prepare_recipes(unit.get("TEST_RECIPES_VALUE")),
            'SPLIT-FACTOR': split_factor,
            'FORK-MODE': fork_mode,
            'FORK-TEST-FILES': fork_test_files,
            'TEST-FILES': serialize_list(tests),
            'SIZE': test_size,
            'TAG': serialize_list(tags),
            'REQUIREMENTS': serialize_list(requirements),
            'USE_ARCADIA_PYTHON': use_arcadia_python or '',
            'OLD_PYTEST': 'yes' if old_pytest else 'no',
            'PYTHON-PATHS': serialize_list(python_paths),
            'TEST-CWD': test_cwd or '',
            'SKIP_TEST': unit.get('SKIP_TEST_VALUE') or '',
            'BUILD-FOLDER-PATH': strip_roots(unit.path()),
            'BLOB': unit.get('TEST_BLOB_DATA') or '',
        }
        if binary_path:
            test_record['BINARY-PATH'] = strip_roots(binary_path)
        if runner_bin:
            test_record['TEST-RUNNER-BIN'] = runner_bin
        data = dump_test(test_record)
        if data:
            unit.set_property(["DART_DATA", data])
            save_in_file(unit.get('TEST_DART_OUT_FILE'), data)


def onsetup_pytest_bin(unit, *args):
    use_arcadia_python = unit.get('USE_ARCADIA_PYTHON') == "yes"
    if use_arcadia_python:
        unit.onresource(['-', 'PY_MAIN={}'.format("library.python.pytest.main:main")])  # XXX
        unit.onadd_pytest_bin(list(args))
    else:
        unit.onno_platform()
        unit.onadd_pytest_script(["PY_TEST"])


def onrun(unit, *args):
    exectest_cmd = unit.get(["EXECTEST_COMMAND_VALUE"]) or ''
    exectest_cmd += "\n" + subprocess.list2cmdline(args)
    unit.set(["EXECTEST_COMMAND_VALUE", exectest_cmd])


def onsetup_exectest(unit, *args):
    command = unit.get(["EXECTEST_COMMAND_VALUE"]).replace("$EXECTEST_COMMAND_VALUE", "")
    if "PYTHON_BIN" in command:
        unit.ondepends('contrib/tools/python')
    unit.set(["TEST_BLOB_DATA", base64.b64encode(command)])
    add_test_to_dart(unit, "exectest", binary_path=os.path.join(unit.path(), unit.filename()).replace(".pkg", ""))


def onsetup_run_python(unit):
    if unit.get("USE_ARCADIA_PYTHON") == "yes":
        unit.ondepends('contrib/tools/python')
