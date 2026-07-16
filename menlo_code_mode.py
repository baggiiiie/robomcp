"""Restricted executable-plan interpreter for the Menlo MCP server.

This module intentionally interprets a small Python subset instead of using
Python exec. Generated plans can compose guarded robot operations, but cannot
import modules, access files or the network, define functions, or call arbitrary
Python objects.
"""

from __future__ import annotations

import ast
import json
import math
import operator
import textwrap
import time
from collections.abc import Awaitable, Callable
from itertools import islice
from typing import Any


MenloCall = Callable[[str, list[Any], dict[str, Any]], Awaitable[Any]]


class CodeModeValidationError(ValueError):
    """The submitted plan uses syntax outside the restricted language."""


class PlanActionError(RuntimeError):
    """A guarded Menlo operation failed its postcondition."""

    def __init__(self, method: str, message: str, result: Any = None) -> None:
        super().__init__(message)
        self.method = method
        self.result = result


class _ReturnSignal(Exception):
    def __init__(self, value: Any) -> None:
        self.value = value


class _BreakSignal(Exception):
    pass


class _ContinueSignal(Exception):
    pass


class _PlanValidator(ast.NodeVisitor):
    allowed_methods = {
        "get_robot_state",
        "get_scene",
        "go_to",
        "pick",
        "place",
        "stop",
        "turn",
        "walk",
    }
    allowed_functions = {
        "abs",
        "bool",
        "float",
        "int",
        "len",
        "max",
        "min",
        "range",
        "sorted",
        "str",
        "sum",
    }
    allowed_nodes = (
        ast.Add,
        ast.And,
        ast.Assert,
        ast.Assign,
        ast.Attribute,
        ast.BinOp,
        ast.BoolOp,
        ast.Break,
        ast.Call,
        ast.Compare,
        ast.Constant,
        ast.Continue,
        ast.Dict,
        ast.Div,
        ast.Eq,
        ast.Expr,
        ast.FloorDiv,
        ast.For,
        ast.Gt,
        ast.GtE,
        ast.If,
        ast.IfExp,
        ast.In,
        ast.Is,
        ast.IsNot,
        ast.List,
        ast.Load,
        ast.Lt,
        ast.LtE,
        ast.Mod,
        ast.Mult,
        ast.Name,
        ast.Not,
        ast.NotEq,
        ast.NotIn,
        ast.Or,
        ast.Pass,
        ast.Return,
        ast.Slice,
        ast.Store,
        ast.Sub,
        ast.Subscript,
        ast.Tuple,
        ast.UAdd,
        ast.USub,
        ast.UnaryOp,
    )

    def __init__(self, max_nodes: int, max_integer_bits: int) -> None:
        self.max_nodes = max_nodes
        self.max_integer_bits = max_integer_bits
        self.node_count = 0
        self.loop_depth = 0

    def generic_visit(self, node: ast.AST) -> None:
        self.node_count += 1
        if self.node_count > self.max_nodes:
            raise CodeModeValidationError(
                f"Plan exceeds the {self.max_nodes}-node syntax budget"
            )
        if not isinstance(node, self.allowed_nodes):
            raise CodeModeValidationError(f"Unsupported syntax: {type(node).__name__}")
        super().generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("_"):
            raise CodeModeValidationError("Private names are not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            not isinstance(node.value, ast.Name)
            or node.value.id != "menlo"
            or node.attr not in self.allowed_methods
        ):
            raise CodeModeValidationError(
                "Only documented menlo.<method> attributes are allowed"
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            if node.func.id not in self.allowed_functions:
                raise CodeModeValidationError(
                    f"Function {node.func.id!r} is not allowed"
                )
        elif isinstance(node.func, ast.Attribute):
            self.visit_Attribute(node.func)
        else:
            raise CodeModeValidationError("Only direct function calls are allowed")
        if any(keyword.arg is None for keyword in node.keywords):
            raise CodeModeValidationError("Expanded keyword arguments are not allowed")
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Dict(self, node: ast.Dict) -> None:
        if any(key is None for key in node.keys):
            raise CodeModeValidationError("Expanded dictionaries are not allowed")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        value = node.value
        if not isinstance(value, (type(None), bool, int, float, str)):
            raise CodeModeValidationError(
                f"Unsupported literal type: {type(value).__name__}"
            )
        if isinstance(value, int) and not isinstance(value, bool):
            if value.bit_length() > self.max_integer_bits:
                raise CodeModeValidationError(
                    f"Integer literal exceeds the {self.max_integer_bits}-bit budget"
                )
        if isinstance(value, float) and not math.isfinite(value):
            raise CodeModeValidationError("Non-finite numeric literals are not allowed")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._validate_target(target)
        self.visit(node.value)

    def visit_For(self, node: ast.For) -> None:
        self._validate_target(node.target)
        self.visit(node.iter)
        self.loop_depth += 1
        try:
            for statement in node.body:
                self.visit(statement)
        finally:
            self.loop_depth -= 1
        for statement in node.orelse:
            self.visit(statement)

    def visit_Break(self, node: ast.Break) -> None:
        if self.loop_depth == 0:
            raise CodeModeValidationError("break is only allowed inside a loop")
        self.generic_visit(node)

    def visit_Continue(self, node: ast.Continue) -> None:
        if self.loop_depth == 0:
            raise CodeModeValidationError("continue is only allowed inside a loop")
        self.generic_visit(node)

    def _validate_target(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            if target.id.startswith("_") or target.id == "menlo":
                raise CodeModeValidationError(
                    f"Assignment target {target.id!r} is not allowed"
                )
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._validate_target(element)
            return
        raise CodeModeValidationError("Only variable assignment is allowed")


class MenloCodeExecutor:
    """Interpret a bounded Python subset against guarded Menlo operations."""

    def __init__(
        self,
        call_menlo: MenloCall,
        *,
        max_calls: int = 20,
        max_statements: int = 120,
        max_loop_items: int = 20,
        max_nodes: int = 500,
        max_source_chars: int = 10_000,
        max_result_bytes: int = 100_000,
        max_integer_bits: int = 4096,
        max_elapsed_s: float = 900,
    ) -> None:
        self.call_menlo = call_menlo
        self.max_calls = max_calls
        self.max_statements = max_statements
        self.max_loop_items = max_loop_items
        self.max_nodes = max_nodes
        self.max_source_chars = max_source_chars
        self.max_result_bytes = max_result_bytes
        self.max_integer_bits = max_integer_bits
        self.max_elapsed_s = max_elapsed_s
        self.environ: dict[str, Any] = {}
        self.trace: list[dict[str, Any]] = []
        self.call_count = 0
        self.statement_count = 0
        self.deadline = 0.0

    async def execute(self, code: str) -> dict[str, Any]:
        self.environ = {}
        self.trace = []
        self.call_count = 0
        self.statement_count = 0
        self.deadline = time.monotonic() + self.max_elapsed_s

        try:
            statements = self._parse_and_validate(code)
            await self._execute_block(statements)
            result = None
        except _ReturnSignal as signal:
            result = signal.value
        except PlanActionError as exc:
            response = {
                "status": "action_failed",
                "error": str(exc),
                "failed_method": exc.method,
                "trace": self.trace,
                "calls": self.call_count,
            }
            if exc.result is not None:
                response["failed_result"] = exc.result
            return response
        except CodeModeValidationError as exc:
            return {
                "status": "rejected",
                "error": str(exc),
                "trace": self.trace,
                "calls": self.call_count,
            }
        except (AssertionError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "status": "execution_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "trace": self.trace,
                "calls": self.call_count,
            }

        try:
            result = self._json_result(result)
        except RuntimeError as exc:
            return {
                "status": "execution_failed",
                "error": f"RuntimeError: {exc}",
                "trace": self.trace,
                "calls": self.call_count,
            }
        return {
            "status": "done",
            "result": result,
            "trace": self.trace,
            "calls": self.call_count,
        }

    def _parse_and_validate(self, code: str) -> list[ast.stmt]:
        if not isinstance(code, str) or not code.strip():
            raise CodeModeValidationError("code must not be empty")
        if len(code) > self.max_source_chars:
            raise CodeModeValidationError(
                f"Plan exceeds the {self.max_source_chars}-character source budget"
            )
        wrapped = "async def __plan__():\n" + textwrap.indent(code, "    ")
        try:
            tree = ast.parse(wrapped, mode="exec")
        except SyntaxError as exc:
            line = max(1, (exc.lineno or 2) - 1)
            raise CodeModeValidationError(
                f"Invalid plan syntax at line {line}: {exc.msg}"
            ) from exc
        function = tree.body[0]
        if not isinstance(function, ast.AsyncFunctionDef):
            raise CodeModeValidationError("Invalid plan wrapper")
        validator = _PlanValidator(self.max_nodes, self.max_integer_bits)
        for statement in function.body:
            validator.visit(statement)
        return function.body

    async def _execute_block(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            await self._execute_statement(statement)

    async def _execute_statement(self, statement: ast.stmt) -> None:
        self._consume_statement()
        if isinstance(statement, ast.Expr):
            await self._evaluate(statement.value)
            return
        if isinstance(statement, ast.Assign):
            value = await self._evaluate(statement.value)
            for target in statement.targets:
                self._assign(target, value)
            return
        if isinstance(statement, ast.If):
            branch = (
                statement.body
                if await self._evaluate(statement.test)
                else statement.orelse
            )
            await self._execute_block(branch)
            return
        if isinstance(statement, ast.For):
            iterable = await self._evaluate(statement.iter)
            try:
                items = list(islice(iter(iterable), self.max_loop_items + 1))
            except TypeError as exc:
                raise RuntimeError("for loop requires an iterable value") from exc
            if len(items) > self.max_loop_items:
                raise RuntimeError(
                    f"Loop exceeds the {self.max_loop_items}-item iteration budget"
                )
            broke = False
            for item in items:
                self._assign(statement.target, item)
                try:
                    await self._execute_block(statement.body)
                except _ContinueSignal:
                    continue
                except _BreakSignal:
                    broke = True
                    break
            if not broke:
                await self._execute_block(statement.orelse)
            return
        if isinstance(statement, ast.Return):
            value = (
                await self._evaluate(statement.value)
                if statement.value is not None
                else None
            )
            raise _ReturnSignal(value)
        if isinstance(statement, ast.Assert):
            if not await self._evaluate(statement.test):
                message = (
                    await self._evaluate(statement.msg)
                    if statement.msg is not None
                    else "plan assertion failed"
                )
                raise AssertionError(message)
            return
        if isinstance(statement, ast.Break):
            raise _BreakSignal()
        if isinstance(statement, ast.Continue):
            raise _ContinueSignal()
        if isinstance(statement, ast.Pass):
            return
        raise CodeModeValidationError(
            f"Unsupported statement: {type(statement).__name__}"
        )

    async def _evaluate(self, expression: ast.expr) -> Any:
        if isinstance(expression, ast.Constant):
            return self._bound_scalar(expression.value)
        if isinstance(expression, ast.Name):
            if expression.id not in self.environ:
                raise RuntimeError(f"Unknown variable {expression.id!r}")
            return self.environ[expression.id]
        if isinstance(expression, ast.List):
            return [await self._evaluate(item) for item in expression.elts]
        if isinstance(expression, ast.Tuple):
            return tuple([await self._evaluate(item) for item in expression.elts])
        if isinstance(expression, ast.Dict):
            return {
                await self._evaluate(key): await self._evaluate(value)
                for key, value in zip(expression.keys, expression.values, strict=True)
            }
        if isinstance(expression, ast.Subscript):
            value = await self._evaluate(expression.value)
            key = await self._evaluate_slice(expression.slice)
            try:
                return value[key]
            except (IndexError, KeyError, TypeError) as exc:
                raise RuntimeError(f"Invalid subscript access: {exc}") from exc
        if isinstance(expression, ast.Call):
            return await self._evaluate_call(expression)
        if isinstance(expression, ast.BoolOp):
            return await self._evaluate_bool_operation(expression)
        if isinstance(expression, ast.UnaryOp):
            operand = await self._evaluate(expression.operand)
            operations = {
                ast.Not: operator.not_,
                ast.USub: operator.neg,
                ast.UAdd: operator.pos,
            }
            return self._bound_scalar(operations[type(expression.op)](operand))
        if isinstance(expression, ast.BinOp):
            left = await self._evaluate(expression.left)
            right = await self._evaluate(expression.right)
            operations = {
                ast.Add: operator.add,
                ast.Sub: operator.sub,
                ast.Mult: operator.mul,
                ast.Div: operator.truediv,
                ast.FloorDiv: operator.floordiv,
                ast.Mod: operator.mod,
            }
            try:
                result = operations[type(expression.op)](left, right)
            except (ArithmeticError, TypeError) as exc:
                raise RuntimeError(f"Invalid binary operation: {exc}") from exc
            if (
                isinstance(result, (str, list, tuple))
                and len(result) > self.max_result_bytes
            ):
                raise RuntimeError("Binary operation produced an oversized value")
            if isinstance(result, (list, tuple)):
                return result
            return self._bound_scalar(result)
        if isinstance(expression, ast.Compare):
            return await self._evaluate_comparison(expression)
        if isinstance(expression, ast.IfExp):
            branch = (
                expression.body
                if await self._evaluate(expression.test)
                else expression.orelse
            )
            return await self._evaluate(branch)
        raise CodeModeValidationError(
            f"Unsupported expression: {type(expression).__name__}"
        )

    async def _evaluate_call(self, call: ast.Call) -> Any:
        arguments = [await self._evaluate(argument) for argument in call.args]
        keywords = {
            keyword.arg: await self._evaluate(keyword.value)
            for keyword in call.keywords
            if keyword.arg is not None
        }
        if isinstance(call.func, ast.Name):
            return self._call_builtin(call.func.id, arguments, keywords)
        if not isinstance(call.func, ast.Attribute):
            raise CodeModeValidationError("Unsupported call target")
        return await self._call_menlo(call.func.attr, arguments, keywords)

    async def _call_menlo(
        self, method: str, arguments: list[Any], keywords: dict[str, Any]
    ) -> Any:
        if self.call_count >= self.max_calls:
            raise RuntimeError(
                f"Plan exceeds the {self.max_calls}-call Menlo operation budget"
            )
        self.call_count += 1
        self._check_deadline()
        started = time.monotonic()
        trace_entry: dict[str, Any] = {
            "call": self.call_count,
            "method": method,
            "status": "running",
        }
        self.trace.append(trace_entry)
        try:
            result = await self.call_menlo(method, arguments, keywords)
        except PlanActionError as exc:
            trace_entry["status"] = "failed"
            trace_entry["error"] = str(exc)
            raise
        except Exception as exc:
            trace_entry["status"] = "error"
            trace_entry["error"] = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(
                f"menlo.{method} raised {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            trace_entry["duration_ms"] = round((time.monotonic() - started) * 1000)
        trace_entry["status"] = "done"
        self._check_deadline()
        return result

    def _call_builtin(
        self, name: str, arguments: list[Any], keywords: dict[str, Any]
    ) -> Any:
        if keywords:
            raise RuntimeError(f"Builtin {name} does not accept keyword arguments")
        functions: dict[str, Callable[..., Any]] = {
            "abs": abs,
            "bool": bool,
            "float": float,
            "int": int,
            "len": len,
            "max": max,
            "min": min,
            "str": str,
            "sum": sum,
        }
        if name == "range":
            try:
                value = range(*arguments)
            except (TypeError, ValueError, OverflowError) as exc:
                raise RuntimeError(f"Invalid range call: {exc}") from exc
            if len(value) > self.max_loop_items:
                raise RuntimeError(
                    f"range exceeds the {self.max_loop_items}-item budget"
                )
            return value
        if name == "sorted":
            if len(arguments) != 1:
                raise RuntimeError("sorted requires exactly one argument")
            value = self._bounded_iterable(arguments[0], "sorted input")
            try:
                return sorted(value)
            except TypeError as exc:
                raise RuntimeError(f"Invalid sorted call: {exc}") from exc
        if name in {"max", "min"}:
            if len(arguments) == 1:
                arguments = [self._bounded_iterable(arguments[0], f"{name} input")]
            elif len(arguments) > self.max_loop_items:
                raise RuntimeError(
                    f"{name} exceeds the {self.max_loop_items}-argument budget"
                )
        if name == "sum":
            if not 1 <= len(arguments) <= 2:
                raise RuntimeError("sum requires one iterable and an optional start")
            arguments[0] = self._bounded_iterable(arguments[0], "sum input")
        try:
            return self._bound_scalar(functions[name](*arguments))
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise RuntimeError(f"Invalid {name} call: {exc}") from exc

    async def _evaluate_bool_operation(self, expression: ast.BoolOp) -> Any:
        if isinstance(expression.op, ast.And):
            result: Any = True
            for value in expression.values:
                result = await self._evaluate(value)
                if not result:
                    return result
            return result
        result = False
        for value in expression.values:
            result = await self._evaluate(value)
            if result:
                return result
        return result

    async def _evaluate_comparison(self, expression: ast.Compare) -> bool:
        operations = {
            ast.Eq: operator.eq,
            ast.NotEq: operator.ne,
            ast.Lt: operator.lt,
            ast.LtE: operator.le,
            ast.Gt: operator.gt,
            ast.GtE: operator.ge,
            ast.In: operator.contains,
            ast.NotIn: lambda right, left: not operator.contains(right, left),
            ast.Is: operator.is_,
            ast.IsNot: operator.is_not,
        }
        left = await self._evaluate(expression.left)
        for operation, comparator in zip(
            expression.ops, expression.comparators, strict=True
        ):
            right = await self._evaluate(comparator)
            function = operations[type(operation)]
            if isinstance(operation, (ast.In, ast.NotIn)):
                passed = function(right, left)
            else:
                passed = function(left, right)
            if not passed:
                return False
            left = right
        return True

    async def _evaluate_slice(self, expression: ast.expr) -> Any:
        if isinstance(expression, ast.Slice):
            return slice(
                await self._evaluate(expression.lower)
                if expression.lower is not None
                else None,
                await self._evaluate(expression.upper)
                if expression.upper is not None
                else None,
                await self._evaluate(expression.step)
                if expression.step is not None
                else None,
            )
        return await self._evaluate(expression)

    def _assign(self, target: ast.expr, value: Any) -> None:
        if isinstance(target, ast.Name):
            self.environ[target.id] = value
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            try:
                values = list(islice(iter(value), len(target.elts) + 1))
            except TypeError as exc:
                raise RuntimeError("Cannot unpack a non-iterable value") from exc
            if len(values) != len(target.elts):
                raise RuntimeError("Assignment unpacking length mismatch")
            for element, item in zip(target.elts, values, strict=True):
                self._assign(element, item)
            return
        raise CodeModeValidationError("Unsupported assignment target")

    def _consume_statement(self) -> None:
        self.statement_count += 1
        if self.statement_count > self.max_statements:
            raise RuntimeError(
                f"Plan exceeds the {self.max_statements}-statement execution budget"
            )
        self._check_deadline()

    def _check_deadline(self) -> None:
        if time.monotonic() > self.deadline:
            raise RuntimeError(
                f"Plan exceeded the {self.max_elapsed_s:g}-second elapsed-time budget"
            )

    def _json_result(self, value: Any) -> Any:
        try:
            encoded = json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Plan returned a non-JSON value: {exc}") from exc
        if len(encoded.encode()) > self.max_result_bytes:
            raise RuntimeError(
                f"Plan result exceeds the {self.max_result_bytes}-byte output budget"
            )
        return value

    def _bound_scalar(self, value: Any) -> Any:
        if not isinstance(value, (type(None), bool, int, float, str)):
            raise RuntimeError(f"Unsupported scalar type: {type(value).__name__}")
        if isinstance(value, int) and not isinstance(value, bool):
            if value.bit_length() > self.max_integer_bits:
                raise RuntimeError(
                    f"Integer value exceeds the {self.max_integer_bits}-bit budget"
                )
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise RuntimeError("Non-finite numeric values are not allowed")
        elif isinstance(value, str):
            if len(value.encode()) > self.max_result_bytes:
                raise RuntimeError("String value exceeds the output budget")
        return value

    def _bounded_iterable(self, value: Any, label: str) -> list[Any]:
        try:
            items = list(islice(iter(value), self.max_loop_items + 1))
        except TypeError as exc:
            raise RuntimeError(f"{label} must be iterable") from exc
        if len(items) > self.max_loop_items:
            raise RuntimeError(f"{label} exceeds the {self.max_loop_items}-item budget")
        return items
