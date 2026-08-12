"""Entrypoint do Streamlit Cloud.

O serviço de deploy aponta o "main module" para este arquivo, na raiz do repo, em
vez de `src/dashboard/app.py` diretamente — ver CLAUDE.md e `src/dashboard/app.py`
para a lógica real do dashboard, que fica intacta.

Por que `runpy.run_path` em vez de `import src.dashboard.app`: `app.py` (e,
igualmente, `react_embed.py`) chama `main()` no nível do módulo (sem guarda
`if __name__ == "__main__"`), e o Streamlit reexecuta o script principal do zero
a cada interação (mudança de filtro, clique etc.). Um `import` comum só
executaria o módulo na primeira vez — fica em cache em `sys.modules` — e os
widgets parariam de reagir depois do primeiro render. `runpy.run_path` reexecuta
o arquivo inteiro (inclusive o `main()` do final) a cada rerun, reproduzindo o
mesmo comportamento de rodar `streamlit run <arquivo>` diretamente.

Toggle `ALYMARKET_UI` (env var, default `"streamlit"`):
- `"streamlit"` (default): dashboard 100% Python (`src/dashboard/app.py`).
- `"react"`: embute o build do frontend React (`src/dashboard/react_embed.py`,
  ver esse módulo para os detalhes) — a API que ele consome é hospedada à parte
  (Render). Rollback pra `"streamlit"` é só trocar a env var no Streamlit Cloud,
  sem reverter commit nem redeploy manual do código.
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

# Garante que `from src.dashboard.data import ...` etc. resolvam em runtime,
# independentemente de o projeto estar instalado como pacote (não está — ver
# `package-mode = false` em pyproject.toml) ou de qual seja o cwd do processo.
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_UI = os.environ.get("ALYMARKET_UI", "streamlit")
_ENTRYPOINTS = {
    "streamlit": REPO_ROOT / "src" / "dashboard" / "app.py",
    "react": REPO_ROOT / "src" / "dashboard" / "react_embed.py",
}
_entrypoint = _ENTRYPOINTS.get(_UI, _ENTRYPOINTS["streamlit"])

runpy.run_path(str(_entrypoint), run_name="__main__")
