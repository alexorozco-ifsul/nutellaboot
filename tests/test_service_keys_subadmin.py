"""Chaves de serviço criadas por sub-admin (o MOJ e o telão de um laboratório).

A regra que este arquivo segura: o glob de uma chave de sub-admin só vale
dentro das imagens do DONO, conferido a cada requisição. Nome de imagem é livre
por ordem de chegada, então conferir o glob na criação não segura a imagem que
outra pessoa cria depois com um nome que casa. E o que é de outro dono não
existe (404), como no console de quem criou a chave: um 403 diria que o nome
está tomado.
"""

import json
import time

import pytest

from server.app import fsdb
from server.app.services import invites, owners, ownership, ratelimit, webhook_guard

MD5 = "0" * 32
TODOS = [
    "machines:read", "commands:write", "alerts:write", "bindings:write", "webhooks:write",
    "roster:read", "roster:write", "config:write", "labs:read",
]


@pytest.fixture
def ha(admin_key):
    return {"Authorization": f"Bearer {admin_key}"}


@pytest.fixture(autouse=True)
def sem_limite():
    ratelimit.reset()
    yield
    ratelimit.reset()


def _hs(credencial):
    return {"Authorization": f"Bearer {credencial}"}


def _convite(client, ha, **kw):
    r = client.post("/api/v1/invites", json={"count": 1, "max_images": 5, **kw}, headers=ha)
    assert r.status_code == 201, r.text
    return r.json()["invites"][0]["code"]


def _imagem(client, headers, ident):
    r = client.post("/api/v1/site-images", json={"id": ident, "fullname": ident, "model": "oficial"}, headers=headers)
    assert r.status_code == 201, r.text


