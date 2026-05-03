"""Regression: package __init__.py used to have a SyntaxError (missing opening triple-quote)."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_root_init_parses_as_python():
    src = (ROOT / '__init__.py').read_text()
    ast.parse(src)  # raises SyntaxError on regression


def test_root_init_exposes_version():
    src = (ROOT / '__init__.py').read_text()
    ns = {}
    exec(compile(src, '__init__.py', 'exec'), ns)
    assert isinstance(ns.get('__version__'), str) and ns['__version__']
