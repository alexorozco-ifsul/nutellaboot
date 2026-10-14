# shellcheck shell=sh
# Instalação no disco: a máquina passa a bootar sem pendrive.
#
# O sistema já mora no disco: as camadas, a home e o swap ficam em
# nutellaboot/, numa partição ext4 ou NTFS (30-storage.sh). O pendrive só leva
# o BOOT (GRUB, kernel, initrd e nutellaboot.conf), e instalar é levar esses
# quatro para o disco. Dois jeitos, escolhidos no menu do pendrive de
# instalação (`nb3-genusb --installer` põe `nb3install=` na linha do kernel):
#
#   disco  o disco inteiro vira um pendrive. A imagem do pendrive da sede vai
#          para o começo dele (p1, a NB3CFG, com o mesmo GRUB para BIOS e
#          UEFI), e o resto ganha uma ext4 (p2, NB3DATA) e um swap (p3,
#          NB3SWAP). APAGA O DISCO. Daí em diante o boot do disco é o do
#          pendrive, inclusive a autoatualização (25-usbupdate.sh);
#   lado   ao lado do Windows, só no UEFI, sem mexer em partição nenhuma. O
#          GRUB, o menu, o conf e o wifi.conf vão para \EFI\nutellaboot\ da ESP
#          (sem letra no Windows: a chave de boot não fica em C:\), e o par
#          kernel+initrd para nutellaboot/boot/, na partição que o boot já usa.
#          O GRUB entrega ao initrd um cpio a mais (nb3conf.cpio) com o conf,
#          o wifi.conf e o marcador nb3-disco, que liga o NB_DISKBOOT do
#          bootstrap.
#
# Os dados vêm do servidor e não do pendrive: /boot/v3/<sede>/install aponta a
# imagem do pendrive da sede (a do console, com o par do modelo). O pendrive
# pode sair na faixa de sempre (invariante 9), e uma sede com iPXE instala sem
# pendrive nenhum.
#
# REGRAS (invariante 21 no CLAUDE.md):
#   * nunca toca disco removível, USB, nem aquele de onde este boot veio. No
#     rig de VM o "pendrive" é um disco IDE comum, e numa máquina real pode ser
#     um SSD externo que se diz fixo: por isso o disco do boot sai pelo nome;
#   * tudo é baixado e conferido pelo md5 ANTES de tocar no disco;
#   * nenhuma pergunta (invariante 5). A confirmação é a escolha no menu do
#     GRUB, e a tela mostra o disco e conta 30 s: desligar cancela. `mkfs`
#     sempre forçado e com a entrada vazia, porque diante de um NTFS velho o
#     mke2fs PERGUNTA, e a máquina ficaria parada;
#   * terminou, reinicia: o primeiro boot pelo disco já confere a instalação.

NB_INST_SYS_BLOCK=${NB_INST_SYS_BLOCK:-/sys/block}
NB_INST_DEV=${NB_INST_DEV:-/dev}
NB_INST_UUIDS=${NB_INST_UUIDS:-/dev/disk/by-uuid}
NB_INST_EFI=${NB_INST_EFI:-/sys/firmware/efi}
NB_INST_MOUNTS=${NB_INST_MOUNTS:-/proc/mounts}
NB_INST_MEMINFO=${NB_INST_MEMINFO:-/proc/meminfo}
NB_INST_TMP=${NB_INST_TMP:-/tmp/nb3install}
NB_INST_ESPMNT=${NB_INST_ESPMNT:-/nb3esp}
NB_INST_IMGMNT=${NB_INST_IMGMNT:-/nb3img}
# o rótulo da entrada do firmware: é por ele que a instalação seguinte e a
# desinstalação acham as entradas antigas
NB_INST_LABEL=${NB_INST_LABEL:-Maratona Linux}

nb_install() {
    case "${nb3install:-}" in
        "") return 0 ;;
        disco | lado) ;;
        *)
            nb_warn "unknown nb3install=$nb3install - booting normally"
            return 0
            ;;
    esac
    NB_INST_TOCOU=
    nb_phase "INSTALL - putting the boot on this computer's disk"
    modprobe loop 2> /dev/null
    nb_inst_prereq "$nb3install"
    nb_inst_baixa
    case "$nb3install" in
        disco) nb_inst_disco ;;
        lado) nb_inst_lado ;;
    esac
}

# --- de onde este boot veio (nb_boot_origem está no 25-usbupdate.sh) ---

# /dev/sdb1 -> sdb. Um disco sem partição é ele mesmo.
nb_inst_disco_de() {
    _idd_p=$(readlink -f "$NB_SYS_CLASS_BLOCK/${1##*/}" 2> /dev/null)
    [ -n "$_idd_p" ] || return 1
    [ -e "$_idd_p/partition" ] && _idd_p=${_idd_p%/*}
    printf '%s\n' "${_idd_p##*/}"
}

# O disco de onde este boot leu o conf: o que o bootstrap guardou, ou o do
# UUID que o GRUB passou. Vazio no boot pela rede.
nb_inst_disco_do_boot() {
    _idb_d=${NB_CFG_DEV:-}
    if [ -z "$_idb_d" ] && [ -n "${nb3cfg:-}" ]; then
        _idb_u=$(printf '%s' "$nb3cfg" | tr 'a-f' 'A-F')
        [ -e "$NB_INST_UUIDS/$_idb_u" ] && _idb_d=$(readlink -f "$NB_INST_UUIDS/$_idb_u")
    fi
    [ -n "$_idb_d" ] && nb_inst_disco_de "$_idb_d"
}

# --- antes de tudo ---

