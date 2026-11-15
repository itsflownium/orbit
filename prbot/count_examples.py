"""Check literal Python range-count examples by inspecting source syntax."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass


_EXAMPLE = re.compile(r"`([^`\r\n]{1,240})`\s+(?:should return|returns)\s+`(-?\d{1,6})`", re.IGNORECASE)


@dataclass(frozen=True)
class RangeCountExample:
    call: str
    actual: int
    expected: int
    excluded: str
    replacement: str


def _parse(source: str, *, mode: str = "exec") -> ast.AST | None:
    try:
        return ast.parse(source, mode=mode)
    except (SyntaxError, ValueError, RecursionError):
        return None


def _function_at(lines: dict[int, str], line: int) -> tuple[ast.FunctionDef, ast.Return] | None:
    # A contiguous prefix keeps a partial diff from being parsed as freestanding code.
    if not 1 <= line <= min(len(lines), 120):
        return None
    prefix = [lines.get(n) for n in range(1, line + 1)]
    if any(text is None for text in prefix):
        return None
    source = "\n".join(text for text in prefix if text is not None)
    module = _parse(source) if len(source) <= 8000 else None
    if not isinstance(module, ast.Module) or not module.body:
        return None
    function = module.body[-1]
    if not isinstance(function, ast.FunctionDef) or function.decorator_list:
        return None
    statements = function.body[1:] if ast.get_docstring(function) is not None else function.body
    if len(statements) != 1 or not isinstance(statements[0], ast.Return):
        return None
    statement = statements[0]
    if statement.lineno != line or statement.end_lineno != line:
        return None
    return function, statement


def _integer(expr: ast.AST) -> int | None:
    if isinstance(expr, ast.Constant) and type(expr.value) is int:
        return expr.value
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, (ast.UAdd, ast.USub)):
        if isinstance(expr.operand, ast.Constant) and type(expr.operand.value) is int:
            value = expr.operand.value
            return -value if isinstance(expr.op, ast.USub) else value
    return None


def verify_range_count(lines: dict[int, str], line: int, code: str, replacement: str,
                       description: str) -> RangeCountExample | None:
    """Confirm when an inclusive comparison repairs a stated example for a simple count."""
    description = description[:4000]
    if lines.get(line) != code or not _EXAMPLE.search(description):
        return None
    anchor = _function_at(lines, line)
    if anchor is None:
        return None
    function, statement = anchor
    match statement.value:
        case ast.Call(func=ast.Name(id="sum"), args=[ast.GeneratorExp(elt=test, generators=[loop])], keywords=[]):
            pass
        case _:
            return None
    match loop, test:
        case (ast.comprehension(target=ast.Name(id=item), iter=ast.Name(id=items), ifs=[], is_async=0),
              ast.Compare(left=ast.Name(id=lower), ops=[lower_op, upper_op],
                          comparators=[ast.Name(id=middle), ast.Name(id=upper)])) if item == middle:
            pass
        case _:
            return None
    params = [arg.arg for arg in (*function.args.posonlyargs, *function.args.args)]
    if (len(params) != 3 or set(params) != {items, lower, upper} or item in params or "sum" in params
            or function.args.vararg or function.args.kwonlyargs or function.args.kwarg):
        return None
    operators = (ast.Lt, ast.LtE)
    if not isinstance(lower_op, operators) or not isinstance(upper_op, operators):
        return None
    lower_inclusive = isinstance(lower_op, ast.LtE)
    upper_inclusive = isinstance(upper_op, ast.LtE)
    if lower_inclusive and upper_inclusive:
        return None

    proposed = _parse(replacement.strip())
    test.ops = [ast.LtE(), ast.LtE()]
    inclusive_return = ast.Module(body=[statement], type_ignores=[])
    if proposed is None or ast.dump(proposed) != ast.dump(inclusive_return):
        return None

    for match in _EXAMPLE.finditer(description):
        typed_call, stated_count = match.groups()
        parsed = _parse(typed_call.strip(), mode="eval")
        match parsed:
            case ast.Expression(body=ast.Call(func=ast.Name(id=name), args=args, keywords=[])) \
                    if name == function.name and len(args) == len(params):
                pass
            case _:
                continue
        arguments = dict(zip(params, args, strict=True))
        low, high = _integer(arguments[lower]), _integer(arguments[upper])
        sequence = arguments[items]
        if low is None or high is None or not isinstance(sequence, (ast.List, ast.Tuple)):
            continue
        values = [_integer(value) for value in sequence.elts]
        if any(value is None for value in values):
            continue
        integers = [value for value in values if value is not None]
        expected = sum(low <= value <= high for value in integers)
        actual = sum((low <= value if lower_inclusive else low < value)
                     and (value <= high if upper_inclusive else value < high) for value in integers)
        if expected == int(stated_count) and actual != expected:
            excluded = "both bounds"
            if lower_inclusive:
                excluded = "the upper bound"
            elif upper_inclusive:
                excluded = "the lower bound"
            indent = code[:len(code) - len(code.lstrip())]
            corrected = f"{indent}return sum({lower} <= {item} <= {upper} for {item} in {items})"
            return RangeCountExample(typed_call.strip(), actual, expected, excluded, corrected)
    return None
