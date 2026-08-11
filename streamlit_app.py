"""Entrypoint do Streamlit Cloud.

O serviço de deploy aponta o "main module" para este arquivo, na raiz do repo, em
vez de `src/dashboard/app.py` diretamente — ver CLAUDE.md e `src/dashboard/app.py`
para a lógica real do dashboard, que fica intacta.

Por que `runpy.run_path` em vez de `import src.dashboard.app`: `app.py` chama
`main()` no nível do módulo (sem guarda `if __name__ == "__main__"`), e o
Streamlit reexecuta o script principal do zero a cada interação (mudança de
filtro, clique etc.). Um `import` comum só executaria `app.py` na primeira vez —
fica em cache em `sys.modules` — e os widgets parariam de reagir depois do
primeiro render. `runpy.run_path` reexecuta o arquivo inteiro (inclusive o
`main()` do final) a cada rerun, reproduzindo o mesmo comportamento de rodar
`streamlit run src/dashboard/app.py` diretamente.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

# Garante que `from src.dashboard.data import ...` etc. resolvam em runtime,
# independentemente de o projeto estar instalado como pacote (não está — ver
# `package-mode = false` em pyproject.toml) ou de qual seja o cwd do processo.
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

runpy.run_path(str(REPO_ROOT / "src" / "dashboard" / "app.py"), run_name="__main__")
