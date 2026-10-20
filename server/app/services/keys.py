"""As chaves de administração e de serviço: um lugar só que lê e escreve.

`admin.json` e `services.json` eram mexidos por três pontos (a rota das chaves
de serviço, o `nb3-init`, e à mão na produção), sem lock e sem data. Aqui toda
escrita segura o lock do diretório `keys/`, toda chave nasce com `created_at` e
`created_by`, e as regras que não podem ser esquecidas moram junto do arquivo:

- nunca se revoga a ÚLTIMA chave de admin (seria trancar-se para fora);
- o id de uma chave de admin é único (o `nb3-init` aceitava repetir: com dois
  "admin", revogar um não derrubava sessão nenhuma);
- criar chave de serviço com nome que já existe é erro, não sobrescrita: recriar
  "moj" pela tela trocaria a credencial do MOJ calado. Trocar é `rotacionar`;
- a chave de serviço de um sub-admin leva o dono, ganha o sufixo
  `@<owner_ref>` no nome e só alcança as imagens dele (`auth.servico_alcanca`).

Só o hash fica em disco. `fp` é a impressão digital curta (o começo do hash):
serve para dizer "esta chave" sem revelar nada útil sobre uma chave aleatória
de 128 bits.
"""

from __future__ import annotations

import re
import time

