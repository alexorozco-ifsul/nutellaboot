# Arquivos tirados da camada base publicada

Cópias fiéis, byte a byte, para os testes compararem com o que a máquina tem de
verdade, e não com o histórico do git de um pacote. Foram tiradas com
`unsquashfs -cat` da camada `maratonalinux2026-24.04.4.squash-2026-08-05-10-24`.

| Arquivo | Origem na camada |
|---|---|
| `maratona-firewall-20240530.sh` | `/usr/share/maratona-firewall/maratona-firewall-configuration.sh`, do pacote `maratona-firewall` 20240530 (github.com/maratona-linux/maratona-firewall, GPL) |
| `hosts-maratonalinux2026` | `/etc/hosts`: localhost, o nome da VM que construiu a base, os servidores do ntp.br e o do NutellaBoot |

Quando a base mudar, tire os arquivos de novo da camada nova.
