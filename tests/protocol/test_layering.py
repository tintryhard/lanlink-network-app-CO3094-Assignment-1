"""AST boundaries prevent disk/network work from creeping into GUI widgets."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_layering():
    common = [ROOT/'common'/f for f in ('protocol.py','models.py','errors.py')]
    engine = list((ROOT/'transfer').glob('*.py'))
    widgets = [ROOT/'client/ui'/f for f in ('file_card.py','transfers_panel.py','file_format.py')]
    for path in common + engine + [p for p in widgets if p.exists()]:
        forbidden = {'socket','ssl','PySide6','client','server'}
        if path in common:
            forbidden.add('transfer')
        if path in widgets:
            forbidden = {'socket','ssl','hashlib','transfer','server','os','pathlib','threading','concurrent'}
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            names = [n.name for n in node.names] if isinstance(node,ast.Import) else [node.module or ''] if isinstance(node,ast.ImportFrom) else []
            assert not {name.split('.')[0] for name in names} & forbidden, path
            if path in widgets and isinstance(node,ast.Call):
                assert not (isinstance(node.func,ast.Name) and node.func.id=='open'), path
            if path in widgets and isinstance(node,ast.ImportFrom):
                assert not {n.name for n in node.names} & {'QFile', 'QDesktopServices', 'QThread'}, path