nb_inst_prereq() {
    if [ "$1" = lado ] && [ ! -d "$NB_INST_EFI" ]; then
        nb_fatal_screen "NO UEFI" \
            "!This computer started in legacy BIOS mode." \
            "" \
            "Installing next to Windows needs UEFI: in BIOS mode it would" \
            "overwrite the Windows boot code. Nothing was changed." \
            "" \
            "!What to do:" \
            "  -> keep starting this computer from the USB drive, or" \
            "  -> choose 'install on the whole disk' (it ERASES the disk)"
    fi
    _ipr_f=
    if [ "$1" = disco ] && ! command -v sfdisk > /dev/null 2>&1; then
        _ipr_f="$_ipr_f sfdisk"
    fi
    if [ -d "$NB_INST_EFI" ] && ! command -v efibootmgr > /dev/null 2>&1; then
        _ipr_f="$_ipr_f efibootmgr"
    fi
    # sem saber de que disco este boot veio, o próprio pendrive poderia ser o
    # alvo: só um initrd anterior ao instalador não diz
    if [ -z "${NB_NETBOOT:-}" ] && [ -z "$(nb_inst_disco_do_boot)" ]; then
        _ipr_f="$_ipr_f boot-device"
    fi
    [ -z "$_ipr_f" ] && return 0
    nb_fatal_screen "TOO OLD" \
        "!This installer USB drive is older than the installer." \
        "" \
        "Its boot files lack:$_ipr_f" \
        "Nothing was changed on the disk." \
        "" \
        "!What to do:" \
        "  -> rebuild the boot files on the server (nb3-build-initrd)," \
        "     then generate the installer image again in the console"
}

# Não volta: tela, espera e reinicia.
nb_inst_falhou() {
    if [ -n "${NB_INST_TOCOU:-}" ]; then
        _ifa_l="The disk WAS changed and may not start: run the installer again."
    else
        _ifa_l="Nothing was changed on the disk."
    fi
    nb_fatal_screen "FAILED" \
        "!The installation did not finish." \
        "" \
        "Reason: $1" \
        "$_ifa_l" \
        "" \
        "!What to do:" \
        "  -> check the network cable and start the installer again" \
        "  -> if it fails the same way, write down the reason above"
}

# A imagem gravada vai inteira para a RAM: no modo `disco` não há outro lugar,
# porque o disco é o que vai ser apagado.
nb_inst_ram() {
    _ira_kb=$(awk '/^MemAvailable:/ {print $2}' "$NB_INST_MEMINFO" 2> /dev/null)
    [ -n "$_ira_kb" ] || return 0
    [ "$_ira_kb" -gt $(($1 / 1024 + 524288)) ] && return 0
    nb_inst_falhou "not enough free memory for the $(($1 / 1048576)) MB installer file"
}

# A imagem do pendrive da sede, pela rota do instalador. Ela pode não estar
# pronta (primeira vez, ou gerada antes do instalador existir): o servidor
# responde WAIT, começa a gerá-la, e aqui se pergunta de novo. FAIL é geração
# que falhou no servidor: parar com a causa vale mais que esperar 15 minutos.
nb_inst_baixa() {
    _iba_n=0
    _iba_mudo=0
    while :; do
        _iba_r=$(nb_get "$NB_SERVER/boot/v3/$IMAGEROOT/install" 2> /dev/null)
        _iba_linha=$(printf '%s\n' "$_iba_r" | sed -n 's/^IMAGE //p' | sed -n 1p)
        [ -n "$_iba_linha" ] && break
        _iba_erro=$(printf '%s\n' "$_iba_r" | sed -n 's/^FAIL //p' | sed -n 1p)
        [ -n "$_iba_erro" ] && nb_inst_falhou "$_iba_erro"
        _iba_espera=$(printf '%s\n' "$_iba_r" | sed -n 's/^WAIT //p' | sed -n 1p)
        if [ -n "$_iba_espera" ]; then
            _iba_n=$((_iba_n + 1))
            [ "$_iba_n" -gt "${NB_INST_POLL_MAX:-90}" ] &&
                nb_inst_falhou "the server did not finish preparing the files ($_iba_espera)"
            nb_log "the server is preparing the installer files: $_iba_espera"
        else
            _iba_mudo=$((_iba_mudo + 1))
            [ "$_iba_mudo" -ge 3 ] &&
                nb_inst_falhou "the server did not answer /boot/v3/$IMAGEROOT/install"
            nb_warn "the server did not answer the installer request - trying again"
        fi
        sleep "${NB_INST_POLL:-10}"
    done
    # IMAGE <md5> <bytes> <arquivo> <url> [<url>...]
    # shellcheck disable=SC2086
    set -- $_iba_linha
    _iba_md5=$1
    _iba_tam=$2
    _iba_arq=$3
    shift 3
    case "$_iba_arq" in "" | .* | */*) nb_inst_falhou "the server sent a bad file name" ;; esac
    nb_inst_ram "$_iba_tam"
    mkdir -p "$NB_INST_TMP"
    NB_DL_PROGRESS=1
    NB_DL_LABEL="installer: $_iba_arq"
    nb_download "$NB_INST_TMP/$_iba_arq" 4 "$@" ||
        nb_inst_falhou "the installer files could not be downloaded"
    NB_DL_PROGRESS=0
    NB_DL_LABEL=
    nutella_md5sum "$_iba_md5" "$NB_INST_TMP/$_iba_arq" ||
        nb_inst_falhou "the downloaded file is damaged (MD5 does not match)"
    NB_INST_IMG=$NB_INST_TMP/$_iba_arq
}

# O início da p1 da imagem, em setores (o nb3-genusb a põe em 1 MiB).
nb_inst_p1() {
    sfdisk -d "$1" 2> /dev/null |
        sed -n 's/^[^:]*1 : start= *\([0-9]*\),.*/\1/p' | sed -n 1p
}

