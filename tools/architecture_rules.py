"""Dependency-free static checks for Guardian architecture boundaries."""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path


PRODUCTION_ROOTS = {"apps", "packages", "services"}
SOURCE_SUFFIXES = {".go", ".java", ".js", ".jsx", ".kt", ".py", ".rs", ".ts", ".tsx"}
POLICY_SUFFIXES = {".json", ".rego", ".yaml", ".yml"}
RBAC_SUFFIXES = {".tpl", ".yaml", ".yml"}

KUBERNETES_WRITE_CLIENTS = {
    "AppsV1Api",
    "AutoscalingV1Api",
    "AutoscalingV2Api",
    "BatchV1Api",
    "CoreV1Api",
    "CustomObjectsApi",
    "DynamicClient",
}
MODEL_CLIENT_MODULES = {
    "anthropic",
    "google.generativeai",
    "langchain",
    "litellm",
    "mistralai",
    "openai",
}
MCP_CLIENT_MODULES = {"mcp", "signoz_mcp", "signoz-mcp"}
MUTATION_PROVIDER_SYMBOLS = {
    "GitOpsRollbackProvider",
    "KubernetesMutationProvider",
    "RollbackProvider",
    "ScaleProvider",
}
WRITE_RBAC_VERBS = {
    "*",
    "bind",
    "create",
    "delete",
    "deletecollection",
    "escalate",
    "impersonate",
    "patch",
    "update",
}
DEMO_SPECIFIC_TERMS = {
    "astronomy-shop",
    "aws-retail",
    "checkoutservice",
    "online-boutique",
    "opentelemetry-demo",
    "rollouts-demo",
    "sample-go-rabbitmq",
}

# Exceptions are exact paths with reviewable rationale. Do not add wildcard or
# substring-based exemptions.
ARCHITECTURE_EXCEPTIONS = {
    Path(
        "packages/provider-sdk/interfaces.py"
    ): "Shared provider protocol definitions only",
    Path(
        "packages/provider_sdk/interfaces.py"
    ): "Shared provider protocol definitions only",
}

TS_IMPORT = re.compile(
    r"^\s*(?:import\s+(?:type\s+)?(?:[^;]*?\s+from\s+)?|export\s+[^;]*?\s+from\s+|"
    r"(?:const|let|var)\s+[^=]+?=\s*require\s*\()['\"]([^'\"]+)['\"]"
)
GO_SINGLE_IMPORT = re.compile(r'^\s*import\s+(?:[._A-Za-z][\w.]*\s+)?["`]([^"`]+)["`]')
GO_BLOCK_ITEM = re.compile(r'^\s*(?:[._A-Za-z][\w.]*\s+)?["`]([^"`]+)["`]')
GENERIC_IMPORT = re.compile(
    r"^\s*(?:use\s+([^;]+)|import\s+(?:static\s+)?([\w.]+)|require\s*\(\s*['\"]([^'\"]+))"
)
DECLARED_SYMBOL = re.compile(
    r"\b(?:class|interface|struct|type)\s+("
    + "|".join(sorted(MUTATION_PROVIDER_SYMBOLS))
    + r")\b"
)


@dataclass(frozen=True, order=True)
class Violation:
    rule_id: str
    path: Path
    line: int
    detail: str
    remediation: str

    def __str__(self) -> str:
        return (
            f"{self.path.as_posix()}:{self.line}: [{self.rule_id}] {self.detail} "
            f"Remediation: {self.remediation}"
        )


@dataclass(frozen=True)
class ImportReference:
    module: str
    symbols: frozenset[str]
    line: int


def _relative(root: Path, path: Path) -> Path:
    return path.relative_to(root)


def _parts(relative: Path) -> tuple[str, ...]:
    return tuple(part.lower() for part in relative.parts)


def _is_under(parts: tuple[str, ...], *prefix: str) -> bool:
    return parts[: len(prefix)] == prefix


