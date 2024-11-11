"""TUF metadata repository"""


from fnmatch import fnmatch
from functools import reduce
import json
import operator
import os
from pathlib import Path
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import shutil
from typing import Dict, List, Optional, Set, Tuple, Union
from securesystemslib.exceptions import StorageError
from cryptography.hazmat.primitives import serialization

from securesystemslib.signer import Signer

from taf import YubikeyMissingLibrary
try:
    import taf.yubikey as yk
except ImportError:
    yk = YubikeyMissingLibrary()  # type: ignore

from taf.constants import DEFAULT_RSA_SIGNATURE_SCHEME
from taf.utils import default_backend, get_file_details, on_rm_error
from tuf.api.metadata import (
    Metadata,
    MetaFile,
    Role,
    Root,
    Snapshot,
    Targets,
    TargetFile,
    Timestamp,
    DelegatedRole,
    Delegations,
)
from tuf.api.serialization.json import JSONSerializer
from taf.exceptions import InvalidKeyError, SigningError, TAFError, TargetsError
from taf.models.types import RolesIterator, RolesKeysData
from taf.tuf.keys import SSlibKey, _get_legacy_keyid, get_sslib_key_from_value
from tuf.repository import Repository

from securesystemslib.signer import CryptoSigner


logger = logging.getLogger(__name__)

# TODO remove this, use from constants or remove from constants
METADATA_DIRECTORY_NAME = "metadata"
TARGETS_DIRECTORY_NAME = "targets"

MAIN_ROLES = ["root", "targets", "snapshot", "timestamp"]

DISABLE_KEYS_CACHING = False
HASH_FUNCTION = "sha256"


def get_role_metadata_path(role: str) -> str:
    return f"{METADATA_DIRECTORY_NAME}/{role}.json"


def get_target_path(target_name: str) -> str:
    return f"{TARGETS_DIRECTORY_NAME}/{target_name}"


def is_delegated_role(role: str) -> bool:
    return role not in ("root", "targets", "snapshot", "timestamp")


def is_auth_repo(repo_path: str) -> bool:
    """Check if the given path contains a valid TUF repository"""
    try:
        Repository(repo_path)._repository
        return True
    except Exception:
        return False