# "256 GB", ou "100 MB" para o que não chega a 1 GB
nb_inst_tamanho() {
    if [ "$1" -ge 2097152 ]; then
        printf '%s GB' "$(($1 / 2097152))"
    else
        printf '%s MB' "$(($1 / 2048))"
    fi
}

nb_inst_modelo() {
    _imo_m=
    if [ -r "$1/device/model" ]; then
        _imo_m=$(tr -s ' ' < "$1/device/model" | sed -e 's/^ //' -e 's/ $//')
    fi
    printf '%s' "${_imo_m:-disk}"
}

# --- modo disco ---

# Uma linha por disco: "nome situação setores modelo". A situação é `ssd` ou
# `hd` para quem pode receber a instalação; para quem não pode, o motivo, que
# vai para a tela. $1 é o tamanho mínimo, em setores.
nb_inst_discos() {
    _ids_boot=$(nb_inst_disco_do_boot)
    for _ids_d in "$NB_INST_SYS_BLOCK"/*; do
        [ -e "$_ids_d/size" ] || continue
        _ids_n=${_ids_d##*/}
        case "$_ids_n" in
            loop* | ram* | zram* | sr* | fd* | dm-* | md* | nbd* | *boot[0-9]* | *rpmb) continue ;;
        esac
        _ids_s=$(cat "$_ids_d/size" 2> /dev/null)
        [ -n "$_ids_s" ] && [ "$_ids_s" -gt 0 ] || continue
        if [ "$_ids_n" = "$_ids_boot" ]; then
            _ids_q=installer
        elif [ "$(cat "$_ids_d/removable" 2> /dev/null)" != 0 ]; then
            _ids_q=removable
        else
            case "$(readlink -f "$_ids_d" 2> /dev/null)" in
                */usb*) _ids_q=usb ;;
                *)
                    if [ "$_ids_s" -lt "$1" ]; then
                        _ids_q=small
                    elif [ "$(cat "$_ids_d/queue/rotational" 2> /dev/null)" = 0 ]; then
                        _ids_q=ssd
                    else
                        _ids_q=hd
                    fi
                    ;;
            esac
        fi
        printf '%s %s %s %s\n' "$_ids_n" "$_ids_q" "$_ids_s" "$(nb_inst_modelo "$_ids_d")"
    done
}

# O disco da instalação. `nb3disk=` (editado na linha do GRUB) vence, mas só
# entre os que podem. Sem ele, SSD/NVMe antes de HD, e empate não se decide:
# a tela lista os discos e nada é apagado.
nb_inst_escolhe() {
    if [ -n "${nb3disk:-}" ]; then
        _iec_d=${nb3disk#/dev/}
        case "$(awk -v d="$_iec_d" '$1 == d {print $2}' "$1")" in
            ssd | hd)
                printf '%s\n' "$_iec_d"
                return 0
                ;;
        esac
        return 1
    fi
    for _iec_t in ssd hd; do
        _iec_n=$(awk -v t="$_iec_t" '$2 == t {n++} END {print n + 0}' "$1")
        if [ "$_iec_n" -eq 1 ]; then
            awk -v t="$_iec_t" '$2 == t {print $1}' "$1"
            return 0
        fi
        [ "$_iec_n" -gt 1 ] && return 1
    done
    return 1
}

nb_inst_tabela_discos() {
    _itd_n=0
    while read -r _itd_d _itd_q _itd_s _itd_m; do
        [ -n "$_itd_d" ] || continue
        _itd_n=$((_itd_n + 1))
        # o orçamento é de 25 linhas (console VGA texto)
        if [ "$_itd_n" -gt 5 ]; then
            nb_ui_dim "    ... and more"
            break
        fi
        case "$_itd_q" in
            ssd) _itd_q="SSD" ;;
            hd) _itd_q="HD" ;;
            installer) _itd_q="this installer" ;;
            small) _itd_q="too small" ;;
        esac
        nb_ui_dim "$(printf '    %-10s %-15s %8s  %s' "$_itd_d" "$_itd_q" "$(nb_inst_tamanho "$_itd_s")" "$_itd_m")"
    done < "$1"
    [ "$_itd_n" = 0 ] && nb_ui_dim "    (no disk was detected)"
}

nb_inst_sem_disco_screen() {
    nb_screen "DISK?" \
        "!No disk was chosen for the installation. Nothing was erased." \
        "" \
        "!Disks on this computer:"
    nb_inst_tabela_discos "$1"
    printf '\n'
    nb_ui_text "What to do:"
    nb_ui_dim "  -> two disks of the same kind: press 'e' in the boot menu and"
    nb_ui_dim "     add nb3disk=NAME to the linux line, NAME from the list above"
    nb_ui_dim "  -> no SSD or HD in the list: this computer cannot be installed"
    nb_ui_dim "     (the disk must be internal and have room for the system)"
    _isd_w=$(nb_screen_wait)
    printf '\n'
    nb_ui_text "This machine will restart in ${_isd_w} seconds."
    sleep "$_isd_w"
    reboot -f
    panic=60 panic "no disk for the installation"
}

