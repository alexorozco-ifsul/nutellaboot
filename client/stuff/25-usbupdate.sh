# shellcheck shell=sh
# O pendrive se atualiza sozinho quando fica para trás.
#
# Até aqui nada avisava que o pendrive estava velho: o sintoma era a tela
# "NO IMAGE" (initrd antigo lendo o formato novo do nutellaboot.conf), ou pior,
# um boot que funciona mas sem as correções da semana. Quem grava 45 pendrives
# não regrava 45 pendrives por causa de uma linha.
#
# REGRAS DESTE ARQUIVO, todas aprendidas doendo:
#
#   * a checagem NÃO tem poder de veto. Servidor fora do ar, resposta estranha,
#     initrd sem carimbo — segue o boot. Enfeite não derruba sala;
#   * o pendrive só é tocado depois de os arquivos novos estarem no disco local
#     E com o md5 conferido. A partição tem ~399 MB e o conteúdo 202: não cabem
#     os dois pares, então os antigos precisam sair antes dos novos entrarem, e
#     essa é a única janela em que o pendrive fica sem boot;
#   * uma tentativa por versão, marcada no disco local. Sem isso, um pendrive
#     protegido contra escrita reinicia a máquina para sempre;
#   * nutellaboot.conf e wifi.conf são da sede, não nossos. Não se toca.
#
# Instalado no disco (27-install.sh), o "pendrive" é outro lugar: no disco
# inteiro, a NB3CFG do próprio disco, gravada do mesmo jeito; ao lado do
# Windows, nutellaboot/boot/ na partição do sistema, onde a troca é um `mv`.

NB_USB_MNT=${NB_USB_MNT:-/nb3usb}
NB_USB_BYLABEL=${NB_USB_BYLABEL:-/dev/disk/by-label/NB3CFG}
NB_USB_BYUUID=${NB_USB_BYUUID:-/dev/disk/by-uuid}

# Lê a resposta de /boot/v3/<img>/usb. Devolve 0 só quando ela faz sentido.
nb_usb_server_build() {
    _uu_resp=$(nb_get "$NB_SERVER/boot/v3/$IMAGEROOT/usb" 2> /dev/null) || return 1
    [ -n "$_uu_resp" ] || return 1
    NB_USB_BUILD=$(printf '%s\n' "$_uu_resp" | sed -n 's/^BUILD //p' | sed -n 1p)
    [ -n "$NB_USB_BUILD" ] || return 1
    [ "$NB_USB_BUILD" = unknown ] && return 1
    # as demais linhas vêm no formato do manifest de camadas: MD5 ARQUIVO URL
    NB_USB_FILES=$(printf '%s\n' "$_uu_resp" | sed -n '/^BUILD /!p')
    [ -n "$NB_USB_FILES" ]
}

# Acha a partição de configuração. Ela foi desmontada no início do boot (o
# pendrive pode ter sido retirado desde então — o boot manda retirar), então
# procurar de novo é a única forma de saber se ainda está aqui.
#
# Primeiro a partição de onde o conf foi lido (o initrd a guarda em
# NB_CFG_DEV) ou a do UUID que o GRUB passou (`nb3cfg=`): com o pendrive
# espetado numa máquina já instalada no disco há duas NB3CFG, e o link por
# label é de quem o udev viu por último. Regravar a outra deixaria esta velha.
nb_usb_device() {
    if [ -n "${NB_CFG_DEV:-}" ] && [ -e "$NB_CFG_DEV" ]; then
        printf '%s' "$NB_CFG_DEV"
        return 0
    fi
    if [ -n "${nb3cfg:-}" ]; then
        _uu_u=$(printf '%s' "$nb3cfg" | tr 'a-f' 'A-F')
        if [ -e "$NB_USB_BYUUID/$_uu_u" ]; then
            readlink -f "$NB_USB_BYUUID/$_uu_u"
            return 0
        fi
    fi
    _uu_try=0
    while [ "$_uu_try" -lt "${NB_USB_TRIES:-3}" ]; do
        # o link do udev primeiro, o blkid de reserva — gêmeo do
        # nb_cfg_device do bootstrap: o `blkid -L` varre todo disco e fica
        # preso num SATA doente (e este laço escreve no pendrive depois)
        if [ -e "$NB_USB_BYLABEL" ]; then
            _uu_dev=$(readlink -f "$NB_USB_BYLABEL")
        else
            _uu_dev=$(blkid -L NB3CFG 2> /dev/null)
        fi
        if [ -n "$_uu_dev" ]; then
            printf '%s' "$_uu_dev"
            return 0
        fi
        sleep "${NB_USB_WAIT:-2}"
        _uu_try=$((_uu_try + 1))
    done
    return 1
}

