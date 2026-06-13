#!/usr/bin/env python
# Copyright (c) 2026 Jifeng Wu
# Licensed under the MIT License. See LICENSE file in the project root for full license information.

"""sshgit: git clone/fetch/pull/push over SSH using dulwich + paramiko."""

import argparse
import logging
import os
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from six.moves.urllib.parse import urlparse

import dulwich.client
import paramiko
import stat
from dulwich.client import SSHGitClient
from dulwich.index import build_index_from_tree
from dulwich.repo import Repo

# --- Python 2/3 compatibility ---
if sys.version_info[0] >= 3:
    fsdecode = os.fsdecode
else:
    def fsdecode(path_bytes):  # type: (bytes) -> str
        return path_bytes


def iter_tree_paths(store, tree):
    """Recursively walk a tree yielding entry paths (replaces iter_tree_contents)."""

    for name, mode, sha in tree.iteritems():
        if stat.S_ISDIR(mode):
            subtree = store[sha]
            for entry_path in iter_tree_paths(store, subtree):
                yield name + b"/" + entry_path
        else:
            yield name


def ensure_bytes(
    value, encoding="utf-8"
):  # type: (Union[str, bytes, None], str) -> Optional[bytes]
    """Convert a string to bytes, pass bytes through, or return None."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    return value.encode(encoding)


def ensure_str(
    value, encoding="utf-8"
):  # type: (Union[str, bytes, None], str) -> Optional[str]
    """Decode bytes to a string, pass strings through, or return None."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.decode(encoding)


class GitSSHError(Exception, object):
    __slots__ = ()

    pass


