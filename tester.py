#!/usr/bin/env python3
"""VYP test runner.

Runs every ``*.vyp`` test found under ``tests/`` through the VYP compiler and
the ``vypint`` interpreter and reports passed / skipped / failed tests.

Uses only the Python standard library.
"""

import argparse
import difflib
import os
import re
import shutil
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

TESTS_DIR = "tests"
BUILD_DIR = "build"
DEFAULT_COMPILER = "./vypint"
DEFAULT_JAR = "vypint-1.0.jar"
DEFAULT_TIMEOUT = 10.0

DIFF_MAX_LINES = 40    # max lines of the stdout diff shown for one failure
OUTPUT_MAX_LINES = 20  # max lines of captured process output shown per failure

COMPILER_MEANINGS = {
    11: "lexical analysis error",
    12: "syntactic analysis error",
    13: "semantic error (type incompatibility)",
    14: "semantic error (missing/multiple definition, ...)",
    15: "target code generation error",
    19: "internal compiler error",
}

INTERPRETER_MEANINGS = {
    20: "invalid command-line arguments",
    21: "lexical or syntax error of VYPcode",
    22: "semantic error of VYPcode",
    25: "runtime error (invalid or non-existing label)",
    27: "runtime error (division by zero)",
    28: "runtime error (invalid memory access)",
    30: "internal interpreter error",
}

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


# --------------------------------------------------------------------- misc


class Palette:
    """Minimal ANSI color helper; rendering is disabled when not enabled."""

    def __init__(self, enabled=True):
        self.enabled = enabled

    def _wrap(self, code, text):
        if self.enabled:
            return "\033[{}m{}\033[0m".format(code, text)
        return text

    def red(self, text):
        return self._wrap("1;31", text)

    def green(self, text):
        return self._wrap("1;32", text)

    def yellow(self, text):
        return self._wrap("1;33", text)

    def cyan(self, text):
        return self._wrap("1;36", text)

    def dim(self, text):
        return self._wrap("90", text)

    def bold(self, text):
        return self._wrap("1", text)


P = Palette(enabled=False)


def warn(message):
    print(P.yellow("warning: " + message), file=sys.stderr)


def die(message):
    print(P.red("error: " + message), file=sys.stderr)
    sys.exit(2)


def fmt_seconds(value):
    return "{:g} s".format(value)


def indent(text, spaces):
    pad = " " * spaces
    return "\n".join((pad + line).rstrip() for line in text.splitlines())


