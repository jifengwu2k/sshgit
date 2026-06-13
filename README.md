# sshgit

Git remote operations (clone, fetch, pull, push) over SSH. Built on [dulwich](https://www.dulwich.io/) and [paramiko](https://www.paramiko.org/).

## Installation

```bash
pip install sshgit
```

## Usage

### `sshgit clone <remote-path> [dest]`

Clone a remote repository over SSH.

```
sshgit clone /home/me/project.git --host devbox --username me
sshgit clone /srv/repos/app.git --host 10.0.1.5 --port 2222 --ed25519-key ~/.ssh/id_ed25519
sshgit clone /home/me/repo.git ./local-repo --host myserver --username deploy --password
sshgit clone /opt/repos/lib.git --host bastion --rsa-key ~/.ssh/id_rsa --branch main
```

| Flag | Description |
|------|-------------|
| `--host HOST` | Remote hostname or IP (required) |
| `--username USER` | SSH username (required) |
| `--port PORT` | SSH port (default: `22`) |
| `--password PASS` | SSH password |
| `--ed25519-key PATH` | Path to Ed25519 private key |
| `--rsa-key PATH` | Path to RSA private key |
| `--branch NAME` | Branch to check out (default: remote HEAD) |

### `sshgit fetch [refspec]`

Fetch objects and refs from the remote. Reads connection details from `.git/config`.

```
sshgit fetch
sshgit fetch main
sshgit fetch --prune
```

| Flag | Description |
|------|-------------|
| `--prune` | Remove stale remote-tracking refs |

### `sshgit pull [branch]`

Fetch and fast-forward the current branch. Refuses if the branch has diverged.

```
sshgit pull
sshgit pull main
```

### `sshgit push [refspec]`

Push local commits to the remote.

```
sshgit push
sshgit push main
sshgit push --force
sshgit push --tags
```

| Flag | Description |
|------|-------------|
| `--force` | Allow non-fast-forward updates |
| `--tags` | Push all tags |

### Authentication

One of `--ed25519-key`, `--rsa-key`, or `--password` is required on every invocation. No implicit agent or config file lookup.

| Flag | Description |
|------|-------------|
| `--ed25519-key PATH` | Path to Ed25519 private key |
| `--rsa-key PATH` | Path to RSA private key |
| `--password PASS` | SSH password |

### Jump Host (Proxy)

All commands accept these optional flags for connecting through a bastion host:

| Flag | Description |
|------|-------------|
| `--proxy-host HOST` | Jump host hostname or IP |
| `--proxy-port PORT` | Jump host SSH port (default: `22`) |
| `--proxy-username USER` | Jump host username (defaults to target username) |
| `--proxy-ed25519-key PATH` | Ed25519 key for jump host |
| `--proxy-rsa-key PATH` | RSA key for jump host |
| `--proxy-password PASS` | Jump host password |

## Contributing

Contributions are welcome! Please submit pull requests or open issues on the GitHub repository.

## License

This project is licensed under the [MIT License](LICENSE).
