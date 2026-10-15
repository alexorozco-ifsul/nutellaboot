"""Autenticação por classes de credencial.

Prefixos de chave: nb3a_ (admin), nb3s_ (serviço/MOJ), nb3i_ (token de
imagem), nb3m_ (chave de máquina). Só hashes sha256 ficam em disco
(exceto token/machine.key por imagem, que são o segredo distribuído).
"""

from __future__ import annotations

import fnmatch
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import Header, HTTPException, Request

from . import fsdb
from .settings import settings
from .errors import erro
from .services import keyusage


def new_key(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


@dataclass
class Principal:
    kind: str  # "admin" | "subadmin" | "service" | "image" | "machine"
    name: str = ""
    scopes: set[str] = field(default_factory=set)
    images: list[str] = field(default_factory=list)  # globs (serviço)
    # serviço com `labs:read`: de quem é a visão da frota que a chave segue
    # (services/fleet_views.py); vazio = só os globs, como sempre foi
    follow: str = ""
    # serviço: de quem é a chave. "admin" (ou vazio) = da administração;
    # `invite:<CÓDIGO>` = de um sub-admin, e ela só alcança as imagens dele
    key_owner: str = ""
    # admin: impressão digital da chave (o começo do hash). A sessão a guarda
    # para morrer junto com ESTA chave, e não com qualquer uma de mesmo id
    key_fp: str = ""
    # True quando sessions.resolve acabou de estender a sessão: o cookie
    # precisa ser reemitido nesta resposta (SessionCookieMiddleware)
    sessao_renovada: bool = False

    @property
    def owner(self) -> str:
        """Identidade usada no campo `owner` de modelos e site-images."""
        if self.kind == "admin":
            return "admin"
        if self.kind == "subadmin":
            return self.name
        return ""

    @property
    def chave_de_subadmin(self) -> bool:
        return self.kind == "service" and self.key_owner not in ("", "admin")

    def can_see_image(self, image_id: str, dono: str | None = None) -> bool:
        """`dono`: o `owner` da imagem, quando quem pergunta já o tem na mão
        (uma lista); sem ele, a chave de sub-admin lê o `image.json`."""
        if self.kind == "admin":
            return True
        if self.kind == "service":
            if self.chave_de_subadmin and dono is None:
                from .services import store

                dono = store.site_image_owner(image_id)
            return servico_alcanca(self.images, self.key_owner, image_id, dono or "admin")
        if self.kind == "subadmin":
            from .services import store

            return store.site_image_owner(image_id) == self.name
        return self.name == image_id


def servico_alcanca(globs: list[str], dono_da_chave: str, image_id: str, dono_da_imagem: str) -> bool:
    """A regra de alcance de uma chave de serviço, num lugar só.

    Da administração, o glob decide (lista vazia = todas). De sub-admin, o glob
    vale DENTRO das imagens do dono, conferido a cada requisição: nome de
    imagem é livre por ordem de chegada, então conferir o glob na criação não
    segura a imagem que outra pessoa cria depois com um nome que casa."""
    if dono_da_chave not in ("", "admin") and dono_da_imagem != dono_da_chave:
        return False
    return not globs or any(fnmatch.fnmatch(image_id, pat) for pat in globs)


def exigir_imagem_do_servico(p: Principal, image: str) -> None:
    """A chave de serviço numa rota de imagem: existe, é do dono, está no glob.

    A chave da administração recebe 404 se a imagem não existe e 403
    `image_out_of_scope` fora do glob: quem a emite enxerga tudo, e o 403 é o
    erro útil para quem integra. A de sub-admin segue o console de quem a
    criou (invariante 11): a imagem de outro dono NÃO EXISTE, 404 como a que
    não existe, senão a chave viraria oráculo de nomes. O 403 fica para a
    imagem dele fora do glob."""
    if not _site_image_dir(image).is_dir():
        raise erro(404, "image_not_found", "imagem não existe")
    dono = None
    if p.chave_de_subadmin:
        from .services import store

        dono = store.site_image_owner(image)
        if dono != p.key_owner:
            raise erro(404, "image_not_found", "imagem não existe")
    if not p.can_see_image(image, dono):
        raise erro(403, "image_out_of_scope", "sem acesso a esta imagem")


def _dono_da_chave_vale(dono: str) -> bool:
    """A chave de um sub-admin vale enquanto o console dele vale: suspenso,
    revogado, vencido ou apagado, ela para junto. Confere direto, sem o
    `identify_subadmin`: aquele chama `owners.ensure`, que grava o último
    acesso em disco, e o MOJ sonda o tempo todo."""
    from .services import invites, owners

    ok, _ = invites.is_valid_for_console(owners.code_of(dono))
    return ok and not owners.disabled(dono)


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:].strip()
    return None