# O que existe no disco hoje, para a tela antes de apagar.
nb_inst_conteudo() {
    _ico_n=0
    for _ico_p in "$NB_INST_SYS_BLOCK/$1/$1"*; do
        [ -e "$_ico_p/partition" ] || continue
        _ico_n=$((_ico_n + 1))
        if [ "$_ico_n" -gt 5 ]; then
            nb_ui_dim "    ... and more"
            break
        fi
        _ico_b=${_ico_p##*/}
        _ico_t=$(blkid -o value -s TYPE "$NB_INST_DEV/$_ico_b" 2> /dev/null)
        _ico_l=$(blkid -o value -s LABEL "$NB_INST_DEV/$_ico_b" 2> /dev/null)
        _ico_s=$(cat "$_ico_p/size" 2> /dev/null)
        nb_ui_dim "$(printf '    %-14s %-8s %8s  %s' "$_ico_b" "${_ico_t:-?}" "$(nb_inst_tamanho "${_ico_s:-0}")" "$_ico_l")"
    done
    [ "$_ico_n" = 0 ] && nb_ui_dim "    (no partitions)"
}

# A última chance. Nada de tecla (invariante 5): quem escolheu a entrada no
# menu já confirmou, e quem errou desliga a máquina.
nb_inst_contagem() {
    _ict_s=$(awk -v d="$1" '$1 == d {print $3}' "$2")
    _ict_m=$(awk -v d="$1" '$1 == d {$1 = $2 = $3 = ""; sub(/^ +/, ""); print}' "$2")
    nb_screen "ERASING" \
        "!EVERYTHING ON THIS DISK WILL BE ERASED:" \
        "" \
        "    /dev/$1   $_ict_m   $(nb_inst_tamanho "${_ict_s:-0}")" \
        "" \
        "!On it now:"
    nb_inst_conteudo "$1"
    printf '\n'
    nb_ui_text "To cancel: TURN THE COMPUTER OFF now."
    nb_ui_dim "Maratona Linux takes the whole disk and starts without the USB drive."
    _ict_n=${NB_INST_WAIT:-30}
    while [ "$_ict_n" -gt 0 ]; do
        printf '\r  %sErasing in %2d seconds...%s ' "$NB_C_TXT" "$_ict_n" "$NB_C_OFF"
        sleep 1
        _ict_n=$((_ict_n - 1))
    done
    printf '\n'
}

# A imagem gravada é igual, byte a byte, à do pendrive de instalação: a mesma
# assinatura do MBR (zero, nas imagens do nb3-genusb) e o mesmo número de série
# da FAT. Com os dois espetados, o UUID que o GRUB passa (nb3cfg=) e a entrada
# do firmware (HD(1,MBR,<assinatura>)) poderiam apontar para o pendrive.
# A FAT32 guarda uma cópia do setor de boot no setor 6 da partição: as duas
# levam o mesmo número, ou o fsck.fat acusa a diferença.
nb_inst_identidade_nova() {
    mkdir -p "$NB_INST_TMP"
    _iid_r=$NB_INST_TMP/aleatorio
    dd if=/dev/urandom of="$_iid_r" bs=4 count=1 2> /dev/null
    dd if="$_iid_r" of="$1" bs=1 seek=440 count=4 conv=notrunc 2> /dev/null
    dd if=/dev/urandom of="$_iid_r" bs=4 count=1 2> /dev/null
    dd if="$_iid_r" of="$1" bs=1 seek=$(($2 * 512 + 67)) count=4 conv=notrunc 2> /dev/null
    dd if="$_iid_r" of="$1" bs=1 seek=$((($2 + 6) * 512 + 67)) count=4 conv=notrunc 2> /dev/null
    rm -f "$_iid_r"
}

# O swap por um loop sobre o pedaço do disco: o mkswap não tem deslocamento.
nb_inst_swap() {
    _isw_l=$(losetup -o $(($2 * 512)) --sizelimit $(($3 * 512)) -f --show "$1" 2> /dev/null) ||
        return 1
    mkswap -L NB3SWAP "$_isw_l" > /dev/null 2>&1
    _isw_rc=$?
    losetup -d "$_isw_l" 2> /dev/null
    return $_isw_rc
}

# Grava a imagem no começo do disco e acrescenta as partições de dados e de
# swap. Formatar não depende de o kernel reler a tabela (com algo do sistema
# antigo preso ao disco a releitura falha): a ext4 sai pelo deslocamento no
# disco inteiro, e o swap por um loop. Em seguida a máquina reinicia.
# $1 disco, $2 imagem, $3 início da p1, $4 swap (setores; 0 = sem swap)
nb_inst_grava() {
    _igr_total=$(cat "$NB_INST_SYS_BLOCK/${1##*/}/size")
    _igr_isect=$(($(stat -c %s "$2") / 512))
    # o MBR só endereça 2 TiB: num disco maior, a instalação fica nos primeiros
    _igr_fim=$_igr_total
    [ "$_igr_fim" -gt 4294967295 ] && _igr_fim=4294967295
    _igr_fim=$((_igr_fim / 2048 * 2048))
    _igr_p2=$(((_igr_isect + 2047) / 2048 * 2048))
    _igr_p3=$(((_igr_fim - $4) / 2048 * 2048))
    _igr_p2tam=$((_igr_p3 - _igr_p2))
    [ "$_igr_p2tam" -gt 0 ] || return 1

    log_begin_msg "Writing the boot partition to $1"
    dd if="$2" of="$1" bs=1048576 conv=notrunc 2> /dev/null || return 1
    log_end_msg
    # o GPT reserva que o disco tinha no fim (o Windows deixa um): sem apagá-lo
    # há firmware e ferramenta que leem o disco como GPT, com a tabela antiga
    dd if=/dev/zero of="$1" bs=512 seek=$((_igr_total - 34)) count=34 conv=notrunc 2> /dev/null
    nb_inst_identidade_nova "$1" "$3"
    sync

    log_begin_msg "Creating the data and swap partitions"
    {
        printf 'start=%s, size=%s, type=83\n' "$_igr_p2" "$_igr_p2tam"
        [ "$4" -gt 0 ] && printf 'start=%s, size=%s, type=82\n' "$_igr_p3" "$4"
    } | sfdisk --append --no-reread --no-tell-kernel -q "$1" > /dev/null 2>&1 || return 1
    log_end_msg

    log_begin_msg "Formatting the data partition (ext4)"
    mke2fs.good -t ext4 -F -q -L NB3DATA -m 0 -E "offset=$((_igr_p2 * 512)),nodiscard" \
        "$1" "$((_igr_p2tam / 2))k" < /dev/null > /dev/null 2>&1 || return 1
    log_end_msg
    if [ "$4" -gt 0 ] && ! nb_inst_swap "$1" "$_igr_p3" "$4"; then
        nb_warn "could not prepare the swap partition: a swap file is used instead"
    fi
    sync
    # o kernel ver as partições novas agora é só enfeite: o próximo passo é
    # reiniciar
    blockdev --rereadpt "$1" 2> /dev/null
    return 0
}

# O wifi.conf da sede é o que está na RAM (o do pendrive, já limpo pelo
# bootstrap); o da imagem do servidor só tem os comentários. Sem isto a sede
# que boota por wifi instalaria um disco sem rede nenhuma, e o primeiro boot
# dele pararia na tela NO NETWORK. A imagem já foi conferida pelo md5: mexer
# nesta cópia local, antes de gravá-la, é seguro.
nb_inst_wifi_na_imagem() {
    _iwi_w=${NB_RUN:-/run/nutellaboot}/wifi.conf
    [ -s "$_iwi_w" ] || return 0
    mkdir -p "$NB_INST_IMGMNT"
    _iwi_l=$(losetup -o $(($2 * 512)) -f --show "$1" 2> /dev/null) || return 1
    _iwi_rc=1
    if mount -t vfat "$_iwi_l" "$NB_INST_IMGMNT" 2> /dev/null; then
        cp "$_iwi_w" "$NB_INST_IMGMNT/wifi.conf" && _iwi_rc=0
        sync
        umount "$NB_INST_IMGMNT" 2> /dev/null
    fi
    losetup -d "$_iwi_l" 2> /dev/null
    return $_iwi_rc
}

nb_inst_disco() {
    _iad_p1=$(nb_inst_p1 "$NB_INST_IMG")
    [ -n "$_iad_p1" ] || nb_inst_falhou "the installer file has no boot partition"
    nb_inst_wifi_na_imagem "$NB_INST_IMG" "$_iad_p1" ||
        nb_inst_falhou "could not put this site's wifi.conf on the disk"
    _iad_isect=$(($(stat -c %s "$NB_INST_IMG") / 512))
    # `nb3swap=` (GB, editado na linha do GRUB) muda o tamanho; 0 é sem swap
    _iad_gb=${nb3swap:-${NB_INST_SWAP_GB:-4}}
    case "$_iad_gb" in "" | *[!0-9]*) _iad_gb=4 ;; esac
    _iad_swap=$((_iad_gb * 2097152))
    # o mínimo: a imagem, o swap e os 30 GB livres que o boot exige
    # (30-storage.sh), mais 1 GB para o que a própria ext4 ocupa
    _iad_min=$((_iad_isect + _iad_swap + NB_MIN_FREE_KB * 2 + ${NB_INST_MARGEM:-2097152}))
    nb_inst_discos "$_iad_min" > "$NB_INST_TMP/discos"
    _iad_d=$(nb_inst_escolhe "$NB_INST_TMP/discos") ||
        nb_inst_sem_disco_screen "$NB_INST_TMP/discos"
    nb_inst_contagem "$_iad_d" "$NB_INST_TMP/discos"
    NB_INST_TOCOU=1
    nb_inst_grava "$NB_INST_DEV/$_iad_d" "$NB_INST_IMG" "$_iad_p1" "$_iad_swap" ||
        nb_inst_falhou "writing to /dev/$_iad_d failed"
    _iad_efi=
    if [ -d "$NB_INST_EFI" ]; then
        if nb_inst_efi_entrada "$NB_INST_DEV/$_iad_d" 1 '\EFI\BOOT\BOOTX64.EFI'; then
            _iad_efi="The firmware got a boot entry named '$NB_INST_LABEL'."
        else
            _iad_efi="The firmware refused the boot entry: if the disk does not start, pick it in the boot menu (F12)."
        fi
    fi
    nb_inst_fim "The whole disk /dev/$_iad_d is now Maratona Linux." "$_iad_efi"
}

