"""O pendrive de instalação: o que ele faz com o disco da máquina.

`client/stuff/27-install.sh` apaga discos. Estes testes exercitam o caminho de
verdade sobre discos de mentira (arquivos), com o `sfdisk`, o `mke2fs` e o `dd`
de verdade. Tudo que tocaria um dispositivo do host vira comando falso que só
anota: `losetup`, `mount`, `efibootmgr`, `blkid`, `mkswap`, `blockdev`.

O que eles protegem, além de "funciona":
  * sem `nb3install=` nada acontece;
  * nunca o disco USB, o removível, nem o de onde o boot veio (no rig de VM o
    pendrive é um disco IDE comum: só o nome o exclui);
  * nada toca o disco antes de a imagem ser baixada e conferida;
  * empate entre discos não se decide: a tela lista e nada é apagado;
  * o disco gravado não fica com a identidade do pendrive (assinatura do MBR
    e número de série da FAT, nas duas cópias do setor de boot);
  * ao lado do Windows, nada do Windows muda, e a desinstalação só leva o que
    é nosso.
"""

import hashlib
import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from server.app import fsdb
from server.app.services import usb

# as fixtures do servidor e do pendrive são as do test_usb (só as fixtures:
# importar um `test_` faria o pytest rodá-lo de novo aqui)
from tests.test_usb import _gera_pendrive, build, client, genusb, ha, sede, sem_limite  # noqa: F401

REPO = Path(__file__).resolve().parents[1]
STUFF = REPO / "client" / "stuff"
GENUSB = REPO / "tools" / "nb3-genusb"
MiB = 2048  # setores

HARNESS = """
export PATH="$FAKEBIN:$BASEPATH"
log_begin_msg() { :; }
log_end_msg() { :; }
log_warning_msg() { printf 'WARN %s\\n' "$*"; }
log_failure_msg() { printf 'FAIL %s\\n' "$*"; }
# funções, não binários: `reboot -f` de verdade não volta
reboot() { echo REBOOT >> "$ACOES"; exit 0; }
panic() { echo "PANIC $*" >> "$ACOES"; exit 0; }
. "$STUFF/00-header.sh"
. "$STUFF/05-ui.sh"
. "$STUFF/20-download.sh"
. "$STUFF/25-usbupdate.sh"
. "$STUFF/27-install.sh"
. "$STUFF/30-storage.sh"
. "$STUFF/40-mount.sh"
IMAGEROOT=sala1
NB_SERVER=https://s
NB_BOOT_KEY=nb3b_abc
"""

# `mount DISPOSITIVO PONTO` vira um link do ponto para o conteúdo daquele
# dispositivo: é assim que o teste diz o que existe em cada partição.
MOUNT = """echo "mount $*" >> "$ACOES"
for a; do penultimo=$ultimo; ultimo=$a; done
alvo="$CONTEUDO/${penultimo##*/}"
[ -d "$alvo" ] || exit 32
rm -rf "$ultimo"
ln -s "$alvo" "$ultimo"
"""
UMOUNT = """echo "umount $*" >> "$ACOES"
[ -L "$1" ] && { rm -f "$1"; mkdir -p "$1"; }
exit 0
"""
# sem argumento lista as entradas (o arquivo do teste); o resto só anota
EFIBOOTMGR = """echo "efibootmgr $*" >> "$ACOES"
[ $# -eq 0 ] && cat "$EFILISTA" 2>/dev/null
exit 0
"""
# `blkid ... -s CHAVE DISPOSITIVO` lê o arquivo <dispositivo>.<CHAVE>
BLKID = """chave=; anterior=
for a; do [ "$anterior" = -s ] && chave=$a; anterior=$a; dev=$a; done
cat "$BLKIDDIR/${dev##*/}.$chave" 2>/dev/null || exit 2
"""
# nb_get: uma resposta por chamada (1, 2, ...), e depois a última
WGET = """n=$(cat "$RESPOSTAS/n" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "$RESPOSTAS/n"
if [ -f "$RESPOSTAS/$n" ]; then cat "$RESPOSTAS/$n"; else cat "$RESPOSTAS/ultima" 2>/dev/null || exit 8; fi
"""
ARIA2C = """dir=. ; base=saida ; url=
while [ $# -gt 0 ]; do
    case $1 in
        -d) dir=$2; shift 2 ;;
        -o) base=$2; shift 2 ;;
        http*) url=$1; shift ;;
        *) shift ;;
    esac
done
nome=${url##*/}
[ -f "$SERVIDOR/$nome" ] || exit 3
cp "$SERVIDOR/$nome" "$dir/$base"
"""


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def imagem_da_sede(destino: Path, mb: int = 64) -> Path:
    """A imagem do pendrive como o nb3-genusb a faz: MBR, p1 FAT32 em 1 MiB."""
    for t in ("sfdisk", "mformat", "truncate"):
        if not shutil.which(t):
            pytest.skip(f"sem {t}")
    subprocess.run(["truncate", "-s", f"{mb}M", str(destino)], check=True)
    subprocess.run(
        ["sfdisk", "--quiet", str(destino)],
        input="label: dos\nunit: sectors\nstart=2048, type=ef, bootable\n",
        text=True,
        check=True,
    )
    subprocess.run(["mformat", "-i", f"{destino}@@1M", "-F", "-v", "NB3CFG", "::"], check=True)
    return destino