def _site_image_dir(image_id: str) -> Path:
    return settings.data_root / "site-images" / image_id


def identify(token: str | None, image_id: str | None = None) -> Principal | None:
    """Resolve uma credencial Bearer para um Principal, ou None."""
    if not token:
        return None
    th = key_hash(token)

    admin = fsdb.read_json(settings.data_root / "keys" / "admin.json", {"keys": []})
    for entry in admin.get("keys", []):
        if secrets.compare_digest(th, entry.get("sha256", "")):
            keyusage.tocar("admin", th[:8])
            return Principal("admin", entry.get("id", "admin"), key_fp=th[:8])

    services = fsdb.read_json(settings.data_root / "keys" / "services.json", {})
    for name, entry in services.items():
        if secrets.compare_digest(th, entry.get("sha256", "")):
            dono = str(entry.get("owner") or "admin")
            if dono != "admin" and not _dono_da_chave_vale(dono):
                return None
            keyusage.tocar("service", name)
            return Principal(
                "service",
                name,
                scopes=set(entry.get("scopes", [])),
                images=list(entry.get("images", [])),
                follow=str(entry.get("follow") or ""),
                key_owner=dono,
            )

    if image_id:
        stored = (fsdb.read_text(_site_image_dir(image_id) / "token") or "").strip()
        if stored and secrets.compare_digest(token, stored):
            return Principal("image", image_id)

    return identify_subadmin(token)


def identify_subadmin(token: str | None) -> Principal | None:
    """O próprio código do convite é a credencial do sub-admin — sem cadastro
    separado, sem senha para esquecer. Quem revoga o convite tira o acesso."""
    from .services import invites, owners

    if not token or not invites.looks_like_code(token):
        return None
    code = invites.normalize(token)
    ok, _ = invites.is_valid_for_console(code)
    if not ok:
        return None
    oid = owners.owner_id(code)
    if owners.disabled(oid):
        return None
    owners.ensure(code)
    return Principal("subadmin", oid)


def identify_machine(machine_key: str | None, image_id: str) -> Principal | None:
    if not machine_key:
        return None
    stored = (fsdb.read_text(_site_image_dir(image_id) / "machine.key") or "").strip()
    if stored and secrets.compare_digest(machine_key.strip(), stored):
        return Principal("machine", image_id)
    # Só no descasamento (o caminho quente não muda): a chave anterior, durante
    # a carência de uma rotação. A máquina ligada só troca de chave no boot.
    prev = fsdb.read_json(_site_image_dir(image_id) / "machine.key.prev")
    if (
        prev
        and prev.get("key")
        and time.time() < float(prev.get("valid_until") or 0)
        and secrets.compare_digest(machine_key.strip(), str(prev["key"]))
    ):
        return Principal("machine", image_id)
    return None


def image_owner(image_id: str, token: str | None) -> bool:
    """True se o token é o da própria imagem (dono). Usado para deixar o dono
    disparar build de camada da SUA imagem, sem chave de admin."""
    if not token:
        return False
    stored = (fsdb.read_text(_site_image_dir(image_id) / "token") or "").strip()
    return bool(stored) and secrets.compare_digest(token.strip(), stored)


def boot_key_required(image_id: str) -> bool:
    """A imagem exige chave de boot? (existe boot.key no diretório dela)

    Imagens criadas pelo nutellaboot3 sempre têm. Apagar o arquivo torna a
    imagem aberta de novo — útil só para depuração, e está documentado.
    """
    return (_site_image_dir(image_id) / "boot.key").is_file()


def check_boot_key(image_id: str, key: str | None) -> bool:
    """Confere a chave que o pendrive carrega no nutellaboot.conf.

    Ela também vale como chave de máquina: a máquina que bootou com o
    pendrive certo é a mesma que reporta telemetria e semeia a imagem.
    """
    stored = (fsdb.read_text(_site_image_dir(image_id) / "boot.key") or "").strip()
    if not stored:
        return not boot_key_required(image_id)
    if not key:
        return False
    return secrets.compare_digest(key.strip(), stored)


def _unauthorized() -> HTTPException:
    return erro(401, "unauthorized", "credencial ausente ou inválida")


