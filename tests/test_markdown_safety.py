"""
Static regression guard for the MarkdownV2 escaping bug reported in
removealias/listalias ("Can't parse entities: character '.' is reserved and
must be escaped with the preceding '\\'").

Rather than mocking every single reply_text() call and checking its exact
text (which only catches messages someone thought to write a test for), this
statically walks handlers.py's AST and finds every call that passes
parse_mode="MarkdownV2", then checks the literal (non-interpolated) parts of
its text argument for unescaped '.' or '!' characters.

Why only '.' and '!' and not the other MarkdownV2 reserved characters
(_ * [ ] ( ) ~ ` > # + - = | { })? Because those others are legitimately used
throughout this codebase as intentional formatting syntax (*bold*, `code`,
[text](url), etc.), so a bare '*' or '`' isn't automatically a bug - but a
bare '.' or '!' never has any special Markdown meaning here, so every
occurrence of one is unescaped-by-mistake by definition. This is exactly
the bug class that shipped in removealias/listalias.

Backtick `code spans` are tracked and excluded: MarkdownV2 doesn't require
(and doesn't want) escaping inside a code span - a backslash there shows up
as a literal backslash in the rendered message rather than being consumed,
so a '.'/'!' inside backticks (e.g. a "..." in a usage example) is correct
as-is and must NOT be flagged.
"""
import ast
import os

RESERVED_MUST_ESCAPE = {".", "!"}


def _extract_literal_parts(node):
    """Recursively pulls out Constant string parts from a (possibly f-string) expression node."""
    parts = []
    if isinstance(node, ast.JoinedStr):
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            # ast.FormattedValue (the {expr} parts of an f-string) is skipped -
            # we can't know its runtime value statically, and it's the
            # caller's responsibility to escape_markdown() any dynamic value
            # that goes into one of these messages.
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        parts.append(node.value)
    elif isinstance(node, ast.BinOp):  # string concatenation via +
        parts.extend(_extract_literal_parts(node.left))
        parts.extend(_extract_literal_parts(node.right))
    return parts


class MarkdownV2Finder(ast.NodeVisitor):
    def __init__(self):
        self.violations = []  # (lineno, offending_text)

    def visit_Call(self, node):
        uses_markdown_v2 = any(
            kw.arg == "parse_mode"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value == "MarkdownV2"
            for kw in node.keywords
        )
        if uses_markdown_v2:
            text_node = node.args[0] if node.args else next(
                (kw.value for kw in node.keywords if kw.arg == "text"), None
            )
            if text_node is not None:
                combined = "".join(_extract_literal_parts(text_node))
                unescaped = []
                in_code = False
                for i, c in enumerate(combined):
                    prev_escaped = i > 0 and combined[i - 1] == "\\"
                    if c == "`" and not prev_escaped:
                        in_code = not in_code
                        continue
                    if c in RESERVED_MUST_ESCAPE and not in_code and not prev_escaped:
                        unescaped.append(c)
                if unescaped:
                    self.violations.append((node.lineno, combined))
        self.generic_visit(node)


def test_no_unescaped_periods_or_exclamations_in_markdownv2_messages():
    handlers_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "handlers.py")
    source = open(handlers_path, encoding="utf-8").read()
    tree = ast.parse(source)

    finder = MarkdownV2Finder()
    finder.visit(tree)

    if finder.violations:
        details = "\n".join(f"  line {lineno}: {text!r}" for lineno, text in finder.violations)
        raise AssertionError(
            f"Found {len(finder.violations)} MarkdownV2 message(s) with an unescaped "
            f"'.' or '!' - these will crash with 'Can't parse entities' in production:\n{details}"
        )
