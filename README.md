requirements
- `incus`
- membership in hte `incus-admin` group
- incus initialized
- these cmds:
```
    sudo ufw allow in on <bridge>
    sudo ufw route allow in on <bridge>
    sudo ufw route allow out on <bridge>
```
- `gh` on host
- authenticated to an account w/ `gh auth login`