class LocalRepoWrapper(object):
    """High-level wrapper around a dulwich Repo for ref and tree operations."""

    __slots__ = ('repo',)

    HEADS = b"refs/heads/"
    TRACKING = b"refs/remotes/origin/"
    SCP_RE = re.compile(r"^([^@]+)@([^:]+):(.+)$")

    def __init__(self, repo):  # type: (Repo) -> None
        self.repo = repo

    @classmethod
    def init(cls, path):  # type: (str) -> LocalRepoWrapper
        return cls(Repo.init(path))

    @classmethod
    def open(cls, path):  # type: (str) -> LocalRepoWrapper
        return cls(Repo(path))

    # --- Branch helpers ---

    def current_branch(self):  # type: () -> bytes
        """Return the current branch name, or raise if HEAD is detached."""
        symrefs = self.repo.refs.get_symrefs()
        head_ref = symrefs.get(b"HEAD")
        if head_ref is None:
            raise GitSSHError("HEAD is detached")
        if not head_ref.startswith(self.HEADS):
            raise GitSSHError("unexpected HEAD ref: %s" % ensure_str(head_ref))
        return head_ref[len(self.HEADS) :]

    def branch_sha(self, name):  # type: (bytes) -> Optional[bytes]
        """Return the SHA for a local branch, or None."""
        return self.repo.refs.read_ref(self.HEADS + name)

    # --- Remote ref operations ---

    def resolve_remote_branch(
        self, refs, name
    ):  # type: (Dict[bytes, bytes], bytes) -> bytes
        """Look up a branch SHA in a remote refs dict, or raise."""
        ref = self.HEADS + name
        if ref not in refs:
            raise GitSSHError("branch '%s' not found on remote" % ensure_str(name))
        return refs[ref]

    def find_default_branch(
        self, refs
    ):  # type: (Dict[bytes, bytes]) -> Optional[bytes]
        """Determine the default branch name from remote HEAD."""
        head_sha = refs.get(b"HEAD")
        if head_sha is None:
            return None
        for ref, sha in refs.items():
            if sha == head_sha and ref.startswith(self.HEADS):
                return ref[len(self.HEADS) :]
        return b"main"

    def update_tracking_refs(self, refs):  # type: (Dict[bytes, bytes]) -> int
        """Write remote-tracking refs from remote heads. Returns update count."""
        updated = 0
        for ref, sha in refs.items():
            if ref.startswith(self.HEADS):
                tracking = self.TRACKING + ref[len(self.HEADS) :]
                if self.repo.refs.read_ref(tracking) != sha:
                    self.repo.refs[tracking] = sha
                    updated += 1
        return updated

    def prune_tracking_refs(self, refs):  # type: (Dict[bytes, bytes]) -> None
        """Remove tracking refs whose branch no longer exists on remote."""
        remote_branches = {
            ref[len(self.HEADS) :] for ref in refs if ref.startswith(self.HEADS)
        }
        for ref in list(self.repo.refs.allkeys()):
            if ref.startswith(self.TRACKING):
                branch = ref[len(self.TRACKING) :]
                if branch not in remote_branches:
                    del self.repo.refs[ref]
                    logging.info("  Pruned %s", ensure_str(ref))

    def set_tracking_ref(self, name, sha):  # type: (bytes, bytes) -> None
        self.repo.refs[self.TRACKING + name] = sha

    # --- Config ---

    def origin(self):  # type: () -> Tuple[str, int, str, str]
        """Return (host, port, username, path) parsed from origin URL."""
        try:
            url = ensure_str(self.repo.get_config().get((b"remote", b"origin"), b"url"))
        except KeyError:
            raise GitSSHError("no 'origin' remote found in .git/config")
        m = self.SCP_RE.match(url)
        if m:
            return m.group(2), 22, m.group(1), m.group(3)
        parsed = urlparse(url)
        if parsed.scheme != "ssh":
            raise GitSSHError(
                "unsupported URL scheme '%s' (only ssh:// is supported)" % parsed.scheme
            )
        if parsed.username is None:
            raise GitSSHError("no username found in remote URL")
        return parsed.hostname, parsed.port or 22, parsed.username, parsed.path

    def write_origin_config(
        self, host, port, username, remote_path
    ):  # type: (str, int, str, str) -> None
        """Write the [remote "origin"] section to repo config."""
        if port != 22:
            origin_url = "ssh://%s@%s:%d/%s" % (username, host, port, remote_path)
        else:
            origin_url = "%s@%s:%s" % (username, host, remote_path)
        config = self.repo.get_config()
        config.set((b"remote", b"origin"), b"url", ensure_bytes(origin_url))
        config.set(
            (b"remote", b"origin"), b"fetch", b"+refs/heads/*:refs/remotes/origin/*"
        )
        config.write_to_path()

    def setup_branch(self, name, sha):  # type: (bytes, bytes) -> None
        """Set local branch ref, HEAD, and tracking config."""
        local_ref = self.HEADS + name
        self.repo.refs[local_ref] = sha
        self.repo.refs.set_symbolic_ref(b"HEAD", local_ref)
        config = self.repo.get_config()
        config.set((b"branch", name), b"remote", b"origin")
        config.set((b"branch", name), b"merge", local_ref)
        config.write_to_path()

    # --- Tree / working copy ---

    def checkout(self, commit_sha):  # type: (bytes) -> None
        """Check out the tree of a commit to the working directory."""
        tree = self.repo[commit_sha].tree
        build_index_from_tree(
            self.repo.path,
            self.repo.index_path(),
            self.repo.object_store,
            tree,
        )

    def fast_forward(self, branch_name, new_sha):  # type: (bytes, bytes) -> None
        """Fast-forward a branch and update the working tree."""
        branch_ref = self.HEADS + branch_name
        old_sha = self.repo.refs.read_ref(branch_ref)

        if old_sha:
            ancestors = self.get_ancestors(new_sha)
            if old_sha not in ancestors:
                raise GitSSHError(
                    "cannot fast-forward: local branch has diverged; " "pull aborted"
                )

        new_tree = self.repo[new_sha].tree
        if old_sha:
            old_paths = set(
                iter_tree_paths(self.repo.object_store, self.repo[old_sha].tree)
            )
            new_paths = set(iter_tree_paths(self.repo.object_store, new_tree))
            for removed in old_paths - new_paths:
                full_path = os.path.join(self.repo.path, fsdecode(removed))
                if os.path.isfile(full_path):
                    os.remove(full_path)
                parent = os.path.dirname(full_path)
                while parent != self.repo.path:
                    if os.path.isdir(parent) and not os.listdir(parent):
                        os.rmdir(parent)
                        parent = os.path.dirname(parent)
                    else:
                        break

        self.repo.refs[branch_ref] = new_sha
        build_index_from_tree(
            self.repo.path,
            self.repo.index_path(),
            self.repo.object_store,
            new_tree,
        )

    # --- Remote interaction ---

    def fetch_from(
        self, client, path
    ):  # type: (SSHGitClient, str) -> Optional[Dict[bytes, bytes]]
        """Fetch objects from remote. Returns refs dict or None if up to date."""
        result = client.fetch(path, self.repo)
        if result is None:
            return None
        return result.refs

    def push_to(
        self,
        client,
        path,
        refspec,
        force=False,
        tags=False,
    ):  # type: (SSHGitClient, str, bytes, bool, bool) -> bytes
        """Push a branch to remote. Returns the pushed SHA."""
        local_sha = self.branch_sha(refspec)
        if local_sha is None:
            raise GitSSHError("local branch '%s' not found" % ensure_str(refspec))

        branch_ref = self.HEADS + refspec

        def update_refs(old_refs):  # type: (Dict[bytes, bytes]) -> Dict[bytes, bytes]
            refs_to_update = {}  # type: Dict[bytes, bytes]
            if tags:
                for ref in self.repo.refs.allkeys():
                    if ref.startswith(b"refs/tags/"):
                        refs_to_update[ref] = self.repo.refs[ref]
            old_remote_sha = old_refs.get(branch_ref)
            if old_remote_sha and not force:
                ancestors = self.get_ancestors(local_sha)
                if old_remote_sha not in ancestors:
                    raise GitSSHError(
                        "non-fast-forward update rejected; " "use --force to override"
                    )
            refs_to_update[branch_ref] = local_sha
            return refs_to_update

        def generate_pack_data(
            have,
            want,
            ofs_delta=False,
            progress=None,
        ):  # type: (List[bytes], List[bytes], bool, Optional[Callable[..., Any]]) -> Any
            return self.repo.object_store.generate_pack_data(
                have, want, ofs_delta=ofs_delta, progress=progress
            )

        client.send_pack(path, update_refs, generate_pack_data)
        self.set_tracking_ref(refspec, local_sha)
        return local_sha

    # --- Internal ---

    def get_ancestors(self, commit_sha):  # type: (bytes) -> Set[bytes]
        ancestors = set()  # type: Set[bytes]
        stack = [commit_sha]  # type: List[bytes]
        while stack:
            sha = stack.pop()
            if sha in ancestors:
                continue
            ancestors.add(sha)
            try:
                commit = self.repo[sha]
                stack.extend(commit.parents)
            except KeyError:
                pass
        return ancestors