from .. import auth, fsdb
from ..settings import settings
from . import keyusage

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
NOME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ErroDeChave(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def _dir():
    return settings.data_root / "keys"


def _trava():
    return fsdb.locked(_dir())


def fp_de(sha256: str) -> str:
    return str(sha256)[:8]


# --- administração ---


def _admin() -> dict:
    return fsdb.read_json(_dir() / "admin.json", {"keys": []}) or {"keys": []}


def admin_listar() -> list[dict]:
    out = []
    for e in _admin().get("keys", []):
        fp = fp_de(e.get("sha256", ""))
        out.append(
            {
                "id": e.get("id", "admin"),
                "fp": fp,
                "created_at": e.get("created_at"),
                "created_by": e.get("created_by", ""),
                "last_used": keyusage.ultimo("admin", fp),
            }
        )
    return out


def admin_criar(ident: str, by: str = "") -> dict:
    ident = str(ident or "").strip()
    if not ID_RE.match(ident):
        raise ErroDeChave(400, "invalid_key_id", "id: 1 a 32 caracteres [a-z0-9._-], começando por letra ou dígito")
    chave = auth.new_key("nb3a")
    with _trava():
        atual = _admin()
        if any(e.get("id", "admin") == ident for e in atual.get("keys", [])):
            raise ErroDeChave(409, "key_exists", f"já existe uma chave de admin com o id {ident}")
        agora = int(time.time())
        atual.setdefault("keys", []).append(
            {"id": ident, "sha256": auth.key_hash(chave), "created_at": agora, "created_by": by}
        )
        fsdb.write_json(_dir() / "admin.json", atual, mode=0o600)
    return {"id": ident, "key": chave, "fp": fp_de(auth.key_hash(chave)), "created_at": agora}


def admin_revogar(ident: str, fp: str = "") -> dict:
    with _trava():
        atual = _admin()
        chaves = atual.get("keys", [])
        alvo = [e for e in chaves if e.get("id", "admin") == ident and (not fp or fp_de(e.get("sha256", "")) == fp)]
        if not alvo:
            raise ErroDeChave(404, "key_not_found", "chave de admin não existe")
        if len(alvo) > 1:
            # herança do nb3-init antigo: dois ids iguais. Só a impressão digital desempata.
            raise ErroDeChave(409, "key_ambiguous", f"há {len(alvo)} chaves com o id {ident}: informe fp")
        if len(chaves) <= 1:
            raise ErroDeChave(409, "last_admin_key", "é a última chave de admin: crie outra antes de revogar esta")
        chaves.remove(alvo[0])
        fsdb.write_json(_dir() / "admin.json", atual, mode=0o600)
    revogada = fp_de(alvo[0].get("sha256", ""))
    keyusage.esquecer("admin", revogada)
    return {"id": ident, "fp": revogada, "remaining": len(chaves)}


# --- serviço ---
#
# A chave de serviço é da administração ou de um sub-admin (`owner`; ausente =
# administração). A de sub-admin ganha no nome o sufixo `@<owner_ref>`: o `@`
# não passa no `NOME_RE`, então ela não colide com a da administração nem com a
# de outro sub-admin, e o 409 de nome repetido não conta o que os outros têm. O
# alcance (o glob dentro das imagens do dono) mora em `auth.servico_alcanca`.

ADMIN = "admin"
GLOBS_MAX = 20
GLOB_MAX = 64


def _servicos() -> dict:
    return fsdb.read_json(_dir() / "services.json", {}) or {}


def dono_de(v: dict) -> str:
    return str(v.get("owner") or ADMIN)


def _servico_publico(nome: str, v: dict, imagens: list[dict] | None = None) -> dict:
    from . import ownership

    dono = dono_de(v)
    out = {
        "name": nome,
        "scopes": v.get("scopes", []),
        "images": v.get("images", []),
        "follow": v.get("follow", ""),
        "created_at": v.get("created_at"),
        "created_by": v.get("created_by", ""),
        "rotated_at": v.get("rotated_at"),
        "last_used": keyusage.ultimo("service", nome),
        # nunca o `owner` cru: o de um sub-admin é o código do convite, que é a
        # credencial do console dele
        **ownership.owner_publico(dono),
    }
    if imagens is not None:
        # quantas imagens a chave alcança AGORA, pela regra da requisição
        out["covers"] = sum(
            1
            for i in imagens
            if auth.servico_alcanca(out["images"], dono, str(i.get("id", "")), str(i.get("owner") or ADMIN))
        )
    return out


def servico_listar(dono: str | None = None) -> list[dict]:
    """Todas (a administração) ou só as de `dono` (um sub-admin)."""
    from . import store

    imagens = store.list_site_images()
    return [_servico_publico(n, v, imagens) for n, v in _servicos().items() if dono is None or dono_de(v) == dono]


def contar() -> int:
    return len(_servicos())


def contar_do_dono(dono: str) -> int:
    return sum(1 for v in _servicos().values() if dono_de(v) == dono)


def _confere_globs(imagens, dono: str) -> list[str]:
    if not isinstance(imagens, list):
        # uma string seria lida letra a letra
        raise ErroDeChave(400, "bad_request", "images é uma lista de globs")
    globs = [str(g).strip() for g in imagens if str(g).strip()]
    if dono != ADMIN:
        if len(globs) > GLOBS_MAX:
            raise ErroDeChave(400, "bad_request", f"no máximo {GLOBS_MAX} globs por chave")
        if any(len(g) > GLOB_MAX for g in globs):
            raise ErroDeChave(400, "bad_request", f"cada glob tem até {GLOB_MAX} caracteres")
    return globs


def _confere_follow(follow, escopos, dono: str = ADMIN) -> str:
    """`owner` segue a visão da frota de quem criou a chave. `admin` é o nome
    antigo da mesma coisa e só vale em chave da administração: o telão de um
    sub-admin não segue a escolha da administração."""
    follow = str(follow or "")
    validos = ("owner", "admin") if dono == ADMIN else ("owner",)
    if follow and follow not in validos:
        if dono == ADMIN:
            raise ErroDeChave(
                400, "bad_request", 'follow: "owner" (a visão da frota de quem criou a chave), "admin" ou vazio'
            )
        raise ErroDeChave(400, "bad_request", 'follow: "owner" (a sua visão da frota) ou vazio')
    if follow and "labs:read" not in escopos:
        raise ErroDeChave(400, "bad_request", "follow só faz sentido numa chave com labs:read")
    return follow


def _da_chave(conf: dict, nome: str, dono: str | None) -> dict:
    """A chave `nome`, se quem pede pode mexer nela. A de outro dono não
    existe: o mesmo 404 da que não existe."""
    v = conf.get(nome)
    if v is None or (dono is not None and dono_de(v) != dono):
        raise ErroDeChave(404, "key_not_found", "chave de serviço não existe")
    return v


def servico_criar(
    nome: str,
    escopos: list,
    imagens,
    follow: str = "",
    by: str = "",
    dono: str = ADMIN,
    teto: int | None = None,
) -> dict:
    """`teto`: a cota de chaves do dono (None = sem limite, a administração)."""
    nome = str(nome or "").strip()
    if not nome:
        raise ErroDeChave(400, "bad_request", "informe um nome")
    if not NOME_RE.match(nome):
        raise ErroDeChave(400, "bad_request", "nome: até 64 caracteres [A-Za-z0-9._-]")
    if dono != ADMIN:
        from . import ownership

        nome = f"{nome}@{ownership.owner_ref(dono)}"
    globs = _confere_globs(imagens, dono)
    follow = _confere_follow(follow, escopos, dono)
    chave = auth.new_key("nb3s")
    with _trava():
        conf = _servicos()
        if nome in conf:
            raise ErroDeChave(409, "key_exists", f"já existe uma chave de serviço chamada {nome}: rotacione ou revogue")
        if teto is not None:
            usadas = sum(1 for v in conf.values() if dono_de(v) == dono)
            if usadas >= teto:
                raise ErroDeChave(403, "forbidden", f"cota esgotada ({usadas}/{teto}); peça mais à administração")
        conf[nome] = {
            "sha256": auth.key_hash(chave),
            "scopes": list(escopos),
            "images": globs,
            "created_at": int(time.time()),
            "created_by": by,
        }
        if dono != ADMIN:
            conf[nome]["owner"] = dono
        if follow:
            conf[nome]["follow"] = follow
        fsdb.write_json(_dir() / "services.json", conf, mode=0o600)
    return {**_servico_publico(nome, conf[nome]), "key": chave}


def servico_alterar(nome: str, campos: dict, dono: str | None = None) -> dict:
    """`dono`: o sub-admin só mexe nas dele; None = qualquer uma (a
    administração). Glob e follow seguem as regras do dono DA CHAVE."""
    with _trava():
        conf = _servicos()
        v = _da_chave(conf, nome, dono)
        if "scopes" in campos:
            v["scopes"] = list(campos["scopes"] or [])
        if "images" in campos:
            v["images"] = _confere_globs(campos["images"] or [], dono_de(v))
        if "follow" in campos or "scopes" in campos:
            follow = _confere_follow(campos.get("follow", v.get("follow", "")), v["scopes"], dono_de(v))
            v.pop("follow", None)
            if follow:
                v["follow"] = follow
        fsdb.write_json(_dir() / "services.json", conf, mode=0o600)
    return _servico_publico(nome, conf[nome])


def servico_rotacionar(nome: str, dono: str | None = None) -> dict:
    """Chave nova, MESMO nome, escopos e globs: a antiga morre na hora."""
    chave = auth.new_key("nb3s")
    with _trava():
        conf = _servicos()
        v = _da_chave(conf, nome, dono)
        v["sha256"] = auth.key_hash(chave)
        v["rotated_at"] = int(time.time())
        fsdb.write_json(_dir() / "services.json", conf, mode=0o600)
    return {**_servico_publico(nome, conf[nome]), "key": chave}


def servico_apagar(nome: str, dono: str | None = None) -> bool:
    """False quando não há o que apagar, inclusive quando a chave é de outro
    dono: a rota responde o mesmo 204 nos dois casos."""
    with _trava():
        conf = _servicos()
        v = conf.get(nome)
        if v is None or (dono is not None and dono_de(v) != dono):
            return False
        conf.pop(nome)
        fsdb.write_json(_dir() / "services.json", conf, mode=0o600)
    keyusage.esquecer("service", nome)
    return True


def servico_apagar_do_dono(dono: str) -> list[str]:
    """Apagar o convite apaga as chaves do sub-admin. Sem o convite elas já
    não valem (`auth` confere o dono), mas ficariam na lista para sempre."""
    with _trava():
        conf = _servicos()
        nomes = [n for n, v in conf.items() if dono_de(v) == dono]
        for n in nomes:
            conf.pop(n)
        if nomes:
            fsdb.write_json(_dir() / "services.json", conf, mode=0o600)
    for n in nomes:
        keyusage.esquecer("service", n)
    return nomes
