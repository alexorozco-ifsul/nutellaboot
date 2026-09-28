# NutellaBoot 3 em container, para o cluster Nomad do IFSul Sapucaia.
#
# Uma imagem, duas tasks no mesmo grupo do Nomad (mesma rede):
#   app   -> uvicorn em 127.0.0.1:8890, UM worker (ver systemd/nutellaboot3.service)
#   nginx -> :8080, serve web/, docs/ e blobs/ e repassa /api, /boot, /lock
# Equivale ao systemd + nginx da VM; nada de supervisor dentro do container.
#
# Dados (NB3_DATA_ROOT) e o kernel/initrd servidos às máquinas (NB3_BUILD_DIR)
# ficam no volume do Nomad, fora da imagem: o initrd tem de ser byte a byte o
# que as máquinas já têm, senão todas se auto-atualizam no boot de prova.
FROM python:3.13-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b

# nginx; openssl (hash de senha, services/config.py); rsync/ssh (publicação,
# services/publish.py); o resto é o nb3-genusb (imagem de pendrive, sem root)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      nginx openssl rsync openssh-client \
      fdisk mtools coreutils grub-common grub-efi-amd64-bin grub-pc-bin \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 1000 nutellaboot \
 && useradd -u 1000 -g 1000 -d /var/lib/nutellaboot3 -M -s /usr/sbin/nologin nutellaboot

WORKDIR /opt/nutellaboot3
COPY ifsul/requirements.txt ifsul/requirements.txt
RUN python -m venv .venv && .venv/bin/pip install --no-cache-dir -r ifsul/requirements.txt

COPY . .
RUN rm -rf client/build && ln -s /var/lib/nutellaboot3/client-build client/build

ENV NB3_DATA_ROOT=/var/lib/nutellaboot3 \
    NB3_BUILD_DIR=/var/lib/nutellaboot3/client-build \
    HOME=/var/lib/nutellaboot3 \
    NB3_LONGPOLL_BACKSTOP=25 \
    PYTHONDONTWRITEBYTECODE=1

USER 1000:1000
EXPOSE 8080
CMD [".venv/bin/uvicorn", "server.app.main:app", \
     "--host", "127.0.0.1", "--port", "8890", "--backlog", "4096", \
     "--timeout-keep-alive", "65", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1", \
     "--log-level", "warning", "--no-access-log"]