class Maquina:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.sys = tmp / "sys"
        self.dev = tmp / "dev"
        self.conteudo = tmp / "conteudo"
        self.blkid = tmp / "blkid"
        self.efi = tmp / "efi"
        self.servidor = tmp / "servidor"
        self.respostas = tmp / "respostas"
        self.run = tmp / "run"
        self.inst = tmp / "inst"
        self.acoes = tmp / "acoes"
        self.efilista = tmp / "efi-lista"
        # o console de onde vem a resposta da pergunta
        self.teclado = tmp / "teclado"
        self.teclado.write_text("yes\n")
        for d in (self.bin, self.sys / "block", self.sys / "class", self.dev, self.conteudo,
                  self.blkid, self.efi / "efivars", self.servidor, self.respostas, self.run):
            d.mkdir(parents=True)
        (tmp / "mounts").write_text("")
        for nome, corpo in (
            ("losetup", 'echo "losetup $*" >> "$ACOES"\ncase "$1" in -d) exit 0 ;; esac\necho /dev/loop7\n'),
            ("mkswap", 'echo "mkswap $*" >> "$ACOES"\n'),
            ("blockdev", 'echo "blockdev $*" >> "$ACOES"\n'),
            ("modprobe", "exit 0\n"),
            ("mount", MOUNT),
            ("umount", UMOUNT),
            ("efibootmgr", EFIBOOTMGR),
            ("blkid", BLKID),
            ("mke2fs.good", 'exec mke2fs "$@"\n'),
            ("wget.good", WGET),
            ("aria2c", ARIA2C),
        ):
            self.stub(nome, corpo)

    def stub(self, nome: str, corpo: str) -> None:
        p = self.bin / nome
        p.write_text("#!/bin/sh\n" + corpo)
        p.chmod(0o755)

    def disco(self, nome, setores, *, ssd=True, usb=False, removable="0",
              modelo="Disco de Teste", partes=(), arquivo=False):
        """Um disco no sysfs de mentira. `partes`: (nome, setores, {chave: valor
        do blkid}). `arquivo=True` cria o nó como arquivo esparso do tamanho do
        disco, para o instalador gravar de verdade."""
        bus = "pci0000:00/0000:00:14.0/usb2/2-1/host6" if usb else "pci0000:00/0000:00:17.0/ata1/host0"
        real = self.tmp / "devices" / bus / "block" / nome
        (real / "queue").mkdir(parents=True)
        (real / "device").mkdir()
        (real / "size").write_text(f"{setores}\n")
        (real / "removable").write_text(removable + "\n")
        (real / "queue" / "rotational").write_text("0\n" if ssd else "1\n")
        (real / "device" / "model").write_text(modelo.ljust(24) + "\n")
        (self.sys / "block" / nome).symlink_to(real)
        (self.sys / "class" / nome).symlink_to(real)
        no = self.dev / nome
        if arquivo:
            subprocess.run(["truncate", "-s", str(setores * 512), str(no)], check=True)
        else:
            no.write_text("")
        for i, (pnome, psetores, blk) in enumerate(partes, 1):
            p = real / pnome
            p.mkdir()
            (p / "partition").write_text(f"{i}\n")
            (p / "size").write_text(f"{psetores}\n")
            (self.sys / "class" / pnome).symlink_to(p)
            (self.dev / pnome).write_text("")
            for chave, valor in blk.items():
                (self.blkid / f"{pnome}.{chave}").write_text(valor + "\n")
        return no

    def servir(self, imagem: Path) -> None:
        """O servidor tem a imagem e responde IMAGE para o instalador."""
        shutil.copy(imagem, self.servidor / imagem.name)
        (self.respostas / "ultima").write_text(
            f"IMAGE {md5(imagem)} {imagem.stat().st_size} {imagem.name} "
            f"https://s/boot/v3/sala1/installfile/{imagem.name}\n"
        )

    def env(self, **env) -> dict:
        return {
            "STUFF": str(STUFF),
            "FAKEBIN": str(self.bin),
            "BASEPATH": "/usr/bin:/bin",
            "ACOES": str(self.acoes),
            "CONTEUDO": str(self.conteudo),
            "EFILISTA": str(self.efilista),
            "BLKIDDIR": str(self.blkid),
            "RESPOSTAS": str(self.respostas),
            "SERVIDOR": str(self.servidor),
            "NB_UI_PLAIN": "1",
            "NB_FATAL_WAIT": "0",
            "NB_DOWNLOAD_TRIES": "1",
            "NB_INST_SYS_BLOCK": str(self.sys / "block"),
            "NB_SYS_CLASS_BLOCK": str(self.sys / "class"),
            "NB_INST_DEV": str(self.dev),
            "NB_INST_UUIDS": str(self.tmp / "by-uuid"),
            "NB_INST_EFI": str(self.efi),
            "NB_INST_MOUNTS": str(self.tmp / "mounts"),
            "NB_INST_TMP": str(self.inst),
            "NB_INST_ESPMNT": str(self.tmp / "espmnt"),
            "NB_INST_IMGMNT": str(self.tmp / "imgmnt"),
            "NB_INST_TTY": str(self.teclado),
            "NB_INST_FIM_WAIT": "0",
            "NB_INST_POLL": "0",
            "NB_RUN": str(self.run),
            **{k: str(v) for k, v in env.items()},
        }

    def roda(self, script: str, **env) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", "-c", HARNESS + "\n" + script],
            capture_output=True,
            text=True,
            env=self.env(**env),
            cwd=str(self.tmp),
            timeout=120,
        )

    def acoes_texto(self) -> str:
        return self.acoes.read_text() if self.acoes.exists() else ""


@pytest.fixture
def maq(tmp_path):
    return Maquina(tmp_path)


def _pendrive_usb(maq, nome="sdb"):
    """O pendrive de onde o boot veio, como o initrd o deixa (NB_CFG_DEV)."""
    maq.disco(nome, 16 * 1024 * MiB, usb=True, removable="1", partes=[(f"{nome}1", 800 * MiB, {})])
    return f"{maq.dev}/{nome}1"


# --- sem a flag, nada ---


def test_sem_a_flag_nao_faz_nada(maq):
    """O stuff roda em todo boot de toda máquina: o instalador tem de ser
    provadamente inerte sem `nb3install=`."""
    maq.disco("sda", 500 * 1024 * MiB, partes=[("sda1", 400 * 1024 * MiB, {"TYPE": "ntfs"})])
    r = maq.roda('nb_install; echo "SAIU=$?"')
    assert "SAIU=0" in r.stdout, r.stdout + r.stderr
    assert maq.acoes_texto() == ""
    assert not (maq.respostas / "n").exists(), "perguntou ao servidor sem a flag"


def test_valor_desconhecido_segue_o_boot(maq):
    r = maq.roda('nb3install=tudo; nb_install; echo "SAIU=$?"')
    assert "SAIU=0" in r.stdout
    assert "unknown nb3install=tudo" in r.stdout
    assert maq.acoes_texto() == ""


def test_o_stuff_chama_o_instalador_antes_de_gravar_no_disco():
    """Depois das checagens de VM e de RAM, antes da autoatualização (que
    regravaria o pendrive de instalação e reiniciaria à toa) e antes das
    camadas (que têm de cair na partição nova)."""
    main = (STUFF / "90-main.sh").read_text()
    assert main.index("runpremountconfigs\n") < main.index("nb_install\n")
    assert main.index("nb_install\n") < main.index("nb_usb_update")
    assert main.index("nb_install\n") < main.index("mount_layers")


# --- antes de tudo ---


def test_initrd_sem_sfdisk_para_antes_de_tudo(maq, tmp_path):
    """Pendrive de instalação gerado com um initrd antigo: parar com a causa,
    sem perguntar nada ao servidor nem tocar em disco."""
    magro = tmp_path / "magro"
    magro.mkdir()
    for nome in ("sh", "sed", "awk", "grep", "cat", "tr", "readlink", "sleep", "printf",
                 "mkdir", "rm", "cp", "mv", "ln", "wc", "basename", "dirname", "stat"):
        alvo = shutil.which(nome)
        if alvo:
            (magro / nome).symlink_to(alvo)
    disco = maq.disco("sda", 500 * 1024 * MiB, arquivo=False)
    r = maq.roda(
        f'NB_CFG_DEV="{_pendrive_usb(maq)}"; nb3install=disco; nb_install; echo NAO',
        BASEPATH=str(magro),
    )
    assert "TOO OLD" in r.stdout and "sfdisk" in r.stdout, r.stdout
    assert "REBOOT" in maq.acoes_texto()
    assert not (maq.respostas / "n").exists()
    assert disco.read_text() == ""


def test_sem_saber_de_onde_bootou_nao_instala(maq):
    """Initrd anterior ao instalador não diz de que disco veio: o próprio
    pendrive poderia ser escolhido como alvo."""
    r = maq.roda("nb3install=disco; nb_install; echo NAO")
    assert "TOO OLD" in r.stdout and "boot-device" in r.stdout, r.stdout
    assert "NAO" not in r.stdout


def test_ao_lado_no_bios_para(maq):
    """Ao lado do Windows em BIOS seria reescrever o MBR dele."""
    r = maq.roda(
        f'NB_CFG_DEV="{_pendrive_usb(maq)}"; nb3install=lado; nb_install; echo NAO',
        NB_INST_EFI=maq.tmp / "sem-efi",
    )
    assert "NO UEFI" in r.stdout, r.stdout
    assert "NAO" not in r.stdout


# --- qual disco ---


