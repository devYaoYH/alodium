"""
envfile — the node's .env, parsed instead of sourced.

The bash jobs opened with `set -a; source .env; set +a`: run the file as shell
and export everything it assigned. That needs a POSIX shell, and it also means
a stray line in .env is EXECUTED on every heartbeat. Compose reads the same
file without a shell, so .env is already held to the assignment-only subset
this module parses; see test_envfile.py, whose expected values were produced
by actually sourcing each fixture line in bash (testdata/bash_baseline.json).

Supported, with bash's meaning:

  KEY=value            KEY="double $VAR ${VAR}"      export KEY=value
  KEY='single'         KEY=a"b"'c' (concatenation)   KEY=  (empty)
  KEY=value # comment  KEY=~/path (leading tilde)    blank lines, # lines

Anything else — command substitution, `${VAR:-default}`, a bare command, a
line continuation, an unquoted space, a reference to an unset variable (the
jobs ran under `set -u`) — raises EnvFileError naming the line. Refusing is
the safe direction: the alternative is a value that differs from what bash
would have exported, and on this node .env holds the domain every API call
is pinned to.
"""

import re

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class EnvFileError(ValueError):
    pass


def _expand_var(text, i, env, lineno):
    """`$NAME` or `${NAME}` at text[i] (which is '$'). Returns (value, next)."""
    if text.startswith("${", i):
        end = text.find("}", i + 2)
        name = text[i + 2:end] if end != -1 else ""
        if end == -1 or not NAME.fullmatch(name):
            raise EnvFileError(f"line {lineno}: unsupported expansion "
                               f"{text[i:end + 1 if end != -1 else None]!r}")
        nxt = end + 1
    else:
        m = NAME.match(text, i + 1)
        if not m:
            if text.startswith("$(", i):
                raise EnvFileError(f"line {lineno}: command substitution is not allowed")
            # A lone `$` (e.g. before a space or at the end) is literal in bash.
            return "$", i + 1
        name, nxt = m.group(0), m.end()
    if name not in env:
        raise EnvFileError(f"line {lineno}: ${name} is not set (the jobs ran "
                           f"under `set -u`, where this aborted)")
    return env[name], nxt


def _parse_value(text, env, lineno, home):
    """One shell word starting at text[0]. Returns the value; raises unless
    the rest of the line is whitespace or a comment."""
    out = []
    i, n = 0, len(text)
    if text.startswith("~") and (n == 1 or text[1] in "/: \t#"):
        out.append(home)
        i = 1
    elif text.startswith("~"):
        raise EnvFileError(f"line {lineno}: ~user expansion is not supported")
    while i < n:
        c = text[i]
        if c in " \t":
            rest = text[i:].strip()
            if rest and not rest.startswith("#"):
                raise EnvFileError(f"line {lineno}: unquoted space — bash would "
                                   f"run {rest.split()[0]!r} as a command")
            break
        if c == "'":
            end = text.find("'", i + 1)
            if end == -1:
                raise EnvFileError(f"line {lineno}: unterminated single quote")
            out.append(text[i + 1:end])
            i = end + 1
        elif c == '"':
            i += 1
            while True:
                if i >= n:
                    raise EnvFileError(f"line {lineno}: unterminated double quote")
                c = text[i]
                if c == '"':
                    i += 1
                    break
                if c == "\\" and i + 1 < n and text[i + 1] in '"\\$`':
                    out.append(text[i + 1])
                    i += 2
                elif c == "$":
                    value, i = _expand_var(text, i, env, lineno)
                    out.append(value)
                elif c == "`":
                    raise EnvFileError(f"line {lineno}: command substitution is not allowed")
                else:
                    out.append(c)
                    i += 1
        elif c == "\\":
            if i + 1 >= n:
                raise EnvFileError(f"line {lineno}: line continuation is not supported")
            out.append(text[i + 1])
            i += 2
        elif c == "$":
            value, i = _expand_var(text, i, env, lineno)
            out.append(value)
        elif c in "`;&|<>()":
            raise EnvFileError(f"line {lineno}: {c!r} is shell syntax, not an assignment")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def parse(text: str, base_env=None, home="") -> dict:
    """The assignments in `text`, in order, as a dict.

    `base_env` is what `$VAR` resolves against when the name was not assigned
    earlier in the file — the process environment, as it was for `source`.
    """
    env = dict(base_env or {})
    assigned = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export"):].lstrip()
        m = NAME.match(line)
        if not m or not line[m.end():].startswith("="):
            raise EnvFileError(f"line {lineno}: not a KEY=value assignment")
        key = m.group(0)
        value = _parse_value(line[m.end() + 1:], env, lineno, home)
        env[key] = value
        assigned[key] = value
    return assigned


def load(path, base_env) -> dict:
    """base_env overlaid with the file's assignments — what `set -a; source`
    left in the environment. .env wins over the inherited value, as it did."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    merged = dict(base_env)
    merged.update(parse(text, base_env, home=base_env.get("HOME", "")))
    return merged