def _add(
    violations: list[Violation],
    rule_id: str,
    relative: Path,
    line: int,
    detail: str,
    remediation: str,
) -> None:
    violations.append(Violation(rule_id, relative, max(1, line), detail, remediation))


def _python_imports(tree: ast.AST) -> list[ImportReference]:
    references: list[ImportReference] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                references.append(ImportReference(alias.name, frozenset(), node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module:
            references.append(
                ImportReference(
                    node.module,
                    frozenset(alias.name for alias in node.names),
                    node.lineno,
                )
            )
    return references


def _text_imports(path: Path, text: str) -> list[ImportReference]:
    references: list[ImportReference] = []
    in_go_block = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if path.suffix == ".go":
            if re.match(r"^import\s*\($", stripped):
                in_go_block = True
                continue
            if in_go_block and stripped == ")":
                in_go_block = False
                continue
            match = (
                GO_BLOCK_ITEM.match(line)
                if in_go_block
                else GO_SINGLE_IMPORT.match(line)
            )
            if match:
                references.append(
                    ImportReference(match.group(1), frozenset(), line_number)
                )
            continue
        if path.suffix in {".js", ".jsx", ".ts", ".tsx"}:
            match = TS_IMPORT.match(line)
            if match:
                references.append(
                    ImportReference(match.group(1), frozenset(), line_number)
                )
            continue
        match = GENERIC_IMPORT.match(line)
        if match:
            module = next(group for group in match.groups() if group)
            references.append(ImportReference(module, frozenset(), line_number))
    return references


def _imports_testbeds(module: str) -> bool:
    return bool(
        re.search(r"(?:^|[./:_-])testbeds?(?:$|[./:_-])", module, re.IGNORECASE)
    )


def _module_matches(module: str, candidates: set[str]) -> bool:
    lowered = module.lower()
    return any(lowered == item or lowered.startswith(f"{item}.") for item in candidates)


def _provider_import(reference: ImportReference) -> bool:
    normalized = reference.module.lower().replace("-", "_")
    return (
        "action_controller" in normalized
        and "provider" in normalized
        or bool(reference.symbols & MUTATION_PROVIDER_SYMBOLS)
    )


def _python_write_client_lines(
    tree: ast.AST, imports: list[ImportReference]
) -> list[int]:
    lines = [
        reference.line
        for reference in imports
        if reference.module.startswith("kubernetes")
        and bool(reference.symbols & KUBERNETES_WRITE_CLIENTS)
    ]
    has_generic_client = any(
        reference.module in {"kubernetes", "kubernetes.client"} for reference in imports
    )
    if has_generic_client:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in KUBERNETES_WRITE_CLIENTS:
                    lines.append(node.lineno)
    return sorted(set(lines))


def _declared_provider_lines(path: Path, text: str, tree: ast.AST | None) -> list[int]:
    if path.suffix == ".py" and tree is not None:
        return sorted(
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name in MUTATION_PROVIDER_SYMBOLS
        )
    return [
        line_number
        for line_number, line in enumerate(text.splitlines(), start=1)
        if DECLARED_SYMBOL.search(line)
    ]


def _check_source(
    root: Path, path: Path, text: str, violations: list[Violation]
) -> None:
    relative = _relative(root, path)
    parts = _parts(relative)
    if not parts or parts[0] not in PRODUCTION_ROOTS:
        return

    tree: ast.AST | None = None
    if path.suffix == ".py":
        try:
            tree = ast.parse(text, filename=str(relative))
        except SyntaxError as error:
            _add(
                violations,
                "ARCH-PYTHON-PARSE",
                relative,
                error.lineno or 1,
                "production Python source could not be parsed",
                "Fix the syntax error so architecture imports can be inspected.",
            )
            return
        imports = _python_imports(tree)
    else:
        imports = _text_imports(path, text)

    for reference in imports:
        if _imports_testbeds(reference.module):
            _add(
                violations,
                "ARCH-PROD-NO-TESTBEDS",
                relative,
                reference.line,
                f"production source imports testbed module {reference.module!r}",
                "Move shared contracts to packages/ and keep testbed adapters test-only.",
            )

    is_reasoner = _is_under(parts, "services", "reasoner")
    is_scaler = _is_under(parts, "services", "keda-scaler") or _is_under(
        parts, "services", "keda_scaler"
    )
    is_guardian_application = _is_under(parts, "apps", "guardian_api") or _is_under(
        parts, "apps", "guardian-api"
    )
    if is_guardian_application:
        for reference in imports:
            if _module_matches(reference.module, MODEL_CLIENT_MODULES):
                _add(
                    violations,
                    "ARCH-GUARDIAN-NO-MODEL-CLIENT",
                    relative,
                    reference.line,
                    f"Guardian application imports model client {reference.module!r}",
                    "Keep classification, eligibility, policy, and action gates deterministic.",
                )
            if _provider_import(reference):
                _add(
                    violations,
                    "ARCH-GUARDIAN-NO-ACTION-PROVIDER",
                    relative,
                    reference.line,
                    "Guardian application imports an action or mutation provider",
                    "Keep execution providers behind the dedicated action controller.",
                )
        if tree is not None:
            for line in _python_write_client_lines(tree, imports):
                _add(
                    violations,
                    "ARCH-GUARDIAN-NO-WRITE-CLIENT",
                    relative,
                    line,
                    "Guardian application imports or initializes a write-capable Kubernetes client",
                    "Remove Kubernetes write credentials and delegate execution to the action controller.",
                )
    for reference in imports:
        if _module_matches(reference.module, MCP_CLIENT_MODULES):
            _add(
                violations,
                "ARCH-PROD-NO-MCP-CLIENT",
                relative,
                reference.line,
                f"production source imports MCP client {reference.module!r}",
                "Keep MCP clients in optional diagnostic integrations outside production services.",
            )
            if is_scaler:
                _add(
                    violations,
                    "ARCH-SCALER-NO-MCP-CLIENT",
                    relative,
                    reference.line,
                    f"KEDA scaler imports MCP client {reference.module!r}",
                    "Keep MCP out of the deterministic KEDA polling path.",
                )
    if is_reasoner:
        for reference in imports:
            if _provider_import(reference):
                _add(
                    violations,
                    "ARCH-REASONER-NO-ACTION-PROVIDER",
                    relative,
                    reference.line,
                    "reasoner imports an action or mutation provider",
                    "Use an allowlisted read-only evidence interface instead.",
                )
        if tree is not None:
            for line in _python_write_client_lines(tree, imports):
                _add(
                    violations,
                    "ARCH-REASONER-NO-WRITE-CLIENT",
                    relative,
                    line,
                    "reasoner imports or initializes a write-capable Kubernetes client",
                    "Replace it with a narrow read-only protocol implemented outside the reasoner.",
                )

    if is_scaler:
        for reference in imports:
            if _module_matches(reference.module, MODEL_CLIENT_MODULES):
                _add(
                    violations,
                    "ARCH-SCALER-NO-MODEL-CLIENT",
                    relative,
                    reference.line,
                    f"KEDA scaler imports model client {reference.module!r}",
                    "Remove model dependencies from the deterministic polling path.",
                )

    controller = _is_under(parts, "services", "action-controller") or _is_under(
        parts, "services", "action_controller"
    )
    if not controller and relative not in ARCHITECTURE_EXCEPTIONS:
        for line in _declared_provider_lines(path, text, tree):
            _add(
                violations,
                "ARCH-MUTATION-PROVIDER-PLACEMENT",
                relative,
                line,
                "mutation provider implementation exists outside action-controller",
                "Move the implementation to services/action-controller/providers/.",
            )


def _strip_line_comments(text: str, markers: tuple[str, ...]) -> str:
    output: list[str] = []
    for line in text.splitlines():
        quote: str | None = None
        escaped = False
        index = 0
        while index < len(line):
            character = line[index]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif quote:
                if character == quote:
                    quote = None
            elif character in {"'", '"'}:
                quote = character
            elif any(line.startswith(marker, index) for marker in markers):
                line = line[:index]
                break
            index += 1
        output.append(line)
    return "\n".join(output)


def _check_policy(
    root: Path, path: Path, text: str, violations: list[Violation]
) -> None:
    relative = _relative(root, path)
    if not _is_under(_parts(relative), "policies") or path.name == "AGENTS.md":
        return
    scanned = _strip_line_comments(text, ("#", "//"))
    for line_number, line in enumerate(scanned.splitlines(), start=1):
        lowered = line.lower()
        for term in sorted(DEMO_SPECIFIC_TERMS):
            if term in lowered:
                _add(
                    violations,
                    "ARCH-POLICY-NO-DEMO-NAMES",
                    relative,
                    line_number,
                    f"production policy contains demo-specific term {term!r}",
                    "Move environment binding to testbeds/ or approved environment configuration.",
                )
                return


def _rbac_verb_blocks(text: str) -> list[tuple[int, set[str]]]:
    blocks: list[tuple[int, set[str]]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = re.match(r"^(\s*)(?:-\s*)?verbs:\s*(.*)$", lines[index])
        if not match:
            index += 1
            continue
        line_number = index + 1
        indent = len(match.group(1))
        remainder = match.group(2)
        values = set(re.findall(r"[A-Za-z*]+", remainder.lower()))
        cursor = index + 1
        while not remainder.strip() and cursor < len(lines):
            child = lines[cursor]
            child_indent = len(child) - len(child.lstrip())
            if child.strip() and child_indent <= indent:
                break
            item = re.match(r"^\s*-\s*['\"]?([A-Za-z*]+)", child)
            if item:
                values.add(item.group(1).lower())
            cursor += 1
        blocks.append((line_number, values))
        index = max(index + 1, cursor)
    return blocks


def _check_scaler_rbac(
    root: Path, path: Path, text: str, violations: list[Violation]
) -> None:
    relative = _relative(root, path)
    parts = _parts(relative)
    if "keda-scaler" not in parts and "keda_scaler" not in parts:
        return
    scanned = _strip_line_comments(text, ("#",))
    if not re.search(r"(?im)^\s*kind:\s*(?:ClusterRole|Role)\s*$", scanned):
        return
    for line, verbs in _rbac_verb_blocks(scanned):
        forbidden = sorted(verbs & WRITE_RBAC_VERBS)
        if forbidden:
            _add(
                violations,
                "ARCH-SCALER-NO-WRITE-RBAC",
                relative,
                line,
                f"KEDA scaler RBAC grants forbidden verb {forbidden[0]!r}",
                "Remove write-capable RBAC; KEDA, not the scaler, changes replicas.",
            )
            return


def check_repository(root: Path) -> list[Violation]:
    """Return deterministic architecture violations for a repository tree."""

    root = root.resolve()
    violations: list[Violation] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(
            part in {".git", ".venv", "node_modules"} for part in path.parts
        ):
            continue
        suffix = path.suffix.lower()
        if suffix not in SOURCE_SUFFIXES | POLICY_SUFFIXES | RBAC_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if suffix in SOURCE_SUFFIXES:
            _check_source(root, path, text, violations)
        if suffix in POLICY_SUFFIXES:
            _check_policy(root, path, text, violations)
        if suffix in RBAC_SUFFIXES:
            _check_scaler_rbac(root, path, text, violations)
    return sorted(set(violations))


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    root = Path(arguments[0]) if arguments else Path.cwd()
    violations = check_repository(root)
    for violation in violations:
        print(violation)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
