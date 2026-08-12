"""Embed do frontend React (`frontend/dist/index.html`) dentro do Streamlit.

O Streamlit Community Cloud roda um único processo Python e não serve arquivos
estáticos arbitrários (não dá pra publicar o build do React num endereço
separado). Em vez disso, este módulo lê o HTML autocontido (JS/CSS/assets todos
inline, via `vite-plugin-singlefile` -- ver `frontend/vite.config.ts`) e o embute
via `st.components.v1.html`, dentro de um iframe sandboxed. A API que esse HTML
consome (fetch para `VITE_API_BASE_URL`, embutido no build em tempo de
compilação -- ver `frontend/.env.production`) fica hospedada separadamente
(Render; ver `requirements-api.txt`), já que ela também não cabe no processo
único do Streamlit Cloud.

`src/dashboard/app.py` (o dashboard 100% Python/Streamlit) continua existindo
intacto como alternativa -- ver o toggle `ALYMARKET_UI` em `streamlit_app.py`.
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

# Path configurável só pra facilitar teste local com um build em outro lugar;
# em produção (Streamlit Cloud) sempre é o default, relativo à raiz do repo.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DIST_INDEX = _REPO_ROOT / "frontend" / "dist" / "index.html"


def main() -> None:
    st.set_page_config(page_title="alymarket — leads", layout="wide")

    dist_index = Path(os.environ.get("FRONTEND_DIST_INDEX", str(DEFAULT_DIST_INDEX)))

    if not dist_index.is_file():
        st.error(
            "Build do frontend React não encontrado em "
            f"`{dist_index}`.\n\n"
            "Gere-o com `cd frontend && npm run build` (precisa de "
            "`frontend/.env.production` com `VITE_API_BASE_URL` apontando pra "
            "API em produção) e garanta que `frontend/dist/index.html` esteja "
            "commitado no repo. Alternativamente, defina "
            "`ALYMARKET_UI=streamlit` para usar o dashboard 100% Python."
        )
        return

    html = dist_index.read_text(encoding="utf-8")
    # Altura grande e fixa (não há como o iframe se auto-ajustar ao conteúdo sem
    # JS extra de postMessage, que o build não implementa) -- 1800px cobre o
    # layout completo (filtros + KPIs + scheduler + tabela + gráficos +
    # compliance) sem scroll duplo na maioria das telas; `scrolling=True` cobre
    # o resto.
    st.components.v1.html(html, height=1800, scrolling=True)


if __name__ == "__main__":
    main()
