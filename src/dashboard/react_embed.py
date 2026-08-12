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

    # `layout="wide"` só zera a margem lateral EXTRA que o layout centralizado
    # aplicaria -- o `.block-container` padrão do Streamlit ainda reserva
    # ~96px em cima / ~80px nas laterais / ~160px embaixo em volta de todo o
    # conteúdo principal, o que sobra como uma moldura clara ao redor do
    # iframe (o React já tem seu próprio `bg-background` de borda a borda por
    # dentro). Zeramos esse padding especificamente pra essa página -- não
    # afeta `src/dashboard/app.py` (outro processo de rerun, CSS não vaza
    # entre eles).
    #
    # `header[data-testid="stHeader"]` (a barra "Share/★/✎/GitHub/⋮" do
    # Streamlit Cloud) fica oculta por completo (`display: none`) -- essa
    # página já tem seu próprio header (a logo/marca do React lá dentro), a
    # barra do Streamlit por cima só duplicava chrome e (como era `position:
    # absolute`, 60px, `z-index: 999990`) obrigava a reservar espaço em cima
    # só pra não tampar nosso conteúdo. Com ela escondida, volta a fazer
    # sentido zerar o padding-top também -- nada mais reserva aquele espaço.
    #
    # Três sobras finas continuavam aparecendo mesmo com o padding zerado
    # (cada uma com uma causa diferente, todas confirmadas inspecionando o
    # box model ao vivo -- ver histórico do commit):
    # 1. `[data-testid="stVerticalBlock"]` é `display:flex; gap:16px`. Este
    #    próprio `st.markdown(...)` (que só injeta um `<style>`, sem saída
    #    visível) também vira um item de layout ali -- um
    #    `stElementContainer` de altura 0 antes do nosso iframe. O `gap`
    #    do flex conta a fronteira entre os dois itens mesmo o primeiro
    #    tendo altura zero, empurrando o iframe 16px pra baixo. Como essa
    #    página só tem esses dois itens (o style injetado e o iframe), zerar
    #    o `gap` do bloco é seguro e não afeta espaçamento visível nenhum.
    # 2. `[data-testid="stIFrame"]` (o próprio `<iframe>`) também tem
    #    `margin: 0 auto 16px` por padrão do Streamlit -- zerado.
    # 3. `[data-testid="stMain"]` tem `overflow-y: auto`: como o iframe é
    #    mais alto que a viewport, a PÁGINA do Streamlit também precisa
    #    rolar, e isso reserva ~10px de canaleta de scrollbar nativa na
    #    lateral, expondo o fundo claro do tema Streamlit (`.streamlit/
    #    config.toml`) por trás -- não dava pra "casar a cor" porque o tema
    #    do React é dinâmico (claro/escuro, depende do usuário). Em vez
    #    disso, o iframe passa a ocupar exatamente `100vh` (via CSS,
    #    sobrepondo o atributo `height` fixo que o Streamlit aplica) e
    #    `stMain` ganha `overflow: hidden` -- a PÁGINA do Streamlit nunca
    #    mais rola; só o iframe rola por dentro (`scrolling=True`, já
    #    configurado), com sua própria barra de rolagem dentro do tema
    #    React, sem expor nada por trás.
    #
    # Seletores sem qualificador de tag (`[data-testid=...]`, não
    # `div[data-testid=...]`) de propósito -- `stMain` por exemplo é uma
    # `<section>`, não uma `<div>`; qualificar a tag errada faz a regra CSS
    # simplesmente não bater com nada, silenciosamente.
    st.markdown(
        """
        <style>
        [data-testid="stMainBlockContainer"] {
            padding: 0 !important;
            max-width: 100% !important;
        }
        [data-testid="stHeader"] {
            display: none !important;
        }
        [data-testid="stMain"] {
            overflow: hidden !important;
        }
        [data-testid="stVerticalBlock"] {
            gap: 0 !important;
        }
        [data-testid="stIFrame"] {
            margin: 0 !important;
            display: block !important;
            height: 100vh !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

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
    # `height=1800` é só o atributo HTML de fallback -- o CSS acima sobrepõe
    # pra `100vh` de verdade (não há como o iframe se auto-ajustar ao
    # conteúdo sem JS extra de postMessage, que o build não implementa; fixar
    # em `100vh` em vez de um pixel fixo é o que faz a página do Streamlit
    # nunca precisar rolar, ver comentário acima). `scrolling=True` deixa o
    # iframe rolar por dentro pro conteúdo que não cabe na tela.
    st.components.v1.html(html, height=1800, scrolling=True)


if __name__ == "__main__":
    main()
