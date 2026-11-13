"""AST boundaries: the shared codec and the file engine never do network work."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_layering():
    common = [ROOT/'common'/f for f in ('protocol.py','builders.py','models.py','errors.py')]
    engine = list((ROOT/'transfer').glob('*.py'))
    for path in [p for p in common if p.exists()] + engine:
        forbidden = {'socket','ssl','client','server'}
        if path in common:
            forbidden.add('transfer')
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            names = [n.name for n in node.names] if isinstance(node,ast.Import) else [node.module or ''] if isinstance(node,ast.ImportFrom) else []
            assert not {name.split('.')[0] for name in names} & forbidden, path
