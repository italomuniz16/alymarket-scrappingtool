"""Cliente HTTP de enriquecimento (httpx + tenacity), com cache local e backoff.

TODO: implementar client genérico usado pelos providers em `providers.py`. Só deve
operar sobre o subconjunto de leads já filtrado/exportável, nunca em massa.
"""