nb_usb_sem_pendrive_screen() {
    nb_fatal_screen "OLD USB" \
        "!This computer booted from an out-of-date USB drive," \
        "!and the drive is no longer plugged in." \
        "" \
        "Site:   $IMAGEROOT" \
        "On the drive: ${NB_INITRD_BUILD:-unknown}" \
        "On the server: $NB_USB_BUILD" \
        "" \
        "!What to do:" \
        "  1. Plug the USB drive back in and turn this computer on again." \
        "     It updates itself, with no keyboard, and reboots." \
        "  2. Or write a fresh drive: the image is in the console of" \
        "     this site, under 'pendrive de boot'." \
        "" \
        "The same drive works for every site - only nutellaboot.conf changes."
}

nb_usb_falhou_screen() {
    if [ "${2:-usb}" != usb ]; then
        nb_fatal_screen "FAILED" \
            "!The boot files on this computer's disk could not be updated." \
            "" \
            "Reason: $1" \
            "Site:   $IMAGEROOT" \
            "" \
            "!What to do:" \
            "  -> start the computer again; if this screen comes back," \
            "     install again with the installer USB drive of this site" \
            "" \
            "On the next start it boots with the old files, without trying again."
    fi
    nb_fatal_screen "USB FAILED" \
        "!The USB drive could not be updated." \
        "" \
        "Reason: $1" \
        "Site:   $IMAGEROOT" \
        "" \
        "!What to do:" \
        "  1. The drive may be write-protected: some models have a small" \
        "     switch on the side." \
        "  2. Write a fresh drive with the image from the console of this" \
        "     site, under 'pendrive de boot'." \
        "" \
        "On the next start it boots with the old drive, without trying again."
}

# Baixa e confere ANTES de tocar no pendrive: quando os arquivos velhos saírem,
# os novos já têm que estar bons e locais.
nb_usb_stage() {
    _uu_dir=$1
    rm -rf "$_uu_dir"
    mkdir -p "$_uu_dir"
    printf '%s\n' "$NB_USB_FILES" | while read -r _uu_md5 _uu_file _uu_urls; do
        [ -z "$_uu_file" ] && continue
        NB_DL_PROGRESS=1
        NB_DL_LABEL="boot update: $_uu_file"
        export NB_DL_PROGRESS NB_DL_LABEL
        # shellcheck disable=SC2086
        nb_download "$_uu_dir/$_uu_file" 4 $_uu_urls || exit 1
        NB_DL_PROGRESS=0
        NB_DL_LABEL=
        nutella_md5sum "$_uu_md5" "$_uu_dir/$_uu_file" || exit 1
    done
}

# A janela de risco. Curta de propósito, e só depois de tudo conferido.
nb_usb_grava() {
    _uu_dir=$1
    _uu_dev=$2
    mkdir -p "$NB_USB_MNT"
    mount "$_uu_dev" "$NB_USB_MNT" 2> /dev/null || return 1
    for _uu_f in vmlinuz initrd.img; do
        rm -f "$NB_USB_MNT/$_uu_f"
    done
    sync
    for _uu_f in vmlinuz initrd.img; do
        log_begin_msg "Writing $_uu_f to $_uu_dev"
        if ! cp "$_uu_dir/$_uu_f" "$NB_USB_MNT/$_uu_f"; then
            log_failure_msg "could not write $_uu_f"
            umount "$NB_USB_MNT" 2> /dev/null
            return 1
        fi
        log_end_msg
    done
    sync
    umount "$NB_USB_MNT" 2> /dev/null
    return 0
}