def _escolhe(maq, **env):
    r = maq.roda(
        'nb_inst_discos 1000 > lista; cat lista; '
        'if d=$(nb_inst_escolhe lista); then echo "ESCOLHIDO=$d"; else echo "ESCOLHIDO=nenhum"; fi',
        **env,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_prefere_o_ssd(maq):
    maq.disco("sda", 1000 * 1024 * MiB, ssd=False)
    maq.disco("nvme0n1", 256 * 1024 * MiB, ssd=True)
    assert "ESCOLHIDO=nvme0n1" in _escolhe(maq)


def test_dois_ssds_nao_se_decide(maq):
    maq.disco("sda", 256 * 1024 * MiB)
    maq.disco("nvme0n1", 256 * 1024 * MiB)
    assert "ESCOLHIDO=nenhum" in _escolhe(maq)


def test_um_hd_so_serve(maq):
    maq.disco("sda", 500 * 1024 * MiB, ssd=False)
    assert "ESCOLHIDO=sda" in _escolhe(maq)


def test_nunca_usb_removivel_nem_o_disco_do_boot(maq):
    """No rig de VM o pendrive é um disco IDE comum (sem nada de USB): só o
    nome do disco de onde o conf veio o tira da lista."""
    maq.disco("sda", 500 * 1024 * MiB, ssd=False)
    maq.disco("sdb", 256 * 1024 * MiB, usb=True)
    maq.disco("sdc", 32 * 1024 * MiB, removable="1")
    maq.disco("sdd", 256 * 1024 * MiB, partes=[("sdd1", 800 * MiB, {})])
    saida = _escolhe(maq, NB_CFG_DEV=maq.dev / "sdd1")
    assert "ESCOLHIDO=sda" in saida, saida
    assert "sdb usb " in saida and "sdc removable " in saida and "sdd installer " in saida


def test_o_uuid_do_grub_tambem_aponta_o_disco_do_boot(maq):
    maq.disco("sda", 256 * 1024 * MiB, partes=[("sda1", 800 * MiB, {})])
    maq.disco("sdb", 256 * 1024 * MiB, ssd=False)
    (maq.tmp / "by-uuid").mkdir()
    (maq.tmp / "by-uuid" / "60F8-3978").symlink_to(maq.dev / "sda1")
    saida = maq.roda(
        'nb3cfg=60f8-3978; nb_inst_discos 1000 > lista; cat lista; nb_inst_escolhe lista'
    ).stdout
    assert "sda installer " in saida
    assert saida.strip().endswith("sdb")


def test_disco_pequeno_fica_de_fora(maq):
    maq.disco("sda", 500, ssd=True)
    saida = _escolhe(maq)
    assert "sda small " in saida and "ESCOLHIDO=nenhum" in saida


def test_nb3disk_vence_mas_so_entre_os_que_podem(maq):
    maq.disco("sda", 500 * 1024 * MiB, ssd=False)
    maq.disco("nvme0n1", 256 * 1024 * MiB)
    maq.disco("sdb", 64 * 1024 * MiB, usb=True)
    assert "ESCOLHIDO=sda" in _escolhe(maq, nb3disk="sda")
    assert "ESCOLHIDO=sda" in _escolhe(maq, nb3disk="/dev/sda")
    assert "ESCOLHIDO=nenhum" in _escolhe(maq, nb3disk="sdb")
    assert "ESCOLHIDO=nenhum" in _escolhe(maq, nb3disk="sdz")


# --- gravar o disco inteiro ---


def _particoes(disco: Path) -> list[dict]:
    r = subprocess.run(["sfdisk", "-J", str(disco)], capture_output=True, text=True, check=True)
    import json

    return json.loads(r.stdout)["partitiontable"]["partitions"]


def test_grava_o_disco_inteiro(maq, tmp_path):
    """A imagem no começo, a ext4 e o swap depois dela, e a identidade nova.
    Tudo no arquivo de verdade, com o sfdisk e o mke2fs de verdade."""
    img = imagem_da_sede(tmp_path / "sala1-abc.img")
    serial_img = img.read_bytes()[MiB * 512 + 67:MiB * 512 + 71]
    total = 512 * MiB
    disco = maq.disco("sda", total, arquivo=True)
    # o fim do disco com o GPT reserva de um Windows que estava lá
    with open(disco, "r+b") as f:
        f.seek((total - 34) * 512)
        f.write(b"\xff" * 34 * 512)
    r = maq.roda(f'nb_inst_grava "{disco}" "{img}" 2048 {16 * MiB}; echo "SAIU=$?"')
    assert "SAIU=0" in r.stdout, r.stdout + r.stderr

    p = _particoes(disco)
    assert [x["type"] for x in p] == ["ef", "83", "82"], p
    assert p[0]["start"] == 2048 and p[0].get("bootable")
    assert p[1]["start"] == 64 * MiB
    assert p[2]["start"] + p[2]["size"] == total and p[2]["size"] == 16 * MiB
    assert p[1]["start"] + p[1]["size"] == p[2]["start"]

    dados = disco.read_bytes()
    # a ext4 no lugar da p2, com o rótulo que nada mais usa
    sb = p[1]["start"] * 512 + 1024
    assert struct.unpack("<H", dados[sb + 56:sb + 58])[0] == 0xEF53
    assert dados[sb + 120:sb + 136].rstrip(b"\0") == b"NB3DATA"
    # a identidade do pendrive não vai junto
    assert dados[440:444] != b"\0\0\0\0"
    serial = dados[MiB * 512 + 67:MiB * 512 + 71]
    assert serial != serial_img
    assert dados[(MiB + 6) * 512 + 67:(MiB + 6) * 512 + 71] == serial, "a cópia do setor de boot ficou diferente"
    # o GPT reserva sumiu
    assert dados[(total - 34) * 512:] == b"\0" * 34 * 512
    # o swap por um loop sobre o pedaço certo, com o nosso rótulo
    acoes = maq.acoes_texto()
    assert f"losetup -o {p[2]['start'] * 512} --sizelimit {16 * MiB * 512} -f --show {disco}" in acoes
    assert "mkswap -L NB3SWAP /dev/loop7" in acoes


def test_sem_swap_a_ext4_vai_ate_o_fim(maq, tmp_path):
    img = imagem_da_sede(tmp_path / "sala1-abc.img")
    disco = maq.disco("sda", 256 * MiB, arquivo=True)
    r = maq.roda(f'nb_inst_grava "{disco}" "{img}" 2048 0; echo "SAIU=$?"')
    assert "SAIU=0" in r.stdout, r.stdout + r.stderr
    p = _particoes(disco)
    assert [x["type"] for x in p] == ["ef", "83"]
    assert p[1]["start"] + p[1]["size"] == 256 * MiB
    assert "mkswap" not in maq.acoes_texto()


def _cena_disco(maq, tmp_path):
    """Um SSD com Windows, o pendrive USB de onde o boot veio e o servidor com a
    imagem. Devolve o script do boot e o disco."""
    img = imagem_da_sede(tmp_path / "sala1-abc.img")
    maq.servir(img)
    disco = maq.disco("sda", 256 * MiB, arquivo=True, modelo="Samsung SSD 870",
                      partes=[("sda1", 200 * MiB, {"TYPE": "ntfs", "LABEL": "Windows"})])
    pendrive = _pendrive_usb(maq)
    maq.efilista.write_text(
        "BootCurrent: 0001\nBootOrder: 0003,0001\n"
        "Boot0001* Windows Boot Manager\tHD(1,GPT,...)/\\EFI\\Microsoft\\Boot\\bootmgfw.efi\n"
        "Boot0003* Maratona Linux\tHD(1,MBR,0x1,0x800,0x1f800)/\\EFI\\BOOT\\BOOTX64.EFI\n"
    )
    script = f'NB_CFG_DEV="{pendrive}"; nb3install=disco; nb3swap=0; nb_install; echo NAO-VOLTA'
    return script, disco


def _instala_disco(maq, tmp_path, **env):
    script, disco = _cena_disco(maq, tmp_path)
    r = maq.roda(script, NB_MIN_FREE_KB="1024", NB_INST_MARGEM="0", **env)
    return r, disco


def test_instala_no_disco_inteiro_de_ponta_a_ponta(maq, tmp_path):
    r, disco = _instala_disco(maq, tmp_path)
    assert "NAO-VOLTA" not in r.stdout, "terminou sem reiniciar"
    assert "=== ERASE? ===" in r.stdout and "Samsung SSD 870" in r.stdout and "Windows" in r.stdout
    assert "=== DONE ===" in r.stdout, r.stdout + r.stderr
    assert [x["type"] for x in _particoes(disco)] == ["ef", "83"]
    acoes = maq.acoes_texto()
    # a entrada antiga com o nosso rótulo sai, a do Windows fica, e a nova
    # aponta o caminho reserva do UEFI na p1
    assert "efibootmgr -q -b 0003 -B" in acoes
    assert "-b 0001" not in acoes
    assert f"efibootmgr -q -c -d {disco} -p 1 -L Maratona Linux -l \\EFI\\BOOT\\BOOTX64.EFI" in acoes
    assert acoes.rstrip().endswith("REBOOT")


def test_no_bios_nao_mexe_no_firmware(maq, tmp_path):
    r, disco = _instala_disco(maq, tmp_path, NB_INST_EFI=tmp_path / "sem-efi")
    assert "=== DONE ===" in r.stdout, r.stdout + r.stderr
    assert "efibootmgr" not in maq.acoes_texto()
    assert [x["type"] for x in _particoes(disco)] == ["ef", "83"]


def test_espera_o_servidor_gerar_a_imagem(maq, tmp_path):
    (maq.respostas / "1").write_text("WAIT generating the USB image of this site\n")
    (maq.respostas / "2").write_text("WAIT generating the USB image of this site\n")
    r, disco = _instala_disco(maq, tmp_path)
    assert "=== DONE ===" in r.stdout, r.stdout
    assert (maq.respostas / "n").read_text().strip() == "3"


def test_geracao_que_falhou_para_sem_tocar_no_disco(maq, tmp_path):
    (maq.respostas / "1").write_text("FAIL the USB image of this site could not be generated: grub\n")
    r, disco = _instala_disco(maq, tmp_path)
    assert "=== FAILED ===" in r.stdout and "could not be generated" in r.stdout
    assert "Nothing was changed on the disk" in r.stdout
    assert disco.read_bytes() == b"\0" * disco.stat().st_size
    assert "REBOOT" in maq.acoes_texto()


def test_md5_errado_nao_toca_no_disco(maq, tmp_path):
    img = imagem_da_sede(tmp_path / "sala1-abc.img")
    maq.servir(img)
    linha = (maq.respostas / "ultima").read_text().split()
    linha[1] = "0" * 32
    (maq.respostas / "ultima").write_text(" ".join(linha) + "\n")
    disco = maq.disco("sda", 256 * MiB, arquivo=True)
    r = maq.roda(
        f'NB_CFG_DEV="{_pendrive_usb(maq)}"; nb3install=disco; nb3swap=0; nb_install',
        NB_MIN_FREE_KB="1024",
        NB_INST_MARGEM="0",
    )
    assert "MD5 does not match" in r.stdout, r.stdout
    assert disco.read_bytes() == b"\0" * disco.stat().st_size


def test_empate_mostra_os_discos_e_nao_apaga(maq, tmp_path):
    img = imagem_da_sede(tmp_path / "sala1-abc.img")
    maq.servir(img)
    a = maq.disco("sda", 256 * MiB, arquivo=True)
    b = maq.disco("nvme0n1", 256 * MiB, arquivo=True)
    r = maq.roda(
        f'NB_CFG_DEV="{_pendrive_usb(maq)}"; nb3install=disco; nb3swap=0; nb_install',
        NB_MIN_FREE_KB="1024",
        NB_INST_MARGEM="0",
    )
    assert "=== DISK? ===" in r.stdout and "nb3disk=" in r.stdout, r.stdout
    for d in (a, b):
        assert d.read_bytes() == b"\0" * d.stat().st_size


# --- a pergunta ---


def _zerado(disco: Path) -> bool:
    return disco.read_bytes() == b"\0" * disco.stat().st_size


def _pty():
    import pty

    mestre, escravo = pty.openpty()
    return mestre, escravo, os.ttyname(escravo)


def test_a_tela_diz_o_que_vai_ser_feito_e_onde(maq, tmp_path):
    """A pergunta mostra o disco, o que há nele e o que fica no lugar, com a
    mesma conta que a gravação usa."""
    maq.teclado.write_text("no\n")
    r, disco = _instala_disco(maq, tmp_path)
    tela = r.stdout
    assert "=== ERASE? ===" in tela, tela + r.stderr
    assert "/dev/sda   SSD   256 MB   Samsung SSD 870" in tela
    assert "sda1" in tela and "ntfs" in tela and "Windows" in tela
    # 256 MiB de disco, imagem de 64 MiB e nb3swap=0
    assert "boot 64 MB," in tela and "system 192 MB (ext4) and no swap." in tela
    assert "'Maratona Linux', first in the order" in tela
    assert "Type YES and press Enter to ERASE this disk and install" in tela


def test_responder_no_nao_toca_em_nada(maq, tmp_path):
    """NO cancela antes do download: o disco, o firmware e a RAM ficam como
    estavam, e a máquina reinicia."""
    maq.teclado.write_text("NO\n")
    r, disco = _instala_disco(maq, tmp_path)
    assert "=== STOPPED ===" in r.stdout and "the answer was NO" in r.stdout, r.stdout + r.stderr
    assert "Nothing was changed on this computer" in r.stdout
    assert "nb3disk=NAME" in r.stdout
    assert "NAO-VOLTA" not in r.stdout
    assert _zerado(disco)
    acoes = maq.acoes_texto()
    assert "efibootmgr" not in acoes and "losetup" not in acoes
    assert not (maq.inst / "sala1-abc.img").exists(), "baixou antes do YES"
    assert acoes.rstrip().endswith("REBOOT")


@pytest.mark.parametrize("console", ["/dev/null", "/nao/existe"])
def test_console_sem_resposta_cancela(maq, tmp_path, console):
    """Fim da entrada (Ctrl-D) ou console que nem abre: cancela na hora, sem
    laço e sem instalar."""
    r, disco = _instala_disco(maq, tmp_path, NB_INST_TTY=console)
    assert "=== STOPPED ===" in r.stdout and "read from the keyboard" in r.stdout, r.stdout + r.stderr
    assert _zerado(disco)
    assert maq.acoes_texto().rstrip().endswith("REBOOT")


def test_resposta_que_nao_vale_pergunta_de_novo(maq, tmp_path):
    """Linha vazia, "y" ou "sim" nem instalam nem cancelam: a tela volta com o
    aviso. Só a palavra inteira instala, em qualquer caixa."""
    maq.teclado.write_text("\ny\nsim\n  Yes  \n")
    r, disco = _instala_disco(maq, tmp_path)
    assert r.stdout.count("=== ERASE? ===") == 4, r.stdout + r.stderr
    assert r.stdout.count("Only the whole word YES installs") == 3
    assert "=== DONE ===" in r.stdout
    assert not _zerado(disco)


def test_sem_resposta_no_prazo_cancela(maq, tmp_path):
    """O prazo é a saída desatendida da exceção à invariante 5: ninguém
    respondeu, nada muda e a máquina reinicia."""
    mestre, escravo, nome = _pty()
    try:
        r, disco = _instala_disco(maq, tmp_path, NB_INST_TTY=nome, NB_INST_ASK_WAIT="1")
    finally:
        os.close(mestre)
        os.close(escravo)
    assert "=== STOPPED ===" in r.stdout and "no answer in time" in r.stdout, r.stdout + r.stderr
    assert _zerado(disco)


def test_o_que_foi_digitado_antes_da_tela_nao_responde(maq, tmp_path):
    """Um yes que já estava no buffer do console (digitado enquanto o boot
    subia) é descartado: quem responde viu a tela."""
    mestre, escravo, nome = _pty()
    try:
        os.write(mestre, b"yes\n")
        r, disco = _instala_disco(maq, tmp_path, NB_INST_TTY=nome, NB_INST_ASK_WAIT="2")
    finally:
        os.close(mestre)
        os.close(escravo)
    assert "=== STOPPED ===" in r.stdout and "no answer in time" in r.stdout, r.stdout + r.stderr
    assert _zerado(disco)


def test_yes_digitado_no_terminal_instala(maq, tmp_path):
    """O caminho de verdade: um terminal, a tela aparece, e só então alguém
    digita yes."""
    import select
    import time

    script, disco = _cena_disco(maq, tmp_path)
    mestre, escravo, nome = _pty()
    try:
        proc = subprocess.Popen(
            ["sh", "-c", HARNESS + "\n" + script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=maq.env(NB_INST_TTY=nome, NB_MIN_FREE_KB="1024", NB_INST_MARGEM="0"),
            cwd=str(maq.tmp),
        )
        saida = b""
        prazo = time.monotonic() + 60
        while b"Type YES" not in saida:
            assert time.monotonic() < prazo, saida
            if select.select([proc.stdout], [], [], 1)[0]:
                pedaco = os.read(proc.stdout.fileno(), 65536)
                assert pedaco, saida
                saida += pedaco
        os.write(mestre, b"yes\n")
        resto, _ = proc.communicate(timeout=120)
        saida = (saida + resto).decode()
    finally:
        os.close(mestre)
        os.close(escravo)
    assert "=== DONE ===" in saida, saida
    assert [x["type"] for x in _particoes(disco)] == ["ef", "83"]


# --- ao lado do Windows ---


def _maquina_com_windows(maq, tmp_path, *, com_windows=True):
    """sda1 = a ESP do Windows, sda2 = o NTFS com espaço; sdb = o pendrive."""
    maq.disco("sda", 500 * 1024 * MiB, partes=[
        ("sda1", 100 * MiB, {"PART_ENTRY_TYPE": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "TYPE": "vfat"}),
        ("sda2", 400 * 1024 * MiB, {"TYPE": "ntfs", "UUID": "1A2B3C4D5E6F7A8B"}),
    ])
    esp = maq.conteudo / "sda1"
    (esp / "EFI" / "Boot").mkdir(parents=True)
    (esp / "EFI" / "Boot" / "bootx64.efi").write_bytes(b"o reserva do Windows")
    if com_windows:
        (esp / "EFI" / "Microsoft" / "Boot").mkdir(parents=True)
        (esp / "EFI" / "Microsoft" / "Boot" / "bootmgfw.efi").write_bytes(b"windows")
    sistema = maq.conteudo / "sda2"
    (sistema / "Users").mkdir(parents=True)
    # o conteúdo da imagem da sede, como o loop dela montado o mostra
    fat = maq.conteudo / "loop7"
    (fat / "EFI" / "BOOT").mkdir(parents=True)
    (fat / "EFI" / "BOOT" / "BOOTX64.EFI").write_bytes(b"grub standalone")
    (fat / "vmlinuz").write_bytes(b"kernel da sede")
    (fat / "initrd.img").write_bytes(b"initrd da sede")
    (fat / "nutellaboot.conf").write_text('set IMAGEROOT="sala1"\nset NB_BOOT_KEY="nb3b_abc"\n')
    (fat / "wifi.conf").write_text("# só comentários\n")
    (maq.run / "wifi.conf").write_text("Rede\tsenha\t\n")
    img = imagem_da_sede(tmp_path / "sala1-abc.img")
    maq.servir(img)
    return esp, sistema


LADO = """
NB_CFG_DEV="{pendrive}"
BLOCKROOT="{tmp}/blockroot"
STORAGEDIR="$BLOCKROOT/nutellaboot"
mkdir -p "$BLOCKROOT"
nb_find_storage() {{ possibledisks="{dev}/sda2"; }}
nb_mount_storage() {{ mount -t ntfs "$1" "$BLOCKROOT"; }}
nb3install=lado
nb_install
echo NAO-VOLTA
"""


def test_instala_ao_lado_do_windows(maq, tmp_path):
    esp, sistema = _maquina_com_windows(maq, tmp_path)
    r = maq.roda(LADO.format(pendrive=_pendrive_usb(maq), tmp=maq.tmp, dev=maq.dev))
    assert "=== DONE ===" in r.stdout, r.stdout + r.stderr
    assert "NAO-VOLTA" not in r.stdout

    nosso = esp / "EFI" / "nutellaboot"
    assert (nosso / "grubx64.efi").read_bytes() == b"grub standalone"
    assert "nb3b_abc" in (nosso / "nutellaboot.conf").read_text()
    # o wifi.conf da sede (o da RAM), não o só-comentários do servidor
    assert (nosso / "wifi.conf").read_text() == "Rede\tsenha\t\n"
    assert (nosso / "nb3-disco").is_file()
    assert (sistema / "nutellaboot" / "boot" / "vmlinuz").read_bytes() == b"kernel da sede"
    assert (sistema / "nutellaboot" / "boot" / "initrd.img").read_bytes() == b"initrd da sede"
    assert (sistema / "nutellaboot" / "boot" / "nb3-disco").is_file()
    # nada do Windows mudou, nem o caminho reserva dele
    assert (esp / "EFI" / "Microsoft" / "Boot" / "bootmgfw.efi").read_bytes() == b"windows"
    assert (esp / "EFI" / "Boot" / "bootx64.efi").read_bytes() == b"o reserva do Windows"
    assert sorted(x.name for x in sistema.iterdir()) == ["Users", "nutellaboot"]

    cfg = (nosso / "grub.cfg").read_text()
    assert "search --no-floppy --fs-uuid --set=nb3stor 1A2B3C4D5E6F7A8B" in cfg
    # o conf, o wifi.conf e o marcador chegam ao initrd num cpio a mais: o
    # `newc:` do GRUB não existe no carregador EFI do Fedora
    assert "initrd.img $nb3esp/nb3conf.cpio" in cfg
    assert "newc:" not in cfg
    dentro = maq.tmp / "cpio-dentro"
    dentro.mkdir()
    subprocess.run(["cpio", "-id", "--quiet"], stdin=open(nosso / "nb3conf.cpio", "rb"), cwd=dentro, check=True)
    assert sorted(x.name for x in dentro.iterdir()) == ["nb3-disco", "nutellaboot.conf", "wifi.conf"]
    assert (dentro / "wifi.conf").read_text() == "Rede\tsenha\t\n"
    assert (dentro / "nutellaboot.conf").read_text() == (nosso / "nutellaboot.conf").read_text()
    assert "chainloader ($nb3win)/EFI/Microsoft/Boot/bootmgfw.efi" in cfg
    assert "IMAGEROOT=sala1 NB_SERVER=https://s" in cfg
    assert "nb3b_abc" not in cfg, "a chave de boot não vai na linha do kernel"
    # o Maratona é o padrão (a primeira entrada), com 10 s para escolher
    assert "set default=0" in cfg and "set timeout=10" in cfg
    assert cfg.index("menuentry \"Maratona Linux") < cfg.index('menuentry "Windows"')

    acoes = maq.acoes_texto()
    assert f"efibootmgr -q -c -d {maq.dev}/sda -p 1 -L Maratona Linux -l \\EFI\\nutellaboot\\grubx64.efi" in acoes
    assert acoes.rstrip().endswith("REBOOT")


def test_o_menu_da_esp_e_grub_valido(maq, tmp_path):
    check = shutil.which("grub2-script-check") or shutil.which("grub-script-check")
    if not check:
        pytest.skip("sem grub-script-check")
    for windows, uuid in ((1, "1A2B3C4D5E6F7A8B"), ("", "")):
        r = maq.roda(f'nb_inst_grubcfg_lado "{uuid}" "{windows}" > cfg')
        assert r.returncode == 0, r.stderr
        cfg = maq.tmp / "cfg"
        p = subprocess.run([check, str(cfg)], capture_output=True, text=True)
        assert p.returncode == 0, p.stderr + cfg.read_text()
        assert ("chainloader" in cfg.read_text()) == bool(windows)


def test_a_linha_do_kernel_e_a_do_pendrive():
    """O menu da ESP é escrito pelo stuff, e o do pendrive pelo nb3-genusb: os
    parâmetros de boot têm de ser os mesmos, ou a máquina instalada boota de
    outro jeito."""
    import re

    genusb = re.search(r'^CMDLINE_BASE="([^"]+)"', GENUSB.read_text(), re.M).group(1)
    stuff = re.search(r'_igl_base="([^"]+)"', (STUFF / "27-install.sh").read_text()).group(1)
    assert stuff.startswith(genusb + " "), (genusb, stuff)


def test_ao_lado_sem_esp_nao_muda_nada(maq, tmp_path):
    esp, sistema = _maquina_com_windows(maq, tmp_path)
    (maq.blkid / "sda1.PART_ENTRY_TYPE").unlink()
    r = maq.roda(LADO.format(pendrive=_pendrive_usb(maq), tmp=maq.tmp, dev=maq.dev))
    assert "=== NO ESP ===" in r.stdout, r.stdout
    assert not (esp / "EFI" / "nutellaboot").exists()
    assert not (sistema / "nutellaboot").exists()
    assert "efibootmgr" not in maq.acoes_texto()


def test_ao_lado_com_a_esp_cheia_nao_muda_nada(maq, tmp_path):
    esp, sistema = _maquina_com_windows(maq, tmp_path)
    maq.stub("df", 'echo "Filesystem 1K-blocks Used Available Use% Mounted on"\necho "esp 102400 102390 10 100% /x"\n')
    r = maq.roda(LADO.format(pendrive=_pendrive_usb(maq), tmp=maq.tmp, dev=maq.dev))
    assert "=== NO ROOM ===" in r.stdout, r.stdout
    assert not (esp / "EFI" / "nutellaboot").exists()
    assert not (sistema / "nutellaboot").exists()


def test_ao_lado_a_tela_mostra_onde_e_no_nao_muda_nada(maq, tmp_path):
    esp, sistema = _maquina_com_windows(maq, tmp_path)
    maq.teclado.write_text("no\n")
    r = maq.roda(LADO.format(pendrive=_pendrive_usb(maq), tmp=maq.tmp, dev=maq.dev))
    tela = r.stdout
    assert "=== CONFIRM ===" in tela and "Nothing is erased" in tela, tela + r.stderr
    assert "EFI system partition (EFI/nutellaboot)" in tela
    assert "sda1" in tela and "vfat" in tela and "sda2" in tela and "ntfs" in tela
    assert "(disk /dev/sda: Disco de Teste, 500 GB)" in tela
    assert "Windows stays" in tela and "already there" not in tela
    assert "Type YES and press Enter to install" in tela
    assert "=== STOPPED ===" in tela and "the answer was NO" in tela
    assert "nb3disk" not in tela
    # nada gravado, nada baixado, e o disco do sistema desmontado antes do reboot
    assert not (esp / "EFI" / "nutellaboot").exists()
    assert not (sistema / "nutellaboot").exists()
    assert not (maq.inst / "sala1-abc.img").exists()
    acoes = maq.acoes_texto()
    assert "efibootmgr" not in acoes
    assert f"umount {maq.tmp}/blockroot" in acoes
    assert acoes.rstrip().endswith("REBOOT")


def test_ao_lado_a_tela_avisa_que_substitui_a_instalacao_anterior(maq, tmp_path):
    esp, sistema = _maquina_com_windows(maq, tmp_path)
    (esp / "EFI" / "nutellaboot").mkdir()
    maq.teclado.write_text("no\n")
    r = maq.roda(LADO.format(pendrive=_pendrive_usb(maq), tmp=maq.tmp, dev=maq.dev))
    assert "The Maratona boot loader already there is replaced" in r.stdout, r.stdout


def test_desinstalar_leva_so_o_que_e_nosso(maq, tmp_path):
    esp, sistema = _maquina_com_windows(maq, tmp_path)
    (esp / "EFI" / "nutellaboot").mkdir()
    (esp / "EFI" / "nutellaboot" / "grubx64.efi").write_bytes(b"grub")
    maq.efilista.write_text(
        "BootOrder: 0005,0001,0003\n"
        "Boot0001* Windows Boot Manager\tHD(1,GPT,...)\n"
        "Boot0003* Maratona Linux\tHD(1,GPT,...)/\\EFI\\nutellaboot\\grubx64.efi\n"
        "Boot0005* Maratona Linux\n"
        "Boot0007* Maratona Linux 2\n"
    )
    r = maq.roda(f'NB_CFG_DEV="{_pendrive_usb(maq)}"; nb_inst_desinstala; echo FIM')
    assert "FIM" in r.stdout, r.stdout + r.stderr
    acoes = maq.acoes_texto()
    assert "efibootmgr -q -b 0003 -B" in acoes and "efibootmgr -q -b 0005 -B" in acoes
    assert "-b 0001" not in acoes and "-b 0007" not in acoes
    assert not (esp / "EFI" / "nutellaboot").exists()
    assert (esp / "EFI" / "Microsoft" / "Boot" / "bootmgfw.efi").is_file()


def test_factory_reset_ao_lado_desinstala(maq, tmp_path):
    """No boot do disco ao lado do Windows o factory reset desfaz a instalação
    inteira, e a tela diz que o Windows volta — não manda tirar pendrive."""
    esp, sistema = _maquina_com_windows(maq, tmp_path)
    (esp / "EFI" / "nutellaboot").mkdir()
    r = maq.roda(
        'NB_DISKBOOT=1; factoryreset=y; BLOCKROOT=x; STORAGEDIR=x/nutellaboot; '
        "nutella_findblock() { :; }; runpremountconfigs; echo NAO"
    )
    assert "Windows starts on the next boot" in r.stdout, r.stdout
    assert not (esp / "EFI" / "nutellaboot").exists()
    assert "NAO" not in r.stdout


# --- de onde veio o boot ---


@pytest.mark.parametrize(
    "prep,esperado",
    [
        ("NB_NETBOOT=1", "rede"),
        ("NB_DISKBOOT=1", "lado"),
        ('NB_CFG_DEV="$NB_INST_DEV/sda1"', "disco"),
        ('NB_CFG_DEV="$NB_INST_DEV/sdb1"', "usb"),
        (":", "usb"),
    ],
)
def test_de_onde_veio_o_boot(maq, prep, esperado):
    maq.disco("sda", 256 * 1024 * MiB, partes=[("sda1", 800 * MiB, {})])
    _pendrive_usb(maq)
    r = maq.roda(f'{prep}; echo "ORIGEM=$(nb_boot_origem)"')
    assert f"ORIGEM={esperado}" in r.stdout, r.stdout + r.stderr


# --- telas ---

MAX_LINHAS = 25


def _sem_cor(texto: str) -> list[str]:
    import re

    return [re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", l) for l in texto.splitlines()]


def test_a_pergunta_do_disco_cabe_na_tela(maq):
    """O pior caso: muitas partições, disco maior que o MBR alcança e o aviso
    de resposta errada. A linha do prompt fica sem quebra e conta."""
    partes = [(f"sda{i}", 10 * 1024 * MiB, {"TYPE": "ntfs", "LABEL": f"Dados {i}"}) for i in range(1, 9)]
    tb3 = 3 * 1024 * 1024 * MiB
    maq.disco("sda", tb3, partes=partes)
    (maq.tmp / "lista").write_text(f"sda hd {tb3} Western Digital WD Blue SN570 4TB NVMe\n")
    r = maq.roda(f"nb_inst_tela_disco 1 sda lista {401 * MiB} {4 * 1024 * MiB}", NB_UI_PLAIN="0")
    linhas = _sem_cor(r.stdout)
    assert len(linhas) <= MAX_LINHAS, f"a pergunta usa {len(linhas)} linhas"
    assert max(len(l) for l in linhas) <= 80, max(linhas, key=len)
    assert "... and more" in r.stdout and "first 2 TB" in r.stdout
    assert "Only the whole word YES installs" in r.stdout


def test_a_pergunta_ao_lado_cabe_na_tela(maq):
    """O pior caso: a ESP e o sistema em discos diferentes, rótulo comprido, a
    instalação anterior e o aviso de resposta errada."""
    maq.disco("sda", 256 * 1024 * MiB, modelo="Samsung SSD 970 EVO Plus 250GB", partes=[
        ("sda1", 100 * MiB, {"TYPE": "vfat", "LABEL": "SYSTEM"})])
    maq.disco("sdc", 1024 * 1024 * MiB, ssd=False, modelo="Western Digital WD10EZEX-08WN4A0", partes=[
        ("sdc1", 16 * MiB, {}),
        ("sdc2", 900 * 1024 * MiB, {"TYPE": "ntfs", "LABEL": "Um rotulo de NTFS bem comprido mesmo"})])
    r = maq.roda(
        "NB_INST_ESPLIVRE=71234 NB_INST_ESPWIN=1 NB_INST_ESPVELHO=1; "
        f'nb_inst_tela_lado 1 sda1 "{maq.dev}/sdc2" 123456789',
        NB_UI_PLAIN="0",
    )
    linhas = _sem_cor(r.stdout)
    assert len(linhas) <= MAX_LINHAS, f"a pergunta usa {len(linhas)} linhas"
    assert max(len(l) for l in linhas) <= 80, max(linhas, key=len)
    assert "/dev/sda:" in r.stdout and "/dev/sdc:" in r.stdout
    assert "already there is replaced" in r.stdout and "Only the whole word" in r.stdout


def test_a_desistencia_cabe_na_tela_e_lista_os_discos(maq):
    """Quem disse NO no modo disco muitas vezes quer outro disco: a tela lista
    os nomes que o nb3disk= aceita."""
    maq.inst.mkdir()
    (maq.inst / "discos").write_text(
        "".join(f"sd{c} hd {500 * 1024 * MiB} Disco {c}\n" for c in "abcdefgh")
    )
    r = maq.roda("NB_INST_MOTIVO=no; nb_inst_cancelada disco", NB_UI_PLAIN="0")
    # o aviso no log sai antes da tela, e a tela limpa o console: conta o que
    # fica visível
    linhas = _sem_cor(r.stdout.split("\x1b[2J")[-1])
    assert len(linhas) <= MAX_LINHAS, f"a desistência usa {len(linhas)} linhas"
    assert max(len(l) for l in linhas) <= 80, max(linhas, key=len)
    assert "sda" in r.stdout and "nb3disk=NAME" in r.stdout and "... and more" in r.stdout
    assert "REBOOT" in maq.acoes_texto()


def test_a_recusa_de_disco_cabe_na_tela(maq):
    (maq.tmp / "lista").write_text(
        "".join(f"sd{c} hd {500 * 1024 * MiB} Disco {c}\n" for c in "abcdefgh")
    )
    r = maq.roda("nb_inst_sem_disco_screen lista", NB_UI_PLAIN="0")
    assert len(r.stdout.splitlines()) <= MAX_LINHAS, r.stdout
    assert "REBOOT" in maq.acoes_texto()


# --- o servidor: /boot/v3/<sede>/install ---


def _chave(sede):
    return {"X-NB-Boot-Key": sede["boot_key"]}


def test_a_rota_exige_a_chave(client, sede):
    assert client.get("/boot/v3/sala1/install").status_code == 401
    assert client.get("/boot/v3/sala1/installfile/x.img").status_code == 401


@pytest.fixture
def agendados(monkeypatch):
    """Sem tarefa em segundo plano: o TestClient não as deixa terminar entre
    uma requisição e outra (o estado ficaria em "building" para sempre). O
    teste anota o que seria agendado e gera na hora, quando precisa."""
    pedidos = []

    def _anota(corotina):
        pedidos.append(corotina.__name__)
        corotina.close()
        return True

    monkeypatch.setattr(usb, "_agendar", _anota)
    return pedidos


def test_sem_imagem_responde_wait_e_gera(agendados, build, genusb, client, data_root, sede):
    """A imagem ainda não existe: WAIT, e a geração começa ali mesmo. Pronta,
    a rota diz o md5, o tamanho, o nome e de onde baixar."""
    import asyncio

    fsdb.write_json(data_root / "site-images" / "sala1" / "usb.json", {})
    agendados.clear()
    r = client.get("/boot/v3/sala1/install", headers=_chave(sede))
    assert r.status_code == 200 and r.text == "WAIT the USB image of this site does not exist yet\n"
    # (a genérica entra junto quando falta, como em toda geração da sala)
    assert "gerar_da_sala" in agendados
    asyncio.run(usb.gerar_da_sala("sala1"))
    r = client.get("/boot/v3/sala1/install", headers=_chave(sede))
    partes = r.text.split()
    assert partes[0] == "IMAGE", r.text
    estado = usb.image_state("sala1")
    assert partes[1] == hashlib.md5(b"imagem de mentira").hexdigest() == estado["md5"]
    assert partes[2] == "17" and partes[3] == estado["file"]
    assert partes[-1].endswith(f"/boot/v3/sala1/installfile/{estado['file']}")
    assert estado["layout"] == usb.GRUB_LAYOUT


def test_enquanto_gera_nao_agenda_de_novo(agendados, client, data_root, sede):
    """Quarenta máquinas pedindo ao mesmo tempo: uma geração só."""
    fsdb.write_json(data_root / "site-images" / "sala1" / "usb.json", {"status": "building"})
    agendados.clear()
    r = client.get("/boot/v3/sala1/install", headers=_chave(sede))
    assert r.text == "WAIT generating the USB image of this site\n"
    assert agendados == []


def test_imagem_de_antes_do_instalador_e_gerada_de_novo(client, data_root, sede, monkeypatch):
    pedidos = []
    monkeypatch.setattr(usb, "agendar", lambda image_id, forcado=False: pedidos.append((image_id, forcado)))
    (data_root / "usb").mkdir(exist_ok=True)
    (data_root / "usb" / "sala1-x.img").write_bytes(b"velha")
    fsdb.write_json(
        data_root / "site-images" / "sala1" / "usb.json",
        {"status": "done", "file": "sala1-x.img", "size": 5, "suffix": "x"},
    )
    r = client.get("/boot/v3/sala1/install", headers=_chave(sede))
    assert r.text == "WAIT the USB image of this site predates the installer\n"
    assert pedidos == [("sala1", True)]


def test_geracao_que_falhou_vira_fail(client, data_root, sede):
    fsdb.write_json(
        data_root / "site-images" / "sala1" / "usb.json",
        {"status": "failed", "error": "faltando: grub2-mkstandalone\nlinha 2"},
    )
    r = client.get("/boot/v3/sala1/install", headers=_chave(sede))
    assert r.text.startswith("FAIL ") and "faltando: grub2-mkstandalone linha 2" in r.text
    assert "\n" not in r.text.rstrip("\n")


def test_installfile_so_entrega_o_arquivo_atual(build, genusb, client, data_root, sede):
    import asyncio

    estado = asyncio.run(usb.gerar_da_sala("sala1"))
    (data_root / "usb" / "outra-sede-y.img").write_bytes(b"de outra sede")
    r = client.get(f"/boot/v3/sala1/installfile/{estado['file']}", headers=_chave(sede))
    assert r.status_code == 200 and r.content == b"imagem de mentira"
    for nome in ("outra-sede-y.img", "../usb.json", "nutellaboot3.img"):
        assert client.get(f"/boot/v3/sala1/installfile/{nome}", headers=_chave(sede)).status_code == 404


def test_gerar_o_pendrive_de_instalacao(agendados, build, genusb, client, data_root, sede, ha):
    """Sob demanda, com a mesma sede, chave e par do pendrive da sala, mais o
    menu de instalar; e nunca publicado."""
    import asyncio

    agendados.clear()
    r = client.post("/api/v1/site-images/sala1/usb/installer", headers=ha)
    assert r.status_code == 202
    assert agendados == ["gerar_instalador"]
    estado = asyncio.run(usb.gerar_instalador("sala1"))
    assert estado["status"] == "done", estado
    assert estado["file"].endswith("-instalador.img")
    args = genusb.read_text()
    assert "arg: --installer" in args and f"arg: {sede['boot_key']}" in args
    assert "arg: --publish" not in args
    visao = client.get("/api/v1/site-images/sala1/usb", headers=ha).json()
    assert visao["installer"]["file"] == estado["file"]
    # o mesmo sufixo secreto do pendrive da sala: o nome não se adivinha
    assert estado["suffix"] == usb._sufixo("sala1")


def test_baixar_o_pendrive_de_instalacao(build, genusb, client, data_root, sede, ha):
    import asyncio

    asyncio.run(usb.gerar_instalador("sala1"))
    assert client.get("/api/v1/site-images/sala1/usb/installer/image").status_code == 401
    r = client.get(f"/api/v1/site-images/sala1/usb/installer/image?tk={sede['token']}")
    assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"


# --- o GRUB do pendrive (nb3-genusb de verdade) ---


def test_o_pendrive_de_instalacao_tem_as_entradas_mas_boota_normal(tmp_path):
    _, ler = _gera_pendrive(tmp_path, "sala1", "Sala 1", "--installer")
    cfg = ler("grub.cfg")
    assert "set default=0" in cfg
    entradas = cfg.split("menuentry ")[1:]
    assert "nb3install" not in entradas[0], "o padrão tem de ser o boot normal"
    assert "nb3install=disco" in cfg and "nb3install=lado" in cfg
    # ao lado do Windows só aparece no UEFI
    assert cfg.index('if [ "$grub_platform" = "efi" ]') < cfg.index("nb3install=lado")


def test_o_pendrive_normal_nao_instala_nada(tmp_path):
    _, ler = _gera_pendrive(tmp_path, "sala1", "Sala 1")
    assert "nb3install" not in ler("grub.cfg")


def test_toda_entrada_passa_a_particao_de_onde_bootou(tmp_path):
    _, ler = _gera_pendrive(tmp_path, "sala1", "Sala 1", "--installer")
    cfg = ler("grub.cfg")
    assert 'probe --set=nb3uuid --fs-uuid "$root"' in cfg
    linhas = [l for l in cfg.splitlines() if l.strip().startswith("linux ")]
    assert linhas and all("nb3cfg=$nb3uuid" in l for l in linhas)


def test_o_grub_embutido_serve_aos_tres_lugares():
    """O instalador copia o BOOTX64.EFI da imagem da sede: a config embutida
    acha o pendrive (e a p1 do disco inteiro) pelo nutellaboot.conf e, por
    último, o menu da ESP de quem instalou ao lado do Windows.

    Sem `$cmdpath`: numa imagem standalone ele chega vazio (visto em VM), e o
    menu da ESP caía no prompt do GRUB. E o resultado da busca não pode ir
    direto para `root`: numa imagem standalone ele começa em `memdisk`, e o
    teste de vazio nunca disparava."""
    import re

    texto = GENUSB.read_text()
    embutida = re.search(r"cat > \"\$WORK/grub-embed.cfg\" << 'EOF'\n(.*?)\nEOF\n", texto, re.S).group(1)
    codigo = "\n".join(l for l in embutida.splitlines() if not l.lstrip().startswith("#"))
    assert "cmdpath" not in codigo
    assert "--set=root" not in codigo
    assert codigo.index("/nutellaboot.conf") < codigo.index("/EFI/nutellaboot/grub.cfg")
    assert 'configfile "($nb3esp)/EFI/nutellaboot/grub.cfg"' in codigo
    assert 'set root="$nb3root"' in codigo
    check = shutil.which("grub2-script-check") or shutil.which("grub-script-check")
    if check:
        p = subprocess.run([check], input=embutida, capture_output=True, text=True)
        assert p.returncode == 0, p.stderr


def test_o_menu_da_esp_nao_depende_do_cmdpath(maq):
    """O menu da ESP acha a própria pasta pelo marcador: com o `$cmdpath`
    vazio, o conf e o wifi.conf não chegariam ao initrd."""
    r = maq.roda('nb_inst_grubcfg_lado "1A2B" 1')
    codigo = "\n".join(l for l in r.stdout.splitlines() if not l.lstrip().startswith("#"))
    assert "cmdpath" not in codigo
    assert "search --no-floppy --file --set=nb3espdev /EFI/nutellaboot/nb3-disco" in codigo
    assert 'set nb3esp="($nb3espdev)/EFI/nutellaboot"' in codigo


def test_o_cpio_do_conf_e_o_que_o_kernel_le(maq):
    """O cpio que vai para o initrd é escrito à mão (o cpio do initrd só
    extrai): o `cpio` de verdade tem de ler os mesmos bytes, inclusive com
    nomes e conteúdos de tamanho que não é múltiplo de 4."""
    a = maq.tmp / "a"
    a.write_text("set IMAGEROOT=\"sala1\"\n")
    b = maq.tmp / "b"
    b.write_bytes(b"x" * 4097)
    vazio = maq.tmp / "c"
    vazio.write_bytes(b"")
    r = maq.roda(f'nb_inst_cpio "{maq.tmp}/saida.cpio" nutellaboot.conf "{a}" w "{b}" nb3-disco "{vazio}"; echo "SAIU=$?"')
    assert "SAIU=0" in r.stdout, r.stdout + r.stderr
    dentro = maq.tmp / "dentro"
    dentro.mkdir()
    p = subprocess.run(["cpio", "-idv"], stdin=open(maq.tmp / "saida.cpio", "rb"), cwd=dentro,
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert (dentro / "nutellaboot.conf").read_bytes() == a.read_bytes()
    assert (dentro / "w").read_bytes() == b.read_bytes()
    assert (dentro / "nb3-disco").read_bytes() == b""
    assert (maq.tmp / "saida.cpio").stat().st_size % 4 == 0


# --- o wifi da sede no disco inteiro ---


def test_disco_inteiro_leva_o_wifi_da_sede(maq, tmp_path):
    """A imagem do servidor só tem os comentários no wifi.conf: o da sede é o
    que está na RAM (o do pendrive). Sem copiá-lo, a sede que boota por wifi
    instalaria um disco sem rede, parado na tela NO NETWORK."""
    (maq.run / "wifi.conf").write_text("Rede da sede\tsenha\t\n")
    fat = maq.conteudo / "loop7"
    fat.mkdir()
    (fat / "wifi.conf").write_text("# só comentários\n")
    r, disco = _instala_disco(maq, tmp_path)
    assert "=== DONE ===" in r.stdout, r.stdout + r.stderr
    assert (fat / "wifi.conf").read_text() == "Rede da sede\tsenha\t\n"
    acoes = maq.acoes_texto()
    # a p1 da imagem baixada, montada por um loop antes de gravar o disco
    imagem = maq.inst / "sala1-abc.img"
    assert f"losetup -o {2048 * 512} -f --show {imagem}" in acoes
    assert acoes.index(f"losetup -o {2048 * 512} -f --show {imagem}") < acoes.index("efibootmgr -q -c")


def test_sem_conseguir_por_o_wifi_nao_apaga_o_disco(maq, tmp_path):
    """Melhor parar antes de apagar do que instalar um disco sem rede."""
    (maq.run / "wifi.conf").write_text("Rede da sede\tsenha\t\n")
    r, disco = _instala_disco(maq, tmp_path)
    assert "could not put this site's wifi.conf" in r.stdout, r.stdout
    assert "Nothing was changed on the disk" in r.stdout
    assert disco.read_bytes() == b"\0" * disco.stat().st_size


# --- apagar a sede ---


def test_apagar_a_sede_leva_o_pendrive_de_instalacao(agendados, build, genusb, client, data_root, sede, ha):
    """O arquivo do pendrive de instalação leva a chave de boot, como o da
    sala: não pode ficar órfão no disco do servidor."""
    import asyncio

    sala = asyncio.run(usb.gerar_da_sala("sala1"))
    inst = asyncio.run(usb.gerar_instalador("sala1"))
    assert usb.file_path(sala["file"]).is_file() and usb.file_path(inst["file"]).is_file()
    assert client.delete("/api/v1/site-images/sala1", headers=ha).status_code == 204
    assert not usb.file_path(sala["file"]).exists()
    assert not usb.file_path(inst["file"]).exists()
