"""Parse user-typed Python literals without handing them to `eval`.

The settings UI asks users to type Python literals: a dict of yt-dlp options, a
list of chat ids, a dict of FFmpeg commands. That text used to go straight into
`eval()`, which executes arbitrary code — a sudo user pasting a crafted string
could read files or spawn processes.

`ast.literal_eval` is the usual replacement, but on its own it is *stricter*
than what this bot documents. `help_messages.py` advertises

    {"fragment_retries": float("inf"), ...}

as a valid `YT_DLP_OPTIONS` value (it is yt-dlp's own idiom for "retry
forever"), and `literal_eval` rejects it: `float("inf")` is a call, not a
literal. Dropping support would silently break a documented input.

So this module is `literal_eval` plus exactly one call form: `float(<number or
string>)`. Allowed calls are folded into constants first, then `literal_eval`
does all remaining validation — the whitelist stays one small dict instead of a
hand-written evaluator that has to be audited for holes.
"""

from __future__ import annotations

import ast

# One entry, deliberately. Anything added here becomes callable by any sudo user
# who can reach the settings menu, so it must be a pure value constructor.
_ALLOWED_CALLS = {"float": float}


class _FoldAllowedCalls(ast.NodeTransformer):
    """Rewrite whitelisted calls to constants; reject every other call."""

    def visit_Call(self, node):
        self.generic_visit(node)
        # Only a bare name has an ``id``; ``os.system(...)`` puts an
        # ``ast.Attribute`` there, and "" is a key the whitelist does not have,
        # so it lands in the reject below with the call spelled out by
        # ``unparse``.
        called = getattr(node.func, "id", "")
        func = _ALLOWED_CALLS.get(called)
        if func is None:
            name = called or ast.unparse(node.func)
            allowed = ", ".join(f"{n}()" for n in _ALLOWED_CALLS)
            raise ValueError(f"{name}() is not allowed here. Allowed: {allowed}")
        if node.keywords or len(node.args) != 1:
            raise ValueError(f"{node.func.id}() takes exactly one argument")
        arg = node.args[0]
        if not isinstance(arg, ast.Constant) or isinstance(arg.value, bool):
            raise ValueError(
                f"{node.func.id}() argument must be a plain number or string"
            )
        if not isinstance(arg.value, str | int | float):
            raise ValueError(
                f"{node.func.id}() argument must be a plain number or string"
            )
        try:
            folded = func(arg.value)
        except ValueError as e:
            raise ValueError(f"{node.func.id}({arg.value!r}): {e}") from e
        return ast.copy_location(ast.Constant(folded), node)


def _explain(tree, error):
    """A message worth showing a user instead of `literal_eval`'s AST dump.

    `literal_eval` reports "malformed node or string on line 1: Name(id='bad',
    ctx=Load())". The bare `eval` this replaced said "name 'bad' is not
    defined", which was more useful — an unquoted string is by far the most
    common mistake here, so name it the same way.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            return f"name '{node.id}' is not defined — quote it to mean a string"
    return f"Not a valid Python literal: {error}"


def parse_literal(text):
    """Return the Python value `text` describes.

    Accepts what `ast.literal_eval` accepts, plus `float(...)`. Raises
    `ValueError` on anything else — including syntax errors, so callers get one
    exception type to report back to the user.
    """
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as e:
        raise ValueError(f"Invalid syntax: {e.msg}") from e
    tree = _FoldAllowedCalls().visit(tree)
    try:
        return ast.literal_eval(tree)
    except (ValueError, TypeError, MemoryError, RecursionError, SyntaxError) as e:
        raise ValueError(_explain(tree, e)) from e


def parse_dict(text):
    """`parse_literal`, but the result must be a dict.

    The three settings that take a dict all guard on `{...}` before parsing, and
    a value like `{1, 2}` passes that guard while being a set.
    """
    value = parse_literal(text)
    if not isinstance(value, dict):
        raise ValueError(f"It must be dict, got {type(value).__name__}!")
    return value