# --- SSH ---


def get_pkey(
    ed25519_key=None,
    rsa_key=None,
):  # type: (Optional[str], Optional[str]) -> Optional[paramiko.PKey]
    """Load a private key from file."""
    if ed25519_key:
        return paramiko.Ed25519Key.from_private_key_file(ed25519_key)
    if rsa_key:
        return paramiko.RSAKey.from_private_key_file(rsa_key)
    return None


def connect_ssh(
    hostname,
    port,
    username,
    password=None,
    pkey=None,
    sock=None,
):  # type: (str, int, str, Optional[str], Optional[paramiko.PKey], Optional[paramiko.Channel]) -> paramiko.SSHClient
    """Connect to an SSH host and return the client."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = {  # type: Dict[str, Any]
        "hostname": hostname,
        "port": port,
        "username": username,
        "allow_agent": False,
        "look_for_keys": False,
    }
    if pkey:
        connect_kwargs["pkey"] = pkey
    elif password:
        connect_kwargs["password"] = password
    if sock:
        connect_kwargs["sock"] = sock
    client.connect(**connect_kwargs)
    return client


class ParamikoSSHVendor(object):
    """SSH vendor that uses paramiko with explicit credentials."""

    __slots__ = ('host', 'username', 'port', 'password', 'pkey', 'bastion')

    def __init__(
        self,
        host,
        username,
        port=22,
        password=None,
        pkey=None,
        bastion=None,
    ):  # type: (str, str, int, Optional[str], Optional[paramiko.PKey], Optional[paramiko.SSHClient]) -> None
        self.host = host  # type: str
        self.username = username  # type: str
        self.port = port  # type: int
        self.password = password  # type: Optional[str]
        self.pkey = pkey  # type: Optional[paramiko.PKey]
        self.bastion = bastion  # type: Optional[paramiko.SSHClient]
        if self.pkey is None and self.password is None:
            raise GitSSHError("pkey or password is required")

    def connect(self):  # type: () -> paramiko.SSHClient
        sock = None
        if self.bastion:
            transport = self.bastion.get_transport()
            sock = transport.open_channel(
                "direct-tcpip", (self.host, self.port), ("", 0)
            )
        return connect_ssh(
            self.host,
            self.port,
            self.username,
            password=self.password,
            pkey=self.pkey,
            sock=sock,
        )

    def run_command(
        self,
        host,
        command,
        username=None,
        port=None,
        password=None,
        key_filename=None,
        **kwargs
    ):  # type: (str, str, Optional[str], Optional[int], Optional[str], Optional[str], **Any) -> ParamikoWrapper
        client = self.connect()
        channel = client.get_transport().open_session()
        channel.exec_command(command)
        return ParamikoWrapper(client, channel)


class ParamikoWrapper(object):
    """Wraps a paramiko channel to satisfy dulwich's SSH interface."""

    __slots__ = ('ssh_client', 'ssh_channel', 'rfile', 'wfile', 'stderr', 'can_read')

    def __init__(
        self, client, channel
    ):  # type: (paramiko.SSHClient, paramiko.Channel) -> None
        self.ssh_client = client  # type: paramiko.SSHClient
        self.ssh_channel = channel  # type: paramiko.Channel
        self.rfile = channel.makefile("rb")
        self.wfile = channel.makefile("wb")
        self.stderr = channel.makefile_stderr("rb")
        self.can_read = channel.recv_ready  # type: Callable[[], bool]

    def read(self, n):  # type: (int) -> bytes
        return self.rfile.read(n)

    def write(self, data):  # type: (bytes) -> None
        self.wfile.write(data)
        self.wfile.flush()

    @property
    def reader(self):  # type: () -> ParamikoWrapper
        return self

    @property
    def writer(self):  # type: () -> ParamikoWrapper
        return self

    def close(self):  # type: () -> None
        self.rfile.close()
        self.wfile.close()
        self.ssh_channel.close()
        self.ssh_client.close()