# --- firmware (UEFI) ---

# Monta o efivarfs se ainda não estiver montado. Devolve 0 só quando montou
# agora: quem montou desmonta, ou o `mount --move /sys` do fim do initrd
# leva junto uma montagem a mais.
nb_inst_efivars() {
    grep -q ' efivarfs ' "$NB_INST_MOUNTS" 2> /dev/null && return 1
    mount -t efivarfs efivarfs "$NB_INST_EFI/efivars" 2> /dev/null
}

# Os números das entradas do firmware com o nosso rótulo, e só ele: o
# efibootmgr põe um TAB entre o rótulo e o caminho (ou nada, nos antigos), e
# "Maratona Linux 2" é de outra pessoa.
nb_inst_efi_nossas() {
    _ien_t=$(printf '\t')
    efibootmgr 2> /dev/null |
        sed -n "s/^Boot\([0-9A-Fa-f]\{4\}\)\*\{0,1\} $NB_INST_LABEL\($_ien_t.*\)\{0,1\}\$/\1/p"
}

# A entrada do firmware, criada ANTES de reiniciar; `efibootmgr -c` a põe em
# primeiro na ordem. As antigas com o mesmo rótulo saem antes.
# $1 disco, $2 número da partição, $3 caminho do carregador
nb_inst_efi_entrada() {
    _iee_m=
    nb_inst_efivars && _iee_m=1
    for _iee_n in $(nb_inst_efi_nossas); do
        efibootmgr -q -b "$_iee_n" -B > /dev/null 2>&1
    done
    efibootmgr -q -c -d "$1" -p "$2" -L "$NB_INST_LABEL" -l "$3" > /dev/null 2>&1
    _iee_rc=$?
    [ -n "$_iee_m" ] && umount "$NB_INST_EFI/efivars" 2> /dev/null
    return $_iee_rc
}