# Ao lado do Windows o par mora em nutellaboot/boot/, na mesma partição onde
# ele acabou de ser baixado e conferido: a troca é um `mv`, sem a janela em
# que o pendrive fica sem boot.
nb_usb_grava_dir() {
    for _uu_f in vmlinuz initrd.img; do
        log_begin_msg "Writing $_uu_f to the disk"
        if ! mv -f "$1/$_uu_f" "$2/$_uu_f"; then
            log_failure_msg "could not write $_uu_f"
            return 1
        fi
        log_end_msg
    done
    sync
    return 0
}

# Partição num disco interno, só com prova: o caminho no sysfs sem `usb` e o
# disco-pai com `removable` 0 (o mesmo critério do bootstrap).
NB_SYS_CLASS_BLOCK=${NB_SYS_CLASS_BLOCK:-/sys/class/block}
nb_part_interna() {
    _upi_p=$(readlink -f "$NB_SYS_CLASS_BLOCK/${1##*/}" 2> /dev/null)
    [ -n "$_upi_p" ] && [ -d "$_upi_p" ] || return 1
    case "$_upi_p" in */usb*) return 1 ;; esac
    [ "$(cat "${_upi_p%/*}/removable" 2> /dev/null)" = 0 ]
}

# De onde veio este boot: `rede`, `lado` (instalado ao lado do Windows),
# `disco` (instalado no disco inteiro: a NB3CFG num disco interno) ou `usb`.
# A autoatualização e o factory reset fazem coisas diferentes em cada um.
nb_boot_origem() {
    if [ -n "${NB_NETBOOT:-}" ]; then
        echo rede
    elif [ -n "${NB_DISKBOOT:-}" ]; then
        echo lado
    elif [ -n "${NB_CFG_DEV:-}" ] && nb_part_interna "$NB_CFG_DEV"; then
        echo disco
    else
        echo usb
    fi
}

nb_usb_update() {
    # sem carimbo não há o que comparar (initrd anterior a esta mudança), e
    # `nousbupdate=y` na linha de comando é a escotilha para o dia em que a
    # prova já começou e não dá para esperar um reinício
    [ -z "${NB_INITRD_BUILD:-}" ] && return 0
    [ "${nousbupdate:-}" = y ] && return 0

    nb_usb_server_build || return 0
    [ "$NB_USB_BUILD" = "$NB_INITRD_BUILD" ] && return 0

    # Boot pela rede (o initrd achou o conf entregue pelo carregador): não há
    # pendrive nenhum. Kernel e initrd vêm do servidor de boot da sede, e só
    # quem cuida dele pode trocá-los — seguir daqui procuraria a NB3CFG e
    # pararia a sala inteira na tela OLD USB.
    if [ -n "${NB_NETBOOT:-}" ]; then
        nb_warn "the network boot files are out of date (${NB_INITRD_BUILD} -> ${NB_USB_BUILD}): copy the new vmlinuz and initrd.img to the boot server"
        return 0
    fi

    # o pendrive, a NB3CFG do disco instalado inteiro, ou o par em
    # nutellaboot/boot/ quando instalado ao lado do Windows
    _uu_origem=$(nb_boot_origem)
    if [ "$_uu_origem" = usb ]; then
        nb_warn "this USB drive is out of date (${NB_INITRD_BUILD} -> ${NB_USB_BUILD})"
    else
        nb_warn "the boot files on this computer's disk are out of date (${NB_INITRD_BUILD} -> ${NB_USB_BUILD})"
    fi

    # O disco local tem de estar montado AQUI: quem o monta é o mount_layers,
    # que roda depois. Antes disto o $STORAGEDIR era um diretório na RAM do
    # initrd, e o marcador e os arquivos baixados sumiam no reinício: um
    # pendrive protegido contra escrita baixava tudo de novo, falhava e
    # reiniciava a máquina para sempre. Visto em VM, com o pendrive em
    # readonly; o teste antigo não via porque o STORAGEDIR dele persistia.
    nb_find_storage
    _uu_disco=
    for _uu_d in $possibledisks; do
        nb_mount_storage "$_uu_d" && {
            _uu_disco=$_uu_d
            break
        }
    done
    if [ -z "$_uu_disco" ]; then
        # sem disco a máquina para no NO DISK logo adiante, que é a tela certa
        nb_warn "no local disk to prepare the USB update - skipping it"
        return 0
    fi

    # Uma tentativa de ESCRITA por versão. Tentada e falha (pendrive protegido
    # contra escrita, ou com defeito), parar de novo seria reiniciar a máquina
    # para sempre: segue com o pendrive velho, e o aviso diz o que fazer.
    _uu_marca=$STORAGEDIR/.usbupd-tried
    if [ "$(cat "$_uu_marca" 2> /dev/null)" = "$NB_USB_BUILD" ]; then
        nb_warn "already tried once to update this USB drive (write-protected?): booting with the old one - write a new drive from the console"
        umount "$BLOCKROOT" 2> /dev/null
        return 0
    fi

    _uu_dev=
    if [ "$_uu_origem" = lado ]; then
        if [ ! -d "$STORAGEDIR/boot" ]; then
            nb_warn "this disk has no nutellaboot/boot to update - skipping it"
            umount "$BLOCKROOT" 2> /dev/null
            return 0
        fi
        nb_screen "UPDATE" \
            "!The boot files on this computer's disk are out of date" \
            "!and are being updated now." \
            "" \
            "Do not turn the computer off. It reboots by itself when it finishes." \
            "" \
            "On the disk:   $NB_INITRD_BUILD" \
            "On the server: $NB_USB_BUILD"
    elif [ "$_uu_origem" = disco ]; then
        _uu_dev=$(nb_usb_device) || nb_usb_sem_pendrive_screen
        nb_screen "UPDATE" \
            "!The boot partition of this computer's disk is out of date" \
            "!and is being updated now." \
            "" \
            "Do not turn the computer off. It reboots by itself when it finishes." \
            "" \
            "On the disk:   $NB_INITRD_BUILD" \
            "On the server: $NB_USB_BUILD"
    else
        _uu_dev=$(nb_usb_device) || nb_usb_sem_pendrive_screen
    fi

    [ "$_uu_origem" = usb ] && nb_screen "USB UPDATE" \
        "!This USB drive is out of date and is being updated now." \
        "" \
        "Do not turn the computer off and do not remove the drive." \
        "It reboots by itself when it finishes." \
        "" \
        "On the drive: $NB_INITRD_BUILD" \
        "On the server: $NB_USB_BUILD"

    # Falha de download NÃO condena o pendrive: nada foi tocado ainda, e uma
    # queda de rede é transitória. Segue o boot com o initrd velho, que é o que
    # a máquina faria de qualquer forma, e tenta de novo no próximo. (Marcar
    # aqui foi a primeira versão disto, e teria transformado um blip de rede em
    # máquina condenada.)
    if ! nb_usb_stage "$STORAGEDIR/usbupd"; then
        nb_warn "the new boot files could not be downloaded - keeping the old USB drive"
        rm -rf "$STORAGEDIR/usbupd"
        umount "$BLOCKROOT" 2> /dev/null
        return 0
    fi

    # Daqui para baixo é irreversível: os arquivos antigos saem do pendrive
    # para os novos caberem. A marca é gravada ANTES, porque o que ela protege
    # é justamente a repetição desta parte.
    mkdir -p "$STORAGEDIR"
    echo "$NB_USB_BUILD" > "$_uu_marca"
    sync

    if [ "$_uu_origem" = lado ]; then
        nb_usb_grava_dir "$STORAGEDIR/usbupd" "$STORAGEDIR/boot" ||
            nb_usb_falhou_screen "writing to the disk failed" lado
    else
        nb_usb_grava "$STORAGEDIR/usbupd" "$_uu_dev" ||
            nb_usb_falhou_screen "writing to the drive failed" "$_uu_origem"
    fi
    rm -rf "$STORAGEDIR/usbupd"
    rm -f "$_uu_marca"
    sync

    _uu_feito="The USB drive was updated."
    [ "$_uu_origem" = usb ] || _uu_feito="The boot files on this computer's disk were updated."
    nb_screen "REBOOTING" \
        "!$_uu_feito" \
        "" \
        "This computer is restarting to use it. Nothing else to do." \
        "" \
        "New version: $NB_USB_BUILD"
    sleep "${NB_USB_REBOOT_WAIT:-8}"
    reboot -f
    # se o reboot não pegar, parar aqui é melhor que seguir: o pendrive agora
    # tem o initrd novo e esta máquina ainda está rodando o velho. Mesmo
    # idioma do nb_fatal (00-header.sh).
    panic=60 panic "the USB drive was updated but the machine did not restart"
}
