"""Installed, non-editable admission for automatic text-only repairs."""
import ast
from pathlib import Path

FUNCTIONS = {'sources.py': {'direct_text', 'timestamp', '_text'},
             'journal.py': {'incident_body'}}
GLOBALS = {'re', 'datetime', 'ZONE', 'STATES', 'isinstance', 'str', 'list', 'int',
           'float', 'dict', 'len', 'enumerate', 'sorted', 'min', 'max', 'ValueError',
           'TypeError', 'WRAPPER'}
ATTRIBUTES = {'sub', 'search', 'split', 'splitlines', 'strip', 'lstrip', 'rstrip',
              'startswith', 'endswith', 'replace', 'join', 'get', 'items', 'values',
              'keys', 'lower', 'upper', 'casefold', 'fromisoformat', 'timestamp',
              'fromtimestamp', 'strftime', 'append', 'extend', 'find', 'count'}

def validate_pure_change(path, text):
    allowed = FUNCTIONS.get(Path(path).name, set())
    if not allowed: raise PermissionError('automatic_surface_not_registered')
    original = ast.parse(Path(__file__).with_name(Path(path).name).read_text())
    candidate = ast.parse(text)
    def protected(tree):
        return [ast.dump(n, include_attributes=False) for n in tree.body
                if not isinstance(n, ast.FunctionDef) or n.name not in allowed]
    if protected(original) != protected(candidate):
        raise PermissionError('protected_code_changed')
    before = {n.name: n for n in original.body if isinstance(n, ast.FunctionDef)}
    after = {n.name: n for n in candidate.body if isinstance(n, ast.FunctionDef)}
    if set(before) != set(after): raise PermissionError('function_surface_changed')
    for name in allowed:
        fn = after[name]
        if ast.dump(fn.args) != ast.dump(before[name].args) or fn.decorator_list:
            raise PermissionError('function_contract_changed')
        locals_ = {a.arg for a in fn.args.args}
        locals_.update(n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
        for n in ast.walk(fn):
            if isinstance(n, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal,
                              ast.With, ast.AsyncWith, ast.ClassDef, ast.Lambda)):
                raise PermissionError('repair_effect_not_allowed')
            if isinstance(n, ast.FunctionDef) and n is not fn:
                raise PermissionError('nested_repair_function')
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in locals_ | GLOBALS:
                raise PermissionError('repair_global_not_allowed')
            if isinstance(n, ast.Attribute) and n.attr not in ATTRIBUTES:
                raise PermissionError('repair_attribute_not_allowed')
            if isinstance(n, ast.Call):
                if not isinstance(n.func, (ast.Name, ast.Attribute)):
                    raise PermissionError('indirect_repair_call')
                if isinstance(n.func, ast.Name) and n.func.id not in GLOBALS:
                    raise PermissionError('repair_callable_not_allowed')
