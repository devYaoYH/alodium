"""
strict_yaml — the loader that refuses a duplicated mapping key.

Strict: a duplicated mapping key FAILS here, as it does for Docker Compose.
PyYAML's safe_load silently keeps the last value — that is how the #93 merge
shipped a docker-compose.yml defining every egress network twice (and
including apps/egress-broker twice): the gate said OK while every
`docker compose` command on main failed. Compose's own tags (!reset /
!override in docker-compose.staging.yml) are accepted.

This module has exactly two jobs — reject a duplicated key, accept the legal
merge patterns — and both have been got wrong in production. `self_check()`
proves them on every invocation; see its docstring.
"""

import io

import yaml

MERGE_TAG = "tag:yaml.org,2002:merge"


class StrictLoader(yaml.SafeLoader):
    pass


def strict_mapping(loader, node, deep=True):
    seen = {}
    merge_line = None
    for key_node, _ in node.value:
        # `<<` is a merge directive, not a key. Constructing it as one is what
        # made this check reject every compose file using an anchor merge
        # (apps/redash/compose.yaml, a924d45): SafeLoader has no constructor for
        # the merge tag, so the gate went red for a legal file.
        #
        # Resolving it by calling flatten_mapping() BEFORE this scan is worse,
        # and was measured rather than assumed: flatten_mapping PREPENDS the
        # merged pairs to node.value, so `<<: *anchor` followed by an explicit
        # key that overrides one of the anchor's keys — the normal, legal
        # pattern — comes out as the same key twice and gets flagged as a
        # duplicate. Skip the merge nodes instead and let
        # SafeConstructor.construct_mapping do the flattening below, where the
        # override wins as YAML says it should.
        #
        # A REPEATED `<<` in one mapping is still a duplicate: compose refuses
        # it ("mapping key \"<<\" already defined"), so this must too.
        if key_node.tag == MERGE_TAG:
            if merge_line is not None:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    "duplicate merge key '<<' (first defined on line %d)" % merge_line,
                    key_node.start_mark)
            merge_line = key_node.start_mark.line + 1
            continue
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                "duplicate key %r (first defined on line %d)" % (key, seen[key]),
                key_node.start_mark)
        seen[key] = key_node.start_mark.line + 1
    return yaml.SafeLoader.construct_mapping(loader, node, deep=True)


def compose_tag(loader, suffix, node):  # !reset / !override: parse the value underneath
    if isinstance(node, yaml.MappingNode):
        return strict_mapping(loader, node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, strict_mapping)
StrictLoader.add_multi_constructor("!", compose_tag)


# `expected` is None for documents that MUST be rejected; otherwise it is the
# value of the `y` mapping, so the merge cases assert the resulting keys and
# not merely that parsing succeeded.
SELF_TESTS = [
    ("a duplicated key is rejected",
     "a: 1\na: 2\n", None),
    ("a duplicated key nested in a service is rejected",
     "services:\n  s:\n    image: x\n    image: y\n", None),
    ("a duplicate inside an anchor is rejected",
     "x: &x\n  a: 1\n  a: 2\ny:\n  <<: *x\n", None),
    ("a repeated merge key is rejected, as compose rejects it",
     "x: &x {a: 1}\nz: &z {b: 2}\ny:\n  <<: *x\n  <<: *z\n", None),
    ("a merge is accepted",
     "x: &x {a: 1}\ny:\n  <<: *x\n  b: 2\n", {"a": 1, "b": 2}),
    ("a key overridden after a merge is accepted, and the override wins",
     "x: &x {a: 1}\ny:\n  <<: *x\n  a: 2\n", {"a": 2}),
    ("a merge of a list of anchors is accepted, first anchor winning",
     "x: &x {a: 1}\nz: &z {a: 9, b: 2}\ny:\n  <<: [*x, *z]\n", {"a": 1, "b": 2}),
]


def self_check(self_tests=None) -> list[str]:
    """Prove the loader before trusting it on the real files.

    Returns the failure messages (empty list = healthy). Both of this loader's
    historical bugs were invisible in the output when the checked files
    happened not to exercise them — a duplicate scan blind to `<<`, and the
    obvious repair (flatten first) turning a legal override into a false
    duplicate. So the gate proves itself on every run instead of trusting a
    comment, and `checks.check_yaml` refuses to look at a real file until this
    passes.
    """
    failures = []
    for name, doc, expected in (SELF_TESTS if self_tests is None else self_tests):
        try:
            got = yaml.load(doc, Loader=StrictLoader)
        except yaml.YAMLError:
            if expected is not None:
                failures.append("FAIL: yaml self-check — %s: rejected, should parse" % name)
            continue
        if expected is None:
            failures.append("FAIL: yaml self-check — %s: parsed, should be rejected" % name)
        elif got.get("y") != expected:
            failures.append("FAIL: yaml self-check — %s: y=%r, want %r"
                            % (name, got.get("y"), expected))
    return failures


def load_all(text: str, name: str = None) -> list:
    """Every document in `text`, parsed strictly. Raises yaml.YAMLError.

    `name` is the filename PyYAML puts in the error mark. Handing it the text
    alone would report the error against "<unicode string>" and paste the
    offending line back with a caret — a different message from the one the
    bash predecessor printed, which named the file. The file name is the
    useful half (the section prints one line per failure), so the text is
    wrapped in a named stream to keep it.
    """
    stream = io.StringIO(text)
    if name is not None:
        stream.name = name
    return list(yaml.load_all(stream, Loader=StrictLoader))