# Cabeçalho que a autenticação por COOKIE exige. Um <form> de outro site não
# consegue definir cabeçalho, e um fetch cross-site com cabeçalho custom
# dispara preflight (não há CORS configurado, então morre ali). É esta linha
# que segura o CSRF que o cookie introduziria — em especial nos POST sem corpo
# obrigatório (lock_all, rotate_token, disable_owner...), os únicos que um
# formulário simples conseguiria disparar.
HEADER_CONSOLE = "x-nb-console"


def principal(request: Request | None, authorization: str | None, image_id: str | None = None):
    """A credencial, de onde ela vier — num ponto só.

    Ordem: `Authorization: Bearer` primeiro (ferramentas, MOJ, máquinas — nada
    mudou para elas), cookie de sessão depois. O cookie só vale com o cabeçalho
    de console.

    É pública porque rota que não usa `Depends` (as de camada por imagem, que
    misturam credencial de console com token da imagem) precisa chamá-la. Ler
    `Authorization` na mão fora daqui já custou caro: as rotas de layerbuild
    faziam isso e ficaram 401 para o console inteiro quando a sessão por cookie
    entrou.
    """
    p = identify(_bearer(authorization), image_id=image_id)
    if p is not None:
        return p
    if request is None:
        return None
    if request.headers.get(HEADER_CONSOLE) is None:
        return None

    from .services import sessions

    sid = request.cookies.get(sessions.COOKIE, "")
    p = sessions.resolve(sid)
    if p is not None and p.sessao_renovada:
        # vive em scope["state"]; o middleware põe o Set-Cookie na resposta
        request.state.reemitir_cookie = sid
    return p


def principal_de_link(request: Request, tk: str = "", image_id: str | None = None):
    """Credencial de um GET que o próprio navegador carrega.

    `<img src>`, `<a download>` e `EventSource` não mandam cabeçalho nenhum:
    aqui vale `?tk=` (o token que já está na URL da tela de sede) ou o cookie de
    sessão **sem** `X-NB-Console`. É a única exceção à invariante 14, e vale só
    para leitura — é GET, não muda estado, e sem CORS uma página de outro site
    não lê a resposta.
    """
    p = principal(request, request.headers.get("authorization"), image_id=image_id)
    if p is None and tk:
        p = identify(tk, image_id=image_id)
    if p is None:
        from .services import sessions

        # sem renovar: <img>/<a download> não recebem cookie de volta, e
        # renovar em disco aqui consumiria a renovação do dia sem reemitir nada
        p = sessions.resolve(request.cookies.get(sessions.COOKIE, ""), renovar=False)
    return p


def recusa_de_console(p: Principal | None) -> HTTPException:
    """A recusa certa para quem bateu numa rota do console.

    Chave de serviço VÁLIDA levava 401 "credencial ausente ou inválida", e o
    preflight do MOJ concluía que a chave estava errada. 401 é só para
    credencial que não vale; a que vale e não pode é 403, com o código que diz
    por quê. (Sub-admin em rota só de admin continua 401: é a invariante 11,
    o console não confirma o que existe para quem não é dono.)"""
    if p is not None and p.kind == "service":
        return erro(403, "console_only", "esta rota é do console; chave de serviço não entra")
    return _unauthorized()


async def require_admin(
    request: Request = None, authorization: str | None = Header(None)
) -> Principal:
    p = principal(request, authorization)
    if not p or p.kind != "admin":
        raise recusa_de_console(p)
    return p


async def require_console(
    request: Request, authorization: str | None = Header(None)
) -> Principal:
    """Console de administração: chave de admin ou código de convite.

    O código de convite é curto o bastante para ser digitado à mão, então o
    erro é limitado por IP — sem isso, adivinhar um código seria só uma
    questão de tempo de CPU alheio.
    """
    from .services import ratelimit

    p = principal(request, authorization)
    if p and p.kind in ("admin", "subadmin"):
        return p
    if p is not None and p.kind == "service":
        # antes do limitador: chave válida não é tentativa de adivinhar código,
        # e não pode gastar o balde do IP (o MOJ levava 429 por perguntar)
        raise recusa_de_console(p)
    ip = ratelimit.client_ip(request)
    ratelimit.exigir(f"console:{ip}", rate=0.2, burst=10)
    raise _unauthorized()


async def require_console_or_service(
    request: Request, authorization: str | None = Header(None)
) -> Principal:
    """Console, ou qualquer chave de serviço válida: as poucas rotas em que o
    integrador precisa se enxergar (`/whoami`, a lista de imagens)."""
    p = principal(request, authorization)
    if p is not None and p.kind == "service":
        return p
    return await require_console(request, authorization)


