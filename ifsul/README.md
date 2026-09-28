# NutellaBoot no IFSul Sapucaia

Branch `ifsul` = `local/hosts-shared-ip` (as correções que rodam no lab 219 e
ainda não estão no upstream) + esta pasta, que só serve à implantação local.
Correções para o upstream saem de branches próprias, sem esta pasta.

- `Dockerfile`: a imagem; o workflow `.github/workflows/imagem-ifsul.yml` a
  publica em `ghcr.io/alexorozco-ifsul/nutellaboot:ifsul-<commit>` a cada push.
- `nginx.conf` / `nginx-proxy.conf`: o site de `deploy/`, sem root, com os
  ajustes para rodar atrás do HAProxy (`X-Forwarded-Proto`, `absolute_redirect`).
- O job do Nomad fica no repositório do cluster (`servicos/nutellaboot/`).

Atualizar o código: rebase desta branch sobre a nova `local/hosts-shared-ip`
(ou sobre o upstream, quando as correções forem aceitas), push, e trocar a tag
no job do Nomad.