class AsDulwichSshVendor(object):
    """Context manager that installs a ParamikoSSHVendor as dulwich's SSH vendor."""

    __slots__ = ('vendor', 'prev_vendor')

    def __init__(self, vendor):  # type: (ParamikoSSHVendor) -> None
        self.vendor = vendor
        self.prev_vendor = None  # type: Optional[Callable]

    def __enter__(self):  # type: () -> SSHGitClient
        self.prev_vendor = dulwich.client.get_ssh_vendor
        dulwich.client.get_ssh_vendor = lambda: self.vendor
        return SSHGitClient(
            self.vendor.host, port=self.vendor.port, username=self.vendor.username
        )

    def __exit__(self, exc_type, exc_val, exc_tb):  # type: (Any, Any, Any) -> None
        dulwich.client.get_ssh_vendor = self.prev_vendor


# --- Commands ---


def cmd_clone(
    remote_path,
    dest,
    vendor,
    branch,
):  # type: (str, Optional[str], ParamikoSSHVendor, Optional[bytes]) -> None
    """Clone a remote repository over SSH."""
    if dest is None:
        dest = os.path.basename(remote_path.rstrip("/"))
        if dest.endswith(".git"):
            dest = dest[:-4]

    dest = os.path.abspath(dest)
    if os.path.exists(dest):
        raise GitSSHError("destination '%s' already exists" % dest)

    logging.info(
        "Cloning %s@%s:%s into %s...", vendor.username, vendor.host, remote_path, dest
    )

    os.makedirs(dest)
    local = LocalRepoWrapper.init(dest)

    with AsDulwichSshVendor(vendor) as client:
        refs = local.fetch_from(client, remote_path)

    if refs is None:
        raise GitSSHError("no refs received from remote")

    local.write_origin_config(vendor.host, vendor.port, vendor.username, remote_path)
    local.update_tracking_refs(refs)

    target_branch = branch
    if target_branch:
        head_sha = local.resolve_remote_branch(refs, target_branch)
    else:
        target_branch = local.find_default_branch(refs)
        if target_branch is None:
            logging.warning("remote HEAD not found, skipping checkout")
            return
        head_sha = refs.get(b"HEAD")

    local.setup_branch(target_branch, head_sha)
    local.checkout(head_sha)

    logging.info("Cloned into '%s' on branch '%s'.", dest, ensure_str(target_branch))