# Para `service_scope`: qualquer chave de serviço válida cujo glob cubra a
# imagem, sem escopo específico.
QUALQUER = "*"


def require_image_access(
    *, service_scope: str | tuple[str, ...] | None = None, allow_machine: bool = False
):
    """Dependência para rotas /images/{image}: aceita admin, token da própria
    imagem e, se `service_scope`, serviço com o escopo (uma tupla = qualquer um
    deles; `QUALQUER` = toda chave válida); `allow_machine` aceita a chave de
    máquina da imagem (header X-NB-Machine-Key)."""
    escopos = (service_scope,) if isinstance(service_scope, str) else tuple(service_scope or ())

    async def dep(
        image: str,
        request: Request = None,
        authorization: str | None = Header(None),
        x_nb_machine_key: str | None = Header(None),
    ) -> Principal:
        if allow_machine:
            p = identify_machine(x_nb_machine_key, image)
            if p:
                return p
        # a credencial vem ANTES da existência: conferir o diretório primeiro
        # deixava um anônimo separar "imagem existe" (401) de "não existe"
        # (404) sem credencial nenhuma
        p = principal(request, authorization, image_id=image)
        if not p:
            raise _unauthorized()
        if p.kind == "service":
            # erro claro vale mais que sigilo para quem integra: "faltou
            # escopo" e "fora do glob" são 403 distintos (o de sub-admin
            # responde 404 na imagem de outro dono: exigir_imagem_do_servico)
            if not escopos or (QUALQUER not in escopos and not p.scopes.intersection(escopos)):
                raise erro(403, "insufficient_scope", "escopo insuficiente")
            exigir_imagem_do_servico(p, image)
            return p
        # invariante 11: para quem entra pelo console (ou com token de outra
        # imagem), o que não é seu não existe. Um 403 aqui viraria oráculo de
        # nomes de sala — e são ~26 rotas herdando esta dependência.
        if not _site_image_dir(image).is_dir() or not p.can_see_image(image):
            raise erro(404, "image_not_found", "imagem não existe")
        return p

    return dep


def conferir_reauth(p: Principal, request: Request, authorization: str | None, current_key) -> None:
    """Para as rotas que cunham ou matam chave de ADMIN: prova de posse da
    chave, não só da sessão.

    O cookie prova que alguém entrou neste navegador; não prova que quem está
    clicando agora é essa pessoa (aba aberta, sessão de 30 dias), e um script
    injetado na tela do admin usaria a sessão para cunhar uma chave que
    sobrevive a ela. Quem vem por Bearer já provou a posse na própria
    requisição. Quem vem por cookie redigita a chave, e ela tem de ser a MESMA
    da sessão (id e impressão digital).

    A recusa é 403, não 401: 401 faria a tela concluir que a sessão morreu."""
    from .services import audit, ratelimit

    via_bearer = identify(_bearer(authorization))
    if via_bearer is not None and via_bearer.kind == "admin":
        return
    q = identify(str(current_key or "").strip()) if current_key else None
    if q is not None and q.kind == "admin" and q.name == p.name and (not p.key_fp or q.key_fp == p.key_fp):
        return
    audit.registrar(p, request, "reauth.failed")
    ratelimit.exigir(f"reauth:{ratelimit.client_ip(request)}", rate=0.1, burst=5)
    raise erro(403, "reauth_required", "confirme com a sua chave de administração")


def require_admin_or_service(scope: str):
    """Rota de uma imagem que é da ADMINISTRAÇÃO (sub-admin e token da sede não
    entram) e que uma chave de serviço com `scope` também alcança, dentro do
    glob dela. É o caso dos webhooks: a chave só vê os que ela criou."""

    async def dep(
        image: str, request: Request = None, authorization: str | None = Header(None)
    ) -> Principal:
        p = principal(request, authorization)
        if p is not None and p.kind == "service":
            if scope not in p.scopes:
                raise erro(403, "insufficient_scope", "escopo insuficiente")
            exigir_imagem_do_servico(p, image)
            return p
        if not p or p.kind != "admin":
            raise _unauthorized()
        if not _site_image_dir(image).is_dir():
            raise erro(404, "image_not_found", "imagem não existe")
        return p

    return dep


def require_machine(image: str, x_nb_machine_key: str | None) -> Principal:
    """Só a chave de máquina da imagem (telemetria/fila)."""
    if not _site_image_dir(image).is_dir():
        raise erro(404, "image_not_found", "imagem não existe")
    p = identify_machine(x_nb_machine_key, image)
    if not p:
        raise _unauthorized()
    return p