def _chave(client, headers, nome="moj", scopes=("machines:read",), images=(), **kw):
    corpo = {"name": nome, "scopes": list(scopes), "images": list(images), **kw}
    r = client.post("/api/v1/service-keys", json=corpo, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _ids(resposta):
    return [i["id"] for i in resposta.json()["images"]]


@pytest.fixture
def mundo(data_root, client, ha):
    """Dois sub-admins e a administração, com nomes que casam com o mesmo glob."""
    fsdb.write_json(data_root / "server.json", {"reserved_prefix_regex": "^[0-9]"})
    client.post("/api/v1/models", json={"name": "oficial", "public": True}, headers=ha)
    client.post("/api/v1/models/oficial/layers", json={"file": "base.squashfs", "md5": MD5}, headers=ha)
    a = _convite(client, ha, label="Lab A")
    b = _convite(client, ha, label="Lab B")
    _imagem(client, _hs(a), "laba1")
    _imagem(client, _hs(a), "laba2")
    _imagem(client, _hs(b), "labb1")
    _imagem(client, ha, "labadm")
    return {"a": a, "b": b}


# --- o nome ---


def test_o_nome_ganha_o_sufixo_do_dono_e_ninguem_colide(client, mundo, ha):
    """Nome de chave é único no servidor: sem o sufixo, o 409 de "moj" diria a
    um sub-admin que a administração tem uma "moj"."""
    a, b = mundo["a"], mundo["b"]
    ref_a = ownership.owner_ref(owners.owner_id(a))
    ref_b = ownership.owner_ref(owners.owner_id(b))
    assert _chave(client, ha)["name"] == "moj"
    assert _chave(client, _hs(a))["name"] == f"moj@{ref_a}"
    assert _chave(client, _hs(b))["name"] == f"moj@{ref_b}"
    r = client.post("/api/v1/service-keys", json={"name": "moj", "scopes": ["machines:read"]}, headers=_hs(a))
    assert r.status_code == 409 and r.json()["code"] == "key_exists"
    # o @ é só do sufixo: a administração não toma o nome de um sub-admin
    r = client.post("/api/v1/service-keys", json={"name": f"moj@{ref_a}", "scopes": ["machines:read"]}, headers=ha)
    assert r.status_code == 400


# --- o que a chave alcança ---


@pytest.mark.parametrize("globs", [[], ["*"], ["lab*"]])
def test_o_glob_so_alcanca_as_imagens_do_dono(client, mundo, globs):
    hk = _hs(_chave(client, _hs(mundo["a"]), images=globs)["key"])
    assert client.get("/api/v1/whoami", headers=hk).json()["images"] == ["laba1", "laba2"]
    assert _ids(client.get("/api/v1/site-images", headers=hk)) == ["laba1", "laba2"]


def test_imagem_criada_depois_por_outro_dono_fica_de_fora(client, mundo, ha):
    a, b = mundo["a"], mundo["b"]
    hk = _hs(_chave(client, _hs(a), images=["lab*"])["key"])
    _imagem(client, _hs(b), "labaz")
    _imagem(client, ha, "labax")
    assert client.get("/api/v1/whoami", headers=hk).json()["images"] == ["laba1", "laba2"]
    r = client.get("/api/v1/site-images/labaz", headers=hk)
    assert r.status_code == 404 and r.json()["code"] == "image_not_found"
    # apagada e recriada por outro dono com o mesmo nome: também fica de fora
    assert client.delete("/api/v1/site-images/laba2", headers=_hs(a)).status_code == 204
    _imagem(client, _hs(b), "laba2")
    assert client.get("/api/v1/whoami", headers=hk).json()["images"] == ["laba1"]
    assert client.get("/api/v1/site-images/laba2", headers=hk).status_code == 404


ROTAS = [
    ("GET", "/api/v1/site-images/{i}"),
    ("GET", "/api/v1/site-images/{i}/machines"),
    ("GET", "/api/v1/site-images/{i}/samples"),
    ("GET", "/api/v1/site-images/{i}/alerts"),
    ("GET", "/api/v1/site-images/{i}/roster"),
    ("PUT", "/api/v1/site-images/{i}/config"),
    ("GET", "/api/v1/site-images/{i}/commands"),
    ("POST", "/api/v1/site-images/{i}/commands"),
    ("POST", "/api/v1/site-images/{i}/lock"),
    ("GET", "/api/v1/site-images/{i}/webhooks"),
    ("GET", "/api/v1/site-images/{i}/report?format=json"),
    ("GET", "/api/v1/site-images/{i}/roster/logos/unb"),
]


@pytest.mark.parametrize("alvo", ["labb1", "labadm", "naoexiste"])
def test_toda_rota_de_imagem_alheia_responde_404(client, mundo, alvo):
    """A imagem de outro dono responde como a que não existe."""
    chave = _chave(client, _hs(mundo["a"]), scopes=TODOS)["key"]
    for metodo, rota in ROTAS:
        corpo = {"command": "reboot"} if rota.endswith("/commands") else {}
        r = client.request(metodo, rota.format(i=alvo), headers=_hs(chave), json=corpo)
        assert r.status_code == 404, (metodo, rota, r.status_code, r.text)
        assert r.json()["code"] == "image_not_found", (metodo, rota)
    # o fluxo SSE só lê ?tk=
    with client.stream("GET", f"/api/v1/site-images/{alvo}/events?tk={chave}") as r:
        assert r.status_code == 404


def test_imagem_do_dono_fora_do_glob_da_403(client, mundo):
    """Na imagem dele o 403 é o erro útil, e não entrega nada de ninguém."""
    hk = _hs(_chave(client, _hs(mundo["a"]), images=["laba1"])["key"])
    assert client.get("/api/v1/site-images/laba1", headers=hk).status_code == 200
    r = client.get("/api/v1/site-images/laba2", headers=hk)
    assert r.status_code == 403 and r.json()["code"] == "image_out_of_scope"


def test_o_cadeado_do_modelo_vale_para_a_chave(client, mundo, ha):
    """A chave não pode mais que o dono: na imagem Oficial, o firewall travado
    no modelo continua travado."""
    c = _convite(client, ha, unlocked=False)
    _imagem(client, _hs(c), "labofc")
    hk = _hs(_chave(client, _hs(c), scopes=["commands:write"])["key"])
    assert "disablefirewall" in client.get("/api/v1/site-images/labofc/commands", headers=hk).json()["blocked"]
    r = client.post("/api/v1/site-images/labofc/commands", json={"command": "disablefirewall"}, headers=hk)
    assert r.status_code == 403 and r.json()["code"] == "command_blocked"


def test_a_chave_da_administracao_nao_muda(client, mundo, ha):
    hk = _hs(_chave(client, ha, images=[])["key"])
    assert client.get("/api/v1/whoami", headers=hk).json()["images"] == ["laba1", "laba2", "labadm", "labb1"]
    assert client.get("/api/v1/site-images/naoexiste", headers=hk).status_code == 404
    hk = _hs(_chave(client, ha, nome="so-a", images=["laba*"])["key"])
    r = client.get("/api/v1/site-images/labb1", headers=hk)
    assert r.status_code == 403 and r.json()["code"] == "image_out_of_scope"


# --- a vida da chave ---


def test_a_chave_vale_enquanto_o_console_do_dono_vale(client, mundo, ha):
    a = mundo["a"]
    hk = _hs(_chave(client, _hs(a))["key"])
    oid = owners.owner_id(a)

    def status():
        return client.get("/api/v1/whoami", headers=hk).status_code

    assert status() == 200
    client.post(f"/api/v1/owners/{oid}/disable", json={}, headers=ha)
    assert status() == 401
    client.post(f"/api/v1/owners/{oid}/disable", json={"disabled": False}, headers=ha)
    assert status() == 200
    client.patch(f"/api/v1/invites/{a}", json={"revoked": True}, headers=ha)
    assert status() == 401
    client.patch(f"/api/v1/invites/{a}", json={"revoked": False}, headers=ha)
    assert status() == 200
    client.patch(f"/api/v1/invites/{a}", json={"expires_at": time.time() - 60}, headers=ha)
    assert status() == 401
    client.patch(f"/api/v1/invites/{a}", json={"expires_at": None}, headers=ha)
    assert status() == 200


def test_a_chave_nao_grava_o_ultimo_acesso_do_dono(client, mundo):
    """O login do sub-admin grava `last_seen` em disco. O MOJ sonda o tempo
    todo, e a conferência da chave não pode escrever por requisição."""
    a = mundo["a"]
    hk = _hs(_chave(client, _hs(a))["key"])
    oid = owners.owner_id(a)
    antes = owners.get(oid)
    for _ in range(5):
        assert client.get("/api/v1/whoami", headers=hk).status_code == 200
    assert owners.get(oid) == antes


def test_apagar_o_convite_apaga_as_chaves(client, mundo, ha):
    # um convite que só tem a chave também pede o force
    c = _convite(client, ha)
    k = _chave(client, _hs(c))
    r = client.delete(f"/api/v1/invites/{c}", headers=ha)
    assert r.status_code == 409 and "chave" in r.json()["detail"]
    r = client.delete(f"/api/v1/invites/{c}?force=true", headers=ha)
    assert r.status_code == 200 and r.json()["orphaned"]["service_keys"] == [k["name"]]
    assert client.get("/api/v1/whoami", headers=_hs(k["key"])).status_code == 401
    nomes = {x["name"] for x in client.get("/api/v1/service-keys", headers=ha).json()["service_keys"]}
    assert k["name"] not in nomes


def test_cota_de_chaves(client, mundo, ha):
    c = _convite(client, ha, max_service_keys=1)
    _chave(client, _hs(c), nome="um")
    r = client.post("/api/v1/service-keys", json={"name": "dois", "scopes": ["machines:read"]}, headers=_hs(c))
    assert r.status_code == 403 and "cota" in r.json()["detail"]
    eu = client.get("/api/v1/whoami", headers=_hs(c)).json()
    assert eu["quotas"]["service_keys"] == 1 and eu["usage"]["service_keys"] == 1
    oid = owners.owner_id(c)
    r = client.patch(f"/api/v1/owners/{oid}/quotas", json={"max_service_keys": 2}, headers=ha)
    assert r.status_code == 200 and r.json()["quotas"]["service_keys"] == 2
    _chave(client, _hs(c), nome="dois")
    # o PATCH do convite grava o campo (o set_fields tem a lista do que aceita)
    assert client.patch(f"/api/v1/invites/{c}", json={"max_service_keys": 7}, headers=ha).status_code == 200
    assert invites.get(c)["max_service_keys"] == 7
    # e o padrão é 5
    d = _convite(client, ha)
    assert client.get("/api/v1/whoami", headers=_hs(d)).json()["quotas"]["service_keys"] == 5
    linha = next(o for o in client.get("/api/v1/owners", headers=ha).json()["owners"] if o["id"] == oid)
    assert linha["usage"]["service_keys"] == 2


def test_cada_um_so_mexe_nas_suas(client, mundo, ha):
    a, b = mundo["a"], mundo["b"]
    ka = _chave(client, _hs(a))
    kb = _chave(client, _hs(b))

    def nomes(h):
        return {x["name"] for x in client.get("/api/v1/service-keys", headers=h).json()["service_keys"]}

    assert nomes(_hs(a)) == {ka["name"]}
    assert nomes(ha) == {ka["name"], kb["name"]}
    for metodo, rota, corpo in [
        ("PATCH", f"/api/v1/service-keys/{ka['name']}", {"images": ["*"]}),
        ("POST", f"/api/v1/service-keys/{ka['name']}/rotate", None),
    ]:
        r = client.request(metodo, rota, headers=_hs(b), json=corpo)
        assert r.status_code == 404 and r.json()["code"] == "key_not_found", (metodo, rota)
    # o DELETE é idempotente: o de outro dono é o mesmo 204 de quem não existe,
    # e nada some
    assert client.delete(f"/api/v1/service-keys/{ka['name']}", headers=_hs(b)).status_code == 204
    assert client.get("/api/v1/whoami", headers=_hs(ka["key"])).status_code == 200
    # a administração vê de quem é e revoga
    linha = next(x for x in client.get("/api/v1/service-keys", headers=ha).json()["service_keys"] if x["name"] == ka["name"])
    assert linha["owner_kind"] == "subadmin" and linha["owner_label"] == "Lab A"
    assert client.delete(f"/api/v1/service-keys/{ka['name']}", headers=ha).status_code == 204
    assert client.get("/api/v1/whoami", headers=_hs(ka["key"])).status_code == 401
    # as chaves de administração e a auditoria continuam só da administração
    assert client.get("/api/v1/admin-keys", headers=_hs(a)).status_code == 401
    assert client.get("/api/v1/audit", headers=_hs(a)).status_code == 401


def test_o_codigo_do_convite_nao_sai_em_resposta_nenhuma(client, mundo, ha):
    """O dono de uma chave de sub-admin é `invite:<CÓDIGO>`, e o código é a
    credencial do console dele (invariante 18). A chave vai para outro
    sistema, e o link do telão vai para a parede."""
    a = mundo["a"]
    k = _chave(client, _hs(a), scopes=["machines:read", "labs:read"], follow="owner")
    assert a not in json.dumps(k)
    assert k["created_by"] == ownership.owner_ref(owners.owner_id(a))
    hk = _hs(k["key"])
    for r in (
        client.get("/api/v1/service-keys", headers=ha),
        client.get("/api/v1/service-keys", headers=_hs(a)),
        client.get("/api/v1/whoami", headers=hk),
        client.get("/api/v1/site-images", headers=hk),
        client.get("/api/v1/site-images/laba1", headers=hk),
        client.get(f"/api/v1/labs?tk={k['key']}"),
    ):
        assert r.status_code == 200, r.text
        assert a not in r.text, r.request.url


def test_o_telao_do_subadmin_segue_a_visao_dele(client, mundo, ha):
    a = mundo["a"]
    assert client.put("/api/v1/labs/view", json={"mode": "custom", "images": ["laba2"]}, headers=_hs(a)).status_code == 200
    # a escolha da administração não vaza para o telão de A
    client.put("/api/v1/labs/view", json={"mode": "custom", "images": ["laba1", "labb1"]}, headers=ha)
    k = _chave(client, _hs(a), nome="telao", scopes=["labs:read"], follow="owner")
    assert [s["id"] for s in client.get(f"/api/v1/labs?tk={k['key']}").json()["sites"]] == ["laba2"]
    r = client.post(
        "/api/v1/service-keys", json={"name": "t2", "scopes": ["labs:read"], "follow": "admin"}, headers=_hs(a)
    )
    assert r.status_code == 400
    # a da administração segue a dela, pelos dois nomes
    for follow in ("admin", "owner"):
        kk = _chave(client, ha, nome=f"telao-{follow}", scopes=["labs:read"], follow=follow)
        assert [s["id"] for s in client.get(f"/api/v1/labs?tk={kk['key']}").json()["sites"]] == ["laba1", "labb1"]


def test_os_webhooks_da_chave_do_subadmin(client, mundo, ha, monkeypatch):
    monkeypatch.setattr(webhook_guard, "_resolver", lambda host: ["93.184.216.34"])
    a = mundo["a"]
    segredo = "s" * 32
    # o webhook da administração na imagem de A não aparece para a chave de A
    r = client.post("/api/v1/site-images/laba1/webhooks", json={"url": "https://adm.example/h", "secret": segredo}, headers=ha)
    assert r.status_code == 201, r.text
    hk = _hs(_chave(client, _hs(a), scopes=["webhooks:write"])["key"])
    assert client.get("/api/v1/site-images/laba1/webhooks", headers=hk).json()["webhooks"] == []
    r = client.post("/api/v1/site-images/laba1/webhooks", json={"url": "https://moj.example/h", "secret": segredo}, headers=hk)
    assert r.status_code == 201, r.text
    urls = [w["url"] for w in client.get("/api/v1/site-images/laba1/webhooks", headers=hk).json()["webhooks"]]
    assert urls == ["https://moj.example/h"]
    r = client.post("/api/v1/site-images/labb1/webhooks", json={"url": "https://moj.example/h", "secret": segredo}, headers=hk)
    assert r.status_code == 404


def test_os_globs_da_chave_de_subadmin_tem_limite(client, mundo):
    a = mundo["a"]

    def cria(nome, images):
        corpo = {"name": nome, "scopes": ["machines:read"], "images": images}
        return client.post("/api/v1/service-keys", json=corpo, headers=_hs(a)).status_code

    assert cria("x", [f"g{i}*" for i in range(21)]) == 400
    assert cria("x", ["a" * 65]) == 400
    assert cria("x", "lab*") == 400  # uma string seria lida letra a letra
    assert cria("x", [f"g{i}*" for i in range(20)]) == 200


def test_a_lista_diz_quantas_imagens_cada_chave_alcanca(client, mundo, ha):
    a = mundo["a"]
    _chave(client, _hs(a), nome="tudo", images=[])
    _chave(client, _hs(a), nome="uma", images=["laba1"])
    _chave(client, ha, nome="adm", images=["lab*"])
    lista = client.get("/api/v1/service-keys", headers=ha).json()["service_keys"]
    assert {x["name"].split("@")[0]: x["covers"] for x in lista} == {"tudo": 2, "uma": 1, "adm": 4}
