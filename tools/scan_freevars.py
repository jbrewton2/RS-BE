import ast, os

ROOT = os.getcwd()

EXCLUDE_DIRS = {".git", ".venv", "venv", "__pycache__", "out", "dist", "build", ".mypy_cache", ".pytest_cache"}
EXCLUDE_FILES = {"tools\\scan_freevars.py", "questionnaire\\service_original.py"}

TARGETS = {
    "storage", "providers",
    "db", "session", "cursor",
    "vector_store", "vectorstore", "index", "search_client",
    "llm", "embedder", "embeddings",
    "s3", "bucket",
}

def iter_py_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root).replace("/", "\\")
                if rel in EXCLUDE_FILES:
                    continue
                yield full

class FuncScope(ast.NodeVisitor):
    def __init__(self):
        self.assigned = set()
        self.used = []  # (name, lineno, col)

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Store):
            self.assigned.add(node.id)
        elif isinstance(node.ctx, ast.Load):
            self.used.append((node.id, getattr(node, "lineno", 0), getattr(node, "col_offset", 0)))
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        if node.name:
            self.assigned.add(node.name)
        self.generic_visit(node)

def get_arg_names(fn: ast.AST):
    args = []
    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = fn.args
        for x in (a.posonlyargs + a.args + a.kwonlyargs):
            args.append(x.arg)
        if a.vararg: args.append(a.vararg.arg)
        if a.kwarg: args.append(a.kwarg.arg)
    return set(args)

def find_globals(fn: ast.AST):
    g = set()
    n = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Global):
            g.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            n.update(node.names)
    return g, n

def report():
    findings = []
    for path in iter_py_files(ROOT):
        rel = os.path.relpath(path, ROOT).replace("/", "\\")
        try:
            txt = open(path, "r", encoding="utf-8").read()
        except UnicodeDecodeError:
            continue

        # strip UTF-8 BOM if present
        if txt and txt[0] == "\ufeff":
            txt = txt.lstrip("\ufeff")

        try:
            tree = ast.parse(txt, filename=rel)
        except SyntaxError as e:
            findings.append((rel, e.lineno or 0, f"SYNTAX ERROR: {e.msg}"))
            continue

        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            arg_names = get_arg_names(fn)
            scope = FuncScope()
            scope.visit(fn)
            g, n = find_globals(fn)

            known = set(arg_names) | set(scope.assigned) | set(g) | set(n)

            for name, lineno, col in scope.used:
                if name in TARGETS and name not in known:
                    findings.append((rel, lineno, f"{fn.name}() uses '{name}' but it's not defined/passed"))

    findings.sort(key=lambda x: (x[0], x[1], x[2]))

    if not findings:
        print("âœ… No suspicious free-variable usage found for:", ", ".join(sorted(TARGETS)))
        return

    print("âš ï¸ Potential free-variable issues (review each):")
    for rel, line, msg in findings:
        print(f"- {rel}:{line}  {msg}")

if __name__ == "__main__":
    report()