class MetadataRepository(Repository):
    """TUF metadata repository implementation for on-disk top-level roles.

    Provides methods to read and edit metadata, handling version and expiry
    bumps, and signature creation, and facilitating snapshot and timestamp
    creation.

    Arguments:
        path: Base path of metadata repository.

    Attributes:
        signer_cache: All signers available to the repository. Keys are role
            names, values are lists of signers. On `close` each signer for a
            role is used to sign the related role metadata.
    """

    expiration_intervals = {"root": 365, "targets": 90, "snapshot": 7, "timestamp": 1}

    serializer = JSONSerializer(compact=False)

    # TODO - what is this?
    # @property
    # def repo_id(self):
    #     return GitRepository(path=self.path).initial_commit

    @property
    def certs_dir(self):
        certs_dir = self.path / "certs"
        certs_dir.mkdir(parents=True, exist_ok=True)
        return str(certs_dir)

    def __init__(self, path: Union[Path, str]) -> None:
        self.signer_cache: Dict[str, Dict[str, Signer]] = defaultdict(dict)
        self.path = Path(path)

        self._snapshot_info = MetaFile(1)
        self._targets_infos: Dict[str, MetaFile] = defaultdict(lambda: MetaFile(1))

    @property
    def metadata_path(self) -> Path:
        return self.path / METADATA_DIRECTORY_NAME

    @property
    def targets_path(self):
        return self.path / TARGETS_DIRECTORY_NAME

    @property
    def targets_infos(self) -> Dict[str, MetaFile]:
        # tracks targets and root metadata changes, needed in `do_snapshot`
        return self._targets_infos

    @property
    def snapshot_info(self) -> MetaFile:
        # tracks snapshot metadata changes, needed in `do_timestamp`
        return self._snapshot_info


    def all_target_files(self):
        """
        Return a set of relative paths of all files inside the targets
        directory
        """
        targets = []
        # Assume self.targets_path is a Path object, or convert it if necessary
        base_path = Path(self.targets_path)

        for filepath in base_path.rglob('*'):
            if filepath.is_file():
                # Get the relative path to the base directory and convert it to a POSIX path
                relative_path = filepath.relative_to(base_path).as_posix()
                targets.append(relative_path)

        return set(targets)

    def add_metadata_keys(self, roles_signers: Dict[str, Signer], roles_keys: Dict[str, List]) -> Tuple[Dict, Dict, Dict]:
        """Add signer public keys for role to root and update signer cache.

        Return:
            added_keys, already_added_keys, invalid_keys
        """
        already_added_keys = defaultdict(list)
        invalid_keys = defaultdict(list)
        added_keys = defaultdict(list)

        def _filter_if_can_be_added(roles):
            keys_to_be_added = defaultdict(list)
            for role, keys in roles_keys.items():
                if role in roles:
                    for key in keys:
                        try:
                            if self.is_valid_metadata_key(role, key):
                                already_added_keys[role].append(key)
                                continue
                        except TAFError:
                            invalid_keys[role].append(key)
                            continue
                        keys_to_be_added[role].append(key)
            return keys_to_be_added

        # when a key is added to one of the main roles
        # root is modified
        keys_to_be_added_to_root = _filter_if_can_be_added(MAIN_ROLES)
        if keys_to_be_added_to_root:
            with self.edit_root() as root:
                for role, keys in keys_to_be_added_to_root.items():
                    for key in keys:
                        root.add_key(key, role)
                        added_keys[role].append(key)

        other_roles = [role for role in roles_keys if role not in MAIN_ROLES]
        keys_to_be_added_to_targets = _filter_if_can_be_added(other_roles)

        roles_by_parents = defaultdict(list)
        if keys_to_be_added_to_targets:
            # group other roles by parents
            for role, keys in keys_to_be_added_to_targets.items():
                parent = self.find_delegated_roles_parent(role)
                roles_by_parents[parent].append(role)

            for parent, roles in roles_by_parents.items():
                with self.edit(parent) as parent_role:
                    for role in roles:
                        keys = roles_keys[role]
                        for key in keys:
                            parent_role.add_key(key, role)
                            added_keys[role].append(key)

        if keys_to_be_added_to_root or keys_to_be_added_to_targets:
            for role, signers in roles_signers.items():
                for signer in signers:
                    key = signer.public_key
                    self.signer_cache[role][key.keyid] = signer

            # Make sure the targets role gets signed with its new key, even though
            # it wasn't updated itself.
            if "targets" in added_keys and "targets" not in roles_by_parents:
                with self.edit_targets():
                    pass
            # TODO should this be done, what about other roles? Do we want that?

            # TODO move this to a function that calls this function
            self.do_snapshot()
            self.do_timestamp()
        return added_keys, already_added_keys, invalid_keys


    def add_target_files_to_role(self, added_data: Dict[str, Dict]) -> None:
        """Add target files to top-level targets metadata.
        Args:
            - added_data(dict): Dictionary of new data whose keys are target paths of repositories
                    (as specified in targets.json, relative to the targets dictionary).
                    The values are of form:
                    {
                        target: content of the target file
                        custom: {
                            custom_field1: custom_value1,
                            custom_field2: custom_value2
                        }
                    }
        """
        self.modify_targets(added_data=added_data)

        self.do_snapshot()
        self.do_timestamp()

    def open(self, role: str) -> Metadata:
        """Read role metadata from disk."""
        try:
            return Metadata.from_file(self.metadata_path / f"{role}.json")
        except StorageError:
            raise TAFError(f"Metadata file {self.metadata_path} does not exist")

    def check_if_role_exists(self, role_name: str) -> bool:
        role = self._role_obj(role_name)
        return role is not None

    def check_roles_expiration_dates(
        self, interval:Optional[int]=None, start_date:Optional[datetime]=None, excluded_roles:Optional[List[str]]=None
    ) -> Tuple[Dict, Dict]:
        """Determines which metadata roles have expired, or will expire within a time frame.
        Args:
        - interval(int): Number of days to look ahead for expiration.
        - start_date(datetime): Start date to look for expiration.
        - excluded_roles(list): List of roles to exclude from the search.

        Returns:
        - A dictionary of roles that have expired, or will expire within the given time frame.
        Results are sorted by expiration date.
        """
        if start_date is None:
            start_date = datetime.now(timezone.utc)
        if interval is None:
            interval = 30
        expiration_threshold = start_date + timedelta(days=interval)

        if excluded_roles is None:
            excluded_roles = []

        target_roles = self.get_all_targets_roles()
        main_roles = ["root", "targets", "snapshot", "timestamp"]
        existing_roles = list(set(target_roles + main_roles) - set(excluded_roles))

        expired_dict = {}
        will_expire_dict = {}
        for role in existing_roles:
            expiry_date = self.get_expiration_date(role)
            if start_date > expiry_date:
                expired_dict[role] = expiry_date
            elif expiration_threshold >= expiry_date:
                will_expire_dict[role] = expiry_date
        # sort by expiry date
        expired_dict = {
            k: v for k, v in sorted(expired_dict.items(), key=lambda item: item[1])
        }
        will_expire_dict = {
            k: v for k, v in sorted(will_expire_dict.items(), key=lambda item: item[1])
        }

        return expired_dict, will_expire_dict

    def _create_target_file(self, target_path, target_data):
        # if the target's parent directory should not be "targets", create
        # its parent directories if they do not exist
        target_dir = target_path.parents[0]
        target_dir.mkdir(parents=True, exist_ok=True)

        # create the target file
        content = target_data.get("target", None)
        if content is None:
            if not target_path.is_file():
                target_path.touch()
        else:
            with open(str(target_path), "w") as f:
                if isinstance(content, dict):
                    json.dump(content, f, indent=4)
                else:
                    f.write(content)

    def close(self, role: str, md: Metadata) -> None:
        """Bump version and expiry, re-sign, and write role metadata to disk."""

        # expiration date is updated before close is called
        md.signed.version += 1

        md.signatures.clear()
        for signer in self.signer_cache[role].values():
            md.sign(signer, append=True)

        fname = f"{role}.json"

        # Track snapshot, targets and root metadata changes, needed in
        # `do_snapshot` and `do_timestamp`
        if role == "snapshot":
            self._snapshot_info.version = md.signed.version
        elif role != "timestamp":  # role in [root, targets, <delegated targets>]
            self._targets_infos[fname].version = md.signed.version

        # Write role metadata to disk (root gets a version-prefixed copy)
        md.to_file(self.metadata_path / fname, serializer=self.serializer)

        if role == "root":
            md.to_file(self.metadata_path / f"{md.signed.version}.{fname}")


    def create(self, roles_keys_data: RolesKeysData, signers: dict, additional_verification_keys: Optional[dict]=None):
        """Create a new metadata repository on disk.

        1. Create metadata subdir (fail, if exists)
        2. Create initial versions of top-level metadata
        3. Perform top-level delegation using keys from passed signers.

        Args:
            roles_keys_data: an object containing information about roles, their threshold, delegations etc.
            signers: A dictionary, where dict-keys are role names and values
                are dictionaries, where-dict keys are keyids and values
                are signers.
            additional_verification_keys: A dictionary where keys are names of roles and values are lists
                of public keys that should be registered as the corresponding role's keys, but the private
                keys are not available. E.g. keys exporeted from YubiKeys of maintainers who are not
                present at the time of the repository's creation
        """
        # TODO add verification keys
        # support yubikeys
        self.metadata_path.mkdir(parents=True)
        self.signer_cache  = defaultdict(dict)

        root = Root(consistent_snapshot=False)

        # Snapshot tracks targets and root versions. targets v1 is included by
        # default in snapshot v1. root must be added explicitly.
        sn = Snapshot()
        sn.meta["root.json"] = MetaFile(1)

        public_keys = {
            role_name: {
                 _get_legacy_keyid(signer.public_key): signer.public_key
              for signer in role_signers
             } for role_name, role_signers in signers.items()
        }
        if additional_verification_keys:
            for role_name, roles_public_keys in additional_verification_keys.items():
                for public_key in roles_public_keys:
                    key_id = _get_legacy_keyid(public_key)
                    if key_id not in public_keys[role_name]:
                        public_keys[role_name][key_id] = public_key


        for role in RolesIterator(roles_keys_data.roles, include_delegations=False):
            if not role.is_yubikey:
                if signers is None:
                    raise TAFError(f"Cannot setup role {role.name}. Keys not specified")
                for signer in signers[role.name]:
                    key_id = _get_legacy_keyid(signer.public_key)
                    self.signer_cache[role.name][key_id] = signer
                for public_key in public_keys[role.name].values():
                    root.add_key(public_key, role.name)
            root.roles[role.name].threshold = role.threshold

        targets = Targets()
        target_roles = {"targets": targets}
        delegations_per_parent = defaultdict(dict)
        for role in RolesIterator(roles_keys_data.roles.targets):
            if role.parent is None:
                continue
            parent = role.parent.name
            parent_obj = target_roles.get(parent)
            keyids = []
            for signer in signers[role.name]:
                self.signer_cache[role.name][key_id] = signer
            delegated_role = DelegatedRole(
                name=role.name,
                threshold=role.threshold,
                paths=role.paths,
                terminating=role.terminating,
                keyids=list(public_keys[role.name].keys()),
            )
            delegated_metadata = Targets()
            target_roles[role.name] = delegated_metadata
            delegations_per_parent[parent][role.name] = delegated_role
            sn.meta[f"{role.name}.json"] = MetaFile(1)

        for parent, role_data in delegations_per_parent.items():
            parent_obj = target_roles[parent]
            delegations = Delegations(roles=role_data, keys=public_keys[role.name])
            parent_obj.delegations = delegations

        for signed in [root, Timestamp(), sn, targets]:
            # Setting the version to 0 here is a trick, so that `close` can
            # always bump by the version 1, even for the first time
            self._set_default_expiration_date(signed)
            signed.version = 0  # `close` will bump to initial valid verison 1
            self.close(signed.type, Metadata(signed))

        for name, signed in target_roles.items():
            if name != "targets":
                self._set_default_expiration_date(signed)
                signed.version = 0  # `close` will bump to initial valid verison 1
                self.close(name, Metadata(signed))


    def add_delegation(self, role_data):
        pass

    def delete_unregistered_target_files(self, targets_role="targets"):
        """
        Delete all target files not specified in targets.json
        """
        target_files_by_roles = self.sort_roles_targets_for_filenames()
        if targets_role in target_files_by_roles:
            for file_rel_path in target_files_by_roles[targets_role]:
                if file_rel_path not in self.get_targets_of_role(targets_role):
                    (self.targets_path / file_rel_path).unlink()

    def find_delegated_roles_parent(self, delegated_role, parent=None):
        if parent is None:
            parent = "targets"

        parents = [parent]

        while parents:
            parent = parents.pop()
            for delegation in self.get_delegations_of_role(parent):
                if delegation == delegated_role:
                    return parent
                parents.append(delegation)
        return None

    def get_delegations_of_role(self, role_name):
        signed_obj = self._signed_obj(role_name)
        if signed_obj.delegations:
            return signed_obj.delegations.roles
        return []

    def get_keyids_of_role(self, role_name):
        role_obj = self._role_obj(role_name)
        return role_obj.keyids


    def get_targets_of_role(self, role_name):
        return self._signed_obj(role_name).targets

    def find_keys_roles(self, public_keys, check_threshold=True):
        """Find all roles that can be signed by the provided keys.
        A role can be signed by the list of keys if at least the number
        of keys that can sign that file is equal to or greater than the role's
        threshold
        """
        roles = []
        for role in MAIN_ROLES:
            roles.append((role, None))
        keys_roles = []
        key_ids = [_get_legacy_keyid(public_key) for public_key in public_keys]
        while roles:
            role_name, parent = roles.pop()
            role_obj = self._role_obj(role_name, parent)
            target_roles_key_ids = role_obj.keyids
            threshold = role_obj.threshold
            num_of_signing_keys = len(
                set(target_roles_key_ids).intersection(key_ids)
            )
            if (
                (not check_threshold and num_of_signing_keys >= 1)
                or num_of_signing_keys >= threshold
            ):
                keys_roles.append(role_name)

            if role_name not in MAIN_ROLES or role_name == "targets":
                for delegation in self.get_delegations_of_role(role_name):
                    roles.append((delegation, role_name))

        return keys_roles

    def find_associated_roles_of_key(self, public_key):
        """
        Find all roles whose metadata files can be signed by this key
        Threshold is not important, as long as the key is one of the signing keys
        """
        return self.find_keys_roles([public_key], check_threshold=False)

    def get_all_roles(self):
        """
        Return a list of all defined roles, main roles combined with delegated targets roles
        """
        all_target_roles = self.get_all_targets_roles()
        all_roles = ["root", "snapshot", "timestamp"] + all_target_roles
        return all_roles

    def get_all_targets_roles(self):
        """
        Return a list containing names of all target roles
        """
        target_roles = ["targets"]
        all_roles = []

        while target_roles:
            role = target_roles.pop()
            all_roles.append(role)
            for delegation in self.get_delegations_of_role(role):
                target_roles.append(delegation)

        return all_roles

    def get_all_target_files_state(self):
        """Create dictionaries of added/modified and removed files by comparing current
        file-system state with current signed targets (and delegations) metadata state.

        Args:
        - None
        Returns:
        - Dict of added/modified files and dict of removed target files (inputs for
          `modify_targets` method.)

        Raises:
        - None
        """
        added_target_files = {}
        removed_target_files = {}

        # current fs state
        fs_target_files = self.all_target_files()
        # current signed state
        signed_target_files = self.get_signed_target_files()

        # existing files with custom data and (modified) content
        for file_name in fs_target_files:
            target_file = self.targets_path / file_name
            _, hashes = get_file_details(str(target_file))
            # register only new or changed files
            if hashes.get(HASH_FUNCTION) != self.get_target_file_hashes(file_name):
                added_target_files[file_name] = {
                    "target": target_file.read_text(),
                    "custom": self.get_target_file_custom_data(file_name),
                }

        # removed files
        for file_name in signed_target_files - fs_target_files:
            removed_target_files[file_name] = {}

        return added_target_files, removed_target_files

    def get_expiration_date(self, role: str) -> datetime:
        meta_file = self._signed_obj(role)
        if meta_file is None:
            raise TAFError(f"Role {role} does not exist")

        date = meta_file.expires
        return date.replace(tzinfo=timezone.utc)

    def get_role_threshold(self, role: str, parent: Optional[str]=None ) -> int:
        """Get threshold of the given role

        Args:
        - role(str): TUF role (root, targets, timestamp, snapshot or delegated one)
        - parent_role(str): Name of the parent role of the delegated role. If not specified,
                            it will be set automatically, but this might be slow if there
                            are many delegations.

        Returns:
        Role's signatures threshold

        Raises:
        - TAFError if the role does not exist or if metadata files are invalid
        """
        role_obj = self._role_obj(role, parent)
        if role_obj is None:
            raise TAFError(f"Role {role} does not exist")
        return role_obj.threshold

    def get_role_paths(self, role, parent_role=None):
        """Get paths of the given role

        Args:
        - role(str): TUF role (root, targets, timestamp, snapshot or delegated one)
        - parent_role(str): Name of the parent role of the delegated role. If not specified,
                            it will be set automatically, but this might be slow if there
                            are many delegations.

        Returns:
        Defined delegated paths of delegate target role or * in case of targets

        Raises:
        - securesystemslib.exceptions.FormatError: If the arguments are improperly formatted.
        - securesystemslib.exceptions.UnknownRoleError: If 'rolename' has not been delegated by this
        """
        if role == "targets":
            return "*"
        role = self._role_obj(role)
        if role is None:
            raise TAFError(f"Role {role} does not exist")
        return role.paths

    def get_role_from_target_paths(self, target_paths):
        """
        Find a common role that can be used to sign given target paths.

        NOTE: Currently each target has only one mapped role.
        """
        targets_roles = self.map_signing_roles(target_paths)
        roles = list(targets_roles.values())

        try:
            # all target files should have at least one common role
            common_role = reduce(
                set.intersection,
                [set([r]) if isinstance(r, str) else set(r) for r in roles],
            )
        except TypeError:
            return None

        if not common_role:
            return None

        return common_role.pop()

    def get_signable_metadata(self, role):
        """Return signable portion of newly generate metadata for given role.

        Args:
        - role(str): TUF role (root, targets, timestamp, snapshot or delegated one)

        Returns:
        A string representing the 'object' encoded in canonical JSON form or None

        Raises:
        None
        """
        signed = self._signed_obj(role)
        return signed.to_dict()

    def get_signed_target_files(self) -> Set[str]:
        """Return all target files signed by all roles.

        Args:
        - None

        Returns:
        - Set of all target paths relative to targets directory
        """
        all_roles = self.get_all_targets_roles()
        return self.get_singed_target_files_of_roles(all_roles)

    def get_singed_target_files_of_roles(self, roles: Optional[List]=None) -> Set[str]:
        """Return all target files signed by the specified roles

        Args:
        - roles whose target files will be returned

        Returns:
        - Set of paths of target files of a role relative to targets directory
        """
        if roles is None:
            roles = self.get_all_targets_roles()

        return set(
            reduce(
                operator.iconcat,
                [self._signed_obj(role).targets.keys()  for role in roles],
                [],
            )
        )

    def get_signed_targets_with_custom_data(self, roles: Optional[List[str]]=None) -> Dict[str, Dict]:
        """Return all target files signed by the specified roles and and their custom data
        as specified in the metadata files

        Args:
        - roles whose target files will be returned

        Returns:
        - A dictionary whose keys are paths target files relative to the targets directory
        and values are custom data dictionaries.
        """
        if roles is None:
            roles = self.get_all_targets_roles()
        target_files = {}
        try:
            for role in roles:
                roles_targets = self.get_targets_of_role(role)
                for target_path, target_file in roles_targets.items():
                    target_files.setdefault(target_path, {}).update(target_file.custom or {})
        except StorageError:
            pass
        return target_files

    def get_target_file_custom_data(self, target_path: str) -> Optional[Dict]:
        """
        Return a custom data of a given target.
        """
        try:
            role = self.get_role_from_target_paths([target_path])
            return self.get_targets_of_role(role)[target_path].custom
        except KeyError:
            raise TAFError(f"Target {target_path} does not exist")

    def get_target_file_hashes(self, target_path, hash_func=HASH_FUNCTION):
        """
        Return hashes of a given target path.
        """
        try:
            role = self.get_role_from_target_paths([target_path])
            hashes = self.get_targets_of_role(role)[target_path].hashes
            if hash_func not in hashes:
                raise TAFError(f"Invalid hashing algorithm {hash_func}")
            return hashes[hash_func]
        except KeyError:
            raise TAFError(f"Target {target_path} does not exist")

    def get_key_length_and_scheme_from_metadata(self, parent_role, keyid):
        try:
            metadata = json.loads(
                Path(
                    self.path, METADATA_DIRECTORY_NAME, f"{parent_role}.json"
                ).read_text()
            )
            metadata = metadata["signed"]
            if "delegations" in metadata:
                metadata = metadata["delegations"]
            scheme = metadata["keys"][keyid]["scheme"]
            pub_key_pem = metadata["keys"][keyid]["keyval"]["public"]
            pub_key = serialization.load_pem_public_key(
                pub_key_pem.encode(), backend=default_backend()
            )
            return pub_key, scheme
        except Exception:
            return None, None

    def generate_roles_description(self) -> Dict:
        roles_description = {}

        def _get_delegations(role_name):
            delegations_info = {}
            for delegation in self.get_delegations_of_role(role_name):
                delegated_role = self._role_obj(delegation)
                delegations_info[delegation] = {
                    "threshold": delegated_role.threshold,
                    "number": len(delegated_role.keyids),
                    "paths": delegated_role.paths,
                    "terminating": delegated_role.terminating,
                }
                pub_key, scheme = self.get_key_length_and_scheme_from_metadata(
                    role_name, delegated_role.keyids[0]
                )

                delegations_info[delegation]["scheme"] = scheme
                delegations_info[delegation]["length"] = pub_key.key_size
                delegated_signed = self._signed_obj(delegation)
                if delegated_signed.delegations:
                    inner_roles_data = _get_delegations(delegation)
                    if len(inner_roles_data):
                        delegations_info[delegation][
                            "delegations"
                        ] = inner_roles_data
            return delegations_info

        for role_name in MAIN_ROLES:
            role_obj = self._role_obj(role_name)
            roles_description[role_name] = {
                "threshold": role_obj.threshold,
                "number": len(role_obj.keyids),
            }
            pub_key, scheme = self.get_key_length_and_scheme_from_metadata(
                "root", role_obj.keyids[0]
            )
            roles_description[role_name]["scheme"] = scheme
            roles_description[role_name]["length"] = pub_key.key_size
            if role_name == "targets":
                targets_signed = self._signed_obj(role_name)
                if targets_signed.delegations:
                    delegations_info = _get_delegations(role_name)
                    if len(delegations_info):
                        roles_description[role_name]["delegations"] = delegations_info
        return {"roles": roles_description}

    def get_role_keys(self, role, parent_role=None):
        """Get keyids of the given role

        Args:
        - role(str): TUF role (root, targets, timestamp, snapshot or delegated one)
        - parent_role(str): Name of the parent role of the delegated role. If not specified,
                            it will be set automatically, but this might be slow if there
                            are many delegations.

        Returns:
        List of the role's keyids (i.e., keyids of the keys).

        Raises:
        - securesystemslib.exceptions.FormatError: If the arguments are improperly formatted.
        - securesystemslib.exceptions.UnknownRoleError: If 'rolename' has not been delegated by this
                                                        targets object.
        """
        role_obj = self._role_obj(role)
        if role_obj is None:
            return None
        try:
            return role_obj.keyids
        except KeyError:
            pass
        return self.get_delegated_role_property("keyids", role, parent_role)

    def is_valid_metadata_key(self, role: str, key: Union[SSlibKey, str], scheme=DEFAULT_RSA_SIGNATURE_SCHEME) -> bool:
        """Checks if metadata role contains key id of provided key.

        Args:
        - role(str): TUF role (root, targets, timestamp, snapshot or delegated one)
        - key(securesystemslib.formats.RSAKEY_SCHEMA): Role's key.

        Returns:
        Boolean. True if key id is in metadata role key ids, False otherwise.

        Raises:
        - TAFError if key is not valid
        """

        try:
            if isinstance(key, str):
                # mypy will complain if we redefine key
                ssl_lib_key = get_sslib_key_from_value(key, scheme)
            else:
                ssl_lib_key = key
            key_id = _get_legacy_keyid(ssl_lib_key)
        except Exception as e:
            # TODO log
            raise TAFError("Invalid public key specified")
        else:
            return key_id in self.get_keyids_of_role(role)


    def is_valid_metadata_yubikey(self, role, public_key=None):
        """Checks if metadata role contains key id from YubiKey.

        Args:
        - role(str): TUF role (root, targets, timestamp, snapshot or delegated one
        - public_key(securesystemslib.formats.RSAKEY_SCHEMA): RSA public key dict

        Returns:
        Boolean. True if smart card key id belongs to metadata role key ids

        Raises:
        - YubikeyError
        """

        if public_key is None:
            public_key = yk.get_piv_public_key_tuf()

        return self.is_valid_metadata_key(role, public_key)

    def _load_signers(self, role: str, signers: List):
        """Verify that the signers can be used to sign the specified role and
        add them to the signer cache

        Args:
        - role(str): TUF role (root, targets, timestamp, snapshot or delegated one)
        - signers: A list of signers

        Returns:
        None

        Raises:
        - InvalidKeyError: If metadata cannot be signed with given key.
        """

        for signer in signers:
            key = signer.public_key
            if not self.is_valid_metadata_key(role, key):
                raise InvalidKeyError(role)
            self.signer_cache[role][key.keyid] = signer

    def map_signing_roles(self, target_filenames):
        """
        For each target file, find delegated role responsible for that target file based
        on the delegated paths. The most specific role (meaning most deeply nested) whose
        delegation path matches the target's path is returned as that file's matching role.
        If there are no delegated roles with a path that matches the target file's path,
        'targets' role will be returned as that file's matching role. Delegation path
        is expected to be relative to the targets directory. It can be defined as a glob
        pattern.
        """

        roles = ["targets"]
        roles_targets = {
            target_filename: "targets" for target_filename in target_filenames
        }
        while roles:
            role = roles.pop()
            path_patterns = self.get_role_paths(role)
            for path_pattern in path_patterns:
                for target_filename in target_filenames:
                    if fnmatch(
                        target_filename.lstrip(os.sep),
                        path_pattern.lstrip(os.sep),
                    ):
                        roles_targets[target_filename] = role

            for delegation in self.get_delegations_of_role(role):
                roles.append(delegation)

        return roles_targets

    def modify_targets(self, added_data=None, removed_data=None):
        """Creates a target.json file containing a repository's commit for each repository.
        Adds those files to the tuf repository.

        Args:
        - added_data(dict): Dictionary of new data whose keys are target paths of repositories
                            (as specified in targets.json, relative to the targets dictionary).
                            The values are of form:
                            {
                                target: content of the target file
                                custom: {
                                    custom_field1: custom_value1,
                                    custom_field2: custom_value2
                                }
                            }
        - removed_data(dict): Dictionary of the old data whose keys are target paths of
                              repositories
                              (as specified in targets.json, relative to the targets dictionary).
                              The values are not needed. This is just for consistency.

        Content of the target file can be a dictionary, in which case a json file will be created.
        If that is not the case, an ordinary textual file will be created.
        If content is not specified and the file already exists, it will not be modified.
        If it does not exist, an empty file will be created. To replace an existing file with an
        empty file, specify empty content (target: '')

        Custom is an optional property which, if present, will be used to specify a TUF target's

        Returns:
        - Role name used to update given targets
        """
        added_data = {} if added_data is None else added_data
        removed_data = {} if removed_data is None else removed_data
        data = dict(added_data, **removed_data)
        if not data:
            raise TargetsError("Nothing to be modified!")

        target_paths = list(data.keys())
        targets_role = self.get_role_from_target_paths(data)
        if targets_role is None:
            raise TargetsError(
                f"Could not find a common role for target paths:\n{'-'.join(target_paths)}"
            )
        # add new target files
        target_files = []
        for path, target_data in added_data.items():
            target_path = (self.targets_path / path).absolute()
            self._create_target_file(target_path, target_data)
            target_file = TargetFile.from_file(
                target_file_path=path,
                local_path=str(target_path),
                hash_algorithms=["sha256", "sha512"],
            )
            custom = target_data.get("custom", None)
            if custom:
                unrecognized_fields = {
                    "custom": custom
                }
                target_file.unrecognized_fields=unrecognized_fields
            target_files.append(target_file)

        # remove existing target files
        removed_paths = []
        for path in removed_data.keys():
            target_path = (self.targets_path / path).absolute()
            if target_path.exists():
                if target_path.is_file():
                    target_path.unlink()
                elif target_path.is_dir():
                    shutil.rmtree(target_path, onerror=on_rm_error)
            removed_paths.append(str(path))


        targets_role = self._modify_tarets_role(target_files, removed_paths, targets_role)
        return targets_role

    def _modify_tarets_role(
            self,
            added_target_files: List[TargetFile],
            removed_paths: List[str],
            role_name: Optional[str]=Targets.type) -> None:
        """Add target files to top-level targets metadata."""
        with self.edit_targets(rolename=role_name) as targets:
            for target_file in added_target_files:
                targets.targets[target_file.path] = target_file
            for path in removed_paths:
                targets.targets.pop(path, None)
        return targets

    def revoke_metadata_key(self, roles_signers: Dict[str, Signer], roles: List[str], key_id: str):
        """Remove metadata key of the provided role.

        Args:
        - role(str): TUF role (root, targets, timestamp, snapshot or delegated one)
        - key_id(str): An object conformant to 'securesystemslib.formats.KEYID_SCHEMA'.

        Returns:
            removed_from_roles, not_added_roles, less_than_threshold_roles
        """

        removed_from_roles = []
        not_added_roles = []
        less_than_threshold_roles = []

        def _check_if_can_remove(key_id, role):
            role_obj = self._role_obj(role)
            if len(role_obj.keyids) - 1 < role_obj.threshold:
                less_than_threshold_roles.append(role)
                return False
            if key_id not in self.get_keyids_of_role(role):
                not_added_roles.append(role)
                return False
            return True

        main_roles = [role for role in roles if role in MAIN_ROLES and _check_if_can_remove(key_id, role)]
        if len(main_roles):
            with self.edit_root() as root:
                for role in main_roles:
                    root.revoke_key(keyid=key_id, role=role)
                    removed_from_roles.append(role)

        roles_by_parents = defaultdict(list)
        delegated_roles = [role for role in roles if role not in MAIN_ROLES and _check_if_can_remove(key_id, role)]
        if len(delegated_roles):
            for role in delegated_roles:
                parent = self.find_delegated_roles_parent(role)
                roles_by_parents[parent].append(role)

            for parent, roles_of_parent in roles_by_parents.items():
                with self.edit(parent) as parent_role:
                    for role in roles_of_parent:
                        parent_role.revoke_key(keyid=key_id, role=role)
                        removed_from_roles.append(role)

        if removed_from_roles:
            for role, signers in roles_signers.items():
                for signer in signers:
                    key = signer.public_key
                    self.signer_cache[role][key.keyid] = signer

            # Make sure the targets role gets signed with its new key, even though
            # it wasn't updated itself.
            if "targets" in removed_from_roles and "targets" not in roles_by_parents:
                with self.edit_targets():
                    pass
            # TODO should this be done, what about other roles? Do we want that?

            self.do_snapshot()
            self.do_timestamp()

        return removed_from_roles, not_added_roles, less_than_threshold_roles

    def roles_targets_for_filenames(self, target_filenames):
        """Sort target files by roles
        Args:
        - target_filenames: List of relative paths of target files
        Returns:
        - A dictionary mapping roles to a list of target files belonging
          to the provided target_filenames list delegated to the role
        """
        targets_roles_mapping = self.map_signing_roles(target_filenames)
        roles_targets_mapping = {}
        for target_filename, role_name in targets_roles_mapping.items():
            roles_targets_mapping.setdefault(role_name, []).append(target_filename)
        return roles_targets_mapping

    def _role_obj(self, role, parent=None):
        if role in MAIN_ROLES:
            md = self.open("root")
            try:
                data = md.to_dict()["signed"]["roles"][role]
                return Role.from_dict(data)
            except (KeyError, ValueError):
                raise TAFError("root.json is invalid")
        else:
            parent_name = self.find_delegated_roles_parent(role, parent)
            if parent_name is None:
                return None
            md = self.open(parent_name)
            delegations_data = md.to_dict()["signed"]["delegations"]["roles"]
            for delegation in delegations_data:
                if delegation["name"] == role:
                    try:
                        return DelegatedRole.from_dict(delegation)
                    except (KeyError, ValueError):
                        raise TAFError(f"{delegation}.json is invalid")
            return None

    def _signed_obj(self, role):
        md = self.open(role)
        try:
            signed_data = md.to_dict()["signed"]
            role_to_role_class = {
                "root": Root,
                "targets": Targets,
                "snapshot": Snapshot,
                "timestamp": Timestamp
            }
            role_class =  role_to_role_class.get(role, Targets)
            return role_class.from_dict(signed_data)
        except (KeyError, ValueError):
            raise TAFError(f"Invalid metadata file {role}.json")

    def _set_default_expiration_date(self, signed):
        interval = self.expiration_intervals[signed.type]
        start_date = datetime.now(timezone.utc)
        expiration_date = start_date + timedelta(interval)
        signed.expires = expiration_date

    def set_metadata_expiration_date(self, role_name: str, signers=List[CryptoSigner], start_date: datetime=None, interval: int=None) -> None:
        """Set expiration date of the provided role.

        Args:
        - role(str): TUF role (root, targets, timestamp, snapshot or delegated one)
        - start_date(datetime): Date to which the specified interval is added when calculating
                                expiration date. If a value is not provided, it is set to the
                                current time.
        - signers(List[CryptoSigner]): a list of signers
        - interval(int): A number of days added to the start date.
                        If not provided, the default value is set based on the role:

                            root - 365 days
                            targets - 90 days
                            snapshot - 7 days
                            timestamp - 1 day
                            all other roles (delegations) - same as targets

        Returns:
        None

        Raises:
        - securesystemslib.exceptions.FormatError: If the arguments are improperly formatted.
        - securesystemslib.exceptions.UnknownRoleError: If 'rolename' has not been delegated by
                                                        this targets object.
        """
        self._load_signers(role_name, signers)
        with self.edit(role_name) as role:
            start_date = datetime.now(timezone.utc)
            if interval is None:
                try:
                    interval = self.expiration_intervals[role_name]
                except KeyError:
                    interval = self.expiration_intervals["targets"]
            expiration_date = start_date + timedelta(interval)
            role.expires = expiration_date


    def sort_roles_targets_for_filenames(self):
        rel_paths = []
        for filepath in self.targets_path.rglob("*"):
            if filepath.is_file():
                file_rel_path = str(
                    Path(filepath).relative_to(self.targets_path).as_posix()
                )
                rel_paths.append(file_rel_path)

        files_to_roles = self.map_signing_roles(rel_paths)
        roles_targets = {}
        for target_file, role in files_to_roles.items():
            roles_targets.setdefault(role, []).append(target_file)
        return roles_targets

    def update_role(self, role_name: str, signers: List[CryptoSigner]):
        self._load_signers(role_name, signers)
        with self.eidt(role_name) as role:
            pass

    def update_snapshot_and_tiemstamp(self, signers_dict: Dict[str, List[CryptoSigner]]):
        self.update_role(Snapshot.type, signers_dict[Snapshot.type])
        self.update_role(Timestamp.type, signers_dict[Timestamp.type])