def cmd_fetch(
    vendor,
    prune,
):  # type: (ParamikoSSHVendor, bool) -> None
    """Fetch new objects and refs from the remote."""
    local = LocalRepoWrapper.open(os.getcwd())
    _, _, _, path = local.origin()

    logging.info("Fetching from %s:%s...", vendor.host, path)

    with AsDulwichSshVendor(vendor) as client:
        refs = local.fetch_from(client, path)

    if refs is None:
        logging.info("Already up to date.")
        return

    updated = local.update_tracking_refs(refs)

    if prune:
        local.prune_tracking_refs(refs)

    logging.info("Fetched. %d ref(s) updated.", updated)


def cmd_pull(
    vendor,
    branch,
):  # type: (ParamikoSSHVendor, Optional[bytes]) -> None
    """Fetch and fast-forward the current branch."""
    local = LocalRepoWrapper.open(os.getcwd())
    _, _, _, path = local.origin()

    current = local.current_branch()
    target_branch = branch or current

    logging.info(
        "Pulling %s from %s:%s...", ensure_str(target_branch), vendor.host, path
    )

    with AsDulwichSshVendor(vendor) as client:
        refs = local.fetch_from(client, path)

    if refs is None:
        logging.info("Already up to date.")
        return

    remote_sha = local.resolve_remote_branch(refs, target_branch)
    local.set_tracking_ref(target_branch, remote_sha)

    local_sha = local.branch_sha(current)
    if local_sha == remote_sha:
        logging.info("Already up to date.")
        return

    local.fast_forward(current, remote_sha)

    logging.info(
        "Fast-forwarded %s to %s.", ensure_str(current), ensure_str(remote_sha)[:8]
    )


def cmd_push(
    vendor,
    refspec,
    force,
    tags,
):  # type: (ParamikoSSHVendor, Optional[bytes], bool, bool) -> None
    """Push local commits to the remote."""
    local = LocalRepoWrapper.open(os.getcwd())
    _, _, _, path = local.origin()

    if refspec is None:
        try:
            refspec = local.current_branch()
        except GitSSHError:
            raise GitSSHError(
                "cannot determine branch to push "
                "(HEAD is detached and no refspec given)"
            )

    logging.info("Pushing %s to %s:%s...", ensure_str(refspec), vendor.host, path)

    with AsDulwichSshVendor(vendor) as client:
        local_sha = local.push_to(client, path, refspec, force=force, tags=tags)

    logging.info("Pushed %s (%s).", ensure_str(refspec), ensure_str(local_sha)[:8])


# --- CLI ---