# --- modo lado ---

# As ESPs dos discos internos: partição do tipo EFI System (GPT c12a7328-…,
# ou 0xef no MBR), fora do disco de onde este boot veio.
nb_inst_esps() {
    _ies_boot=$(nb_inst_disco_do_boot)
    for _ies_d in "$NB_INST_SYS_BLOCK"/*; do
        _ies_n=${_ies_d##*/}
        [ "$_ies_n" = "$_ies_boot" ] && continue
        [ "$(cat "$_ies_d/removable" 2> /dev/null)" = 0 ] || continue
        case "$(readlink -f "$_ies_d" 2> /dev/null)" in */usb*) continue ;; esac
        for _ies_p in "$_ies_d/$_ies_n"*; do
            [ -e "$_ies_p/partition" ] || continue
            _ies_b=${_ies_p##*/}
            case "$(blkid -p -o value -s PART_ENTRY_TYPE "$NB_INST_DEV/$_ies_b" 2> /dev/null)" in
                c12a7328-f81f-11d2-ba4b-00a0c93ec93b | 0xef) echo "$_ies_b" ;;
            esac
        done
    done
}

# A ESP da instalação: a que tem o Windows; depois, a do mesmo disco da
# partição do sistema ($1); depois, a primeira.
nb_inst_acha_esp() {
    _iae_disco=$(nb_inst_disco_de "$1")
    _iae_win=
    _iae_mesmo=
    _iae_prim=
    mkdir -p "$NB_INST_ESPMNT"
    for _iae_b in $(nb_inst_esps); do
        [ -z "$_iae_prim" ] && _iae_prim=$_iae_b
        if [ -z "$_iae_mesmo" ] && [ "$(nb_inst_disco_de "$_iae_b")" = "$_iae_disco" ]; then
            _iae_mesmo=$_iae_b
        fi
        if [ -z "$_iae_win" ] && mount -t vfat -o ro "$NB_INST_DEV/$_iae_b" "$NB_INST_ESPMNT" 2> /dev/null; then
            [ -f "$NB_INST_ESPMNT/EFI/Microsoft/Boot/bootmgfw.efi" ] && _iae_win=$_iae_b
            umount "$NB_INST_ESPMNT" 2> /dev/null
        fi
    done
    printf '%s\n' "${_iae_win:-${_iae_mesmo:-$_iae_prim}}"
}