def read_optional(path):
    """Return the file's bytes, or b'' when the file does not exist."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return b""


def tail_text(data, max_lines=OUTPUT_MAX_LINES):
    """First lines of captured process output, for failure reports."""
    if not data:
        return ""
    text = data.decode("utf-8", "replace").strip()
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + ["... (output truncated)"]
    return "\n".join(lines)


def rc_meaning(code, table):
    if code == 0:
        return "success"
    return table.get(code, "unknown code")


def run_command(cmd, timeout, input_bytes=None):
    """Run a command capturing stdout/stderr.

    Returns (process, None) on completion, or (None, error) where error is a
    subprocess.TimeoutExpired or OSError instance.
    """
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
    }
    if input_bytes is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = input_bytes
    try:
        return subprocess.run(cmd, **kwargs), None
    except subprocess.TimeoutExpired as exc:
        return None, exc
    except OSError as exc:
        return None, exc


# ---------------------------------------------------------------- test files


class Test:
    def __init__(self, suite, name, path):
        self.suite = suite  # None for tests directly in tests/, else POSIX-style subpath
        self.name = name    # file name without the .vyp extension
        self.path = path
        self.compiler_rc = 0
        self.interpreter_rc = 0
        self.required = []
        self.incompatible = []

    @property
    def full_name(self):
        return self.name if self.suite is None else "{}/{}".format(self.suite, self.name)


TEST_INFO_RE = re.compile(r"^//\s*TEST-INFO\s+(\S+)(?:\s+(.*\S))?\s*$")


def split_extensions(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_test_header(test):
    """Parse leading ``// TEST-INFO ...`` comments. Returns a list of warnings.

    Scanning stops at the first line that is neither blank nor a ``//`` comment,
    i.e. directives must appear at the very start of the file.
    """
    warnings = []
    with open(test.path, "r", encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue  # blank line inside the leading comment block
            if not line.startswith("//"):
                break  # first non-comment line: end of the header
            match = TEST_INFO_RE.match(line)
            if not match:
                continue  # an ordinary comment
            key, value = match.group(1), (match.group(2) or "").strip()
            if key in ("compiler-return-code", "interpreter-return-code"):
                if re.fullmatch(r"[0-9]+", value):
                    if key == "compiler-return-code":
                        test.compiler_rc = int(value)
                    else:
                        test.interpreter_rc = int(value)
                else:
                    warnings.append(
                        "{}: invalid {} value {!r} (expected a non-negative integer), "
                        "using default 0".format(test.full_name, key, value))
            elif key == "required-extensions":
                test.required = split_extensions(value)
            elif key == "incompatible-extensions":
                test.incompatible = split_extensions(value)
            else:
                warnings.append("{}: unknown TEST-INFO directive {!r} ignored".format(
                    test.full_name, key))
    return warnings


def discover_tests(tests_dir):
    """Return [(suite_or_None, [Test, ...]), ...].

    Tests directly in ``tests_dir`` come first (suite None), then one entry per
    subdirectory (nested subdirectories allowed, suite name = path relative to
    ``tests_dir``), sorted by name.
    """
    standalone = []
    suites = {}
    for dirpath, dirnames, filenames in os.walk(tests_dir):
        dirnames.sort()
        rel = os.path.relpath(dirpath, tests_dir)
        suite = None if rel == os.curdir else rel.replace(os.sep, "/")
        for filename in sorted(filenames):
            if not filename.lower().endswith(".vyp"):
                continue
            name = filename[:-4]
            if not name:
                continue
            test = Test(suite, name, os.path.join(dirpath, filename))
            if suite is None:
                standalone.append(test)
            else:
                suites.setdefault(suite, []).append(test)
    return [(None, standalone)] + [(suite, suites[suite]) for suite in sorted(suites)]


# -------------------------------------------------------------------- output

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def display_lines(data):
    """Split bytes into printable lines; control and non-UTF-8 bytes escaped."""
    lines = []
    for chunk in data.split(b"\n"):
        text = chunk.decode("utf-8", "backslashreplace")
        lines.append(_CONTROL_RE.sub(
            lambda match: "\\x{:02x}".format(ord(match.group())), text))
    return lines


def make_diff(expected, actual):
    diff = difflib.unified_diff(
        display_lines(expected), display_lines(actual),
        fromfile="expected stdout", tofile="actual stdout", lineterm="")
    lines = list(diff)
    if len(lines) > DIFF_MAX_LINES:
        lines = lines[:DIFF_MAX_LINES] + ["... (diff truncated)"]
    return "\n".join(lines)


def print_config(ctx):
    print(P.bold("compiler:") + " " + ctx.compiler)
    print(P.bold("interpreter:") + " java -jar " + ctx.jar)
    print(P.bold("extensions:") + " " +
          (", ".join(sorted(ctx.extensions)) if ctx.extensions else "(none)"))
    print(P.bold("timeout:") + " " + fmt_seconds(ctx.timeout))


def print_results(groups, results, show_all):
    for suite, tests in groups:
        visible = []
        for test in tests:
            status, reasons = results[test.full_name]
            if show_all or status == FAIL:
                visible.append((test, status, reasons))
        if not visible:
            continue
        header = "standalone tests" if suite is None else "suite: " + suite
        print(P.cyan("== " + header + " =="))
        for test, status, reasons in visible:
            if status == PASS:
                print("  {}  {}".format(P.green(PASS), test.name))
            elif status == SKIP:
                print("  {}  {}  {}".format(
                    P.yellow(SKIP), test.name, P.dim("({})".format(reasons[0]))))
            else:
                print("  {}  {}".format(P.red(FAIL), test.name))
                for reason in reasons:
                    print(indent(reason, 6))
        print()


def print_summary(counts, total):
    print(P.bold("Summary: {}, {}, {} ({} total)".format(
        P.green("{} passed".format(counts[PASS])),
        P.yellow("{} skipped".format(counts[SKIP])),
        P.red("{} failed".format(counts[FAIL])),
        total)))


# ------------------------------------------------------------------ testing


class Context:
    def __init__(self, args):
        self.compiler = args.compiler
        self.jar = args.interpreter or os.path.join(SCRIPT_DIR, DEFAULT_JAR)
        self.extensions = {ext.strip().lower()
                           for ext in args.extensions.split(",") if ext.strip()}
        self.timeout = args.timeout
        self.build_dir = BUILD_DIR


def work_path(test, ctx, suffix):
    rel = test.name if test.suite is None else os.path.join(
        test.suite.replace("/", os.sep), test.name)
    return os.path.join(ctx.build_dir, rel + suffix)


def run_test(test, ctx):
    """Run a single test. Returns (status, list of report lines)."""
    missing = [ext for ext in test.required if ext.lower() not in ctx.extensions]
    if missing:
        return SKIP, ["missing required extension(s): " + ", ".join(missing)]
    conflicting = [ext for ext in test.incompatible if ext.lower() in ctx.extensions]
    if conflicting:
        return SKIP, ["incompatible extension(s) supported by the compiler: " +
                      ", ".join(conflicting)]

    # --- compiler stage -----------------------------------------------------
    vc_path = work_path(test, ctx, ".vc")
    os.makedirs(os.path.dirname(vc_path), exist_ok=True)

    proc, err = run_command([ctx.compiler, test.path, vc_path], ctx.timeout)
    if isinstance(err, subprocess.TimeoutExpired):
        lines = ["compiler timed out after " + fmt_seconds(ctx.timeout)]
        captured = tail_text(err.stdout) or tail_text(err.stderr)
        if captured:
            lines.append("compiler output:\n" + captured)
        return FAIL, lines
    if err is not None:
        return FAIL, ["failed to run the compiler: {}".format(err)]
    if proc.returncode != test.compiler_rc:
        lines = ["expected compiler return code {} ({}), got {} ({})".format(
            test.compiler_rc, rc_meaning(test.compiler_rc, COMPILER_MEANINGS),
            proc.returncode, rc_meaning(proc.returncode, COMPILER_MEANINGS))]
        captured = tail_text(proc.stderr) or tail_text(proc.stdout)
        if captured:
            lines.append("compiler output:\n" + captured)
        return FAIL, lines
    if test.compiler_rc != 0:
        return PASS, []  # expected compiler error matched; there is nothing to interpret

    # --- interpreter stage --------------------------------------------------
    source_base = os.path.splitext(test.path)[0]
    stdin_bytes = read_optional(source_base + ".in")
    expected_stdout = read_optional(source_base + ".out")

    proc, err = run_command(["java", "-jar", ctx.jar, vc_path],
                            ctx.timeout, input_bytes=stdin_bytes)
    if isinstance(err, subprocess.TimeoutExpired):
        lines = ["interpreter timed out after " + fmt_seconds(ctx.timeout)]
        captured = tail_text(err.stdout) or tail_text(err.stderr)
        if captured:
            lines.append("interpreter output:\n" + captured)
        return FAIL, lines
    if err is not None:
        return FAIL, ["failed to run the interpreter: {}".format(err)]

    with open(work_path(test, ctx, ".stdout"), "wb") as fh:
        fh.write(proc.stdout)

    lines = []
    if proc.returncode != test.interpreter_rc:
        lines.append("expected interpreter return code {} ({}), got {} ({})".format(
            test.interpreter_rc, rc_meaning(test.interpreter_rc, INTERPRETER_MEANINGS),
            proc.returncode, rc_meaning(proc.returncode, INTERPRETER_MEANINGS)))
        captured = tail_text(proc.stderr)
        if captured:
            lines.append("interpreter output:\n" + captured)
    if proc.stdout != expected_stdout:
        lines.append("stdout differs (expected {} bytes, got {} bytes)\n{}".format(
            len(expected_stdout), len(proc.stdout),
            make_diff(expected_stdout, proc.stdout)))
    if lines:
        return FAIL, lines
    return PASS, []


# -------------------------------------------------------------------- setup


def check_compiler(path):
    if not os.path.isfile(path):
        die("compiler not found: " + path)
    if not os.access(path, os.X_OK):
        die("compiler is not executable: " + path)


def check_interpreter(ctx, timeout):
    if not os.path.isfile(ctx.jar):
        die("interpreter jar not found: " + ctx.jar)
    cmd = ["java", "-jar", ctx.jar, "--help"]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=max(timeout, 10.0))
    except FileNotFoundError:
        die("'java' executable not found in PATH")
    except subprocess.TimeoutExpired:
        die("interpreter check timed out: {}".format(" ".join(cmd)))
    if proc.returncode != 0:
        die("interpreter check failed: '{}' returned {} ({}).\n{}".format(
            " ".join(cmd), proc.returncode,
            rc_meaning(proc.returncode, INTERPRETER_MEANINGS),
            indent(tail_text(proc.stderr) or tail_text(proc.stdout) or "(no output)", 4)))
    print("interpreter check: {} ({})".format(P.green("OK"), " ".join(cmd)))


# --------------------------------------------------------------------- main


def use_color(args):
    if args.no_color or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Test runner for the VYP compiler and the vypint interpreter.",
        epilog="example: python3 tester.py --compiler ./vypint --extensions STRING,FLOAT --timeout 5",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--compiler", default=DEFAULT_COMPILER, metavar="PATH",
                        help="compiler executable, called as 'COMPILER input.vyp output.vc' "
                             "(default: %(default)s)")
    parser.add_argument("--interpreter", default=None, metavar="JAR",
                        help="path to the interpreter .jar, run as 'java -jar JAR program.vc' "
                             "(default: {} next to tester.py)".format(DEFAULT_JAR))
    parser.add_argument("--extensions", default="", metavar="EXT[,EXT...]",
                        help="comma-separated list of extensions implemented by the compiler, "
                             "matched case-insensitively (default: none)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SECONDS",
                        help="timeout for the compiler and the interpreter (default: %(default)s)")
    parser.add_argument("--show-all", action="store_true",
                        help="also print passed and skipped tests (by default only failed "
                             "tests are printed)")
    parser.add_argument("--no-color", action="store_true",
                        help="disable colored output")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be a positive number")
    return args


def main(argv=None):
    global P
    args = parse_args(argv)
    P = Palette(enabled=use_color(args))
    # keep stdout/stderr ordering intact even when stdout is a pipe
    sys.stdout.reconfigure(line_buffering=True)
    ctx = Context(args)

    check_compiler(ctx.compiler)
    print_config(ctx)
    check_interpreter(ctx, ctx.timeout)

    if not os.path.isdir(TESTS_DIR):
        die("tests directory not found: " + TESTS_DIR)
    groups = discover_tests(TESTS_DIR)
    total = sum(len(tests) for _, tests in groups)
    if total == 0:
        die("no .vyp tests found in " + TESTS_DIR)

    for _, tests in groups:
        for test in tests:
            try:
                for message in parse_test_header(test):
                    warn(message)
            except OSError as exc:
                die("cannot read {}: {}".format(test.path, exc))

    shutil.rmtree(ctx.build_dir, ignore_errors=True)
    os.makedirs(ctx.build_dir, exist_ok=True)

    print()
    results = {}
    counts = {PASS: 0, FAIL: 0, SKIP: 0}
    for _, tests in groups:
        for test in tests:
            status, reasons = run_test(test, ctx)
            results[test.full_name] = (status, reasons)
            counts[status] += 1

    print_results(groups, results, args.show_all)
    print_summary(counts, total)
    return 1 if counts[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
