# VYP test runner

`tester.py` is a dependency-free (Python standard library only) test runner for
the VYP compiler and the `vypint` interpreter. Run it from the project root —
it expects the `tests/` directory here and the interpreter jar next to the
tester.

If you have tests to contribute, i will gladly accept PRs. I have included a 
markdown version of the assignment in [assignment.md](./assignment.md), which
might be useful if you want to generate some tests using a clanker.

Also, note that this tester was completely ✨vibecoded✨.

## Usage

```
python3 tester.py [--compiler PATH] [--extensions EXT1,EXT2,...] [--timeout SECONDS]
                  [--filter PATH[,PATH...]] [--show-all] [--interpreter JAR] [--no-color]
```

| Option | Meaning | Default |
|---|---|---|
| `--compiler PATH` | Compiler executable, invoked as `COMPILER input.vyp output.vc` | `./vypcomp` |
| `--extensions A,B` | Extensions implemented by the compiler (case-insensitive), used for skipping tests | *(empty)* |
| `--timeout SECONDS` | Timeout for the compiler, the interpreter and the startup check | `10` |
| `--filter A,B` | Run only tests matching the given path(s); see *Filtering* below | *(empty)* |
| `--show-all` | Also print passed and skipped tests (default: only failed tests are printed) | off |
| `--interpreter JAR` | Interpreter jar, run as `java -jar JAR program.vc` | `vypint-1.0.jar` next to `tester.py` |
| `--no-color` | Disable colored output (colors are also off without a terminal or when `NO_COLOR` is set) | off |

Before running any test, the tester verifies the interpreter by running
`java -jar vypint-1.0.jar --help`; it must exit with code 0, otherwise the
tester aborts.

Compiled programs (`.vc`) and captured interpreter stdout (`.stdout`) are
stored in `build/` (never inside `tests/`); `build/` is wiped at the start of
every run and kept afterwards for debugging.

Exit status: `0` — all tests passed · `1` — at least one test failed ·
`2` — setup error (compiler/jar/`java` not found, interpreter check failed, or
no tests found).

## Output

Tests directly in `tests/` are listed under *standalone tests*; every
subdirectory of `tests/` (nested subdirectories allowed) is listed as a
separate *test suite*, named by its path relative to `tests/`. Every run ends
with a summary of the total passed / skipped / failed counts. Failed tests are
always printed with the reason (e.g. `expected compiler return code 13
(semantic error (type incompatibility)), got 0 (success)`, or `stdout differs`
followed by a unified diff).

## Filtering

`--filter` accepts a comma-separated list of patterns; a test runs when it
matches **any** of them. Each pattern gets an implicit trailing `*` (glob
style, `*` also crosses `/`):

- `--filter tests/semantic/function` runs everything under
  `tests/semantic/function*` — the whole `tests/semantic/function/` directory
  *and* tests like `tests/semantic/function_args.vyp`.
- A single test can be selected exactly: `--filter tests/ok.vyp`.
- Patterns may also be given relative to `tests/`, e.g.
  `--filter semantic/function`.

A slash-less pattern that does not refer to an existing directory or test file
(neither in the working directory nor under `tests/`) is instead treated as a
name search: `--filter function_return` runs every suite and every test whose
name contains `function_return`.

A pattern that matches no test produces a warning; if no pattern matches
anything, the tester exits with status 2. When a filter excludes tests, the
summary is followed by a `filtered out: N test(s)` line.

## Writing tests

- A test is any `<name>.vyp` file inside `tests/` or a subdirectory of it; the
  test name is the file name without the extension.
- Optional sibling files: `<name>.in` — fed to the interpreted program's
  stdin — and `<name>.out` — the expected stdout, compared **byte-for-byte**.
  Missing files are treated as empty.
- Test metadata is given by `// TEST-INFO` line comments in the leading comment
  block of the `.vyp` file. The lines may appear in any order; blank lines and
  ordinary `//` comments are allowed among them, and the first non-comment line
  ends the header:

  ```c
  // TEST-INFO compiler-return-code 13
  // TEST-INFO interpreter-return-code 0
  // TEST-INFO required-extensions STRING,FLOAT
  // TEST-INFO incompatible-extensions ASM
  ```

  All directives are optional; the defaults are `0`, `0`, an empty list and an
  empty list respectively. Unknown or malformed directives produce a warning
  and are ignored.

## How a test is judged

1. **Skip check** — the test is skipped when the compiler does not support all
   of the test's `required-extensions`, or supports any of its
   `incompatible-extensions`.
2. **Compiler** — run as `COMPILER <name>.vyp build/.../<name>.vc`. If the
   return code differs from `compiler-return-code`, the test fails (the
   compiler's stderr is shown). If the expected compiler return code is
   non-zero and matches, the test passes and the interpreter is not run.
3. **Interpreter** (only when the compiler was expected to succeed) — run as
   `java -jar vypint-1.0.jar <name>.vc` with `<name>.in` on stdin. Exceeding
   `--timeout` is a failure. The return code must equal
   `interpreter-return-code` and the stdout must match `<name>.out`
   byte-for-byte; on mismatch a unified diff is printed.

## Return-code meanings

These meanings are shown next to return codes in failure messages (`0` means
success on both sides; any other unlisted code is reported as *unknown code*):

| Compiler | Meaning | Interpreter | Meaning |
|---|---|---|---|
| 11 | lexical analysis error | 20 | invalid command-line arguments |
| 12 | syntactic analysis error | 21 | lexical/syntax error of VYPcode |
| 13 | semantic error (type incompatibility) | 22 | semantic error of VYPcode |
| 14 | semantic error (missing/multiple definition, ...) | 25 | runtime error (invalid/non-existing label) |
| 15 | target code generation error | 27 | runtime error (division by zero) |
| 19 | internal compiler error | 28 | runtime error (invalid memory access) |
| | | 30 | internal interpreter error |