# O conf, o wifi.conf e o marcador num cpio (newc), que o GRUB entrega ao
# initrd como um segundo initrd: os três aparecem na raiz, como o conf do
# iPXE. O `newc:` do comando `initrd` do GRUB faria isso sozinho, mas o
# carregador EFI do GRUB do Fedora não o conhece ("invalid file name", visto
# em VM), e nada garante o de outra distribuição; arquivo a mais no initrd todo
# GRUB aceita. O cpio do initrd (o do klibc) só extrai: o formato vai escrito
# aqui, um cabeçalho de texto por arquivo.
# $1 = o arquivo; depois, pares "nome-dentro-do-cpio origem"
nb_inst_cpio() {
    _icp_saida=$1
    shift
    : > "$_icp_saida" || return 1
    _icp_i=0
    while [ $# -ge 2 ]; do
        _icp_i=$((_icp_i + 1))
        nb_inst_cpio_um "$1" "$2" "$_icp_i" >> "$_icp_saida" || return 1
        shift 2
    done
    nb_inst_cpio_um 'TRAILER!!!' /dev/null 0 >> "$_icp_saida"
}

nb_inst_cpio_um() {
    if [ "$1" = 'TRAILER!!!' ]; then
        _icu_t=0
        _icu_m=0
    else
        _icu_t=$(stat -c %s "$2") || return 1
        _icu_m=33188 # 0100644: arquivo comum
    fi
    _icu_n=$((${#1} + 1))
    printf '070701%08X%08X%08X%08X%08X%08X%08X%08X%08X%08X%08X%08X%08X' \
        "$3" "$_icu_m" 0 0 1 0 "$_icu_t" 0 0 0 0 "$_icu_n" 0
    printf '%s\000' "$1"
    nb_inst_zeros $(((4 - (110 + _icu_n) % 4) % 4))
    [ "$_icu_t" -gt 0 ] && cat "$2"
    nb_inst_zeros $(((4 - _icu_t % 4) % 4))
    return 0
}

nb_inst_zeros() {
    [ "$1" -gt 0 ] && dd if=/dev/zero bs=1 count="$1" 2> /dev/null
    return 0
}

# O menu da ESP. A linha do kernel é a do pendrive (nb3-genusb, CMDLINE_BASE;
# há teste que confere as duas); o conf, o wifi.conf e o marcador vão para o
# initrd no nb3conf.cpio.
# $1 UUID da partição do sistema, $2 = 1 quando a ESP tem o Windows
nb_inst_grubcfg_lado() {
    _igl_base="boot=nutellaboot noresume pcie_aspm=off net.ifnames=0 IMAGEROOT=$IMAGEROOT NB_SERVER=$NB_SERVER"
    # shellcheck disable=SC2016
    _igl_extra='$nb3esp/nb3conf.cpio'
    cat << EOF
# Gerado pelo instalador do NutellaBoot (client/stuff/27-install.sh): Maratona
# Linux ao lado do Windows. O GRUB e este menu moram na pasta EFI/nutellaboot
# da ESP; o kernel e o initrd, em nutellaboot/boot/ na partição do sistema.
set timeout=10
set default=0
insmod part_gpt
insmod part_msdos
insmod fat
insmod ntfs
insmod ext2
insmod chain
# esta pasta, pelo marcador: o \$cmdpath de uma imagem standalone chega vazio
set nb3espdev=""
search --no-floppy --file --set=nb3espdev /EFI/nutellaboot/nb3-disco
set nb3esp="(\$nb3espdev)/EFI/nutellaboot"
if [ -f "\$nb3esp/nutellaboot.conf" ]; then
	source "\$nb3esp/nutellaboot.conf"
fi
set nb3stor=""
EOF
    if [ -n "$1" ]; then
        printf 'search --no-floppy --fs-uuid --set=nb3stor %s\n' "$1"
    fi
    cat << EOF
if [ -z "\$nb3stor" ]; then
	search --no-floppy --file --set=nb3stor /nutellaboot/boot/nb3-disco
fi

menuentry "Maratona Linux — \$NB_SITE_NAME (\$IMAGEROOT)" {
	linux (\$nb3stor)/nutellaboot/boot/vmlinuz $_igl_base persistenthome=y
	initrd (\$nb3stor)/nutellaboot/boot/initrd.img $_igl_extra
}
menuentry "Maratona Linux — limpar a home (clean home)" {
	linux (\$nb3stor)/nutellaboot/boot/vmlinuz $_igl_base persistenthome=y cleanhome=y
	initrd (\$nb3stor)/nutellaboot/boot/initrd.img $_igl_extra
}
menuentry "Maratona Linux — modo live (sem home persistente)" {
	linux (\$nb3stor)/nutellaboot/boot/vmlinuz $_igl_base persistenthome=n
	initrd (\$nb3stor)/nutellaboot/boot/initrd.img $_igl_extra
}
EOF
    if [ "$2" = 1 ]; then
        cat << 'EOF'
menuentry "Windows" {
	search --no-floppy --file --set=nb3win /EFI/Microsoft/Boot/bootmgfw.efi
	chainloader ($nb3win)/EFI/Microsoft/Boot/bootmgfw.efi
}
EOF
    fi
    cat << EOF
menuentry "Remover o NutellaBoot deste computador (o Windows volta a ser o padrão)" {
	linux (\$nb3stor)/nutellaboot/boot/vmlinuz $_igl_base factoryreset=y
	initrd (\$nb3stor)/nutellaboot/boot/initrd.img $_igl_extra
}
EOF
}

nb_inst_sem_esp_screen() {
    nb_fatal_screen "NO ESP" \
        "!This disk has no EFI system partition. Nothing was changed." \
        "" \
        "Windows on this computer starts in legacy BIOS mode, so there" \
        "is no place for a UEFI boot entry next to it." \
        "" \
        "!What to do:" \
        "  -> keep starting this computer from the USB drive, or" \
        "  -> choose 'install on the whole disk' (it ERASES the disk)"
}

nb_inst_sem_espaco_screen() {
    nb_fatal_screen "NO ROOM" \
        "!The EFI system partition is full. Nothing was changed." \
        "" \
        "Free:   $1 KB" \
        "Needed: $2 KB (the boot loader of Maratona Linux)" \
        "" \
        "!What to do:" \
        "  -> keep starting this computer from the USB drive, or" \
        "  -> choose 'install on the whole disk' (it ERASES the disk)"
}

nb_inst_lado() {
    # a partição do sistema: a mesma que o boot de sempre escolheria
    nb_find_storage
    _ila_part=
    for _ila_d in $possibledisks; do
        if nb_mount_storage "$_ila_d"; then
            _ila_part=$_ila_d
            break
        fi
    done
    [ -n "$_ila_part" ] || nb_no_disk_screen
    _ila_esp=$(nb_inst_acha_esp "$_ila_part")
    [ -n "$_ila_esp" ] || nb_inst_sem_esp_screen

    # a imagem da sede, só para ler o que vai para o disco
    mkdir -p "$NB_INST_ESPMNT" "$NB_INST_IMGMNT"
    _ila_p1=$(nb_inst_p1 "$NB_INST_IMG")
    [ -n "$_ila_p1" ] || nb_inst_falhou "the installer file has no boot partition"
    _ila_loop=$(losetup -r -o $((_ila_p1 * 512)) -f --show "$NB_INST_IMG" 2> /dev/null) ||
        nb_inst_falhou "could not open the installer file"
    mount -t vfat -o ro "$_ila_loop" "$NB_INST_IMGMNT" 2> /dev/null ||
        nb_inst_falhou "could not open the installer file"
    mount -t vfat "$NB_INST_DEV/$_ila_esp" "$NB_INST_ESPMNT" 2> /dev/null ||
        nb_inst_falhou "could not open the EFI system partition /dev/$_ila_esp"

    # o GRUB tem ~13 MB, e a ESP do Windows costuma ter 100 MB. Uma instalação
    # anterior será substituída: o espaço dela conta como livre.
    _ila_dir=$NB_INST_ESPMNT/EFI/nutellaboot
    _ila_precisa=$(($(stat -c %s "$NB_INST_IMGMNT/EFI/BOOT/BOOTX64.EFI") / 1024 + 1024))
    _ila_livre=$(df -k "$NB_INST_ESPMNT" | awk 'END {print $4}')
    if [ -d "$_ila_dir" ]; then
        _ila_livre=$((_ila_livre + $(du -sk "$_ila_dir" | awk '{print $1}')))
    fi
    [ "$_ila_livre" -ge "$_ila_precisa" ] || nb_inst_sem_espaco_screen "$_ila_livre" "$_ila_precisa"

    NB_INST_TOCOU=1
    # o par: `.new` e `mv`, no mesmo sistema de arquivos
    mkdir -p "$STORAGEDIR/boot"
    for _ila_f in vmlinuz initrd.img; do
        cp "$NB_INST_IMGMNT/$_ila_f" "$STORAGEDIR/boot/$_ila_f.new" &&
            mv -f "$STORAGEDIR/boot/$_ila_f.new" "$STORAGEDIR/boot/$_ila_f" ||
            nb_inst_falhou "could not write $_ila_f to $_ila_part"
    done
    # é por este arquivo que o GRUB acha a partição se o UUID dela mudar
    echo "NutellaBoot 3 - instalado ao lado do Windows" > "$STORAGEDIR/boot/nb3-disco"

    mkdir -p "$_ila_dir"
    cp "$NB_INST_IMGMNT/EFI/BOOT/BOOTX64.EFI" "$_ila_dir/grubx64.efi" &&
        cp "$NB_INST_IMGMNT/nutellaboot.conf" "$_ila_dir/nutellaboot.conf" ||
        nb_inst_falhou "could not write to the EFI system partition"
    # o wifi.conf da sede é o que está na RAM (o deste pendrive, já limpo); a
    # imagem do servidor só tem os comentários
    if [ -s "${NB_RUN:-/run/nutellaboot}/wifi.conf" ]; then
        cp "${NB_RUN:-/run/nutellaboot}/wifi.conf" "$_ila_dir/wifi.conf"
    else
        cp "$NB_INST_IMGMNT/wifi.conf" "$_ila_dir/wifi.conf"
    fi
    echo "NutellaBoot 3 - instalado ao lado do Windows" > "$_ila_dir/nb3-disco"
    nb_inst_cpio "$_ila_dir/nb3conf.cpio" \
        nutellaboot.conf "$_ila_dir/nutellaboot.conf" \
        wifi.conf "$_ila_dir/wifi.conf" \
        nb3-disco "$_ila_dir/nb3-disco" ||
        nb_inst_falhou "could not write to the EFI system partition"
    _ila_win=
    [ -f "$NB_INST_ESPMNT/EFI/Microsoft/Boot/bootmgfw.efi" ] && _ila_win=1
    nb_inst_grubcfg_lado "$(blkid -o value -s UUID "$_ila_part" 2> /dev/null)" "$_ila_win" \
        > "$_ila_dir/grub.cfg" || nb_inst_falhou "could not write to the EFI system partition"
    sync
    umount "$NB_INST_IMGMNT" 2> /dev/null
    losetup -d "$_ila_loop" 2> /dev/null
    umount "$NB_INST_ESPMNT" 2> /dev/null
    umount "$BLOCKROOT" 2> /dev/null

    _ila_disco=$(nb_inst_disco_de "$_ila_esp")
    _ila_num=$(cat "$NB_SYS_CLASS_BLOCK/$_ila_esp/partition" 2> /dev/null)
    nb_inst_efi_entrada "$NB_INST_DEV/$_ila_disco" "$_ila_num" '\EFI\nutellaboot\grubx64.efi' ||
        nb_inst_falhou "the firmware refused the boot entry"
    nb_inst_fim "Windows stays: it is the last entry of the boot menu." \
        "The firmware got a boot entry named '$NB_INST_LABEL', now the first."
}

# Desfaz o modo `lado` no factory reset: a entrada do firmware e
# \EFI\nutellaboot\. O nutellaboot/, com o par, o nutella_findblock já apagou.
nb_inst_desinstala() {
    if [ -d "$NB_INST_EFI" ]; then
        _ide_m=
        nb_inst_efivars && _ide_m=1
        for _ide_n in $(nb_inst_efi_nossas); do
            efibootmgr -q -b "$_ide_n" -B > /dev/null 2>&1
        done
        [ -n "$_ide_m" ] && umount "$NB_INST_EFI/efivars" 2> /dev/null
    fi
    mkdir -p "$NB_INST_ESPMNT"
    for _ide_b in $(nb_inst_esps); do
        mount -t vfat "$NB_INST_DEV/$_ide_b" "$NB_INST_ESPMNT" 2> /dev/null || continue
        if [ -d "$NB_INST_ESPMNT/EFI/nutellaboot" ]; then
            nb_warn "removing the Maratona Linux boot loader from $_ide_b"
            rm -rf "$NB_INST_ESPMNT/EFI/nutellaboot"
        fi
        sync
        umount "$NB_INST_ESPMNT" 2> /dev/null
    done
}

# --- fim ---

nb_inst_fim() {
    nb_screen "DONE" \
        "!Maratona Linux was installed on this computer." \
        "" \
        "$1" \
        "${2:-}" \
        "" \
        "!Remove the USB drive now." \
        "From the next start on, this computer starts from its own disk" \
        "and downloads the system there. Nothing else to do."
    sleep "${NB_INST_FIM_WAIT:-15}"
    reboot -f
    panic=60 panic "installed, but the machine did not restart"
}