def main():  # type: () -> None
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        prog="sshgit",
        description="Git remote operations (clone/fetch/pull/push) over SSH.",
    )
    shared_parser = argparse.ArgumentParser(add_help=False)
    auth_group = shared_parser.add_argument_group("authentication")
    auth_group.add_argument(
        "--ed25519-key", metavar="PATH", help="path to Ed25519 private key"
    )
    auth_group.add_argument("--rsa-key", metavar="PATH", help="path to RSA private key")
    auth_group.add_argument("--password", metavar="PASS", help="SSH password")
    proxy_group = shared_parser.add_argument_group("jump host")
    proxy_group.add_argument(
        "--proxy-host", metavar="HOST", help="jump host hostname or IP"
    )
    proxy_group.add_argument(
        "--proxy-port", type=int, default=22, metavar="PORT", help="jump host SSH port"
    )
    proxy_group.add_argument(
        "--proxy-username",
        metavar="USER",
        help="jump host username (defaults to target username)",
    )
    proxy_group.add_argument(
        "--proxy-ed25519-key", metavar="PATH", help="path to Ed25519 key for jump host"
    )
    proxy_group.add_argument(
        "--proxy-rsa-key", metavar="PATH", help="path to RSA key for jump host"
    )
    proxy_group.add_argument(
        "--proxy-password", metavar="PASS", help="jump host password"
    )

    subparsers = parser.add_subparsers(dest="command")

    clone_parser = subparsers.add_parser(
        "clone", help="clone a remote repository", parents=[shared_parser]
    )
    clone_parser.add_argument("remote_path", help="absolute path to repo on remote")
    clone_parser.add_argument("dest", nargs="?", help="local destination directory")
    clone_parser.add_argument("--host", required=True, help="remote hostname or IP")
    clone_parser.add_argument("--port", type=int, default=22, help="SSH port")
    clone_parser.add_argument("--username", required=True, help="SSH username")
    clone_parser.add_argument("--branch", help="branch to check out")

    fetch_parser = subparsers.add_parser(
        "fetch", help="fetch refs and objects from remote", parents=[shared_parser]
    )
    fetch_parser.add_argument(
        "--prune", action="store_true", help="remove stale remote-tracking refs"
    )

    pull_parser = subparsers.add_parser(
        "pull", help="fetch and fast-forward", parents=[shared_parser]
    )
    pull_parser.add_argument("branch", nargs="?", help="branch to pull")

    push_parser = subparsers.add_parser(
        "push", help="push local commits to remote", parents=[shared_parser]
    )
    push_parser.add_argument("refspec", nargs="?", help="branch to push")
    push_parser.add_argument(
        "--force", action="store_true", help="allow non-fast-forward updates"
    )
    push_parser.add_argument("--tags", action="store_true", help="push all tags")

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        raise SystemExit(2)

    if not (args.ed25519_key or args.rsa_key or args.password):
        raise GitSSHError("one of --ed25519-key, --rsa-key, or --password is required")
    pkey = get_pkey(args.ed25519_key, args.rsa_key)

    if args.command == "clone":
        host = args.host
        port = args.port
        username = args.username
    else:
        local = LocalRepoWrapper.open(os.getcwd())
        host, port, username, _ = local.origin()

    bastion = None  # type: Optional[paramiko.SSHClient]
    if args.proxy_host:
        proxy_pkey = get_pkey(args.proxy_ed25519_key, args.proxy_rsa_key)
        bastion = connect_ssh(
            hostname=args.proxy_host,
            port=args.proxy_port,
            username=args.proxy_username or username,
            password=args.proxy_password or args.password,
            pkey=proxy_pkey or pkey,
        )

    vendor = ParamikoSSHVendor(
        host=host,
        port=port,
        username=username,
        password=args.password,
        pkey=pkey,
        bastion=bastion,
    )

    if args.command == "clone":
        cmd_clone(
            remote_path=args.remote_path,
            dest=args.dest,
            vendor=vendor,
            branch=ensure_bytes(args.branch),
        )
    elif args.command == "fetch":
        cmd_fetch(vendor=vendor, prune=args.prune)
    elif args.command == "pull":
        cmd_pull(
            vendor=vendor,
            branch=ensure_bytes(args.branch),
        )
    elif args.command == "push":
        cmd_push(
            vendor=vendor,
            refspec=ensure_bytes(args.refspec),
            force=args.force,
            tags=args.tags,
        )


if __name__ == "__main__":
    main()
