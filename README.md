# Interway Sync pour Proxmox

Crée un LXC `140`, se connecte à Planning Tech Web et synchronise les périodes occupées vers l'agenda Google principal `GodeloupF`, ensuite repris par Odoo. Les journées FDJ et les week-ends sont bloqués de 08:00 à 16:30, les matinées de 08:00 à 12:00 et les après-midi de 12:30 à 16:30. Pour une demi-journée EPACK, seule l'intervention exacte est créée.

Dans le **Shell du serveur Proxmox**, colle cette ligne :

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/TheDrLucky/interway-sync/main/install-interway-sync.sh)"
```

La clé Google doit être présente sur Proxmox dans `/root/interway-google.json`. L'installation demande l'identifiant Interway puis le mot de passe, sans afficher celui-ci. Elle crée un LXC Debian à l'adresse `192.168.10.140`, avec 1 cœur, 1 Go de mémoire et 8 Go de disque, puis vérifie une première synchronisation. Le planning est ensuite actualisé à 06:00, 12:00 et 18:00, heure de Paris. Un événement absent est supprimé de Google Agenda seulement après deux contrôles consécutifs ; une erreur Interway ne déclenche aucune suppression.

Lors du prochain changement de mot de passe, relance exactement la même commande : elle mettra uniquement les identifiants à jour dans le LXC géré par Interway Sync.

Le résultat se trouve dans le LXC :

```text
/var/lib/interway-sync/planning.json
```

Le script refuse d'écraser un LXC existant qui n'appartient pas à Interway Sync. Si une nouvelle installation échoue, il supprime uniquement le LXC qu'il vient de créer.

Si le stockage Proxmox refuse la création du LXC non privilégié avec `Cannot open: Permission denied`, utilise le mode de compatibilité :

```bash
INTERWAY_PRIVILEGED=1 bash -c "$(curl -fsSL https://raw.githubusercontent.com/TheDrLucky/interway-sync/main/install-interway-sync.sh)"
```

## Vérification du code

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python -m unittest -v
```
